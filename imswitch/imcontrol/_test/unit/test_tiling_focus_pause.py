"""Tiling with "pause focus lock during moves": pause before each XY move,
relock before the tile, record the status per tile, carry on after a failed
relock, and never hang on Stop. Off by default."""
import json
import threading

import pytest

from imswitch.imcontrol.model.workflows.tile_dataset import MANIFEST_NAME
from imswitch.imcontrol.view.widgets.TilingWidget import MODE_TRIGGERED

from .test_tiling_workflow_run import _Harness, _settings


class _FocusLock:
    """Scripted focus lock: ``results`` are what each resume returns."""

    positioner = 'PIEZO'

    def __init__(self, events, results=(), state='locked', block=False):
        self.events = events
        self.results = list(results)
        self.state = state
        self.block = block
        self.cancelled = threading.Event()
        self.reason = ''

    def focusLockState(self):
        return self.state

    def pauseFocusLock(self):
        self.events.append('pause')
        if self.state in ('locked', 'holding'):
            self.state = 'paused'
        return self.state

    def relockTimeoutS(self):
        return 5.0

    def resumeFocusLock(self, settleS, timeoutS):
        self.events.append('resume')
        if self.block:
            assert self.cancelled.wait(5), 'resume was never cancelled'
            self.state = 'lost'
            return 'cancelled'
        result = self.results.pop(0) if self.results else 'locked'
        self.state = 'locked' if result == 'locked' else 'lost'
        self.reason = '' if result == 'locked' else 'excursion limit reached'
        return result

    def getFocusLockResumeReason(self):
        return self.reason

    def cancelFocusLockResume(self):
        self.cancelled.set()


@pytest.fixture
def harness(tmp_path, monkeypatch):
    return _Harness(tmp_path, monkeypatch)


def _attach(harness, focus):
    harness.ctrl._commChannel._focusLock = focus
    return focus


def test_off_by_default_nothing_is_paused(harness, tmp_path):
    focus = _attach(harness, _FocusLock(harness.events))
    result = harness.run(_settings(), tmp_path / 't0')
    assert result.completed
    assert 'pause' not in harness.events and 'resume' not in harness.events
    assert result.focus_lock_tiles == ()
    assert focus.state == 'locked'


def test_pause_before_each_move_and_relock_before_each_tile(harness, tmp_path):
    focus = _attach(harness, _FocusLock(harness.events))
    folder = tmp_path / 't0'
    result = harness.run(_settings(pause_focus_lock=True), folder)
    assert result.completed, result.message
    seq = [e for e in harness.events if e in ('pause', 'resume', 'frame')]
    # tile 1 at the start position; then pause, (move), resume, frame per tile;
    # then pause for the return to the origin and a final relock there.
    assert seq[:1] == ['frame']
    assert seq[1:4] == ['pause', 'resume', 'frame']
    assert seq[-2:] == ['pause', 'resume']
    assert [t['status'] for t in result.focus_lock_tiles] == ['locked'] * 4
    manifest = json.loads((folder / MANIFEST_NAME).read_text())
    assert [t['status'] for t in manifest['focus_lock_tiles']] == ['locked'] * 4
    assert focus.state == 'locked'


def test_failed_relock_marks_the_tile_and_the_next_tile_tries_again(harness, tmp_path):
    _attach(harness, _FocusLock(harness.events, results=['locked', 'failed', 'locked']))
    folder = tmp_path / 't0'
    result = harness.run(_settings(pause_focus_lock=True), folder)
    assert result.tiles_acquired == 4, 'the run continues after a failed relock'
    statuses = [t['status'] for t in result.focus_lock_tiles]
    assert statuses == ['locked', 'locked', 'relock-failed', 'locked']
    assert result.focus_lock_tiles[2]['reason'] == 'excursion limit reached'
    assert 'tile(s) [3]' in result.message
    manifest = json.loads((folder / MANIFEST_NAME).read_text())
    assert manifest['focus_lock_tiles'][2]['status'] == 'relock-failed'


def test_stop_during_a_relock_wait_does_not_hang(harness, tmp_path):
    focus = _attach(harness, _FocusLock(harness.events, block=True))
    folder = tmp_path / 't0'
    box = {}

    def run():
        box['result'] = harness.ctrl.runTilingForWorkflow(
            _settings(pause_focus_lock=True), folder)

    thread = threading.Thread(target=run)
    thread.start()
    for _ in range(500):
        if 'resume' in harness.events:
            break
        threading.Event().wait(0.01)
    harness.ctrl.stopTiling()
    thread.join(5)
    assert not thread.is_alive(), 'Stop did not hang on the focus relock'
    result = box['result']
    assert result.stopped and focus.cancelled.is_set()
    assert result.focus_lock_tiles[-1]['status'] == 'relock-cancelled'
    assert harness.ctrl._scanning is False


def test_unlocked_focus_lock_is_not_paused(harness, tmp_path):
    _attach(harness, _FocusLock(harness.events, state='unlocked'))
    result = harness.run(_settings(pause_focus_lock=True), tmp_path / 't0')
    assert result.completed
    assert 'pause' not in harness.events


def test_triggered_mode_leaves_it_to_the_scan_interlock(harness):
    focus = _attach(harness, _FocusLock(harness.events))
    assert harness.ctrl._focusPauser(True, MODE_TRIGGERED) is None
    assert harness.ctrl._focusPauser(False, 'free') is None
    assert harness.ctrl._focusPauser(True, 'free') is focus
