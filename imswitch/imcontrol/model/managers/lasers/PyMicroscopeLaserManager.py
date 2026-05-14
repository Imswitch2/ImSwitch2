import importlib
from .LaserManager import LaserManager
from imswitch.imcommon.model import pythontools, initLogger

class PyMicroscopeLaserManager(LaserManager):
    """ Generic LaserManager for laser handlers supported by Python Microscope.

    Manager properties:

    - ``pyMicroscopeDriver`` -- string describing the Python Microscope
        object to initialize; requires to specify the module
        and the class name, e.g. ``toptica.TopticaiBeam``
    - ``digitalPorts`` -- string describing the COM port
        to connect to, e.g. ``["COM4"]``
    """
    def __init__(self, laserInfo, name, **_lowLevelManager) -> None:
        self.__logger = initLogger(self, instanceName=name)
        self._isMock = False
        self.__port = laserInfo.managerProperties.get("digitalPorts", "Unknown")
        self.__driver = str(laserInfo.managerProperties.get("pyMicroscopeDriver", "Unknown"))
        
        try:
            driver = self.__driver.split(".")
            if len(driver) != 2:
                raise ValueError(
                    f"pyMicroscopeDriver must be in format 'module.class', got: '{self.__driver}'"
                )
            package = importlib.import_module(
                pythontools.joinModulePath("microscope.lights", driver[0])
            )
            self.__laser = getattr(package, driver[1])(self.__port)
            self.__logger.info(f"[{self.__port}] {self.__driver} initialized. ")
        except Exception as e:
            self._isMock = True
            self.__logger.warning(
                f'Failed to initialize PyMicroscope hardware, running in mock mode: {e}'
            )
            self.__laser = None
        
        super().__init__(laserInfo, name, isBinary=False, valueUnits='mW', valueDecimals=1)
        self.__maxPower = float(laserInfo.valueRangeMax)
    
    def setEnabled(self, enabled: bool) -> None:
        if self._isMock:
            self.__logger.debug(f'Mock mode: setEnabled({enabled}) ignored')
            return
        
        (self.__laser.enable() if enabled else self.__laser.disable())
    
    def setValue(self, value) -> None:
        # laser power is handled as percentage
        # so we divide for the max power to obtain
        # the actual percentage to which we set
        # the output power
        if self._isMock:
            self.__logger.debug(f'Mock mode: setValue({value}) ignored')
            return
        
        if self.__maxPower == 0:
            self.__logger.error(f"Cannot set power: maxPower is zero")
            return
        self.__laser.power = float(value) / self.__maxPower
    
    def finalize(self) -> None:
        if self._isMock:
            self.__logger.debug(f'Mock mode: finalize() ignored')
            return
        
        self.__logger.info(f"[{self.__port}] {self.__driver} closed.")
        self.__laser.shutdown()