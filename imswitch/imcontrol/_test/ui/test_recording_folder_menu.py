"""Preferences > Recordings folder..., end to end on a running imcontrol.

The unit tests cover the dialog and the save rules with a stub controller.
This one covers the wiring no stub can: that the menu entry opens the dialog
seeded from the options file, and that saving it moves the real Recording
widget to the new folder. The options file itself is patched: a test run must
never rewrite the imcontrol_options.json of whoever runs it.
"""

import os
import time

import pytest

from imswitch.imcontrol.model import configfiletools
from . import getApp, prepareUI, setupInfoBasic
from .. import optionsBasic

mainView = None


@pytest.fixture(scope='module')
def qapp():
    global mainView
    app = getApp()
    mainView = prepareUI(optionsBasic, setupInfoBasic)
    yield app


@pytest.fixture
def optionsFile(monkeypatch):
    saved = []
    monkeypatch.setattr(configfiletools, 'loadOptions', lambda: (optionsBasic, False))
    monkeypatch.setattr(configfiletools, 'saveOptions', saved.append)
    return saved


def test_saving_a_new_folder_moves_the_recording_widget(qapp, qtbot, tmp_path, optionsFile):
    dialog = mainView.recordingFolderDialog
    mainView.recordingFolderAction.trigger()
    qapp.processEvents()
    try:
        assert dialog.isVisible()
        assert dialog.values()['outputFolder'] == optionsBasic.recording.outputFolder

        dialog.folderEdit.setText(str(tmp_path))
        dialog.dateSubfolderBox.setChecked(True)
        dialog.buttons.accepted.emit()
        qapp.processEvents()

        [saved] = optionsFile
        assert saved.recording.outputFolder == str(tmp_path)
        assert saved.setupFileName == optionsBasic.setupFileName
        assert not dialog.isVisible()
        assert mainView.widgets['Recording'].getRecFolder() == os.path.join(
            str(tmp_path), time.strftime('%Y-%m-%d'))
    finally:
        dialog.hide()
