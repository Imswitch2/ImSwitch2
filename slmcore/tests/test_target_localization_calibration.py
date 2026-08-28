from types import SimpleNamespace

import numpy as np
import pytest


def _calibration_api():
    try:
        from slmcore.core.calibration import (
            TargetLocalizationCalibrationCandidate,
            fit_target_localization_calibration,
        )
    except Exception as error:
        pytest.skip(
            f"slmcore calibration dependencies are unavailable: {error}"
        )
    return (
        TargetLocalizationCalibrationCandidate,
        fit_target_localization_calibration,
    )


class LatticeTargetGeometry(SimpleNamespace):
    pass


def _geometry(
    *,
    period_x=12.0,
    period_y=8.0,
    count_x=3,
    count_y=2,
):
    return LatticeTargetGeometry(
        geometry_type="lattice",
        period_x_reference_px=period_x,
        period_y_reference_px=period_y,
        count_x=count_x,
        count_y=count_y,
    )


def _resolution(geometry=None):
    return SimpleNamespace(geometry=geometry if geometry is not None else _geometry())


def _localization(*, period_x=24.0, period_y=10.0, matched=None):
    indices = np.array(
        [
            [0,1,2,0,1,2],
            [0,0,0,1,1,1],
        ],
        dtype=np.int64,
    )
    if matched is None:
        matched = (True,True,True,True,False,False)
    return SimpleNamespace(
        period_x_px=period_x,
        period_y_px=period_y,
        lattice_indices=indices,
        diagnostics={
            "matched_mask":tuple(bool(value) for value in matched),
            "rms_residual_px":0.25,
        },
    )


def test_fit_target_localization_calibration_returns_diagonal_candidate():
    Candidate,fit = _calibration_api()
    candidate = fit(
        resolution=_resolution(),
        localization=_localization(),
        detector_pixel_size_um=0.5,
        plane="Plane A",
        metadata={"slm_key":"SLM"},
        created_at="2026-08-13T12:00:00",
    )

    assert isinstance(candidate,Candidate)
    assert candidate.target_period_x_reference_px == 12.0
    assert candidate.target_period_y_reference_px == 8.0
    assert candidate.target_kx == pytest.approx(12.0 / 512.0)
    assert candidate.target_ky == pytest.approx(8.0 / 512.0)
    assert candidate.measured_period_x_um == pytest.approx(12.0)
    assert candidate.measured_period_y_um == pytest.approx(5.0)
    assert candidate.calibration.kx_per_um == pytest.approx(
        (12.0 / 512.0) / 12.0
    )
    assert candidate.calibration.ky_per_um == pytest.approx(
        (8.0 / 512.0) / 5.0
    )
    assert candidate.calibration.source == "target_localization"
    assert candidate.calibration.plane == "Plane A"
    assert candidate.calibration.cam_px_size_um == 0.5
    assert candidate.calibration.metadata["slm_key"] == "SLM"
    assert candidate.matched_count == 4
    assert candidate.expected_count == 6
    assert candidate.rms_residual_px == 0.25


def test_fit_target_localization_calibration_warns_on_symmetric_target():
    _Candidate,fit = _calibration_api()
    candidate = fit(
        resolution=_resolution(
            _geometry(period_x=8.0,period_y=8.0,count_x=3,count_y=3)
        ),
        localization=_localization(),
        detector_pixel_size_um=0.5,
    )

    assert candidate.warnings
    assert "Symmetric target" in candidate.warnings[0]
    assert candidate.calibration.metadata["warnings"] == candidate.warnings


@pytest.mark.parametrize(
    "geometry,error",
    [
        (SimpleNamespace(geometry_type="unknown"),"lattice target geometry"),
        (_geometry(count_x=1,count_y=3),"target count X"),
        (_geometry(count_x=3,count_y=1),"target count Y"),
    ],
)
def test_fit_target_localization_calibration_rejects_non_2d_targets(
    geometry,error,
):
    _Candidate,fit = _calibration_api()
    with pytest.raises(ValueError,match=error):
        fit(
            resolution=_resolution(geometry),
            localization=_localization(),
            detector_pixel_size_um=0.5,
        )


@pytest.mark.parametrize(
    "localization,error",
    [
        (_localization(period_x=0.0),"localized period X"),
        (_localization(period_y=-1.0),"localized period Y"),
        (
            _localization(matched=(True,True,True,False,False,False)),
            "target Y indices",
        ),
        (
            _localization(matched=(True,False,False,True,False,False)),
            "At least three",
        ),
        (
            SimpleNamespace(
                period_x_px=24.0,
                period_y_px=10.0,
                lattice_indices=np.zeros((2,4),dtype=np.int64),
                diagnostics={},
            ),
            "matched-point information",
        ),
    ],
)
def test_fit_target_localization_calibration_rejects_invalid_localization(
    localization,error,
):
    _Candidate,fit = _calibration_api()
    with pytest.raises(ValueError,match=error):
        fit(
            resolution=_resolution(),
            localization=localization,
            detector_pixel_size_um=0.5,
        )


def test_fit_target_localization_calibration_rejects_invalid_detector_scale():
    _Candidate,fit = _calibration_api()
    with pytest.raises(ValueError,match="detector pixel size"):
        fit(
            resolution=_resolution(),
            localization=_localization(),
            detector_pixel_size_um=0.0,
        )
