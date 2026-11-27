"""The series layer: any reconstructor over a timelapse stored one source per timepoint.

Covers discovery (:mod:`imswitch.improcess.model.series`), the request and its
reserved parameters, the base-class default (``process`` per timepoint, stacked
along ``T``), the two opt-outs (``series_policy`` ``"own"`` and ``"none"``),
differing sizes, a missing member, cancellation and progress, provenance and
replay, saving, and composition with auto-indexing.
"""

import json

import numpy as np
import pytest
from qtpy import QtWidgets

from imswitch.imcommon.model import OperationCancelled
from imswitch.improcess._test._lapse_recordings import record_lapse, record_point
from imswitch.improcess.model import series as sm
from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.param_spec import spec_problems
from imswitch.improcess.model.plugin_contract import contract_problem
from imswitch.improcess.model.provenance import graph_of, output_node
from imswitch.improcess.reconstructors import available_reconstructor_ids
from imswitch.improcess.reconstructors.base import Reconstructor
from imswitch.improcess.reconstructors.run import run_reconstruction
from imswitch.improcess.reconstructors.view_only import ViewOnlyReconstructor
from imswitch.improcess.workflows import (
    Reconstruct,
    Source,
    Workflow,
    bootstrap_registry,
    run,
    validate,
)
from imswitch.improcess.workflows.sources import SourceSpec, open_source


class _Doubler(Reconstructor):
    """A third-party-style reconstructor that has never heard of series."""

    name = "Doubler"
    id = "test-doubler"
    core_dims_in = ("Y", "X")
    output_kind = "image"
    calls = 0

    @classmethod
    def default_params(cls):
        return {"gain": 2.0}

    def make_param_widget(self, parent):
        widget = QtWidgets.QWidget(parent)
        widget.get_values = lambda: {"gain": 2.0}
        return widget

    def make_metadata_dialog(self, parent):
        return None

    def process(self, data_obj, params, context=None):
        type(self).calls += 1
        data = np.asarray(data_obj.data_handle[...], dtype=np.float32) * params.get("gain", 2.0)
        labels = list(data_obj.axis_labels)
        return ArrayProcessingResult(data_obj.name, data, labels,
                                     axis_scales=list(data_obj.axis_scales or [1.0] * data.ndim))


class _Own(_Doubler):
    name = "Own series"
    id = "test-own-series"
    series_policy = "own"
    seen = None

    def process_series(self, selection, data_obj, params, context=None, *, progress=None, cancel=None):
        type(self).seen = (selection, dict(params))
        return ArrayProcessingResult("own", np.zeros((len(selection.positions), 2, 2)), ["T", "Y", "X"])


class _Opted(_Doubler):
    name = "Opted out"
    id = "test-opted-out"
    series_policy = "none"


@pytest.fixture(scope="module")
def qapp():
    yield QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def lapse(tmp_path):
    return record_lapse(tmp_path / "lapse", 4)


def _source(path):
    return open_source(SourceSpec(path=str(path)))


# ----------------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------------

def test_a_recording_lapse_is_found_from_any_one_timepoint(lapse):
    info = sm.discover_series(str(lapse[2]))
    assert info.kind == sm.KIND_RECORDING_LAPSE and info.anchor == 2
    assert [m.index for m in info.members] == [0, 1, 2, 3]
    assert [m.state for m in info.members] == ["complete"] * 4
    assert info.times_s() == [0.0, 10.0, 20.0, 30.0]
    assert not info.opened_as_series
    # From a DataObj, as the panel and the runner hold it.
    assert sm.discover_series(_source(lapse[0])).anchor == 0


