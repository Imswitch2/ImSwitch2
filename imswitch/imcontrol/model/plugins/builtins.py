"""Built-in device manager contributions."""

from .manifest import DeviceManagerContribution


BUILTIN_DEVICE_MANAGERS: list[DeviceManagerContribution] = [
    DeviceManagerContribution(
        id="AVManager",
        kind="detector",
        display_name="Generic video detector (OpenCV)",
        python_name="imswitch.imcontrol.model.managers.detectors.AVManager:AVManager",
        plugin_name="imswitch-core",
        manager_name_aliases=("builtin.av",),
    ),
    DeviceManagerContribution(
        id="HamamatsuManager",
        kind="detector",
        display_name="Hamamatsu ORCA camera",
        python_name="imswitch.imcontrol.model.managers.detectors.HamamatsuManager:HamamatsuManager",
        plugin_name="imswitch-core",
        manager_name_aliases=("hamamatsu.orca",),
    ),
    DeviceManagerContribution(
        id="NidaqLaserManager",
        kind="laser",
        display_name="NI-DAQ analog laser control",
        python_name="imswitch.imcontrol.model.managers.lasers.NidaqLaserManager:NidaqLaserManager",
        plugin_name="imswitch-core",
        manager_name_aliases=("builtin.nidaq-laser",),
    ),
    DeviceManagerContribution(
        id="CoboltLaserManager",
        kind="laser",
        display_name="Cobolt laser",
        python_name="imswitch.imcontrol.model.managers.lasers.CoboltLaserManager:CoboltLaserManager",
        plugin_name="imswitch-core",
        manager_name_aliases=("cobolt.laser",),
    ),
    DeviceManagerContribution(
        id="MockPositionerManager",
        kind="positioner",
        display_name="Mock positioner for testing",
        python_name="imswitch.imcontrol.model.managers.positioners.MockPositionerManager:MockPositionerManager",
        plugin_name="imswitch-core",
        manager_name_aliases=("builtin.mock-positioner",),
    ),
    DeviceManagerContribution(
        id="NidaqPositionerManager",
        kind="positioner",
        display_name="NI-DAQ analog positioner control",
        python_name="imswitch.imcontrol.model.managers.positioners.NidaqPositionerManager:NidaqPositionerManager",
        plugin_name="imswitch-core",
        manager_name_aliases=("builtin.nidaq-positioner",),
    ),
    DeviceManagerContribution(
        id="ThorlabsMFFManager",
        kind="flip_mirror",
        display_name="Thorlabs MFF flip mirror",
        python_name="imswitch.imcontrol.model.managers.flipMirrors.ThorlabsMFF:ThorlabsMFFManager",
        plugin_name="imswitch-core",
        manager_name_aliases=("ThorlabsMFF", "builtin.thorlabs-mff"),
    ),
    DeviceManagerContribution(
        id="ThorlabsMFFMockManager",
        kind="flip_mirror",
        display_name="Mock Thorlabs MFF flip mirror",
        python_name="imswitch.imcontrol.model.managers.flipMirrors.ThorlabsMFF_mock:MockThorlabsMFFManager",
        plugin_name="imswitch-core",
        manager_name_aliases=(
            "ThorlabsMFF_mock",
            "MockThorlabsMFF",
            "builtin.thorlabs-mff-mock",
        ),
    ),
    DeviceManagerContribution(
        id="LeicaDMIStandMockManager",
        kind="stand",
        display_name="Mock Leica DMI microscope stand",
        python_name="imswitch.imcontrol.model.managers.stands.LeicaDMIManager_mock:MockLeicaDMIStandManager",
        plugin_name="imswitch-core",
        manager_name_aliases=(
            "LeicaDMIManager_mock",
            "MockLeicaDMIManager",
            "builtin.leica-dmi-stand-mock",
        ),
    ),
]
