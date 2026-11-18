"""The Timelapse controller against fake Laser, Recording and Tiling panels.

What is pinned: the light is on only for a released timepoint and always
switched off again; Recording gets an externally paced series with the
operator's numbers and the gate holds every point until it is due; a run is
cleaned up exactly once whatever ends it; a timelapse of tilings writes one
folder per timepoint plus a manifest.
"""

import json
import time
from types import SimpleNamespace

import numpy as np
import pytest

import imswitch.imcontrol.controller.controllers.TimelapseController as module
from imswitch.imcontrol.controller.controllers.TilingController import (
    TilingRunResult,
)
from imswitch.imcontrol.controller.controllers.TimelapseController import (
    TimelapseController,
)
from imswitch.imcontrol.model.SetupInfo import TimelapseInfo
from imswitch.imcontrol.view.widgets.TilingWidget import (
    MODE_FREE_RUNNING,
    MODE_TRIGGERED,
)
from imswitch.imcontrol.view.widgets.TimelapseWidget import (
    KIND_CAMERA,
    KIND_SCAN,
    KIND_TILING,
    MODE_END_TO_START,
    TimelapseWidget,
)


class _Events(list):
    def lasers(self):
        return [e for e in self if e[0] == 'laser']


class _LaserManager:
    def __init__(self, name, binary=False):
        self.name = name
        self.isBinary = binary
        self.wavelength = 488 if not binary else None
        self.valueUnits = 'mW'
        self.valueRangeMin = 0
        self.valueRangeMax = 100
        self.valueRangeStep = 1
        self.valueDecimals = 1


class _Lasers:
    def __init__(self, names):
        # 'Gate' is the usual TTL placeholder whose power lives on a partner
        # device ('AOTF'); like 'LED' it has no setpoint of its own.
        self._m = {n: _LaserManager(n, binary=(n in ('LED', 'Gate')))
                   for n in names}

    def __iter__(self):
        return iter(self._m.items())

    def __getitem__(self, name):
        return self._m[name]


class _LaserController:
    def __init__(self, events, powerDevices=None):
        self.events = events
        self.active = {}
        self.powers = {'L488': 10.0, 'AOTF': 50.0}
        self.powerDevices = powerDevices or {}

    def setLaserActive(self, name, active):
        self.active[name] = bool(active)
        self.events.append(('laser', name, bool(active)))

    def getLaserPowerSetting(self, name, context='live'):
        return self.powers.get(name, 0.0)

    def setLaserPowerSetting(self, name, value, context='live'):
        self.powers[name] = value
        self.events.append(('power', name, value))

    def resolveLiveIlluminationLasers(self, names):
        out = []
        for n in names:
            out.append(n)
            if n in self.powerDevices:
                out.append(self.powerDevices[n])
        return tuple(out)


class _Recording:
    def __init__(self, events, accept=True):
        self.events = events
        self.accept = accept
        self.provider = None
        self.terminal = None
        self.calls = []
        self.stops = []
        self.cleared = 0
        self.recording = False
        self.onEnded = None

    def setPositioningProvider(self, provider, partitionKind='position'):
        self.provider = provider
        self.partitionKind = partitionKind

    def setCycleTerminalCallback(self, callback):
        self.terminal = callback

    def clearPositioningProvider(self):
        self.provider = None
        self.terminal = None
        self.cleared += 1

    def getActiveSeriesInfo(self):
        return None

    def getRecFileFormat(self):
        return 'HDF5'

    def _answer(self, kind, args, kwargs):
        self.calls.append((kind, args, kwargs))
        self.onEnded = kwargs.get('onEnded')
        if self.accept:
            return SimpleNamespace(accepted=True, reason='')
        return SimpleNamespace(accepted=False, reason='writer busy')

    def recordCameraSeries(self, *args, **kwargs):
        return self._answer('camera', args, kwargs)

    def recordScanSeries(self, *args, **kwargs):
        return self._answer('scan', args, kwargs)

    def stopSeries(self, owner):
        self.stops.append(owner)
        return True


class _Tiling:
    def __init__(self, events, mode=MODE_FREE_RUNNING, results=None):
        self.events = events
        self.mode = mode
        self.results = list(results or [])
        self.runs = []
        self.stopped = 0

    def snapshotRunSettings(self):
        return SimpleNamespace(mode=self.mode)

    def runTilingForWorkflow(self, settings, folder, *, illuminationGate=None):
        self.runs.append((folder, illuminationGate))
        if illuminationGate is not None:
            illuminationGate.before_tile(0)
            self.events.append(('tile', folder.name))
            illuminationGate.after_tile(0)
        if self.results:
            return self.results.pop(0)
        return TilingRunResult(True, False, '', str(folder), 1, 0,
                               illuminationGate is not None)

    def stopTiling(self):
        self.stopped += 1


