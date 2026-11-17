"""Codex rollout reader and fail-closed tmux border adapter (MIT).

Only an open rollout file in the pane's Codex process subtree establishes
identity. No directory-based guessing, daemon, network or credential access.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import sys
import time
from contextlib import contextmanager

TYPE_CHECKING = False

if TYPE_CHECKING:
    from typing import Any, Dict, List, Optional

from . import common

MAX_LINE_BYTES = 1024 * 1024
MAX_BATCH_BYTES = 4 * 1024 * 1024
COMMAND_TIMEOUT = 1.0
FORMULA = "codex-tui-12k-v0.169.0"
PARSER_VERSION = 2
EFFORTS = frozenset(("low", "medium", "high", "xhigh", "max"))
# Bound the interval without FD/parentage verification, including a same-pid
# thread switch. Hot calls do not spawn tmux, ps or lsof. Expiry re-resolves.
MAPPING_MAX_AGE = 60.0
_PANE = re.compile(r"%[0-9]+\Z")
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class Unresolved(Exception):
    """No unambiguous live pane-to-rollout association is available."""


def _number(value: Any) -> Optional[float]:
    result = common.as_float(value)
    return result if result is not None and math.isfinite(result) else None


def _count(value: Any) -> Optional[int]:
    number = _number(value)
    if number is None or number < 0 or not number.is_integer():
        return None
    return int(number)


def _timestamp(value: Any) -> Optional[float]:
    number = _number(value)
    if number is not None:
        return number if number >= 0 else None
    if not isinstance(value, str):
        return None
    try:
        import datetime
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def context_percent(tokens: Any, window: Any) -> Optional[int]:
    """Use the installed-version TUI baseline and Rust's positive rounding."""
    current, capacity = _count(tokens), _count(window)
    if current is None or capacity is None or capacity == 0:
        return None
    if capacity <= 12000:
        return 100
    effective = capacity - 12000
    remaining = max(0, effective - max(current - 12000, 0))
    left = min(100.0, 100.0 * remaining / effective)
    return 100 - int(math.floor(left + 0.5))


def _run(args: List[str]) -> str:
    import shutil
    import subprocess
    executable = shutil.which(args[0])
    if executable is None:
        raise Unresolved("required local command unavailable")
    result = subprocess.run(
        [executable] + args[1:], stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, timeout=COMMAND_TIMEOUT,
        encoding="utf-8", errors="replace", check=False,
    )
    if result.returncode != 0:
        raise Unresolved("local identity query failed")
    return result.stdout


def _processes(text: str) -> Dict[int, Dict[str, Any]]:
    records = {}
    for line in text.splitlines():
        parts = line.split(None, 7)
        if len(parts) != 8:
            continue
        try:
            pid, parent = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        records[pid] = {
            "pid": pid, "ppid": parent, "start": " ".join(parts[2:7]),
            "command": parts[7],
        }
    return records


def _descendants(root: int, records: Dict[int, Dict[str, Any]]) -> List[int]:
    found, pending = set(), [root]
    children = {}
    for pid, record in records.items():
        children.setdefault(record["ppid"], []).append(pid)
    while pending:
        pid = pending.pop()
        if pid not in found:
            found.add(pid)
            pending.extend(children.get(pid, []))
    return sorted(found)


def _rollout_path(path: str) -> Optional[str]:
    if not path.endswith(".jsonl"):
        return None
    resolved = os.path.realpath(path)
    for folder in ("sessions", "archived_sessions"):
        root = os.path.realpath(os.path.join(common.codex_home(), folder))
        try:
            if os.path.commonpath([root, resolved]) == root and os.path.isfile(resolved):
                return resolved
        except ValueError:
            pass
    return None


def _metadata(path: str) -> Dict[str, Any]:
    with open(path, "rb") as stream:
        first = stream.readline(MAX_LINE_BYTES + 1)
    if len(first) > MAX_LINE_BYTES or not first.endswith(b"\n"):
        raise Unresolved("rollout metadata incomplete")
    try:
        record = json.loads(first)
    except (ValueError, UnicodeError):
        raise Unresolved("invalid rollout metadata")
    payload = record.get("payload") if isinstance(record, dict) else None
    if not isinstance(record, dict) or record.get("type") != "session_meta" or not isinstance(payload, dict):
        raise Unresolved("rollout has no session metadata")
    sid = payload.get("id") or payload.get("session_id")
    if not isinstance(sid, str) or _ID.fullmatch(sid) is None:
        raise Unresolved("invalid thread identifier")
    cwd = payload.get("cwd")
    return {"session_id": sid, "cwd": cwd if isinstance(cwd, str) else None,
            "observed_at": _timestamp(record.get("timestamp"))}


