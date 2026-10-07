"""Laser power LUT procedure on a mock laser and a mock PM100 (plan §10, P-5)."""
import math

import numpy as np
import pytest

from imswitch.imcommon.algorithms.power_lut import (
    LutRefused,
    construct_lut,
    isotonic_increasing,
    write_calib_csv,
)
from imswitch.imcommon.model.measurement_run import (
    AcquisitionOutcome,
    CleanupOutcome,
    MeasurementRunFile,
)
from imswitch.imcontrol.model.measurement import InstrumentSession
from imswitch.imcontrol.model.measurement.laser_lut import LaserLutSettings, run_laser_lut
from imswitch.imcontrol.model.measurement.mocks import MockLaser, MockPowerMeterDriver
from imswitch.imcontrol.model.resources import ResourceRegistry, set_resource_registry


@pytest.fixture(autouse=True)
def registry():
    fresh = ResourceRegistry()
    previous = set_resource_registry(fresh)
    yield fresh
    set_resource_registry(previous)


def _rig(**laser_kw):
    laser = MockLaser('775', value=1.0, enabled=False, **laser_kw)
    meter = InstrumentSession('pm1', MockPowerMeterDriver([laser]))
    meter.connect()
    return laser, meter


def _settings(**kw):
    base = dict(drive_values=list(np.linspace(0, 5, 21)), samples_per_point=3,
                settle_s=0.0, fsync=False, plane_label='BFP', dark_samples=5)
    base.update(kw)
    return LaserLutSettings(**base)


def _load_lut(path):
    return np.loadtxt(path)          # exactly what NidaqLaserManager / AAAOTF do


# -------------------------------------------------------------- the maths
def test_isotonic_fit():
    np.testing.assert_allclose(isotonic_increasing([1, 3, 2, 4]), [1, 2.5, 2.5, 4])
    np.testing.assert_allclose(isotonic_increasing([1, 2, 3]), [1, 2, 3])
    np.testing.assert_allclose(isotonic_increasing([3, 2, 1], [1, 1, 2]), [1.75] * 3)


def test_plateaus_collapse_to_their_lowest_drive():
    analysis = construct_lut([0, 1, 2, 3, 4], [0.0, 0.0, 1.0, 2.0, 2.0],
                             [0.01] * 5, [5] * 5)
    assert analysis.accepted
    np.testing.assert_array_equal(analysis.lut_drive, [0, 2, 3])
    assert np.all(np.diff(analysis.lut_power) > 0)


def test_flat_and_non_monotonic_curves_are_refused():
    flat = construct_lut([0, 1, 2], [1.0, 1.0, 1.0], [0.1] * 3, [3] * 3)
    assert not flat.accepted and 'flat' in flat.reasons[0]
    bumpy = construct_lut([0, 1, 2, 3], [0.0, 5.0, 1.0, 6.0], [0.1] * 4, [3] * 4)
    assert not bumpy.accepted and 'not monotonic' in bumpy.reasons[0]
    with pytest.raises(LutRefused):
        write_calib_csv('unused.csv', bumpy, {})


def test_noise_within_the_limit_is_corrected_and_reported():
    analysis = construct_lut([0, 1, 2, 3], [0.0, 1.02, 1.0, 2.0], [0.02] * 4, [5] * 4)
    assert analysis.accepted
    assert 0 < analysis.correction_sigma <= 3


