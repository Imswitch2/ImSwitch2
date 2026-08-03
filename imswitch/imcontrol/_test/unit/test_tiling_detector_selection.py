"""Choosing the tiling detector at runtime.

The detector used to come from ``tiling.camera`` alone, falling back to the
first acquisition detector. It is resolved once per run inside the worker, so
offering a per-run choice needs no lifecycle change -- but it does need three
things to hold: the selection is read before the worker starts, the setup file
stays the default, and an overview built with a different pixel size is not
left on screen for click-to-navigate to misread.
"""

from types import SimpleNamespace

import pytest

from imswitch.imcontrol.controller.controllers.TilingController import (
    TilingController,
)


class _Logger:
    def __init__(self):
        self.messages = []

    def info(self, message, *args, **kwargs):
        self.messages.append(message)

    def warning(self, message, *args, **kwargs):
        self.messages.append(message)

    def error(self, message, *args, **kwargs):  # pragma: no cover - unused
        pass


class _Widget:
    def __init__(self):
        self.offered = None
        self.selected = None
        self.cellMarkersCleared = 0
        self.cellTargetingEnabled = []

    def setDetectors(self, names):
        self.offered = list(names)

    def setDetector(self, name):
        self.selected = name

    def getDetector(self):
        return self.selected if (self.offered or []) and len(self.offered) > 1 else None

    def clearCellMarkers(self):
        self.cellMarkersCleared += 1

    def setCellTargetingEnabled(self, enabled):
        self.cellTargetingEnabled.append(enabled)


def _makeController(detectors):
    ctrl = TilingController.__new__(TilingController)
    ctrl._logger = _Logger()
    ctrl._widget = _Widget()
    ctrl._setupInfo = SimpleNamespace(detectors={
        name: SimpleNamespace(forAcquisition=acq)
        for name, acq in detectors.items()
    })
    ctrl._scanning = False
    ctrl._stitcher = None
    ctrl._originXY = None
    ctrl._gridPositions = []
    ctrl._cellPositionsRC = None
    ctrl._cellProps = None
    return ctrl


def test_only_acquisition_detectors_are_offered():
    """The focus-lock camera is a detector but never a tiling source."""
    ctrl = _makeController({'WFCam': True, 'APD': True, 'FocusLockCam': False})

    TilingController._populateDetectors(ctrl, '')

    assert ctrl._widget.offered == ['WFCam', 'APD']


def test_the_setup_file_supplies_the_default():
    ctrl = _makeController({'WFCam': True, 'APD': True})

    TilingController._populateDetectors(ctrl, 'APD')

    assert ctrl._widget.selected == 'APD'


def test_a_setup_value_that_is_not_selectable_is_ignored():
    ctrl = _makeController({'WFCam': True, 'APD': True})

    TilingController._populateDetectors(ctrl, 'NotADetector')

    assert ctrl._widget.selected is None


def test_a_single_detector_rig_is_never_asked_to_choose():
    ctrl = _makeController({'WFCam': True})

    TilingController._populateDetectors(ctrl, 'WFCam')

    assert ctrl._widget.offered == ['WFCam']
    assert ctrl._widget.getDetector() is None, (
        'with no choice on offer the setup file stays the only authority'
    )


def test_changing_detector_discards_the_overview():
    """Its pixel size no longer maps overview pixels to stage coordinates."""
    ctrl = _makeController({'WFCam': True, 'APD': True})
    ctrl._stitcher = object()
    ctrl._originXY = (1.0, 2.0)
    ctrl._gridPositions = [(0, 0)]

    TilingController._onDetectorChanged(ctrl)

    assert ctrl._stitcher is None
    assert ctrl._originXY is None
    assert ctrl._gridPositions == []
    assert ctrl._widget.cellMarkersCleared == 1
    assert ctrl._widget.cellTargetingEnabled == [False]
    assert ctrl._logger.messages


def test_a_change_mid_run_does_not_touch_the_running_mosaic():
    ctrl = _makeController({'WFCam': True, 'APD': True})
    stitcher = object()
    ctrl._stitcher = stitcher
    ctrl._scanning = True

    TilingController._onDetectorChanged(ctrl)

    assert ctrl._stitcher is stitcher


def test_the_selection_is_read_before_the_worker_starts():
    """It must not be re-read on the worker: the detector lease is taken
    against the resolved name and released in the finally, so a change
    mid-run would leak that lease and read a detector nobody armed."""
    import inspect

    source = inspect.getsource(TilingController.startTiling)
    assert 'self._widget.getDetector()' in source

    worker = inspect.getsource(TilingController._runScan)
    assert 'getDetector()' not in worker
