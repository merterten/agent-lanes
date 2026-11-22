"""Generic multi-machine support for agent-lanes, built on ssh and rsync.

Hosts are configured in ~/.config/agent-lanes/hosts.toml (override with
LANE_HOSTS).  Every ssh/rsync invocation is an argument list and can be
redirected with LANE_SSH / LANE_RSYNC so tests never touch the network.
"""
import json
import os
import posixpath
import shlex
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_HOSTS = "~/.config/agent-lanes/hosts.toml"
DEFAULT_CONNECT_TIMEOUT = 5
RUNNER_REMOTE_DIR = ".agent-lanes"
RUNNER_NAME = "remote_runner.sh"

PUSH_EXCLUDES = (
    ".git/", ".git", ".lane/", ".lane.prev/", "node_modules/", "obj/", "bin/",
    ".env*", "*.key", "*.pem", "id_*", "*credential*", "*secret*",
)
PULL_EXCLUDES = (".git/", ".git", ".lane-remote/", ".lane-task")
PUSH_PROTECTED = (".lane-remote/",)  # remote runner state survives a --delete push

CHECK_HINTS = {
    "ssh probe": "check the alias in ~/.ssh/config, the key, and the network",
    "prefix": "check the prefix words in hosts.toml (e.g. [\"wsl.exe\", \"-d\", \"Ubuntu\", \"--\"])",
    "rsync": "install rsync on the remote host (or set rsync_path for WSL)",
    "worker": "install the worker CLI on the remote host or set worker = ...",
    "root": "create the root folder and make it writable by the remote user",
    "systemd": "optional: without systemd --user the runner falls back to setsid/nohup",
}


class HostError(Exception):
    pass


@dataclass(frozen=True)
class Host:
    name: str
    ssh: str
    root: str
    prefix: tuple = field(default_factory=tuple)
    worker: str = ""
    nice: int = 19
    max_lanes: int = 2
    rsync_path: str = ""

    def effective_worker(self, default):
        return self.worker or default


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

def hosts_path():
    value = os.environ.get("LANE_HOSTS")
    return Path(value).expanduser() if value else Path(DEFAULT_HOSTS).expanduser()


def _as_int(table, key, default, name):
    value = table.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise HostError(f"host {name!r}: {key} must be an integer")
    return value


def _host_from_table(name, table):
    if not isinstance(table, dict):
        raise HostError(f"host {name!r} must be a table")
    ssh = table.get("ssh")
    root = table.get("root")
    if not isinstance(ssh, str) or not ssh.strip():
        raise HostError(f"host {name!r}: ssh is required")
    if not isinstance(root, str) or not root.strip():
        raise HostError(f"host {name!r}: root is required")
    prefix = table.get("prefix", [])
    if isinstance(prefix, str):
        prefix = shlex.split(prefix)
    if not isinstance(prefix, (list, tuple)) or not all(isinstance(p, str) for p in prefix):
        raise HostError(f"host {name!r}: prefix must be a list of strings")
    worker = table.get("worker", "")
    if worker is not None and not isinstance(worker, str):
        raise HostError(f"host {name!r}: worker must be a string")
    rsync_path = table.get("rsync_path", "")
    if rsync_path is not None and not isinstance(rsync_path, str):
        raise HostError(f"host {name!r}: rsync_path must be a string")
    nice = _as_int(table, "nice", 19, name)
    max_lanes = _as_int(table, "max_lanes", 2, name)
    if max_lanes < 1:
        raise HostError(f"host {name!r}: max_lanes must be at least 1")
    return Host(name=name, ssh=ssh.strip(), root=root.strip(), prefix=tuple(prefix),
                worker=worker or "", nice=nice, max_lanes=max_lanes,
                rsync_path=rsync_path or "")


def parse_hosts(data):
    if not isinstance(data, dict):
        raise HostError("hosts file must contain a TOML table")
    raw = data.get("hosts")
    tables = {}
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                raise HostError("each [[hosts]] entry needs a name")
            tables[item["name"]] = {k: v for k, v in item.items() if k != "name"}
    elif isinstance(raw, dict):
        tables = raw
    else:
        tables = {k: v for k, v in data.items() if k != "hosts"}
    return {name: _host_from_table(name, table) for name, table in tables.items()}


def load_hosts(path=None):
    target = Path(path).expanduser() if path else hosts_path()
    if not target.exists():
        return {}
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise HostError(f"cannot read hosts file {target}: {exc}")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise HostError(f"invalid TOML in {target}: {exc}")
    return parse_hosts(data)


def get_host(name, path=None):
    hosts = load_hosts(path)
    if name not in hosts:
        raise HostError(f"unknown host {name!r} (configure it in {hosts_path()})")
    return hosts[name]


def ssh_exe():
    return os.environ.get("LANE_SSH") or "ssh"


def rsync_exe():
    return os.environ.get("LANE_RSYNC") or "rsync"