class _Detectors:
    def __init__(self, scanDriven=()):
        self._d = {
            name: SimpleNamespace(name=name, forAcquisition=True,
                                  isScanDriven=name in scanDriven)
            for name in ('CamA', 'CamB', 'APD')
        }
        self.leases = []
        self.released = []

    def execOnAll(self, fn, condition=lambda c: True):
        return {n: fn(d) for n, d in self._d.items() if condition(d)}

    def __getitem__(self, name):
        return self._d[name]

    def acquire(self, names, purpose):
        self.leases.append((tuple(names), purpose))
        return 'lease-1'

    def release(self, handle):
        self.released.append(handle)


class _Harness:
    def __init__(self, qtbot, tmp_path, monkeypatch, *, scanSources=(),
                 tiling=None, accept=True, fresh=True, info=None):
        self.events = _Events()
        self.laser = _LaserController(self.events, {'Gate': 'AOTF'})
        self.recording = _Recording(self.events, accept=accept)
        self.tiling = tiling
        self.detectors = _Detectors(scanDriven=('APD',))
        self.fresh = fresh
        events = self.events

        def grab(detector, key, **kwargs):
            events.append(('frames', detector.name))
            if self.fresh:
                return np.zeros((2, 2)), True
            return None, False

        monkeypatch.setattr(module, 'grab_fresh_frame', grab)
        registry = {'Laser': self.laser, 'Recording': self.recording}
        if tiling is not None:
            registry['Tiling'] = tiling
        self.scanSource = SimpleNamespace(name='scanA')
        commChannel = SimpleNamespace(
            controllerRegistry=lambda: registry,
            getRecordingScanSourceNames=lambda: list(scanSources),
            getRecordingScanSource=lambda key=None: self.scanSource,
            getRecordingFolder=lambda: str(tmp_path),
            scanWorkflow=SimpleNamespace(laser_scan_state=lambda: {}),
        )
        master = SimpleNamespace(
            lasersManager=_Lasers(['L488', 'LED', 'Gate', 'AOTF']),
            detectorsManager=self.detectors,
        )
        setupInfo = SimpleNamespace(
            timelapse=info,
            tiling=SimpleNamespace() if tiling is not None else None,
        )
        self.widget = TimelapseWidget(None)
        qtbot.addWidget(self.widget)
        self.ctrl = TimelapseController(
            setupInfo, commChannel, master, self.widget, None, None)
        self.qtbot = qtbot

    def configureCamera(self, *, lasers=('L488',), timepoints=3,
                        interval=0.0, mode=None):
        w = self.widget
        w.setKind(KIND_CAMERA)
        w.setTimepoints(timepoints)
        w.setIntervalS(interval)
        if mode:
            w.setIntervalMode(mode)
        for name in lasers:
            w.setLaserChecked(name, True)

    def releasePoint(self, index, timeout=5000):
        request = self.recording.provider(index)
        self.qtbot.waitUntil(lambda: request.settled, timeout=timeout)
        return request


@pytest.fixture
def make(qtbot, tmp_path, monkeypatch):
    return lambda **kw: _Harness(qtbot, tmp_path, monkeypatch, **kw)


# ----------------------------------------------------------------------
# What is offered
# ----------------------------------------------------------------------

def _kinds(widget):
    return [widget.kindCombo.itemData(i)
            for i in range(widget.kindCombo.count())]


def test_camera_only_without_scan_sources_or_tiling(make):
    h = make()
    assert _kinds(h.widget) == [KIND_CAMERA]


def test_scan_and_tiling_are_offered_when_present(make):
    h = make(scanSources=['scanA'], tiling=_Tiling(_Events()))
    assert _kinds(h.widget) == [KIND_CAMERA, KIND_SCAN, KIND_TILING]


def test_defaults_come_from_the_setup_section(make):
    info = TimelapseInfo(defaultTimepoints=42, defaultIntervalS=7.5,
                         defaultIntervalMode=MODE_END_TO_START,
                         illuminationSettleMs=20.0)
    h = make(info=info)
    assert h.widget.getTimepoints() == 42
    assert h.widget.getIntervalS() == 7.5
    assert h.widget.getIntervalMode() == MODE_END_TO_START
    assert h.widget.getIlluminationSettleMs() == 20.0


