"""Preferences > Recordings folder…, and where ImControl's menu items live.

The default recordings folder describes the computer, so it lives in
imcontrol_options.json; this dialog is how it is changed without editing
that file. Saving also points the Recording widget at the new folder, which
is safe mid-recording because a recording fixes its file name when it starts.
"""
import os
import time
from types import SimpleNamespace

import pytest
from qtpy import QtWidgets

_APP = None


@pytest.fixture(scope="module")
def qapp():
    # Held at module level: dropping the last QApplication reference deletes
    # every Python-owned QObject.
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


@pytest.fixture
def dialog(qapp):
    from imswitch.imcontrol.view.RecordingFolderDialog import RecordingFolderDialog

    widget = RecordingFolderDialog()
    yield widget
    widget.deleteLater()


# --- the rule: one folder, plus today's date when asked -------------------------

def test_the_folder_gets_a_dated_subfolder_only_when_asked(tmp_path):
    from imswitch.imcontrol.model.Options import RecordingOptions

    when = time.mktime((2026, 3, 4, 12, 0, 0, 0, 0, -1))
    dated = RecordingOptions(outputFolder=str(tmp_path), includeDateInOutputFolder=True)
    plain = RecordingOptions(outputFolder=str(tmp_path), includeDateInOutputFolder=False)
    assert dated.folderFor(when) == os.path.join(str(tmp_path), '2026-03-04')
    assert plain.folderFor(when) == str(tmp_path)


# --- the dialog -------------------------------------------------------------------

def test_it_shows_what_the_file_holds_and_where_today_goes(dialog, tmp_path):
    dialog.setValues(SimpleNamespace(outputFolder=str(tmp_path),
                                     includeDateInOutputFolder=True))
    assert dialog.values() == {'outputFolder': str(tmp_path),
                               'includeDateInOutputFolder': True}
    assert os.path.join(str(tmp_path), time.strftime('%Y-%m-%d')) in dialog.previewLabel.text()

    dialog.dateSubfolderBox.setChecked(False)
    assert time.strftime('%Y-%m-%d') not in dialog.previewLabel.text()
    assert str(tmp_path) in dialog.previewLabel.text()
    assert not dialog.statusLabel.isVisibleTo(dialog)


def test_restore_default_puts_back_the_shipped_folder(dialog, tmp_path):
    from imswitch.imcontrol.model.Options import RecordingOptions

    dialog.setValues(SimpleNamespace(outputFolder=str(tmp_path),
                                     includeDateInOutputFolder=False))
    dialog.restoreDefaults()
    assert dialog.values() == {'outputFolder': RecordingOptions().outputFolder,
                               'includeDateInOutputFolder': True}


def test_save_asks_the_controller_and_does_not_close_by_itself(dialog, tmp_path):
    asked = []
    dialog.sigSaveRequested.connect(asked.append)
    dialog.setValues(SimpleNamespace(outputFolder=f'  {tmp_path}  ',
                                     includeDateInOutputFolder=False))
    dialog.show()
    dialog.buttons.accepted.emit()
    assert asked == [{'outputFolder': str(tmp_path), 'includeDateInOutputFolder': False}]
    assert dialog.result() != QtWidgets.QDialog.Accepted


# --- the controller: save to the file, point the Recording widget at it ---------

def _controller(dialog, *, recordingController):
    from imswitch.imcontrol.controller import ImConMainController as module

    stub = SimpleNamespace()
    stub._ImConMainController__mainView = SimpleNamespace(recordingFolderDialog=dialog)
    stub._ImConMainController__logger = SimpleNamespace(error=lambda *_a, **_k: None,
                                                         info=lambda *_a, **_k: None,
                                                         warning=lambda *_a, **_k: None)
    stub.controllers = {} if recordingController is None else {'Recording': recordingController}
    return module.ImConMainController.saveRecordingFolder.__get__(stub), module


@pytest.fixture
def optionsFile(monkeypatch):
    from imswitch.imcontrol.controller import ImConMainController as module
    from imswitch.imcontrol.model.Options import MemoryOptions, Options

    options = Options(setupFileName='x.json', memory=MemoryOptions(writerQueueMB=64))
    saved = []
    monkeypatch.setattr(module.configfiletools, 'loadOptions', lambda: (options, False))
    monkeypatch.setattr(module.configfiletools, 'saveOptions', saved.append)
    return saved


