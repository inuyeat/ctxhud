"""Tests for ctxhud.install (Python 3.9 compatible, standard library only).

Every test runs against a temporary HOME and state directory; nothing here reads or
writes the developer's real settings, and tmux is never contacted (``install._tmux`` is
replaced by a fake).
"""
from __future__ import annotations

import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from ctxhud import cli, common, install  # noqa: E402

EXPECTED_ITEMS = '["context-used", "five-hour-limit", "weekly-limit"]'


def dump(obj, trailing_newline=False):
    return json.dumps(obj, indent=2, ensure_ascii=False) + ("\n" if trailing_newline else "")


class InstallTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="ctxhud-install-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.home)
        self.exe = self.make_exe(os.path.join(self.tmp, "bin", "ctxhud"))
        env = {"HOME": self.home, "CTXHUD_STATE_DIR": self.state,
               "CTXHUD_CONFIG": os.path.join(self.tmp, "none.json")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "CTXHUD_AGY_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME",
                     "CTXHUD_EXE", "NO_COLOR"):
            os.environ.pop(name, None)
        common._config_cache = None
        self.addCleanup(setattr, common, "_config_cache", None)
        self.tmux_calls = []
        self.tmux_answers = {}
        tmux_patch = mock.patch.object(install, "_tmux", self.fake_tmux)
        tmux_patch.start()
        self.addCleanup(tmux_patch.stop)
        which_patch = mock.patch.object(install.shutil, "which", lambda name: None)
        which_patch.start()
        self.addCleanup(which_patch.stop)
        self.claude_settings = os.path.join(self.home, ".claude", "settings.json")
        self.agy_settings = os.path.join(self.home, ".gemini", "antigravity-cli", "settings.json")
        self.codex_config = os.path.join(self.home, ".codex", "config.toml")
        self.tmux_conf = os.path.join(self.home, ".tmux.conf")

    # -- helpers
    def make_exe(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(path, 0o755)
        return path

    def fake_tmux(self, *args):
        self.tmux_calls.append(args)
        return self.tmux_answers.get(args, (127, ""))

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)

    def read(self, path):
        with open(path, "r", encoding="utf-8", newline="") as fh:
            return fh.read()

    def run_cmd(self, *argv):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            rc = install.main(list(argv))
        return rc, out.getvalue()

    def install_args(self, *extra):
        return ("install", "--exe", self.exe) + extra

    def backups(self):
        base = os.path.join(self.state, "backups")
        found = []
        for root, _dirs, files in os.walk(base):
            found.extend(os.path.join(root, f) for f in files)
        return sorted(found)

    def manifest(self):
        return common.read_json(install.manifest_path())


