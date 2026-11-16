"""ISM Estimate is raw-data calibration, not an image reconstruction."""

import numpy as np
import pytest

from imswitch.improcess.reconstructors.monalisa.calibration import PatternLocalization
from imswitch.improcess.reconstructors.monalisa.ism_calibration import estimate_ism_shift
from imswitch.improcess.reconstructors.monalisa.reassignment import ShiftFactor


def _localization():
    return PatternLocalization.manual_grid(
        period_x=16., period_y=16., offset_x=8., offset_y=8., source="manual"
    )


def _factor(alpha=0.4):
    return ShiftFactor(alpha, alpha - 0.05, alpha + 0.05,
                       np.array([[1., 0., alpha, 0.9]]), 0.02)


def test_direct_calibration_uses_raw_scan_geometry_without_gaussian_fit(monkeypatch):
    import sys
    from imswitch.improcess.reconstructors.monalisa import ism_calibration as helper

    seen = {}
    def measure(frames, x, y, offsets, raster):
        seen.update(frames=frames, x=x, y=y, offsets=offsets, raster=raster)
        return _factor()
    monkeypatch.setattr(helper, "measure_shift_factor", measure)
    frames = np.random.default_rng(3).normal(10, 1, (16, 64, 64))
    result = estimate_ism_shift(
        frames, {"ScanTTL:Nx": 4, "ScanTTL:Ny": 4}, None,
        {"scan_steps_fast": 4, "scan_steps_slow": 4},
        _localization(), "+x+y",
    )
    assert seen["frames"].shape == (16, 64, 64)
    assert seen["x"].size > 0
    assert seen["offsets"].shape == (16, 2)
    np.testing.assert_allclose(seen["offsets"][0], [0, 0])
    assert result.factor.alpha == 0.4
    assert result.scanned_axes == ("x", "y")
    assert result.num_stacks == 1


def test_estimate_does_not_guess_unresolved_auto_orientation():
    frames = np.ones((16, 64, 64))
    with pytest.raises(ValueError, match="resolved scan orientation"):
        estimate_ism_shift(frames, {}, None, {}, _localization(), "")


def test_pick_modulated_stack_not_first(monkeypatch):
    from imswitch.improcess.reconstructors.monalisa import ism_calibration as helper
    seen = []
    def measure(frames, *_):
        seen.append(float(frames.var()))
        return _factor()
    monkeypatch.setattr(helper, "measure_shift_factor", measure)
    frames = np.zeros((32, 64, 64))
    frames[16:] = np.random.default_rng(2).normal(20, 4, (16, 64, 64))
    result = estimate_ism_shift(
        frames, {}, None, {"scan_steps_fast": 4, "scan_steps_slow": 4},
        _localization(), "+x+y",
    )
    assert result.stack_number == 1
    assert result.num_stacks == 2
    assert seen[0] > 0


def test_single_scan_axis_remains_single_axis(monkeypatch):
    from imswitch.improcess.reconstructors.monalisa import ism_calibration as helper
    offsets_seen = []
    def measure(frames, _x, _y, offsets, _raster):
        offsets_seen.append(offsets)
        return _factor()
    monkeypatch.setattr(helper, "measure_shift_factor", measure)
    frames = np.random.default_rng(2).normal(10, 1, (5, 64, 64))
    result = estimate_ism_shift(
        frames, {}, None, {"scan_steps_fast": 1, "scan_steps_slow": 5},
        _localization(), "+x+y",
    )
    assert result.scanned_axes == ("y",)
    assert np.allclose(offsets_seen[0][:, 0], 0)
    assert np.ptp(offsets_seen[0][:, 1]) > 0


def test_raw_estimate_recovers_known_physical_ism_shift():
    from imswitch.improcess._test._monalisa_synthetic import make_physical_scan

    scan = make_physical_scan(sigma_e=1.2, sigma_d=1.5, specimen="points")
    localization = PatternLocalization.manual_grid(
        period_x=11., period_y=11., offset_x=3.5, offset_y=4.5,
    )
    result = estimate_ism_shift(
        scan.frames, {}, None,
        {"scan_steps_fast": 22, "scan_steps_slow": 22},
        localization, "+x+y",
    )
    assert result.factor.alpha == pytest.approx(scan.alpha, abs=0.05)
    assert result.factor.alpha_x == pytest.approx(scan.alpha, abs=0.05)
    assert result.factor.alpha_y == pytest.approx(scan.alpha, abs=0.05)


def test_yz_recorded_layout_uses_only_scanned_camera_axis(monkeypatch):
    from imswitch.improcess.reconstructors.monalisa import ism_calibration as helper
    from imswitch.improcess.reconstructors.monalisa_lattice import scan as scan_module

    class YZLayout:
        counts = (1, 5)
        step_nm = (None, 62.)
        num_stacks = 1
        index = np.column_stack((np.zeros(5, dtype=int), np.arange(5)))
        def frames_of(self, stack):
            return np.arange(5)

    monkeypatch.setattr(scan_module, "layout_scan", lambda *_: YZLayout())
    seen = {}
    def measure(frames, x, y, offsets, raster):
        seen["offsets"] = offsets
        return _factor()
    monkeypatch.setattr(helper, "measure_shift_factor", measure)
    frames = np.random.default_rng(3).normal(4, 1, (5, 64, 64))
    result = estimate_ism_shift(
        frames, {}, object(), {"pixel_size_nm": 80.}, _localization(), "+x+y"
    )
    assert result.scanned_axes == ("y",)
    np.testing.assert_allclose(seen["offsets"][:, 0], 0)
    assert np.ptp(seen["offsets"][:, 1]) > 0


def test_stack_selection_excludes_outer_foci_on_456_pixel_camera():
    """Foci around index 457 must not be indexed in a 456-pixel image."""
    from imswitch.improcess.reconstructors.monalisa.ism_calibration import _sampled_stack

    class Scan:
        frames_per_stack = 16
        num_stacks = 1

    localization = PatternLocalization.manual_grid(
        period_x=50., period_y=50., offset_x=7., offset_y=7.,
    )
    # Positive margin includes the off-frame index 457 on *both* axes.
    x, y = localization.points_in_frame(456, 456, margin=3.)
    assert np.any(np.rint(x).astype(int) == 457)
    assert np.any(np.rint(y).astype(int) == 457)
    frames = np.random.default_rng(41).normal(size=(16, 456, 456)).astype("float32")
    selected, ids = _sampled_stack(frames, Scan(), localization)
    assert selected == 0
    np.testing.assert_array_equal(ids, np.arange(16))


def test_stack_selection_requires_safe_detector_offsets():
    """Do not silently index edge pixels when no focus has usable reach."""
    from imswitch.improcess.reconstructors.monalisa.ism_calibration import _sampled_stack

    class Scan:
        frames_per_stack = 4
        num_stacks = 1

    localization = PatternLocalization.manual_grid(
        period_x=100., period_y=100., offset_x=1., offset_y=1.,
    )
    frames = np.ones((4, 20, 20), dtype="float32")
    with pytest.raises(ValueError, match="required detector-offset margin"):
        _sampled_stack(frames, Scan(), localization)
