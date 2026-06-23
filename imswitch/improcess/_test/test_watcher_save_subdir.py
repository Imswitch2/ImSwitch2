"""Contract tests for the file-watcher output subdirectory.

Exercises Reconstructor.default_save_subdir and the helpers on
WatcherFrameController that consume it. The watcher's full Qt-coupled
toggleWatch path isn't called here; we only verify the contract that the
main controller relies on when it routes the active reconstructor's
default_save_subdir to the watcher.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock
import importlib

from imswitch.improcess.controller.WatcherFrameController import (
    WatcherFrameController,
)
from imswitch.improcess.reconstructors.base import Reconstructor


# --- base contract ---------------------------------------------------------


def test_base_reconstructor_default_save_subdir_is_rec():
    assert Reconstructor.default_save_subdir == 'rec'


def test_view_only_inherits_default_save_subdir():
    from imswitch.improcess.reconstructors.view_only import ViewOnlyReconstructor

    assert ViewOnlyReconstructor.default_save_subdir == 'rec'


def test_modality_reconstructors_inherit_default_save_subdir():
    from imswitch.improcess.reconstructors.monalisa import MonalisaReconstructor
    from imswitch.improcess.reconstructors.snouty import SnoutyReconstructor

    # Plugins are free to override later — this just locks in that the
    # current built-ins inherit the base 'rec' default, so changing the
    # base attribute name in the future is a single-line edit.
    for cls in (MonalisaReconstructor, SnoutyReconstructor):
        assert cls.default_save_subdir == 'rec', cls.__name__


# --- watcher setters -------------------------------------------------------


def _make_watcher_stub():
    """Bind the setter methods to a bare stub — the full controller pulls
    in Qt + comm channel + the FileWatcher background thread, which is far
    beyond what these tests need."""
    stub = SimpleNamespace(_saveSubdir='rec', _savePrefix='rec_')
    stub.setSaveSubdir = WatcherFrameController.setSaveSubdir.__get__(stub)
    stub.setSavePrefix = WatcherFrameController.setSavePrefix.__get__(stub)
    return stub


def test_set_save_subdir_overrides_default():
    stub = _make_watcher_stub()
    stub.setSaveSubdir('deskew')
    assert stub._saveSubdir == 'deskew'


def test_set_save_subdir_falls_back_when_empty():
    stub = _make_watcher_stub()
    stub.setSaveSubdir('')
    assert stub._saveSubdir == 'rec'
    stub.setSaveSubdir(None)
    assert stub._saveSubdir == 'rec'


def test_set_save_subdir_strips_separators_and_whitespace():
    stub = _make_watcher_stub()
    stub.setSaveSubdir('  /out/  ')
    assert stub._saveSubdir == 'out'


def test_set_save_prefix_overrides_default():
    stub = _make_watcher_stub()
    stub.setSavePrefix('deskew_')
    assert stub._savePrefix == 'deskew_'


def test_set_save_prefix_accepts_empty_string():
    stub = _make_watcher_stub()
    stub.setSavePrefix('')
    assert stub._savePrefix == ''
    stub.setSavePrefix(None)
    assert stub._savePrefix == ''


# --- watcher queue error handling -----------------------------------------


def _make_queue_watcher_stub(tmp_path):
    stub = SimpleNamespace(
        _widget=SimpleNamespace(path=str(tmp_path)),
        _commChannel=SimpleNamespace(sigReconstruct=SimpleNamespace(emit=MagicMock())),
        _saveSubdir='rec',
        _savePrefix='rec_',
        execution=False,
        toExecute=[],
        attrs=None,
        watcher=SimpleNamespace(removeFromList=MagicMock()),
    )
    setattr(stub, '_WatcherFrameController__logger', MagicMock())
    stub.runNextFile = WatcherFrameController.runNextFile.__get__(stub)
    stub._markForRedetection = WatcherFrameController._markForRedetection.__get__(stub)
    stub._closeDataObjs = WatcherFrameController._closeDataObjs
    return stub


class _FakeDataObj:
    def __init__(self, name, dataset_name, *, path=None, file=None):
        self.name = name
        self.datasetName = dataset_name
        self.path = path
        self.file = file
        self.attrs = {'writing': False}
        self.closed = False

    @staticmethod
    def getDatasetNames(path):
        if path.endswith('bad.hdf5'):
            raise ValueError('unsupported layout')
        if path.endswith('busy.hdf5'):
            raise OSError('writing in progress')
        return ['CAM']

    @staticmethod
    def _open(path, dataset_name):
        return object(), dataset_name

    def checkLock(self):
        return None

    def checkAndUnloadData(self):
        self.closed = True


def test_run_next_file_skips_unreadable_file_and_continues(monkeypatch, tmp_path):
    watcher_module = importlib.import_module(
        'imswitch.improcess.controller.WatcherFrameController'
    )

    monkeypatch.setattr(watcher_module, 'DataObj', _FakeDataObj)
    stub = _make_queue_watcher_stub(tmp_path)
    # runNextFile pops from the end, so bad is attempted before good.
    stub.toExecute = ['good.hdf5', 'bad.hdf5']

    stub.runNextFile()

    stub._commChannel.sigReconstruct.emit.assert_called_once()
    data_objs, consolidate = stub._commChannel.sigReconstruct.emit.call_args.args
    assert consolidate is True
    assert len(data_objs) == 1
    assert data_objs[0].datasetName == 'CAM'
    assert stub.current.endswith('good.hdf5')
    assert stub.execution is True


def test_run_next_file_marks_not_ready_file_for_redetection(monkeypatch, tmp_path):
    watcher_module = importlib.import_module(
        'imswitch.improcess.controller.WatcherFrameController'
    )

    monkeypatch.setattr(watcher_module, 'DataObj', _FakeDataObj)
    stub = _make_queue_watcher_stub(tmp_path)
    stub.toExecute = ['busy.hdf5']

    stub.runNextFile()

    stub._commChannel.sigReconstruct.emit.assert_not_called()
    stub.watcher.removeFromList.assert_called_once_with(['busy.hdf5'])
    assert stub.execution is False


def test_run_next_file_continues_after_not_ready_file(monkeypatch, tmp_path):
    watcher_module = importlib.import_module(
        'imswitch.improcess.controller.WatcherFrameController'
    )

    monkeypatch.setattr(watcher_module, 'DataObj', _FakeDataObj)
    stub = _make_queue_watcher_stub(tmp_path)
    # busy is attempted first, then good should still be reconstructed.
    stub.toExecute = ['good.hdf5', 'busy.hdf5']

    stub.runNextFile()

    stub.watcher.removeFromList.assert_called_once_with(['busy.hdf5'])
    stub._commChannel.sigReconstruct.emit.assert_called_once()
    assert stub.current.endswith('good.hdf5')
    assert stub.execution is True
