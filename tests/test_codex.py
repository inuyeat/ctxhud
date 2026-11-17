"""Synthetic-only Codex adapter tests; no local tools or real sessions are used."""
import contextlib
import io
import json
import os
import subprocess
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ctxhud import codex, common


STAMP = "2030-01-01T00:00:00Z"


def metadata(sid="thread-one"):
    return {"type": "session_meta", "timestamp": STAMP,
            "payload": {"id": sid, "cwd": "/home/user/project"}}


def usage(tokens=18000, window=24000, limits=None, info=True):
    return {"type": "event_msg", "timestamp": STAMP, "payload": {
        "type": "token_count", "info": {
            "last_token_usage": {"total_tokens": tokens},
            "total_token_usage": {"total_tokens": 999999},
            "model_context_window": window,
        } if info else None, "rate_limits": limits}}


def limits(used=37):
    return {"limit_id": "codex", "primary": {
        "used_percent": used, "window_minutes": 300, "resets_at": 1893459600},
        "secondary": {"used_percent": 11, "window_minutes": 10080,
                      "resets_at": 1894060800}}


class CodexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.sessions = self.home / "sessions"
        self.sessions.mkdir(parents=True)
        self.rollout = self.sessions / "rollout-fake.jsonl"
        self.environment = mock.patch.dict(os.environ, {
            "CODEX_HOME": str(self.home), "CTXHUD_STATE_DIR": str(self.root / "state"),
            "CTXHUD_CONFIG": str(self.root / "config.json"),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.write(metadata())

    def write(self, *records, append=False):
        with self.rollout.open("ab" if append else "wb") as stream:
            for record in records:
                stream.write(json.dumps(record).encode("utf-8") + b"\n")

    def tail(self, previous=None, **kwargs):
        return codex.tail_rollout(str(self.rollout), previous, **kwargs)

    def mapping(self):
        # Every PID in this file is invented test data.
        return {"session_id": "thread-one", "pid": 702, "pane_pid": 701,
                "starts": {"701": "fake-start", "702": "fake-start"},
                "cwd": "/home/user/project", "rollout_path": str(self.rollout),
                "tmux_pane": "%7", "width": 80}

    def command_output(self, args):
        if args[0] == "tmux":
            return "701\t80\t0\n"
        if args[0] == "ps":
            return ("701 1 Tue Jan 1 00:00:00 2030 /bin/zsh\n"
                    "702 701 Tue Jan 1 00:00:01 2030 /opt/tools/codex\n"
                    "999 1 Tue Jan 1 00:00:01 2030 /opt/tools/codex\n")
        if args[0] == "lsof":
            self.assertIn("702", args)
            self.assertNotIn("999", args)
            return "p702\nf9\nn" + str(self.rollout) + "\n"
        self.fail("unexpected tool")

    def test_baseline_and_rounding(self):
        for tokens, window, expected in [
            (18000, 24000, 50), (0, 24000, 0), (40000, 24000, 100),
            (12300, 32000, 1), (12000, 12000, 100),
            (20, None, None), (20, 0, None), (-1, 20000, None),
            (True, 24000, None), (float("inf"), 24000, None),
        ]:
            with self.subTest(tokens=tokens, window=window):
                self.assertEqual(codex.context_percent(tokens, window), expected)

    def test_current_context_not_session_total(self):
        self.write(usage(), append=True)
        snapshot = self.tail()["snapshot"]
        self.assertEqual(snapshot["ctx"]["tokens"], 18000)
        self.assertEqual(snapshot["ctx"]["used_pct"], 50)
        self.assertEqual(snapshot["ctx"]["source"], "observed_w")

    def test_used_percent_not_remaining(self):
        self.write(usage(limits=limits(37)), append=True)
        quota = self.tail()["snapshot"]["quota"]
        self.assertEqual(quota["five_hour"]["used_pct"], 37)
        self.assertEqual(quota["weekly"]["used_pct"], 11)

    def test_append_no_double_count_and_lower_usage_replaces(self):
        self.write(usage(limits=limits(80)), append=True)
        first = self.tail()
        same = self.tail(first)
        self.assertEqual(first, same)
        self.write(usage(15000, limits=limits(20)), append=True)
        second = self.tail(same)
        self.assertEqual(second["snapshot"]["ctx"]["tokens"], 15000)
        self.assertEqual(second["snapshot"]["quota"]["five_hour"]["used_pct"], 20)
        self.assertEqual(second["offset"], self.rollout.stat().st_size)

    def test_incomplete_line_retried_and_utf8_safe(self):
        self.write(usage(), append=True)
        first = self.tail()
        raw = json.dumps(usage(16000), ensure_ascii=False).encode("utf-8")
        with self.rollout.open("ab") as stream:
            stream.write(raw[:40])
        partial = self.tail(first)
        self.assertEqual(partial["offset"], first["offset"])
        with self.rollout.open("ab") as stream:
            stream.write(raw[40:] + b"\n")
        self.assertEqual(self.tail(partial)["snapshot"]["ctx"]["tokens"], 16000)

    def test_null_info_and_null_quota_keep_independent_observations(self):
        self.write(usage(limits=limits()), append=True)
        first = self.tail()
        self.write(usage(info=False, limits=limits(42)), append=True)
        second = self.tail(first)
        self.assertEqual(second["snapshot"]["ctx"], first["snapshot"]["ctx"])
        self.assertEqual(second["snapshot"]["quota"]["five_hour"]["used_pct"], 42)
        self.write(usage(19000), append=True)
        third = self.tail(second)
        self.assertEqual(third["snapshot"]["quota"], second["snapshot"]["quota"])

    def test_bucket_and_duration_selection(self):
        self.write(usage(limits=limits()), append=True)
        first = self.tail()
        other = limits(99)
        other["limit_id"] = "other-model"
        self.write(usage(limits=other), append=True)
        second = self.tail(first)
        self.assertEqual(first["snapshot"]["quota"], second["snapshot"]["quota"])
        swapped = limits()
        swapped["primary"], swapped["secondary"] = swapped["secondary"], swapped["primary"]
        self.write(usage(limits=swapped), append=True)
        third = self.tail(second)
        self.assertEqual(third["snapshot"]["quota"]["five_hour"]["used_pct"], 37)
        swapped["primary"]["window_minutes"] = 15
        self.write(usage(limits=swapped), append=True)
        self.assertIsNone(self.tail(third)["snapshot"]["quota"]["weekly"])

    def test_bad_values_unknown_no_model_window_guess(self):
        self.write(usage(window=None, limits={"primary": {"used_percent": float("inf")}}), append=True)
        snapshot = self.tail()["snapshot"]
        self.assertIsNone(snapshot["ctx"]["used_pct"])
        self.assertEqual(snapshot["ctx"]["tokens"], 18000)
        self.assertIsNone(snapshot["quota"]["five_hour"])

    def test_expired_quota_retains_observed_value_for_renderer(self):
        value = limits(85)
        value["primary"]["resets_at"] = 1
        self.write(usage(limits=value), append=True)
        quota = self.tail()["snapshot"]["quota"]["five_hour"]
        self.assertEqual(quota["used_pct"], 85)
        self.assertEqual(quota["resets_at"], 1)

    def test_model_change_waits_for_measurement_and_decreases_are_natural(self):
        self.write(usage(), append=True)
        first = self.tail()
        self.write({"type": "turn_context", "payload": {"model": "model-test"}}, append=True)
        second = self.tail(first)
        self.assertIsNone(second["snapshot"]["ctx"]["tokens"])
        self.write(usage(), append=True)
        third = self.tail(second)
        self.write(usage(13000), append=True)
        final = self.tail(third)
        self.assertEqual(final["snapshot"]["ctx"]["tokens"], 13000)

    def test_turn_context_effort_is_separate_from_unshortened_model(self):
        model = "model-synthetic-full-display-name-" + "x" * 140
        self.write({"type": "turn_context", "payload": {
            "model": model, "effort": "xhigh",
            "collaboration_mode": {"settings": {"reasoning_effort": "low"}},
        }}, usage(), append=True)
        with mock.patch.object(codex, "resolve_pane", return_value=self.mapping()):
            result = codex.collect_for_pane("%7")
        self.assertEqual(result["entry"]["model"], model)
        self.assertEqual(result["entry"]["effort"], "xhigh")
        cached = common.read_json(common.cache_path("codex", "thread-one"))
        self.assertEqual(cached["effort"], "xhigh")

    def test_thread_settings_applied_updates_model_and_effort(self):
        self.write({"type": "turn_context", "payload": {"model": "model-one", "effort": "low"}},
                   usage(), append=True)
        first = self.tail()
        self.write({"type": "event_msg", "payload": {
            "type": "thread_settings_applied", "thread_settings": {
                "model": "model-two", "reasoning_effort": "max"}}}, append=True)
        snapshot = self.tail(first)["snapshot"]
        self.assertEqual(snapshot["model"], "model-two")
        self.assertEqual(snapshot["effort"], "max")
        self.assertIsNone(snapshot["ctx"]["tokens"])

    def test_effort_only_change_keeps_observed_context(self):
        self.write({"type": "turn_context", "payload": {"model": "model-one", "effort": "low"}},
                   usage(), append=True)
        first = self.tail()
        self.write({"type": "event_msg", "payload": {
            "type": "thread_settings_applied", "thread_settings": {
                "model": "model-one", "reasoning_effort": "high"}}}, append=True)
        snapshot = self.tail(first)["snapshot"]
        self.assertEqual(snapshot["effort"], "high")
        self.assertEqual(snapshot["ctx"], first["snapshot"]["ctx"])

    def test_effort_nested_fallback_and_unknown_do_not_invent_defaults(self):
        for level in ("low", "medium", "high", "xhigh", "max"):
            self.assertEqual(codex._effort({"collaboration_mode": {
                "settings": {"reasoning_effort": level}}}), level)
        for level in (None, "future-level", True, 1, {}, []):
            self.assertIsNone(codex._effort({"effort": level, "collaboration_mode": {
                "settings": {"reasoning_effort": "high"}}}))
        self.assertIsNone(codex._effort({}))
        self.write({"type": "turn_context", "payload": {"effort": "high"}}, append=True)
        first = self.tail()
        self.write({"type": "turn_context", "payload": {}}, append=True)
        self.assertIsNone(self.tail(first)["snapshot"]["effort"])

    def test_legacy_checkpoint_replays_context_to_backfill_effort(self):
        self.write({"type": "turn_context", "payload": {"model": "model-one", "effort": "medium"}},
                   usage(), append=True)
        first = self.tail()
        first.pop("parser_version")
        first["snapshot"].pop("effort")
        rebuilt = self.tail(first)
        self.assertEqual(rebuilt["snapshot"]["effort"], "medium")
        self.assertEqual(rebuilt["snapshot"]["ctx"]["tokens"], 18000)
        self.assertEqual(rebuilt["parser_version"], codex.PARSER_VERSION)

    def test_truncate_and_same_size_rewrite_invalidate(self):
        self.write(usage(), append=True)
        first = self.tail()
        self.write(metadata())
        second = self.tail(first)
        self.assertIsNone(second["snapshot"]["ctx"]["tokens"])
        self.write(metadata(), usage(19000))
        third = self.tail(second)
        self.write(metadata(), usage(14000))
        self.assertEqual(self.tail(third)["snapshot"]["ctx"]["tokens"], 14000)

    def test_replace_inode_invalidates(self):
        self.write(usage(), append=True)
        first = self.tail()
        replacement = self.sessions / "replacement.jsonl"
        replacement.write_text(json.dumps(metadata()) + "\n")
        os.replace(str(replacement), str(self.rollout))
        self.assertIsNone(self.tail(first)["snapshot"]["ctx"]["tokens"])

    def test_small_head_can_grow_without_losing_snapshot(self):
        self.write(usage(), append=True)
        first = self.tail()
        self.write({"type": "unrelated", "payload": {"text": "x" * 1000}}, append=True)
        self.assertEqual(self.tail(first)["snapshot"]["ctx"], first["snapshot"]["ctx"])

    def test_malformed_and_oversized_lines_make_progress(self):
        with self.rollout.open("ab") as stream:
            stream.write(b"not json\n[]\n")
            stream.write(b"x" * (codex.MAX_LINE_BYTES + 50) + b"\n")
        self.write(usage(), append=True)
        result = self.tail()
        self.assertEqual(result["snapshot"]["ctx"]["tokens"], 18000)
        self.assertEqual(result["offset"], self.rollout.stat().st_size)

    def test_bounded_catchup_progresses(self):
        self.write(usage(), usage(16000), usage(14000), append=True)
        checkpoint = self.tail(batch_bytes=1)
        self.assertTrue(checkpoint["catching_up"])
        for _ in range(10):
            checkpoint = self.tail(checkpoint, batch_bytes=1)
            if not checkpoint["catching_up"]:
                break
        self.assertFalse(checkpoint["catching_up"])
        self.assertEqual(checkpoint["snapshot"]["ctx"]["tokens"], 14000)

    def test_corrupt_cursor_is_rebuilt(self):
        self.write(usage(), append=True)
        previous = self.tail()
        previous["head_size"] = {"invalid": True}
        previous["snapshot"] = None
        rebuilt = self.tail(previous)
        self.assertEqual(rebuilt["snapshot"]["ctx"]["tokens"], 18000)

    def test_metadata_must_be_object_and_have_safe_id(self):
        for data in ([], {"type": "session_meta", "payload": {"id": "../escape"}}):
            self.write(data)
            with self.assertRaises(codex.Unresolved):
                codex._metadata(str(self.rollout))

    def test_collection_rejects_identity_race(self):
        self.write(metadata("thread-two"), usage())
        with mock.patch.object(codex, "resolve_pane", return_value=self.mapping()):
            with self.assertRaises(codex.Unresolved):
                codex.collect_for_pane("%7")
        self.assertIsNone(common.read_json(common.cache_path("codex", "thread-one")))

    def test_resolver_revalidates_open_files_even_when_cached(self):
        with mock.patch.object(codex, "_run", side_effect=self.command_output):
            cached = codex.resolve_pane("%7")
        def closed(args):
            return "p702\n" if args[0] == "lsof" else self.command_output(args)
        with mock.patch.object(codex, "_run", side_effect=closed):
            with self.assertRaises(codex.Unresolved):
                codex.resolve_pane("%7", cached)

    def test_renderer_uses_shared_quota_selection(self):
        from ctxhud import render
        with mock.patch.object(render, "pick_quota", return_value={"group": "codex"}) as pick, \
                mock.patch.object(render, "render_line", return_value="CTX --") as draw:
            self.assertEqual(codex.render_line({"ctx": {}}, style="tmux"), "CTX --")
        pick.assert_called_once_with("codex")
        draw.assert_called_once_with({"ctx": {}}, quota={"group": "codex"}, style="tmux")

    def test_explicit_process_and_open_file_mapping(self):
        with mock.patch.object(codex, "_run", side_effect=self.command_output):
            mapping = codex.resolve_pane("%7")
        self.assertEqual(mapping["session_id"], "thread-one")
        self.assertEqual(mapping["pid"], 702)

    def test_invalid_pane_never_executes_command(self):
        with mock.patch.object(codex, "_run") as run:
            with self.assertRaises(codex.Unresolved):
                codex.resolve_pane("%7; echo unsafe")
        run.assert_not_called()

    def test_command_runner_has_timeout_no_shell_and_suppresses_stderr(self):
        response = mock.Mock(returncode=0, stdout="safe")
        with mock.patch("shutil.which", return_value="/bin/tool"), \
                mock.patch("subprocess.run", return_value=response) as run:
            self.assertEqual(codex._run(["tool", "argument"]), "safe")
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["/bin/tool", "argument"])
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(kwargs["timeout"], codex.COMMAND_TIMEOUT)
        self.assertFalse(kwargs.get("shell", False))

    def test_timeout_from_identity_query_is_safe(self):
        with mock.patch.object(codex, "_run", side_effect=subprocess.TimeoutExpired("tool", 1)), \
                contextlib.redirect_stdout(io.StringIO()) as stdout, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(codex.main(["%7"]), 0)
        self.assertIn("unresolved", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

    def test_same_cwd_does_not_supply_a_rollout(self):
        def output(args):
            return "p702\nn/home/user/project/file.py\n" if args[0] == "lsof" else self.command_output(args)
        with mock.patch.object(codex, "_run", side_effect=output):
            with self.assertRaises(codex.Unresolved):
                codex.resolve_pane("%7")

    def test_multiple_rollouts_fail_closed(self):
        other = self.sessions / "other.jsonl"
        other.write_text(json.dumps(metadata("thread-two")) + "\n")
        def output(args):
            value = self.command_output(args)
            return value + "n" + str(other) + "\n" if args[0] == "lsof" else value
        with mock.patch.object(codex, "_run", side_effect=output):
            with self.assertRaises(codex.Unresolved):
                codex.resolve_pane("%7")

    def test_pid_reuse_and_surviving_process_thread_switch(self):
        with mock.patch.object(codex, "_run", side_effect=self.command_output):
            first = codex.resolve_pane("%7")
            first["starts"]["702"] = "old-start"
            self.write(metadata("thread-two"))
            second = codex.resolve_pane("%7", first)
        self.assertEqual(second["session_id"], "thread-two")
        self.assertNotEqual(second["starts"]["702"], "old-start")

    def test_symlink_outside_codex_roots_rejected(self):
        outside = self.root / "outside.jsonl"
        outside.write_text(json.dumps(metadata("untrusted")) + "\n")
        link = self.sessions / "linked.jsonl"
        link.symlink_to(outside)
        self.assertIsNone(codex._rollout_path(str(link)))

    def test_collection_and_permissions(self):
        self.write(usage(limits=limits()), append=True)
        with mock.patch.object(codex, "resolve_pane", return_value=self.mapping()):
            first = codex.collect_for_pane("%7")
            second = codex.collect_for_pane("%7")
        self.assertEqual(first["entry"]["ctx"], second["entry"]["ctx"])
        self.assertEqual(first["entry"]["quota"]["five_hour"]["used_pct"], 37)
        for path in (self.root / "state").rglob("*.json"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root / "state").stat().st_mode), 0o700)

    def test_mapping_failure_removes_cache_and_does_not_show_old_thread(self):
        with mock.patch.object(codex, "resolve_pane", return_value=self.mapping()):
            codex.collect_for_pane("%7")
        with mock.patch.object(codex, "resolve_pane", side_effect=codex.Unresolved("closed")):
            with self.assertRaises(codex.Unresolved):
                codex.collect_for_pane("%7")
        self.assertNotIn("%7", common.read_json(common.sub_state("codex", "panes.json")))

    def test_overlapping_invocation_is_nonblocking(self):
        with codex._state_lock():
            with self.assertRaises(codex.Unresolved):
                with codex._state_lock():
                    self.fail("lock should not be reentered")

    def test_main_accepts_dispatch_argv_and_tmux_style(self):
        result = {"entry": {"tool": "codex"}, "width": 80,
                  "catching_up": False, "compaction_pending": False}
        with mock.patch.object(codex, "collect_for_pane", return_value=result), \
                mock.patch.object(codex, "render_line", return_value="CTX 50%!") as render, \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(codex.main(["codex-border", "%7"]), 0)
        render.assert_called_once_with(result["entry"], width=80, style="tmux")
        self.assertEqual(stdout.getvalue(), "CTX 50%!\n")

    def test_width_override_before_or_after_pane_and_quiet(self):
        result = {"entry": {"tool": "codex"}, "width": 80, "catching_up": False}
        for args in (["codex-border", "%7", "--width", "40"],
                     ["--width", "40", "%7", "--quiet-unresolved"]):
            with self.subTest(args=args), \
                    mock.patch.object(codex, "collect_for_pane", return_value=result), \
                    mock.patch.object(codex, "render_line", return_value="CTX 1%") as render, \
                    contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(codex.main(args), 0)
            render.assert_called_once_with(result["entry"], width=38, style="tmux")
            self.assertEqual(stdout.getvalue(), "CTX 1%\n")

    def test_width_margin_is_clamped_to_one(self):
        result = {"entry": {}, "width": 80, "catching_up": False}
        for value in ("1", "2", "3"):
            with mock.patch.object(codex, "collect_for_pane", return_value=result), \
                    mock.patch.object(codex, "render_line", return_value="CTX --") as render, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(codex.main(["%7", "--width", value]), 0)
            self.assertEqual(render.call_args.kwargs["width"], 1)

    def test_invalid_width_fails_safely_before_collection(self):
        for options in (["--width"], ["--width", "0"], ["--width", "-1"],
                        ["--width", "wide"], ["--width", "1.5"],
                        ["--width", "40", "--width", "50"]):
            with self.subTest(options=options), \
                    mock.patch.object(codex, "collect_for_pane") as collect, \
                    contextlib.redirect_stdout(io.StringIO()) as stdout, \
                    contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(codex.main(["%7", "--quiet-unresolved"] + options), 0)
            collect.assert_not_called()
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "")

    def test_end_to_end_synthetic_queries_tail_cache_and_real_renderer(self):
        self.write(usage(limits=limits(85)), append=True)
        config = dict(common.DEFAULT_CONFIG, color=False)
        with mock.patch.object(codex, "_run", side_effect=self.command_output), \
                mock.patch.object(common, "now", return_value=1893456000), \
                mock.patch.object(common, "load_config", return_value=config), \
                contextlib.redirect_stdout(io.StringIO()) as stdout, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(codex.main(["codex-border", "%7"]), 0)
        self.assertIn("CTX ~50%!", stdout.getvalue())
        self.assertIn("5h 85%!", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

    def test_all_status_failures_exit_zero_no_stderr(self):
        for failure in [codex.Unresolved("missing"), PermissionError("blocked"), RuntimeError("failed")]:
            with self.subTest(failure=type(failure)), \
                    mock.patch.object(codex, "collect_for_pane", side_effect=failure), \
                    contextlib.redirect_stdout(io.StringIO()) as stdout, \
                    contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(codex.main(["%7"]), 0)
            self.assertIn("unresolved", stdout.getvalue())
            self.assertEqual(stderr.getvalue(), "")

    def test_hot_cache_spawns_no_commands_and_does_not_rewrite_idle_files(self):
        self.write(usage(), append=True)
        with mock.patch.object(codex, "_run", side_effect=self.command_output):
            first = codex.collect_for_pane("%7")
        with mock.patch.object(codex.os, "kill"), \
                mock.patch.object(codex, "_run", side_effect=AssertionError("no subprocess on hit")), \
                mock.patch.object(common, "atomic_write_json") as write:
            second = codex.collect_for_pane("%7")
        write.assert_not_called()
        self.assertEqual(second["entry"]["ctx"], first["entry"]["ctx"])

    def test_expired_or_dead_mapping_is_reidentified(self):
        with mock.patch.object(codex, "_run", side_effect=self.command_output):
            mapping = codex.resolve_pane("%7")
        for change in ({"validated_at": 0}, {"codex_home": "different"}, {"mapping_version": 1}):
            stale = dict(mapping, **change)
            with mock.patch.object(codex.os, "kill"):
                self.assertIsNone(codex.cached_mapping("%7", stale))
        with mock.patch.object(codex.os, "kill", side_effect=ProcessLookupError()):
            self.assertIsNone(codex.cached_mapping("%7", mapping))
        with mock.patch.object(codex.os, "kill", side_effect=PermissionError()):
            self.assertIsNone(codex.cached_mapping("%7", mapping))

    def test_hot_mapping_still_tails_new_data(self):
        self.write(usage(), append=True)
        with mock.patch.object(codex, "_run", side_effect=self.command_output):
            codex.collect_for_pane("%7")
        self.write(usage(14000), append=True)
        with mock.patch.object(codex.os, "kill"), \
                mock.patch.object(codex, "_run", side_effect=AssertionError("no subprocess on hit")):
            result = codex.collect_for_pane("%7")
        self.assertEqual(result["entry"]["ctx"]["tokens"], 14000)

    def test_quiet_unresolved_outputs_exactly_empty_string(self):
        with mock.patch.object(codex, "collect_for_pane", side_effect=codex.Unresolved("other tool")), \
                contextlib.redirect_stdout(io.StringIO()) as stdout, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(codex.main(["codex-border", "%7", "--quiet-unresolved"]), 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_quiet_option_keeps_success_output(self):
        result = {"entry": {}, "width": 80, "catching_up": False}
        with mock.patch.object(codex, "collect_for_pane", return_value=result), \
                mock.patch.object(codex, "render_line", return_value="CTX 1%"), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(codex.main(["--quiet-unresolved", "%7"]), 0)
        self.assertEqual(stdout.getvalue(), "CTX 1%\n")

    def test_missing_renderer_and_broken_stdout_are_safe(self):
        result = {"entry": {}, "width": 80, "catching_up": False, "compaction_pending": False}
        with mock.patch.object(codex, "collect_for_pane", return_value=result), \
                mock.patch.object(codex, "render_line", side_effect=ImportError()), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(codex.main(["%7"]), 0)
        self.assertIn("unresolved", stdout.getvalue())
        with mock.patch.object(sys, "stdout") as stdout, mock.patch.object(codex, "collect_for_pane", side_effect=OSError()):
            stdout.write.side_effect = BrokenPipeError()
            self.assertEqual(codex.main(["%7"]), 0)

    def test_catchup_does_not_render_partial_value(self):
        for flag, label in [("catching_up", "catching-up")]:
            result = {"entry": {}, "width": 80, "catching_up": False, "compaction_pending": False}
            result[flag] = True
            with mock.patch.object(codex, "collect_for_pane", return_value=result), \
                    mock.patch.object(codex, "render_line") as render, \
                    contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(codex.main(["%7"]), 0)
            render.assert_not_called()
            self.assertIn(label, stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
