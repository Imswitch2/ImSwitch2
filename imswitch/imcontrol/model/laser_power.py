from enum import Enum


class LaserPowerContext(str, Enum):
    """Power state owned by the laser subsystem.

    ``MANUAL`` is the live interactive setpoint and therefore hardware-backed.
    ``SCAN`` is an acquisition setpoint kept in software until a scan lifecycle
    explicitly applies it.
    """

    MANUAL = "manual"
    SCAN = "scan"


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
