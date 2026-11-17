import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from ctxhud import common, render  # noqa: E402

NOW = 1_800_000_000


def entry(tool="claude", sid="s1", ctx=None, q5=None, q7=None, group="claude", obs=NOW):
    quota = None
    if q5 is not None or q7 is not None:
        quota = {"group": group,
                 "five_hour": None if q5 is None else {"used_pct": q5, "resets_at": NOW + 2460, "observed_at": obs},
                 "weekly": None if q7 is None else {"used_pct": q7, "resets_at": NOW + 300000, "observed_at": obs}}
    return {"schema": 1, "tool": tool, "session_id": sid, "model": "m",
            "ctx": {"used_pct": ctx, "source": "native"}, "quota": quota, "observed_at": obs}


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CTXHUD_STATE_DIR"] = self.tmp.name
        os.environ["CTXHUD_CONFIG"] = os.path.join(self.tmp.name, "none.json")
        common._config_cache = None

    def tearDown(self):
        self.tmp.cleanup()
        common._config_cache = None

    def line(self, e, **kw):
        return render.render_line(e, style="plain", now=NOW, **kw)

    def test_threshold_floor(self):
        self.assertIn("CTX 49% ", self.line(entry(ctx=49.9)))
        self.assertIn("CTX 50%!", self.line(entry(ctx=50.0)))
        self.assertIn("5h 79% ", self.line(entry(ctx=1, q5=79.9)))
        self.assertIn("5h 80%!", self.line(entry(ctx=1, q5=80.0)))

    def test_bars_only_ctx_and_5h(self):
        out = self.line(entry(ctx=62, q5=41, q7=18))
        L = "\u2501"
        self.assertIn("CTX 62%! " + L * 10, out)
        self.assertIn("5h 41% " + L * 10, out)
        self.assertTrue(out.endswith("7d 18%"))

    def test_bar_width_constant(self):
        for pct in (0, 1, 2.5, 33.3, 50, 87.5, 99.9, 100):
            fill, track = render._bar(pct, 10)
            self.assertEqual(len(fill) + len(track), 10, pct)
        self.assertEqual(len(render._bar(62, 10)[0]), 6)
        self.assertEqual(len(render._bar(99.9, 10)[0]), 9)

    def set_config(self, **kw):
        import json
        with open(os.environ["CTXHUD_CONFIG"], "w") as fh:
            json.dump(kw, fh)
        common._config_cache = None

    def test_default_is_one_line(self):
        self.assertNotIn("\n", self.line(entry(ctx=62, q5=84, q7=18)))

    def test_two_line_layout(self):
        self.set_config(layout="two-line")
        out = self.line(entry(ctx=62, q5=84, q7=18))
        first, second = out.split("\n")
        self.assertIn("CTX 62%!", first)
        self.assertNotIn("5h", first)
        self.assertTrue(second.lstrip().startswith("5h 84%!"))
        self.assertIn("7d 18%", second)

    def test_tmux_always_one_line(self):
        out = render.render_line(entry(ctx=62, q5=84, q7=18), style="tmux", now=NOW)
        self.assertNotIn("\n", out)

    def test_narrow_drops_bars_first(self):
        out = self.line(entry(ctx=62, q5=84, q7=18), width=40)
        self.assertNotIn("\u2501", out)
        self.assertIn("CTX 62%!", out)
        self.assertIn("5h 84%! 41m", out)

    def test_reset_never_zero(self):
        e = entry(ctx=10, q5=30)
        e["quota"]["five_hour"]["resets_at"] = NOW - 1
        self.assertIn("5h --[reset]", self.line(e))

    def test_weekly_days(self):
        self.assertIn("7d 83%! 3d", self.line(entry(ctx=1, q7=83)))

    def test_pick_quota_group_filter(self):
        common.write_cache(entry(tool="agy", sid="a", ctx=1, q5=10, group="gemini", obs=NOW - 100))
        common.write_cache(entry(tool="agy", sid="b", ctx=1, q5=90, group="3p", obs=NOW))
        self.assertEqual(render.pick_quota("agy", "gemini")["five_hour"]["used_pct"], 10)
        self.assertEqual(render.pick_quota("agy", "3p")["five_hour"]["used_pct"], 90)
        self.assertEqual(render.pick_quota("agy")["five_hour"]["used_pct"], 90)

    def test_pick_quota_newest_not_max(self):
        common.write_cache(entry(sid="old", ctx=1, q5=70, obs=NOW - 600))
        common.write_cache(entry(sid="new", ctx=1, q5=60, obs=NOW))
        self.assertEqual(render.pick_quota("claude")["five_hour"]["used_pct"], 60)

    def test_bubble_tone(self):
        os.environ["NO_COLOR"] = ""
        e = entry(tool="codex", ctx=62, q5=84)
        out = render.render_line(e, style="tmux", now=NOW)
        self.assertTrue(out.startswith("◈#[fg=red,bold](!)"))

    def test_short_model(self):
        m = render.short_model
        self.assertEqual(m("Opus 5.5", "high"), "Opus 5.5\u00b7high")
        self.assertEqual(m("gpt-6.1-sol", "medium"), "6.1-sol\u00b7med")
        self.assertEqual(m("Gemini 4 Hydrogen (High)"), "Hydrogen\u00b7high")
        self.assertEqual(m("Claude Sonnet 5.5 (Low)"), "Sonnet 5.5\u00b7low")
        self.assertEqual(m("Sonnet 5.5"), "Sonnet 5.5")

    def test_never_raises(self):
        self.assertEqual(render.render_line({"ctx": "garbage"}, style="plain", now=NOW).startswith("CTX"), True)


if __name__ == "__main__":
    unittest.main()
