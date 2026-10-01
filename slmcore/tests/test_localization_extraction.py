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


def _localization_with_missing(total: int,missing: int) -> LocalizationResult:
    indices = np.vstack((np.arange(total,dtype=int),np.zeros(total,dtype=int)))
    positions = np.vstack((np.arange(total,dtype=float),np.zeros(total,dtype=float)))
    matched = np.ones(total,dtype=bool)
    if missing:
        matched[-missing:] = False
    return LocalizationResult(
        target_type="multi_foci_vector",
        target_params={},
        parameters={},
        lattice_indices=indices,
        crop_coord=(0,10,0,max(10,total)),
        cropped_image=np.zeros((10,max(10,total))),
        expected_positions_px=positions,
        measured_positions_px=positions,
        period_x_px=1.0,
        period_y_px=1.0,
        offset_x_px=0.0,
        offset_y_px=0.0,
        diagnostics={
            "matched_mask":tuple(bool(value) for value in matched),
            "matched_count":int(np.count_nonzero(matched)),
            "missing_count":int(missing),
        },
    )


def test_automatic_missing_inference_accepts_up_to_ten_percent():
    from slmcore.application.feedback.localization import LocalizationService

    localization = _localization_with_missing(10,1)
    inferred = LocalizationService._infer_missing_within_fraction(localization,0.10)

    assert inferred.diagnostics["matched_count"] == 9
    assert inferred.diagnostics["inferred_count"] == 1
    assert inferred.diagnostics["missing_count"] == 0


def test_automatic_missing_inference_rejects_more_than_ten_percent():
    import pytest
    from slmcore.application.feedback.localization import LocalizationService

    localization = _localization_with_missing(10,2)
    with pytest.raises(RuntimeError,match="exceeding the 10.0% automatic inference limit"):
        LocalizationService._infer_missing_within_fraction(localization,0.10)
