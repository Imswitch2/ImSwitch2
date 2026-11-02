"""The first processors that turn an image into coordinates.

``threshold`` (image -> labels), ``regionprops`` (labels [+ image] -> points)
and ``pick`` (points -> at most one point) are the measuring half of a smart
pipeline: detector frame -> threshold -> regionprops -> pick -> move. Pinned
here on a synthetic frame with two discs of different size and brightness,
so each rule has one right answer.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.improcess.model import ArrayProcessingResult, PointsResult  # noqa: E402
from imswitch.improcess.model.plugin_contract import contract_problem  # noqa: E402
from imswitch.improcess.model.result import result_kind  # noqa: E402
from imswitch.improcess.processors import _AVAILABLE_PROCESSOR_CLASSES  # noqa: E402
from imswitch.improcess.processors.base import call_output_spec  # noqa: E402
from imswitch.improcess.processors.pick import PickProcessor, pick_index  # noqa: E402
from imswitch.improcess.processors.regionprops import (  # noqa: E402
    RegionPropsProcessor,
    measure_labelled_regions,
)
from imswitch.improcess.processors.run import run_processor  # noqa: E402
from imswitch.improcess.processors.threshold import ThresholdProcessor, threshold_labels  # noqa: E402

POINTS_PROCESSOR_IDS = ("threshold", "regionprops", "pick")

#: Two discs on a 64x64 frame: a big dim one and a small bright one.
BIG = dict(center=(20.0, 20.0), radius=8, value=100.0)
SMALL = dict(center=(44.0, 44.0), radius=4, value=200.0)


class _Logger:
    def exception(self, *args, **kwargs):
        pass


def _disc_frame(discs=(BIG, SMALL), shape=(64, 64)) -> np.ndarray:
    yy, xx = np.mgrid[: shape[0], : shape[1]]
    frame = np.zeros(shape, dtype=np.float32)
    for disc in discs:
        cy, cx = disc["center"]
        frame[(yy - cy) ** 2 + (xx - cx) ** 2 <= disc["radius"] ** 2] = disc["value"]
    return frame


def _disc_area(radius: int) -> int:
    yy, xx = np.mgrid[-radius - 1 : radius + 2, -radius - 1 : radius + 2]
    return int(((yy ** 2 + xx ** 2) <= radius ** 2).sum())


@pytest.fixture
def image():
    return ArrayProcessingResult("frame", _disc_frame(), ["Y", "X"], metadata={"frame_index": 7})


@pytest.fixture
def labels(image):
    results, failures = run_processor(ThresholdProcessor(), [image], {"method": "otsu"}, _Logger())
    assert failures == []
    return results[0]


@pytest.fixture
def points(labels, image):
    results, failures = run_processor(
        RegionPropsProcessor(), [labels, image], {"frame": "detector:Camera"}, _Logger()
    )
    assert failures == []
    return results[0]


# -- registration and contract -------------------------------------------------------------------

@pytest.mark.parametrize("pid", POINTS_PROCESSOR_IDS)
def test_each_is_registered_with_a_headless_contract_and_a_declared_kind(pid):
    cls = _AVAILABLE_PROCESSOR_CLASSES[pid]
    assert cls.id == pid
    assert contract_problem(cls) is None
    assert cls.output_kind == {"threshold": "labels"}.get(pid, "points")
    assert call_output_spec(cls(), cls.default_params(), ("labels",)).kind == cls.output_kind
    assert {field.key for field in cls.param_spec()} == set(cls.default_params())


@pytest.mark.parametrize("pid", POINTS_PROCESSOR_IDS)
def test_each_appears_in_the_editor_catalogue(pid):
    from imswitch.improcess.model.workfloweditor.catalog import build_catalog

    entries = {entry.id: entry for entry in build_catalog(user_plugins=False).processors}
    assert pid in entries
    assert entries[pid].category in ("Segmentation", "Measurement")


def test_a_pipeline_of_the_three_validates_and_declares_points():
    from imswitch.improcess.workflows import Workflow, bootstrap_registry, validate
    from imswitch.improcess.workflows.steps import declared_kinds

    registry = bootstrap_registry(user_plugins=False)
    workflow = Workflow.from_dict({"name": "smart", "schema": 1, "steps": [
        {"step": "source", "id": "raw"},
        {"step": "reconstruct", "id": "rec", "reconstructor": "view-only", "inputs": ["raw"]},
        {"step": "process", "id": "thr", "processor": "threshold", "inputs": ["rec"]},
        {"step": "process", "id": "rp", "processor": "regionprops", "inputs": ["thr", "rec"],
         "params": {"frame": "detector:Camera"}},
        {"step": "process", "id": "pk", "processor": "pick", "inputs": ["rp"],
         "params": {"rule": "brightest"}},
    ]})
    assert validate(workflow, registry) == []
    kinds = declared_kinds(workflow, registry)
    assert (kinds["thr"], kinds["rp"], kinds["pk"]) == ("labels", "points", "points")


def test_the_kind_gates_follow_the_pipeline(image, labels, points):
    assert ThresholdProcessor().accepts(image) and not ThresholdProcessor().accepts(labels)
    regionprops = RegionPropsProcessor()
    assert regionprops.check_inputs([labels]) == (True, "")
    assert regionprops.check_inputs([labels, image]) == (True, "")
    ok, reason = regionprops.check_inputs([image, labels])
    assert not ok and "labels" in reason
    assert PickProcessor().accepts(points) and not PickProcessor().accepts(labels)


# -- threshold ------------------------------------------------------------------------------------

def test_otsu_threshold_labels_the_two_discs(labels):
    assert result_kind(labels) == "labels"
    assert labels.data.shape == (64, 64) and labels.data.dtype == np.int32
    assert labels.data.max() == 2
    assert labels.metadata["frame_index"] == 7
    assert labels.metadata["method"] == "otsu"
    areas = np.bincount(labels.data.ravel())[1:]
    assert sorted(areas.tolist()) == sorted([_disc_area(BIG["radius"]), _disc_area(SMALL["radius"])])


def test_absolute_threshold_min_area_and_invert():
    frame = _disc_frame()
    both, threshold = threshold_labels(frame, method="absolute", value=50.0)
    assert threshold == 50.0 and both.max() == 2
    bright_only, _ = threshold_labels(frame, method="absolute", value=150.0)
    assert bright_only.max() == 1 and (bright_only > 0).sum() == _disc_area(SMALL["radius"])
    big_only, _ = threshold_labels(frame, method="absolute", value=50.0, min_area=100)
    assert big_only.max() == 1 and (big_only > 0).sum() == _disc_area(BIG["radius"])
    background, _ = threshold_labels(frame, method="absolute", value=50.0, invert=True)
    assert background.max() == 1 and (background > 0).sum() == 64 * 64 - (both > 0).sum()


def test_threshold_labels_a_stack_plane_by_plane():
    stack = np.stack([_disc_frame(), _disc_frame((BIG,)), np.zeros((64, 64), np.float32)])
    result = ArrayProcessingResult("stack", stack, ["T", "Y", "X"])
    labels = ThresholdProcessor().apply(result, {"method": "absolute", "value": 50.0})
    assert labels.data.shape == (3, 64, 64)
    assert [int(plane.max()) for plane in labels.data] == [2, 1, 0]
    assert labels.axis_labels == ["T", "Y", "X"]


# -- regionprops ----------------------------------------------------------------------------------

def test_regionprops_measures_centroids_areas_and_intensities(points):
    assert result_kind(points) == "points" and isinstance(points, PointsResult)
    assert points.count == 2
    assert points.frame == "detector:Camera"
    assert points.frame_index == 7
    assert points.axis_names == ["y", "x"]
    by_label = {int(label): index for index, label in enumerate(points.properties["label"])}
    big, small = by_label[1], by_label[2]
    assert np.allclose(points.coordinates[big], BIG["center"], atol=0.5)
    assert np.allclose(points.coordinates[small], SMALL["center"], atol=0.5)
    assert points.properties["area"][big] == _disc_area(BIG["radius"])
    assert points.properties["area"][small] == _disc_area(SMALL["radius"])
    assert points.properties["mean_intensity"].tolist() == [100.0, 200.0]
    assert points.properties["max_intensity"].tolist() == [100.0, 200.0]


def test_regionprops_without_an_image_has_no_intensity_columns(labels):
    points = RegionPropsProcessor().apply(labels, {"frame": "unknown", "min_area": 1})
    assert points.count == 2
    assert set(points.properties) == {"label", "area"}
    assert points.frame == "unknown" and points.frame_index == 7


def test_regionprops_min_area_drops_the_small_disc(labels, image):
    points = RegionPropsProcessor().apply(
        labels, {"frame": "detector:Camera", "min_area": 100, "results": [labels, image]}
    )
    assert points.count == 1
    assert np.allclose(points.coordinates[0], BIG["center"], atol=0.5)


def test_regionprops_on_a_stack_tags_each_point_with_its_plane():
    stack = np.stack([_disc_frame(), _disc_frame((BIG,)), np.zeros((64, 64), np.float32)])
    image = ArrayProcessingResult("stack", stack, ["T", "Y", "X"])
    labels = ThresholdProcessor().apply(image, {"method": "absolute", "value": 50.0})
    points = RegionPropsProcessor().apply(
        labels, {"frame": "detector:Camera", "min_area": 1, "results": [labels, image]}
    )
    assert points.count == 3
    assert points.properties["t"].tolist() == [0, 0, 1]
    assert points.properties["label"].tolist() == [1, 2, 1]
    assert points.properties["mean_intensity"].tolist() == [100.0, 200.0, 100.0]
    assert points.coordinates.shape == (3, 2)


def test_regionprops_refuses_an_image_as_its_labels(image):
    with pytest.raises(ValueError, match="labels"):
        RegionPropsProcessor().apply(image, {"frame": "unknown", "min_area": 1})


def test_measure_labelled_regions_agrees_with_the_roi_table_for_the_same_pixels():
    frame = _disc_frame()
    labels, _ = threshold_labels(frame, method="absolute", value=50.0)
    columns = measure_labelled_regions(labels, frame)
    assert columns["label"].tolist() == [1, 2]
    assert np.allclose(columns["y"], [BIG["center"][0], SMALL["center"][0]], atol=0.5)
    assert np.allclose(columns["x"], [BIG["center"][1], SMALL["center"][1]], atol=0.5)
    empty = measure_labelled_regions(np.zeros((8, 8), np.int32), None)
    assert empty["label"].size == 0 and set(empty) == {"y", "x", "label", "area"}


# -- pick -----------------------------------------------------------------------------------------

def _run_pick(points, **params):
    results, failures = run_processor(PickProcessor(), [points], {"rule": "largest", **params}, _Logger())
    assert failures == [], failures
    return results[0]


@pytest.mark.parametrize("rule, expected", [
    ("largest", BIG), ("brightest", SMALL), ("first", BIG),
])
def test_pick_chooses_the_expected_disc(points, rule, expected):
    one = _run_pick(points, rule=rule)
    assert isinstance(one, PointsResult) and one.count == 1
    assert np.allclose(one.coordinates[0], expected["center"], atol=0.5)
    assert one.properties["mean_intensity"].tolist() == [expected["value"]]
    assert set(one.properties) == set(points.properties)
    assert one.frame == "detector:Camera" and one.frame_index == 7


def test_pick_nearest_measures_distance_to_the_typed_point(points):
    near_small = _run_pick(points, rule="nearest", x=40.0, y=40.0)
    assert np.allclose(near_small.coordinates[0], SMALL["center"], atol=0.5)
    near_big = _run_pick(points, rule="nearest", x=10.0, y=30.0)
    assert np.allclose(near_big.coordinates[0], BIG["center"], atol=0.5)


def test_pick_on_no_points_returns_no_points_in_the_same_frame():
    empty = PointsResult(
        "none", np.zeros((0, 2)), frame="detector:Camera", frame_index=3,
        properties={"label": np.zeros(0, dtype=np.int64), "area": np.zeros(0, dtype=np.int64)},
    )
    one = _run_pick(empty, rule="largest")
    assert one.count == 0 and one.coordinates.shape == (0, 2)
    assert one.frame == "detector:Camera" and one.frame_index == 3
    assert set(one.properties) == {"label", "area"}


def test_pick_names_the_property_a_rule_needs(labels):
    without_intensity = RegionPropsProcessor().apply(labels, {"frame": "unknown", "min_area": 1})
    with pytest.raises(ValueError, match="mean_intensity"):
        PickProcessor().apply(without_intensity, {"rule": "brightest", "x": 0.0, "y": 0.0})
    bare = PointsResult("bare", np.array([[1.0, 2.0], [3.0, 4.0]]))
    with pytest.raises(ValueError, match="area"):
        PickProcessor().apply(bare, {"rule": "largest", "x": 0.0, "y": 0.0})


def test_brightest_breaks_a_mean_tie_on_the_peak():
    coords = np.array([[0.0, 0.0], [1.0, 1.0], [2.0, 2.0]])
    properties = {
        "mean_intensity": np.array([5.0, 5.0, 1.0]),
        "max_intensity": np.array([6.0, 9.0, 50.0]),
    }
    assert pick_index(coords, properties, "brightest") == 1
    assert pick_index(coords, {"mean_intensity": np.array([5.0, 7.0, 1.0])}, "brightest") == 1
    assert pick_index(coords, {}, "nearest", x=2.0, y=2.0) == 2


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
