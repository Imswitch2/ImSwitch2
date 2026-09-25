"""The scripting tutorials shipped in ``_data/user_defaults/scripts/tutorial``.

Every tutorial names the mock setup it is written for in its header
(``Mock setup: <file>.json``). The header is checked here, and then the
tutorial is run on that setup, headless, through ImScripting's own executor
(:mod:`.tutorial_runner`) -- so "this works on the mock setup" is a tested
claim, and a change that breaks a tutorial breaks this test.
"""

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import imswitch

pytestmark = pytest.mark.nohardware

USER_DEFAULTS = Path(imswitch.__file__).parent / '_data' / 'user_defaults'
TUTORIALS = USER_DEFAULTS / 'scripts' / 'tutorial'
SETUPS = USER_DEFAULTS / 'imcontrol_setups'

# The file name may be followed by a note, e.g. "<- a new setup".
_MOCK_SETUP = re.compile(r'^\s*Mock setup:\s*(\S+\.json)(?:\s.*)?$', re.MULTILINE)
_IMPORTED_BY = re.compile(r'^\s*Imported by:\s*(\S+\.py)\s*$', re.MULTILINE)


def _docstring(path):
    return ast.get_docstring(ast.parse(path.read_text(encoding='utf-8'))) or ''


def _scripts():
    return sorted(TUTORIALS.rglob('*.py'))


def _tutorials():
    """(script, setup) for every script that is run on its own."""
    found = []
    for path in _scripts():
        doc = _docstring(path)
        if _IMPORTED_BY.search(doc):
            continue
        match = _MOCK_SETUP.search(doc)
        found.append((path, match.group(1) if match else None))
    return found


def _id(path):
    return str(path.relative_to(TUTORIALS))


def test_there_are_basic_and_scanning_tutorials():
    folders = {path.parent.name for path, _setup in _tutorials()}
    assert {'basic', 'scanning'} <= folders


@pytest.mark.parametrize('path', [p for p, _ in _tutorials()], ids=_id)
def test_header_says_what_it_teaches_and_what_it_needs(path):
    doc = _docstring(path)
    assert doc.startswith('Tutorial '), 'first line: "Tutorial <folder> <nn> -- <title>"'
    for section in ('You will learn', 'Setup', 'Mock setup:', 'It simulates:',
                    'Your own microscope:', 'Next:'):
        assert section in doc, f'header is missing "{section}"'
    setup = _MOCK_SETUP.search(doc).group(1)
    assert (SETUPS / setup).is_file(), f'{setup} is not a shipped setup'
    json.loads((SETUPS / setup).read_text(encoding='utf-8'))


@pytest.mark.parametrize('path', [p for p in _scripts()
                                  if _IMPORTED_BY.search(_docstring(p))], ids=_id)
def test_helper_is_imported_by_the_tutorial_it_names(path):
    importer = path.parent / _IMPORTED_BY.search(_docstring(path)).group(1)
    assert importer.is_file()
    assert f"importScript('{path.name}')" in importer.read_text(encoding='utf-8')


def test_the_index_lists_every_tutorial():
    index = (TUTORIALS / 'README.md').read_text(encoding='utf-8')
    for path, setup in _tutorials():
        assert f'`{path.name}`' in index, f'{_id(path)} is missing from README.md'
        assert f'`{setup}`' in index


@pytest.mark.skipif(os.name == 'nt', reason='the runner cannot isolate '
                    'Documents\\ImSwitchConfig from the real one on Windows')
@pytest.mark.parametrize('path, setup', _tutorials(),
                         ids=lambda v: _id(v) if isinstance(v, Path) else v)
def test_tutorial_runs_on_its_mock_setup(path, setup, tmp_path):
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', MPLBACKEND='Agg')
    try:
        completed = subprocess.run(
            [sys.executable, '-m', 'imswitch.imscripting._test.tutorial_runner',
             setup, str(path), '--timeout', '120', '--home', str(tmp_path)],
            capture_output=True, text=True, env=env, timeout=170,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(f'{_id(path)} did not finish:\n{(error.stdout or "")[-3000:]}')
    lines = completed.stdout.strip().splitlines()
    summary = json.loads(lines[-1]) if lines and lines[-1].startswith('{') else {}
    assert completed.returncode == 0 and summary.get('status') == 'succeeded', (
        f'{_id(path)} on {setup}: {summary or "no summary"}\n'
        f'--- stdout (tail) ---\n{completed.stdout[-3000:]}\n'
        f'--- stderr (tail) ---\n{completed.stderr[-3000:]}'
    )
