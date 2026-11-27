"""Pure regression tests for experimental XY-to-Z feed-forward and validation."""

import csv
import json

import pytest

from imswitch.imcontrol.model.workflows.stage_tilt_compensation import (
    TiltCompensationRecorder, load_tilt_plane, require_reference_signal,
    validate_z_target,
)


def _calibration(tmp_path, **overrides):
    fit = dict(
        model='plane', slope_x_um_per_um=-0.0092599672,
        slope_y_um_per_um=-0.0029596949,
        rms_residual_um=0.0593, n_fit_points=25,
        valid_xy_bounds_um={'x': [1000., 1400.], 'y': [2000., 2400.]},
    )
    document = dict(
        version=1, completed=True, fit_status='ok',
        xy_positioner='Stage', focus_positioner='FocusPiezoDAC',
        z_source='last_commanded_position_not_hardware_readback',
        fit=fit,
    )
    for key, value in overrides.items():
        if key.startswith('fit_') and key not in ('fit_status',):
            fit[key[4:]] = value
        else:
            document[key] = value
    path = tmp_path / 'tilt_fit.json'
    path.write_text(json.dumps(document), encoding='utf-8')
    return path


def _read_plane(path):
    return load_tilt_plane(
        path, xy_positioner='Stage', focus_positioner='FocusPiezoDAC'
    )


def _focus(signal=20, **overrides):
    value = dict(focus_signal_px=signal, measurement_age_s=.02,
                 max_measurement_age_s=1., lock_state='unlocked',
                 positioner_available=True)
    value.update(overrides)
    return value


def test_load_and_relative_delta_independent_of_absolute_z(tmp_path):
    plane = _read_plane(_calibration(tmp_path))
    assert plane.delta_z(100, 0) == pytest.approx(-.92599672)
    assert plane.delta_z(0, 100) == pytest.approx(-.29596949)
    assert plane.delta_z(-100, -100) == pytest.approx(1.22196621)
    plane.check_xy(1000., 2000.)
    plane.check_xy(1400., 2400.)
    plane.check_grid(origin_xy=(1200., 2200.),
                     moves=[(0, 0), (-1, 0), (0, 1), (1, 0)], step_um=100)
    with pytest.raises(ValueError, match='outside'):
        plane.check_xy(1403., 2200.)
    with pytest.raises(ValueError, match='outside'):
        plane.check_grid(origin_xy=(1400., 2200.),
                         moves=[(0, 0), (1, 0)], step_um=100)


@pytest.mark.parametrize('override', [
    {'completed': False}, {'fit_status': 'insufficient'},
    {'version': 999}, {'xy_positioner': 'Other'},
    {'focus_positioner': 'Other'}, {'fit_model': 'quadratic'},
    {'fit_slope_y_um_per_um': float('nan')},
    {'fit_valid_xy_bounds_um': {'x': [0, 0], 'y': [0, 1]}},
])
def test_invalid_calibrations_are_rejected(tmp_path, override):
    with pytest.raises((ValueError, TypeError)):
        _read_plane(_calibration(tmp_path, **override))


def test_first_tile_requires_fresh_unlocked_focus_signal():
    assert require_reference_signal(_focus(17.5)) == 17.5
    for value in (_focus(lock_state='locked'),
                  _focus(measurement_age_s=5.0),
                  _focus(signal=None),
                  _focus(positioner_available=False)):
        with pytest.raises((ValueError, RuntimeError)):
            require_reference_signal(value)


def test_dac_target_preflight_rejects_clamping_or_disconnection():
    class DAC:
        _offset_voltage = 1.0
        _volts_per_um = 0.1
        _min_voltage = 0.
        _max_voltage = 2.
        isAvailable = True

    dac = DAC()
    validate_z_target(dac, 9.9, strict_limits=True)
    with pytest.raises(ValueError, match='refusing to clamp'):
        validate_z_target(dac, 11.0, strict_limits=True)
    with pytest.raises(ValueError, match='Nonfinite'):
        validate_z_target(dac, float('nan'))
    dac.isAvailable = False
    with pytest.raises(RuntimeError, match='unavailable'):
        validate_z_target(dac, 2.0)


