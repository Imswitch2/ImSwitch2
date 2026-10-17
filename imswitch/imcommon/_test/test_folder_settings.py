"""The two pieces every folder setting shares: the path check and the field.

ImControl's Recordings folder and ImProcess's Default folders both save a
folder the user typed or picked, so both use one rule for what counts as a
folder and one field to pick it with.
"""
import os

import pytest
from qtpy import QtWidgets

from imswitch.imcommon.model.dirtools import checkedFolderPath

_APP = None


@pytest.fixture(scope="module")
def qapp():
    # Held at module level: dropping the last QApplication reference deletes
    # every Python-owned QObject.
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


# --- checkedFolderPath -----------------------------------------------------------

def test_a_full_path_is_normalised_and_need_not_exist(tmp_path):
    folder = os.path.join(str(tmp_path), 'not', '..', 'later')
    assert checkedFolderPath(f'  {folder}  ') == os.path.join(str(tmp_path), 'later')


def test_home_is_expanded():
    assert checkedFolderPath(os.path.join('~', 'data')) == os.path.join(
        os.path.expanduser('~'), 'data')


def test_empty_is_refused_unless_allowed():
    with pytest.raises(ValueError, match='Enter a folder'):
        checkedFolderPath('  ')
    assert checkedFolderPath(None, allowEmpty=True) == ''


def test_a_relative_path_is_refused_with_the_reason():
    with pytest.raises(ValueError, match='full path'):
        checkedFolderPath('recordings')


def test_an_existing_file_is_refused(tmp_path):
    aFile = tmp_path / 'notes.txt'
    aFile.write_text('')
    with pytest.raises(ValueError, match='is a file'):
        checkedFolderPath(str(aFile))


# --- FolderPathEdit ----------------------------------------------------------------

@pytest.fixture
def edit(qapp):
    from imswitch.imcommon.view.guitools import FolderPathEdit

    widget = FolderPathEdit(fallback='')
    yield widget
    widget.deleteLater()


def test_browse_starts_at_the_nearest_folder_that_exists(edit, tmp_path):
    edit.setText(str(tmp_path / 'recordings' / '2026-09-29'))
    assert edit.browseStart() == str(tmp_path)


def test_browse_falls_back_when_nothing_typed_exists(qapp, tmp_path):
    from imswitch.imcommon.view.guitools import FolderPathEdit

    widget = FolderPathEdit(fallback=str(tmp_path))
    try:
        assert widget.browseStart() == str(tmp_path)
    finally:
        widget.deleteLater()


def test_a_picked_folder_lands_in_the_field(edit, tmp_path, monkeypatch):
    changed = []
    edit.textChanged.connect(changed.append)
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getExistingDirectory',
                        staticmethod(lambda *_args: str(tmp_path)))
    edit.browseButton.click()
    assert edit.text() == str(tmp_path)
    assert changed[-1] == str(tmp_path)


def test_cancelling_the_picker_keeps_what_was_there(edit, tmp_path, monkeypatch):
    edit.setText(str(tmp_path))
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getExistingDirectory',
                        staticmethod(lambda *_args: ''))
    edit.browse()
    assert edit.text() == str(tmp_path)
