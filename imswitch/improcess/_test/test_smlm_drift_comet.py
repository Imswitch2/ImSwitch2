"""The COMET drift-correction processor (optional ``comet-smlm`` package)."""

import os
import sys
import types

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.imcommon.model.cancellation import (
    CancelToken,
    OperationCancelled,
    clearCurrentCancelToken,
    setCurrentCancelToken,
)
from imswitch.improcess.model import LocalizationResult, localizations_from_columns
from imswitch.improcess.processors import available_processor_ids
from imswitch.improcess.processors.smlm_drift import DriftCorrectedLocalizationResult
from imswitch.improcess.processors.smlm_drift_comet import (
    INSTALL_HINT,
    TARGET_SIGMA_NM,
    SmlmCometDriftProcessor,
    comet_installed,
    comet_problem,
)
from imswitch.improcess.processors.smlm_drift_comet.processor import summarize_run

N_EMITTERS = 60


@pytest.fixture(scope="module")
def qapp():
    from qtpy import QtWidgets

    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _drifting_cloud(n_frames=40, drift_per_frame=(4.0, -3.0, 0.0), seed=0, with_z=False):
    """A fixed emitter pattern re-observed every frame under linear drift."""
    rng = np.random.default_rng(seed)
    base_x = rng.uniform(0, 4000, N_EMITTERS)
    base_y = rng.uniform(0, 4000, N_EMITTERS)
    base_z = rng.uniform(-400, 400, N_EMITTERS) if with_z else np.zeros(N_EMITTERS)
    frames = np.repeat(np.arange(n_frames), N_EMITTERS)
    columns = {
        "frame": frames,
        "x_nm": np.tile(base_x, n_frames) + frames * drift_per_frame[0],
        "y_nm": np.tile(base_y, n_frames) + frames * drift_per_frame[1],
        "z_nm": np.tile(base_z, n_frames) + frames * drift_per_frame[2],
        "photons": np.full(frames.size, 1000.0),
    }
    return localizations_from_columns(columns)


def _result(locs, name="storm", **kwargs):
    return LocalizationResult(name, locs, pixel_size_nm=100.0, **kwargs)


#: Eight windows over the 40-frame cloud, on the CPU kernel so the test does
#: not depend on what GPU the machine has.
_PARAMS = {"window_mode": "windows", "window_size": 8, "backend": "cpu"}


def _slope(trace, frames):
    span = frames[-1] - frames[0]
    return (trace[-1] - trace[0]) / span


def _fake_comet(captured: dict, n_frames=40):
    """A stand-in ``comet`` module that records what it was called with."""

    class Details:
        frame_range = (0, n_frames - 1)
        backend = "cpu"
        n_pairs = 1234
        sigma_accepted_nm = TARGET_SIGMA_NM
        auto_downsampled = False
        timings_s = {"pairs": 0.1, "optimization": 0.2}
        knot_frames = np.arange(8)
        segmentation = None

    def comet_run_kd(dataset, **kwargs):
        captured.update(kwargs)
        drift = np.zeros((n_frames, 4))
        drift[:, 3] = np.arange(n_frames)
        return drift, dataset, Details()

    fake = types.ModuleType("comet")
    fake.comet_run_kd = comet_run_kd
    fake.available_backends = lambda: ["cpu"]
    fake.__version__ = "fake"
    return fake


# --- no package needed ---------------------------------------------------------


def test_comet_processor_is_registered_and_accepts_only_localizations():
    assert "smlm-drift-comet" in available_processor_ids()
    processor = SmlmCometDriftProcessor()
    assert processor.accepts(_result(_drifting_cloud()))

    from imswitch.improcess.model.result import ProcessingResult

    image = ProcessingResult(name="img", data=np.zeros((4, 4)), axis_labels=["Y", "X"])
    assert not processor.accepts(image)


