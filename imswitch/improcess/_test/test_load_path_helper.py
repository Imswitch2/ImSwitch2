"""Contract tests for FileIOController._loadFromPath.

The full controller can't easily be constructed under offscreen Qt (it spins
up napari), so these tests bind the method to a hand-built stand-in object
that records every routing decision. The point is to lock in the
multi-dataset picker / single-dataset / prefer-as-current branches so that
future refactors don't quietly diverge again.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from imswitch.improcess.controller.FileIOController import (
    FileIOController,
)


class _RecordingMultiData:
    def __init__(self):
        self.added = []

    def makeAndAddDataObj(self, name, datasetName, path=None, file=None):
        self.added.append((name, datasetName, path))


class _RecordingPickController:
    def __init__(self):
        self.lastDatasets = None
        self.selected = []

    def setDatasets(self, path, datasets):
        self.lastDatasets = (path, list(datasets))

    def getSelectedDatasets(self):
        return list(self.selected)


class _FakeWidget:
    def __init__(self, accept_pick=True):
        self.accept_pick = accept_pick
        self.dialog_shown = False
        self.raised_current = False

    def showPickDatasetsDialog(self, blocking=False):
        self.dialog_shown = True
        return self.accept_pick

    def raiseCurrentDataDock(self):
        self.raised_current = True


class _FakeDataObj:
    """Stand-in DataObj that pretends a load succeeded."""

    @classmethod
    def install(cls, monkeypatch, dataset_map):
        """Patch the DataObj symbol inside the controller module so the
        helper's enumeration + construction calls use the fake."""

        # The real resolver returns pathlib paths. On Windows, str(Path(...))
        # uses backslashes even when the test input used forward slashes.
        # Compare dataset keys as native paths, while exercising the actual
        # resolver (including Zarr-child -> store-root normalization).
        def key(path):
            return os.path.normcase(os.path.normpath(os.fspath(path)))

        dataset_map = {key(path): datasets for path, datasets in dataset_map.items()}

        def fake_get_names(path):
            return list(dataset_map.get(key(path), []))

        cls.last = None

        def construct(name, datasetName, path=None, file=None):
            obj = SimpleNamespace(
                name=name,
                datasetName=datasetName,
                path=path,
                dataLoaded=False,
                sourceLoaded=False,
                attrs={},
                load_calls=0,
                open_calls=0,
                checkAndUnloadData=lambda: None,
            )

            def check_and_load():
                obj.load_calls += 1
                obj.dataLoaded = True

            def check_and_open():
                obj.open_calls += 1
                obj.sourceLoaded = True

            obj.checkAndLoadData = check_and_load
            obj.checkAndOpenData = check_and_open
            cls.last = obj
            return obj

        # Wire the fake. The helper uses DataObj.getDatasetNames as classmethod
        # and DataObj(...) as constructor.
        FakeNS = SimpleNamespace()
        FakeNS.getDatasetNames = staticmethod(fake_get_names)
        FakeNS.__call__ = staticmethod(construct)

        class _CallableFake:
            getDatasetNames = staticmethod(fake_get_names)

            def __new__(cls_, name, datasetName, path=None, file=None):
                return construct(name, datasetName, path=path, file=file)

        monkeypatch.setattr(
            'imswitch.improcess.controller.FileIOController.DataObj',
            _CallableFake,
        )


class _CommChannel:
    """Stand-in comm channel that records sigCurrentDataChanged emissions."""

    def __init__(self):
        self.emitted = []

        class _Signal:
            def __init__(self, owner):
                self._owner = owner

            def emit(self, obj):
                self._owner.emitted.append(obj)

        self.sigCurrentDataChanged = _Signal(self)


