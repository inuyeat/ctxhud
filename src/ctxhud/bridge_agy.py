"""Antigravity CLI (`agy`) statusLine bridge.

Reads statusLine JSON from stdin, optionally persists a PII-stripped raw
payload when ``config.capture_raw`` is enabled, normalizes to schema=1 cache,
and writes one rendered status line to stdout. Never raises, never writes to
stderr, never touches network or auth files.
"""
from __future__ import annotations

import os
import sys

from ctxhud import common

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any, Dict, List, Optional

_CONTROL_TABLE = {code: None for code in list(range(0x20)) + list(range(0x7F, 0xA0))}
_MAX_NAME = 64
_MAX_PATH = 512

_RAW_ALLOWED_KEYS = (
    "agent_state",
    "context_window",
    "conversation_id",
    "cwd",
    "cycle_mode",
    "exceeds_200k_tokens",
    "model",
    "plan_tier",
    "product",
    "quota",
    "sandbox",
    "session_id",
    "terminal_width",
    "version",
    "workspace",
)


def _clean_text(value: Any, limit: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = value.translate(_CONTROL_TABLE).strip()
    if not cleaned:
        return None
    return cleaned[:limit]


def sanitize_raw_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only whitelisted non-PII fields (never store email or chat titles)."""
    return {k: payload[k] for k in _RAW_ALLOWED_KEYS if k in payload}


def _days_from_civil(y: int, m: int, d: int) -> int:
    # Howard Hinnant's civil-to-days algorithm (days since 1970-01-01).
    y -= 1 if m <= 2 else 0
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def parse_iso_timestamp(value: Any) -> Optional[int]:
    """Convert an ISO-8601 string or numeric epoch to Unix integer seconds."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        num = common.as_float(value)
        return int(num) if num is not None and num > 0 else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    # Fast zero-import path for standard UTC strings like "2026-11-17T18:00:16Z"
    if len(text) >= 20 and text[4] == "-" and text[7] == "-" and text[10] in ("T", " ") and text[13] == ":" and text[16] == ":":
        tail = text[19:]
        if tail.startswith("."):
            dot_end = 1
            while dot_end < len(tail) and tail[dot_end].isdigit():
                dot_end += 1
            tail = tail[dot_end:]
        if tail in ("Z", "+00:00", "-00:00"):
            try:
                yr, mo, dy = int(text[0:4]), int(text[5:7]), int(text[8:10])
                hr, mn, sc = int(text[11:13]), int(text[14:16]), int(text[17:19])
                if 1 <= mo <= 12 and 1 <= dy <= 31 and 0 <= hr <= 23 and 0 <= mn <= 59 and 0 <= sc <= 59:
                    return _days_from_civil(yr, mo, dy) * 86400 + hr * 3600 + mn * 60 + sc
            except ValueError:
                pass
    import datetime as _dt

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return int(parsed.timestamp())


def resolve_session_id(payload: Dict[str, Any], ppid: Optional[int] = None) -> str:
    """Pick a stable session identifier from stdin payload."""
    for key in ("conversation_id", "session_id"):
        val = _clean_text(payload.get(key), 128)
        if val:
            return val
    parent = os.getppid() if ppid is None else ppid
    return "agy-%d" % parent


def _extract_model_fields(payload: Dict[str, Any]) -> Dict[str, Optional[str]]:
    raw_model = payload.get("model")
    if isinstance(raw_model, dict):
        return {
            "display": _clean_text(raw_model.get("display_name"), _MAX_NAME),
            "id": _clean_text(raw_model.get("id"), _MAX_NAME),
        }
    text = _clean_text(raw_model, _MAX_NAME)
    if text:
        return {"display": text, "id": text}
    return {"display": None, "id": None}


def short_model_name(payload: Dict[str, Any]) -> Optional[str]:
    """Return the raw display model name untouched (renderer shortens it)."""
    fields = _extract_model_fields(payload)
    return fields["display"] or fields["id"]


def extract_effort(payload: Dict[str, Any]) -> Optional[str]:
    """Return stdin model.effort lowercased when present, else None."""
    raw_model = payload.get("model")
    if not isinstance(raw_model, dict):
        return None
    eff = _clean_text(raw_model.get("effort"), 16)
    return eff.lower() if eff else None


def detect_quota_group(payload: Dict[str, Any]) -> str:
    """Map current model.id / display_name to SPEC §5 quota group ('gemini' | '3p')."""
    fields = _extract_model_fields(payload)
    text = (fields["id"] or fields["display"] or "").lower()
    if "claude" in text or "gpt" in text or "oss" in text:
        return "3p"
    return "gemini"


def is_argon_model(payload: Dict[str, Any]) -> bool:
    """Return True when the active model is an Argon planner variant."""
    fields = _extract_model_fields(payload)
    text = (fields["id"] or fields["display"] or "").lower()
    return "argon" in text


def normalize_context(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize context_window fields into schema=1 ctx object."""
    empty = {
        "used_pct": None,
        "tokens": None,
        "window": None,
        "source": "none",
        "formula": "",
    }
    if payload.get("agent_state") == "authenticating":
        return empty
    cw = payload.get("context_window")
    if not isinstance(cw, dict):
        return empty

    window = common.as_int(cw.get("context_window_size"))
    if window is None or window <= 0:
        return empty

    used_pct = common.as_float(cw.get("used_percentage"))
    formula = ""
    source = "none"
    if used_pct is not None:
        used_pct = max(0.0, min(100.0, used_pct))
        source = "native"
        formula = "agy.context_window.used_percentage"
    else:
        rem_pct = common.as_float(cw.get("remaining_percentage"))
        if rem_pct is not None:
            used_pct = max(0.0, min(100.0, 100.0 - rem_pct))
            source = "native"
            formula = "100-agy.context_window.remaining_percentage"

    tokens: Optional[int] = None
    if used_pct is not None:
        tokens = int(round((used_pct / 100.0) * window))

    return {
        "used_pct": used_pct,
        "tokens": tokens,
        "window": window,
        "source": source,
        "formula": formula,
    }


def _normalize_quota_bucket(
    entry: Any,
    observed_at: int,
    prev_win: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    if not isinstance(entry, dict):
        return None
    used_pct: Optional[float] = None
    rem = common.as_float(entry.get("remaining_fraction"))
    if rem is not None:
        clamped = max(0.0, min(1.0, rem))
        used_pct = round((1.0 - clamped) * 100.0, 4)
    else:
        direct_used = common.as_float(entry.get("used_percentage"))
        if direct_used is not None:
            used_pct = max(0.0, min(100.0, direct_used))

    resets_at = parse_iso_timestamp(entry.get("reset_time"))
    if resets_at is None:
        reset_in = common.as_float(entry.get("reset_in_seconds"))
        if reset_in is not None and reset_in >= 0:
            resets_at = observed_at + int(reset_in)

    if used_pct is None and resets_at is None:
        return None

    win_observed = observed_at
    if (
        isinstance(prev_win, dict)
        and prev_win.get("used_pct") == used_pct
        and prev_win.get("resets_at") == resets_at
        and common.as_int(prev_win.get("observed_at")) is not None
    ):
        win_observed = int(prev_win["observed_at"])

    return {
        "used_pct": used_pct,
        "resets_at": resets_at,
        "observed_at": win_observed,
    }


def _pick_more_constrained(
    a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """Return whichever quota bucket has higher used_pct (hits limit sooner)."""
    if a is None:
        return b
    if b is None:
        return a
    a_used = common.as_float(a.get("used_pct"))
    b_used = common.as_float(b.get("used_pct"))
    if a_used is None:
        return b
    if b_used is None:
        return a
    return a if a_used >= b_used else b


def normalize_quota(
    payload: Dict[str, Any],
    group: str,
    observed_at: int,
    argon: bool = False,
    prev_quota: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Extract only the active SPEC §5 group ('gemini' | '3p') quota windows."""
    prev_matching = (
        prev_quota
        if isinstance(prev_quota, dict) and prev_quota.get("group") == group
        else None
    )
    prev_5h = prev_matching.get("five_hour") if prev_matching else None
    prev_wk = prev_matching.get("weekly") if prev_matching else None

    quota = payload.get("quota")
    if not isinstance(quota, dict) or not quota:
        return prev_matching

    if group == "3p":
        five_hour = _normalize_quota_bucket(quota.get("3p-5h"), observed_at, prev_5h)
        weekly = _normalize_quota_bucket(quota.get("3p-weekly"), observed_at, prev_wk)
    else:
        five_hour = _normalize_quota_bucket(quota.get("gemini-5h"), observed_at, prev_5h)
        gem_wk = _normalize_quota_bucket(quota.get("gemini-weekly"), observed_at, prev_wk)
        if argon:
            argon_wk = _normalize_quota_bucket(
                quota.get("gemini-argon-wtu-weekly"), observed_at, prev_wk
            )
            weekly = _pick_more_constrained(gem_wk, argon_wk)
        else:
            weekly = gem_wk

    if five_hour is None and weekly is None:
        return prev_matching
    return {
        "group": group,
        "five_hour": five_hour,
        "weekly": weekly,
    }


def _extract_cwd(payload: Dict[str, Any]) -> str:
    ws = payload.get("workspace")
    if isinstance(ws, dict):
        cur = _clean_text(ws.get("current_dir") or ws.get("project_dir"), _MAX_PATH)
        if cur:
            return cur
    return _clean_text(payload.get("cwd"), _MAX_PATH) or ""


def normalize_payload(
    payload: Dict[str, Any],
    ts: Optional[int] = None,
    pid: Optional[int] = None,
    tmux_pane: Optional[str] = None,
    prev_cache: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Normalize whitelisted agy statusLine stdin fields to schema=1 cache."""
    now_ts = common.now() if ts is None else ts
    parent_pid = os.getppid() if pid is None else pid
    pane = (os.environ.get("TMUX_PANE") or None) if tmux_pane is None else tmux_pane
    sid = resolve_session_id(payload, ppid=parent_pid)
    group = detect_quota_group(payload)
    prev_quota = prev_cache.get("quota") if isinstance(prev_cache, dict) else None

    return {
        "schema": common.SCHEMA,
        "tool": "agy",
        "session_id": sid,
        "pid": parent_pid,
        "cwd": _extract_cwd(payload),
        "model": short_model_name(payload),
        "effort": extract_effort(payload),
        "tmux_pane": pane,
        "ctx": normalize_context(payload),
        "quota": normalize_quota(
            payload, group, now_ts, argon=is_argon_model(payload), prev_quota=prev_quota
        ),
        "observed_at": now_ts,
        "received_at": now_ts,
    }


def _fallback_render(cache: Dict[str, Any]) -> str:
    parts: List[str] = []
    model = cache.get("model")
    if model:
        parts.append(str(model))

    ctx = cache.get("ctx") or {}
    pct = ctx.get("used_pct")
    if pct is None:
        parts.append("CTX ?")
    else:
        mark = "!" if pct >= float(common.load_config().get("ctx_warn", 50.0)) else ""
        parts.append("CTX %d%%%s" % (int(round(pct)), mark))

    quota = cache.get("quota") or {}
    q_warn = float(common.load_config().get("quota_warn", 80.0))
    for label, key in (("5h", "five_hour"), ("7d", "weekly")):
        win = quota.get(key)
        if isinstance(win, dict) and win.get("used_pct") is not None:
            wpct = float(win["used_pct"])
            wmark = "!" if wpct >= q_warn else ""
            parts.append("%s %d%%%s" % (label, int(round(wpct)), wmark))
    return " | ".join(parts)


def _render_output(
    cache: Dict[str, Any], width: Optional[int], group: str
) -> str:
    try:
        from ctxhud import render  # type: ignore

        picked = render.pick_quota("agy", group=group)
        quota = picked if picked is not None else cache.get("quota")
        return render.render_line(cache, quota=quota, width=width, style="ansi")
    except ImportError:
        return _fallback_render(cache)
    except Exception as exc:
        common.log_error("bridge_agy.render", exc)
        return _fallback_render(cache)


def main(argv: Optional[List[str]] = None) -> int:
    del argv
    try:
        payload = common.read_stdin_json()
        if payload is None:
            sys.stdout.write("CTX ?\n")
            return 0
        sid = resolve_session_id(payload)
        common.write_raw("agy", sid, sanitize_raw_payload(payload))
        prev = common.read_json(common.cache_path("agy", sid))
        cache = normalize_payload(
            payload, prev_cache=prev if isinstance(prev, dict) else None
        )
        common.write_cache(cache)
        width = common.as_int(payload.get("terminal_width"))
        if width is not None and width <= 0:
            width = None
        if width is None:
            width = common.as_int(os.environ.get("COLUMNS"))
        group = detect_quota_group(payload)
        line = _render_output(cache, width, group=group)
        sys.stdout.write(line + "\n")
    except Exception as exc:
        common.log_error("bridge_agy.main", exc)
        try:
            sys.stdout.write("CTX ?\n")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
