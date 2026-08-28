"""Qt-safe access to host-provided image measurements."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any,Callable,Mapping,Sequence

from qtpy import QtCore

from ...host.measurement import MeasurementProvider,MeasurementRequestHandle
from ...measurement import ImageMeasurement


@dataclass
class _MeasurementRequestState:
    on_result: Callable[[ImageMeasurement],None]
    on_error: Callable[[Exception],None]
    host_handle: MeasurementRequestHandle | None = None


class QtMeasurementRequest:
    """One cancellable measurement request managed by ``QtMeasurementDispatcher``."""

    def __init__(self,dispatcher: "QtMeasurementDispatcher",request_id: int) -> None:
        self._dispatcher = dispatcher
        self.request_id = int(request_id)

    @property
    def active(self) -> bool:
        return self._dispatcher.is_request_active(self.request_id)

    def cancel(self) -> None:
        self._dispatcher.cancel(self.request_id)


class QtMeasurementDispatcher(QtCore.QObject):
    """Qt implementation of the application measurement-dispatch contract.

    Run host measurement requests and deliver callbacks on the Qt thread.

    A host camera may finish on a worker thread and call ``on_result`` or
    ``on_error`` there.  Those callbacks must not directly update Qt widgets.
    This dispatcher always sends completion through Qt's event queue first, so
    feedback and calibration callbacks run on the dispatcher's Qt thread.

    Delivery is also always queued when the provider completes immediately on
    the Qt thread.  This means a result cannot run inside ``acquire()`` before
    the returned host cancellation handle has been recorded.
    """

    _sigResult = QtCore.Signal(int,object)
    _sigError = QtCore.Signal(int,object)

    def __init__(
        self,
        provider: MeasurementProvider | None,
        *,
        parent: QtCore.QObject | None=None,
    ) -> None:
        super().__init__(parent)
        self._provider = provider
        self._request_counter = 0
        self._requests: dict[int, _MeasurementRequestState] = {}
        self._disposed = False

        # Always queue these signals.  Besides protecting Qt widgets from
        # worker-thread callbacks, this keeps completion ordering identical for
        # asynchronous cameras and providers that return a result immediately.
        self._sigResult.connect(
            self._deliver_result,
            type=QtCore.Qt.QueuedConnection,
        )
        self._sigError.connect(
            self._deliver_error,
            type=QtCore.Qt.QueuedConnection,
        )

    @property
    def available(self) -> bool:
        return self._provider is not None

    def available_sources(self,section_key: str) -> Sequence[str]:
        provider = self._provider
        if provider is None:
            return ()
        return tuple(
            str(item) for item in provider.available_sources(section_key)
        )

    def preferred_source(
        self,section_key: str,available: Sequence[str],
    ) -> str | None:
        provider = self._provider
        if provider is None:
            return None
        return provider.preferred_source(section_key,available)

    def acquire(
        self,
        section_key: str,
        source: str,
        *,
        metadata: Mapping[str, Any] | None,
        on_result: Callable[[ImageMeasurement],None],
        on_error: Callable[[Exception],None],
    ) -> QtMeasurementRequest:
        """Start one request whose final callbacks run through Qt's event loop."""
        self._require_active()
        self._request_counter += 1
        request_id = self._request_counter
        self._requests[request_id] = _MeasurementRequestState(
            on_result=on_result,
            on_error=on_error,
        )
        request = QtMeasurementRequest(self,request_id)

        provider = self._provider
        source = str(source or "").strip()
        if provider is None:
            self._sigError.emit(
                request_id,
                RuntimeError("No host measurement provider is configured."),
            )
            return request
        if not source:
            self._sigError.emit(
                request_id,
                ValueError("Select a detector before acquisition."),
            )
            return request

        def provider_result(measurement: ImageMeasurement) -> None:
            # The provider may call this from any thread. Emitting is the only
            # work done here; workflow/UI code runs later on the Qt thread.
            if not self._disposed:
                self._sigResult.emit(request_id,measurement)

        def provider_error(error: Exception) -> None:
            if not self._disposed:
                self._sigError.emit(request_id,error)

        try:
            handle = provider.acquire(
                section_key,
                source,
                metadata=metadata,
                on_result=provider_result,
                on_error=provider_error,
            )
        except Exception as error:
            self._sigError.emit(request_id,error)
            return request

        state = self._requests.get(request_id)
        if state is not None:
            state.host_handle = handle
        elif handle is not None:
            # If Qt already processed completion before provider.acquire()
            # returned, do not leave a finished host request alive.
            self._cancel_handle(handle)
        return request

    def is_request_active(self,request_id: int) -> bool:
        return int(request_id) in self._requests

    def cancel(self,request_id: int) -> bool:
        state = self._requests.pop(int(request_id),None)
        if state is None:
            return False
        self._cancel_handle(state.host_handle)
        return True

    def cancel_all(self) -> None:
        for request_id in tuple(self._requests):
            self.cancel(request_id)

    @QtCore.Slot(int,object)
    def _deliver_result(self,request_id: int,measurement: Any) -> None:
        state = self._requests.pop(int(request_id),None)
        if state is None:
            return
        state.on_result(measurement)

    @QtCore.Slot(int,object)
    def _deliver_error(self,request_id: int,error: Any) -> None:
        state = self._requests.pop(int(request_id),None)
        if state is None:
            return
        if not isinstance(error,Exception):
            error = RuntimeError(str(error))
        state.on_error(error)

    @staticmethod
    def _cancel_handle(handle: MeasurementRequestHandle | None) -> None:
        cancel = getattr(handle,"cancel",None)
        if callable(cancel):
            cancel()

    def _require_active(self) -> None:
        if self._disposed:
            raise RuntimeError("QtMeasurementDispatcher has been disposed")

    def dispose(self) -> None:
        if self._disposed:
            return
        self.cancel_all()
        self._disposed = True
