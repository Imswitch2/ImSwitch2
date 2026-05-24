from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventRunMode(Enum):
    """Runtime modes shared by event-triggered controllers."""

    Experiment = 1
    Visualize = 2
    Validate = 3


class EventScanInitiationMode(Enum):
    """Scan launch modes shared by event-triggered controllers."""

    ScanWidget = 1
    RecordingWidget = 2


@dataclass
class EventTriggeredSessionState:
    """Mutable session state for event-triggered acquisition controllers.

    The object intentionally stores runtime bookkeeping only. It must not store
    active hardware states such as laser emission, DAQ task state, or stage
    motion state.
    """

    runMode: EventRunMode = EventRunMode.Experiment
    scanInitiationMode: EventScanInitiationMode | None = None
    detectorFast: str | None = None
    laserFast: str | None = None
    running: bool = False
    validating: bool = False
    busy: bool = False
    imageSignalConnected: bool = False
    scanEndSignalConnected: bool = False
    frame: int = 0
    validationFrames: int = 0
    tCallMs: float = 0
    maxAnaImgVal: float = 0
    detLog: dict[str, Any] = field(default_factory=dict)

    def reset_runtime_counters(self) -> None:
        """Reset transient counters while preserving selected devices and mode."""
        self.running = False
        self.validating = False
        self.busy = False
        self.frame = 0
        self.validationFrames = 0
        self.tCallMs = 0
        self.maxAnaImgVal = 0
