"""Preferences > Default folders…: kept between sessions, applied at once.

The default data and save folders used to be set from the File menu with a
bare folder picker and forgotten when ImSwitch closed. They now live in
improcess_options.json, are shown and cleared in one dialog, and are read
back when ImProcess starts.
"""
from __future__ import annotations

import json
import os
from importlib import import_module
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtWidgets  # noqa: E402

from imswitch.improcess.controller.CommunicationChannel import CommunicationChannel  # noqa: E402
from imswitch.improcess.model.folder_preferences import (  # noqa: E402
    FolderPreferences,
    load_folder_preferences,
    save_folder_preferences,
)

# import_module, not `from ... import`: the controller package's lazy exports
# resolve the module name to the class, and the tests patch the module.
fileio = import_module("imswitch.improcess.controller.FileIOController")

_APP = None


@pytest.fixture(scope="module")
def qapp():
    # Held at module level: dropping the last QApplication reference deletes
    # every Python-owned QObject.
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


# --- the options file ----------------------------------------------------------

def test_no_file_means_no_defaults(tmp_path):
    assert load_folder_preferences(tmp_path / "improcess_options.json") == FolderPreferences()


def test_saved_folders_come_back(tmp_path):
    path = tmp_path / "config" / "improcess_options.json"
    saved = FolderPreferences(dataFolder="/data/raw", saveFolder="/data/results")
    save_folder_preferences(saved, path)
    assert load_folder_preferences(path) == saved


def test_saving_keeps_keys_it_does_not_know(tmp_path):
    path = tmp_path / "improcess_options.json"
    path.write_text(json.dumps({"somethingLater": 3, "dataFolder": "/old"}))
    save_folder_preferences(FolderPreferences(dataFolder="/new"), path)
    assert json.loads(path.read_text()) == {
        "somethingLater": 3, "dataFolder": "/new", "saveFolder": "",
    }


@pytest.mark.parametrize("content", ["{not json", "[1, 2]"])
def test_an_unreadable_file_gives_the_defaults_and_is_replaced_on_save(tmp_path, content):
    path = tmp_path / "improcess_options.json"
    path.write_text(content)
    assert load_folder_preferences(path) == FolderPreferences()
    save_folder_preferences(FolderPreferences(saveFolder="/out"), path)
    assert load_folder_preferences(path) == FolderPreferences(saveFolder="/out")


def test_a_value_that_is_not_a_string_is_unset(tmp_path):
    path = tmp_path / "improcess_options.json"
    path.write_text(json.dumps({"dataFolder": 5, "saveFolder": "/out"}))
    assert load_folder_preferences(path) == FolderPreferences(saveFolder="/out")


# --- the dialog ------------------------------------------------------------------

@pytest.fixture
def dialog(qapp):
    from imswitch.improcess.view.FolderPreferencesDialog import FolderPreferencesDialog

    widget = FolderPreferencesDialog()
    yield widget
    widget.deleteLater()


def test_it_shows_the_folders_in_force(dialog):
    dialog.setValues(FolderPreferences(dataFolder="/data/raw", saveFolder=""))
    assert dialog.values() == {"dataFolder": "/data/raw", "saveFolder": ""}
    assert not dialog.statusLabel.isVisibleTo(dialog)


def test_save_asks_the_controller_and_does_not_close_by_itself(dialog):
    asked = []
    dialog.sigSaveRequested.connect(asked.append)
    dialog.setValues(FolderPreferences(dataFolder="  /data/raw  ", saveFolder="/out"))
    dialog.show()
    dialog.buttons.accepted.emit()
    assert asked == [{"dataFolder": "/data/raw", "saveFolder": "/out"}]
    assert dialog.result() != QtWidgets.QDialog.Accepted


# --- the controller ---------------------------------------------------------------

def _controller(dialog, commChannel):
    controller = fileio.FileIOController.__new__(fileio.FileIOController)
    controller._widget = SimpleNamespace(folderPreferencesDialog=dialog,
                                         showFolderPreferencesDialog=lambda: None)
    controller._commChannel = commChannel
    controller._logger = SimpleNamespace(error=lambda *_a, **_k: None)
    controller._dataFolder = None
    controller._saveFolder = None
    return controller


