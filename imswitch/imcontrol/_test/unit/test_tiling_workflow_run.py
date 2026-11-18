"""Tiling run from a workflow: frozen settings, the caller's folder, the gate.

A timelapse of tilings runs one tiling per timepoint from its own worker
thread (``TilingController.runTilingForWorkflow``). These tests pin what that
caller relies on: the run saves where it is told, reports how it ended, and
its illumination gate is on exactly around each tile's frames -- never during
stitching or disk writes, and always switched off again.
"""

import threading
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcontrol.controller.controllers.TilingController import (
    TilingController,
    TilingRunResult,
    TilingRunSettings,
)
from imswitch.imcontrol.model.workflows.spiral import SPIRAL
from imswitch.imcontrol.model.workflows.tile_dataset import MANIFEST_NAME
from imswitch.imcontrol.view.widgets.TilingWidget import (
    MODE_FREE_RUNNING,
    MODE_TRIGGERED,
)

_SIGNALS = (
    'sigOverviewUpdated', 'sigProgressUpdated', 'sigRunningChanged',
    'sigShowCellMarkers', 'sigHighlightCell', 'sigCellTargetingEnabled',
    'sigRegistrationSummary',
)


class _Logger:
    def __init__(self):
        self.messages = []

    def _log(self, *a, **k):
        self.messages.append(a[0] if a else '')

    debug = info = warning = error = _log


class _Stage:
    def __init__(self):
        self.position = {'X': 0.0, 'Y': 0.0}

    def move(self, value, axis):
        self.position[axis] += value


class _Signal:
    def __init__(self, name, log):
        self._name = name
        self._log = log

    def emit(self, *args):
        self._log.append((self._name, args))


def _settings(**overrides):
    base = dict(
        n_tiles_x=2, n_tiles_y=2, pattern=SPIRAL, step_um=64.0,
        blend_overlaps=False, intensity_correction=False, settle_s=0.0,
        register_tiles=False, orientation=(False, False, False),
        save_tiles=False, mode=MODE_FREE_RUNNING, scan_source_key=None,
        detector_name=None,
    )
    base.update(overrides)
    return TilingRunSettings(**base)


class _Harness:
    """A TilingController with doubles, logging what happens in order."""

    def __init__(self, tmp_path, monkeypatch, frames=None):
        self.events = []
        self.emitted = []
        tile = np.arange(16 * 16, dtype=np.uint16).reshape(16, 16)
        frames = iter(frames) if frames is not None else None
        events = self.events

        class _Detector:
            name = 'CAM'
            pixelSizeUm = [1.0, 1.0, 1.0]
            binning = 1
            dtype = np.dtype(np.uint16)
            parameters = {}

            def getLatestFrameShared(self, is_save=False):
                events.append('frame')
                if frames is not None:
                    return next(frames)
                return tile

        detector = _Detector()

        class _Detectors:
            def acquire(self, names, purpose):
                return 'lease'

            def release(self, handle):
                pass

            def __getitem__(self, name):
                return detector

        ctrl = TilingController.__new__(TilingController)
        for name in _SIGNALS:
            ctrl.__dict__[name] = _Signal(name, self.emitted)
        ctrl._master = SimpleNamespace(
            positionersManager={'STAGE': _Stage()},
            detectorsManager=_Detectors(),
        )
        ctrl._setupInfo = SimpleNamespace(
            positioners={'STAGE': SimpleNamespace(axes=['X', 'Y'])},
            tiling=SimpleNamespace(
                xyPositioner='STAGE', camera='CAM', saveFormat='TIFF',
                measurementsRoot='',
            ),
        )
        ctrl._commChannel = SimpleNamespace(
            getRecordingFolder=lambda: str(tmp_path))
        ctrl._stopRequested = False
        ctrl._stitcher = None
        ctrl._originXY = None
        ctrl._gridPositions = []
        ctrl._scanAcqHandle = None
        ctrl._scanning = False
        ctrl._scanThread = None
        ctrl._closed = False
        ctrl._cellTargetingRunning = False
        ctrl._registrationReport = None
        ctrl._orientation = (False, False, False)
        ctrl._lastSaveFolder = None
        ctrl._lastRunResult = None
        ctrl._logger = _Logger()

        def saveTile(images, *args, **kwargs):
            events.append('save')

        ctrl._saveTile = saveTile
        ctrl._saveMosaic = lambda *a, **k: events.append('mosaic')
        monkeypatch.setattr(
            'imswitch.imcontrol.controller.controllers.TilingController'
            '.time.sleep', lambda _s: None,
        )
        self.ctrl = ctrl

    def run(self, settings, folder, gate=None):
        """Run from a worker thread, as a timelapse does."""
        box = {}

        def worker():
            try:
                box['result'] = self.ctrl.runTilingForWorkflow(
                    settings, folder, illuminationGate=gate)
            except BaseException as error:  # surfaced below
                box['error'] = error

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(30)
        assert not thread.is_alive()
        if 'error' in box:
            raise box['error']
        return box['result']


