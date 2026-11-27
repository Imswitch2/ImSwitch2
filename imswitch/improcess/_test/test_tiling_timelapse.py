"""A timelapse of tilings reconstructed as one stack: discovery, one canvas, layout across time, drift."""

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcommon.algorithms import tile_timelapse as ttl
from imswitch.imcommon.algorithms.tile_mosaic import (
    LayoutOptions,
    PayloadSelection,
    inspect_dataset,
    solve_layout,
)
from imswitch.improcess._test import _tiling_timelapse_fixtures as fx
from imswitch.improcess.model import series as sm
from imswitch.improcess.model.dataset_sources import resolve_dataset_source
from imswitch.improcess.model.provenance import graph_of, output_node
from imswitch.improcess.reconstructors.base import (
    CancellationToken,
    ReconstructionCancelled,
    ReconstructionContext,
)
from imswitch.improcess.reconstructors.run import run_reconstruction
from imswitch.improcess.reconstructors.tiling import TilingReconstructor
from imswitch.improcess.workflows.sources import SourceSpec, open_source

TILE, STEP, GRID = (96, 96), (72, 72), (3, 3)


def _params(**overrides):
    values = {**TilingReconstructor.default_params(), **overrides}
    return values


@pytest.fixture(scope="module")
def scene():
    return fx.smooth_scene((360, 360), seed=3)


@pytest.fixture(scope="module")
def jitter():
    return fx.random_jitter(9, amplitude=4, seed=5)


def _timelapse(path, scene, jitter, timepoints=4, **kwargs):
    return fx.write_timelapse(path, scene, timepoints, nx=3, ny=3, tile=TILE, step_px=STEP,
                              jitter=jitter, **kwargs)


def _source(path):
    return open_source(SourceSpec(path=str(path)))


def _run(path, **params):
    return run_reconstruction(TilingReconstructor(), _source(path), _params(**params)).result


# ----------------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------------

def test_a_timelapse_is_found_from_a_tile_a_timepoint_folder_or_its_manifest(tmp_path, scene, jitter):
    manifest = _timelapse(tmp_path / "tl", scene, jitter, timepoints=3, interval_s=30.0)
    for opened in (manifest, manifest.parent, manifest.parent / "t1",
                   manifest.parent / "t1" / "tiles.json",
                   next((manifest.parent / "t1").glob("*.tiff"))):
        series = ttl.read_timelapse(opened)
        assert series is not None and series.planned == 3 and series.manifest == manifest
    info = sm.discover_series(str(next((manifest.parent / "t2").glob("*.tiff"))))
    assert info.kind == sm.KIND_TILING_TIMELAPSE and info.anchor == 2 and not info.opened_as_series
    assert info.member_kind == "tiling-manifest" and info.times_s() == [0.0, 30.0, 60.0]
    assert sm.discover_series(str(manifest)).opened_as_series


def test_complete_incomplete_missing_and_unrecorded_timepoints(tmp_path, scene, jitter):
    manifest = _timelapse(tmp_path / "tl", scene, jitter, timepoints=6, incomplete=(2,), missing=(4, 5),
                          unrecorded=(3,))
    series = ttl.read_timelapse(manifest)
    states = {p.index: p.state for p in series.points}
    assert states == {0: "complete", 1: "complete", 2: "incomplete", 3: "incomplete", 4: "missing", 5: "missing"}
    assert "stopped" in series.points[2].reason and "interrupted" in series.points[3].reason
    assert series.counts() == {"complete": 2, "incomplete": 2, "missing": 2}
    assert series.points[0].time_s == 0.0 and series.points[1].time_s == 60.0
    assert "2 complete" in series.describe() and "2 missing" in series.describe()


def test_grids_are_compared_and_the_odd_one_out_is_named(tmp_path, scene, jitter):
    manifest = _timelapse(tmp_path / "tl", scene, jitter, timepoints=4,
                          grid_overrides={2: {"nx": 2, "ny": 3}})
    verdict, parsed = ttl.compatibility(ttl.read_timelapse(manifest))
    assert verdict.compatible == (0, 1, 3) and not verdict.all_compatible
    assert set(verdict.incompatible) == {2} and "grid" in verdict.incompatible[2][0]
    assert sorted(parsed) == [0, 1, 2, 3]
    clean = _timelapse(tmp_path / "clean", scene, jitter, timepoints=3)
    assert ttl.compatibility(ttl.read_timelapse(clean))[0].all_compatible
    differs = _timelapse(tmp_path / "px", scene, jitter, timepoints=2, grid_overrides={1: {"pixel_um": 0.25}})
    assert "pixel size" in " ".join(ttl.compatibility(ttl.read_timelapse(differs))[0].incompatible[1])


