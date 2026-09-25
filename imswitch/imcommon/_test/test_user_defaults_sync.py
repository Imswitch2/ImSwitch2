"""Keeping the user folder's copies of the shipped defaults up to date.

ImSwitch copied the default setups and scripts into ~/ImSwitchConfig once
and never touched them again, so a user who installed an update kept the
old copies: a reviewer's hamamatsu_mock_scan_setup.json lacked the camera
trigger line the scanning tutorials need, and the tutorials failed. Now an
untouched copy of an older shipped version is updated, an edited copy is
kept, and an untouched copy of a script ImSwitch no longer ships is moved
to the trash.
"""

import json
from pathlib import Path

import pytest

import imswitch
from imswitch.imcommon.model import dirtools

OLD = b'{"camera": {"digitalLine": null}}\n'
NEW = b'{"camera": {"digitalLine": "Dev1/port0/line0"}}\n'
EDITED = b'{"camera": {"digitalLine": "my own line"}}\n'


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def roots(tmp_path):
    return tmp_path / 'user_defaults', tmp_path / 'ImSwitchConfig'


class _Trash:
    def __init__(self, fail=False):
        self.paths, self.fail = [], fail

    def __call__(self, path):
        if self.fail:
            raise OSError('no trash here')
        self.paths.append(Path(path))
        Path(path).unlink()


def test_a_missing_default_is_copied(roots):
    source, user = roots
    _write(source / 'imcontrol_setups' / 'mock.json', NEW)

    report = dirtools.syncUserDefaults(source, user, {}, trash=_Trash())

    assert (user / 'imcontrol_setups' / 'mock.json').read_bytes() == NEW
    assert report.copied == ['imcontrol_setups/mock.json']


def test_an_untouched_older_version_is_updated(roots):
    source, user = roots
    _write(source / 'imcontrol_setups' / 'mock.json', NEW)
    _write(user / 'imcontrol_setups' / 'mock.json', OLD)
    history = {'imcontrol_setups/mock.json': {dirtools.defaultContentHash(OLD),
                                              dirtools.defaultContentHash(NEW)}}

    report = dirtools.syncUserDefaults(source, user, history, trash=_Trash())

    assert (user / 'imcontrol_setups' / 'mock.json').read_bytes() == NEW
    assert report.updated == ['imcontrol_setups/mock.json']


def test_an_edited_copy_is_kept_and_reported(roots):
    source, user = roots
    _write(source / 'scripts' / 'a.py', NEW)
    _write(user / 'scripts' / 'a.py', EDITED)
    history = {'scripts/a.py': {dirtools.defaultContentHash(OLD),
                                dirtools.defaultContentHash(NEW)}}

    report = dirtools.syncUserDefaults(source, user, history, trash=_Trash())

    assert (user / 'scripts' / 'a.py').read_bytes() == EDITED
    assert report.keptEdited == ['scripts/a.py']
    assert not report.updated


def test_line_endings_do_not_count_as_an_edit(roots):
    # A Windows checkout (or editor) may hold the same file with CRLF.
    source, user = roots
    _write(source / 'scripts' / 'a.py', NEW)
    _write(user / 'scripts' / 'a.py', NEW.replace(b'\n', b'\r\n'))
    _write(source / 'scripts' / 'b.py', NEW)
    _write(user / 'scripts' / 'b.py', OLD.replace(b'\n', b'\r\n'))
    history = {'scripts/a.py': {dirtools.defaultContentHash(NEW)},
               'scripts/b.py': {dirtools.defaultContentHash(OLD)}}

    report = dirtools.syncUserDefaults(source, user, history, trash=_Trash())

    assert report.updated == ['scripts/b.py']       # old version, CRLF: updated
    assert not report.keptEdited                    # current version, CRLF: left


