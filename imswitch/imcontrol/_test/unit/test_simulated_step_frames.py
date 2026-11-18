"""A simulated step board's camera pulses reach the mock cameras
(``scan_engines/simulated_frames.py``), as a simulated NI-DAQ's do: counted
from the run's log, handed to the cameras that are acquiring, spread over the
run's duration, the run reported over after the last one -- and a Stop
meanwhile stops it."""

import threading
import time
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.managers.scan_engines.loader import build_scan_engines
from imswitch.imcontrol.model.managers.scan_engines.simulated_frames import (
    MIN_DURATION_S, FrameDelivery, deliver, rising_edges,
)
from imswitch.imcontrol.model.managers.scan_engines.SimulatedStepEngine import (
    SimulatedStepEngine,
)
from imswitch.imcontrol.model.scan_program import Loop, ScanProgram, TimelineEvent
from imswitch.imcontrol.model.scan_program import legacy_microscope_gui as lm
from imswitch.imcontrol.model.scan_program.engine import DeviceChannels, setup_channels
from imswitch.imcontrol.view import ViewSetupInfo

SETUP = 'imswitch/_data/user_defaults/imcontrol_setups/nucleo_polarization_mock_setup.json'


class _Camera:
    def __init__(self, mock=True):
        self.frames = 0
        self.forAcquisition = True
        self._lock = threading.Lock()
        if not mock:
            self.mockTrigger = None

    def mockTrigger(self, n):
        with self._lock:
            self.frames += n


class _Detectors:
    def __init__(self, leased=True, **cameras):
        self.cameras, self.leased = cameras, leased

    def getAllDeviceNames(self):
        return list(self.cameras)

    def __getitem__(self, name):
        return self.cameras[name]

    def isDetectorLeased(self, name):
        return self.leased


class _FakeNidaq:
    sigScanBuilt = sigScanStarted = sigScanDone = sigScanBuildFailed = None


@pytest.fixture
def nucleo():
    setup = ViewSetupInfo.from_json(open(SETUP).read())
    engines = build_scan_engines(setup, nidaqManager=_FakeNidaq())
    yield engines.engines['nucleo'], setup_channels(setup), lm.Wiring.from_setup(setup)
    engines.finalize()


def _run(engine, channels, program, **kwargs):
    compiled = engine.compile(program, channels)
    engine.prepare(compiled, quiet=True)
    started = time.monotonic()
    engine.run(compiled, **kwargs)
    return started


def test_a_window_over_a_step_boundary_is_one_pulse():
    entries = [('on', 0, 0, 'Cam', 1), ('off', 0, 10, 'Cam', 0),   # step 1, to its end
               ('on', 0, 10, 'Cam', 1), ('off', 0, 15, 'Cam', 0),  # step 2 goes on: one pulse
               ('on', 0, 30, 'Cam', 1), ('off', 0, 35, 'Cam', 0),  # a second pulse
               ('on', 1, 0, 'Cam', 1), ('off', 1, 5, 'Cam', 0),    # after a wait: a third
               ('dac', 0, 0, 'X', 1.0), ('on', 0, 3, 'L', 1), ('off', 0, 4, 'L', 0)]
    assert rising_edges(entries) == {'Cam': 3, 'L': 1}


def test_frames_go_only_to_mock_cameras_that_are_acquiring():
    detectors = _Detectors(Cam=_Camera(), Real=_Camera(mock=False))
    delivery = FrameDelivery(detectors)
    assert delivery.armed({'Cam': 4, 'Real': 4, '488': 4}) == {'Cam': 4}
    detectors.leased = False
    assert delivery.armed({'Cam': 4}) == {}


def test_frames_are_spread_over_the_duration_and_a_stop_ends_them():
    calls, stop = [], threading.Event()
    started = time.monotonic()
    assert deliver({'Cam': 7}, 0.001, lambda name, n: calls.append(n), stop)
    assert sum(calls) == 7 and len(calls) > 1            # several hand-overs, not one burst
    assert time.monotonic() - started >= MIN_DURATION_S * 0.9
    calls.clear()
    threading.Timer(0.3, stop.set).start()
    assert not deliver({'Cam': 1000}, 10.0, lambda name, n: calls.append(n), stop)
    assert 0 < sum(calls) < 1000


def test_the_nucleo_mock_triggers_its_camera_once_per_frame(nucleo):
    engine, channels, wiring = nucleo
    camera = _Camera()
    engine.frameDelivery = FrameDelivery(_Detectors(**{wiring.camera: camera}))
    started = _run(engine, channels, lm.wide_field_2d(lm.LegacyParams(), wiring, pairs=1),
                   blocking=True)
    assert engine.outcome[0] == 'done'
    assert camera.frames == 3 + 2                         # dark frames, one pair
    assert time.monotonic() - started >= MIN_DURATION_S * 0.9


def test_a_camera_not_acquiring_gets_nothing_and_the_run_does_not_wait(nucleo):
    engine, channels, wiring = nucleo
    camera = _Camera()
    engine.frameDelivery = FrameDelivery(_Detectors(leased=False, **{wiring.camera: camera}))
    started = _run(engine, channels, lm.wide_field_2d(lm.LegacyParams(), wiring, pairs=1),
                   blocking=True)
    assert engine.outcome[0] == 'done' and camera.frames == 0
    assert time.monotonic() - started < MIN_DURATION_S


def test_a_stop_while_the_frames_are_handed_over_stops_the_run(nucleo):
    engine, channels, wiring = nucleo
    camera = _Camera()
    engine.frameDelivery = FrameDelivery(_Detectors(**{wiring.camera: camera}))
    # 288 frames over about 10 s of program: handed over in 5 s.
    _run(engine, channels, lm.line_scan_2d(lm.LegacyParams(), wiring, pairs=1))
    time.sleep(0.5)
    engine.abort()
    engine.join(5)
    assert engine.outcome[0] == 'stopped'
    assert 0 < camera.frames < 288


def test_the_in_process_simulated_board_triggers_its_camera_too():
    board = SimulatedStepEngine(SimpleNamespace(managerProperties={
        'timingGridS': 1e-6, 'digitalLines': ['S/ttl0']}), name='S')
    camera = _Camera()
    board.frameDelivery = FrameDelivery(_Detectors(Cam=camera))
    program = ScanProgram(loops=(Loop('t', 'time', 4),), drives=(), step_s=1e-3,
                          timeline=(TimelineEvent('Cam', 'ttl', 0.0, 0.2e-3, during='any'),))
    channels = {'Cam': DeviceChannels('Cam', digital='S/ttl0', kind='detector')}
    _run(board, channels, program, blocking=True)
    assert board.outcome[0] == 'done' and camera.frames == 4
