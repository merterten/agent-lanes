#!/usr/bin/env python3
"""Tests for tools/lanes/review.py and tools/lanes/ask.py."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ask  # noqa: E402
import review  # noqa: E402


class _OpenPipeOnStdin:
    """Put an open, never-written pipe on fd 0 (the condition that made `opencode run` hang), restore after."""
    def __enter__(self):
        self.r, self.w = os.pipe()
        self.saved = os.dup(0)
        os.dup2(self.r, 0)
        return self

    def __exit__(self, *exc):
        os.dup2(self.saved, 0)
        for fd in (self.r, self.w, self.saved):
            os.close(fd)


FAKE_OC = r'''#!/usr/bin/env python3
import json
import os
import shutil
import sys
import time

record = os.environ.get("FAKE_OC_RECORD")
if record:
    with open(record, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "stdin": os.readlink("/proc/self/fd/0")}) + "\n")

snap = os.environ.get("FAKE_OC_SNAPSHOT")
if snap:
    src = os.path.join(os.getcwd(), ".lane", "review")
    if os.path.isdir(src):
        if os.path.isdir(snap):
            shutil.rmtree(snap)
        shutil.copytree(src, snap)

sleep = float(os.environ.get("FAKE_OC_SLEEP", "0") or "0")
if sleep:
    time.sleep(sleep)

sys.stdout.write(os.environ.get("FAKE_OC_OUT", ""))
sys.stdout.flush()
sys.stderr.write(os.environ.get("FAKE_OC_ERR", ""))
sys.stderr.flush()
sys.exit(int(os.environ.get("FAKE_OC_RC", "0") or "0"))
'''


class ReviewAskTests(unittest.TestCase):
    def setUp(self):
        self.old_env = os.environ.copy()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="lane-test-", dir="/tmp"))
        self.repo = self.tmpdir / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Lane Test")
        (self.repo / "tracked.txt").write_text("line one\nline two\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        self.git("commit", "-qm", "init")

        self.fake = self.tmpdir / "fake_oc.py"
        self.fake.write_text(FAKE_OC, encoding="utf-8")
        self.fake.chmod(0o755)
        self.record = self.tmpdir / "record.jsonl"
        self.out = self.tmpdir / "out"
        self.trading = self.tmpdir / "trading"
        self.trading.mkdir()
        self.snap = self.tmpdir / "snapshot"

        os.environ["LANE_OC_BIN"] = str(self.fake)
        os.environ["LANE_ROOT"] = str(self.trading)
        os.environ["FAKE_OC_RECORD"] = str(self.record)
        os.environ["FAKE_OC_OUT"] = ""
        os.environ["FAKE_OC_ERR"] = ""
        os.environ["FAKE_OC_RC"] = "0"
        os.environ["FAKE_OC_SLEEP"] = "0"
        os.environ.pop("FAKE_OC_SNAPSHOT", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old_env)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # helpers -------------------------------------------------------------
    def git(self, *args, cwd=None):
        subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.repo),
            check=True,
            capture_output=True,
            text=True,
        )

    def set_worker(self, out="", err="", rc="0", sleep="0", snapshot=None):
        os.environ["FAKE_OC_OUT"] = out
        os.environ["FAKE_OC_ERR"] = err
        os.environ["FAKE_OC_RC"] = rc
        os.environ["FAKE_OC_SLEEP"] = sleep
        if snapshot is None:
            os.environ.pop("FAKE_OC_SNAPSHOT", None)
        else:
            os.environ["FAKE_OC_SNAPSHOT"] = str(snapshot)

    def call_review(self, **kwargs):
        return review.run_review(
            self.repo,
            kwargs.pop("lane", "oc-worker-1"),
            kwargs.pop("base", "HEAD"),
            self.out,
            **kwargs,
        )

    def recorded(self):
        if not self.record.exists():
            return []
        return [
            json.loads(line)
            for line in self.record.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def snapshot_diff(self):
        snap = self.tmpdir / "snapshot"
        return (snap / "diff.patch").read_text(encoding="utf-8")

    def snapshot_files(self):
        snap = self.tmpdir / "snapshot"
        return (snap / "files.txt").read_text(encoding="utf-8").splitlines()

    # review --------------------------------------------------------------
    def test_review_returns_dict_and_saves_report(self):
        self.set_worker(out="FINDINGS: 0\n")
        result = self.call_review()
        self.assertEqual(result["rc"], 0)
        self.assertEqual(result["findings"], 0)
        report = Path(result["report"])
        self.assertTrue(report.is_file())
        self.assertEqual(report.parent, self.out)
        self.assertTrue(report.name.startswith("review-"))
        self.assertTrue(report.name.endswith(".md"))

    def test_review_report_contains_stdout_and_stderr(self):
        self.set_worker(out="OUTMARK\nFINDINGS: 0\n", err="ERRMARK\n")
        result = self.call_review()
        text = Path(result["report"]).read_text(encoding="utf-8")
        self.assertIn("OUTMARK", text)
        self.assertIn("ERRMARK", text)

    def test_review_worker_is_read_only_and_runs_in_worktree(self):
        self.set_worker(out="FINDINGS: 0\n")
        self.call_review()
        entries = self.recorded()
        self.assertEqual(len(entries), 1)
        self.assertEqual(len(entries[0]["argv"]), 1)
        self.assertNotIn("--write", entries[0]["argv"])
        self.assertEqual(Path(entries[0]["cwd"]).resolve(), self.repo.resolve())

    def test_review_and_ask_workers_get_no_stdin(self):
        # Regression: an inherited open stdin made `opencode run` wait forever for input.
        self.set_worker(out="FINDINGS: 0\n")
        with _OpenPipeOnStdin():
            self.call_review()
            ask.run_ask("What is X?", self.repo, self.out)
        entries = self.recorded()
        self.assertEqual(len(entries), 2)
        self.assertEqual([e["stdin"] for e in entries], ["/dev/null", "/dev/null"])

    def test_review_prompt_contains_checklist_and_read_instruction(self):
        self.set_worker(out="FINDINGS: 0\n")
        self.call_review()
        task = self.recorded()[0]["argv"][0].lower()
        for topic in [
            "fail-closed",
            "tradingenabled",
            "killswitch",
            "arm phrase",
            "dry-run",
            "look-ahead",
            "future data",
            "untrusted input",
            "tests weakened",
            "numbers in reports",
            "secrets",
            "resource leaks",
            "timeout",
        ]:
            self.assertIn(topic, task)
        self.assertIn(
            "read .lane/review/diff.patch and the changed files in full", task
        )

    def test_review_prompt_contains_focus(self):
        self.set_worker(out="FINDINGS: 0\n")
        self.call_review(focus="check the risk gate bypass")
        task = self.recorded()[0]["argv"][0]
        self.assertIn("check the risk gate bypass", task)

    def test_review_diff_contains_tracked_change(self):
        with open(self.repo / "tracked.txt", "a", encoding="utf-8") as fh:
            fh.write("TRACKED-CHANGE-MARKER\n")
        self.set_worker(out="FINDINGS: 0\n", snapshot=self.snap)
        self.call_review()
        diff = self.snapshot_diff()
        self.assertIn("tracked.txt", diff)
        self.assertIn("TRACKED-CHANGE-MARKER", diff)

    def test_review_diff_contains_untracked_file_content(self):
        (self.repo / "newfile.txt").write_text("UNTRACKED-MARKER\n", encoding="utf-8")
        self.set_worker(out="FINDINGS: 0\n", snapshot=self.snap)
        self.call_review()
        diff = self.snapshot_diff()
        self.assertIn("NEW FILE", diff)
        self.assertIn("newfile.txt", diff)
        self.assertIn("UNTRACKED-MARKER", diff)

    def test_review_diff_excludes_lane_directories(self):
        (self.repo / ".lane").mkdir()
        (self.repo / ".lane" / "secret.txt").write_text("LANE-SECRET\n", encoding="utf-8")
        (self.repo / ".lane.prev").mkdir()
        (self.repo / ".lane.prev" / "old.txt").write_text("PREV-SECRET\n", encoding="utf-8")
        self.set_worker(out="FINDINGS: 0\n", snapshot=self.snap)
        self.call_review()
        diff = self.snapshot_diff()
        files = self.snapshot_files()
        self.assertNotIn("LANE-SECRET", diff)
        self.assertNotIn("PREV-SECRET", diff)
        self.assertFalse(any(".lane" in name for name in files))

    def test_review_large_untracked_file_is_listed_not_inlined(self):
        big = self.repo / "big.bin"
        big.write_text("BIGSTART" + ("x" * (300 * 1024)), encoding="utf-8")
        self.set_worker(out="FINDINGS: 0\n", snapshot=self.snap)
        self.call_review()
        diff = self.snapshot_diff()
        self.assertIn("SKIPPED LARGE FILE", diff)
        self.assertIn("big.bin", diff)
        self.assertNotIn("BIGSTART", diff)
        self.assertIn("big.bin", self.snapshot_files())

    def test_review_files_txt_lists_tracked_and_untracked(self):
        with open(self.repo / "tracked.txt", "a", encoding="utf-8") as fh:
            fh.write("changed\n")
        (self.repo / "newfile.txt").write_text("hello\n", encoding="utf-8")
        self.set_worker(out="FINDINGS: 0\n", snapshot=self.snap)
        self.call_review()
        files = self.snapshot_files()
        self.assertIn("tracked.txt", files)
        self.assertIn("newfile.txt", files)

    def test_review_parses_zero_findings(self):
        self.set_worker(out="All good.\n\nFINDINGS: 0\n")
        result = self.call_review()
        self.assertEqual(result["findings"], 0)

    def test_review_parses_three_findings(self):
        output = (
            "FINDING 1 | HIGH | a.py:1\nbad\nconfirm\n"
            "FINDING 2 | LOW | b.py:2\nbad\nconfirm\n"
            "FINDING 3 | MEDIUM | c.py:3\nbad\nconfirm\n"
            "FINDINGS: 3\n"
        )
        self.set_worker(out=output)
        result = self.call_review()
        self.assertEqual(result["findings"], 3)
        text = Path(result["report"]).read_text(encoding="utf-8")
        self.assertNotIn("NOTE:", text)

    def test_review_counts_findings_without_final_line(self):
        self.set_worker(out="FINDING 1 | HIGH | a.py:1\nbad\nconfirm\n")
        result = self.call_review()
        self.assertEqual(result["findings"], 1)

    def test_review_notes_findings_count_mismatch_at_top(self):
        output = (
            "FINDING 1 | HIGH | a.py:1\nbad\nconfirm\n"
            "FINDING 2 | LOW | b.py:2\nbad\nconfirm\n"
            "FINDINGS: 5\n"
        )
        self.set_worker(out=output)
        result = self.call_review()
        self.assertEqual(result["findings"], 2)
        text = Path(result["report"]).read_text(encoding="utf-8")
        first_line = text.splitlines()[0]
        self.assertTrue(first_line.startswith("NOTE:"))
        self.assertIn("5", first_line)
        self.assertIn("2", first_line)

    def test_review_removes_review_dir_on_success(self):
        self.set_worker(out="FINDINGS: 0\n")
        self.call_review()
        self.assertFalse((self.repo / ".lane" / "review").exists())

    def test_review_removes_review_dir_on_timeout(self):
        self.set_worker(out="", sleep="5")
        result = self.call_review(timeout=0.3)
        self.assertNotEqual(result["rc"], 0)
        self.assertTrue(Path(result["report"]).is_file())
        self.assertFalse((self.repo / ".lane" / "review").exists())

    def test_review_removes_review_dir_on_worker_failure(self):
        self.set_worker(out="", err="BOOM\n", rc="3")
        result = self.call_review()
        self.assertEqual(result["rc"], 3)
        text = Path(result["report"]).read_text(encoding="utf-8")
        self.assertIn("BOOM", text)
        self.assertFalse((self.repo / ".lane" / "review").exists())

    def test_review_missing_binary_still_writes_report(self):
        os.environ["LANE_OC_BIN"] = str(self.tmpdir / "does-not-exist")
        result = self.call_review()
        self.assertNotEqual(result["rc"], 0)
        self.assertTrue(Path(result["report"]).is_file())
        self.assertFalse((self.repo / ".lane" / "review").exists())

    def test_review_creates_output_directory(self):
        self.set_worker(out="FINDINGS: 0\n")
        nested = self.tmpdir / "nested" / "reports"
        result = review.run_review(self.repo, "oc-worker-1", "HEAD", nested)
        self.assertTrue(Path(result["report"]).is_file())
        self.assertEqual(Path(result["report"]).parent, nested)

    # ask -----------------------------------------------------------------
    def test_ask_appends_suffix_and_saves_answer(self):
        self.set_worker(out="ANSWER-MARKER\n")
        result = ask.run_ask("What is X?", self.repo, self.out)
        self.assertEqual(result["rc"], 0)
        prompt = self.recorded()[0]["argv"][0]
        self.assertTrue(prompt.startswith("What is X?"))
        self.assertTrue(prompt.endswith(ask.ANSWER_SUFFIX))
        text = Path(result["answer_path"]).read_text(encoding="utf-8")
        self.assertIn("What is X?", text)
        self.assertIn("ANSWER-MARKER", text)

    def test_ask_answer_filename_is_stamped(self):
        self.set_worker(out="ok\n")
        result = ask.run_ask("filename?", self.repo, self.out)
        name = Path(result["answer_path"]).name
        self.assertTrue(name.startswith("ask-"))
        self.assertTrue(name.endswith(".md"))

    def test_ask_prints_answer(self):
        self.set_worker(out="PRINTED-ANSWER\n")
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            ask.run_ask("print?", self.repo, self.out)
        self.assertIn("PRINTED-ANSWER", stream.getvalue())

    def test_ask_worker_is_read_only_and_honours_cwd(self):
        self.set_worker(out="ok\n")
        ask.run_ask("cwd?", self.repo, self.out)
        entry = self.recorded()[0]
        self.assertEqual(len(entry["argv"]), 1)
        self.assertNotIn("--write", entry["argv"])
        self.assertEqual(Path(entry["cwd"]).resolve(), self.repo.resolve())

    def test_ask_rejects_cwd_outside_allowed_roots(self):
        self.set_worker(out="ok\n")
        with self.assertRaises(ValueError):
            ask.run_ask("outside?", Path("/etc"), self.out)
        self.assertEqual(self.recorded(), [])

    def test_ask_accepts_cwd_under_lanes_root(self):
        sub = self.trading / "sub"
        sub.mkdir()
        self.set_worker(out="ok\n")
        result = ask.run_ask("under trading?", sub, self.out)
        self.assertEqual(result["rc"], 0)
        self.assertEqual(
            Path(self.recorded()[0]["cwd"]).resolve(), sub.resolve()
        )

    def test_ask_accepts_cwd_under_tmp(self):
        self.set_worker(out="ok\n")
        result = ask.run_ask("under tmp?", self.repo, self.out)
        self.assertEqual(result["rc"], 0)

    def test_ask_timeout_returns_nonzero_and_writes_file(self):
        self.set_worker(out="", sleep="5")
        result = ask.run_ask("TIMEOUTQ", self.repo, self.out, timeout=0.3)
        self.assertNotEqual(result["rc"], 0)
        path = Path(result["answer_path"])
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        self.assertIn("TIMEOUTQ", text)
        self.assertIn("TIMEOUT", text)

    def test_ask_missing_binary_still_writes_file(self):
        os.environ["LANE_OC_BIN"] = str(self.tmpdir / "does-not-exist")
        result = ask.run_ask("Q", self.repo, self.out)
        self.assertNotEqual(result["rc"], 0)
        path = Path(result["answer_path"])
        self.assertTrue(path.is_file())
        self.assertIn("could not run worker", path.read_text(encoding="utf-8"))

    def test_ask_creates_output_directory(self):
        self.set_worker(out="ok\n")
        nested = self.tmpdir / "deep" / "answers"
        result = ask.run_ask("create dir?", self.repo, nested)
        self.assertTrue(Path(result["answer_path"]).is_file())
        self.assertEqual(Path(result["answer_path"]).parent, nested)


if __name__ == "__main__":
    unittest.main()
