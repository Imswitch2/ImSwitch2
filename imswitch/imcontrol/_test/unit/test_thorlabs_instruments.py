"""Thorlabs PM100 / PAX1000 drivers against a fake VISA (plan §6.6, P-4 step 3)."""
import math

import pytest

from imswitch.imcommon.model.measurement_run import Verification, WindowCause
from imswitch.imcontrol.model.measurement.instrument import (
    InstrumentSession,
    MalformedReading,
    TransportError,
    WindowRefused,
)
from imswitch.imcontrol.model.measurement.thorlabs import (
    ThorlabsPAX1000Driver,
    ThorlabsPM100Driver,
    parse_pax_packet,
)
from imswitch.imcontrol.model.resources import ResourceRegistry, set_resource_registry


@pytest.fixture(autouse=True)
def registry():
    fresh = ResourceRegistry()
    previous = set_resource_registry(fresh)
    yield fresh
    set_resource_registry(previous)


class FakeVisaError(Exception):
    pass


class FakeInstrument:
    """Answers SCPI queries from a dict of callables / strings; logs writes."""

    def __init__(self, answers, on_write=None):
        self.answers = answers
        self.on_write = on_write
        self.writes = []
        self.unplugged = False
        self.closed = False

    def query(self, command):
        if self.unplugged:
            raise FakeVisaError('VI_ERROR_TMO (-1073807339): Timeout expired')
        if command not in self.answers:
            raise FakeVisaError(f'VI_ERROR_TMO: no answer to {command}')
        answer = self.answers[command]
        return (answer() if callable(answer) else answer) + '\n'

    def write(self, command):
        if self.unplugged:
            raise FakeVisaError('VI_ERROR_CONN_LOST')
        self.writes.append(command)
        if self.on_write:
            self.on_write(command)

    def close(self):
        self.closed = True


class FakeResourceManager:
    def __init__(self, resources):
        self.resources = resources          # name -> FakeInstrument
        self.opened = []
        self.backend = None
        self.closed = False

    def __call__(self, backend):            # used as the factory
        self.backend = backend
        return self

    def list_resources(self):
        return tuple(self.resources)

    def open_resource(self, name, timeout=None):
        self.opened.append((name, timeout))
        return self.resources[name]

    def close(self):
        self.closed = True


PM_RESOURCE = 'USB0::0x1313::0x8078::P0011748::INSTR'
PAX_RESOURCE = 'USB0::0x1313::0x8031::M01012314::INSTR'


def _pm100(power='1.25E-03', **answers):
    state = {'wav': 633.0, 'zeroing': 0}

    def on_write(command):
        if command.startswith('SENS:CORR:WAV '):
            state['wav'] = min(max(float(command.split()[1]), 400.0), 1100.0)
        if command == 'SENS:CORR:COLL:ZERO:INIT':
            state['zeroing'] = 2

    def zero_state():
        state['zeroing'] = max(0, state['zeroing'] - 1)
        return str(1 if state['zeroing'] else 0)

    base = {
        '*IDN?': 'Thorlabs,PM100D,P0011748,2.4.0',
        'READ?': power,
        'SENS:CORR:WAV?': lambda: f'{state["wav"]:.1f}',
        'SENS:CORR:COLL:ZERO:STAT?': zero_state,
        'SYST:ERR?': '0,"No error"',
    }
    base.update(answers)
    inst = FakeInstrument(base, on_write)
    return inst, FakeResourceManager({PM_RESOURCE: inst, PAX_RESOURCE: FakeInstrument({})})


def _pax_packet(az=0.1, el=-0.05, dop=0.99, power=1e-3):
    return ','.join(['12', '3456.5', '9', '0', '3', '1', '2', '0.01', '0'] +
                    [f'{az}', f'{el}', f'{dop}', f'{power}'])


def _pax(packet=None, **answers):
    state = {'mode': 0, 'wav': 633e-9, 'rot': 0}

    def on_write(command):
        key, _, value = command.partition(' ')
        if key == 'SENS:CALC':
            state['mode'] = int(value)
        elif key == 'SENS:WAV':
            state['wav'] = float(value)
        elif key == 'INP:ROT:STAT':
            state['rot'] = int(value)

    base = {
        '*IDN?': 'Thorlabs,PAX1000IR2/M,M01012314,1.2.3',
        'SENS:CALC?': lambda: str(state['mode']),
        'SENS:WAV?': lambda: f'{state["wav"]:.6E}',
        'SENS:DATA:LAT?': _pax_packet() if packet is None else packet,
    }
    base.update(answers)
    inst = FakeInstrument(base, on_write)
    inst.state = state
    return inst, FakeResourceManager({PAX_RESOURCE: inst})


