from types import SimpleNamespace

from imswitch.imcontrol.controller.controllers.ScanControllerAdvanced import (
    ScanControllerAdvanced,
)
from imswitch.imcontrol.model import LaserScanPowerControl


def _controller():
    ctrl = ScanControllerAdvanced.__new__(ScanControllerAdvanced)
    ctrl._setupInfo = SimpleNamespace(
        lasers={
            '561': SimpleNamespace(analogChannel=None, powerDevice='561AOTF'),
            '561AOTF': SimpleNamespace(analogChannel=None, powerDevice=None),
            '488': SimpleNamespace(analogChannel='ao0', powerDevice=None),
            '640': SimpleNamespace(analogChannel='ao1', powerDevice=None),
        }
    )
    return ctrl


def test_advanced_scan_state_reports_participation_power_device_and_ao():
    ctrl = _controller()
    params = {
        'target_device': ['561', '488'],
        'advanced_mode': True,
        'linestep_power_percent': {
            '488': [35.0],
            '640': [20.0],
        },
        'linestep_power_enabled': {
            '488': True,
            '640': True,
        },
    }

    states = ctrl._laserScanStateFromParameters(params)

    assert states['561'].active_for_scan is True
    assert states['561'].power_control is LaserScanPowerControl.DIRECT
    assert states['561AOTF'].active_for_scan is True
    assert states['561AOTF'].power_control is LaserScanPowerControl.DIRECT

    assert states['488'].active_for_scan is True
    assert states['488'].power_control is LaserScanPowerControl.AO

    # AO ownership follows the actual Advanced Scan AO program, while TTL
    # participation remains a separate semantic flag.
    assert states['640'].active_for_scan is False
    assert states['640'].power_control is LaserScanPowerControl.AO


def test_advanced_scan_state_respects_optional_ao_power_toggle_and_legacy_default():
    ctrl = _controller()
    base = {
        'target_device': ['488'],
        'advanced_mode': True,
        'linestep_power_percent': {'488': [35.0]},
    }

    legacy = ctrl._laserScanStateFromParameters(base)
    assert legacy['488'].power_control is LaserScanPowerControl.AO

    disabled = dict(base)
    disabled['linestep_power_enabled'] = {'488': False}
    state = ctrl._laserScanStateFromParameters(disabled)
    assert state['488'].active_for_scan is True
    assert state['488'].power_control is LaserScanPowerControl.DIRECT


def test_advanced_mode_off_means_direct_power_reporting():
    ctrl = _controller()
    state = ctrl._laserScanStateFromParameters({
        'target_device': ['488'],
        'advanced_mode': False,
        'linestep_power_percent': {'488': [35.0]},
        'linestep_power_enabled': {'488': True},
    })

    assert state['488'].active_for_scan is True
    assert state['488'].power_control is LaserScanPowerControl.DIRECT


class _WorkflowRecorder:
    def __init__(self):
        self.published = []

    def publish_laser_scan_state(self, state):
        self.published.append(state)


def test_preview_parameter_rebuild_republishes_laser_scan_state():
    ctrl = _controller()
    workflow = _WorkflowRecorder()
    ctrl._commChannel = SimpleNamespace(scanWorkflow=workflow)
    ctrl.settingParameters = False

    # ``plotSignalGraph`` is the callback wired to Advanced Scan parameter
    # changes. Keep plotting absent here and exercise the real
    # rebuild -> publish path through ``getParameters``.
    ctrl._setupInfo.scan = None
    ctrl._buildAnalogParameterDict = lambda: ({}, [])
    ctrl._nextDigitalParameters = {
        'target_device': ['488'],
        'advanced_mode': True,
        'linestep_power_percent': {'488': [35.0]},
        'linestep_power_enabled': {'488': True},
    }
    ctrl._buildDigitalParameterDict = (
        lambda analog: dict(ctrl._nextDigitalParameters)
    )

    ctrl.plotSignalGraph()

    ctrl._nextDigitalParameters = {
        'target_device': [],
        'advanced_mode': True,
        'linestep_power_percent': {'488': [35.0]},
        'linestep_power_enabled': {'488': False},
    }
    ctrl.plotSignalGraph()

    assert len(workflow.published) == 2
    assert workflow.published[0]['488'].active_for_scan is True
    assert workflow.published[0]['488'].power_control is LaserScanPowerControl.AO
    assert workflow.published[1]['488'].active_for_scan is False
    assert workflow.published[1]['488'].power_control is LaserScanPowerControl.DIRECT