@pytest.fixture
def channel(qapp):
    commChannel = CommunicationChannel()
    emitted = {"data": [], "save": []}
    commChannel.sigDataFolderChanged.connect(emitted["data"].append)
    commChannel.sigSaveFolderChanged.connect(emitted["save"].append)
    return commChannel, emitted


@pytest.fixture
def optionsFile(monkeypatch):
    saved = []
    monkeypatch.setattr(fileio, "save_folder_preferences", saved.append)
    return saved


def test_saving_writes_the_file_and_applies_the_folders(dialog, channel, optionsFile, tmp_path):
    commChannel, emitted = channel
    controller = _controller(dialog, commChannel)

    controller.saveFolderPreferences({"dataFolder": str(tmp_path / "raw"), "saveFolder": ""})

    assert optionsFile == [FolderPreferences(dataFolder=str(tmp_path / "raw"), saveFolder="")]
    assert emitted == {"data": [str(tmp_path / "raw")], "save": [None]}
    assert dialog.result() == QtWidgets.QDialog.Accepted


def test_a_relative_folder_keeps_the_dialog_open_and_says_which(dialog, channel, optionsFile):
    commChannel, emitted = channel
    controller = _controller(dialog, commChannel)

    controller.saveFolderPreferences({"dataFolder": "", "saveFolder": "results"})

    assert optionsFile == [] and emitted == {"data": [], "save": []}
    assert dialog.statusLabel.text().startswith("Save results to:")
    assert "full path" in dialog.statusLabel.text()


def test_a_file_that_cannot_be_written_keeps_the_dialog_open(dialog, channel, monkeypatch):
    commChannel, emitted = channel
    controller = _controller(dialog, commChannel)

    def refuse(_preferences):
        raise PermissionError("read-only")
    monkeypatch.setattr(fileio, "save_folder_preferences", refuse)

    controller.saveFolderPreferences({"dataFolder": "", "saveFolder": ""})

    assert emitted == {"data": [], "save": []}
    assert "read-only" in dialog.statusLabel.text()


def test_opening_seeds_the_dialog_with_the_folders_in_force(dialog, channel):
    commChannel, _emitted = channel
    controller = _controller(dialog, commChannel)
    controller._dataFolder = "/data/raw"

    controller.openFolderPreferences()

    assert dialog.values() == {"dataFolder": "/data/raw", "saveFolder": ""}


def test_startup_uses_the_saved_folders(channel, monkeypatch):
    """What was saved last session is what the dialogs start in -- including
    the add-data dialog, which hears it through the channel."""
    commChannel, emitted = channel
    monkeypatch.setattr(fileio, "load_folder_preferences",
                        lambda: FolderPreferences(dataFolder="/data/raw", saveFolder="/out"))
    factory = SimpleNamespace(createController=lambda *_a, **_k: SimpleNamespace())
    widget = SimpleNamespace(multiDataFrame=None, pickDatasetsDialog=None)

    controller = fileio.FileIOController(commChannel, widget=widget, factory=factory,
                                         moduleCommChannel=None, mainController=None)

    assert (controller._dataFolder, controller._saveFolder) == ("/data/raw", "/out")
    assert emitted["data"] == ["/data/raw"]


# --- the menu --------------------------------------------------------------------

def test_the_preferences_menu_offers_it_and_the_app_entries_join_it(qapp):
    from qtpy import QtCore

    from imswitch.imcommon.view.MultiModuleWindow import MultiModuleWindow
    from imswitch.improcess.view.ImProcessMainView import ImProcessMainView

    class _View(QtWidgets.QMainWindow):
        sigOpenFolderPreferences = QtCore.Signal()

    view = _View()
    window = MultiModuleWindow("test")
    try:
        menuBar = view.menuBar()
        menuBar.addMenu("&View")
        ImProcessMainView._buildPreferencesMenu(view, menuBar)
        window.addItemsToMenuBar(menuBar)

        menus = {a.text(): [b.text() for b in a.menu().actions() if not b.isSeparator()]
                 for a in menuBar.actions() if a.menu() is not None}
        assert list(menus) == ["&View", "&Preferences", "&Help"]
        assert menus["&Preferences"] == ["Default folders…", "Set active modules…",
                                         "Open user files folder"]
        opened = []
        view.sigOpenFolderPreferences.connect(lambda: opened.append(True))
        view.folderPreferencesAction.trigger()
        assert opened == [True]
    finally:
        window.deleteLater()
        view.deleteLater()