# --------------------------------------------------------------------- PM100
def test_pm100_finds_its_serial_and_reads_watts():
    inst, rm = _pm100()
    driver = ThorlabsPM100Driver('P0011748', backend='@py', wavelength_nm=775,
                                 resource_manager_factory=rm)
    identity = driver.connect()
    assert (identity.model, identity.serial, identity.firmware) == ('PM100D', 'P0011748', '2.4.0')
    assert rm.backend == '@py' and rm.opened == [(PM_RESOURCE, 2000)]
    assert inst.writes == ['SENS:CORR:WAV 775']
    assert driver.settings() == {'wavelength_nm': 775.0}
    assert driver.read().values == {'power': 1.25e-3}


def test_pm100_wavelength_readback_is_the_applied_value():
    inst, rm = _pm100()
    driver = ThorlabsPM100Driver('P0011748', resource_manager_factory=rm)
    driver.connect()
    assert driver.set_setting('wavelength_nm', 1550) == 1100.0      # clamped by the sensor


def test_pm100_zero_waits_for_completion_and_reports_refusal():
    inst, rm = _pm100()
    driver = ThorlabsPM100Driver('P0011748', resource_manager_factory=rm, sleep=lambda s: None)
    driver.connect()
    driver.run_action('zero')
    assert 'SENS:CORR:COLL:ZERO:INIT' in inst.writes

    inst.answers['SYST:ERR?'] = '-113,"Undefined header"'
    with pytest.raises(RuntimeError, match='Undefined header'):
        driver.run_action('zero')


def test_pm100_zero_that_never_finishes_times_out():
    inst, rm = _pm100(**{'SENS:CORR:COLL:ZERO:STAT?': '1'})
    now = [0.0]

    def sleep(s):
        now[0] += s
    driver = ThorlabsPM100Driver('P0011748', resource_manager_factory=rm,
                                 sleep=sleep, clock=lambda: now[0])
    driver.connect()
    with pytest.raises(RuntimeError, match='did not finish'):
        driver.run_action('zero')


@pytest.mark.parametrize('serial, match', [
    ('NOPE', 'not found'), ('', 'no serial'), ('M01012314', 'not a PM100'),
])
def test_pm100_connect_failures_are_transport_errors(serial, match):
    _, rm = _pm100()
    rm.resources[PAX_RESOURCE] = FakeInstrument({'*IDN?': 'Thorlabs,PAX1000IR2/M,M01012314,1'})
    driver = ThorlabsPM100Driver(serial, resource_manager_factory=rm)
    with pytest.raises(TransportError, match=match):
        driver.connect()
    assert not driver.link.is_open


def test_pm100_unparseable_answer_is_an_invalid_sample_not_a_fault():
    inst, rm = _pm100(power='OVER')
    driver = ThorlabsPM100Driver('P0011748', resource_manager_factory=rm)
    driver.connect()
    with pytest.raises(MalformedReading):
        driver.read()
    inst.unplugged = True
    with pytest.raises(TransportError, match='Timeout'):
        driver.read()


def test_pm100_session_refuses_quantitative_windows_until_timing_is_verified():
    inst, rm = _pm100(power='9.9E37')                 # SCPI overrange marker
    session = InstrumentSession('pm1', ThorlabsPM100Driver('P0011748', resource_manager_factory=rm))
    session.connect()
    assert session.verification is Verification.UNVERIFIED
    with pytest.raises(WindowRefused, match='not verified'):
        session.open_window()
    result = session.sample_window(session.open_window(allow_unverified=True), 2, 1.0)
    assert not result.samples and len(result.invalid) >= 2       # overrange is invalid
    inst.answers['READ?'] = '2.0E-03'
    result = session.sample_window(session.open_window(allow_unverified=True), 3, 1.0)
    assert result.complete and [s.values['power'] for s in result.samples] == [2e-3] * 3
    assert all(s.verification is Verification.UNVERIFIED for s in result.samples)