def runner_local_path():
    return Path(__file__).resolve().parent / RUNNER_NAME


# ---------------------------------------------------------------------------
# ssh argument building
# ---------------------------------------------------------------------------

def ssh_options(connect_timeout=DEFAULT_CONNECT_TIMEOUT):
    return ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={int(connect_timeout)}"]


def quote_word(word):
    if word == "~" or word.startswith("~/"):
        return word
    return shlex.quote(word)


def remote_words(host, words):
    return list(host.prefix) + list(words)


def remote_command(host, words):
    return " ".join(quote_word(word) for word in remote_words(host, words))


def ssh_argv(host, words, connect_timeout=DEFAULT_CONNECT_TIMEOUT):
    return [ssh_exe()] + ssh_options(connect_timeout) + [host.ssh, remote_command(host, words)]


def probe_argv(host, connect_timeout=DEFAULT_CONNECT_TIMEOUT):
    return ssh_argv(host, ["true"], connect_timeout=connect_timeout)


def probe(host, timeout=15, connect_timeout=DEFAULT_CONNECT_TIMEOUT):
    try:
        return subprocess.run(probe_argv(host, connect_timeout), capture_output=True, stdin=subprocess.DEVNULL,
                              timeout=timeout).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def runner_path(host):
    return posixpath.join(host.root.rstrip("/"), RUNNER_REMOTE_DIR, RUNNER_NAME)


def remote_lane_dir(host, lane):
    return posixpath.join(host.root.rstrip("/"), lane)


def runner_words(host, subcommand, lane, env=None):
    words = []
    if env:
        words.append("env")
        words.extend(f"{key}={value}" for key, value in env.items())
    words += [runner_path(host), subcommand, remote_lane_dir(host, lane)]
    return words


def runner_ssh_argv(host, subcommand, lane, env=None, connect_timeout=DEFAULT_CONNECT_TIMEOUT):
    return ssh_argv(host, runner_words(host, subcommand, lane, env), connect_timeout)


def remote_exec(host, words, input_text=None, timeout=120, connect_timeout=DEFAULT_CONNECT_TIMEOUT):
    # No input -> empty stdin: ssh would otherwise read (and block on / swallow) the caller's stdin.
    stdin = {"input": input_text} if input_text is not None else {"stdin": subprocess.DEVNULL}
    return subprocess.run(ssh_argv(host, words, connect_timeout), capture_output=True, text=True,
                          timeout=timeout, **stdin)


# ---------------------------------------------------------------------------
# rsync argument building
# ---------------------------------------------------------------------------

def rsync_ssh(host, connect_timeout=DEFAULT_CONNECT_TIMEOUT):
    return " ".join(shlex.quote(part) for part in [ssh_exe()] + ssh_options(connect_timeout)
                    + [host.ssh])


def rsync_base(host, connect_timeout=DEFAULT_CONNECT_TIMEOUT):
    argv = [rsync_exe(), "-az", "-e", rsync_ssh(host, connect_timeout)]
    if host.rsync_path:
        argv += ["--rsync-path", host.rsync_path]
    return argv


def push_argv(host, local, lane, connect_timeout=DEFAULT_CONNECT_TIMEOUT):
    # --delete mirrors local deletions (else the next pull would resurrect them); excluded paths
    # (secrets, .git, remote state) are never deleted because --delete-excluded is not used.
    argv = rsync_base(host, connect_timeout) + ["--delete"]
    for pattern in PUSH_EXCLUDES + PUSH_PROTECTED:
        argv += ["--exclude", pattern]
    source = str(local).rstrip("/") + "/"
    destination = f"{host.ssh}:{remote_lane_dir(host, lane)}/"
    return argv + [source, destination]


def pull_argv(host, local, lane, dry_run=False, delete=False,
              connect_timeout=DEFAULT_CONNECT_TIMEOUT):
    argv = rsync_base(host, connect_timeout)
    for pattern in PULL_EXCLUDES:
        argv += ["--exclude", pattern]
    if dry_run:
        argv.append("--dry-run")
    if delete:
        argv.append("--delete")
    if dry_run and delete:
        argv.append("--itemize-changes")
    source = f"{host.ssh}:{remote_lane_dir(host, lane)}/"
    destination = str(local).rstrip("/") + "/"
    return argv + [source, destination]


def setup_argv(host, local_runner, connect_timeout=DEFAULT_CONNECT_TIMEOUT):
    argv = rsync_base(host, connect_timeout)
    return argv + [str(local_runner), f"{host.ssh}:{runner_path(host)}"]


def parse_deletions(output):
    deletions = []
    for line in output.splitlines():
        marker = "*deleting "
        if marker in line:
            path = line.split(marker, 1)[1].strip()
            if path:
                deletions.append(path)
    return deletions


