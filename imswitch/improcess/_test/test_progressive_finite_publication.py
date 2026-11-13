"""M2D-C: semantic T/Dataset assembly and progressive publication.

Uses synthetic results; requires no camera, GPU, HDF5 or Qt event loop.
"""

from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.improcess.execution.result_assembly import AssemblyError, ResultAssemblyPlan
from imswitch.improcess.execution.progressive_results import ProgressiveResultAssembler
from imswitch.improcess.execution.result_publication import ResultPublication, PublicationHooks
from imswitch.improcess.model.timepoint_scope import TimepointScope


@dataclass
class Mode:
    name: str
    transpose: tuple[int, ...]


def part(index, *, shape=(1, 2, 3, 4), axes=("T", "Z", "Y", "X"), **extra):
    arr = np.full(shape, float(index), np.float32)
    obj = SimpleNamespace(
        name="full", data=arr, axis_labels=list(axes),
        axis_scales=[1., 50., 35., 35.][:len(shape)], scale_unit="nm",
        kind="image", metadata={},
        view_modes=[Mode("Standard", tuple(range(len(shape))))],
        background=arr * .5, sharpened=arr * 2, pinholes=None,
        coeffs=None,
    )
    for k, v in extra.items():
        setattr(obj, k, v)
    return obj


def publisher(plan):
    events = []
    hooks = PublicationHooks(
        preview=lambda r: events.append(("preview", r)),
        timepoint_done=lambda i: events.append(("done", i)),
        finalized=lambda r, n: events.append(("finalized", r)),
        current_changed=lambda r: events.append(("current", r)),
    )
    return ResultPublication(plan, hooks), events


def test_timepoints_publish_once_with_stable_identity_and_constant_allocation():
    plan = ResultAssemblyPlan.for_timepoints(
        "run", "raw", scope=TimepointScope.range(2, 4), total=10,
    )
    pub, events = publisher(plan)
    a = part(1)
    pub.append_completed(a, 0)
    uid = id(a)
    assert pub.published is a
    assert a.data.shape == (3, 2, 3, 4)
    assert np.isnan(a.data[1:]).all()
    backing_id = id(a.data)
    pub.append_completed(part(2), 1)
    assert id(pub.published) == uid and id(a.data) == backing_id
    assert np.all(a.data[1] == 2)
    assert np.all(a.background[1] == 1)
    assert np.all(a.sharpened[1] == 4)
    assert pub.coverage.to_dict()["published_t_ranges"] == [[2, 4]]
    pub.append_completed(part(3), 2)
    assert pub.finish("complete") is a
    assert pub.finish("complete") is None
    assert a.metadata["result_assembly"]["status"] == "complete"
    assert len([x for x in events if x[0] == "finalized"]) == 1
    assert len([x for x in events if x[0] == "preview"]) == 3


def test_cancel_retains_published_data_and_exact_source_scope():
    plan = ResultAssemblyPlan.for_timepoints(
        "run", "raw", scope=TimepointScope.range(5, 8), total=12,
    )
    pub, events = publisher(plan)
    assert pub.finish("cancelled") is None
    assert events == []
    pub, events = publisher(plan)
    pub.append_completed(part(1), 0)
    pub.append_completed(part(2), 1)
    result = pub.finish("cancelled")
    assert result is pub.published
    assert result.metadata["result_assembly"]["published_t_ranges"] == [[5, 7]]
    assert result.metadata["result_assembly"]["status"] == "cancelled"
    assert np.isnan(result.data[2:]).all()


def test_flexible_without_t_gets_real_time_axis_and_valid_modes():
    plan = ResultAssemblyPlan.for_timepoints("job", "lapse", total=2)
    acc = ProgressiveResultAssembler(plan)
    result = part(3, shape=(3, 4), axes=("Y", "X"))
    result.axis_scales = [30., 30.]
    result.background = None
    result.sharpened = None
    acc.append(result, 0)
    assert result.axis_labels == ["T", "Y", "X"]
    assert result.data.shape == (2, 3, 4)
    assert result.view_modes[0].transpose == (0, 1, 2)
    later = part(4, shape=(3, 4), axes=("Y", "X"))
    later.axis_scales = [30., 30.]
    later.background = None
    later.sharpened = None
    acc.append(later, 1)
    assert np.all(result.data[1] == 4)


