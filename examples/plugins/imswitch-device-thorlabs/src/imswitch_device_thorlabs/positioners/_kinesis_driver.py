"""Thorlabs MLS203 Kinesis XY stage driver (real + mock).

Bundled with the plugin so the manager has no dependency on ImSwitch's
internal ``interfaces`` package. The real driver talks to the stage through
``pylablib`` (the ``[hardware]`` extra); the mock is pure Python and needs no
SDK, so the plugin is testable headless.
"""

try:
    from imswitch.imcommon.model import initLogger
except Exception:  # pragma: no cover - only when run outside ImSwitch
    import logging

    def initLogger(owner, instanceName=None):
        name = type(owner).__name__
        if instanceName:
            name = f"{name}.{instanceName}"
        return logging.getLogger(name)


class KinesisStage:
    """Thin wrapper around pylablib's KinesisMotor device for two-axis stages."""

    def __init__(self, snr: str, scale: str = "MLS203", is_rack_system: bool = True):
        """
        Args:
            snr: Device serial number.
            scale: Stage scale identifier (e.g., "MLS203").
            is_rack_system: Whether the device is a rack-mounted system.
        """
        self.__logger = initLogger(self)
        from pylablib.devices.Thorlabs import KinesisMotor as _KinesisMotor

        self._stage = _KinesisMotor(snr, scale=scale, is_rack_system=is_rack_system)
        self.__logger.info(f"Initialized Thorlabs Kinesis stage {snr} (scale={scale})")

    def get_position(self, channel: int) -> float:
        """Return current position in mm for the specified channel (1=X, 2=Y)."""
        return self._stage.get_position(channel=channel)

    def move_to(self, position: float, channel: int) -> None:
        """Move to an absolute position in mm."""
        self._stage.move_to(position, channel=channel)

    def move_by(self, delta: float, channel: int) -> None:
        """Move by a relative displacement in mm."""
        self._stage.move_by(delta, channel=channel)

    def jog(self, direction: str, channel: int, kind: str = "continuous") -> None:
        """Start jogging: ``direction`` is ``'+'`` or ``'-'``."""
        self._stage.jog(direction, channel, kind=kind)

    def stop(self, channel: int) -> None:
        """Stop motion on the specified channel."""
        self._stage.stop(channel=channel)

    def home(self, channel: int, sync: bool = False, force: bool = True) -> None:
        """Home the specified channel."""
        self._stage.home(sync=sync, force=force, channel=channel)

    def close(self) -> None:
        """Disconnect from the device."""
        self._stage.close()


class MockKinesisStage:
    """Simulated Kinesis XY stage for headless operation."""

    def __init__(self, snr: str, scale: str = "MLS203", is_rack_system: bool = True):
        """``scale``/``is_rack_system`` are accepted for signature parity but
        ignored by the simulation."""
        self.__logger = initLogger(self)
        self._snr = snr
        self._position = {1: 0.0, 2: 0.0}  # channel -> position in mm
        self._jogging = {1: False, 2: False}  # channel -> jogging state
        self.__logger.info(f"Initialized mock Kinesis stage {snr} (scale={scale})")

    def get_position(self, channel: int) -> float:
        return self._position.get(channel, 0.0)

    def move_to(self, position: float, channel: int) -> None:
        self._position[channel] = position
        self.__logger.debug(
            f"Mock stage {self._snr} ch{channel}: moved to {position:.3f} mm"
        )

    def move_by(self, delta: float, channel: int) -> None:
        self._position[channel] = self._position.get(channel, 0.0) + delta
        self.__logger.debug(
            f"Mock stage {self._snr} ch{channel}: moved by {delta:.3f} mm "
            f"to {self._position[channel]:.3f} mm"
        )

    def jog(self, direction: str, channel: int, kind: str = "continuous") -> None:
        self._jogging[channel] = True
        self.__logger.debug(
            f"Mock stage {self._snr} ch{channel}: started jogging {direction} ({kind})"
        )

    def stop(self, channel: int) -> None:
        self._jogging[channel] = False
        self.__logger.debug(f"Mock stage {self._snr} ch{channel}: stopped")

    def home(self, channel: int, sync: bool = False, force: bool = True) -> None:
        self._position[channel] = 0.0
        self.__logger.debug(f"Mock stage {self._snr} ch{channel}: homed to 0.0 mm")

    def close(self) -> None:
        """No-op cleanup."""


__all__ = ["KinesisStage", "MockKinesisStage"]
