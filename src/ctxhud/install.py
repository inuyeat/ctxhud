"""``ctxhud install | uninstall | doctor``: wire ctxhud into agent and tmux configs (MIT).

What gets touched (each only if present, or if named with ``--tools``):

* Claude Code  ``<claude home>/settings.json``  ->  ``statusLine`` (+ ``refreshInterval``)
* Antigravity  ``<agy home>/settings.json``     ->  ``statusLine``
* Codex CLI    ``<codex home>/config.toml``     ->  ``[tui] status_line`` (built-in items)
* tmux         ``~/.tmux.conf``                 ->  one marked block (pane border at the bottom)

Rules: every run is planned first and nothing is written when any tool conflicts
(an existing, non-ctxhud ``statusLine`` needs ``--force``); files are backed up under
``<state>/backups/<timestamp>/`` before they change; re-running is a no-op; uninstall
removes exactly what install added and puts back what ``--force`` replaced. Configs
are edited minimally (JSON: same indent and trailing newline; TOML and tmux: single
inserted lines / one marked block). No network, no credentials.

Standard library only, Python >= 3.9.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any, Callable, Dict, List, Optional, Tuple

from . import common

ALL_TOOLS = ("claude", "agy", "codex", "tmux")
DEFAULT_REFRESH = 30
CODEX_ITEMS = ("context-used", "five-hour-limit", "weekly-limit")
BLOCK_BEGIN = "# >>> ctxhud >>>"
BLOCK_END = "# <<< ctxhud <<<"
TOML_MARK = "# ctxhud"
_TMUX_SAFE_PATH = re.compile(r"[A-Za-z0-9_@%+=:./-]+\Z")
_OWN_SUBCOMMANDS = ("bridge", "capture", "codex-border")


class Conflict(Exception):
    """Something already there is not ours (needs --force) or cannot be edited safely."""


class Fatal(Exception):
    """The run cannot continue (bad arguments, missing executable, failed write)."""


# ---------------------------------------------------------------- small helpers

def _tilde(text: str) -> str:
    home = os.path.expanduser("~")
    return text.replace(home, "~") if home and home != "/" else text


def _read_text(path: str) -> "Optional[str]":
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            return fh.read()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise Conflict("cannot read %s (%s)" % (_tilde(path), type(exc).__name__))


def _write_text(path: str, text: str, mode: "Optional[int]" = None) -> None:
    """Atomic write; keeps the permissions of an existing file (0600 for new ones)."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    if mode is None:
        try:
            mode = os.stat(path).st_mode & 0o777
        except OSError:
            mode = 0o600
    tmp = os.path.join(directory, ".ctxhud-tmp-%d" % os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _newline(text: "Optional[str]") -> str:
    return "\r\n" if text and "\r\n" in text else "\n"


def _diff(old: "Optional[str]", new: "Optional[str]", label: str) -> str:
    """Unified diff with no context lines, so unrelated settings never get printed."""
    old_lines = (old or "").splitlines(keepends=True)
    new_lines = (new or "").splitlines(keepends=True)
    name = _tilde(label)
    out = difflib.unified_diff(old_lines, new_lines, "a/" + name if old is not None else "/dev/null",
                               "b/" + name if new is not None else "/dev/null", n=0)
    lines = []
    for line in out:
        lines.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(lines)


def resolve_exe(explicit: "Optional[str]" = None) -> str:
    """Absolute path of the installed ``ctxhud`` executable."""
    candidate = explicit or os.environ.get("CTXHUD_EXE")
    if candidate:
        path = os.path.abspath(os.path.expanduser(candidate))
        if not (os.path.isfile(path) and os.access(path, os.X_OK)):
            raise Fatal("--exe %s is not an executable file" % _tilde(path))
        return path
    found = shutil.which("ctxhud")
    if not found and os.path.basename(sys.argv[0]) == "ctxhud" and os.path.isfile(sys.argv[0]):
        found = sys.argv[0]
    if not found:
        raise Fatal("cannot locate the ctxhud executable; install it with pipx/uv "
                    "or pass --exe /absolute/path/to/ctxhud")
    return os.path.abspath(found)


def _command(exe: str, *args: str) -> str:
    return " ".join([shlex.quote(exe)] + list(args))


def _is_ctxhud_command(command: "Any") -> bool:
    """True if ``command`` runs the ctxhud executable (any path) with one of our subcommands."""
    if not isinstance(command, str):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return False
    if os.path.basename(tokens[0]) == "ctxhud":
        return len(tokens) > 1 and tokens[1] in _OWN_SUBCOMMANDS
    return False


def _is_ctxhud_status(value: "Any") -> bool:
    return isinstance(value, dict) and _is_ctxhud_command(value.get("command"))


# ---------------------------------------------------------------- manifest

def manifest_path() -> str:
    return os.path.join(common.state_dir(), "install.json")


def load_manifest() -> "Dict[str, Any]":
    data = common.read_json(manifest_path())
    if not isinstance(data, dict) or not isinstance(data.get("tools"), dict):
        return {"version": 1, "tools": {}}
    return data


def save_manifest(data: "Dict[str, Any]") -> None:
    if not data.get("tools"):
        try:
            os.unlink(manifest_path())
        except OSError:
            pass
        return
    common.atomic_write_json(manifest_path(), data)


# ---------------------------------------------------------------- plan

class Plan:
    """One file's planned change (``new is None`` deletes the file)."""

    def __init__(self, tool: str, target: str, old: "Optional[str]", new: "Optional[str]",
                 note: str = "", entry: "Optional[Dict[str, Any]]" = None,
                 drop_entry: bool = False) -> None:
        self.tool = tool
        self.target = target
        self.old = old
        self.new = new
        self.note = note
        self.entry = entry or {}
        self.drop_entry = drop_entry

    @property
    def changed(self) -> bool:
        return self.old != self.new


class Context:
    def __init__(self, exe: str, force: bool, refresh: int, tmux_conf: "Optional[str]") -> None:
        self.exe = exe
        self.force = force
        self.refresh = refresh
        self.tmux_conf = tmux_conf


# ---------------------------------------------------------------- JSON settings (Claude, agy)

def _detect_indent(text: str) -> "Any":
    for line in text.splitlines():
        stripped = line.lstrip(" \t")
        if stripped.startswith('"') and stripped != line:
            lead = line[: len(line) - len(stripped)]
            return "\t" if "\t" in lead else len(lead)
    return 2


def _load_settings(path: str) -> "Tuple[Optional[str], Dict[str, Any]]":
    text = _read_text(path)
    if text is None:
        return None, {}
    try:
        data = json.loads(text)
    except ValueError:
        raise Conflict("%s is not strict JSON (comments?); edit it by hand" % _tilde(path))
    if not isinstance(data, dict):
        raise Conflict("%s is not a JSON object" % _tilde(path))
    return text, data


def _dump_settings(data: "Dict[str, Any]", old_text: "Optional[str]") -> str:
    indent = _detect_indent(old_text) if old_text else 2
    nl = _newline(old_text)
    out = json.dumps(data, indent=indent, ensure_ascii=False)
    if nl != "\n":
        out = out.replace("\n", nl)
    trailing = old_text.endswith("\n") if old_text else True
    return out + (nl if trailing else "")


_WS = " \t\r\n"


def _skip_ws(text: str, i: int) -> int:
    while i < len(text) and text[i] in _WS:
        i += 1
    return i


def _layout(text: str) -> "Tuple[int, int, List[Dict[str, Any]]]":
    """Positions of the top-level members of a JSON object: (open, close, members).

    Each member records its key, where the key and value start/end and where its
    trailing comma is. Raises ValueError/IndexError on anything unexpected.
    """
    decoder = json.JSONDecoder()
    open_idx = _skip_ws(text, 0)
    if text[open_idx] != "{":
        raise ValueError("not an object")
    i = _skip_ws(text, open_idx + 1)
    members: "List[Dict[str, Any]]" = []
    if text[i] == "}":
        return open_idx, i, members
    while True:
        key, key_end = decoder.raw_decode(text, i)
        colon = _skip_ws(text, key_end)
        if text[colon] != ":":
            raise ValueError("expected a colon")
        val_start = _skip_ws(text, colon + 1)
        _value, val_end = decoder.raw_decode(text, val_start)
        member = {"key": key, "key_start": i, "val_start": val_start, "val_end": val_end, "comma": None}
        members.append(member)
        nxt = _skip_ws(text, val_end)
        if text[nxt] == ",":
            member["comma"] = nxt
            i = _skip_ws(text, nxt + 1)
            continue
        if text[nxt] == "}":
            return open_idx, nxt, members
        raise ValueError("expected , or }")


def _member_text(value: "Any", indent: "Any", lead: str, nl: str) -> str:
    """JSON text of ``value`` whose continuation lines are prefixed with ``lead``."""
    body = json.dumps(value, indent=indent, ensure_ascii=False)
    return body.replace("\n", nl + lead) if "\n" in body else body


def _line_lead(text: str, pos: int, floor: int) -> "Optional[str]":
    """Whitespace between the start of the line and ``pos`` (None if text precedes ``pos``)."""
    start = text.rfind("\n", 0, pos) + 1
    if start <= floor:                       # on the same line as the opening brace
        return None
    segment = text[start:pos]
    return segment if not segment.strip(_WS) else None


def _status_member(text: str) -> "Optional[Dict[str, Any]]":
    members = _layout(text)[2]
    found = [m for m in members if m["key"] == "statusLine"]
    return found[-1] if found else None


def _surgical_set(text: str, value: "Any", raw: "Optional[str]" = None) -> str:
    """Insert or replace ``statusLine`` by editing only its own bytes.

    ``raw`` is exact JSON text to put in place of ``value`` (used to restore a replaced
    value exactly as the user had written it).
    """
    open_idx, close_idx, members = _layout(text)
    nl = _newline(text)
    indent = _detect_indent(text)
    unit = indent if isinstance(indent, str) else " " * indent
    existing = [m for m in members if m["key"] == "statusLine"]
    if existing:
        member = existing[-1]
        lead = _line_lead(text, member["key_start"], open_idx) or ""
        body = raw if raw is not None else _member_text(value, indent, lead, nl)
        return text[:member["val_start"]] + body + text[member["val_end"]:]
    lead = _line_lead(text, members[0]["key_start"], open_idx) if members else None
    lead = unit if lead is None else lead
    entry = nl + lead + '"statusLine": ' + _member_text(value, indent, lead, nl)
    if members:
        return text[:open_idx + 1] + entry + "," + text[open_idx + 1:]
    return text[:open_idx + 1] + entry + nl + text[close_idx:]


def _surgical_remove(text: str) -> str:
    """Delete the ``statusLine`` member (and its comma) without touching other bytes."""
    open_idx, close_idx, members = _layout(text)
    positions = [n for n, m in enumerate(members) if m["key"] == "statusLine"]
    if not positions:
        return text
    n = positions[-1]
    member = members[n]
    if len(members) == 1:
        return text[:open_idx + 1] + text[close_idx:]
    if member["comma"] is None:                                   # last member: drop the previous comma
        return text[:members[n - 1]["comma"]] + text[member["val_end"]:]
    start = member["key_start"]
    lead_start = text.rfind("\n", 0, start) + 1
    own_line = lead_start > open_idx and not text[lead_start:start].strip(_WS)
    after_comma = member["comma"] + 1
    end = after_comma
    while end < len(text) and text[end] in " \t":
        end += 1
    if own_line:
        start = lead_start
        if text.startswith("\r\n", end):
            end += 2
        elif text.startswith("\n", end):
            end += 1
        elif n == 0 and text[start - 1:start] == "\n":
            # inserted by us right after "{" in front of the user's first key: undo exactly that,
            # keeping the user's own spacing after our comma
            end = after_comma
            start -= 2 if text[start - 2:start] == "\r\n" else 1
    return text[:start] + text[end:]


def _edit_statusline(old_text: "Optional[str]", expected: "Dict[str, Any]", remove: bool = False,
                     value: "Any" = None, raw: "Optional[str]" = None) -> str:
    """New file text: a minimal edit, falling back to canonical formatting if it cannot be verified."""
    if old_text is None:
        return _dump_settings(expected, None)
    try:
        text = _surgical_remove(old_text) if remove else _surgical_set(old_text, value, raw)
        if json.loads(text) == expected:
            return text
    except (ValueError, IndexError):
        pass
    return _dump_settings(expected, old_text)


def _plan_json(ctx: Context, tool: str, settings_path: str, desired: "Dict[str, Any]",
               keep_keys: "Tuple[str, ...]") -> Plan:
    target = os.path.realpath(settings_path)
    old_text, data = _load_settings(target)
    current = data.get("statusLine")
    owned = _is_ctxhud_status(current)
    entry: "Dict[str, Any]" = {"path": target}
    if current is not None and not owned:
        if not ctx.force:
            raise Conflict("%s already has a statusLine that is not ctxhud's (%s); "
                           "re-run with --force to replace it (it is backed up and restored by uninstall)"
                           % (_tilde(target), _tilde(json.dumps(current, ensure_ascii=False))[:120]))
        entry.update({"previous_present": True, "previous": current})
        try:                                    # the user's own text, to restore it exactly
            member = _status_member(old_text or "")
            if member:
                entry["previous_raw"] = (old_text or "")[member["val_start"]:member["val_end"]]
        except (ValueError, IndexError):
            pass
    elif current is None:
        entry["previous_present"] = False
    new_status = dict(desired)
    if owned:
        for key in keep_keys:           # user tuning of an ours-owned entry survives re-installs
            if key in current:
                new_status[key] = current[key]
        for key, value in current.items():
            new_status.setdefault(key, value)
    if old_text is None:
        entry["created_file"] = True
    if "statusLine" in data:
        expected = dict(data, statusLine=new_status)         # replaced where it already is
    else:
        # Inserted as the first key: only the new lines change, so no neighbouring line (which
        # could hold a secret) is touched or shown in the diff just to gain a trailing comma.
        expected = dict([("statusLine", new_status)] + list(data.items()))
    new_text = old_text if current == new_status else _edit_statusline(old_text, expected, value=new_status)
    if old_text == new_text:
        note = "already up to date"
    elif owned:
        note = "upgrade statusLine"
    else:
        note = "replace statusLine" if current is not None else "add statusLine"
    return Plan(tool, target, old_text, new_text, note, entry)


def plan_claude(ctx: Context) -> Plan:
    desired = {"type": "command", "command": _command(ctx.exe, "bridge", "claude"),
               "refreshInterval": ctx.refresh}
    return _plan_json(ctx, "claude", os.path.join(common.claude_home(), "settings.json"), desired,
                      ("refreshInterval", "padding"))


def plan_agy(ctx: Context) -> Plan:
    desired = {"type": "command", "command": _command(ctx.exe, "bridge", "agy"), "enabled": True}
    return _plan_json(ctx, "agy", os.path.join(common.agy_home(), "settings.json"), desired,
                      ("enabled", "padding", "stack_with_default"))


def unplan_json(tool: str, settings_path: str, entry: "Dict[str, Any]") -> Plan:
    target = os.path.realpath(entry.get("path") or settings_path)
    old_text, data = _load_settings(target)
    if old_text is None:
        return Plan(tool, target, None, None, "file not present", drop_entry=True)
    if not _is_ctxhud_status(data.get("statusLine")):
        return Plan(tool, target, old_text, old_text, "no ctxhud statusLine (left alone)", drop_entry=True)
    if entry.get("previous_present"):
        expected = dict(data, statusLine=entry.get("previous"))   # same key position
        new_text = _edit_statusline(old_text, expected, value=entry.get("previous"), raw=entry.get("previous_raw"))
        note = "restored the previous statusLine"
    else:
        expected = {k: v for k, v in data.items() if k != "statusLine"}
        if entry.get("created_file") and not expected:
            return Plan(tool, target, old_text, None, "removed the file ctxhud created", drop_entry=True)
        new_text = _edit_statusline(old_text, expected, remove=True)
        note = "removed statusLine"
    return Plan(tool, target, old_text, new_text, note, drop_entry=True)


# ---------------------------------------------------------------- TOML (Codex config.toml)

def _line_states(lines: "List[str]") -> "List[Tuple[int, Optional[str]]]":
    """(bracket depth, open multi-line string) at the start of every line."""
    depth = 0
    multi: "Optional[str]" = None
    states = []
    for line in lines:
        states.append((depth, multi))
        i = 0
        n = len(line)
        while i < n:
            if multi:
                if multi == '"""' and line[i] == "\\":
                    i += 2
                    continue
                if line.startswith(multi, i):
                    multi = None
                    i += 3
                    continue
                i += 1
                continue
            ch = line[i]
            if ch == "#":
                break
            if line.startswith('"""', i) or line.startswith("'''", i):
                multi = line[i:i + 3]
                i += 3
                continue
            if ch == '"':
                i += 1
                while i < n and line[i] != '"':
                    i += 2 if line[i] == "\\" else 1
                i += 1
                continue
            if ch == "'":
                end = line.find("'", i + 1)
                i = end + 1 if end != -1 else n
                continue
            if ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
            i += 1
    return states


_HEADER = re.compile(r"^\s*(\[\[?)\s*(.+?)\s*\]\]?\s*(#.*)?$")


def _norm_table(name: str) -> str:
    return re.sub(r"\s+", "", name).replace('"', "").replace("'", "")


def _headers(lines: "List[str]", states: "List[Tuple[int, Optional[str]]]") -> "List[Tuple[int, str]]":
    found = []
    for idx, line in enumerate(lines):
        if states[idx] == (0, None) and line.lstrip().startswith("["):
            match = _HEADER.match(line.rstrip("\r\n"))
            if match:
                name = _norm_table(match.group(2))
                found.append((idx, ("[[" + name) if match.group(1) == "[[" else name))
    return found


_STATUS_KEY = re.compile(r"""^\s*(?:status_line|"status_line"|'status_line')\s*=""")
_TUI_ROOT_KEY = re.compile(r"""^\s*(?:tui|"tui"|'tui')\s*[.=]""")


def _toml_locate(text: str) -> "Dict[str, Any]":
    lines = text.splitlines(keepends=True)
    states = _line_states(lines)
    headers = _headers(lines, states)
    first_header = headers[0][0] if headers else len(lines)
    for idx in range(first_header):
        if states[idx] == (0, None) and _TUI_ROOT_KEY.match(lines[idx]):
            raise Conflict("config.toml defines `tui` with dotted or inline keys; add status_line by hand")
    info: "Dict[str, Any]" = {"lines": lines, "states": states, "header": None, "end": len(lines),
                             "key": None, "key_end": None}
    for pos, (idx, name) in enumerate(headers):
        if name == "tui":
            if info["header"] is not None:
                raise Conflict("config.toml has more than one [tui] table")
            info["header"] = idx
            info["end"] = headers[pos + 1][0] if pos + 1 < len(headers) else len(lines)
    if info["header"] is not None:
        for idx in range(info["header"] + 1, info["end"]):
            if states[idx] == (0, None) and _STATUS_KEY.match(lines[idx]):
                if info["key"] is not None:
                    raise Conflict("[tui] sets status_line twice")
                nxt = idx + 1
                while nxt < info["end"] and states[nxt] != (0, None):
                    nxt += 1
                info["key"], info["key_end"] = idx, nxt
    return info


def _toml_items(first_line: str) -> "Optional[Tuple[str, ...]]":
    """The strings of a simple one-line ``key = ["a", "b"]`` array, else None."""
    match = re.search(r"=\s*\[([^\]]*)\]", first_line)
    if not match:
        return None
    return tuple(a or b for a, b in re.findall(r'"([^"]*)"|\'([^\']*)\'', match.group(1)))


def _codex_line(nl: str) -> str:
    return 'status_line = [%s]  %s%s' % (", ".join('"%s"' % item for item in CODEX_ITEMS), TOML_MARK, nl)


def _key_is_ours(lines: "List[str]", start: int) -> bool:
    return re.search(r"#\s*ctxhud\b", lines[start]) is not None


def plan_codex(ctx: Context) -> Plan:
    target = os.path.realpath(os.path.join(common.codex_home(), "config.toml"))
    old = _read_text(target)
    text = old or ""
    nl = _newline(text)
    entry: "Dict[str, Any]" = {"path": target}
    if old is None:
        entry["created_file"] = True
    info = _toml_locate(text)
    lines = list(info["lines"])
    new_line = _codex_line(nl)
    if info["header"] is None:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += nl
        add_blank = bool(lines) and bool(lines[-1].strip())
        block = ([nl] if add_blank else []) + ["[tui]" + nl, new_line]
        entry.update({"created_table": True, "added_blank": add_blank})
        return Plan("codex", target, old, "".join(lines + block), "add [tui] status_line", entry)
    if info["key"] is not None:
        start, end = info["key"], info["key_end"]
        current = "".join(lines[start:end])
        if _key_is_ours(lines, start):
            if tuple(_toml_items(lines[start]) or ()) == CODEX_ITEMS:
                return Plan("codex", target, old, old, "already up to date", entry)
            lines[start:end] = [new_line]
            return Plan("codex", target, old, "".join(lines), "upgrade", entry)
        if tuple(_toml_items(lines[start]) or ()) == CODEX_ITEMS and end == start + 1:
            return Plan("codex", target, old, old, "same items already set (left as is, not managed)", {})
        if not ctx.force:
            raise Conflict("%s [tui] already sets status_line (%s); re-run with --force to replace it "
                           "(it is backed up and restored by uninstall)" % (_tilde(target), current.strip()[:100]))
        entry.update({"previous_text": current})
        lines[start:end] = [new_line]
        return Plan("codex", target, old, "".join(lines), "replace status_line", entry)
    insert_at = info["end"]
    while insert_at - 1 > info["header"] and not lines[insert_at - 1].strip():
        insert_at -= 1
    if not lines[insert_at - 1].endswith(("\n", "\r")):
        lines[insert_at - 1] += nl
    lines.insert(insert_at, new_line)
    return Plan("codex", target, old, "".join(lines), "add status_line to [tui]", entry)


def unplan_codex(entry: "Dict[str, Any]") -> Plan:
    target = os.path.realpath(entry.get("path") or os.path.join(common.codex_home(), "config.toml"))
    old = _read_text(target)
    if old is None:
        return Plan("codex", target, None, None, "file not present", drop_entry=True)
    info = _toml_locate(old)
    if info["key"] is None or not _key_is_ours(info["lines"], info["key"]):
        return Plan("codex", target, old, old, "no ctxhud status_line (left alone)", drop_entry=True)
    lines = list(info["lines"])
    start, end = info["key"], info["key_end"]
    if entry.get("previous_text"):
        lines[start:end] = [entry["previous_text"]]
        return Plan("codex", target, old, "".join(lines), "restored the previous status_line", drop_entry=True)
    del lines[start:end]
    note = "removed status_line"
    header = info["header"]
    if entry.get("created_table") and header is not None:
        table_end = info["end"] - (end - start)
        body = [l for l in lines[header + 1:table_end] if l.strip()]
        if not body:
            del lines[header:table_end]
            if entry.get("added_blank") and header > 0 and not lines[header - 1].strip():
                del lines[header - 1]
            note = "removed the [tui] table ctxhud created"
    new = "".join(lines)
    if entry.get("created_file") and not new.strip():
        return Plan("codex", target, old, None, "removed the file ctxhud created", drop_entry=True)
    return Plan("codex", target, old, new, note, drop_entry=True)


# ---------------------------------------------------------------- tmux

_TMUX_OPTION = re.compile(r"^\s*(?:set|set-option|setw|set-window-option)\b[^#\n]*\bpane-border-(?:status|format)\b")


def tmux_conf_path(explicit: "Optional[str]" = None) -> str:
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    home = os.path.expanduser("~")
    classic = os.path.join(home, ".tmux.conf")
    xdg = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), "tmux", "tmux.conf")
    if os.path.exists(classic) or not os.path.exists(xdg):
        return classic
    return xdg


