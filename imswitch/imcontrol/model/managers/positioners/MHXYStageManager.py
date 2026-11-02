import math
import threading
from contextlib import contextmanager

from imswitch.imcommon.model import initLogger
from .PositionerManager import PositionerManager
from imswitch.imcontrol.model.devices.graph import HardwareDeviceId, rs232BackedPrimarySpec
from imswitch.imcontrol.model.devices.lifecycle import (
    DeviceLifecycleAction, DeviceLifecycleCapabilities, DeviceLifecycleResult,
    DeviceLifecycleBusyError, DeviceLifecycleNotSupportedError,
)
from imswitch.imcontrol.model.devices.status import DeviceId, DeviceRuntimeMode


class _MHXYLifecycle:
    capabilities = DeviceLifecycleCapabilities(reconnect=True)

    def __init__(self, manager):
        self._manager = manager
        self.hardware_id = HardwareDeviceId('positioner', f'marzhauser:{manager.name}')

    def _unsupported(self):
        raise DeviceLifecycleNotSupportedError('Marzhauser supports reconnect only.')

    connect = disconnect = probe = shutdown = _unsupported

    def reconnect(self):
        """Direct use; the service reopens the shared port itself and calls
        the manager's transport hooks (every device on the port with it)."""
        manager = self._manager
        with manager._operation(reconnect=True):
            manager.positionSynced = False
            real = False
            try:
                real = bool(manager._rs232Manager.reconnectTransport())
            except Exception:
                real = False
            errors = manager._onTransportReconnected(real, _locked=True)
            if errors:
                return DeviceLifecycleResult(
                    hardware_id=self.hardware_id, action=DeviceLifecycleAction.RECONNECT,
                    success=False, summary='Marzhauser reconnect failed; motion disabled',
                    details='; '.join(errors), affected_device_ids=(DeviceId('positioner', manager.name),),
                )
            return DeviceLifecycleResult(
                hardware_id=self.hardware_id, action=DeviceLifecycleAction.RECONNECT,
                success=True, summary='Marzhauser reconnected; XY position synchronized',
                affected_device_ids=(DeviceId('positioner', manager.name),),
            )


