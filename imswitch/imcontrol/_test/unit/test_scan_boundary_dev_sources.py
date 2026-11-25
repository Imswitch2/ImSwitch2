"""The scan frame boundary (``beadRecBoundary``) on the scan sources that
exist only on dev: the Expert panel on each of its scan engines.

PR #95 lets BeadRec and a Scan once / Scan lapse recording set their frame
boundary at the signal a scan source declares: ``'built'`` where sigScanBuilt
fires after the detectors are leased and before the first camera trigger,
``'started'`` where sigScanBuilt comes too early (the TriggerScope uploads its
parameters after it). There is no fallback release, so a source may declare
``'built'`` only if every start path emits sigScanBuilt.

The Expert panel is a SuperScanController and so declares ``'built'``. That is
right on every engine it runs on, and these tests hold each engine to it:

* NI-DAQ: the panel's sampled signals go through NidaqManager.runScan, which
  emits sigScanBuilt once the tasks are built, before it starts them.
* step board (StepProtocolScanEngine), simulated step engine, a leader with a
  follower (CompositeScanEngine), pulse generator: ``prepare`` uploads the
  program and then emits sigScanBuilt; ``run`` (RUN, or the play thread)
  follows and emits sigScanStarted. Both are called by the scan coordinator's
  starter after the detector lease, on every iteration.

Frames are index coded as in test_beadrec_frame_alignment: the k-th scan
frame is the constant k, a frame that is not the scan's is 1000.
"""
import copy

import numpy as np
import pytest
from qtpy import QtTest

from imswitch.imcontrol._test import optionsBasic
from imswitch.imcontrol.controller.basecontrollers import SuperScanController
from imswitch.imcontrol.controller.controllers.RecordingController import RecordingController
from imswitch.imcontrol.controller.controllers.ScanControllerExpert import ScanControllerExpert
from imswitch.imcontrol.view.widgets.RecordingWidget import RecordingWidget

from .test_beadrec_frame_alignment import IndexFrames
from .test_recording_scan_boundary import KEY, STALE, _record
from .test_scan_controller_expert import STEP_BOARD, ExpertRig, _ms_resolft, _setup_dict

#: cycles x planes of _ms_resolft with one timepoint: one camera frame each.
FRAMES = 2 * 3


def _board_setup():
    setup = _setup_dict(engine='board')
    setup['scanEngines'] = {'board': copy.deepcopy(STEP_BOARD)}
    setup['positioners']['Z']['analogChannel'] = 'board/dac0'
    for laser, line in zip(list(setup['lasers'])[:2], ('board/ttl0', 'board/ttl1')):
        setup['lasers'][laser]['digitalLine'] = line
    setup['detectors']['Camera']['digitalLine'] = 'board/ttl2'
    return setup


def _composite_setup():
    setup = _setup_dict(engine='lead')
    setup['scanEngines'] = {
        'lead': {'managerName': 'SimulatedStepEngine', 'managerProperties': {
            'timingGridS': 1e-5, 'digitalLines': ['lead/ttl0', 'lead/ttl3'],
            'clockAccuracyPpm': 20.0}},
        'follow': {'managerName': 'StepProtocolScanEngine', 'triggerInput': 'lead/ttl3',
                   'managerProperties': {'port': 'simulated', 'simulatedBoard': {
                       'gridNs': 10_000, 'dacs': {'0': [0, 10]}, 'ttlLines': 2,
                       'triggerIn': True, 'triggerLatencyTicks': 2, 'triggerMinTicks': 1,
                       'clockPpm': 20.0}}}}
    setup['positioners']['Z']['analogChannel'] = 'follow/dac0'
    first, second = list(setup['lasers'])[:2]
    setup['lasers'][first]['digitalLine'] = 'lead/ttl0'
    setup['lasers'][second]['digitalLine'] = 'follow/ttl0'
    setup['detectors']['Camera']['digitalLine'] = 'follow/ttl1'
    return setup


ENGINES = {
    'nidaq': lambda: _setup_dict(),
    'board': _board_setup,
    'composite': _composite_setup,
}


def test_each_scan_source_declares_its_boundary():
    from imswitch.imcontrol.controller.controllers import resolve_controller

    (LightSheetMulticolorController, ScanControllerAdvanced, ScanControllerBase,
     ScanControllerMoNaLISA, ScanControllerPointScan, ScanControllerSimpleMoNaLISA,
     ScanControllerSimplePointScan, TriggerScopeRasterController,
     TriggerScopeScanController) = (resolve_controller(name) for name in (
        'LightSheetMulticolorController', 'ScanControllerAdvanced', 'ScanControllerBase',
        'ScanControllerMoNaLISA', 'ScanControllerPointScan', 'ScanControllerSimpleMoNaLISA',
        'ScanControllerSimplePointScan', 'TriggerScopeRasterController',
        'TriggerScopeScanController'))
    for source in (ScanControllerExpert, ScanControllerAdvanced, ScanControllerBase,
                   ScanControllerMoNaLISA, ScanControllerPointScan,
                   ScanControllerSimpleMoNaLISA, ScanControllerSimplePointScan):
        assert issubclass(source, SuperScanController)
        assert source.beadRecBoundary == 'built', source
    # The TriggerScope family uploads its parameters after sigScanBuilt.
    for source in (TriggerScopeRasterController, TriggerScopeScanController,
                   LightSheetMulticolorController):
        assert source.beadRecBoundary == 'started', source


