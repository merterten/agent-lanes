# How this was built: six AI agents, one reviewer

`agent-lanes` was built with the workflow it implements, in a single evening (22 November 2026).

## Setup

- **Reviewer / lead:** Claude Code. It wrote the briefs, reviewed every diff, ran the tests, wrote its own mutants, fixed what it found and merged.
- **Workers:** six DeepSeek coding agents (via OpenCode), each in its own sandboxed git worktree with no credentials and no git access, running in parallel.
- **Split by file, not by feature,** so no two agents ever edited the same file. Where modules had to talk, the interface (function name, arguments, return shape) was fixed in both briefs.

| Lane | Built | Agent's own tests |
|---|---|---|
| LN1 | `lane.py`: CLI, ledger, run/collect, gc | 34 |
| LN2 | `accept.py`: test reruns, claim checks, mutants | 33 |
| LN3 | `mutants.py`: mutant suggestions for changed lines | 30 |
| LN4 | `lanequeue.py`: parallel queue with quota brake | 31 |
| LN5 | `review.py` + `ask.py`: read-only reviewer | 29 |
| LN6 | `merge.py`: commit, merge, test, revert | 35 |

Each brief had four parts: context, task (exact interfaces), safety rules, and acceptance numbers. It ended with the result contract, and told the agent: *"the reviewer reruns your tests and mutates your code."*

## What the review found

Every lane came back saying *done, all tests pass*. That was true, and still not enough:

| Lane | Bug the agent's tests missed | Why it matters |
|---|---|---|
| LN2 | `accept` returned **PASS when no test commands were given** | An acceptance check that checks nothing must fail |
| LN2 | Mutants ran even when the baseline tests already failed, so **every mutant counted as "killed"** | A meaningless score that looks perfect |
| LN2 | Two reviewer mutants survived the agent's 33 tests: a "find" that matched **zero** times was accepted, and dotnet counts were only taken from the last project | The tests didn't pin the behaviour they claimed to |
| LN1 | `gc --apply` treated a worktree with no commits as "merged" and **would delete it while an agent was still working in it** | Data loss |
| LN1 | Waiting on a remote machine **looped forever** if it dropped off the network, and a failed `push` was ignored | Hangs, and lanes "running" that never started |
| LN6 | `merge` merged when **no tests** were given | Untested code on main |

Each fix came with a test, and the reviewer then ran its own mutants against the finished code (deliberately breaking guards, comparisons and flags):

| Module | Reviewer mutants killed |
|---|---|
| `accept.py` | 11 / 11 |
| `lane.py` | 15 / 15 |
| CLI wiring | 5 / 6 (the survivor is a duplicate guard: `merge.py` refuses the same case on its own) |

Final state: 219 tests, all passing.

## Lessons

1. **Agents are honest about blockers but optimistic about tests.** "All green" usually means the tests check what the agent thought of, not what can go wrong.
2. **Mutate every time.** One survivor tells you exactly which behaviour is unpinned.
3. **Split work by file and fix the interfaces up front.** Six agents in parallel produced zero merge conflicts.
4. **Make the agent report in a schema**, then verify every number in it. Reading a 30-line result instead of a transcript is what makes reviewing six lanes at once possible.
5. **Destructive commands default to a dry run.** Most of the dangerous bugs above were in the "clean up" and "merge" paths.

## Round 2: multi-machine lanes (same workflow)

Two more DeepSeek lanes added built-in SSH hosts (AL1) and a queue that spreads lanes over several machines (AL2).
Both came back green; review still found three real bugs:

| Lane | Bug the agent's tests missed | Why it matters |
|---|---|---|
| AL1 | Push to the remote host did not mirror deletions | A file deleted locally came back on the next pull |
| AL1 | ssh/rsync calls without input inherited the caller's stdin | Run from a pipe or a queue, every probe hung until timeout ("flaky" tests), and real ssh can swallow input meant for something else |
| AL2 | No test pinned the allocator's own per-host capacity check | Only a second guard in the queue protected it |

Each fix came with a test; reviewer mutants on the new code: 14 / 14 killed.
