# agent-lanes

**Run AI coding agents in parallel lanes and verify their work before it merges.**

You hand coding tasks to several cheap AI agents at once. Each one works in its own git worktree (a *lane*). When an agent says *"done, 33 tests pass"*, `agent-lanes` doesn't take its word for it:

1. it **reruns your tests** and compares the measured pass/fail counts with what the agent claimed,
2. it plants **deliberate bugs (mutants)** in the agent's code to prove its tests would catch them,
3. it only **merges** work that survives, and reverts the merge if the tests fail on `main`.

It was built this way too: six DeepSeek agents wrote the modules in parallel, and Claude Code reviewed them. The review found real bugs that the agents' own green tests had missed. See [docs/how-this-was-built.md](docs/how-this-was-built.md).

Plain Python 3.12 standard library: no dependencies, 270 tests.

## The loop

```
lane new oc-worker-01 --from main            # isolated worktree + branch, recorded in a ledger
lane run oc-worker-01 brief.txt              # agent works; must write .lane/LANE_RESULT.json + REPORT.md
lane review oc-worker-01                     # optional read-only AI review with a fixed checklist
lane mutants oc-worker-01                    # suggest mutants for the lines the agent changed
lane accept oc-worker-01 --test "python3 -m pytest -q" --mutants ~/agent-lanes/lanes/oc-worker-01/mutants.json
lane merge oc-worker-01 -m "add parser" --test "python3 -m pytest -q"
lane gc                                      # dry run: which finished worktrees can go
lane hosts                                   # configured hosts, reachability, running lanes
lane host check gpu-box                      # ssh, rsync, worker, root, systemd
lane host setup gpu-box                      # install remote_runner.sh
lane stop oc-worker-01                       # stop a lane running on a remote host
```

| Command | What it does |
|---|---|
| `new` | Creates a worktree and branch from a ref and records the base commit. |
| `run` | Sends the brief plus a **result contract** to the agent CLI, waits, and validates the result: schema, lane name, and whether `files_changed` matches the real diff. Can run on another machine over ssh (`--host NAME`) and falls back to local with `--host-or-local` if it's unreachable. |
| `accept` | Reruns **your** test commands (never commands taken from the agent's file), parses pytest / unittest / dotnet / node counts, flags claim mismatches, and runs mutants in place with byte-identical restore and crash recovery. Writes `ACCEPT_REPORT.md` with PASS/FAIL. |
| `mutants` | Proposes mutants only on changed lines: Python via `ast` (each mutant must still parse), plus text rules for C#, JS/TS and shell. |
| `queue` | Runs many lanes from a JSON list with a concurrency cap. It stops starting new ones when an agent log shows quota or rate-limit errors, or when you create a `STOP` file. |
| `review` / `ask` | Runs the agent CLI in **read-only** mode for a checklist review of a diff, or for questions about a codebase. |
| `merge` | Requires a passing `accept` result, checks file overlap with other open lanes, commits, merges with `--no-ff`, reruns the tests on main, and reverts if they fail. |
| `gc` | Classifies worktrees as running / busy (a process is inside) / open / dirty / ignored-data / merged / unmerged. It only removes anything with `--apply`, and tags unmerged branches before removing them. |
| `hosts` | Lists configured hosts with an ssh reachability probe and the lanes the ledger says are running there. |
| `host check` | Checks ssh, the configured command prefix, rsync, the worker CLI, a writable root and `systemd --user`, printing PASS/FAIL with a fix hint. |
| `host setup` | Copies `agent_lanes/remote_runner.sh` to the host and makes it executable. Idempotent. |
| `stop` | Stops a lane running on a remote host. |

## The result contract

Every brief gets this appended (`agent_lanes/contract.txt`). The agent must write:

```json
{"lane": "oc-worker-01", "status": "done",
 "summary": "Added CSV parser with header validation.",
 "files_changed": ["src/parser.py", "tests/test_parser.py"],
 "tests": [{"cmd": "python3 -m pytest -q", "passed": 12, "failed": 0}],
 "claims": [{"name": "rows/s", "value": 50000, "evidence": "bench.py"}],
 "blockers": []}
```

The reviewer reads 30 lines instead of a whole agent transcript. Every number in the file is then checked, not trusted.

## Setup

```
git clone https://github.com/merterten/agent-lanes && cd agent-lanes
python3 -m unittest discover -s agent_lanes          # 270 tests, temp dirs only
ln -s "$PWD/agent_lanes/lane.py" ~/.local/bin/lane
```

Configuration is all environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `LANE_ROOT` | `~/agent-lanes` | Base folder |
| `LANE_MAIN_REPO` | `$LANE_ROOT/repo` | The repo whose `main` the lanes branch from |
| `LANE_WORKTREES` | `$LANE_ROOT/worktrees` | Where lane worktrees go |
| `LANE_STATE_DIR` | `$LANE_ROOT/lanes` | Ledger, logs and reports (outside every worktree, so an agent can't forge them) |
| `LANE_OC_BIN` / `LANE_CC_BIN` | `oc-worker` / `cc-worker` | Agent CLI for `oc-worker-N` / `cc-worker-N` lanes |
| `LANE_HOSTS` | `~/.config/agent-lanes/hosts.toml` | Remote host definitions |
| `LANE_SSH` / `LANE_RSYNC` | `ssh` / `rsync` | Binaries used for remote hosts (override for tests) |

The agent CLI is your own wrapper. It is called as `<bin> --write [-m MODEL] "<task>"` with the worktree as its working directory, and as `<bin> "<task>"` for read-only review/ask. Wrap any coding agent (OpenCode, Claude Code, Aider, …) behind that interface. **Run it in a sandbox:** the wrapper is where you keep the agent away from credentials and other folders. This repo deliberately doesn't ship one, because sandboxing depends on your machine.


## Remote hosts

Define each machine once in `~/.config/agent-lanes/hosts.toml`:

```toml
[hosts.gpu-box]
ssh = "user@203.0.113.10"          # or an ~/.ssh/config alias
root = "~/agent-lanes/worktrees"   # mirrors LANE_WORKTREES
prefix = ["wsl.exe", "-d", "Ubuntu", "--"]   # optional command prefix
worker = "oc-worker"               # remote agent CLI (default: same as local)
nice = 19
max_lanes = 2
rsync_path = "wsl.exe -d Ubuntu -- rsync"     # optional, for WSL
```

`lane host setup NAME` installs the runner, then `lane run LANE --host NAME` pushes the
lane with rsync, runs the worker detached over ssh, polls for completion and pulls the
result back. Pushes exclude `.git/`, `.lane/`, build output and credential files; pulls
never delete local files (remote-side deletions are reported as warnings). Every ssh
call uses `BatchMode=yes`, `ConnectTimeout` and no agent forwarding.

## Safety rules the tool enforces

- Nothing in an agent's result file is ever executed. Test commands come from the reviewer.
- Stale results are moved aside before each run, so an old `LANE_RESULT.json` can never be accepted.
- `accept` fails when no test commands are given, and doesn't run mutants when the tests already fail (otherwise every mutant would look "killed").
- `merge` refuses unaccepted lanes, untested merges, a dirty `main`, and overlaps with other open lanes; it uses `revert`, never `reset --hard`.
- `gc` is a dry run by default and never removes a worktree with a running process inside, uncommitted changes, ignored data, or an open ledger status.

## License

MIT
