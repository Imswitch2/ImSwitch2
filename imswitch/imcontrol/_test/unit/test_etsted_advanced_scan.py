"""etSTED's triggered scan on the REAL Advanced scan panel.

The other etSTED tests drive the controller with mock scan managers, which is
how an Advanced scan, whose designers return a different shape of signals
than the scan manager's generic design, went unnoticed. Here the whole
application runs on ``etsted_advanced_mock_setup.json`` (simulated NI-DAQ,
Advanced galvo scan, mock camera and APD on one virtual sample) and a bright
spot is injected into the mock camera's frames.
"""
import copy

import numpy as np
import pytest

from ._etsted_rig import EtSTEDRig, Session, same as _same

@pytest.fixture(scope='module')
def rig(tmp_path_factory):
    # One application per module: a second one booted in the same process
    # while the first is garbage-collected crashes Qt.
    coefficients = np.zeros(20)
    coefficients[7] = coefficients[18] = 5.0      # 5 um per pixel: far out of range
    rig = EtSTEDRig(tmp_path_factory.mktemp('etsted'), coefficients=coefficients)
    yield rig
    rig.close()


@pytest.fixture
def new_session(rig):
    sessions = []

    def make(**kwargs):
        sessions.append(Session(rig, **kwargs))
        return sessions[-1]

    yield make
    for session in sessions:
        session.close()


def test_the_triggered_scan_has_the_signals_the_advanced_panel_would_play(rig, new_session, monkeypatch):
    session = new_session()
    session.start_detecting()
    session.trigger()
    assert rig.wait_for(lambda: session.armed, 30), session.status()
    triggered_signals, triggered_info = session.armed[0]
    assert rig.wait_for(lambda: session.apd_frames and not rig.etsted._experimentActive, 30)

    # The panel, given the very parameters etSTED built from, plays the same
    # scan, bit for bit.
    analog = rig.etsted._analogParameterDict
    monkeypatch.setattr(rig.scan, '_analogParameterDict', copy.deepcopy(analog))
    monkeypatch.setattr(rig.scan, '_digitalParameterDict',
                        copy.deepcopy(rig.etsted._digitalParameterDict))
    monkeypatch.setattr(rig.scan, 'getParameters', lambda: None)
    panel_signals, panel_info = rig.scan._buildScanSignals()
    for info in (triggered_info, panel_info):
        info.pop('participants', None)
        info.pop('excludedDetectors', None)
    _same(triggered_signals, panel_signals)
    _same(triggered_info, panel_info)
    # ... and it is a scan centred on the event: (302, 202) px at 0.15 um/px
    # from the frame centre (256, 256), as the binned pixel is reported.
    centres = analog['axis_centerpos']
    assert centres[0] == pytest.approx(0.15 * (302 - 256), abs=1e-6)
    assert centres[1] == pytest.approx(0.15 * (202 - 256), abs=1e-6)


def test_event_to_scan_one_shot_stops_with_the_laser_off(rig, new_session):
    session = new_session()
    session.start_detecting()
    session.trigger()
    assert rig.wait_for(lambda: session.apd_frames, 30), session.status()
    assert rig.wait_for(lambda: not rig.etsted._experimentActive, 20)

    frame = session.apd_frames[-1]
    assert frame.shape == (50, 50) and frame.max() > 0
    assert len(session.armed) == 1
    assert session.digital.get('488 (EXC)') is False
    assert not rig.master.nidaqManager.busy
    assert rig.etsted._scanCoordinator.activeRunToken is None
    assert rig.widget.initiateButton.text() != 'Stop'


def test_endless_returns_to_detecting_and_scans_again(rig, new_session):
    session = new_session(endless=True)
    session.start_detecting()
    session.trigger()
    assert rig.wait_for(lambda: len(session.apd_frames) >= 2, 90), session.status()
    assert rig.etsted._experimentActive
    session.event.on = False
    assert rig.wait_for(lambda: session.status().lower().startswith('detecting')
                        and rig.etsted._state.running, 30), session.status()
    rig.etsted.stopExperiment(resetParams=True)
    rig.process(0.5)
    assert session.digital.get('488 (EXC)') is False
    assert not rig.master.nidaqManager.busy


def test_a_centre_outside_the_scanner_range_is_refused_and_the_panel_stays_usable(rig, new_session):
    # 5 um per pixel puts the event hundreds of um from the origin; the
    # galvo's +-17.5 um range refuses it in the Advanced scan designer.
    try:
        session = new_session()
        rig.select(rig.widget.transformCoefsPar, rig.widget.transformCoefs, 'rig')
        session.start_detecting()
        session.trigger()
        assert rig.wait_for(lambda: 'error' in session.statuses, 30), session.statuses
        rig.process(0.5)
        assert session.armed == []          # nothing reached the DAQ
        assert session.apd_frames == []
        assert session.digital.get('488 (EXC)') is False
        assert not rig.master.nidaqManager.busy
        assert rig.etsted._scanCoordinator.activeRunToken is None

        # the Advanced panel still runs its own scan
        rig.scan.runScan()
        assert rig.wait_for(lambda: session.apd_frames, 30)
        assert rig.wait_for(lambda: not rig.scan.isRunning, 30)
    finally:
        rig.select(rig.widget.transformCoefsPar, rig.widget.transformCoefs, 'mock_rehearsal_only')


def test_a_failed_arm_leaves_lasers_off_and_the_panel_usable(rig, new_session, monkeypatch):
    session = new_session()
    session.start_detecting()
    coordinator = rig.etsted._scanCoordinator

    def refuse(*args, **kwargs):
        raise RuntimeError('the DAQ refused')

    monkeypatch.setattr(coordinator, 'arm', refuse)
    session.trigger()
    assert rig.wait_for(lambda: 'error' in session.statuses, 30), session.statuses
    rig.process(0.5)
    assert session.apd_frames == []
    assert session.digital.get('488 (EXC)') is False
    assert coordinator.activeRunToken is None
    monkeypatch.undo()
    rig.scan.runScan()
    assert rig.wait_for(lambda: session.apd_frames, 30)