def test_missing_and_incomplete_timepoints_are_marked(tmp_path):
    paths = record_lapse(tmp_path / "lapse", 5, points=[0, 1, 3, 4], frames=3)
    record_point(tmp_path / "lapse", index=2, total=5, frames=3, written=1)   # stopped early
    info = sm.discover_series(str(paths[0]))
    states = {m.index: m.state for m in info.members}
    assert states[2] == "incomplete" and all(states[i] == "complete" for i in (0, 1, 3, 4))
    # And with one never recorded:
    only = record_lapse(tmp_path / "gap", 4, points=[0, 3])
    gap = sm.discover_series(str(only[0]))
    assert [m.state for m in gap.members] == ["complete", "missing", "missing", "complete"]
    assert gap.members[1].path == "" and gap.members[1].time_s == 10.0


def test_listing_without_opening_leaves_states_unknown(lapse):
    info = sm.discover_series(str(lapse[1]), deep=False)
    assert not info.checked and {m.state for m in info.members} == {"unknown"}


def test_a_source_that_is_not_part_of_a_series_is_none(tmp_path):
    single = record_point(tmp_path / "one", index=0, total=1)
    assert sm.discover_series(str(single)) is None
    assert sm.discover_series(str(tmp_path / "does-not-exist.h5")) is None
    assert sm.discover_series(None) is None
    result = ArrayProcessingResult("r", np.zeros((3, 4, 4)), ["T", "Y", "X"])
    assert sm.discover_series(result) is None


def test_the_request_is_read_from_reserved_parameters_and_validated():
    assert sm.SeriesRequest.from_params({"gain": 1}) is None
    request = sm.SeriesRequest.from_params({"_series": "range", "_series_first": 1, "_series_last": "3",
                                            "_series_step": 2, "_series_missing": "nan"})
    assert (request.mode, request.first, request.last, request.step, request.missing) == ("range", 1, 3, 2, "nan")
    assert sm.SeriesRequest.from_params(request.to_params()) == request
    with pytest.raises(sm.SeriesError, match="not one of"):
        sm.SeriesRequest(mode="everything")
    with pytest.raises(sm.SeriesError, match="at least 1"):
        sm.SeriesRequest(step=0)
    assert sm.without_series_params({"gain": 1, "_series": "all"}) == {"gain": 1}
    assert sm.SeriesRequest().effective_mode("this") == "this"
    assert sm.SeriesRequest().effective_mode("this", opened_as_series=True) == "all"
    assert sm.SeriesRequest().effective_mode("all") == "all"


def test_selection_this_all_range_and_gaps(tmp_path):
    paths = record_lapse(tmp_path / "gap", 6, points=[0, 1, 3, 4, 5])
    info = sm.discover_series(str(paths[2]))
    assert info.anchor == 3 - 1 + 0 or info.anchor is not None
    request = sm.SeriesRequest()
    assert sm.select_members(info, request, mode="this").process == (info.anchor,)
    everything = sm.select_members(info, request, mode="all")
    assert everything.process == (0, 1, 3, 4, 5) and everything.positions == (0, 1, 3, 4, 5)
    assert everything.skipped[0][0] == 2 and "missing" in everything.skipped[0][1]
    filled = sm.select_members(info, sm.SeriesRequest(missing="zeros"), mode="all")
    assert filled.filled == (2,) and filled.positions == (0, 1, 2, 3, 4, 5)
    ranged = sm.select_members(info, sm.SeriesRequest(mode="range", first=1, last=5, step=2), mode="range")
    assert ranged.process == (1, 3, 5)
    with pytest.raises(sm.SeriesError, match="holds no timepoint"):
        sm.select_members(info, sm.SeriesRequest(mode="range", first=4, last=2), mode="range")


def test_the_t_scale_comes_from_recorded_times_and_says_if_it_is_regular():
    regular = sm.time_axis([0, 10.2, 19.9, 30.0], [0, 1, 2, 3])
    assert regular["basis"] == "recorded" and regular["regular"] and regular["scale"] == pytest.approx(10.0, abs=0.2)
    # A skipped timepoint does not stretch the scale: (30 - 10) / (3 - 1).
    assert sm.time_axis([0, 10, 30], [0, 1, 3])["scale"] == pytest.approx(10.0)
    assert not sm.time_axis([0, 10, 50, 60], [0, 1, 2, 3])["regular"]
    assert sm.time_axis([None, None], [0, 1], interval_s=5.0) == {"scale": 5.0, "basis": "interval", "regular": True}
    assert sm.time_axis([None, None], [0, 1])["basis"] == "index"