def tmux_block(exe: str, nl: str = "\n") -> str:
    if not _TMUX_SAFE_PATH.match(exe):
        raise Conflict("the ctxhud path %s has characters tmux formats cannot carry; "
                       "link it from a plain path (e.g. ~/.local/bin/ctxhud) and pass --exe" % _tilde(exe))
    # Codex panes: ctxhud's line. Every other pane: just its title with one space on each side
    # (no pane number, no active-pane reverse video).
    # codex-border gets the pane's width so it can fit the line to the border it draws into.
    fmt = ("#{?#{==:#{pane_current_command},codex},#(%s codex-border #{pane_id} --width #{pane_width}),"
           " #{pane_title} }" % exe)
    return nl.join([
        BLOCK_BEGIN + " (managed by `ctxhud install`; `ctxhud uninstall` removes it)",
        "set-option -g pane-border-status bottom",
        "set-option -g pane-border-format '%s'" % fmt,
        BLOCK_END,
    ]) + nl


def _block_range(lines: "List[str]") -> "Optional[Tuple[int, int]]":
    begin = next((i for i, l in enumerate(lines) if l.startswith(BLOCK_BEGIN)), None)
    if begin is None:
        return None
    end = next((i for i in range(begin + 1, len(lines)) if lines[i].startswith(BLOCK_END)), None)
    if end is None:
        raise Conflict("tmux config has a ctxhud block start without its end marker; fix it by hand")
    return begin, end + 1


