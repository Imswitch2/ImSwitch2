"""LiveReconController: ImProcess reconstructors over the acquisition stream.

The detector, its manager and the communication channel are doubles that
implement the parts the controller touches (lease bookkeeping, the chunk
consumer fan-out, the scan lifecycle signals and geometry accessors); the
reconstructors, the live runtime and its worker threads are the real ones.
"""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import h5py
import numpy as np
import pytest
from qtpy import QtCore

from imswitch.imcommon.framework import Signal, SignalInterface
from imswitch.imcontrol.controller.controllers.LiveReconController import LiveReconController
from imswitch.imcontrol.controller.controllers._acquisition_layout_source import (
    build_point_scan_layouts,
)
from imswitch.imcontrol.model.managers._acquisition_leases import LeasePurpose
from imswitch.imcontrol.model.managers.detectors.DetectorManager import (
    ChunkConsumerOverflowError,
    ChunkKind,
)
from imswitch.imcontrol.model.state_contracts import ComponentStateApplyMode
from imswitch.imcontrol.view.widgets.LiveReconWidget import (
    DISPLAY_PANEL,
    DISPLAY_VIEWER,
    MODE_FREE,
    MODE_SCAN,
    LiveReconWidget,
)
from imswitch.improcess.model.result import ProcessingResult


class _FakeDetector:
    def __init__(self, name='CAM', shape=(5, 4)):
        self.name = name
        self.shape = shape                 # (width, height)
        self.dtype = np.dtype(np.uint16)
        self.pixelSizeUm = [1.0, 0.2, 0.1]
        self.forAcquisition = True
        self.isScanDriven = False
        self._hardware = []
        self.consumers = {}
        self.kinds = {}
        self.overflowed = set()
        self.calls = []

    def getExposureTime(self):
        return 12.5

    def produce(self, *frames):
        self._hardware.extend(frames)

    def _distribute(self):
        frames, self._hardware = self._hardware, []
        for queue in self.consumers.values():
            queue.extend(frames)

    def startChunkConsumer(self, key, kind=ChunkKind.DISPLAY):
        self.calls.append(('start', key, kind))
        self._distribute()
        self.consumers[key] = []
        self.kinds[key] = kind
        self.overflowed.discard(key)

    def readChunk(self, key):
        self.consumers.setdefault(key, [])
        self._distribute()
        if key in self.overflowed:
            raise ChunkConsumerOverflowError('overflow')
        frames = list(self.consumers[key])
        self.consumers[key].clear()
        return frames

    def releaseChunkConsumer(self, key):
        self.calls.append(('release', key))
        self.consumers.pop(key, None)


class _FakeDetectorsManager:
    def __init__(self, *detectors, current=None):
        self._detectors = {d.name: d for d in detectors}
        self._current = current or next(iter(self._detectors))
        self.acquired = []
        self.released = []
        self._seq = 0

    def getAllDeviceNames(self, condition=None):
        return [name for name, d in self._detectors.items() if condition is None or condition(d)]

    def getCurrentDetectorName(self):
        return self._current

    def execOn(self, name, func):
        return func(self._detectors[name])

    def __getitem__(self, name):
        return self._detectors[name]

    def acquire(self, detectorNames, purpose):
        self._seq += 1
        handle = f'lease-{self._seq}'
        self.acquired.append((tuple(detectorNames), purpose, handle))
        return handle

    def release(self, handle):
        self.released.append(handle)


class _SharedAttrs:
    def getHDF5Attributes(self):
        return {'ScanStage:target_device': 'X', 'Detector:exposure': '12.5'}


class _FakeCommChannel(SignalInterface):
    sigScanStarting = Signal()
    sigScanStarted = Signal()
    sigScanEnded = Signal()
    sigResultLayersUpdated = Signal(str, object)
    sigResultLayersRemoved = Signal(str)

    def __init__(self, *, positions=4, layout=None):
        super().__init__()
        self.sharedAttrs = _SharedAttrs()
        self.positions = positions
        self.scanRunning = False
        self._layout = layout

    def getNumScanPositions(self):
        return self.positions

    def getNumCamTTL(self):
        return {'CAM': 1}

    def getActiveScanSource(self):
        if self._layout is None:
            return None
        return SimpleNamespace(
            getAcquisitionLayouts=lambda names: {name: self._layout for name in names}
        )

    def isScanRunning(self):
        return self.scanRunning