def find_deletions(host, local, lane, timeout=120, connect_timeout=DEFAULT_CONNECT_TIMEOUT):
    argv = pull_argv(host, local, lane, dry_run=True, delete=True,
                     connect_timeout=connect_timeout)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return parse_deletions((proc.stdout or "") + (proc.stderr or ""))


# ---------------------------------------------------------------------------
# remote operations
# ---------------------------------------------------------------------------

def prepare(host, lane, timeout=120):
    return remote_exec(host, ["mkdir", "-p", remote_lane_dir(host, lane)], timeout=timeout)


def clean_lane(host, lane, timeout=120):
    target = remote_lane_dir(host, lane)
    quoted = " ".join(quote_word(posixpath.join(target, name))
                      for name in (".lane", ".lane.prev", ".lane-remote", ".lane-task"))
    return remote_exec(host, ["sh", "-c", f"rm -rf {quoted}"], timeout=timeout)


def stub(host, lane, timeout=120):
    return remote_exec(host, runner_words(host, "stub", lane), timeout=timeout)


def status(host, lane, timeout=120):
    return remote_exec(host, runner_words(host, "status", lane), timeout=timeout)


def stop(host, lane, timeout=120):
    return remote_exec(host, runner_words(host, "stop", lane), timeout=timeout)


def setup(host, local_runner=None, timeout=120):
    local_runner = Path(local_runner) if local_runner else runner_local_path()
    directory = posixpath.dirname(runner_path(host))
    made = remote_exec(host, ["mkdir", "-p", directory], timeout=timeout)
    if made.returncode != 0:
        return made
    copied = subprocess.run(setup_argv(host, local_runner), capture_output=True, stdin=subprocess.DEVNULL,
                            text=True, timeout=timeout)
    if copied.returncode != 0:
        return copied
    return remote_exec(host, ["chmod", "+x", runner_path(host)], timeout=timeout)


def _check(rows, name, ok, detail):
    rows.append({"check": name, "ok": bool(ok), "detail": detail,
                 "hint": CHECK_HINTS.get(name, "")})


def check_host(host, default_worker="oc-worker"):
    rows = []
    reachable = probe(host)
    _check(rows, "ssh probe", reachable, "reachable" if reachable else "no answer")
    if not reachable:
        for name in ("prefix", "rsync", "worker", "root", "systemd"):
            _check(rows, name, False, "not checked: ssh probe failed")
        return rows

    try:
        prefixed = remote_exec(host, ["sh", "-c", "printf agent-lanes-prefix-ok"])
        marked = "agent-lanes-prefix-ok" in (prefixed.stdout or "")
        _check(rows, "prefix", prefixed.returncode == 0 and marked,
               "ok" if marked else "command failed")
    except (OSError, subprocess.TimeoutExpired) as exc:
        _check(rows, "prefix", False, str(exc))

    for name, command, detail in (
        ("rsync", "command -v rsync >/dev/null 2>&1", "rsync found"),
        ("worker", f"command -v {shlex.quote(shlex.split(default_worker)[0])} >/dev/null 2>&1",
         f"{default_worker} found"),
    ):
        try:
            proc = remote_exec(host, ["sh", "-c", command])
            _check(rows, name, proc.returncode == 0,
                   detail if proc.returncode == 0 else "not found")
        except (OSError, subprocess.TimeoutExpired) as exc:
            _check(rows, name, False, str(exc))

    root = quote_word(host.root)
    try:
        proc = remote_exec(host, ["sh", "-c", f"mkdir -p {root} && test -w {root}"])
        _check(rows, "root", proc.returncode == 0,
               host.root if proc.returncode == 0 else "not writable")
        mode = remote_exec(host, ["sh", "-c", f"stat -c %a {root} 2>/dev/null || true"])
        value = (mode.stdout or "").strip()
        if value and value[-1].isdigit() and int(value[-1]) & 2:
            rows[-1]["warning"] = f"root {host.root} is world-writable (mode {value})"
    except (OSError, subprocess.TimeoutExpired) as exc:
        _check(rows, "root", False, str(exc))

    try:
        proc = remote_exec(host, ["sh", "-c",
                                  "command -v systemd-run >/dev/null 2>&1 && "
                                  "systemd-run --user --scope --quiet true >/dev/null 2>&1"])
        available = proc.returncode == 0
        _check(rows, "systemd", available,
               "yes" if available else "no (setsid fallback)")
    except (OSError, subprocess.TimeoutExpired) as exc:
        _check(rows, "systemd", False, str(exc))
    return rows


def load_ledger_hosts(path=None):
    value = os.environ.get("LANE_STATE_DIR")
    base = Path(value).expanduser() if value else Path(os.environ.get("LANE_ROOT")
                                                       or "~/agent-lanes").expanduser() / "lanes"
    ledger = base / "ledger.json"
    if not ledger.exists():
        return {}
    try:
        data = json.loads(ledger.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data.get("lanes", {}) if isinstance(data, dict) else {}