def test_a_plain_tiling_is_not_a_timelapse_and_behaves_as_before(tmp_path, scene, jitter):
    manifest = fx.write_tiling(tmp_path / "plain" / "run", scene, jitter=jitter, tile=TILE, step_px=STEP)
    assert ttl.read_timelapse(manifest) is None and sm.discover_series(str(manifest)) is None
    source = _source(manifest)
    assert sm.discover_series(source) is None
    inspection = TilingReconstructor().inspect_source(source)
    assert "series" not in inspection.metadata
    result = run_reconstruction(TilingReconstructor(), source, _params(refine=False)).result
    assert result.axis_labels == ["Y", "X"] and result.timelapse is None
    asked = run_reconstruction(TilingReconstructor(), _source(manifest), _params(refine=False, _series="all")).result
    assert asked.data.shape == result.data.shape and asked.axis_labels == ["Y", "X"]
    # A folder named like a timepoint, with no manifest above it, is not one either.
    lone = fx.write_tiling(tmp_path / "other" / "t0", scene, tile=TILE, step_px=STEP)
    assert ttl.read_timelapse(lone) is None


def test_the_timelapse_manifest_opens_and_unknown_keys_are_ignored(tmp_path, scene, jitter):
    manifest = _timelapse(tmp_path / "tl", scene, jitter, timepoints=2,
                          extra_settings={"tilt_plane": {"calibration_path": "x", "slope_x_um_per_um": 0.1},
                                          "save_only": True})
    raw = json.loads(manifest.read_text())
    raw["some_future_key"] = {"a": 1}
    manifest.write_text(json.dumps(raw))
    for opened in (manifest, manifest.parent):
        resolved = resolve_dataset_source(str(opened), allowed_specs=_tiling_specs())
        assert resolved.path == manifest.parent / "t0" / "tiles.json"
    source = _source(manifest)
    assert source.sourceKind == "tiling-manifest" and Path(source.dataPath).parent.name == "t0"
    assert sm.discover_series(source).opened_as_series
    assert ttl.read_timelapse(manifest).tiling_settings["tilt_plane"]["slope_x_um_per_um"] == 0.1


def _tiling_specs():
    from imswitch.improcess.model.dataset_sources import SOURCE_SPECS, TILING_MANIFEST_SPEC, TILING_RUN_SPEC

    return [*SOURCE_SPECS, TILING_MANIFEST_SPEC, TILING_RUN_SPEC]


def test_the_source_inspection_reports_the_timelapse(tmp_path, scene, jitter):
    manifest = _timelapse(tmp_path / "tl", scene, jitter, timepoints=5, incomplete=(1,), missing=(4,),
                          grid_overrides={3: {"nx": 2, "ny": 3}})
    inspection = TilingReconstructor().inspect_source(_source(manifest))
    series = inspection.metadata["series"]
    assert series["planned"] == 5 and series["counts"] == {"complete": 3, "incomplete": 1, "missing": 1}
    assert series["states"] == ["complete", "incomplete", "complete", "complete", "missing"]
    assert series["times_s"][:3] == [0.0, 60.0, 120.0] and series["opened_as_series"]
    assert not series["compatible"] and series["incompatible"] == [3]
    assert "different grid" in inspection.warning


# ----------------------------------------------------------------------------
# One canvas, every mode
# ----------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["stage", "shared", "shared+drift", "independent"])
def test_every_timepoint_is_on_the_same_canvas_in_every_mode(tmp_path, scene, jitter, mode):
    drifts = {t: (2.0 * t, -1.0 * t) for t in range(4)}
    manifest = _timelapse(tmp_path / "tl", scene, jitter, timepoints=4, noise=0.3, seed=2, drifts=drifts)
    result = _run(manifest, time_alignment=mode)
    assert result.data.shape[0] == 4 and result.axis_labels == ["T", "Y", "X"]
    height, width = result.data.shape[1:]
    record = result.timelapse
    assert record["canvas"]["shape_yx"] == [height, width]
    assert record["canvas"]["origin_yx"] == list(result.output_origin_yx)
    # Every plane holds a mosaic that fills (almost) the whole canvas: nothing was cropped away.
    for plane in np.asarray(result.data):
        assert (plane > 0).mean() > 0.9
    # The canvas holds every tile of every timepoint: a tile-sized margin is not needed.
    assert height >= 3 * 72 + 24 and width >= 3 * 72 + 24
    assert result.axis_scales[0] == pytest.approx(60.0) and result.axis_scales[1:] == [0.5, 0.5]
    assert result.timelapse["alignment_mode"] == mode


def test_planes_agree_with_a_single_tiling_when_nothing_moves(tmp_path, scene, jitter):
    manifest = _timelapse(tmp_path / "tl", scene, jitter, timepoints=3)
    stack = _run(manifest, time_alignment="shared", refine=False)
    single = run_reconstruction(
        TilingReconstructor(), _source(manifest.parent / "t1" / "tiles.json"),
        _params(refine=False, _series="this")).result
    assert single.axis_labels == ["Y", "X"]
    np.testing.assert_allclose(np.asarray(stack.data)[1], np.asarray(single.data))


