"""Acquisition-following adapter for the *unified* MoNaLISA Fast algorithm.

A complete scan timepoint is the atomic scientific output for this checkpoint:
raw frames are buffered only until their timepoint is committed, then handed
unaltered to the same ``MonalisaReconstructor.process`` implementation used
for finite data. This is deliberately NOT the older rectangular Fast-Gauss
live processor. First-frame updates with explicit calibration are a separate
initialization/performance milestone.

The generic reader enforces the committed-frame barrier and back-pressure;
this class owns only processing and its timepoint output buffer. The existing
semantic result assembler validates axes, spatial calibration, and optional
layers before publication.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import math
from types import SimpleNamespace

import numpy as np

from imswitch.improcess.execution.progressive_results import ProgressiveResultAssembler
from imswitch.improcess.execution.result_assembly import ResultAssemblyPlan
from imswitch.improcess.reconstructors.base import (
    Incompatible, StreamPlan, StreamingSession,
)
from .params import (
    LOCALIZATION_AUTO, ORIENTATION_AUTO, ORIENTATION_LOCKED, PRESET_FAST,
)


ALGORITHM_ID = "unified-monalisa-fast-gaussian"


def timepoint_layout(resolved):
    """Make a per-timepoint view of a recorded scan, without guessing axes.

    A producer's time loop must be outermost for the raw reader's contiguous
    `frames_per_timepoint` contract to hold. A filtered selection spanning a
    time loop cannot be divided without recomputing its event spans: reject
    that case instead of silently misplacing frames.
    """
    layout = getattr(resolved, "layout", None)
    if layout is None:
        return resolved
    loops = tuple(layout.event_loops)
    times = [index for index, loop in enumerate(loops) if loop.kind == "time"]
    if not times or all(loops[index].count == 1 for index in times):
        return resolved
    if len(times) != 1 or times[0] != 0:
        raise Incompatible("Live MoNaLISA requires a contiguous outermost time loop")
    time_loop = loops[0]
    if layout.recorded_event_spans is not None:
        raise Incompatible("Timepoint-filtered acquisition spans need finite reconstruction")
    if any(time_loop.id in rule.parity_loops for rule in layout.traversal):
        raise Incompatible("Time-dependent scan traversal needs finite reconstruction")
    return replace(
        resolved, layout=replace(layout, event_loops=(replace(time_loop, count=1), *loops[1:]))
    )


def timepoint_stream_contract(info, params):
    """Metadata-only source gate, independently of the Session checkbox."""
    if not info.S or info.frames_per_timepoint < 2:
        raise Incompatible("MoNaLISA requires multiple frames per scan timepoint")
    if info.T is None or int(info.T) < 1:
        raise Incompatible(
            "Unified Fast live processing currently requires a declared timepoint count; "
            "wait for completed acquisition"
        )
    if info.mode is not None and str(info.mode).strip().lower() not in (
        "scanonce", "scanlapse",
    ):
        raise Incompatible("MoNaLISA live reconstruction requires ScanOnce or ScanLapse frames")
    timepoint_layout(info.acquisition_layout)
    # A divisible chunk is important: a scan of e.g. 242 frames must be
    # published at frame 242, not delayed until 256 or recording finalization.
    per_timepoint = int(info.frames_per_timepoint)
    return per_timepoint, max(1, math.gcd(per_timepoint, 32))


class UnifiedFastStreamingSession(StreamingSession):
    """Incremental *timepoint* processing with finite Fast numerical parity.

    The first timepoint is required for calibration/orientation, as agreed for
    M3C-2. Once ready, each later complete timepoint is processed using the
    same finite Fast method, with Auto calibration resolved only on T0.
    """

    def __init__(self, reconstructor):
        self._reconstructor = reconstructor
        self._original_params = None
        self._effective_params = None
        self._attrs = None
        self._layout = None
        self._name = None
        self._detector = None
        self._source_path = None
        self._per_timepoint = None
        self._planned = None
        self._frame_shape = None
        self._chunks = []
        self._buffered = 0
        self._next_frame = 0
        self._assembled = None
        self._calibration = None
        self._completed = 0
        self._previewed = 0

    def begin(self, init_obj, params: dict) -> StreamPlan:
        if self._assembled is not None:
            raise RuntimeError("Streaming session cannot begin twice")
        if (str((params or {}).get("preset", PRESET_FAST)) != PRESET_FAST
                or "reconstruction_method" in (params or {})):
            raise Incompatible("Only the unified MoNaLISA Fast parameter contract can stream")
        stack_info = init_obj.stack_info
        if stack_info is None or not stack_info.frames_per_stack:
            raise Incompatible("MoNaLISA streaming requires scan-frame metadata")
        frames = np.asarray(init_obj.data)
        per_timepoint = int(stack_info.frames_per_stack)
        if frames.ndim != 3 or frames.shape[0] != per_timepoint:
            raise Incompatible("Initialization must contain exactly one complete scan timepoint")
        planned = stack_info.planned_timepoints
        if planned is None or int(planned) < 1:
            raise Incompatible("A declared timepoint count is required for unified Fast streaming")

        self._name = str(init_obj.name)
        self._detector = str(init_obj.dataset_name)
        self._source_path = init_obj.source_path
        self._original_params = deepcopy(dict(params or {}))
        self._effective_params = deepcopy(self._original_params)
        self._per_timepoint = per_timepoint
        self._next_frame = per_timepoint
        self._planned = int(planned)
        self._frame_shape = tuple(frames.shape[1:])
        self._layout = timepoint_layout(stack_info.acquisition_layout)
        self._attrs = dict(init_obj.attrs or {})
        self._attrs.update({
            "recording:num_timepoints": 1,
            "recording:expected_frames": per_timepoint,
            "recording:frames_per_stack": per_timepoint,
        })
        plan = ResultAssemblyPlan.for_timepoints(
            f"stream:{self._name}", self._detector or self._name,
            total=self._planned,
        )
        self._assembled = ProgressiveResultAssembler(plan)
        self._process_timepoint(frames)
        return StreamPlan(
            out_shape=tuple(self._assembled.result.data.shape),
            axis_labels=list(self._assembled.result.axis_labels),
            view_modes=list(self._assembled.result.view_modes or ()),
            dtype=np.dtype(self._assembled.result.data.dtype),
            scale_unit=str(self._assembled.result.scale_unit),
            axis_scales=list(self._assembled.result.axis_scales),
        )

    def _process_timepoint(self, frames: np.ndarray):
        if self._completed >= self._planned:
            raise ValueError("More completed timepoints than acquisition metadata declares")
        # The same method and settings as offline, using an owned in-memory
        # data object. The StreamInit's resolved layout is retained rather
        # than re-inferring hex/stagger geometry from filenames or Nx/Ny.
        data_obj = SimpleNamespace(
            name=self._name,
            datasetName=self._detector,
            dataPath=self._source_path,
            data=np.asarray(frames),
            dataLoaded=True,
            attrs=self._attrs,
            acquisition_layout=self._layout,
        )
        part = self._reconstructor.process(data_obj, deepcopy(self._effective_params))
        if getattr(part, "monalisa_representation", None) != "flexible_image":
            raise Incompatible("Unified Fast streaming received a non-Gaussian MoNaLISA result")
        calibration = deepcopy((getattr(part, "metadata", {}) or {}).get("monalisa_calibration") or {})
        if self._completed == 0:
            self._calibration = calibration
            if self._original_params.get("localization_mode", LOCALIZATION_AUTO) == LOCALIZATION_AUTO:
                resolved = calibration.get("resolved_localization")
                if isinstance(resolved, dict) and resolved:
                    self._effective_params["auto_localization"] = deepcopy(resolved)
            if self._original_params.get("orientation_mode", ORIENTATION_AUTO) == ORIENTATION_AUTO:
                orientation = calibration.get("resolved_orientation")
                if orientation:
                    self._effective_params["orientation_mode"] = ORIENTATION_LOCKED
                    self._effective_params["locked_orientation"] = str(orientation)
        # Document *requested* settings separately from internal resolution.
        # No subsequent TP may claim that the user selected Locked when Auto
        # was actually resolved from the first recorded timepoint.
        part.params = deepcopy(self._original_params)
        part.params["calibration_snapshot"] = deepcopy(calibration)
        part.metadata.setdefault("monalisa_streaming", {})["algorithm"] = ALGORITHM_ID
        self._assembled.append(part, self._completed)
        result = self._assembled.result
        for attr in ("_fast_placement_cache", "_monalisa_fast_update_signature"):
            if hasattr(result, attr):
                delattr(result, attr)
        self._completed += 1
        result.metadata["monalisa_streaming"] = {
            "algorithm": ALGORITHM_ID,
            "preset": PRESET_FAST,
            "source_path": self._source_path,
            "frames_per_timepoint": self._per_timepoint,
            "completed_timepoints": self._completed,
            "planned_timepoints": self._planned,
            "localization_mode_requested": self._original_params.get("localization_mode", LOCALIZATION_AUTO),
            "orientation_mode_requested": self._original_params.get("orientation_mode", ORIENTATION_AUTO),
            "first_timepoint_calibration": deepcopy(self._calibration),
        }

    def push(self, chunk: np.ndarray, start: int, end: int) -> None:
        if self._assembled is None:
            raise RuntimeError("Call begin() before push()")
        frames = np.asarray(chunk)
        if frames.ndim != 3 or frames.shape[1:] != self._frame_shape:
            raise ValueError("Raw frame geometry changed during streaming")
        if int(start) != self._next_frame or end - start != frames.shape[0]:
            raise ValueError("Noncontiguous MoNaLISA stream: frames cannot be silently dropped")
        self._next_frame = int(end)
        self._chunks.append(frames.copy())
        self._buffered += int(frames.shape[0])
        if self._buffered > self._per_timepoint:
            raise ValueError("Streaming chunk crosses the declared timepoint boundary")
        if self._buffered == self._per_timepoint:
            assembled = np.concatenate(self._chunks, axis=0)
            self._chunks.clear()
            self._buffered = 0
            self._process_timepoint(assembled)

    def preview_revision(self) -> int:
        """A new preview is meaningful only after another complete timepoint."""
        return self._completed

    def live_plane(self):
        if self._assembled is None or self._completed <= self._previewed:
            return None
        result = self._assembled.result
        # The generic plane-update signal carries only the primary image.
        # When optional scientific layers are enabled, publish a complete
        # owned snapshot instead of leaving their displayed T slices stale.
        if any(getattr(result, layer, None) is not None for layer in
               ("background", "sharpened", "pinholes")):
            return None
        time_axis = result.axis_labels.index("T")
        slices = [slice(None)] * result.data.ndim
        slices[time_axis] = slice(self._completed - 1, self._completed)
        self._previewed = self._completed
        return self._completed - 1, np.asarray(result.data[tuple(slices)]).copy()

    def result(self):
        if self._assembled is None or self._completed == 0:
            raise RuntimeError("No complete scan timepoint has been reconstructed")
        # Qt signals cross threads: never expose arrays that the worker later
        # writes into. Plane updates use an independently copied T slice.
        import copy
        original = self._assembled.result
        snapshot = copy.copy(original)
        for name in ("data", "background", "sharpened", "pinholes"):
            value = getattr(original, name, None)
            if value is not None:
                setattr(snapshot, name, np.asarray(value).copy())
        snapshot.metadata = deepcopy(original.metadata)
        snapshot.params = deepcopy(original.params)
        snapshot.diagnostics = deepcopy(original.diagnostics)
        self._previewed = self._completed
        return snapshot

    def finish(self):
        if self._buffered:
            raise ValueError(
                f"Recording ended with {self._buffered} unprocessed frames; "
                "only complete scan timepoints can be published"
            )
        return self.result()

    def close(self):
        self._chunks.clear()
        self._buffered = 0
        self._reconstructor = None
