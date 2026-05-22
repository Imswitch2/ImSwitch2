"""Oxxius Laser Driver."""

import sys
from enum import Enum, IntEnum
from functools import cache
from time import perf_counter
from serial import Serial, EIGHTBITS, STOPBITS_ONE, PARITY_NONE, \
    SerialTimeoutException
import serial
import logging

# Define StrEnums if they don't yet exist.
if sys.version_info < (3, 11):
    class StrEnum(str, Enum):
        pass
else:
    from enum import StrEnum



class Cmd(StrEnum):
    LaserDriverControlMode = "ACC"  # Set laser mode: [Power=0, Current=1]
    ExternalPowerControl = "AM"  # Enable(1)/Disable(0) External power control
    LaserEmission = "L"  # Enable/Disable Laser Emission. Or DL?
    LaserCurrent = "CM"  # Set laser current ##.# [mA] or C? C saves to memory
    LaserPower = "P"  # Set laser power ###.# [mW] Or PM?
    FiveSecEmissionDelay = "CDRH"  # Enable/Disable 5-second CDRH delay
    FaultCodeReset = "RST"  # Clears all fault codes or resets the laser unit (0)
    TemperatureRegulationLoop = "T"  # Set Temperature Regulation Loop
    PercentageSplit = "IPA"  # Set % split between lasers
    DigitalModulation = "TTL"  # Sets the digital high-speed modulation


class Query(StrEnum):
    DigitalModulation = "?TTL"
    EmmissionKeyStatus = "?KEY"
    LaserType = "INF?"
    USBConfiguration = "?CDC"
    LaserDriverControlMode = "?ACC"  # Request laser control mode
    FaultCode = "?F"  # Request fault code
    ExternalPowerControl = "?AM"  # Request external power control
    BasePlateTemperature = "?BT"    # Request baseplate temp
    FiveSecEmissionDelay = "?CDRH"  # Request 5-second CDRH Delay status
    LaserOperatingHours = "?HH"  # Request laser operating hours.
    LaserIdentification = "?HID"  # Request Laser type.
    LaserEmission = "?L"  # Request laser emission status.
    LaserPower = "?P"  # Request measured laser power.
    LaserPowerSetting = "?SP"  # Request desired laser power setpoint.
    MaximumLaserPower = "?MAXLP"  # Request maximum laser power.
    LaserCurrent = "?C"  # Request measured laser current
    LaserCurrentSetting = "?SC"  # Request desired laser current setpoint
    MaximumLaserCurrent = "?MAXLC"  # Request maximum laser current.
    InterlockStatus = "?INT"  # Request interlock status
    LaserVoltage = "?IV"  # Request measured laser voltage
    TemperatureRegulationLoopStatus = "?T"  # Request Temperature Regulation Loop status
    PercentageSplitStatus = "?IPA"

class FaultCodeField(IntEnum):
    NO_ALARM = 0,
    DIODE_CURRENT = 1,
    LASER_POWER = 2,
    POWER_SUPPLY = 3,
    DIODE_TEMPERATURE = 4,
    BASE_TEMPERATURE = 5,
    INTERLOCK = 7


# Laser State Representation
class OxxiusState(IntEnum):
    WARMUP = 0,
    STANDBY = 2,
    LASER_EMISSION_ACTIVE = 3,
    INTERNAL_ERROR = 4,
    FAULT = 5,
    SLEEP = 6

class OxxiusUSBConfiguration(IntEnum):
    STANDARD_USB = 0
    VIRTUAL_SERIAL_PORT = 1

# Boolean command value that can also be compared like a boolean.
class BoolVal(StrEnum):
    OFF = "0"
    ON = "1"

OXXIUS_COM_SETUP = \
    {
        "baudrate": 9600,
        "bytesize": EIGHTBITS,
        "parity": PARITY_NONE,
        "stopbits": STOPBITS_ONE,
        "xonxoff": False,
        "timeout": 1
    }

REPLY_TERMINATION = b'\r\n'