def test_this_timepoint_only_and_a_range(tmp_path, scene, jitter):
    manifest = _timelapse(tmp_path / "tl", scene, jitter, timepoints=6)
    member = manifest.parent / "t2" / "tiles.json"
    default = run_reconstruction(TilingReconstructor(), _source(member), _params(refine=False)).result
    assert default.data.shape[0] == 6                      # the tiling reconstructor defaults to all
    this = run_reconstruction(TilingReconstructor(), _source(member), _params(refine=False, _series="this")).result
    assert this.axis_labels == ["Y", "X"]
    ranged = run_reconstruction(TilingReconstructor(), _source(member),
                                _params(refine=False, _series="range", _series_first=1, _series_last=5,
                                        _series_step=2)).result
    assert ranged.data.shape[0] == 3 and ranged.metadata[sm.METADATA_KEY]["timepoints"] == [1, 3, 5]
    assert ranged.metadata[sm.METADATA_KEY]["t_scale_s"] == pytest.approx(60.0)


def test_incomplete_missing_and_incompatible_timepoints_are_reported(tmp_path, scene, jitter):
    manifest = _timelapse(tmp_path / "tl", scene, jitter, timepoints=6, incomplete=(1,), missing=(5,),
                          grid_overrides={3: {"nx": 2, "ny": 3}}, interval_s=30.0)
    skipped = _run(manifest, refine=False)
    record = skipped.metadata[sm.METADATA_KEY]
    assert skipped.data.shape[0] == 3 and record["timepoints"] == [0, 2, 4]
    reasons = {item["index"]: item["why"] for item in record["left_out"]}
    assert set(reasons) == {1, 3, 5} and "different grid" in reasons[3] and "missing" in reasons[5]
    assert record["times_s"] == [0.0, 60.0, 120.0] and record["t_scale_s"] == pytest.approx(30.0)
    zero = _run(manifest, refine=False, _series_missing="zeros")
    assert zero.data.shape[0] == 6 and record["t_regular"]
    assert float(np.abs(np.asarray(zero.data)[1]).max()) == 0.0 and np.asarray(zero.data)[0].max() > 0
    assert zero.timelapse["filled"] == [1, 3, 5] and zero.axis_scales[0] == pytest.approx(30.0)
    nan = _run(manifest, refine=False, _series_missing="nan")
    assert np.isnan(np.asarray(nan.data)[3]).all() and not np.isnan(np.asarray(nan.data)[2]).any()
    assert [p["index"] for p in nan.timelapse["planes"]] == [0, 1, 2, 3, 4, 5]
    assert [p["used"] for p in nan.timelapse["planes"]] == [True, False, True, False, True, False]


def test_independent_mode_may_jitter_but_keeps_the_canvas(tmp_path, scene, jitter):
    manifest = _timelapse(tmp_path / "tl", scene, jitter, timepoints=3, noise=2.0, seed=4)
    own = _run(manifest, time_alignment="independent")
    shared = _run(manifest, time_alignment="shared")
    assert own.data.shape == shared.data.shape or own.data.shape[0] == shared.data.shape[0] == 3
    layouts = own.timelapse["layout"]["positions_yx"]
    assert set(layouts) == {"0", "1", "2"}
    assert own.timelapse["layout_source"] == "own"
    assert len({tuple(map(tuple, v.values())) for v in layouts.values()}) >= 1


# ----------------------------------------------------------------------------
# Layout across time: the shared estimate against single-timepoint registration
# ----------------------------------------------------------------------------

def _layout_errors(tmp_path, scene, jitter, noise, timepoints, seed):
    manifest = _timelapse(tmp_path / f"n{noise}_{timepoints}_{seed}", scene, jitter, timepoints=timepoints,
                          noise=noise, seed=seed)
    _verdict, parsed = ttl.compatibility(ttl.read_timelapse(manifest))
    truth = fx.true_positions(*GRID, STEP, jitter)
    options = LayoutOptions(stage_positions=True, refine=True)
    per = [fx.layout_error(solve_layout(index, options).positions_yx, truth, *GRID)
           for _t, index in sorted(parsed.items())]
    shared, _ = ttl.solve_shared_layout(parsed, sorted(parsed), options)
    # The same estimator given one timepoint at a time: how much of the gain is the
    # accumulation rather than the estimator itself.
    single = [fx.layout_error(ttl.solve_shared_layout(parsed, [t], options)[0].positions_yx, truth, *GRID)
              for t in sorted(parsed)]
    nominal = solve_layout(parsed[0], LayoutOptions(refine=False))
    return {
        "nominal": fx.layout_error(nominal.positions_yx, truth, *GRID),
        "per_timepoint": float(np.mean(per)),
        "per_timepoint_worst": float(np.max(per)),
        "one_timepoint_same_estimator": float(np.mean(single)),
        "shared": fx.layout_error(shared.positions_yx, truth, *GRID),
    }


