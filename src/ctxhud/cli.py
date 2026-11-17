"""Command dispatch. Each subcommand module exposes main(argv) -> int."""
from __future__ import annotations

import importlib
import sys
TYPE_CHECKING = False  # avoid importing typing at runtime (~20ms on uncached 3.9 stdlib)
if TYPE_CHECKING:
    from typing import List, Optional

USAGE = """usage: ctxhud <command> [args]

commands:
  bridge claude|agy        status line command (reads stdin JSON)
  codex-border <pane_id>   tmux pane-border-format helper for Codex
  install / uninstall      wire ctxhud into agent and tmux configs
  doctor                   check paths, configs and recent caches
  tmux [dir]               attach or create a tmux session for a project
  list                     show all cached sessions
  capture <tool>           record raw status line stdin (diagnostics)
"""

_ROUTES = {
    ("bridge", "claude"): "ctxhud.bridge_claude",
    ("bridge", "agy"): "ctxhud.bridge_agy",
    ("codex-border",): "ctxhud.codex",
    ("install",): "ctxhud.install",
    ("uninstall",): "ctxhud.install",
    ("doctor",): "ctxhud.install",
    ("tmux",): "ctxhud.tmuxlaunch",
    ("list",): "ctxhud.render",
    ("capture",): "ctxhud.capture",
}


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        sys.stdout.write(USAGE)
        return 0
    for key, module in _ROUTES.items():
        if tuple(args[: len(key)]) == key:
            mod = importlib.import_module(module)
            return int(mod.main(args) or 0)
    sys.stdout.write(USAGE)
    return 2