class _Gate:
    def __init__(self, events, *, failBefore=None, failAfter=None,
                 onAfter=None):
        self.events = events
        self.failBefore = failBefore
        self.failAfter = failAfter
        self.onAfter = onAfter
        self.before = []
        self.after = []

    def before_tile(self, index):
        self.before.append(index)
        self.events.append(f'on{index}')
        if index == self.failBefore:
            raise RuntimeError('laser refused')

    def after_tile(self, index):
        self.after.append(index)
        self.events.append(f'off{index}')
        if self.onAfter is not None:
            self.onAfter(index)
        if index == self.failAfter:
            raise RuntimeError('laser stuck')


@pytest.fixture
def harness(tmp_path, monkeypatch):
    return _Harness(tmp_path, monkeypatch)


def test_refused_on_the_gui_thread(harness, tmp_path):
    with pytest.raises(RuntimeError, match='worker thread'):
        harness.ctrl.runTilingForWorkflow(_settings(), tmp_path / 't0')


def test_refused_while_a_run_is_busy(harness, tmp_path):
    harness.ctrl._scanning = True
    result = harness.run(_settings(), tmp_path / 't0')
    assert isinstance(result, TilingRunResult)
    assert result.completed is False
    assert 'already in progress' in result.message
    assert harness.events == []


def test_refused_while_cell_targeting_owns_the_stage(harness, tmp_path):
    harness.ctrl._cellTargetingRunning = True
    result = harness.run(_settings(), tmp_path / 't0')
    assert result.completed is False
    assert 'Cell targeting' in result.message


def test_saves_into_the_given_folder_even_without_save_tiles(
        harness, tmp_path):
    folder = tmp_path / 'timelapse' / 't0'
    result = harness.run(_settings(save_tiles=False), folder)

    assert result.completed is True and result.stopped is False
    assert result.folder == str(folder)
    assert result.tiles_acquired == 4
    assert (folder / MANIFEST_NAME).is_file()
    assert harness.events.count('save') == 4
    # The claim is released for the next timepoint.
    assert harness.ctrl._scanning is False
    assert harness.ctrl._scanThread is None
    running = [args for name, args in harness.emitted
               if name == 'sigRunningChanged']
    assert running[0] == (True,) and running[-1] == (False,)


def test_the_gate_is_on_only_around_each_tiles_frames(harness, tmp_path):
    gate = _Gate(harness.events)
    result = harness.run(_settings(), tmp_path / 't0', gate)

    assert result.completed is True
    assert result.illumination_gated is True
    tileEvents = [e for e in harness.events if e != 'mosaic']
    # Per tile: light on, frame(s), light off, and only then the disk write.
    for i in range(4):
        on = tileEvents.index(f'on{i}')
        off = tileEvents.index(f'off{i}')
        between = tileEvents[on + 1:off]
        assert between and set(between) == {'frame'}
        assert tileEvents[off + 1] == 'save'
    assert gate.before == gate.after == [0, 1, 2, 3]


