from __future__ import annotations

from dataclasses import dataclass,field
from typing import Any

from ....cgh.localization.reference import TargetLocalizationReference
from ....measurement import ImageMeasurement
from ....calibration.target_localization_calibration import TargetLocalizationCalibrationCandidate


@dataclass
class TargetCalibrationState:
    plane_name: str | None = None
    measurement: ImageMeasurement | None = None
    localization_parameters: dict[str, Any] = field(default_factory=dict)
    reference: TargetLocalizationReference | None = None
    target_signature: str | None = None
    localization: Any = None
    calibration_candidate: TargetLocalizationCalibrationCandidate | None = None
