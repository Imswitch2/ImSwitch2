"""In-process live reconstruction: ImControl's side of the ImProcess live runtime.

ImProcess's live pipeline (``imswitch.improcess.live``) consumes any object
that implements its ``LiveSource`` contract. Every source shipped with
ImProcess reads a growing file; the one here reads the per-consumer detector
chunk queue instead, so a reconstruction can follow an acquisition as it
happens without a recording in between. ImProcess must not import ImControl,
so the source lives on this side of the boundary and imports the ImProcess
*library* layers lazily.
"""

from .detector_chunk_source import (
    DetectorChunkLiveSource,
    LiveStreamStats,
    build_live_stack_info,
    frame_shape_for_detector,
)

__all__ = [
    'DetectorChunkLiveSource',
    'LiveStreamStats',
    'build_live_stack_info',
    'frame_shape_for_detector',
]
