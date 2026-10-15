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
from imswitch.improcess.processors.smlm_drift_comet.processor import (
    PAIR_WARNING_THRESHOLD,
    ProgressReporter,
    summarize_run,
)

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


def _fake_comet(captured: dict, n_frames=40, estimated_pairs=1234):
    """A stand-in ``comet`` module that records what it was called with.

    Its ``comet_run_kd`` reports a few progress stages the way the real one
    does before returning a flat drift.
    """

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
        progress = kwargs.get("progress")
        if progress is not None:
            progress("segmentation", {"n_segments": 8, "n_locs_valid": len(dataset),
                                      "median_locs_per_segment": 300.0})
            progress("pairs_start", {"n_locs_valid": len(dataset), "radius_nm": 300.0})
            progress("pairs_done", {"n_pairs": 1234, "auto_downsampled": False,
                                    "max_locs_per_segment": None})
            progress("run_start", {"run": 1, "sigma_nm": 100.0, "n_failures": 0})
            for count in range(1, 51):
                progress("evaluation", {"run": 1, "sigma_nm": 100.0,
                                        "n_evaluations": count, "cost": -1.5})
            progress("run_end", {"run": 1, "sigma_nm": 100.0, "success": True,
                                 "nit": 12, "nfev": 50, "accepted": True})
            progress("interpolation", {"n_valid_segments": 8})
            progress("apply", {"n_locs": len(dataset)})
        drift = np.zeros((n_frames, 4))
        drift[:, 3] = np.arange(n_frames)
        return drift, dataset, Details()

    fake = types.ModuleType("comet")
    fake.comet_run_kd = comet_run_kd
    fake.estimate_pairs = lambda coordinates, radius: estimated_pairs
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
    assert set(defaults) == {"window_mode", "window_size", "max_drift_nm", "backend", "stage"}
    assert defaults["stage"] == "run"
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
    # The declared defaults are what the (disabled) controls hold; the stage
    # is the panel's own business (estimate first) and declared volatile.
    values = widget.get_values()
    assert values.pop("stage") == "estimate"
    defaults = SmlmCometDriftProcessor.default_params()
    defaults.pop("stage")
    assert values == defaults


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


def test_the_pair_estimate_and_the_progress_are_printed_for_the_panel(monkeypatch, capsys):
    captured = {}
    monkeypatch.setitem(sys.modules, "comet", _fake_comet(captured))

    corrected = SmlmCometDriftProcessor().apply(_result(_drifting_cloud()), _PARAMS)

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "Estimated pairs within 300 nm: 1,234"
    assert not any(line.startswith("Warning") for line in lines)
    assert "8 windows over 2,400 localizations (median 300 per window)" in lines
    assert "Searching pairs within 300 nm..." in lines
    assert "1,234 pairs found" in lines
    assert "Optimizer step 1: kernel 100 nm" in lines
    # Fifty evaluations thinned to two lines, not fifty.
    assert [line for line in lines if "evaluations, cost" in line] == [
        "  step 1: 25 evaluations, cost -1.5",
        "  step 1: 50 evaluations, cost -1.5",
    ]
    assert "  step 1 done at 100 nm: accepted after 12 iterations (50 evaluations)" in lines
    assert "Correcting 2,400 localizations" in lines
    assert corrected.metadata["drift_pairs_estimated"] == 1234


def test_a_huge_pair_estimate_is_warned_about_before_the_run(monkeypatch, capsys):
    huge = PAIR_WARNING_THRESHOLD + 1
    monkeypatch.setitem(sys.modules, "comet", _fake_comet({}, estimated_pairs=huge))
    warnings = []
    processor = SmlmCometDriftProcessor()
    monkeypatch.setattr(processor._logger, "warning", lambda message, *a: warnings.append(message))

    corrected = processor.apply(_result(_drifting_cloud()), _PARAMS)

    out = capsys.readouterr().out
    assert f"Warning: {huge:,} pairs is very many" in out
    assert len(warnings) == 1 and "max drift" in warnings[0]
    assert corrected.metadata["drift_pairs_estimated"] == huge


