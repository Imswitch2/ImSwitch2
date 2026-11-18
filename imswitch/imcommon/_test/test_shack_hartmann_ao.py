"""Shack-Hartmann sensing and adaptive optics on synthetic spot frames: a
known wavefront in, its Zernike coefficients out; a simulated 69-actuator
mirror calibrated, closed-loop corrected, and driven in Zernike modes."""

import numpy as np
import pytest

from imswitch.imcommon.algorithms import adaptive_optics as ao
from imswitch.imcommon.algorithms import shack_hartmann as sh
from imswitch.imcommon.algorithms import zernike as z

SHAPE = (360, 360)
PITCH = 20.0            # lenslet spacing on the camera, px
PIXEL_UM = 3.45
FOCAL_UM = 5200.0       # MLA150-5C


def _grid():
    return sh.regular_grid((180.3, 179.6), PITCH, 15, 15, window=8, pupil_radius=150)


def _sensor(grid=None, method='centroid'):
    return sh.ShackHartmannSensor(grid=grid or _grid(), pixel_um=PIXEL_UM,
                                  focal_length_um=FOCAL_UM, method=method)


def _frame(sensor, slopes=None, noise=0.0, seed=0):
    grid = sensor.grid
    if slopes is None:
        slopes = np.zeros((grid.count, 2))
    positions = sh.spots_for_slopes(grid, slopes, sensor.slope_per_pixel)
    return sh.render_spots(SHAPE, positions, sigma=1.6, peak=800, offset=100, noise=noise,
                           rng=np.random.default_rng(seed))


def _zernike_slopes(grid, coefficients):
    x, y = grid.pupil_xy()
    modes = list(coefficients)
    gx, gy = z.mean_gradients(modes, x, y, grid.half_width())
    c = np.array([coefficients[j] for j in modes])
    return np.stack([gx @ c, gy @ c], axis=1)


# ── Zernike ────────────────────────────────────────────────────────────────
def test_one_zernike_basis_for_beads_and_mirrors():
    from imswitch.improcess.analysis import psf_aberrations
    assert psf_aberrations.zernike is z.zernike and psf_aberrations.noll_to_nm is z.noll_to_nm


def test_the_modes_have_unit_rms_over_the_disk():
    y, x = np.mgrid[-1:1:801j, -1:1:801j]
    inside = np.hypot(x, y) <= 1
    for j in (2, 4, 5, 8, 11, 22):
        values = z.zernike_xy(j, x[inside], y[inside])
        assert np.sqrt(np.mean(values ** 2)) == pytest.approx(1.0, rel=5e-3), j


def test_lenslet_mean_gradients_are_the_mean_of_the_derivative():
    x0, y0, h = 0.31, -0.42, 0.05
    gx, gy = z.mean_gradients([4, 7, 11], [x0], [y0], h)
    t = np.linspace(-h, h, 401)
    xs, ys = np.meshgrid(x0 + t, y0 + t)
    for column, j in enumerate([4, 7, 11]):
        w = z.zernike_xy(j, xs, ys)
        dy, dx = np.gradient(w, t, t)
        assert gx[0, column] == pytest.approx(dx.mean(), rel=1e-3)
        assert gy[0, column] == pytest.approx(dy.mean(), rel=1e-3)


# ── the lenslet grid ───────────────────────────────────────────────────────
def test_the_grid_is_found_in_a_reference_frame():
    truth = _grid()
    frame = _frame(_sensor(truth), noise=3.0)
    found = sh.find_grid(frame, pitch_px=19.0, window=8)
    assert found.count == truth.count
    assert found.pitch == pytest.approx((PITCH, PITCH), abs=0.02)
    order = np.lexsort((found.lattice[:, 1], found.lattice[:, 0]))
    expected = np.lexsort((truth.lattice[:, 1], truth.lattice[:, 0]))
    assert np.abs(found.lattice[order] - truth.lattice[expected]).max() < 0.1
    assert found.pupil_radius == pytest.approx(truth.pupil_radius, abs=PITCH)


def test_the_old_guis_typed_grid_puts_the_middle_lenslet_on_the_centre():
    grid = sh.regular_grid((1039, 1074), 47, 34, 34, window=16)
    assert grid.count == 34 * 34
    middle = np.nonzero((grid.indices == [17, 17]).all(axis=1))[0][0]
    assert tuple(grid.lattice[middle]) == (1039, 1074)


# ── measuring ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize('method', ['centroid', 'correlation'])
def test_a_known_wavefront_comes_back_as_its_zernike_coefficients(method):
    sensor = _sensor(method=method)
    truth = {4: 0.12, 5: -0.08, 7: 0.05, 11: 0.03}
    sensor.set_reference(_frame(sensor, noise=2.0, seed=1))
    reading = sensor.measure(_frame(sensor, _zernike_slopes(sensor.grid, truth), noise=2.0, seed=2))
    assert reading.valid.all()
    fit = sh.fit_zernike(reading, sensor.grid, z.modes_up_to(15))
    for j in range(2, 16):
        assert fit[j] == pytest.approx(truth.get(j, 0.0), abs=0.006), (j, fit[j])


def test_the_zonal_map_is_the_wavefront_on_the_lattice():
    sensor = _sensor()
    truth = {4: 0.1, 6: 0.05}
    sensor.set_reference(_frame(sensor))
    reading = sensor.measure(_frame(sensor, _zernike_slopes(sensor.grid, truth)))
    w = sh.zonal_wavefront(reading, sensor.grid)
    x, y = sensor.grid.pupil_xy()
    expected = np.full_like(w, np.nan)
    expected[sensor.grid.indices[:, 0], sensor.grid.indices[:, 1]] = z.wavefront(truth, x, y)
    expected -= np.nanmean(expected)
    inside = np.isfinite(w)
    assert np.sqrt(np.nanmean((w - expected)[inside] ** 2)) < 0.01 * np.nanstd(expected) + 2e-3


