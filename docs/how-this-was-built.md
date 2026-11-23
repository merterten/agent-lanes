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

## Round 3: bugs found in real use (23 November 2026)

Using the tool on real work turned up two bugs that 311 green tests did not cover:

| Bug | What happened | Cause |
|---|---|---|
| Local `lane run`, `review` and `ask` passed their own stdin to the worker | Started from an agent's shell, an OpenCode worker printed nothing and never returned, until an outer 10-minute timeout | `opencode run` reads stdin when it is not a terminal. Round 2 fixed exactly this for the ssh calls (AL1), but not for the local worker launch |
| `lane merge` failed with *"paths are ignored"* | Every merge in a repo that lists `.lane/` in `.gitignore` | `git add -A -- . ':!.lane'` names an ignored path, and git refuses that even as an exclusion |

Fixes: every worker, ssh and test launch now gets `stdin=/dev/null`; merge stages with a plain `git add -A -- .` and the existing
`restore --staged` step keeps `.lane/` out of the commit. The regression tests put an open pipe on the test's own stdin first,
so they cannot pass by accident in a CI where stdin is already closed. Reverting either fix fails a test.

## Round 4: a real job, and these docs (same day)

**Eight intros, four lanes.** Four OpenCode lanes wrote eight "what does X do?" intros (text plus an SVG diagram each) for our
Turkish setup guide. A contract test came first: required facts per tool, word limits, safe SVG (allowed tags and classes only,
every shape inside its box). Result: 8/8 files passed, 24/24 reviewer mutants killed. Reading every sentence still found one
factual error no test could see: an intro said long-running live jobs run inside herdr, the opposite of our rule.
This job is also where the `lane merge` bug above showed up.

**This README.** The split between the tool and our setup was planned by an OpenCode worker in read-only mode. Its first planning
run hung on the same stdin bug: it was started through the wrapper directly, not through `lane`, so the fix did not apply. With
stdin closed, the second run answered in about three minutes. The reviewer kept most of the plan and changed two things (where
the diagram lives, and a visible link to the Turkish guide instead of a hidden one).

The docs are tested now too: every public page is checked for private data, every relative link, image and heading anchor must
resolve, the four diagram files must match their generator, and the English and Turkish READMEs must have the same structure.
315 tests.

## Lessons, continued

6. **A bug fixed in one place is usually still somewhere else.** Round 2 closed stdin for ssh; the local launch kept the same bug
   for a day. After a fix, search for every other call of the same kind.
7. **Tests check shape, not meaning.** A contract test can prove every intro has its facts and a safe diagram; only reading proves
   a sentence is true.
8. **Use the tool on real work early.** Both Round 3 bugs were invisible to the test suite and obvious within an hour of real use.
