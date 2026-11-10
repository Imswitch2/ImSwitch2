"""``imswitch --setup <file>``: another hardware setup for one run.

The options file names the setup ImSwitch starts with, and a dozen places save
changes to "the running setup" by asking loadOptions() which file that is. So
the run's setup has to be what loadOptions() names, while nothing but an
explicit choice of setup may change what the options file names -- otherwise a
laser preset saved while running B lands in A's file, or saving the memory
limits quietly makes B the setup ImSwitch starts with.
"""

import importlib
import json
from unittest.mock import Mock, patch

import pytest

from imswitch.imcommon.model import ostools
from imswitch.imcontrol.model import configfiletools
from imswitch.imcontrol.model.Options import MemoryOptions
from imswitch.imcontrol.model.SetupInfo import SetupInfo


@pytest.fixture
def setups(tmp_path, monkeypatch):
    """A setups folder with two setups and an options file naming the first."""
    setupDir = tmp_path / 'imcontrol_setups'
    setupDir.mkdir()
    for name in ('a_scope.json', 'b_scope.json'):
        (setupDir / name).write_text('{}')
    optionsPath = tmp_path / 'imcontrol_options.json'
    optionsPath.write_text(json.dumps({'setupFileName': 'a_scope.json'}))

    monkeypatch.setattr(configfiletools, '_setupFilesDir', str(setupDir))
    monkeypatch.setattr(configfiletools, '_optionsFilePath', str(optionsPath))
    monkeypatch.setattr(configfiletools, '_options', None)
    monkeypatch.setattr(configfiletools, '_setupForThisRun', None)
    return setupDir, optionsPath


@pytest.fixture(autouse=True)
def _no_restart_left_armed():
    ostools.cancelRestart()
    yield
    ostools.cancelRestart()


def _optionsFileSetup(optionsPath):
    return json.loads(optionsPath.read_text())['setupFileName']


# ── Which file --setup names ──────────────────────────────────────────────
@pytest.mark.parametrize('value', ['b_scope.json', 'b_scope', ' b_scope.json '])
def test_a_setup_is_named_with_or_without_its_extension(setups, value):
    assert configfiletools.resolveSetupArgument(value) == 'b_scope.json'


def test_a_path_into_the_setups_folder_is_accepted(setups):
    setupDir, _ = setups
    assert configfiletools.resolveSetupArgument(str(setupDir / 'b_scope.json')) == 'b_scope.json'


def test_a_file_outside_the_setups_folder_is_refused(setups, tmp_path):
    elsewhere = tmp_path / 'b_scope.json'
    elsewhere.write_text('{}')
    with pytest.raises(ValueError, match='not in the setups folder'):
        configfiletools.resolveSetupArgument(str(elsewhere))


def test_an_unknown_setup_is_refused_with_the_list_to_choose_from(setups):
    with pytest.raises(ValueError, match='Available: a_scope.json, b_scope.json'):
        configfiletools.resolveSetupArgument('c_scope')


# ── The options while it is in force ──────────────────────────────────────
def test_load_options_names_the_setup_for_this_run(setups):
    _, optionsPath = setups
    configfiletools.useSetupForThisRun('b_scope.json')

    options, didNotExist = configfiletools.loadOptions()

    assert options.setupFileName == 'b_scope.json'
    assert didNotExist is False
    assert _optionsFileSetup(optionsPath) == 'a_scope.json'


def test_a_change_to_the_running_setup_is_saved_to_its_own_file(setups):
    """What LaserController and SettingsController do on a preset or ROI save."""
    setupDir, _ = setups
    configfiletools.useSetupForThisRun('b_scope.json')
    setupInfo = SetupInfo.from_json(json.dumps({'availableWidgets': ['Laser']}),
                                    infer_missing=True)

    configfiletools.saveSetupInfo(configfiletools.loadOptions()[0], setupInfo)

    assert json.loads((setupDir / 'b_scope.json').read_text())['availableWidgets'] == ['Laser']
    assert (setupDir / 'a_scope.json').read_text() == '{}'