def test_one_timepoint_through_the_shared_estimator_matches_ordinary_registration(tmp_path, scene, jitter):
    manifest = _timelapse(tmp_path / "tl", scene, jitter, timepoints=1)
    _verdict, parsed = ttl.compatibility(ttl.read_timelapse(manifest))
    truth = fx.true_positions(*GRID, STEP, jitter)
    options = LayoutOptions(stage_positions=True, refine=True)
    ordinary = solve_layout(parsed[0], options)
    shared, report = ttl.solve_shared_layout(parsed, [0], options)
    # Same links to a tenth of a pixel, so the same layout.
    for tile_id, position in ordinary.positions_yx.items():
        assert shared.positions_yx[tile_id] == pytest.approx(position, abs=0.25)
    assert report.pairs == report.pairs_measured == 20


def test_the_shared_layout_beats_registering_each_timepoint_on_noisy_data(tmp_path, scene, jitter, capsys):
    rows = []
    for noise in (1.5, 2.5, 3.5):
        errors = [_layout_errors(tmp_path, scene, jitter, noise, 8, seed) for seed in (1, 2, 3)]
        mean = {key: float(np.mean([e[key] for e in errors])) for key in errors[0]}
        rows.append((noise, mean))
    with capsys.disabled():
        print("\nlayout error (px RMS vs ground truth), 3x3 grid, T=8, mean of 3 noise seeds")
        print("noise/std  nominal  per-timepoint  worst-timepoint  1 timepoint (shared est.)  shared")
        for noise, mean in rows:
            print(f"{noise:9.1f}  {mean['nominal']:7.2f}  {mean['per_timepoint']:13.2f}  "
                  f"{mean['per_timepoint_worst']:15.2f}  {mean['one_timepoint_same_estimator']:23.2f}  "
                  f"{mean['shared']:6.2f}")
    for noise, mean in rows:
        assert mean["shared"] < mean["per_timepoint"], (noise, mean)
        assert mean["shared"] < mean["one_timepoint_same_estimator"]
        assert mean["shared"] < mean["nominal"]
    # The gap opens as the data get worse.
    assert rows[-1][1]["per_timepoint"] - rows[-1][1]["shared"] > rows[0][1]["per_timepoint"] - rows[0][1]["shared"] - 0.2


def test_correlation_surfaces_are_accumulated_in_batches_without_changing_the_answer(tmp_path, scene, jitter):
    manifest = _timelapse(tmp_path / "tl", scene, jitter, timepoints=3, noise=0.5, seed=7)
    _verdict, parsed = ttl.compatibility(ttl.read_timelapse(manifest))
    options = LayoutOptions(stage_positions=True, refine=True)
    whole, report_whole = ttl.solve_shared_layout(parsed, [0, 1, 2], options)
    batched, report_batched = ttl.solve_shared_layout(parsed, [0, 1, 2], options, accumulator_bytes=1)
    assert report_whole.passes == 1 and report_batched.passes == report_batched.pairs > 1
    for tile_id, position in whole.positions_yx.items():
        assert batched.positions_yx[tile_id] == pytest.approx(position, abs=1e-6)


# ----------------------------------------------------------------------------
# Drift between timepoints
# ----------------------------------------------------------------------------

def test_drift_is_recovered_and_the_canvas_is_padded_once(tmp_path, scene, jitter):
    drifts = {0: (0.0, 0.0), 1: (3.0, -2.0), 2: (6.0, -4.0), 3: (8.0, -7.0)}
    manifest = _timelapse(tmp_path / "tl", scene, jitter, timepoints=4, noise=0.3, seed=2, drifts=drifts)
    _verdict, parsed = ttl.compatibility(ttl.read_timelapse(manifest))
    estimate = ttl.estimate_drift(parsed, [0, 1, 2, 3], 0)
    for t, (dy, dx) in drifts.items():
        # The correction is the negative of how far the sample moved.
        assert estimate.shift_yx[t] == pytest.approx((-dy, -dx), abs=0.3)
    assert estimate.shift_yx[0] == (0.0, 0.0) and all(v >= 5 for v in estimate.votes[1:])
    result = _run(manifest, time_alignment="shared+drift")
    recorded = np.asarray(result.timelapse["drift"]["shift_yx_px"])
    assert np.abs(recorded - np.array([[-dy, -dx] for dy, dx in drifts.values()])).max() < 0.3
    np.testing.assert_allclose(result.drift_yx, recorded)
    # The mosaic of every timepoint now shows the sample where the reference does.
    stage = _run(manifest, time_alignment="shared")
    corrected = np.asarray(result.data)
    plain = np.asarray(stage.data)

    def registration_error(stack):
        from skimage.registration import phase_cross_correlation

        h, w = stack.shape[1:]
        shifts = []
        for t in (1, 2, 3):
            shift, _e, _p = phase_cross_correlation(stack[0][40:h - 40, 40:w - 40], stack[t][40:h - 40, 40:w - 40],
                                                    upsample_factor=10)
            shifts.append(float(np.hypot(*shift)))
        return max(shifts)

    assert registration_error(plain) > 5.0
    assert registration_error(corrected) < 1.5
    assert corrected.shape[1] >= plain.shape[1] and result.data.shape[0] == 4


