"""Common one-line renderer shared by every display surface.

    <model> | CTX 62% | 5h 41% | 7d 18%

Markers: ``!`` warning, ``~`` estimate, ``--[reset]`` window reset with no new
observation yet, ``[old 12m]`` stale quota observation.
"""
from __future__ import annotations

import math
import sys
TYPE_CHECKING = False  # avoid importing typing at runtime (~20ms on uncached 3.9 stdlib)
if TYPE_CHECKING:
    from typing import Any, Dict, List, Optional, Tuple

from . import common

_SEP = " | "

# style -> {tone: (on, off)}. Context warnings are yellow, quota warnings red.
_STYLES = {
    "ansi": {"ctx": ("\x1b[1;33m", "\x1b[0m"), "quota": ("\x1b[1;31m", "\x1b[0m"),
             "dim": ("\x1b[2m", "\x1b[0m")},
    "tmux": {"ctx": ("#[fg=yellow,bold]", "#[default]"), "quota": ("#[fg=red,bold]", "#[default]"),
             "dim": ("#[dim]", "#[default]")},
    "plain": {"ctx": ("", ""), "quota": ("", ""), "dim": ("", "")},
}

_BUBBLE = {"nerd": "\U000f0361", "unicode": "(!)"}  # nerd: md-message_alert


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return "%dm" % max(1, seconds // 60)
    if seconds < 86400:
        h, rem = divmod(seconds, 3600)
        return "%dh%02dm" % (h, rem // 60)
    d, rem = divmod(seconds, 86400)
    return "%dd%dh" % (d, rem // 3600)


def _fmt_days(seconds: float) -> str:
    """Coarse reset countdown for weekly windows: whole days, hours under a day."""
    seconds = max(0, int(seconds))
    if seconds >= 86400:
        return "%dd" % (seconds // 86400)
    return "%dh" % max(1, seconds // 3600)


def _fmt_tokens(n: Optional[int]) -> str:
    if n is None:
        return "?"
    if n >= 1_000_000:
        return "%.1fM" % (n / 1_000_000.0)
    if n >= 1000:
        return "%dk" % round(n / 1000.0)
    return str(n)


def _pct(v: float) -> str:
    # floor, so a value just under a threshold never displays as the threshold
    # itself without its warning marker (49.9 -> "49%", not "50%").
    return "%d%%" % int(math.floor(max(0.0, min(100.0, v))))


class _Seg:
    """One segment: text plus whether it is a warning / dim, and drop priority."""

    def __init__(self, text: str, warn: bool = False, dim: bool = False, drop: int = 0,
                 tone: str = "ctx", bar: Optional[float] = None, suffix: str = ""):
        self.text = text          # "CTX 62%!"
        self.bar = bar            # percent for an optional horizontal bar, or None
        self.suffix = suffix      # " 41m", " [old 12m]" ... (after the bar)
        self.warn = warn
        self.tone = tone  # "ctx" (yellow) or "quota" (red) when warn
        self.dim = dim
        self.drop = drop  # higher = dropped first when narrow; 0 = never drop


_EFFORT_ABBR = {"low": "low", "medium": "med", "med": "med", "high": "high",
                "xhigh": "xhigh", "extra high": "xhigh", "max": "max", "minimal": "min",
                "ultra": "ultra", "persistent": "pers"}


def short_model(model: Any, effort: Any = None) -> str:
    """Compact "family + effort" label, e.g.

    "Opus 5.5" + high          -> "Opus 5.5·high"
    "gpt-6.1-sol" + medium     -> "6.1-sol·med"
    "Gemini 4 Hydrogen (High)" -> "Hydrogen·high"
    "Claude Sonnet 5.5 (Low)"  -> "Sonnet 5.5·low"
    """
    name = str(model or "").strip()
    eff = str(effort or "").strip().lower()
    # trailing "(Effort)" in display names
    if name.endswith(")") and "(" in name:
        head, _, tail = name.rpartition("(")
        tail = tail[:-1].strip().lower()
        if tail in _EFFORT_ABBR:
            name = head.strip()
            eff = eff or tail
    low = name.lower()
    for prefix in ("gpt-", "claude-", "claude "):
        if low.startswith(prefix):
            name = name[len(prefix):]
            low = name.lower()
            break
    if low.startswith("gemini "):
        parts = name.split()
        # "Gemini 4 Hydrogen" -> "Hydrogen"; "Gemini 3.8 Flash" -> "Flash"
        rest = [w for w in parts[1:] if not w.replace(".", "").isdigit()]
        if rest:
            name = " ".join(rest)
    name = name[:16]
    abbr = _EFFORT_ABBR.get(eff, eff[:5]) if eff else ""
    return name + ("\u00b7" + abbr if abbr else "")


def _ctx_segment(cache: Dict[str, Any], cfg: Dict[str, Any], now: int) -> _Seg:
    ctx = cache.get("ctx") or {}
    source = ctx.get("source") or "none"
    used = common.as_float(ctx.get("used_pct"))
    tokens = common.as_int(ctx.get("tokens"))
    window = common.as_int(ctx.get("window"))

    if source == "mapped" or (used is None and tokens is not None):
        text = "CTX ~%s/%s" % (_fmt_tokens(tokens), _fmt_tokens(window) if source != "mapped" else "?")
        warn = False
    elif used is None:
        text, warn = "CTX ?", False
    else:
        prefix = "~" if source == "observed_w" else ""
        warn = used >= float(cfg["ctx_warn"])
        text = "CTX %s%s%s" % (prefix, _pct(used), "!" if warn else "")

    bar = used if (used is not None and source != "mapped") else None
    observed = common.as_int(cache.get("observed_at"))
    dim = False
    suffix = ""
    if observed is not None and now - observed > int(cfg["ctx_age_sec"]):
        suffix = " [%s]" % _fmt_duration(now - observed)
        dim = not warn
    return _Seg(text, warn=warn, dim=dim, drop=0, bar=bar, suffix=suffix)


def _quota_segment(label: str, win: Optional[Dict[str, Any]], cfg: Dict[str, Any],
                   now: int, drop: int, coarse: bool = False,
                   with_bar: bool = False) -> Optional[_Seg]:
    if not isinstance(win, dict):
        return None
    used = common.as_float(win.get("used_pct"))
    resets = common.as_int(win.get("resets_at"))
    observed = common.as_int(win.get("observed_at"))

    if resets is not None and resets <= now:
        return _Seg("%s --[reset]" % label, dim=True, drop=drop)
    if used is None:
        return _Seg("%s ?" % label, dim=True, drop=drop)

    warn = used >= float(cfg["quota_warn"])
    text = "%s %s" % (label, _pct(used))
    suffix = ""
    if warn:
        text += "!"
        if resets is not None:
            suffix += " " + (_fmt_days(resets - now) if coarse else _fmt_duration(resets - now))
    dim = False
    if observed is not None and now - observed > int(cfg["quota_old_sec"]):
        suffix += " [old %s]" % _fmt_duration(now - observed)
        dim = not warn
    return _Seg(text, warn=warn, dim=dim, drop=0 if warn else drop, tone="quota",
                bar=used if with_bar else None, suffix=suffix)


def pick_quota(tool: str, group: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Newest quota block among all cached sessions of a tool (no max-merging).

    Quota is account-wide, so the most recent observation from any session is
    the best estimate for every session. Each window is chosen independently
    by its own observed_at; ties keep the first seen. When ``group`` is given
    (e.g. agy "gemini" vs "3p"), only sessions of that quota group count.
    Returns None when no session has quota data.
    """
    best: Dict[str, Tuple[int, Dict[str, Any]]] = {}
    out_group = group
    for entry in common.iter_cache(tool):
        quota = entry.get("quota")
        if not isinstance(quota, dict):
            continue
        if group is not None and quota.get("group") != group:
            continue
        for key in ("five_hour", "weekly"):
            win = quota.get(key)
            if not isinstance(win, dict):
                continue
            obs = common.as_int(win.get("observed_at")) or common.as_int(entry.get("observed_at")) or 0
            if key not in best or obs > best[key][0]:
                best[key] = (obs, win)
                out_group = quota.get("group", out_group)
    if not best:
        return None
    out: Dict[str, Any] = {"group": out_group, "five_hour": None, "weekly": None}
    for key, (_, win) in best.items():
        out[key] = win
    return out


def render_line(cache: Dict[str, Any], quota: Optional[Dict[str, Any]] = None,
                width: Optional[int] = None, style: str = "ansi",
                now: Optional[int] = None) -> str:
    """Render one status line. Never raises."""
    try:
        return _render(cache, quota, width, style, now)
    except Exception as exc:
        common.log_error("render", exc)
        return "CTX ?"


def _render(cache: Dict[str, Any], quota: Optional[Dict[str, Any]], width: Optional[int],
            style: str, now: Optional[int]) -> str:
    cfg = common.load_config()
    now = common.now() if now is None else now
    if quota is None:
        quota = cache.get("quota") if isinstance(cache.get("quota"), dict) else None

    segs: List[_Seg] = []
    model = cache.get("model")
    if model:
        segs.append(_Seg(short_model(model, cache.get("effort")), drop=3))
    segs.append(_ctx_segment(cache, cfg, now))
    if quota:
        for label, key, drop in (("5h", "five_hour", 1), ("7d", "weekly", 2)):
            seg = _quota_segment(label, quota.get(key), cfg, now, drop, coarse=(key == "weekly"),
                                 with_bar=(key == "five_hour"))
            if seg:
                segs.append(seg)

    bar_cells = int(cfg.get("bar_width") or 0) if cfg.get("bars", True) else 0
    if bar_cells < 1:
        bar_cells = 0

    if style not in _STYLES:
        style = "plain"
    if not cfg.get("color", True):
        style = "plain"
    tones = _STYLES[style]

    def paint(text: str, tone: str) -> str:
        on, off = tones[tone]
        return on + text + off if on else text

    def esc(text: str) -> str:
        return text.replace("#", "##") if style == "tmux" else text

    # Icon + warning bubble consider every segment, whatever the layout.
    head = ""
    prefix = _prefix(cache.get("tool"), segs, cfg)
    if prefix:
        icon, bubble_tone = prefix
        head = _color_icon(esc(icon[0]), cache.get("tool"), cfg, style) if icon[0] else ""
        if icon[1]:
            head += paint(esc(icon[1]), bubble_tone)
        head += " "
    head_w = _visible_width(cache.get("tool"), segs, cfg)

    def assemble(group: List[_Seg], avail: Optional[int]) -> str:
        group = list(group)
        cells = bar_cells

        def plain_len(c: int) -> int:
            return len(_SEP.join(_seg_plain(x, c) for x in group))

        if avail:
            if cells and plain_len(cells) > avail:
                cells = 0  # bars are the first thing to go when narrow
            while plain_len(cells) > avail:
                droppable = [x for x in group if x.drop > 0]
                if not droppable:
                    break
                group.remove(max(droppable, key=lambda x: x.drop))
        parts = []
        for x in group:
            tone = x.tone if x.warn else ("dim" if x.dim else None)
            text = esc(x.text)
            if tone:
                text = paint(text, tone)
            if cells and x.bar is not None:
                fill, track = _bar(x.bar, cells)
                if x.warn:  # color only when warning; otherwise terminal default
                    fill = paint(fill, x.tone)
                track_color = str(cfg.get("bar_track_color") or "")
                track = _fg(track, track_color, style) if track_color else paint(track, "dim")
                text += " " + fill + track
            if x.suffix:
                suffix = esc(x.suffix)
                text += paint(suffix, tone) if tone else suffix
            parts.append(text)
        return _SEP.join(parts)

    avail = (width - head_w) if width else None
    two_line = str(cfg.get("layout", "one-line")) == "two-line" and style != "tmux"
    quota_segs = [x for x in segs if x.tone == "quota"]
    if two_line and quota_segs:
        first = [x for x in segs if x.tone != "quota"]
        line1 = head + assemble(first, avail)
        line2 = " " * head_w + assemble(quota_segs, avail)
        return line1 + "\n" + line2
    return head + assemble(segs, avail)


def _visible_width(tool: Any, segs: List[_Seg], cfg: Dict[str, Any]) -> int:
    """Approximate cell width of the icon + bubble prefix (incl. trailing space)."""
    pre = _prefix(tool, segs, cfg)
    if not pre:
        return 0
    (icon, bubble), _ = pre
    return len(icon) + len(bubble) + 1


_LINE = "\u2501"  # heavy horizontal line


def _bar(pct: float, cells: int) -> Tuple[str, str]:
    """Heavy-line bar: (filled part, empty track), total width == cells.

    Floors like the percentage text, so the bar never looks fuller than the
    number next to it.
    """
    pct = max(0.0, min(100.0, pct))
    filled = int(math.floor(pct / 100.0 * cells))
    return _LINE * filled, _LINE * (cells - filled)


def _fg(text: str, color: str, style: str) -> str:
    """Wrap text in a truecolor "#rrggbb" foreground; no-op if color is invalid."""
    if not text or style == "plain" or len(color) != 7 or not color.startswith("#"):
        return text
    try:
        r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
    except ValueError:
        return text
    if style == "tmux":
        return "#[fg=%s]%s#[default]" % (color, text)
    return "\x1b[38;2;%d;%d;%dm%s\x1b[0m" % (r, g, b, text)


def _seg_plain(s: "_Seg", bar_cells: int) -> str:
    out = s.text
    if bar_cells and s.bar is not None:
        out += " " + "x" * bar_cells
    return out + s.suffix


def _color_icon(text: str, tool: Any, cfg: Dict[str, Any], style: str) -> str:
    colors = cfg.get("icon_colors") or {}
    color = str(colors.get(tool, "")) if isinstance(colors, dict) else ""
    return _fg(text, color, style)


def _prefix(tool: Any, segs: List[_Seg], cfg: Dict[str, Any]) -> Optional[Tuple[Tuple[str, str], str]]:
    """Tool icon, plus a warning bubble when any segment warns.

    The bubble is red if any quota warns (more urgent), otherwise yellow.
    """
    icons = cfg.get("icons") or {}
    icon = str(icons.get(tool, "")) if isinstance(icons, dict) else ""
    warns = [s for s in segs if s.warn]
    bubble = ""
    tone = "ctx"
    if warns:
        bubble = _BUBBLE.get(str(cfg.get("icons_style")), _BUBBLE["unicode"])
        tone = "quota" if any(s.tone == "quota" for s in warns) else "ctx"
    if not icon and not bubble:
        return None
    return (icon, bubble), tone


def main(argv: List[str]) -> int:
    """`ctxhud list`: one line per cached session (debug view)."""
    now = common.now()
    rows = sorted(common.iter_cache(), key=lambda e: (e.get("tool", ""), -(e.get("observed_at") or 0)))
    if not rows:
        sys.stdout.write("no cached sessions in %s\n" % common.state_dir())
        return 0
    style = "ansi" if sys.stdout.isatty() else "plain"
    for e in rows:
        alive = common.pid_alive(e.get("pid"))
        state = {True: "alive", False: "dead", None: "?"}[alive]
        own = e.get("quota") if isinstance(e.get("quota"), dict) else {}
        quota = pick_quota(e["tool"], own.get("group"))
        label = "%-6s %-8s %-5s" % (e["tool"], str(e.get("session_id", ""))[:8], state)
        sys.stdout.write("%s  %s\n" % (label, render_line(e, quota, style=style, now=now)))
    return 0
