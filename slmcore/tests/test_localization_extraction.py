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


def test_infer_missing_localization_preserves_match_provenance():
    from slmcore.core.cgh.localization import infer_missing_localization

    positions = np.array([
        [5.0,15.0,5.0,15.0],
        [5.0,5.0,15.0,15.0],
    ])
    measured = positions.copy()
    measured[:,1] = [100.0,100.0]
    localization = LocalizationResult(
        target_type="multi_foci_vector",
        target_params={},
        parameters={},
        lattice_indices=np.array([[0,1,0,1],[0,0,1,1]]),
        crop_coord=(0,20,0,20),
        cropped_image=np.zeros((20,20)),
        expected_positions_px=positions,
        measured_positions_px=measured,
        period_x_px=10.0,
        period_y_px=10.0,
        offset_x_px=5.0,
        offset_y_px=5.0,
        diagnostics={
            "matched_mask":(True,False,True,True),
            "matched_count":3,
            "missing_count":1,
        },
    )

    inferred = infer_missing_localization(localization)

    assert inferred is not localization
    assert inferred.diagnostics["matched_mask"] == (True,False,True,True)
    assert inferred.diagnostics["inferred_mask"] == (False,True,False,False)
    assert inferred.diagnostics["matched_count"] == 3
    assert inferred.diagnostics["inferred_count"] == 1
    assert inferred.diagnostics["missing_count"] == 0
    np.testing.assert_array_equal(
        inferred.measured_positions_px[:,1],positions[:,1],
    )
    np.testing.assert_array_equal(
        localization.measured_positions_px[:,1],[100.0,100.0],
    )
