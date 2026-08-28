import numpy as np

from slmcore import ImageMeasurement
from slmcore.core.cgh.feedback import FeedbackMeasurement,RoundEvaluation
from slmcore.core.cgh.localization import LocalizationResult


def test_feedback_measurement_uses_generic_measurement_and_localization_types():
    measurement = ImageMeasurement(
        image=np.ones((2,2),dtype=np.float64),
        source="detector",
    )
    feedback = FeedbackMeasurement(acquisition=measurement)
    assert feedback.acquisition is measurement
    assert feedback.localization is None


def test_image_measurement_is_immutable_and_host_neutral():
    source = np.arange(16,dtype=np.float64).reshape(4,4)
    measurement = ImageMeasurement(
        image=source,
        source="detector",
        detector="camera_1",
        metadata={"exposure_ms":5.0},
    )

    source[:] = 0.0
    assert measurement.detector == "camera_1"
    assert measurement.source == "detector"
    assert measurement.metadata["exposure_ms"] == 5.0
    assert np.array_equal(
        measurement.image,np.arange(16,dtype=np.float64).reshape(4,4)
    )
    assert measurement.image.flags.writeable is False


def test_round_evaluation_keeps_host_measurement_detector():
    measurement = ImageMeasurement(
        image=np.ones((2,2)),
        source="detector",
        detector="camera_2",
    )
    evaluation = RoundEvaluation(
        index=0,
        measurement=FeedbackMeasurement(acquisition=measurement),
    )

    assert isinstance(evaluation.measurement.acquisition,ImageMeasurement)
    assert evaluation.measurement.acquisition.detector == "camera_2"
    assert np.array_equal(evaluation.measurement.acquisition.image,measurement.image)
