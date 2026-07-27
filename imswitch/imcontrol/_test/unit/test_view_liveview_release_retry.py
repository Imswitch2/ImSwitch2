"""Focused regressions for live-view frame-stream release retry authority."""

from types import SimpleNamespace

import pytest

from imswitch.imcontrol.controller.controllers.ViewController import (
    ViewController,
)
from imswitch.imcontrol.model.managers import LeasePurpose


class _Logger:
    def error(self, *args, **kwargs):
        pass


class _RetryingDetectorsManager:
    def __init__(self, failures=0):
        self.failures = failures
        self.acquired = []
        self.releaseAttempts = []
        self.released = []

    def acquire(self, detectorNames, purpose):
        handle = object()
        self.acquired.append((tuple(detectorNames), purpose, handle))
        return handle

    def release(self, handle):
        self.releaseAttempts.append(handle)
        if self.failures:
            self.failures -= 1
            raise TimeoutError('frame poller is still stopping')
        self.released.append(handle)


def _controller(manager, handle):
    controller = ViewController.__new__(ViewController)
    controller._master = SimpleNamespace(detectorsManager=manager)
    controller._logger = _Logger()
    controller._acqHandle = handle
    controller._acqReleasePending = False
    controller._closed = False
    controller._liveViewDetectors = lambda: ['CAM']
    return controller


def test_liveview_disable_retains_handle_when_poller_stop_times_out():
    manager = _RetryingDetectorsManager(failures=1)
    handle = object()
    controller = _controller(manager, handle)

    with pytest.raises(RuntimeError, match='still stopping'):
        controller.liveview(False)

    assert controller._acqHandle is handle
    assert controller._acqReleasePending is True
    assert manager.releaseAttempts == [handle]
    assert manager.released == []


def test_liveview_reenable_releases_pending_handle_before_reacquiring():
    manager = _RetryingDetectorsManager(failures=1)
    oldHandle = object()
    controller = _controller(manager, oldHandle)

    with pytest.raises(RuntimeError, match='still stopping'):
        controller.liveview(False)
    controller.liveview(True)

    assert manager.releaseAttempts == [oldHandle, oldHandle]
    assert manager.released == [oldHandle]
    assert len(manager.acquired) == 1
    detectorNames, purpose, newHandle = manager.acquired[0]
    assert detectorNames == ('CAM',)
    assert purpose is LeasePurpose.LIVE_VIEW
    assert controller._acqHandle is newHandle
    assert controller._acqReleasePending is False


def test_liveview_shutdown_checker_retries_exact_handle_until_released():
    manager = _RetryingDetectorsManager(failures=1)
    handle = object()
    controller = _controller(manager, handle)
    controller._closed = True

    assert controller.shutdownComplete() is False
    assert controller._acqHandle is handle

    assert controller.shutdownComplete() is True
    assert controller._acqHandle is None
    assert manager.releaseAttempts == [handle, handle]
    assert manager.released == [handle]


def test_closed_liveview_controller_cannot_reacquire():
    manager = _RetryingDetectorsManager()
    controller = _controller(manager, None)
    controller._closed = True

    controller.liveview(True)

    assert manager.acquired == []
