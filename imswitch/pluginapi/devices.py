"""Stable plugin API for ImSwitch device managers.

This module re-exports the public surface that plugin authors should use.
Do not import deep internal paths; use only imswitch.pluginapi imports.
"""

from imswitch.imcontrol.model.SetupInfo import (
    DeviceInfo,
    DetectorInfo,
    LaserInfo,
    PositionerInfo,
    RS232Info,
)
from imswitch.imcontrol.model.managers.detectors.DetectorManager import (
    DetectorManager,
    DetectorAction,
    DetectorParameter,
    DetectorNumberParameter,
    DetectorListParameter,
)
from imswitch.imcontrol.model.managers.lasers.LaserManager import LaserManager
from imswitch.imcontrol.model.managers.positioners.PositionerManager import PositionerManager
from imswitch.imcontrol.model.managers.rotators.RotatorManager import RotatorManager

__all__ = [
    "DeviceInfo",
    "DetectorInfo",
    "LaserInfo",
    "PositionerInfo",
    "RS232Info",
    "DetectorManager",
    "DetectorAction",
    "DetectorParameter",
    "DetectorNumberParameter",
    "DetectorListParameter",
    "LaserManager",
    "PositionerManager",
    "RotatorManager",
]
