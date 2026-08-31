import threading

from imswitch.imcommon.framework import Signal, SignalInterface
from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.devices.graph import HardwareDeviceId
from imswitch.imcontrol.model.devices.lifecycle import (
    DeviceLifecycleAction,
    DeviceLifecycleCapabilities,
    DeviceLifecycleNotSupportedError,
    DeviceLifecycleResult,
)
from imswitch.imcontrol.model.devices.status import (
    DeviceFailureKind,
    DeviceId,
    DeviceRuntimeMode,
)
from imswitch.imcontrol.model.interfaces.pipython.pidevice import (
    GCSDevice,
    GCSError,
    gcserror,
)
from imswitch.imcontrol.model.interfaces.pipython.pidevice.gcs2 import gcs2pitools
from qtpy import QtCore

QTimer = QtCore.QTimer

from .PositionerManager import PositionerManager


_PI_COMMUNICATION_ERROR_CODES = frozenset(
    code
    for code in (
        getattr(gcserror, "E_1_COM_ERROR", None),
        getattr(gcserror, "E_2_SEND_ERROR", None),
        getattr(gcserror, "E_3_REC_ERROR", None),
        getattr(gcserror, "E_4_NOT_CONNECTED_ERROR", None),
        getattr(gcserror, "E_5_COM_BUFFER_OVERFLOW", None),
        getattr(gcserror, "E_6_CONNECTION_FAILED", None),
        getattr(gcserror, "E_7_COM_TIMEOUT", None),
        getattr(gcserror, "E_34_COM_FTDIUSB_INVALID_HANDLE", None),
        getattr(gcserror, "E_35_COM_FTDIUSB_DEVICE_NOT_FOUND", None),
        getattr(gcserror, "E_36_COM_FTDIUSB_DEVICE_NOT_OPENED", None),
        getattr(gcserror, "E_37_COM_FTDIUSB_IO_ERROR", None),
        getattr(gcserror, "E_43_COM_FTDIUSB_FAILED_TO_WRITE_DEVICE", None),
        getattr(gcserror, "E_54_COM_SOCKET_NOT_READY", None),
        getattr(gcserror, "E_56_COM_SOCKET_NOT_CONNECTED", None),
        getattr(gcserror, "E_57_COM_SOCKET_TERMINATED", None),
        getattr(gcserror, "E_58_COM_SOCKET_NO_RESPONSE", None),
        getattr(gcserror, "E_59_COM_SOCKET_INTERRUPTED", None),
        getattr(gcserror, "E_62_COM_SOCKET_HOST_NOT_FOUND", None),
    )
    if code is not None
)


def _is_pi_communication_error(exc: Exception) -> bool:
    """Return whether ``exc`` means the PI transport itself is unusable.

    PIPython uses ``GCSError`` both for communication failures and ordinary
    controller/command errors. Only the narrow COM/USB/socket error subset is
    allowed to trigger automatic real -> mock fallback.
    """
    if isinstance(exc, (OSError, TimeoutError, ConnectionError)):
        return True
    return isinstance(exc, GCSError) and getattr(exc, "val", None) in _PI_COMMUNICATION_ERROR_CODES


class _PIStageDeviceNotFoundError(RuntimeError):
    pass


class _PIStageMockBackend:
    is_mock = True

    def __init__(self, position_um=None, *, range_min=0.0, range_max=25.0):
        position_um = position_um or {"X": 0.0, "Y": 0.0}
        self._position = {
            "X": float(position_um.get("X", 0.0)),
            "Y": float(position_um.get("Y", 0.0)),
        }
        self.range_min = float(range_min)
        self.range_max = float(range_max)
        self.joystick_enabled = False

    @property
    def X(self):
        return None

    @property
    def Y(self):
        return None

    def get_position_um(self):
        return dict(self._position)

    def set_position_mm(self, axis, position):
        self._position[axis] = float(position) * 1000

    def is_movement_finished(self, axis):
        return True

    def set_joystick_enabled(self, enabled):
        self.joystick_enabled = bool(enabled)

    def get_joystick_enabled(self):
        return self.joystick_enabled

    def get_button_state(self, which):
        return False

    def set_speed(self, speed):
        return None

    def close(self):
        return None