def plan_tmux(ctx: Context) -> Plan:
    target = os.path.realpath(tmux_conf_path(ctx.tmux_conf))
    old = _read_text(target)
    text = old or ""
    nl = _newline(text)
    lines = text.splitlines(keepends=True)
    block = tmux_block(ctx.exe, nl)
    entry: "Dict[str, Any]" = {"path": target}
    if old is None:
        entry["created_file"] = True
    span = _block_range(lines)
    if span:
        start, end = span
        if "".join(lines[start:end]) == block:
            return Plan("tmux", target, old, old, "already up to date", entry)
        lines[start:end] = [block]
        return Plan("tmux", target, old, "".join(lines), "upgrade", entry)
    clash = [i + 1 for i, l in enumerate(lines) if _TMUX_OPTION.match(l)]
    if clash and not ctx.force:
        raise Conflict("%s already sets pane-border-status/format (line %s); the ctxhud block would "
                       "override it. Re-run with --force to add the block anyway (uninstall removes only the block)"
                       % (_tilde(target), ", ".join(str(n) for n in clash[:3])))
    add_blank = bool(lines)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += nl
    entry["added_blank"] = add_blank
    return Plan("tmux", target, old, "".join(lines) + (nl if add_blank else "") + block,
                "add pane-border block", entry)


