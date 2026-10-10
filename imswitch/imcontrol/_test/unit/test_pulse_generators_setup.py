"""Pulse generators by name (``pulseGenerators``; docs/devices/
pulse_generators.rst, docs/design/plans/scan-engines-and-expert-scan.md
§7.8).

Each entry resolves through the device-plugin registry (kind
``pulse_generator``), so a setup may have several and a plugin's generator
joins like any other device. A scan engine or a laser names the one it
drives; naming none still means the ``teensyPulse`` section's.
"""

import json
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.SetupInfo import LaserInfo, PulseGeneratorInfo, TeensyPulseInfo
from imswitch.imcontrol.model.managers.lasers.PulseGeneratorLaserManager import (
    PulseGeneratorLaserManager,
)
from imswitch.imcontrol.model.managers.pulsegen import PulseStreamerManager, TeensyPulseManager
from imswitch.imcontrol.model.managers.pulsegen.TeensyPulseManager import teensy_info
from imswitch.imcontrol.model.managers.pulsegen.loader import (
    LEGACY_GENERATOR, build_pulse_generators, pulse_generator_named,
)
from imswitch.imcontrol.model.managers.scan_engines.PulseGeneratorScanEngine import (
    PulseGeneratorScanEngine,
)
from imswitch.imcontrol.model.plugins.manifest import DeviceManagerContribution
from imswitch.imcontrol.model.plugins.registry import build_default_registry
from imswitch.imcontrol.model.scan_program.engine import SetupError
from imswitch.imcontrol.view import ViewSetupInfo

#: A Teensy on no port: the in-process mock, with this many channels.
MOCK = {'port': None, 'useMockOnFailure': True}


def _setup(**generators):
    return ViewSetupInfo.from_json(json.dumps({'pulseGenerators': generators}))


def _teensy(**props):
    return {'managerName': 'TeensyPulseManager', 'managerProperties': {**MOCK, **props}}


def test_a_setup_has_two_generators_by_name_through_the_registry():
    built = build_pulse_generators(_setup(widefield=_teensy(mockNChannels=8),
                                          second=_teensy(mockNChannels=4)))
    assert sorted(built.generators) == ['second', 'widefield']
    assert all(isinstance(g, TeensyPulseManager) for g in built.generators.values())
    assert built.generators['widefield'].n_digital_channels == 8
    assert built.generators['second'].n_digital_channels == 4
    assert built.finalize() is not False


def test_an_alias_names_a_builtin_generator_too():
    built = build_pulse_generators(_setup(g={'managerName': 'builtin.teensy-pulse',
                                             'managerProperties': MOCK}))
    assert isinstance(built.generators['g'], TeensyPulseManager)


class _PluginGenerator:
    """What a plugin contributes: built as ``cls(info, name, **lowLevelManagers)``."""

    def __init__(self, info, name, **lowLevelManagers):
        self.info, self.name, self.lowLevel = info, name, lowLevelManagers


def test_a_plugins_generator_is_built_by_its_contribution():
    registry = build_default_registry(discover=False)
    registry.register(DeviceManagerContribution(
        id='FpgaPulses', kind='pulse_generator', display_name='An FPGA',
        python_name=f'{__name__}:_PluginGenerator', plugin_name='imswitch-fpga'))
    built = build_pulse_generators(
        _setup(fpga={'managerName': 'FpgaPulses', 'managerProperties': {'card': 2}}),
        lowLevelManagers={'rs232sManager': 'rs232'}, registry=registry)
    fpga = built.generators['fpga']
    assert isinstance(fpga, _PluginGenerator) and fpga.name == 'fpga'
    assert fpga.info.managerProperties == {'card': 2} and fpga.lowLevel == {'rs232sManager': 'rs232'}


def test_an_unknown_manager_fails_the_setup_with_what_is_installed():
    with pytest.raises(SetupError, match="pulse_generator manager 'NoSuchPulser'"):
        build_pulse_generators(_setup(g={'managerName': 'NoSuchPulser'}))


def test_the_teensypulse_name_cannot_be_reused():
    with pytest.raises(SetupError, match="that name is the teensyPulse section's"):
        build_pulse_generators(_setup(**{LEGACY_GENERATOR: _teensy()}))


# --- the Teensy's managerProperties are the teensyPulse section's ----------------

def test_every_teensypulse_key_is_read_from_managerproperties():
    values = {'port': '/dev/ttyACM9', 'baud': 9600, 'pinMap': {'laser': 3},
              'useMockOnFailure': False, 'mockNChannels': 5, 'mockMinPulseUs': 7,
              'mockMaxSteps': 11}
    assert set(values) == set(TeensyPulseInfo.__dataclass_fields__)
    assert teensy_info('g', values) == TeensyPulseInfo(**values)
    assert teensy_info('g', {}) == TeensyPulseInfo()


def test_a_key_the_teensy_does_not_take_is_refused_by_name():
    with pytest.raises(ValueError, match='g: TeensyPulseManager does not take managerProperties '
                                         'baudrate; it takes port, baud'):
        teensy_info('g', {'baudrate': 9600})


