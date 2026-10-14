"""Shared contracts and immutable state for application feedback workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any,Callable,Mapping,Protocol,Sequence

from ...core.cgh.feedback import IntensityAnalysis
from ...core.measurement import ImageMeasurement
from ...core.engine.transition import SectionStateTransition


def _noop(*_args,**_kwargs) -> None:
    return None

class MeasurementRequest(Protocol):
    """Minimal cancellable measurement request consumed by feedback workflows."""

    @property
    def active(self) -> bool: ...

    def cancel(self) -> None: ...

class MeasurementDispatcher(Protocol):
    """Host-neutral asynchronous measurement dispatch contract.

    Implementations decide the execution context of completion callbacks. A Qt
    adapter can therefore marshal callbacks onto the GUI thread without making
    the application service depend on Qt.

    Feedback relies on the dispatcher delivering each completion at most once and
    suppressing callbacks after request cancellation. The Qt dispatcher provides
    both guarantees; the application service does not add its own request guard.
    """

    @property
    def available(self) -> bool: ...

    def available_sources(self,section_key: str) -> Sequence[str]: ...

    def preferred_source(
        self,section_key: str,available: Sequence[str],
    ) -> str | None: ...

    def acquire(
        self,
        section_key: str,
        source: str,
        *,
        metadata: Mapping[str,Any] | None,
        on_result: Callable[[ImageMeasurement],None],
        on_error: Callable[[Exception],None],
    ) -> MeasurementRequest: ...

@dataclass(frozen=True)
class AutomaticFeedbackState:
    """Immutable observable state of the automatic intensity-feedback loop."""

    active: bool=False
    section_key: str | None=None
    requested_rounds: int=0
    completed_rounds: int=0
    stop_requested: bool=False
    stage: str="idle"
    progress: str=""

@dataclass(frozen=True)
class FeedbackParameterUpdateResult:
    """Parameter mutation result, optionally including preview intensity metrics."""

    changed: bool
    candidate_analysis: IntensityAnalysis | None=None

@dataclass(frozen=True)
class SLMFeedbackCallbacks:
    """Synchronous events on the caller/dispatcher thread, replaceable at runtime.

    The session handles committed transitions and frame publication. Adapters
    own any required thread marshalling; callbacks are not a persistence layer.
    """

    on_section_changed: Callable[[str],None]=_noop
    on_feedback_state_changed: Callable[[str],None]=_noop
    on_position_reference_changed: Callable[[str],None]=_noop
    on_transition_committed: Callable[[str,SectionStateTransition],None]=_noop
    on_measurement_busy_changed: Callable[[str,bool,str],None]=_noop
    on_measurement_error: Callable[[str,Exception],None]=_noop
    on_localization_error: Callable[[str,Exception],None]=_noop
    on_automatic_state_changed: Callable[[AutomaticFeedbackState],None]=_noop
    on_automatic_finished: Callable[[str,str],None]=_noop
    on_warning: Callable[[str,Any],None]=_noop
    on_error: Callable[[str,Exception],None]=_noop
