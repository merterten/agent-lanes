"""Tests for built-in remote host support (agent_lanes/remote.py)."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import lane  # noqa: E402
import remote  # noqa: E402
from test_lane import FAKE_SSH, FAKE_RSYNC, VALID_WORKER, LaneTest  # noqa: E402

HOSTS_TEMPLATE = '''[hosts.gpu-box]
ssh = "user@203.0.113.10"
root = "~/agent-lanes/worktrees"
prefix = ["wsl.exe", "-d", "Ubuntu", "--"]
worker = "oc-worker"
nice = 19
max_lanes = 2
rsync_path = "wsl.exe -d Ubuntu -- rsync"
'''


def host(**overrides):
    values = dict(name="gpu-box", ssh="user@203.0.113.10", root="~/agent-lanes/worktrees")
    values.update(overrides)
    return remote.Host(**values)


class HostsConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="remote-test-"))
        self._old = os.environ.get("LANE_HOSTS")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("LANE_HOSTS", None)
        else:
            os.environ["LANE_HOSTS"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, text, name="hosts.toml"):
        path = self.tmp / name
        path.write_text(textwrap.dedent(text), encoding="utf-8")
        return path

    def test_parses_hosts_table(self):
        hosts = remote.load_hosts(self.write(HOSTS_TEMPLATE))
        item = hosts["gpu-box"]
        self.assertEqual("user@203.0.113.10", item.ssh)
        self.assertEqual("~/agent-lanes/worktrees", item.root)
        self.assertEqual(("wsl.exe", "-d", "Ubuntu", "--"), item.prefix)
        self.assertEqual("wsl.exe -d Ubuntu -- rsync", item.rsync_path)

    def test_parses_top_level_table(self):
        path = self.write('''\
            [laptop]
            ssh = "laptop"
            root = "~/lanes"
        ''')
        item = remote.load_hosts(path)["laptop"]
        self.assertEqual("laptop", item.ssh)

    def test_parses_hosts_list(self):
        path = self.write('''\
            [[hosts]]
            name = "gpu-box"
            ssh = "user@gpu-box"
            root = "/srv/lanes"
        ''')
        self.assertEqual("/srv/lanes", remote.load_hosts(path)["gpu-box"].root)

    def test_missing_file_is_empty(self):
        self.assertEqual({}, remote.load_hosts(self.tmp / "nope.toml"))

    def test_invalid_toml_is_an_error(self):
        with self.assertRaises(remote.HostError):
            remote.load_hosts(self.write("not = [toml"))

    def test_missing_ssh_is_an_error(self):
        with self.assertRaisesRegex(remote.HostError, "ssh is required"):
            remote.load_hosts(self.write('[hosts.x]\nroot = "/tmp"\n'))

    def test_missing_root_is_an_error(self):
        with self.assertRaisesRegex(remote.HostError, "root is required"):
            remote.load_hosts(self.write('[hosts.x]\nssh = "x"\n'))

    def test_bad_prefix_is_an_error(self):
        with self.assertRaisesRegex(remote.HostError, "prefix"):
            remote.load_hosts(self.write('[hosts.x]\nssh = "x"\nroot = "/tmp"\nprefix = [1]\n'))

    def test_bad_nice_is_an_error(self):
        with self.assertRaisesRegex(remote.HostError, "nice"):
            remote.load_hosts(self.write('[hosts.x]\nssh = "x"\nroot = "/tmp"\nnice = "high"\n'))

    def test_bad_max_lanes_is_an_error(self):
        with self.assertRaisesRegex(remote.HostError, "max_lanes"):
            remote.load_hosts(self.write('[hosts.x]\nssh = "x"\nroot = "/tmp"\nmax_lanes = 0\n'))

    def test_defaults_are_applied(self):
        item = remote.load_hosts(self.write('[hosts.x]\nssh = "x"\nroot = "/tmp"\n'))["x"]
        self.assertEqual((), item.prefix)
        self.assertEqual("", item.worker)
        self.assertEqual(19, item.nice)
        self.assertEqual(2, item.max_lanes)
        self.assertEqual("", item.rsync_path)

    def test_lane_hosts_env_overrides_path(self):
        path = self.write('[hosts.envhost]\nssh = "x"\nroot = "/tmp"\n')
        os.environ["LANE_HOSTS"] = str(path)
        self.assertIn("envhost", remote.load_hosts())

    def test_unknown_host_is_an_error(self):
        path = self.write(HOSTS_TEMPLATE)
        with self.assertRaisesRegex(remote.HostError, "unknown host"):
            remote.get_host("nowhere", path)

    def test_string_prefix_is_split(self):
        item = remote.load_hosts(self.write(
            '[hosts.x]\nssh = "x"\nroot = "/tmp"\nprefix = "wsl.exe -d Ubuntu --"\n'))["x"]
        self.assertEqual(("wsl.exe", "-d", "Ubuntu", "--"), item.prefix)


class ArgumentTests(unittest.TestCase):
    def setUp(self):
        self._old = {key: os.environ.get(key) for key in ("LANE_SSH", "LANE_RSYNC")}

    def tearDown(self):
        for key, value in self._old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_ssh_has_batchmode_and_connect_timeout(self):
        argv = remote.ssh_argv(host(), ["true"])
        self.assertIn("BatchMode=yes", argv)
        self.assertTrue(any(a.startswith("ConnectTimeout=") for a in argv))

    def test_ssh_never_forwards_the_agent(self):
        for words in (["true"], ["env", "A=1", "/x/runner", "run", "/x/lane"]):
            self.assertNotIn("-A", remote.ssh_argv(host(prefix=("wsl.exe", "--")), words))

    def test_ssh_custom_connect_timeout(self):
        self.assertIn("ConnectTimeout=9", remote.ssh_argv(host(), ["true"], connect_timeout=9))

    def test_prefix_is_prepended_to_the_remote_command(self):
        argv = remote.ssh_argv(host(prefix=("wsl.exe", "-d", "Ubuntu", "--")), ["uname"])
        self.assertEqual("wsl.exe -d Ubuntu -- uname", argv[-1])

    def test_remote_words_are_shell_quoted(self):
        argv = remote.ssh_argv(host(), ["echo", "a b", "it's"])
        self.assertEqual("echo 'a b' 'it'\"'\"'s'", argv[-1])

    def test_tilde_paths_stay_expandable(self):
        item = host(root="~/agent-lanes/worktrees")
        argv = remote.ssh_argv(item, remote.runner_words(item, "run", "oc-worker-01"))
        self.assertIn("~/agent-lanes/worktrees/.agent-lanes/remote_runner.sh", argv[-1])
        self.assertNotIn("'~/agent-lanes", argv[-1])

    def test_lane_ssh_override(self):
        os.environ["LANE_SSH"] = "/tmp/my-ssh"
        self.assertEqual("/tmp/my-ssh", remote.ssh_argv(host(), ["true"])[0])

    def test_probe_uses_batchmode(self):
        self.assertIn("BatchMode=yes", remote.probe_argv(host()))

    def test_push_excludes_every_secret_pattern(self):
        argv = remote.push_argv(host(), "/local/lane", "oc-worker-01")
        for pattern in (".git/", ".lane/", ".lane.prev/", "node_modules/", "obj/", "bin/",
                        ".env*", "*.key", "*.pem", "id_*", "*credential*", "*secret*"):
            self.assertIn(pattern, argv)

    def test_rsync_path_is_applied(self):
        argv = remote.push_argv(host(rsync_path="wsl.exe -d Ubuntu -- rsync"),
                                "/local/lane", "oc-worker-01")
        self.assertIn("--rsync-path", argv)
        self.assertIn("wsl.exe -d Ubuntu -- rsync", argv)

    def test_rsync_ssh_command_has_batchmode_and_no_agent_forwarding(self):
        argv = remote.push_argv(host(), "/local/lane", "oc-worker-01")
        ssh_arg = argv[argv.index("-e") + 1]
        self.assertIn("BatchMode=yes", ssh_arg)
        self.assertIn("ConnectTimeout=5", ssh_arg)
        self.assertNotIn("-A", ssh_arg.split())

    def test_pull_never_deletes(self):
        argv = remote.pull_argv(host(), "/local/lane", "oc-worker-01")
        self.assertNotIn("--delete", argv)

    def test_pull_excludes_git_and_runner_state(self):
        argv = remote.pull_argv(host(), "/local/lane", "oc-worker-01")
        for pattern in (".git", ".git/", ".lane-remote/", ".lane-task"):
            self.assertIn(pattern, argv)

    def test_rsync_override(self):
        os.environ["LANE_RSYNC"] = "/tmp/my-rsync"
        self.assertEqual("/tmp/my-rsync", remote.push_argv(host(), "/l", "oc-worker-01")[0])

    def test_runner_path_and_lane_dir(self):
        item = host(root="/srv/agent-lanes/worktrees/")
        self.assertEqual("/srv/agent-lanes/worktrees/oc-worker-01",
                         remote.remote_lane_dir(item, "oc-worker-01"))
        self.assertEqual("/srv/agent-lanes/worktrees/.agent-lanes/remote_runner.sh",
                         remote.runner_path(item))

    def test_runner_words_include_env_and_lane(self):
        words = remote.runner_words(host(), "run", "oc-worker-01",
                                    env={"LANE_WORKER": "oc-worker", "LANE_NICE": "19"})
        self.assertEqual("env", words[0])
        self.assertIn("LANE_WORKER=oc-worker", words)
        self.assertIn("LANE_NICE=19", words)
        self.assertEqual("run", words[-2])
        self.assertTrue(words[-1].endswith("oc-worker-01"))

    def test_parse_deletions(self):
        text = ">f+++++++++ keep.txt\n*deleting   gone.txt\n*deleting   sub/old.txt\n"
        self.assertEqual(["gone.txt", "sub/old.txt"], remote.parse_deletions(text))

    def test_setup_argv_targets_the_runner_path(self):
        argv = remote.setup_argv(host(), "/repo/agent_lanes/remote_runner.sh")
        self.assertEqual("user@203.0.113.10:~/agent-lanes/worktrees/.agent-lanes/remote_runner.sh",
                         argv[-1])


class RemoteOperationTests(LaneTest):
    def test_push_excludes_secrets_on_disk(self):
        self.fake_remote()
        local = self.tmp / "local-lane"
        local.mkdir()
        for name in (".env", ".env.local", "server.key", "cert.pem", "id_rsa",
                     "aws_credential", "my_secret.txt", "normal.txt"):
            (local / name).write_text("x\n", encoding="utf-8")
        (local / ".git").mkdir()
        (local / ".git" / "config").write_text("x\n", encoding="utf-8")
        (local / "src").mkdir()
        (local / "src" / ".env").write_text("x\n", encoding="utf-8")
        target = self.tmp / "oc-worker-01"
        proc = subprocess.run(remote.push_argv(host(root=str(self.tmp)), local, "oc-worker-01"),
                              capture_output=True)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertTrue((target / "normal.txt").exists())
        for name in (".env", ".env.local", "server.key", "cert.pem", "id_rsa",
                     "aws_credential", "my_secret.txt", ".git"):
            self.assertFalse((target / name).exists(), name)
        self.assertFalse((target / "src" / ".env").exists())
        excludes = [arg for call in self.rsync_calls() for arg in call if arg.startswith("*")]
        self.assertIn("*secret*", excludes)

    def test_find_deletions_uses_dry_run(self):
        self.fake_remote(deletions=["gone.txt", "sub/old.txt"])
        found = remote.find_deletions(host(root=str(self.tmp)), self.tmp / "local", "oc-worker-01")
        self.assertEqual(["gone.txt", "sub/old.txt"], found)
        call = self.rsync_calls()[-1]
        self.assertIn("--dry-run", call)
        self.assertIn("--delete", call)

    def test_setup_copies_runner_and_is_idempotent(self):
        root = self.write_hosts()
        self.fake_remote()
        code, out = self.cli("host", "setup", "gpu-box")
        self.assertEqual(0, code, out)
        runner = root / ".agent-lanes" / "remote_runner.sh"
        self.assertTrue(runner.exists())
        self.assertTrue(os.access(runner, os.X_OK))
        code, out = self.cli("host", "setup", "gpu-box")
        self.assertEqual(0, code, out)
        self.assertEqual(2, len(self.rsync_calls()))

    def test_hosts_lists_reachability_and_running_lanes(self):
        self.write_hosts()
        self.fake_remote()
        self.make_lane("oc-worker-01")
        lane.mutate_ledger(lambda l: l["oc-worker-01"].update(status="running", where="gpu-box"))
        with mock.patch.object(lane.remote, "probe", side_effect=lambda h, **kw: h.name == "gpu-box"):
            code, out = self.cli("hosts")
        self.assertEqual(0, code, out)
        self.assertIn("gpu-box", out)
        self.assertIn("yes", out)
        self.assertIn("oc-worker-01", out)

    def test_host_check_prints_pass_fail_and_fixes(self):
        self.write_hosts()
        class Proc:
            def __init__(self, rc, stdout=""):
                self.returncode = rc
                self.stdout = stdout
                self.stderr = ""
        def fake_exec(h, words, **kw):
            text = " ".join(words)
            if "printf agent-lanes-prefix-ok" in text:
                return Proc(0, "agent-lanes-prefix-ok")
            if "command -v rsync" in text:
                return Proc(1)
            if "stat -c %a" in text:
                return Proc(0, "777\n")
            return Proc(0)
        with mock.patch.object(lane.remote, "probe", return_value=True), \
                mock.patch.object(lane.remote, "remote_exec", side_effect=fake_exec):
            code, out = self.cli("host", "check", "gpu-box")
        self.assertEqual(1, code, out)
        self.assertIn("ssh probe", out)
        self.assertIn("PASS", out)
        self.assertIn("FAIL", out)
        self.assertIn("fix:", out)
        self.assertIn("world-writable", out)
        self.assertIn("systemd", out)

    def test_host_check_systemd_no_is_not_a_failure(self):
        self.write_hosts()
        class Proc:
            def __init__(self, rc, stdout=""):
                self.returncode = rc
                self.stdout = stdout
                self.stderr = ""
        def fake_exec(h, words, **kw):
            text = " ".join(words)
            if "printf agent-lanes-prefix-ok" in text:
                return Proc(0, "agent-lanes-prefix-ok")
            if "systemd-run" in text:
                return Proc(1)
            if "stat -c %a" in text:
                return Proc(0, "755\n")
            return Proc(0)
        with mock.patch.object(lane.remote, "probe", return_value=True), \
                mock.patch.object(lane.remote, "remote_exec", side_effect=fake_exec):
            code, out = self.cli("host", "check", "gpu-box")
        self.assertEqual(0, code, out)
        self.assertIn("no (setsid fallback)", out)

    def test_stop_remote_lane(self):
        root = self.write_hosts()
        self.fake_remote()
        self.setup_remote(root)
        self.make_lane("oc-worker-01")
        lane.mutate_ledger(lambda l: l["oc-worker-01"].update(status="running", where="gpu-box"))
        state = root / "oc-worker-01" / ".lane-remote"
        state.mkdir(parents=True)
        (state / "oc-worker-01.status").write_text("RUNNING 2026-01-01T00:00:00Z\n",
                                                   encoding="utf-8")
        code, out = self.cli("stop", "oc-worker-01")
        self.assertEqual(0, code, out)
        self.assertIn("stopped", out)
        status = (state / "oc-worker-01.status").read_text(encoding="utf-8")
        self.assertIn("END rc=143", status)
        self.assertIn("stopped", lane.load_ledger()["lanes"]["oc-worker-01"]["note"])

    def test_stop_local_lane_is_refused(self):
        self.make_lane("oc-worker-01")
        code, out = self.cli("stop", "oc-worker-01")
        self.assertEqual(1, code, out)
        self.assertIn("not a remote lane", out)

    def test_max_lanes_guard(self):
        self.write_hosts(max_lanes=1)
        self.fake_remote()
        self.make_lane("oc-worker-01")
        self.make_lane("oc-worker-02")
        lane.mutate_ledger(lambda l: l["oc-worker-01"].update(status="running", where="gpu-box"))
        code, out = self.cli("run", "oc-worker-02", str(self.brief()), "--host", "gpu-box")
        self.assertEqual(1, code, out)
        self.assertIn("max_lanes", out)

    def test_pull_deletions_are_warned(self):
        worker = self.script("fake-worker", VALID_WORKER)
        root = self.write_hosts(worker=str(worker))
        self.fake_remote(deletions=["old.txt"])
        self.setup_remote(root)
        self.make_lane("oc-worker-01")
        code, out = self.cli("run", "oc-worker-01", str(self.brief()), "--host", "gpu-box",
                             "--poll", "0")
        self.assertEqual(0, code, out)
        self.assertIn("warning: gpu-box deleted old.txt", out)

    def test_unknown_host_is_refused(self):
        self.make_lane("oc-worker-01")
        code, out = self.cli("run", "oc-worker-01", str(self.brief()), "--host", "missing")
        self.assertEqual(1, code, out)
        self.assertIn("unknown host", out)

    def test_no_wait_starts_without_polling_or_pulling(self):
        worker = self.script("fake-worker", VALID_WORKER)
        root = self.write_hosts(worker=str(worker))
        self.fake_remote()
        self.setup_remote(root)
        self.make_lane("oc-worker-01")
        code, out = self.cli("run", "oc-worker-01", str(self.brief()), "--host", "gpu-box",
                             "--no-wait", "--poll", "0")
        self.assertEqual(0, code, out)
        self.assertIn("no-wait", out)
        self.assertFalse([c for c in self.rsync_calls() if ":" in c[-2]])
        self.assertEqual("gpu-box", lane.load_ledger()["lanes"]["oc-worker-01"]["where"])


class RunnerScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="runner-test-"))
        self.runner = HERE / "remote_runner.sh"
        self.lane = self.tmp / "oc-worker-01"
        self.lane.mkdir()
        self.worker = self.tmp / "worker.py"
        self.worker.write_text("#!" + sys.executable + textwrap.dedent('''
            import json, os, sys
            open("worker.out", "w").write("task=" + sys.argv[-1][:20] + "\\n")
            os.makedirs(".lane", exist_ok=True)
            result = {"lane": os.path.basename(os.getcwd()), "status": "done",
                      "summary": "remote", "files_changed": ["worker.out"],
                      "tests": [], "claims": [], "blockers": []}
            json.dump(result, open(".lane/LANE_RESULT.json", "w"))
            open(".lane/REPORT.md", "w").write("ok\\n")
        '''), encoding="utf-8")
        self.worker.chmod(0o755)
        self.cleanup_path = None

    def tearDown(self):
        if self.cleanup_path:
            os.environ["PATH"] = self.cleanup_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    def minimal_path(self):
        bindir = self.tmp / "bin"
        bindir.mkdir(exist_ok=True)
        for name in ("mkdir", "cat", "basename", "dirname", "date", "sh", "setsid",
                     "nohup", "nice", "sed", "chmod", "grep", "rm", "kill", "tail", "env"):
            found = shutil.which(name)
            if found:
                (bindir / name).symlink_to(found)
        return str(bindir)

    def run_runner(self, *args, input_text="", env=None):
        values = dict(os.environ)
        values["LANE_WORKER"] = str(self.worker)
        values["LANE_NICE"] = "19"
        if env:
            values.update(env)
        return subprocess.run(["/bin/sh", str(self.runner)] + list(args),
                              input=input_text, capture_output=True, text=True, env=values)

    def wait_for_end(self, timeout=5):
        end = time.time() + timeout
        status = self.lane / ".lane-remote" / "oc-worker-01.status"
        while time.time() < end:
            if status.exists() and "END rc=" in status.read_text(encoding="utf-8"):
                return status.read_text(encoding="utf-8")
            time.sleep(0.05)
        return status.read_text(encoding="utf-8") if status.exists() else ""

    def test_run_then_status_reports_running_and_end(self):
        path = self.minimal_path()
        self.assertIsNone(shutil.which("systemd-run", path=path))
        proc = self.run_runner("run", str(self.lane), input_text="do it\\n",
                               env={"PATH": path, "LANE_NO_SYSTEMD": "0"})
        self.assertEqual(0, proc.returncode, proc.stderr)
        text = self.wait_for_end()
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("RUNNING "), text)
        self.assertIn("END rc=0", lines[-1])
        self.assertIn("do it", (self.lane / ".lane-task").read_text(encoding="utf-8"))
        self.assertTrue((self.lane / ".lane" / "LANE_RESULT.json").exists())
        status = self.run_runner("status", str(self.lane))
        self.assertIn("END rc=0", status.stdout)

    def test_stub_writes_a_git_stub(self):
        proc = self.run_runner("stub", str(self.lane))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("gitdir:", (self.lane / ".git").read_text(encoding="utf-8"))

    def test_invalid_lane_names_are_refused(self):
        for name in ("oc-worker-1234", "x-worker-1", "OC-worker-01", "oc-worker-1x"):
            bad = self.tmp / name
            bad.mkdir()
            proc = self.run_runner("stub", str(bad))
            self.assertEqual(2, proc.returncode, name)
            self.assertIn("invalid lane name", proc.stderr)

    def test_status_without_a_run(self):
        proc = self.run_runner("status", str(self.lane))
        self.assertEqual(0, proc.returncode)
        self.assertIn("no status", proc.stdout)

    def test_stop_writes_end_and_removes_pidfile(self):
        state = self.lane / ".lane-remote"
        state.mkdir()
        (state / "oc-worker-01.status").write_text("RUNNING now\\n", encoding="utf-8")
        (state / "oc-worker-01.pid").write_text("999999\\n", encoding="utf-8")
        proc = self.run_runner("stop", str(self.lane))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("END rc=143", (state / "oc-worker-01.status").read_text(encoding="utf-8"))
        self.assertFalse((state / "oc-worker-01.pid").exists())

    def test_systemd_run_is_preferred_when_present(self):
        bindir = self.tmp / "systemd-bin"
        bindir.mkdir()
        log = self.tmp / "systemd.log"
        fake = bindir / "systemd-run"
        fake.write_text("#!" + sys.executable + textwrap.dedent(f'''
            import json, os, subprocess, sys
            args = sys.argv[1:]
            open({str(log)!r}, "a").write(json.dumps(args) + "\\n")
            launcher = args[args.index("--") + 1]
            sys.exit(subprocess.run([launcher]).returncode)
        '''), encoding="utf-8")
        fake.chmod(0o755)
        env = dict(os.environ)
        env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
        proc = self.run_runner("run", str(self.lane), input_text="systemd task\\n", env=env)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.wait_for_end()
        calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertIn("--unit", calls[0])
        self.assertIn("agent-lane-oc-worker-01", calls[0])
        self.assertIn("Nice=19", calls[0])

    def test_ps_shows_the_last_status_line(self):
        state = self.lane / ".lane-remote"
        state.mkdir()
        (state / "oc-worker-01.status").write_text("RUNNING now\n", encoding="utf-8")
        proc = self.run_runner("ps", str(self.lane))
        self.assertEqual(0, proc.returncode)
        self.assertIn("oc-worker-01", proc.stdout)
        self.assertIn("RUNNING", proc.stdout)


if __name__ == "__main__":
    unittest.main()


class ReviewerPushDeleteTest(unittest.TestCase):
    """Reviewer: push mirrors local deletions but never deletes excluded secrets or runner state."""

    def host(self):
        return remote.Host(name="gpu-box", ssh="user@203.0.113.10", root="~/agent-lanes/worktrees")

    def test_push_uses_delete_without_delete_excluded(self):
        argv = remote.push_argv(self.host(), "/tmp/wt", "oc-worker-01")
        self.assertIn("--delete", argv)
        self.assertNotIn("--delete-excluded", argv)

    def test_push_protects_runner_state_and_secrets(self):
        argv = remote.push_argv(self.host(), "/tmp/wt", "oc-worker-01")
        excluded = [argv[i + 1] for i, a in enumerate(argv) if a == "--exclude"]
        for pattern in (".lane-remote/", ".env*", "*.key", "*.pem", "id_*", "*secret*", ".git/"):
            self.assertIn(pattern, excluded, pattern)

    def test_pull_never_deletes(self):
        argv = remote.pull_argv(self.host(), "/tmp/wt", "oc-worker-01")
        self.assertNotIn("--delete", argv)


class ReviewerStdinTest(unittest.TestCase):
    """Reviewer: ssh/rsync calls without input must not read the caller's stdin (they hung when lane ran from a pipe)."""

    def test_probe_and_exec_do_not_block_on_an_open_stdin(self):
        import tempfile, textwrap
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "fake-ssh"
            fake.write_text("#!" + sys.executable + "\nimport sys\nsys.stdin.read()\nsys.exit(0)\n")
            fake.chmod(0o755)
            code = textwrap.dedent(f"""
                import os, sys
                sys.path.insert(0, {str(Path(remote.__file__).parent)!r})
                os.environ["LANE_SSH"] = {str(fake)!r}
                import remote
                h = remote.Host(name="gpu-box", ssh="user@203.0.113.10", root="~/w")
                assert remote.probe(h, timeout=5), "probe blocked on stdin"
                assert remote.remote_exec(h, ["true"], timeout=5).returncode == 0
                print("ok")
            """)
            proc = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                rc = proc.wait(timeout=20)  # stdin is never written or closed while we wait
            except subprocess.TimeoutExpired:
                rc = None
            finally:
                proc.kill()
                proc.stdin.close()
            out, err = proc.stdout.read(), proc.stderr.read()
            self.assertEqual(0, rc, f"blocked on stdin or failed: {err}")
            self.assertIn("ok", out, err)