def test_pyvisa_missing_is_a_transport_error(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_pyvisa(name, *args, **kwargs):
        if name == 'pyvisa':
            raise ImportError('No module named pyvisa')
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', no_pyvisa)
    with pytest.raises(TransportError, match='pyvisa is not installed'):
        ThorlabsPM100Driver('P0011748').connect()


# ------------------------------------------------------------------- PAX1000
def test_pax_connect_sets_mode_9_wavelength_and_starts_rotation():
    inst, rm = _pax()
    driver = ThorlabsPAX1000Driver('M01012314', wavelength_nm=633, resource_manager_factory=rm)
    driver.connect()
    assert inst.writes == ['SENS:CALC 9', 'SENS:WAV 6.33e-07', 'INP:ROT:STAT 1']
    assert driver.settings()['mode'] == 9
    assert driver.settings()['wavelength_nm'] == pytest.approx(633.0)
    assert not driver.unconfirmed_settings
    driver.close()
    assert inst.writes[-1] == 'INP:ROT:STAT 0' and inst.closed


def test_pax_read_takes_fields_9_to_12_and_keeps_the_packet():
    inst, rm = _pax(_pax_packet(az=0.5, el=-0.2, dop=0.97, power=2.5))
    driver = ThorlabsPAX1000Driver('M01012314', power_unit='mW', resource_manager_factory=rm)
    driver.connect()
    values = driver.read().values
    assert values == {'azimuth': 0.5, 'ellipticity': -0.2, 'dop': 0.97,
                      'power': pytest.approx(2.5e-3)}
    assert driver.last_fields[0] == 12.0 and len(driver.last_fields) == 13


@pytest.mark.parametrize('packet', ['1,2,3', '1,2,3,4,5,6,7,8,9,NaN-ish,1,1,1', ''])
def test_pax_malformed_packets_are_invalid_samples(packet):
    inst, rm = _pax(packet)
    driver = ThorlabsPAX1000Driver('M01012314', resource_manager_factory=rm)
    driver.connect()
    with pytest.raises(MalformedReading):
        driver.read()


def test_pax_packet_parser_accepts_mixed_separators():
    assert parse_pax_packet('1, 2;3\t4') == [1.0, 2.0, 3.0, 4.0]


def test_pax_unanswered_readback_keeps_the_commanded_value_and_says_so():
    inst, rm = _pax()
    del inst.answers['SENS:WAV?']
    driver = ThorlabsPAX1000Driver('M01012314', wavelength_nm=780, resource_manager_factory=rm)
    driver.connect()
    assert driver.settings()['wavelength_nm'] == 780.0
    assert driver.unconfirmed_settings == {'wavelength_nm'}


def test_pax_without_wavelength_readback_or_setting_refuses_to_connect():
    inst, rm = _pax()
    del inst.answers['SENS:WAV?']
    driver = ThorlabsPAX1000Driver('M01012314', resource_manager_factory=rm)
    with pytest.raises(TransportError, match='SENS:WAV'):
        driver.connect()
    assert inst.state['rot'] == 0                  # rotation stopped again


def test_pax_session_window_and_usb_removal():
    inst, rm = _pax()
    driver = ThorlabsPAX1000Driver('M01012314', update_bound_s=0.0, update_period_s=0.0,
                                   resource_manager_factory=rm)
    session = InstrumentSession('pax1', driver)
    session.connect()
    assert session.profile.id == 'pax-mode9-update-bound'
    result = session.sample_window(session.open_window(allow_unverified=True), 3, 1.0)
    assert result.complete
    assert result.samples[0].values['azimuth'] == pytest.approx(0.1)

    session.set_setting('mode', 5)                 # out of every profile
    assert session.profile is None
    session.set_setting('mode', 9)

    inst.unplugged = True
    result = session.sample_window(session.open_window(allow_unverified=True), 3, 1.0)
    assert result.cause is WindowCause.TRANSPORT_FAULT
    assert session.faulted


def test_pax_azimuth_out_of_range_is_invalid():
    inst, rm = _pax(_pax_packet(az=math.pi))
    session = InstrumentSession('pax1', ThorlabsPAX1000Driver(
        'M01012314', update_bound_s=0.0, update_period_s=0.0, resource_manager_factory=rm))
    session.connect()
    result = session.sample_window(session.open_window(allow_unverified=True), 1, 0.3)
    assert not result.samples and 'azimuth' in result.invalid[0].reason


def test_pax_power_unit_is_checked():
    with pytest.raises(ValueError, match='power_unit'):
        ThorlabsPAX1000Driver('M01012314', power_unit='dBm')


# ------------------------------------------------------------------ managers
def test_real_instrument_managers_start_absent_without_pyvisa_calls():
    from imswitch.imcontrol.model.devices import DeviceRuntimeMode
    from imswitch.imcontrol.model.managers.instruments.ThorlabsPAX1000Manager import (
        ThorlabsPAX1000Manager,
    )
    from imswitch.imcontrol.model.managers.instruments.ThorlabsPM100Manager import (
        ThorlabsPM100Manager,
    )
    from imswitch.imcontrol.model.SetupInfo import InstrumentInfo

    pm = ThorlabsPM100Manager(InstrumentInfo(
        managerName='ThorlabsPM100Manager', transient=True,
        managerProperties={'serial': 'P0011748', 'wavelengthNm': 775}), 'pm1')
    pax = ThorlabsPAX1000Manager(InstrumentInfo(
        managerName='ThorlabsPAX1000Manager', transient=True,
        managerProperties={'serial': 'M01012314', 'powerUnit': 'mW'}), 'pax1')
    for manager in (pm, pax):
        assert manager.runtimeMode is DeviceRuntimeMode.ABSENT
        assert not manager.session.driver.link.is_open