@pytest.mark.parametrize('engine', sorted(ENGINES))
def test_every_expert_iteration_is_built_after_the_lease_and_before_it_starts(qtbot, engine):
    rig = ExpertRig(ENGINES[engine]())
    try:
        events = []
        coordinator = rig.scan._scanCoordinator

        def leased():
            token = coordinator.tokenForOwner(rig.scan)
            return token is not None and getattr(token, 'leaseHandle', None) is not None

        rig.channel.sigScanBuilt.connect(
            lambda *a: events.append(('built', leased(),
                                      rig.channel.getActiveScanSource() is rig.scan)))
        rig.channel.sigScanStarted.connect(lambda *a: events.append(('started',)))
        rig.scan.setProgram(_ms_resolft(rig, timepoints=1))
        assert rig.widget.reportText().startswith(f'Runnable on {"lead" if engine == "composite" else engine}.'), \
            rig.widget.reportText()
        for _ in range(2):                       # two runs back to back
            assert rig.run(timeoutS=15.0) and rig.rejected == []
        assert [e[0] for e in events] == ['built', 'started', 'built', 'started'], events
        # leased and announced as the active source when the boundary is set
        assert all(e[1] and e[2] for e in events if e[0] == 'built'), events
    finally:
        rig.close()


def test_a_pulse_generator_run_is_built_before_it_starts(qtbot):
    setup = _setup_dict(engine='teensy')
    setup['teensyPulse'] = {'port': None, 'useMockOnFailure': True}
    setup['scanEngines'] = {'teensy': {'managerName': 'PulseGeneratorScanEngine',
                                       'managerProperties': {}}}
    setup['lasers']['Pulsed'] = {'managerName': 'PulseGeneratorLaserManager',
                                 'managerProperties': {}, 'digitalLine': 2,
                                 'wavelength': 405, 'valueRangeMin': 0, 'valueRangeMax': 1}
    rig = ExpertRig(setup)
    try:
        from imswitch.imcontrol.model.scan_program import presets

        events = []
        rig.channel.sigScanBuilt.connect(lambda *a: events.append('built'))
        rig.channel.sigScanStarted.connect(lambda *a: events.append('started'))
        rig.scan.setProgram(presets.make('pulse-sequence', dict(
            pulses=[['Pulsed', 1e-3, 2e-3]], camera='', exposure_pulse=1, frames=3,
            pause_s=0.0)))
        assert rig.run(timeoutS=10.0)
        assert events == ['built', 'started']
    finally:
        rig.close()


class _RecordingRig(ExpertRig):
    """An Expert rig with index-coded camera frames and a Recording panel,
    shaped like test_beadrec_frame_alignment.BeadRig for ``_record``."""

    def __init__(self, setup, triggerOnStart):
        super().__init__(setup)
        self.master.detectorsManager.setCurrentDetector('Camera')
        self.camera = self.master.detectorsManager['Camera']._camera
        self.source = IndexFrames()
        self.camera.mock_frame_source = self.source
        self.rec = self.factory.createController(RecordingController,
                                                 RecordingWidget(optionsBasic))
        self.main.controllers['Recording'] = self.rec
        if triggerOnStart:
            # A step engine does not pulse the mock camera: its frames are
            # fired when the run starts, the earliest a real trigger can come.
            self.channel.sigScanStarted.connect(lambda *a: self.camera.mockTrigger(FRAMES))

    def trigger(self, *special):
        self.source.special.extend(special)
        self.camera.mockTrigger(1)


@pytest.mark.parametrize('engine', ['nidaq', 'board'])
def test_an_expert_recording_drops_a_frame_from_before_the_boundary(qtbot, engine):
    rig = _RecordingRig(ENGINES[engine](), triggerOnStart=engine != 'nidaq')
    try:
        rig.scan.setProgram(_ms_resolft(rig, timepoints=1))
        assert rig.rec._scanBoundaryForRecording() is None   # not armed yet
        rig.channel.sigScanDevicesResolved.connect(lambda *a: rig.trigger(STALE))
        frames, attrs = _record(rig, rig.rec)
        assert frames == list(range(1, FRAMES + 1))
        assert int(attrs[KEY]) == 1
        assert np.all(np.asarray(frames) < STALE)
    finally:
        rig.close()


@pytest.mark.parametrize('engine', ['nidaq', 'board'])
def test_an_expert_recording_keeps_every_scan_frame(qtbot, engine):
    rig = _RecordingRig(ENGINES[engine](), triggerOnStart=engine != 'nidaq')
    try:
        rig.scan.setProgram(_ms_resolft(rig, timepoints=1))
        frames, attrs = _record(rig, rig.rec)
        assert frames == list(range(1, FRAMES + 1))
        assert int(attrs[KEY]) == 0
    finally:
        rig.close()


def test_the_test_rigs_run_where_they_say(qtbot):
    """Guards the parametrisation: each setup's program runs on that engine."""
    for engine, make in ENGINES.items():
        rig = ExpertRig(make())
        try:
            rig.scan.setProgram(_ms_resolft(rig, timepoints=1))
            expected = 'lead' if engine == 'composite' else engine
            assert rig.scan._choice(rig.scan._program).engine.name == expected
        finally:
            rig.close()