def test_saving_writes_the_options_file_and_moves_the_recording_widget(dialog, tmp_path,
                                                                        optionsFile):
    pointedAt = []
    recording = SimpleNamespace(setRecFolder=pointedAt.append)
    save, _module = _controller(dialog, recordingController=recording)

    save({'outputFolder': str(tmp_path / 'data'), 'includeDateInOutputFolder': True})

    [saved] = optionsFile
    assert saved.recording.outputFolder == str(tmp_path / 'data')
    assert saved.recording.includeDateInOutputFolder is True
    assert saved.setupFileName == 'x.json'                 # nothing else changed
    assert saved.memory.writerQueueMB == 64
    assert pointedAt == [os.path.join(str(tmp_path / 'data'), time.strftime('%Y-%m-%d'))]
    assert dialog.result() == QtWidgets.QDialog.Accepted


def test_a_setup_without_a_recording_widget_still_saves(dialog, tmp_path, optionsFile):
    save, _module = _controller(dialog, recordingController=None)
    save({'outputFolder': str(tmp_path), 'includeDateInOutputFolder': False})
    assert optionsFile[0].recording.outputFolder == str(tmp_path)


def test_a_home_relative_path_is_expanded(dialog, optionsFile):
    save, _module = _controller(dialog, recordingController=None)
    save({'outputFolder': os.path.join('~', 'ImSwitchData'), 'includeDateInOutputFolder': False})
    assert optionsFile[0].recording.outputFolder == os.path.join(
        os.path.expanduser('~'), 'ImSwitchData')


@pytest.mark.parametrize('folder, reason', [
    ('', 'Enter the folder'),
    ('recordings', 'full path'),
])
def test_a_folder_that_cannot_be_one_keeps_the_dialog_open_with_the_reason(
        dialog, optionsFile, folder, reason):
    pointedAt = []
    save, _module = _controller(dialog,
                                recordingController=SimpleNamespace(setRecFolder=pointedAt.append))
    save({'outputFolder': folder, 'includeDateInOutputFolder': True})
    assert optionsFile == [] and pointedAt == []
    assert reason in dialog.statusLabel.text()


def test_an_existing_file_is_not_a_folder(dialog, tmp_path, optionsFile):
    aFile = tmp_path / 'notes.txt'
    aFile.write_text('')
    save, _module = _controller(dialog, recordingController=None)
    save({'outputFolder': str(aFile), 'includeDateInOutputFolder': False})
    assert optionsFile == []
    assert 'is a file' in dialog.statusLabel.text()


# --- the menu bar -------------------------------------------------------------

@pytest.fixture
def mainView(qapp):
    from imswitch.imcontrol.model.Options import Options
    from imswitch.imcontrol.view import ImConMainView, ViewSetupInfo

    view = ImConMainView(Options(setupFileName='x.json'),
                         ViewSetupInfo.from_dict({}, infer_missing=True))
    yield view
    view.deleteLater()


def _menus(view):
    return {action.text(): [a.text() for a in action.menu().actions() if not a.isSeparator()]
            for action in view.menuBar().actions() if action.menu() is not None}


def test_each_menu_holds_one_kind_of_thing(mainView):
    menus = _menus(mainView)
    assert list(menus) == ['&File', '&Hardware', '&View', '&Shortcuts', '&Preferences']
    assert menus['&File'] == ['Session notes…',
                              'Load parameters from saved HDF5 file…',
                              'Load parameters from saved Zarr store…',
                              'Save Widget States…', 'Load Widget States…']
    assert menus['&Hardware'] == ['Pick hardware setup…', 'Edit hardware configuration…']
    assert menus['&View'] == ['Reset panel layout']
    assert menus['&Preferences'] == ['Recordings folder…', 'Memory limits…']


def test_the_recordings_folder_entry_asks_the_controller(mainView):
    opened = []
    mainView.sigOpenRecordingFolder.connect(lambda: opened.append(True))
    mainView.recordingFolderAction.trigger()
    assert opened == [True]


def test_the_application_preferences_join_imcontrols_own(mainView):
    """MultiModuleWindow adds its entries to the Preferences menu ImControl
    already has, after a separator, rather than a second Preferences menu."""
    from imswitch.imcommon.view.MultiModuleWindow import MultiModuleWindow

    window = MultiModuleWindow('test')
    try:
        window.addItemsToMenuBar(mainView.menuBar())
        menus = _menus(mainView)
        assert list(menus) == ['&File', '&Hardware', '&View', '&Shortcuts',
                               '&Preferences', '&Help']
        assert menus['&Preferences'] == ['Recordings folder…', 'Memory limits…',
                                         'Set active modules…', 'Open user files folder']
    finally:
        window.deleteLater()
