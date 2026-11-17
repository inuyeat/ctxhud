"""Tests for ctxhud.bridge_claude (Python 3.9 compatible, standard library only).

Run: PYTHONPATH=src /usr/bin/python3 -I -m unittest discover -s tests
All fixtures are invented data or anonymized captures (see tests/tools/anonymize.py).
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from ctxhud import bridge_claude, common  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures", "claude")
NOW = 1763370000


def load_fixture(name):
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


class StubRender(types.ModuleType):
    """Stands in for ctxhud.render so these tests do not depend on its implementation."""

    def __init__(self, line="STUB LINE", quota=None, fail=False):
        super().__init__("ctxhud.render")
        self.line = line
        self.quota = quota
        self.fail = fail
        self.calls = []

    def render_line(self, cache, quota=None, width=None, style="ansi", now=None):
        self.calls.append({"cache": cache, "quota": quota, "width": width, "style": style})
        if self.fail:
            raise RuntimeError("render exploded")
        return self.line

    def pick_quota(self, tool):
        return self.quota


class BridgeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ctxhud-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = os.path.join(self.tmp, "state")
        patcher = mock.patch.dict(os.environ, {
            "CTXHUD_STATE_DIR": self.state,
            "CTXHUD_CONFIG": os.path.join(self.tmp, "config.json"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("TMUX_PANE", "COLUMNS", "NO_COLOR"):
            os.environ.pop(name, None)
        common._config_cache = None
        self.addCleanup(setattr, common, "_config_cache", None)

    def run_main(self, stdin_text, argv=None, render=None):
        """Run bridge_claude.main with captured stdio; returns (rc, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        modules = {"ctxhud.render": render}
        with mock.patch.dict(sys.modules, modules), \
                mock.patch("sys.stdin", io.StringIO(stdin_text)), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = bridge_claude.main(argv if argv is not None else ["bridge", "claude"])
        return rc, out.getvalue(), err.getvalue()

    def cache_file(self, session_id):
        return common.cache_path("claude", session_id)

    def write_config(self, data):
        with open(os.environ["CTXHUD_CONFIG"], "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        common._config_cache = None


class NormalizeTests(unittest.TestCase):
    def test_basic_payload(self):
        entry = bridge_claude.normalize(load_fixture("statusline_basic.json"), now_ts=NOW, pid=4321,
                                        tmux_pane="%3")
        self.assertEqual(entry["schema"], 1)
        self.assertEqual(entry["tool"], "claude")
        self.assertEqual(entry["session_id"], "00000000-0000-4000-8000-0000000000a1")
        self.assertEqual(entry["pid"], 4321)
        self.assertEqual(entry["cwd"], "/home/user/project")
        self.assertEqual(entry["model"], "Example 1")
        self.assertEqual(entry["tmux_pane"], "%3")
        self.assertEqual(entry["ctx"], {"used_pct": 50.0, "tokens": 100000, "window": 200000,
                                        "source": "native", "formula": "claude.statusline.used_percentage"})
        self.assertEqual(entry["quota"]["group"], "claude")
        self.assertEqual(entry["quota"]["five_hour"],
                         {"used_pct": 41.0, "resets_at": 1763380800, "observed_at": NOW})
        self.assertEqual(entry["quota"]["weekly"],
                         {"used_pct": 18.0, "resets_at": 1763900000, "observed_at": NOW})
        self.assertEqual(entry["observed_at"], NOW)
        self.assertEqual(entry["received_at"], NOW)

    def test_only_whitelisted_fields_are_stored(self):
        entry = bridge_claude.normalize(load_fixture("statusline_basic.json"), now_ts=NOW)
        self.assertEqual(set(entry), {"schema", "tool", "session_id", "pid", "cwd", "model", "effort",
                                      "tmux_pane", "ctx", "quota", "observed_at", "received_at"})
        text = json.dumps(entry)
        for secret in ("example session", "transcript", "total_cost_usd", "0.0.0-fixture"):
            self.assertNotIn(secret, text)

    def test_early_session_keeps_unknowns_as_none(self):
        entry = bridge_claude.normalize(load_fixture("statusline_early.json"), now_ts=NOW)
        self.assertIsNone(entry["ctx"]["used_pct"])      # never 0
        self.assertIsNone(entry["ctx"]["tokens"])        # 0 tokens before the first reply = unknown
        self.assertEqual(entry["ctx"]["window"], 200000)
        self.assertEqual(entry["ctx"]["source"], "none")
        self.assertIsNone(entry["quota"])
        self.assertIsNone(entry["pid"])
        self.assertIsNone(entry["tmux_pane"])

    def test_weekly_only_plan(self):
        entry = bridge_claude.normalize(load_fixture("statusline_weekly_only.json"), now_ts=NOW)
        self.assertIsNone(entry["quota"]["five_hour"])
        self.assertEqual(entry["quota"]["weekly"]["used_pct"], 63.0)
        self.assertEqual(entry["cwd"], "/home/user/project")        # from workspace.current_dir
        self.assertEqual(entry["model"], "example-model-2")         # falls back to model.id
        self.assertEqual(entry["ctx"]["window"], 1000000)

    def test_hostile_values_are_sanitized_not_trusted(self):
        entry = bridge_claude.normalize(load_fixture("statusline_hostile.json"), now_ts=NOW)
        for text in (entry["cwd"], entry["model"]):
            self.assertFalse(any(ord(c) < 32 or 0x7f <= ord(c) <= 0x9f for c in text), repr(text))
        self.assertEqual(entry["ctx"]["used_pct"], 100.0)   # clamped
        self.assertIsNone(entry["ctx"]["tokens"])           # negative
        self.assertIsNone(entry["ctx"]["window"])           # zero
        self.assertIsNone(entry["quota"])                   # bool / negative percentages rejected

    def test_effort_is_taken_from_effort_level(self):
        entry = bridge_claude.normalize(load_fixture("statusline_basic.json"), now_ts=NOW)
        self.assertEqual(entry["effort"], "high")
        for name in ("real_opus_low.json", "real_sonnet_high.json"):
            self.assertEqual(bridge_claude.normalize(load_fixture(name), now_ts=NOW)["effort"], "medium", name)

    def test_effort_is_none_when_the_model_reports_none(self):
        for name in ("statusline_early.json", "statusline_weekly_only.json", "statusline_hostile.json"):
            entry = bridge_claude.normalize(load_fixture(name), now_ts=NOW)
            self.assertIn("effort", entry)
            self.assertIsNone(entry["effort"], name)           # never a default like "medium"

    def test_every_known_level_is_accepted_and_normalized(self):
        for given, expected in (("low", "low"), ("medium", "medium"), ("high", "high"), ("xhigh", "xhigh"),
                                ("max", "max"), ("HIGH", "high"), ("  Max ", "max")):
            payload = {"session_id": "s", "effort": {"level": given}}
            self.assertEqual(bridge_claude.normalize(payload, now_ts=NOW)["effort"], expected, given)

    def test_unknown_or_malformed_effort_becomes_none(self):
        bad = ({"level": "ultra"}, {"level": ""}, {"level": None}, {"level": True}, {"level": 3},
               {"level": ["high"]}, {"level": "high\u001b[31m"}, {"level": "hi\ngh"}, {"lvl": "high"}, {},
               "high", ["high"], 5, None, True)
        for effort in bad:
            payload = {"session_id": "s", "effort": effort}
            self.assertIsNone(bridge_claude.normalize(payload, now_ts=NOW)["effort"], repr(effort))
        self.assertIsNone(bridge_claude.normalize({"session_id": "s"}, now_ts=NOW)["effort"])

    def test_effort_does_not_change_the_model_name_which_stays_unshortened(self):
        payload = {"session_id": "s", "model": {"id": "x", "display_name": "Claude Sonnet 5.5 (High)"},
                   "effort": {"level": "max"}}
        entry = bridge_claude.normalize(payload, now_ts=NOW)
        self.assertEqual(entry["model"], "Claude Sonnet 5.5 (High)")
        self.assertEqual(entry["effort"], "max")

    def test_effort_follows_the_current_input_and_is_not_carried_forward(self):
        payload = load_fixture("statusline_basic.json")
        first = bridge_claude.normalize(payload, now_ts=NOW)
        del payload["effort"]                                   # e.g. switched to a model without effort
        second = bridge_claude.normalize(payload, now_ts=NOW + 5, previous=first)
        self.assertEqual(first["effort"], "high")
        self.assertIsNone(second["effort"])
        payload["effort"] = {"level": "low"}
        self.assertEqual(bridge_claude.normalize(payload, now_ts=NOW + 9, previous=second)["effort"], "low")

    def test_observed_w_when_only_tokens_and_window_are_given(self):
        payload = {"session_id": "s", "context_window": {"total_input_tokens": 50000, "context_window_size": 200000}}
        ctx = bridge_claude.normalize(payload, now_ts=NOW)["ctx"]
        self.assertEqual(ctx["source"], "observed_w")
        self.assertAlmostEqual(ctx["used_pct"], 25.0)

    def test_non_dict_sections_do_not_crash(self):
        payload = {"session_id": "s", "context_window": "oops", "rate_limits": [1, 2], "model": 5, "workspace": None}
        entry = bridge_claude.normalize(payload, now_ts=NOW)
        self.assertIsNone(entry["ctx"]["used_pct"])
        self.assertIsNone(entry["quota"])
        self.assertIsNone(entry["model"])

    def test_invalid_pid_is_none(self):
        for bad in (0, -1, "12", True):
            self.assertIsNone(bridge_claude.normalize({"session_id": "s"}, now_ts=NOW, pid=bad)["pid"], bad)

    def test_unchanged_quota_keeps_its_observation_time(self):
        payload = load_fixture("statusline_basic.json")
        first = bridge_claude.normalize(payload, now_ts=NOW)
        again = bridge_claude.normalize(payload, now_ts=NOW + 600, previous=first)
        self.assertEqual(again["quota"]["five_hour"]["observed_at"], NOW)     # re-sent value, not a new observation
        self.assertEqual(again["received_at"], NOW + 600)

    def test_changed_quota_gets_a_new_observation_time(self):
        payload = load_fixture("statusline_basic.json")
        first = bridge_claude.normalize(payload, now_ts=NOW)
        payload["rate_limits"]["five_hour"]["used_percentage"] = 42
        second = bridge_claude.normalize(payload, now_ts=NOW + 60, previous=first)
        self.assertEqual(second["quota"]["five_hour"]["observed_at"], NOW + 60)
        self.assertEqual(second["quota"]["weekly"]["observed_at"], NOW)       # weekly untouched

    def test_window_that_disappears_is_carried_forward_unchanged(self):
        payload = load_fixture("statusline_basic.json")
        first = bridge_claude.normalize(payload, now_ts=NOW)
        del payload["rate_limits"]["five_hour"]                               # Claude drops a window after its reset
        later = bridge_claude.normalize(payload, now_ts=NOW + 99999, previous=first)
        self.assertEqual(later["quota"]["five_hour"], first["quota"]["five_hour"])
        self.assertEqual(later["quota"]["five_hour"]["resets_at"], 1763380800)

    def test_previous_garbage_is_ignored(self):
        for junk in ("x", 5, [], {"quota": "bad"}, {"quota": {"five_hour": "bad"}}):
            entry = bridge_claude.normalize(load_fixture("statusline_basic.json"), now_ts=NOW, previous=junk)
            self.assertEqual(entry["quota"]["five_hour"]["observed_at"], NOW)

    def test_anonymized_real_captures_normalize(self):
        for name in ("real_opus_low.json", "real_sonnet_high.json"):
            entry = bridge_claude.normalize(load_fixture(name), now_ts=NOW)
            self.assertEqual(entry["ctx"]["source"], "native")
            self.assertEqual(entry["ctx"]["window"], 1000000)
            self.assertIsNotNone(entry["quota"]["five_hour"]["resets_at"])
            self.assertIsNotNone(entry["quota"]["weekly"]["used_pct"])


class MainTests(BridgeTestCase):
    def test_end_to_end_prints_the_rendered_line_and_writes_cache(self):
        stub = StubRender(line="MODEL | CTX 50%!")
        rc, out, err = self.run_main(json.dumps(load_fixture("statusline_basic.json")), render=stub)
        self.assertEqual((rc, out, err), (0, "MODEL | CTX 50%!\n", ""))
        cached = common.read_json(self.cache_file("00000000-0000-4000-8000-0000000000a1"))
        self.assertEqual(cached["ctx"]["used_pct"], 50.0)
        self.assertEqual(cached["pid"], os.getppid())

    def test_render_receives_documented_arguments(self):
        stub = StubRender(quota={"five_hour": None})
        os.environ["COLUMNS"] = "97"
        self.run_main(json.dumps(load_fixture("statusline_basic.json")), render=stub)
        call = stub.calls[0]
        self.assertEqual((call["width"], call["style"]), (97, "ansi"))
        self.assertEqual(call["quota"], {"five_hour": None})
        self.assertEqual(call["cache"]["tool"], "claude")
        self.assertEqual((call["cache"]["model"], call["cache"]["effort"]), ("Example 1", "high"))

    def test_effort_is_written_to_the_cache_file(self):
        self.run_main(json.dumps(load_fixture("statusline_basic.json")), render=StubRender())
        cached = common.read_json(self.cache_file("00000000-0000-4000-8000-0000000000a1"))
        self.assertEqual((cached["model"], cached["effort"]), ("Example 1", "high"))
        self.run_main(json.dumps(load_fixture("statusline_early.json")), render=StubRender())
        self.assertIsNone(common.read_json(self.cache_file("00000000-0000-4000-8000-0000000000a2"))["effort"])

    def test_color_off_uses_plain_style(self):
        self.write_config({"color": False})
        stub = StubRender()
        self.run_main(json.dumps(load_fixture("statusline_basic.json")), render=stub)
        self.assertEqual(stub.calls[0]["style"], "plain")

    def test_cache_is_written_before_render_so_pick_quota_sees_it(self):
        seen = {}

        class Peek(StubRender):
            def pick_quota(inner, tool):
                seen["files"] = [n for n in os.listdir(os.path.join(self.state, "cache", "claude"))
                                 if not n.startswith(".")]
                return None

        self.run_main(json.dumps(load_fixture("statusline_basic.json")), render=Peek())
        self.assertEqual(len(seen["files"]), 1)

    def test_cache_permissions(self):
        self.run_main(json.dumps(load_fixture("statusline_basic.json")), render=StubRender())
        path = self.cache_file("00000000-0000-4000-8000-0000000000a1")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)

    def test_tmux_pane_comes_from_the_environment(self):
        os.environ["TMUX_PANE"] = "%7"
        self.run_main(json.dumps(load_fixture("statusline_basic.json")), render=StubRender())
        self.assertEqual(common.read_json(self.cache_file("00000000-0000-4000-8000-0000000000a1"))["tmux_pane"], "%7")

    def test_second_call_preserves_quota_observation(self):
        text = json.dumps(load_fixture("statusline_basic.json"))
        self.run_main(text, render=StubRender())
        path = self.cache_file("00000000-0000-4000-8000-0000000000a1")
        first = common.read_json(path)
        first["quota"]["five_hour"]["observed_at"] = 123       # pretend it was observed long ago
        common.atomic_write_json(path, first)
        self.run_main(text, render=StubRender())
        self.assertEqual(common.read_json(path)["quota"]["five_hour"]["observed_at"], 123)

    def test_missing_renderer_falls_back_to_a_plain_line(self):
        rc, out, err = self.run_main(json.dumps(load_fixture("statusline_basic.json")), render=None)
        self.assertEqual((rc, out, err), (0, "Example 1 | CTX 50%\n", ""))

    def test_early_session_fallback_shows_unknown_not_zero(self):
        rc, out, _ = self.run_main(json.dumps(load_fixture("statusline_early.json")), render=None)
        self.assertEqual(out, "Example 1 | CTX --\n")

    def test_renderer_exception_never_escapes_or_reaches_stderr(self):
        rc, out, err = self.run_main(json.dumps(load_fixture("statusline_basic.json")),
                                     render=StubRender(fail=True))
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        self.assertEqual(out, "Example 1 | CTX 50%\n")
        with open(os.path.join(self.state, "log", "ctxhud.log"), "r", encoding="utf-8") as fh:
            self.assertIn("bridge_claude.render", fh.read())

    def test_empty_renderer_output_falls_back(self):
        rc, out, _ = self.run_main(json.dumps(load_fixture("statusline_basic.json")), render=StubRender(line="  "))
        self.assertEqual(out, "Example 1 | CTX 50%\n")

    def test_multiline_renderer_output_is_collapsed_to_one_line(self):
        _, out, _ = self.run_main(json.dumps(load_fixture("statusline_basic.json")),
                                  render=StubRender(line="a\nb\r\nc"))
        self.assertEqual(out, "a b  c\n")

    def test_bad_input_is_silent_and_exits_zero(self):
        for text in ("", "   ", "not json", "[1, 2]", "42", "null", "{"):
            rc, out, err = self.run_main(text, render=StubRender())
            self.assertEqual((rc, out, err), (0, "", ""), repr(text))
        self.assertFalse(os.path.isdir(os.path.join(self.state, "cache")))

    def test_payload_without_session_id_still_prints_but_stores_nothing(self):
        rc, out, _ = self.run_main(json.dumps({"model": {"display_name": "X"}}), render=None)
        self.assertEqual((rc, out), (0, "X | CTX --\n"))
        self.assertFalse(os.path.isdir(os.path.join(self.state, "cache")))

    def test_hostile_session_id_cannot_escape_the_state_dir(self):
        rc, out, err = self.run_main(json.dumps(load_fixture("statusline_hostile.json")), render=None)
        self.assertEqual((rc, err), (0, ""))
        self.assertFalse(any(ord(c) < 32 for c in out.rstrip("\n")))
        self.assertEqual(sorted(os.listdir(self.tmp)), ["state"])          # nothing created next to it
        cached = os.listdir(os.path.join(self.state, "cache", "claude"))
        self.assertEqual(len(cached), 1)
        self.assertNotIn("/", cached[0])
        self.assertFalse(cached[0].startswith("."))

    def test_unwritable_state_dir_still_prints_a_line(self):
        blocker = os.path.join(self.tmp, "blocker")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("a regular file where a directory is needed")
        os.environ["CTXHUD_STATE_DIR"] = os.path.join(blocker, "state")
        rc, out, err = self.run_main(json.dumps(load_fixture("statusline_basic.json")), render=None)
        self.assertEqual((rc, out, err), (0, "Example 1 | CTX 50%\n", ""))

    def test_argv_is_ignored(self):
        for argv in (None, [], ["bridge", "claude"], ["bridge", "claude", "--whatever"]):
            rc, out, _ = self.run_main(json.dumps(load_fixture("statusline_basic.json")), argv=argv,
                                       render=StubRender(line="ok"))
            self.assertEqual((rc, out), (0, "ok\n"))

    def test_raw_capture_is_off_by_default(self):
        self.run_main(json.dumps(load_fixture("statusline_basic.json")), render=StubRender())
        self.assertFalse(os.path.isdir(os.path.join(self.state, "raw")))

    def test_raw_capture_when_enabled(self):
        self.write_config({"capture_raw": True})
        payload = load_fixture("statusline_basic.json")
        self.run_main(json.dumps(payload), render=StubRender())
        raw = common.read_json(common.raw_path("claude", payload["session_id"]))
        self.assertEqual(raw, payload)

    def test_non_ascii_model_name_survives_an_ascii_stdout(self):
        class AsciiOut(io.StringIO):
            def write(self, text):
                text.encode("ascii")
                return super().write(text)

        payload = load_fixture("statusline_basic.json")
        payload["model"]["display_name"] = "Modèle"
        out = AsciiOut()
        with mock.patch.dict(sys.modules, {"ctxhud.render": None}), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), mock.patch("sys.stdout", out):
            self.assertEqual(bridge_claude.main(), 0)
        self.assertEqual(out.getvalue(), "Mod?le | CTX 50%\n")

    @unittest.skipUnless(importlib.util.find_spec("ctxhud.render"), "ctxhud.render not written yet")
    def test_integration_with_the_real_renderer(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(load_fixture("statusline_basic.json")))), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = bridge_claude.main(["bridge", "claude"])
        self.assertEqual((rc, err.getvalue()), (0, ""))
        self.assertIn("CTX", out.getvalue())
        self.assertIn("Example 1·high", out.getvalue())     # render.short_model(model, effort)
        self.assertEqual(out.getvalue().count("\n"), 1)

    @unittest.skipUnless(importlib.util.find_spec("ctxhud.render"), "ctxhud.render not written yet")
    def test_integration_without_effort_shows_the_bare_model_name(self):
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(load_fixture("statusline_weekly_only.json")))), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(bridge_claude.main(["bridge", "claude"]), 0)
        self.assertIn("example-model-2", out.getvalue())
        self.assertNotIn("·", out.getvalue())


if __name__ == "__main__":
    unittest.main()
