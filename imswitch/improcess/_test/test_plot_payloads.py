import numpy as np

from imswitch.improcess.model import PlotPayload, PlotSeries, ProcessingResult
from imswitch.improcess.processors.drift_correct.result import DriftCorrectedResult


def test_processing_result_default_plot_payloads_empty():
    class MinimalResult(ProcessingResult):
        def save(self, path, fmt):
            pass

    result = MinimalResult(name="minimal", data=np.zeros((2, 2)), axis_labels=["Y", "X"])

    assert result.plot_payloads() == []


def test_drift_corrected_result_exposes_drift_trace_payload():
    drift_xy = np.array([[0.0, 0.0], [1.5, -0.5], [2.0, -1.0]], dtype=np.float32)
    result = DriftCorrectedResult(
        name="drifted",
        data=np.zeros((3, 4, 4), dtype=np.float32),
        axis_labels=["T", "Y", "X"],
        drift_xy=drift_xy,
    )

    payloads = result.plot_payloads()

    assert len(payloads) == 1
    assert isinstance(payloads[0], PlotPayload)
    assert payloads[0].title == "Drift correction"
    assert [series.name for series in payloads[0].series] == ["Y shift", "X shift"]
    assert all(isinstance(series, PlotSeries) for series in payloads[0].series)
    np.testing.assert_array_equal(payloads[0].series[0].x, np.array([0, 1, 2]))
    np.testing.assert_array_equal(payloads[0].series[0].y, drift_xy[:, 0])
    np.testing.assert_array_equal(payloads[0].series[1].y, drift_xy[:, 1])