class ClaudeSettingsTests(InstallTestCase):
    def test_dry_run_writes_nothing_and_never_prints_unrelated_settings(self):
        original = dump({"model": "example", "env": {"API_TOKEN": "hunter2-secret"}, "theme": "dark"})
        self.write(self.claude_settings, original)
        rc, out = self.run_cmd(*self.install_args("--dry-run", "--tools", "claude"))
        self.assertEqual(rc, 0)
        self.assertEqual(self.read(self.claude_settings), original)
        self.assertFalse(os.path.exists(self.state))
        self.assertIn('+  "statusLine": {', out)
        self.assertIn("bridge claude", out)
        self.assertNotIn("hunter2", out)
        self.assertNotIn("dark", out)                      # the diff has no context lines
        self.assertIn("Dry run: nothing was written", out)

    def test_install_adds_statusline_keeping_format_and_other_keys(self):
        original = dump({"model": "mé", "env": {"K": "v"}, "theme": "dark"})      # no trailing newline
        self.write(self.claude_settings, original)
        os.chmod(self.claude_settings, 0o640)
        rc, out = self.run_cmd(*self.install_args("--tools", "claude"))
        self.assertEqual(rc, 0, out)
        self.assertEqual(stat.S_IMODE(os.stat(self.claude_settings).st_mode), 0o640)   # existing mode kept
        text = self.read(self.claude_settings)
        self.assertFalse(text.endswith("\n"))
        self.assertTrue(text.startswith('{\n  "statusLine": {'))
        data = json.loads(text)
        self.assertEqual(data["statusLine"], {"type": "command", "command": "%s bridge claude" % self.exe,
                                              "refreshInterval": 30})
        self.assertEqual({k: v for k, v in data.items() if k != "statusLine"},
                         {"model": "mé", "env": {"K": "v"}, "theme": "dark"})
        backups = self.backups()
        self.assertEqual(len(backups), 1)
        self.assertEqual(self.read(backups[0]), original)
        self.assertEqual(stat.S_IMODE(os.stat(backups[0]).st_mode), 0o600)
        # the backup folder and every parent up to the state dir are private
        folder = os.path.dirname(backups[0])
        while folder != self.tmp:
            self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), 0o700, folder)
            folder = os.path.dirname(folder)

    def test_trailing_newline_and_tab_indent_are_preserved(self):
        self.write(self.claude_settings, '{\n\t"model": "m"\n}\n')
        self.run_cmd(*self.install_args("--tools", "claude"))
        text = self.read(self.claude_settings)
        self.assertTrue(text.endswith("}\n"))
        self.assertIn('\n\t"statusLine"', text)

    def test_custom_refresh_interval(self):
        os.makedirs(os.path.dirname(self.claude_settings))
        self.run_cmd(*self.install_args("--tools", "claude", "--refresh-interval", "45"))
        self.assertEqual(json.loads(self.read(self.claude_settings))["statusLine"]["refreshInterval"], 45)

    def test_fresh_install_creates_file_and_second_run_is_a_no_op(self):
        os.makedirs(os.path.dirname(self.claude_settings))
        rc, _ = self.run_cmd(*self.install_args())
        self.assertEqual(rc, 0)
        first = self.read(self.claude_settings)
        self.assertEqual(stat.S_IMODE(os.stat(self.claude_settings).st_mode), 0o600)
        self.assertTrue(first.endswith("\n"))
        self.assertEqual(self.backups(), [])                      # nothing existed to back up
        manifest_before = self.manifest()
        rc, out = self.run_cmd(*self.install_args())
        self.assertEqual(rc, 0)
        self.assertIn("Already up to date", out)
        self.assertEqual(self.read(self.claude_settings), first)
        self.assertEqual(self.manifest(), manifest_before)
        self.assertEqual(self.backups(), [])

    def test_missing_tool_directories_are_skipped_by_default(self):
        rc, out = self.run_cmd(*self.install_args())
        self.assertEqual(rc, 0)
        self.assertIn("[claude] skipped", out)
        self.assertFalse(os.path.exists(self.claude_settings))

    def test_explicit_tools_create_missing_directories(self):
        rc, _ = self.run_cmd(*self.install_args("--tools", "claude"))
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isfile(self.claude_settings))

    def test_foreign_statusline_blocks_everything_without_force(self):
        foreign = dump({"model": "m", "statusLine": {"type": "command", "command": "my-own-line.sh"}})
        self.write(self.claude_settings, foreign)
        self.write(self.agy_settings, dump({"model": "g"}))
        rc, out = self.run_cmd(*self.install_args())
        self.assertEqual(rc, 1)
        self.assertIn("not ctxhud's", out)
        self.assertIn("Nothing was changed", out)
        self.assertEqual(self.read(self.claude_settings), foreign)
        self.assertEqual(json.loads(self.read(self.agy_settings)), {"model": "g"})     # untouched too
        self.assertFalse(os.path.exists(self.state))

    def test_force_replaces_with_backup_and_uninstall_restores_exactly(self):
        foreign = dump({"model": "m", "statusLine": {"type": "command", "command": "my-own-line.sh", "padding": 1},
                        "theme": "dark"})
        self.write(self.claude_settings, foreign)
        rc, _ = self.run_cmd(*self.install_args("--tools", "claude", "--force"))
        self.assertEqual(rc, 0)
        self.assertIn("bridge claude", self.read(self.claude_settings))
        self.assertEqual(self.read(self.backups()[0]), foreign)
        rc, out = self.run_cmd("uninstall", "--tools", "claude")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read(self.claude_settings), foreign)
        self.assertIsNone(self.manifest())

    def test_uninstall_removes_only_ours_and_deletes_a_file_it_created(self):
        original = dump({"model": "m", "theme": "dark"})
        self.write(self.claude_settings, original)
        self.run_cmd(*self.install_args("--tools", "claude"))
        self.run_cmd("uninstall", "--tools", "claude")
        self.assertEqual(self.read(self.claude_settings), original)
        os.unlink(self.claude_settings)
        self.run_cmd(*self.install_args("--tools", "claude"))
        self.assertTrue(os.path.exists(self.claude_settings))
        self.run_cmd("uninstall", "--tools", "claude")
        self.assertFalse(os.path.exists(self.claude_settings))

    def test_uninstall_without_manifest_still_removes_ours_but_not_foreign_lines(self):
        self.write(self.claude_settings, dump({"model": "m", "statusLine": {
            "type": "command", "command": "/somewhere/bin/ctxhud bridge claude", "refreshInterval": 30}}))
        rc, _ = self.run_cmd("uninstall", "--tools", "claude")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(self.read(self.claude_settings)), {"model": "m"})
        foreign = dump({"statusLine": {"type": "command", "command": "mine.sh"}})
        self.write(self.claude_settings, foreign)
        self.run_cmd("uninstall", "--tools", "claude")
        self.assertEqual(self.read(self.claude_settings), foreign)

    def test_reinstall_updates_the_executable_but_keeps_user_tuning(self):
        self.write(self.claude_settings, dump({"statusLine": {
            "type": "command", "command": "/old/place/ctxhud bridge claude", "refreshInterval": 99, "padding": 2}}))
        rc, _ = self.run_cmd(*self.install_args("--tools", "claude"))
        self.assertEqual(rc, 0)
        status = json.loads(self.read(self.claude_settings))["statusLine"]
        self.assertEqual(status["command"], "%s bridge claude" % self.exe)
        self.assertEqual((status["refreshInterval"], status["padding"]), (99, 2))

    def test_invalid_json_is_never_overwritten(self):
        self.write(self.claude_settings, '{ "model": "m", // comment\n}')
        rc, out = self.run_cmd(*self.install_args("--tools", "claude"))
        self.assertEqual(rc, 1)
        self.assertIn("not strict JSON", out)
        self.assertEqual(self.read(self.claude_settings), '{ "model": "m", // comment\n}')

    def test_symlinked_settings_edit_the_target(self):
        real = os.path.join(self.tmp, "dotfiles", "claude-settings.json")
        self.write(real, dump({"model": "m"}))
        os.makedirs(os.path.dirname(self.claude_settings))
        os.symlink(real, self.claude_settings)
        rc, _ = self.run_cmd(*self.install_args("--tools", "claude"))
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.islink(self.claude_settings))
        self.assertIn("statusLine", json.loads(self.read(real)))

    def test_claude_config_dir_environment_variable_is_honoured(self):
        other = os.path.join(self.tmp, "elsewhere")
        os.environ["CLAUDE_CONFIG_DIR"] = other
        os.makedirs(other)
        rc, _ = self.run_cmd(*self.install_args("--tools", "claude"))
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isfile(os.path.join(other, "settings.json")))
        self.assertFalse(os.path.exists(self.claude_settings))

    def test_a_failed_write_rolls_back_files_already_written(self):
        first = dump({"model": "m"})
        self.write(self.claude_settings, first)
        self.write(self.agy_settings, dump({"model": "g"}))
        real_write = install._write_text
        calls = []

        def flaky(path, text, mode=None):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("disk full")
            return real_write(path, text, mode)

        with mock.patch.object(install, "_write_text", flaky):
            rc, out = self.run_cmd(*self.install_args("--tools", "claude,agy"))
        self.assertEqual(rc, 1)
        self.assertIn("rolled back", out)
        self.assertEqual(self.read(self.claude_settings), first)
        self.assertEqual(json.loads(self.read(self.agy_settings)), {"model": "g"})
        self.assertIsNone(self.manifest())