def test_saving_other_options_leaves_the_options_file_setup_alone(setups):
    """The memory-limits and recordings-folder dialogs save the whole options."""
    _, optionsPath = setups
    configfiletools.useSetupForThisRun('b_scope.json')
    options, _ = configfiletools.loadOptions()

    configfiletools.saveOptions(
        configfiletools.Options(setupFileName=options.setupFileName,
                                memory=MemoryOptions(perDetectorQueueMB=123))
    )

    saved = json.loads(optionsPath.read_text())
    assert saved['setupFileName'] == 'a_scope.json'
    assert saved['memory']['perDetectorQueueMB'] == 123
    assert configfiletools.loadOptions()[0].setupFileName == 'b_scope.json'


def test_choosing_a_setup_writes_it_but_this_run_keeps_its_own(setups):
    """Until the restart, the running setup is still the one saves must reach."""
    _, optionsPath = setups
    configfiletools.useSetupForThisRun('b_scope.json')

    configfiletools.saveSetupChoice('a_scope.json')
    assert _optionsFileSetup(optionsPath) == 'a_scope.json'
    configfiletools.saveSetupChoice('b_scope.json')
    assert _optionsFileSetup(optionsPath) == 'b_scope.json'

    assert configfiletools.loadOptions()[0].setupFileName == 'b_scope.json'


def test_without_setup_for_this_run_saving_options_is_unchanged(setups):
    _, optionsPath = setups
    options, _ = configfiletools.loadOptions()

    configfiletools.saveOptions(configfiletools.Options(setupFileName='b_scope.json'))

    assert options.setupFileName == 'a_scope.json'
    assert _optionsFileSetup(optionsPath) == 'b_scope.json'


# ── The restart after a choice ────────────────────────────────────────────
@pytest.mark.parametrize('argv, expected', [
    (['imswitch', '--setup', 'b_scope.json', '--debug'], ['--debug']),
    (['imswitch', '--debug', '--setup=b_scope.json', '--scale', '0.8'],
     ['--debug', '--scale', '0.8']),
    (['imswitch', '--scale', '0.8'], ['--scale', '0.8']),
])
def test_restart_arguments_after_a_choice_drop_setup(monkeypatch, argv, expected):
    monkeypatch.setattr(configfiletools.sys, 'argv', argv)
    assert configfiletools.restartArgumentsForSetupChoice() == expected


def test_a_restart_with_arguments_execs_with_those_arguments():
    ostools.requestRestart(arguments=['--debug'])
    with patch.object(ostools.sys, 'argv', ['imswitch', '--setup', 'b_scope.json', '--debug']), \
            patch.object(ostools.os, 'execv') as execv:
        ostools.restartSoftware()

    assert execv.call_args.args[1][-1:] == ['--debug']
    assert '--setup' not in execv.call_args.args[1]


def test_a_withdrawn_restart_forgets_its_arguments():
    ostools.requestRestart(arguments=['--debug'])
    ostools.cancelRestart()
    with patch.object(ostools.sys, 'argv', ['imswitch', '--setup', 'b_scope.json']), \
            patch.object(ostools.os, 'execv') as execv:
        ostools.restartSoftware()

    assert execv.call_args.args[1][-2:] == ['--setup', 'b_scope.json']


# ── Startup ───────────────────────────────────────────────────────────────
def test_first_run_with_setup_neither_asks_nor_writes_the_options_file(setups, monkeypatch):
    pytest.importorskip('PyQt5')
    import imswitch.imcontrol as imcontrol
    import imswitch.imcontrol.view as view

    _, optionsPath = setups
    optionsPath.unlink()
    configfiletools.useSetupForThisRun('b_scope.json')
    monkeypatch.setattr(importlib.import_module('imswitch.imcontrol.view.PickSetupDialog'),
                        'PickSetupDialog',
                        Mock(side_effect=AssertionError('asked for a setup')))
    monkeypatch.setattr(view, 'ImConMainView', Mock())
    controllerClass = Mock()
    monkeypatch.setattr(
        importlib.import_module('imswitch.imcontrol.controller.ImConMainController'),
        'ImConMainController', controllerClass)

    imcontrol.getMainViewAndController(Mock())

    options = controllerClass.call_args.args[0]
    assert options.setupFileName == 'b_scope.json'
    assert not optionsPath.exists()


