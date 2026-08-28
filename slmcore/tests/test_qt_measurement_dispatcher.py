import threading

import numpy as np
import pytest


pytest.importorskip("qtpy")
pytest.importorskip("pyqtgraph")
try:
    from qtpy import QtWidgets
except Exception as error:
    pytest.skip(f"Qt bindings are unavailable: {error}",allow_module_level=True)

from slmcore.core.measurement import ImageMeasurement
from slmcore.qt.application.measurement_dispatcher import QtMeasurementDispatcher


def _app():
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])
    return app


class _Handle:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class _ThreadedProvider:
    def __init__(self):
        self.thread = None

    def available_sources(self,section_key):
        return ("Camera",)

    def preferred_source(self,section_key,available):
        return available[0] if available else None

    def acquire(self,section_key,source,*,metadata,on_result,on_error):
        handle = _Handle()
        measurement = ImageMeasurement(
            np.zeros((4,4)),source=source,metadata=metadata,
        )
        self.thread = threading.Thread(target=lambda:on_result(measurement))
        self.thread.start()
        return handle


class _ImmediateProvider:
    def __init__(self):
        self.handle = _Handle()

    def available_sources(self,section_key):
        return ("Camera",)

    def preferred_source(self,section_key,available):
        return available[0] if available else None

    def acquire(self,section_key,source,*,metadata,on_result,on_error):
        on_result(ImageMeasurement(np.zeros((4,4)),source=source))
        return self.handle


class _DeferredProvider:
    def __init__(self):
        self.handle = _Handle()
        self.on_result = None
        self.on_error = None

    def available_sources(self,section_key):
        return ("Camera",)

    def preferred_source(self,section_key,available):
        return available[0] if available else None

    def acquire(self,section_key,source,*,metadata,on_result,on_error):
        self.on_result = on_result
        self.on_error = on_error
        return self.handle


def test_worker_thread_result_is_delivered_on_qt_thread():
    app = _app()
    provider = _ThreadedProvider()
    dispatcher = QtMeasurementDispatcher(provider)
    gui_thread_id = threading.get_ident()
    received = []

    try:
        dispatcher.acquire(
            "sec_0","Camera",metadata={},
            on_result=lambda measurement:received.append(
                (measurement,threading.get_ident())
            ),
            on_error=lambda error:pytest.fail(str(error)),
        )
        provider.thread.join()

        # The camera thread has finished, but workflow code waits for Qt's
        # event loop instead of running on that camera thread.
        assert received == []
        app.processEvents()

        assert len(received) == 1
        assert received[0][1] == gui_thread_id
    finally:
        dispatcher.dispose()
        dispatcher.deleteLater()


def test_immediate_provider_completion_is_still_queued():
    app = _app()
    provider = _ImmediateProvider()
    dispatcher = QtMeasurementDispatcher(provider)
    received = []

    try:
        request = dispatcher.acquire(
            "sec_0","Camera",metadata={},
            on_result=received.append,
            on_error=lambda error:pytest.fail(str(error)),
        )

        # Even though the provider called on_result inside acquire(), the real
        # workflow callback must not run until control returns to Qt's event loop.
        assert received == []
        assert request.active

        app.processEvents()
        assert len(received) == 1
        assert not request.active
        assert not provider.handle.cancelled
    finally:
        dispatcher.dispose()
        dispatcher.deleteLater()


def test_cancelled_request_ignores_late_provider_result():
    app = _app()
    provider = _DeferredProvider()
    dispatcher = QtMeasurementDispatcher(provider)
    received = []

    try:
        request = dispatcher.acquire(
            "sec_0","Camera",metadata={},
            on_result=received.append,
            on_error=lambda error:pytest.fail(str(error)),
        )
        request.cancel()
        assert provider.handle.cancelled
        assert not request.active

        provider.on_result(ImageMeasurement(np.zeros((4,4)),source="Camera"))
        app.processEvents()
        assert received == []
    finally:
        dispatcher.dispose()
        dispatcher.deleteLater()


def test_duplicate_provider_completion_is_delivered_once():
    app = _app()
    provider = _DeferredProvider()
    dispatcher = QtMeasurementDispatcher(provider)
    received = []

    try:
        request = dispatcher.acquire(
            "sec_0","Camera",metadata={},
            on_result=received.append,
            on_error=lambda error:pytest.fail(str(error)),
        )
        measurement = ImageMeasurement(np.zeros((4,4)),source="Camera")
        provider.on_result(measurement)
        provider.on_result(measurement)
        app.processEvents()

        assert received == [measurement]
        assert not request.active
    finally:
        dispatcher.dispose()
        dispatcher.deleteLater()


def test_provider_exception_is_reported_through_queued_error_callback():
    app = _app()

    class _FailingProvider(_DeferredProvider):
        def acquire(self,section_key,source,*,metadata,on_result,on_error):
            raise RuntimeError("camera failed")

    dispatcher = QtMeasurementDispatcher(_FailingProvider())
    errors = []
    try:
        request = dispatcher.acquire(
            "sec_0","Camera",metadata={},
            on_result=lambda measurement:pytest.fail("unexpected result"),
            on_error=errors.append,
        )
        assert errors == []
        assert request.active

        app.processEvents()
        assert len(errors) == 1
        assert str(errors[0]) == "camera failed"
        assert not request.active
    finally:
        dispatcher.dispose()
        dispatcher.deleteLater()