def test_the_panel_defaults_to_500_localizations_per_window():
    defaults = SmlmCometDriftProcessor.default_params()
    assert defaults["window_mode"] == "localizations"
    assert defaults["window_size"] == 500
    # The former advanced group is gone from the contract; the two settings a
    # script may still want are accepted without being offered.
    assert set(defaults) == {"window_mode", "window_size", "max_drift_nm", "backend"}
    assert SmlmCometDriftProcessor.param_keys() == set(defaults) | {"max_locs_per_window", "seed"}


def test_a_non_positive_window_size_is_refused_before_the_package_is_needed(monkeypatch):
    monkeypatch.setitem(sys.modules, "comet", None)
    with pytest.raises(ValueError, match="positive count"):
        SmlmCometDriftProcessor().apply(
            _result(_drifting_cloud()), {"window_size": 0, "backend": "cpu"}
        )


def test_missing_package_fails_with_the_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "comet", None)
    assert comet_installed() is False
    assert INSTALL_HINT in comet_problem()
    with pytest.raises(RuntimeError, match="comet-smlm") as info:
        SmlmCometDriftProcessor().apply(_result(_drifting_cloud()), _PARAMS)
    assert INSTALL_HINT in str(info.value)


def test_a_broken_install_is_reported_as_broken_not_missing(monkeypatch):
    import importlib

    original = importlib.import_module

    def explode(name, package=None):
        if name == "comet":
            raise OSError("numba could not load its DLL")
        return original(name, package)

    monkeypatch.setattr(importlib, "import_module", explode)
    problem = comet_problem()
    assert "installed but cannot be imported" in problem
    assert "numba could not load its DLL" in problem
    assert INSTALL_HINT in problem


def test_check_inputs_refuses_a_run_without_the_package(monkeypatch):
    monkeypatch.setitem(sys.modules, "comet", None)
    ok, reason = SmlmCometDriftProcessor().check_inputs([_result(_drifting_cloud())])
    assert ok is False
    assert "comet-smlm" in reason

    monkeypatch.setitem(sys.modules, "comet", _fake_comet({}))
    assert SmlmCometDriftProcessor().check_inputs([_result(_drifting_cloud())]) == (True, "")


def test_the_widget_says_so_and_disables_its_controls_without_the_package(monkeypatch, qapp):
    from qtpy import QtWidgets

    monkeypatch.setitem(sys.modules, "comet", None)
    widget = SmlmCometDriftProcessor().make_param_widget(None)
    labels = [label.text() for label in widget.findChildren(QtWidgets.QLabel)]
    assert any("comet-smlm" in text for text in labels)
    assert widget.problem
    assert not widget.form.isEnabled()
    assert widget.installEdit.text() == INSTALL_HINT
    # The declared defaults are what the (disabled) controls hold.
    assert widget.get_values() == SmlmCometDriftProcessor.default_params()


def test_check_again_enables_the_widget_once_the_package_appears(monkeypatch, qapp):
    monkeypatch.setitem(sys.modules, "comet", None)
    widget = SmlmCometDriftProcessor().make_param_widget(None)
    changes = []
    widget.sigRunStateChanged.connect(lambda: changes.append(True))

    widget.recheck()
    assert widget.problem and not widget.form.isEnabled()
    assert changes == []  # still missing: nothing to re-decide

    monkeypatch.setitem(sys.modules, "comet", _fake_comet({}))
    widget.recheck()
    assert widget.problem == ""
    assert widget.form.isEnabled()
    assert widget.availabilityBox.isHidden()
    assert changes == [True]


def test_the_panel_keeps_run_off_until_the_package_is_found(monkeypatch, qapp):
    from imswitch.improcess.view.ResultProcessorWidget import ResultProcessorWidget

    monkeypatch.setitem(sys.modules, "comet", None)
    panel = ResultProcessorWidget(SmlmCometDriftProcessor())
    panel.setCurrentResult(_result(_drifting_cloud()))
    assert not panel.runButton.isEnabled()
    assert "comet-smlm" in panel.statusLabel.text()

    monkeypatch.setitem(sys.modules, "comet", _fake_comet({}))
    panel.paramWidget.recheck()
    assert panel.runButton.isEnabled()
    assert "comet-smlm" not in panel.statusLabel.text()


