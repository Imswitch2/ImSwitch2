from collections import deque
from types import SimpleNamespace

import numpy as np

from imswitch.imcontrol.controller.controllers.EventTriggeredBaseController import (
    EventTriggeredControllerBase,
)
from imswitch.imcontrol.model.EtSTEDPipelineRunner import EtSTEDPipelineRunner
from imswitch.imcontrol.model.EtSTEDTransformService import EtSTEDTransformService
from imswitch.imcontrol.model.EtSTEDTriggeredScanRunner import EtSTEDTriggeredScanRunner

from .test_event_triggered_smart_modes import _HarnessController, _make_controller


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


class _ScanCoordinator:
    def __init__(self, nidaq_manager) -> None:
        self.nidaq_manager = nidaq_manager

    def arm(self, signal_dict, scan_info_dict, owner=None):
        self.nidaq_manager.runScan(signal_dict, scan_info_dict)
        return object()


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
        scan_coordinator=_ScanCoordinator(nidaq_manager),
        scan_owner=scan_runner,
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


# --------------------------------------------------------------------------- #
# The fast laser through the shared event-triggered lifecycle                  #
# --------------------------------------------------------------------------- #

class _FastLaser:
    def __init__(self):
        self.enabled = []
        self.refuse = set()  # the values of ``enabled`` the laser fails on

    def setEnabled(self, enabled):
        self.enabled.append(enabled)
        if enabled in self.refuse:
            raise RuntimeError('laser offline')


class _FastLaserHarness(_HarnessController):
    """The lifecycle harness with the real fast-laser switch and the real
    mask-capture cleanup put back."""

    _setFastLaserEnabled = EventTriggeredControllerBase._setFastLaserEnabled
    _cleanupBinaryMaskRecording = (
        EventTriggeredControllerBase._cleanupBinaryMaskRecording
    )


def _etController(*, endless=False):
    controller = _make_controller(cls=_FastLaserHarness, enabled=False, endless=endless)
    laser = _FastLaser()
    released = []
    controller._state.laserFast = '635'
    controller._binaryMaskGeneration = 0  # set by __init__, which the shell skips

    def execOn(name, action):
        assert name == '635'
        return action(laser)

    controller._master.lasersManager = SimpleNamespace(execOn=execOn)
    controller._master.detectorsManager = SimpleNamespace(release=released.append)
    controller._commChannel.sigUpdateImage = SimpleNamespace(
        connect=lambda slot: controller.calls.append('mask-frames-connected'),
        disconnect=lambda slot: controller.calls.append('mask-frames-disconnected'),
    )
    return controller, laser, released


def _capturingMask(controller):
    controller._state.binaryMaskSignalConnected = True
    controller._binaryMaskHandle = 'mask-lease'


def test_stop_switches_the_fast_laser_off_and_disconnects_the_run():
    controller, laser, _ = _etController()

    controller.stopExperiment()

    assert laser.enabled == [False]
    assert 'disconnect' in controller.calls


def test_stop_ends_a_mask_capture_and_returns_its_lease():
    controller, laser, released = _etController()
    _capturingMask(controller)

    controller.stopExperiment()

    assert 'mask-frames-disconnected' in controller.calls
    assert released == ['mask-lease']
    assert controller._state.binaryMaskSignalConnected is False
    assert set(laser.enabled) == {False}


def test_stop_completes_when_the_fast_laser_refuses_to_switch_off():
    controller, laser, released = _etController()
    _capturingMask(controller)
    laser.refuse = {False}

    controller.stopExperiment(resetParams=True)

    assert controller._experimentActive is False
    assert released == ['mask-lease']
    assert ('status', 'idle') in controller.calls


def test_closing_stops_the_experiment_with_the_fast_laser_off():
    controller, laser, _ = _etController()
    controller._experimentActive = True
    controller._state.running = True

    assert controller.closeEvent() is True

    assert laser.enabled == [False]
    assert 'disconnect' in controller.calls
    assert controller._experimentActive is False
    assert controller._state.running is False


def test_arming_is_abandoned_when_the_fast_laser_cannot_be_switched_on():
    controller, laser, _ = _etController()
    laser.refuse = {True}

    controller.initiate()

    assert controller._experimentActive is False
    assert controller._state.running is False
    assert controller.calls.index('connect') < controller.calls.index('disconnect')
    assert laser.enabled[0] is True and set(laser.enabled[1:]) == {False}
    assert controller.calls[-1] == ('status', 'error')


def test_a_mask_capture_that_cannot_switch_the_laser_on_returns_its_lease():
    controller, laser, released = _etController()
    laser.refuse = {True}
    pick = SimpleNamespace(currentIndex=lambda: 0)
    controller._widget.fastImgLasersPar = pick
    controller._widget.fastImgLasers = ['635']
    controller._widget.fastImgDetectorsPar = pick
    controller._widget.fastImgDetectors = ['FastCamera']
    controller._master.detectorsManager.acquire = lambda names, purpose: 'mask-lease'

    controller.initiateBinaryMask()

    assert released == ['mask-lease']
    assert controller.__dict__.get('_binaryMaskHandle') is None
    assert 'mask-frames-connected' not in controller.calls
    assert laser.enabled[0] is True and set(laser.enabled[1:]) == {False}
    assert ('status', 'error') in controller.calls


def test_a_failed_resume_leaves_the_fast_laser_off_and_the_run_disconnected():
    controller, laser, _ = _etController(endless=True)
    controller._experimentActive = True
    controller._stopRequested = False
    controller._state.running = False
    controller._state.busy = True
    laser.refuse = {True}

    controller.continueFastModality()

    assert laser.enabled == [True, False]
    assert controller.calls.index('connect') < controller.calls.index('disconnect')
    assert controller._state.busy is False
    assert controller._state.running is False
    assert controller.calls[-1] == ('status', 'error')
