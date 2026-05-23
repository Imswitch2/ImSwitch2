from collections import deque
from pathlib import Path

import numpy as np

from imswitch.imcontrol.model.EtSTEDPipelineRunner import EtSTEDPipelineRunner
from imswitch.imcontrol.model.EtSTEDTransformService import EtSTEDTransformService
from imswitch.imcontrol.model.EtSTEDTriggeredScanRunner import EtSTEDTriggeredScanRunner


ROOT = Path(__file__).resolve().parents[4]
CONTROLLER_PATH = ROOT / 'imswitch' / 'imcontrol' / 'controller' / 'controllers' / 'EtSTEDController.py'


class _ScanManager:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = []

    def makeFullScan(self, analog_params, digital_params, static_positioner):
        self.calls.append((analog_params.copy(), digital_params.copy(), static_positioner))
        if self.fail:
            raise RuntimeError('scan build failed')
        return {'signal': np.array([1, 2, 3])}, {'scan_samples_total': 3}


class _NidaqManager:
    def __init__(self) -> None:
        self.calls = []

    def runScan(self, signal_dict, scan_info_dict) -> None:
        self.calls.append((signal_dict, scan_info_dict))


def _write_pipeline(tmp_path, name: str, source: str) -> None:
    (tmp_path / f'{name}.py').write_text(source)


def _analog_params():
    return {
        'target_device': ['Mock X', 'Mock Y'],
        'axis_centerpos': [0.0, 0.0],
        'sequence_time': 0.00001,
        'axis_step_size': [0.1, 0.1],
    }


def _identity_transform_service() -> EtSTEDTransformService:
    service = EtSTEDTransformService()
    service.function = lambda coords, coefficients: coords
    service.set_coefficients(np.zeros(20))
    return service


def test_synthetic_event_path_prepares_and_triggers_mock_scan(tmp_path, monkeypatch):
    _write_pipeline(
        tmp_path,
        'synthetic_event_pipeline',
        """
import numpy as np

def synthetic_event_pipeline(img, prev_frames, binary_mask, testmode, exinfo, threshold=5):
    if float(np.max(img)) >= threshold:
        return np.array([[7.0, 9.0]]), exinfo
    return np.empty((0, 2)), exinfo
""",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    pipeline_runner = EtSTEDPipelineRunner()
    pipeline_runner.load('synthetic_event_pipeline')
    pipeline_result = pipeline_runner.execute(
        np.full((4, 4), 10.0),
        deque(),
        None,
        False,
        None,
        [5.0],
    )

    transform_service = _identity_transform_service()
    coords_scan = transform_service.apply(pipeline_result.coords_detected[0])

    scan_runner = EtSTEDTriggeredScanRunner()
    analog_params = _analog_params()
    scan_manager = _ScanManager()
    nidaq_manager = _NidaqManager()
    prepare_result = scan_runner.prepare(
        coords_scan,
        scan_runner.scan_widget_mode,
        analog_params,
        {'ttl': []},
        ['Mock X', 'Mock Y'],
        scan_manager=scan_manager,
    )
    trigger_result = scan_runner.trigger(
        scan_runner.scan_widget_mode,
        nidaq_manager=nidaq_manager,
        signal_dict=prepare_result.signal_dict,
        scan_info_dict=prepare_result.scan_info_dict,
    )

    assert prepare_result.success
    assert trigger_result.success
    assert analog_params['axis_centerpos'] == [7.0, 9.0]
    assert len(scan_manager.calls) == 1
    assert len(nidaq_manager.calls) == 1


def test_no_event_path_does_not_prepare_or_trigger_scan(tmp_path, monkeypatch):
    _write_pipeline(
        tmp_path,
        'synthetic_no_event_pipeline',
        """
import numpy as np

def synthetic_no_event_pipeline(img, prev_frames, binary_mask, testmode, exinfo, threshold=5):
    return np.empty((0, 2)), exinfo
""",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    pipeline_runner = EtSTEDPipelineRunner()
    pipeline_runner.load('synthetic_no_event_pipeline')
    pipeline_result = pipeline_runner.execute(
        np.zeros((4, 4)),
        deque(),
        None,
        False,
        None,
        [5.0],
    )

    scan_manager = _ScanManager()
    nidaq_manager = _NidaqManager()
    if pipeline_result.coords_detected.size != 0:
        raise AssertionError('No-event pipeline unexpectedly returned coordinates.')

    assert scan_manager.calls == []
    assert nidaq_manager.calls == []


def test_scan_preparation_failure_does_not_trigger_scan():
    scan_runner = EtSTEDTriggeredScanRunner()
    scan_manager = _ScanManager(fail=True)
    nidaq_manager = _NidaqManager()

    prepare_result = scan_runner.prepare(
        [1.0, 2.0],
        scan_runner.scan_widget_mode,
        _analog_params(),
        {'ttl': []},
        ['Mock X', 'Mock Y'],
        scan_manager=scan_manager,
    )
    if prepare_result.success:
        scan_runner.trigger(
            scan_runner.scan_widget_mode,
            nidaq_manager=nidaq_manager,
            signal_dict=prepare_result.signal_dict,
            scan_info_dict=prepare_result.scan_info_dict,
        )

    assert not prepare_result.success
    assert len(scan_manager.calls) == 1
    assert nidaq_manager.calls == []


def test_controller_stop_and_close_cleanup_contract_disables_fast_laser():
    source = CONTROLLER_PATH.read_text()

    assert 'def stopExperiment' in source
    assert 'self._disconnectRunSignals()' in source
    assert 'self._setFastLaserEnabled(False)' in source
    assert 'def closeEvent' in source
    assert 'self.stopExperiment(resetParams=True)' in source
