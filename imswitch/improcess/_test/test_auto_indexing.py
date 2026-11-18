"""Auto-indexing: a plugin with declared core dims loops over the axes in front of them.

SNOUTY deskews one ``(Z, Y, X)`` stack; handed ``(T, Z, Y, X)`` it runs once
per timepoint and the result is the same as deskewing each timepoint alone
and stacking. A processor of ``(Y, X)`` planes over ``(T, C, Y, X)`` runs
T*C times. One provenance node says what was looped over; a cancelled loop
leaves nothing behind; a replay loops again.
"""

import os

import h5py
import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.imcommon.model import OperationCancelled  # noqa: E402
from imswitch.improcess.model.array_result import ArrayProcessingResult  # noqa: E402
from imswitch.improcess.model.core_dims import CoreMatch  # noqa: E402
from imswitch.improcess.model.iteration import IterationError, stack_arrays, stack_results  # noqa: E402
from imswitch.improcess.model.provenance import graph_of, output_node  # noqa: E402
from imswitch.improcess.model.result_source import data_obj_from_result  # noqa: E402
from imswitch.improcess.processors.base import Processor  # noqa: E402
from imswitch.improcess.reconstructors.run import InputContractError, run_reconstruction  # noqa: E402
from imswitch.improcess.workflows import (  # noqa: E402
    Process,
    Reconstruct,
    RunError,
    Save,
    Source,
    Workflow,
    bootstrap_registry,
    run,
    validate,
    workflow_from_file,
)

SNOUTY = {"device": "CPU", "c_px": 100.0, "alpha_deg": 30.0, "dy": 210.0,
          "sample_vx_size": 200.0, "camera_offset": 100.0, "n_timepoints": 1}

#: Side-by-side timepoints along X -> a (T, Z, Y, X) skewed stack.
RESHAPE = (
    "t = 2\n"
    "z, y, x = data.shape\n"
    "stack = data.reshape(z, y, t, x // t).transpose(2, 0, 1, 3)\n"
    'out = make_result(stack, axes=["T", "Z", "Y", "X"], attrs={"geometry": "skewed"})\n'
)


class PlaneSum(Processor):
    """Adds one per call: counts how often it ran, per plane."""

    name = "Plane counter"
    id = "test-plane-counter"
    kinds = ("image",)
    core_dims_in = ("Y", "X")
    core_dims_out = ("Y", "X")
    calls = 0

    @classmethod
    def default_params(cls):
        return {"offset": 1.0}

    def make_param_widget(self, parent):
        return None

    @property
    def applies_to(self):
        return self.accepts

    def accepts(self, result):
        return np.ndim(result.data) == 2         # only a plane: it must be handed slices

    def apply(self, result, params):
        type(self).calls += 1
        return ArrayProcessingResult(f"{result.name}+", np.asarray(result.data) + params["offset"],
                                     list(result.axis_labels))


@pytest.fixture(scope="module")
def registry():
    registry = bootstrap_registry(user_plugins=False)
    registry.register_processor(PlaneSum())
    return registry


def side_by_side(path, timepoints=2, planes=8, size=16, seed=0):
    rng = np.random.default_rng(seed)
    volumes = [rng.random((planes, size, size)).astype(np.float32) * 100 + 101 for _ in range(timepoints)]
    with h5py.File(str(path), "w") as handle:
        dataset = handle.create_dataset("data", data=np.concatenate(volumes, axis=-1))
        dataset.attrs["axes"] = "ZYX"
    return path, volumes


def deskew_chain(raw, *, snouty=None):
    return Workflow("auto", [
        Source("raw", path=str(raw)),
        Reconstruct("view", "view-only", inputs=["raw"]),
        Process("reshape", "python", {"code": RESHAPE}, inputs=["view"]),
        Reconstruct("deskew", "snouty", dict(snouty or SNOUTY), inputs=["reshape"]),
        Save("save", input="deskew", fmt="tiff"),
    ])


# -- stacking ----------------------------------------------------------------------

def test_a_loop_axis_fills_a_length_one_output_axis_of_the_same_name():
    match = CoreMatch(("T",), (3,), ("Z", "Y", "X"))
    arrays = [np.full((1, 2, 1, 1, 4, 5), i) for i in range(3)]
    data, labels, _ = stack_arrays(arrays, ["Dataset", "Base", "T", "Z", "Y", "X"], match)
    assert labels == ["Dataset", "Base", "T", "Z", "Y", "X"] and data.shape == (1, 2, 3, 1, 4, 5)
    assert list(data[0, 0, :, 0, 0, 0]) == [0, 1, 2]


