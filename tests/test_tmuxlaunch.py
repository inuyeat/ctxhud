"""Synthetic tmux commands; tests never attach, create or switch real sessions."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ctxhud import tmuxlaunch


class TmuxLaunchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "example.project:one"
        self.project.mkdir()
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("TMUX", None)
        self.which = mock.patch.object(tmuxlaunch.shutil, "which", return_value="/opt/tools/tmux")
        self.which.start()
        self.addCleanup(self.which.stop)

    def test_folder_name_sanitizing(self):
        for directory, expected in [
            ("/home/user/project.one:two", "project_one_two"),
            ("/home/user/project name", "project_name"),
            ("/home/user/.hidden", "_hidden"),
            ("/home/user/-example", "_-example"),
            ("/home/user/한글", "한글"),
            ("/", "root"),
            ("/home/user/line\nbreak", "line_break"),
        ]:
            with self.subTest(directory=directory):
                self.assertEqual(tmuxlaunch.session_name(directory), expected)

    def test_outside_tmux_atomic_attach_or_create(self):
        with mock.patch.object(tmuxlaunch.subprocess, "call", return_value=0) as call, \
                mock.patch.object(tmuxlaunch.subprocess, "run") as run:
            self.assertEqual(tmuxlaunch.main(["tmux", str(self.project)]), 0)
        call.assert_called_once_with(["/opt/tools/tmux", "new-session", "-A", "-s",
                                      "example_project_one", "-c", str(self.project.resolve())])
        run.assert_not_called()

    def test_inside_existing_session_switches_without_creating_or_detaching(self):
        os.environ["TMUX"] = "synthetic-socket,101,0"
        with mock.patch.object(tmuxlaunch.subprocess, "run", return_value=mock.Mock(returncode=0)) as run, \
                mock.patch.object(tmuxlaunch.subprocess, "call", return_value=0) as call:
            self.assertEqual(tmuxlaunch.main([str(self.project)]), 0)
        self.assertEqual(run.call_args.args[0], ["/opt/tools/tmux", "has-session", "-t", "=example_project_one"])
        call.assert_called_once_with(["/opt/tools/tmux", "switch-client", "-t", "=example_project_one"])

    def test_inside_new_session_is_created_detached_then_switched(self):
        os.environ["TMUX"] = "synthetic-socket,101,0"
        with mock.patch.object(tmuxlaunch.subprocess, "run", side_effect=[mock.Mock(returncode=1), mock.Mock(returncode=0)]) as run, \
                mock.patch.object(tmuxlaunch.subprocess, "call", return_value=0) as call:
            self.assertEqual(tmuxlaunch.launch(str(self.project)), 0)
        self.assertEqual(run.call_args_list[1].args[0], ["/opt/tools/tmux", "new-session", "-d", "-s",
                                                      "example_project_one", "-c", str(self.project.resolve())])
        call.assert_called_once_with(["/opt/tools/tmux", "switch-client", "-t", "=example_project_one"])

    def test_creation_race_rechecks_exact_session(self):
        os.environ["TMUX"] = "synthetic-socket,101,0"
        with mock.patch.object(tmuxlaunch.subprocess, "run", side_effect=[mock.Mock(returncode=1), mock.Mock(returncode=1), mock.Mock(returncode=0)]) as run, \
                mock.patch.object(tmuxlaunch.subprocess, "call", return_value=0):
            self.assertEqual(tmuxlaunch.launch(str(self.project)), 0)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args_list[2].args[0][1], "has-session")

    def test_creation_error_does_not_switch(self):
        os.environ["TMUX"] = "synthetic-socket,101,0"
        with mock.patch.object(tmuxlaunch.subprocess, "run", return_value=mock.Mock(returncode=1)), \
                mock.patch.object(tmuxlaunch.subprocess, "call") as call, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(tmuxlaunch.main([str(self.project)]), 1)
        call.assert_not_called()
        self.assertIn("cannot create", stderr.getvalue())

    def test_default_directory_is_cwd(self):
        with mock.patch.object(tmuxlaunch.os, "getcwd", return_value=str(self.project)), \
                mock.patch.object(tmuxlaunch, "launch", return_value=0) as launch:
            self.assertEqual(tmuxlaunch.main(["tmux"]), 0)
        launch.assert_called_once_with(str(self.project))

    def test_nonexistent_path_never_runs_tmux(self):
        with mock.patch.object(tmuxlaunch.subprocess, "call") as call, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(tmuxlaunch.main([str(self.project / "missing")]), 1)
        call.assert_not_called()
        self.assertIn("directory", stderr.getvalue())

    def test_file_is_not_a_project_directory(self):
        path = self.project / "file"
        path.write_text("synthetic")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(tmuxlaunch.main([str(path)]), 1)

    def test_missing_tmux_is_actionable(self):
        with mock.patch.object(tmuxlaunch.shutil, "which", return_value=None), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(tmuxlaunch.main([str(self.project)]), 1)
        self.assertIn("tmux is not installed", stderr.getvalue())

    def test_arguments_do_not_go_through_a_shell(self):
        directory = self.project / "literal;$(nothing)"
        directory.mkdir()
        with mock.patch.object(tmuxlaunch.subprocess, "call", return_value=0) as call:
            self.assertEqual(tmuxlaunch.launch(str(directory)), 0)
        self.assertIsInstance(call.call_args.args[0], list)
        self.assertFalse(call.call_args.kwargs.get("shell", False))
        self.assertEqual(call.call_args.args[0][-1], str(directory.resolve()))

    def test_return_code_and_timeout(self):
        with mock.patch.object(tmuxlaunch.subprocess, "call", return_value=3):
            self.assertEqual(tmuxlaunch.main([str(self.project)]), 3)
        os.environ["TMUX"] = "synthetic-socket,101,0"
        with mock.patch.object(tmuxlaunch.subprocess, "run", side_effect=subprocess.TimeoutExpired("tmux", 5)), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(tmuxlaunch.main([str(self.project)]), 1)

    def test_help_and_invalid_arguments(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tmuxlaunch.main(["tmux", "--help"]), 0)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(tmuxlaunch.main(["one", "two"]), 2)


if __name__ == "__main__":
    unittest.main()
