"""Tests for tiling acquisition modes: free-running vs triggered.

Triggered mode covers scanned detectors (APD/PMT) and a camera clocked by the
scan's trigger output with the same code, because both are ready at the same
event: scan completion.
"""

import threading
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcontrol.controller.CommunicationChannel import (
    SCAN_SOURCE_METHODS,
    _scanSourceMatches,
)
from imswitch.imcontrol.controller.controllers.TilingController import (
    TilingController,
    _FreeRunningTileSource,
    _TriggeredTileSource,
)
from imswitch.imcontrol.view.widgets.TilingWidget import (
    MODE_FREE_RUNNING,
    MODE_TRIGGERED,
)


class _Logger:
    def __init__(self):
        self.errors = []

    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, msg='', *a, **k):
        self.errors.append(str(msg))


class _Completion:
    def __init__(self, successful=True, finishes=True, message=''):
        self.successful = successful
        self.message = message
        self._finishes = finishes

    def wait(self, timeout=None):
        return self._finishes


class _Result:
    def __init__(self, accepted=True, completion=None,
                 rejection='refused by controller'):
        self.accepted = accepted
        self.rejectionMessage = rejection
        self.acceptedCompletions = (
            (('owner', 'token', completion),) if completion is not None else ()
        )


class _ScanWorkflow:
    def __init__(self, result=None):
        self.result = result
        self.calls = []
        self.aborts = []

    def run_scan_from(self, source, recalculate_signals,
                      is_non_final_part_of_sequence):
        self.calls.append((source, recalculate_signals))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def abort_scan_from(self, source, runToken=None):
        self.aborts.append(source)


def _controller(scanWorkflow=None, frame=None):
    ctrl = TilingController.__new__(TilingController)
    ctrl._logger = _Logger()
    ctrl._commChannel = SimpleNamespace(scanWorkflow=scanWorkflow)
    ctrl._setupInfo = SimpleNamespace(tiling=SimpleNamespace(scanTimeoutS=1.0))
    ctrl._stopRequested = False
    ctrl._closed = False
    return ctrl


class _ScanDetector:
    name = 'APD'
    isScanDriven = True
    pixelSizeUm = [1.0, 0.05, 0.05]

    def __init__(self, frame=None):
        self._frame = frame if frame is not None else np.ones((8, 8), np.uint16)

    def getLatestFrameShared(self):
        return self._frame


# ----------------------------------------------------------------------
# The narrower scan-source predicate
# ----------------------------------------------------------------------


def test_scan_source_predicate_is_weaker_than_the_recording_one():
    """Tiling only needs to run and abort a scan, not read its geometry."""
    from imswitch.imcontrol.controller.CommunicationChannel import (
        RECORDING_SCAN_SOURCE_METHODS,
    )
    assert set(SCAN_SOURCE_METHODS) < set(RECORDING_SCAN_SOURCE_METHODS)


def test_scan_source_predicate_accepts_a_runner_without_geometry():
    runnable = SimpleNamespace(
        runScanExternal=lambda *a: None, abortScan=lambda *a: None,
    )
    notRunnable = SimpleNamespace(abortScan=lambda *a: None)

    matches = _scanSourceMatches(
        {'Scan': runnable, 'Other': notRunnable}, SCAN_SOURCE_METHODS
    )

    assert [key for key, _ in matches] == ['Scan']


# ----------------------------------------------------------------------
# Source selection
# ----------------------------------------------------------------------


def test_free_running_mode_refuses_a_scan_driven_detector():
    """An APD produces nothing at all unless a scan is running."""
    ctrl = _controller()

    with pytest.raises(RuntimeError, match='scan-driven'):
        TilingController._makeTileSource(
            ctrl, MODE_FREE_RUNNING, _ScanDetector(),
            SimpleNamespace(xyPositioner='STAGE'), None,
        )


def test_free_running_mode_selects_the_camera_source():
    ctrl = _controller()
    camera = SimpleNamespace(name='CAM', isScanDriven=False)

    source = TilingController._makeTileSource(
        ctrl, MODE_FREE_RUNNING, camera,
        SimpleNamespace(xyPositioner='STAGE'), None,
    )

    assert isinstance(source, _FreeRunningTileSource)


