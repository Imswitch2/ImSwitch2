from contextlib import contextmanager

from imswitch.imcontrol.model.SetupInfo import DetectorInfo
from imswitch.imcontrol.model.devices import (
    DeviceConnectionState, DeviceRuntimeMode, HardwareDeviceId,
)
from imswitch.imcontrol.model.interfaces.hamamatsu_mock import MockHamamatsu
from imswitch.imcontrol.model.managers.detectors.HamamatsuManager import (
    HamamatsuManager,
)


class _FakeCamera:
    def __init__(self, model, width=2048, height=2048):
        self.camera_model = model.encode()
        self.max_width = width
        self.max_height = height
        self.properties = {
            'image_width': width,
            'image_height': height,
            'image_framebytes': width * height * 2,
            'subarray_hpos': 0,
            'subarray_vpos': 0,
            'subarray_hsize': width,
            'subarray_vsize': height,
            'subarray_mode': b'OFF',
            'exposure_time': 0.01,
            'internal_frame_interval': 0.01,
            'timing_readout_time': 0.001,
            'internal_frame_rate': 100.0,
            'trigger_source': 1,
            'trigger_mode': 1,
            'binning': 1,
            'readout_speed': 1,
        }
        self.shutdownCalled = False
        self.started = False

    def getPropertyValue(self, name):
        return self.properties[name], 'REAL'

    def setPropertyValue(self, name, value):
        if name == 'subarray_mode':
            self.properties[name] = value
            return value
        if name == 'binning':
            if isinstance(value, bytes):
                value = int(value.decode().split('x', 1)[0])
            self.properties[name] = value
            return value
        self.properties[name] = value
        return value

    def startAcquisition(self):
        self.started = True

    def stopAcquisition(self):
        self.started = False

    def shutdown(self):
        self.shutdownCalled = True

    def updateIndices(self):
        pass

    def getLast(self):
        return None

    def getFrames(self):
        return [], (self.properties['subarray_hsize'], self.properties['subarray_vsize'])


class _LifecycleHost:
    def __init__(self):
        self.maintenanceCalls = 0
        self.clearedFaults = []

    @contextmanager
    def detectorLifecycleMaintenance(self, detectorName):
        self.maintenanceCalls += 1
        yield

    def clearFaultAfterHardwareReplacement(self, detectorName):
        self.clearedFaults.append(detectorName)


def _info(camera_id=0, *, width=2048, height=2048):
    return DetectorInfo(
        analogChannel=None,
        digitalLine=None,
        managerName='HamamatsuManager',
        managerProperties={
            'cameraListIndex': camera_id,
            'hamamatsu': {
                'subarray_hsize': width,
                'subarray_vsize': height,
            },
            'cameraPixelSizeUm': 0.15,
        },
        forAcquisition=True,
        forFocusLock=False,
    )


def test_hamamatsu_mock_accepts_configured_sensor_size():
    camera = MockHamamatsu(width=2304, height=2304)

    assert camera.max_width == 2304
    assert camera.max_height == 2304
    assert camera.getPropertyValue('image_width')[0] == 2304
    assert camera.getPropertyValue('image_height')[0] == 2304
    assert camera.getPropertyValue('subarray_hsize')[0] == 2304
    assert camera.getPropertyValue('subarray_vsize')[0] == 2304