def test_a_dark_lenslet_is_left_out_not_measured_as_a_shift():
    sensor = _sensor()
    sensor.set_reference(_frame(sensor))
    positions = sh.spots_for_slopes(sensor.grid, np.zeros((sensor.grid.count, 2)),
                                    sensor.slope_per_pixel)
    brightness = np.ones(sensor.grid.count)
    brightness[:5] = 0.0                                   # vignetted
    frame = sh.render_spots(SHAPE, positions, sigma=1.6, peak=800, offset=100,
                            brightness=brightness)
    reading = sensor.measure(frame)
    assert not reading.valid[:5].any() and reading.valid[5:].all()


def test_a_sensor_saved_and_loaded_measures_the_same(tmp_path):
    sensor = _sensor()
    sensor.set_reference(_frame(sensor, noise=2.0))
    frame = _frame(sensor, _zernike_slopes(sensor.grid, {5: 0.1}), noise=2.0, seed=3)
    again = sh.ShackHartmannSensor.load(sensor.save(tmp_path / 'wfs.npz'))
    assert np.allclose(again.measure(frame).slopes, sensor.measure(frame).slopes, equal_nan=True)


# ── a mirror: calibration, closed loop, modes ──────────────────────────────
def _measure_mirror(sensor, mirror, command, seed):
    x, y = sensor.grid.pupil_xy()
    slopes = mirror.mean_slopes(x, y, sensor.grid.half_width(), command)
    return sensor.measure(_frame(sensor, slopes, noise=1.0, seed=seed))


@pytest.fixture(scope='module')
def calibrated():
    sensor = _sensor()
    mirror = ao.SimulatedMirror(69, stroke_um=0.5)
    sensor.set_reference(_frame(sensor, noise=1.0, seed=99))
    amplitude = 0.1
    columns = []
    for k in range(mirror.count):
        push = np.zeros(mirror.count)
        push[k] = amplitude
        plus = _measure_mirror(sensor, mirror, push, seed=2 * k)
        minus = _measure_mirror(sensor, mirror, -push, seed=2 * k + 1)
        columns.append(ao.influence_column(plus.vector(), minus.vector(), amplitude,
                                           plus.valid_rows(), minus.valid_rows()))
    calibration = ao.MirrorCalibration(influence=np.stack(columns, axis=1),
                                       amplitude=amplitude, n_singular=60)
    return sensor, mirror, calibration


def test_the_closed_loop_flattens_an_aberration(calibrated):
    sensor, mirror, calibration = calibrated
    aberrated = ao.SimulatedMirror(69, stroke_um=0.5, aberration={5: 0.1, 7: 0.06, 9: -0.04})
    command = np.zeros(69)
    first = _measure_mirror(sensor, aberrated, command, seed=500)
    control = calibration.control_matrix(first.valid_rows())
    residuals = [ao.rms(first.vector())]
    for step in range(12):
        reading = _measure_mirror(sensor, aberrated, command, seed=501 + step)
        command = ao.integrator_step(command, reading.vector(), control, gain=0.5)
        residuals.append(ao.rms(_measure_mirror(sensor, aberrated, command, 600 + step).vector()))
    assert residuals[-1] < residuals[0] / 5, residuals
    assert np.abs(command).max() < 1.0


def test_mode_commands_make_the_mirror_speak_zernike(calibrated, tmp_path):
    sensor, mirror, calibration = calibrated
    modes = z.modes_up_to(11, first=4)
    calibration.modes = modes
    calibration.mode_commands = ao.mode_commands(
        calibration, sh.zernike_slope_matrix(sensor.grid, modes))
    again = ao.MirrorCalibration.load(calibration.save(tmp_path / 'calibration.npz'))
    assert again.modes == modes and np.allclose(again.mode_commands, calibration.mode_commands)
    for j in (5, 8, 11):
        command = 0.05 * calibration.mode_commands[modes.index(j)]
        fit = sh.fit_zernike(_measure_mirror(sensor, mirror, command, seed=700 + j),
                             sensor.grid, z.modes_up_to(15))
        assert fit[j] == pytest.approx(0.05, rel=0.15), (j, fit)
        assert max(abs(c) for k, c in fit.items() if k != j) < 0.012, (j, fit)


# ── sensorless ─────────────────────────────────────────────────────────────
def test_the_mode_scan_peak_and_its_refusals():
    a = np.linspace(-0.2, 0.2, 7)
    assert ao.quadratic_peak(a, 5 - (a - 0.05) ** 2 * 30).peak == pytest.approx(0.05)
    assert 'upwards' in ao.quadratic_peak(a, a ** 2).reason
    assert 'above' in ao.quadratic_peak(a, 5 - (a - 0.5) ** 2).reason
    assert ao.quadratic_peak(a[:2], a[:2]).peak is None


def test_a_sharper_image_scores_higher():
    from scipy import ndimage
    rng = np.random.default_rng(4)
    sharp = np.zeros((128, 128))
    sharp[rng.integers(10, 118, 40), rng.integers(10, 118, 40)] = 1000
    sharp = ndimage.gaussian_filter(sharp, 1.0)
    blurred = ndimage.gaussian_filter(sharp, 2.5)
    # (band weighs one spatial frequency, e.g. a pattern's: not sharpness.)
    for kind in ('max', 'highpass', 'snr'):
        assert ao.image_metric(sharp, kind) > ao.image_metric(blurred, kind), kind
    with pytest.raises(ValueError, match='needs a mask'):
        ao.image_metric(sharp, 'masked')