class MHXYStageManager(PositionerManager):
    """ PositionerManager for control of a Marzhauser XY-stage through RS232
    communication.

    The manager's tracked position is kept in the *controller's* absolute
    coordinate frame, seeded from the hardware at startup via ``?pos``. This
    matters because ``move()`` issues a relative ``mor`` while
    ``setPosition()`` issues an absolute ``moa``: if the tracked frame were
    seeded at zero (as it used to be) those two would disagree by however far
    the stage happened to be from hardware zero when ImSwitch started, and any
    absolute move would fly off to the wrong part of the travel range.

    Manager properties:

    - ``rs232device`` -- name of the defined rs232 communication channel
      through which the communication should take place
    """

    def __init__(self, positionerInfo, name, *args, **lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)
        self._operationLock = threading.RLock()
        #: Thread running a reconnect; other threads' operations are refused
        #: while it runs (and it waits for one already in flight).
        self._reconnectThread = None

        if (len(positionerInfo.axes) != 2
                or 'X' not in positionerInfo.axes or 'Y' not in positionerInfo.axes):
            raise RuntimeError(f'{self.__class__.__name__} requires two axes named X and Y'
                               f' respectively, {positionerInfo.axes} provided.')

        # The RS232 channel must exist before the initial position query below.
        self._usingMockFallback = False
        try:
            self._rs232Manager = lowLevelManagers['rs232sManager'][
                positionerInfo.managerProperties['rs232device']
            ]
        except Exception as e:
            self.__logger.warning(
                f'Failed to initialize rs232sManager, falling back to mock mode: {e}'
            )
            from imswitch.imcontrol.model.interfaces.RS232Driver_mock import MockRS232Driver
            self._rs232Manager = MockRS232Driver(name='mock', settings={'port': 'Mock'})
            self._usingMockFallback = True
            self._setConnectionError(
                e,
                summary="Marzhauser RS232 backend unavailable; mock fallback active",
                mock_active=True,
            )

        super().__init__(positionerInfo, name, initialPosition=self._readHardwarePosition(
            list(positionerInfo.axes)
        ))
        self._lifecycle = _MHXYLifecycle(self)

        try:
            self.__logger.info(f"MHXYStage serial no: {self._rs232Manager.query('?readsn')}")
        except Exception as e:
            self.__logger.warning(f"Failed to read stage serial number: {e}")

    def _readHardwarePosition(self, axes):
        """ Return ``{axis: position}`` read from the controller via ``?pos``.

        Falls back to zeros for any axis that cannot be read, so a mock port or
        an unresponsive controller degrades to the previous behaviour instead
        of preventing startup. Callers that rely on absolute moves should check
        :attr:`positionSynced` to know whether the frame is trustworthy.
        """
        fallback = {axis: 0.0 for axis in axes}
        try:
            if (self._usingMockFallback or getattr(self._rs232Manager, 'runtimeMode', None)
                    is DeviceRuntimeMode.MOCK):
                raise RuntimeError('Stage serial transport is using a mock backend.')
            reply = self._rs232Manager.query('?pos')
        except Exception as e:
            self.__logger.warning(
                f'Could not read stage position (?pos): {e}. Tracked position '
                f'starts at zero, so absolute moves will be offset from the '
                f'controller frame until a successful sync.'
            )
            self.positionSynced = False
            self._setConnectionError(e, summary='Marzhauser position unavailable')
            return fallback

        parsed = self._parsePositionReply(reply, axes)
        if parsed is None:
            self.__logger.warning(
                f'Could not parse stage position reply {reply!r}. Tracked '
                f'position starts at zero, so absolute moves will be offset '
                f'from the controller frame until a successful sync.'
            )
            self.positionSynced = False
            self._setConnectionError('Invalid XY position reply', summary='Marzhauser position unavailable')
            return fallback

        self.positionSynced = True
        if not self._usingMockFallback:
            self._setConnected("Marzhauser stage responding")
        self.__logger.info(
            f'MHXYStage position synced from hardware: '
            f'{ {axis: round(value, 3) for axis, value in parsed.items()} }'
        )
        return parsed

    @staticmethod
    def _parsePositionReply(reply, axes):
        """ Parse a Tango ``?pos`` reply into ``{axis: float}``, or None.

        The controller answers with one whitespace- (or comma-) separated value
        per configured axis, in axis order. Replies with fewer values than we
        have axes are rejected rather than partially applied.
        """
        if reply is None:
            return None

        tokens = str(reply).replace(',', ' ').split()
        values = []
        for token in tokens:
            try:
                values.append(float(token))
            except ValueError:
                continue

        if len(values) < len(axes):
            return None

        if not all(math.isfinite(value) for value in values):
            return None
        return {axis: values[index] for index, axis in enumerate(axes)}

    def syncPositionFromHardware(self):
        """ Re-read the controller position into the tracked position.

        Use before any absolute move that must land accurately after the stage
        may have been moved outside ImSwitch — most notably by the joystick,
        which the manager cannot observe.

        Returns True when the tracked position now reflects hardware.
        """
        with self._operation():
            positions = self._readHardwarePosition(list(self.axes))
            if not self.positionSynced:
                return False
            self.updateTrackedPosition(positions)
            return True

    #: How long an ordinary operation (or a reconnect) waits for one in flight.
    OPERATION_WAIT_S = 30.0

    @contextmanager
    def _operation(self, *, reconnect=False):
        """Serialise stage operations; a reconnect excludes all others.

        Ordinary operations (moves, position syncs) wait for each other -- two
        callers overlapping is normal, not an error. While a reconnect runs,
        operations from other threads are refused at once; the reconnect
        itself first waits for an operation already in flight.
        """
        me = threading.get_ident()
        reconnecting = self._reconnectThread
        if reconnecting is not None and reconnecting != me:
            raise DeviceLifecycleBusyError('Marzhauser stage is being reconnected.')
        if reconnect:
            self._reconnectThread = me
        try:
            if not self._operationLock.acquire(timeout=self.OPERATION_WAIT_S):
                raise DeviceLifecycleBusyError(
                    f'Marzhauser stage: an earlier operation did not finish within '
                    f'{self.OPERATION_WAIT_S:g} s.')
            try:
                yield
            finally:
                self._operationLock.release()
        finally:
            if reconnect:
                self._reconnectThread = None

    def getDeviceLifecycle(self):
        if callable(getattr(self._rs232Manager, 'reconnectTransport', None)):
            return self._lifecycle
        return None

    # Transport hooks (DeviceLifecycleService §4.3): the shared port was
    # reopened in place; the stage must trust no position until it has read
    # one back.
    def _lifecycleSafeState(self, *, verified: bool):
        return []                       # a stage does not move by itself

    def _onTransportReconnected(self, real: bool, *, _locked: bool = False):
        """Re-synchronise the XY frame from the hardware; the errors found,
        if any. Exclusive against moves like a reconnect is."""
        if not _locked:
            with self._operation(reconnect=True):
                return self._onTransportReconnected(real, _locked=True)
        self.positionSynced = False
        try:
            if not real:
                raise RuntimeError('Could not reopen the configured serial port.')
            self._usingMockFallback = False
            if not self.syncPositionFromHardware():
                raise RuntimeError('Stage did not return a valid XY position after reconnect.')
        except Exception as exc:
            self._setConnectionError(
                exc, summary='Marzhauser reconnect failed; motion disabled',
                mock_active=getattr(self._rs232Manager, 'runtimeMode', None)
                is DeviceRuntimeMode.MOCK,
            )
            return [str(exc)]
        return []


    def getDeviceDescriptorSpec(self):
        rs232_name = (self._positionerInfo.managerProperties or {}).get('rs232device')
        return rs232BackedPrimarySpec(
            category='positioner',
            rs232_name=str(rs232_name),
            hardware_id=self._lifecycle.hardware_id,
        )

    def _commandMove(self, cmd, value, axis, *, relative):
        with self._operation():
            if not self.positionSynced:
                raise RuntimeError('Marzhauser position is unknown; reconnect or synchronize first.')
            try:
                self._rs232Manager.query(cmd)
            except Exception as exc:
                self.positionSynced = False
                self._setConnectionError(exc, summary='Marzhauser motion failed; position unknown')
                raise
            self._position[axis] = self._position[axis] + value if relative else value

    def move(self, value, axis):
        if axis == 'X':
            cmd = 'mor x ' + str(float(value))
        elif axis == 'Y':
            cmd = 'mor y ' + str(float(value))
        else:
            self.__logger.error('Wrong axis, has to be "X" or "Y".')
            return
        self._commandMove(cmd, value, axis, relative=True)

    def setPosition(self, value, axis):
        if axis == 'X':
            cmd = 'moa x ' + str(float(value))
        elif axis == 'Y':
            cmd = 'moa y ' + str(float(value))
        else:
            self.__logger.error('Wrong axis, has to be "X" or "Y".')
            return
        self._commandMove(cmd, value, axis, relative=False)


# Copyright (C) 2020-2021 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