def test_an_untouched_script_no_longer_shipped_goes_to_the_trash(roots):
    source, user = roots
    _write(source / 'scripts' / 'workflows' / 'wfs' / 'z.py', NEW)
    old = _write(user / 'scripts' / 'wfs' / 'z.py', NEW)      # moved in this version
    history = {'scripts/wfs/z.py': {dirtools.defaultContentHash(NEW)},
               'scripts/workflows/wfs/z.py': {dirtools.defaultContentHash(NEW)}}
    trash = _Trash()

    report = dirtools.syncUserDefaults(source, user, history, trash=trash)

    assert trash.paths == [old]
    assert report.removed == ['scripts/wfs/z.py']
    assert not (user / 'scripts' / 'wfs').exists()          # left empty: removed
    assert (user / 'scripts' / 'workflows' / 'wfs' / 'z.py').is_file()


def test_an_edited_script_no_longer_shipped_is_kept(roots):
    source, user = roots
    source.mkdir(parents=True)
    _write(user / 'scripts' / 'example_record.py', EDITED)
    history = {'scripts/example_record.py': {dirtools.defaultContentHash(OLD)}}
    trash = _Trash()

    report = dirtools.syncUserDefaults(source, user, history, trash=trash)

    assert trash.paths == [] and not report.removed
    assert (user / 'scripts' / 'example_record.py').read_bytes() == EDITED


def test_setup_files_are_never_deleted(roots):
    # A setup that is no longer shipped may be the one a rig's options name.
    source, user = roots
    source.mkdir(parents=True)
    _write(user / 'imcontrol_setups' / 'retired_example.json', OLD)
    history = {'imcontrol_setups/retired_example.json': {dirtools.defaultContentHash(OLD)}}

    report = dirtools.syncUserDefaults(source, user, history, trash=_Trash())

    assert (user / 'imcontrol_setups' / 'retired_example.json').is_file()
    assert not report.removed


def test_a_failed_trash_is_reported_not_raised(roots):
    source, user = roots
    source.mkdir(parents=True)
    _write(user / 'scripts' / 'gone.py', OLD)
    history = {'scripts/gone.py': {dirtools.defaultContentHash(OLD)}}

    report = dirtools.syncUserDefaults(source, user, history, trash=_Trash(fail=True))

    assert (user / 'scripts' / 'gone.py').is_file()
    assert report.failed and 'no trash here' in report.failed[0]


def test_readmes_and_caches_are_not_copied(roots):
    source, user = roots
    _write(source / 'scripts' / 'README.txt', b'repo notes')
    _write(source / 'scripts' / '__pycache__' / 'a.cpython-312.pyc', b'\0')
    _write(source / 'scripts' / 'a.py', NEW)

    report = dirtools.syncUserDefaults(source, user, {}, trash=_Trash())

    assert report.copied == ['scripts/a.py']


def test_a_missing_history_file_means_copy_only(tmp_path):
    assert dirtools.loadDefaultsHistory(tmp_path / 'missing.json') == {}
    (tmp_path / 'broken.json').write_text('{not json')
    assert dirtools.loadDefaultsHistory(tmp_path / 'broken.json') == {}


def test_the_shipped_history_knows_every_current_default():
    """Fails when a default file changed but the history was not regenerated:
    run  python tools/update_user_defaults_history.py  and commit the result.
    Without it, the copies users have of the previous version would count as
    edited and never be updated."""
    source = Path(imswitch.__file__).parent / '_data' / 'user_defaults'
    history = dirtools.loadDefaultsHistory()
    assert history, 'imswitch/_data/user_defaults_history.json is missing or unreadable'
    stale = [
        path.relative_to(source).as_posix()
        for path in sorted(source.rglob('*'))
        if path.is_file()
        and dirtools.isShippedDefault(path.relative_to(source).as_posix())
        and dirtools.defaultFileHash(path)
        not in history.get(path.relative_to(source).as_posix(), ())
    ]
    assert not stale, ('run python tools/update_user_defaults_history.py; '
                       f'not in the history yet: {stale}')


def test_the_history_file_is_valid_json_with_a_files_map():
    path = Path(imswitch.__file__).parent / '_data' / 'user_defaults_history.json'
    files = json.loads(path.read_text(encoding='utf-8'))['files']
    assert all(isinstance(hashes, list) and hashes for hashes in files.values())