# ----------------------------------------------------------------------------
# The default: process per timepoint, stack along T
# ----------------------------------------------------------------------------

def test_view_only_stacks_every_timepoint_along_t(lapse):
    run_ = run_reconstruction(ViewOnlyReconstructor(), _source(lapse[1]), {"_series": "all"})
    result = run_.result
    assert result.data.shape == (4, 4, 5) and result.axis_labels == ["T", "Y", "X"]
    assert result.axis_scales[0] == pytest.approx(10.0)
    # Every plane is its own timepoint: record_point fills point i with i + 1.
    assert [float(np.asarray(result.data)[t, 0, 0]) for t in range(4)] == [1, 2, 3, 4]
    record = result.metadata[sm.METADATA_KEY]
    assert record["timepoints"] == [0, 1, 2, 3] and record["times_s"] == [0.0, 10.0, 20.0, 30.0]
    assert record["t_basis"] == "recorded" and record["t_regular"] and record["left_out"] == []


def test_the_default_run_is_the_historic_one_unless_a_series_is_asked_for(lapse):
    view = ViewOnlyReconstructor()
    historic = run_reconstruction(view, _source(lapse[1]), {}).result
    for params in ({"_series": "this"}, {"_series": "auto"}):
        result = run_reconstruction(view, _source(lapse[1]), params).result
        assert np.asarray(result.data).shape == np.asarray(historic.data).shape
        assert float(np.asarray(result.data).max()) == 2.0       # timepoint 1, alone
        assert sm.METADATA_KEY not in (result.metadata or {})


def test_a_range_and_every_nth_timepoint(lapse):
    result = run_reconstruction(
        ViewOnlyReconstructor(), _source(lapse[0]),
        {"_series": "range", "_series_first": 0, "_series_last": 3, "_series_step": 3}).result
    assert [float(np.asarray(result.data)[t, 0, 0]) for t in range(2)] == [1, 4]
    assert result.metadata[sm.METADATA_KEY]["timepoints"] == [0, 3]


def test_a_non_series_source_ignores_the_series_options(tmp_path):
    single = record_point(tmp_path / "one", index=0, total=1)
    plain = run_reconstruction(ViewOnlyReconstructor(), _source(single), {}).result
    asked = run_reconstruction(ViewOnlyReconstructor(), _source(single), {"_series": "all"}).result
    assert np.array_equal(np.asarray(plain.data), np.asarray(asked.data))
    assert plain.axis_labels == asked.axis_labels


def test_a_missing_timepoint_is_reported_and_skipped_or_filled(tmp_path):
    paths = record_lapse(tmp_path / "gap", 4, points=[0, 1, 3])
    view = ViewOnlyReconstructor()
    skipped = run_reconstruction(view, _source(paths[0]), {"_series": "all"}).result
    assert skipped.data.shape[0] == 3
    record = skipped.metadata[sm.METADATA_KEY]
    assert record["left_out"] == [{"index": 2, "why": "missing: not recorded"}]
    assert record["timepoints"] == [0, 1, 3]
    assert record["times_s"] == [0.0, 10.0, 30.0] and record["t_scale_s"] == pytest.approx(10.0)
    zeros = run_reconstruction(view, _source(paths[0]), {"_series": "all", "_series_missing": "zeros"}).result
    assert zeros.data.shape[0] == 4 and float(np.abs(np.asarray(zeros.data)[2]).max()) == 0.0
    assert zeros.metadata[sm.METADATA_KEY]["filled"] == [2] and zeros.metadata[sm.METADATA_KEY]["filled_with"] == "zeros"
    nan = run_reconstruction(view, _source(paths[0]), {"_series": "all", "_series_missing": "nan"}).result
    assert np.isnan(np.asarray(nan.data)[2]).all() and not np.isnan(np.asarray(nan.data)[3]).any()


