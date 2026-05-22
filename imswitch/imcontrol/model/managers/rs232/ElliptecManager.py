try:
    from thorlabs_elliptec import ELLx, ELLError, ELLStatus, list_devices
    _ELLIPTEC_AVAILABLE = True
except ImportError:
    _ELLIPTEC_AVAILABLE = False

from imswitch.imcommon.model import initLogger

class ElliptecManager:
    def __init__(self, rs232Info, *args, **kwargs):
        self.__logger = initLogger(self)
        self._port = rs232Info.managerProperties['port']
        self.device_active = False

        if not _ELLIPTEC_AVAILABLE:
            self.__logger.error(
                'thorlabs_elliptec is not installed — ElliptecManager disabled. '
                'Install it with: pip install thorlabs-elliptec'
            )
            self.raw_counts_per_position = 1
            return

        try:
            self._device = ELLx(serial_port=self._port)
            self.device_active = True
        except Exception:
            self.__logger.error('Failed to initialize slider, check connection and port')

        self.raw_counts_per_position = rs232Info.managerProperties['posConvFac']

    def moveToPosition(self, position):

        """Position given as index 0-3 for the 4-position ELL9/M slider"""
        if self.device_active:
            counts = position * self.raw_counts_per_position
            self._device.move_absolute_raw(counts, blocking=True)
            if self._device.status.description == 'ok':
                self.__logger.info('Slider movement completed successfully')
            else:
                self.__logger.error('Slider movement failed')