def _make_controller_stub():
    ctl = SimpleNamespace()
    ctl._logger = SimpleNamespace(
        debug=lambda *_: None,
        info=lambda *_: None,
        warning=lambda *_: None,
        error=lambda *_: None,
    )
    ctl._main = SimpleNamespace(_currentDataObj=None, _activeReconstructor=None)
    ctl._commChannel = _CommChannel()
    ctl._dataFolder = None
    ctl.multiDataFrameController = _RecordingMultiData()
    ctl.pickDatasetsController = _RecordingPickController()
    ctl._widget = _FakeWidget()
    # Bind the real helpers from the class so the production code path is
    # exercised verbatim.
    ctl._loadFromPath = FileIOController._loadFromPath.__get__(ctl)
    ctl._loadAsCurrent = FileIOController._loadAsCurrent.__get__(ctl)
    ctl._activeSourceSpecs = FileIOController._activeSourceSpecs.__get__(ctl)
    ctl._requestLoadPath = FileIOController._requestLoadPath.__get__(ctl)
    ctl._requestQuickLoadPaths = FileIOController._requestQuickLoadPaths.__get__(ctl)
    ctl._needsSourceFamilyChoice = FileIOController._needsSourceFamilyChoice
    ctl._requestSourceFamily = FileIOController._requestSourceFamily.__get__(ctl)
    ctl.quickLoadData = FileIOController.quickLoadData.__get__(ctl)
    ctl.quickLoadVirtualData = FileIOController.quickLoadVirtualData.__get__(ctl)
    return ctl


# --- single-dataset routing ------------------------------------------------


def test_single_dataset_default_goes_to_multidata(monkeypatch):
    _FakeDataObj.install(monkeypatch, {'/x/file.h5': ['frame']})
    ctl = _make_controller_stub()

    outcome = ctl._loadFromPath('/x/file.h5')

    assert outcome == 'multidata'
    assert ctl.multiDataFrameController.added == [('file.h5', 'frame', os.path.normpath('/x/file.h5'))]
    assert ctl._main._currentDataObj is None
    assert not ctl._widget.dialog_shown


def test_zarr_child_path_is_normalized_before_routing(monkeypatch):
    _FakeDataObj.install(monkeypatch, {'/x/store.zarr': ['APD']})
    ctl = _make_controller_stub()

    outcome = ctl._loadFromPath('/x/store.zarr/APD/data/0.0.0')

    assert outcome == 'multidata'
    assert ctl.multiDataFrameController.added == [('store.zarr', 'APD', os.path.normpath('/x/store.zarr'))]


def test_quick_load_zarr_source_uses_folder_dialog(monkeypatch):
    ctl = _make_controller_stub()
    ctl._widget.loadedDataFrame = SimpleNamespace(sourceMode=lambda: 'zarr')

    monkeypatch.setattr(
        'imswitch.improcess.controller.FileIOController.guitools.askForFolderPath',
        lambda *args, **kwargs: '/picked/store.zarr',
    )
    monkeypatch.setattr(
        'imswitch.improcess.controller.FileIOController.guitools.askForFilePath',
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('file dialog used')),
    )

    assert ctl._requestQuickLoadPaths() == ['/picked/store.zarr']


def test_quick_load_files_source_uses_multi_file_dialog(monkeypatch):
    ctl = _make_controller_stub()
    ctl._widget.loadedDataFrame = SimpleNamespace(sourceMode=lambda: 'files')
    ctl._main._activeReconstructor = SimpleNamespace(
        file_extensions=['hdf5', 'tiff', 'zarr']
    )

    monkeypatch.setattr(
        'imswitch.improcess.controller.FileIOController.guitools.askForFolderPath',
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('folder dialog used')),
    )
    seen = {}
    def _pick(*args, **kwargs):
        seen.update(kwargs)
        return ['/picked/a.h5', '/picked/b.h5']
    monkeypatch.setattr(
        'imswitch.improcess.controller.FileIOController.guitools.askForFilePath',
        _pick,
    )

    assert ctl._requestQuickLoadPaths() == ['/picked/a.h5', '/picked/b.h5']
    assert seen.get('multiFiles') is True

def test_single_dataset_prefer_current_routes_to_current(monkeypatch):
    _FakeDataObj.install(monkeypatch, {'/x/file.h5': ['frame']})
    ctl = _make_controller_stub()

    outcome = ctl._loadFromPath('/x/file.h5', prefer_as_current=True)

    assert outcome == 'current'
    assert ctl._main._currentDataObj is not None
    assert ctl._main._currentDataObj.datasetName == 'frame'
    assert ctl._commChannel.emitted == [ctl._main._currentDataObj]
    assert ctl._widget.raised_current
    assert ctl.multiDataFrameController.added == []
    assert ctl._main._currentDataObj.load_calls == 1
    assert ctl._main._currentDataObj.open_calls == 0


