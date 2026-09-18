"""Single check entry: file gates for the pre-commit hook, file gates plus the
pytest stage for full runs (CI). Scan-only only while no tests/ directory
exists; local full runs prefer the repo .venv when present."""
from pathlib import Path
import argparse
import ast
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / 'tests'

try:
    from scripts.repo_guard import inspect
except ImportError:  # executed as a script: scripts/ is importable, root is not
    from repo_guard import inspect

SYNTAX_SUFFIX = '.py'
WHITESPACE_SUFFIXES = {'.py', '.yml', '.yaml'}
FINAL_NEWLINE_SUFFIXES = {'.py', '.md', '.yml', '.yaml'}
TEST_REQUIRED_NOTE = ('no tests yet: test stage skipped; add tests/ before the '
                      'full-check mode is enforced by CI')


def tracked_paths(staged=False):
    args = ['git', 'diff', '--cached', '--name-only', '--diff-filter=ACMR', '-z'] if staged else ['git', 'ls-files', '-z']
    out = subprocess.check_output(args, cwd=ROOT).decode('utf-8')
    return [p for p in out.split('\0') if p]


def file_bytes(path, staged=False):
    if staged:
        return subprocess.check_output(['git', 'show', ':' + path], cwd=ROOT)
    return (ROOT / path).read_bytes()


def syntax_issues(path, data):
    if Path(path).suffix != SYNTAX_SUFFIX:
        return []
    try:
        ast.parse(data, filename=path)
    except SyntaxError as exc:
        return [f'syntax error line {exc.lineno}: {exc.msg}']
    except ValueError as exc:
        return [f'syntax error: {exc}']
    return []


def format_issues(path, data):
    suffix = Path(path).suffix.lower()
    if suffix not in WHITESPACE_SUFFIXES and suffix not in FINAL_NEWLINE_SUFFIXES:
        return []
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return []  # already reported by inspect as non-UTF8
    issues = []
    if suffix in WHITESPACE_SUFFIXES:
        for lineno, line in enumerate(text.splitlines(), 1):
            if line != line.rstrip(' \t'):
                issues.append(f'trailing whitespace line {lineno}')
    if suffix in FINAL_NEWLINE_SUFFIXES and data and not data.endswith(b'\n'):
        issues.append('missing final newline')
    return issues


def test_stage_issues():
    """Run pytest when a tests/ directory exists (full-check mode).

    Scan-only before that; once tests exist the suite must pass on every
    full run (pre-commit without --staged, CI). --staged runs stay fast.
    """
    if not TESTS_DIR.is_dir():
        return None  # no tests yet: scan-only mode
    suffix = '.exe' if sys.platform == 'win32' else ''
    venv_py = ROOT / '.venv' / ('Scripts' if sys.platform == 'win32' else 'bin') / ('python' + suffix)
    runner = [str(venv_py)] if venv_py.exists() else [sys.executable]
    proc = subprocess.run(
        runner + ['-m', 'pytest', '-q', '--no-header'],
        cwd=ROOT, capture_output=True, text=True, timeout=600,
    )
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
    if proc.returncode != 0:
        return ['test stage failed:\n' + '\n'.join(tail)]
    print('test stage: ' + (tail[-1] if tail else 'pytest ok'))
    return []


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--staged', action='store_true')
    args = parser.parse_args()
    print('== file checks ==')
    paths = tracked_paths(args.staged)
    if not paths:
        if args.staged:
            print('No staged files to check.')
            failures = []
        else:
            print('ERROR: no tracked files')
            failures = ['no tracked files']
    else:
        failures = []
        for path in paths:
            try:
                data = file_bytes(path, args.staged)
            except subprocess.CalledProcessError:
                failures.append(f'{path}: cannot read staged content')
                continue
            except OSError:
                failures.append(f'{path}: tracked file missing from worktree')
                continue
            for problem in inspect(path, data) + syntax_issues(path, data) + format_issues(path, data):
                failures.append(f'{path}: {problem}')
    print('\n'.join(failures) if failures else f'PASS: checked {len(paths)} files')
    test_failures = test_stage_issues()
    if test_failures:
        failures.extend(test_failures)
    if not args.staged and test_failures is None:
        print(TEST_REQUIRED_NOTE)
    raise SystemExit(1 if failures else 0)