class _OnePulseEach(dict):
    def __contains__(self, key):
        return True

    def __getitem__(self, key):
        return 1

    def get(self, key, default=None):
        return 1


def _layout(x=2, y=2):
    return build_point_scan_layouts(
        {'img_dims': [x, y], 'img_axes_phys': ['x', 'y'], 'pixel_sizes': [0.1, 0.2]},
        ('CAM',), scan_source='ScanControllerPointScan', pulse_counts=_OnePulseEach(),
    )['CAM']


def _frame(value, shape=(4, 5)):
    return np.full(shape, value, dtype=np.uint16)


def _setup(reconstructors=('view-only', 'beadrec')):
    return SimpleNamespace(
        detectors={'CAM': SimpleNamespace(managerProperties={})},
        _catchAll={'processing': {'reconstructors': list(reconstructors)}},
    )


@pytest.fixture(autouse=True)
def registry():
    """Keep the process-wide widget-state registry out of these tests."""
    fake = Mock()
    with patch(
        'imswitch.imcontrol.controller.controllers.LiveReconController.getWidgetStatePersistence',
        return_value=fake,
    ):
        yield fake


class _FakeModuleChannel:
    """The module channel as the send-to-ImProcess action sees it."""

    def __init__(self, registered=('imcontrol', 'improcess')):
        self._registered = set(registered)
        self.sent = []
        self.sigProcessingResultProduced = SimpleNamespace(
            emit=lambda result, name: self.sent.append((result, name))
        )

    def isModuleRegistered(self, moduleId):
        return moduleId in self._registered


def _build_rig(qtbot, moduleChannel=None):
    detector = _FakeDetector()
    manager = _FakeDetectorsManager(detector)
    channel = _FakeCommChannel(positions=4, layout=_layout(2, 2))
    widget = LiveReconWidget(SimpleNamespace())
    qtbot.addWidget(widget)
    controller = LiveReconController(
        _setup(), channel, SimpleNamespace(detectorsManager=manager),
        widget=widget, factory=None, moduleCommChannel=moduleChannel,
    )
    return SimpleNamespace(detector=detector, manager=manager, channel=channel,
                           widget=widget, controller=controller, module=moduleChannel)


@pytest.fixture
def rig(qtbot):
    built = _build_rig(qtbot)
    yield built
    built.controller.closeEvent()


@pytest.fixture
def improcess_rig(qtbot):
    built = _build_rig(qtbot, _FakeModuleChannel())
    yield built
    built.controller.closeEvent()


def _run_one_scan(rig, qtbot, frames=4):
    """Drive one scan-coupled run to its end; the controller then holds its result."""
    controller, detector, channel = rig.controller, rig.detector, rig.channel
    controller.setLiveEnabled(True)
    channel.scanRunning = True
    channel.sigScanStarting.emit()
    detector.produce(*[_frame(i + 1) for i in range(frames)])
    qtbot.waitUntil(lambda: controller._heldResult is not None, timeout=8000)
    channel.scanRunning = False
    channel.sigScanEnded.emit()
    qtbot.waitUntil(lambda: not controller.hasActiveRun, timeout=8000)


def test_offers_the_setup_reconstructors_and_hosts_their_parameter_widgets(rig):
    controller, widget = rig.controller, rig.widget

    assert controller.reconstructorIds == ['view-only', 'beadrec']
    assert controller.activeReconstructorId == 'view-only'
    assert widget.selectedReconstructor() == 'view-only'
    assert widget.parameterWidget() is not None
    assert controller.currentParams() == {}

    assert controller.selectReconstructor('beadrec') is True
    assert widget.selectedReconstructor() == 'beadrec'
    params = controller.currentParams()
    assert params['scan_x'] == 0 and params['fit_model'] == 'none'
    assert widget.parameterWidget().get_values()['roi'] is None