# ── Picking a setup in a run started with --setup ─────────────────────────
def _controller():
    from imswitch.imcontrol.controller.ImConMainController import ImConMainController

    controller = ImConMainController.__new__(ImConMainController)
    controller._ImConMainController__logger = Mock()
    controller._ImConMainController__mainView = Mock()
    controller._ImConMainController__configEditor = None
    controller._ImConMainController__activeSetupPath = None
    controller.pickSetupController = Mock()
    return controller


def test_picking_a_setup_makes_it_the_default_and_restarts_without_setup(setups, monkeypatch):
    pytest.importorskip('PyQt5')
    _, optionsPath = setups
    configfiletools.useSetupForThisRun('b_scope.json')
    monkeypatch.setattr(configfiletools.sys, 'argv',
                        ['imswitch', '--setup', 'b_scope.json', '--debug'])
    controller = _controller()
    controller.pickSetupController.getSelectedSetup.return_value = 'b_scope.json'
    with patch('imswitch.imcontrol.controller.ImConMainController.guitools.askYesNoQuestion',
               return_value=True):
        controller.pickSetup()

    assert _optionsFileSetup(optionsPath) == 'b_scope.json'
    assert ostools.restartRequested() == 'imswitch'
    with patch.object(ostools.os, 'execv') as execv:
        ostools.restartSoftware()
    assert execv.call_args.args[1][-1:] == ['--debug']
    assert '--setup' not in execv.call_args.args[1]


@pytest.mark.parametrize('activeChanged, keepsSetup', [(False, True), (True, False)])
def test_a_config_edit_restart_keeps_setup_unless_the_active_config_changed(
        setups, monkeypatch, activeChanged, keepsSetup):
    """Saving the running setup restarts into it; *Set as active* is a choice."""
    pytest.importorskip('PyQt5')
    from qtpy import QtCore

    monkeypatch.setattr(configfiletools.sys, 'argv', ['imswitch', '--setup', 'b_scope.json'])
    controller = _controller()
    controller._ImConMainController__activeSetupPath = '/setups/b_scope.json'
    editor = Mock()
    editor.saved_files.return_value = frozenset(['/setups/b_scope.json'])
    editor.active_config_changed = activeChanged
    controller._ImConMainController__configEditor = editor

    with patch.object(QtCore.QTimer, 'singleShot') as singleShot:
        controller._onConfigEditorClosed()
    with patch('imswitch.imcontrol.controller.ImConMainController.guitools.askYesNoQuestion',
               return_value=True):
        singleShot.call_args.args[1]()
    with patch.object(ostools.os, 'execv') as execv:
        ostools.restartSoftware()

    assert ('--setup' in execv.call_args.args[1]) is keepsSetup


def test_the_recovery_page_pick_also_restarts_without_setup(setups, monkeypatch, qtbot):
    pytest.importorskip('PyQt5')
    from qtpy import QtWidgets
    module = importlib.import_module('imswitch.imcontrol.view.ImControlLoadErrorView')

    _, optionsPath = setups
    configfiletools.useSetupForThisRun('b_scope.json')
    monkeypatch.setattr(configfiletools.sys, 'argv', ['imswitch', '--setup', 'b_scope.json'])
    view = module.ImControlLoadErrorView(RuntimeError('camera failed'))
    qtbot.addWidget(view)
    dialog = Mock()
    dialog.exec_.return_value = QtWidgets.QDialog.Accepted
    dialog.getSelectedSetup.return_value = 'a_scope.json'
    monkeypatch.setattr(module, 'PickSetupDialog', Mock(return_value=dialog))
    monkeypatch.setattr(QtWidgets.QMessageBox, 'question',
                        Mock(return_value=QtWidgets.QMessageBox.Yes))
    monkeypatch.setattr(view, 'window', Mock())

    view.pickSetup()

    assert _optionsFileSetup(optionsPath) == 'a_scope.json'
    with patch.object(ostools.os, 'execv') as execv:
        ostools.restartSoftware()
    assert '--setup' not in execv.call_args.args[1]
