"""The agent guides (``AGENTS.md`` files) name only things that exist.

The first AGENTS.md was deleted because it drifted from the project. These
guides are dense lists of paths and names, so they are checked mechanically:

* every backticked repository path exists (a glob matches at least one file;
  a path relative to the package a table row names must end some path of the
  checkout);
* every identifier in a backticked code name occurs somewhere in the
  repository's code or config (``.py``, ``.toml``, ``.cfg``, ``.yml``,
  ``.yaml``), so a rename that leaves a guide behind fails here;
* repository paths inside backticked commands exist;
* every red-zone glob matches at least one file, so a move cannot silently
  empty the red zone.

Registries (processors, reconstructors, managers, widgets) are deliberately
not copied into the guides; the guides name the dict that holds them, and
that name is checked like any other.
"""
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.nohardware

REPO = Path(__file__).resolve().parents[3]

GUIDES = [
    REPO / 'AGENTS.md',
    REPO / 'imswitch' / 'imcontrol' / 'AGENTS.md',
    REPO / 'imswitch' / 'improcess' / 'AGENTS.md',
    REPO / 'imswitch' / 'imscripting' / 'AGENTS.md',
]

#: Words the guides quote that are not identifiers in the code: UI labels,
#: environment variables, changelog headings. Keep this short.
ALLOWED_NAMES = {
    'HOME', 'PYTHONPATH', 'PWD', 'Other', 'Unreleased', 'imswitch2',
}

_SPAN = re.compile(r'`([^`\n]+)`')
_PATH_PREFIXES = ('imswitch/', 'tools/', 'docs/', 'examples/', '.github/', 'scripts/')
_FILE_SUFFIXES = ('.py', '.rst', '.md', '.json', '.toml', '.cfg', '.yml', '.yaml', '.txt')
_IDENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
_COMMIT = re.compile(r'^[0-9a-f]{7,40}$')
_SKIPPED_DIRS = {'.git', '__pycache__', 'node_modules', '_build'}


def _spans(text):
    # Fenced blocks hold commands or the red-zone list, checked separately.
    text = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
    return _SPAN.findall(text)


def _isPath(token):
    token = token.split('[')[0]
    return (token.startswith(_PATH_PREFIXES) or token.endswith(_FILE_SUFFIXES)
            or ('/' in token and token.endswith('/')))


def _walk(pattern):
    for path in REPO.rglob(pattern):
        if not _SKIPPED_DIRS.intersection(path.relative_to(REPO).parts):
            yield path


_repoPaths = None


def _allRepoPaths():
    """Every file and directory of the checkout, as posix paths from the root."""
    global _repoPaths
    if _repoPaths is None:
        _repoPaths = {path.relative_to(REPO).as_posix() for path in _walk('*')}
    return _repoPaths


def _pathExists(token, guide):
    token = token.split('[')[0].rstrip('/')
    if '/' not in token and any(ch in token for ch in '*?'):
        return any(_walk(token))  # a file-name pattern such as *mock_scan*.json
    for base in (REPO, guide.parent):
        if any(ch in token for ch in '*?'):
            if any(base.glob(token)):
                return True
        elif (base / token).exists():
            return True
    # Relative to a package the row names (``model/api.py`` in the imcommon
    # row): some file or directory of the checkout ends with it.
    return any(path == token or path.endswith('/' + token) for path in _allRepoPaths())


_identifiers = None


def _codeIdentifiers():
    global _identifiers
    if _identifiers is None:
        words = set()
        for pattern in ('*.py', '*.toml', '*.cfg', '*.yml', '*.yaml'):
            for path in _walk(pattern):
                try:
                    words.update(_IDENT.findall(path.read_text(encoding='utf-8', errors='ignore')))
                except OSError:
                    continue
        _identifiers = words
    return _identifiers


def _commandPaths(span):
    """Repository paths inside a command span such as ``python tools/x.py --write``."""
    return [token.rstrip(';,') for token in span.split()
            if token.startswith(_PATH_PREFIXES)]


def _problems(guide):
    problems = []
    for span in _spans(guide.read_text(encoding='utf-8')):
        span = span.strip()
        if (span in ALLOWED_NAMES or _COMMIT.match(span)
                or span.startswith(('-', '~')) or '<' in span or '$' in span):
            continue  # flags, user-folder paths, templates such as <Key>Widget.py
        if ' ' in span:
            for token in _commandPaths(span):
                if not _pathExists(token, guide):
                    problems.append(f'path in `{span}`: {token}')
            continue
        if _isPath(span):
            if not _pathExists(span, guide):
                problems.append(f'path: {span}')
            continue
        if ':' in span or '=' in span:
            continue  # setup-file snippets such as cameraListIndex: "mock"
        for name in _IDENT.findall(span):
            if name not in _codeIdentifiers() and name not in ALLOWED_NAMES:
                problems.append(f'name: {span} ({name})')
                break
    return problems


@pytest.mark.parametrize('guide', GUIDES, ids=lambda p: str(p.relative_to(REPO)))
def test_every_name_in_the_guide_exists(guide):
    assert guide.exists(), f'{guide} is missing'
    problems = _problems(guide)
    assert not problems, (
        f'{guide.relative_to(REPO)} names things that do not exist; fix the guide '
        f'(or, for a word that is not code, add it to ALLOWED_NAMES):\n  '
        + '\n  '.join(problems)
    )


def _redZoneGlobs():
    text = (REPO / 'AGENTS.md').read_text(encoding='utf-8')
    block = re.search(r'<!-- redzone-globs:start -->\s*```\n(.*?)```\s*<!-- redzone-globs:end -->',
                      text, re.DOTALL)
    assert block, 'AGENTS.md lost its redzone-globs block'
    return [line.strip() for line in block.group(1).splitlines() if line.strip()]


def test_the_red_zone_is_not_empty():
    assert len(_redZoneGlobs()) >= 10


@pytest.mark.parametrize('pattern', _redZoneGlobs())
def test_each_red_zone_glob_matches_a_file(pattern):
    assert any(REPO.glob(pattern)), (
        f'red-zone glob {pattern!r} matches nothing; a file moved -- update AGENTS.md')


def test_claude_md_only_points_to_agents_md():
    text = (REPO / 'CLAUDE.md').read_text(encoding='utf-8')
    assert 'AGENTS.md' in text
    assert len(text.splitlines()) <= 10, 'CLAUDE.md is a pointer; the rules live in AGENTS.md'
