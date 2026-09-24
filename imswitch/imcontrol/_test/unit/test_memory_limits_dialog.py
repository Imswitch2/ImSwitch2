"""Tools > Memory limits…: shows what is in force, saves, applies at once.

The limits describe the computer, so they live in imcontrol_options.json;
this dialog is how they are changed without editing that file. Saving is
refused while a recording runs, because every queue check reads the limit in
force and a smaller queue would fail the recording in progress.
"""
from types import SimpleNamespace

import pytest
from qtpy import QtWidgets

from imswitch.imcommon.model import memory_limits
from imswitch.imcommon.model.memory_limits import MIB

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
    from imswitch.imcontrol.view.MemoryLimitsDialog import MemoryLimitsDialog

    widget = MemoryLimitsDialog()
    yield widget
    widget.deleteLater()


def test_it_shows_what_the_file_holds(dialog):
    dialog.setValues(SimpleNamespace(writerQueueMB=128, perDetectorQueueMB=64,
                                     processingWorkingSetMB=2048))
    assert dialog.values() == {'writerQueueMB': 128, 'perDetectorQueueMB': 64,
                               'processingWorkingSetMB': 2048}
    assert not dialog.statusLabel.isVisibleTo(dialog)


def test_a_value_the_file_cannot_use_shows_the_default_in_force_and_says_so(dialog):
    dialog.setValues(SimpleNamespace(writerQueueMB='lots', perDetectorQueueMB=64,
                                     processingWorkingSetMB=None))
    assert dialog.values() == {'writerQueueMB': 512, 'perDetectorQueueMB': 64,
                               'processingWorkingSetMB': 1024}
    assert "'lots'" in dialog.statusLabel.text()


def test_restore_defaults_puts_back_the_shipped_numbers(dialog):
    dialog.setValues(SimpleNamespace(writerQueueMB=8, perDetectorQueueMB=8,
                                     processingWorkingSetMB=8))
    dialog.restoreDefaults()
    assert dialog.values() == {'writerQueueMB': 512, 'perDetectorQueueMB': 256,
                               'processingWorkingSetMB': 1024}


def test_save_asks_the_controller_and_does_not_close_by_itself(dialog):
    asked = []
    dialog.sigSaveRequested.connect(asked.append)
    dialog.setValues(SimpleNamespace(writerQueueMB=100, perDetectorQueueMB=50,
                                     processingWorkingSetMB=900))
    dialog.show()
    dialog.buttons.accepted.emit()
    assert asked == [{'writerQueueMB': 100, 'perDetectorQueueMB': 50,
                      'processingWorkingSetMB': 900}]
    assert dialog.result() != QtWidgets.QDialog.Accepted


# --- the controller: save to the file, adopt at once, never mid-recording ------

def _controller(dialog, *, recording):
    from imswitch.imcontrol.controller import ImConMainController as module

    stub = SimpleNamespace()
    stub._ImConMainController__mainView = SimpleNamespace(memoryLimitsDialog=dialog)
    stub._ImConMainController__masterController = SimpleNamespace(
        recordingManager=SimpleNamespace(record=recording))
    stub._ImConMainController__logger = SimpleNamespace(error=lambda *_a, **_k: None,
                                                         info=lambda *_a, **_k: None,
                                                         warning=lambda *_a, **_k: None)
    return module.ImConMainController.saveMemoryLimits.__get__(stub), module


def test_saving_writes_the_options_file_and_applies_the_limits_now(dialog, monkeypatch):
    from imswitch.imcontrol.model.Options import Options

    save, module = _controller(dialog, recording=False)
    saved = []
    options = Options(setupFileName='x.json')
    monkeypatch.setattr(module.configfiletools, 'loadOptions', lambda: (options, False))
    monkeypatch.setattr(module.configfiletools, 'saveOptions', saved.append)

    save({'writerQueueMB': 100, 'perDetectorQueueMB': 50, 'processingWorkingSetMB': 900})

    assert len(saved) == 1
    assert saved[0].setupFileName == 'x.json'                    # nothing else changed
    assert (saved[0].memory.writerQueueMB, saved[0].memory.perDetectorQueueMB,
            saved[0].memory.processingWorkingSetMB) == (100, 50, 900)
    assert memory_limits.effectiveBytes('perDetectorQueueBytes', 1) == 50 * MIB
    assert memory_limits.effectiveBytes('processingWorkingSetBytes', 1) == 900 * MIB
    assert dialog.result() == QtWidgets.QDialog.Accepted


def test_saving_is_refused_while_a_recording_runs(dialog, monkeypatch):
    save, module = _controller(dialog, recording=True)
    saved = []
    monkeypatch.setattr(module.configfiletools, 'saveOptions', saved.append)

    save({'writerQueueMB': 1, 'perDetectorQueueMB': 1, 'processingWorkingSetMB': 1})

    assert saved == []
    assert memory_limits.configuredBytes('perDetectorQueueBytes') is None
    assert 'recording is running' in dialog.statusLabel.text()


def test_the_tools_menu_offers_it(qapp):
    """The real main view, on an empty setup: the entry is in Tools and asks
    the controller to open the dialog."""
    from imswitch.imcontrol.model.Options import Options
    from imswitch.imcontrol.view import ImConMainView, ViewSetupInfo

    view = ImConMainView(Options(setupFileName='x.json'),
                         ViewSetupInfo.from_dict({}, infer_missing=True))
    try:
        [tools] = [a.menu() for a in view.menuBar().actions() if a.text() == '&Tools']
        assert 'Memory limits…' in [a.text() for a in tools.actions()]
        opened = []
        view.sigOpenMemoryLimits.connect(lambda: opened.append(True))
        view.memoryLimitsAction.trigger()
        assert opened == [True]
    finally:
        view.deleteLater()
