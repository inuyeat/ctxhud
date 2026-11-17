# Purpose: read public files below the repository root and its .gitignore.
"""Scan publishable files; extra literal deny terms come from CTXHUD_SCAN_EXTRA.

Git repositories use check-ignore --no-index (including tracked files). Before
git init, the fallback supports simple globs, directory rules, and negation.
It is deliberately not a complete implementation of Git's ignore grammar.

Only the literal home account placeholder ``user`` is allowed. Email domains
reserved for examples/testing are allowed. Intentional test values may use a
trailing ``# public-scan: allow`` preceded by a nonempty reason comment on the
same line, for example ``# Synthetic anonymization input. # public-scan: allow``.
"""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path
import re
import subprocess
import sys


HOME = re.compile(r"/" + r"(?:Users|home)/([^/\s\"'<>),;:\[\]{}\\`]+)")
EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
UUID = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}(?![0-9a-fA-F])")
FAKE_UUID = re.compile(r"00000000-0000-4000-8000-[0-9a-fA-F]{12}\Z")
ALLOW_MARKER = "# public-scan: allow"


def reserved_email_domain(domain: str) -> bool:
    domain = domain.lower()
    return domain in ("example.com", "example.net", "example.org") or any(
        domain == suffix or domain.endswith("." + suffix)
        for suffix in ("example", "invalid", "test", "localhost")
    )


def fallback_ignored(root: Path, relative: str, is_dir: bool) -> bool:
    """Apply simple .gitignore rules from root through the containing folder."""
    parts = relative.split("/")
    ignored = False
    for depth in range(len(parts)):
        ignore_file = root.joinpath(*parts[:depth], ".gitignore")
        if not ignore_file.is_file():
            continue
        local = "/".join(parts[depth:])
        for raw in ignore_file.read_text(encoding="utf-8").splitlines():
            rule = raw.rstrip()
            if not rule or rule.startswith("#"):
                continue
            negate = rule.startswith("!")
            if negate:
                rule = rule[1:]
            directory_only = rule.endswith("/")
            rule = rule.rstrip("/")
            anchored = rule.startswith("/")
            rule = rule.lstrip("/")
            if not rule:
                continue
            local_parts = local.split("/")
            candidates = ["/".join(local_parts[:i + 1]) for i in range(len(local_parts))]
            for index, candidate in enumerate(candidates):
                candidate_is_dir = index < len(local_parts) - 1 or is_dir
                if directory_only and not candidate_is_dir:
                    continue
                target = candidate if anchored or "/" in rule else local_parts[index]
                if fnmatch.fnmatchcase(target, rule):
                    ignored = not negate
    return ignored


class PublicFiles:
    def __init__(self, root: Path):
        self.root = root
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"], cwd=str(root),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
        except FileNotFoundError:
            self.git = False
        else:
            self.git = result.returncode == 0

    def ignored(self, paths, is_dir=False):
        if not paths:
            return set()
        if not self.git:
            return {p for p in paths if fallback_ignored(self.root, p, is_dir)}
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "-z", "--stdin"],
            cwd=str(self.root), input=b"\0".join(os.fsencode(p) for p in paths) + b"\0",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if result.returncode not in (0, 1):
            raise RuntimeError("git check-ignore failed")
        return {os.fsdecode(p) for p in result.stdout.split(b"\0") if p}

    def files(self):
        for directory, dirs, files in os.walk(str(self.root), followlinks=False):
            base = Path(directory)
            dirs.sort()
            files.sort()
            relative_dirs = [(base / name).relative_to(self.root).as_posix() for name in dirs if name != ".git"]
            excluded = self.ignored(relative_dirs, is_dir=True)
            dirs[:] = [name for name in dirs if name != ".git" and (base / name).relative_to(self.root).as_posix() not in excluded]
            relative_files = [(base / name).relative_to(self.root).as_posix() for name in files]
            excluded = self.ignored(relative_files)
            for name in relative_files:
                if name not in excluded:
                    yield name


def findings(line: str, extra):
    kinds = []
    if line.rstrip().endswith(ALLOW_MARKER):
        prefix = line.rstrip()[:-len(ALLOW_MARKER)]
        # A second comment introduces the reason; the final marker stays exact.
        if "#" in prefix and prefix.rsplit("#", 1)[1].strip():
            return []
        kinds.append("allow-marker-missing-reason")
    if any(match.group(1) != "user" for match in HOME.finditer(line)):
        kinds.append("absolute-home-path")
    if any(not reserved_email_domain(match.group(1)) for match in EMAIL.finditer(line)):
        kinds.append("email")
    if any(not FAKE_UUID.fullmatch(match.group()) for match in UUID.finditer(line)):
        kinds.append("uuid")
    if any(term in line for term in extra):
        kinds.append("extra-deny-term")
    return kinds


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) > 1:
        print("usage: public_scan.py [root]", file=sys.stderr)
        return 2
    root = Path(args[0]).resolve() if args else Path(__file__).resolve().parents[2]
    extra = [term.strip() for term in os.environ.get("CTXHUD_SCAN_EXTRA", "").split(",") if term.strip()]
    count = 0
    scanned = 0
    try:
        public = PublicFiles(root)
        for name in public.files():
            path = root / name
            # Inspect link text, never follow links into private local files.
            content = os.fsencode(os.readlink(str(path))) if path.is_symlink() else path.read_bytes()
            scanned += 1
            for number, line in enumerate(content.decode("utf-8", errors="replace").splitlines(), 1):
                kinds = findings(line, extra)
                if kinds:
                    print("{}:{}: {}".format(name, number, ", ".join(kinds)))
                    count += 1
    except (OSError, RuntimeError) as exc:
        print("public scan failed ({})".format(type(exc).__name__), file=sys.stderr)
        return 1
    mode = "git check-ignore" if public.git else "pre-git fallback"
    print("public scan: {} files, {} flagged lines ({})".format(scanned, count, mode))
    return 1 if count else 0


if __name__ == "__main__":
    sys.exit(main())
