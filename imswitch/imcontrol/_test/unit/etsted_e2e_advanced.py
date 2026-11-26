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

from ._etsted_rig import EtSTEDRig, Session, WARMUP, same as _same

def baseline_settled(rig):
    """Detecting, with a baseline built from a few quiet frames since the last
    reset (the controller clears it after each triggered scan)."""
    exinfo = rig.etsted._exinfo
    return bool(rig.etsted._state.running and isinstance(exinfo, dict)
                and exinfo['n'] >= WARMUP + 4)


def pulse(rig, session, expected_armed):
    """Raise the event once the fast modality is detecting on a settled
    baseline, and drop it again once the scan it caused is armed (the baseline
    is rebuilt from scratch after each scan, so an event that stays on would
    simply become the new baseline)."""
    assert rig.wait_for(lambda: baseline_settled(rig), 90), \
        session.statuses
    session.event.on = True
    assert rig.wait_for(lambda: len(session.armed) >= expected_armed, 60), session.statuses
    session.event.on = False


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
    # ... and it is a scan centred on the event: (302, 202) px at 0.05 um/px
    # from the frame centre (256, 256), as the binned pixel is reported.
    centres = analog['axis_centerpos']
    assert centres[0] == pytest.approx(0.05 * (302 - 256), abs=1e-6)
    assert centres[1] == pytest.approx(0.05 * (202 - 256), abs=1e-6)


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
    pulse(rig, session, 1)
    assert rig.wait_for(lambda: len(session.apd_frames) >= 1, 40)
    assert rig.wait_for(lambda: session.statuses[-1] == 'detecting'
                        and rig.etsted._state.running, 40), session.statuses
    pulse(rig, session, 2)
    assert rig.wait_for(lambda: len(session.apd_frames) >= 2, 40)
    assert rig.etsted._experimentActive
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


def test_cooldown_ignores_events_that_follow_a_scan_too_closely(rig, new_session, caplog):
    session = new_session(endless=True, cooldown=3600)
    session.start_detecting()
    pulse(rig, session, 1)
    assert rig.wait_for(lambda: len(session.apd_frames) >= 1, 40)
    assert rig.wait_for(lambda: baseline_settled(rig), 90)
    with caplog.at_level('WARNING'):
        session.event.on = True
        assert rig.wait_for(
            lambda: any('cooldown' in r.getMessage() for r in caplog.records), 30)
    session.event.on = False
    assert len(session.armed) == 1
    assert rig.etsted._experimentActive


def test_the_maximum_number_of_scans_stops_the_run(rig, new_session):
    session = new_session(endless=True, max_scans=2)
    session.start_detecting()
    pulse(rig, session, 1)
    assert rig.wait_for(lambda: len(session.apd_frames) >= 1, 40)
    pulse(rig, session, 2)
    assert rig.wait_for(lambda: not rig.etsted._experimentActive, 60), session.statuses
    assert len(session.armed) == 2
    assert session.digital.get('488 (EXC)') is False


def test_neutral_safeguards_leave_the_behaviour_alone(rig, new_session):
    session = new_session(endless=True, cooldown=0, max_scans=0)
    session.start_detecting()
    for count in (1, 2, 3):
        pulse(rig, session, count)
        assert rig.wait_for(lambda: len(session.apd_frames) >= count, 40)


# ---- RecordingWidget initiation -------------------------------------------------

def test_recording_widget_initiation_says_up_front_when_the_recording_is_not_scan_once(rig, new_session):
    from imswitch.imcontrol.model import RecMode
    rig.main.controllers['Recording'].setRecMode(RecMode.SpecFrames)
    session = new_session()
    rig.select(rig.widget.scanInitiationPar, rig.widget.scanInitiation, 'RecordingWidget')
    try:
        rig.etsted.initiate()
        assert 'error' in session.statuses
        assert "Scan once" in rig.widget.statusMessageLabel.text()
        assert not rig.etsted._experimentActive
        assert session.armed == []
    finally:
        rig.select(rig.widget.scanInitiationPar, rig.widget.scanInitiation, 'ScanWidget')


def test_recording_widget_initiation_records_the_triggered_advanced_scan(rig, new_session, tmp_path):
    """The Recording widget on 'Scan once', the APD selected: the event moves
    the scan centre, starts the recording, and the recording holds the scan's
    frame (it discards frames until the scan's boundary signal, so an
    etSTED-triggered start that did not emit it would leave the file empty)."""
    import h5py
    from imswitch.imcontrol.model import RecMode
    recording = rig.main.controllers['Recording']
    recording.setRecMode(RecMode.ScanOnce)
    widget = recording._widget
    widget.setDetectorMode(-3)
    widget.setSelectedSpecificDetectors(['APD'])
    widget.setRecFolder(str(tmp_path / 'rec'))
    rig.process(0.3)
    session = new_session()
    rig.select(rig.widget.scanInitiationPar, rig.widget.scanInitiation, 'RecordingWidget')
    ended = []
    on_end = lambda *a: ended.append(1)
    rig.channel.sigScanEnded.connect(on_end)
    try:
        session.start_detecting()
        session.trigger()
        assert rig.wait_for(lambda: ended or 'error' in session.statuses, 60), session.statuses
        assert 'error' not in session.statuses
        assert rig.wait_for(lambda: not rig.etsted._experimentActive, 30)
        rig.wait_for(lambda: list((tmp_path / 'rec').glob('*APD.hdf5')), 20)
        files = list((tmp_path / 'rec').glob('*APD.hdf5'))
        assert len(files) == 1
        with h5py.File(files[0]) as handle:
            assert handle['APD/data'].shape == (1, 50, 50)
            assert int(handle['APD/stream_complete'][0]) == 1
            assert handle['APD/data'][...].max() > 0
        analog = rig.etsted._analogParameterDict
        assert analog['axis_centerpos'][0] == pytest.approx(0.05 * (302 - 256), abs=1e-6)
    finally:
        rig.channel.sigScanEnded.disconnect(on_end)
        rig.select(rig.widget.scanInitiationPar, rig.widget.scanInitiation, 'ScanWidget')
        recording.setRecMode(RecMode.SpecFrames)


def test_mock_rehearsal_switching_the_laser_on_triggers_a_scan_on_the_sample(rig, new_session):
    """The steps of docs/gui.rst 'etSTED first run': the laser has a power but
    is off, so the camera sees the dark; switching it on lights the virtual
    sample, a rise the dF/F pipeline reports, and the APD scans there."""
    session = new_session()
    rig.main.controllers['Laser'].toggleLaser('488 (EXC)', False)
    rig.process(0.3)
    session.start_detecting()
    assert baseline_settled(rig) or rig.wait_for(lambda: baseline_settled(rig), 60)
    assert not session.armed
    rig.main.controllers['Laser'].toggleLaser('488 (EXC)', True)
    assert rig.wait_for(lambda: session.armed, 40), session.statuses
    assert rig.wait_for(lambda: session.apd_frames, 40)
    centres = rig.etsted._analogParameterDict['axis_centerpos'][:2]
    assert all(abs(c) < 0.05 * 256 for c in centres)       # on the camera's field