def unplan_tmux(entry: "Dict[str, Any]") -> Plan:
    target = os.path.realpath(entry.get("path") or tmux_conf_path(None))
    old = _read_text(target)
    if old is None:
        return Plan("tmux", target, None, None, "file not present", drop_entry=True)
    lines = old.splitlines(keepends=True)
    span = _block_range(lines)
    if not span:
        return Plan("tmux", target, old, old, "no ctxhud block (left alone)", drop_entry=True)
    start, end = span
    del lines[start:end]
    if entry.get("added_blank") and start > 0 and not lines[start - 1].strip():
        del lines[start - 1]
    new = "".join(lines)
    if entry.get("created_file") and not new.strip():
        return Plan("tmux", target, old, None, "removed the file ctxhud created", drop_entry=True)
    return Plan("tmux", target, old, new, "removed the pane-border block", drop_entry=True)


# ---------------------------------------------------------------- tool selection

_PLANNERS = {"claude": plan_claude, "agy": plan_agy, "codex": plan_codex, "tmux": plan_tmux}


def _tool_home(tool: str) -> str:
    if tool == "claude":
        return common.claude_home()
    if tool == "agy":
        return common.agy_home()
    if tool == "codex":
        return common.codex_home()
    return tmux_conf_path(None)


def _present(tool: str, tmux_conf: "Optional[str]") -> bool:
    if tool == "tmux":
        return bool(shutil.which("tmux")) or os.path.exists(tmux_conf_path(tmux_conf))
    return os.path.isdir(_tool_home(tool))


