from pathlib import Path

import pytest

from imswitch.imcontrol.model.SetupInfo import SetupInfo
from imswitch.imcontrol.model.managers import (
    DetectorsManager,
    LasersManager,
    NidaqManager,
    PositionersManager,
    RS232sManager,
)


pytestmark = pytest.mark.nohardware


PROFILE_PATH = (
    Path(__file__).resolve().parent
    / "_data"
    / "user_defaults"
    / "imcontrol_setups"
    / "example_no_hardware.json"
)


def _load_no_hardware_setup():
    return SetupInfo.from_json(PROFILE_PATH.read_text(), infer_missing=True)


def test_no_hardware_profile_parses():
    setup_info = _load_no_hardware_setup()

    assert setup_info.detectors["Mock Camera"].managerName == "AVManager"
    assert setup_info.detectors["Mock Camera"].managerProperties["cameraListIndex"] == "mock"
    assert setup_info.positioners["Mock X"].managerName == "MockPositionerManager"
    assert setup_info.positioners["Mock Y"].managerName == "MockPositionerManager"
    assert setup_info.nidaq.simulation is True
    assert setup_info._catchAll["availableWidgets"]


def test_no_hardware_profile_has_no_physical_io_channels():
    setup_info = _load_no_hardware_setup()

    for device_info in setup_info.getAllDevices().values():
        assert device_info.getAnalogChannel() is None
        assert device_info.getDigitalLine() is None

    assert setup_info.rs232devices == {}
    assert setup_info.scan.lineClockLine is None
    assert setup_info.scan.frameStartClockLine is None
    assert setup_info.scan.frameEndClockLine is None


def test_no_hardware_profile_builds_core_managers():
    setup_info = _load_no_hardware_setup()
    nidaq_manager = NidaqManager(setup_info)
    rs232s_manager = RS232sManager(setup_info.rs232devices)
    low_level_managers = {
        "nidaqManager": nidaq_manager,
        "rs232sManager": rs232s_manager,
    }
    detectors_manager = DetectorsManager(setup_info.detectors, updatePeriod=300, **low_level_managers)
    positioners_manager = PositionersManager(setup_info.positioners, **low_level_managers)
    lasers_manager = LasersManager(setup_info.lasers, **low_level_managers)

    try:
        assert detectors_manager.hasDevices()
        assert detectors_manager.getCurrentDetectorName() == "Mock Camera"
        assert positioners_manager.hasDevices()
        assert positioners_manager["Mock X"].position["X"] == 0
        assert positioners_manager["Mock Y"].position["Y"] == 0
        assert not lasers_manager.hasDevices()
    finally:
        detectors_manager.finalize()
        positioners_manager.finalize()
        lasers_manager.finalize()
