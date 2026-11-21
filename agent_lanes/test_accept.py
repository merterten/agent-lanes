"""Tests for tools/lanes/accept.py.

Run from the worktree root:

    python3 -m unittest discover -s tools/lanes -p 'test_accept*.py' -v

Every git repository is created under a temporary directory in /tmp.  No test
touches the real lanes root, the network or a worker command.
"""
import hashlib
import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import accept  # noqa: E402

PYTHON = shlex.quote(sys.executable)


def pycmd(code):
    return f"{PYTHON} -c {shlex.quote(code)}"


PASS_CMD = pycmd('print("5 passed in 0.1s")')
FAIL_CMD = pycmd('import sys; print("1 failed, 4 passed in 0.1s"); sys.exit(1)')
SLEEP_CMD = pycmd('import time; time.sleep(5); print("1 passed in 0.1s")')


def git(repo, *args):
    proc = subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout.strip()


def make_repo(root, files=None):
    files = files or {
        "app.txt": "hello\n",
        "calc.py": "value = 1\n",
    }
    repo = Path(root) / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "lane@example.test")
    git(repo, "config", "user.name", "Lane Test")
    for name, text in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "initial")
    return repo, git(repo, "rev-parse", "HEAD")


def write_result(worktree, tests=None, claims=None, summary="worker summary", blockers=None):
    lane = Path(worktree) / ".lane"
    lane.mkdir(parents=True, exist_ok=True)
    payload = {
        "lane": "oc-worker-18",
        "status": "done",
        "summary": summary,
        "files_changed": [],
        "tests": tests or [],
        "claims": claims or [],
        "blockers": blockers or [],
    }
    (lane / "LANE_RESULT.json").write_text(json.dumps(payload), encoding="utf-8")


def write_mutants(path, rows):
    Path(path).write_text(json.dumps(rows), encoding="utf-8")


class ParseCountsTests(unittest.TestCase):
    def test_pytest_failures_and_errors_count_as_failed(self):
        output = "== 3 failed, 10 passed, 1 error in 2.1s ==\n"
        self.assertEqual((10, 4), accept.parse_counts(output))

    def test_pytest_passed_only(self):
        self.assertEqual((10, 0), accept.parse_counts("10 passed in 0.5s\n"))

    def test_pytest_skips_do_not_count_as_passed(self):
        output = "2 failed, 7 passed, 3 skipped in 1.2s\n"
        self.assertEqual((7, 2), accept.parse_counts(output))

    def test_pytest_zero_passed_is_still_a_summary(self):
        self.assertEqual((0, 0), accept.parse_counts("0 passed in 0.1s\n"))

    def test_unittest_ok_with_skips(self):
        output = "Ran 5 tests in 0.003s\n\nOK (skipped=2)\n"
        self.assertEqual((3, 0), accept.parse_counts(output))

    def test_unittest_failed_failures_errors_and_skips(self):
        output = (
            "Ran 6 tests in 0.010s\n\n"
            "FAILED (failures=1, errors=2, skipped=1)\n"
        )
        self.assertEqual((2, 3), accept.parse_counts(output))

    def test_dotnet_passed_project(self):
        output = (
            "Passed!  - Failed:     0, Passed:    12, Skipped:     0, "
            "Total:    12, Duration: 1 s - Tests.dll\n"
        )
        self.assertEqual((12, 0), accept.parse_counts(output))

    def test_dotnet_failed_project(self):
        output = "Failed!  - Failed: 1, Passed: 2, Skipped: 0, Total: 3\n"
        self.assertEqual((2, 1), accept.parse_counts(output))

    def test_dotnet_multi_project_sums_every_line(self):
        output = (
            "Passed!  - Failed:     0, Passed:    12, Skipped: 0, Total: 12\n"
            "Failed!  - Failed: 2, Passed: 3, Skipped: 0, Total: 5\n"
        )
        self.assertEqual((15, 2), accept.parse_counts(output))

    def test_node_hash_summary(self):
        self.assertEqual((7, 2), accept.parse_counts("# pass 7\n# fail 2\n"))

    def test_node_unicode_summary(self):
        self.assertEqual((4, 1), accept.parse_counts("\u2139 pass 4\n\u2139 fail 1\n"))

    def test_unknown_output_is_none(self):
        self.assertIsNone(accept.parse_counts("all good\n"))
        self.assertIsNone(accept.parse_counts(""))


class LaneCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.repo, self.base = make_repo(self.tmp)
        self.out = self.tmp / "out"

    def accept(self, tests, mutants=None, timeout=10, out=None):
        return accept.run_accept(
            self.repo, "oc-worker-18", self.base, tests, mutants, timeout, out or self.out
        )

    def report_text(self, result):
        return Path(result["report"]).read_text(encoding="utf-8")


class RunAcceptCoreTests(LaneCase):
    def test_minimal_pass(self):
        write_result(self.repo, tests=[{"cmd": PASS_CMD, "passed": 5, "failed": 0}])
        result = self.accept([PASS_CMD])
        self.assertEqual("PASS", result["verdict"])
        self.assertEqual([], result["reasons"])
        self.assertEqual("VERDICT: PASS", self.report_text(result).splitlines()[0])

    def test_missing_result_file_fails(self):
        result = self.accept([PASS_CMD])
        self.assertEqual("FAIL", result["verdict"])
        self.assertIn("no valid LANE_RESULT.json", result["reasons"])

    def test_garbled_result_file_fails(self):
        lane = self.repo / ".lane"
        lane.mkdir()
        (lane / "LANE_RESULT.json").write_text("{not json", encoding="utf-8")
        result = self.accept([PASS_CMD])
        self.assertEqual("FAIL", result["verdict"])
        self.assertIn("no valid LANE_RESULT.json", result["reasons"])

    def test_claim_match_passes(self):
        write_result(self.repo, tests=[{"cmd": PASS_CMD, "passed": 5, "failed": 0}])
        result = self.accept([PASS_CMD])
        self.assertEqual("PASS", result["verdict"])
        self.assertIn("| yes |", self.report_text(result))

    def test_claim_mismatch_fails(self):
        write_result(self.repo, tests=[{"cmd": PASS_CMD, "passed": 9, "failed": 0}])
        result = self.accept([PASS_CMD])
        self.assertEqual("FAIL", result["verdict"])
        self.assertTrue(any("claim mismatch" in reason for reason in result["reasons"]))
        self.assertIn("claimed 9/0, measured 5/0", "\n".join(result["reasons"]))

    def test_not_rerun_claim_is_listed_but_not_a_failure(self):
        write_result(
            self.repo,
            tests=[{"cmd": "not-a-reviewer-command --x", "passed": 1, "failed": 0}],
        )
        result = self.accept([PASS_CMD])
        self.assertEqual("PASS", result["verdict"])
        self.assertIn("not-a-reviewer-command --x", self.report_text(result))

    def test_worker_command_is_never_executed(self):
        marker = self.tmp / "worker-marker"
        evidence_marker = self.tmp / "evidence-marker"
        write_result(
            self.repo,
            tests=[{"cmd": f"touch {shlex.quote(str(marker))}", "passed": 0, "failed": 0}],
            claims=[
                {
                    "name": "marker",
                    "value": 1,
                    "evidence": f"touch {shlex.quote(str(evidence_marker))}",
                }
            ],
        )
        result = self.accept([PASS_CMD])
        self.assertEqual("PASS", result["verdict"])
        self.assertFalse(marker.exists())
        self.assertFalse(evidence_marker.exists())

    def test_test_timeout_fails(self):
        write_result(self.repo)
        result = self.accept([SLEEP_CMD], timeout=1)
        self.assertEqual("FAIL", result["verdict"])
        self.assertTrue(any("timed out" in reason for reason in result["reasons"]))
        self.assertIn("timeout", self.report_text(result))

    def test_change_set_excludes_lane_directories(self):
        write_result(self.repo)
        (self.repo / ".lane.prev").mkdir()
        (self.repo / ".lane.prev" / "old.txt").write_text("old\n", encoding="utf-8")
        (self.repo / "newfile.txt").write_text("new\n", encoding="utf-8")
        result = self.accept([PASS_CMD])
        report = self.report_text(result)
        self.assertIn("newfile.txt", report)
        self.assertNotIn("LANE_RESULT.json", report)
        self.assertNotIn(".lane.prev/old.txt", report)
        self.assertNotIn("old.txt", report)

    def test_change_set_shows_tracked_diff(self):
        write_result(self.repo)
        (self.repo / "app.txt").write_text("changed\n", encoding="utf-8")
        result = self.accept([PASS_CMD])
        self.assertIn("app.txt", self.report_text(result))

    def test_worker_claims_are_unverified_not_failures(self):
        write_result(
            self.repo,
            claims=[{"name": "coverage", "value": "90%", "evidence": "coverage.log"}],
        )
        result = self.accept([PASS_CMD])
        self.assertEqual("PASS", result["verdict"])
        report = self.report_text(result)
        self.assertIn("Unverified claims", report)
        self.assertIn("coverage", report)

    def test_invalid_mutants_file_fails(self):
        write_result(self.repo)
        bad = self.tmp / "mutants.json"
        bad.write_text("not json", encoding="utf-8")
        result = self.accept([PASS_CMD], mutants=bad)
        self.assertEqual("FAIL", result["verdict"])
        self.assertTrue(any("invalid mutants file" in reason for reason in result["reasons"]))