def test_multidata_assembles_dataset_not_time_and_retains_coefficients():
    plan = ResultAssemblyPlan.for_datasets("multi", "selection", ["A", "B", "C"])
    acc = ProgressiveResultAssembler(plan)
    a = part(1, shape=(1, 2, 1, 1, 3, 4), axes=("Dataset", "Base", "T", "Z", "Y", "X"))
    a.axis_scales = [1., 1., 1., 20., 35., 35.]
    a.coeffs = np.ones((1, 2, 7, 2, 2), np.float32)
    acc.append(a, 0)
    b = part(2, shape=(1, 2, 1, 1, 3, 4), axes=("Dataset", "Base", "T", "Z", "Y", "X"))
    b.axis_scales = list(a.axis_scales)
    b.coeffs = np.ones((1, 2, 7, 2, 2), np.float32) * 2
    acc.append(b, 1)
    assert a.axis_labels[0] == "Dataset"
    assert a.data.shape == (3, 2, 1, 1, 3, 4)
    assert a.coeffs.shape == (3, 2, 7, 2, 2)
    assert np.all(a.data[1] == 2) and np.all(a.coeffs[1] == 2)
    assert np.isnan(a.data[2]).all()


def test_dataset_assembly_can_add_axis_without_relabeling_t():
    plan = ResultAssemblyPlan.for_datasets("multi", "selection", ["A", "B"])
    acc = ProgressiveResultAssembler(plan)
    a = part(1, shape=(1, 3, 4), axes=("T", "Y", "X"))
    a.axis_scales = [1., 35., 35.]
    a.sharpened = None
    a.background = None
    acc.append(a, 0)
    b = part(2, shape=(1, 3, 4), axes=("T", "Y", "X"))
    b.axis_scales = [1., 35., 35.]
    b.sharpened = None
    b.background = None
    acc.append(b, 1)
    assert a.axis_labels == ["Dataset", "T", "Y", "X"]
    assert a.data.shape == (2, 1, 3, 4)
    assert np.all(a.data[1] == 2)


def test_bad_calibration_or_layers_fail_before_changing_published_image():
    acc = ProgressiveResultAssembler(ResultAssemblyPlan.for_timepoints("j", "s", total=3))
    a = part(1)
    acc.append(a, 0)
    original = a.data.copy()
    b = part(2)
    b.axis_scales[-1] = 42.
    with pytest.raises(AssemblyError, match="calibration"):
        acc.append(b, 1)
    b = part(2)
    b.sharpened = None
    with pytest.raises(AssemblyError, match="layers"):
        acc.append(b, 1)
    np.testing.assert_array_equal(a.data, original)
    acc.append(part(2), 1)
    with pytest.raises(AssemblyError, match="Duplicate"):
        acc.append(part(3), 1)


def test_condition_fold_and_coefficients_refuse_invalid_time_append():
    plan = ResultAssemblyPlan.for_timepoints("j", "s", total=2)
    a = part(1, acquisition_projection={"components": ("time", "condition")})
    with pytest.raises(AssemblyError, match="conditions"):
        ProgressiveResultAssembler(plan).append(a, 0)
    a = part(1, condition_labels=("A", "B"))
    with pytest.raises(AssemblyError, match="condition"):
        ProgressiveResultAssembler(plan).append(a, 0)
    a = part(1, coeffs=np.ones((1, 1, 6, 2, 2), np.float32))
    with pytest.raises(AssemblyError, match="coefficient"):
        ProgressiveResultAssembler(plan).append(a, 0)


def test_unpublished_result_cannot_finalise_complete():
    pub, events = publisher(ResultAssemblyPlan.for_datasets("j", "m", ["A", "B"]))
    assert pub.finish("complete") is None
    assert events == []


def test_source_provenance_keeps_each_individual_run_without_copying_images():
    plan = ResultAssemblyPlan.for_timepoints("j", "s", total=2)
    pub, _events = publisher(plan)
    a, b = part(1), part(2)
    a.metadata["monalisa_calibration"] = {"orientation": "+x+y"}
    b.metadata["monalisa_calibration"] = {"orientation": "-x+y"}
    run1 = SimpleNamespace(source={"path": "scan0"}, params={"preset": "Full"}, node={"node": "n0"})
    run2 = SimpleNamespace(source={"path": "scan1"}, params={"preset": "Full"}, node={"node": "n1"})
    pub.append_completed(a, 0, run=run1)
    pub.append_completed(b, 1, run=run2)
    saved = a.metadata["result_assembly_inputs"]
    assert [x["source"]["path"] for x in saved] == ["scan0", "scan1"]
    assert [x["monalisa_calibration"]["orientation"] for x in saved] == ["+x+y", "-x+y"]
    assert all("image" not in x for x in saved)
    assert pub.finish("complete") is a
