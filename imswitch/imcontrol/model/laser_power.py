from dataclasses import dataclass
from enum import Enum


class LaserPowerContext(str, Enum):
    """Power state owned by the laser subsystem.

    ``MANUAL`` is the live interactive setpoint and therefore hardware-backed.
    ``SCAN`` is an acquisition setpoint kept in software until a scan lifecycle
    explicitly applies it.
    """

    MANUAL = "manual"
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
        f"Unknown laser power context {context!r}; expected 'manual' or 'scan'."
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
