"""LiveReconController: ImProcess reconstructors over the acquisition stream.

The detector, its manager and the communication channel are doubles that
implement the parts the controller touches (lease bookkeeping, the chunk
consumer fan-out, the scan lifecycle signals and geometry accessors); the
reconstructors, the live runtime and its worker threads are the real ones.
"""

from types import SimpleNamespace
from unittest.mock import Mock, patch

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


@pytest.fixture
def rig(qtbot):
    detector = _FakeDetector()
    manager = _FakeDetectorsManager(detector)
    channel = _FakeCommChannel(positions=4, layout=_layout(2, 2))
    widget = LiveReconWidget(SimpleNamespace())
    qtbot.addWidget(widget)
    controller = LiveReconController(
        _setup(), channel, SimpleNamespace(detectorsManager=manager),
        widget=widget, factory=None, moduleCommChannel=None,
    )
    yield SimpleNamespace(detector=detector, manager=manager, channel=channel,
                          widget=widget, controller=controller)
    controller.closeEvent()


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
