"""Tilt-compensation safety: limits that always apply, fit quality gates,
referencing, the return path, operator messages, reservations, and the
tiling-active flag for workflow runs."""
import json
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcontrol.model import resources
from imswitch.imcontrol.model.workflows.spiral import SPIRAL
from imswitch.imcontrol.model.workflows.stage_tilt_calibration import fit_plane
from imswitch.imcontrol.model.workflows.stage_tilt_compensation import (
    TiltPlane, TiltSafetyError, TiltSafetyLimits, load_tilt_plane, plan_compensation,
    return_path,
)
from imswitch.imcontrol.model.workflows.spiral import tile_moves

from .test_tiling_tilt_dual_actuator import rig  # noqa: F401  (fixture)


def _plane(path='p', sx=-0.004, sy=0.002, bounds=300.0, **kw):
    return TiltPlane(path=path, sha256='b' * 64, xy_positioner='Stage', focus_positioner='Z',
                     slope_x=sx, slope_y=sy, x_bounds=(-bounds, bounds),
                     y_bounds=(-bounds, bounds), rms_residual_um=0.05, **kw)


MOVES = list(tile_moves(3, 3, SPIRAL))


# ------------------------------------------------------------- B.1 limits

def test_plan_predicts_and_passes_within_limits():
    plan = plan_compensation(_plane(), moves=MOVES, step_um=100.0, limits=TiltSafetyLimits())
    assert plan.max_excursion_um == pytest.approx(0.6)
    assert len(plan.deltas_um) == 9


@pytest.mark.parametrize('plane,limits,match', [
    (_plane(sx=-0.02), TiltSafetyLimits(), 'slope'),
    (_plane(sx=-0.01, bounds=5000), TiltSafetyLimits(max_excursion_um=10.0), 'up to'),
    (_plane(sx=-0.01, bounds=5000), TiltSafetyLimits(max_step_um=2.0), 'step'),
])
def test_plan_refuses_over_limit(plane, limits, match):
    with pytest.raises(TiltSafetyError, match=match):
        plan_compensation(plane, moves=list(tile_moves(5, 5, SPIRAL)), step_um=500.0,
                          limits=limits)


def test_plan_refuses_extrapolation_with_and_without_an_origin():
    with pytest.raises(TiltSafetyError, match='larger than the calibrated'):
        plan_compensation(_plane(bounds=50), moves=MOVES, step_um=100.0,
                          limits=TiltSafetyLimits())
    with pytest.raises(ValueError, match='outside'):
        plan_compensation(_plane(bounds=150), moves=MOVES, step_um=100.0,
                          limits=TiltSafetyLimits(), origin_xy=(100.0, 0.0))


def test_steep_calibration_is_refused_before_anything_moves(rig):  # noqa: F811
    from .test_tiling_tilt_dual_actuator import _run as run_dual
    ctrl, xy, z, fine, source, folder = rig
    ctrl._widget = SimpleNamespace(getTiltLimits=lambda: dict(
        max_abs_slope_um_per_mm=5.0, max_excursion_um=30.0, max_step_um=5.0))
    run_dual(rig)          # slope -0.01 = 10 µm/mm > 5 µm/mm
    assert ctrl._logger.errors
    assert not xy.moves and not z.moves and source.count == 0


# ---------------------------------------------------------- B.3 reference

def test_unreferenced_z_is_refused_without_precheck(rig):  # noqa: F811
    from .test_tiling_tilt_dual_actuator import _run as run_dual
    ctrl, xy, z, fine, source, folder = rig
    z.requiresReference, z.isReferenced = True, False
    run_dual(rig, precheck=False)
    assert any('referenced' in str(e) for e in ctrl._logger.errors)
    assert not z.moves and not xy.moves


# -------------------------------------------------------- B.4 return path

