import ctypes

import pytest

from imswitch.imcontrol.model.interfaces import hamamatsu


class _FakeDcam:
    def __init__(self):
        self.openCalls = 0
        self.closeCalls = 0
        self.initCalls = 0
        self.uninitCalls = 0
        self.discoveredCameras = 1

    def dcam_init(self, _instance, count_ptr, _reserved):
        self.initCalls += 1
        count_ptr._obj.value = self.discoveredCameras
        return hamamatsu.DCAMERR_NOERROR

    def dcam_uninit(self, _instance, _reserved):
        self.uninitCalls += 1
        return hamamatsu.DCAMERR_NOERROR

    def dcam_open(self, handle_ptr, camera_id, _reserved):
        self.openCalls += 1
        handle_ptr._obj.value = 123
        return hamamatsu.DCAMERR_NOERROR

    def dcam_close(self, _handle):
        self.closeCalls += 1
        return hamamatsu.DCAMERR_NOERROR


def test_partial_hamamatsu_construction_closes_open_handle(monkeypatch):
    fake = _FakeDcam()
    monkeypatch.setattr(hamamatsu, 'dcam', fake)
    monkeypatch.setattr(hamamatsu, 'initDcam', lambda: None)
    monkeypatch.setattr(
        hamamatsu.HamamatsuCamera, 'getModelInfo', lambda self, camera_id: b'fake'
    )
    monkeypatch.setattr(
        hamamatsu.HamamatsuCamera, 'checkStatus', lambda self, value, name: value
    )
    monkeypatch.setattr(
        hamamatsu.HamamatsuCamera,
        'getCameraProperties',
        lambda self: (_ for _ in ()).throw(RuntimeError('property discovery failed')),
    )

    with pytest.raises(RuntimeError, match='property discovery failed'):
        hamamatsu.HamamatsuCamera(0)

    assert fake.openCalls == 1
    assert fake.closeCalls == 1


def test_hamamatsu_shutdown_is_idempotent(monkeypatch):
    fake = _FakeDcam()
    monkeypatch.setattr(hamamatsu, 'dcam', fake)
    camera = object.__new__(hamamatsu.HamamatsuCamera)
    camera.camera_handle = ctypes.c_void_p(123)
    camera.checkStatus = lambda value, name: value

    camera.shutdown()
    camera.shutdown()

    assert fake.closeCalls == 1
    assert not camera.camera_handle.value


def test_hamamatsu_shutdown_keeps_handle_when_close_fails(monkeypatch):
    fake = _FakeDcam()
    monkeypatch.setattr(hamamatsu, 'dcam', fake)
    camera = object.__new__(hamamatsu.HamamatsuCamera)
    camera.camera_handle = ctypes.c_void_p(123)

    def check_status(value, name):
        raise hamamatsu.DCAMException('No camera connection')

    camera.checkStatus = check_status

    with pytest.raises(hamamatsu.DCAMException, match='No camera connection'):
        camera.shutdown()

    assert fake.closeCalls == 1
    assert camera.camera_handle.value == 123


def test_reset_dcam_session_single_camera_reenumerates(monkeypatch):
    fake = _FakeDcam()
    monkeypatch.setattr(hamamatsu, 'dcam', fake)
    monkeypatch.setattr(hamamatsu, 'n_cameras', 1)

    discovered = hamamatsu.resetDcamSessionSingleCamera()

    assert discovered == 1
    assert fake.uninitCalls == 1
    assert fake.initCalls == 1
    assert hamamatsu.n_cameras == 1


def test_reset_dcam_session_single_camera_retries_init_after_failed_session(monkeypatch):
    fake = _FakeDcam()
    monkeypatch.setattr(hamamatsu, 'dcam', fake)
    monkeypatch.setattr(hamamatsu, 'n_cameras', -1)

    discovered = hamamatsu.resetDcamSessionSingleCamera()

    assert discovered == 1
    assert fake.uninitCalls == 0
    assert fake.initCalls == 1
    assert hamamatsu.n_cameras == 1

