"""The Lifetime widget on the shipped FLIM mock setup: it constructs on the
setup's own Time Tagger block and detector, the Signals panel sees the
card's roles, and one mock scan fills the decay and a viewer layer."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcommon.framework import Signal, SignalInterface
from imswitch.imcontrol.controller.controllers.LifetimeController import LifetimeController
from imswitch.imcontrol.model.managers._scan_execution import PARTICIPANTS_KEY
from imswitch.imcontrol.model.managers.detectors.SwabianTimeTaggerManager import (
    SwabianTimeTaggerManager,
    _TTFlimWorker,
)
from imswitch.imcontrol.model.managers.TimeTaggerManager import TimeTaggerManager
from imswitch.imcontrol.model.SetupInfo import SetupInfo
from imswitch.imcontrol.view.widgets.LifetimeWidget import LifetimeWidget
from imswitch.imcontrol.view.widgets.basewidgets import WidgetFactory

pytestmark = pytest.mark.nohardware

SETUP = (Path(__file__).resolve().parents[3] / '_data' / 'user_defaults' / 'imcontrol_setups'
         / 'galvo_flim_mock_scan_setup.json')
SAMPLE_RATE = 100_000
DWELL, FLYBACK, NX, NY = 10, 50, 8, 4


class _Sig:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


class _Nidaq:
    isSimulated = True

    def __init__(self):
        self.sigScanBuilt, self.sigScanStarted, self.sigScanDone = _Sig(), _Sig(), _Sig()
        self.scanBuildFailures = []

    def reportScanBuildFailure(self, stage, error):
        self.scanBuildFailures.append((stage, error))


class _Comm(SignalInterface):
    sigUpsertStaticLayer = Signal(str, np.ndarray, object, object)
    sigRemoveStaticLayer = Signal(str)
    sigAbortScan = Signal()
    sigScanDone = Signal()

    def __init__(self):
        super().__init__()
        self.layers = {}
        self.sigUpsertStaticLayer.connect(lambda n, im, sc, opt: self.layers.__setitem__(n, (im, sc, opt)))
        self.sigRemoveStaticLayer.connect(lambda n: self.layers.pop(n, None))

    def getRecordingFolder(self):
        return None


class _Detectors:
    def __init__(self, items):
        self._items = dict(items)

    def getAllDeviceNames(self, condition=None):
        return list(self._items)

    def __getitem__(self, name):
        return self._items[name]


def _ttl():
    line = np.zeros(NX * DWELL + FLYBACK, dtype=bool)
    line[:10] = True
    frame = np.zeros(line.size * NY, dtype=bool)
    frame[:10] = True
    return {'line_clock': np.tile(line, NY), 'frame_start_clock': frame}


def test_the_widget_runs_on_the_shipped_flim_setup(qapp):
    raw = json.loads(SETUP.read_text(encoding='utf-8'))
    assert 'Lifetime' in raw['availableWidgets']
    setup = SetupInfo.from_json(SETUP.read_text(encoding='utf-8'), infer_missing=True)
    nidaq = _Nidaq()
    card = TimeTaggerManager(setup.timeTagger, setup, nidaq)
    assert card.connected and card.isMock
    detector = SwabianTimeTaggerManager(setup.detectors['FLIM'], 'FLIM', nidaq, timeTaggerManager=card)
    comm = _Comm()
    master = SimpleNamespace(detectorsManager=_Detectors({'FLIM': detector}), timeTaggerManager=card)
    widget = WidgetFactory(None).createWidget(LifetimeWidget)
    controller = LifetimeController(setupInfo=setup, commChannel=comm, master=master,
                                    widget=widget, factory=None, moduleCommChannel=None)
    try:
        assert widget.getDetector() == 'FLIM'
        assert widget.getSetting('rep_rate_mhz') == 80.0
        assert widget.sweepRoleCombo.count() == 5, "photons, sync, line, frame and STED-pulse roles"
        assert widget.preflightButton.isEnabled()

        # One mock scan through the detector's own worker.
        scan_info = {PARTICIPANTS_KEY: ['FLIM'], 'img_dims': [NX, NY], 'img_axes_phys': ['x', 'y'],
                     'pixel_sizes': [0.1, 0.1], 'dwell_time': DWELL / SAMPLE_RATE,
                     'n_linesteps': 1, 'tot_scan_time_s': 1.0}
        nidaq.sigScanBuilt.emit(scan_info, {'TTLCycleSignalsDict': _ttl()}, [])
        assert detector._preparedScanGeneration is not None, nidaq.scanBuildFailures
        generation = detector._preparedScanGeneration
        detector._activeScanGeneration = generation
        worker = _TTFlimWorker(detector, generation)
        worker.sigFrameReady.connect(detector._on_frame_ready)
        worker.signal_done()
        worker.run()

        assert 'FLIM › lifetime' in comm.layers
        image, scale, options = comm.layers['FLIM › lifetime']
        assert image.shape == (NY, NX) and image.max() > 0
        assert scale == [0.1, 0.1]
        assert 'photons' in widget._decayInfo.text()
        assert 'Valid pixels:' in widget.histStatLabel.text()
        assert not card.scanHeld, "released once the final frame landed"
    finally:
        controller.closeEvent()