def test_fractional_drift_and_smoothing(tmp_path, scene, jitter):
    drifts = {t: (0.4 * t + 0.25, -0.3 * t) for t in range(6)}
    drifts[0] = (0.0, 0.0)
    manifest = _timelapse(tmp_path / "tl", scene, jitter, timepoints=6, noise=0.3, seed=2, drifts=drifts)
    _verdict, parsed = ttl.compatibility(ttl.read_timelapse(manifest))
    estimate = ttl.estimate_drift(parsed, list(range(6)), 0)
    for t, (dy, dx) in drifts.items():
        assert estimate.shift_yx[t] == pytest.approx((-dy, -dx), abs=0.35)
    smooth = ttl.estimate_drift(parsed, list(range(6)), 0, smoothing=3)
    assert smooth.smoothing == 3 and smooth.shift_yx[0] == (0.0, 0.0)
    assert smooth.as_dict()["reference"] == 0


def test_a_different_reference_timepoint_moves_the_zero_of_the_drift(tmp_path, scene, jitter):
    drifts = {0: (0.0, 0.0), 1: (4.0, 0.0), 2: (8.0, 0.0)}
    manifest = _timelapse(tmp_path / "tl", scene, jitter, timepoints=3, drifts=drifts)
    result = _run(manifest, time_alignment="shared+drift", reference_timepoint=1)
    shifts = np.asarray(result.timelapse["drift"]["shift_yx_px"])
    assert shifts[1] == pytest.approx([0.0, 0.0], abs=1e-6) and shifts[0][0] == pytest.approx(4.0, abs=0.3)
    with pytest.raises(ValueError, match="Reference timepoint 9"):
        _run(manifest, time_alignment="shared+drift", reference_timepoint=9)


def test_the_drift_is_a_table_and_a_plot(tmp_path, scene, jitter):
    drifts = {0: (0.0, 0.0), 1: (3.0, -2.0), 2: (6.0, -4.0)}
    manifest = _timelapse(tmp_path / "tl", scene, jitter, timepoints=3, drifts=drifts)
    result = _run(manifest, time_alignment="shared+drift")
    assert result.table_columns() == ["timepoint", "time_s", "drift_y_px", "drift_x_px", "note"]
    rows = result.table_records()
    assert [r["timepoint"] for r in rows] == [0, 1, 2] and rows[2]["drift_y_px"] == pytest.approx(-6.0, abs=0.3)
    payload = result.plot_payloads()[0]
    assert [s.name for s in payload.series] == ["Y shift", "X shift"]
    assert _run(manifest, time_alignment="shared").plot_payloads() == []


# ----------------------------------------------------------------------------
# Resources, progress, cancellation
# ----------------------------------------------------------------------------

def test_the_memory_estimate_scales_with_the_number_of_timepoints(tmp_path, scene, jitter):
    manifest = _timelapse(tmp_path / "tl", scene, jitter, timepoints=6)
    member = _source(manifest.parent / "t0" / "tiles.json")
    rec = TilingReconstructor()
    one = rec.estimate_resources(member, _params(_series="this"))
    allp = rec.estimate_resources(member, _params())
    assert allp.output_shape == (6, *one.output_shape)
    assert allp.canvas_bytes == 6 * one.canvas_bytes and allp.weight_bytes == one.weight_bytes
    some = rec.estimate_resources(member, _params(_series="range", _series_first=0, _series_last=5, _series_step=2))
    assert some.output_shape[0] == 3 and "6-timepoint" in allp.description
    assert "every n-th" in " ".join(allp.suggestions)


def test_beyond_the_budget_it_refuses_with_a_clear_message_until_confirmed(tmp_path, scene, jitter):
    manifest = _timelapse(tmp_path / "tl", scene, jitter, timepoints=4)
    source = _source(manifest)
    small = ReconstructionContext(memory_budget_bytes=100_000)
    with pytest.raises(MemoryError, match="4 planes.*reconstruction budget.*range or every n-th"):
        run_reconstruction(TilingReconstructor(), source, _params(refine=False), context=small)
    confirmed = ReconstructionContext(memory_budget_bytes=100_000, confirmed_over_budget=True)
    result = run_reconstruction(TilingReconstructor(), _source(manifest), _params(refine=False),
                                context=confirmed).result
    assert result.data.shape[0] == 4
    roomy = ReconstructionContext(memory_budget_bytes=10 ** 9)
    assert run_reconstruction(TilingReconstructor(), _source(manifest), _params(refine=False),
                              context=roomy).result.data.shape[0] == 4


def test_progress_is_per_timepoint_and_monotonic(tmp_path, scene, jitter):
    manifest = _timelapse(tmp_path / "tl", scene, jitter, timepoints=4)
    updates = []
    context = ReconstructionContext(progress_callback=updates.append)
    seen = []
    run_reconstruction(TilingReconstructor(), _source(manifest), _params(time_alignment="shared"),
                       context=context, progress=lambda done, total, text: seen.append(text))
    assert seen == ["T=1/4", "T=2/4", "T=3/4", "T=4/4"]
    fractions = [u.fraction for u in updates]
    assert all(b >= a - 1e-9 for a, b in zip(fractions, fractions[1:])), "the bar never steps back"
    assert fractions[-1] == pytest.approx(1.0)
    messages = [u.message for u in updates]
    assert any(m.startswith("Timepoint 1/4") for m in messages) and any(m.startswith("Timepoint 4/4") for m in messages)
    assert {u.phase for u in updates} >= {"inspect", "align", "assemble"}


