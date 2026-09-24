import re
import pyvisa
from pyvisa import constants

from imswitch.imcommon.model import initLogger


_PARITIES = {
    'none': constants.Parity.none,
    'odd': constants.Parity.odd,
    'even': constants.Parity.even,
    'mark': constants.Parity.mark,
    'space': constants.Parity.space,
}

_STOP_BITS = {
    1.0: constants.StopBits.one,
    1.5: constants.StopBits.one_and_a_half,
    2.0: constants.StopBits.two,
}

# A line ending written out as escape sequences: backslash + r, backslash + n.
_ESCAPED_TERMINATION = re.compile(r'(?:\\[rn])+')


def decodeTermination(value, key='termination'):
    """Return the line ending ``value`` stands for.

    The config editor used to save a chosen ``\\r`` as the two characters
    backslash + ``r`` (``"\\\\r"`` in the JSON) rather than a carriage return.
    pyvisa then sent that pair after every command and waited for it before
    returning a reply, so the device never saw the end of a command and every
    query timed out. No device ends its lines with a literal backslash, so a
    value made only of those escapes is decoded -- with a warning, because the
    setup file itself is still wrong.
    """
    if isinstance(value, str) and _ESCAPED_TERMINATION.fullmatch(value):
        decoded = value.replace('\\r', '\r').replace('\\n', '\n')
        initLogger('RS232Driver').warning(
            f'{key} is written as the characters {value!r} rather than the '
            f'line ending {decoded!r}; using {decoded!r}. Fix the setup file: '
            f'in JSON a carriage return is "\\r", not "\\\\r".'
        )
        return decoded
    return value


class _SerialAdapter:
    """Thin pyserial wrapper that mimics the pyvisa resource interface used by RS232Driver."""

    # Map pyvisa attribute names → serial.Serial attribute names
    _ATTR_MAP = {
        'baud_rate': 'baudrate',
        'write_termination': '_write_term',
        'read_termination': '_read_term',
        'encoding': '_encoding',
        # pyvisa stop_bits / parity are constants objects; we convert on set
    }

    def __init__(self, port: str, defaults: dict):
        import serial
        from pyvisa import constants as vc

        self._write_term = '\r'
        self._read_term = '\r\n'
        self._encoding = 'ascii'

        baud = defaults.get('baud_rate', 115200)
        self._write_term = defaults.get('write_termination', '\r')
        self._read_term = defaults.get('read_termination', '\r\n')
        self._encoding = defaults.get('encoding', 'ascii')

        # Convert pyvisa parity constant → pyserial parity char
        parity_map = {
            vc.Parity.none: 'N',
            vc.Parity.odd: 'O',
            vc.Parity.even: 'E',
            vc.Parity.mark: 'M',
            vc.Parity.space: 'S',
        }
        parity = parity_map.get(defaults.get('parity', vc.Parity.none), 'N')

        # Convert pyvisa stop_bits constant → pyserial stopbits
        stopbits_map = {
            vc.StopBits.one: 1,
            vc.StopBits.one_and_a_half: 1.5,
            vc.StopBits.two: 2,
        }
        stopbits = stopbits_map.get(defaults.get('stop_bits', vc.StopBits.one), 1)

        self._ser = serial.Serial(
            port, baudrate=baud, parity=parity, stopbits=stopbits, timeout=1
        )

    def __setattr__(self, name, value):
        # Allow setting pyvisa-style attribute names after construction
        if name.startswith('_') or name not in ('baud_rate', 'write_termination',
                                                  'read_termination', 'encoding',
                                                  'parity', 'stop_bits'):
            super().__setattr__(name, value)
            return
        if name == 'baud_rate':
            self._ser.baudrate = value
        elif name == 'write_termination':
            super().__setattr__('_write_term', value)
        elif name == 'read_termination':
            super().__setattr__('_read_term', value)
        elif name == 'encoding':
            super().__setattr__('_encoding', value)
        # parity / stop_bits changes after open are rare; ignore silently

    def write(self, command: str):
        msg = (command + self._write_term).encode(self._encoding)
        self._ser.write(msg)

    def read(self) -> str:
        return self._ser.readline().decode(self._encoding).rstrip(self._read_term)

    def query(self, command: str) -> str:
        self.write(command)
        return self.read()

    def close(self):
        if self._ser and self._ser.is_open:
            self._ser.close()