def _parse_tools(value: "Optional[str]") -> "Tuple[List[str], bool]":
    if not value:
        return list(ALL_TOOLS), False
    chosen = []
    for name in value.split(","):
        name = name.strip().lower()
        if name == "all":
            return list(ALL_TOOLS), True
        if name not in ALL_TOOLS:
            raise Fatal("unknown tool %r (choose from %s)" % (name, ", ".join(ALL_TOOLS)))
        if name not in chosen:
            chosen.append(name)
    return chosen, True


# ---------------------------------------------------------------- apply

def _private_makedirs(path: str) -> None:
    """Create ``path`` and every missing parent with mode 0700.

    ``os.makedirs(mode=...)`` only applies the mode to the last directory; the parents
    would be created with the default (umask) permissions and expose file names.
    """
    missing = []
    probe = path
    while probe and not os.path.isdir(probe):
        missing.append(probe)
        probe = os.path.dirname(probe)
    for directory in reversed(missing):
        os.mkdir(directory, 0o700)


def _backup(target: str, tool: str, stamp_dir: str) -> str:
    _private_makedirs(stamp_dir)
    dest = os.path.join(stamp_dir, "%s-%s" % (tool, os.path.basename(target)))
    shutil.copyfile(target, dest)
    os.chmod(dest, 0o600)
    return dest


