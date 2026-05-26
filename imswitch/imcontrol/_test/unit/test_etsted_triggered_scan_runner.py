import numpy as np
import pytest

from imswitch.imcontrol.model.EtSTEDTriggeredScanRunner import EtSTEDTriggeredScanRunner


class _Signal:
    def __init__(self) -> None:
        self.emitted = []

    def emit(self, *args) -> None:
        self.emitted.append(args)


class _CommChannel:
    def __init__(self) -> None:
        self.sigRequestScanFreq = _Signal()
        self.sigSetAxisCenters = _Signal()
        self.sigStartRecordingExternal = _Signal()


class _ScanWorkflow:
    def __init__(self) -> None:
        self.request_scan_frequency_calls = 0
        self.axis_centers = []
        self.start_external_recording_calls = 0

    def request_scan_frequency(self) -> None:
        self.request_scan_frequency_calls += 1

    def set_axis_centers(self, devices, centers) -> None:
        self.axis_centers.append((devices, centers))

    def start_external_recording(self) -> None:
        self.start_external_recording_calls += 1


class _ScanManager:
    def __init__(self) -> None:
        self.calls = []

    def makeFullScan(self, analog_params, digital_params, static_positioner):
        self.calls.append((analog_params.copy(), digital_params.copy(), static_positioner))
        return {'analog': np.array([1])}, {'scan_samples_total': 10}


class _NidaqManager:
    def __init__(self) -> None:
        self.calls = []

    def runScan(self, signal_dict, scan_info_dict) -> None:
        self.calls.append((signal_dict, scan_info_dict))


class _Positioner:
    def __init__(self) -> None:
        self.calls = []

    def setPosition(self, position, axis) -> None:
        self.calls.append((position, axis))


def _analog_params():
    return {
        'target_device': ['StageY', 'StageX', 'None', 'PiezoZ'],
        'axis_centerpos': [0.0, 0.0, 0.0, 1.0],
        'sequence_time': 0.00001,
        'axis_step_size': [0.1, 0.1, 1.0, 1.0],
    }


def test_prepare_scan_widget_maps_event_coords_by_loaded_scan_axis_order():
    runner = EtSTEDTriggeredScanRunner()
    analog_params = _analog_params()
    scan_manager = _ScanManager()
    positioners = {'PiezoZ': _Positioner()}

    result = runner.prepare(
        [12.0, 34.0],
        runner.scan_widget_mode,
        analog_params,
        {'ttl': []},
        ['StageY', 'StageX'],
        scan_manager=scan_manager,
        positioners_manager=positioners,
    )

    assert result.success
    assert analog_params['axis_centerpos'] == [12.0, 34.0, 0.0, 1.0]
    assert scan_manager.calls[0][2] is False
    np.testing.assert_array_equal(result.signal_dict['analog'], np.array([1]))
    assert result.scan_info_dict == {'scan_samples_total': 10}
    assert positioners['PiezoZ'].calls == [(1.0, 0)]


def test_prepare_recording_widget_requests_frequency_and_updates_centers():
    runner = EtSTEDTriggeredScanRunner()
    analog_params = _analog_params()
    comm_channel = _CommChannel()

    result = runner.prepare(
        [2.0, 3.0],
        runner.recording_widget_mode,
        analog_params,
        {'ttl': []},
        ['StageY', 'StageX'],
        comm_channel=comm_channel,
    )

    assert result.success
    assert comm_channel.sigRequestScanFreq.emitted == [()]
    assert comm_channel.sigSetAxisCenters.emitted == [
        (['StageY', 'StageX', 'None', 'PiezoZ'], [2.0, 3.0, 0.0, 1.0])
    ]


def test_prepare_recording_widget_can_use_scan_workflow_service():
    runner = EtSTEDTriggeredScanRunner()
    analog_params = _analog_params()
    scan_workflow = _ScanWorkflow()

    result = runner.prepare(
        [2.0, 3.0],
        runner.recording_widget_mode,
        analog_params,
        {'ttl': []},
        ['StageY', 'StageX'],
        scan_workflow=scan_workflow,
    )

    assert result.success
    assert scan_workflow.request_scan_frequency_calls == 1
    assert scan_workflow.axis_centers == [
        (['StageY', 'StageX', 'None', 'PiezoZ'], [2.0, 3.0, 0.0, 1.0])
    ]


def test_prepare_validates_loaded_scan_parameters():
    runner = EtSTEDTriggeredScanRunner()

    with pytest.raises(ValueError, match='No analog scan parameters'):
        runner.validate_scan_parameters({}, {}, [])


def test_trigger_scan_widget_runs_prepared_scan():
    runner = EtSTEDTriggeredScanRunner()
    nidaq_manager = _NidaqManager()

    result = runner.trigger(
        runner.scan_widget_mode,
        nidaq_manager=nidaq_manager,
        signal_dict={'signal': 1},
        scan_info_dict={'info': 2},
    )

    assert result.success
    assert nidaq_manager.calls == [({'signal': 1}, {'info': 2})]


def test_trigger_recording_widget_emits_external_recording_signal():
    runner = EtSTEDTriggeredScanRunner()
    comm_channel = _CommChannel()

    result = runner.trigger(runner.recording_widget_mode, comm_channel=comm_channel)

    assert result.success
    assert comm_channel.sigStartRecordingExternal.emitted == [()]


def test_trigger_recording_widget_can_use_scan_workflow_service():
    runner = EtSTEDTriggeredScanRunner()
    scan_workflow = _ScanWorkflow()

    result = runner.trigger(runner.recording_widget_mode, scan_workflow=scan_workflow)

    assert result.success
    assert scan_workflow.start_external_recording_calls == 1
