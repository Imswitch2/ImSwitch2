"""The COMET drift-correction processor (optional ``comet-smlm`` package)."""

import sys

import numpy as np
import pytest

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
    SmlmCometDriftProcessor,
    comet_installed,
)
from imswitch.improcess.processors.smlm_drift_comet.processor import summarize_run

N_EMITTERS = 60


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


# --- no package needed ---------------------------------------------------------


def test_comet_processor_is_registered_and_accepts_only_localizations():
    assert "smlm-drift-comet" in available_processor_ids()
    processor = SmlmCometDriftProcessor()
    assert processor.accepts(_result(_drifting_cloud()))

    from imswitch.improcess.model.result import ProcessingResult

    image = ProcessingResult(name="img", data=np.zeros((4, 4)), axis_labels=["Y", "X"])
    assert not processor.accepts(image)


def test_window_size_is_refused_when_unset_before_the_package_is_needed(monkeypatch):
    # The refusal names the missing value; it must not depend on comet being
    # importable, since defaults are checked before the import.
    monkeypatch.setitem(sys.modules, "comet", None)
    with pytest.raises(ValueError, match="no default window size"):
        SmlmCometDriftProcessor().apply(_result(_drifting_cloud()), {"backend": "cpu"})


def test_missing_package_fails_with_the_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "comet", None)
    assert comet_installed() is False
    with pytest.raises(RuntimeError, match="comet-smlm") as info:
        SmlmCometDriftProcessor().apply(_result(_drifting_cloud()), _PARAMS)
    assert INSTALL_HINT in str(info.value)


def test_widget_builds_without_the_package_and_says_so(monkeypatch, qapp):
    from qtpy import QtWidgets

    monkeypatch.setitem(sys.modules, "comet", None)
    widget = SmlmCometDriftProcessor().make_param_widget(None)
    labels = [label.text() for label in widget.findChildren(QtWidgets.QLabel)]
    assert any("comet-smlm" in text for text in labels)
    assert widget.get_values() == SmlmCometDriftProcessor.default_params()


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
    # COMET references the trace to the acquisition's centre, not its first
    # window: over 39 frames at 4 nm/frame the trace spans 156 nm, ±78 nm.
    assert corrected.drift_x_nm[-1] - corrected.drift_x_nm[0] == pytest.approx(39 * 4.0, rel=0.05)
    assert meta["drift_max_nm"] == pytest.approx(39 * 4.0 / 2, rel=0.05)
    assert meta["comet_version"] == comet.__version__
    assert meta["drift_params"]["window_size"] == 8
    payload = corrected.plot_payloads()[0]
    assert payload.title == "Estimated drift"
    assert {series.name for series in payload.series} == {"X drift", "Y drift"}


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
