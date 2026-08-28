from __future__ import annotations

from typing import Callable,Mapping,Protocol,Sequence,Any

from ..core.measurement import ImageMeasurement


class MeasurementRequestHandle(Protocol):
    """Cancellation handle returned by a host measurement request."""

    def cancel(self) -> None:
        ...


class MeasurementProvider(Protocol):
    """Host capability for acquiring generic image measurements.

    One provider is normally bound to one physical SLM integration.  slmcore
    supplies the section key and semantic provenance; the host decides how a
    named source maps to its detector/acquisition infrastructure.

    ``on_result`` and ``on_error`` may be called from any thread.  Hosts do not
    need to know about Qt thread rules; :mod:`slmcore.qt` queues completion onto
    its Qt thread before feedback or calibration code updates widgets.
    """

    def available_sources(self,section_key: str) -> Sequence[str]:
        ...

    def preferred_source(
        self,section_key: str,available: Sequence[str],
    ) -> str | None:
        ...

    def acquire(
        self,
        section_key: str,
        source: str,
        *,
        metadata: Mapping[str, Any] | None,
        on_result: Callable[[ImageMeasurement],None],
        on_error: Callable[[Exception],None],
    ) -> MeasurementRequestHandle | None:
        ...
