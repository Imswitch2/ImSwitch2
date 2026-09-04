from .SharedAttributes import SharedAttributes, JSON_ATTR_PREFIX
from .VFileCollection import VFileItem, VFileCollection
from .api import APIExport, generateAPI
from .logging import initLogger
from .shortcut import shortcut, generateShortcuts, ShortcutScope, ShortcutAction, getBoundShortcuts
from .state_contracts import (
    ComponentStateApplyMode, RestoreWarning, isCriticalRestoreWarning,
    rewordRestoreWarning,
)
from .WidgetStatePersistence import WidgetStatePersistence, getWidgetStatePersistence
from .acquisition_layout import (
    ACQUISITION_LAYOUT_SCHEMA,
    MAX_INLINE_LAYOUT_BYTES,
    AcquisitionLayout,
    AcquisitionLayoutError,
    AcquisitionLoop,
    AcquisitionPartition,
    LayoutIssue,
    RecordedEventSpan,
    TraversalRule,
    UnfoldedArray,
    canonicalize_recorded_event_spans,
    decode_acquisition_layout,
    encode_acquisition_layout,
    iter_physical_coordinates,
    iter_recorded_coordinates,
    producer_event_coordinates,
    physical_frame_coordinates,
    physical_orientation_flips,
    recorded_frame_coordinates,
    unfold_frame_axis,
    validate_acquisition_layout,
)
from .acquisition_metadata import (
    RecordingLifecycle,
    RecordingLifecycleMarkers,
    flatten_acquisition_metadata,
    normalize_recording_lifecycle,
)