# ------------------------------------------------------------- procedure
def test_lut_procedure_measures_exports_and_restores(tmp_path):
    laser, meter = _rig()
    asked = []

    def confirm():
        asked.append(laser.enabled)   # emission must already be off
        return True

    report = run_laser_lut(laser_control=laser, laser=laser, meter=meter, folder=tmp_path,
                           settings=_settings(), confirm_dark=confirm)
    assert report.refused == [], report.refused
    assert asked == [False]
    assert report.run.acquisition is AcquisitionOutcome.COMPLETE
    assert report.run.cleanup is CleanupOutcome.DONE
    # restored: value first, then the enabled state it had (off)
    assert (laser.value, laser.enabled) == (1.0, False)
    assert laser.events[-1] == ('restore', 1.0)
    # zeroed in the dark: the offset is the ambient light only
    assert meter.driver.zeroed_with_w == pytest.approx(meter.driver.ambient_w)
    table = _load_lut(report.lut_file)
    assert table.shape[1] == 2
    assert np.all(np.diff(table[:, 1]) > 0)
    # the LUT is the meter's view of the laser's real response
    expected = np.array([laser.response(d) + laser.leak_w for d in table[:, 0]]) * 0.8
    np.testing.assert_allclose(table[:, 1], expected, atol=5e-6)
    text = report.lut_file.read_text()
    assert '# zeroed in the dark: True' in text and '# setting unit: V' in text
    run = MeasurementRunFile.load(report.run.run_file)
    assert run.metadata['preparation']['dark zero']['zeroed'] is True
    assert report.lut_file.name.endswith('.calib.csv')
    assert report.run.run_file.name.endswith('.run.h5')


def test_beam_not_confirmed_blocked_measures_nothing_and_restores(tmp_path):
    laser, meter = _rig()
    laser.enabled = True
    report = run_laser_lut(laser_control=laser, laser=laser, meter=meter, folder=tmp_path,
                           settings=_settings(), confirm_dark=lambda: False)
    assert report.lut_file is None
    assert 'not confirmed blocked' in report.refused[0]
    assert meter.driver.zeroed_with_w is None          # never zeroed
    assert laser.enabled is True                       # restored
    assert not any(e[0] == 'drive' for e in laser.events)


def test_recalibration_with_a_loaded_lut_records_raw_drive(tmp_path):
    """The sweep must write raw volts, not % setpoints through the old LUT."""
    laser, meter = _rig(uses_lut=True)
    laser.value = 40.0            # a % setpoint
    report = run_laser_lut(laser_control=laser, laser=laser, meter=meter, folder=tmp_path,
                           settings=_settings(drive_values=[0, 1, 2, 3, 4, 5]),
                           confirm_dark=lambda: True)
    assert report.refused == []
    run = MeasurementRunFile.load(report.run.run_file)
    np.testing.assert_allclose(run.controls['775']['requested'], [0, 1, 2, 3, 4, 5])
    assert run.metadata['laser']['lut_loaded_during_run'] is True
    assert _load_lut(report.lut_file)[:, 0].max() <= 5.0
    assert laser.value == 40.0 and laser.drive == pytest.approx(2.0)   # restored via its LUT


def test_flat_curve_is_never_exported(tmp_path):
    laser, meter = _rig(response=lambda d: 0.0)
    report = run_laser_lut(laser_control=laser, laser=laser, meter=meter, folder=tmp_path,
                           settings=_settings(drive_values=[0, 1, 2, 3]),
                           confirm_dark=lambda: True)
    assert report.lut_file is None
    assert any('flat' in r for r in report.refused)
    assert report.run.run_file.exists()      # the measurement is kept


def test_non_monotonic_curve_is_never_exported(tmp_path):
    laser, meter = _rig(response=lambda d: 0.01 * (1.0 if 1.5 < d < 2.5 else d / 5.0))
    report = run_laser_lut(laser_control=laser, laser=laser, meter=meter, folder=tmp_path,
                           settings=_settings(drive_values=[0, 1, 2, 3, 4, 5]),
                           confirm_dark=lambda: True)
    assert report.lut_file is None
    assert any('not monotonic' in r for r in report.refused)


def test_partial_run_is_never_exported(tmp_path):
    laser, meter = _rig()
    runner_box = {}

    def progress(event):
        if event.point == 4:
            meter.driver.unplugged = True       # USB pulled mid-sweep

    report = run_laser_lut(laser_control=laser, laser=laser, meter=meter, folder=tmp_path,
                           settings=_settings(), confirm_dark=lambda: True, progress=progress)
    assert report.lut_file is None
    assert any('not complete' in r for r in report.refused)
    assert (laser.value, laser.enabled) == (1.0, False)    # still restored


