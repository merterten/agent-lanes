#!/usr/bin/env python3
"""Commit an accepted worker lane, merge it into main and rerun the test suite.

The worker never commits and its ``.lane/`` result files are untrusted data.  This
module treats them as excluded files only; nothing in a worker result is executed.

All git calls use argument lists.  Only reviewer-supplied test commands use a shell
and every one of them runs inside the main repository with an explicit timeout.

Pass ``dry_run=True`` (``--dry-run`` on the CLI) to inspect the plan without
changing anything.  Every destructive step is either a normal local commit/merge or
a history-preserving ``git revert`` of a merge whose tests failed on main.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

EXCLUDED_PREFIXES = ('.lane', '.lane.prev')


def _text(value):
    if value is None:
        return ''
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return str(value)


def _git(repo, args):
    """Run one git command in ``repo`` without a shell."""
    return subprocess.run(
        ['git', '-C', str(repo), *args],
        capture_output=True,
        text=True,
        env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
    )


def _is_excluded(path):
    path = path.replace('\\', '/')
    return any(path == prefix or path.startswith(prefix + '/') for prefix in EXCLUDED_PREFIXES)


def _changed_files(worktree, base):
    """Tracked changes against ``base`` plus untracked files, minus worker result dirs."""
    diff = _git(worktree, ['diff', '--name-only', base, '--'])
    if diff.returncode != 0:
        raise RuntimeError(f'git diff failed in {worktree}: {diff.stderr.strip()}')
    files = {line for line in diff.stdout.splitlines() if line}

    other = _git(worktree, ['ls-files', '--others', '--exclude-standard'])
    if other.returncode != 0:
        raise RuntimeError(f'git ls-files failed in {worktree}: {other.stderr.strip()}')
    files.update(line for line in other.stdout.splitlines() if line)

    return sorted(path for path in files if not _is_excluded(path))


def overlaps(main_repo, lane, other_lanes, worktrees_dir, base_of):
    """Return ``{other_lane: [shared paths]}`` for each other lane that touches our files."""
    worktrees_dir = Path(worktrees_dir)
    base_of = base_of or {}

    def files_for(name):
        base = base_of.get(name) or 'main'
        return set(_changed_files(worktrees_dir / name, base))

    mine = files_for(lane)
    result = {}
    for other in other_lanes:
        if other == lane:
            continue
        shared = sorted(mine & files_for(other))
        if shared:
            result[other] = shared
    return result


def _status_without_lane(worktree):
    return _git(worktree, [
        'status', '--porcelain', '--', '.', ':!.lane', ':!.lane.prev',
    ])


def _commit_count_ahead(worktree):
    result = _git(worktree, ['rev-list', '--count', 'main..HEAD'])
    if result.returncode != 0:
        return 0
    try:
        return int(result.stdout.strip() or 0)
    except ValueError:
        return 0


def _branch(repo):
    result = _git(repo, ['rev-parse', '--abbrev-ref', 'HEAD'])
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _head(repo):
    result = _git(repo, ['rev-parse', 'HEAD'])
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _list_or_none(items):
    return ', '.join(sorted(items)) if items else '(none)'


def run_merge(main_repo, worktree, lane, message, tests, timeout=1800,
              dry_run=False, allow_overlap=False, others=None, out_dir=None):
    """Commit, merge and test one lane.

    Returns ``{"ok": True, "commit": ..., "merge": ..., "tests": [...]}`` or
    ``{"ok": False, "step": name, "detail": text}`` at the first failure.
    """
    main_repo = Path(main_repo)
    worktree = Path(worktree)
    tests = list(tests or [])
    others = others or {}
    trailer_message = (message or '').rstrip()

    # 1. accept (the reviewer already accepted): refuse an empty lane.
    status = _status_without_lane(worktree)
    if status.returncode != 0:
        return {'ok': False, 'step': 'accept',
                'detail': f'cannot read worktree status: {status.stderr.strip()}'}
    ahead = _commit_count_ahead(worktree)
    if not status.stdout.strip() and ahead == 0:
        return {'ok': False, 'step': 'accept',
                'detail': 'worktree has no changes and no commits ahead of main'}

    if not tests:
        return {'ok': False, 'step': 'tests',
                'detail': 'no reviewer test commands given: refusing to merge untested'}

    # 2. overlap with other open lanes.
    if others and not allow_overlap:
        shared = {name: files for name, files in others.items() if files}
        if shared:
            parts = [f'{name}: {_list_or_none(files)}' for name, files in sorted(shared.items())]
            return {'ok': False, 'step': 'overlap',
                    'detail': 'overlapping changed files with other lanes: ' + '; '.join(parts)}

    # 3. main must be clean and on main.
    main_branch = _branch(main_repo)
    if main_branch != 'main':
        return {'ok': False, 'step': 'main-clean',
                'detail': f'main repo is on branch {main_branch!r}, not "main"'}
    main_status = _git(main_repo, ['status', '--porcelain'])
    if main_status.returncode != 0:
        return {'ok': False, 'step': 'main-clean',
                'detail': f'cannot read main status: {main_status.stderr.strip()}'}
    if main_status.stdout.strip():
        return {'ok': False, 'step': 'main-clean',
                'detail': 'main repo is not clean:\n' + main_status.stdout.rstrip()}

    wt_branch = _branch(worktree)

    if dry_run:
        if not trailer_message:
            return {'ok': False, 'step': 'commit',
                    'detail': 'commit message must not be empty'}
        print(f'[dry-run] would commit changes in {worktree} on branch {wt_branch}',
              file=sys.stderr)
        print(f'[dry-run] commit message: {trailer_message}', file=sys.stderr)
        print(f'[dry-run] would append trailer: Lane: {lane}', file=sys.stderr)
        print(f'[dry-run] would merge branch {wt_branch} into main with --no-ff',
              file=sys.stderr)
        print(f'[dry-run] would run {len(tests)} test command(s) in {main_repo}',
              file=sys.stderr)
        return {'ok': True, 'dry_run': True, 'commit': None, 'merge': None, 'tests': [],
                'planned': {'worktree': str(worktree), 'branch': wt_branch,
                            'message': trailer_message, 'tests': tests}}

    # 4. commit in the worktree, never adding .lane/ or .lane.prev/.
    if not trailer_message:
        return {'ok': False, 'step': 'commit',
                'detail': 'commit message must not be empty'}
    full_message = trailer_message + f'\n\nLane: {lane}'

    add = _git(worktree, ['add', '-A', '--', '.', ':!.lane', ':!.lane.prev'])
    if add.returncode != 0:
        return {'ok': False, 'step': 'commit',
                'detail': f'git add failed: {add.stderr.strip()}'}

    staged = _git(worktree, ['diff', '--cached', '--name-only'])
    if staged.returncode != 0:
        return {'ok': False, 'step': 'commit',
                'detail': f'cannot inspect staged changes: {staged.stderr.strip()}'}

    staged_names = [line for line in staged.stdout.splitlines() if line]
    excluded_dirs = sorted({
        path.split('/', 1)[0] for path in staged_names if _is_excluded(path)
    })
    if excluded_dirs:
        unstaged = _git(worktree, [
            'restore', '--staged', '--', *excluded_dirs,
        ])
        if unstaged.returncode != 0:
            return {'ok': False, 'step': 'commit',
                    'detail': f'cannot unstage worker result files: {unstaged.stderr.strip()}'}
        staged = _git(worktree, ['diff', '--cached', '--name-only'])
        if staged.returncode != 0:
            return {'ok': False, 'step': 'commit',
                    'detail': f'cannot inspect staged changes: {staged.stderr.strip()}'}

    commit_sha = None
    if staged.stdout.strip():
        commit = _git(worktree, ['commit', '-m', full_message])
        if commit.returncode != 0:
            return {'ok': False, 'step': 'commit',
                    'detail': f'git commit failed: {commit.stderr.strip()}'}
        commit_sha = _head(worktree)

    # 5. merge the worktree branch into main.
    if not wt_branch or wt_branch == 'HEAD':
        return {'ok': False, 'step': 'merge',
                'detail': 'worktree HEAD is detached; cannot merge a branch'}

    merge = _git(main_repo, ['merge', '--no-ff', '--no-edit', wt_branch])
    if merge.returncode != 0:
        in_progress = _git(main_repo, ['rev-parse', '-q', '--verify', 'MERGE_HEAD'])
        if in_progress.returncode == 0:
            conflicts = _git(main_repo, ['diff', '--name-only', '--diff-filter=U'])
            names = [line for line in conflicts.stdout.splitlines() if line]
            _git(main_repo, ['merge', '--abort'])
            return {'ok': False, 'step': 'merge',
                    'detail': 'merge conflict in: ' + _list_or_none(names)}
        return {'ok': False, 'step': 'merge',
                'detail': f'git merge failed: {merge.stderr.strip()}'}

    merge_sha = _head(main_repo)

    # 6. rerun the reviewer's tests on main; revert the merge if any of them fails.
    if out_dir is None:
        out_dir = main_repo / '.git'
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for index, command in enumerate(tests, 1):
        log_path = out_dir / f'lane-merge-{lane}-{index}.log'
        started = time.monotonic()
        try:
            proc = subprocess.run(
                command, shell=True, cwd=str(main_repo),
                capture_output=True, text=True, timeout=timeout,
            )
            rc = proc.returncode
            output = _text(proc.stdout) + _text(proc.stderr)
        except subprocess.TimeoutExpired as exc:
            rc = -1
            output = (_text(exc.stdout) + _text(exc.stderr)
                      + f'\nTIMEOUT after {timeout}s\n')
        elapsed = time.monotonic() - started

        log_path.write_text(
            f'$ {command}\nrc={rc}\nseconds={elapsed:.3f}\n\n{output}',
            encoding='utf-8',
        )
        results.append({'cmd': command, 'rc': rc,
                        'seconds': round(elapsed, 3), 'log': str(log_path)})

        if rc != 0:
            revert = _git(main_repo, ['revert', '-m', '1', '--no-edit', 'HEAD'])
            if revert.returncode != 0:
                return {'ok': False, 'step': 'tests',
                        'detail': ('tests failed on main; merge reverted FAILED: '
                                   + revert.stderr.strip())}
            return {'ok': False, 'step': 'tests',
                    'detail': 'tests failed on main, merge reverted'}

    return {'ok': True, 'commit': commit_sha, 'merge': merge_sha, 'tests': results}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Commit, merge and test one accepted lane.')
    parser.add_argument('main_repo', type=Path)
    parser.add_argument('worktree', type=Path)
    parser.add_argument('lane')
    parser.add_argument('-m', '--message', required=True)
    parser.add_argument('--test', action='append', default=[], dest='tests',
                        metavar='CMD', help='reviewer test command (repeatable)')
    parser.add_argument('--out', type=Path, default=None,
                        help='directory for test logs')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--allow-overlap', action='store_true')
    parser.add_argument('--timeout', type=int, default=1800)
    args = parser.parse_args(argv)

    result = run_merge(
        args.main_repo, args.worktree, args.lane, args.message, args.tests,
        timeout=args.timeout, dry_run=args.dry_run,
        allow_overlap=args.allow_overlap, out_dir=args.out,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get('ok') else 1


if __name__ == '__main__':
    sys.exit(main())
