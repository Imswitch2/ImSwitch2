import json

from imswitch.imcontrol.model.SetupInfo import (
    CalibrationsInfo,
    SetupInfo,
    TransformCalibrationSetupInfo,
)
from imswitch.imcontrol.model.configfiletools import pruneDefaultSetupInfoFields


def test_transform_calibration_setup_round_trips():
    setup = SetupInfo(
        calibrations=CalibrationsInfo(
            transforms={
                "image_registration": TransformCalibrationSetupInfo(
                    auto_load=True,
                    calibration_file="calibrations/transforms/registration.json",
                )
            }
        )
    )

    serialized = pruneDefaultSetupInfoFields(setup)
    reloaded = SetupInfo.from_json(json.dumps(serialized), infer_missing=True)

    info = reloaded.calibrations.transforms["image_registration"]
    assert info.auto_load is True
    assert info.calibration_file == "calibrations/transforms/registration.json"


def test_default_calibrations_section_is_pruned():
    serialized = pruneDefaultSetupInfoFields(SetupInfo())
    assert "calibrations" not in serialized