def test_validation_records_open_loop_signal_in_csv_and_png(tmp_path):
    plane = _read_plane(_calibration(tmp_path))
    rec = TiltCompensationRecorder(
        tmp_path / 'validation', plane=plane, origin_xy=(1200., 2200.),
        origin_z_um=7.5, origin_signal_px=20., n_tiles=3,
    )
    rec.record(tile_index=1, gx=0, gy=0, x_um=1200, y_um=2200,
               commanded_z_um=7.5, focus=_focus())
    rec.record(tile_index=2, gx=1, gy=0, x_um=1300, y_um=2200,
               commanded_z_um=7.5 + plane.delta_z(100, 0), focus=_focus(20.3))
    rec.record(tile_index=3, gx=1, gy=1, x_um=1300, y_um=2300,
               commanded_z_um=7.5 + plane.delta_z(100, 100),
               focus=_focus(99., measurement_age_s=2.))
    info = rec.finish(completed=False, failure='cancelled')
    assert info['n_recorded_tiles'] == 3
    assert info['n_valid_focus_samples'] == 2
    assert info['completed'] is False
    assert info['max_abs_focus_signal_delta_px'] == pytest.approx(.3)
    assert (tmp_path / 'validation' / 'tilt_validation.png').stat().st_size > 3000
    with (tmp_path / 'validation' / 'tilt_compensation.csv').open(newline='') as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 3
    assert rows[0]['focus_signal_delta_px'] == '0.0'
    assert float(rows[1]['predicted_delta_z_um']) == pytest.approx(plane.delta_z(100, 0))
    assert rows[2]['valid_focus_signal'] == 'False'
    assert rows[2]['rejection_reason'] == 'stale_focus_signal'


def test_loaded_calibration_names_z_independently_of_focus_setup(tmp_path):
    path = _calibration(tmp_path, focus_positioner='Z')
    plane = load_tilt_plane(path, xy_positioner='Stage')
    assert plane.focus_positioner == 'Z'
    with pytest.raises(ValueError, match='requested actuator'):
        load_tilt_plane(path, xy_positioner='Stage', focus_positioner='FocusPiezoDAC')


def test_optional_preflight_and_nidaq_voltage_limits():
    class DAQZ:
        isAvailable = True
        requiresReference = True
        isReferenced = False
        _conversionFactor = 10
        _minVolt = -10
        _maxVolt = 10

    daq = DAQZ()
    # The optional pre-check is intentionally not an implicit restriction.
    validate_z_target(daq, 110)
    with pytest.raises(RuntimeError, match='referenced'):
        validate_z_target(daq, 10, strict_limits=True)
    daq.isReferenced = True
    validate_z_target(daq, 90, strict_limits=True)
    with pytest.raises(ValueError, match='NI-DAQ voltage'):
        validate_z_target(daq, 110, strict_limits=True)
    with pytest.raises(ValueError, match='Nonfinite'):
        validate_z_target(daq, float('nan'))


def test_fine_focus_locked_is_valid_for_validation_with_separate_actuator(tmp_path):
    from imswitch.imcontrol.model.workflows.stage_tilt_compensation import TiltCompensationRecorder
    plane = _read_plane(_calibration(tmp_path))
    reference = require_reference_signal(_focus(18, lock_state='locked'), allow_locked=True)
    rec = TiltCompensationRecorder(
        tmp_path / 'locked', plane=plane, origin_xy=(1200, 2200),
        origin_z_um=1.0, origin_signal_px=reference, n_tiles=2,
    )
    result = rec.record(tile_index=1, gx=0, gy=0, x_um=1200, y_um=2200,
                        commanded_z_um=1.0, focus=_focus(18.1, lock_state='locked'))
    assert result['valid_focus_signal']
    assert result['focus_signal_delta_px'] == pytest.approx(.1)
    rec.finish(completed=True)


def test_compensation_without_focus_camera_keeps_numerical_record(tmp_path):
    plane = _read_plane(_calibration(tmp_path))
    rec = TiltCompensationRecorder(
        tmp_path / 'no_camera', plane=plane, origin_xy=(1200, 2200),
        origin_z_um=1.0, origin_signal_px=None, n_tiles=1,
    )
    rec.record(tile_index=1, gx=0, gy=0, x_um=1200, y_um=2200,
               commanded_z_um=1., focus={'lock_state': 'unavailable'})
    result = rec.finish(completed=True)
    assert result['n_recorded_tiles'] == 1
    assert result['n_valid_focus_samples'] == 0
    assert result['completed']


@pytest.mark.parametrize('z_source', [
    'last_commanded_position_not_hardware_readback',
    'positioner_get_abs_software_command_or_physical_readback',
])
def test_loads_legacy_and_recorded_readback_z_sources(tmp_path, z_source):
    # The new recorder logs Z in µm from get_abs(), which may be software
    # commanded or physical readback depending on the positioner.
    path = _calibration(tmp_path, z_source=z_source, focus_positioner='Z')
    plane = load_tilt_plane(path, xy_positioner='Stage')
    assert plane.focus_positioner == 'Z'
    assert plane.delta_z(100, 50) == pytest.approx(
        -0.0092599672 * 100 - 0.0029596949 * 50
    )


def test_unrecognized_z_source_still_rejected(tmp_path):
    path = _calibration(tmp_path, z_source='unknown_dimension_or_source')
    with pytest.raises(ValueError, match='Unknown calibration Z units/source'):
        _read_plane(path)