def test_unknown_setup_reconstructors_fall_back_to_everything_known(qtbot):
    widget = LiveReconWidget(SimpleNamespace())
    qtbot.addWidget(widget)
    controller = LiveReconController(
        _setup(('does-not-exist',)), _FakeCommChannel(),
        SimpleNamespace(detectorsManager=_FakeDetectorsManager(_FakeDetector())),
        widget=widget, factory=None, moduleCommChannel=None,
    )
    try:
        assert 'view-only' in controller.reconstructorIds
        assert 'monalisa' in controller.reconstructorIds
        assert controller.activeReconstructorId == controller.reconstructorIds[0]
    finally:
        controller.closeEvent()


def test_load_adds_a_known_reconstructor_to_the_offer(rig):
    controller, widget = rig.controller, rig.widget
    assert 'snouty' not in controller.reconstructorIds

    assert controller.loadReconstructor('snouty') is True

    assert controller.reconstructorIds[-1] == 'snouty'
    assert controller.activeReconstructorId == 'snouty'
    assert widget.selectedReconstructor() == 'snouty'
    assert controller.loadReconstructor('no-such-plugin') is False


def test_live_toggled_during_a_scan_waits_for_a_boundary(rig):
    """Stacks are counted from the first frame a run sees, so a scan already
    producing frames is not joined; the next iteration start begins the run."""
    controller, manager, channel = rig.controller, rig.manager, rig.channel
    channel.scanRunning = True

    controller.setLiveEnabled(True)

    assert not controller.hasActiveRun
    assert manager.acquired == []
    assert 'next iteration' in rig.widget.statusText()

    channel.sigScanStarted.emit()
    assert controller.hasActiveRun
    assert rig.detector.calls[0] == ('start', 'LiveRecon', ChunkKind.RAW)


def test_scan_driven_detector_streams_are_built_once_the_scan_is(rig, qtbot):
    """An APD's volume shape is set when the scan is built and its one frame
    arrives at the scan's end: lease at sigScanStarting, source at sigScanStarted."""
    controller, manager, detector, channel = rig.controller, rig.manager, rig.detector, rig.channel
    detector.isScanDriven = True
    detector.shape = (3, 5, 4)                 # (S, Ny, Nx), array order
    channel._layout = None                     # a point scan declares no camera layout

    controller.setLiveEnabled(True)
    channel.scanRunning = True
    channel.sigScanStarting.emit()

    assert controller.hasActiveRun
    assert manager.acquired[0][1] is LeasePurpose.WORKFLOW
    assert detector.calls == []                # nothing armed yet
    assert 'built' in rig.widget.statusText()

    channel.sigScanStarted.emit()
    assert detector.calls[0] == ('start', 'LiveRecon', ChunkKind.RAW)
    assert controller._run.source.stack_info.frame_shape == (3, 5, 4)

    channel.scanRunning = False
    channel.sigScanEnded.emit()
    qtbot.waitUntil(lambda: not controller.hasActiveRun, timeout=8000)
    assert manager.released == ['lease-1']


def test_a_scan_that_ends_before_its_stream_was_built_releases_the_lease(rig):
    controller, manager, detector, channel = rig.controller, rig.manager, rig.detector, rig.channel
    detector.isScanDriven = True
    controller.setLiveEnabled(True)
    channel.sigScanStarting.emit()
    assert controller.hasActiveRun

    channel.sigScanEnded.emit()

    assert not controller.hasActiveRun
    assert manager.released == ['lease-1']
    assert detector.calls == []


def test_close_event_unregisters_the_component(rig, registry):
    rig.controller.closeEvent()
    registry.unregister.assert_called_with('LiveRecon')


def test_scan_starting_leases_and_arms_the_detector_once_per_run(rig, qtbot):
    controller, manager, detector, channel = rig.controller, rig.manager, rig.detector, rig.channel
    controller.setLiveEnabled(True)
    assert 'waiting' in rig.widget.statusText().lower()
    assert manager.acquired == []

    channel.scanRunning = True
    channel.sigScanStarting.emit()

    assert controller.hasActiveRun
    assert manager.acquired == [(('CAM',), LeasePurpose.WORKFLOW, 'lease-1')]
    assert detector.calls[0] == ('start', 'LiveRecon', ChunkKind.RAW)
    info = controller._run.source.stack_info
    assert info.frames_per_stack == 4
    assert info.expected_frames is None
    assert info.attrs['recording:source_format'] == 'memory'
    assert info.attrs['recording:frames_per_stack'] == 4
    assert info.attrs['ScanStage:target_device'] == 'X'
    assert info.attrs['acquisition:exposure_time_ms'] == '12.5'
    assert 'AcquisitionLayout:json' in info.attrs

    # A repeat iteration or a lapse timepoint of the same run: no second lease.
    channel.sigScanStarting.emit()
    assert len(manager.acquired) == 1

    channel.scanRunning = False
    channel.sigScanEnded.emit()
    assert controller._run.ended is True

    qtbot.waitUntil(lambda: not controller.hasActiveRun, timeout=8000)
    assert manager.released == ['lease-1']
    assert ('release', 'LiveRecon') in detector.calls


