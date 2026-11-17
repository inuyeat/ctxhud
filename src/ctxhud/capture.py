"""`ctxhud capture <tool>`: record raw status line stdin for diagnostics.

Keeps the most recent 50 payloads per tool under <state>/raw/<tool>/ so that
call frequency (e.g. whether idle sessions are re-invoked) can be inspected.
Prints a short marker line and always exits 0.
"""
from __future__ import annotations

import os
import sys
import time
TYPE_CHECKING = False  # avoid importing typing at runtime (~20ms on uncached 3.9 stdlib)
if TYPE_CHECKING:
    from typing import List

from . import common

_KEEP = 50


def main(argv: List[str]) -> int:
    tool = argv[1] if len(argv) > 1 else "unknown"
    try:
        raw = sys.stdin.read()
        directory = common.ensure_dir(os.path.join(common.state_dir(), "raw", "capture-" + common._safe_name(tool)))
        name = "%d-%06d.json" % (time.time(), os.getpid() % 1000000)
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(raw)
        os.chmod(path, 0o600)
        files = sorted(f for f in os.listdir(directory) if f.endswith(".json"))
        for old in files[:-_KEEP]:
            os.unlink(os.path.join(directory, old))
    except Exception as exc:
        common.log_error("capture", exc)
    sys.stdout.write("ctxhud capture (%s)\n" % tool)
    return 0
