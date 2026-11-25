try:
    from thorlabs_apt_device.devices.kdc101 import KDC101
    _KDC101_AVAILABLE = True
except ImportError:
    _KDC101_AVAILABLE = False

from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.devices.graph import DeviceDescriptorSpec, DeviceRole
from imswitch.imcontrol.model.devices.status import (
    DeviceManagerStatusMixin, DeviceNotConnectedError, backend_attribute,
)

"""
Encoder steps per degree found here:
https://www.thorlabs.com/Software/Motion%20Control/APT_Communications_Protocol.pdf

Encoder steps per degree for PRMTZ8 is 1919.6418578623391
"""


class KDC101Manager(DeviceManagerStatusMixin):
    """ RS232Manager for a Thorlabs KDC101 DC servo motor controller.

    Manager properties:

    - ``port`` -- serial port (e.g. ``COM15``)
    - ``posConvFac`` -- encoder steps per physical unit (e.g. 1919.64 steps/degree for PRMTZ8)
    - ``velConvFac`` -- encoder velocity conversion factor
    - ``accConvFac`` -- encoder acceleration conversion factor

    A controller that cannot be opened is left absent: reported not
    connected, every command refused with its name, reconnectable from
    Hardware status (``transient`` / ``connectOnStartup`` as for any device).
    """

    def __init__(self, rs232Info, *args, **kwargs):
        self.__logger = initLogger(self)
        self._port = rs232Info.managerProperties['port']

        self._name = f'KDC101 on {self._port}'
        self._posConvFac = rs232Info.managerProperties['posConvFac']
        self._velConvFac = rs232Info.managerProperties['velConvFac']
        self._accConvFac = rs232Info.managerProperties['accConvFac']

        def open_real():
            if not _KDC101_AVAILABLE:
                raise ImportError(
                    'thorlabs_apt_device is not installed. Install it with: '
                    'pip install thorlabs-apt-device')
            return KDC101(serial_port=self._port)

        self._installBackend(
            open_real, label=f'KDC101 on {self._port}',
            transient=bool(getattr(rs232Info, 'transient', False)),
            connect_on_startup=bool(getattr(rs232Info, 'connectOnStartup', False)),
            close=lambda device: device.close() if hasattr(device, 'close') else None,
        )

    #: The vendor device, read from the backend holder: a reconnect replaces it.
    _device = backend_attribute()

    @property
    def device_active(self) -> bool:
        return self.backendIsReal

    def _requireDevice(self):
        if not self.backendIsReal:
            raise DeviceNotConnectedError(
                f"{self._name}: command refused, "
                f"{self.connectionStatusSummary or 'not connected'}")

    def reconnectTransport(self) -> bool:
        """Open the controller again in place (DeviceLifecycleService)."""
        return self._replaceBackend()

    def disconnectTransport(self) -> None:
        self.backendHolder.disconnect()

    def getDeviceDescriptorSpec(self):
        return DeviceDescriptorSpec(role=DeviceRole.RESOURCE)

    def finalize(self):
        self.backendHolder.close(suppress_errors=True)
        self._setFinalizedStatus()

    def _toEncPosition(self, units):
        return int(units * self._posConvFac)

    def _toPositionInUnits(self, encPosition):
        return encPosition / self._posConvFac

    def _toEncVelocity(self, unitsPerS):
        return int(unitsPerS * self._velConvFac)

    def _toVelocityInUnits(self, encVelocity):
        return encVelocity / self._velConvFac

    def _toEncAcceleration(self, unitsPerSS):
        return int(unitsPerSS * self._accConvFac)

    def _toAccelerationInUnits(self, encAcceleration):
        return encAcceleration / self._velConvFac

    def home(self):
        self._requireDevice()
        self._device.home()

    def moveToInUnits(self, units):
        self._requireDevice()
        self._device.move_absolute(self._toEncPosition(units))

    def jog(self, forwardBool=True):
        self._requireDevice()
        self._device.move_jog(forwardBool)

    def startRotation(self, forwardBool=True):
        self._requireDevice()
        self._device.move_velocity(forwardBool)

    def stopRotation(self):
        self._requireDevice()
        self._device.stop()

    def setJogDistanceInUnits(self, distInUnits):
        self._requireDevice()
        self._device.set_jog_params(self._toEncPosition(distInUnits),
                                    self._device.jogparams['acceleration'],
                                    self._device.jogparams['max_velocity'])

    def setJogAccelerationInUnits(self, accInUnits):
        self._requireDevice()
        self._device.set_jog_params(self._device.jogparams['step_size'],
                                    self._toEncAcceleration(accInUnits),
                                    self._device.jogparams['max_velocity'])

    def setJogVelocityInUnits(self, velInUnits):
        self._requireDevice()
        self._device.set_jog_params(self._device.jogparams['step_size'],
                                    self._device.jogparams['acceleration'],
                                    self._toEncVelocity(velInUnits))

    def setMoveVelocityInUnits(self, velInUnits):
        self._requireDevice()
        self._device.set_velocity_params(self._device.velparams['acceleration'],
                                         self._toEncVelocity(velInUnits))

    def setMoveAccelerationInUnits(self, accInUnits):
        self._requireDevice()
        self._device.set_velocity_params(self._toEncAcceleration(accInUnits),
                                         self._device.velparams['max_velocity'])

    def getPositionInUnits(self):
        self._requireDevice()
        return self._toPositionInUnits(self._device.status['position'])