class HandFormattedJsonTests(InstallTestCase):
    """Files that are not in canonical json.dumps form must survive byte for byte."""

    CASES = {
        "four-space indent, inline arrays, escapes, trailing newline":
            '{\n    "permissions": {"allow": ["Bash(ls)", "Read"], "deny": []},\n    "model": "m\\u00e9",\n'
            '    "env": {"TOKEN": "hunter2-secret"}\n}\n',
        "crlf line endings":
            '{\r\n  "model": "m",\r\n  "theme": "dark"\r\n}\r\n',
        "single line": '{"model":"m","theme":"dark"}',
        "empty object": "{}",
        "empty object with whitespace": "{\n}\n",
        "odd spacing": '{ "a" :  1 ,\n\n   "b":[1,\n2] }\n',
        "leading blank lines": '\n\n{\n  "a": 1\n}\n',
    }

    def test_install_and_uninstall_round_trip_every_shape_exactly(self):
        for label, original in self.CASES.items():
            self.write(self.claude_settings, original)
            rc, out = self.run_cmd(*self.install_args("--tools", "claude"))
            self.assertEqual(rc, 0, (label, out))
            installed = self.read(self.claude_settings)
            data = json.loads(installed)
            self.assertEqual(data["statusLine"]["command"], "%s bridge claude" % self.exe, label)
            self.assertEqual({k: v for k, v in data.items() if k != "statusLine"},
                             json.loads(original), label)
            if original.strip() not in ("{}", "{\n}"):
                # the original text is still in the file, byte for byte, after the inserted member
                tail = original[original.index("{") + 1:]
                self.assertTrue(installed.endswith(tail), label)
            rc, out = self.run_cmd("uninstall", "--tools", "claude")
            self.assertEqual(rc, 0, (label, out))
            expected = original if label != "empty object with whitespace" else "{}\n"
            self.assertEqual(self.read(self.claude_settings), expected, label)
            shutil.rmtree(self.state, ignore_errors=True)

    def test_dry_run_diff_of_a_hand_formatted_file_has_no_removed_lines(self):
        original = ('{\n    "permissions": {"allow": ["Bash(ls)"]},\n    "env": {"TOKEN": "hunter2-secret"}\n}\n')
        self.write(self.claude_settings, original)
        rc, out = self.run_cmd(*self.install_args("--dry-run", "--tools", "claude"))
        self.assertEqual(rc, 0)
        self.assertNotIn("hunter2", out)
        self.assertNotIn("permissions", out)
        removed = [l for l in out.splitlines() if l.startswith("-") and not l.startswith("---")]
        self.assertEqual(removed, [])
        self.assertTrue(any(l.startswith('+    "statusLine"') for l in out.splitlines()), out)

    def test_statusline_in_the_middle_or_at_the_end_is_replaced_and_removed_cleanly(self):
        foreign_middle = '{\n  "a": 1,\n  "statusLine": {"type": "command", "command": "mine"},\n  "b": 2\n}\n'
        foreign_last = '{\n  "a": 1,\n  "statusLine": {"type": "command", "command": "mine"}\n}\n'
        for original in (foreign_middle, foreign_last):
            self.write(self.claude_settings, original)
            self.assertEqual(self.run_cmd(*self.install_args("--tools", "claude", "--force"))[0], 0)
            data = json.loads(self.read(self.claude_settings))
            self.assertEqual(list(data), list(json.loads(original)))             # same key order
            self.assertEqual(self.run_cmd("uninstall", "--tools", "claude")[0], 0)
            self.assertEqual(self.read(self.claude_settings), original)
            shutil.rmtree(self.state, ignore_errors=True)

    def test_removing_our_statusline_from_the_middle_or_end_keeps_the_rest(self):
        ours = '{"type": "command", "command": "/p/ctxhud bridge claude", "refreshInterval": 30}'
        cases = (
            ('{\n  "a": 1,\n  "statusLine": %s,\n  "b": 2\n}\n' % ours, '{\n  "a": 1,\n  "b": 2\n}\n'),
            ('{\n  "a": 1,\n  "statusLine": %s\n}\n' % ours, '{\n  "a": 1\n}\n'),
            ('{"a":1,"statusLine":%s}' % ours, '{"a":1}'),
            ('{\n  "statusLine": %s\n}\n' % ours, "{}\n"),
        )
        for original, expected in cases:
            self.write(self.claude_settings, original)
            self.assertEqual(self.run_cmd("uninstall", "--tools", "claude")[0], 0)
            self.assertEqual(self.read(self.claude_settings), expected, original)

    def test_the_layout_scanner_handles_nested_values_and_strings_with_braces(self):
        text = '{"a": {"x": [1, {"y": "}"}]}, "statusLine": "s,}", "b": "{"}'
        _open, _close, members = install._layout(text)
        self.assertEqual([m["key"] for m in members], ["a", "statusLine", "b"])
        self.assertEqual(text[members[1]["val_start"]:members[1]["val_end"]], '"s,}"')
        self.assertEqual(install._surgical_remove(text), '{"a": {"x": [1, {"y": "}"}]}, "b": "{"}')

    def test_an_unverifiable_edit_falls_back_to_canonical_formatting(self):
        self.write(self.claude_settings, '{"a": 1}')
        with mock.patch.object(install, "_surgical_set", lambda text, value, raw=None: '{"broken": true}'):
            rc, _ = self.run_cmd(*self.install_args("--tools", "claude"))
        data = json.loads(self.read(self.claude_settings))
        self.assertEqual(rc, 0)
        self.assertEqual(data["a"], 1)
        self.assertNotIn("broken", data)
        self.assertIn("statusLine", data)