class OxxiusLaser:

    def __init__(self, port, prefix=None):
        """Generic class for lasers L6CC combiner, LBX, and LCX"""

        self.prefix = f'{prefix} ' if prefix is not None else ''
        self.ser = Serial(port, **OXXIUS_COM_SETUP) if type(port) != Serial else port
        self.ser.reset_input_buffer()
        # Since we're likely connected over an RS232-to-usb-serial interface,
        # ask for some sort of reply to make sure we're not timing out.
        try:
            # Put the interface into a known state to simplify communication.
            self.get(Query.LaserCurrent)
        except SerialTimeoutException:
            print(f"Connected to '{self.ser.port}' but the device is not responding.")
            raise

    @property
    def temperature(self):
         """Return temperature of baseplate"""
         return self.get(Query.BasePlateTemperature)

    @property
    def faults(self):
        """return a list of faults or empty list if no faults are present."""
        faults = []
        fault_code = int(self.get(Query.FaultCode))
        # Skip first Enum (LASER_EMISSION_ACTIVE), which is not really a fault.
        fault_code_fields = iter(FaultCodeField)
        next(fault_code_fields)
        for index, field in enumerate(fault_code_fields):
            if bin(fault_code)[-1] == '1':
                faults.append(field)
            fault_code = fault_code >> 1
            return faults

    @property
    def serial_number(self):
        """Retrieves the unit’s serial number"""
        return self.get(Query.LaserIdentification)

    def get(self, msg: Query) -> str:
        """Request a setting from the device."""
        reply = self._send(msg.value)
        return reply


    def set(self, msg: Cmd, value) -> str:
        return self._send(f"{msg} {value}")


    def _send(self, msg: str, raise_timeout: bool = True) -> str:
        """send a message and return the reply.
        :param msg: the message to send in string format
        :param raise_timeout: bool to indicate if we should raise an exception
            if we timed out.
        :returns: the reply (without line formatting chars) in str format
            or emptystring if no reply. Raises a timeout exception if flagged
            to do so.
        """
        # Note: Timing out on a serial port read does not throw an exception,
        #   so we need to do this manually.

        # All outgoing commands are bookended with a '\r\n' at the beginning
        # and end of the message.
        prefix_msg = f'{self.prefix}{msg}\r'
        self.ser.write(prefix_msg.encode('ascii'))
        start_time = perf_counter()
        # Read the first '\r\n'.
        reply = self.ser.read_until(REPLY_TERMINATION)
        # Raise a timeout if we got no reply and have been flagged to do so.
        if not len(reply) and raise_timeout and \
                perf_counter() - start_time > self.ser.timeout:
            raise SerialTimeoutException
        return reply.rstrip(REPLY_TERMINATION).decode('utf-8')

class LCX(OxxiusLaser):

    def __init__(self, port, prefix):
        """Class for the LBX series oxxius laser"""

        super().__init__(port, prefix)
        self.log = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    @property
    def emission_status(self):
        """Returns if laser is on or off"""
        return BoolVal(self.get(Query.LaserEmission))

    def enable(self):
        """Enable emission. This opens the shutter on laser"""
        self.set(Cmd.LaserEmission, BoolVal.ON)

    def disable(self):
        """disable emission. This closes shutter on laser"""
        self.set(Cmd.LaserEmission, BoolVal.OFF)

    @property
    def max_power(self):
        """Returns maximum power of laser"""
        return self.get(Query.MaximumLaserPower)

    @property
    def power(self):
        """Returns current power of laser in mW"""
        return self.get(Query.LaserPower)


    @power.setter
    def power(self, value: float):
        """Set laser power setpoint."""

        self.set(Cmd.LaserPower, value)

    @property
    def power_setpoint(self):
        """Return to setpoint of laser power in mW"""
        return self.get(Query.LaserPowerSetting)

    @power_setpoint.setter
    def power_setpoint(self, value: float):
        """Set laser power setpoint."""
        if 0 > value > self.max_power:
            reason = f"exceeds maximum power output {self.max_power}mW" if value > self.max_power else f"is below 0mW"
            self.log.error(f"Cannot set laser to {value}ml because it {reason}")
        else:
            self.set(Cmd.LaserPower, value)

class LBX(OxxiusLaser):

    def __init__(self, port, prefix):
        """Class for the LBX series oxxius laser"""

        super().__init__(port, prefix)
        self.log = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    @property
    def cdrh(self):
        status = self.get(Query.FiveSecEmissionDelay)
        return BoolVal(status)

    @cdrh.setter
    def cdrh(self, status: BoolVal):
        self.set(Cmd.FiveSecEmissionDelay, status)

    @property
    def constant_current(self):
        """Return if constant current is on or off.
        Note digital modulation can only be on in constant current mode"""
        return BoolVal(self.get(Query.LaserDriverControlMode))

    @constant_current.setter
    def constant_current(self, value:BoolVal):
        """Set constant current mode on or off"""

        if value == BoolVal.OFF and self.digital_modulation == BoolVal.ON:
            self.log.warning(f'Putting Laser {self.prefix} in constant power mode and disabling digital modulation mode')
        self.set(Cmd.LaserDriverControlMode, value)

    @property
    def digital_modulation(self):
        """Return if digital modulation mode is on or off"""
        return BoolVal(self.get(Query.DigitalModulation))

    @digital_modulation.setter
    def digital_modulation(self, value: BoolVal):
        """Set digital modulation mode.
        Note if laser in constant power mode, digital modulation can't be turned on"""
        if self.constant_current == BoolVal.OFF:
            self.log.warning(f'Laser {self.prefix} is in constant power mode and cannot be put in digital modulation mode')
        else:
            self.set(Cmd.DigitalModulation, value)

    @property
    def external_control_mode(self):
        """Returns external control mode/analog modulation mode"""
        return BoolVal(self.get(Query.ExternalPowerControl))

    @external_control_mode.setter
    def external_control_mode(self, value: BoolVal):
        """Sets external control mode/analog modulation mode"""
        self.set(Cmd.ExternalPowerControl, value)

    @property
    def emission_status(self):
        """Returns if laser is on or off"""
        return BoolVal(self.get(Query.LaserEmission))

    def enable(self):
        """Enable emission."""
        self.set(Cmd.LaserEmission, BoolVal.ON)

    def disable(self):
        """disable emission."""
        self.set(Cmd.LaserEmission, BoolVal.OFF)

    @property
    def max_power(self):
        """Returns maximum power of laser"""
        return self.get(Query.MaximumLaserPower)

    @property
    def power(self):
        """Returns current power of laser in mW"""
        return self.get(Query.LaserPower)

    @property
    def power_setpoint(self):
        """Return to setpoint of laser power in mW"""
        return self.get(Query.LaserPowerSetting)

    @power_setpoint.setter
    def power_setpoint(self, value:float):
        """Set laser power setpoint. Note, if laser in constant current mode this won't change intensity"""
        if 0 > value > self.max_power:
            reason = f"exceeds maximum power output {self.max_power}mW" if value > self.max_power else f"is below 0mW"
            self.log.error(f"Cannot set laser to {value}ml because it {reason}")
        else:
            if self.constant_current == BoolVal.ON:
                self.log.warning("Laser is in constant current mode so changing power will not change intensity")
            self.set(Cmd.LaserPower, value)

    @property
    def max_current(self):
        """Returns maximum power of laser"""
        return self.get(Query.MaximumLaserCurrent)

    @property
    def current(self):
        """Returns current power of laser in mA"""
        return self.get(Query.LaserPower)

    @property
    def current_setpoint(self):
        """Return to setpoint of laser current in mA. This is a percentage of current"""
        return self.get(Query.LaserCurrentSetting)

    @current_setpoint.setter
    def current_setpoint(self, value: float):
        """Set laser current setpoint as a percent. Note, if laser in constant power mode this won't change intensity"""
        if 0 > value > 100:
            reason = f"exceeds 100%" if value > self.max_power else f"is below 0%"
            self.log.error(f"Cannot set laser to {value}ml because it {reason}")
        else:
            if self.constant_current == BoolVal.OFF:
                self.log.warning("Laser is in constant power mode so changing power will not change intensity")
            self.set(Cmd.LaserCurrent, value)



