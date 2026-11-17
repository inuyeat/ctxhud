"""Tests for tests/tools/anonymize.py (Python 3.9 compatible, standard library only)."""
from __future__ import annotations

import copy
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "tools"))

import anonymize  # noqa: E402

# Built from pieces so that this public test file itself contains no real-looking local path.
HOME = "/" + "Users" + "/jdoe"
ENCODED = "-" + "Users" + "-jdoe-secret-project"
REAL_UUID = "3f2b8c1e-9d4a-4e6f-8a1b-2c3d4e5f6a7b"  # Synthetic UUID tests anonymization. # public-scan: allow
OTHER_UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"  # Synthetic UUID tests distinct identifier mapping. # public-scan: allow


def sample():
    return {
        "session_id": REAL_UUID,
        "prompt_id": OTHER_UUID,
        "transcript_path": "%s/.claude/projects/%s/%s.jsonl" % (HOME, ENCODED, REAL_UUID),
        "cwd": HOME + "/Documents/secret-project",
        "scratchpad_dir": "/private/tmp/claude-501/%s/%s/scratchpad" % (ENCODED, REAL_UUID),
        "session_name": "my private session title",
        "email": "jdoe@corp.example",
        "note": "contact jdoe@corp.example about %s/file.txt" % HOME,
        "model": {"id": "some-model", "display_name": "Some Model"},
        "workspace": {"current_dir": HOME + "/Documents/secret-project", "added_dirs": [HOME + "/other"],
                      "repo": {"host": "example.org", "owner": "jdoe", "name": "secret-repo"}},
        "version": "1.2.3",
        "cost": {"total_cost_usd": 4.6245942, "total_duration_ms": 16970157, "total_lines_added": 666},
        "context_window": {"total_input_tokens": 244391, "context_window_size": 1000000,
                           "used_percentage": 24, "remaining_percentage": 76,
                           "current_usage": {"input_tokens": 3, "cache_read_input_tokens": 242939}},
        "prompt_cache": {"expires_at": 1763373844, "last_miss_at": 1763370011, "hit_ratio": 0.9481831778,
                         "requests": 59, "warm": True},
        "rate_limits": {"five_hour": {"used_percentage": 13, "resets_at": 1763387387},
                        "seven_day": {"used_percentage": 14, "resets_at": 1763863187}},
    }


class AnonymizeTests(unittest.TestCase):
    def test_no_identifying_text_survives(self):
        out = anonymize.anonymize(sample())
        text = json.dumps(out)
        for secret in (REAL_UUID, OTHER_UUID, "jdoe", "secret", "corp.example", "private session", "claude-501",
                       "Users"):
            self.assertNotIn(secret, text)
        self.assertEqual(anonymize.find_leaks(out), [])

    def test_identifiers_become_stable_fake_uuids(self):
        out = anonymize.anonymize(sample())
        self.assertEqual(out["session_id"], "00000000-0000-4000-8000-000000000001")
        self.assertEqual(out["prompt_id"], "00000000-0000-4000-8000-000000000002")
        self.assertIn(out["session_id"], out["transcript_path"])      # same id -> same fake everywhere
        self.assertEqual(anonymize.anonymize(sample()), out)           # deterministic

    def test_paths_and_names(self):
        out = anonymize.anonymize(sample())
        self.assertEqual(out["cwd"], "/home/user/project")
        self.assertEqual(out["workspace"]["current_dir"], "/home/user/project")
        self.assertEqual(out["workspace"]["added_dirs"], ["/home/user/project"])
        self.assertEqual(out["scratchpad_dir"], "/home/user/project")
        self.assertEqual(out["workspace"]["repo"]["owner"], "owner")
        self.assertEqual(out["workspace"]["repo"]["name"], "repo")
        self.assertEqual(out["email"], "user@example.com")
        self.assertEqual(out["note"], "contact user@example.com about /home/user/file.txt")
        self.assertEqual(out["session_name"], "session")

    def test_non_identifying_fields_are_kept(self):
        out = anonymize.anonymize(sample())
        self.assertEqual(out["model"], {"id": "some-model", "display_name": "Some Model"})
        self.assertEqual(out["version"], "1.2.3")
        self.assertEqual(out["context_window"]["context_window_size"], 1000000)
        self.assertIs(out["prompt_cache"]["warm"], True)

    def test_coarse_numbers_are_rounded_but_consistent(self):
        cw = anonymize.anonymize(sample())["context_window"]
        self.assertEqual(cw["used_percentage"], 25)               # nearest 5
        self.assertEqual(cw["remaining_percentage"], 75)          # stays 100 - used
        self.assertEqual(cw["total_input_tokens"], 250000)        # stays used * window
        self.assertEqual(anonymize.anonymize(sample())["cost"]["total_cost_usd"], 4.6)
        self.assertEqual(anonymize.anonymize(sample())["prompt_cache"]["hit_ratio"], 0.95)

    def test_keep_mode_leaves_usage_numbers_alone(self):
        original = sample()
        out = anonymize.anonymize(original, numbers="keep")
        self.assertEqual(out["context_window"], original["context_window"])
        self.assertEqual(out["cost"], original["cost"])

    def test_timestamps_shift_together_and_keep_their_distances(self):
        original = sample()
        out = anonymize.anonymize(original)
        self.assertEqual(out["prompt_cache"]["last_miss_at"], anonymize.EPOCH_BASE)   # earliest -> base
        orig_gap = original["rate_limits"]["seven_day"]["resets_at"] - original["rate_limits"]["five_hour"]["resets_at"]
        new_gap = out["rate_limits"]["seven_day"]["resets_at"] - out["rate_limits"]["five_hour"]["resets_at"]
        self.assertEqual(orig_gap, new_gap)
        self.assertEqual(out["prompt_cache"]["expires_at"] - out["prompt_cache"]["last_miss_at"], 3833)

    def test_millisecond_timestamps_are_shifted_too(self):
        # earliest timestamp (the millisecond one, 1763380000 s) maps to the base; the other keeps its distance
        out = anonymize.anonymize({"created_at": 1763380000123, "reset_at": 1763390000})
        self.assertEqual(out["created_at"], anonymize.EPOCH_BASE * 1000 + 123)
        self.assertEqual(out["reset_at"], anonymize.EPOCH_BASE + 10000)

    def test_input_is_not_modified(self):
        original = sample()
        snapshot = copy.deepcopy(original)
        anonymize.anonymize(original)
        self.assertEqual(original, snapshot)

    def test_nulls_and_booleans_survive(self):
        out = anonymize.anonymize({"used_percentage": None, "flag": False, "items": [None, True, 0]})
        self.assertEqual(out, {"used_percentage": None, "flag": False, "items": [None, True, 0]})

    def test_invalid_numbers_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            anonymize.anonymize({}, numbers="exact")