def _new_stamp_dir() -> str:
    base = os.path.join(common.state_dir(), "backups")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path, n = os.path.join(base, stamp), 0
    while os.path.exists(path):
        n += 1
        path = os.path.join(base, "%s-%d" % (stamp, n))
    return path


def _apply(plans: "List[Plan]") -> "Optional[str]":
    """Write all changed plans; on failure undo the ones already written."""
    stamp_dir = None
    done: "List[Plan]" = []
    try:
        for plan in plans:
            if not plan.changed:
                continue
            if plan.old is not None:
                stamp_dir = stamp_dir or _new_stamp_dir()
                plan.entry["backup"] = _backup(plan.target, plan.tool, stamp_dir)
            if plan.new is None:
                os.unlink(plan.target)
            else:
                _write_text(plan.target, plan.new)
            done.append(plan)
    except Exception as exc:
        for plan in reversed(done):
            try:
                if plan.old is None:
                    os.unlink(plan.target)
                else:
                    _write_text(plan.target, plan.old)
            except OSError:
                pass
        raise Fatal("write failed (%s); every change made so far was rolled back" % type(exc).__name__)
    return stamp_dir


def _report(plans: "List[Plan]", out: "Any") -> None:
    for plan in plans:
        state = "change" if plan.changed else "ok"
        out.write("[%s] %s: %s (%s)\n" % (plan.tool, _tilde(plan.target), plan.note, state))
        if plan.changed:
            out.write(_diff(plan.old, plan.new, plan.target))


# ---------------------------------------------------------------- manifest merge

_PREVIOUS_KEYS = ("previous_present", "previous", "previous_raw", "previous_text")
_FIRST_INSTALL_KEYS = ("created_file", "created_table", "added_blank")


def _merge_entry(existing: "Dict[str, Any]", new: "Dict[str, Any]") -> "Dict[str, Any]":
    """Combine what an earlier install recorded with this one.

    Facts about the state *before ctxhud* (file/table creation, the replaced value) are
    written once, by the first install that saw that state, and never overwritten by
    later re-installs, so uninstall can still restore the original.
    """
    merged = dict(existing)
    for key, value in new.items():
        if key in _PREVIOUS_KEYS or key in _FIRST_INSTALL_KEYS:
            continue
        merged[key] = value
    for key in _FIRST_INSTALL_KEYS:
        if key not in merged and key in new:
            merged[key] = new[key]
    if "previous" in new or "previous_text" in new:          # this install replaced a foreign value
        for key in _PREVIOUS_KEYS:
            merged.pop(key, None)
            if key in new:
                merged[key] = new[key]
    elif not any(key in existing for key in _PREVIOUS_KEYS) and "previous_present" in new:
        merged["previous_present"] = new["previous_present"]
    return merged


# ---------------------------------------------------------------- commands

def cmd_install(args: "argparse.Namespace", out: "Any") -> int:
    tools, explicit = _parse_tools(args.tools)
    exe = resolve_exe(args.exe)
    ctx = Context(exe, args.force, args.refresh_interval, args.tmux_conf)
    out.write("ctxhud install%s\nexecutable: %s\n" % (" (dry run)" if args.dry_run else "", _tilde(exe)))
    plans: "List[Plan]" = []
    problems: "List[str]" = []
    for tool in tools:
        if not explicit and not _present(tool, args.tmux_conf):
            out.write("[%s] skipped (not found on this machine)\n" % tool)
            continue
        try:
            plans.append(_PLANNERS[tool](ctx))
        except Conflict as exc:
            problems.append("[%s] %s" % (tool, exc))
    if problems:
        out.write("\n".join(problems) + "\nNothing was changed.\n")
        return 1
    _report(plans, out)
    if not any(p.changed for p in plans):
        out.write("Already up to date.\n")
        return 0
    if args.dry_run:
        out.write("Dry run: nothing was written.\n")
        return 0
    try:
        stamp_dir = _apply(plans)
    except Fatal as exc:
        out.write("%s\n" % exc)
        return 1
    manifest = load_manifest()
    manifest["exe"] = exe
    manifest["updated"] = common.now()
    for plan in plans:
        if plan.changed:
            existing = manifest["tools"].get(plan.tool)
            if not (isinstance(existing, dict) and existing.get("path") == plan.target):
                existing = {}
            manifest["tools"][plan.tool] = _merge_entry(existing, plan.entry)
    save_manifest(manifest)
    if stamp_dir:
        out.write("Backup of changed files: %s\n" % _tilde(stamp_dir))
    out.write("Done. Restart running agents so they pick up the new settings"
              "%s.\n" % ("; reload tmux with `tmux source-file %s`" % _tilde(tmux_conf_path(args.tmux_conf))
                         if any(p.tool == "tmux" and p.changed for p in plans) else ""))
    return 0


