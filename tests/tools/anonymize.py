#!/usr/bin/env python3
"""Anonymize a captured status line / session JSON so it can be committed as a fixture.

    anonymize.py INPUT.json [-o OUTPUT.json] [--numbers coarse|keep]
    anonymize.py --check PATH [PATH ...]

What it does (deterministic, so re-running gives the same output):
* session / thread / conversation / prompt ids (any UUID-looking string) -> fake UUIDs
* working directories and project paths -> /home/user/project, other absolute
  paths -> the home prefix replaced by /home/user
* e-mail addresses, session titles, repository owner/name, branch names -> placeholders
* epoch timestamps are shifted by one constant so relative distances survive
* numbers: ``coarse`` (default) rounds percentages to the nearest 5 and other usage
  numbers to two significant digits; ``keep`` leaves them untouched (do not commit
  captures made with ``keep`` unless the values are not real usage)

``--check`` scans files/directories for leftovers (home paths, e-mails, non-fake
UUIDs) and exits 1 when something is found. Standard library only, Python >= 3.9.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple

FAKE_UUID_PREFIX = "00000000-0000-4000-8000-"
FAKE_EMAIL = "user@example.com"
FAKE_HOME = "/home/user"
FAKE_PROJECT = FAKE_HOME + "/project"
EPOCH_BASE = 1763370000  # arbitrary fixed base; earliest timestamp in a file maps here

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_HOME_PREFIX = re.compile(r"(?:/Users|/home)/[^/\s\"']+")
_HOME_ANY = re.compile(r"(?:/Users/|/home/|/private/var/|/var/folders/|/private/tmp/)")  # Regex alternatives detect paths; no account name. # public-scan: allow
# Claude Code encodes project paths as "-Users-<name>-..." in folder names.
# The whole folder segment (user name and project) is replaced, up to the next slash.
_ENCODED_HOME = re.compile(r"-(?:Users|home)-(?!user-project(?:/|$))[^/\s\"']*")

_CWD_KEYS = {"cwd", "original_cwd", "added_dirs", "worktree_path"}
_REDACT_KEYS = {"session_name": "session", "conversation_title": "conversation", "title": "title",
                "email": FAKE_EMAIL, "branch": "main", "original_branch": "main",
                "git_worktree": "worktree"}
_CONTEXT_KEYS = {("repo", "owner"): "owner", ("repo", "name"): "repo",
                 ("worktree", "name"): "worktree", ("worktree", "branch"): "main",
                 ("worktree", "path"): FAKE_HOME + "/project-worktree"}
_PERCENT_HINTS = ("percentage", "percent", "utilization")
_EPOCH_KEY_HINTS = ("_at", "reset_time", "timestamp")


class _State:
    def __init__(self, numbers: str) -> None:
        self.numbers = numbers
        self.uuids: Dict[str, str] = {}
        self.epoch_shift: Optional[int] = None

    def fake_uuid(self, original: str) -> str:
        key = original.lower()
        if key not in self.uuids:
            self.uuids[key] = "%s%012d" % (FAKE_UUID_PREFIX, len(self.uuids) + 1)
        return self.uuids[key]


# ---------------------------------------------------------------- strings

def _scrub_string(text: str, state: _State, key: str = "", parent: str = "") -> str:
    if (parent, key) in _CONTEXT_KEYS:
        return _CONTEXT_KEYS[(parent, key)]
    if key in _REDACT_KEYS:
        return _REDACT_KEYS[key]
    if key in _CWD_KEYS or key.endswith("_dir"):
        return FAKE_PROJECT
    if key == "url" and text.startswith("http"):
        return "https://example.com/owner/repo/pull/1"
    out = _UUID.sub(lambda m: state.fake_uuid(m.group(0)), text)
    out = _EMAIL.sub(FAKE_EMAIL, out)
    if key.endswith("_path"):
        match = _UUID.search(text)
        name = state.fake_uuid(match.group(0)) if match else "session"
        return "%s/.claude/projects/-home-user-project/%s.jsonl" % (FAKE_HOME, name)
    out = _HOME_PREFIX.sub(FAKE_HOME, out)
    out = _ENCODED_HOME.sub("-home-user-project", out)
    return out


# ---------------------------------------------------------------- numbers

def _round_sig(value: float, digits: int = 2) -> float:
    if value == 0 or not math.isfinite(value):
        return value
    magnitude = math.floor(math.log10(abs(value)))
    factor = 10 ** (digits - 1 - magnitude)
    return round(value * factor) / factor


def _epoch_seconds(key: str, value: Any) -> Optional[int]:
    """Seconds since the epoch if ``value`` under ``key`` looks like a timestamp, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not any(h in key for h in _EPOCH_KEY_HINTS):
        return None
    if 1_000_000_000 <= value < 4_000_000_000:
        return int(value)
    if 1_000_000_000_000 <= value < 4_000_000_000_000:  # milliseconds
        return int(value // 1000)
    return None


def _is_epoch(key: str, value: Any) -> bool:
    return _epoch_seconds(key, value) is not None


def _scrub_number(value: Any, key: str, state: _State) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    seconds = _epoch_seconds(key, value)
    if seconds is not None:
        if state.epoch_shift is None:
            state.epoch_shift = EPOCH_BASE - seconds
        millis = value >= 1_000_000_000_000
        return type(value)(value + state.epoch_shift * (1000 if millis else 1))
    if state.numbers == "keep":
        return value
    if "window" in key:
        return value  # a model property (context window size), not usage
    if any(h in key for h in _PERCENT_HINTS):
        rounded = round(value / 5.0) * 5
        return int(rounded) if isinstance(value, int) or float(value).is_integer() else float(rounded)
    rounded = _round_sig(float(value))  # every other number is treated as usage
    return int(rounded) if isinstance(value, int) else rounded


# ---------------------------------------------------------------- walk

def _first_epoch(obj: Any, key: str = "") -> Optional[int]:
    """Earliest epoch-like value in document order (keeps shifts deterministic)."""
    if isinstance(obj, dict):
        found = [e for e in (_first_epoch(v, k) for k, v in obj.items()) if e is not None]
        return min(found) if found else None
    if isinstance(obj, list):
        found = [e for e in (_first_epoch(v, key) for v in obj) if e is not None]
        return min(found) if found else None
    return _epoch_seconds(key, obj)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _keep_context_consistent(block: Dict[str, Any]) -> None:
    """After rounding, keep used/remaining/tokens of a context_window block coherent."""
    used = block.get("used_percentage")
    if not _is_number(used):
        return
    if _is_number(block.get("remaining_percentage")):
        block["remaining_percentage"] = 100 - used
    window = block.get("context_window_size")
    if _is_number(window) and _is_number(block.get("total_input_tokens")):
        block["total_input_tokens"] = int(round(used * window / 100.0))


def _walk(obj: Any, state: _State, key: str = "", parent: str = "") -> Any:
    if isinstance(obj, dict):
        out = {k: _walk(v, state, k, key) for k, v in obj.items()}
        if state.numbers != "keep":
            _keep_context_consistent(out)
        return out
    if isinstance(obj, list):
        return [_walk(v, state, key, parent) for v in obj]
    if isinstance(obj, str):
        return _scrub_string(obj, state, key, parent)
    return _scrub_number(obj, key, state)


def anonymize(data: Any, numbers: str = "coarse") -> Any:
    """Return an anonymized deep copy of ``data`` (JSON-compatible)."""
    if numbers not in ("coarse", "keep"):
        raise ValueError("numbers must be 'coarse' or 'keep'")
    state = _State(numbers)
    first = _first_epoch(data)
    if first is not None:
        state.epoch_shift = EPOCH_BASE - first
    return _walk(data, state)


# ---------------------------------------------------------------- leak check

def find_leaks(data: Any) -> List[str]:
    """Describe anything in ``data`` that should not be in a public fixture."""
    problems: List[str] = []

    def visit(obj: Any, where: str) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                visit(v, "%s.%s" % (where, k))
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                visit(v, "%s[%d]" % (where, i))
        elif isinstance(obj, str):
            for match in _UUID.finditer(obj):
                if not match.group(0).lower().startswith(FAKE_UUID_PREFIX):
                    problems.append("%s: non-fake UUID" % where)
            for match in _EMAIL.finditer(obj):
                if not match.group(0).endswith("@example.com"):
                    problems.append("%s: e-mail address" % where)
            for match in _HOME_ANY.finditer(obj):
                tail = obj[match.start():]
                if not tail.startswith(FAKE_HOME + "/") and tail != FAKE_HOME:
                    problems.append("%s: local path" % where)
                    break
            if _ENCODED_HOME.search(obj):
                problems.append("%s: encoded local path" % where)

    visit(data, "$")
    return problems


def _iter_json_files(paths: Iterable[str]) -> Iterable[str]:
    for path in paths:
        if os.path.isdir(path):
            for root, _dirs, files in os.walk(path):
                for name in sorted(files):
                    if name.endswith(".json"):
                        yield os.path.join(root, name)
        else:
            yield path


def check_paths(paths: Iterable[str]) -> List[Tuple[str, str]]:
    findings: List[Tuple[str, str]] = []
    for path in _iter_json_files(paths):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            findings.append((path, "unreadable: %s" % type(exc).__name__))
            continue
        for problem in find_leaks(data):
            findings.append((path, problem))
    return findings


# ---------------------------------------------------------------- CLI

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Anonymize captured JSON for use as a public fixture.")
    parser.add_argument("input", nargs="*", help="input JSON file ('-' for stdin); with --check: files or directories")
    parser.add_argument("-o", "--output", help="write here instead of stdout")
    parser.add_argument("--numbers", choices=("coarse", "keep"), default="coarse",
                        help="coarse (default) rounds usage numbers; keep leaves them as captured")
    parser.add_argument("--check", action="store_true", help="scan for leftovers instead of converting")
    args = parser.parse_args(argv)

    if args.check:
        if not args.input:
            parser.error("--check needs at least one path")
        findings = check_paths(args.input)
        for path, problem in findings:
            print("%s: %s" % (path, problem))
        return 1 if findings else 0

    if len(args.input) != 1:
        parser.error("exactly one input file is required")
    source = args.input[0]
    try:
        if source == "-":
            data = json.load(sys.stdin)
        else:
            with open(source, "r", encoding="utf-8") as fh:
                data = json.load(fh)
    except (OSError, ValueError) as exc:
        print("cannot read input: %s" % type(exc).__name__, file=sys.stderr)
        return 2

    text = json.dumps(anonymize(data, args.numbers), indent=2, ensure_ascii=False, sort_keys=False) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