def test_return_path_keeps_z_on_the_plane():
    plane = _plane(sx=-0.01, sy=0.004, bounds=5000)
    segments = return_path(plane, (400.0, -300.0), step_um=100.0, max_z_step_um=1.0)
    x, y = 400.0, -300.0
    previous = plane.delta_z(x, y)
    for dx, dy, dz in segments:
        assert np.hypot(dx, dy) <= 100.0 + 1e-9
        x, y = x + dx, y + dy
        assert dz == pytest.approx(plane.delta_z(x, y), abs=1e-9)
        assert abs(dz - previous) <= 1.0 + 1e-9
        previous = dz
    assert (x, y) == pytest.approx((0.0, 0.0)) and segments[-1][2] == 0.0


def test_run_returns_with_z_following_xy(rig):  # noqa: F811
    from .test_tiling_tilt_dual_actuator import _run as run_dual
    ctrl, xy, z, fine, source, folder = rig
    log = []
    original_move, original_set = xy.move, z.setPosition

    def move(value, axis):
        original_move(value, axis)
        log.append(('xy', dict(xy.position)))

    def set_position(value, axis):
        log.append(('z', value, dict(xy.position)))
        return original_set(value, axis)

    xy.move, z.setPosition = move, set_position
    run_dual(rig)
    assert not ctrl._logger.errors
    plane_sx, plane_sy = -.01, -.003
    returning = log[[i for i, e in enumerate(log) if e[0] == 'z'][8]:]  # after the last tile
    for entry in returning:
        if entry[0] == 'z':
            pos = entry[2]
            assert entry[1] == pytest.approx(plane_sx * pos['X'] + plane_sy * pos['Y'], abs=1e-6)
    assert z.position == pytest.approx(0.0)
    assert xy.position == pytest.approx({'X': 0.0, 'Y': 0.0})


def test_xy_return_failing_leaves_z_matched_and_says_so(rig):  # noqa: F811
    from .test_tiling_tilt_dual_actuator import _run as run_dual
    ctrl, xy, z, fine, source, folder = rig
    original_move = xy.move
    state = {'tiles': 0}

    def acquire():
        state['tiles'] += 1
        return np.ones((8, 8), dtype=np.uint16), True

    source.acquire = acquire

    def move(value, axis):
        if state['tiles'] >= 9:
            raise RuntimeError('stage stalled')
        original_move(value, axis)

    xy.move = move
    run_dual(rig)
    result = ctrl._lastRunResult
    assert 'Compensation Z was left' in result.message
    # Z matches the plane at the XY where the stage actually stopped
    assert z.position == pytest.approx(-.01 * xy.position['X'] - .003 * xy.position['Y'])


def test_lock_engaged_mid_run_on_the_shared_z_stops_and_reports(rig):  # noqa: F811
    ctrl, xy, z, fine, source, folder = rig
    calls = {'n': 0}

    def state():
        calls['n'] += 1
        return 'unlocked' if calls['n'] < 6 else 'locked'

    ctrl._commChannel._focusLock = SimpleNamespace(
        positioner='Z', focusLockState=state, focusCameraAcquisitionActive=lambda: False)
    plane = _plane(path=folder / 'tilt_fit.json', sx=-.01, sy=-.003)
    ctrl._runScan(SimpleNamespace(xyPositioner='Stage', camera='CAM'), n_tiles_x=3,
                  n_tiles_y=3, pattern=SPIRAL, step_um=100., blend_overlaps=False,
                  intensity_correction=False, settle_s=0., tilt_plane=plane)
    result = ctrl._lastRunResult
    assert not result.completed
    assert 'Focus feedback engaged' in result.message


# ------------------------------------------------------ B.6 no modal in workflow