def test_unverified_timing_is_never_exported(tmp_path):
    laser = MockLaser('775', value=1.0)
    meter = InstrumentSession('pm1', MockPowerMeterDriver([laser], verified=False))
    meter.connect()
    report = run_laser_lut(laser_control=laser, laser=laser, meter=meter, folder=tmp_path,
                           settings=_settings(allow_unverified_timing=True),
                           confirm_dark=lambda: True)
    assert report.lut_file is None
    assert any('unverified timing' in r for r in report.refused)


def test_wavelength_outside_the_sensor_refuses_before_measuring(tmp_path):
    laser = MockLaser('1550', wavelength_nm=1550.0)
    meter = InstrumentSession('pm1', MockPowerMeterDriver([laser]))   # 400-1100 nm
    meter.connect()
    report = run_laser_lut(laser_control=laser, laser=laser, meter=meter, folder=tmp_path,
                           settings=_settings(), confirm_dark=lambda: True)
    assert report.lut_file is None
    assert 'does not cover this wavelength' in report.refused[0]


def test_restoration_failure_is_reported_beside_complete_data(tmp_path):
    laser, meter = _rig()
    laser.fail_restore = 'laser controller did not answer'
    report = run_laser_lut(laser_control=laser, laser=laser, meter=meter, folder=tmp_path,
                           settings=_settings(), confirm_dark=lambda: True)
    assert report.run.acquisition is AcquisitionOutcome.COMPLETE
    assert report.run.cleanup is CleanupOutcome.FAILED
    assert 'did not answer' in report.run.cleanup_detail
    assert report.lut_file is not None          # the data is fine; cleanup is separate
    run = MeasurementRunFile.load(report.run.run_file)
    assert run.cleanup == 'failed'


def test_zeroing_with_the_laser_on_would_have_corrupted_the_offset():
    """Why emission is switched off before zeroing (the 775 script did not)."""
    laser = MockLaser('775', value=2.5, enabled=True)
    laser.drive = 2.5
    driver = MockPowerMeterDriver([laser])
    driver.run_action('zero')
    assert driver.zeroed_with_w > 100 * driver.ambient_w


class FakeDaq:
    """NI-DAQ writes that NidaqManager would make; ``fail_digital`` /
    ``fail_analog`` inject the DAQ errors the real manager would swallow
    without ``raise_on_error``."""

    def __init__(self):
        self.voltage, self.digital = 0.0, False
        self.fail_digital = self.fail_analog = False

    def setAnalog(self, target, voltage, min_val, max_val, raise_on_error=False):
        if self.fail_analog:
            if raise_on_error:
                raise OSError('DAQ analog write failed')
            return False
        self.voltage = voltage
        return True

    def setDigital(self, target, enabled, raise_on_error=False):
        if self.fail_digital:
            if raise_on_error:
                raise OSError('DAQ digital write failed')
            return False
        self.digital = bool(enabled)
        return True


def nidaq_laser(daq, name='775', digital_line=0):
    from types import SimpleNamespace

    from imswitch.imcontrol.model.managers.lasers.NidaqLaserManager import NidaqLaserManager

    info = SimpleNamespace(managerProperties={}, wavelength=775, valueRangeMin=0.0,
                           valueRangeMax=5.0, valueRangeStep=0.01,
                           getAnalogChannel=lambda: 'ao0',
                           getDigitalLine=lambda: digital_line)
    return NidaqLaserManager(info, name, nidaqManager=daq)


