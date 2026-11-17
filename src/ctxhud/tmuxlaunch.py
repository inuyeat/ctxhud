"""Attach or create a tmux session named after a project folder (MIT)."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import List, Optional


def session_name(directory: str) -> str:
    """Keep folder names readable while removing tmux target separators."""
    name = os.path.basename(os.path.normpath(directory)) or "root"
    name = "".join("_" if char in ".:" or char.isspace() or not char.isprintable()
                   else char for char in name)
    if name.startswith("-"):
        name = "_" + name
    return name or "project"


def _exists(executable: str, name: str) -> bool:
    result = subprocess.run(
        [executable, "has-session", "-t", "=" + name],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=5, check=False,
    )
    return result.returncode == 0


def launch(directory: str) -> int:
    path = os.path.realpath(os.path.abspath(os.path.expanduser(directory)))
    if not os.path.isdir(path):
        raise ValueError("project directory does not exist or is not a directory")
    executable = shutil.which("tmux")
    if executable is None:
        raise FileNotFoundError("tmux is not installed or is not on PATH")
    name = session_name(path)
    if not os.environ.get("TMUX"):
        # -A performs atomic attach-or-create and preserves existing sessions.
        return subprocess.call([executable, "new-session", "-A", "-s", name, "-c", path])
    if not _exists(executable, name):
        created = subprocess.run(
            [executable, "new-session", "-d", "-s", name, "-c", path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5, check=False,
        )
        # Another launcher can win the creation race; switch to that session.
        if created.returncode != 0 and not _exists(executable, name):
            raise RuntimeError("cannot create tmux session")
    # Never use attach-session from a client already inside tmux or detach
    # another client as a side effect of an existing-session -Ad invocation.
    return subprocess.call([executable, "switch-client", "-t", "=" + name])


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "tmux":
        args = args[1:]
    parser = argparse.ArgumentParser(prog="ctxhud tmux", description=__doc__)
    parser.add_argument("directory", nargs="?", help="project directory (default: current directory)")
    try:
        options = parser.parse_args(args)
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        return launch(os.getcwd() if options.directory is None else options.directory)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        sys.stderr.write("ctxhud tmux: %s\n" % exc)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