class AgyTests(InstallTestCase):
    def test_capture_command_is_upgraded_to_the_bridge_without_force(self):
        self.write(self.agy_settings, dump({"model": "g", "statusLine": {
            "type": "command", "command": "/x/bin/ctxhud capture agy", "enabled": True}}))
        rc, out = self.run_cmd(*self.install_args("--tools", "agy"))
        self.assertEqual(rc, 0, out)
        status = json.loads(self.read(self.agy_settings))["statusLine"]
        self.assertEqual(status, {"type": "command", "command": "%s bridge agy" % self.exe, "enabled": True})
        self.run_cmd("uninstall", "--tools", "agy")
        self.assertEqual(json.loads(self.read(self.agy_settings)), {"model": "g"})

    def test_agy_home_override(self):
        os.environ["CTXHUD_AGY_HOME"] = os.path.join(self.tmp, "agyhome")
        os.makedirs(os.environ["CTXHUD_AGY_HOME"])
        self.run_cmd(*self.install_args("--tools", "agy"))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "agyhome", "settings.json")))


class CodexTomlTests(InstallTestCase):
    def setUp(self):
        super().setUp()
        os.makedirs(os.path.dirname(self.codex_config))

    def install_codex(self, *extra):
        return self.run_cmd(*self.install_args("--tools", "codex", *extra))

    def test_appends_a_tui_table_and_uninstall_restores_the_original_bytes(self):
        original = 'model = "m"\n\n[mcp_servers.demo]\ncommand = "x"  # keep me\n'
        self.write(self.codex_config, original)
        rc, out = self.install_codex()
        self.assertEqual(rc, 0, out)
        text = self.read(self.codex_config)
        self.assertTrue(text.startswith(original))
        self.assertTrue(text.endswith("\n[tui]\nstatus_line = %s  # ctxhud\n" % EXPECTED_ITEMS))
        rc, _ = self.run_cmd("uninstall", "--tools", "codex")
        self.assertEqual(rc, 0)
        self.assertEqual(self.read(self.codex_config), original)

    def test_inserts_into_an_existing_tui_table_after_its_last_key(self):
        original = ('[tui]\nscreen_reader_detection_done = true\n\n[other]\nkeep = 1\n')
        self.write(self.codex_config, original)
        self.install_codex()
        self.assertEqual(self.read(self.codex_config),
                         '[tui]\nscreen_reader_detection_done = true\nstatus_line = %s  # ctxhud\n\n[other]\nkeep = 1\n'
                         % EXPECTED_ITEMS)
        self.run_cmd("uninstall", "--tools", "codex")
        self.assertEqual(self.read(self.codex_config), original)

    def test_existing_table_header_survives_uninstall_when_it_was_not_created_by_us(self):
        self.write(self.codex_config, "[tui]\n")
        self.install_codex()
        self.run_cmd("uninstall", "--tools", "codex")
        self.assertEqual(self.read(self.codex_config), "[tui]\n")

    def test_brackets_inside_arrays_and_strings_do_not_look_like_tables(self):
        original = ('[mcp]\nargs = [\n  ["a"]\n]\nnote = """\n[tui]\nstatus_line = ["decoy"]\n"""\n\n'
                    '[tui]\nsetting = 1\n')
        self.write(self.codex_config, original)
        rc, _ = self.install_codex()
        self.assertEqual(rc, 0)
        text = self.read(self.codex_config)
        self.assertIn('status_line = ["decoy"]\n"""', text)                      # decoy untouched
        self.assertTrue(text.endswith("[tui]\nsetting = 1\nstatus_line = %s  # ctxhud\n" % EXPECTED_ITEMS))

    def test_foreign_status_line_needs_force_and_is_restored_exactly(self):
        original = '[tui]\nstatus_line = [\n  "model-name",\n  "git-branch",\n]\nother = 2\n'
        self.write(self.codex_config, original)
        rc, out = self.install_codex()
        self.assertEqual(rc, 1)
        self.assertIn("--force", out)
        self.assertEqual(self.read(self.codex_config), original)
        rc, _ = self.install_codex("--force")
        self.assertEqual(rc, 0)
        self.assertEqual(self.read(self.codex_config),
                         '[tui]\nstatus_line = %s  # ctxhud\nother = 2\n' % EXPECTED_ITEMS)
        self.run_cmd("uninstall", "--tools", "codex")
        self.assertEqual(self.read(self.codex_config), original)

    def test_dotted_or_inline_tui_definitions_are_refused(self):
        for original in ('tui.status_line = ["a"]\n', 'tui = { status_line = ["a"] }\n'):
            self.write(self.codex_config, original)
            rc, out = self.install_codex()
            self.assertEqual(rc, 1, original)
            self.assertIn("by hand", out)
            self.assertEqual(self.read(self.codex_config), original)

    def test_identical_unmarked_value_is_left_alone(self):
        original = '[tui]\nstatus_line = %s\n' % EXPECTED_ITEMS
        self.write(self.codex_config, original)
        rc, out = self.install_codex()
        self.assertEqual(rc, 0)
        self.assertIn("not managed", out)
        self.assertEqual(self.read(self.codex_config), original)
        self.run_cmd("uninstall", "--tools", "codex")
        self.assertEqual(self.read(self.codex_config), original)

    def test_second_run_is_a_no_op_and_an_outdated_line_is_upgraded(self):
        self.write(self.codex_config, "[tui]\nx = 1\n")
        self.install_codex()
        rc, out = self.install_codex()
        self.assertIn("Already up to date", out)
        text = self.read(self.codex_config).replace('"weekly-limit"', '"old-item"')
        self.write(self.codex_config, text)
        self.install_codex()
        self.assertIn('"weekly-limit"', self.read(self.codex_config))
        self.assertEqual(self.read(self.codex_config).count("status_line"), 1)

    def test_crlf_files_keep_their_line_endings(self):
        self.write(self.codex_config, 'model = "m"\r\n[tui]\r\nx = 1\r\n')
        self.install_codex()
        text = self.read(self.codex_config)
        self.assertNotIn("\n", text.replace("\r\n", ""))
        self.assertIn('x = 1\r\nstatus_line = ', text)

    def test_file_created_for_the_install_is_removed_by_uninstall(self):
        rc, _ = self.install_codex()
        self.assertEqual(rc, 0)
        self.assertEqual(self.read(self.codex_config), "[tui]\nstatus_line = %s  # ctxhud\n" % EXPECTED_ITEMS)
        self.run_cmd("uninstall", "--tools", "codex")
        self.assertFalse(os.path.exists(self.codex_config))

    def test_codex_home_override(self):
        os.environ["CODEX_HOME"] = os.path.join(self.tmp, "chome")
        os.makedirs(os.environ["CODEX_HOME"])
        self.install_codex()
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "chome", "config.toml")))

    def test_dry_run_output_shows_only_the_added_line(self):
        self.write(self.codex_config, 'api_key_note = "do-not-print"\n[tui]\nx = 1\n')
        rc, out = self.install_codex("--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("+status_line", out)
        self.assertNotIn("do-not-print", out)


class TmuxTests(InstallTestCase):
    def install_tmux(self, *extra):
        return self.run_cmd(*self.install_args("--tools", "tmux", *extra))

    def test_creates_the_config_with_one_marked_block(self):
        rc, out = self.install_tmux()
        self.assertEqual(rc, 0, out)
        text = self.read(self.tmux_conf)
        self.assertEqual(text.count(install.BLOCK_BEGIN), 1)
        self.assertEqual(text.count(install.BLOCK_END), 1)
        self.assertIn("set-option -g pane-border-status bottom", text)
        self.assertIn("#(%s codex-border #{pane_id} --width #{pane_width})" % self.exe, text)
        self.assertIn("#{==:#{pane_current_command},codex}", text)
        self.assertIn("reload tmux with `tmux source-file", out)
        self.assertEqual(self.tmux_calls, [])                    # install never talks to a tmux server

    def test_non_codex_panes_show_only_their_title_with_a_space_on_each_side(self):
        self.install_tmux()
        text = self.read(self.tmux_conf)
        self.assertIn("set-option -g pane-border-format "
                      "'#{?#{==:#{pane_current_command},codex},"
                      "#(%s codex-border #{pane_id} --width #{pane_width}), #{pane_title} }'" % self.exe, text)
        for gone in ("pane_index", "pane_active", "reverse", "#[default]"):
            self.assertNotIn(gone, text)

    def test_codex_border_receives_the_pane_id_first_and_the_pane_width_option_once(self):
        self.install_tmux()
        text = self.read(self.tmux_conf)
        self.assertEqual(text.count("--width #{pane_width}"), 1)
        self.assertEqual(text.count("codex-border #{pane_id} --width #{pane_width}"), 1)
        self.assertEqual(text.count("#("), 1)                    # still a single external command

    def test_the_installed_command_is_accepted_by_codex_border(self):
        """Contract with codex.py: the exact command tmux would run must parse (not 'unresolved')."""
        import shlex
        from ctxhud import codex
        self.install_tmux()
        text = self.read(self.tmux_conf)
        start = text.index("#(") + 2
        command = text[start:text.index("),", start)]
        expanded = command.replace("#{pane_id}", "%7").replace("#{pane_width}", "120")
        self.assertEqual(shlex.split(expanded), [self.exe, "codex-border", "%7", "--width", "120"])
        seen = []

        def fake_collect(pane):
            seen.append(pane)
            return {"catching_up": True}

        out = io.StringIO()
        with mock.patch.object(codex, "collect_for_pane", fake_collect), mock.patch("sys.stdout", out):
            rc = codex.main(shlex.split(expanded)[1:])        # what `ctxhud` receives after its own name
        self.assertEqual(rc, 0)
        self.assertEqual(seen, ["%7"])                        # the pane id was found, the width option consumed
        self.assertIn("catching-up", out.getvalue())
        self.assertNotIn("unresolved", out.getvalue())

    GENERATIONS = (1, 2)    # 1: pane number + reverse video, 2: title only without --width

    @staticmethod
    def previous_block(exe, nl="\n", generation=1):
        """The block as an earlier release wrote it."""
        if generation == 1:        # first release: pane number + active reverse video
            fmt = ("#{?#{==:#{pane_current_command},codex},#(%s codex-border #{pane_id}),"
                   "#{?pane_active,#[reverse],}#{pane_index}#[default] \"#{pane_title}\"}" % exe)
        else:                      # second release: just the title, codex-border without a width
            fmt = ("#{?#{==:#{pane_current_command},codex},#(%s codex-border #{pane_id}),"
                   " #{pane_title} }" % exe)
        return nl.join([
            install.BLOCK_BEGIN + " (managed by `ctxhud install`; `ctxhud uninstall` removes it)",
            "set-option -g pane-border-status bottom",
            "set-option -g pane-border-format '%s'" % fmt,
            install.BLOCK_END,
        ]) + nl

    def test_an_installed_block_of_every_earlier_format_is_upgraded_in_place(self):
        before, after = "set -g mouse on\n\n", "\nbind r source-file ~/.tmux.conf\n"
        for generation in self.GENERATIONS:
            with self.subTest(generation=generation):
                shutil.rmtree(self.state, ignore_errors=True)
                old = before + self.previous_block(self.exe, generation=generation) + after
                self.write(self.tmux_conf, old)
                manifest = {"version": 1, "tools": {"tmux": {"path": self.tmux_conf, "added_blank": True}}}
                common.atomic_write_json(install.manifest_path(), manifest)
                rc, out = self.install_tmux()
                self.assertEqual(rc, 0, out)
                self.assertIn("[tmux]", out)
                self.assertIn("upgrade", out)
                text = self.read(self.tmux_conf)
                self.assertEqual(text, before + install.tmux_block(self.exe) + after)   # same spot, user lines intact
                self.assertEqual(text.count(install.BLOCK_BEGIN), 1)
                self.assertIn("codex-border #{pane_id} --width #{pane_width}", text)
                self.assertNotIn("codex-border #{pane_id})", text)                      # the no-width call is gone
                for gone in ("pane_index", "pane_active", "reverse"):
                    self.assertNotIn(gone, text)
                self.assertEqual(len(self.backups()), 1)                               # the old file was backed up
                self.assertEqual(self.read(self.backups()[0]), old)
                # the diff changes only the format line
                changed = [l for l in out.splitlines() if l[:1] in "+-" and l[:3] not in ("+++", "---")]
                self.assertEqual(len(changed), 2, out)
                self.assertTrue(all("pane-border-format" in l for l in changed), changed)
                # a second run is a no-op, and uninstall removes exactly the block
                rc, out = self.install_tmux()
                self.assertIn("Already up to date", out)
                self.assertEqual(self.read(self.tmux_conf), before + install.tmux_block(self.exe) + after)
                self.assertEqual(len(self.backups()), 1)
                self.assertEqual(self.run_cmd("uninstall", "--tools", "tmux")[0], 0)
                self.assertEqual(self.read(self.tmux_conf), "set -g mouse on\n" + after)

    def test_an_installed_block_is_upgraded_even_when_the_executable_also_changed(self):
        for generation in self.GENERATIONS:
            with self.subTest(generation=generation):
                self.write(self.tmux_conf, self.previous_block("/old/place/bin/ctxhud", generation=generation))
                rc, _ = self.install_tmux()
                text = self.read(self.tmux_conf)
                self.assertEqual(rc, 0)
                self.assertEqual(text, install.tmux_block(self.exe))
                self.assertNotIn("/old/place", text)

    def test_earlier_formats_with_crlf_line_endings_are_upgraded(self):
        for generation in self.GENERATIONS:
            with self.subTest(generation=generation):
                self.write(self.tmux_conf,
                           "set -g mouse on\r\n" + self.previous_block(self.exe, "\r\n", generation))
                self.assertEqual(self.install_tmux()[0], 0)
                self.assertEqual(self.read(self.tmux_conf),
                                 "set -g mouse on\r\n" + install.tmux_block(self.exe, "\r\n"))

    def test_the_current_block_is_not_rewritten_by_a_plain_rerun(self):
        self.write(self.tmux_conf, "set -g mouse on\n")
        self.install_tmux()
        first = self.read(self.tmux_conf)
        before = os.stat(self.tmux_conf).st_mtime_ns
        backups_before = self.backups()                                    # one, from the first install
        rc, out = self.install_tmux()
        self.assertEqual((rc, self.read(self.tmux_conf)), (0, first))
        self.assertEqual(os.stat(self.tmux_conf).st_mtime_ns, before)       # untouched, not just equal
        self.assertEqual(self.backups(), backups_before)                   # and no new backup

    def test_existing_config_is_kept_and_uninstall_restores_it_byte_for_byte(self):
        for original in ("set -g mouse on\nbind r source-file ~/.tmux.conf\n", "set -g mouse on",
                         "set -g mouse on\n\n"):
            self.write(self.tmux_conf, original)
            self.assertEqual(self.install_tmux()[0], 0)
            self.assertTrue(self.read(self.tmux_conf).startswith(original.rstrip("\n")))
            self.assertEqual(self.run_cmd("uninstall", "--tools", "tmux")[0], 0)
            self.assertEqual(self.read(self.tmux_conf).rstrip("\n"), original.rstrip("\n"), repr(original))

    def test_idempotent_and_executable_change_replaces_the_block_in_place(self):
        self.write(self.tmux_conf, "set -g mouse on\n")
        self.install_tmux()
        first = self.read(self.tmux_conf)
        rc, out = self.install_tmux()
        self.assertIn("Already up to date", out)
        self.assertEqual(self.read(self.tmux_conf), first)
        other = self.make_exe(os.path.join(self.tmp, "newbin", "ctxhud"))
        rc, _ = self.run_cmd("install", "--exe", other, "--tools", "tmux")
        text = self.read(self.tmux_conf)
        self.assertEqual(rc, 0)
        self.assertEqual(text.count(install.BLOCK_BEGIN), 1)
        self.assertIn(other, text)
        self.assertNotIn(self.exe, text)

    def test_user_defined_pane_border_options_conflict_unless_forced(self):
        original = "set -g pane-border-status top\n"
        self.write(self.tmux_conf, original)
        rc, out = self.install_tmux()
        self.assertEqual(rc, 1)
        self.assertIn("line 1", out)
        self.assertEqual(self.read(self.tmux_conf), original)
        rc, _ = self.install_tmux("--force")
        self.assertEqual(rc, 0)
        self.run_cmd("uninstall", "--tools", "tmux")
        self.assertEqual(self.read(self.tmux_conf), original)

    def test_commented_out_options_are_not_conflicts(self):
        self.write(self.tmux_conf, "# set -g pane-border-status top\n")
        self.assertEqual(self.install_tmux()[0], 0)

    def test_paths_tmux_formats_cannot_carry_are_refused(self):
        for name in ("bin dir", "bin,comma", "bin'quote", "bin}brace"):
            odd = self.make_exe(os.path.join(self.tmp, name, "ctxhud"))
            rc, out = self.run_cmd("install", "--exe", odd, "--tools", "tmux")
            self.assertEqual(rc, 1, name)
            self.assertIn("cannot carry", out)
        self.assertFalse(os.path.exists(self.tmux_conf))

    def test_block_without_end_marker_is_refused(self):
        self.write(self.tmux_conf, "%s\nset -g mouse on\n" % install.BLOCK_BEGIN)
        rc, out = self.install_tmux()
        self.assertEqual(rc, 1)
        self.assertIn("without its end marker", out)

    def test_xdg_config_is_used_when_only_it_exists(self):
        xdg = os.path.join(self.home, ".config", "tmux", "tmux.conf")
        self.write(xdg, "set -g mouse on\n")
        rc, _ = self.install_tmux()
        self.assertEqual(rc, 0)
        self.assertIn(install.BLOCK_BEGIN, self.read(xdg))
        self.assertFalse(os.path.exists(self.tmux_conf))

    def test_tmux_conf_option(self):
        custom = os.path.join(self.tmp, "custom.conf")
        rc, _ = self.install_tmux("--tmux-conf", custom)
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isfile(custom))
        self.assertFalse(os.path.exists(self.tmux_conf))