class MutantTests(LaneCase):
    def kill_test(self):
        return pycmd(
            'import pathlib; text = pathlib.Path("calc.py").read_text(); '
            'assert "value = 1" in text; print("1 passed in 0.1s")'
        )

    def test_mutant_killed(self):
        write_result(self.repo)
        path = self.tmp / "mutants.json"
        write_mutants(
            path,
            [{"name": "value-two", "file": "calc.py", "find": "value = 1",
              "replace": "value = 2", "test": self.kill_test()}],
        )
        result = self.accept([PASS_CMD], mutants=path)
        self.assertEqual("PASS", result["verdict"])
        self.assertIn("KILLED", self.report_text(result))
        self.assertEqual("value = 1\n", (self.repo / "calc.py").read_text(encoding="utf-8"))
        self.assertFalse((self.out / "mutant-backup.json").exists())

    def test_mutant_survived_fails(self):
        write_result(self.repo)
        path = self.tmp / "mutants.json"
        write_mutants(
            path,
            [{"name": "value-two", "file": "calc.py", "find": "value = 1",
              "replace": "value = 2", "test": PASS_CMD}],
        )
        result = self.accept([PASS_CMD], mutants=path)
        self.assertEqual("FAIL", result["verdict"])
        self.assertTrue(any("survived" in reason for reason in result["reasons"]))
        self.assertIn("SURVIVED", self.report_text(result))
        self.assertEqual("value = 1\n", (self.repo / "calc.py").read_text(encoding="utf-8"))

    def test_mutant_with_non_unique_find_is_refused(self):
        write_result(self.repo)
        (self.repo / "dupes.txt").write_text("a\na\n", encoding="utf-8")
        path = self.tmp / "mutants.json"
        write_mutants(
            path,
            [{"name": "dupe", "file": "dupes.txt", "find": "a", "replace": "b"}],
        )
        result = self.accept([PASS_CMD], mutants=path)
        self.assertEqual("FAIL", result["verdict"])
        self.assertTrue(any("find not unique" in reason for reason in result["reasons"]))
        self.assertEqual("a\na\n", (self.repo / "dupes.txt").read_text(encoding="utf-8"))
        self.assertFalse((self.out / "mutant-backup.json").exists())

    def test_mutant_path_escape_with_parent_is_refused(self):
        write_result(self.repo)
        (self.tmp / "escape.txt").write_text("x = 1\n", encoding="utf-8")
        path = self.tmp / "mutants.json"
        write_mutants(
            path,
            [{"name": "escape", "file": "../escape.txt", "find": "x = 1",
              "replace": "x = 2"}],
        )
        result = self.accept([PASS_CMD], mutants=path)
        self.assertEqual("FAIL", result["verdict"])
        self.assertTrue(any("path escapes" in reason for reason in result["reasons"]))
        self.assertEqual("x = 1\n", (self.tmp / "escape.txt").read_text(encoding="utf-8"))

    def test_mutant_absolute_path_is_refused(self):
        write_result(self.repo)
        outside = self.tmp / "outside.txt"
        outside.write_text("x = 1\n", encoding="utf-8")
        path = self.tmp / "mutants.json"
        write_mutants(
            path,
            [{"name": "absolute", "file": str(outside), "find": "x = 1",
              "replace": "x = 2"}],
        )
        result = self.accept([PASS_CMD], mutants=path)
        self.assertEqual("FAIL", result["verdict"])
        self.assertTrue(any("path escapes" in reason for reason in result["reasons"]))
        self.assertEqual("x = 1\n", outside.read_text(encoding="utf-8"))

    def test_file_is_byte_identical_after_mutant_test_times_out(self):
        write_result(self.repo)
        calc = self.repo / "calc.py"
        original = "value = 1\n# caf\u00e9\n".encode("utf-8")
        calc.write_bytes(original)
        before = hashlib.sha256(original).hexdigest()
        path = self.tmp / "mutants.json"
        write_mutants(
            path,
            [{"name": "slow", "file": "calc.py", "find": "value = 1",
              "replace": "value = 2", "test": SLEEP_CMD}],
        )
        result = self.accept([PASS_CMD], mutants=path, timeout=1)
        self.assertEqual("PASS", result["verdict"])
        self.assertIn("KILLED", self.report_text(result))
        self.assertEqual(original, calc.read_bytes())
        self.assertEqual(before, hashlib.sha256(calc.read_bytes()).hexdigest())
        self.assertFalse((self.out / "mutant-backup.json").exists())

    def test_crash_recovery_restores_leftover_backup(self):
        write_result(self.repo)
        calc = self.repo / "calc.py"
        original_text = "value = 1\n"
        calc.write_text("value = 999\n", encoding="utf-8")
        self.out.mkdir(parents=True)
        backup = self.out / "mutant-backup.json"
        backup.write_text(
            json.dumps({str(calc.resolve()): original_text}), encoding="utf-8"
        )
        result = self.accept([PASS_CMD])
        self.assertEqual("PASS", result["verdict"])
        self.assertEqual(original_text, calc.read_text(encoding="utf-8"))
        self.assertFalse(backup.exists())
        self.assertIn("Recovery:", self.report_text(result))

    def test_crash_recovery_accepts_relative_backup_paths(self):
        write_result(self.repo)
        calc = self.repo / "calc.py"
        calc.write_text("value = 999\n", encoding="utf-8")
        self.out.mkdir(parents=True)
        backup = self.out / "mutant-backup.json"
        backup.write_text(json.dumps({"calc.py": "value = 1\n"}), encoding="utf-8")
        result = self.accept([PASS_CMD])
        self.assertEqual("PASS", result["verdict"])
        self.assertEqual("value = 1\n", calc.read_text(encoding="utf-8"))
        self.assertFalse(backup.exists())

    def test_restore_backup_helper(self):
        out = self.tmp / "restore-out"
        out.mkdir()
        target = self.tmp / "restore-me.txt"
        target.write_text("mutated\n", encoding="utf-8")
        backup = out / "mutant-backup.json"
        backup.write_text(
            json.dumps({str(target): "original\n"}), encoding="utf-8"
        )
        restored = accept.restore_backup(out)
        self.assertEqual([str(target)], restored)
        self.assertEqual("original\n", target.read_text(encoding="utf-8"))
        self.assertFalse(backup.exists())