def test_apply_hands_comet_the_fixed_target_sigma_and_its_own_defaults(monkeypatch):
    captured = {}
    monkeypatch.setitem(sys.modules, "comet", _fake_comet(captured))

    corrected = SmlmCometDriftProcessor().apply(
        _result(_drifting_cloud()), SmlmCometDriftProcessor.default_params()
    )

    assert captured["segmentation_mode"] == 1  # localizations per window
    assert captured["segmentation_var"] == 500
    assert captured["max_drift_nm"] == 300.0
    assert captured["target_sigma_nm"] == TARGET_SIGMA_NM == 10.0
    assert captured["mode"] is None  # auto
    # Not passed, so COMET's own: initial sigma = max drift / 3, no boxcar,
    # cubic interpolation.
    for key in ("initial_sigma_nm", "boxcar_width", "interpolation_method"):
        assert key not in captured
    assert captured["max_locs_per_segment"] is None
    assert captured["random_state"] is None
    assert corrected.metadata["drift_target_sigma_nm"] == 10.0
    assert corrected.metadata["drift_windows"] == 8


def test_the_extra_keys_reach_comet_without_being_in_the_panel(monkeypatch):
    captured = {}
    monkeypatch.setitem(sys.modules, "comet", _fake_comet(captured))
    SmlmCometDriftProcessor().apply(
        _result(_drifting_cloud()),
        {**_PARAMS, "max_locs_per_window": 100, "seed": 7},
    )
    assert captured["max_locs_per_segment"] == 100
    assert captured["random_state"] == 7


def test_summarize_run_reads_the_recorded_metadata():
    line = summarize_run(
        {
            "drift_method": "comet",
            "drift_backend": "cpu",
            "drift_windows": 8,
            "drift_pairs": 97622,
            "drift_max_nm": 78.0,
            "drift_timings_s": {"pairs": 0.25, "optimization": 0.5},
        }
    )
    assert line == "cpu backend, 8 windows, 97622 pairs, max drift 78.0 nm, 0.8 s"
    assert summarize_run({}) == ""


# --- with the package ---------------------------------------------------------


comet = pytest.importorskip("comet")


def test_processor_recovers_linear_drift_and_leaves_the_input_alone():
    locs = _drifting_cloud()
    result = _result(locs)
    x_before = result.locs.x_nm.copy()

    corrected = SmlmCometDriftProcessor().apply(result, _PARAMS)

    assert isinstance(corrected, DriftCorrectedLocalizationResult)
    assert corrected.kind == "localization"
    assert corrected.count == result.count
    np.testing.assert_array_equal(result.locs.x_nm, x_before)

    assert _slope(corrected.drift_x_nm, corrected.drift_frames) == pytest.approx(4.0, rel=0.05)
    assert _slope(corrected.drift_y_nm, corrected.drift_frames) == pytest.approx(-3.0, rel=0.05)
    assert corrected.drift_frames[0] == 0 and corrected.drift_frames[-1] == 39
    assert corrected.drift_z_nm is None

    x_spread_before = locs.x_nm.reshape(-1, N_EMITTERS).std(axis=0).mean()
    x_spread_after = corrected.locs.x_nm.reshape(-1, N_EMITTERS).std(axis=0).mean()
    assert x_spread_after < 0.05 * x_spread_before

    meta = corrected.metadata
    assert meta["drift_method"] == "comet"
    assert meta["drift_backend"] == "cpu"
    assert meta["drift_windows"] == 8
    assert meta["drift_pairs"] > 0
    assert meta["drift_target_sigma_nm"] == TARGET_SIGMA_NM
    # COMET references the trace to the acquisition's centre, not its first
    # window: over 39 frames at 4 nm/frame the trace spans 156 nm, ±78 nm.
    assert corrected.drift_x_nm[-1] - corrected.drift_x_nm[0] == pytest.approx(39 * 4.0, rel=0.05)
    assert meta["drift_max_nm"] == pytest.approx(39 * 4.0 / 2, rel=0.05)
    assert meta["comet_version"] == comet.__version__
    assert meta["drift_params"]["window_size"] == 8
    payload = corrected.plot_payloads()[0]
    assert payload.title == "Estimated drift"
    assert {series.name for series in payload.series} == {"X drift", "Y drift"}


