"""PositionerManager for Thorlabs MLS203 Kinesis XY motorized stages.

Extracted from the ImSwitch core (``KinesisStageManager``). Built on the real
``PositionerManager`` contract via :mod:`imswitch.pluginapi`; the vendor SDK
(``pylablib``) is a lazy ``[hardware]`` extra, and a bundled mock keeps the
plugin fully testable headless.
"""

from imswitch.pluginapi import PositionerManager

try:
    from imswitch.imcommon.model import initLogger
except Exception:  # pragma: no cover - only when run outside ImSwitch
    import logging

    def initLogger(owner, instanceName=None):
        name = type(owner).__name__
        if instanceName:
            name = f"{name}.{instanceName}"
        return logging.getLogger(name)

from ._kinesis_driver import KinesisStage, MockKinesisStage


class KinesisStageManager(PositionerManager):
    """PositionerManager for Thorlabs MLS203 two-axis motorized stages.

    Supports real hardware (via pylablib) and headless mock operation, with
    continuous jogging for interactive positioning.

    Manager properties:
        - ``snr`` (str, required): device serial number.
        - ``scale`` (str, default ``"MLS203"``): stage scale identifier.
        - ``isRackSystem`` (bool, default ``True``): whether the device is
          rack-mounted.
        - ``homeOnInit`` (bool, default ``False``): home both axes on init.
        - ``useMock`` (bool, default ``False``): force the mock driver, e.g. for
          headless testing, regardless of whether pylablib/hardware is present.
        - ``driverUnitsPerPositionUnit`` (float, default ``1.0``): conversion
          factor between ImSwitch position units and the raw values the pylablib
          driver reports/accepts.
    """

    def __init__(self, positionerInfo, name: str, **lowLevelManagers):
        super().__init__(
            positionerInfo,
            name,
            initialPosition={axis: 0.0 for axis in positionerInfo.axes},
        )
        self.__logger = initLogger(self, instanceName=name)

        if positionerInfo is None:
            return

        self._snr = positionerInfo.managerProperties["snr"]
        self._scale = positionerInfo.managerProperties.get("scale", "MLS203")
        self._is_rack_system = positionerInfo.managerProperties.get("isRackSystem", True)
        self._use_mock = bool(positionerInfo.managerProperties.get("useMock", False))
        home_on_init = positionerInfo.managerProperties.get("homeOnInit", False)
        self._driver_units_per_position_unit = self._read_driver_units_per_position_unit(
            positionerInfo.managerProperties
        )

        self._stage = self._getStageObj(self._snr, self._scale, self._is_rack_system)

        if home_on_init:
            self.__logger.info(f"Homing Kinesis stage {self._snr}")
            for axis in self.axes:
                channel = self._axis_to_channel(axis)
                self._stage.home(channel=channel)

        self._update_position()

    def move(self, dist: float, axis: str) -> None:
        """Move by a relative displacement in ImSwitch position units."""
        channel = self._axis_to_channel(axis)
        self._stage.move_by(dist * self._driver_units_per_position_unit, channel=channel)
        self._update_position()

    def setPosition(self, position: float, axis: str) -> None:
        """Move to an absolute position in ImSwitch position units."""
        channel = self._axis_to_channel(axis)
        self._stage.move_to(
            position * self._driver_units_per_position_unit, channel=channel
        )
        self._update_position()

    def jog_start(self, axis: str, sign: int) -> None:
        """Start continuous jogging: ``sign`` is +1 / -1 for direction."""
        channel = self._axis_to_channel(axis)
        direction = "+" if sign > 0 else "-"
        self._stage.jog(direction, channel=channel, kind="continuous")
        self.__logger.debug(f"Started jogging {axis} in direction {direction}")

    def jog_stop(self, axis: str) -> None:
        """Stop continuous jogging on the specified axis."""
        channel = self._axis_to_channel(axis)
        self._stage.stop(channel=channel)
        self._update_position()
        self.__logger.debug(f"Stopped jogging {axis}")

    def _axis_to_channel(self, axis: str) -> int:
        """Map axis name to hardware channel number (X=1, Y=2)."""
        if axis == "X":
            return 1
        if axis == "Y":
            return 2
        raise ValueError(f"Unknown axis: {axis}. Must be X or Y.")

    def _update_position(self) -> None:
        """Read current positions from the driver into internal state."""
        for axis in self.axes:
            channel = self._axis_to_channel(axis)
            raw = self._stage.get_position(channel=channel)
            self._position[axis] = raw / self._driver_units_per_position_unit

    def updatePosition(self) -> None:
        """Refresh cached widget/API positions in ImSwitch position units."""
        self._update_position()

    def _read_driver_units_per_position_unit(self, manager_properties: dict) -> float:
        """Return the configured raw-driver scaling factor."""
        value = manager_properties.get(
            "driverUnitsPerPositionUnit",
            manager_properties.get("unitsPerUm", 1.0),
        )
        scale = float(value)
        if scale == 0:
            raise ValueError("driverUnitsPerPositionUnit must be non-zero.")
        if "unitsPerUm" in manager_properties:
            self.__logger.warning(
                'KinesisStageManager manager property "unitsPerUm" is deprecated; '
                'use "driverUnitsPerPositionUnit" instead.'
            )
        return scale

    def _getStageObj(self, snr: str, scale: str, is_rack_system: bool):
        """Instantiate the stage driver.

        ``useMock`` forces the simulation. Otherwise the real driver is tried
        first and the mock is the automatic fallback when the SDK or hardware is
        unavailable, so a headless machine still boots.
        """
        if self._use_mock:
            self.__logger.info(f"useMock set; loading mock Kinesis stage {snr}")
            return MockKinesisStage(snr, scale=scale, is_rack_system=is_rack_system)
        try:
            stage = KinesisStage(snr, scale=scale, is_rack_system=is_rack_system)
            self.__logger.info(f"Initialized Thorlabs Kinesis stage {snr}")
            return stage
        except Exception as e:
            self.__logger.warning(
                f"Failed to initialize Kinesis stage {snr} (real hardware): {e}"
            )
            self.__logger.warning("Loading mock Kinesis stage for headless operation")
            return MockKinesisStage(snr, scale=scale, is_rack_system=is_rack_system)

    def finalize(self) -> None:
        """Close the stage connection."""
        self._stage.close()


__all__ = ["KinesisStageManager"]