class LeakCheckTests(unittest.TestCase):
    def test_detects_each_kind_of_leak(self):
        for value, label in ((HOME + "/x", "local path"), (ENCODED, "encoded local path"),
                             (REAL_UUID, "non-fake UUID"), ("a@corp.example", "e-mail address"),
                             ("/private/tmp/x", "local path")):
            problems = anonymize.find_leaks({"k": value})
            self.assertTrue(any(label in p for p in problems), (value, problems))

    def test_accepts_fake_values(self):
        clean = {"a": "/home/user/project", "b": "user@example.com", "c": "00000000-0000-4000-8000-000000000042",
                 "d": "/home/user/.claude/projects/-home-user-project/x.jsonl", "e": [1, None, True]}
        self.assertEqual(anonymize.find_leaks(clean), [])

    def test_check_command_walks_directories_and_reports_files(self):
        tmp = tempfile.mkdtemp(prefix="ctxhud-anon-")
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "good.json"), "w", encoding="utf-8") as fh:
            json.dump({"cwd": "/home/user/project"}, fh)
        with mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(anonymize.main(["--check", tmp]), 0)
            with open(os.path.join(tmp, "bad.json"), "w", encoding="utf-8") as fh:
                json.dump({"cwd": HOME + "/x"}, fh)
            self.assertEqual(anonymize.main(["--check", tmp]), 1)
            self.assertIn("bad.json", sys.stdout.getvalue())

    def test_committed_claude_fixtures_are_clean(self):
        # Only this module's own fixtures: other tools' fixtures are checked by their owners
        # (run `python3 tests/tools/anonymize.py --check tests/fixtures` for the whole tree).
        self.assertEqual(anonymize.check_paths([os.path.join(HERE, "fixtures", "claude")]), [])


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ctxhud-anon-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_convert_file_to_file(self):
        src, dst = os.path.join(self.tmp, "in.json"), os.path.join(self.tmp, "out.json")
        with open(src, "w", encoding="utf-8") as fh:
            json.dump(sample(), fh)
        self.assertEqual(anonymize.main([src, "-o", dst]), 0)
        with open(dst, "r", encoding="utf-8") as fh:
            converted = json.load(fh)
        self.assertEqual(anonymize.find_leaks(converted), [])
        self.assertEqual(converted["cwd"], "/home/user/project")

    def test_unreadable_input_returns_error_code(self):
        self.assertEqual(anonymize.main([os.path.join(self.tmp, "missing.json")]), 2)
        bad = os.path.join(self.tmp, "bad.json")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(anonymize.main([bad]), 2)


if __name__ == "__main__":
    unittest.main()
