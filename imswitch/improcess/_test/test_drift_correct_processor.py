from pathlib import Path

import numpy as np

from imswitch.improcess.model import ProcessingResult
from imswitch.improcess.processors.drift_correct import DriftCorrectProcessor
from imswitch.improcess.processors.drift_correct.result import DriftCorrectedResult


class MinimalResult(ProcessingResult):
    def save(self, path: Path, fmt: str):
        pass


def test_drift_correct_processor_preserves_axis_calibration():
    data = np.zeros((2, 12, 12), dtype=np.float32)
    data[:, 4:8, 5:9] = 1.0
    result = MinimalResult(
        name="stack",
        data=data,
        axis_labels=["T", "Y", "X"],
        axis_scales=[1.0, 0.12, 0.13],
        scale_unit="um",
    )
    processor = DriftCorrectProcessor()

    corrected = processor.apply(
        result,
        {
            "reference_frame": 0,
            "mode": "reference",
            "upsample_factor": 1,
        },
    )

    assert isinstance(corrected, DriftCorrectedResult)
    assert corrected.axis_labels == ["T", "Y", "X"]
    assert corrected.axis_scales == [1.0, 0.12, 0.13]
    assert corrected.scale_unit == "um"
