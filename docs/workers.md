# Workers: OpenCode and Command Code on DeepSeek V4.1 Flash

`agent-lanes` does not ship a worker; it calls whatever CLI you put behind `LANE_OC_BIN` / `LANE_CC_BIN`.
This page describes the two wrappers we run behind it, so you can copy the design. Paths are examples.

## Roles

| Who | Does | Never does |
|---|---|---|
| Lead (Claude Code) | Plans, splits work by file, writes briefs, reads every diff, runs `accept` with its own tests and mutants, commits, merges | Trusts a worker's "tests pass" without rerunning them |
| `oc-worker` (OpenCode) | Default worker. Writes code and tests in its own worktree | Commit, merge, see credentials, touch other folders |
| `cc-worker` (Command Code) | Fallback worker, same interface and same sandbox design | Same |

Both workers run **DeepSeek V4.1 Flash**: OpenCode as `opencode-go/deepseek-v4.1-flash` (OpenCode Go plan),
Command Code as `deepseek/deepseek-v4.1-flash`. A cheap model is enough because nothing it claims is taken on
trust: `accept` reruns the tests and plants mutants, and the lead reads the diff first.

## The wrapper interface

```
oc-worker "question"                   # read-only: no edits, no shell
oc-worker --write [-m MODEL] "task"    # edits allowed, cwd must be its own worktree
cc-worker [--write] [-m MODEL] "task"  # same interface
```

`lane run` calls `<bin> --write "<brief + result contract>"` with the lane worktree as the working directory.
`lane review` / `lane ask` call the read-only form.

## The sandbox (both wrappers)

Each run starts inside a [bubblewrap](https://github.com/containers/bubblewrap) allowlist sandbox:

- **Only what is needed exists**: the OS, the agent's program files, toolchains (shell, python, dotnet) and the
  repo. Home folders, other projects, SSH keys, cloud logins and the lead's own config are simply not mounted.
- **Write scope**: `--write` is accepted only when the working directory is a lane worktree whose name matches
  the worker (`oc-worker-NN`, `cc-worker-NN`). Everything else is read-only.
- **No git**: the worktree's `.git` pointer target is hidden, so the worker cannot commit, rewrite history or
  read other branches. The lead commits after a PASS.
- **API key on a file descriptor**: the key file (mode 0600, outside the repo) is opened by the wrapper and
  handed to the agent on descriptor 3, read once and closed, so it is not in the environment or argv.
  OpenCode: a small plugin blanks it from every shell the agent spawns, and every agent shell runs in a nested
  user + pid namespace so it cannot see or ptrace the agent process. Command Code: a preload strips the key from
  every child process.
- **One state dir per run** (OpenCode): parallel runs sharing one SQLite state file fail with "database is
  locked", so each run gets its own data dir.
- **WSL note**: `/etc/resolv.conf` is a symlink into `/mnt/wsl/`; bind that one file or DNS fails inside the
  sandbox and the CLI reports it cannot reach its API.
- **Not covered**: the network is open (the agent needs its API), so a worker could send repo content out.
  Treat every worker as a third party and only put in the repo what you would show one.

Each wrapper has a `check.sh` that proves the boundary with plain shell probes (missing folders, unreadable key,
no git, write outside the worktree refused, key absent from agent shells). We rerun it after **any** change to
the wrapper: 24/24 for OpenCode, 31/31 for Command Code.

## A lane, end to end

```
lane new oc-worker-07 --from main --branch agent/oc-worker-07-parser
$EDITOR brief.md                          # CONTEXT / TASK / SAFETY / ACCEPTANCE
lane run oc-worker-07 brief.md            # waits; exits when the worker is done
cat ~/agent-lanes/worktrees/oc-worker-07/.lane/REPORT.md
git -C ~/agent-lanes/worktrees/oc-worker-07 diff <base>   # read BEFORE running anything
lane accept oc-worker-07 --test "python3 -m pytest -q" --mutants mutants.json
lane merge oc-worker-07 -m "add parser" --test "python3 -m pytest -q"
```

Brief template:

```
CONTEXT    - what the code is, paths, what NOT to touch (other lanes' files, config)
TASK       - exact behaviour, interfaces, file names
SAFETY     - hard rules: fail closed, no network calls in code, dry-run by default for anything destructive
ACCEPTANCE - numbers: test count, named cases that must exist, the commands the reviewer will rerun,
             and "the reviewer mutates your tests"
```

## Rules we keep

- **About five lanes at once.** Parallel lanes are split by file and the shared interface is written into both
  briefs.
- **Read the diff first.** `accept` runs worker code outside the sandbox.
- **Always mutate.** 3-6 mutants per lane: drop a safety check, flip a comparison, remove a required argument.
  A worker's "tested" claim has failed a mutant more than once.
- **Let the mutant run finish** before quoting counts in a commit message.
- **Wait with the blocking `lane run`**, not polling loops; read `REPORT.md` and `LANE_RESULT.json`, and open the
  raw log only when the result is missing or invalid.
- **Workers are honest about blockers**; their green tests are still only a claim. In the review that built this
  tool, worker code passed its own tests while it had an `accept` that said PASS with zero tests, mutants run on a
  failing baseline, and a `gc` that would have removed a worktree with a live worker inside
  ([how-this-was-built.md](how-this-was-built.md)).
- **Model choice is per wrapper default.** Lanes launch with no `-m`, so every lane gets DeepSeek V4.1 Flash
  unless you pick another model on purpose.