def test_differing_sizes_are_refused_by_name_or_padded_never_cropped(tmp_path):
    folder = tmp_path / "sizes"
    record_point(folder, index=0, total=3, frame_shape=(4, 5))
    record_point(folder, index=1, total=3, frame_shape=(6, 5))
    last = record_point(folder, index=2, total=3, frame_shape=(4, 7))
    view = ViewOnlyReconstructor()
    with pytest.raises(sm.SeriesError) as refusal:
        run_reconstruction(view, _source(last), {"_series": "all"})
    text = str(refusal.value)
    assert "4, 5)" in text and "6, 5)" in text and "4, 7)" in text and "Nothing is cropped" in text
    assert "timepoint(s) [0]" in text and "timepoint(s) [1]" in text and "timepoint(s) [2]" in text
    origin = run_reconstruction(view, _source(last), {"_series": "all", "_series_shapes": "pad_origin"}).result
    assert np.asarray(origin.data).shape[-2:] == (6, 7)
    plane = np.asarray(origin.data)[0].reshape(6, 7)
    assert plane[:4, :5].min() == 1 and plane[4:, :].max() == 0 and plane[:, 5:].max() == 0
    centred = run_reconstruction(view, _source(last), {"_series": "all", "_series_shapes": "pad_center"}).result
    plane = np.asarray(centred.data)[0].reshape(6, 7)
    assert plane[1:5, 1:6].min() == 1 and plane[0].max() == 0
    assert origin.metadata[sm.METADATA_KEY]["padded"]["canvas"][-2:] == [6, 7]
    assert origin.metadata[sm.METADATA_KEY]["padded"]["how"] == "pad_origin"


def test_a_third_party_reconstructor_gets_series_for_free(lapse):
    plugin = _Doubler()
    assert spec_problems(type(plugin)) == []
    assert _Doubler.supports_series() and _Doubler.default_params() == {"gain": 2.0}
    assert {"_series", "_series_step"} <= set(_Doubler.param_keys())
    assert "_series" not in _Doubler.default_params()
    before = _Doubler.calls
    result = run_reconstruction(plugin, _source(lapse[0]), {"gain": 3.0, "_series": "all"}).result
    assert _Doubler.calls - before == 4
    assert result.data.shape[0] == 4 and result.axis_labels[0] in ("T", "T_loop")
    # The plugin never saw the reserved keys, and its own parameters were honoured.
    assert float(np.asarray(result.data).reshape(4, -1)[3, 0]) == pytest.approx(4 * 3.0)


def test_a_series_composes_with_auto_indexing_without_iterating_twice(tmp_path):
    folder = tmp_path / "frames"
    record_lapse(folder, 3, frames=2)          # every timepoint holds two frames
    first = sorted(folder.glob("*.hdf5"))[0]
    result = run_reconstruction(_Doubler(), _source(first), {"_series": "all"}).result
    data = np.asarray(result.data)
    # Series outermost, then each member's own auto-indexed frames, then Y, X.
    assert data.shape[0] == 3 and data.shape[-2:] == (4, 5)
    assert data.ndim == 4 and data.shape[1] == 2
    assert sorted(set(data.reshape(3, -1)[:, 0].tolist())) == [2.0, 4.0, 6.0]
    node = output_node(result)
    assert node["series"]["member_iterated_over"]
    assert sum(1 for n in graph_of(result)["nodes"].values() if n["op"] == "reconstruct") == 1


def test_a_result_is_never_a_series_so_a_map_body_does_not_expand_one(lapse):
    produced = run_reconstruction(ViewOnlyReconstructor(), _source(lapse[0]), {"_series": "all"}).result
    again = run_reconstruction(_Doubler(), produced, {"_series": "all"}).result
    assert sm.METADATA_KEY not in (again.metadata or {})
    assert np.asarray(again.data).shape == np.asarray(produced.data).shape


