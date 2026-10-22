import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Union




class LaserPowerMode(str, Enum):
    """Setup-level policy for how acquisition power relates to Live power.

    ``COUPLED`` preserves the immediate pre-Live/Scan ImSwitch2 lifecycle:
    acquisition uses the current hardware setpoint and does not perform a
    Live/Scan handoff. ``SEPARATED`` enables the independent Live and Scan
    contexts introduced by the laser-power rework.
    """

    COUPLED = "coupled"
    SEPARATED = "separated"


class LaserPowerContext(str, Enum):
    """Power state owned by the laser subsystem.

    ``LIVE`` is the interactive setpoint and therefore hardware-backed.
    ``SCAN`` is an acquisition setpoint kept in software until a scan lifecycle
    explicitly applies it.
    """

    LIVE = "live"
    SCAN = "scan"


class LaserScanPowerControl(str, Enum):
    """Which subsystem owns acquisition power for one laser."""

    DIRECT = "direct"
    AO = "ao"


@dataclass(frozen=True)
class LaserScanState:
    """Semantic scan status for one laser.

    This state is deliberately independent of Qt widgets.  The scan workflow
    publishes it and consumers such as :class:`LaserController` decide how to
    present it.
    """

    active_for_scan: bool = False
    power_control: LaserScanPowerControl = LaserScanPowerControl.DIRECT


@dataclass(frozen=True)
class LaserAcquisitionState:
    """Resolved acquisition-power state for one configured laser path.

    Unlike :class:`LaserScanState`, which reports scan configuration only,
    this object answers what acquisition would actually use under the current
    laser-category power mode. ``value`` is intentionally ``None`` when the laser is
    inactive for the current scan, when AO owns acquisition power, or when the
    effective power-bearing device is binary and therefore has no scalar
    setpoint.
    """

    active_for_scan: bool
    power_mode: LaserPowerMode
    power_control: LaserScanPowerControl
    power_device: str
    value: Optional[Union[int, float]] = None
    value_context: Optional[LaserPowerContext] = None
    uses_fallback: bool = False


def normalizeLaserPowerContext(context) -> LaserPowerContext:
    """Return ``context`` as :class:`LaserPowerContext`.

    Strings are accepted at public/controller boundaries so scripting and RPC
    callers do not need to construct the enum themselves.
    """
    if isinstance(context, LaserPowerContext):
        return context
    if isinstance(context, str):
        try:
            return LaserPowerContext(context.strip().lower())
        except ValueError:
            pass
    raise ValueError(
        f"Unknown laser power context {context!r}; expected 'live' or 'scan'."
    )


def normalizeLaserScanState(state) -> LaserScanState:
    """Normalize a service payload to :class:`LaserScanState`.

    Mapping input is accepted to keep the workflow-service boundary friendly to
    scripts and lightweight tests.
    """
    if isinstance(state, LaserScanState):
        return state
    if isinstance(state, dict):
        control = state.get("power_control", LaserScanPowerControl.DIRECT)
        if not isinstance(control, LaserScanPowerControl):
            control = LaserScanPowerControl(str(control).lower())
        return LaserScanState(
            active_for_scan=bool(state.get("active_for_scan", False)),
            power_control=control,
        )
    raise TypeError(f"Unsupported laser scan state: {state!r}")


def normalizeLaserPowerMode(mode) -> LaserPowerMode:
    """Return ``mode`` as :class:`LaserPowerMode`. Missing/None is coupled."""
    if mode is None:
        return LaserPowerMode.COUPLED
    if isinstance(mode, LaserPowerMode):
        return mode
    if isinstance(mode, str):
        try:
            return LaserPowerMode(mode.strip().lower())
        except ValueError:
            pass
    raise ValueError(
        f"Unknown laser power mode {mode!r}; expected 'coupled' or 'separated'."
    )


def laserPowerModeFromSetup(setupInfo) -> LaserPowerMode:
    """Resolve the configured laser power policy from setup category settings.

    The setting lives at ``deviceCategorySettings.lasers.powerMode`` so device
    dictionaries stay pure name→device mappings. Missing/malformed settings
    intentionally fall back to :attr:`LaserPowerMode.COUPLED`.
    """
    settings = getattr(setupInfo, "deviceCategorySettings", None)
    if not isinstance(settings, dict):
        return LaserPowerMode.COUPLED
    laser_settings = settings.get("lasers")
    if not isinstance(laser_settings, dict):
        return LaserPowerMode.COUPLED
    try:
        return normalizeLaserPowerMode(laser_settings.get("powerMode"))
    except ValueError as e:
        # A hand-edited typo must not stop the laser controller from starting.
        logging.getLogger(__name__).warning(f"{e} Using 'coupled'.")
        return LaserPowerMode.COUPLED
