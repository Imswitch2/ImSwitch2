"""Pure-function tests for the standalone AA AOTF calibration utility."""

import copy
import json

import numpy as np
import pytest

from utility_scripts.aa_aotf_calibration import (
    FREQUENCY_PROFILE,
    apply_calibration_to_setup,
    build_output_command,
    discover_aotfs,
    load_setup,
    save_calibration_csv,
    save_setup_with_backup,
)


@pytest.fixture
def setup_dict():
    return {
        "lasers": {
            "473AOTF": {
                "managerName": "AAAOTFLaserManager",
                "managerProperties": {
                    "rs232device": "aaaotf473",
                    "channel": 1,
                },
                "wavelength": 473,
                "valueRangeMin": 0,
                "valueRangeMax": 1023,
            },
            "640AOTF": {
                "managerName": "AAAOTFLaserManager",
                "managerProperties": {
                    "rs232device": "aaaotf",
                    "channel": 2,
                    "protocolProfile": FREQUENCY_PROFILE,
                    "frequencyMHz": 91.25,
                    "calibCsvPath": "/tmp/640.csv",
                },
                "wavelength": 640,
                "valueRangeMin": 2,
                "valueRangeMax": 800,
            },
            "NotAnAOTF": {
                "managerName": "NidaqLaserManager",
                "managerProperties": {},
                "wavelength": 775,
                "valueRangeMin": 0,
                "valueRangeMax": 10,
            },
        },
        "rs232devices": {
            "aaaotf473": {
                "managerName": "RS232Manager",
                "managerProperties": {"port": "COM14"},
            },
            "aaaotf": {
                "managerName": "RS232Manager",
                "managerProperties": {"port": "COM5"},
            },
        },
    }


def test_discover_aotfs_finds_every_aa_manager(setup_dict):
    devices = discover_aotfs(setup_dict)
    assert [device.name for device in devices] == ["473AOTF", "640AOTF"]
    assert devices[0].frequency_mhz is None
    assert devices[1].frequency_mhz == 91.25
    assert devices[1].channel == 2


def test_discover_rejects_missing_serial_reference(setup_dict):
    del setup_dict["rs232devices"]["aaaotf473"]
    with pytest.raises(ValueError, match="missing RS232"):
        discover_aotfs(setup_dict)


def test_combined_test_command_matches_hardware_transcript():
    assert build_output_command(
        1, 143.0, 7, enabled=True
    ) == "L1F143.0P7O1"
    assert build_output_command(
        2, 91.25, 0, enabled=False
    ) == "L2F91.25P0O0"


def test_apply_calibration_updates_only_selected_laser(setup_dict, tmp_path):
    before_other = copy.deepcopy(setup_dict["lasers"]["640AOTF"])
    calibration = tmp_path / "473_calib.csv"
    apply_calibration_to_setup(
        setup_dict,
        laser_name="473AOTF",
        frequency_mhz=143.0,
        value_min=3,
        value_max=700,
        calibration_path=calibration,
    )

    laser = setup_dict["lasers"]["473AOTF"]
    assert laser["managerProperties"]["protocolProfile"] == FREQUENCY_PROFILE
    assert laser["managerProperties"]["frequencyMHz"] == 143.0
    assert laser["managerProperties"]["calibCsvPath"] == str(
        calibration.resolve()
    )
    assert laser["valueRangeMin"] == 3
    assert laser["valueRangeMax"] == 700
    assert setup_dict["lasers"]["640AOTF"] == before_other


def test_save_calibration_csv_is_manager_readable(tmp_path):
    path = tmp_path / "473.csv"
    rows = [(0, 0.001), (7, 1.5), (12, 2.75)]
    save_calibration_csv(
        path,
        rows,
        laser_name="473AOTF",
        wavelength_nm=473,
        frequency_mhz=143.0,
        notes="test calibration",
    )
    assert np.allclose(np.loadtxt(path), np.asarray(rows))
    text = path.read_text(encoding="utf-8")
    assert "measured optical power [mW]" in text
    assert "test calibration" in text


def test_save_setup_creates_backup_and_valid_json(setup_dict, tmp_path):
    path = tmp_path / "setup.json"
    original = {"original": True}
    path.write_text(json.dumps(original), encoding="utf-8")

    backup = save_setup_with_backup(path, setup_dict)

    assert json.loads(path.read_text(encoding="utf-8")) == setup_dict
    assert json.loads(backup.read_text(encoding="utf-8")) == original
    assert backup.name.startswith("setup.json.")
    assert backup.name.endswith(".bak")


def test_load_setup_reports_invalid_json_location(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text('{"lasers":', encoding="utf-8")
    with pytest.raises(ValueError, match="line 1"):
        load_setup(path)
