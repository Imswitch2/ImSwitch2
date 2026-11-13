"""UI-independent descriptors for incremental ImProcess reconstruction.

``T`` denotes logical timepoints; ``S`` is the shape *in raw frames* of one
logical timepoint.  Scan-axis geometry belongs to acquisition-layout metadata,
not to ``S``.  No pixels are inspected to build these descriptors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass(frozen=True)
class RawInfo:
    T: int | None
    S: tuple[int, ...]
    frame_shape: tuple[int, int]
    dtype: np.dtype
    name: str = ""
    mode: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    detector_name: str | None = None
    source_path: str | None = None
    physical_paths: tuple[str, ...] = ()
    acquisition_layout: Any = None
    observed_timepoints: tuple[int, ...] = ()
    planned_timepoints: int | None = None
    source_format: str | None = None

    @property
    def frames_per_timepoint(self) -> int:
        return int(np.prod(self.S)) if self.S else 1


@dataclass(frozen=True)
class Contract:
    """Reader scheduling requirements, not a scientific correction policy.

    ``init_frames`` controls when begin() is called (0 means first frame in
    legacy watcher semantics), and ``chunk_frames`` controls push() batches.
    Parameters are the snapped reconstruction settings; any independent
    timepoint finalization is owned by the StreamingSession implementation,
    not mandated or selected here by a generic boolean.
    """

    init_frames: int
    chunk_frames: int
    params: dict[str, Any]

    def __post_init__(self):
        if self.init_frames < 0 or self.chunk_frames < 1:
            raise ValueError("init_frames must be nonnegative and chunk_frames positive")

    @property
    def begin_frames(self) -> int:
        return max(1, self.init_frames)


class Incompatible(ValueError):
    """Reconstructor rejected this source from metadata, before pixel reads."""


def _meta(attrs: dict, *keys):
    for key in keys:
        value = attrs.get(key)
        if value is None and isinstance(attrs.get("ImswitchData"), dict):
            value = attrs["ImswitchData"].get(key)
        if value is not None:
            if isinstance(value, (bytes, np.bytes_)):
                value = value.decode(errors="replace")
            return value
    return None


def _positive(value) -> int | None:
    try:
        result = int(value)
    except (ValueError, TypeError):
        return None
    return result if result > 0 else None


def raw_info_from_stack(
    info,
    *,
    name: str = "",
    source_path: str | None = None,
    physical_paths: tuple[str, ...] = (),
    observed_timepoints: tuple[int, ...] = (),
) -> RawInfo:
    """Adapt an existing Session ``StackInfo`` without selecting detectors.

    Ordinary ``SpecFrames`` / ``SpecTime`` camera recordings have ``S=()``:
    their frames are *timepoints*, even when a reader's historical
    ``frames_per_stack`` hint equals the whole 12-frame camera series.
    """
    attrs = dict(getattr(info, "attrs", {}) or {})
    mode_value = _meta(attrs, "recording:mode", "Rec:Mode", "mode")
    mode = str(mode_value).strip() if mode_value is not None else None
    normalized_mode = str(mode or "").lower()
    fps = _positive(getattr(info, "frames_per_stack", None)) or 1
    planned = _positive(getattr(info, "planned_timepoints", None))
    expected = _positive(getattr(info, "expected_frames", None))
    scan_mode = normalized_mode in {"scanonce", "scanlapse"}
    camera_mode = normalized_mode in {"specframes", "spectime", "untilstop"}
    if scan_mode:
        shape = (fps,)
        total = 1 if normalized_mode == "scanonce" else planned
    elif camera_mode:
        shape = ()
        total = expected if normalized_mode == "specframes" else None
    elif normalized_mode == "cameralapse":
        shape = (fps,) if fps > 1 else ()
        total = planned
    else:
        # Unknown mode: prefer explicit layout/recorder stack evidence over
        # a reader's fallback derived from the full number of camera frames.
        explicit_fps = _positive(_meta(attrs, "recording:frames_per_stack"))
        shape = (explicit_fps,) if explicit_fps and explicit_fps > 1 else ()
        total = planned or (expected if shape == () else None)

    return RawInfo(
        T=total,
        S=shape,
        frame_shape=tuple(info.frame_shape[-2:]),
        dtype=np.dtype(info.dtype),
        name=name,
        mode=mode,
        attrs=attrs,
        detector_name=getattr(info, "detector_name", None),
        source_path=source_path,
        physical_paths=tuple(physical_paths or ((source_path,) if source_path else ())),
        acquisition_layout=getattr(info, "acquisition_layout", None),
        observed_timepoints=tuple(observed_timepoints),
        planned_timepoints=planned,
        source_format=getattr(info, "source_format", None),
    )