def test_camera_kind_disables_scan_driven_detectors(make):
    h = make()
    assert not h.widget._detectorBoxes['APD'].isEnabled()
    assert h.widget._detectorBoxes['CamA'].isEnabled()
    assert h.widget.getDetectors() == ['CamA']


def test_scan_kind_shows_lasers_read_only(make):
    h = make(scanSources=['scanA'])
    h.widget.setKind(KIND_SCAN)
    assert not h.widget._laserRows['L488'][0].isEnabled()
    assert 'scan program decides' in h.widget.laserNoteLabel.text()
    assert 'does not publish' in h.widget.laserNoteLabel.text()
    assert not h.widget.framesPerPointSpinbox.isEnabled()


def test_triggered_tiling_shows_lasers_read_only(make):
    h = make(tiling=_Tiling(_Events(), mode=MODE_TRIGGERED))
    h.widget.setKind(KIND_TILING)
    assert not h.widget._laserRows['L488'][0].isEnabled()
    assert not h.widget._detectorBoxes['CamA'].isEnabled()
    assert not h.widget.singleFileCheck.isEnabled()


# ----------------------------------------------------------------------
# Starting a camera timelapse
# ----------------------------------------------------------------------

def test_invalid_settings_do_not_start(make):
    h = make()
    h.widget._detectorBoxes['CamA'].setChecked(False)
    h.ctrl.startTimelapse()
    assert h.recording.calls == []
    assert h.ctrl._run is None
    assert 'detector' in h.widget.statusLabel.text()
    assert h.widget.startButton.isEnabled()


def test_start_darkens_sets_power_leases_and_hands_recording_a_paced_series(
        make):
    h = make()
    h.configureCamera(timepoints=5, interval=2.0)
    h.widget.setLaserPower('L488', 33.0)
    h.widget.setFramesPerPoint(2)
    h.ctrl.startTimelapse()

    offs = [e for e in h.events.lasers() if e[2] is False]
    assert {e[1] for e in offs} == {'L488', 'LED', 'Gate', 'AOTF'}
    assert not any(e[2] for e in h.events.lasers()), 'nothing on yet'
    assert ('power', 'L488', 33.0) in h.events
    assert h.detectors.leases[0][0] == ('CamA',)
    assert h.recording.provider is not None
    assert h.recording.partitionKind == 'time'
    kind, args, kwargs = h.recording.calls[0]
    assert kind == 'camera'
    assert args == (['CamA'], 5, 2.0, True)
    assert kwargs['framesPerPoint'] == 2
    assert kwargs['externallyPaced'] is True
    assert kwargs['owner'] is h.ctrl
    assert h.widget.stopButton.isEnabled()
    h.ctrl.closeEvent()


def test_a_refusal_undoes_everything(make):
    h = make(accept=False)
    h.configureCamera()
    h.widget.setLaserPower('L488', 33.0)
    h.ctrl.startTimelapse()

    assert h.ctrl._run is None
    assert h.recording.cleared == 1
    assert h.detectors.released == ['lease-1']
    assert h.laser.powers['L488'] == 10.0, 'power restored'
    assert 'writer busy' in h.widget.statusLabel.text()
    assert h.widget.startButton.isEnabled()


# ----------------------------------------------------------------------
# The gate
# ----------------------------------------------------------------------

def test_a_point_is_lit_proven_and_released(make):
    h = make()
    h.configureCamera(lasers=('Gate',))
    h.ctrl.startTimelapse()
    request = h.releasePoint(0)

    assert request.mayProceed
    sequence = [e for e in h.events if e[0] in ('laser', 'frames')
                and (e[0] == 'frames' or e[2])]
    assert sequence == [('laser', 'Gate', True), ('laser', 'AOTF', True),
                        ('frames', 'CamA')], (
        'the gate and its power device go on before the frame proof'
    )
    assert request.attributes['timelapse:index'] == 0
    assert request.attributes['timelapse:total'] == 3
    assert json.loads(request.attributes['timelapse:lasers']) == {'Gate': None}
    h.ctrl.closeEvent()


def test_the_terminal_switches_the_light_off(make):
    h = make()
    h.configureCamera()
    h.ctrl.startTimelapse()
    h.releasePoint(0)
    assert h.laser.active['L488'] is True
    h.recording.terminal()
    assert h.laser.active['L488'] is False
    h.ctrl.closeEvent()


def test_no_fresh_frame_fails_the_point_and_switches_off(make):
    h = make(fresh=False)
    h.configureCamera()
    h.ctrl.startTimelapse()
    request = h.releasePoint(0)

    assert request.outcome.value == 'failed'
    assert 'no new frame' in request.message
    assert h.laser.active['L488'] is False
    h.ctrl.closeEvent()


