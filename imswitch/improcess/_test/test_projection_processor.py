from pathlib import Path

import h5py
import numpy as np

from imswitch.improcess.analysis.projections import axis_index_from_label, project_array
from imswitch.improcess.model import PlotPayload, ProcessingResult
from imswitch.improcess.processors import available_processor_ids
from imswitch.improcess.processors.projection import ProjectionProcessor, ProjectionResult


class MinimalResult(ProcessingResult):
    def save(self, path: Path, fmt: str):
        pass


def test_project_array_max_removes_axis_and_labels():
    data = np.arange(2 * 3 * 4, dtype=np.float32).reshape(2, 3, 4)

    analysis = project_array(
        data,
        axis=1,
        mode="max",
        axis_labels=["T", "Z", "X"],
        axis_scales=[1.0, 2.0, 3.0],
    )

    assert analysis.data.shape == (2, 4)
    assert np.array_equal(analysis.data, np.max(data, axis=1))
    assert analysis.axis_label == "Z"
    assert analysis.output_axis_labels == ["T", "X"]
    assert analysis.output_axis_scales == [1.0, 3.0]


def test_project_array_ignores_nan_for_mean():
    data = np.array([[1.0, np.nan], [3.0, 5.0]])

    analysis = project_array(data, axis=0, mode="mean", axis_labels=["Y", "X"])

    assert np.allclose(analysis.data, np.array([2.0, 5.0]))


def test_axis_index_from_label_accepts_labels_and_indices():
    labels = ["T", "Z", "Y", "X"]

    assert axis_index_from_label("Z", labels, 4) == 1
    assert axis_index_from_label("D2", labels, 4) == 2
    assert axis_index_from_label("-1", labels, 4) == 3


def test_projection_processor_registered_and_generates_result_payload():
    data = np.arange(2 * 3 * 4, dtype=np.float32).reshape(2, 3, 4)
    result = MinimalResult(
        name="stack",
        data=data,
        axis_labels=["T", "Y", "X"],
        axis_scales=[2.0, 1.0, 1.0],
        scale_unit="um",
    )
    processor = ProjectionProcessor()

    projected = processor.apply(result, {"axis": "T", "mode": "sum"})

    assert "projection" in available_processor_ids()
    assert isinstance(projected, ProjectionResult)
    assert projected.data.shape == (3, 4)
    assert np.array_equal(projected.data, np.sum(data, axis=0))
    assert projected.axis_labels == ["Y", "X"]
    assert projected.axis_scales == [1.0, 1.0]
    assert projected.scale_unit == "um"
    payloads = projected.plot_payloads()
    assert len(payloads) == 1
    assert isinstance(payloads[0], PlotPayload)


def test_projection_result_saves_hdf5(tmp_path):
    analysis = project_array(
        np.arange(2 * 3 * 4, dtype=np.float32).reshape(2, 3, 4),
        axis=0,
        mode="max",
        axis_labels=["T", "Y", "X"],
    )
    result = ProjectionResult("projection", analysis, params={"axis": "T"})
    out_path = tmp_path / "projection.h5"

    result.save(out_path, "hdf5")

    with h5py.File(out_path, "r") as h5:
        # One dataset name across every image result: a reader should not have
        # to know which processor made a file to find its pixels.
        assert "data" in h5
        assert h5.attrs["projection_mode"] == "max"
        assert h5.attrs["projection_axis_label"] == "T"