def test_scan_coupled_run_reaches_the_viewer_signal(rig, qtbot):
    controller, detector, channel = rig.controller, rig.detector, rig.channel
    received = []
    channel.sigResultLayersUpdated.connect(lambda name, layers: received.append((name, layers)))

    controller.setLiveEnabled(True)
    channel.scanRunning = True
    channel.sigScanStarting.emit()
    detector.produce(*[_frame(i) for i in range(4)])

    qtbot.waitUntil(lambda: bool(received), timeout=8000)
    jobName, layers = received[-1]
    assert jobName.endswith('(CAM)')
    data, kwargs, kind = layers[0]
    assert kind == 'image'
    assert data.shape == (4, 5)              # latest plane of the (T, Z, Y, X) buffer
    assert kwargs['name'].startswith('Recon: ')
    assert 'units' not in kwargs

    channel.scanRunning = False
    channel.sigScanEnded.emit()
    qtbot.waitUntil(lambda: not controller.hasActiveRun, timeout=8000)
    assert rig.manager.released == ['lease-1']
    assert 'Finished: 4 frames' in rig.widget.statusText()


def test_free_running_mode_reconstructs_in_chunks_until_switched_off(rig, qtbot):
    controller, detector, channel, widget = rig.controller, rig.detector, rig.channel, rig.widget
    received = []
    channel.sigResultLayersUpdated.connect(lambda name, layers: received.append(layers))
    widget.setMode(MODE_FREE)
    widget.setFramesPerUpdate(2)

    controller.setLiveEnabled(True)

    assert controller.hasActiveRun
    assert rig.manager.acquired[0][1] is LeasePurpose.WORKFLOW
    assert controller._run.source.stack_info.frames_per_stack == 2
    detector.produce(_frame(1), _frame(2), _frame(3), _frame(4))
    qtbot.waitUntil(lambda: bool(received), timeout=8000)

    controller.setLiveEnabled(False)
    qtbot.waitUntil(lambda: not controller.hasActiveRun, timeout=8000)
    assert rig.manager.released == ['lease-1']
    assert widget.isLiveChecked() is False


def test_panel_display_draws_into_the_widget(rig, qtbot):
    controller, detector, channel, widget = rig.controller, rig.detector, rig.channel, rig.widget
    widget.setDisplayTarget(DISPLAY_PANEL)
    widget.setMode(MODE_FREE)
    widget.setFramesPerUpdate(2)

    controller.setLiveEnabled(True)
    detector.produce(_frame(7), _frame(9))
    qtbot.waitUntil(lambda: widget.getImage() is not None, timeout=8000)

    assert widget.getImage().shape == (4, 5)
    controller.setLiveEnabled(False)
    qtbot.waitUntil(lambda: not controller.hasActiveRun, timeout=8000)


def test_frame_stack_reconstructors_refuse_free_running(rig):
    controller, widget = rig.controller, rig.widget
    assert controller.loadReconstructor('monalisa') is True
    widget.setMode(MODE_FREE)

    controller.setLiveEnabled(True)

    assert not controller.hasActiveRun
    assert rig.manager.acquired == []
    assert 'During scans' in widget.statusText()


def test_a_failed_lease_arms_nothing(rig):
    controller, manager, detector, widget = rig.controller, rig.manager, rig.detector, rig.widget

    def refuse(names, purpose):
        raise RuntimeError('detector busy')

    manager.acquire = refuse
    widget.setMode(MODE_FREE)
    controller.setLiveEnabled(True)

    assert not controller.hasActiveRun
    assert detector.calls == []
    assert 'Could not arm' in widget.statusText()