def resolve_pane(pane: str, cached: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Establish identity using process birth times and open descriptors.

    A cached pid alone cannot detect a thread switch in a surviving process.
    Multiple rollouts or multiple Codex owners are deliberately unresolved.
    """
    if _PANE.fullmatch(pane) is None:
        raise Unresolved("invalid pane identifier")
    values = _run(["tmux", "display-message", "-p", "-t", pane,
                   "#{pane_pid}\t#{pane_width}\t#{pane_dead}"]).strip().split("\t")
    if len(values) != 3 or values[2] != "0":
        raise Unresolved("pane is not live")
    try:
        pane_pid, width = int(values[0]), int(values[1])
    except ValueError:
        raise Unresolved("invalid pane process")
    records = _processes(_run(["ps", "-ax", "-o", "pid=,ppid=,lstart=,comm="]))
    if pane_pid not in records:
        raise Unresolved("pane process not observable")
    candidates = []
    for pid in _descendants(pane_pid, records):
        name = os.path.basename(records.get(pid, {}).get("command", ""))
        if name == "codex" or name.startswith("codex-"):
            candidates.append(pid)
    if not candidates:
        raise Unresolved("no Codex process in pane")
    output = _run(["lsof", "-nP", "-a", "-p", ",".join(map(str, candidates)), "-Fpn"])
    owners = set()
    owner = None
    for line in output.splitlines():
        if line.startswith("p"):
            try:
                owner = int(line[1:])
            except ValueError:
                owner = None
        elif line.startswith("n") and owner in candidates:
            path = _rollout_path(line[1:])
            if path is not None:
                owners.add((owner, path))
    if len(owners) != 1:
        raise Unresolved("rollout ownership ambiguous or unavailable")
    pid, path = next(iter(owners))
    starts = {str(item): records[item]["start"] for item in candidates + [pane_pid]}
    signature = {"pane_pid": pane_pid, "pid": pid, "starts": starts, "rollout_path": path}
    if (isinstance(cached, dict) and all(cached.get(key) == value for key, value in signature.items())
            and isinstance(cached.get("session_id"), str)
            and _ID.fullmatch(cached["session_id"])):
        meta = _metadata(path)
        if meta["session_id"] != cached["session_id"]:
            raise Unresolved("rollout identity changed")
    else:
        meta = _metadata(path)
    result = dict(signature, **meta, tmux_pane=pane, width=max(1, width))
    result.update(mapping_version=2, validated_at=time.time(),
                  codex_home=os.path.realpath(common.codex_home()),
                  tmux_server=os.environ.get("TMUX", "").split(",", 1)[0])
    return result


def cached_mapping(pane: str, cached: Any) -> Optional[Dict[str, Any]]:
    """Cheap bounded cache check; never execute an identity subprocess.

    Birth time/FD ownership is verified only on the slow path. A surviving
    process switching threads can therefore take at most MAX_AGE to refresh.
    """
    if not isinstance(cached, dict) or cached.get("mapping_version") != 2:
        return None
    validated = _number(cached.get("validated_at"))
    age = time.time() - validated if validated is not None else -1
    if not 0 <= age < MAPPING_MAX_AGE:
        return None
    sid = cached.get("session_id")
    if (cached.get("tmux_pane") != pane or not isinstance(sid, str)
            or _ID.fullmatch(sid) is None
            or cached.get("codex_home") != os.path.realpath(common.codex_home())
            or cached.get("tmux_server") != os.environ.get("TMUX", "").split(",", 1)[0]):
        return None
    for key in ("pane_pid", "pid"):
        pid = cached.get(key)
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return None
        try:
            os.kill(pid, 0)
        except OSError:
            return None
    path = cached.get("rollout_path")
    if not isinstance(path, str) or _rollout_path(path) != path:
        return None
    if (not isinstance(cached.get("width"), int) or isinstance(cached["width"], bool)
            or cached["width"] <= 0):
        return None
    return cached


def _empty_context() -> Dict[str, Any]:
    return {"used_pct": None, "tokens": None, "window": None,
            "source": "none", "formula": FORMULA}


def _empty_quota() -> Dict[str, Any]:
    return {"group": "codex", "five_hour": None, "weekly": None}


def _effort(settings: Dict[str, Any]) -> Optional[str]:
    # Explicit null/unsupported direct fields are authoritative, not a reason
    # to reuse a potentially stale collaboration-mode setting.
    if "effort" in settings:
        value = settings["effort"]
    elif "reasoning_effort" in settings:
        value = settings["reasoning_effort"]
    else:
        mode = settings.get("collaboration_mode")
        nested = mode.get("settings") if isinstance(mode, dict) else None
        value = nested.get("reasoning_effort") if isinstance(nested, dict) else None
    value = value.strip().lower() if isinstance(value, str) else None
    return value if value in EFFORTS else None


def _apply_settings(settings: Dict[str, Any], snapshot: Dict[str, Any]) -> None:
    model = settings.get("model")
    if isinstance(model, str) and model != snapshot.get("model"):
        snapshot["model"] = model  # Original display name; renderer owns shortening.
        snapshot["ctx"] = _empty_context()
    snapshot["effort"] = _effort(settings)


def _quota(payload: Dict[str, Any], observed: Optional[float]) -> Dict[str, Any]:
    result = _empty_quota()
    for key, fallback in (("primary", "five_hour"), ("secondary", "weekly")):
        window = payload.get(key)
        if not isinstance(window, dict):
            continue
        used = _number(window.get("used_percent"))
        if used is None:
            continue
        minutes = window.get("window_minutes")
        if minutes is None:
            slot = fallback
        elif _number(minutes) == 300:
            slot = "five_hour"
        elif _number(minutes) == 10080:
            slot = "weekly"
        else:
            continue  # Do not label an explicitly different window as 5h/weekly.
        result[slot] = {"used_pct": max(0.0, min(100.0, used)),
                        "resets_at": _timestamp(window.get("resets_at")),
                        "observed_at": observed}
    return result


def _apply(record: Any, snapshot: Dict[str, Any]) -> None:
    if not isinstance(record, dict):
        return
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return
    if record.get("type") == "session_meta":
        sid = payload.get("id") or payload.get("session_id")
        snapshot["session_id"] = sid if isinstance(sid, str) and _ID.fullmatch(sid) else None
        return
    if record.get("type") == "turn_context":
        _apply_settings(payload, snapshot)
        return
    if record.get("type") != "event_msg":
        return
    event = payload.get("type")
    if event == "thread_settings_applied":
        settings = payload.get("thread_settings")
        if isinstance(settings, dict):
            _apply_settings(settings, snapshot)
        return
    if event != "token_count":
        return
    observed = _timestamp(record.get("timestamp"))
    info = payload.get("info")
    if isinstance(info, dict):
        last = info.get("last_token_usage")
        tokens = _count(last.get("total_tokens")) if isinstance(last, dict) else None
        if tokens is not None:
            window = _count(info.get("model_context_window"))
            if window == 0:
                window = None
            snapshot["ctx"] = {"used_pct": context_percent(tokens, window),
                               "tokens": tokens, "window": window,
                               "source": "observed_w" if window is not None else "none",
                               "formula": FORMULA}
            snapshot["observed_at"] = observed
    limits = payload.get("rate_limits")
    if isinstance(limits, dict) and limits and limits.get("limit_id") in (None, "codex"):
        snapshot["quota"] = _quota(limits, observed)


def tail_rollout(path: str, previous: Optional[Dict[str, Any]],
                 batch_bytes: int = MAX_BATCH_BYTES) -> Dict[str, Any]:
    """Read complete appended lines only; return a serializable checkpoint."""
    stat = os.stat(path)
    old = previous if isinstance(previous, dict) else {}
    offset = old.get("offset", 0)
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        offset = 0
    with open(path, "rb") as stream:
        head_size = old.get("head_size", 0)
        if not isinstance(head_size, int) or isinstance(head_size, bool) or not 0 <= head_size <= 512:
            head_size = 0
        head = stream.read(max(0, head_size))
        valid = (old.get("parser_version") == PARSER_VERSION
                 and old.get("device") == stat.st_dev and old.get("inode") == stat.st_ino
                 and offset <= stat.st_size and hashlib.sha256(head).hexdigest() == old.get("head"))
        if valid:
            stream.seek(max(0, offset - 64))
            anchor = stream.read(min(offset, 64))
            valid = hashlib.sha256(anchor).hexdigest() == old.get("anchor")
        saved = old.get("snapshot")
        if (valid and isinstance(saved, dict) and isinstance(saved.get("ctx"), dict)
                and isinstance(saved.get("quota"), dict)):
            snapshot = dict(old["snapshot"])
            skipping = bool(old.get("skipping_long_line"))
        else:
            offset, skipping = 0, False
            snapshot = {"session_id": None, "model": None, "effort": None,
                        "ctx": _empty_context(), "quota": _empty_quota(),
                        "observed_at": None}
        stream.seek(offset)
        consumed = 0
        catching_up = False
        while True:
            if consumed >= max(1, batch_bytes):
                catching_up = stream.tell() < stat.st_size
                break
            start = stream.tell()
            line = stream.readline(MAX_LINE_BYTES + 1)
            if not line:
                break
            consumed += len(line)
            complete = line.endswith(b"\n")
            if skipping or len(line) > MAX_LINE_BYTES:
                skipping = not complete
                offset = stream.tell()
                continue
            if not complete:
                stream.seek(start)
                break
            offset = stream.tell()
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            _apply(record, snapshot)
        stream.seek(0)
        head_size = min(stat.st_size, 512)
        head = stream.read(head_size)
        stream.seek(max(0, offset - 64))
        anchor = stream.read(min(offset, 64))
    return {"parser_version": PARSER_VERSION,
            "device": stat.st_dev, "inode": stat.st_ino, "offset": offset,
            "head_size": head_size, "head": hashlib.sha256(head).hexdigest(),
            "anchor": hashlib.sha256(anchor).hexdigest(), "snapshot": snapshot,
            "skipping_long_line": skipping, "catching_up": catching_up}


@contextmanager
def _state_lock():
    # Create each parent explicitly: makedirs only applies mode to its leaf.
    root = common.ensure_dir(common.state_dir())
    common.ensure_dir(os.path.join(root, "cache"))
    descriptor = os.open(common.sub_state("codex", ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Unresolved("collector is busy")
        yield
    finally:
        os.close(descriptor)


def collect_for_pane(pane: str) -> Dict[str, Any]:
    with _state_lock():
        panes_path = common.sub_state("codex", "panes.json")
        offsets_path = common.sub_state("codex", "offsets.json")
        panes, offsets = common.read_json(panes_path), common.read_json(offsets_path)
        panes = panes if isinstance(panes, dict) else {}
        offsets = offsets if isinstance(offsets, dict) else {}
        try:
            mapping = cached_mapping(pane, panes.get(pane))
            if mapping is None:
                mapping = resolve_pane(pane, panes.get(pane))
        except Exception:
            if pane in panes:
                del panes[pane]
                common.atomic_write_json(panes_path, panes)
            raise
        path = mapping["rollout_path"]
        identity = hashlib.sha256(path.encode("utf-8")).hexdigest()
        checkpoint = tail_rollout(path, offsets.get(identity))
        snapshot = checkpoint["snapshot"]
        if snapshot.get("session_id") != mapping["session_id"]:
            raise Unresolved("rollout changed while collecting")
        entry = {"schema": common.SCHEMA, "tool": "codex", "session_id": mapping["session_id"],
                 "pid": mapping["pid"], "cwd": mapping["cwd"], "model": snapshot.get("model"),
                 "effort": snapshot.get("effort"),
                 "tmux_pane": pane, "ctx": snapshot["ctx"], "quota": snapshot["quota"],
                 "observed_at": snapshot.get("observed_at"), "received_at": common.now()}
        if checkpoint["catching_up"]:
            entry["ctx"], entry["quota"] = _empty_context(), _empty_quota()
        # No new source data means no new received_at and no repeated disk writes.
        changed = checkpoint != offsets.get(identity) or mapping != panes.get(pane)
        if changed or not os.path.isfile(common.cache_path("codex", mapping["session_id"])):
            common.write_cache(entry)  # Cache before cursor: a crash replays, never skips.
        if checkpoint != offsets.get(identity):
            offsets[identity] = checkpoint
            common.atomic_write_json(offsets_path, offsets)
        if mapping != panes.get(pane):
            panes[pane] = mapping
            common.atomic_write_json(panes_path, panes)
        return {"entry": entry, "width": mapping["width"],
                "catching_up": checkpoint["catching_up"]}


def render_line(*args: Any, **kwargs: Any) -> str:
    from .render import pick_quota, render_line as render
    return render(*args, quota=pick_quota("codex"), **kwargs)


def main(argv: Optional[List[str]] = None) -> int:
    text = "Codex | CTX -- [unresolved]"
    quiet = False
    try:
        args = list(sys.argv[1:] if argv is None else argv)
        if args and args[0] == "codex-border":
            args = args[1:]
        quiet = "--quiet-unresolved" in args
        args = [arg for arg in args if arg != "--quiet-unresolved"]
        requested_width = None
        width_options = [index for index, arg in enumerate(args) if arg == "--width"]
        if width_options:
            index = width_options[0]
            if len(width_options) != 1 or index + 1 >= len(args):
                raise Unresolved("--width needs one positive integer")
            requested_width = int(args[index + 1])
            if requested_width <= 0:
                raise Unresolved("--width needs one positive integer")
            del args[index:index + 2]
        if len(args) != 1 or _PANE.fullmatch(args[0]) is None:
            raise Unresolved("one pane identifier is required")
        result = collect_for_pane(args[0])
        if result["catching_up"]:
            text = "Codex | CTX -- [catching-up]"
        else:
            width = result["width"] if requested_width is None else max(1, requested_width - 2)
            text = render_line(result["entry"], width=width, style="tmux")
    except Exception as exc:
        common.log_error("codex-border", exc)
        if quiet:
            text = ""
    try:
        sys.stdout.write(" ".join(str(text).splitlines()) + "\n" if text else "")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    main()