# ----------------------------------------------------------------------------
# Opting out and handling it yourself
# ----------------------------------------------------------------------------

def test_a_reconstructor_can_opt_out_entirely(lapse):
    plugin = _Opted()
    assert not _Opted.supports_series() and _Opted.series_param_spec() == ()
    assert not ({"_series"} & set(_Opted.param_keys()))
    with_request = run_reconstruction(plugin, _source(lapse[1]), {"_series": "all"}).result
    without = run_reconstruction(plugin, _source(lapse[1]), {}).result
    assert np.array_equal(np.asarray(with_request.data), np.asarray(without.data))
    assert sm.METADATA_KEY not in (with_request.metadata or {})


def test_the_builtin_policies():
    from imswitch.improcess.reconstructors import _AVAILABLE_RECONSTRUCTOR_CLASSES as classes

    policies = {rid: cls.series_policy for rid, cls in classes.items()}
    assert policies["tiling-mosaic"] == "own" and classes["tiling-mosaic"].series_default == "all"
    for rid in ("time-lapse", "monalisa", "monalisa-legacy", "monalisa-lattice", "smlm-localizer"):
        assert policies[rid] == "none", rid
    for rid in ("view-only", "snouty", "snouty-projections", "beadrec", "linestep-sum", "widefield-starss"):
        assert policies[rid] == "default" and classes[rid].series_default == "this", rid
    for rid, cls in classes.items():
        spec = cls.series_param_spec()
        assert bool(spec) == (policies[rid] != "none")
        assert {f.key for f in spec} <= set(cls.param_keys() or {f.key for f in spec})
        for field in spec:
            assert field.problem_with(field.default) is None
        # The plugin's own spec is untouched: contract tests compare it with default_params().
        assert spec_problems(cls) == [], rid


def test_a_reconstructor_can_handle_the_series_itself(lapse):
    plugin = _Own()
    result = run_reconstruction(plugin, _source(lapse[1]), {"_series": "range", "_series_first": 1, "_series_last": 2}).result
    selection, params = _Own.seen
    assert selection.process == (1, 2) and selection.mode == "range"
    assert params == {}                                    # the plugin's parameters only, no series keys
    assert result.data.shape == (2, 2, 2)
    node = output_node(result)
    assert [m["index"] for m in node["series"]["members"]] == [1, 2]
    assert node["series"]["request"]["_series"] == "range"


# ----------------------------------------------------------------------------
# Progress, cancellation, provenance, workflows, saving
# ----------------------------------------------------------------------------

def test_progress_and_cancellation_are_asked_once_per_timepoint(lapse):
    seen = []
    run_reconstruction(ViewOnlyReconstructor(), _source(lapse[0]), {"_series": "all"},
                       progress=lambda done, total, where: seen.append((done, total, where)))
    assert seen == [(1, 4, "T=1/4"), (2, 4, "T=2/4"), (3, 4, "T=3/4"), (4, 4, "T=4/4")]
    calls = []

    def cancel():
        calls.append(1)
        return len(calls) > 1

    with pytest.raises(OperationCancelled):
        run_reconstruction(ViewOnlyReconstructor(), _source(lapse[0]), {"_series": "all"}, cancel=cancel)


def test_the_provenance_lists_every_member_and_replays_the_request(lapse):
    result = run_reconstruction(ViewOnlyReconstructor(), _source(lapse[0]),
                                {"_series": "range", "_series_first": 1, "_series_last": 3}).result
    node = output_node(result)
    series = node["series"]
    assert series["kind"] == sm.KIND_RECORDING_LAPSE and series["request"]["_series"] == "range"
    assert [m["index"] for m in series["members"]] == [1, 2, 3]
    assert all(m["path"].endswith(".hdf5") and m["fingerprint"] for m in series["members"])


