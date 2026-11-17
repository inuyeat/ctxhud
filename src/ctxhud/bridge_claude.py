"""Claude Code statusLine bridge.

Reads the JSON that Claude Code passes to a statusLine command on stdin, stores a
normalized per-session cache entry (schema=1) and prints one rendered line.

Design rules (see SPEC 1, 5):
* Never fails: every error is swallowed (logged to the ctxhud log file), the exit
  status is always 0 and nothing is written to stderr.
* Missing or null numbers stay ``None``; they are never replaced by 0.
* Quota windows are stored exactly as the tool reported them (no merging across
  sessions). A window that disappears from the input (Claude Code drops a window
  once its ``resets_at`` has passed) is carried forward unchanged so the renderer
  can show it as reset instead of silently treating it as unused.
* No network access, no credentials, no Keychain.
"""
from __future__ import annotations

import importlib
import os
import sys

from ctxhud import common

# Annotations are never evaluated (``from __future__ import annotations``), so typing is only
# imported for type checkers. This keeps the hot path free of the (slow, uncached on some
# interpreters) ``typing`` and ``re`` imports.
TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any, Dict, Optional

TOOL = "claude"
SCHEMA_GROUP = "claude"

# Control characters (C0, DEL, C1) are removed from every string taken from the input.
_CONTROL_TABLE = {code: None for code in list(range(0x20)) + list(range(0x7F, 0xA0))}
_MAX_NAME = 64
_MAX_PATH = 512


# ---------------------------------------------------------------- helpers

def _clean_text(value: Any, limit: int = _MAX_NAME) -> Optional[str]:
    """Strip control characters (terminal escape injection) and cap the length."""
    if not isinstance(value, str):
        return None
    text = value.translate(_CONTROL_TABLE).strip()
    return text[:limit] or None


def _pct(value: Any) -> Optional[float]:
    """Percent used in [0, 100], or None when absent/invalid."""
    number = common.as_float(value)
    if number is None or number < 0:
        return None
    return min(number, 100.0)


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


# Reasoning-effort levels Claude Code reports in ``effort.level``. Anything else (a new level,
# a wrong type, escape sequences) is stored as None instead of being passed to the renderer.
_EFFORT_LEVELS = frozenset(("low", "medium", "high", "xhigh", "max"))


def _effort(value: Any) -> Optional[str]:
    level = _dict(value).get("level")
    if not isinstance(level, str):
        return None
    level = level.strip().lower()
    return level if level in _EFFORT_LEVELS else None


# ---------------------------------------------------------------- normalization

def _normalize_ctx(context_window: Any) -> Dict[str, Any]:
    cw = _dict(context_window)
    used = _pct(cw.get("used_percentage"))
    window = common.as_int(cw.get("context_window_size"))
    if window is not None and window <= 0:
        window = None
    tokens = common.as_int(cw.get("total_input_tokens"))
    if tokens is not None and tokens < 0:
        tokens = None
    if tokens == 0 and used is None:
        tokens = None  # nothing measured yet (Claude sends 0 / null before the first reply)

    source = "native" if used is not None else "none"
    formula = "claude.statusline.used_percentage"
    if used is None and tokens and window:
        # The tool gave us both numerator and denominator but no percentage.
        used = min(100.0, tokens * 100.0 / window)
        source = "observed_w"
        formula = "total_input_tokens/context_window_size"
    return {"used_pct": used, "tokens": tokens, "window": window,
            "source": source, "formula": formula}


def _normalize_window(raw: Any) -> Optional[Dict[str, Any]]:
    window = _dict(raw)
    used = _pct(window.get("used_percentage"))
    if used is None:
        return None
    return {"used_pct": used, "resets_at": common.as_int(window.get("resets_at"))}


def _merge_window(new: Optional[Dict[str, Any]], old: Any, now_ts: int) -> Optional[Dict[str, Any]]:
    """Stamp ``observed_at`` for a window.

    Claude Code does not report when a quota value was measured, and a re-run of
    the status line command can re-send an old value. So ``observed_at`` is the
    time the value last *changed*; an unchanged value keeps its previous stamp
    and the renderer shows it as old once it ages.
    """
    previous = _dict(old)
    if new is None:
        # Field absent from the input: keep what we last knew, untouched.
        return dict(previous) if previous else None
    stamp = common.as_int(previous.get("observed_at"))
    unchanged = (previous.get("used_pct") == new["used_pct"]
                 and previous.get("resets_at") == new["resets_at"])
    new["observed_at"] = stamp if (unchanged and stamp is not None) else now_ts
    return new