def test_close_event_releases_a_running_lease(qtbot):
    detector = _FakeDetector()
    manager = _FakeDetectorsManager(detector)
    channel = _FakeCommChannel()
    widget = LiveReconWidget(SimpleNamespace())
    qtbot.addWidget(widget)
    controller = LiveReconController(
        _setup(), channel, SimpleNamespace(detectorsManager=manager),
        widget=widget, factory=None, moduleCommChannel=None,
    )
    widget.setMode(MODE_FREE)
    controller.setLiveEnabled(True)
    assert controller.hasActiveRun

    controller.closeEvent()

    assert not controller.hasActiveRun
    assert manager.released == ['lease-1']
    assert ('release', 'LiveRecon') in detector.calls


class _Result(ProcessingResult):
    def save(self, path, fmt):
        return None


def test_layer_data_shows_the_latest_plane_in_micrometres(rig):
    controller, widget = rig.controller, rig.widget
    data = np.zeros((3, 2, 4, 5), dtype=np.float32)
    data[2, 1] = 7
    result = _Result('recon', data, ['T', 'Z', 'Y', 'X'], axis_scales=[1, 1, 100, 50],
                     scale_unit='nm')

    layers = controller._layerDataFor(result, 'job')
    plane, kwargs, kind = layers[0]
    assert kind == 'image'
    assert plane.shape == (4, 5)
    assert float(plane[0, 0]) == 7.0
    assert kwargs['scale'] == pytest.approx((0.1, 0.05))
    assert kwargs['name'] == 'Recon: recon'

    widget.setFullLayer(True)
    full, kwargs, _ = controller._layerDataFor(result, 'job')[0]
    assert full.shape == (3, 2, 4, 5)
    assert len(kwargs['scale']) == 4


def test_component_state_round_trip(rig):
    controller, widget = rig.controller, rig.widget
    widget.setMode(MODE_FREE)
    widget.setDisplayTarget(DISPLAY_PANEL)
    widget.setFramesPerUpdate(7)
    widget.setUseDisplayed(True)
    widget.setKeepLayer(False)
    controller.selectReconstructor('beadrec')
    state = controller.getComponentState()

    widget.setMode(MODE_SCAN)
    widget.setDisplayTarget(DISPLAY_VIEWER)
    widget.setFramesPerUpdate(50)
    widget.setUseDisplayed(False)
    widget.setKeepLayer(True)
    controller.selectReconstructor('view-only')

    warnings = controller.applyComponentState(
        state, applyMode=ComponentStateApplyMode.STARTUP_RESTORE
    )

    assert warnings == []
    assert controller.activeReconstructorId == 'beadrec'
    assert widget.getMode() == MODE_FREE
    assert widget.getDisplayTarget() == DISPLAY_PANEL
    assert widget.getFramesPerUpdate() == 7
    assert widget.getUseDisplayed() is True
    assert widget.getKeepLayer() is False
    assert controller.describeComponentState(state)[0].startswith('Live reconstruction: beadrec')
    assert controller.applyComponentState(
        {'reconstructor': 'nope', 'detector': 'NOPE'},
        applyMode=ComponentStateApplyMode.STARTUP_RESTORE,
    ) == [
        "Reconstructor 'nope' is not available.",
        "Detector 'NOPE' is not available.",
    ]


def test_widget_round_trips_its_controls(qtbot):
    widget = LiveReconWidget(SimpleNamespace())
    qtbot.addWidget(widget)
    seen = []
    widget.sigReconstructorChanged.connect(lambda plugin_id: seen.append(('recon', plugin_id)))
    widget.sigModeChanged.connect(lambda mode: seen.append(('mode', mode)))
    widget.sigDisplayTargetChanged.connect(lambda target: seen.append(('display', target)))

    widget.setReconstructors([('a', 'A', 'first'), ('b', 'B', '')], selected='b')
    assert widget.selectedReconstructor() == 'b'
    assert seen == []                       # programmatic population is silent
    widget.reconstructorCombo.setCurrentIndex(0)
    assert seen == [('recon', 'a')]

    widget.setDetectors(['CAM', 'CAM2'], current='CAM2')
    assert widget.selectedDetector() == 'CAM2'
    widget.modeCombo.setCurrentIndex(1)
    assert widget.getMode() == MODE_FREE
    assert widget.framesPerUpdateSpin.isEnabled()
    widget.displayCombo.setCurrentIndex(1)
    assert widget.getDisplayTarget() == DISPLAY_PANEL
    assert widget.imageView.isVisibleTo(widget)
    assert seen[1:] == [('mode', MODE_FREE), ('display', DISPLAY_PANEL)]

    widget.setImage(np.arange(12, dtype=np.float32).reshape(3, 4))
    assert widget.getImage().shape == (3, 4)
    widget.clearImage()
    assert widget.getImage() is None