def test_hamamatsu_reconnect_replays_runtime_state_and_updates_sensor(monkeypatch):
    startup = _FakeCamera('startup', 2048, 2048)
    replacement = _FakeCamera('replacement', 2304, 2304)
    cameras = [startup, replacement]

    def get_camera(manager, _camera_id):
        camera = cameras.pop(0)
        manager._setConnected(f'{manager._cameraModel(camera)} connected')
        return camera

    monkeypatch.setattr(HamamatsuManager, '_getCameraObj', get_camera)
    manager = HamamatsuManager(_info(), 'Orca')
    host = _LifecycleHost()
    manager._bindDetectorLifecycleHost(host, 'Orca')

    manager.setParameter('Set exposure time', 0.05)
    manager.setParameter('Trigger source', 'External "frame-trigger"')
    manager.setBinning(2)
    manager.crop(100, 120, 400, 304)
    result = manager.setAdvancedProperty('readout_speed', 7)
    assert result['success'] is True

    reconnect = manager.getDeviceLifecycle().reconnect()

    assert reconnect.success is True
    assert reconnect.hardware_id == HardwareDeviceId('detector', 'detector:Orca')
    assert startup.shutdownCalled is True
    assert manager._camera is replacement
    assert manager.model == 'replacement'
    assert manager.fullShape == (2304, 2304)
    assert manager.frameStart == (100, 120)
    assert manager.shape == (400, 304)
    assert replacement.properties['exposure_time'] == 0.05
    assert replacement.properties['trigger_source'] == 2
    assert replacement.properties['trigger_mode'] == 1
    assert replacement.properties['binning'] == 2
    assert replacement.properties['readout_speed'] == 7
    assert replacement.started is False
    assert host.clearedFaults == ['Orca']


def test_hamamatsu_full_frame_maps_to_new_real_sensor_size(monkeypatch):
    startup = _FakeCamera('startup', 2048, 2048)
    replacement = _FakeCamera('replacement', 2304, 2304)
    cameras = [startup, replacement]

    def get_camera(manager, _camera_id):
        camera = cameras.pop(0)
        manager._setConnected('connected')
        return camera

    monkeypatch.setattr(HamamatsuManager, '_getCameraObj', get_camera)
    manager = HamamatsuManager(_info(), 'Orca')
    host = _LifecycleHost()
    manager._bindDetectorLifecycleHost(host, 'Orca')

    reconnect = manager.getDeviceLifecycle().reconnect()

    assert reconnect.success is True
    assert manager.fullShape == (2304, 2304)
    assert manager.frameStart == (0, 0)
    assert manager.shape == (2304, 2304)


