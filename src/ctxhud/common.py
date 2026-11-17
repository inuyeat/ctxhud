"""Shared paths, config, atomic cache I/O, logging and time helpers.

Standard library only; must stay importable on Python 3.9.
"""
from __future__ import annotations

import json
import os
import sys
import time
TYPE_CHECKING = False  # avoid importing typing at runtime (~20ms on uncached 3.9 stdlib)
if TYPE_CHECKING:  # typing costs ~20ms to import on an uncached stdlib (Python 3.9 CLT)
    from typing import Any, Dict, Iterator, List, Optional

SCHEMA = 1
TOOLS = ("claude", "codex", "agy")

DEFAULT_CONFIG: "Dict[str, Any]" = {
    "ctx_warn": 50.0,          # context warning threshold (percent used)
    "quota_warn": 80.0,        # 5h / weekly warning threshold (percent used)
    "quota_old_sec": 600,      # quota observation older than this -> [old Nm]
    "ctx_age_sec": 900,        # context observation older than this -> show age
    "color": True,
    "capture_raw": False,      # keep last raw stdin per session (debug / fixtures)
    # Generic symbols, not product logos. Users may override locally.
    "icons": {"claude": "\u273b", "codex": "\u25c8", "agy": "\u25b2"},
    # "unicode": works everywhere, warning bubble rendered as "(!)"
    # "nerd": Nerd Font message-alert glyph as the warning bubble
    "icons_style": "unicode",
    # Optional per-tool icon color as "#rrggbb" (truecolor). Empty = terminal default.
    "icon_colors": {"claude": "", "codex": "", "agy": ""},
    "bars": True,              # heavy-line bars next to CTX and 5h (dropped first when narrow)
    "bar_width": 10,           # cells (10% per cell)
    "bar_track_color": "",     # "#rrggbb" for the empty part; empty = dim attribute
    # "one-line" (default): everything on one line.
    # "two-line": line 1 = icon, model, CTX / line 2 = 5h, 7d. Opt-in; tmux borders stay one line.
    "layout": "one-line",
}

_LOG_MAX_BYTES = 1_000_000


# ---------------------------------------------------------------- paths

def _home() -> str:
    return os.path.expanduser("~")


def state_dir() -> str:
    env = os.environ.get("CTXHUD_STATE_DIR")
    if env:
        return os.path.expanduser(env)
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(_home(), ".local", "state")
    return os.path.join(base, "ctxhud")


def config_path() -> str:
    env = os.environ.get("CTXHUD_CONFIG")
    if env:
        return os.path.expanduser(env)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(_home(), ".config")
    return os.path.join(base, "ctxhud", "config.json")


def claude_home() -> str:
    return os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(_home(), ".claude"))


def codex_home() -> str:
    return os.path.expanduser(os.environ.get("CODEX_HOME") or os.path.join(_home(), ".codex"))


def agy_home() -> str:
    return os.path.expanduser(
        os.environ.get("CTXHUD_AGY_HOME") or os.path.join(_home(), ".gemini", "antigravity-cli")
    )


def ensure_dir(path: str) -> str:
    """Create path and any missing parents with 0700.

    os.makedirs(mode=...) only applies the mode to the leaf; parents get the
    umask default (often 0755), which would expose session-id file names.
    Existing directories are left untouched.
    """
    path = os.path.abspath(path)
    missing = []
    cur = path
    while cur and not os.path.isdir(cur):
        missing.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    for d in reversed(missing):
        try:
            os.mkdir(d, 0o700)
        except FileExistsError:
            pass
    return path


def sub_state(*parts: str) -> str:
    """Path under the state dir; parent directories are created (0700)."""
    path = os.path.join(state_dir(), *parts)
    ensure_dir(os.path.dirname(path))
    return path


def _safe_name(name: str) -> str:
    keep = "".join(c for c in str(name) if c.isalnum() or c in "-_.")
    return keep.strip(".")[:128] or "unknown"


def cache_path(tool: str, session_id: str) -> str:
    return sub_state("cache", tool, _safe_name(session_id) + ".json")


def raw_path(tool: str, session_id: str) -> str:
    return sub_state("raw", tool, _safe_name(session_id) + ".json")


# ---------------------------------------------------------------- config

_config_cache: "Optional[Dict[str, Any]]" = None


def load_config() -> Dict[str, Any]:
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(config_path(), "r", encoding="utf-8") as fh:
            user = json.load(fh)
        if isinstance(user, dict):
            for key, val in user.items():
                if key not in DEFAULT_CONFIG:
                    continue
                if isinstance(DEFAULT_CONFIG[key], dict) and isinstance(val, dict):
                    merged = dict(DEFAULT_CONFIG[key])
                    merged.update(val)
                    cfg[key] = merged
                else:
                    cfg[key] = val
    except FileNotFoundError:
        pass
    except Exception as exc:  # malformed config must never break a status line
        log_error("config", exc)
    if os.environ.get("NO_COLOR"):
        cfg["color"] = False
    _config_cache = cfg
    return cfg


# ---------------------------------------------------------------- I/O

def atomic_write_json(path: str, data: Any) -> None:
    """Write JSON atomically with 0600 permissions."""
    directory = ensure_dir(os.path.dirname(path))
    # Hand-rolled temp name instead of tempfile (saves ~13ms of imports on 3.9).
    tmp = os.path.join(directory, ".tmp-%d-%s" % (os.getpid(), os.urandom(4).hex()))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: str) -> Optional[Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_cache(entry: Dict[str, Any]) -> str:
    """Validate the minimum shape of a normalized cache entry and store it."""
    tool = entry.get("tool")
    sid = entry.get("session_id")
    if tool not in TOOLS or not sid:
        raise ValueError("cache entry needs tool in %s and session_id" % (TOOLS,))
    entry.setdefault("schema", SCHEMA)
    entry.setdefault("received_at", now())
    entry.setdefault("observed_at", entry["received_at"])
    path = cache_path(tool, sid)
    atomic_write_json(path, entry)
    return path


def write_raw(tool: str, session_id: str, payload: Any) -> None:
    if load_config().get("capture_raw"):
        atomic_write_json(raw_path(tool, session_id), payload)


def iter_cache(tool: Optional[str] = None) -> Iterator[Dict[str, Any]]:
    tools = [tool] if tool else list(TOOLS)
    for t in tools:
        directory = os.path.join(state_dir(), "cache", t)
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            if name.endswith(".json") and not name.startswith("."):
                data = read_json(os.path.join(directory, name))
                if isinstance(data, dict) and data.get("schema") == SCHEMA:
                    yield data


def read_stdin_json() -> Optional[Dict[str, Any]]:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception as exc:
        log_error("stdin", exc)
        return None


# ---------------------------------------------------------------- misc

def now() -> int:
    return int(time.time())


def as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None  # drop NaN


def as_int(value: Any) -> Optional[int]:
    f = as_float(value)
    return int(f) if f is not None else None


def pid_alive(pid: Optional[int]) -> Optional[bool]:
    if not pid:
        return None
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False


def log_error(where: str, exc: BaseException) -> None:
    """Append one line to the log; never raises, never writes to stderr."""
    try:
        path = sub_state("log", "ctxhud.log")
        try:
            if os.path.getsize(path) > _LOG_MAX_BYTES:
                os.replace(path, path + ".1")
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("%s %s %s: %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), where,
                                         type(exc).__name__, exc))
        os.chmod(path, 0o600)
    except Exception:
        pass
