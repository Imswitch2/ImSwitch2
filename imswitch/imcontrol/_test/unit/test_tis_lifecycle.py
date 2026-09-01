from contextlib import contextmanager

from imswitch.imcontrol.model.SetupInfo import DetectorInfo
from imswitch.imcontrol.model.devices import (
    DeviceConnectionState, DeviceRuntimeMode, HardwareDeviceId,
)
from imswitch.imcontrol.model.managers.detectors.TISManager import TISManager


class _FakeCamera:
    def __init__(self, model):
        self.model = model
        self.properties = {
            'image_width': 640,
            'image_height': 480,
            'exposure': 0,
            'gain': 0,
            'brightness': 0,
        }
        self.roi = None
        self.closed = False
        self.propertiesGuiCalls = 0

    def setPropertyValue(self, name, value):
        self.properties[name] = value
        return value

    def getPropertyValue(self, name):
        return self.properties[name]

    def setROI(self, hpos, vpos, hsize, vsize):
        self.roi = (hpos, vpos, hsize, vsize)

    def start_live(self):
        pass

    def stop_live(self):
        pass

    def suspend_live(self):
        pass

    def openPropertiesGUI(self):
        self.propertiesGuiCalls += 1

    def grabFrame(self):
        return None

    def close(self):
        self.closed = True


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


def _info(camera_id=0):
    return DetectorInfo(
        analogChannel=None,
        digitalLine=None,
        managerName='TISManager',
        managerProperties={
            'cameraListIndex': camera_id,
            'tis': {
                'image_width': 640,
                'image_height': 480,
                'exposure': 12,
                'gain': 2,
                'brightness': 3,
            },
            'cameraPixelSizeUm': 0.2,
        },
        forAcquisition=True,
        forFocusLock=False,
    )


def test_tis_reconnect_replaces_backend_and_replays_runtime_state(monkeypatch):
    startup = _FakeCamera('startup-camera')
    replacement = _FakeCamera('replacement-camera')
    cameras = [startup, replacement]

    def get_camera(manager, _camera_id):
        camera = cameras.pop(0)
        manager._setConnected(f'{camera.model} connected')
        return camera

    monkeypatch.setattr(TISManager, '_getTISObj', get_camera)
    manager = TISManager(_info(), 'WidefieldCamera')
    host = _LifecycleHost()
    manager._bindDetectorLifecycleHost(host, 'WidefieldCamera')

    manager.setParameter('exposure', 25)
    manager.setParameter('gain', 4)
    manager.setParameter('brightness', 5)
    manager.crop(11, 13, 320, 240)

    result = manager.getDeviceLifecycle().reconnect()

    assert result.success is True
    assert result.hardware_id == HardwareDeviceId(
        'detector', 'detector:WidefieldCamera'
    )
    assert startup.closed is True
    assert manager._camera is replacement
    assert manager.model == 'replacement-camera'
    assert replacement.properties['exposure'] == 25
    assert replacement.properties['gain'] == 4
    assert replacement.properties['brightness'] == 5
    assert replacement.roi == (11, 13, 320, 240)
    assert manager._running is False
    assert host.clearedFaults == ['WidefieldCamera']

    # The action must resolve through the manager, not the retired startup
    # camera object captured at construction time.
    manager.actions['More properties'].func()
    assert startup.propertiesGuiCalls == 0
    assert replacement.propertiesGuiCalls == 1


def test_tis_reconnect_keeps_startup_real_to_mock_fallback_semantics(monkeypatch):
    startup = _FakeCamera('startup-camera')
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
            summary='TIS camera initialization failed; mock fallback active',
            mock_active=True,
        )
        return fallback

    monkeypatch.setattr(TISManager, '_getTISObj', get_camera)
    manager = TISManager(_info(), 'WidefieldCamera')
    host = _LifecycleHost()
    manager._bindDetectorLifecycleHost(host, 'WidefieldCamera')

    result = manager.getDeviceLifecycle().reconnect()

    assert result.success is False
    assert 'mock fallback active' in result.summary
    assert manager._camera is fallback
    assert manager.runtimeMode is DeviceRuntimeMode.MOCK
    assert manager.connectionState is DeviceConnectionState.ERROR
    assert host.clearedFaults == ['WidefieldCamera']
    assert manager._running is False


def test_tis_reconnect_failure_keeps_fault_clear_deferred(monkeypatch):
    startup = _FakeCamera('startup-camera')
    calls = 0

    def get_camera(manager, _camera_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            manager._setConnected('camera connected')
            return startup
        raise RuntimeError('backend construction failed')

    monkeypatch.setattr(TISManager, '_getTISObj', get_camera)
    manager = TISManager(_info(), 'WidefieldCamera')
    host = _LifecycleHost()
    manager._bindDetectorLifecycleHost(host, 'WidefieldCamera')

    result = manager.getDeviceLifecycle().reconnect()

    assert result.success is False
    assert host.clearedFaults == []
    assert startup.closed is True
    assert manager._camera is None
    assert manager.connectionState is DeviceConnectionState.ERROR