def cmd_uninstall(args: "argparse.Namespace", out: "Any") -> int:
    tools, explicit = _parse_tools(args.tools)
    manifest = load_manifest()
    out.write("ctxhud uninstall%s\n" % (" (dry run)" if args.dry_run else ""))
    plans: "List[Plan]" = []
    problems: "List[str]" = []
    for tool in tools:
        entry = manifest["tools"].get(tool, {})
        try:
            if tool == "claude":
                plans.append(unplan_json("claude", os.path.join(common.claude_home(), "settings.json"), entry))
            elif tool == "agy":
                plans.append(unplan_json("agy", os.path.join(common.agy_home(), "settings.json"), entry))
            elif tool == "codex":
                plans.append(unplan_codex(entry))
            else:
                if args.tmux_conf and not entry.get("path"):
                    entry = dict(entry, path=tmux_conf_path(args.tmux_conf))
                plans.append(unplan_tmux(entry))
        except Conflict as exc:
            problems.append("[%s] %s" % (tool, exc))
    if problems:
        out.write("\n".join(problems) + "\nNothing was changed.\n")
        return 1
    _report(plans, out)
    if args.dry_run:
        out.write("Dry run: nothing was written.\n")
        return 0
    try:
        stamp_dir = _apply(plans)
    except Fatal as exc:
        out.write("%s\n" % exc)
        return 1
    for plan in plans:
        if plan.drop_entry:
            manifest["tools"].pop(plan.tool, None)
    save_manifest(manifest)
    if stamp_dir:
        out.write("Backup of changed files: %s\n" % _tilde(stamp_dir))
    out.write("Done.\n")
    return 0


# ---------------------------------------------------------------- doctor

def _tmux(*args: str) -> "Tuple[int, str]":
    """Read-only tmux query; (127, '') when tmux is missing or not running."""
    try:
        proc = subprocess.run(["tmux"] + list(args), capture_output=True, text=True, timeout=2)
        return proc.returncode, proc.stdout.strip()
    except Exception:
        return 127, ""