def test_hamamatsu_reconnect_keeps_real_to_mock_fallback_semantics(monkeypatch):
    startup = _FakeCamera('startup')
    fallback = _FakeCamera('mock')
    calls = 0

    def get_camera(manager, _camera_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            manager._setConnected('camera connected')
            return startup
        manager._setConnectionError(
            RuntimeError('camera not found'),
            summary='Hamamatsu camera initialization failed; mock fallback active',
            mock_active=True,
        )
        return fallback

    monkeypatch.setattr(HamamatsuManager, '_getCameraObj', get_camera)
    manager = HamamatsuManager(_info(), 'Orca')
    host = _LifecycleHost()
    manager._bindDetectorLifecycleHost(host, 'Orca')

    result = manager.getDeviceLifecycle().reconnect()

    assert result.success is False
    assert 'mock fallback active' in result.summary
    assert manager._camera is fallback
    assert manager.runtimeMode is DeviceRuntimeMode.MOCK
    assert manager.connectionState is DeviceConnectionState.ERROR
    assert host.clearedFaults == ['Orca']


def test_hamamatsu_reconnect_can_recover_real_after_mock_fallback(monkeypatch):
    startup = _FakeCamera('startup')
    disconnected = _FakeCamera('disconnected')
    replacement = _FakeCamera('replacement')

    def get_camera(manager, _camera_id):
        manager._setConnected('startup connected')
        return startup

    monkeypatch.setattr(HamamatsuManager, '_getCameraObj', get_camera)
    manager = HamamatsuManager(_info(width=2304, height=2304), 'Orca')
    host = _LifecycleHost()
    manager._bindDetectorLifecycleHost(host, 'Orca')

    cameras = [disconnected, replacement]
    monkeypatch.setattr(manager, '_makeRealCamera', lambda: cameras.pop(0))

    original_configure = manager._configureReplacementCamera

    def configure(camera, **kwargs):
        if camera is disconnected:
            raise RuntimeError('property replay failed')
        return original_configure(camera, **kwargs)

    monkeypatch.setattr(manager, '_configureReplacementCamera', configure)

    first = manager.getDeviceLifecycle().reconnect()

    assert first.success is False
    assert isinstance(manager._camera, MockHamamatsu)
    assert manager.runtimeMode is DeviceRuntimeMode.MOCK
    assert manager.connectionState is DeviceConnectionState.ERROR
    assert disconnected.shutdownCalled is True

    second = manager.getDeviceLifecycle().reconnect()

    assert second.success is True
    assert manager._camera is replacement
    assert manager.runtimeMode is DeviceRuntimeMode.REAL
    assert manager.connectionState is DeviceConnectionState.CONNECTED


def test_hamamatsu_reconnect_configuration_failure_installs_mock(monkeypatch):
    startup = _FakeCamera('startup')
    stale = _FakeCamera('stale')

    def get_camera(manager, _camera_id):
        manager._setConnected('startup connected')
        return startup

    monkeypatch.setattr(HamamatsuManager, '_getCameraObj', get_camera)
    manager = HamamatsuManager(_info(width=2304, height=2304), 'Orca')
    host = _LifecycleHost()
    manager._bindDetectorLifecycleHost(host, 'Orca')

    monkeypatch.setattr(manager, '_makeRealCamera', lambda: stale)
    original_configure = manager._configureReplacementCamera
    def configure(camera, **kwargs):
        if camera is stale:
            raise RuntimeError('property replay failed')
        return original_configure(camera, **kwargs)
    monkeypatch.setattr(manager, '_configureReplacementCamera', configure)


    result = manager.getDeviceLifecycle().reconnect()

    assert result.success is False
    assert isinstance(manager._camera, MockHamamatsu)
    assert manager.runtimeMode is DeviceRuntimeMode.MOCK
    assert manager.connectionState is DeviceConnectionState.ERROR
    assert manager.fullShape == (2304, 2304)
    assert host.clearedFaults == ['Orca']

    # Regression: a failed physical reconnect must still leave a usable backend.
    manager.startAcquisition()
    manager.stopAcquisition()


def test_hamamatsu_noconnection_marks_session_stale_and_resets_next_reconnect(monkeypatch):
    startup = _FakeCamera('startup')
    disconnected = _FakeCamera('disconnected')
    replacement = _FakeCamera('replacement')

    monkeypatch.setattr(
        HamamatsuManager,
        '_getCameraObj',
        lambda manager, _camera_id: startup,
    )
    manager = HamamatsuManager(_info(), 'Orca')
    host = _LifecycleHost()
    manager._bindDetectorLifecycleHost(host, 'Orca')

    cameras = [disconnected, replacement]
    monkeypatch.setattr(manager, '_makeRealCamera', lambda: cameras.pop(0))
    original_configure = manager._configureReplacementCamera

    def configure(camera, **kwargs):
        if camera is disconnected:
            raise RuntimeError(
                'dcam error 0x80000F07: No camera connection!'
            )
        return original_configure(camera, **kwargs)

    monkeypatch.setattr(manager, '_configureReplacementCamera', configure)

    first = manager.getDeviceLifecycle().reconnect()

    assert first.success is False
    assert isinstance(manager._camera, MockHamamatsu)
    assert manager._dcamSessionStale is True
    assert startup.shutdownCalled is True
    assert disconnected.shutdownCalled is True
    assert 'restart' in first.summary.lower()

    resetCalls = []

    def reset_stale_session():
        resetCalls.append(True)
        manager._dcamSessionStale = False
        return 1

    monkeypatch.setattr(
        manager, '_resetStaleDcamSessionSingleCamera', reset_stale_session
    )

    second = manager.getDeviceLifecycle().reconnect()

    assert second.success is True
    assert resetCalls == [True]
    assert manager._dcamSessionStale is False
    assert manager._camera is replacement


def test_hamamatsu_single_camera_reset_refuses_multiple_hamamatsu_managers(monkeypatch):
    startup = _FakeCamera('startup')
    monkeypatch.setattr(
        HamamatsuManager,
        '_getCameraObj',
        lambda manager, _camera_id: startup,
    )
    manager = HamamatsuManager(_info(), 'Orca')
    manager._dcamSessionStale = True
    monkeypatch.setattr(
        manager, '_configuredHamamatsuManagerNames',
        lambda: ['Orca', 'Orca2'],
    )

    import pytest
    with pytest.raises(RuntimeError, match='exactly one Hamamatsu camera'):
        manager._resetStaleDcamSessionSingleCamera()

    assert manager._dcamSessionStale is True