def test_before_tile_raising_still_switches_off_and_aborts(harness, tmp_path):
    gate = _Gate(harness.events, failBefore=1)
    result = harness.run(_settings(), tmp_path / 't0', gate)

    assert gate.before == [0, 1] and gate.after == [0, 1]
    assert result.completed is False
    assert 'laser refused' in result.message
    assert harness.ctrl._scanning is False


def test_after_tile_raising_stops_the_run(harness, tmp_path):
    gate = _Gate(harness.events, failAfter=0)
    result = harness.run(_settings(), tmp_path / 't0', gate)

    assert gate.before == [0] and gate.after == [0]
    assert result.completed is False
    assert 'laser stuck' in result.message


def test_stop_pairs_every_switch_on(harness, tmp_path):
    def stopAfterFirst(index):
        harness.ctrl._stopRequested = True

    gate = _Gate(harness.events, onAfter=stopAfterFirst)
    result = harness.run(_settings(), tmp_path / 't0', gate)

    assert gate.before == gate.after == [0]
    assert result.stopped is True and result.completed is False
    assert result.tiles_acquired == 1


def test_a_missing_frame_still_switches_off(tmp_path, monkeypatch):
    harness = _Harness(tmp_path, monkeypatch, frames=[None])
    gate = _Gate(harness.events)
    result = harness.run(_settings(), tmp_path / 't0', gate)

    assert gate.before == gate.after == [0]
    assert result.completed is False
    assert 'No frame' in result.message


def test_triggered_mode_ignores_the_gate(harness, tmp_path):
    class _TriggeredSource:
        needsSettleWait = True
        needsFreshFrame = False

        def prepare(self):
            pass

        def acquire(self):
            harness.events.append('frame')
            return np.zeros((16, 16), dtype=np.uint16), True

        def finish(self):
            pass

    harness.ctrl._makeTileSource = lambda *a, **k: _TriggeredSource()
    harness.ctrl._scanFrame = lambda detector: np.zeros((16, 16), np.uint16)
    gate = _Gate(harness.events)
    result = harness.run(_settings(mode=MODE_TRIGGERED), tmp_path / 't0', gate)

    assert gate.before == [] and gate.after == []
    assert result.illumination_gated is False
    assert result.completed is True


def test_start_tiling_still_runs_the_widget_snapshot_on_a_thread(
        harness, monkeypatch):
    class _Widget:
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            values = {
                'getNTilesX': 3, 'getNTilesY': 2, 'getPattern': SPIRAL,
                'getTileStepUm': 50.0, 'getBlendOverlaps': True,
                'getIntensityCorrection': False, 'getSettleTimeMs': 150.0,
                'getRegisterTiles': True,
                'getTileOrientation': (True, False, False),
                'getSaveTiles': True, 'getMode': MODE_FREE_RUNNING,
                'getScanSource': 'scanA', 'getDetector': 'CAM',
            }
            if name in values:
                return lambda: values[name]
            return lambda *a, **k: self.calls.append(name)

    started = {}

    class _Thread:
        def __init__(self, target, args, kwargs, daemon):
            started.update(target=target, args=args, kwargs=kwargs)

        def start(self):
            started['started'] = True

    monkeypatch.setattr(
        'imswitch.imcontrol.controller.controllers.TilingController'
        '.threading.Thread', _Thread,
    )
    harness.ctrl._widget = _Widget()
    harness.ctrl.startTiling()

    assert started['started'] is True
    assert started['args'][1:] == (3, 2, SPIRAL, 50.0, True, False)
    assert started['kwargs'] == dict(
        settle_s=0.15, register_tiles=True,
        orientation=(True, False, False), save_tiles=True,
        mode=MODE_FREE_RUNNING, scan_source_key='scanA', detector_name='CAM',
    )
    assert {'setRunning', 'setProgress', 'clearCellMarkers'} <= set(
        harness.ctrl._widget.calls)
