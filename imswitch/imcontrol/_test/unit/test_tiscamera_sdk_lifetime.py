"""Regression tests for TIS SDK and per-camera resource lifetime.

These tests intentionally replace the vendor DLL modules before importing the
wrapper, so they run without TIS hardware or tisgrabber.dll installed.
"""

import importlib
import sys
import types


_IMAGING_MODULE = (
    'imswitch.imcontrol.model.interfaces.pyicic.IC_ImagingControl'
)
_GRABBER_MODULE = (
    'imswitch.imcontrol.model.interfaces.pyicic.IC_GrabberDLL'
)
_CAMERA_MODULE = (
    'imswitch.imcontrol.model.interfaces.pyicic.IC_Camera'
)
_EXCEPTION_MODULE = (
    'imswitch.imcontrol.model.interfaces.pyicic.IC_Exception'
)


def _load_imaging_control(monkeypatch):
    events = []

    class FakeGrabberDLL:
        @staticmethod
        def init_library(_license):
            events.append('init-library')
            return 1

        @staticmethod
        def release_grabber(handle):
            events.append(('release-grabber', handle))

        @staticmethod
        def close_library():
            events.append('close-library')

    grabber_module = types.ModuleType(_GRABBER_MODULE)
    grabber_module.IC_GrabberDLL = FakeGrabberDLL
    camera_module = types.ModuleType(_CAMERA_MODULE)
    camera_module.IC_Camera = object
    exception_module = types.ModuleType(_EXCEPTION_MODULE)
    exception_module.IC_Exception = RuntimeError

    monkeypatch.setitem(sys.modules, _GRABBER_MODULE, grabber_module)
    monkeypatch.setitem(sys.modules, _CAMERA_MODULE, camera_module)
    monkeypatch.setitem(sys.modules, _EXCEPTION_MODULE, exception_module)
    monkeypatch.delitem(sys.modules, _IMAGING_MODULE, raising=False)

    module = importlib.import_module(_IMAGING_MODULE)
    monkeypatch.setattr(module.atexit, 'register', lambda _func: None)
    module.IC_ImagingControl._library_initialized = False
    module.IC_ImagingControl._atexit_registered = False
    return module.IC_ImagingControl, events


def test_tis_sdk_initializes_only_once_per_process(monkeypatch):
    imaging_control, events = _load_imaging_control(monkeypatch)

    first = imaging_control()
    second = imaging_control()
    first.init_library()
    second.init_library()

    assert events.count('init-library') == 1


def test_release_resources_keeps_process_global_sdk_alive(monkeypatch):
    imaging_control, events = _load_imaging_control(monkeypatch)

    class FakeCamera:
        _handle = 'grabber-1'

        def is_open(self):
            events.append('is-open')
            return True

        def close(self):
            events.append('camera-close')

    wrapper = imaging_control()
    wrapper._devices = {'camera': FakeCamera()}
    wrapper._unique_device_names = ['camera']

    wrapper.release_resources()

    assert events == [
        'is-open',
        'camera-close',
        ('release-grabber', 'grabber-1'),
    ]
    assert wrapper._devices is None
    assert wrapper._unique_device_names is None


def test_shutdown_library_is_process_global_and_idempotent(monkeypatch):
    imaging_control, events = _load_imaging_control(monkeypatch)
    imaging_control._library_initialized = True

    imaging_control.shutdown_library()
    imaging_control.shutdown_library()

    assert events == ['close-library']
    assert imaging_control._library_initialized is False


def _load_camera_tis(monkeypatch):
    pyicic_package_name = 'imswitch.imcontrol.model.interfaces.pyicic'
    tiscamera_module_name = 'imswitch.imcontrol.model.interfaces.tiscamera'

    pyicic_package = importlib.import_module(pyicic_package_name)

    imaging_module = types.ModuleType(_IMAGING_MODULE)
    imaging_module.IC_ImagingControl = object
    camera_module = types.ModuleType(_CAMERA_MODULE)

    class NoFrameAvailable(Exception):
        pass

    camera_module.IC_NoFrameAvailable = NoFrameAvailable
    monkeypatch.setitem(sys.modules, _IMAGING_MODULE, imaging_module)
    monkeypatch.setitem(sys.modules, _CAMERA_MODULE, camera_module)
    monkeypatch.setattr(
        pyicic_package, 'IC_ImagingControl', imaging_module, raising=False
    )
    monkeypatch.delitem(sys.modules, tiscamera_module_name, raising=False)
    return importlib.import_module(tiscamera_module_name).CameraTIS


def test_camera_close_detaches_filter_before_releasing_wrapper(monkeypatch):
    camera_tis = _load_camera_tis(monkeypatch)
    events = []

    class Logger:
        def info(self, _message):
            pass

        def debug(self, _message):
            pass

        def warning(self, _message):
            pass

    class Device:
        def stop_live(self):
            events.append('stop-live')

        def clear_frame_filters_from_device(self):
            events.append('clear-filters')

        def delete_frame_filter(self, handle):
            events.append(('delete-filter', handle))

        def is_open(self):
            return True

        def close(self):
            events.append('device-close')

    class Wrapper:
        def release_resources(self):
            events.append('release-wrapper')

    camera = camera_tis.__new__(camera_tis)
    camera._CameraTIS__logger = Logger()
    camera._closed = False
    camera.model = 'fake-camera'
    camera.cam = Device()
    camera.roi_filter = 'roi-handle'
    camera._ic_ic = Wrapper()

    camera.close()

    assert events == [
        'stop-live',
        'clear-filters',
        ('delete-filter', 'roi-handle'),
        'device-close',
        'release-wrapper',
    ]
    assert camera.roi_filter is None
    assert camera.cam is None
    assert camera._ic_ic is None
