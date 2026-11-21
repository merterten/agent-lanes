"""Tests for tools/lanes/lane.py.

Run from the worktree root:

    python3 -m unittest discover -s tools/lanes -v

Every test uses a temporary LANE_ROOT tree and temporary git repositories.
No test touches the real lanes root or the network.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import lane  # noqa: E402

ENV_KEYS = (
    "LANE_ROOT", "LANE_MAIN_REPO", "LANE_WORKTREES", "LANE_STATE_DIR",
    "LANE_OC_BIN", "LANE_CC_BIN", "LANE_LAPTOP_BIN",
    "FAKE_LAPTOP_LOG", "FAKE_LAPTOP_STDIN", "LANE_LAPTOP_PROBE", "LANE_PROC",
)

VALID_WORKER = textwrap.dedent('''
    import json, os
    wt = os.getcwd()
    lane_name = os.path.basename(wt)
    with open(os.path.join(wt, "change.txt"), "w") as fh:
        fh.write("x\\n")
    os.makedirs(os.path.join(wt, ".lane"), exist_ok=True)
    result = {
        "lane": lane_name,
        "status": "done",
        "summary": "all good",
        "files_changed": ["change.txt"],
        "tests": [{"cmd": "true", "passed": 1, "failed": 0}],
        "claims": [],
        "blockers": [],
    }
    with open(os.path.join(wt, ".lane", "LANE_RESULT.json"), "w") as fh:
        json.dump(result, fh)
    with open(os.path.join(wt, ".lane", "REPORT.md"), "w") as fh:
        fh.write("ok\\n")
''')

MISSING_WORKER = textwrap.dedent('''
    import sys
    print("line one")
    print("line two")
    sys.exit(0)
''')

LAPTOP = textwrap.dedent('''
    import json, os, sys
    cmd = sys.argv[1]
    nn = sys.argv[2]
    with open(os.environ["FAKE_LAPTOP_LOG"], "a") as fh:
        fh.write(cmd + " " + nn + "\\n")
    if cmd == "push":
        sys.exit(0)
    if cmd == "run":
        data = sys.stdin.read()
        with open(os.environ["FAKE_LAPTOP_STDIN"], "w") as fh:
            fh.write(data)
        sys.exit(0)
    if cmd == "status":
        print("working")
        print("END rc=0")
        sys.exit(0)
    if cmd == "pull":
        wt = os.getcwd()
        lane_name = os.path.basename(wt)
        with open(os.path.join(wt, "change.txt"), "w") as fh:
            fh.write("x\\n")
        os.makedirs(os.path.join(wt, ".lane"), exist_ok=True)
        result = {
            "lane": lane_name,
            "status": "done",
            "summary": "from laptop",
            "files_changed": ["change.txt"],
            "tests": [{"cmd": "true", "passed": 1, "failed": 0}],
            "claims": [],
            "blockers": [],
        }
        with open(os.path.join(wt, ".lane", "LANE_RESULT.json"), "w") as fh:
            json.dump(result, fh)
        with open(os.path.join(wt, ".lane", "REPORT.md"), "w") as fh:
            fh.write("ok\\n")
        sys.exit(0)
    sys.exit(3)
''')


def make_result(**overrides):
    data = {
        "lane": "oc-worker-01",
        "status": "done",
        "summary": "summary",
        "files_changed": [],
        "tests": [],
        "claims": [],
        "blockers": [],
    }
    data.update(overrides)
    return data


class LaneTest(unittest.TestCase):
    def setUp(self):
        self._old_env = {key: os.environ.get(key) for key in ENV_KEYS}
        self.tmp = Path(tempfile.mkdtemp(prefix="lane-test-"))
        os.environ["LANE_ROOT"] = str(self.tmp)
        for key in ENV_KEYS:
            if key != "LANE_ROOT":
                os.environ.pop(key, None)
        os.environ["LANE_LAPTOP_PROBE"] = "true"          # never ssh from tests
        os.environ["LANE_PROC"] = str(self.tmp / "no-proc")  # no busy processes unless a test says so
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main", cwd=self.repo)
        self.git("config", "user.email", "t@example.com", cwd=self.repo)
        self.git("config", "user.name", "Tester", cwd=self.repo)
        (self.repo / "README.md").write_text("hello\n", encoding="utf-8")
        (self.repo / ".gitignore").write_text("*.dat\n", encoding="utf-8")
        self.git("add", ".", cwd=self.repo)
        self.git("commit", "-m", "init", cwd=self.repo)

    def tearDown(self):
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ---------------------------------------------------------
    def git(self, *args, cwd=None):
        proc = subprocess.run(["git"] + list(args), cwd=str(cwd or self.repo),
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise AssertionError(f"git {args} failed: {proc.stderr}")
        return proc

    def git_rc(self, *args, cwd=None):
        return subprocess.run(["git"] + list(args), cwd=str(cwd or self.repo),
                              capture_output=True, text=True)

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = lane.main(list(argv))
        return code, out.getvalue()

    def script(self, name, body):
        path = self.tmp / name
        path.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def brief(self, text="Do the thing\n"):
        path = self.tmp / "brief.txt"
        path.write_text(text, encoding="utf-8")
        return path

    def make_lane(self, name="oc-worker-01", branch=None, from_ref="HEAD", commit=False):
        argv = ["new", name, "--from", from_ref]
        if branch:
            argv += ["--branch", branch]
        code, out = self.cli(*argv)
        self.assertEqual(0, code, out)
        if commit:
            worktree = lane.worktree_path(name)
            (worktree / "feature.txt").write_text("feature\n", encoding="utf-8")
            self.git("add", ".", cwd=worktree)
            self.git("commit", "-m", "feature", cwd=worktree)
        return lane.worktree_path(name)

    def write_result(self, worktree, data, report="ok\n"):
        lane_dir = Path(worktree) / ".lane"
        lane_dir.mkdir(parents=True, exist_ok=True)
        (lane_dir / "LANE_RESULT.json").write_text(json.dumps(data), encoding="utf-8")
        if report is not None:
            (lane_dir / "REPORT.md").write_text(report, encoding="utf-8")

    def validate_repo(self):
        return self.repo, lane.head_sha(self.repo)


class TestNew(LaneTest):
    def test_new_creates_worktree_branch_and_ledger(self):
        worktree = self.make_lane()
        self.assertTrue((worktree / ".git").exists())
        ledger = lane.load_ledger()
        entry = ledger["lanes"]["oc-worker-01"]
        self.assertEqual("created", entry["status"])
        self.assertEqual("agent/oc-worker-01", entry["branch"])
        self.assertEqual(str(worktree), entry["worktree"])
        self.assertEqual(lane.head_sha(self.repo), entry["base"])

    def test_new_uses_default_agent_branch(self):
        self.make_lane("cc-worker-7")
        self.assertEqual("agent/cc-worker-7",
                         lane.load_ledger()["lanes"]["cc-worker-7"]["branch"])

    def test_new_refuses_invalid_lane(self):
        for bad in ("worker-1", "oc-worker-", "oc-worker-1234", "xx-worker-1", "oc-worker-1x"):
            code, out = self.cli("new", bad, "--from", "HEAD")
            self.assertEqual(1, code, bad)
            self.assertIn("invalid lane name", out)

    def test_new_refuses_existing_folder(self):
        worktree = lane.worktree_path("oc-worker-01")
        worktree.mkdir(parents=True)
        code, out = self.cli("new", "oc-worker-01", "--from", "HEAD")
        self.assertEqual(1, code)
        self.assertIn("worktree folder exists", out)

    def test_new_refuses_existing_branch(self):
        self.git("branch", "agent/oc-worker-01", "main")
        code, out = self.cli("new", "oc-worker-01", "--from", "HEAD")
        self.assertEqual(1, code)
        self.assertIn("branch already exists", out)


class TestRun(LaneTest):
    def test_local_valid_result_returns_zero(self):
        os.environ["LANE_OC_BIN"] = str(self.script("fake-oc", VALID_WORKER))
        worktree = self.make_lane()
        code, out = self.cli("run", "oc-worker-01", str(self.brief()))
        self.assertEqual(0, code, out)
        entry = lane.load_ledger()["lanes"]["oc-worker-01"]
        self.assertEqual("returned", entry["status"])
        self.assertEqual(0, entry["rc"])
        self.assertEqual("local", entry["where"])
        self.assertEqual("oc-worker", entry["worker"])
        self.assertTrue((worktree / ".lane" / "LANE_RESULT.json").exists())
        self.assertIn("all good", out)

    def test_local_missing_result_returns_one_and_shows_log_tail(self):
        os.environ["LANE_OC_BIN"] = str(self.script("fake-oc", MISSING_WORKER))
        self.make_lane()
        code, out = self.cli("run", "oc-worker-01", str(self.brief()))
        self.assertEqual(1, code, out)
        entry = lane.load_ledger()["lanes"]["oc-worker-01"]
        self.assertEqual("no-result", entry["status"])
        self.assertEqual(0, entry["rc"])
        self.assertIn("log tail", out)
        self.assertIn("line two", out)

    def test_stale_lane_is_moved_to_prev(self):
        os.environ["LANE_OC_BIN"] = str(self.script("fake-oc", VALID_WORKER))
        worktree = self.make_lane()
        stale = worktree / ".lane"
        stale.mkdir()
        (stale / "stale.txt").write_text("old\n", encoding="utf-8")
        older = worktree / ".lane.prev"
        older.mkdir()
        (older / "older.txt").write_text("older\n", encoding="utf-8")
        code, out = self.cli("run", "oc-worker-01", str(self.brief()))
        self.assertEqual(0, code, out)
        self.assertTrue((worktree / ".lane.prev" / "stale.txt").exists())
        self.assertFalse((worktree / ".lane.prev" / "older.txt").exists())
        self.assertTrue((worktree / ".lane" / "LANE_RESULT.json").exists())

    def test_luxalgo_is_rejected_for_cc(self):
        self.make_lane("cc-worker-01")
        code, out = self.cli("run", "cc-worker-01", str(self.brief()), "--luxalgo")
        self.assertEqual(1, code)
        self.assertIn("--luxalgo", out)

    def test_laptop_needs_two_digit_oc_lane(self):
        self.make_lane("oc-worker-1")
        code, out = self.cli("run", "oc-worker-1", str(self.brief()), "--laptop")
        self.assertEqual(1, code)
        self.assertIn("--laptop", out)
        self.make_lane("cc-worker-01")
        code, out = self.cli("run", "cc-worker-01", str(self.brief()), "--laptop")
        self.assertEqual(1, code)
        self.assertIn("--laptop", out)

    def test_laptop_flow_push_run_status_pull(self):
        laptop = self.script("fake-laptop", LAPTOP)
        os.environ["LANE_LAPTOP_BIN"] = str(laptop)
        os.environ["FAKE_LAPTOP_LOG"] = str(self.tmp / "laptop.log")
        os.environ["FAKE_LAPTOP_STDIN"] = str(self.tmp / "laptop.stdin")
        worktree = self.make_lane("oc-worker-01")
        code, out = self.cli("run", "oc-worker-01", str(self.brief("write the code\n")),
                             "--laptop", "--poll", "0")
        self.assertEqual(0, code, out)
        calls = (self.tmp / "laptop.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(["push 01", "run 01", "status 01", "pull 01"], calls)
        task = (self.tmp / "laptop.stdin").read_text(encoding="utf-8")
        self.assertIn("write the code", task)
        self.assertIn("oc-worker-01", task)
        self.assertTrue((worktree / ".lane" / "LANE_RESULT.json").exists())
        entry = lane.load_ledger()["lanes"]["oc-worker-01"]
        self.assertEqual("returned", entry["status"])
        self.assertEqual("laptop", entry["where"])


class TestValidateResult(LaneTest):
    def test_missing_file_is_fatal(self):
        result, problems = lane.validate_result(self.repo, "oc-worker-01", None)
        self.assertIsNone(result)
        self.assertIn("missing .lane/LANE_RESULT.json", problems)

    def test_bad_json_is_fatal(self):
        (self.repo / ".lane").mkdir()
        (self.repo / ".lane" / "LANE_RESULT.json").write_text("{oops", encoding="utf-8")
        result, problems = lane.validate_result(self.repo, "oc-worker-01", None)
        self.assertIsNone(result)
        self.assertTrue(any("invalid JSON" in p for p in problems))

    def test_non_object_is_fatal(self):
        self.write_result(self.repo, [])
        result, problems = lane.validate_result(self.repo, "oc-worker-01", None)
        self.assertIsNone(result)
        self.assertIn("not a JSON object", problems[0])

    def test_missing_key_is_fatal(self):
        data = make_result()
        del data["claims"]
        self.write_result(self.repo, data)
        result, problems = lane.validate_result(self.repo, "oc-worker-01", None)
        self.assertIsNone(result)
        self.assertIn("missing key: claims", problems)

    def test_wrong_type_is_fatal(self):
        self.write_result(self.repo, make_result(summary=42))
        result, problems = lane.validate_result(self.repo, "oc-worker-01", None)
        self.assertIsNone(result)
        self.assertIn("summary must be a string", problems)

    def test_lane_mismatch_is_fatal(self):
        self.write_result(self.repo, make_result(lane="oc-worker-02"))
        result, problems = lane.validate_result(self.repo, "oc-worker-01", None)
        self.assertIsNone(result)
        self.assertTrue(any("lane mismatch" in p for p in problems))

    def test_bool_is_not_an_int_in_tests(self):
        self.write_result(self.repo, make_result(
            tests=[{"cmd": "x", "passed": True, "failed": 0}]))
        result, problems = lane.validate_result(self.repo, "oc-worker-01", None)
        self.assertIsNone(result)
        self.assertIn("tests[0].passed must be an int", problems)

    def test_missing_report_is_a_problem_not_fatal(self):
        self.write_result(self.repo, make_result(), report=None)
        result, problems = lane.validate_result(self.repo, "oc-worker-01", None)
        self.assertIsNotNone(result)
        self.assertIn("missing .lane/REPORT.md", problems)

    def test_long_report_is_a_problem(self):
        self.write_result(self.repo, make_result(), report="x\n" * 201)
        result, problems = lane.validate_result(self.repo, "oc-worker-01", None)
        self.assertIsNotNone(result)
        self.assertTrue(any("max 200" in p for p in problems))

    def test_invalid_status_is_a_problem_not_fatal(self):
        self.write_result(self.repo, make_result(status="banana"))
        result, problems = lane.validate_result(self.repo, "oc-worker-01", None)
        self.assertIsNotNone(result)
        self.assertTrue(any("status not in done/partial/blocked" in p for p in problems))

    def test_files_changed_listed_but_not_changed(self):
        base = lane.head_sha(self.repo)
        self.write_result(self.repo, make_result(files_changed=["ghost.txt"]))
        result, problems = lane.validate_result(self.repo, "oc-worker-01", base)
        self.assertIsNotNone(result)
        self.assertTrue(any(p.startswith("listed but not changed:") and "ghost.txt" in p
                            for p in problems))

    def test_files_changed_changed_but_not_listed(self):
        base = lane.head_sha(self.repo)
        (self.repo / "new.txt").write_text("new\n", encoding="utf-8")
        self.write_result(self.repo, make_result(files_changed=[]))
        result, problems = lane.validate_result(self.repo, "oc-worker-01", base)
        self.assertIsNotNone(result)
        self.assertTrue(any(p.startswith("changed but not listed:") and "new.txt" in p
                            for p in problems))

    def test_lane_files_are_excluded_from_comparison(self):
        base = lane.head_sha(self.repo)
        self.write_result(self.repo, make_result(files_changed=[]))
        result, problems = lane.validate_result(self.repo, "oc-worker-01", base)
        self.assertIsNotNone(result)
        self.assertEqual([], problems)


class TestGc(LaneTest):
    def make_lane(self, name="oc-worker-01", **kw):
        # gc never touches lanes the ledger still calls open; these tests model judged lanes
        path = super().make_lane(name, **kw)
        code, out = self.cli("set", name, "rejected")
        self.assertEqual(0, code, out)
        return path

    def setup_five(self):
        names = {
            "running": "oc-worker-01",
            "dirty": "oc-worker-02",
            "ignored-data": "oc-worker-03",
            "merged": "oc-worker-04",
            "unmerged": "oc-worker-05",
        }
        for name in names.values():
            self.make_lane(name)
        unmerged = lane.worktree_path(names["unmerged"])
        (unmerged / "feature.txt").write_text("feature\n", encoding="utf-8")
        self.git("add", ".", cwd=unmerged)
        self.git("commit", "-m", "feature", cwd=unmerged)
        dirty = lane.worktree_path(names["dirty"])
        (dirty / "README.md").write_text("dirty\n", encoding="utf-8")
        ignored = lane.worktree_path(names["ignored-data"])
        (ignored / "junk.dat").write_text("junk\n", encoding="utf-8")
        code, out = self.cli("set", names["running"], "running")
        self.assertEqual(0, code, out)
        return names

    def test_gc_classifies_all_five_and_dry_run_changes_nothing(self):
        names = self.setup_five()
        code, out = self.cli("gc")
        self.assertEqual(0, code, out)
        for cls, name in names.items():
            matching = [line for line in out.splitlines()
                        if line.startswith(cls) and name in line]
            self.assertTrue(matching, f"{cls} not classified:\n{out}")
        for name in names.values():
            self.assertTrue(lane.worktree_path(name).exists(), name)
            entry = lane.load_ledger()["lanes"][name]
            self.assertNotEqual("removed", entry["status"], name)
        unmerged_branch = "agent/oc-worker-05"
        self.assertEqual(0, self.git_rc("show-ref", "--verify", "--quiet",
                                        f"refs/heads/{unmerged_branch}").returncode)
        self.assertEqual(1, self.git_rc("rev-parse", "-q", "--verify",
                                        f"refs/tags/{lane.tag_name(unmerged_branch)}").returncode)

    def test_gc_apply_removes_merged_only(self):
        names = self.setup_five()
        code, out = self.cli("gc", "--apply")
        self.assertEqual(0, code, out)
        self.assertFalse(lane.worktree_path(names["merged"]).exists())
        self.assertNotEqual(0, self.git_rc("show-ref", "--verify", "--quiet",
                                           "refs/heads/agent/oc-worker-04").returncode)
        self.assertEqual("removed", lane.load_ledger()["lanes"][names["merged"]]["status"])
        for key in ("running", "dirty", "ignored-data", "unmerged"):
            self.assertTrue(lane.worktree_path(names[key]).exists(), key)
        self.assertNotEqual(0, self.git_rc("rev-parse", "-q", "--verify",
                                           "refs/tags/archive/lanes/agent/oc-worker-05").returncode)

    def test_gc_include_unmerged_dry_run_creates_no_tag(self):
        names = self.setup_five()
        branch = "agent/oc-worker-05"
        code, out = self.cli("gc", "--include-unmerged")
        self.assertEqual(0, code, out)
        self.assertTrue(lane.worktree_path(names["unmerged"]).exists())
        self.assertEqual(0, self.git_rc("show-ref", "--verify", "--quiet",
                                        f"refs/heads/{branch}").returncode)
        self.assertNotEqual(0, self.git_rc("rev-parse", "-q", "--verify",
                                           f"refs/tags/{lane.tag_name(branch)}").returncode)

    def test_gc_include_unmerged_creates_tag_first(self):
        names = self.setup_five()
        unmerged = lane.worktree_path(names["unmerged"])
        tip = lane.head_sha(unmerged)
        branch = "agent/oc-worker-05"
        code, out = self.cli("gc", "--apply", "--include-unmerged")
        self.assertEqual(0, code, out)
        self.assertFalse(lane.worktree_path(names["unmerged"]).exists())
        tag = lane.tag_name(branch)
        tagged = self.git_rc("rev-parse", "-q", "--verify", f"refs/tags/{tag}")
        self.assertEqual(0, tagged.returncode)
        self.assertEqual(tip, tagged.stdout.strip())
        self.assertNotEqual(0, self.git_rc("show-ref", "--verify", "--quiet",
                                           f"refs/heads/{branch}").returncode)

    def test_gc_tag_conflict_keeps_unmerged_lane(self):
        names = self.setup_five()
        unmerged = lane.worktree_path(names["unmerged"])
        branch = "agent/oc-worker-05"
        main_tip = self.git("rev-parse", "main").stdout.strip()
        self.git("tag", lane.tag_name(branch), main_tip)
        code, out = self.cli("gc", "--apply", "--include-unmerged")
        self.assertEqual(0, code, out)
        self.assertTrue(unmerged.exists())
        self.assertEqual(0, self.git_rc("show-ref", "--verify", "--quiet",
                                        f"refs/heads/{branch}").returncode)
        self.assertIn("tag", out)


class TestLedgerAndList(LaneTest):
    def test_ledger_is_atomic_and_lanes_md_regenerated(self):
        self.make_lane("oc-worker-01")
        self.make_lane("oc-worker-02")
        code, out = self.cli("set", "oc-worker-02", "merged", "--note", "done|ok")
        self.assertEqual(0, code, out)
        state = lane.state_dir()
        self.assertTrue((state / "ledger.lock").exists())
        self.assertEqual([], list(state.glob("*.tmp*")))
        ledger = json.loads((state / "ledger.json").read_text(encoding="utf-8"))
        self.assertEqual({"oc-worker-01", "oc-worker-02"}, set(ledger["lanes"]))
        md = (state / "LANES.md").read_text(encoding="utf-8")
        self.assertIn("| Lane | Status |", md)
        self.assertIn("oc-worker-01", md)
        self.assertIn("merged", md)
        self.assertIn("done\\|ok", md)

    def test_concurrent_set_keeps_every_entry(self):
        names = [f"oc-worker-{i}" for i in range(10, 18)]
        env = os.environ.copy()
        procs = [subprocess.Popen([sys.executable, str(HERE / "lane.py"), "set", name, "created"],
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True) for name in names]
        for proc in procs:
            _out, err = proc.communicate(timeout=60)
            self.assertEqual(0, proc.returncode, err)
        ledger = lane.load_ledger()
        for name in names:
            self.assertIn(name, ledger["lanes"])

    def test_ls_hides_closed_lanes_unless_all(self):
        self.make_lane("oc-worker-01")
        self.make_lane("oc-worker-02")
        self.make_lane("oc-worker-03")
        self.cli("set", "oc-worker-02", "merged")
        self.cli("set", "oc-worker-03", "rejected")
        code, out = self.cli("ls")
        self.assertEqual(0, code, out)
        self.assertIn("oc-worker-01", out)
        self.assertNotIn("oc-worker-02", out)
        self.assertNotIn("oc-worker-03", out)
        code, out_all = self.cli("ls", "--all")
        self.assertEqual(0, code, out_all)
        self.assertIn("oc-worker-02", out_all)
        self.assertIn("oc-worker-03", out_all)


class TestAcceptWiring(LaneTest):
    def fake_accept(self, verdict, reasons=None):
        calls = {}

        def run_accept(**kwargs):
            report = Path(kwargs["out_dir"]) / "ACCEPT_REPORT.md"
            report.write_text(f"ACCEPT REPORT {verdict}\n", encoding="utf-8")
            calls.update(kwargs)
            return {"verdict": verdict, "reasons": reasons or [], "report": str(report)}

        module = types.ModuleType("accept")
        module.run_accept = run_accept
        old = sys.modules.get("accept")
        sys.modules["accept"] = module
        self.addCleanup(lambda: sys.modules.pop("accept", None) if old is None
                        else sys.modules.__setitem__("accept", old))
        return calls

    def test_accept_pass_is_wired_and_recorded(self):
        calls = self.fake_accept("PASS")
        worktree = self.make_lane("oc-worker-01")
        base = lane.load_ledger()["lanes"]["oc-worker-01"]["base"]
        code, out = self.cli("accept", "oc-worker-01", "--test", "true")
        self.assertEqual(0, code, out)
        self.assertEqual("oc-worker-01", calls["lane"])
        self.assertEqual(worktree, calls["worktree"])
        self.assertEqual(base, calls["base"])
        self.assertEqual(["true"], calls["tests"])
        self.assertEqual(900, calls["timeout"])
        self.assertEqual(lane.state_dir() / "oc-worker-01", calls["out_dir"])
        self.assertIn("ACCEPT REPORT PASS", out)
        self.assertEqual("accept-pass", lane.load_ledger()["lanes"]["oc-worker-01"]["status"])

    def test_accept_fail_exits_one(self):
        self.fake_accept("FAIL", reasons=["unit tests failed"])
        self.make_lane("oc-worker-01")
        code, out = self.cli("accept", "oc-worker-01", "--test", "false")
        self.assertEqual(1, code, out)
        self.assertIn("ACCEPT REPORT FAIL", out)
        self.assertIn("unit tests failed", out)
        self.assertEqual("accept-fail", lane.load_ledger()["lanes"]["oc-worker-01"]["status"])


if __name__ == "__main__":
    unittest.main()
