"""RS232Driver turns setup-file serial settings into pyvisa attributes.

No port is opened: ``getDefaults`` is the whole mapping. The regression these
guard is a setup file whose line ending the device never sees -- the config
editor saved a chosen ``\\r`` as backslash + ``r``, pyvisa sent those two
characters after every command, and every query of an otherwise working
device timed out.
"""

import logging

import pytest

pytest.importorskip("pyvisa")

from pyvisa import constants

from imswitch.imcontrol.model.interfaces.RS232Driver import (
    RS232Driver,
    decodeTermination,
    generateDriverClass,
)


def _settings(**overrides):
    settings = {
        'port': 'ASRL7::INSTR',
        'baudrate': 9600,
        'bytesize': 8,
        'parity': 'none',
        'stopbits': 1,
        'encoding': 'ascii',
        'recv_termination': '\r',
        'send_termination': '\r',
    }
    settings.update(overrides)
    return settings


def test_real_line_endings_pass_through_unchanged(caplog):
    with caplog.at_level(logging.WARNING):
        asrl = RS232Driver.getDefaults(_settings())['ASRL']
    assert asrl['write_termination'] == '\r'
    assert asrl['read_termination'] == '\r'
    assert not caplog.records


@pytest.mark.parametrize('escaped, decoded', [
    ('\\r', '\r'), ('\\n', '\n'), ('\\r\\n', '\r\n'),
])
def test_an_escaped_line_ending_is_decoded_with_a_warning(caplog, escaped, decoded):
    with caplog.at_level(logging.WARNING):
        asrl = RS232Driver.getDefaults(
            _settings(recv_termination=escaped, send_termination=escaped)
        )['ASRL']
    assert asrl['write_termination'] == decoded
    assert asrl['read_termination'] == decoded
    messages = ' '.join(record.getMessage() for record in caplog.records)
    assert 'send_termination' in messages and 'recv_termination' in messages


@pytest.mark.parametrize('value', ['\\x', 'ETX\\r', '\\\\r', '', None, '>'])
def test_anything_else_is_left_alone(value):
    assert decodeTermination(value) is value


@pytest.mark.parametrize('parity, expected', [
    ('none', constants.Parity.none),
    ('even', constants.Parity.even),
    ('odd', constants.Parity.odd),
    ('None', constants.Parity.none),
])
def test_every_parity_the_editor_offers_is_mapped(parity, expected):
    """``even`` and ``odd`` used to hit an unbound local, which the manager
    swallowed by silently substituting a mock port."""
    assert RS232Driver.getDefaults(_settings(parity=parity))['ASRL']['parity'] == expected


@pytest.mark.parametrize('stopbits, expected', [
    (1, constants.StopBits.one),
    ('1', constants.StopBits.one),
    (1.5, constants.StopBits.one_and_a_half),
    (2, constants.StopBits.two),
])
def test_stop_bits_are_mapped(stopbits, expected):
    assert RS232Driver.getDefaults(_settings(stopbits=stopbits))['ASRL']['stop_bits'] == expected


@pytest.mark.parametrize('key, value', [('parity', 'sometimes'), ('stopbits', 3)])
def test_an_unsupported_setting_names_itself(key, value):
    with pytest.raises(ValueError, match=key):
        RS232Driver.getDefaults(_settings(**{key: value}))


def test_generated_driver_carries_the_decoded_line_ending():
    driver = generateDriverClass(_settings(send_termination='\\r'))
    assert driver.DEFAULTS['ASRL']['write_termination'] == '\r'
    assert 'bytesize' not in driver.DEFAULTS['ASRL']