def test_single_dataset_virtual_current_opens_source_without_loading(monkeypatch):
    _FakeDataObj.install(monkeypatch, {'/x/file.h5': ['frame']})
    ctl = _make_controller_stub()

    outcome = ctl._loadFromPath(
        '/x/file.h5',
        prefer_as_current=True,
        virtual_current=True,
    )

    assert outcome == 'current'
    assert ctl._main._currentDataObj.datasetName == 'frame'
    assert ctl._main._currentDataObj.sourceLoaded is True
    assert ctl._main._currentDataObj.dataLoaded is False
    assert ctl._main._currentDataObj.open_calls == 1
    assert ctl._main._currentDataObj.load_calls == 0
    assert ctl._commChannel.emitted == [ctl._main._currentDataObj]
    assert ctl._widget.raised_current


# --- multi-dataset picker --------------------------------------------------


def test_multi_dataset_picker_cancel_returns_cancelled(monkeypatch):
    _FakeDataObj.install(monkeypatch, {'/x/file.h5': ['a', 'b']})
    ctl = _make_controller_stub()
    ctl._widget.accept_pick = False

    outcome = ctl._loadFromPath('/x/file.h5', prefer_as_current=True)

    assert outcome == 'cancelled'
    assert ctl._widget.dialog_shown
    assert ctl.multiDataFrameController.added == []
    assert ctl._main._currentDataObj is None


def test_multi_dataset_picker_zero_selected_returns_empty(monkeypatch):
    _FakeDataObj.install(monkeypatch, {'/x/file.h5': ['a', 'b']})
    ctl = _make_controller_stub()
    ctl.pickDatasetsController.selected = []  # accepted but nothing picked

    outcome = ctl._loadFromPath('/x/file.h5')

    assert outcome == 'empty'
    assert ctl.multiDataFrameController.added == []


def test_multi_dataset_picker_one_selected_with_prefer_current_routes_to_current(monkeypatch):
    _FakeDataObj.install(monkeypatch, {'/x/file.h5': ['a', 'b']})
    ctl = _make_controller_stub()
    ctl.pickDatasetsController.selected = ['b']

    outcome = ctl._loadFromPath('/x/file.h5', prefer_as_current=True)

    assert outcome == 'current'
    assert ctl._main._currentDataObj.datasetName == 'b'
    assert ctl.multiDataFrameController.added == []


def test_multi_dataset_picker_many_selected_goes_to_multidata_even_with_prefer_current(monkeypatch):
    _FakeDataObj.install(monkeypatch, {'/x/file.h5': ['a', 'b', 'c']})
    ctl = _make_controller_stub()
    ctl.pickDatasetsController.selected = ['a', 'c']

    outcome = ctl._loadFromPath('/x/file.h5', prefer_as_current=True)

    assert outcome == 'multidata'
    assert ctl.multiDataFrameController.added == [
        ('file.h5', 'a', os.path.normpath('/x/file.h5')),
        ('file.h5', 'c', os.path.normpath('/x/file.h5')),
    ]
    assert ctl._main._currentDataObj is None


# --- empty / error handling ------------------------------------------------


def test_empty_file_returns_empty(monkeypatch):
    _FakeDataObj.install(monkeypatch, {'/x/file.h5': []})
    ctl = _make_controller_stub()

    outcome = ctl._loadFromPath('/x/file.h5', prefer_as_current=True)

    assert outcome == 'empty'
    assert ctl.multiDataFrameController.added == []


def test_dataset_enumeration_failure_returns_empty(monkeypatch):
    class _Boom:
        @staticmethod
        def getDatasetNames(_):
            raise OSError("dataset enumeration failed")

        def __new__(cls, *args, **kwargs):
            raise AssertionError("constructor must not run when enumeration fails")

    monkeypatch.setattr(
        'imswitch.improcess.controller.FileIOController.DataObj',
        _Boom,
    )
    ctl = _make_controller_stub()
    captured = []
    ctl._logger.error = lambda msg: captured.append(msg)

    outcome = ctl._loadFromPath('/x/nope.h5')

    assert outcome == 'empty'
    assert any('Could not read datasets' in m for m in captured)


# --- basename derivation ---------------------------------------------------


@pytest.mark.parametrize(
    'dataPath,expected',
    [
        ('/abs/file.h5', 'file.h5'),
        ('relative.tif', 'relative.tif'),
        ('/abs/group.zarr', 'group.zarr'),
    ],
)
def test_basename_used_for_data_obj_name(monkeypatch, dataPath, expected):
    _FakeDataObj.install(monkeypatch, {dataPath: ['only']})
    ctl = _make_controller_stub()
    ctl._loadFromPath(dataPath)
    assert ctl.multiDataFrameController.added[0][0] == expected