class L6CCCombiner(OxxiusLaser):


    def __init__(self, port):
        """Class for the L6CC oxxius combiner. This combiner can have LBX lasers or LCX"""

        super().__init__(port)
        self.log = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    @property
    def percentage_split(self):
        """Set percentage split of lasers"""

        return self.get(Query.PercentageSplitStatus)


    @percentage_split.setter
    def percentage_split(self, value):
        """Get percentage split of lasers"""
        if value > 100 or value < 0:
            self.log.error(f'Impossible to set percentage spilt to {value}')
            return
        self.set(Cmd.PercentageSplit, value)

    @property
    def port_configuration(self):
        """Retrieves the configuration of the USB port """
        configuration = self.get(Query.USBConfiguration)
        return OxxiusUSBConfiguration(configuration)

    @property
    def cdrh(self):
        status = self.get(Query.FiveSecEmissionDelay)
        return BoolVal(status)

    @cdrh.setter
    def cdrh(self, status:BoolVal):
        self.set(Cmd.FiveSecEmissionDelay, status)

    @property
    def laser_type(self):
        """Retrieves the type of laser"""
        return self.get(Query.LaserType)

    @property
    def interlock_status(self):
        """Retrieves the status of the interlock circuit"""
        return BoolVal(self.get(Query.InterlockStatus))

    @property
    def emmision_key_status(self):
        """Retrieves the status of the emission key,
            or the “Key” signal on the DE-15
            electrical interface
            """
        return BoolVal(self.get(Query.EmmissionKeyStatus))

    @property
    def LBX_constant_current_status(self):
        """Retrieves the status of automatic constant current for all LBX lasers.
         Only one LBX needs to be in constant power to return OFF"""
        return BoolVal(self.get(Query.LaserDriverControlMode))

    @LBX_constant_current_status.setter
    def LBX_constant_current_status(self, status:BoolVal):
        """Set all LBX lasers to constant current mode (ON) or constant power mode (OFF).
        If any LBX lasers are in digital modulation mode, it will be disabled when set to constant power"""
        self.set(Cmd.LaserDriverControlMode, status)

    def digital_modualtion(self, prefix:str):
        """Returns digital modulation mode of specific laser in box"""
        return BoolVal(self.get(Query.DigitalModulation+prefix))

    def set_digital_modulation(self, prefix:str, value: BoolVal):
        """sets digital modulation mode of specific laser in box"""
        # If laser is in constant power mode, then digital modulation can't be turned on
        if self.get(f"L{prefix} "+Query.LaserDriverControlMode) == BoolVal.OFF:
            self.log.warning(f'Laser {prefix} is in constant power mode and cannot be put in digital modulation mode')
        else:
            self.set(Cmd.DigitalModulation+prefix, value)

    def external_control_mode(self, prefix:str):
        """Returns external control mode/analog modulation mode of specific laser in box"""
        return BoolVal(self.get(Query.ExternalPowerControl+prefix))

    def set_external_control_mode(self, prefix:str, value: BoolVal):
        """Sets external control mode/analog modulation mode of specific laser in box"""
        self.set(Cmd.ExternalPowerControl+prefix, value)