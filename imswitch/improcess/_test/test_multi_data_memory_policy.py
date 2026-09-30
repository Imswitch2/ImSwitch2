"""The Multidata panel decides what a recording handed over in memory becomes."""

from io import BytesIO

import h5py
import numpy as np
import pytest

from imswitch.imcommon.framework import Signal, SignalInterface
from imswitch.improcess.controller import MultiDataFrameController as mdfc_module
from imswitch.improcess.controller.CommunicationChannel import CommunicationChannel
from imswitch.improcess.controller.MultiDataFrameController import MultiDataFrameController


class _FakeWidget(SignalInterface):
    sigAddDataClicked = Signal()
    sigLoadCurrentDataClicked = Signal()
    sigLoadAllDataClicked = Signal()
    sigUnloadCurrentDataClicked = Signal()
    sigUnloadAllDataClicked = Signal()
    sigDeleteCurrentDataClicked = Signal()
    sigDeleteAllDataClicked = Signal()
    sigSaveCurrentDataClicked = Signal()
    sigSaveAllDataClicked = Signal()
    sigSetAsCurrentDataClicked = Signal()
    sigSelectedItemChanged = Signal()
    sigMemoryRecordingPolicyChanged = Signal(str)

    def __init__(self):
        super().__init__()
        self.rows = []
        self.memoryFlags = {}
        self.policy = None
        self.highlighted = []

    def setMemoryRecordingPolicy(self, policy):
        self.policy = policy

    def addDataObj(self, name, datasetName, dataObj):
        self.rows.append(dataObj)

    def setDataObjMemoryFlag(self, dataObj, inMemory):
        self.memoryFlags[id(dataObj)] = inMemory

    def getAllDataObjs(self):
        return list(self.rows)

    def getSelectedDataObj(self):
        return self.rows[-1] if self.rows else None

    def getSelectedDataObjs(self):
        return list(self.rows[-1:])

    def setAllRowsHighlighted(self, highlighted):
        self.highlighted.append(('all', highlighted))

    def setRowHighlightedByDataObj(self, dataObj, highlighted):
        self.highlighted.append((dataObj, highlighted))

    def setLoadedStatusText(self, text):
        pass

    def __getattr__(self, name):
        # The enable/disable helpers updateInfo drives, all no-ops here.
        if name.startswith('set') and name.endswith('Enabled'):
            return lambda value: None
        raise AttributeError(name)

    def delDataByDataObj(self, dataObj):
        self.rows.remove(dataObj)


class _FakeMemoryRecordings(SignalInterface):
    sigDataSet = Signal(str, object)
    sigDataSavedToDisk = Signal(str, str)
    sigDataWillRemove = Signal(str)

    def __init__(self):
        super().__init__()
        self.items = {}

    def __getitem__(self, name):
        return self.items[name]

    def emit_dataset(self, name, item):
        self.items[name] = item
        self.sigDataSet.emit(name, item)


class _FakeModuleCommChannel:
    def __init__(self):
        self.memoryRecordings = _FakeMemoryRecordings()


class _VFileItem:
    def __init__(self, data):
        self.data = data
        self.savedToDisk = False
        self.filePath = None


class _Factory:
    def createController(self, *args, **kwargs):
        pass


@pytest.fixture
def rig(monkeypatch):
    saved = []
    monkeypatch.setattr(mdfc_module, 'load_memory_recording_policy', lambda: 'list')
    monkeypatch.setattr(mdfc_module, 'save_memory_recording_policy', lambda policy: saved.append(policy))
    channel = CommunicationChannel()
    widget = _FakeWidget()
    module_channel = _FakeModuleCommChannel()
    controller = MultiDataFrameController(
        channel, widget, _Factory(), moduleCommChannel=module_channel,
    )
    current = []
    channel.sigCurrentDataChanged.connect(lambda dataObj: current.append(dataObj))
    policies = []
    channel.sigMemoryRecordingPolicyChanged.connect(lambda policy: policies.append(policy))
    return controller, widget, module_channel, current, policies, saved


def _hdf5(frames=3):
    buf = BytesIO()
    with h5py.File(buf, 'w') as f:
        f.create_group('CAM').create_dataset(
            'data', data=np.arange(frames * 4, dtype=np.uint16).reshape(frames, 2, 2)
        )
    return buf


def test_the_persisted_policy_is_shown_and_list_only_adds_a_row(rig):
    controller, widget, module_channel, current, _, _ = rig
    assert widget.policy == 'list'

    module_channel.memoryRecordings.emit_dataset('rec', _VFileItem(_hdf5()))

    assert len(widget.rows) == 1
    assert widget.rows[0].datasetName == 'CAM'
    assert widget.memoryFlags[id(widget.rows[0])] is True
    assert current == []


def test_open_as_current_loads_the_first_dataset(rig):
    controller, widget, module_channel, current, policies, saved = rig

    widget.sigMemoryRecordingPolicyChanged.emit('current')
    assert controller.memoryRecordingPolicy == 'current'
    assert saved == ['current']
    assert policies == ['current']

    module_channel.memoryRecordings.emit_dataset('rec', _VFileItem(_hdf5()))

    assert len(current) == 1
    assert current[0] is widget.rows[0]
    assert current[0].dataLoaded
    assert (widget.rows[0], True) in widget.highlighted


def test_reconstruct_policy_also_opens_the_recording(rig):
    controller, widget, module_channel, current, _, _ = rig
    widget.sigMemoryRecordingPolicyChanged.emit('reconstruct')

    module_channel.memoryRecordings.emit_dataset('rec', _VFileItem(_hdf5()))

    assert len(current) == 1


def test_a_zarr_group_is_listed_as_is(rig, tmp_path):
    import zarr

    controller, widget, module_channel, _, _, _ = rig
    root = zarr.open_group(str(tmp_path / 'rec.zarr'), mode='w')
    array = root.create_group('CAM').create_array('data', shape=(2, 2, 2), dtype='uint16')
    array[:] = 1

    module_channel.memoryRecordings.emit_dataset('zarr', _VFileItem(root))

    assert len(widget.rows) == 1
    assert widget.rows[0].datasetName == 'CAM'
    assert list(controller.getDataObjsByMemRecordingName('zarr')) == [widget.rows[0]]


def test_an_unreadable_payload_is_reported_not_raised(rig):
    controller, widget, module_channel, _, _, _ = rig
    module_channel.memoryRecordings.emit_dataset('junk', _VFileItem(BytesIO(b'not hdf5')))
    assert widget.rows == []


def test_a_recording_is_listed_once(rig):
    controller, widget, module_channel, _, _, _ = rig
    item = _VFileItem(_hdf5())
    module_channel.memoryRecordings.emit_dataset('rec', item)
    module_channel.memoryRecordings.emit_dataset('rec', item)
    assert len(widget.rows) == 1
