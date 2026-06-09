from pathlib import Path

import h5py
import numpy as np
import pytest

from imswitch.improcess.analysis.colocalization import (
    colocalization_batch,
    colocalization_metrics,
)
from imswitch.improcess.analysis.roi_manager import ROIRecord
from imswitch.improcess.model import PlotPayload, ProcessingResult
from imswitch.improcess.processors import available_processor_ids
from imswitch.improcess.processors.colocalization import (
    ColocalizationProcessor,
    ColocalizationResult,
)


class MinimalResult(ProcessingResult):
    def save(self, path: Path, fmt: str):
        pass


def test_colocalization_metrics_detects_correlated_images():
    a = np.arange(100, dtype=float).reshape(10, 10)
    b = 2.0 * a + 5.0

    record = colocalization_metrics(a, b)

    assert record.pearson == pytest.approx(1.0)
    assert record.overlap_coefficient > 0.99
    assert record.pixel_count == 100


def test_colocalization_metrics_detects_anticorrelation():
    a = np.arange(100, dtype=float).reshape(10, 10)
    b = np.flipud(a)

    record = colocalization_metrics(a, b)

    assert record.pearson < 0


def test_colocalization_batch_uses_mask_roi_and_thresholds():
    a = np.zeros((4, 4), dtype=float)
    b = np.zeros((4, 4), dtype=float)
    a[0, 0] = 10.0
    a[0, 1] = 8.0
    b[0, 0] = 9.0
    b[1, 1] = 7.0
    roi = ROIRecord("mask", "mask", (0, 2, 0, 2), pixels=((0, 0), (0, 1), (1, 1)))

    analysis = colocalization_batch(a, b, [roi], threshold_a=1.0, threshold_b=1.0)
    row = analysis.rows()[0]

    assert row["name"] == "mask"
    assert row["pixel_count"] == 3
    assert row["manders_m1"] == pytest.approx(9.0 / 16.0)
    assert row["manders_m2"] == pytest.approx(8.0 / 14.0)
    assert analysis.metadata["source"] == "roi"


def test_colocalization_processor_registered_and_generates_payload():
    y, x = np.mgrid[:8, :8]
    a = y + x
    b = 3.0 * a
    result = MinimalResult(
        name="channels",
        data=np.stack([a, b], axis=0),
        axis_labels=["C", "Y", "X"],
    )
    processor = ColocalizationProcessor()

    coloc = processor.apply(
        result,
        {"compare_axis": "C", "index_a": 0, "index_b": 1, "threshold_a": 0.0, "threshold_b": 0.0},
    )

    assert "colocalization" in available_processor_ids()
    assert isinstance(coloc, ColocalizationResult)
    assert coloc.data.shape == (1, len(ColocalizationResult._METRICS))
    payloads = coloc.plot_payloads()
    assert len(payloads) == 1
    assert isinstance(payloads[0], PlotPayload)
    assert payloads[0].metadata["region_count"] == 1


def test_colocalization_result_saves_hdf5(tmp_path):
    a = np.arange(25, dtype=float).reshape(5, 5)
    b = a.copy()
    analysis = colocalization_batch(a, b)
    result = ColocalizationResult("coloc", analysis, params={"compare_axis": "C"})
    out_path = tmp_path / "coloc.h5"

    result.save(out_path, "hdf5")

    with h5py.File(out_path, "r") as h5:
        assert h5.attrs["region_count"] == 1
        assert "records" in h5
        assert "scatter" in h5
        assert h5["records"]["pearson"][0] == pytest.approx(1.0)
