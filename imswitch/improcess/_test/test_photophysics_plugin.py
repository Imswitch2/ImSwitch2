"""Tests for the camera-based photophysics drop-in plugin (P0: FATIGUE).

Loads the plugin the same way ImProcess discovery does — a standalone ``.py``
imported in isolation — and exercises the analysis core, the Processor, the
curve result's plot payload, and ascii save.
"""
import importlib.util
import types
from pathlib import Path

import numpy as np
import pytest

_PLUGIN_PATH = (
    Path(__file__).resolve().parents[3]
    / "examples" / "improcess_plugins" / "photophysics_suite.py"
)


@pytest.fixture(scope="module")
def plugin():
    assert _PLUGIN_PATH.exists(), _PLUGIN_PATH
    spec = importlib.util.spec_from_file_location("photophysics_suite_test", _PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _decaying_stack(T=20, Y=4, X=4, base=100.0, r=0.5, bkg=5.0):
    """Uniform frames: signal base*r**t decaying to ~0, plus constant bkg."""
    t = np.arange(T, dtype=float)
    per_frame = base * (r ** t) + bkg
    return per_frame[:, None, None] * np.ones((T, Y, X), dtype=float)


def test_frame_profile_sum_and_mean(plugin):
    stack = np.ones((5, 3, 4), dtype=float) * 2.0
    prof_sum = plugin.frame_profile(stack, reduce="sum")
    prof_mean = plugin.frame_profile(stack, reduce="mean")
    assert prof_sum.shape == (5,)
    assert np.allclose(prof_sum, 2.0 * 3 * 4)   # sum over 12 px
    assert np.allclose(prof_mean, 2.0)          # mean


def test_frame_profile_roi_and_errors(plugin):
    stack = np.arange(2 * 4 * 4, dtype=float).reshape(2, 4, 4)
    prof = plugin.frame_profile(stack, roi=(0, 2, 0, 2), reduce="sum")
    assert prof.shape == (2,)
    assert np.allclose(prof[0], stack[0, 0:2, 0:2].sum())
    with pytest.raises(ValueError):
        plugin.frame_profile(np.ones((3, 3)), reduce="sum")  # <3 dims


def test_subtract_background_tail_mean(plugin):
    profile = np.array([100.0, 50.0, 10.0, 5.0, 5.0, 5.0], dtype=float)
    corrected, bkg = plugin.subtract_background(profile, mode="tail_mean", tail=3)
    assert bkg == pytest.approx(5.0)
    assert corrected[0] == pytest.approx(95.0)
    assert plugin.subtract_background(profile, mode="constant", value=7.0)[1] == 7.0
    assert plugin.subtract_background(profile, mode="none")[1] == 0.0


def test_analyze_fatigue_normalizes_to_first(plugin):
    profile = np.array([105.0, 55.0, 30.0, 5.0, 5.0], dtype=float)
    out = plugin.analyze_fatigue(profile, background="tail_mean", tail=2, normalize="first")
    assert out["background"] == pytest.approx(5.0)
    assert out["normalized"][0] == pytest.approx(1.0)          # normalized to first
    assert np.all(np.diff(out["normalized"]) <= 1e-9)          # monotonically decreasing
    assert out["cycles"][0] == 1.0 and out["cycles"][-1] == 5.0


def test_processor_apply_fatigue_produces_curve_result(plugin):
    stack = _decaying_stack()
    result = types.SimpleNamespace(name="rec", data=stack, axis_labels=["T", "Y", "X"])
    proc = plugin.PhotophysicsProcessor()

    assert proc.accepts(result)  # image kind + >=3 dims

    out = proc.apply(result, {"mode": "fatigue", "background": "tail_mean",
                              "tail": 3, "normalize": "first", "reduce": "sum"})
    assert out.kind == "curve"
    assert out.name == "rec (fatigue)"
    assert out.columns == ["cycle", "profile_bkg", "normalized"]
    assert out.data.shape == (20, 3)
    # normalized column starts at 1 and decays
    assert out.data[0, 2] == pytest.approx(1.0)
    assert out.data[-1, 2] < out.data[0, 2]


def test_curve_result_plot_payload_and_save(plugin, tmp_path):
    stack = _decaying_stack()
    result = types.SimpleNamespace(name="rec", data=stack, axis_labels=["T", "Y", "X"])
    out = plugin.PhotophysicsProcessor().apply(result, {"mode": "fatigue", "tail": 3})

    payloads = out.plot_payloads()
    assert len(payloads) == 1
    payload = payloads[0]
    assert payload.x_label == "# cycle"
    assert len(payload.series) == 1
    series = payload.series[0]
    assert series.kind == "line"
    assert series.x.shape == series.y.shape == (20,)

    path = tmp_path / "fatigue.txt"
    out.save(path, fmt="txt")
    loaded = np.loadtxt(path)
    assert loaded.shape == (20, 3)
    assert np.allclose(loaded, out.data)


def _off_stack(n_cycles=6, window=50, tau=15.0, peak=1000.0, bkg=10.0,
               Y=3, X=3, dark_tail=60):
    """Tiled exponential off-switch cycles + a dark tail (baseline)."""
    cycle = peak * np.exp(-np.arange(window) / tau) + bkg
    prof = np.concatenate([np.tile(cycle, n_cycles), np.full(dark_tail, bkg)])
    return prof[:, None, None] * np.ones((prof.size, Y, X), dtype=float)


def test_analyze_off_recovers_kinetics(plugin):
    tau, window = 15.0, 50
    profile = plugin.frame_profile(_off_stack(tau=tau, window=window), reduce="sum")
    out = plugin.analyze_off(profile, time_unit_ms=1.0, window=window,
                             background="tail_mean", tail=30, do_fit=True)
    assert out["n_cycles"] >= 3                      # several cycles averaged
    assert out["normalized"][0] == pytest.approx(1.0)
    assert out["normalized"][-1] < 0.1               # decays to ~0
    assert 10.0 <= out["t_half_ms"] <= 14.0          # ~ tau*ln2 + 1 frame
    assert out["fit1"] is not None
    assert out["fit1"]["tau_ms"] == pytest.approx(tau, abs=3.0)
    assert out["fit1"]["r2"] > 0.99
    assert out["fit2"] is not None                   # 2-exp also converges


def test_processor_apply_off_produces_curve_with_fits(plugin):
    result = types.SimpleNamespace(name="rec", data=_off_stack(), axis_labels=["T", "Y", "X"])
    out = plugin.PhotophysicsProcessor().apply(
        result, {"mode": "off", "time_unit_ms": 1.0, "window": 50,
                 "background": "tail_mean", "tail": 30, "do_fit": True}
    )
    assert out.kind == "curve"
    assert out.name == "rec (off-switch)"
    assert out.columns == ["time_ms", "mean", "std", "normalized"]
    assert "t_half_ms" in out.scalars and "fit1_tau_ms" in out.scalars
    # payload has the averaged decay plus the two fit series
    payload = out.plot_payloads()[0]
    assert payload.x_label == "time (ms)"
    assert len(payload.series) == 3


def _on_stack(powers, step_len=30, Y=3, X=3):
    """Step-like activation: a step_len-frame plateau per power, level == power."""
    prof = np.concatenate([np.full(step_len, float(p)) for p in powers])
    return prof[:, None, None] * np.ones((prof.size, Y, X), dtype=float)


def test_parse_power_sequence(plugin):
    assert plugin.parse_power_sequence("0, 10, 25, 50") == [0.0, 10.0, 25.0, 50.0]
    assert plugin.parse_power_sequence("0 10  x 25") == [0.0, 10.0, 25.0]  # skips junk
    assert plugin.parse_power_sequence("") == []


def test_analyze_on_auto_and_manual_agree(plugin):
    powers = [0, 10, 25, 50, 100, 200]
    profile = plugin.frame_profile(_on_stack(powers, step_len=30), reduce="sum")

    auto = plugin.analyze_on(profile, powers, auto_detect=True, dpnts=10, background="none")
    assert auto["activation"].shape == (6,)
    # activation ∝ power (level==power, summed over 9 px)
    assert np.allclose(auto["activation"], 9.0 * np.array(powers, dtype=float))
    assert auto["normalized"][-1] == pytest.approx(1.0)      # norm to max (last)
    assert np.all(np.diff(auto["normalized"]) >= -1e-9)      # monotonically increasing

    manual = plugin.analyze_on(profile, powers, auto_detect=False,
                               offset=15, jump=30, dpnts=8, background="none")
    assert np.allclose(manual["activation"], auto["activation"])


def test_processor_apply_on_produces_curve_result(plugin):
    powers = [0, 10, 25, 50, 100, 200]
    result = types.SimpleNamespace(name="rec", data=_on_stack(powers), axis_labels=["T", "Y", "X"])
    out = plugin.PhotophysicsProcessor().apply(
        result, {"mode": "on", "powers": powers, "auto_detect": True,
                 "dpnts": 10, "background": "none", "normalize": "max"}
    )
    assert out.kind == "curve"
    assert out.name == "rec (photo-activation)"
    assert out.columns == ["power", "activation", "error", "normalized"]
    assert out.data.shape == (6, 4)
    assert np.allclose(out.data[:, 0], powers)               # power column
    payload = out.plot_payloads()[0]
    assert payload.x_label == "activation power"


def test_processor_unknown_mode_raises(plugin):
    result = types.SimpleNamespace(name="rec", data=_decaying_stack(), axis_labels=["T", "Y", "X"])
    with pytest.raises(ValueError):
        plugin.PhotophysicsProcessor().apply(result, {"mode": "bogus"})