def test_triggered_mode_selects_the_scan_source_for_a_camera_too():
    """A camera on the scan's trigger takes the same path as an APD."""
    scanSource = SimpleNamespace(
        getScanPositionerNames=lambda: ['GALVO_X', 'GALVO_Y'],
    )
    ctrl = _controller()
    ctrl._commChannel.getScanSource = lambda key=None: scanSource
    ctrl._setupInfo.positioners = {}

    source = TilingController._makeTileSource(
        ctrl, MODE_TRIGGERED, SimpleNamespace(name='CAM', isScanDriven=False),
        SimpleNamespace(xyPositioner='STAGE'), None,
    )

    assert isinstance(source, _TriggeredTileSource)


# ----------------------------------------------------------------------
# The stage-scan conflict
# ----------------------------------------------------------------------


def test_triggered_mode_refuses_a_scan_that_drives_the_tiling_stage():
    """Both would command the same positioner — refuse before touching it."""
    scanSource = SimpleNamespace(getScanPositionerNames=lambda: ['STAGE'])
    ctrl = _controller()
    ctrl._commChannel.getScanSource = lambda key=None: scanSource
    ctrl._setupInfo.positioners = {}

    with pytest.raises(RuntimeError, match='same one tiling steps'):
        TilingController._makeTileSource(
            ctrl, MODE_TRIGGERED, _ScanDetector(),
            SimpleNamespace(xyPositioner='STAGE'), None,
        )


def test_conflict_check_falls_back_to_the_setup_scanning_flags():
    """A controller that cannot name its devices still gets checked."""
    ctrl = _controller()
    ctrl._setupInfo.positioners = {
        'STAGE': SimpleNamespace(forScanning=True),
        'GALVO': SimpleNamespace(forScanning=True),
        'FOCUS': SimpleNamespace(forScanning=False),
    }

    names = TilingController._scanPositionerNames(ctrl, SimpleNamespace())

    assert names == {'STAGE', 'GALVO'}


def test_galvo_scan_over_a_stage_tiling_is_allowed():
    scanSource = SimpleNamespace(getScanPositionerNames=lambda: ['GALVO'])
    ctrl = _controller()
    ctrl._setupInfo.positioners = {'STAGE': SimpleNamespace(forScanning=False)}

    TilingController._assertScanDoesNotOwnTilingStage(
        ctrl, SimpleNamespace(xyPositioner='STAGE'), scanSource,
    )


# ----------------------------------------------------------------------
# Triggered acquisition
# ----------------------------------------------------------------------


def test_triggered_source_runs_a_scan_and_returns_its_frame():
    frame = np.arange(64, dtype=np.uint16).reshape(8, 8)
    workflow = _ScanWorkflow(_Result(completion=_Completion()))
    ctrl = _controller(workflow)
    source = _TriggeredTileSource(ctrl, _ScanDetector(frame), 'SRC')

    image, ok = source.acquire()

    assert ok is True
    np.testing.assert_array_equal(image, frame)
    assert len(workflow.calls) == 1


def test_triggered_source_recalculates_signals_only_for_the_first_tile():
    """Scan geometry is fixed across tiles; rebuilding it each time is latency."""
    workflow = _ScanWorkflow(_Result(completion=_Completion()))
    ctrl = _controller(workflow)
    source = _TriggeredTileSource(ctrl, _ScanDetector(), 'SRC')

    source.acquire()
    source.acquire()
    source.acquire()

    assert [recalc for _src, recalc in workflow.calls] == [True, False, False]


def test_triggered_source_reports_a_refused_scan():
    workflow = _ScanWorkflow(_Result(accepted=False, rejection='busy'))
    ctrl = _controller(workflow)
    source = _TriggeredTileSource(ctrl, _ScanDetector(), 'SRC')

    image, ok = source.acquire()

    assert image is None and ok is False
    assert any('busy' in e for e in ctrl._logger.errors)


def test_triggered_source_aborts_a_scan_that_never_finishes():
    workflow = _ScanWorkflow(
        _Result(completion=_Completion(finishes=False))
    )
    ctrl = _controller(workflow)
    source = _TriggeredTileSource(ctrl, _ScanDetector(), 'SRC')

    image, ok = source.acquire()

    assert image is None and ok is False
    assert workflow.aborts == ['SRC']