# -- send to ImProcess ------------------------------------------------------------

def test_send_to_improcess_hands_over_the_held_result_after_the_run(improcess_rig, qtbot):
    rig = improcess_rig
    controller, widget = rig.controller, rig.widget
    assert widget.sendButton.isEnabled() is False

    _run_one_scan(rig, qtbot)

    assert widget.sendButton.isEnabled()
    assert controller.sendResultToImProcess() is True
    (result, name), = rig.module.sent
    assert result is controller._heldResult                  # the object itself crosses
    assert name.endswith('(CAM)')
    assert widget.statusText().startswith('Sent to ImProcess')


def test_send_to_improcess_says_so_when_improcess_is_not_loaded(qtbot):
    rig = _build_rig(qtbot, _FakeModuleChannel(registered=('imcontrol',)))
    try:
        _run_one_scan(rig, qtbot)

        assert rig.controller.sendResultToImProcess() is False
        assert rig.module.sent == []
        assert 'ImProcess is not loaded' in rig.widget.statusText()
    finally:
        rig.controller.closeEvent()


def test_send_without_a_result_or_a_module_channel_does_nothing(rig):
    assert rig.controller.sendResultToImProcess() is False
    assert 'No reconstruction' in rig.widget.statusText()


def test_send_during_a_run_hands_over_a_snapshot(improcess_rig, qtbot):
    rig = improcess_rig
    controller, detector, widget = rig.controller, rig.detector, rig.widget
    widget.setMode(MODE_FREE)
    widget.setFramesPerUpdate(2)
    controller.setLiveEnabled(True)
    detector.produce(_frame(1), _frame(2))
    qtbot.waitUntil(lambda: controller._heldResult is not None, timeout=8000)
    held = controller._heldResult

    assert controller.sendResultToImProcess() is True
    (sent, _name), = rig.module.sent
    assert sent is not held
    np.testing.assert_array_equal(np.asarray(sent.data), np.asarray(held.data))
    assert sent.result_uid != held.result_uid

    controller.setLiveEnabled(False)
    qtbot.waitUntil(lambda: not controller.hasActiveRun, timeout=8000)


# -- kept raw frames and the manual save ----------------------------------------------

def _text(value):
    return value.decode() if isinstance(value, bytes) else str(value)


def test_raw_frames_are_gone_after_a_run_unless_kept(rig, qtbot):
    _run_one_scan(rig, qtbot)

    assert rig.controller.rawStack is None
    assert 'raw frames' not in rig.widget.statusText()
    assert rig.widget.saveButton.isEnabled()                 # the result alone can be saved


def test_kept_raw_frames_and_the_result_are_saved_as_a_recording_and_a_result(rig, qtbot, tmp_path):
    controller, widget = rig.controller, rig.widget
    widget.setKeepRaw(True)

    _run_one_scan(rig, qtbot)

    stack = controller.rawStack
    assert stack is not None and stack.frames == 4 and stack.complete
    assert 'raw frames kept: 4' in widget.statusText()
    assert widget.saveButton.isEnabled()

    target = tmp_path / 'run_CAM.h5'
    widget.askForSavePath = lambda suggested: str(target)
    assert controller.saveRawAndResult() is True
    assert widget.saveButton.isEnabled() is False
    qtbot.waitUntil(lambda: controller._saveThread is None, timeout=20000)

    with h5py.File(target, 'r') as file:
        assert _text(file.attrs['rec_mode']) == 'recording'
        dataset = file['CAM/data']
        assert dataset.shape == (4, 4, 5)
        assert [int(plane[0, 0]) for plane in dataset[()]] == [1, 2, 3, 4]
        assert _text(dataset.attrs['recording:source_format']) == 'HDF5'
        assert int(dataset.attrs['recording:actual_frames']) == 4
        assert _text(dataset.attrs['recording:completion_outcome']) == 'complete'
        assert 'AcquisitionLayout:json' in dataset.attrs         # the scan's layout travels
        assert _text(file['CAM/metadata/ScanStage'].attrs['target_device']) == 'X'

    # The file is a recording to ImProcess, like one the recorder wrote.
    from imswitch.improcess.model import DataObj
    dataObj = DataObj('run_CAM.h5', 'CAM', path=str(target))
    dataObj.checkAndLoadData()
    assert dataObj.data.shape == (4, 4, 5)
    assert _text(dataObj.attrs['detector_name']) == 'CAM'
    dataObj.checkAndUnloadData()

    recon = sorted(tmp_path.glob('run_CAM_recon*'))
    assert len(recon) == 1 and recon[0].stat().st_size > 0
    assert widget.statusText() == f'Saved run_CAM.h5, {recon[0].name}'
    assert widget.saveButton.isEnabled()


