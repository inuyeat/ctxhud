#!/usr/bin/env python3
"""Generate docs/demo.svg from synthetic demo data using ctxhud.render.render_line.

Runs completely isolated from user configuration and state: overrides
CTXHUD_CONFIG to an empty temporary file and CTXHUD_STATE_DIR to a temporary
directory, clears NO_COLOR, and uses only public generic default icons.
Standard library only; Python >= 3.9.
"""
from __future__ import annotations

import argparse
import html
import os
from pathlib import Path
import re
import sys
import tempfile

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any, Dict, List, Optional, Tuple

_ROOT = Path(__file__).resolve().parents[1]
_SRC = str(_ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from ctxhud import common, render  # noqa: E402

DEMO_NOW = 1_800_000_000

_ANSI_SGR = re.compile(r"\x1b\[([0-9;]*)m")

_BG = "#0d1117"
_CARD_BG = "#161b22"
_BORDER = "#30363d"
_SUB_BORDER = "#21262d"
_FG = "#e6edf3"
_MUTED = "#8b949e"
_DIM = "#545d68"
_CTX_YELLOW = "#f2cc60"
_QUOTA_RED = "#ff7b72"
_FONT_FAMILY = (
    "ui-monospace, 'SFMono-Regular', Menlo, Monaco, Consolas, "
    "'Liberation Mono', 'DejaVu Sans Mono', monospace"
)


def demo_scenarios(now: int = DEMO_NOW) -> List[Tuple[str, Dict[str, Any]]]:
    """Return synthetic (label, cache_entry) pairs covering 3 tools and 3 states."""
    return [
        (
            "# Claude Code \u2014 normal",
            {
                "schema": 1,
                "tool": "claude",
                "session_id": "demo-claude-1",
                "model": "Opus 5.5",
                "effort": "high",
                "ctx": {"used_pct": 31.0, "source": "native"},
                "quota": {
                    "group": "claude",
                    "five_hour": {
                        "used_pct": 41.0,
                        "resets_at": now + 5400,
                        "observed_at": now,
                    },
                    "weekly": {
                        "used_pct": 18.0,
                        "resets_at": now + 345600,
                        "observed_at": now,
                    },
                },
                "observed_at": now,
            },
        ),
        (
            "# Codex CLI \u2014 context warning (CTX >= 50%)",
            {
                "schema": 1,
                "tool": "codex",
                "session_id": "demo-codex-1",
                "model": "gpt-6.1-sol",
                "effort": "medium",
                "ctx": {"used_pct": 62.0, "source": "native"},
                "quota": {
                    "group": "codex",
                    "five_hour": {
                        "used_pct": 35.0,
                        "resets_at": now + 7200,
                        "observed_at": now,
                    },
                    "weekly": {
                        "used_pct": 22.0,
                        "resets_at": now + 432000,
                        "observed_at": now,
                    },
                },
                "observed_at": now,
            },
        ),
        (
            "# Antigravity CLI \u2014 quota warning (5h >= 80%)",
            {
                "schema": 1,
                "tool": "agy",
                "session_id": "demo-agy-1",
                "model": "Gemini 4 Hydrogen (High)",
                "effort": None,
                "ctx": {"used_pct": 28.0, "source": "native"},
                "quota": {
                    "group": "gemini",
                    "five_hour": {
                        "used_pct": 84.0,
                        "resets_at": now + 2460,
                        "observed_at": now,
                    },
                    "weekly": {
                        "used_pct": 27.0,
                        "resets_at": now + 259200,
                        "observed_at": now,
                    },
                },
                "observed_at": now,
            },
        ),
    ]


def render_isolated_lines(now: int = DEMO_NOW) -> List[Tuple[str, str]]:
    """Render each scenario with CTXHUD_CONFIG pointed to an empty temp file."""
    saved = {k: os.environ.get(k) for k in ("CTXHUD_CONFIG", "CTXHUD_STATE_DIR", "NO_COLOR")}
    saved_cache = common._config_cache
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            empty_cfg = os.path.join(tmpdir, "empty-config.json")
            with open(empty_cfg, "w", encoding="utf-8"):
                pass
            os.environ["CTXHUD_CONFIG"] = empty_cfg
            os.environ["CTXHUD_STATE_DIR"] = tmpdir
            os.environ.pop("NO_COLOR", None)
            common._config_cache = None

            lines: List[Tuple[str, str]] = []
            for label, entry in demo_scenarios(now):
                lines.append((label, render.render_line(entry, style="ansi", now=now)))
            return lines
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        common._config_cache = saved_cache


def parse_ansi_runs(ansi_text: str) -> List[Tuple[str, str, bool]]:
    """Convert ANSI SGR text into merged (text, hex_color, bold) spans."""
    runs: List[Tuple[str, str, bool]] = []
    fg = _FG
    bold = False
    pos = 0

    def push(chunk: str) -> None:
        if not chunk:
            return
        if runs and runs[-1][1] == fg and runs[-1][2] == bold:
            runs[-1] = (runs[-1][0] + chunk, fg, bold)
        else:
            runs.append((chunk, fg, bold))

    for match in _ANSI_SGR.finditer(ansi_text):
        push(ansi_text[pos:match.start()])
        pos = match.end()
        codes = [c for c in match.group(1).split(";") if c != ""]
        if not codes:
            fg, bold = _FG, False
            continue
        i = 0
        while i < len(codes):
            code = codes[i]
            if code == "0":
                fg, bold = _FG, False
            elif code == "1":
                bold = True
            elif code == "2":
                fg = _DIM
            elif code == "31":
                fg = _QUOTA_RED
            elif code == "33":
                fg = _CTX_YELLOW
            elif code == "38" and i + 4 < len(codes) and codes[i + 1] == "2":
                r, g, b = int(codes[i + 2]), int(codes[i + 3]), int(codes[i + 4])
                fg = "#%02x%02x%02x" % (r, g, b)
                i += 4
            i += 1

    push(ansi_text[pos:])
    return runs


def _runs_to_tspans(runs: List[Tuple[str, str, bool]]) -> str:
    parts: List[str] = []
    for text, fg, bold in runs:
        attrs = []
        if fg != _FG:
            attrs.append('fill="%s"' % fg)
        if bold:
            attrs.append('font-weight="700"')
        attr_str = (" " + " ".join(attrs)) if attrs else ""
        parts.append("<tspan%s>%s</tspan>" % (attr_str, html.escape(text, quote=False)))
    return "".join(parts)


def build_svg(lines: List[Tuple[str, str]]) -> str:
    """Build a standalone dark-terminal SVG with no external fonts or scripts."""
    width = 800
    header_h = 36
    block_h = 62
    top_pad = 58
    height = top_pad + len(lines) * block_h + 6

    body: List[str] = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'viewBox="0 0 %d %d" role="img" aria-label="ctxhud terminal status line demo">'
        % (width, height, width, height),
        '  <rect x="0.5" y="0.5" width="%d" height="%d" rx="12" fill="%s" stroke="%s"/>'
        % (width - 1, height - 1, _BG, _BORDER),
        '  <line x1="1" y1="%d" x2="%d" y2="%d" stroke="%s"/>'
        % (header_h, width - 1, header_h, _SUB_BORDER),
        '  <circle cx="20" cy="18" r="5.5" fill="#ff5f56"/>',
        '  <circle cx="38" cy="18" r="5.5" fill="#ffbd2e"/>',
        '  <circle cx="56" cy="18" r="5.5" fill="#27c93f"/>',
        '  <text x="%d" y="22" text-anchor="middle" fill="%s" '
        'font-family="%s" font-size="12">ctxhud \u2014 status line</text>'
        % (width // 2, _MUTED, html.escape(_FONT_FAMILY, quote=True)),
    ]

    font_attr = html.escape(_FONT_FAMILY, quote=True)
    for idx, (label, ansi_line) in enumerate(lines):
        label_y = top_pad + idx * block_h
        card_y = label_y + 8
        text_y = card_y + 20
        tspans = _runs_to_tspans(parse_ansi_runs(ansi_line))
        body.append(
            '  <text x="24" y="%d" fill="%s" font-family="%s" font-size="12">%s</text>'
            % (label_y, _MUTED, font_attr, html.escape(label, quote=False))
        )
        body.append(
            '  <rect x="20" y="%d" width="%d" height="29" rx="6" fill="%s" stroke="%s"/>'
            % (card_y, width - 40, _CARD_BG, _SUB_BORDER)
        )
        body.append(
            '  <text x="32" y="%d" xml:space="preserve" fill="%s" font-family="%s" '
            'font-size="13.5">%s</text>'
            % (text_y, _FG, font_attr, tspans)
        )

    body.append("</svg>")
    return "\n".join(body) + "\n"


def generate_demo(output_path: Optional[Path] = None, now: int = DEMO_NOW) -> Path:
    target = Path(output_path) if output_path is not None else (_ROOT / "docs" / "demo.svg")
    target.parent.mkdir(parents=True, exist_ok=True)
    svg = build_svg(render_isolated_lines(now=now))
    target.write_text(svg, encoding="utf-8")
    return target


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Generate README demo SVG using ctxhud.render.")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=_ROOT / "docs" / "demo.svg",
        help="output SVG path (default: docs/demo.svg)",
    )
    args = parser.parse_args(argv)
    generate_demo(args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