def test_the_default_window_runs_on_a_real_cloud():
    # 60 emitters per frame over 40 frames: 500 localizations per window is
    # about eight frames per window, which resolves a linear drift.
    corrected = SmlmCometDriftProcessor().apply(
        _result(_drifting_cloud()), {**SmlmCometDriftProcessor.default_params(), "backend": "cpu"}
    )
    assert _slope(corrected.drift_x_nm, corrected.drift_frames) == pytest.approx(4.0, rel=0.1)
    assert corrected.metadata["drift_params"]["window_mode"] == "localizations"


def test_processor_corrects_z_on_a_3d_table():
    result = _result(_drifting_cloud(drift_per_frame=(4.0, -3.0, 2.0), with_z=True))
    assert result.dims == "3D"

    corrected = SmlmCometDriftProcessor().apply(result, _PARAMS)

    assert corrected.drift_z_nm is not None
    assert _slope(corrected.drift_z_nm, corrected.drift_frames) == pytest.approx(2.0, rel=0.05)
    z_before = result.locs.z_nm.reshape(-1, N_EMITTERS).std(axis=0).mean()
    z_after = corrected.locs.z_nm.reshape(-1, N_EMITTERS).std(axis=0).mean()
    assert z_after < 0.05 * z_before
    assert {series.name for series in corrected.plot_payloads()[0].series} == {
        "X drift", "Y drift", "Z drift",
    }


def test_trace_starts_at_the_first_frame_of_the_data():
    locs = _drifting_cloud()
    locs.frame += 100
    corrected = SmlmCometDriftProcessor().apply(_result(locs), _PARAMS)
    assert corrected.drift_frames[0] == 100
    assert corrected.drift_frames[-1] == 139
    assert len(corrected.drift_frames) == 40


def test_auto_backend_is_one_comet_offers():
    corrected = SmlmCometDriftProcessor().apply(
        _result(_drifting_cloud()), {**_PARAMS, "backend": "auto"}
    )
    assert corrected.metadata["drift_backend"] in comet.available_backends()


@pytest.mark.skipif(comet.cuda_available(), reason="CUDA is available here")
def test_unavailable_backend_is_refused_with_the_available_ones():
    with pytest.raises(RuntimeError, match="not available") as info:
        SmlmCometDriftProcessor().apply(_result(_drifting_cloud()), {**_PARAMS, "backend": "cuda"})
    assert "cpu" in str(info.value)


def test_too_coarse_a_window_surfaces_comets_own_reason():
    with pytest.raises(ValueError, match="Reduce segmentation_var"):
        SmlmCometDriftProcessor().apply(
            _result(_drifting_cloud()),
            {"window_mode": "frames", "window_size": 1000, "backend": "cpu"},
        )


def test_a_cancelled_run_stops_at_comets_first_report_and_changes_nothing():
    result = _result(_drifting_cloud())
    x_before = result.locs.x_nm.copy()
    token = CancelToken()
    token.requestStop()
    setCurrentCancelToken(token)
    try:
        with pytest.raises(OperationCancelled):
            SmlmCometDriftProcessor().apply(result, _PARAMS)
    finally:
        clearCurrentCancelToken()
    np.testing.assert_array_equal(result.locs.x_nm, x_before)


def test_a_seeded_capped_run_repeats_exactly():
    params = {**_PARAMS, "max_locs_per_window": 100, "seed": 7}
    first = SmlmCometDriftProcessor().apply(_result(_drifting_cloud()), params)
    second = SmlmCometDriftProcessor().apply(_result(_drifting_cloud()), params)
    np.testing.assert_array_equal(first.drift_x_nm, second.drift_x_nm)
    assert first.metadata["drift_params"]["seed"] == 7