def test_cancelling_between_timepoints_leaves_no_result(tmp_path, scene, jitter):
    manifest = _timelapse(tmp_path / "tl", scene, jitter, timepoints=5)
    token = CancellationToken()

    finished = []

    def cancel_after_two(update):
        if update.message == "Timepoint 2 assembled":
            finished.append(update.message)
            token.cancel()

    context = ReconstructionContext(progress_callback=cancel_after_two, cancellation_token=token)
    source = _source(manifest)
    with pytest.raises(ReconstructionCancelled):
        run_reconstruction(TilingReconstructor(), source, _params(refine=False), context=context)
    assert token.cancelled and finished == ["Timepoint 2 assembled"]
    from imswitch.imcommon.model import OperationCancelled

    with pytest.raises(OperationCancelled):
        run_reconstruction(TilingReconstructor(), _source(manifest), _params(refine=False),
                           cancel=lambda: True)


# ----------------------------------------------------------------------------
# Provenance, saving, workflows
# ----------------------------------------------------------------------------

def test_the_provenance_names_every_manifest_and_the_layout_that_was_applied(tmp_path, scene, jitter):
    drifts = {0: (0.0, 0.0), 1: (3.0, -2.0), 2: (6.0, -4.0)}
    manifest = _timelapse(tmp_path / "tl", scene, jitter, timepoints=3, noise=0.3, seed=2, drifts=drifts)
    result = _run(manifest, time_alignment="shared+drift", _series="all")
    record = result.timelapse
    assert record["timelapse_manifest"] == str(manifest)
    assert record["timelapse_manifest_fingerprint"]["sha256"]
    assert [m["index"] for m in record["manifests"]] == [0, 1, 2]
    for entry in record["manifests"]:
        assert entry["manifest"].endswith("tiles.json") and len(entry["fingerprint"]) == 4
        assert entry["fingerprint"][3] and entry["skipped_tiles"] == []
    assert record["alignment_mode"] == "shared+drift" and record["layout_timepoints"] == [0, 1, 2]
    assert set(record["layout"]["positions_yx"]) == {str(i) for i in range(9)}
    assert record["layout"]["shared"]["pairs_measured"] > 0 and record["drift"]["reference"] == 0
    assert record["canvas"]["shape_yx"] == list(result.data.shape[1:])
    summary = result.provenance_summary()
    assert summary["kind"] == "imswitch-tiling-timelapse-provenance/1" and summary["detector"] == "CAM"
    node = output_node(result)
    series = node["series"]
    assert series["manifest"] == str(manifest) and series["manifest_fingerprint"]["sha256"]
    assert [m["index"] for m in series["members"]] == [0, 1, 2]
    assert series["request"]["_series"] == "all" and node["params"]["time_alignment"] == "shared+drift"
    assert sum(1 for n in graph_of(result)["nodes"].values() if n["op"] == "reconstruct") == 1
    # Every number needed to replay it: the same record rebuilt from the manifests gives the same stack.
    again = _run(manifest, time_alignment="shared+drift", _series="all")
    np.testing.assert_allclose(np.asarray(again.data), np.asarray(result.data))


def test_a_timelapse_mosaic_saves_in_every_container_with_its_time_axis(tmp_path, scene, jitter):
    import h5py
    import tifffile
    import zarr

    manifest = _timelapse(tmp_path / "tl", scene, jitter, timepoints=3, interval_s=20.0)
    result = _run(manifest, refine=False)
    assert result.supported_formats == ("tiff", "hdf5", "zarr")
    shape = result.data.shape
    result.save(str(tmp_path / "out.ome.tif"))
    with tifffile.TiffFile(tmp_path / "out.ome.tif") as handle:
        assert handle.series[0].shape == shape and handle.series[0].axes.startswith("TYX") or handle.series[0].shape == shape
        ome = handle.ome_metadata
        description = handle.pages[0].description
    assert 'TimeIncrement="20.0"' in ome and 'DeltaT="40.0"' in ome
    assert "imswitch-tiling-timelapse-provenance/1" in description and "timelapse_manifest" in description
    result.save(str(tmp_path / "out.h5"))
    with h5py.File(tmp_path / "out.h5", "r") as handle:
        assert handle["data"].shape == shape and list(handle["t_seconds"][...]) == [0.0, 20.0, 40.0]
        assert handle["data"].attrs["axis_labels"].startswith("T,Y,X")
    result.save(str(tmp_path / "out.ome.zarr"))
    root = zarr.open_group(str(tmp_path / "out.ome.zarr"), mode="r")
    assert root["0"].shape == shape and list(root.attrs["t_seconds"]) == [0.0, 20.0, 40.0]
    # A single tiling still saves as TIFF only.
    single = run_reconstruction(TilingReconstructor(), _source(manifest.parent / "t0" / "tiles.json"),
                                _params(refine=False, _series="this")).result
    assert single.supported_formats == ("tiff",)