def _age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return "%d%s" % (seconds // size, unit)
    return "%ds" % seconds


class _Report:
    def __init__(self, out: "Any") -> None:
        self.out = out
        self.fails = 0
        self.warns = 0

    def add(self, level: str, text: str) -> None:
        if level == "fail":
            self.fails += 1
        elif level == "warn":
            self.warns += 1
        self.out.write("[%-4s] %s\n" % (level, text))


def _doctor_json_tool(rep: "_Report", tool: str, path: str, exe: "Optional[str]") -> None:
    try:
        text, data = _load_settings(path)
    except Conflict as exc:
        rep.add("warn", "%s: %s" % (tool, exc))
        return
    if text is None:
        rep.add("warn", "%s: %s not found (run `ctxhud install --tools %s` if you use it)" % (tool, _tilde(path), tool))
        return
    status = data.get("statusLine")
    if status is None:
        rep.add("warn", "%s: no statusLine in %s (not wired; run `ctxhud install`)" % (tool, _tilde(path)))
    elif not _is_ctxhud_status(status):
        rep.add("warn", "%s: statusLine in %s is not ctxhud's (left alone)" % (tool, _tilde(path)))
    else:
        try:
            tokens = shlex.split(status["command"])
        except ValueError:
            tokens = []
        first = tokens[0] if tokens else ""
        if not (os.path.isfile(first) and os.access(first, os.X_OK)):
            rep.add("fail", "%s: statusLine runs %s which is missing or not executable" % (tool, _tilde(first)))
        elif len(tokens) > 1 and tokens[1] == "capture":
            rep.add("warn", "%s: statusLine is the diagnostic `capture` command, not the bridge "
                            "(run `ctxhud install --tools %s` to switch)" % (tool, tool))
        else:
            extra = ""
            if tool == "claude":
                extra = ", refreshInterval %s" % status.get("refreshInterval", "unset")
            if exe and os.path.abspath(first) != exe:
                rep.add("warn", "%s: statusLine uses %s but `ctxhud` on PATH is %s%s"
                        % (tool, _tilde(first), _tilde(exe), extra))
            else:
                rep.add("ok", "%s: statusLine -> %s%s" % (tool, _tilde(status["command"]), extra))


def cmd_doctor(args: "argparse.Namespace", out: "Any") -> int:
    rep = _Report(out)
    out.write("ctxhud doctor\n")
    rep.add("ok" if sys.version_info >= (3, 9) else "fail", "python %s" % sys.version.split()[0])
    try:
        exe = resolve_exe(None)
        rep.add("ok", "executable: %s" % _tilde(exe))
    except Fatal as exc:
        exe = None
        rep.add("warn", "executable: %s" % exc)

    state = common.state_dir()
    if not os.path.isdir(state):
        rep.add("warn", "state dir %s does not exist yet (nothing has run)" % _tilde(state))
    else:
        mode = os.stat(state).st_mode & 0o777
        rep.add("ok" if mode & 0o077 == 0 else "warn", "state dir %s (mode %o)" % (_tilde(state), mode))
        log = os.path.join(state, "log", "ctxhud.log")
        if os.path.isfile(log):
            with open(log, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
            tail = _tilde(lines[-1])[:110] if lines else ""
            rep.add("warn" if lines else "ok", "log: %d line(s)%s" % (len(lines), (", last: " + tail) if lines else ""))

    cfg = common.config_path()
    if os.path.exists(cfg):
        data = common.read_json(cfg)
        if not isinstance(data, dict):
            rep.add("warn", "config %s is not a valid JSON object (defaults are used)" % _tilde(cfg))
        else:
            unknown = sorted(set(data) - set(common.DEFAULT_CONFIG))
            rep.add("warn" if unknown else "ok", "config %s%s" % (_tilde(cfg), (" (unknown keys: %s)" % ", ".join(unknown)) if unknown else ""))
    else:
        rep.add("ok", "config: none (defaults)")

    now = common.now()
    for tool in common.TOOLS:
        entries = list(common.iter_cache(tool))
        if not entries:
            rep.add("ok", "cache %s: empty" % tool)
            continue
        newest = max(common.as_int(e.get("received_at")) or 0 for e in entries)
        loose = 0
        directory = os.path.join(state, "cache", tool)
        for name in os.listdir(directory):
            try:
                if os.stat(os.path.join(directory, name)).st_mode & 0o077:
                    loose += 1
            except OSError:
                pass
        level = "warn" if loose else "ok"
        rep.add(level, "cache %s: %d session(s), newest %s ago%s" % (
            tool, len(entries), _age(now - newest), (", %d file(s) readable by others" % loose) if loose else ""))

    _doctor_json_tool(rep, "claude", os.path.join(common.claude_home(), "settings.json"), exe)
    _doctor_json_tool(rep, "agy", os.path.join(common.agy_home(), "settings.json"), exe)

    codex_cfg = os.path.join(common.codex_home(), "config.toml")
    try:
        text = _read_text(codex_cfg)
        if text is None:
            rep.add("warn", "codex: %s not found" % _tilde(codex_cfg))
        else:
            info = _toml_locate(text)
            if info["key"] is None:
                rep.add("warn", "codex: no [tui] status_line (built-in footer items not set; run `ctxhud install --tools codex`)")
            elif _key_is_ours(info["lines"], info["key"]):
                rep.add("ok", "codex: [tui] status_line set by ctxhud (%s)" % ", ".join(_toml_items(info["lines"][info["key"]]) or ()))
            else:
                rep.add("warn", "codex: [tui] status_line exists but is not ctxhud's")
    except Conflict as exc:
        rep.add("warn", "codex: %s" % exc)

    conf = tmux_conf_path(None)
    try:
        conf_text = _read_text(conf)
        lines = (conf_text or "").splitlines(keepends=True)
        span = _block_range(lines)
        if span:
            rep.add("ok", "tmux: ctxhud block in %s" % _tilde(conf))
        else:
            rep.add("warn", "tmux: no ctxhud block in %s (run `ctxhud install --tools tmux`)" % _tilde(conf))
    except Conflict as exc:
        rep.add("fail", "tmux: %s" % exc)
    rc, live = _tmux("show-options", "-gwv", "pane-border-format")
    if rc == 127:
        rep.add("ok", "tmux live: server not running or tmux missing (skipped)")
    else:
        status_rc, status = _tmux("show-options", "-gwv", "pane-border-status")
        applied = "codex-border" in live and status == "bottom"
        rep.add("ok" if applied else "warn",
                "tmux live: pane-border-status=%s, ctxhud format %s%s" % (
                    status or "?", "active" if "codex-border" in live else "not loaded",
                    "" if applied else " (run `tmux source-file %s`)" % _tilde(conf)))
        wrc, windows = _tmux("list-windows", "-a", "-F", "#{session_name}:#{window_index}")
        overrides = []
        for target in windows.splitlines() if wrc == 0 else []:
            rc2, value = _tmux("show-options", "-w", "-v", "-t", target, "pane-border-status")
            if rc2 == 0 and value and value != "bottom":
                overrides.append("%s=%s" % (target, value))
        if overrides:
            rep.add("warn", "tmux live: window-level pane-border-status overrides the global setting in: %s "
                            "(unset with `tmux set-option -wu -t <window> pane-border-status`)" % ", ".join(overrides[:5]))

    out.write("%d problem(s), %d warning(s)\n" % (rep.fails, rep.warns))
    return 1 if rep.fails else 0


# ---------------------------------------------------------------- entry point

def _parser(command: str) -> "argparse.ArgumentParser":
    parser = argparse.ArgumentParser(prog="ctxhud " + command, add_help=True)
    if command == "doctor":
        return parser
    parser.add_argument("--dry-run", action="store_true", help="show the diff, write nothing")
    parser.add_argument("--tools", help="comma list of: %s (default: every tool found here)" % ", ".join(ALL_TOOLS))
    parser.add_argument("--tmux-conf", help="tmux config file to edit (default ~/.tmux.conf)")
    if command == "install":
        parser.add_argument("--force", action="store_true", help="replace an existing non-ctxhud statusLine (backed up)")
        parser.add_argument("--exe", help="absolute path of the ctxhud executable to write into configs")
        parser.add_argument("--refresh-interval", type=int, default=DEFAULT_REFRESH,
                            help="Claude statusLine refreshInterval in seconds (default %d)" % DEFAULT_REFRESH)
    return parser


def main(argv: "Optional[List[str]]" = None) -> int:
    args_in = list(sys.argv[1:] if argv is None else argv)
    command = args_in[0] if args_in and args_in[0] in ("install", "uninstall", "doctor") else "doctor"
    rest = args_in[1:] if args_in and args_in[0] == command else args_in
    try:
        args = _parser(command).parse_args(rest)
    except SystemExit as exc:
        return int(exc.code or 0)
    if command == "install" and args.refresh_interval < 1:
        sys.stdout.write("--refresh-interval must be at least 1\n")
        return 2
    try:
        handler = {"install": cmd_install, "uninstall": cmd_uninstall, "doctor": cmd_doctor}[command]
        return handler(args, sys.stdout)
    except Fatal as exc:
        sys.stdout.write("error: %s\n" % exc)
        return 1
