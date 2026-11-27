"""Pure, hardware-independent regression tests for the experimental tilt trace."""

import csv
import json
from pathlib import Path

import numpy as np
import pytest

from imswitch.imcontrol.model.workflows.stage_tilt_calibration import (
    StageTiltRecorder, fit_plane, validate_sample,
)


def _focus(z, **overrides):
    return {
        'focus_z_um': z,
        'focus_signal_px': 40.1,
        'focus_setpoint_px': 40.0,
        'focus_error_px': 0.1,
        'measurement_age_s': 0.02,
        'max_measurement_age_s': 1.0,
        'lock_state': 'locked',
        'positioner_available': True,
        'dac_voltage_v': 2.0,
        'dac_voltage_clamped': False,
        **overrides,
    }


def test_records_every_tile_and_fits_real_xy_coordinates(tmp_path):
    recorder = StageTiltRecorder(tmp_path, step_um=50, settle_s=2.0,
                                 focus_positioner='FocusPiezoDAC',
                                 xy_positioner='XY', n_tiles=25)
    k = 0
    for ix in range(-2, 3):
        for iy in range(-2, 3):
            k += 1
            x, y = ix * 50.0 + 200, iy * 50.0 + 100
            z = 5 + 0.007 * (x - 200) - 0.003 * (y - 100)
            recorder.record(tile_index=k, gx=ix, gy=iy, x_um=x, y_um=y,
                            focus=_focus(z))
    result = recorder.finish(completed=True)
    fit = result['fit']
    assert fit['slope_x_um_per_um'] == pytest.approx(.007, abs=1e-12)
    assert fit['slope_y_um_per_um'] == pytest.approx(-.003, abs=1e-12)
    assert fit['rms_residual_um'] < 1e-10
    assert result['n_valid_tiles'] == 25
    assert result['completed'] is True
    assert result['z_source'] == 'positioner_get_abs_software_command_or_physical_readback'
    with (tmp_path / 'tilt_measurements.csv').open(newline='') as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 25
    assert rows[0]['grid_x'] == '-2'
    assert rows[0]['focus_z_um'] != ''
    assert json.loads((tmp_path / 'tilt_fit.json').read_text())['fit'] == fit
    assert (tmp_path / 'tilt_fit.png').stat().st_size > 3000


@pytest.mark.parametrize('overrides,reason', [
    ({'lock_state': 'unlocked'}, 'focus_not_locked'),
    ({'positioner_available': False}, 'positioner_unavailable'),
    ({'focus_error_px': None}, 'missing_or_nonfinite_measurement'),
    ({'focus_z_um': np.nan}, 'missing_or_nonfinite_measurement'),
    ({'measurement_age_s': 2.0}, 'stale_focus_measurement'),
    ({'dac_voltage_clamped': True}, 'dac_voltage_clamped'),
])
def test_invalid_samples_kept_for_diagnostics_but_not_fit(overrides, reason):
    sample = {'stage_x_um': 1, 'stage_y_um': 2, **_focus(3)}
    sample.update(overrides)
    assert validate_sample(sample) == reason


def test_partial_run_saves_csv_and_fit_when_enough_points(tmp_path):
    rec = StageTiltRecorder(tmp_path, step_um=100, settle_s=3,
                            focus_positioner='DAC', xy_positioner='XY', n_tiles=9)
    for i, (x, y, z) in enumerate(((0, 0, 1), (100, 0, 2),
                                  (0, 100, 3), (100, 100, 4)), start=1):
        rec.record(tile_index=i, gx=int(x / 100), gy=int(y / 100),
                   x_um=x, y_um=y, focus=_focus(z))
    rec.record(tile_index=5, gx=2, gy=2, x_um=200, y_um=200,
               focus=_focus(999, lock_state='unlocked'))
    result = rec.finish(completed=False)
    assert result['completed'] is False
    assert result['n_recorded_tiles'] == 5
    assert result['n_valid_tiles'] == 4
    assert result['rejected_tiles'] == [{'tile_index': 5, 'reason': 'focus_not_locked'}]
    assert result['fit']['slope_x_um_per_um'] == pytest.approx(.01)
    assert result['fit']['slope_y_um_per_um'] == pytest.approx(.02)


