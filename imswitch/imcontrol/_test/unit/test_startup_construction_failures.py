"""A pulse generator, a Time Tagger card or a scan engine that cannot be built
must not stop the application from starting (the "startup never crashes" rule
the device-failure work set for the other devices).

Each is left out and reported like any failed device; what needs it is refused
by name instead of crashing. Each test failed (the MasterController raised)
before the change.
"""
import json

import pytest

from imswitch.imcontrol.controller.CommunicationChannel import CommunicationChannel
from imswitch.imcontrol.controller.MasterController import MasterController
from imswitch.imcontrol.model.devices import DeviceConnectionState
from imswitch.imcontrol.model.errors import ScanDesignRefusedError
from imswitch.imcontrol.model.managers.pulsegen.loader import pulse_generator_named
from imswitch.imcontrol.model.managers.scan_engines.UnavailableScanEngine import (
    UnavailableScanEngine,
)
from imswitch.imcontrol.model.scan_program.engine import ScanEngineRefused
from imswitch.imcontrol.view import ViewSetupInfo

from .test_simple_point_scan_controller import SETUP, _Main

BAD_ENGINE = {'board': {'managerName': 'NoSuchScanEngine', 'managerProperties': {}}}


def _master(**extra):
    data = json.load(open(SETUP))
    data.update(extra)
    setup = ViewSetupInfo.from_json(json.dumps(data), infer_missing=True)
    main = _Main()
    channel = CommunicationChannel(main, setup)
    return MasterController(setup, channel, main._moduleCommChannel)


def _scan(**changes):
    return {**json.load(open(SETUP))['scan'], **changes}


def _statuses(master):
    return {s.name: s for s in master.deviceSupervisor.getAllStatuses()}


def test_a_pulse_generator_that_cannot_be_built_is_reported_and_startup_continues():
    master = _master(pulseGenerators={
        'gen': {'managerName': 'NoSuchPulseGenerator', 'managerProperties': {}}})
    assert master.pulseGenerators.generators == {}
    assert 'gen' in master.pulseGenerators.failures
    status = _statuses(master)['gen']
    assert status.kind == 'pulse_generator'
    assert status.connection is DeviceConnectionState.ERROR
    # a consumer that names it is told which one and why
    found, available = pulse_generator_named({'pulseGenerators': master.pulseGenerators}, 'gen')
    assert found is None and 'did not start' in available[0] and 'gen' in available[0]
    master.closeEvent()


def test_a_pulse_generator_section_that_raises_as_a_whole_leaves_none():
    # the legacy name is the teensyPulse section's: a setup error that used to abort startup
    master = _master(pulseGenerators={
        'pulseGeneratorManager': {'managerName': 'x', 'managerProperties': {}}})
    assert master.pulseGenerators.generators == {}
    assert 'pulseGenerators' in master.startupFailures
    master.closeEvent()


def test_a_time_tagger_card_that_cannot_be_built_is_reported(monkeypatch):
    import imswitch.imcontrol.model.managers.TimeTaggerManager as module

    class _NoCard:
        def __init__(self, *args, **kwargs):
            raise OSError('no card on this computer')

    monkeypatch.setattr(module, 'TimeTaggerManager', _NoCard)
    master = _master(timeTagger={})
    assert master.timeTaggerManager is None
    status = _statuses(master)['Time Tagger']
    assert status.kind == 'time_tagger'
    assert status.connection is DeviceConnectionState.ERROR
    assert 'no card on this computer' in status.details
    master.closeEvent()


def test_a_scan_engine_that_cannot_be_built_refuses_its_scans_by_name():
    master = _master(scanEngines=BAD_ENGINE)
    engine = master.scanEngines.engines['board']
    assert isinstance(engine, UnavailableScanEngine) and 'board' in master.scanEngines.failures
    assert master.scanEngine is not engine, 'the scan panel still runs on the NI-DAQ'
    assert _statuses(master)['board'].connection is DeviceConnectionState.ERROR
    with pytest.raises(ScanDesignRefusedError, match="'board' did not start"):
        engine.run_signals({}, {})
    with pytest.raises(ScanEngineRefused) as refused:
        engine.compile(None, {})
    assert "'board' did not start" in ' '.join(refused.value.report.reasons())
    master.closeEvent()


def test_the_scan_panel_engine_failing_leaves_a_refusing_engine_not_a_crash():
    master = _master(scanEngines=BAD_ENGINE, scan=_scan(engine='board'))
    assert isinstance(master.scanEngine, UnavailableScanEngine)
    with pytest.raises(ScanDesignRefusedError, match="'board' did not start"):
        master.scanEngine.run_signals({}, {})
    master.closeEvent()


def test_an_unreadable_engine_section_leaves_the_nidaq_alone():
    master = _master(scan=_scan(engine='nope'))
    assert list(master.scanEngines.engines) == ['nidaq']
    assert 'scanEngines' in master.startupFailures
    assert 'nope' in _statuses(master)['Scan engines'].details
    master.closeEvent()


def test_the_capability_report_lists_an_engine_that_did_not_start():
    from imswitch.imcontrol.model.managers.scan_engines.capability_report import (
        setup_capabilities,
    )
    master = _master(scanEngines=BAD_ENGINE)
    report = setup_capabilities(master.scanEngines, master._MasterController__setupInfo)
    assert any(section.engine == 'board' for section in report.engines)
    master.closeEvent()