class _PIStageRealBackend:
    is_mock = False

    def __init__(self, x_device, y_device, *, range_min, range_max, position_um):
        self.X = x_device
        self.Y = y_device
        self.range_min = float(range_min)
        self.range_max = float(range_max)
        self._initial_position_um = dict(position_um)

    def _controller(self, axis):
        if axis == "X":
            return self.X
        if axis == "Y":
            return self.Y
        raise ValueError(f"Unknown PI stage axis {axis!r}")

    def get_position_um(self):
        # Read both before returning either so callers can commit XY atomically.
        x = self.X.qPOS(1)[1] * 1000
        y = self.Y.qPOS(1)[1] * 1000
        return {"X": x, "Y": y}

    def set_position_mm(self, axis, position):
        self._controller(axis).MOV(1, position)

    def is_movement_finished(self, axis):
        controller = self._controller(axis)
        if controller.HasqONT():
            return all(bool(value) for value in controller.qONT(1).values())
        if controller.HasIsMoving():
            return not any(bool(value) for value in controller.IsMoving(1).values())
        return None

    def set_joystick_enabled(self, enabled):
        enabled = bool(enabled)
        self.X.JON(1, enabled)
        self.Y.JON(1, enabled)

    def get_joystick_enabled(self):
        return bool(self.X.qJON()[1])

    def get_button_state(self, which):
        result = self._controller(which).qJBS(1, 1)
        return bool(result[1][1])

    def set_speed(self, speed):
        self.X.VEL(1, speed)
        self.Y.VEL(1, speed)

    def close(self):
        # CloseDaisyChain is the canonical teardown for this shared USB chain.
        try:
            self.X.CloseDaisyChain()
        finally:
            for controller in (self.Y, self.X):
                close = getattr(controller, "CloseConnection", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass


class _PIStageLifecycle:
    capabilities = DeviceLifecycleCapabilities(reconnect=True)

    def __init__(self, manager):
        self._manager = manager
        self._hardware_id = HardwareDeviceId(
            category="positioner", key=f"positioner:{manager.name}"
        )

    @property
    def hardware_id(self):
        return self._hardware_id

    def _unsupported(self, action):
        raise DeviceLifecycleNotSupportedError(
            f"PI stage lifecycle does not yet support {action.value}."
        )

    def connect(self):
        return self._unsupported(DeviceLifecycleAction.CONNECT)

    def disconnect(self):
        return self._unsupported(DeviceLifecycleAction.DISCONNECT)

    def probe(self):
        return self._unsupported(DeviceLifecycleAction.PROBE)

    def shutdown(self):
        return self._unsupported(DeviceLifecycleAction.SHUTDOWN)

    def reconnect(self):
        device_id = DeviceId("positioner", self._manager.name)
        retired_backends = self._manager._prepareBackendReconnect()
        for backend in retired_backends:
            self._manager._closeBackendBestEffort(backend)

        try:
            backend, usb_description = self._manager._buildRealBackend()
        except Exception as exc:
            self._manager._markReconnectFailure(exc)
            return DeviceLifecycleResult(
                hardware_id=self.hardware_id,
                action=DeviceLifecycleAction.RECONNECT,
                success=False,
                summary="PI stage reconnect failed; mock fallback active",
                details=str(exc),
                affected_device_ids=(device_id,),
            )

        self._manager._installRealBackend(
            backend,
            usb_description,
            summary="PI stage reconnected",
        )
        return DeviceLifecycleResult(
            hardware_id=self.hardware_id,
            action=DeviceLifecycleAction.RECONNECT,
            success=True,
            summary="PI stage reconnected",
            affected_device_ids=(device_id,),
        )


class PIStageManager(PositionerManager, SignalInterface):
    """PositionerManager for a PI C-663 XY stage connected through USB.

    The manager is a stable logical positioner. Its runtime hardware backend is
    replaceable so startup failures and lost USB connections can fall back to a
    stateful mock without removing the configured positioner from the UI.
    """

    sigJoystickStatusChanged = Signal(bool)

    def __init__(self, positionerInfo, name, *args, **lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)

        if (
            len(positionerInfo.axes) != 2
            or "X" not in positionerInfo.axes
            or "Y" not in positionerInfo.axes
        ):
            raise RuntimeError(
                f"{self.__class__.__name__} requires two axes named X and Y"
                f" respectively, {positionerInfo.axes} provided."
            )

        PositionerManager.__init__(
            self,
            positionerInfo,
            name,
            initialPosition={axis: 0 for axis in positionerInfo.axes},
        )
        SignalInterface.__init__(self)

        manager_properties = positionerInfo.managerProperties or {}
        self.runtimeTimeoutMs = self._parseRuntimeTimeoutMs(
            manager_properties.get("runtime_timeout_ms", 500)
        )
        self._backendLock = threading.RLock()
        # Compatibility alias for code/tests that used the old lock name.
        self._piLock = self._backendLock

        self._deviceModel = manager_properties.get("device")
        if not self._deviceModel:
            raise ValueError(
                "PIStageManager requires 'device' in managerProperties "
                "(for example 'C-663.11')."
            )
        # Keep the configured model stable. Historically ``device`` was set to
        # None on failure, conflating configuration with runtime availability.
        self.device = self._deviceModel
        self._configuredUsbDescription = manager_properties.get("usb_description")
        self.usb_description = self._configuredUsbDescription

        self.rangeMin = 0.0
        self.rangeMax = 25.0  # mm
        self.joystickStatus = False

        self.fastSpeed = 1.0
        self.slowSpeed = 0.2
        self.buttonPollIntervalMs = 200
        self._buttonPollFailureCount = 0
        self._buttonPollBackoffMs = (1000, 2000, 5000, 10000)
        self.speedButtonPressed = None
        self.speedButtonController = "Y"
        self.enableButtonController = "X"
        self.enableButtonPressed = False
        self.buttonTimer = QTimer()
        if hasattr(self.buttonTimer, "setInterval"):
            self.buttonTimer.setInterval(self.buttonPollIntervalMs)
        self.buttonTimer.timeout.connect(self._pollButtons)

        self._backend = _PIStageMockBackend(self._position)
        self._retiredBackends = []
        self._lifecycle = _PIStageLifecycle(self)

        try:
            backend, usb_description = self._buildRealBackend()
        except Exception as exc:
            self.__logger.warning(
                f"Could not initialize PI motorized stage; mock fallback active: {exc}"
            )
            self._activateMockFallback(
                exc,
                summary="PI stage initialization failed; mock fallback active",
            )
        else:
            self._installRealBackend(
                backend,
                usb_description,
                summary="PI stage connected",
            )

    @staticmethod
    def _parseRuntimeTimeoutMs(value):
        try:
            timeout = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "PIStageManager managerProperties.runtime_timeout_ms must be a positive integer."
            ) from exc
        if timeout <= 0:
            raise ValueError(
                "PIStageManager managerProperties.runtime_timeout_ms must be a positive integer."
            )
        return timeout

    def _resolve_usb_description(self, manager_properties=None):
        configured_usb_description = self._configuredUsbDescription
        if manager_properties is not None:
            configured_usb_description = manager_properties.get(
                "usb_description", configured_usb_description
            )

        if configured_usb_description:
            self.__logger.debug(
                f"Using PI USB description from setup config: {configured_usb_description}"
            )
            return configured_usb_description

        finder = GCSDevice(self._deviceModel)
        try:
            usb_devices = finder.EnumerateUSB()
        finally:
            try:
                finder.CloseConnection()
            except Exception as exc:
                self.__logger.debug(f"Failed to close PI finder connection: {exc}")
            try:
                finder.CloseDaisyChain()
            except Exception as exc:
                self.__logger.debug(f"Failed to close PI finder daisy chain: {exc}")

        if not usb_devices:
            raise _PIStageDeviceNotFoundError(
                f"No PI USB devices found while searching for {self._deviceModel!r}"
            )

        device_candidates = [self._deviceModel]
        if "." in self._deviceModel:
            device_candidates.append(self._deviceModel.split(".", 1)[0])

        selected_device = next(
            (
                dev
                for dev in usb_devices
                if any(candidate in dev for candidate in device_candidates)
            ),
            None,
        )
        if selected_device is None:
            raise _PIStageDeviceNotFoundError(
                f"No enumerated PI USB device matched {self._deviceModel!r}; "
                f"found {usb_devices}"
            )
        self.__logger.debug(f"Auto-selected PI USB device: {selected_device}")
        return selected_device

    def _buildRealBackend(self):
        """Build and fully validate a candidate real backend off to the side."""
        usb_description = self._resolve_usb_description(
            {"usb_description": self._configuredUsbDescription}
        )
        if usb_description is None:
            raise _PIStageDeviceNotFoundError("No PI USB device selected")

        x_device = GCSDevice(self._deviceModel)
        y_device = GCSDevice(self._deviceModel)
        try:
            x_device.OpenUSBDaisyChain(description=usb_description)
            daisychainid = x_device.dcid
            x_device.ConnectDaisyChainDevice(1, daisychainid)
            y_device.ConnectDaisyChainDevice(2, daisychainid)
            gcs2pitools.startup(x_device)
            gcs2pitools.startup(y_device)

            # Keep startup on PIPython's conservative timeout, then fail fast
            # during normal runtime if a previously-connected stage disappears.
            x_device.timeout = self.runtimeTimeoutMs
            y_device.timeout = self.runtimeTimeoutMs

            range_min = x_device.qTMN()["1"]
            range_max = x_device.qTMX()["1"]
            position_um = {
                "X": x_device.qPOS(1)[1] * 1000,
                "Y": y_device.qPOS(1)[1] * 1000,
            }

            self.__logger.debug(
                "\n{}:\n{}".format(
                    x_device.GetInterfaceDescription(), x_device.qIDN()
                )
            )
            self.__logger.debug(
                "\n{}:\n{}".format(
                    y_device.GetInterfaceDescription(), y_device.qIDN()
                )
            )
            return (
                _PIStageRealBackend(
                    x_device,
                    y_device,
                    range_min=range_min,
                    range_max=range_max,
                    position_um=position_um,
                ),
                usb_description,
            )
        except Exception:
            candidate = _PIStageRealBackend(
                x_device,
                y_device,
                range_min=self.rangeMin,
                range_max=self.rangeMax,
                position_um=self._position,
            )
            self._closeBackendBestEffort(candidate)
            raise

    @property
    def X(self):
        with self._backendLock:
            return self._backend.X

    @property
    def Y(self):
        with self._backendLock:
            return self._backend.Y

    @property
    def isAvailable(self) -> bool:
        # The configured manager remains usable through its mock backend.
        return True

    @property
    def isMock(self) -> bool:
        with self._backendLock:
            return bool(self._backend.is_mock)

    def getDeviceLifecycle(self):
        return self._lifecycle

    def _mockFromCurrentState(self):
        return _PIStageMockBackend(
            self._position,
            range_min=self.rangeMin,
            range_max=self.rangeMax,
        )

    def _setJoystickStatusCached(self, enabled):
        enabled = bool(enabled)
        changed = enabled != self.joystickStatus
        self.joystickStatus = enabled
        if changed:
            self.sigJoystickStatusChanged.emit(enabled)

    def _installRealBackend(self, backend, usb_description, *, summary):
        try:
            joystick_enabled = backend.get_joystick_enabled()
        except Exception as exc:
            if _is_pi_communication_error(exc):
                self._closeBackendBestEffort(backend)
                raise
            self.__logger.warning(f"Failed to read PI joystick state: {exc}")
            joystick_enabled = False

        # Joystick button/speed support is optional; failure here must not make
        # the physical stage connection itself unavailable.
        button_poll_available = True
        try:
            backend.set_speed(self.slowSpeed)
        except Exception as exc:
            if _is_pi_communication_error(exc):
                self._closeBackendBestEffort(backend)
                raise
            button_poll_available = False
            self.__logger.warning(
                f"Failed to initialize PI joystick speed support: {exc}"
            )

        old_backend = None
        with self._backendLock:
            old_backend = self._backend
            self._backend = backend
            self.usb_description = usb_description
            self.rangeMin = backend.range_min
            self.rangeMax = backend.range_max
            self._position.update(backend._initial_position_um)
            self._buttonPollFailureCount = 0
            self.speedButtonPressed = None
            self.enableButtonPressed = False
            self._setJoystickStatusCached(joystick_enabled)
            self._setConnected(summary)

        if old_backend is not backend:
            self._closeBackendBestEffort(old_backend)

        if button_poll_available:
            self._startButtonPolling()
        else:
            self._stopButtonPolling()

    def _activateMockFallback(self, exc, *, summary):
        with self._backendLock:
            old_backend = self._backend
            if not old_backend.is_mock:
                self._backend = self._mockFromCurrentState()
            self._stopButtonPolling()
            self._setJoystickStatusCached(False)
            failure_kind = (
                DeviceFailureKind.DEVICE_NOT_FOUND
                if isinstance(exc, _PIStageDeviceNotFoundError)
                else DeviceFailureKind.CONNECTION_ERROR
            )
            self._setConnectionError(
                exc,
                summary=summary,
                failure_kind=failure_kind,
                mock_active=True,
            )
        if not old_backend.is_mock:
            self._closeBackendBestEffort(old_backend)

    def _runBackend(self, operation):
        with self._backendLock:
            backend = self._backend
            try:
                return operation(backend)
            except Exception as exc:
                if backend.is_mock or not _is_pi_communication_error(exc):
                    raise
                # Install the mock while still holding the same lock that
                # serializes normal PI calls. No caller can observe a
                # half-replaced backend. Do not close the broken vendor object
                # here: this code often runs on the GUI/live-poll thread, and
                # vendor teardown must not introduce another blocking path.
                self._backend = self._mockFromCurrentState()
                self._retiredBackends.append(backend)
                self._stopButtonPolling()
                self._setJoystickStatusCached(False)
                self._setConnectionError(
                    exc,
                    summary="PI communication lost; mock fallback active",
                    mock_active=True,
                )
                raise

    def _prepareBackendReconnect(self):
        """Expose a mock immediately and detach real backends for worker teardown."""
        with self._backendLock:
            old_backend = self._backend
            self._backend = self._mockFromCurrentState()
            retired = list(self._retiredBackends)
            self._retiredBackends.clear()
            if not old_backend.is_mock and old_backend not in retired:
                retired.append(old_backend)
            self._stopButtonPolling()
            self._setJoystickStatusCached(False)
            self._setMockActive("PI stage reconnecting on mock backend")
            return tuple(retired)

    def _markReconnectFailure(self, exc):
        with self._backendLock:
            self._stopButtonPolling()
            self._setJoystickStatusCached(False)
            failure_kind = (
                DeviceFailureKind.DEVICE_NOT_FOUND
                if isinstance(exc, _PIStageDeviceNotFoundError)
                else DeviceFailureKind.CONNECTION_ERROR
            )
            self._setConnectionError(
                exc,
                summary="PI stage reconnect failed; mock fallback active",
                failure_kind=failure_kind,
                mock_active=True,
            )

    def _closeBackendBestEffort(self, backend):
        if backend is None or backend.is_mock:
            return
        try:
            backend.close()
        except Exception as exc:
            self.__logger.debug(f"Failed to close PI backend: {exc}")

    def connect(self):
        """Synchronously connect and install a fresh real backend.

        Runtime UI reconnects should go through ``DeviceLifecycleService``;
        this method is retained as the manager-level primitive/compatibility API.
        """
        retired_backends = self._prepareBackendReconnect()
        for backend in retired_backends:
            self._closeBackendBestEffort(backend)
        try:
            backend, usb_description = self._buildRealBackend()
            self._installRealBackend(
                backend, usb_description, summary="PI stage connected"
            )
        except Exception as exc:
            self._markReconnectFailure(exc)
            raise

    def finalize(self) -> None:
        self._stopButtonPolling()
        with self._backendLock:
            backend = self._backend
            self._backend = self._mockFromCurrentState()
            retired = list(self._retiredBackends)
            self._retiredBackends.clear()
            if not backend.is_mock and backend not in retired:
                retired.append(backend)
            self._setJoystickStatusCached(False)

        for retired_backend in retired:
            if retired_backend is backend:
                # Preserve the historical shutdown behavior as best effort:
                # leave the currently-live physical joystick enabled and fast.
                try:
                    retired_backend.set_speed(self.fastSpeed)
                    retired_backend.set_joystick_enabled(True)
                except Exception as exc:
                    self.__logger.debug(
                        f"Failed to restore PI joystick on shutdown: {exc}"
                    )
            self._closeBackendBestEffort(retired_backend)
        self._setFinalizedStatus()

    def move(self, value, axis):
        def operation(backend):
            dist = self._position[axis] / 1000 + value / 1000
            if self.rangeMax >= dist >= self.rangeMin:
                backend.set_joystick_enabled(False)
                self._setJoystickStatusCached(False)
                backend.set_position_mm(axis, dist)
                self._position[axis] = dist * 1000
            else:
                self.__logger.debug("Out of the stage range")
            return dict(self._position)

        return self._runBackend(operation)

    def setPosition(self, position: float, axis: str):
        def operation(backend):
            if self.rangeMax >= position >= self.rangeMin:
                backend.set_joystick_enabled(False)
                self._setJoystickStatusCached(False)
                backend.set_position_mm(axis, position)
                self._position[axis] = position * 1000
            else:
                self.__logger.debug("Out of the stage range")
            return dict(self._position)

        return self._runBackend(operation)

    def updatePosition(self):
        def operation(backend):
            positions = backend.get_position_um()
            self._position.update(positions)
            return dict(self._position)

        return self._runBackend(operation)

    def isMovementFinished(self, axis=None):
        axes = self.axes if axis in (None, "all") else [axis]

        def operation(backend):
            states = []
            for axis_name in axes:
                state = backend.is_movement_finished(axis_name)
                if state is None:
                    return None
                states.append(state)
            return all(states) if states else None

        return self._runBackend(operation)

    def setJoystickEnabled(self, enabled: bool):
        def operation(backend):
            backend.set_joystick_enabled(enabled)
            self._setJoystickStatusCached(enabled)

        return self._runBackend(operation)

    def activate_joystick(self):
        if not self.joystickStatus:
            self.setJoystickEnabled(True)
            self.__logger.debug("Joystick activated")

    def deactivate_joystick(self):
        if self.joystickStatus:
            self.setJoystickEnabled(False)
            self.__logger.debug("Joystick deactivated")

    def getJoystickEnabledStatus(self):
        def operation(backend):
            enabled = backend.get_joystick_enabled()
            self._setJoystickStatusCached(enabled)
            return enabled

        return self._runBackend(operation)

    def _getJoystickButtonState(self, which="X"):
        def operation(backend):
            if backend.is_mock:
                return False
            try:
                return backend.get_button_state(which)
            except (KeyError, TypeError, IndexError) as exc:
                self.__logger.debug(f"Unexpected qJBS return format: {exc}")
                return False

        return self._runBackend(operation)

    def _setJoystickSpeed(self, speed):
        return self._runBackend(lambda backend: backend.set_speed(speed))

    def _timerMethod(self, method_name, interval=None):
        """Start/stop the manager-owned QTimer on its Qt affinity thread.

        Device lifecycle reconnects run in ``HardwareStatusController``'s
        worker thread. Directly starting/stopping a QTimer from there would
        produce Qt timer-affinity warnings and can leave polling disabled.
        """
        timer = self.buttonTimer
        method = getattr(timer, method_name)
        try:
            owner_thread = timer.thread()
            current_thread = QtCore.QThread.currentThread()
        except Exception:
            if interval is None:
                method()
            else:
                method(interval)
            return

        if owner_thread is current_thread:
            if interval is None:
                method()
            else:
                method(interval)
            return

        queued = getattr(QtCore.Qt, "QueuedConnection", None)
        if queued is None:
            queued = QtCore.Qt.ConnectionType.QueuedConnection
        try:
            if interval is None:
                QtCore.QMetaObject.invokeMethod(timer, method_name, queued)
            else:
                QtCore.QMetaObject.invokeMethod(
                    timer, method_name, queued, QtCore.Q_ARG(int, int(interval))
                )
        except Exception as exc:
            # Never call the timer directly from the wrong thread as a
            # fallback; leave it stopped and report the scheduling failure.
            self.__logger.warning(
                f"Could not schedule PI button timer {method_name}: {exc}"
            )

    def _startButtonPolling(self, interval=None):
        interval = self.buttonPollIntervalMs if interval is None else int(interval)
        self._timerMethod("start", interval)

    def _stopButtonPolling(self):
        self._timerMethod("stop")

    def _pollButtons(self):
        with self._backendLock:
            if self._backend.is_mock:
                self._stopButtonPolling()
                return

        try:
            speed_pressed = self._getJoystickButtonState(self.speedButtonController)
            if speed_pressed != self.speedButtonPressed:
                self._setJoystickSpeed(self.fastSpeed if speed_pressed else self.slowSpeed)
                self.__logger.debug(
                    "Joystick speed set to FAST" if speed_pressed else "Joystick speed set to SLOW"
                )
                self.speedButtonPressed = speed_pressed

            enable_pressed = self._getJoystickButtonState(self.enableButtonController)
            if enable_pressed and not self.enableButtonPressed:
                self.setJoystickEnabled(not self.joystickStatus)
            self.enableButtonPressed = enable_pressed

        except Exception as exc:
            # A communication failure already transitioned the manager to mock.
            # Do not re-arm physical button polling against the mock backend.
            if self.runtimeMode is DeviceRuntimeMode.MOCK:
                self._stopButtonPolling()
                return

            self._buttonPollFailureCount += 1
            delay_ms = self._buttonPollBackoffMs[
                min(self._buttonPollFailureCount - 1, len(self._buttonPollBackoffMs) - 1)
            ]
            if (
                self._buttonPollFailureCount <= len(self._buttonPollBackoffMs)
                or self._buttonPollFailureCount % 10 == 0
            ):
                self.__logger.warning(
                    f"PI joystick button polling failed; retrying in {delay_ms / 1000:g} s "
                    f"(failure {self._buttonPollFailureCount}): {exc}"
                )
            else:
                self.__logger.debug(
                    f"PI joystick button polling still failing "
                    f"(failure {self._buttonPollFailureCount}): {exc}"
                )
            self._startButtonPolling(delay_ms)
            return

        if self._buttonPollFailureCount:
            self.__logger.info(
                f"PI joystick button polling recovered after "
                f"{self._buttonPollFailureCount} failure(s)."
            )
            self._buttonPollFailureCount = 0
            self._startButtonPolling(self.buttonPollIntervalMs)