def test_workflow_run_reports_in_the_result_not_a_popup(rig):  # noqa: F811
    from .test_tiling_tilt_dual_actuator import _run as run_dual
    ctrl, xy, z, fine, source, folder = rig
    emitted = []
    ctrl.__dict__['sigTiltCompensationError'] = SimpleNamespace(emit=emitted.append)
    for name in ('sigRunningChanged', 'sigProgressUpdated', 'sigOverviewUpdated'):
        ctrl.__dict__[name] = SimpleNamespace(emit=lambda *a: None)
    ctrl._closed = False
    ctrl._refreshTargets = lambda: None
    xy.move = lambda *a: (_ for _ in ()).throw(RuntimeError('stage stalled'))
    ctrl._workflowRun = True
    run_dual(rig)
    assert emitted == []
    assert ctrl._lastRunResult.message
    ctrl._workflowRun = False
    run_dual(rig)
    assert emitted, 'the GUI run still shows the popup'


# ------------------------------------------------------------ B.8 reservation

@pytest.fixture
def registry():
    fresh = resources.ResourceRegistry()
    previous = resources.set_resource_registry(fresh)
    yield fresh
    resources.set_resource_registry(previous)


def test_run_reserves_and_releases_its_positioners(rig, registry):  # noqa: F811
    from .test_tiling_tilt_dual_actuator import _run as run_dual
    ctrl, xy, z, fine, source, folder = rig
    seen = []
    source_acquire = source.acquire

    def acquire():
        seen.append(sorted(k for r in registry.reservations() for k in r.resources))
        return source_acquire()

    source.acquire = acquire
    run_dual(rig)
    assert seen and seen[0] == ['positioner:Stage', 'positioner:Z']
    assert registry.reservations() == []


def test_run_is_refused_cleanly_when_another_owner_holds_the_stage(rig, registry):  # noqa: F811
    from .test_tiling_tilt_dual_actuator import _run as run_dual
    ctrl, xy, z, fine, source, folder = rig
    held = registry.reserve(['positioner:Stage'], 'script', deadline_s=0.1)
    run_dual(rig)
    assert ctrl._logger.errors and not xy.moves and not z.moves
    assert 'reserve' in ctrl._lastRunResult.message.lower()
    registry.release(held.token)


# --------------------------------------------------------------- B.2 fit gates

def _samples(points):
    return [dict(tile_index=i, stage_x_um=x, stage_y_um=y, focus_z_um=z, valid_for_fit=True)
            for i, (x, y, z) in enumerate(points)]


def test_fit_rejects_an_outlier_and_reports_it():
    rng = np.random.default_rng(0)
    pts = [(x, y, -0.004 * x + 0.002 * y + rng.normal(0, 0.02))
           for x in (0, 100, 200, 300) for y in (0, 100, 200, 300)]
    pts[5] = (pts[5][0], pts[5][1], pts[5][2] + 4.0)     # dirt: 4 µm off
    fit = fit_plane(_samples(pts))
    assert fit['n_outliers_rejected'] == 1 and fit['outlier_tile_indices'] == [5]
    assert fit['slope_x_um_per_um'] == pytest.approx(-0.004, abs=2e-4)
    assert fit['quality_gate']['passed']


def test_noisy_fit_fails_the_gate():
    rng = np.random.default_rng(1)
    pts = [(x, y, rng.normal(0, 3.0)) for x in (0, 100, 200) for y in (0, 100, 200)]
    fit = fit_plane(_samples(pts))
    assert not fit['quality_gate']['passed']


def _write(tmp_path, **fit_over):
    fit = dict(model='plane', slope_x_um_per_um=-0.004, slope_y_um_per_um=0.002,
               rms_residual_um=0.05, max_abs_residual_um=0.1, n_fit_points=9,
               valid_xy_bounds_um={'x': [0, 400], 'y': [0, 400]})
    fit.update(fit_over)
    doc = dict(version=1, completed=True, fit_status='ok', xy_positioner='Stage',
               focus_positioner='Z', z_source='last_commanded_position_not_hardware_readback',
               created_utc=datetime.now(timezone.utc).isoformat(), setup_file_name='rig.json',
               fit=fit)
    path = tmp_path / 'tilt_fit.json'
    path.write_text(json.dumps(doc))
    return path