class RS232Driver:
    """RS-232 driver backed by pyvisa with a pyserial fallback.

    Tries pyvisa (default backend, then @py) first; if both fail it opens the
    port directly with pyserial.  This handles environments where NI-VISA's
    ASRL component is absent or the pyvisa-py ASRL address mapping doesn't
    match the host OS serial port naming scheme.
    """

    DEFAULTS = None  # subclasses set this via generateDriverClass()

    def __init__(self, port, *args):
        self._port = port
        self._resource = None

    @staticmethod
    def _to_visa_name(port: str) -> str:
        """Translate plain port names to VISA resource strings.

        COM3          → ASRL3::INSTR
        /dev/ttyS2    → ASRL/dev/ttyS2::INSTR
        ASRL3::INSTR  → unchanged
        """
        if '::' in port:
            return port
        m = re.match(r'^COM(\d+)$', port.strip(), re.IGNORECASE)
        if m:
            return f'ASRL{m.group(1)}::INSTR'
        return f'ASRL{port.strip()}::INSTR'

    @staticmethod
    def _to_serial_port(port: str) -> str:
        """Extract a pyserial-compatible port name.

        ASRL3::INSTR           → COM3  (Windows)
        ASRL/dev/ttyUSB0::INSTR → /dev/ttyUSB0
        COM3                   → COM3
        /dev/tty.*             → unchanged
        """
        m = re.match(r'^ASRL(\d+)::INSTR$', port.strip(), re.IGNORECASE)
        if m:
            return f'COM{m.group(1)}'
        m = re.match(r'^ASRL(.+)::INSTR$', port.strip(), re.IGNORECASE)
        if m:
            return m.group(1)
        return port.strip()

    def initialize(self):
        visa_name = self._to_visa_name(self._port)
        defaults = (self.DEFAULTS or {}).get('ASRL', {})

        # --- attempt 1 & 2: pyvisa (NI-VISA then pyvisa-py) ---
        pyvisa_exc = None
        for backend in ('', '@py'):
            try:
                rm = pyvisa.ResourceManager(backend) if backend else pyvisa.ResourceManager()
                self._resource = rm.open_resource(visa_name)
                for key, value in defaults.items():
                    setattr(self._resource, key, value)
                return
            except Exception as exc:
                pyvisa_exc = exc

        # --- attempt 3: raw pyserial ---
        serial_port = self._to_serial_port(self._port)
        try:
            self._resource = _SerialAdapter(serial_port, defaults)
            return
        except Exception as serial_exc:
            raise RuntimeError(
                f'Could not open serial port {self._port!r}. '
                f'pyvisa error: {pyvisa_exc}. '
                f'pyserial error: {serial_exc}.'
            ) from serial_exc

    def finalize(self):
        if self._resource is not None:
            self._resource.close()
            self._resource = None

    def close(self):
        self.finalize()

    def query(self, command):
        if self._resource is None:
            raise OSError('RS232 resource already closed')
        return self._resource.query(command)

    def write(self, command):
        if self._resource is None:
            raise OSError('RS232 resource already closed')
        return self._resource.write(command)

    def read(self, *args, **kwargs):
        if self._resource is None:
            raise OSError('RS232 resource already closed')
        return self._resource.read()

    def getDefaults(cls, settings):
        try:
            baudrate = int(settings["baudrate"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(
                f'Invalid baudrate {settings.get("baudrate")!r}; expected an integer'
            ) from None

        parity = str(settings["parity"]).strip().lower()
        if parity not in _PARITIES:
            raise ValueError(
                f'Unsupported parity {settings["parity"]!r}; expected one of '
                f'{", ".join(_PARITIES)}'
            )
        set_par = _PARITIES[parity]

        try:
            set_stopb = _STOP_BITS[float(settings["stopbits"])]
        except (KeyError, TypeError, ValueError):
            raise ValueError(
                f'Unsupported stopbits {settings["stopbits"]!r}; expected 1, '
                f'1.5 or 2'
            ) from None

        defaults = {
            'ASRL': {
                'write_termination': decodeTermination(
                    settings["send_termination"],
                    'send_termination',
                ),
                'read_termination': decodeTermination(
                    settings["recv_termination"],
                    'recv_termination',
                ),
                 'baud_rate': baudrate,
                 'parity': set_par,
                 'stop_bits': set_stopb,
                 'encoding': settings["encoding"],
                 }
        }
        return defaults


def generateDriverClass(settings):
    class GeneratedDriver(RS232Driver):
        DEFAULTS = RS232Driver.getDefaults(settings)
        try:
            del DEFAULTS['ASRL']['bytesize']
        except KeyError:
            pass

    return GeneratedDriver


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