def test_save_waits_for_the_run_and_a_cancelled_dialog_saves_nothing(rig, qtbot, tmp_path):
    controller, detector, widget = rig.controller, rig.detector, rig.widget
    widget.setMode(MODE_FREE)
    widget.setFramesPerUpdate(2)
    widget.setKeepRaw(True)
    controller.setLiveEnabled(True)
    detector.produce(_frame(1), _frame(2))
    qtbot.waitUntil(lambda: controller._heldResult is not None, timeout=8000)

    assert widget.saveButton.isEnabled() is False
    assert controller.saveRawAndResult() is False
    assert 'Wait for the run' in widget.statusText()

    controller.setLiveEnabled(False)
    qtbot.waitUntil(lambda: not controller.hasActiveRun, timeout=8000)
    assert controller.rawStack is not None and controller.rawStack.frames == 2

    widget.askForSavePath = lambda suggested: None
    assert controller.saveRawAndResult() is False
    assert list(tmp_path.iterdir()) == []


def test_clear_drops_the_kept_raw_frames_and_a_new_run_replaces_them(rig, qtbot):
    controller, widget = rig.controller, rig.widget
    widget.setKeepRaw(True)
    _run_one_scan(rig, qtbot)
    assert controller.rawStack is not None

    controller.clearResult()

    assert controller.rawStack is None
    assert widget.saveButton.isEnabled() is False
    assert widget.sendButton.isEnabled() is False


def test_keep_raw_is_part_of_the_component_state(rig):
    controller, widget = rig.controller, rig.widget
    widget.setKeepRaw(True)
    assert controller.getComponentState()['keepRaw'] is True

    widget.setKeepRaw(False)
    controller.applyComponentState(
        {'keepRaw': True}, applyMode=ComponentStateApplyMode.STARTUP_RESTORE
    )
    assert widget.getKeepRaw() is True


# -- MoNaLISA's pattern: each run's fresh localization is what is shown ---------------

def test_a_run_s_fresh_localization_is_shown_in_the_hosted_parameter_widget(rig):
    controller, widget = rig.controller, rig.widget
    paramWidget = Mock()
    controller._paramWidget = paramWidget

    controller._onLivePatternLocalized({
        'row_offset': 1.5, 'col_offset': 2.5, 'row_period': 10.0, 'col_period': 11.0,
        'source': 'auto',
    })

    paramWidget.set_pattern_params.assert_called_once_with(1.5, 2.5, 10.0, 11.0)
    assert widget.statusText().startswith('Pattern localized afresh on the first stack')

    paramWidget.set_pattern_params.reset_mock()
    controller._onLivePatternLocalized({'row_offset': None})
    paramWidget.set_pattern_params.assert_not_called()


def test_the_private_live_bus_relays_the_localized_pattern(rig):
    controller = rig.controller
    controller._liveController()
    shown = []
    controller._paramWidget = SimpleNamespace(set_pattern_params=lambda *v: shown.append(v))

    controller._bus.sigLivePatternLocalized.emit({
        'row_offset': 1.0, 'col_offset': 2.0, 'row_period': 11.0, 'col_period': 11.5,
        'source': 'auto',
    })

    assert shown == [(1.0, 2.0, 11.0, 11.5)]