def test_start_to_start_cadence_holds_the_next_point(make):
    h = make()
    h.configureCamera(interval=0.4)
    h.ctrl.startTimelapse()
    first = h.releasePoint(0)
    released0 = time.monotonic()
    h.recording.terminal()
    second = h.recording.provider(1)
    h.qtbot.wait(100)
    assert not second.settled, 'held until due'
    h.qtbot.waitUntil(lambda: second.settled, timeout=5000)
    assert second.mayProceed and first.mayProceed
    assert time.monotonic() - released0 >= 0.35
    h.ctrl.closeEvent()


def test_start_to_start_is_anchored_at_switch_on_not_at_release(
        make, monkeypatch):
    """The frame proof takes time after the light goes on; counting the
    period from the release would add that time to every interval."""
    h = make()
    fast = module.grab_fresh_frame

    def slowProof(detector, key, **kwargs):
        time.sleep(0.5)
        return fast(detector, key, **kwargs)

    monkeypatch.setattr(module, 'grab_fresh_frame', slowProof)
    switchOns = []
    setActive = h.laser.setLaserActive

    def timed(name, active):
        if active:
            switchOns.append(time.monotonic())
        setActive(name, active)

    h.laser.setLaserActive = timed
    h.configureCamera(interval=0.5)
    h.ctrl.startTimelapse()
    h.releasePoint(0)
    h.recording.terminal()
    h.releasePoint(1)
    gap = switchOns[1] - switchOns[0]
    assert 0.45 <= gap < 0.8, f'{gap:.3f} s between switch-ons'
    h.ctrl.closeEvent()


def test_end_to_start_waits_after_the_previous_end(make):
    h = make()
    h.configureCamera(interval=0.3, mode=MODE_END_TO_START)
    h.ctrl.startTimelapse()
    h.releasePoint(0)
    h.qtbot.wait(300)  # a long point
    ended = time.monotonic()
    h.recording.terminal()
    second = h.recording.provider(1)
    h.qtbot.waitUntil(lambda: second.settled, timeout=5000)
    assert time.monotonic() - ended >= 0.25
    h.ctrl.closeEvent()


def test_progress_counts_points_recording_moved_past(make):
    h = make()
    h.configureCamera(timepoints=3)
    h.ctrl.startTimelapse()
    h.releasePoint(0)
    h.recording.terminal()
    h.releasePoint(1)
    assert h.ctrl.getTimelapseStatus()['done'] == 1
    h.recording.terminal()
    h.releasePoint(2)
    h.recording.terminal()
    h.recording.onEnded(False, '')
    assert h.widget.progressBar.value() == 3
    assert h.widget.statusLabel.text() == 'Done: 3/3 timepoints.'
    assert h.ctrl._run is None


# ----------------------------------------------------------------------
# Ending
# ----------------------------------------------------------------------

def test_cleanup_happens_exactly_once(make):
    h = make()
    h.configureCamera()
    h.widget.setLaserPower('L488', 33.0)
    h.ctrl.startTimelapse()
    run = h.ctrl._run
    h.recording.onEnded(True, 'disk full')
    h.recording.onEnded(True, 'disk full')

    assert run.cleanedUp
    assert h.recording.cleared == 1
    assert h.detectors.released == ['lease-1']
    assert h.laser.powers['L488'] == 10.0
    assert not any(h.laser.active.values())
    assert 'disk full' in h.widget.statusLabel.text()
    assert h.widget.statusLabel.styleSheet() != ''


def test_stop_between_points_cancels_the_held_point(make):
    h = make()
    h.configureCamera(interval=30.0)
    h.ctrl.startTimelapse()
    h.releasePoint(0)
    h.recording.terminal()
    held = h.recording.provider(1)
    h.qtbot.wait(50)
    h.ctrl.stopTimelapse()

    assert h.recording.stops == [h.ctrl]
    h.qtbot.waitUntil(lambda: held.settled, timeout=5000)
    assert held.outcome.value == 'cancelled'
    h.recording.onEnded(False, '')
    assert h.widget.statusLabel.text().startswith('Stopped after 1/3')
    assert not any(h.laser.active.values())


def test_stop_during_a_point_leaves_it_running(make):
    h = make()
    h.configureCamera()
    h.ctrl.startTimelapse()
    h.releasePoint(0)
    h.ctrl.stopTimelapse()
    assert h.laser.active['L488'] is True, 'the point is still being recorded'
    h.recording.terminal()
    assert h.laser.active['L488'] is False
    h.recording.onEnded(False, '')
    assert h.ctrl._run is None