@pytest.mark.parametrize('over,match', [
    (dict(n_fit_points=3), 'fitted points'),
    (dict(rms_residual_um=2.0), 'RMS'),
    (dict(max_abs_residual_um=5.0), 'worst residual'),
    (dict(quality_gate={'passed': False, 'reasons': ['x']}), 'quality gate'),
])
def test_load_refuses_a_fit_failing_the_gate(tmp_path, over, match):
    with pytest.raises(TiltSafetyError, match=match):
        load_tilt_plane(_write(tmp_path, **over), xy_positioner='Stage')


def test_old_file_without_residuals_must_be_re_recorded(tmp_path):
    path = _write(tmp_path)
    doc = json.loads(path.read_text())
    del doc['fit']['max_abs_residual_um']
    path.write_text(json.dumps(doc))
    with pytest.raises(TiltSafetyError, match='re-record'):
        load_tilt_plane(path, xy_positioner='Stage')


# ------------------------------------------------------- B.5 foreign / stale

def test_foreign_or_old_calibration_needs_confirmation(tmp_path):
    plane = load_tilt_plane(_write(tmp_path), xy_positioner='Stage')
    limits = TiltSafetyLimits()
    assert plane.foreign_or_stale(setup_name='rig.json', limits=limits) == []
    assert plane.foreign_or_stale(setup_name='other.json', limits=limits)
    later = datetime.now(timezone.utc) + timedelta(days=45)
    assert 'days old' in ' '.join(plane.foreign_or_stale(
        setup_name='rig.json', limits=limits, now=later))
    assert 'RMS' in plane.summary() and 'µm/mm' in plane.summary()


# -------------------------------------------------- B.7 / B.9 run guards

def test_tiling_active_is_set_for_workflow_runs(tmp_path, monkeypatch):
    from .test_tiling_workflow_run import _Harness, _settings
    harness = _Harness(tmp_path, monkeypatch)
    seen = []
    harness.ctrl._commChannel._tilingActive = False
    detector = harness.ctrl._master.detectorsManager['CAM']
    original = type(detector).getLatestFrameShared

    def frame(self, is_save=False):
        seen.append(harness.ctrl._commChannel._tilingActive)
        return original(self, is_save)

    monkeypatch.setattr(type(detector), 'getLatestFrameShared', frame)
    harness.run(_settings(), tmp_path / 't0')
    assert seen and all(seen)
    assert harness.ctrl._commChannel._tilingActive is False


def test_a_run_that_keeps_nothing_is_refused(monkeypatch):
    from imswitch.imcontrol.controller.controllers import TilingController as module
    from .test_tiling_workflow_run import _settings
    ctrl = module.TilingController.__new__(module.TilingController)
    ctrl._setupInfo = SimpleNamespace(tiling=SimpleNamespace())
    ctrl._scanning = False
    ctrl._closed = False
    ctrl._stopRequested = False
    ctrl._scanThread = None
    ctrl._activityLock = threading.Lock()
    ctrl._getActivityLock = lambda: ctrl._activityLock
    ctrl._commChannel = SimpleNamespace(_tilingActive=False)
    logged, warned = [], []
    ctrl._logger = SimpleNamespace(error=lambda *a, **k: logged.append(a[0]))
    ctrl._widget = SimpleNamespace(setRunning=lambda r: None,
                                   getApplyTiltCalibration=lambda: False)
    ctrl.snapshotRunSettings = lambda: _settings(build_overview=False, save_tiles=False)
    monkeypatch.setattr(module.QtWidgets.QMessageBox, 'warning',
                        lambda *a: warned.append(a[2]))
    started = []
    monkeypatch.setattr(module.threading, 'Thread',
                        lambda *a, **k: started.append(1))
    module.TilingController.startTiling(ctrl)
    assert not started and warned and 'Nothing would be kept' in warned[0]
    assert ctrl._scanning is False