class WholeRunTests(InstallTestCase):
    def test_install_all_tools_then_uninstall_everything(self):
        originals = {
            self.claude_settings: dump({"model": "m"}),
            self.agy_settings: dump({"model": "g"}, True),
            self.codex_config: 'model = "x"\n[tui]\nk = 1\n',
            self.tmux_conf: "set -g mouse on\n",
        }
        for path, text in originals.items():
            self.write(path, text)
        rc, out = self.run_cmd(*self.install_args())
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(self.manifest()["tools"]), ["agy", "claude", "codex", "tmux"])
        self.assertEqual(len(self.backups()), 4)
        for path, text in originals.items():
            self.assertNotEqual(self.read(path), text)
        rc, out = self.run_cmd("uninstall")
        self.assertEqual(rc, 0, out)
        for path, text in originals.items():
            self.assertEqual(self.read(path), text, path)
        self.assertIsNone(self.manifest())

    def test_unknown_tool_and_bad_arguments(self):
        rc, out = self.run_cmd(*self.install_args("--tools", "claude,vim"))
        self.assertEqual(rc, 1)
        self.assertIn("unknown tool", out)
        rc, _ = self.run_cmd(*self.install_args("--refresh-interval", "0"))
        self.assertEqual(rc, 2)

    def test_missing_executable_is_a_clear_error(self):
        rc, out = self.run_cmd("install", "--tools", "claude")
        self.assertEqual(rc, 1)
        self.assertIn("cannot locate the ctxhud executable", out)
        rc, out = self.run_cmd("install", "--exe", os.path.join(self.tmp, "nope"), "--tools", "claude")
        self.assertEqual(rc, 1)
        self.assertIn("not an executable file", out)

    def test_executable_is_found_on_path(self):
        with mock.patch.object(install.shutil, "which", lambda name: self.exe if name == "ctxhud" else None):
            os.makedirs(os.path.dirname(self.claude_settings))
            rc, _ = self.run_cmd("install", "--tools", "claude")
        self.assertEqual(rc, 0)
        self.assertIn(self.exe, self.read(self.claude_settings))

    def test_dispatch_through_the_cli_module(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            rc = cli.main(["install", "--dry-run", "--exe", self.exe, "--tools", "claude"])
        self.assertEqual(rc, 0)
        self.assertIn("Dry run", out.getvalue())

    def test_real_home_is_never_used(self):
        self.assertTrue(os.path.expanduser("~").startswith(self.tmp))
        self.assertTrue(common.state_dir().startswith(self.tmp))


class DoctorTests(InstallTestCase):
    def test_doctor_on_an_empty_machine_warns_but_passes(self):
        rc, out = self.run_cmd("doctor")
        self.assertEqual(rc, 0, out)
        self.assertIn("[ok  ] python", out)
        self.assertIn("claude: ", out)
        self.assertIn("[warn]", out)
        self.assertIn("tmux live: server not running", out)

    def test_doctor_after_install_reports_the_wiring(self):
        self.write(self.claude_settings, dump({"model": "m"}))
        self.write(self.agy_settings, dump({"model": "g"}))
        self.write(self.codex_config, "[tui]\n")
        self.run_cmd(*self.install_args("--tools", "claude,agy,codex,tmux"))
        with mock.patch.object(install.shutil, "which", lambda name: self.exe if name == "ctxhud" else None):
            rc, out = self.run_cmd("doctor")
        self.assertEqual(rc, 0, out)
        self.assertIn("[ok  ] claude: statusLine", out)
        self.assertIn("refreshInterval 30", out)
        self.assertIn("[ok  ] agy: statusLine", out)
        self.assertIn("[ok  ] codex: [tui] status_line set by ctxhud (context-used, five-hour-limit, weekly-limit)", out)
        self.assertIn("[ok  ] tmux: ctxhud block", out)

    def test_doctor_fails_when_a_statusline_points_at_a_missing_executable(self):
        self.write(self.claude_settings, dump({"statusLine": {"type": "command",
                                                              "command": "%s/gone/ctxhud bridge claude" % self.tmp}}))
        rc, out = self.run_cmd("doctor")
        self.assertEqual(rc, 1)
        self.assertIn("[fail] claude", out)
        self.assertIn("missing or not executable", out)

    def test_doctor_warns_about_the_diagnostic_capture_command(self):
        self.write(self.agy_settings, dump({"statusLine": {"type": "command",
                                                           "command": "%s capture agy" % self.exe}}))
        rc, out = self.run_cmd("doctor")
        self.assertEqual(rc, 0)
        self.assertIn("[warn] agy: statusLine is the diagnostic `capture` command", out)

    def test_doctor_shows_cache_age_and_flags_loose_permissions(self):
        entry = {"tool": "claude", "session_id": "s1", "received_at": common.now() - 600, "observed_at": 1}
        path = common.write_cache(entry)
        os.chmod(path, 0o644)
        rc, out = self.run_cmd("doctor")
        self.assertEqual(rc, 0)
        self.assertIn("cache claude: 1 session(s), newest 10m ago, 1 file(s) readable by others", out)
        self.assertIn("cache codex: empty", out)

    def test_doctor_reports_window_level_tmux_overrides(self):
        self.tmux_answers = {
            ("show-options", "-gwv", "pane-border-format"): (0, "...codex-border..."),
            ("show-options", "-gwv", "pane-border-status"): (0, "bottom"),
            ("list-windows", "-a", "-F", "#{session_name}:#{window_index}"): (0, "demo:0\ndemo:1"),
            ("show-options", "-w", "-v", "-t", "demo:0", "pane-border-status"): (0, "top"),
            ("show-options", "-w", "-v", "-t", "demo:1", "pane-border-status"): (0, ""),
        }
        rc, out = self.run_cmd("doctor")
        self.assertEqual(rc, 0)
        self.assertIn("tmux live: pane-border-status=bottom, ctxhud format active", out)
        self.assertIn("overrides the global setting in: demo:0=top", out)
        self.assertNotIn("demo:1", out)

    def test_doctor_warns_when_the_live_tmux_has_not_loaded_the_block(self):
        self.tmux_answers = {
            ("show-options", "-gwv", "pane-border-format"): (0, "#{pane_index}"),
            ("show-options", "-gwv", "pane-border-status"): (0, "off"),
        }
        rc, out = self.run_cmd("doctor")
        self.assertIn("ctxhud format not loaded", out)
        self.assertIn("tmux source-file", out)


class TomlScannerTests(unittest.TestCase):
    def test_states_track_depth_and_multiline_strings(self):
        lines = ['a = [\n', '  1,\n', ']\n', 'b = """\n', '[tui]\n', '"""\n', '[tui]\n']
        states = install._line_states(lines)
        self.assertEqual(states[0], (0, None))
        self.assertEqual(states[1], (1, None))
        self.assertEqual(states[2], (1, None))
        self.assertEqual(states[3], (0, None))
        self.assertEqual(states[4], (0, '"""'))
        self.assertEqual(states[6], (0, None))
        self.assertEqual([name for _, name in install._headers(lines, states)], ["tui"])

    def test_comments_and_quoted_brackets_do_not_change_depth(self):
        lines = ['x = "a[b"  # [ not counted\n', "y = '[['\n", 'z = 1\n']
        self.assertEqual([s[0] for s in install._line_states(lines)], [0, 0, 0])

    def test_header_variants(self):
        text = '[ tui ]  # c\n[tui.sub]\n[["tui"]]\n["tui"]\n'
        lines = text.splitlines(keepends=True)
        names = [name for _, name in install._headers(lines, install._line_states(lines))]
        self.assertEqual(names, ["tui", "tui.sub", '[[tui', "tui"])


class OwnershipTests(unittest.TestCase):
    def test_ownership_rules(self):
        owned = ["ctxhud bridge claude", "/opt/x/bin/ctxhud bridge agy", "'/path with space/ctxhud' capture agy",
                 "/a/ctxhud codex-border %3"]
        foreign = ["ctxhud list", "ctxhud", "my-ctxhud-wrapper bridge claude", "python -m ctxhud bridge claude",
                   "echo ctxhud bridge claude", "", None, 5]
        for command in owned:
            self.assertTrue(install._is_ctxhud_command(command), command)
        for command in foreign:
            self.assertFalse(install._is_ctxhud_command(command), command)


if __name__ == "__main__":
    unittest.main()