def test_procedure_through_a_real_nidaq_laser_manager(tmp_path, registry):
    """Real manager path: guarded raw drive + enable/restore with the token,
    while the GUI path is refused during the run."""
    from types import SimpleNamespace

    from imswitch.imcontrol.model.measurement.adapters import (
        LaserRawDriveControl,
        ManagerLaserState,
    )
    from imswitch.imcontrol.model.resources import ResourceReservedError

    daq = FakeDaq()
    manager = nidaq_laser(daq)
    shared = {'775': (1.0, False)}
    state = ManagerLaserState(manager, get_value=lambda n: shared[n][0],
                              get_enabled=lambda n: shared[n][1])
    light = SimpleNamespace(emitted_w=lambda: (
        0.01 * math.sin(0.5 * math.pi * daq.voltage / 5.0) ** 2 + 2e-5) if daq.digital else 0.0)
    meter = InstrumentSession('pm1', MockPowerMeterDriver([light]))
    meter.connect()
    refused_gui = []

    def progress(event):
        try:
            manager.setValue(0.0)         # the GUI during the run
        except ResourceReservedError:
            refused_gui.append(event.point)

    report = run_laser_lut(laser_control=LaserRawDriveControl(manager), laser=state,
                           meter=meter, folder=tmp_path, progress=progress,
                           settings=_settings(drive_values=list(np.linspace(0, 5, 11))),
                           confirm_dark=lambda: not daq.digital)
    assert report.refused == [], report.refused
    assert refused_gui == list(range(11))
    assert (daq.voltage, daq.digital) == (1.0, False)      # restored value, then off
    assert np.all(np.diff(_load_lut(report.lut_file)[:, 1]) > 0)


# ---------------------------------------------- review: checked laser commands
def _real_lut_setup(tmp_path, daq):
    from types import SimpleNamespace

    from imswitch.imcontrol.model.measurement.adapters import (
        LaserRawDriveControl,
        ManagerLaserState,
    )

    manager = nidaq_laser(daq)
    shared = {'775': (1.0, True)}
    state = ManagerLaserState(manager, get_value=lambda n: shared[n][0],
                              get_enabled=lambda n: shared[n][1])
    light = SimpleNamespace(emitted_w=lambda: (
        0.01 * math.sin(0.5 * math.pi * daq.voltage / 5.0) ** 2 + 2e-5) if daq.digital else 0.0)
    meter = InstrumentSession('pm1', MockPowerMeterDriver([light]))
    meter.connect()
    return manager, state, meter, LaserRawDriveControl(manager)


def test_failed_emission_off_stops_the_procedure_before_zeroing(tmp_path, registry):
    daq = FakeDaq()
    daq.digital = True
    manager, state, meter, control = _real_lut_setup(tmp_path, daq)
    daq.fail_digital = True
    asked = []
    report = run_laser_lut(laser_control=control, laser=state, meter=meter, folder=tmp_path,
                           settings=_settings(drive_values=[0.0, 1.0]),
                           confirm_dark=lambda: asked.append(1) or True)
    assert report.lut_file is None
    assert any('switching emission off failed' in r for r in report.refused), report.refused
    assert asked == []                       # never asked to confirm a dark that is not


def test_failed_restore_is_a_failed_cleanup_not_done(tmp_path, registry):
    from imswitch.imcommon.model.measurement_run import CleanupOutcome

    daq = FakeDaq()
    manager, state, meter, control = _real_lut_setup(tmp_path, daq)
    calls = {'n': 0}
    real = daq.setAnalog

    def fail_after_sweep(*args, **kwargs):
        calls['n'] += 1
        if calls['n'] > 2:                   # the two sweep points pass; the restore fails
            daq.fail_analog = True
        return real(*args, **kwargs)
    daq.setAnalog = fail_after_sweep
    report = run_laser_lut(laser_control=control, laser=state, meter=meter, folder=tmp_path,
                           settings=_settings(drive_values=[0.0, 5.0]),
                           confirm_dark=lambda: not daq.digital)
    assert report.run.cleanup is CleanupOutcome.FAILED
    assert 'DAQ analog write failed' in report.run.cleanup_detail


def test_checked_commands_raise_where_the_ordinary_ones_log():
    from imswitch.imcontrol.model.managers.lasers.LaserManager import RawDriveError

    daq = FakeDaq()
    manager = nidaq_laser(daq)
    daq.fail_digital = daq.fail_analog = True
    manager.setEnabled(False)                # the GUI path still only logs
    manager.setValue(1.0)
    with pytest.raises(RawDriveError, match='emission off failed'):
        manager.applyEnabled(False)
    with pytest.raises(RawDriveError, match='analog write failed'):
        manager.applyValue(1.0)
    assert nidaq_laser(FakeDaq(), name='a', digital_line=None).applyEnabled(False) is False