def test_collinear_or_too_few_valid_points_do_not_produce_plane(tmp_path):
    rec = StageTiltRecorder(tmp_path, step_um=10, settle_s=1,
                            focus_positioner='DAC', xy_positioner='XY', n_tiles=3)
    for i in range(3):
        rec.record(tile_index=i + 1, gx=i, gy=0, x_um=i * 10, y_um=0,
                   focus=_focus(i * .1))
    result = rec.finish(completed=True)
    assert result['fit'] is None
    assert result['fit_status'] == 'insufficient_noncollinear_valid_points'
    assert not (tmp_path / 'tilt_fit.png').exists()
    assert len(list(csv.DictReader((tmp_path / 'tilt_measurements.csv').open()))) == 3


def test_bidirectional_pair_averaging_cancels_linear_time_drift(tmp_path):
    # Reversed acquisition makes the observation times for each XY pair sum
    # to a constant, so a linearly drifting Z intercept cannot bias slope.
    points = [(x, y) for y in range(5) for x in range(5)]
    rec = StageTiltRecorder(
        tmp_path, step_um=100., settle_s=0., focus_positioner='LeicaZ',
        xy_positioner='Stage', n_tiles=25, bidirectional=True,
        calibration_only=True, sampling_grid_xy=(5, 5),
    )
    rec.record_origin(focus=_focus(10.), x_um=0., y_um=0.)
    observations = [(1, p) for p in points] + [(2, p) for p in reversed(points)]
    for i, (pass_index, (x, y)) in enumerate(observations, start=1):
        z = 10. + .007 * (x * 100.) - .002 * (y * 100.) + .01 * i
        rec.record(tile_index=i, gx=x, gy=y, x_um=x * 100., y_um=y * 100.,
                   focus=_focus(z), pass_index=pass_index)
    rec.record_origin(focus=_focus(10.5), x_um=0., y_um=0., final=True)
    result = rec.finish(completed=True)
    assert result['fit']['n_fit_points'] == 25
    assert result['fit']['slope_x_um_per_um'] == pytest.approx(.007, abs=1e-12)
    assert result['fit']['slope_y_um_per_um'] == pytest.approx(-.002, abs=1e-12)
    assert result['origin_drift']['delta_z_um'] == pytest.approx(.5)
    assert result['origin_drift']['duration_s'] >= 0
    assert result['bidirectional_diagnostics']['n_valid_pairs'] == 25
    assert (tmp_path / 'tilt_fit.png').is_file()


def test_missing_reverse_excludes_pair_from_fit(tmp_path):
    rec = StageTiltRecorder(tmp_path, step_um=10., settle_s=0.,
                            focus_positioner='DAC', xy_positioner='Stage',
                            n_tiles=4, bidirectional=True)
    for idx, (x, y) in enumerate(((0, 0), (1, 0), (0, 1), (1, 1)), start=1):
        rec.record(tile_index=idx, gx=x, gy=y, x_um=x*10., y_um=y*10.,
                   focus=_focus(x+y), pass_index=1)
    for idx, (x, y) in enumerate(((1, 1), (0, 1), (1, 0), (0, 0)), start=5):
        rec.record(tile_index=idx, gx=x, gy=y, x_um=x*10., y_um=y*10.,
                   focus=_focus(x+y, lock_state='unlocked' if x+y == 2 else 'locked'),
                   pass_index=2)
    result = rec.finish(completed=True)
    assert result['bidirectional_diagnostics']['n_valid_pairs'] == 3
    assert result['fit']['n_fit_points'] == 3
    assert result['origin_drift']['available'] is False
