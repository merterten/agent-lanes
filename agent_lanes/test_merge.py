"""Tests for tools/lanes/merge.py.

Every test builds a throwaway main repo plus real ``git worktree`` lanes under the
system temp directory.  The real lanes root is never touched and no network or
shell path handling is involved.

    python3 -m unittest discover -s tools/lanes -p 'test_merge*.py' -v
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import merge  # noqa: E402


class MergeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='lane-merge-')
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.main = self.tmp / 'main'
        self.main.mkdir()
        self.git(self.main, 'init', '-q', '-b', 'main')
        self.git(self.main, 'config', 'user.name', 'Lane Test')
        self.git(self.main, 'config', 'user.email', 'lane@example.test')
        self.git(self.main, 'config', 'commit.gpgsign', 'false')
        self.write(self.main / 'a.txt', 'alpha\n')
        self.git(self.main, 'add', '-A')
        self.git(self.main, 'commit', '-q', '-m', 'initial')
        self.initial = self.head(self.main)
        self.worktrees = self.tmp / 'worktrees'
        self.worktrees.mkdir()
        self.bases = {}

    # -- helpers ---------------------------------------------------------
    def git(self, repo, *args, check=True):
        result = subprocess.run(
            ['git', '-C', str(repo), *args], capture_output=True, text=True)
        if check and result.returncode != 0:
            raise AssertionError(
                f'git {" ".join(args)} failed in {repo}: {result.stderr}')
        return result

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def read(self, path):
        return (path).read_text(encoding='utf-8')

    def head(self, repo):
        return self.git(repo, 'rev-parse', 'HEAD').stdout.strip()

    def tree(self, repo):
        return self.git(repo, 'rev-parse', 'HEAD^{tree}').stdout.strip()

    def status(self, repo):
        return self.git(repo, 'status', '--porcelain').stdout

    def parents(self, repo, rev='HEAD'):
        return self.git(repo, 'rev-list', '--parents', '-n', '1', rev).stdout.split()[1:]

    def lane(self, name, files=None, commit=False):
        path = self.worktrees / name
        self.git(self.main, 'worktree', 'add', '-q', '-b', name, str(path), 'main')
        self.bases[name] = self.head(self.main)
        for rel, text in (files or {}).items():
            self.write(path / rel, text)
        if commit:
            self.git(path, 'add', '-A')
            self.git(path, 'commit', '-q', '-m', f'{name} work')
        return path

    def merge_lane(self, name, **kwargs):
        kwargs.setdefault('message', f'merge {name}')
        kwargs.setdefault('tests', ['true'])  # reviewer: merge now refuses an empty test list
        kwargs.setdefault('out_dir', self.tmp / 'logs')
        return merge.run_merge(
            self.main, self.worktrees / name, name, **kwargs)

    def log_files(self):
        logs = self.tmp / 'logs'
        return sorted(logs.glob('lane-merge-*.log')) if logs.exists() else []

    # -- overlaps --------------------------------------------------------
    def test_overlaps_none(self):
        self.lane('cc-worker-1', {'one.txt': '1\n'})
        self.lane('cc-worker-2', {'two.txt': '2\n'})
        self.assertEqual(
            merge.overlaps(self.main, 'cc-worker-1', ['cc-worker-2'],
                           self.worktrees, self.bases),
            {})

    def test_overlaps_shared_tracked_file(self):
        self.lane('oc-worker-1', {'a.txt': 'lane one\n'})
        self.lane('oc-worker-2', {'a.txt': 'lane two\n'})
        self.assertEqual(
            merge.overlaps(self.main, 'oc-worker-1', ['oc-worker-2'],
                           self.worktrees, self.bases),
            {'oc-worker-2': ['a.txt']})

    def test_overlaps_untracked_shared_file(self):
        self.lane('cc-worker-3', {'new.txt': 'from three\n'})
        self.lane('cc-worker-4', {'new.txt': 'from four\n'})
        self.assertEqual(
            merge.overlaps(self.main, 'cc-worker-3', ['cc-worker-4'],
                           self.worktrees, self.bases),
            {'cc-worker-4': ['new.txt']})

    def test_overlaps_excludes_lane_result_directories(self):
        self.lane('cc-worker-5', {
            '.lane/REPORT.md': 'five\n',
            '.lane/LANE_RESULT.json': '{}\n',
            '.lane.prev/old.md': 'old\n',
        })
        self.lane('cc-worker-6', {
            '.lane/REPORT.md': 'six\n',
            '.lane.prev/old.md': 'older\n',
        })
        self.assertEqual(
            merge.overlaps(self.main, 'cc-worker-5', ['cc-worker-6'],
                           self.worktrees, self.bases),
            {})

    def test_overlaps_ignores_ignored_untracked_files(self):
        self.write(self.main / '.gitignore', 'ignored.txt\n')
        self.git(self.main, 'add', '-A')
        self.git(self.main, 'commit', '-q', '-m', 'ignore rules')
        self.lane('cc-worker-7', {'ignored.txt': 'seven\n'})
        self.lane('cc-worker-8', {'ignored.txt': 'eight\n'})
        self.assertEqual(
            merge.overlaps(self.main, 'cc-worker-7', ['cc-worker-8'],
                           self.worktrees, self.bases),
            {})

    def test_overlaps_uses_base_for_committed_changes(self):
        self.lane('cc-worker-9', {'a.txt': 'nine\n'}, commit=True)
        self.lane('cc-worker-10', {'a.txt': 'ten\n'}, commit=True)
        self.assertEqual(
            merge.overlaps(self.main, 'cc-worker-9', ['cc-worker-10'],
                           self.worktrees, self.bases),
            {'cc-worker-10': ['a.txt']})

    def test_overlaps_returns_only_nonempty_lanes(self):
        self.lane('cc-worker-11', {'shared.txt': 'eleven\n'})
        self.lane('cc-worker-12', {'shared.txt': 'twelve\n'})
        self.lane('cc-worker-13', {'other.txt': 'thirteen\n'})
        self.assertEqual(
            merge.overlaps(self.main, 'cc-worker-11',
                           ['cc-worker-12', 'cc-worker-13'],
                           self.worktrees, self.bases),
            {'cc-worker-12': ['shared.txt']})

    def test_overlaps_sees_files_in_subdirectories(self):
        self.lane('cc-worker-14', {'src/deep/file.py': 'a\n'})
        self.lane('cc-worker-15', {'src/deep/file.py': 'b\n'})
        self.assertEqual(
            merge.overlaps(self.main, 'cc-worker-14', ['cc-worker-15'],
                           self.worktrees, self.bases),
            {'cc-worker-15': ['src/deep/file.py']})

    # -- refusals --------------------------------------------------------
    def test_refuses_lane_with_no_changes(self):
        self.lane('cc-worker-20')
        result = self.merge_lane('cc-worker-20')
        self.assertFalse(result['ok'])
        self.assertEqual(result['step'], 'accept')
        self.assertIn('no changes', result['detail'])

    def test_refuses_lane_with_only_lane_result_files(self):
        self.lane('cc-worker-21', {'.lane/REPORT.md': 'nothing real\n'})
        result = self.merge_lane('cc-worker-21')
        self.assertFalse(result['ok'])
        self.assertEqual(result['step'], 'accept')

    def test_refuses_overlap(self):
        self.lane('cc-worker-22', {'a.txt': 'lane a\n'})
        self.lane('cc-worker-23', {'a.txt': 'lane b\n'})
        result = self.merge_lane(
            'cc-worker-22', others={'cc-worker-23': ['a.txt']})
        self.assertFalse(result['ok'])
        self.assertEqual(result['step'], 'overlap')
        self.assertIn('cc-worker-23', result['detail'])
        self.assertIn('a.txt', result['detail'])

    def test_allow_overlap_proceeds(self):
        self.lane('cc-worker-24', {'a.txt': 'lane a\n'})
        self.lane('cc-worker-25', {'a.txt': 'lane b\n'})
        result = self.merge_lane(
            'cc-worker-24', others={'cc-worker-25': ['a.txt']},
            allow_overlap=True)
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.read(self.main / 'a.txt'), 'lane a\n')

    def test_refuses_dirty_main(self):
        self.lane('cc-worker-26', {'a.txt': 'lane\n'})
        self.write(self.main / 'a.txt', 'dirty main\n')
        result = self.merge_lane('cc-worker-26')
        self.assertFalse(result['ok'])
        self.assertEqual(result['step'], 'main-clean')
        self.assertIn('not clean', result['detail'])

    def test_refuses_untracked_file_on_main(self):
        self.lane('cc-worker-27', {'a.txt': 'lane\n'})
        self.write(self.main / 'scratch.txt', 'untracked\n')
        result = self.merge_lane('cc-worker-27')
        self.assertFalse(result['ok'])
        self.assertEqual(result['step'], 'main-clean')

    def test_refuses_main_not_on_main_branch(self):
        self.lane('cc-worker-28', {'a.txt': 'lane\n'})
        self.git(self.main, 'checkout', '-q', '-b', 'other')
        result = self.merge_lane('cc-worker-28')
        self.assertFalse(result['ok'])
        self.assertEqual(result['step'], 'main-clean')
        self.assertIn('other', result['detail'])

    def test_refuses_empty_message(self):
        self.lane('cc-worker-29', {'a.txt': 'lane\n'})
        result = self.merge_lane('cc-worker-29', message='   \n\t')
        self.assertFalse(result['ok'])
        self.assertEqual(result['step'], 'commit')
        self.assertIn('message', result['detail'])

    def test_dry_run_also_refuses_empty_message(self):
        self.lane('cc-worker-30', {'a.txt': 'lane\n'})
        result = self.merge_lane('cc-worker-30', message='', dry_run=True)
        self.assertFalse(result['ok'])
        self.assertEqual(result['step'], 'commit')

    # -- commit and merge ------------------------------------------------
    def test_commit_excludes_lane_result_files(self):
        self.lane('oc-worker-31', {
            'a.txt': 'merged\n',
            '.lane/REPORT.md': 'report\n',
            '.lane/LANE_RESULT.json': '{"lane":"oc-worker-31"}\n',
            '.lane.prev/REPORT.md': 'old\n',
        })
        result = self.merge_lane('oc-worker-31')
        self.assertTrue(result['ok'], result)
        committed = self.git(
            self.main, 'show', '--name-only', '--format=', 'HEAD^2').stdout
        self.assertIn('a.txt', committed)
        self.assertNotIn('.lane/', committed)
        self.assertNotIn('.lane.prev/', committed)
        self.assertFalse((self.main / '.lane').exists())
        self.assertEqual(self.read(self.main / 'a.txt'), 'merged\n')

    def test_commit_excludes_pre_staged_lane_result_files(self):
        lane = self.lane('oc-worker-58', {
            'a.txt': 'real change\n',
            '.lane/REPORT.md': 'worker report\n',
        })
        self.git(lane, 'add', '.lane/REPORT.md')
        result = self.merge_lane('oc-worker-58')
        self.assertTrue(result['ok'], result)
        committed = self.git(
            self.main, 'show', '--name-only', '--format=', 'HEAD^2').stdout
        self.assertIn('a.txt', committed)
        self.assertNotIn('.lane/', committed)

    def test_commit_message_has_lane_trailer(self):
        self.lane('oc-worker-32', {'a.txt': 'trailer\n'})
        result = self.merge_lane('oc-worker-32', message='accept lane 32')
        self.assertTrue(result['ok'], result)
        body = self.git(self.main, 'log', '-1', '--format=%B', 'HEAD^2').stdout
        self.assertIn('accept lane 32', body)
        self.assertIn('Lane: oc-worker-32', body)

    def test_merge_commit_has_two_parents(self):
        self.lane('oc-worker-33', {'a.txt': 'two parents\n'})
        result = self.merge_lane('oc-worker-33')
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(self.parents(self.main)), 2)

    def test_success_returns_commit_and_merge_shas(self):
        lane = self.lane('oc-worker-34', {'a.txt': 'shas\n'})
        result = self.merge_lane('oc-worker-34')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['commit'], self.head(lane))
        self.assertEqual(result['merge'], self.head(self.main))
        self.assertNotEqual(result['commit'], result['merge'])

    def test_no_new_commit_when_lane_already_committed(self):
        lane = self.lane('oc-worker-35', {'a.txt': 'already committed\n'}, commit=True)
        lane_head = self.head(lane)
        result = self.merge_lane('oc-worker-35')
        self.assertTrue(result['ok'], result)
        self.assertIsNone(result['commit'])
        self.assertEqual(self.head(lane), lane_head)
        self.assertEqual(self.read(self.main / 'a.txt'), 'already committed\n')

    def test_conflict_aborts_merge_and_lists_files(self):
        self.lane('oc-worker-36', {'a.txt': 'lane side\n'})
        self.write(self.main / 'a.txt', 'main side\n')
        self.git(self.main, 'add', '-A')
        self.git(self.main, 'commit', '-q', '-m', 'advance main')
        main_before = self.head(self.main)

        result = self.merge_lane('oc-worker-36')
        self.assertFalse(result['ok'])
        self.assertEqual(result['step'], 'merge')
        self.assertIn('a.txt', result['detail'])
        self.assertEqual(self.head(self.main), main_before)
        self.assertEqual(self.status(self.main), '')
        merge_head = self.git(self.main, 'rev-parse', '-q', '--verify', 'MERGE_HEAD',
                              check=False)
        self.assertNotEqual(merge_head.returncode, 0)

    def test_conflict_leaves_main_tree_unchanged(self):
        self.lane('oc-worker-37', {'a.txt': 'lane side\n'})
        self.write(self.main / 'a.txt', 'main side\n')
        self.git(self.main, 'add', '-A')
        self.git(self.main, 'commit', '-q', '-m', 'advance main')
        tree_before = self.tree(self.main)
        self.merge_lane('oc-worker-37')
        self.assertEqual(self.tree(self.main), tree_before)

    # -- tests after merge ----------------------------------------------
    def test_successful_tests_reported(self):
        self.lane('oc-worker-40', {'a.txt': 'tested\n'})
        result = self.merge_lane(
            'oc-worker-40', tests=['true', 'python3 -c "print(42)"'])
        self.assertTrue(result['ok'], result)
        self.assertEqual([t['cmd'] for t in result['tests']],
                         ['true', 'python3 -c "print(42)"'])
        self.assertTrue(all(t['rc'] == 0 for t in result['tests']))
        self.assertTrue(all(t['seconds'] >= 0 for t in result['tests']))
        for entry in result['tests']:
            log = Path(entry['log'])
            self.assertTrue(log.is_file())
            self.assertIn(entry['cmd'], self.read(log))
        self.assertIn('42', self.read(Path(result['tests'][1]['log'])))

    def test_failing_test_reverts_merge(self):
        self.lane('oc-worker-41', {'a.txt': 'will be reverted\n'})
        tree_before = self.tree(self.main)
        head_before = self.head(self.main)

        result = self.merge_lane('oc-worker-41', tests=['false'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['step'], 'tests')
        self.assertIn('merge reverted', result['detail'])
        self.assertEqual(self.tree(self.main), tree_before)
        self.assertNotEqual(self.head(self.main), head_before)
        self.assertEqual(self.status(self.main), '')
        self.assertEqual(self.read(self.main / 'a.txt'), 'alpha\n')

        self.assertEqual(len(self.parents(self.main)), 1)
        self.assertEqual(len(self.parents(self.main, 'HEAD^')), 2)

    def test_second_test_failure_reverts_and_logs_both_commands(self):
        self.lane('oc-worker-42', {'a.txt': 'second fails\n'})
        tree_before = self.tree(self.main)
        result = self.merge_lane('oc-worker-42', tests=['true', 'false'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['step'], 'tests')
        self.assertEqual(self.tree(self.main), tree_before)
        logs = self.log_files()
        self.assertEqual(len(logs), 2)
        self.assertIn('$ true', self.read(logs[0]))
        self.assertIn('$ false', self.read(logs[1]))

    def test_failed_test_log_captures_output_and_rc(self):
        self.lane('oc-worker-43', {'a.txt': 'boom\n'})
        result = self.merge_lane(
            'oc-worker-43',
            tests=['python3 -c "import sys; print(\'boom\'); sys.exit(3)"'])
        self.assertFalse(result['ok'])
        logs = self.log_files()
        self.assertEqual(len(logs), 1)
        text = self.read(logs[0])
        self.assertIn('boom', text)
        self.assertIn('rc=3', text)

    def test_test_timeout_reverts_merge(self):
        self.lane('oc-worker-44', {'a.txt': 'timeout\n'})
        tree_before = self.tree(self.main)
        result = self.merge_lane(
            'oc-worker-44', tests=['sleep 5'], timeout=1)
        self.assertFalse(result['ok'])
        self.assertEqual(result['step'], 'tests')
        self.assertEqual(self.tree(self.main), tree_before)
        self.assertIn('TIMEOUT', self.read(self.log_files()[0]))

    # -- dry run ---------------------------------------------------------
    def test_dry_run_changes_nothing(self):
        lane = self.lane('oc-worker-50', {'a.txt': 'dry run\n'})
        main_head = self.head(self.main)
        lane_head = self.head(lane)
        main_status = self.status(self.main)
        lane_status = self.status(lane)

        result = self.merge_lane('oc-worker-50', dry_run=True)
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['dry_run'])
        self.assertEqual(self.head(self.main), main_head)
        self.assertEqual(self.head(lane), lane_head)
        self.assertEqual(self.status(self.main), main_status)
        self.assertEqual(self.status(lane), lane_status)
        self.assertEqual(self.read(self.main / 'a.txt'), 'alpha\n')

    def test_dry_run_refuses_overlap(self):
        self.lane('oc-worker-51', {'a.txt': 'lane a\n'})
        self.lane('oc-worker-52', {'a.txt': 'lane b\n'})
        main_head = self.head(self.main)
        result = self.merge_lane(
            'oc-worker-51', others={'oc-worker-52': ['a.txt']}, dry_run=True)
        self.assertFalse(result['ok'])
        self.assertEqual(result['step'], 'overlap')
        self.assertEqual(self.head(self.main), main_head)

    def test_dry_run_allows_overlap_with_flag(self):
        self.lane('oc-worker-53', {'a.txt': 'lane a\n'})
        self.lane('oc-worker-54', {'a.txt': 'lane b\n'})
        result = self.merge_lane(
            'oc-worker-53', others={'oc-worker-54': ['a.txt']},
            allow_overlap=True, dry_run=True)
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['dry_run'])

    def test_dry_run_does_not_create_out_dir(self):
        self.lane('oc-worker-55', {'a.txt': 'no logs\n'})
        out = self.tmp / 'never-created'
        result = self.merge_lane('oc-worker-55', dry_run=True, out_dir=out)
        self.assertTrue(result['ok'], result)
        self.assertFalse(out.exists())

    def test_cli_prints_json_for_dry_run(self):
        self.lane('oc-worker-56', {'a.txt': 'cli\n'})
        proc = subprocess.run(
            [sys.executable, str(HERE / 'merge.py'), str(self.main),
             str(self.worktrees / 'oc-worker-56'), 'oc-worker-56',
             '-m', 'cli message', '--dry-run', '--test', 'true'],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload['ok'])
        self.assertTrue(payload['dry_run'])
        self.assertEqual(self.status(self.main), '')


if __name__ == '__main__':
    unittest.main()


class ReviewerNoTestsTest(unittest.TestCase):
    """Added by the reviewer (Claude): merging without any test command is refused before any change."""

    def test_no_tests_refused_before_commit(self):
        import tempfile, subprocess as sp
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "main"
            repo.mkdir()
            for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@e.st"], ["config", "user.name", "T"]):
                sp.run(["git", "-C", str(repo), *args], check=True)
            (repo / "a.txt").write_text("a\n")
            sp.run(["git", "-C", str(repo), "add", "-A"], check=True)
            sp.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
            wt = Path(tmp) / "oc-worker-01"
            sp.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "agent/x", str(wt)], check=True)
            (wt / "b.txt").write_text("b\n")
            head = sp.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
            result = merge.run_merge(repo, wt, "oc-worker-01", "msg", [])
            self.assertFalse(result["ok"])
            self.assertEqual("tests", result["step"])
            self.assertEqual(head, sp.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout)
            self.assertIn("?? b.txt", sp.run(["git", "-C", str(wt), "status", "--porcelain"], capture_output=True, text=True).stdout)