def test_the_reconstructor_runs_as_a_workflow_step_on_the_timelapse_manifest(tmp_path, scene, jitter):
    from imswitch.improcess.workflows import Reconstruct, Save, Source, Workflow, bootstrap_registry, run, validate

    manifest = _timelapse(tmp_path / "tl", scene, jitter, timepoints=3, noise=0.3, seed=2)
    registry = bootstrap_registry(user_plugins=False)
    workflow = Workflow("tl", [
        Source("tiles", path=str(manifest), source_kind="tiling-manifest"),
        Reconstruct("mosaic", "tiling-mosaic",
                    {"time_alignment": "shared", "_series_missing": "zeros"}, inputs=["tiles"]),
        Save("save", input="mosaic", fmt="tiff"),
    ])
    assert validate(workflow, registry) == []
    with run(workflow, registry=registry, out_dir=tmp_path / "out") as report:
        result = report.result("mosaic")
        assert result.data.shape[0] == 3 and result.axis_labels == ["T", "Y", "X"]
        saved = report.receipts[0].primary
    assert Path(saved).exists()
    bad = Workflow("bad", [
        Source("tiles", path=str(manifest), source_kind="tiling-manifest"),
        Reconstruct("mosaic", "tiling-mosaic", {"time_alignment": "sideways"}, inputs=["tiles"]),
    ])
    with pytest.raises(Exception, match="time_alignment"):
        with run(bad, registry=registry, out_dir=tmp_path / "bad"):
            pass


def test_parameters_spec_and_widget_agree_for_the_new_options(qtbot):
    from qtpy import QtWidgets

    from imswitch.improcess.model.param_spec import spec_problems

    assert spec_problems(TilingReconstructor) == []
    parent = QtWidgets.QWidget()
    widget = TilingReconstructor().make_param_widget(parent)
    assert widget.get_values() == TilingReconstructor.default_params()
    assert widget.timelapseBox.isHidden()                # only shown for a source in a series
    keys = set(TilingReconstructor.default_params())
    assert {"time_alignment", "layout_source", "reference_timepoint", "drift_smoothing", "max_drift_px"} <= keys
    fields = {f.key: f for f in TilingReconstructor.param_spec()}
    assert fields["time_alignment"].options == ("stage", "shared", "shared+drift", "independent")
    # An older saved workflow, which knows none of them, keeps running with the defaults.
    old = {k: v for k, v in TilingReconstructor.default_params().items() if k not in keys - {
        "refine", "blend", "shading_correction", "max_shift_px", "project", "stage_positions", "detector",
        "alignment_diagnostic", "channel", "project_z"}}
    assert TilingReconstructor.param_keys() >= set(old)


# ----------------------------------------------------------------------------
# Written by the acquisition code itself
# ----------------------------------------------------------------------------

