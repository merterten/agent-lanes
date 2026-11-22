# Multi-machine lanes

`agent-lanes` can spread a queue over the spare PCs, laptops and GPU boxes on
your desk or office network. One machine stays the coordinator: it owns the git
worktrees, the ledger and the queue state. Workers run over ssh on the other
machines, and results are copied back with rsync.

The point is throughput: a long queue can use every idle machine instead of
waiting for one. It is not a cluster scheduler. Each lane still runs in its own
git worktree, and the remote machine only sees one lane folder at a time.

## 1. Describe each machine

Hosts live in `~/.config/agent-lanes/hosts.toml` (override with `LANE_HOSTS`).

```toml
[hosts.gpu-box]
ssh = "gpu-box"                       # an ~/.ssh/config alias works best
root = "~/agent-lanes/worktrees"      # mirrors LANE_WORKTREES
worker = "oc-worker"                  # remote agent CLI
nice = 19
max_lanes = 2                         # queue capacity on this machine

[hosts.laptop]
ssh = "user@203.0.113.10"
root = "~/agent-lanes/worktrees"
max_lanes = 1
# optional command prefix: Windows PC running the worker inside WSL
prefix = ["wsl.exe", "-d", "Ubuntu", "--"]
# WSL also needs rsync inside the distro
rsync_path = "wsl.exe -d Ubuntu -- rsync"

[hosts.mac-mini]
ssh = "mac-mini"
root = "/Users/user/agent-lanes/worktrees"
max_lanes = 1
```

Reference:

| Key | Meaning |
|---|---|
| `ssh` | ssh destination: an alias from `~/.ssh/config`, or `user@203.0.113.10`. |
| `root` | Remote worktree root. It must mirror the coordinator's layout; each lane is `root/LANE`. |
| `prefix` | Optional argv prefix for every remote command (WSL, containers). |
| `worker` | Remote agent CLI. Defaults to the local worker name. |
| `nice` | `nice` value passed to the remote runner. |
| `max_lanes` | How many lanes this host may run at once. The default is 2. |
| `rsync_path` | Optional remote rsync command, needed for WSL. |

`local` is not written in the file. Its capacity is the queue's `--max` value
(default 5).

## 2. SSH setup

Use a dedicated key for lanes, not your personal interactive key:

```
ssh-keygen -t ed25519 -f ~/.ssh/agent_lanes -C "agent-lanes"
ssh-copy-id -i ~/.ssh/agent_lanes.pub gpu-box
```

Give every machine an alias in `~/.ssh/config`:

```
Host gpu-box
  HostName 203.0.113.10
  User user
  IdentityFile ~/.ssh/agent_lanes
  IdentitiesOnly yes
  BatchMode yes
```

Rules that matter:

- `BatchMode yes` (the tool also passes `-o BatchMode=yes`): never prompt.
- `IdentitiesOnly yes`: do not offer every key in your agent.
- Never enable agent forwarding. Lanes do not need your keys on the remote host.
- Check once with `ssh gpu-box true` before configuring lanes.

## 3. Prepare each remote machine

```
lane host setup gpu-box      # installs remote_runner.sh under root/.agent-lanes
lane host check gpu-box      # prints a PASS/FAIL table with fix hints
```

`lane host check` tests:

| Row | What it means |
|---|---|
| `ssh probe` | The host answers a non-interactive ssh command. |
| `prefix` | The configured command prefix can run a shell command. |
| `rsync` | `rsync` exists on the remote side (or via `rsync_path`). |
| `worker` | The remote agent CLI is on `PATH`. |
| `root` | The worktree root exists and is writable. |
| `systemd` | Optional: `systemd --user` is usable. "no" is not a failure. |

The runner prefers `systemd-run --user` and falls back to `setsid`/`nohup`.
For lanes that must survive logout, enable lingering on the remote account:

```
loginctl enable-linger user
```

Without lingering, a user systemd session may be killed when you disconnect;
the setsid fallback still runs while the ssh session's process tree survives,
but it is less robust.

## 4. Windows notes

- Install the Windows OpenSSH Server feature and start `sshd`.
- The Windows network profile must be **Private** for inbound sshd to be
  allowed by the firewall. Public/guest networks usually block it.
- If the worker lives in WSL, set `prefix` and `rsync_path` as shown above, and
  make sure the WSL distro is running (`wsl.exe -d Ubuntu -- true`). A stopped
  distro makes every ssh command fail even when Windows itself is reachable.
- Use the WSL user's paths for `root`; they are the paths the prefixed commands
  see.

macOS needs nothing special beyond Remote Login being enabled and rsync (or a
newer rsync installed with Homebrew) on `PATH`.

## 5. Run a multi-machine queue

```
lane queue work.json --hosts local,gpu-box,laptop --max 3
```

- `--max 3` is the local capacity. Each remote host uses its `max_lanes`.
- An entry may pin a host: `{"lane": "oc-worker-07", "brief": "brief.md", "host": "gpu-box"}`.
- Without `host`, the scheduler picks any machine with free capacity, preferring
  reachable remote hosts over local.
- Each remote host is probed once. A host that failed is probed again before the
  next assignment, so a machine that comes back can start receiving lanes.
- Unreachable hosts never receive new lanes.
- A pinned entry may opt into local fallback:
  `{"lane": "oc-worker-08", "brief": "brief.md", "host": "gpu-box", "fallback_local": true}`.
- Quota/rate-limit detection and the `STOP` file still stop new starts across all
  machines. Lanes already running are allowed to finish.
- The queue state file records the chosen `host` for every lane.

## 6. What is never copied

Pushes exclude `.git/`, `.lane/`, build output and credential-looking files
(`.env*`, `*.key`, `*.pem`, `id_*`, `*credential*`, `*secret*`). These patterns
are deliberate: a remote machine should not receive your API keys, ssh keys or
repository metadata. Pulls never delete local files; if the remote side deleted
something, the coordinator warns and keeps its copy.

The remote machine still runs your agent CLI. Treat it as untrusted for
credentials: run the agent inside its own sandbox there too, with no access to
your home directory, ssh agent or cloud tokens. The excludes reduce accidental
copies, they do not make the remote side safe by themselves.

## 7. Troubleshooting

- **Host becomes unreachable while a lane runs.** The lane keeps running on the
  remote host. The coordinator records it as still running; retry later with
  `lane collect oc-worker-07` from a network that reaches the host.
- **Guest or corporate wifi blocks ssh.** Use `lane run ... --host-or-local` for
  a single lane, or leave that host out of `--hosts`; the queue will use local
  capacity.
- **`lane host check` says rsync missing on WSL.** Set `rsync_path` to
  `wsl.exe -d Ubuntu -- rsync` and install rsync inside the distro.
- **A pinned lane never starts.** The host is down and `fallback_local` is not
  set. Add the flag or remove the pin. Such lanes are marked `skipped-host` when
  nothing else can run.
- **Everything runs locally.** Check that the host names in `--hosts` match
  `hosts.toml`, then run `lane host check NAME`.