def test_a_comet_without_an_estimate_runs_without_one(monkeypatch, capsys):
    fake = _fake_comet({})
    del fake.estimate_pairs
    monkeypatch.setitem(sys.modules, "comet", fake)
    # The real package may already be imported in this process; its
    # estimator must not be found behind the stand-in's back.
    monkeypatch.setitem(sys.modules, "comet.core.pair_indices", None)

    corrected = SmlmCometDriftProcessor().apply(_result(_drifting_cloud()), _PARAMS)

    assert "Estimated pairs" not in capsys.readouterr().out
    assert corrected.metadata["drift_pairs_estimated"] is None


def test_the_reporter_says_when_windows_were_capped_and_a_step_was_rejected():
    assert ProgressReporter.describe(
        "pairs_done", {"n_pairs": 5000, "auto_downsampled": True, "max_locs_per_segment": 900}
    ) == "5,000 pairs found; windows were capped to 900 localizations to fit in memory"
    assert ProgressReporter.describe(
        "run_end", {"run": 3, "sigma_nm": 10.0, "accepted": False, "nit": 4, "nfev": 9}
    ) == "  step 3 done at 10 nm: not accepted after 4 iterations (9 evaluations)"
    assert ProgressReporter.describe("done", {"details": None}) == ""


def test_the_widget_shows_what_the_run_prints(monkeypatch, qapp):
    monkeypatch.setitem(sys.modules, "comet", _fake_comet({}))
    widget = SmlmCometDriftProcessor().make_param_widget(None)
    widget.output_appended("Estimated pairs within 300 nm: 1,234\n")
    widget.output_appended("8 windows")
    assert widget.logView.toPlainText() == "Estimated pairs within 300 nm: 1,234\n8 windows"
    widget.before_run()
    assert widget.logView.toPlainText() == ""


def test_the_estimate_stage_counts_and_publishes_nothing_and_the_run_reuses_it(monkeypatch, capsys):
    from imswitch.improcess.processors.base import normalize_processor_output

    calls = []
    fake = _fake_comet({})
    fake.estimate_pairs = lambda coordinates, radius: calls.append(radius) or 1234
    monkeypatch.setitem(sys.modules, "comet", fake)
    processor = SmlmCometDriftProcessor()
    result = _result(_drifting_cloud())

    output = processor.apply(result, {**_PARAMS, "stage": "estimate"})

    assert normalize_processor_output(output) == ()
    assert processor.last_estimate.pairs == 1234
    assert processor.last_estimate.matches(result.result_uid, result.count, 300.0)
    out = capsys.readouterr().out
    assert "Estimated pairs within 300 nm: 1,234" in out
    assert "Pairs estimated; nothing was corrected." in out
    assert calls == [300.0]

    corrected = processor.apply(result, _PARAMS)
    assert calls == [300.0]  # the same table at the same radius: not counted again
    assert corrected.metadata["drift_pairs_estimated"] == 1234

    processor.apply(result, {**_PARAMS, "max_drift_nm": 200.0})
    assert calls == [300.0, 200.0]  # another radius: counted afresh


def test_the_panel_button_estimates_first_then_runs(monkeypatch, qapp):
    from imswitch.improcess.processors.base import normalize_processor_output
    from imswitch.improcess.view.ResultProcessorWidget import ResultProcessorWidget

    monkeypatch.setitem(sys.modules, "comet", _fake_comet({}))
    processor = SmlmCometDriftProcessor()
    panel = ResultProcessorWidget(processor)
    result = _result(_drifting_cloud())
    panel.setCurrentResult(result)
    widget = panel.paramWidget

    assert panel.runButton.text() == "Estimate pairs"
    params = panel.parameterValues()
    assert params["stage"] == "estimate"

    # What the controller does with that click, in short.
    widget.before_run()
    assert normalize_processor_output(processor.apply(result, params)) == ()
    widget.after_run([], [])

    assert panel.runButton.text() == "Run COMET"
    assert panel.parameterValues()["stage"] == "run"
    assert widget.statusLabel.text() == "1,234 pairs within 300 nm."
    assert widget.no_results_status() == "Pairs estimated. Press Run COMET to correct the drift."

    # The count is only good for this table at this radius.
    widget.driftSpin.setValue(200.0)
    assert panel.runButton.text() == "Estimate pairs"
    assert widget.estimate is None
    widget.driftSpin.setValue(300.0)
    widget.after_run([], [])
    assert panel.runButton.text() == "Run COMET"
    panel.setCurrentResult(_result(_drifting_cloud(n_frames=10)))
    assert panel.runButton.text() == "Estimate pairs"


