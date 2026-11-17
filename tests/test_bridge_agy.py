"""Unit tests for ctxhud.bridge_agy using synthetic and anonymized fixtures."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SRC = str(Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from ctxhud import bridge_agy, common, render  # noqa: E402

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "agy"


def _load_fixture(name: str) -> dict:
    with open(_FIXTURE_DIR / name, "r", encoding="utf-8") as fh:
        return json.load(fh)


class TestBridgeAgyNormalization(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._env_patch = mock.patch.dict(
            os.environ,
            {
                "CTXHUD_STATE_DIR": self._tmp.name,
                "CTXHUD_CONFIG": os.path.join(self._tmp.name, "missing_config.json"),
                "TMUX_PANE": "%7",
            },
            clear=False,
        )
        self._env_patch.start()
        common._config_cache = None

    def tearDown(self) -> None:
        common._config_cache = None
        self._env_patch.stop()
        self._tmp.cleanup()

    def test_basic_gemini_normalization(self) -> None:
        payload = _load_fixture("basic_sample.json")
        out = bridge_agy.normalize_payload(payload, ts=1767225600, pid=4242)

        self.assertEqual(out["schema"], 1)
        self.assertEqual(out["tool"], "agy")
        self.assertEqual(out["session_id"], "00000000-0000-4000-8000-000000000001")
        self.assertEqual(out["pid"], 4242)
        self.assertEqual(out["cwd"], "/home/user/project")
        self.assertEqual(out["model"], "Gemini 3.8 Flash (Low)")
        self.assertEqual(out["effort"], "low")
        self.assertEqual(out["tmux_pane"], "%7")

        ctx = out["ctx"]
        self.assertEqual(ctx["used_pct"], 54.0)
        self.assertEqual(ctx["window"], 1048576)
        self.assertEqual(ctx["tokens"], int(round(0.54 * 1048576)))
        self.assertEqual(ctx["source"], "native")

        quota = out["quota"]
        self.assertIsNotNone(quota)
        self.assertEqual(quota["group"], "gemini")
        self.assertAlmostEqual(quota["five_hour"]["used_pct"], 84.0)
        self.assertAlmostEqual(quota["weekly"]["used_pct"], 18.0)
        self.assertEqual(quota["five_hour"]["resets_at"], 1767236400)

    def test_third_party_model_selects_3p_group(self) -> None:
        payload = _load_fixture("basic_sample.json")
        payload["model"] = {"id": "Claude Sonnet", "display_name": "Claude Sonnet"}
        out = bridge_agy.normalize_payload(payload, ts=1767225600, pid=4242)

        self.assertEqual(out["quota"]["group"], "3p")
        self.assertAlmostEqual(out["quota"]["five_hour"]["used_pct"], 10.0)
        self.assertAlmostEqual(out["quota"]["weekly"]["used_pct"], 15.0)

    def test_argon_uses_gemini_group_and_more_constrained_weekly(self) -> None:
        payload = _load_fixture("basic_sample.json")
        payload["model"] = {"id": "Orchestrator (Argon)", "display_name": "Orchestrator (Argon)"}
        # gemini-weekly used = 18%, gemini-argon-wtu-weekly used = 25% -> 25% wins
        out = bridge_agy.normalize_payload(payload, ts=1767225600, pid=4242)
        self.assertEqual(out["quota"]["group"], "gemini")
        self.assertAlmostEqual(out["quota"]["five_hour"]["used_pct"], 84.0)
        self.assertAlmostEqual(out["quota"]["weekly"]["used_pct"], 25.0)

        # Flip: gemini-weekly used = 90%, argon-wtu used = 25% -> 90% wins
        payload["quota"]["gemini-weekly"]["remaining_fraction"] = 0.10
        out2 = bridge_agy.normalize_payload(payload, ts=1767225600, pid=4242)
        self.assertEqual(out2["quota"]["group"], "gemini")
        self.assertAlmostEqual(out2["quota"]["weekly"]["used_pct"], 90.0)

    def test_argon_falls_back_when_wtu_key_absent(self) -> None:
        payload = _load_fixture("basic_sample.json")
        payload["model"] = {"id": "Orchestrator (Argon)", "display_name": "Orchestrator (Argon)"}
        del payload["quota"]["gemini-argon-wtu-weekly"]
        out = bridge_agy.normalize_payload(payload, ts=1767225600, pid=4242)

        self.assertEqual(out["quota"]["group"], "gemini")
        self.assertAlmostEqual(out["quota"]["five_hour"]["used_pct"], 84.0)
        self.assertAlmostEqual(out["quota"]["weekly"]["used_pct"], 18.0)

    def test_anonymized_captured_authenticating_fixture(self) -> None:
        payload = _load_fixture("captured_authenticating.json")
        out = bridge_agy.normalize_payload(payload, ts=1767225600, pid=99)

        self.assertEqual(out["session_id"], "agy-99")
        self.assertEqual(out["model"], "Gemini 4 Hydrogen (Medium)")
        self.assertIsNone(out["effort"])
        self.assertIsNone(out["ctx"]["used_pct"])
        self.assertIsNone(out["ctx"]["window"])
        self.assertEqual(out["ctx"]["source"], "none")
        self.assertIsNone(out["quota"])

    def test_anonymized_captured_idle_fixture_strips_email(self) -> None:
        payload = _load_fixture("captured_idle.json")
        self.assertIn("email", payload)
        out = bridge_agy.normalize_payload(payload, ts=1767225600, pid=101)

        self.assertEqual(out["session_id"], "agy-101")
        self.assertEqual(out["model"], "Gemini 3.8 Flash (Low)")
        self.assertEqual(out["effort"], "low")
        self.assertEqual(out["ctx"]["used_pct"], 0.0)
        self.assertEqual(out["ctx"]["window"], 1048576)
        self.assertEqual(out["ctx"]["tokens"], 0)
        self.assertEqual(out["quota"]["group"], "gemini")
        self.assertAlmostEqual(out["quota"]["five_hour"]["used_pct"], 3.0)
        self.assertAlmostEqual(out["quota"]["weekly"]["used_pct"], 9.0)
        self.assertNotIn("email", json.dumps(out))

    def test_missing_context_and_quota_never_fill_zero(self) -> None:
        payload = {
            "conversation_id": "00000000-0000-4000-8000-000000000002",
            "cwd": "/home/user/project",
            "model": {"display_name": "Gemini"},
        }
        out = bridge_agy.normalize_payload(payload, ts=1767225600, pid=99)

        self.assertIsNone(out["ctx"]["used_pct"])
        self.assertIsNone(out["ctx"]["tokens"])
        self.assertIsNone(out["ctx"]["window"])
        self.assertEqual(out["ctx"]["source"], "none")
        self.assertIsNone(out["quota"])

    def test_remaining_fraction_missing_stays_none(self) -> None:
        payload = _load_fixture("basic_sample.json")
        payload["quota"] = {
            "gemini-5h": {"reset_time": "2026-01-01T03:00:00Z"},
            "gemini-weekly": {},
        }
        out = bridge_agy.normalize_payload(payload, ts=1767225600, pid=1)
        self.assertIsNone(out["quota"]["five_hour"]["used_pct"])
        self.assertIsNone(out["quota"]["weekly"])

    def test_unchanged_quota_preserves_observed_at(self) -> None:
        payload = _load_fixture("basic_sample.json")
        first = bridge_agy.normalize_payload(payload, ts=1000, pid=1)
        second = bridge_agy.normalize_payload(payload, ts=1500, pid=1, prev_cache=first)
        self.assertEqual(second["quota"]["five_hour"]["observed_at"], 1000)
        self.assertEqual(second["quota"]["weekly"]["observed_at"], 1000)

        payload["quota"]["gemini-5h"]["remaining_fraction"] = 0.10
        third = bridge_agy.normalize_payload(payload, ts=1600, pid=1, prev_cache=second)
        self.assertEqual(third["quota"]["five_hour"]["observed_at"], 1600)
        self.assertEqual(third["quota"]["weekly"]["observed_at"], 1000)

    def test_email_and_non_whitelisted_fields_stripped_from_cache_and_raw(self) -> None:
        cfg_path = os.path.join(self._tmp.name, "cfg.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump({"capture_raw": True}, fh)
        payload = _load_fixture("basic_sample.json")
        payload["email"] = "secret-user@example.invalid"
        payload["conversation_title"] = "private chat title"
        payload["transcript_path"] = "/home/user/secret/transcript.jsonl"

        with mock.patch.dict(os.environ, {"CTXHUD_CONFIG": cfg_path}):
            common._config_cache = None
            buf_out = io.StringIO()
            buf_err = io.StringIO()
            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                 mock.patch.object(sys, "stdout", buf_out), \
                 mock.patch.object(sys, "stderr", buf_err):
                rc = bridge_agy.main(["bridge", "agy"])

        self.assertEqual(rc, 0)
        self.assertEqual(buf_err.getvalue(), "")

        sid = "00000000-0000-4000-8000-000000000001"
        cache_text = Path(common.cache_path("agy", sid)).read_text(encoding="utf-8")
        raw_text = Path(common.raw_path("agy", sid)).read_text(encoding="utf-8")
        for forbidden in ("secret-user@example.invalid", "private chat title", "transcript.jsonl"):
            self.assertNotIn(forbidden, cache_text)
            self.assertNotIn(forbidden, raw_text)
        self.assertEqual(os.stat(common.cache_path("agy", sid)).st_mode & 0o777, 0o600)

    def test_main_handles_invalid_stdin_gracefully(self) -> None:
        buf_out = io.StringIO()
        buf_err = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO("not-json")), \
             mock.patch.object(sys, "stdout", buf_out), \
             mock.patch.object(sys, "stderr", buf_err):
            rc = bridge_agy.main(["bridge", "agy"])

        self.assertEqual(rc, 0)
        self.assertEqual(buf_err.getvalue(), "")
        self.assertIn("CTX", buf_out.getvalue())

    def test_render_delegates_to_render_module_with_group_arg(self) -> None:
        payload = _load_fixture("basic_sample.json")

        buf_out = io.StringIO()
        with mock.patch.object(render, "pick_quota", return_value=None) as m_pick, \
             mock.patch.object(render, "render_line", return_value="MOCKED_LINE") as m_render, \
             mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
             mock.patch.object(sys, "stdout", buf_out):
            rc = bridge_agy.main(["bridge", "agy"])

        self.assertEqual(rc, 0)
        self.assertEqual(buf_out.getvalue(), "MOCKED_LINE\n")
        m_pick.assert_called_once_with("agy", group="gemini")
        m_render.assert_called_once()

    def test_real_render_output_format(self) -> None:
        payload = _load_fixture("basic_sample.json")
        future_iso = "2099-01-01T03:00:00Z"
        payload["quota"]["gemini-5h"]["reset_time"] = future_iso
        payload["quota"]["gemini-weekly"]["reset_time"] = future_iso
        buf_out = io.StringIO()
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            common._config_cache = None
            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                 mock.patch.object(sys, "stdout", buf_out):
                rc = bridge_agy.main(["bridge", "agy"])
        self.assertEqual(rc, 0)
        line = buf_out.getvalue().strip()
        self.assertIn("CTX 54%!", line)
        self.assertIn("5h 84%!", line)
        self.assertIn("7d 18%", line)
        self.assertIn("Flash·low", line)

    def test_effort_extraction_and_short_model_rendering(self) -> None:
        # 1) When model.effort is absent, bridge sets effort=None and preserves raw model name
        #    (parenthesis parsing is delegated to render.short_model).
        payload_no_effort = _load_fixture("basic_sample.json")
        payload_no_effort["model"] = {
            "id": "Gemini 4 Hydrogen (Medium)",
            "display_name": "Gemini 4 Hydrogen (Medium)",
        }
        out_none = bridge_agy.normalize_payload(payload_no_effort, ts=1767225600, pid=4242)
        self.assertEqual(out_none["model"], "Gemini 4 Hydrogen (Medium)")
        self.assertIsNone(out_none["effort"])

        # 2) When model.effort is present, bridge normalizes it to lowercase without altering model.
        payload_with_effort = _load_fixture("basic_sample.json")
        payload_with_effort["model"] = {
            "id": "Gemini 4 Hydrogen (Medium)",
            "display_name": "Gemini 4 Hydrogen (Medium)",
            "effort": "High",
        }
        out_high = bridge_agy.normalize_payload(payload_with_effort, ts=1767225600, pid=4242)
        self.assertEqual(out_high["model"], "Gemini 4 Hydrogen (Medium)")
        self.assertEqual(out_high["effort"], "high")

        # 3) End-to-end render_line produces short_model output from both paths.
        future_iso = "2099-01-01T03:00:00Z"
        payload_no_effort["quota"]["gemini-5h"]["reset_time"] = future_iso
        payload_no_effort["quota"]["gemini-weekly"]["reset_time"] = future_iso
        buf_out = io.StringIO()
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            common._config_cache = None
            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload_no_effort))), \
                 mock.patch.object(sys, "stdout", buf_out):
                rc = bridge_agy.main(["bridge", "agy"])
        self.assertEqual(rc, 0)
        self.assertIn("Hydrogen·med", buf_out.getvalue())

        payload_with_effort["quota"]["gemini-5h"]["reset_time"] = future_iso
        payload_with_effort["quota"]["gemini-weekly"]["reset_time"] = future_iso
        buf_out2 = io.StringIO()
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            common._config_cache = None
            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload_with_effort))), \
                 mock.patch.object(sys, "stdout", buf_out2):
                rc2 = bridge_agy.main(["bridge", "agy"])
        self.assertEqual(rc2, 0)
        self.assertIn("Hydrogen·high", buf_out2.getvalue())


if __name__ == "__main__":
    unittest.main()