def test_triggered_source_reports_a_failed_scan():
    workflow = _ScanWorkflow(
        _Result(completion=_Completion(successful=False, message='TTL fault'))
    )
    ctrl = _controller(workflow)
    source = _TriggeredTileSource(ctrl, _ScanDetector(), 'SRC')

    image, ok = source.acquire()

    assert image is None and ok is False
    assert any('TTL fault' in e for e in ctrl._logger.errors)


def test_triggered_source_refuses_a_controller_with_no_completion_terminal():
    """Without an exact terminal, tile timing would be a guess."""
    workflow = _ScanWorkflow(_Result(completion=None))
    ctrl = _controller(workflow)
    source = _TriggeredTileSource(ctrl, _ScanDetector(), 'SRC')

    image, ok = source.acquire()

    assert image is None and ok is False
    assert any('completion terminal' in e for e in ctrl._logger.errors)


def test_triggered_source_survives_a_raising_scan_request():
    workflow = _ScanWorkflow(RuntimeError('hardware offline'))
    ctrl = _controller(workflow)
    source = _TriggeredTileSource(ctrl, _ScanDetector(), 'SRC')

    image, ok = source.acquire()

    assert image is None and ok is False


# ----------------------------------------------------------------------
# Scan frames
# ----------------------------------------------------------------------


@pytest.mark.parametrize('shape', [(8, 8), (1, 8, 8), (1, 1, 8, 8)])
def test_scan_frame_drops_the_leading_frame_wrapper(shape):
    """Scan detectors wrap their raster in a frame axis; that goes."""
    ctrl = _controller()
    detector = _ScanDetector(np.ones(shape, dtype=np.uint16))

    frame = TilingController._scanFrame(ctrl, detector)

    assert frame is not None
    assert frame.shape == (8, 8)


def test_scan_frame_keeps_a_real_z_stack():
    """A 3D scan's tile is the whole stack — dropping planes loses the data."""
    ctrl = _controller()
    detector = _ScanDetector(np.ones((1, 5, 8, 8), dtype=np.uint16))

    frame = TilingController._scanFrame(ctrl, detector)

    assert frame.shape == (5, 8, 8)


def test_display_plane_max_projects_a_stack():
    stack = np.zeros((4, 6, 6), dtype=np.uint16)
    stack[2, 3, 3] = 900  # only visible in one plane

    plane = TilingController._displayPlane(stack)

    assert plane.shape == (6, 6)
    assert plane[3, 3] == 900


def test_display_plane_passes_a_2d_tile_through():
    tile = np.ones((6, 6), dtype=np.uint16)

    assert TilingController._displayPlane(tile) is tile


@pytest.mark.parametrize('sizes, expected', [
    ([0.3, 0.05, 0.05], 0.3),
    ([0.0, 0.05, 0.05], 0.0),
    ([0.05, 0.05], 0.0),
])
def test_detector_z_step_comes_from_the_scan_calibration(sizes, expected):
    detector = SimpleNamespace(pixelSizeUm=sizes)

    assert TilingController._detectorZStepUm(detector) == expected


def test_scan_frame_returns_none_for_an_empty_read():
    ctrl = _controller()
    detector = _ScanDetector(np.empty((0, 0), dtype=np.uint16))

    assert TilingController._scanFrame(ctrl, detector) is None


def test_scan_frame_survives_a_raising_detector():
    class _Broken:
        def getLatestFrameShared(self):
            raise RuntimeError('detector offline')

    ctrl = _controller()
    assert TilingController._scanFrame(ctrl, _Broken()) is None


# ----------------------------------------------------------------------
# Pixel size
# ----------------------------------------------------------------------


def test_binning_is_not_applied_to_a_scan_driven_detector():
    """A scanned detector's pixel size IS the scan step — never scale it."""
    ctrl = TilingController.__new__(TilingController)
    ctrl._logger = _Logger()
    detector = SimpleNamespace(
        name='APD', pixelSizeUm=[1.0, 0.05, 0.05],
        isScanDriven=True, binning=4,
    )

    assert ctrl._detectorPixelSizeUm(detector) == (0.05, 0.05)


def test_binning_is_still_applied_to_a_camera():
    ctrl = TilingController.__new__(TilingController)
    ctrl._logger = _Logger()
    detector = SimpleNamespace(
        name='CAM', pixelSizeUm=[1.0, 0.5, 0.5],
        isScanDriven=False, binning=4,
    )

    assert ctrl._detectorPixelSizeUm(detector) == (2.0, 2.0)
