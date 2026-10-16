"""An integer channel means what its device's manager makes of it (review
2026-10-16): a pulse generator's channel number on a pulse-generator laser,
the legacy NI-DAQ shorthand (``Dev1/port0/line<N>``, ``Dev1/ao<N>``) on every
other device. The engines' ownership read the raw integer and the NI-DAQ
the shorthand, so each got one of the two wrong: an NI-DAQ laser written
``"digitalLine": 1`` was owned by the Teensy engine, and a Teensy laser
written ``"digitalLine": 3`` (as the laser docs show it) was not greyed in
the Advanced panel and was played on Dev1/port0/line3."""
import json

from imswitch.imcontrol.view import ViewSetupInfo
from imswitch.imcontrol.model.managers.pulsegen.TeensyPulseManager import TeensyPulseManager
from imswitch.imcontrol.model.managers.pulsegen.loader import build_pulse_generators
from imswitch.imcontrol.model.managers.scan_engines.loader import build_scan_engines
from imswitch.imcontrol.model.scan_program.engine import (
    device_analog_channel, device_digital_line, setup_channels,
)


class _FakeNidaq:
    sigScanBuilt = sigScanStarted = sigScanDone = sigScanBuildFailed = None


def _setup():
    setup = json.load(open('imswitch/_data/user_defaults/imcontrol_setups/example_sted.json'))
    setup['lasers']['561']['digitalLine'] = 1                  # the NI-DAQ shorthand
    setup['lasers']['PG488'] = {
        'managerName': 'PulseGeneratorLaserManager', 'managerProperties': {},
        'wavelength': 488, 'valueRangeMin': 0, 'valueRangeMax': 5, 'digitalLine': 3,
        'analogChannel': 0,
    }
    setup['teensyPulse'] = {'port': None, 'useMockOnFailure': True}
    setup['scanEngines'] = {'teensy': {'managerName': 'PulseGeneratorScanEngine',
                                       'managerProperties': {}}}
    return ViewSetupInfo.from_json(json.dumps(setup))


def test_each_device_reads_its_integer_as_its_manager_does():
    info = _setup()
    assert device_digital_line(info.lasers['561']) == 'Dev1/port0/line1'
    assert device_digital_line(info.lasers['PG488']) == '3'
    assert device_analog_channel(info.lasers['PG488']) == '0'
    channels = setup_channels(info)
    assert channels['561'].digital == 'Dev1/port0/line1'
    assert channels['PG488'].digital == '3'


def test_ownership_follows_the_reading():
    info = _setup()
    teensy = TeensyPulseManager(info.teensyPulse)
    low = {'pulseGeneratorManager': teensy,
           'pulseGenerators': build_pulse_generators(info, lowLevelManagers={})}
    engines = build_scan_engines(info, nidaqManager=_FakeNidaq(), lowLevelManagers=low)
    channels = setup_channels(info)
    assert engines.owner_of(channels['561'].digital) == 'nidaq'
    assert engines.owner_of(channels['PG488'].digital) == 'teensy'
    assert not engines.engines['nidaq'].owns(channels['PG488'].digital)


def test_the_nidaq_never_plays_a_pulse_generators_channel():
    """At the NI-DAQ boundary, whatever panel built the scan: a pulse
    generator's laser is no target of the NI-DAQ's outputs."""
    from imswitch.imcontrol.model.managers.NidaqManager import NidaqManager
    info = _setup()
    manager = NidaqManager(info)
    try:
        digital = dict(manager._NidaqManager__makeSortedTargets('getDigitalLine'))
        analog = dict(manager._NidaqManager__makeSortedTargets('getAnalogChannel'))
        assert 'PG488' not in digital and 'PG488' not in analog
        assert digital['561'] == 'Dev1/port0/line1'
    finally:
        try:
            manager.finalize()
        except Exception:
            pass


def test_the_advanced_panel_greys_what_its_engine_does_not_play():
    """The Advanced scan plays on the leading engine alone: a Teensy laser,
    and a line a follower owns, are greyed with the reason (the composite
    owned the followers' lines too, so neither was)."""
    from types import SimpleNamespace
    from imswitch.imcontrol.controller.controllers.ScanControllerAdvanced import (
        ScanControllerAdvanced,
    )
    info = _setup()
    teensy = TeensyPulseManager(info.teensyPulse)
    low = {'pulseGeneratorManager': teensy,
           'pulseGenerators': build_pulse_generators(info, lowLevelManagers={})}
    engines = build_scan_engines(info, nidaqManager=_FakeNidaq(), lowLevelManagers=low)
    nidaq = engines.engines['nidaq']
    composite = SimpleNamespace(name='nidaq+followers', leader=nidaq, owns=lambda line: True)
    for engine in (nidaq, composite):
        panel = SimpleNamespace(
            TTLDevices={'561': info.lasers['561'], 'PG488': info.lasers['PG488']},
            _engineForScans=lambda engine=engine: engine,
            ANALOG_ONLY_NOTE=ScanControllerAdvanced.ANALOG_ONLY_NOTE)
        unavailable, _note = ScanControllerAdvanced._ttlAvailability(panel)
        assert list(unavailable) == ['PG488']
        assert 'wired to 3' in unavailable['PG488']
