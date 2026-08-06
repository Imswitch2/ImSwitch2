"""Choosing the tiling detector at runtime.

The detector used to come from ``tiling.camera`` alone, falling back to the
first acquisition detector. It is resolved once per run inside the worker, so
offering a per-run choice needs no lifecycle change -- but it does need three
things to hold: the selection is read before the worker starts, the setup file
stays the default, and an overview built with a different pixel size is not
left on screen for click-to-navigate to misread.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.controller.controllers.TilingController import (
    TilingController,
)
from imswitch.imcontrol.view.widgets.TilingWidget import MODE_TRIGGERED


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


def test_config_editor_exposes_the_required_detector_transform_map():
    template = (
        Path(__file__).parents[4]
        / 'utility_scripts/builtin_templates/sections/tiling.json'
    )
    fields = {
        field['key']: field
        for field in json.loads(template.read_text(encoding='utf-8'))['fields']
    }

    assert fields['camera']['label'] == 'Align on (opt.)'
    assert fields['detectorTransforms']['type'] == 'json'
    assert fields['detectorTransforms']['default'] == '{}'


@pytest.mark.parametrize('alignment, expected', [
    ('APD1', ['APD1', 'APD2']),
    ('APD2', ['APD2', 'APD1']),
])
def test_all_recording_detectors_join_when_registration_is_declared(
        alignment, expected):
    """Either registered APD can be the alignment detector for the run."""
    ctrl = _makeController({'APD1': True, 'APD2': True})
    ctrl._commChannel = SimpleNamespace(
        getRecordingDetectors=lambda: ['APD1', 'APD2'],
    )
    detector = lambda name: SimpleNamespace(  # noqa: E731
        name=name,
        pixelSizeUm=[1.0, 0.05, 0.05],
        shape=(64, 64),
        isScanDriven=True,
    )
    ctrl._master = SimpleNamespace(detectorsManager={
        'APD1': detector('APD1'),
        'APD2': detector('APD2'),
    })
    tiling = SimpleNamespace(detectorTransforms={
        'APD1': 'identity',
        'APD2': 'identity',
    })

    save_set = TilingController._resolveSaveSet(
        ctrl, tiling, alignment, MODE_TRIGGERED,
    )

    assert save_set == expected
    assert set(ctrl._saveSetTransforms) == {'APD1', 'APD2'}


def test_an_undeclared_recording_detector_is_still_saved():
    """Not knowing how to overlay two detectors is no reason to discard one.

    Writing pixels needs a stage position and nothing more. Whether two
    detectors can be laid over each other is answered offline, against a
    transform that may not exist yet — and of "saved it without knowing" and
    "did not save it", only the second cannot be undone.
    """
    ctrl = _makeController({'APD1': True, 'APD2': True})
    ctrl._commChannel = SimpleNamespace(
        getRecordingDetectors=lambda: ['APD1', 'APD2'],
    )
    detector = SimpleNamespace(
        name='APD',
        pixelSizeUm=[1.0, 0.05, 0.05],
        shape=(64, 64),
        isScanDriven=True,
    )
    ctrl._master = SimpleNamespace(detectorsManager={
        'APD1': detector,
        'APD2': detector,
    })

    save_set = TilingController._resolveSaveSet(
        ctrl,
        SimpleNamespace(detectorTransforms={}),
        'APD1',
        MODE_TRIGGERED,
    )

    assert save_set == ['APD1', 'APD2']
    # Saved, and recorded as unknown rather than as an assertion of identity:
    # a reader must be able to tell the absence of a claim from a claim.
    assert ctrl._saveSetTransforms['APD2']['kind'] == 'unknown'
    assert ctrl._saveSetTransforms['APD1']['kind'] == 'identity'
    # And it says so, because a silently unregistered channel is a trap of a
    # different kind: the operator should learn it now, not when an overlay
    # comes out misaligned.
    assert any('APD2' in message and 'unknown' in message
               for message in ctrl._logger.messages)