def test_otherwise_the_loop_axes_go_in_front_and_a_clash_is_renamed():
    match = CoreMatch(("T", "C"), (3, 2), ("Y", "X"))
    data, labels, _ = stack_arrays([np.full((4, 5), i) for i in range(6)], ["Y", "X"], match)
    assert labels == ["T", "C", "Y", "X"] and data[:, :, 0, 0].tolist() == [[0, 1], [2, 3], [4, 5]]
    clash = CoreMatch(("T",), (2,), ("Z", "Y", "X"))
    _, labels, _ = stack_arrays([np.zeros((3, 4, 4))] * 2, ["T", "Y", "X"], clash)
    assert labels == ["T_loop", "T", "Y", "X"]


def test_outputs_of_different_shapes_name_the_index():
    match = CoreMatch(("T",), (2,), ("Y", "X"))
    with pytest.raises(IterationError, match="T=2/2"):
        stack_arrays([np.zeros((4, 4)), np.zeros((4, 5))], ["Y", "X"], match)


def test_the_stacked_result_takes_loop_scales_from_the_input_and_attrs_from_the_output():
    first = ArrayProcessingResult("a", np.zeros((4, 4)), ["Y", "X"], axis_scales=[0.1, 0.2])
    first.attrs = {"geometry": "deskewed"}
    second = ArrayProcessingResult("a", np.ones((4, 4)), ["Y", "X"], axis_scales=[0.1, 0.2])
    stacked = stack_results([first, second], CoreMatch(("T",), (2,), ("Y", "X")), loop_scales=[5.0])
    assert stacked.axis_labels == ["T", "Y", "X"] and stacked.axis_scales == [5.0, 0.1, 0.2]
    assert stacked.attrs == {"geometry": "deskewed"}


# -- reconstructions ----------------------------------------------------------------

def test_snouty_on_a_4d_stack_equals_each_timepoint_deskewed_and_stacked(registry):
    snouty = registry.get_reconstructor("snouty")
    rng = np.random.default_rng(4)
    volumes = [rng.random((8, 16, 16)).astype(np.float32) * 100 + 101 for _ in range(3)]
    stack = ArrayProcessingResult("lapse", np.stack(volumes), ["T", "Z", "Y", "X"], axis_scales=[2.5, 1, 1, 1])
    made = run_reconstruction(snouty, stack, dict(SNOUTY))
    assert made.result.axis_labels == ["T", "Z", "Y", "X"]
    for t, volume in enumerate(volumes):
        alone = snouty.process(data_obj_from_result(ArrayProcessingResult("t", volume, ["Z", "Y", "X"])), dict(SNOUTY))
        np.testing.assert_allclose(made.result.data[t], alone.data)
    node = output_node(made.result)
    assert node["iterated_over"] == {"T": 3}
    assert sum(1 for n in graph_of(made.result)["nodes"].values() if n["op"] == "reconstruct") == 1
    assert made.result.attrs["geometry"] == "deskewed"


def test_progress_and_cancel_are_asked_per_index(registry):
    snouty = registry.get_reconstructor("snouty")
    stack = ArrayProcessingResult("lapse", np.ones((3, 8, 16, 16), np.float32) * 150, ["T", "Z", "Y", "X"])
    seen = []
    run_reconstruction(snouty, stack, dict(SNOUTY), progress=lambda done, total, where: seen.append(where))
    assert seen == ["T=1/3", "T=2/3", "T=3/3"]
    calls = []

    def cancel():
        calls.append(1)
        return len(calls) > 1

    with pytest.raises(OperationCancelled):
        run_reconstruction(snouty, stack, dict(SNOUTY), cancel=cancel)
    assert "provenance" not in (getattr(stack, "metadata", None) or {}) or graph_of(stack) is not None


def test_iterate_false_hands_the_plugin_the_whole_stack(registry):
    snouty = registry.get_reconstructor("snouty")
    stack = ArrayProcessingResult("lapse", np.ones((2, 8, 16, 16), np.float32) * 150, ["T", "Z", "Y", "X"])
    with pytest.raises(ValueError, match="Expected 3D"):          # SNOUTY itself refuses 4D
        run_reconstruction(snouty, stack, {**SNOUTY, "_iterate": False})


