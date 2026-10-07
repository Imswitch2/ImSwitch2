"""Thorlabs PM100 power meters and PAX1000 polarimeters over VISA (SCPI).

Both instruments are USB-TMC devices found by serial number among the VISA
resources (``USB0::0x1313::<product>::<serial>::INSTR``). ``pyvisa`` is
imported on connect only, so ImSwitch runs without it until an instrument is
connected (``pip install imswitch2[hardware]`` installs it, plus pyvisa-py).

Failure kinds (``instrument.py``): anything that goes wrong talking to the
device is a :class:`TransportError` (the connection is faulted); an answer
that cannot be parsed is a :class:`MalformedReading` (an invalid sample, the
connection stays up).

Timing is ``UNVERIFIED`` for both drivers until checked on a rig (plan §6.6):

- PM100: whether ``READ?`` starts a fresh measurement after the command
  (then the profile becomes ``TRIGGERED`` + verified);
- PAX1000: what fields 0-8 of ``SENS:DATA:LAT?`` are -- a revolution counter
  or timestamp would allow ``DEVICE_TIMESTAMP``; until then a measured
  ``UPDATE_BOUND``. :attr:`ThorlabsPAX1000Driver.last_fields` keeps the last
  packet for that check.

Also unverified: the unit of PAX field 12 (power; ``power_unit``), the unit
of ``SENS:WAV`` (metres per the helper this was ported from; the readback is
interpreted either way), and PM100 zeroing completion
(``SENS:CORR:COLL:ZERO:STAT?``).
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from imswitch.imcommon.model.measurement_run import TimingRule

from .instrument import (
    ActionSpec,
    InstrumentDriver,
    InstrumentIdentity,
    MalformedReading,
    RawReading,
    SettingSpec,
    TimingProfile,
    TransportError,
)
from .quantities import PAX_QUANTITIES, POWER_QUANTITY

#: ``(backend) -> pyvisa.ResourceManager``-like; injected by tests.
ResourceManagerFactory = Callable[[str], Any]


def _pyvisa_resource_manager(backend: str):
    try:
        import pyvisa
    except ImportError as exc:
        raise TransportError(
            'pyvisa is not installed (pip install "imswitch2[hardware]", '
            'or pip install pyvisa pyvisa-py)') from exc
    try:
        return pyvisa.ResourceManager(backend)
    except Exception as exc:
        raise TransportError(f'no VISA library for backend {backend!r}: {exc}') from exc


class VisaLink:
    """One VISA instrument, found by serial number. Every failure is a
    :class:`TransportError`."""

    def __init__(self, serial: str, *, backend: str = '', timeout_ms: int = 2000,
                 product: str = '', resource_manager_factory: Optional[ResourceManagerFactory] = None,
                 label: str = 'instrument') -> None:
        self.serial = str(serial or '').strip()
        self.backend = str(backend or '')
        self.timeout_ms = int(timeout_ms)
        #: Optional resource-string filter, e.g. ``0x8078`` for a PM100D.
        self.product = product
        self.label = label
        self._factory = resource_manager_factory or _pyvisa_resource_manager
        self._rm = None
        self._inst = None
        self.resource_name: Optional[str] = None

    @property
    def is_open(self) -> bool:
        return self._inst is not None

    def open(self) -> None:
        if self._inst is not None:
            return
        if not self.serial:
            raise TransportError(f'{self.label}: no serial number configured')
        self._rm = self._factory(self.backend)
        try:
            resources = tuple(self._rm.list_resources())
        except Exception as exc:
            self.close()
            raise TransportError(f'{self.label}: listing VISA resources failed: {exc}') from exc
        candidates = [r for r in resources if self.serial in r
                      and (not self.product or self.product.lower() in r.lower())]
        if not candidates:
            self.close()
            raise TransportError(
                f'{self.label} with serial {self.serial!r} not found '
                f'(VISA resources: {", ".join(resources) or "none"})')
        if len(candidates) > 1:
            self.close()
            raise TransportError(
                f'{self.label}: serial {self.serial!r} matches several resources: '
                f'{", ".join(candidates)}')
        try:
            self._inst = self._rm.open_resource(candidates[0], timeout=self.timeout_ms)
        except Exception as exc:
            self.close()
            raise TransportError(f'{self.label}: opening {candidates[0]} failed: {exc}') from exc
        self.resource_name = candidates[0]

    def close(self) -> None:
        inst, rm = self._inst, self._rm
        self._inst = self._rm = None
        self.resource_name = None
        for handle in (inst, rm):
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass

    def query(self, command: str) -> str:
        inst = self._require()
        try:
            return str(inst.query(command)).strip()
        except Exception as exc:
            raise TransportError(f'{self.label}: {command} failed: {exc}') from exc

    def write(self, command: str) -> None:
        inst = self._require()
        try:
            inst.write(command)
        except Exception as exc:
            raise TransportError(f'{self.label}: {command} failed: {exc}') from exc

    def check_error(self, after: str) -> None:
        """Raise ``RuntimeError`` if the SCPI error queue is not empty (a
        refused or unknown command; the connection itself is fine)."""
        answer = self.query('SYST:ERR?')
        code = answer.split(',', 1)[0].strip()
        try:
            failed = int(float(code)) != 0
        except ValueError:
            failed = True
        if failed:
            raise RuntimeError(f'{self.label} refused {after}: {answer}')

    def _require(self):
        if self._inst is None:
            raise TransportError(f'{self.label} is not connected')
        return self._inst


def parse_idn(answer: str, label: str) -> InstrumentIdentity:
    parts = [part.strip() for part in answer.split(',')]
    if len(parts) < 3:
        raise TransportError(f'{label}: unexpected *IDN? answer {answer!r}')
    return InstrumentIdentity(vendor=parts[0], model=parts[1], serial=parts[2],
                              firmware=parts[3] if len(parts) > 3 else '')


def _float(text: str, what: str) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        raise MalformedReading(f'{what}: not a number ({text!r})') from None


def _wavelength_nm(readback: float) -> float:
    """SCPI wavelengths come back in metres or nanometres depending on the
    instrument; anything below 1 is metres."""
    return readback * 1e9 if readback < 1.0 else readback


class ThorlabsPM100Driver(InstrumentDriver):
    """PM100D / PM100USB / PM100A power meter."""

    quantities = (POWER_QUANTITY,)
    settings_spec = (SettingSpec('wavelength_nm', 'Wavelength', 'nm'),)
    actions_spec = (ActionSpec('zero', 'Zero', confirm='Block all light at the sensor',
                               requires_dark=True, timeout_s=15.0),)
    timing_profiles = (
        # READ? is documented to start a new measurement; not yet checked.
        TimingProfile(id='pm100-read', rule=TimingRule.TRIGGERED, verified=False),
    )
    poll_interval_s = 0.0

    def __init__(self, serial: str, *, backend: str = '', timeout_ms: int = 2000,
                 wavelength_nm: Optional[float] = None,
                 resource_manager_factory: Optional[ResourceManagerFactory] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.link = VisaLink(serial, backend=backend, timeout_ms=timeout_ms,
                             resource_manager_factory=resource_manager_factory,
                             label='PM100')
        self._startup_wavelength_nm = wavelength_nm
        self._settings: Dict[str, Any] = {}
        self._sleep = sleep
        self._clock = clock

    def connect(self) -> InstrumentIdentity:
        self.link.open()
        try:
            identity = parse_idn(self.link.query('*IDN?'), 'PM100')
            if 'PM1' not in identity.model.upper():
                raise TransportError(
                    f'serial {self.link.serial!r} is a {identity.vendor} {identity.model}, '
                    f'not a PM100 power meter')
            if self._startup_wavelength_nm is not None:
                self.link.write(f'SENS:CORR:WAV {float(self._startup_wavelength_nm):g}')
            self._read_settings()
            return identity
        except BaseException:
            self.link.close()
            raise

    def close(self) -> None:
        self.link.close()
        self._settings = {}

    def settings(self) -> Dict[str, Any]:
        return dict(self._settings)

    def _read_settings(self) -> None:
        wavelength = self.link.query('SENS:CORR:WAV?')
        try:
            self._settings['wavelength_nm'] = _wavelength_nm(float(wavelength))
        except ValueError:
            raise TransportError(f'PM100: unexpected wavelength readback {wavelength!r}') from None

    def set_setting(self, name: str, value: Any) -> Any:
        if name != 'wavelength_nm':
            raise KeyError(name)
        self.link.write(f'SENS:CORR:WAV {float(value):g}')
        self._read_settings()           # the meter clamps to its sensor's range
        return self._settings['wavelength_nm']

    def run_action(self, name: str, **kwargs: Any) -> None:
        if name != 'zero':
            raise KeyError(name)
        self.link.write('SENS:CORR:COLL:ZERO:INIT')
        self.link.check_error('zeroing')
        timeout = next(a.timeout_s for a in self.actions_spec if a.name == 'zero')
        deadline = self._clock() + timeout
        while self.link.query('SENS:CORR:COLL:ZERO:STAT?').strip() not in ('0', '0.0'):
            if self._clock() > deadline:
                raise RuntimeError(f'PM100: zeroing did not finish within {timeout:g} s')
            self._sleep(0.1)

    def read(self) -> RawReading:
        return RawReading(values={'power': _float(self.link.query('READ?'), 'PM100 READ?')})


#: Field positions in a ``SENS:DATA:LAT?`` packet (measurement mode 9).
PAX_AZIMUTH, PAX_ELLIPTICITY, PAX_DOP, PAX_POWER = 9, 10, 11, 12


def parse_pax_packet(raw: str) -> List[float]:
    """Comma-, semicolon-, tab- or space-separated numbers; text tokens are
    rejected (they shift every later field)."""
    parts = [p for p in raw.replace(';', ',').replace('\t', ',').replace(' ', ',').split(',') if p]
    values = []
    for index, part in enumerate(parts):
        values.append(_float(part, f'PAX field {index}'))
    return values


class ThorlabsPAX1000Driver(InstrumentDriver):
    """PAX1000 rotating-waveplate polarimeter in measurement mode 9."""

    quantities = PAX_QUANTITIES
    settings_spec = (SettingSpec('wavelength_nm', 'Wavelength', 'nm'),
                     SettingSpec('mode', 'Measurement mode'))
    actions_spec = ()

    def __init__(self, serial: str, *, backend: str = '', timeout_ms: int = 5000,
                 wavelength_nm: Optional[float] = None, power_unit: str = 'W',
                 update_bound_s: float = 0.5, update_period_s: float = 0.1,
                 resource_manager_factory: Optional[ResourceManagerFactory] = None) -> None:
        if power_unit not in ('W', 'mW'):
            raise ValueError(f"power_unit must be 'W' or 'mW', not {power_unit!r}")
        self.link = VisaLink(serial, backend=backend, timeout_ms=timeout_ms,
                             resource_manager_factory=resource_manager_factory,
                             label='PAX1000')
        self._startup_wavelength_nm = wavelength_nm
        self._power_scale = 1e-3 if power_unit == 'mW' else 1.0
        self._settings: Dict[str, Any] = {}
        #: Settings whose readback query was not answered (commanded value kept).
        self.unconfirmed_settings: set = set()
        #: The last raw packet, for checking fields 0-8 on a rig.
        self.last_fields: Tuple[float, ...] = ()
        self.timing_profiles = (
            # A bound measured by hand; no device timing used yet.
            TimingProfile(id='pax-mode9-update-bound', rule=TimingRule.UPDATE_BOUND,
                          verified=False, conditions={'mode': 9},
                          update_bound_s=float(update_bound_s),
                          update_period_s=float(update_period_s)),
        )

    def connect(self) -> InstrumentIdentity:
        self.link.open()
        try:
            identity = parse_idn(self.link.query('*IDN?'), 'PAX1000')
            if 'PAX' not in identity.model.upper():
                raise TransportError(
                    f'serial {self.link.serial!r} is a {identity.vendor} {identity.model}, '
                    f'not a PAX polarimeter')
            commanded: Dict[str, Any] = {'mode': 9}
            self.link.write('SENS:CALC 9')
            if self._startup_wavelength_nm is not None:
                self.link.write(f'SENS:WAV {float(self._startup_wavelength_nm) * 1e-9:.12g}')
                commanded['wavelength_nm'] = float(self._startup_wavelength_nm)
            self.link.write('INP:ROT:STAT 1')
            self._read_settings(commanded)
            return identity
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self.link.is_open:
            try:
                self.link.write('INP:ROT:STAT 0')       # stop the waveplate first
            except TransportError:
                pass
        self.link.close()
        self._settings = {}
        self.unconfirmed_settings = set()

    def settings(self) -> Dict[str, Any]:
        return dict(self._settings)

    def _read_settings(self, commanded: Optional[Dict[str, Any]] = None) -> None:
        """Read back mode and wavelength. A query the firmware does not
        answer keeps the commanded value and is listed in
        :attr:`unconfirmed_settings` (a rig check, plan §6.6) -- refusing to
        connect over an unconfirmed readback would make the PAX unusable."""
        commanded = dict(commanded or {})
        for name, query, convert in (
            ('mode', 'SENS:CALC?', lambda v: int(float(v))),
            ('wavelength_nm', 'SENS:WAV?', lambda v: _wavelength_nm(float(v))),
        ):
            try:
                self._settings[name] = convert(self.link.query(query))
                self.unconfirmed_settings.discard(name)
            except (TransportError, ValueError) as exc:
                if name not in commanded:
                    if name in self._settings:
                        continue
                    raise TransportError(f'PAX1000: {query} not answered: {exc}') from None
                self._settings[name] = commanded[name]
                self.unconfirmed_settings.add(name)

    def set_setting(self, name: str, value: Any) -> Any:
        if name == 'wavelength_nm':
            value = float(value)
            self.link.write(f'SENS:WAV {value * 1e-9:.12g}')
        elif name == 'mode':
            value = int(value)
            self.link.write(f'SENS:CALC {value}')
        else:
            raise KeyError(name)
        self._read_settings({name: value})
        return self._settings[name]

    def read(self) -> RawReading:
        fields = parse_pax_packet(self.link.query('SENS:DATA:LAT?'))
        if len(fields) <= PAX_POWER:
            raise MalformedReading(
                f'PAX packet has {len(fields)} fields, mode 9 needs {PAX_POWER + 1}')
        self.last_fields = tuple(fields)
        return RawReading(values={
            'azimuth': fields[PAX_AZIMUTH],
            'ellipticity': fields[PAX_ELLIPTICITY],
            'dop': fields[PAX_DOP],
            'power': fields[PAX_POWER] * self._power_scale,
        })
