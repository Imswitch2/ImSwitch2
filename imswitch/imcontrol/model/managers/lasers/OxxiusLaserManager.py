from imswitch.imcommon.model import initLogger
from .LaserManager import LaserManager
from imswitch.imcontrol.model.interfaces.oxxius import LBX, LCX


class OxxiusLaserManager(LaserManager):
    """ LaserManager for a single Oxxius LBX or LCX laser connected directly
    over a serial port.

    Manager properties:

    - ``port`` -- serial port (e.g. ``COM3`` or ``/dev/ttyUSB0``)
    - ``prefix`` -- optional combiner prefix string (e.g. ``"L1"``)
    - ``laserType`` -- ``"LBX"`` (default) or ``"LCX"``
    """

    def __init__(self, laserInfo, name, **_lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)
        port = laserInfo.managerProperties['port']
        prefix = laserInfo.managerProperties.get('prefix', None)
        laser_type = laserInfo.managerProperties.get('laserType', 'LBX')

        try:
            if laser_type == 'LCX':
                self._laser = LCX(port=port, prefix=prefix)
            else:
                self._laser = LBX(port=port, prefix=prefix)
            self.__logger.info(f'Connected to Oxxius {laser_type} on {port}')
            self._setConnected("Oxxius laser connected")
        except Exception as e:
            self.__logger.error(f'Failed to connect to Oxxius laser on {port}: {e}')
            self._laser = None
            self._setConnectionError(e, summary="Oxxius laser connection failed")

        super().__init__(laserInfo, name, isBinary=False, valueUnits='mW', valueDecimals=1)

    def setEnabled(self, enabled):
        if self._laser is None:
            return
        try:
            if enabled:
                self._laser.enable()
            else:
                self._laser.disable()
        except Exception as e:
            self.__logger.error(f'Error setting laser enabled={enabled}: {e}')

    def setValue(self, power):
        if self._laser is None:
            return
        try:
            self._laser.power = float(power)
        except Exception as e:
            self.__logger.error(f'Error setting laser power to {power}: {e}')