def test_a_double_time_split_is_refused_before_any_pixel(registry):
    snouty = registry.get_reconstructor("snouty")
    stack = ArrayProcessingResult("lapse", np.ones((2, 8, 16, 16), np.float32), ["T", "Z", "Y", "X"])
    with pytest.raises(InputContractError, match="n_timepoints to 1"):
        run_reconstruction(snouty, stack, {**SNOUTY, "n_timepoints": 2})


def test_an_auto_indexed_workflow_runs_saves_and_replays(registry, tmp_path):
    raw, volumes = side_by_side(tmp_path / "raw.h5")
    progress = []
    with run(deskew_chain(raw), registry=registry, out_dir=tmp_path / "first",
             progress=lambda i, n, text: progress.append(text)) as report:
        deskewed = report.result("deskew")
        assert deskewed.data.shape[0] == 2 and deskewed.axis_labels == ["T", "Z", "Y", "X"]
        snouty = registry.get_reconstructor("snouty")
        for t, volume in enumerate(volumes):
            alone = snouty.process(data_obj_from_result(ArrayProcessingResult("t", volume, ["Z", "Y", "X"])),
                                   dict(SNOUTY))
            np.testing.assert_allclose(deskewed.data[t], alone.data)
        assert output_node(deskewed)["iterated_over"] == {"T": 2}
        assert "deskew [T=1/2]" in progress and "deskew [T=2/2]" in progress
        original = np.asarray(deskewed.data)
        saved = report.receipts[0].primary
    replay = workflow_from_file(saved, registry=registry)
    assert validate(replay.workflow, registry) == []
    with run(replay.workflow, registry=registry, out_dir=tmp_path / "second", mode="replay") as again:
        np.testing.assert_allclose(np.asarray(again.result("rec2").data), original)
        assert output_node(again.result("rec2"))["iterated_over"] == {"T": 2}


def test_a_cancelled_workflow_leaves_no_half_result(registry, tmp_path):
    raw, _ = side_by_side(tmp_path / "raw.h5", timepoints=2)
    asked = []

    def cancel():
        asked.append(1)
        # Steps raw, view, reshape and the first index pass; the second index stops.
        return len(asked) > 5

    with pytest.raises(RunError) as caught:
        run(deskew_chain(raw), registry=registry, out_dir=tmp_path, cancel=cancel)
    report = caught.value.report
    assert report.failed_step == "deskew" and "cancelled" in report.error
    assert not any(key.startswith("deskew.") for key in report.results)
    report.close()


# -- processors -------------------------------------------------------------------

def test_a_plane_processor_runs_once_per_plane_of_a_4d_input(registry, tmp_path):
    PlaneSum.calls = 0
    path = tmp_path / "tcyx.h5"
    data = np.arange(3 * 2 * 4 * 4, dtype=np.float32).reshape(3, 2, 4, 4)
    with h5py.File(path, "w") as handle:
        dataset = handle.create_dataset("data", data=data)
        dataset.attrs["axes"] = "TCYX"
    wf = Workflow("planes", [
        Source("raw", path=str(path)),
        Reconstruct("view", "view-only", inputs=["raw"]),
        Process("count", "test-plane-counter", {"offset": 2.0}, inputs=["view"]),
    ])
    assert validate(wf, registry) == []
    with run(wf, registry=registry, out_dir=tmp_path) as report:
        counted = report.result("count")
        assert PlaneSum.calls == 6
        assert counted.axis_labels == ["T", "C", "Y", "X"]
        np.testing.assert_allclose(counted.data, data + 2.0)
        node = output_node(counted)
        assert node["iterated_over"] == {"T": 3, "C": 2} and node["plugin_id"] == "test-plane-counter"
        assert sum(1 for n in graph_of(counted)["nodes"].values() if n.get("plugin_id") == "test-plane-counter") == 1


def test_a_processor_without_core_dims_is_not_looped(registry, tmp_path):
    path = tmp_path / "tyx.h5"
    with h5py.File(path, "w") as handle:
        dataset = handle.create_dataset("data", data=np.ones((3, 4, 4), np.float32))
        dataset.attrs["axes"] = "TYX"
    wf = Workflow("plain", [
        Source("raw", path=str(path)),
        Reconstruct("view", "view-only", inputs=["raw"]),
        Process("blur", "filter", {"method": "gaussian", "radius": 1.0}, inputs=["view"]),
    ])
    with run(wf, registry=registry, out_dir=tmp_path) as report:
        assert "iterated_over" not in output_node(report.result("blur"))
