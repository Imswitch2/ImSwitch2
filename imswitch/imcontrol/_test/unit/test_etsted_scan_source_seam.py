"""The seam between etSTED and the scan panel that builds the triggered scan."""
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.controller.basecontrollers import SuperScanController
from imswitch.imcontrol.controller.controllers.ScanControllerExpert import ScanControllerExpert
from imswitch.imcontrol.model import ScanDesignRefusedError
from imswitch.imcontrol.model.EtSTEDTriggeredScanRunner import EtSTEDTriggeredScanRunner


def _params():
    return ({'target_device': ['X', 'Y'], 'axis_centerpos': [0.0, 0.0]}, {'target_device': []}, [])


class _Manager:
    def __init__(self):
        self.calls = []

    def makeFullScan(self, analog, digital, static):
        self.calls.append((analog['axis_centerpos'][:], digital, static))
        return {'from': 'manager'}, {}


class _Source:
    def __init__(self):
        self.calls = []

    def buildTriggeredScanSignals(self, analog, digital):
        self.calls.append(analog['axis_centerpos'][:])
        return {'from': 'source'}, {'i': 1}


def test_the_source_builds_the_triggered_scan_with_the_event_as_centre():
    analog, digital, positioners = _params()
    source, manager = _Source(), _Manager()
    result = EtSTEDTriggeredScanRunner().prepare(
        [3.0, 4.0], 'ScanWidget', analog, digital, positioners,
        scan_manager=manager, scan_source=source)
    assert result.success and result.signal_dict == {'from': 'source'}
    assert source.calls == [[3.0, 4.0]] and manager.calls == []


def test_without_a_source_the_scan_manager_builds_it_as_before():
    analog, digital, positioners = _params()
    manager = _Manager()
    result = EtSTEDTriggeredScanRunner().prepare(
        [3.0, 4.0], 'ScanWidget', analog, digital, positioners, scan_manager=manager)
    assert result.success and result.signal_dict == {'from': 'manager'}
    assert manager.calls == [([3.0, 4.0], digital, False)]


def test_a_source_that_refuses_the_design_fails_the_preparation_with_its_reason():
    class Refusing:
        def buildTriggeredScanSignals(self, analog, digital):
            raise ScanDesignRefusedError('outside the scanner range')

    analog, digital, positioners = _params()
    result = EtSTEDTriggeredScanRunner().prepare(
        [3.0, 4.0], 'ScanWidget', analog, digital, positioners, scan_source=Refusing())
    assert not result.success and 'outside the scanner range' in result.message


def test_the_base_default_is_the_scan_managers_moving_axis_design():
    manager = _Manager()
    fake_panel = SimpleNamespace(_master=SimpleNamespace(scanManager=manager))
    analog, digital, _ = _params()
    signals, _info = SuperScanController.buildTriggeredScanSignals(fake_panel, analog, digital)
    assert signals == {'from': 'manager'}
    assert manager.calls == [([0.0, 0.0], digital, False)]


def test_the_expert_panel_is_refused_with_an_explanation():
    with pytest.raises(ScanDesignRefusedError, match='Expert'):
        ScanControllerExpert.buildTriggeredScanSignals(None, {}, {})