def normalize(payload: Dict[str, Any], now_ts: Optional[int] = None, pid: Optional[int] = None,
              tmux_pane: Optional[str] = None, previous: Any = None) -> Dict[str, Any]:
    """Convert one Claude Code statusLine payload into a schema=1 cache entry.

    Pure function: ``previous`` is the last cache entry of the same session (or None).
    """
    now_ts = common.now() if now_ts is None else now_ts
    workspace = _dict(payload.get("workspace"))
    model = _dict(payload.get("model"))
    prev_quota = _dict(_dict(previous).get("quota"))

    cwd = _clean_text(payload.get("cwd"), _MAX_PATH) or _clean_text(workspace.get("current_dir"), _MAX_PATH)
    model_name = _clean_text(model.get("display_name")) or _clean_text(model.get("id"))

    limits = _dict(payload.get("rate_limits"))
    five = _merge_window(_normalize_window(limits.get("five_hour")), prev_quota.get("five_hour"), now_ts)
    weekly = _merge_window(_normalize_window(limits.get("seven_day")), prev_quota.get("weekly"), now_ts)
    quota = {"group": SCHEMA_GROUP, "five_hour": five, "weekly": weekly} if (five or weekly) else None

    session_id = payload.get("session_id")
    entry: Dict[str, Any] = {
        "schema": common.SCHEMA,
        "tool": TOOL,
        "session_id": _clean_text(session_id, 128),
        "pid": pid if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 else None,
        "cwd": cwd,
        "model": model_name,                       # the tool's own display name; the renderer shortens it
        "effort": _effort(payload.get("effort")),  # None when the model has no effort setting
        "tmux_pane": _clean_text(tmux_pane, 16),
        "ctx": _normalize_ctx(payload.get("context_window")),
        "quota": quota,
        "observed_at": now_ts,   # the context figures are recomputed by Claude on every call
        "received_at": now_ts,
    }
    return entry


# ---------------------------------------------------------------- output

def fallback_line(entry: Dict[str, Any]) -> str:
    """Minimal plain line used when the shared renderer is unavailable."""
    ctx = _dict(entry.get("ctx"))
    used = ctx.get("used_pct")
    shown = "--" if used is None else "%d%%" % int(round(used))
    name = entry.get("model") or "claude"
    return "%s | CTX %s" % (name, shown)


def _terminal_width() -> Optional[int]:
    width = common.as_int(os.environ.get("COLUMNS"))
    return width if width and width > 0 else None


def render_entry(entry: Dict[str, Any]) -> str:
    """Render via ctxhud.render; fall back to a minimal line on any problem."""
    try:
        render = importlib.import_module("ctxhud.render")
        quota = None
        try:
            quota = render.pick_quota(TOOL)
        except Exception as exc:
            common.log_error("bridge_claude.pick_quota", exc)
        style = "ansi" if common.load_config().get("color", True) else "plain"
        line = render.render_line(entry, quota=quota, width=_terminal_width(), style=style)
        if isinstance(line, str) and line.strip():
            return line
    except Exception as exc:
        common.log_error("bridge_claude.render", exc)
    return fallback_line(entry)


def _emit(line: str) -> None:
    text = line.replace("\r", " ").replace("\n", " ") + "\n"
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.write(text.encode("ascii", "replace").decode("ascii"))
    sys.stdout.flush()


# ---------------------------------------------------------------- main

def _run(state: Dict[str, bool]) -> None:
    payload = common.read_stdin_json()
    if payload is None:
        state["printed"] = True  # nothing usable on stdin: stay silent rather than guess
        return
    session_id = _clean_text(payload.get("session_id"), 128)

    previous = None
    if session_id:
        try:
            common.write_raw(TOOL, session_id, payload)
        except Exception as exc:
            common.log_error("bridge_claude.raw", exc)
        try:
            previous = common.read_json(common.cache_path(TOOL, session_id))
        except Exception as exc:  # e.g. state dir not creatable: still print a line
            common.log_error("bridge_claude.previous", exc)

    entry = normalize(payload, pid=os.getppid(), tmux_pane=os.environ.get("TMUX_PANE"),
                      previous=previous)
    if session_id:
        try:
            common.write_cache(entry)
        except Exception as exc:  # the line is still worth printing without a cache
            common.log_error("bridge_claude.cache", exc)
    line = render_entry(entry)
    state["printed"] = True
    _emit(line)


def main(argv: Optional[list] = None) -> int:
    """statusLine entry point. ``argv`` (e.g. ``["bridge", "claude"]``) is ignored."""
    state = {"printed": False}
    try:
        _run(state)
    except Exception as exc:
        common.log_error("bridge_claude", exc)
        if not state["printed"]:
            try:
                _emit("claude | CTX --")
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
