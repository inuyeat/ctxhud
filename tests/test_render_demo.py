"""Tests for tools/render_demo.py and the committed docs/demo.svg."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from ctxhud import common  # noqa: E402

MODULE_PATH = ROOT / "tools" / "render_demo.py"
SPEC = importlib.util.spec_from_file_location("ctxhud_render_demo_tool", MODULE_PATH)
render_demo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(render_demo)


class RenderDemoTests(unittest.TestCase):
    def test_ignores_user_config_and_no_color(self):
        saved_cfg = os.environ.get("CTXHUD_CONFIG")
        saved_state = os.environ.get("CTXHUD_STATE_DIR")
        saved_nocolor = os.environ.get("NO_COLOR")
        saved_cache = common._config_cache
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_cfg = os.path.join(tmpdir, "bad.json")
            with open(bad_cfg, "w", encoding="utf-8") as fh:
                fh.write('{"icons":{"claude":"CUSTOM"},"icons_style":"nerd","color":false,"bars":false,"layout":"two-line"}')
            os.environ["CTXHUD_CONFIG"] = bad_cfg
            os.environ["CTXHUD_STATE_DIR"] = tmpdir
            os.environ["NO_COLOR"] = "1"
            common._config_cache = {"icons": {"claude": "DIRTY"}, "color": False}
            try:
                lines = render_demo.render_isolated_lines()
            finally:
                for key, val in (("CTXHUD_CONFIG", saved_cfg), ("CTXHUD_STATE_DIR", saved_state), ("NO_COLOR", saved_nocolor)):
                    if val is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = val
                common._config_cache = saved_cache

        self.assertEqual(len(lines), 3)
        rendered = [line for _, line in lines]
        self.assertTrue(rendered[0].startswith("\u273b "))
        self.assertTrue(rendered[1].startswith("\u25c8\x1b[1;33m(!)\x1b[0m "))
        self.assertTrue(rendered[2].startswith("\u25b2\x1b[1;31m(!)\x1b[0m "))
        for line in rendered:
            self.assertNotIn("\n", line)
            self.assertIn("\u2501" * 2, line)
            self.assertNotIn("\U000f0361", line)

    def test_committed_demo_svg_matches_generator_and_is_standalone(self):
        demo_svg = ROOT / "docs" / "demo.svg"
        self.assertTrue(demo_svg.is_file())
        content = demo_svg.read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "demo.svg"
            render_demo.generate_demo(out)
            self.assertEqual(content, out.read_text(encoding="utf-8"))

        root = ET.fromstring(content)
        self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")

        # Standalone check: no scripts, external URLs, local paths, emails, or foreignObject.
        lower = content.lower().replace('xmlns="http://www.w3.org/2000/svg"', "")
        for forbidden in ("<script", "foreignobject", "@import", "url(", "http://", "https://", "/users/", "/home/", "@"):
            self.assertNotIn(forbidden, lower)

        # Public default icons, unicode warning bubbles, heavy bars, and warning colors.
        for marker in ("\u273b", "\u25c8", "\u25b2", "(!)", "\u2501", render_demo._CTX_YELLOW, render_demo._QUOTA_RED):
            self.assertIn(marker, content)


if __name__ == "__main__":
    unittest.main()