# ----------------------------------------------------------------------
# Scan and tiling
# ----------------------------------------------------------------------

def test_a_scan_timelapse_takes_no_lasers_and_switches_none_on(make):
    h = make(scanSources=['scanA'])
    h.widget.setLaserChecked('L488', True)
    h.widget.setKind(KIND_SCAN)
    h.widget.setTimepoints(2)
    h.ctrl.startTimelapse()
    kind, args, kwargs = h.recording.calls[0]
    assert kind == 'scan' and args[0] is h.scanSource
    assert kwargs['externallyPaced'] is True
    request = h.releasePoint(0)
    assert request.mayProceed
    assert not any(e[2] for e in h.events.lasers())
    assert ('frames', 'CamA') not in h.events
    h.ctrl.stopTimelapse()
    assert h.recording.stops == [h.scanSource]
    h.recording.onEnded(False, '')


def test_one_scan_needs_no_gate_and_counts_as_done(make):
    h = make(scanSources=['scanA'])
    h.widget.setKind(KIND_SCAN)
    h.widget.setTimepoints(1)
    h.ctrl.startTimelapse()
    assert h.recording.calls[0][2]['externallyPaced'] is False
    h.recording.terminal()
    h.recording.onEnded(False, '')
    assert h.widget.statusLabel.text() == 'Done: 1/1 timepoints.'


def test_a_timelapse_of_tilings_writes_a_folder_per_timepoint(make, tmp_path):
    tiling = _Tiling(_Events())
    h = make(tiling=tiling)
    tiling.events = h.events
    h.widget.setKind(KIND_TILING)
    h.widget.setTimepoints(3)
    h.widget.setIntervalS(0.0)
    h.widget.setLaserChecked('L488', True)
    h.ctrl.startTimelapse()
    h.qtbot.waitUntil(lambda: h.ctrl._run is None, timeout=10000)

    folders = [folder for folder, _gate in tiling.runs]
    assert [f.name for f in folders] == ['t0', 't1', 't2']
    root = folders[0].parent
    assert root.parent == tmp_path and root.name.startswith('timelapse_')
    manifest = json.loads((root / 'timelapse.json').read_text())
    assert [p['folder'] for p in manifest['points']] == ['t0', 't1', 't2']
    assert manifest['lasers'] == {'L488': 10.0}
    # Lit only around each tile.
    seq = [e for e in h.events if e[0] == 'tile' or (e[0] == 'laser' and e[2])]
    assert seq[:2] == [('laser', 'L488', True), ('tile', 't0')]
    assert not any(h.laser.active.values())
    assert h.widget.statusLabel.text() == 'Done: 3/3 timepoints.'


def test_triggered_tiling_gets_no_gate(make):
    tiling = _Tiling(_Events(), mode=MODE_TRIGGERED)
    h = make(tiling=tiling)
    h.widget.setKind(KIND_TILING)
    h.widget.setTimepoints(1)
    h.ctrl.startTimelapse()
    h.qtbot.waitUntil(lambda: h.ctrl._run is None, timeout=10000)
    assert tiling.runs[0][1] is None


def test_a_failed_tiling_stops_the_timelapse(make):
    failed = TilingRunResult(False, False, 'stage fault', None, 2, 0, False)
    tiling = _Tiling(_Events(), results=[failed])
    h = make(tiling=tiling)
    h.widget.setKind(KIND_TILING)
    h.widget.setTimepoints(3)
    h.ctrl.startTimelapse()
    h.qtbot.waitUntil(lambda: h.ctrl._run is None, timeout=10000)
    assert len(tiling.runs) == 1
    assert 'stage fault' in h.widget.statusLabel.text()


def test_stopping_a_timelapse_of_tilings_between_timepoints(make):
    tiling = _Tiling(_Events())
    h = make(tiling=tiling)
    h.widget.setKind(KIND_TILING)
    h.widget.setTimepoints(3)
    h.widget.setIntervalS(30.0)
    h.ctrl.startTimelapse()
    h.qtbot.waitUntil(lambda: len(tiling.runs) == 1, timeout=10000)
    h.ctrl.stopTimelapse()
    h.qtbot.waitUntil(lambda: h.ctrl._run is None, timeout=10000)
    assert len(tiling.runs) == 1
    assert tiling.stopped == 1
    assert h.widget.statusLabel.text().startswith('Stopped after 1/3')