if __name__ == "__main__":
    unittest.main()


class ReviewerFixTests(LaneCase):
    """Added by the reviewer (Claude) at integration."""

    def test_no_reviewer_tests_is_fail(self):
        write_result(self.repo)
        result = self.accept([])
        self.assertEqual("FAIL", result["verdict"])
        self.assertTrue(any("no reviewer test commands" in r for r in result["reasons"]))

    def test_mutants_skipped_when_baseline_fails(self):
        write_result(self.repo)
        path = self.tmp / "mutants.json"
        write_mutants(path, [{"name": "value-two", "file": "calc.py", "find": "value = 1",
                              "replace": "value = 2"}])
        result = self.accept([FAIL_CMD], mutants=path)
        self.assertEqual("FAIL", result["verdict"])
        self.assertTrue(any("mutants not run" in r for r in result["reasons"]))
        self.assertNotIn("KILLED", self.report_text(result))
        self.assertEqual("value = 1\n", (self.repo / "calc.py").read_text(encoding="utf-8"))

    def test_dotnet_failures_in_first_project_are_summed(self):
        output = (
            "Failed!  - Failed: 2, Passed: 3, Skipped: 0, Total: 5\n"
            "Passed!  - Failed:     0, Passed:    12, Skipped: 0, Total: 12\n"
        )
        self.assertEqual((15, 2), accept.parse_counts(output))

    def test_mutant_with_zero_match_find_is_refused(self):
        write_result(self.repo)
        path = self.tmp / "mutants.json"
        write_mutants(path, [{"name": "nomatch", "file": "calc.py", "find": "value = 9",
                              "replace": "value = 8", "test": PASS_CMD}])
        result = self.accept([PASS_CMD], mutants=path)
        self.assertEqual("FAIL", result["verdict"])
        self.assertTrue(any("find not unique" in r and "0 matches" in r for r in result["reasons"]))
