# CLI reference

Part of [the tool](../README.md#the-tool). The loop:

```
lane new oc-worker-01 --from main            # isolated worktree + branch, recorded in a ledger
lane run oc-worker-01 brief.txt              # agent works; must write .lane/LANE_RESULT.json + REPORT.md
lane review oc-worker-01                     # optional read-only AI review with a fixed checklist
lane mutants oc-worker-01                    # suggest mutants for the lines the agent changed
lane accept oc-worker-01 --test "python3 -m pytest -q" --mutants ~/agent-lanes/lanes/oc-worker-01/mutants.json
lane merge oc-worker-01 -m "add parser" --test "python3 -m pytest -q"
lane queue work.json --hosts local,gpu-box,laptop --max 3   # spread a queue over machines
lane gc                                      # dry run: which finished worktrees can go
lane hosts                                   # configured hosts, reachability, used/free capacity
lane host check gpu-box                      # ssh, rsync, worker, root, systemd
lane host setup gpu-box                      # install remote_runner.sh
lane stop oc-worker-01                       # stop a lane running on a remote host
```

## Commands

| Command | What it does |
|---|---|
| `new` | Creates a worktree and branch from a ref and records the base commit. |
| `run` | Sends the brief plus a **result contract** to the agent CLI, waits, and validates the result: schema, lane name, and whether `files_changed` matches the real diff. Can run on another machine over ssh (`--host NAME`) and falls back to local with `--host-or-local` if it's unreachable. |
| `accept` | Reruns **your** test commands (never commands taken from the agent's file), parses pytest / unittest / dotnet / node counts, flags claim mismatches, and runs mutants in place with byte-identical restore and crash recovery. Writes `ACCEPT_REPORT.md` with PASS/FAIL. |
| `mutants` | Proposes mutants only on changed lines: Python via `ast` (each mutant must still parse), plus text rules for C#, JS/TS and shell. |
| `queue` | Runs many lanes from a JSON list with a concurrency cap. `--hosts local,gpu-box,laptop` spreads them using each host's `max_lanes`; entries may pin a `host` or set `fallback_local`. It stops starting new ones when an agent log shows quota or rate-limit errors, or when you create a `STOP` file. |
| `review` / `ask` | Runs the agent CLI in **read-only** mode for a checklist review of a diff, or for questions about a codebase. |
| `merge` | Requires a passing `accept` result, checks file overlap with other open lanes, commits, merges with `--no-ff`, reruns the tests on main, and reverts if they fail. |
| `gc` | Classifies worktrees as running / busy (a process is inside) / open / dirty / ignored-data / merged / unmerged. It only removes anything with `--apply`, and tags unmerged branches before removing them. |
| `hosts` | Lists configured hosts with an ssh reachability probe, the lanes the ledger says are running there, and used/free capacity. |
| `host check` | Checks ssh, the configured command prefix, rsync, the worker CLI, a writable root and `systemd --user`, printing PASS/FAIL with a fix hint. |
| `host setup` | Copies `agent_lanes/remote_runner.sh` to the host and makes it executable. Idempotent. |
| `stop` | Stops a lane running on a remote host. |

## Configuration

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

## The agent interface

The agent CLI is your own wrapper. It is called as `<bin> --write [-m MODEL] "<task>"` with the worktree as its working directory, and as `<bin> "<task>"` for read-only review/ask. Wrap any coding agent (OpenCode, Claude Code, Aider, …) behind that interface. **Run it in a sandbox:** the wrapper is where you keep the agent away from credentials and other folders. This repo deliberately doesn't ship one, because sandboxing depends on your machine.

Our own wrappers are described in [workers.md](workers.md) (part of [our setup](setup-guide.md), not the tool).
