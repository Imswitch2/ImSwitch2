from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.devices.graph import (
    DeviceDescriptorSpec,
    DeviceDependencySpec,
    DeviceRelationKind,
    DeviceRole,
    HardwareDeviceId,
)
from imswitch.imcontrol.model.devices.status import (
    DeviceFailureKind,
    DeviceId,
    DeviceRuntimeMode,
)
from imswitch.imcontrol.model.interfaces.LeicaDMIHardware import createLeicaDMIHardware
from imswitch.imcontrol.model.managers.LeicaDMILifecycle import getLeicaDMILifecycle

from .PositionerManager import PositionerManager


class LeicaDMIZPositionerManager(PositionerManager):
    """Positioner adapter exposing Leica DMI objective Z in micrometers.

    The physical Leica connection is owned by ``LeicaDMIHardware`` and shared
    with the stand adapter when both reference the same RS232 manager.  This
    class only provides Positioner semantics and cached device status.
    """

    def __init__(self, positionerInfo, name, *args, **lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)
        self._positionerInfo = positionerInfo

        if len(positionerInfo.axes) != 1:
            raise RuntimeError(
                f"{self.__class__.__name__} only supports one axis, "
                f"{len(positionerInfo.axes)} provided."
            )

        axis = positionerInfo.axes[0]
        super().__init__(positionerInfo, name, initialPosition={axis: 0})

        self._axis = axis
        self._hardware = None
        self._connectionError = None
        self._rs232Manager = None
        self._deviceLifecycle = None

        managerProperties = positionerInfo.managerProperties or {}
        rs232DeviceName = managerProperties.get("rs232device")
        if not rs232DeviceName:
            self._connectionError = "Missing managerProperties.rs232device."
            self._setConnectionError(
                self._connectionError,
                summary="Leica DMI Z configuration is incomplete",
                failure_kind=DeviceFailureKind.CONFIGURATION_ERROR,
            )
            self.__logger.error(
                "Leica DMI Z positioner unavailable: missing "
                "managerProperties.rs232device."
            )
            return

        try:
            rs232Manager = lowLevelManagers["rs232sManager"][rs232DeviceName]
        except Exception as exc:
            self._connectionError = str(exc)
            self._setConnectionError(
                exc,
                summary="Leica DMI Z RS232 transport is unavailable",
            )
            self.__logger.error(
                "Leica DMI Z positioner unavailable: failed to access RS232 "
                f"device {rs232DeviceName!r}: {exc}"
            )
            return

        self._rs232Manager = rs232Manager
        self._deviceLifecycle = getLeicaDMILifecycle(rs232Manager, rs232DeviceName)
        self._deviceLifecycle.registerManager(self, managerProperties)

        transport_is_mock = (
            getattr(rs232Manager, "runtimeMode", None) is DeviceRuntimeMode.MOCK
        )
        self._hardware = createLeicaDMIHardware(
            rs232Manager,
            managerProperties=managerProperties,
            logger=self.__logger,
        )

        if self._hardware is None:
            if transport_is_mock:
                self._connectionError = getattr(
                    rs232Manager,
                    "connectionStatusDetails",
                    "Leica RS232 transport is using a mock backend",
                )
                self._setConnectionError(
                    self._connectionError,
                    summary="Leica DMI Z transport is mock",
                    mock_active=True,
                )
            else:
                self._connectionError = "Leica DMI hardware interface unavailable."
                self._setConnectionError(
                    self._connectionError,
                    summary="Leica DMI Z hardware interface is unavailable",
                )
            self.__logger.warning(
                "Leica DMI Z positioner unavailable. See Leica DMI hardware "
                "log entry above."
            )
            return

        if not self._hardware.has_z_position_um():
            self._connectionError = "Leica DMI Z micrometer conversion is unavailable."
            self._setConnectionError(
                self._connectionError,
                summary="Leica DMI Z calibration is unavailable",
                failure_kind=DeviceFailureKind.CONFIGURATION_ERROR,
            )
            self.__logger.warning(
                "Leica DMI Z positioner unavailable: no calibration LUT is "
                "configured and command 71042 did not return a valid Z "
                "conversion factor."
            )
            return

        if transport_is_mock:
            self._setConnectionError(
                "Leica RS232 transport is using a mock backend",
                summary="Leica DMI Z transport is mock",
                mock_active=True,
            )
        else:
            self._setConnected("Leica DMI Z connected")
        self.updatePosition()

    def getDeviceLifecycle(self):
        return self._deviceLifecycle

    def _leicaLifecycleDeviceId(self):
        return DeviceId("positioner", self.name)

    def _leicaLifecycleSortKey(self):
        return f"positioner:{self.name}"

    def _adoptLeicaHardware(self, hardware, *, error=None, mock_active=False):
        self._hardware = hardware
        if hardware is None:
            self._connectionError = str(error or "Leica DMI hardware unavailable")
            self._setConnectionError(
                self._connectionError,
                summary=(
                    "Leica DMI Z transport is mock"
                    if mock_active
                    else "Leica DMI Z hardware interface is unavailable"
                ),
                mock_active=mock_active,
            )
            return

        if not hardware.has_z_position_um():
            self._connectionError = "Leica DMI Z micrometer conversion is unavailable."
            self._setConnectionError(
                self._connectionError,
                summary="Leica DMI Z calibration is unavailable",
                failure_kind=DeviceFailureKind.CONFIGURATION_ERROR,
            )
            return

        self._connectionError = None
        self._setConnected("Leica DMI Z connected")
        try:
            self.updatePosition()
        except Exception as exc:
            self._connectionError = str(exc)
            self._setConnectionError(
                exc,
                summary="Leica DMI Z position refresh failed after reconnect",
            )

    def getDeviceDescriptorSpec(self):
        rs232_name = (self._positionerInfo.managerProperties or {}).get("rs232device")
        return DeviceDescriptorSpec(
            role=DeviceRole.COMPONENT,
            hardware_id=HardwareDeviceId("stand", f"leica:{rs232_name}"),
            display_name="Leica objective Z",
            category="stand",
            dependencies=(
                DeviceDependencySpec(
                    DeviceRelationKind.USES_TRANSPORT,
                    target=DeviceId("rs232", str(rs232_name)),
                    label=str(rs232_name),
                ),
            ),
        )

    @property
    def isAvailable(self) -> bool:
        # Availability is a configuration/capability property, not the current
        # transport state. Keeping a transiently disconnected stage available
        # lets PositionerController continue its guarded live-poll retries.
        return bool(
            self._hardware is not None
            and self._hardware.has_z_position_um()
        )

    @property
    def resetOnClose(self) -> bool:
        return False

    @property
    def connectionError(self):
        if self._hardware is not None:
            return getattr(self._hardware, "connectionError", None)
        return self._connectionError

    def move(self, dist, axis=None):
        self._check_axis(axis)
        if not self.isAvailable:
            return self._position
        return self._call_hardware("move_z_relative_um", dist)

    def setPosition(self, position, axis=None):
        self._check_axis(axis)
        if not self.isAvailable:
            return self._position
        return self._call_hardware("set_z_position_um", position)

    def updatePosition(self):
        if not self.isAvailable:
            return self._position

        position = self._call_hardware("get_z_position_um", updatePosition=False)
        if position is not None:
            self.updateTrackedPosition({self._axis: position})
        return self._position

    def get_abs(self, axis=None):
        self._check_axis(axis)
        self.updatePosition()
        return self._position[self._axis]

    def get_pos_nm(self):
        if not self.isAvailable:
            return None
        return self._call_hardware("get_pos_nm", updatePosition=False)

    def set_pos_nm(self, pos_nm):
        if not self.isAvailable:
            return self._position

        result = self._call_hardware("set_pos_nm", pos_nm, updatePosition=False)
        self.updatePosition()
        return result

    def _call_hardware(self, methodName, *args, updatePosition=True):
        method = getattr(self._hardware, methodName)
        try:
            result = method(*args)
        except Exception as exc:
            self._connectionError = str(exc)
            self._setConnectionError(
                exc,
                summary="Leica DMI Z communication failed",
            )
            self.__logger.warning(
                f"Leica DMI Z positioner command {methodName} failed: {exc}"
            )
            raise

        self._connectionError = None
        if self.runtimeMode is not DeviceRuntimeMode.MOCK:
            self._setConnected("Leica DMI Z connected")
        if updatePosition:
            if result is not None:
                self.updateTrackedPosition({self._axis: result})
            return self._position

        return result

    def _check_axis(self, axis):
        if axis in (None, self._axis, 0):
            return

        raise ValueError(
            f"{self.__class__.__name__} only controls axis {self._axis}, "
            f"got {axis}."
        )
