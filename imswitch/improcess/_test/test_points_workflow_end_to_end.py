"""A workflow that turns a recording into one coordinate: the detection half
of a smart pipeline, run as an ordinary ImProcess workflow."""
import csv

import h5py
import numpy as np
import pytest

from imswitch.improcess.model import PointsResult
from imswitch.improcess.workflows.runner import run
from imswitch.improcess.workflows.runtime import bootstrap_registry
from imswitch.improcess.workflows.steps import (
    Process, Reconstruct, Save, Source, Workflow, declared_kinds, validate,
)


@pytest.fixture(scope="module")
def registry():
    return bootstrap_registry(user_plugins=False)


def _two_discs(size=64):
    """A dark frame with a big dim disc at (20, 20) and a small bright one at (44, 44)."""
    yy, xx = np.mgrid[:size, :size]
    image = np.full((size, size), 10.0, dtype=np.float32)
    image[(yy - 20) ** 2 + (xx - 20) ** 2 <= 8 ** 2] = 100.0
    image[(yy - 44) ** 2 + (xx - 44) ** 2 <= 4 ** 2] = 200.0
    return image


def _recording(path, frames=2):
    stack = np.stack([_two_discs()] * frames)
    with h5py.File(str(path), "w") as handle:
        dataset = handle.create_dataset("data", data=stack)
        dataset.attrs["element_size_um"] = [1.0, 0.1, 0.1]
        dataset.attrs["axes"] = "TYX"
    return path


def _workflow(raw, rule="largest"):
    return Workflow("find the cell", [
        Source("raw", path=str(raw)),
        Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("thr", "threshold", {"method": "otsu", "min_area": 5}, inputs=["rec"]),
        Process("rp", "regionprops", {"frame": "detector:Camera"}, inputs=["thr", "rec"]),
        Process("pk", "pick", {"rule": rule}, inputs=["rp"]),
        Save("out", input="pk", fmt="csv"),
    ])


def test_the_workflow_declares_points_and_validates(registry, tmp_path):
    workflow = _workflow(_recording(tmp_path / "cells.h5"))

    assert validate(workflow, registry) == []
    kinds = declared_kinds(workflow, registry)
    assert kinds["thr"] == "labels" and kinds["rp"] == "points" and kinds["pk"] == "points"


def test_threshold_regionprops_pick_yield_one_tagged_coordinate(registry, tmp_path):
    raw = _recording(tmp_path / "cells.h5")

    with run(_workflow(raw), registry=registry, out_dir=tmp_path / "out") as report:
        regions = report.result("rp")
        picked = report.result("pk")
        saved = report.receipts[0].primary

    assert isinstance(regions, PointsResult) and regions.frame == "detector:Camera"
    assert regions.count == 4  # two discs on each of two frames
    assert set(regions.properties) >= {"label", "area", "mean_intensity", "t"}

    assert isinstance(picked, PointsResult) and picked.count == 1
    assert picked.frame == "detector:Camera"
    y, x = picked.coordinates[0]
    assert abs(y - 20) < 0.5 and abs(x - 20) < 0.5  # the largest disc
    assert picked.properties["area"][0] > 150

    with open(saved, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1 and abs(float(rows[0]["x"]) - 20) < 0.5


def test_pick_brightest_chooses_the_other_disc(registry, tmp_path):
    raw = _recording(tmp_path / "cells.h5")
    with run(_workflow(raw, rule="brightest"), registry=registry, out_dir=tmp_path / "out",
             write_saves=False) as report:
        y, x = report.result("pk").coordinates[0]
    assert abs(y - 44) < 0.5 and abs(x - 44) < 0.5