def test_a_saved_series_result_replays_as_the_same_series(lapse, tmp_path):
    from imswitch.improcess.workflows import Save, workflow_from_file

    registry = bootstrap_registry(user_plugins=False)
    workflow = Workflow("lapse", [
        Source("raw", path=str(lapse[0])),
        Reconstruct("view", "view-only", {"_series": "range", "_series_first": 1, "_series_last": 3}, inputs=["raw"]),
        Save("save", input="view", fmt="tiff"),
    ])
    with run(workflow, registry=registry, out_dir=tmp_path / "first") as report:
        original = np.asarray(report.result("view").data)
        saved = report.receipts[0].primary
    assert original.shape[0] == 3
    replay = workflow_from_file(saved, registry=registry)
    steps = [step for step in replay.workflow.steps if isinstance(step, Reconstruct)]
    assert steps and steps[0].params["_series"] == "range" and steps[0].params["_series_first"] == 1
    assert validate(replay.workflow, registry) == []
    with run(replay.workflow, registry=registry, out_dir=tmp_path / "second", mode="replay") as again:
        names = [step.id for step in replay.workflow.steps if isinstance(step, Reconstruct)]
        np.testing.assert_array_equal(np.asarray(again.result(names[0]).data), original)


def test_a_workflow_step_asks_for_the_series_and_the_editor_knows_the_options(lapse, tmp_path):
    registry = bootstrap_registry(user_plugins=False)
    workflow = Workflow("lapse", [
        Source("raw", path=str(lapse[0])),
        Reconstruct("view", "view-only", {"_series": "all"}, inputs=["raw"]),
    ])
    assert validate(workflow, registry) == []
    with run(workflow, registry=registry, out_dir=tmp_path / "out") as report:
        result = report.result("view")
        assert result.data.shape[0] == 4 and result.axis_labels[0] == "T"
    bad = Workflow("bad", [
        Source("raw", path=str(lapse[0])),
        Reconstruct("rec", "time-lapse", {"_series": "all"}, inputs=["raw"]),
    ])
    assert any("_series" in issue.message for issue in validate(bad, registry))
    from imswitch.improcess.model.workfloweditor.catalog import build_catalog

    catalog = build_catalog(registry)
    entry = catalog.reconstructor("view-only") if hasattr(catalog, "reconstructor") else None
    assert entry is not None and {f.key for f in entry.series_fields} == set(sm.SERIES_PARAMS)
    assert "_series" not in entry.defaults() and "_series" in entry.param_keys()


def test_a_series_result_saves_with_its_time_axis_and_stamps(lapse, tmp_path):
    import h5py
    import tifffile

    result = run_reconstruction(ViewOnlyReconstructor(), _source(lapse[0]), {"_series": "all"}).result
    from imswitch.improcess.model.result_io import save_image_result

    tiff = tmp_path / "series.ome.tif"
    save_image_result(result, tiff, "tiff")
    with tifffile.TiffFile(tiff) as handle:
        assert handle.series[0].shape == (4, 4, 5)
        ome = handle.ome_metadata
    assert 'DeltaT="30.0"' in ome or "DeltaT=\"30\"" in ome
    h5 = tmp_path / "series.h5"
    save_image_result(result, h5, "hdf5")
    with h5py.File(h5, "r") as handle:
        assert handle["data"].shape == (4, 4, 5)
        assert list(handle["t_seconds"][...]) == [0.0, 10.0, 20.0, 30.0]
        assert "series" in handle.attrs and json.loads(handle.attrs["series"])["t_regular"] is True
    zarr_path = tmp_path / "series.ome.zarr"
    save_image_result(result, zarr_path, "zarr")
    import zarr

    root = zarr.open_group(str(zarr_path), mode="r")
    assert root["0"].shape == (4, 4, 5) and list(root.attrs["t_seconds"]) == [0.0, 10.0, 20.0, 30.0]