def test_a_pulse_streamer_entry_gives_its_address():
    streamer = PulseStreamerManager(PulseGeneratorInfo('PulseStreamerManager',
                                                       {'ipAddress': '192.0.2.7'}), 'ps')
    assert streamer._PulseStreamerManager__ipAddress == '192.0.2.7'


# --- consumers name the generator they drive ---------------------------------------

def _lowLevel(legacy=True, **named):
    from imswitch.imcontrol.model.managers.pulsegen.loader import PulseGenerators
    return {LEGACY_GENERATOR: TeensyPulseManager(TeensyPulseInfo(**MOCK)) if legacy else None,
            'pulseGenerators': PulseGenerators(named)}


def test_the_lookup_is_by_name_and_the_legacy_one_when_none_is_named():
    second = TeensyPulseManager(TeensyPulseInfo(**MOCK))
    lowLevel = _lowLevel(second=second)
    assert pulse_generator_named(lowLevel, 'second') == (second, ['second', LEGACY_GENERATOR])
    assert pulse_generator_named(lowLevel, None)[0] is lowLevel[LEGACY_GENERATOR]
    assert pulse_generator_named(lowLevel, LEGACY_GENERATOR)[0] is lowLevel[LEGACY_GENERATOR]
    assert pulse_generator_named(lowLevel, 'third')[0] is None


def test_a_scan_engine_drives_the_generator_it_names():
    second = TeensyPulseManager(TeensyPulseInfo(**MOCK, mockNChannels=4))
    engine = PulseGeneratorScanEngine(
        SimpleNamespace(managerProperties={'pulseGenerator': 'second'}), 'pulses',
        **_lowLevel(second=second))
    assert engine._generator is second and engine.owns('3') and not engine.owns('4')


def test_a_scan_engine_naming_a_missing_generator_fails_the_setup_with_the_names():
    with pytest.raises(SetupError, match="no pulse generator 'third'; it has second, "
                                         "pulseGeneratorManager"):
        PulseGeneratorScanEngine(SimpleNamespace(managerProperties={'pulseGenerator': 'third'}),
                                 'pulses', **_lowLevel(second=object()))
    with pytest.raises(SetupError, match="it has none"):
        PulseGeneratorScanEngine(SimpleNamespace(managerProperties={}), 'pulses',
                                 **_lowLevel(legacy=False))


def _laser(**props):
    return LaserInfo(analogChannel=None, digitalLine=2, managerName='PulseGeneratorLaserManager',
                     managerProperties=props, valueRangeMin=0, valueRangeMax=1, wavelength=488)


def test_a_laser_switches_on_the_generator_it_names():
    second = TeensyPulseManager(TeensyPulseInfo(**MOCK))
    lowLevel = _lowLevel(second=second)
    laser = PulseGeneratorLaserManager(_laser(pulseGenerator='second'), 'L488', **lowLevel)
    laser.setEnabled(True)
    assert second.driver.pin_states[2] and not lowLevel[LEGACY_GENERATOR].driver.pin_states[2]


def test_a_laser_naming_a_missing_generator_fails_but_an_unnamed_one_still_mocks():
    with pytest.raises(ValueError, match="L488: the setup has no pulse generator 'third'"):
        PulseGeneratorLaserManager(_laser(pulseGenerator='third'), 'L488', **_lowLevel())
    laser = PulseGeneratorLaserManager(_laser(), 'L488', **_lowLevel(legacy=False))
    assert laser._isMock



# --- through MasterController, as a setup file builds them -------------------------

def test_master_controller_builds_them_hands_them_on_and_closes_them(qtbot):
    from .test_simple_point_scan_controller import SETUP, Rig
    setup = json.loads(open(SETUP).read())
    setup['pulseGenerators'] = {'widefield': _teensy(mockNChannels=8)}
    setup['scanEngines'] = {'sequence': {'managerName': 'PulseGeneratorScanEngine',
                                         'managerProperties': {'pulseGenerator': 'widefield',
                                                               'channels': [5]}}}
    setup['lasers']['Pulsed'] = {'managerName': 'PulseGeneratorLaserManager',
                                 'managerProperties': {'pulseGenerator': 'widefield'},
                                 'digitalLine': 5, 'wavelength': 405,
                                 'valueRangeMin': 0, 'valueRangeMax': 1}
    rig = Rig(json.dumps(setup))
    widefield = rig.master.pulseGenerators.generators['widefield']
    assert rig.master.scanEngines.engines['sequence']._generator is widefield
    assert rig.master.scanEngines.owner_of('5') == 'sequence'
    rig.master.lasersManager['Pulsed'].setEnabled(True)
    assert widefield.driver.pin_states[5]
    finalized = []
    widefield.finalize = lambda: finalized.append(True)
    rig.master.closeEvent()
    assert finalized == [True]

# Copyright (C) 2020-2026 ImSwitch developers
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
