"""The setup folder: a setting of the computer, with setups in subfolders.

ImSwitch used to take setups only from ``<ImSwitchConfig>/imcontrol_setups``
and only from its top level, where the shipped examples piled up. The folder
is now ``setupFolder`` in ``imcontrol_options.json`` (Preferences > Setup
folder…, in ImSwitch and in the Config Studio), setups may sit in its
subfolders, and the setup in use is named relative to it.
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model import configfiletools, setupfolder
from imswitch.imcontrol.model.Options import Options
from imswitch.imcontrol.model.SetupInfo import SetupInfo


def _touch(path, text='{}'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


# -- listing and naming ------------------------------------------------------

def test_setups_at_the_top_come_first_then_each_subfolder(tmp_path):
    for name in ('zeta.json', 'Alpha.json', 'real/sted/sted.json',
                 'mock/b.json', 'mock/A/deep.json', 'notes.txt'):
        _touch(tmp_path / name)

    assert setupfolder.listSetupFiles(tmp_path) == [
        'Alpha.json', 'zeta.json',
        'mock/A/deep.json', 'mock/b.json',
        'real/sted/sted.json',
    ]


def test_hidden_folders_caches_and_deep_folders_are_not_searched(tmp_path):
    _touch(tmp_path / '.git' / 'x.json')
    _touch(tmp_path / '__pycache__' / 'y.json')
    _touch(tmp_path / 'a' / 'b' / 'c' / 'd' / 'ok.json')
    _touch(tmp_path / 'a' / 'b' / 'c' / 'd' / 'e' / 'too_deep.json')

    assert setupfolder.listSetupFiles(tmp_path) == ['a/b/c/d/ok.json']
    assert setupfolder.listSetupFiles(tmp_path / 'missing') == []


def test_a_setup_is_found_by_its_relative_name_or_its_unique_file_name(tmp_path):
    _touch(tmp_path / 'mock' / 'scan' / 'rig.json')
    _touch(tmp_path / 'a' / 'twin.json')
    _touch(tmp_path / 'b' / 'twin.json')

    assert setupfolder.findSetupFile(tmp_path, 'mock/scan/rig.json') == 'mock/scan/rig.json'
    assert setupfolder.findSetupFile(tmp_path, 'rig.json') == 'mock/scan/rig.json'
    assert setupfolder.findSetupFile(tmp_path, 'twin.json') is None  # never guess
    assert setupfolder.findSetupFile(tmp_path, 'other/rig.json') is None
    assert setupfolder.findSetupFile(tmp_path, 'missing.json') is None
    assert setupfolder.findSetupFile(tmp_path, '') is None


def test_a_setup_is_named_relative_to_the_folder_or_by_its_full_path(tmp_path):
    folder = tmp_path / 'setups'
    inside = _touch(folder / 'mock' / 'rig.json')
    outside = _touch(tmp_path / 'elsewhere' / 'rig.json')

    assert setupfolder.setupNameFor(inside, folder) == 'mock/rig.json'
    assert setupfolder.setupNameFor(outside, folder) == os.path.abspath(outside)
    # Either name leads back to the file.
    for path in (inside, outside):
        name = setupfolder.setupNameFor(path, folder)
        assert Path(setupfolder.setupFilePath(folder, name)) == path


def test_the_default_folder_is_stored_as_unset(tmp_path, monkeypatch):
    monkeypatch.setattr(setupfolder, 'defaultSetupFolder', lambda: str(tmp_path / 'default'))

    assert setupfolder.setupFolderSetting(tmp_path / 'default') == ''
    assert setupfolder.setupFolderSetting('') == ''
    assert setupfolder.setupFolderSetting(tmp_path / 'lab') == str(tmp_path / 'lab')
    assert setupfolder.setupFolderOf('') == str(tmp_path / 'default')
    assert setupfolder.setupFolderOf(str(tmp_path / 'lab')) == str(tmp_path / 'lab')


# -- configfiletools ---------------------------------------------------------

@pytest.fixture
def options_file(tmp_path, monkeypatch):
    path = tmp_path / 'config' / 'imcontrol_options.json'
    path.parent.mkdir()
    monkeypatch.setattr(configfiletools, '_optionsFilePath', str(path))
    monkeypatch.setattr(configfiletools, '_options', None)
    return path


def test_setups_are_read_from_and_written_to_the_chosen_folder(tmp_path, options_file):
    lab = tmp_path / 'lab_share'
    _touch(lab / 'scopes' / 'rig.json')
    options_file.write_text(json.dumps(
        {'setupFileName': 'scopes/rig.json', 'setupFolder': str(lab)}
    ))

    options, _ = configfiletools.loadOptions()
    assert configfiletools.getSetupFilesDir() == str(lab)
    assert configfiletools.getSetupList() == ['scopes/rig.json']

    setupInfo = configfiletools.loadSetupInfo(options, SetupInfo)
    setupInfo.disabledDevices = {'lasers': {'405': {'managerName': 'NidaqLaserManager'}}}
    configfiletools.saveSetupInfo(options, setupInfo)
    saved = json.loads((lab / 'scopes' / 'rig.json').read_text())
    assert saved['disabledDevices'] == {'lasers': {'405': {'managerName': 'NidaqLaserManager'}}}


def test_options_without_a_setup_folder_use_the_default(options_file):
    options_file.write_text(json.dumps({'setupFileName': 'example_no_hardware.json'}))

    options, _ = configfiletools.loadOptions()

    assert options.setupFolder == ''
    assert configfiletools.getSetupFilesDir(options) == setupfolder.defaultSetupFolder()


def test_options_follow_a_setup_sorted_into_a_subfolder(tmp_path, options_file):
    # An update sorted the shipped examples into subfolders, or the user did:
    # options naming the old top-level file follow it, and say so on disk.
    lab = tmp_path / 'lab'
    _touch(lab / 'mock' / 'point_scanning' / 'simple_mock_setup.json')
    options_file.write_text(json.dumps(
        {'setupFileName': 'simple_mock_setup.json', 'setupFolder': str(lab)}
    ))

    options, _ = configfiletools.loadOptions()

    assert options.setupFileName == 'mock/point_scanning/simple_mock_setup.json'
    assert json.loads(options_file.read_text())['setupFileName'] == options.setupFileName


def test_options_naming_an_ambiguous_or_missing_setup_are_left_alone(tmp_path, options_file):
    lab = tmp_path / 'lab'
    _touch(lab / 'a' / 'rig.json')
    _touch(lab / 'b' / 'rig.json')
    options_file.write_text(json.dumps({'setupFileName': 'rig.json', 'setupFolder': str(lab)}))

    options, _ = configfiletools.loadOptions()

    assert options.setupFileName == 'rig.json'  # the setup picker decides


def test_the_setup_folder_survives_a_save_of_other_preferences(tmp_path, options_file):
    import dataclasses
    from imswitch.imcontrol.model.Options import RecordingOptions

    configfiletools.saveOptions(Options(setupFileName='x.json', setupFolder=str(tmp_path)))
    options, _ = configfiletools.loadOptions()
    configfiletools.saveOptions(dataclasses.replace(
        options, recording=RecordingOptions(outputFolder=str(tmp_path / 'rec'))
    ))

    assert json.loads(options_file.read_text())['setupFolder'] == str(tmp_path)


def test_improcess_reads_a_setup_in_a_subfolder(tmp_path, options_file):
    from imswitch.improcess.model.processing_config import selected_setup_path

    lab = tmp_path / 'lab'
    rig = _touch(lab / 'processing' / 'fiji_processor.json')
    options_file.write_text(json.dumps(
        {'setupFileName': 'processing/fiji_processor.json', 'setupFolder': str(lab)}
    ))

    assert selected_setup_path() == rig


# -- the shipped examples ----------------------------------------------------

def test_the_default_setup_is_the_only_one_at_the_top_of_the_examples():
    shipped = setupfolder.listSetupFiles(setupfolder.shippedSetupFolder())

    top = [name for name in shipped if '/' not in name]
    assert top == ['example_no_hardware.json']
    assert configfiletools._defaultSetupFileName(shipped) == 'example_no_hardware.json'


def test_the_examples_are_sorted_into_mock_real_and_processing_folders():
    shipped = setupfolder.listSetupFiles(setupfolder.shippedSetupFolder())

    for name in shipped:
        parts = name.split('/')
        if len(parts) == 1:
            continue
        assert parts[0] in ('mock', 'real', 'processing'), name
        if parts[0] in ('mock', 'real'):
            assert len(parts) == 3, f'{name}: mock/ and real/ hold one folder per modality'
        else:
            assert len(parts) == 2, name


def test_every_example_file_name_is_unique():
    # Bare names find them (tutorials, options from before the sort).
    shipped = setupfolder.listSetupFiles(setupfolder.shippedSetupFolder())
    names = [name.rsplit('/', 1)[-1] for name in shipped]

    assert len(names) == len(set(names))
    for name in names:
        assert setupfolder.shippedSetupPath(name).is_file()


# -- ImSwitch: Preferences > Setup folder… -----------------------------------

class _Dialog:
    def __init__(self):
        self.status, self.accepted = None, False

    def setStatus(self, text):
        self.status = text

    def accept(self):
        self.accepted = True


def _controller(dialog, restarts):
    from imswitch.imcontrol.controller.ImConMainController import ImConMainController

    fake = SimpleNamespace(
        _ImConMainController__mainView=SimpleNamespace(setupFolderDialog=dialog),
        _ImConMainController__logger=SimpleNamespace(info=lambda *a: None,
                                                     error=lambda *a, **k: None),
        _restartAfterShutdown=lambda **kwargs: restarts.append(kwargs or True),
    )
    return lambda values: ImConMainController.saveSetupFolder(fake, values)


def test_choosing_another_folder_saves_it_with_its_setup_and_restarts(
        tmp_path, options_file, monkeypatch):
    from imswitch.imcontrol.view import guitools

    lab = tmp_path / 'lab'
    _touch(lab / 'rigs' / 'scope.json')
    configfiletools.saveOptions(Options(setupFileName='example_no_hardware.json'))
    monkeypatch.setattr(guitools, 'askYesNoQuestion', lambda *args: True)
    dialog, restarts = _Dialog(), []

    _controller(dialog, restarts)({'setupFolder': str(lab), 'setupFileName': 'rigs/scope.json'})

    saved = json.loads(options_file.read_text())
    assert saved['setupFolder'] == str(lab)
    assert saved['setupFileName'] == 'rigs/scope.json'
    assert dialog.accepted and restarts == [{'setupChosen': True}]


def test_a_folder_without_a_setup_is_refused_and_nothing_is_saved(
        tmp_path, options_file, monkeypatch):
    configfiletools.saveOptions(Options(setupFileName='example_no_hardware.json'))
    before = options_file.read_text()
    dialog, restarts = _Dialog(), []

    _controller(dialog, restarts)({'setupFolder': str(tmp_path / 'empty'), 'setupFileName': ''})

    assert 'no setup file' in dialog.status
    assert options_file.read_text() == before
    assert not dialog.accepted and restarts == []


def test_a_relative_folder_is_refused(options_file):
    configfiletools.saveOptions(Options(setupFileName='example_no_hardware.json'))
    dialog, restarts = _Dialog(), []

    _controller(dialog, restarts)({'setupFolder': 'setups', 'setupFileName': 'x.json'})

    assert 'full path' in dialog.status and restarts == []


# -- the dialog --------------------------------------------------------------

def test_the_dialog_lists_the_folder_and_preselects_the_setup_in_use(qtbot, tmp_path):
    pytest.importorskip('PyQt5')
    from imswitch.imcontrol.view.SetupFolderDialog import SetupFolderDialog

    _touch(tmp_path / 'top.json')
    _touch(tmp_path / 'mock' / 'rig.json')
    dialog = SetupFolderDialog()
    qtbot.addWidget(dialog)

    # The setup in use is named by its old top-level name: still preselected.
    dialog.setValues(str(tmp_path), 'rig.json')

    items = [dialog.setupPicker.itemText(i) for i in range(dialog.setupPicker.count())]
    assert items == ['top.json', 'mock/rig.json']
    assert dialog.values() == {'setupFolder': str(tmp_path), 'setupFileName': 'mock/rig.json'}
    assert '2 setup files' in dialog.previewLabel.text()

    dialog.folderEdit.setText(str(tmp_path / 'empty'))
    dialog.refreshSetups()
    assert dialog.setupPicker.count() == 0 and not dialog.setupPicker.isEnabled()
    assert 'does not exist' in dialog.previewLabel.text()


# -- the Config Studio -------------------------------------------------------

@pytest.fixture
def editor_module(monkeypatch, tmp_path):
    pytest.importorskip('PyQt5')
    from imswitch.imcontrol.view.configeditor import editor

    monkeypatch.setattr(editor, '_user_template_dir', lambda: tmp_path / 'templates')
    return editor


def test_the_config_list_is_a_tree_of_the_folder(qtbot, tmp_path, editor_module):
    folder = tmp_path / 'setups'
    top = _touch(folder / 'example_no_hardware.json')
    nested = _touch(folder / 'mock' / 'scan' / 'rig.json')
    panel = editor_module.LeftPanel()
    qtbot.addWidget(panel)

    panel.set_folder(str(folder))

    tree = panel._file_list
    assert [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())] == [
        'example_no_hardware.json', 'mock'
    ]
    assert [Path(item.data(0, editor_module.Qt.UserRole)) for item in panel._file_items()] == [
        top, nested
    ]
    assert panel.select_config_path(str(nested))
    assert panel.selected_config_path() == str(nested)

    # Opening the nested file keeps the whole folder listed.
    panel.show_folder_of(str(nested))
    assert panel.config_dir == str(folder)

    # Folder names filter too, and a folder left empty is hidden.
    panel._config_filter.setText('scan')
    mock = tree.topLevelItem(1)
    assert tree.topLevelItem(0).isHidden() and not mock.isHidden()
    panel._config_filter.setText('example')
    assert mock.isHidden()


class _Session:
    """What Set as Active and the setup-folder preference touch of a MainWindow."""

    def __init__(self, editor, options_path):
        self._editor = editor
        self._options_path = str(options_path)
        self._path = ''
        self._modified = False
        self._active_config_changed = False
        self._status = SimpleNamespace(text='', setText=lambda text: None)
        self._left = SimpleNamespace(set_folder=lambda folder: setattr(self, 'listed', folder))
        self.listed = None

    def __getattr__(self, name):
        attribute = getattr(self._editor.MainWindow, name)
        return attribute.__get__(self) if callable(attribute) else attribute

    def _update_active_banner(self):
        pass


def test_set_as_active_names_the_setup_relative_to_the_setup_folder(tmp_path, editor_module):
    lab = tmp_path / 'lab'
    nested = _touch(lab / 'mock' / 'rig.json')
    outside = _touch(tmp_path / 'other' / 'rig.json')
    options = _touch(tmp_path / 'imcontrol_options.json',
                     json.dumps({'setupFileName': 'x.json', 'setupFolder': str(lab)}))
    session = _Session(editor_module, options)

    session._set_path_as_active_config(str(nested))
    assert json.loads(options.read_text())['setupFileName'] == 'mock/rig.json'
    assert session._active_setup_path() == str(nested)

    session._set_path_as_active_config(str(outside))
    assert json.loads(options.read_text())['setupFileName'] == str(outside)
    assert session._active_setup_path() == str(outside)


def test_the_config_studio_sets_the_setup_folder(qtbot, tmp_path, editor_module):
    from imswitch.imcontrol.view.SetupFolderDialog import SetupFolderDialog

    lab = tmp_path / 'lab'
    _touch(lab / 'rigs' / 'scope.json')
    options = _touch(tmp_path / 'imcontrol_options.json',
                     json.dumps({'setupFileName': 'example_no_hardware.json', 'other': 1}))
    session = _Session(editor_module, options)
    session._setup_folder_dialog = SetupFolderDialog()
    qtbot.addWidget(session._setup_folder_dialog)

    session._save_setup_folder({'setupFolder': str(lab), 'setupFileName': 'rigs/scope.json'})

    saved = json.loads(options.read_text())
    assert saved == {'setupFileName': 'rigs/scope.json', 'setupFolder': str(lab), 'other': 1}
    assert session.listed == str(lab)
    assert session._active_config_changed  # a hosting ImSwitch offers a restart
    assert Path(session._active_setup_path()) == lab / 'rigs' / 'scope.json'


def test_the_config_studio_keeps_the_active_name_for_a_folder_without_setups(
        qtbot, tmp_path, editor_module):
    from imswitch.imcontrol.view.SetupFolderDialog import SetupFolderDialog

    options = _touch(tmp_path / 'imcontrol_options.json',
                     json.dumps({'setupFileName': 'example_no_hardware.json'}))
    session = _Session(editor_module, options)
    session._setup_folder_dialog = SetupFolderDialog()
    qtbot.addWidget(session._setup_folder_dialog)

    session._save_setup_folder({'setupFolder': str(tmp_path / 'new_lab'), 'setupFileName': ''})

    saved = json.loads(options.read_text())
    assert saved['setupFolder'] == str(tmp_path / 'new_lab')
    assert saved['setupFileName'] == 'example_no_hardware.json'
    assert (tmp_path / 'new_lab').is_dir()