def test_a_timelapse_written_by_the_acquisition_code_reconstructs(tmp_path, monkeypatch):
    """Real ``TilingController._runScan`` per timepoint, real ``TimelapseController._writeManifest``."""
    pytest.importorskip("tifffile")
    from datetime import datetime, timezone

    from imswitch.imcontrol.controller.controllers.TilingController import TilingController, TilingRunSettings
    from imswitch.imcontrol.controller.controllers.TimelapseController import TimelapseController
    from imswitch.imcontrol.model.managers.RecordingManager import RecordingManager
    from imswitch.imcontrol.model.workflows.spiral import SPIRAL
    from imswitch.imcontrol.model.workflows.timelapse import (
        IntervalMode,
        TimelapseKind,
        TimelapsePlan,
        timelapse_folder,
        timepoint_folder,
    )

    class Logger:
        def debug(self, *a, **k): pass
        info = warning = error = debug

    class Stage:
        def __init__(self):
            self.position = {"X": 0.0, "Y": 0.0}

        def move(self, value, axis):
            self.position[axis] += value

    tile = (np.arange(32 * 32, dtype=np.uint16).reshape(32, 32) * 7) % 5000

    class Detector:
        name = "CAM"
        pixelSizeUm = [1.0, 1.0, 1.0]
        binning = 1
        dtype = np.dtype(np.uint16)
        parameters = {}

        def getLatestFrameShared(self, is_save=False):
            return tile

    detector = Detector()

    class Detectors:
        def acquire(self, names, purpose):
            return "lease"

        def release(self, handle):
            pass

        def __getitem__(self, name):
            return detector

    monkeypatch.setattr("imswitch.imcontrol.controller.controllers.TilingController.time.sleep", lambda _s: None)
    root = timelapse_folder(tmp_path)
    root.mkdir(parents=True)
    points = []
    settings = TilingRunSettings(
        n_tiles_x=2, n_tiles_y=2, pattern="spiral", step_um=24.0, blend_overlaps=False,
        intensity_correction=False, settle_s=0.0, register_tiles=False, orientation=(False, False, False),
        save_tiles=True, mode="free_running", scan_source_key=None, detector_name="CAM",
    )
    for index in range(3):
        controller = TilingController.__new__(TilingController)
        controller._master = SimpleNamespace(positionersManager={"STAGE": Stage()}, detectorsManager=Detectors(),
                                             recordingManager=RecordingManager(Detectors()))
        controller._setupInfo = SimpleNamespace(
            positioners={"STAGE": SimpleNamespace(axes=["X", "Y"])}, tiling=SimpleNamespace(saveFormat="TIFF"))
        controller._commChannel = SimpleNamespace(getRecordingFolder=lambda: str(tmp_path))
        for name, value in dict(_stopRequested=False, _stitcher=None, _originXY=None, _gridPositions=[],
                                _scanAcqHandle=None, _scanning=True, _scanThread=object(), _closed=True,
                                _registrationReport=None, _orientation=(False, False, False),
                                _lastSaveFolder=None, _logger=Logger()).items():
            setattr(controller, name, value)
        folder = timepoint_folder(root, index, 3)
        TilingController._runScan(
            controller,
            SimpleNamespace(xyPositioner="STAGE", camera="CAM", saveFormat="TIFF", measurementsRoot=""),
            n_tiles_x=2, n_tiles_y=2, pattern=SPIRAL, step_um=24.0, blend_overlaps=False,
            intensity_correction=False, settle_s=0.0, register_tiles=False,
            orientation=(False, False, False), save_tiles=True, save_folder=folder,
        )
        points.append(dict(index=index, folder=folder.name, scheduled_offset_s=index * 15.0, slip_s=0.0,
                           completed=True, stopped=False, message="", tiles_acquired=4, stale_tiles=0,
                           illumination_gated=False))
    run_ = SimpleNamespace(
        plan=TimelapsePlan(kind=TimelapseKind.TILING, timepoints=3, interval_s=15.0,
                           interval_mode=IntervalMode.START_TO_START),
        kind="tiling", runStartUtc=datetime(2026, 9, 23, tzinfo=timezone.utc), tilingSettings=settings,
        points=points, folder=root)
    TimelapseController._writeManifest(SimpleNamespace(_logger=Logger()), run_)
    assert (root / "timelapse.json").is_file() and (root / "t0" / "tiles.json").is_file()

    series = ttl.read_timelapse(root)
    assert series is not None and series.counts()["complete"] == 3 and series.interval_s == 15.0
    assert ttl.compatibility(series)[0].all_compatible
    result = _run(root / "timelapse.json", refine=False, time_alignment="stage")
    assert result.data.shape[0] == 3 and result.axis_labels == ["T", "Y", "X"]
    assert result.axis_scales[0] == pytest.approx(15.0)
    assert result.timelapse["timelapse_manifest"] == str(root / "timelapse.json")


def test_the_shipped_example_runs_on_its_own_synthetic_timelapse(tmp_path, monkeypatch):
    import importlib.util

    examples = Path(__file__).resolve().parents[3] / "examples" / "improcess_workflows"
    monkeypatch.syspath_prepend(str(examples))
    spec = importlib.util.spec_from_file_location("tiling_timelapse_example", examples / "tiling_timelapse_mosaic.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main([]) == 0


def test_a_reconstructor_that_cannot_read_tilings_says_so_for_a_tiling_series(tmp_path, scene, jitter):
    from imswitch.improcess.reconstructors.view_only import ViewOnlyReconstructor

    manifest = _timelapse(tmp_path / "tl", scene, jitter, timepoints=3)
    tile = next((manifest.parent / "t1").glob("*.tiff"))
    with pytest.raises(sm.SeriesError, match="tiling datasets.*does not read"):
        run_reconstruction(ViewOnlyReconstructor(), _source(tile), {"_series": "all"})
    # Not asked for: the tile opens as the image it is, as always.
    assert run_reconstruction(ViewOnlyReconstructor(), _source(tile), {}).result.data is not None


def test_the_layout_from_the_reference_alone_is_the_single_timepoint_registration(tmp_path, scene, jitter):
    manifest = _timelapse(tmp_path / "tl", scene, jitter, timepoints=3, noise=0.5, seed=3)
    result = _run(manifest, time_alignment="shared", layout_source="reference", reference_timepoint=1)
    _verdict, parsed = ttl.compatibility(ttl.read_timelapse(manifest))
    alone = solve_layout(parsed[1], LayoutOptions(stage_positions=True, refine=True))
    recorded = result.timelapse["layout"]["positions_yx"]
    for tile_id, position in alone.positions_yx.items():
        assert recorded[str(tile_id)] == pytest.approx(list(position))
    assert result.timelapse["layout_timepoints"] == [1] and result.timelapse["layout"]["shared"] is None