def test_a_huge_estimate_is_said_in_the_panel(monkeypatch, qapp):
    huge = PAIR_WARNING_THRESHOLD + 1
    monkeypatch.setitem(sys.modules, "comet", _fake_comet({}, estimated_pairs=huge))
    processor = SmlmCometDriftProcessor()
    monkeypatch.setattr(processor._logger, "warning", lambda *a: None)
    widget = processor.make_param_widget(None)
    result = _result(_drifting_cloud())
    widget.setResult(result)

    processor.apply(result, {**_PARAMS, "stage": "estimate"})
    widget.after_run([], [])

    assert widget.statusLabel.text().startswith(f"{huge:,} pairs within 300 nm. Warning:")
    assert widget.run_label() == "Run COMET"


def test_a_count_made_for_another_table_is_not_adopted(monkeypatch, qapp):
    monkeypatch.setitem(sys.modules, "comet", _fake_comet({}))
    processor = SmlmCometDriftProcessor()
    widget = processor.make_param_widget(None)
    widget.setResult(_result(_drifting_cloud()))

    processor.apply(_result(_drifting_cloud(n_frames=10)), {**_PARAMS, "stage": "estimate"})
    widget.after_run([], [])

    assert widget.estimate is None
    assert widget.run_label() == "Estimate pairs"


def test_the_controller_shows_the_widgets_word_for_a_run_that_made_nothing():
    from types import SimpleNamespace

    from imswitch.improcess.controller.ResultProcessorController import finish_run

    statuses = []
    panel = SimpleNamespace(
        processor=SimpleNamespace(id="x"),
        paramWidget=SimpleNamespace(no_results_status=lambda: "Pairs estimated."),
        setStatusText=statuses.append,
        setCurrentResult=lambda r: None,
    )
    finish_run(panel, SimpleNamespace(), panel.processor, [object()], [], [], None)
    assert statuses == ["Pairs estimated."]

    panel.paramWidget = SimpleNamespace()
    finish_run(panel, SimpleNamespace(), panel.processor, [object()], [], [], None)
    assert statuses[-1] == "The processor produced no results."


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


def test_a_real_estimate_counts_what_the_run_then_finds():
    from imswitch.improcess.processors.base import normalize_processor_output

    processor = SmlmCometDriftProcessor()
    result = _result(_drifting_cloud())
    assert normalize_processor_output(processor.apply(result, {**_PARAMS, "stage": "estimate"})) == ()
    counted = processor.last_estimate.pairs
    assert counted > 0

    corrected = processor.apply(result, _PARAMS)
    assert corrected.metadata["drift_pairs"] == counted
    assert corrected.metadata["drift_pairs_estimated"] == counted


def test_a_real_run_prints_its_pair_estimate_and_progress(capsys):
    corrected = SmlmCometDriftProcessor().apply(_result(_drifting_cloud()), _PARAMS)

    out = capsys.readouterr().out
    assert out.startswith("Estimated pairs within 300 nm: ")
    assert "8 windows over 2,400 localizations" in out
    assert f"{corrected.metadata['drift_pairs']:,} pairs found" in out
    assert "Optimizer step 1: kernel 100 nm" in out
    assert "Correcting 2,400 localizations" in out
    # The estimate counts exactly what the search finds.
    assert corrected.metadata["drift_pairs_estimated"] == corrected.metadata["drift_pairs"]


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
