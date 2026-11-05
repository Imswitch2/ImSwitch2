"""Acquisition-following adapter for the *unified* MoNaLISA Fast algorithm.

The unified Fast extraction operator now processes arriving frame batches.
Provisional images use its recorded lattice geometry and placement; complete
timepoints still use the canonical offline Fast finalization for numerical
parity. No legacy rectangular Gauss or correction substitution is allowed.

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
    LOCALIZATION_AUTO, LOCALIZATION_LOCKED, LOCALIZATION_MANUAL,
    ORIENTATION_AUTO, ORIENTATION_LOCKED, ORIENTATION_MANUAL, PRESET_FAST,
)
from .incremental_fast import IncrementalFastAccumulator, IncrementalFastGeometry


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
    if (str(info.mode or "").strip().lower() == "scanonce"
            and (info.planned_timepoints or 1) > 1):
        raise Incompatible(
            "Inconsistent acquisition metadata: Rec:Mode is ScanOnce, but "
            f"recording declares {info.planned_timepoints} timepoints; "
            "a timelapse must declare ScanLapse"
        )
    per_timepoint = int(info.frames_per_timepoint)
    resolved = timepoint_layout(info.acquisition_layout)
    from imswitch.improcess.reconstructors.monalisa_lattice.scan import layout_scan
    scan = layout_scan(resolved, per_timepoint)
    if scan is None or scan.num_stacks != 1 or scan.frames_per_stack != per_timepoint:
        raise Incompatible(
            "Live Fast needs a recorded, contiguous, single-plane scan layout; "
            "this source can use finite reconstruction after completion"
        )
    # Explicit calibration and orientation can begin at the first committed
    # frame. Use single-frame chunks to preserve the first-scan boundary; a
    # larger fixed chunk would cross it when init_frames=1 (e.g. 1+2*N).
    immediate = (
        str(params.get("localization_mode", LOCALIZATION_AUTO)) in
        (LOCALIZATION_LOCKED, LOCALIZATION_MANUAL)
        and str(params.get("orientation_mode", ORIENTATION_AUTO)) in
        (ORIENTATION_LOCKED, ORIENTATION_MANUAL)
    )
    if immediate:
        return 1, 1
    return per_timepoint, max(1, math.gcd(per_timepoint, 32))


class UnifiedFastStreamingSession(StreamingSession):
    """Frame-progressive previews and completed-timepoint Fast publication.

    A provisional partial scan never becomes a completed Session timepoint.
    Explicit Manual/Locked settings initialize on the first frame. Auto
    localization/orientation initialize using a complete first timepoint.
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
        self._preview_revision = 0
        self._incremental = None

    def begin(self, init_obj, params: dict) -> StreamPlan:
        if self._assembled is not None:
            raise RuntimeError("Streaming session cannot begin twice")
        if (str((params or {}).get("preset", PRESET_FAST)) != PRESET_FAST
                or "reconstruction_method" in (params or {})):
            raise Incompatible("Only the unified MoNaLISA Fast parameter contract can stream")
        capability = getattr(self._reconstructor, "streaming_capability", None)
        if callable(capability):
            supported, reason = capability(params)
            if not supported:
                raise Incompatible(reason)
        stack_info = init_obj.stack_info
        if stack_info is None or not stack_info.frames_per_stack:
            raise Incompatible("MoNaLISA streaming requires scan-frame metadata")
        frames = np.asarray(init_obj.data)
        per_timepoint = int(stack_info.frames_per_stack)
        if frames.ndim != 3 or frames.shape[0] not in (1, per_timepoint):
            raise Incompatible("Live initialization must contain the first frame or one complete scan")
        planned = stack_info.planned_timepoints
        if planned is None or int(planned) < 1:
            raise Incompatible("A declared timepoint count is required for unified Fast streaming")

        self._name = str(init_obj.name)
        self._detector = str(init_obj.dataset_name)
        self._source_path = init_obj.source_path
        self._original_params = deepcopy(dict(params or {}))
        self._effective_params = deepcopy(self._original_params)
        self._per_timepoint = per_timepoint
        self._next_frame = int(frames.shape[0])
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
        if len(frames) == per_timepoint:
            # Auto (or another non-immediate configuration) calibrated from
            # the *complete* first T before partial previews become possible.
            self._process_timepoint(frames)
            self._initialize_incremental(first_frame=frames[0])
            self._preview_revision += 1
            template = self._assembled.result
            return StreamPlan(
                out_shape=tuple(template.data.shape),
                axis_labels=list(template.axis_labels),
                view_modes=list(template.view_modes or ()),
                dtype=np.dtype(template.data.dtype),
                scale_unit=str(template.scale_unit),
                axis_scales=list(template.axis_scales),
            )
        # Fully explicit calibration: first committed frame is already enough
        # to allocate the complete output raster, without fitting it from a
        # partial sample of the scan.
        self._initialize_incremental(first_frame=frames[0])
        self._incremental.push(frames)
        self._chunks = [frames.copy()]
        self._buffered = len(frames)
        self._preview_revision += 1
        provisional = self.provisional_preview()
        return StreamPlan(
            out_shape=(self._planned, *self._incremental.shape),
            axis_labels=["T", "Y", "X"],
            view_modes=[], dtype=np.dtype("float32"),
            scale_unit=str(provisional.scale_unit),
            axis_scales=[1., *list(provisional.axis_scales)],
        )

    def _initialize_incremental(self, *, first_frame):
        if self._completed:
            calibrated = IncrementalFastGeometry.from_calibrated(
                self._reconstructor, self._effective_params, self._layout,
                self._per_timepoint, first_frame, self._calibration,
            )
        else:
            calibrated = IncrementalFastGeometry.from_locked(
                self._reconstructor, self._effective_params, self._layout,
                first_frame, self._per_timepoint,
            )
        self._incremental = IncrementalFastAccumulator(calibrated)

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
        if self._buffered + len(frames) > self._per_timepoint:
            raise ValueError("Streaming chunk crosses the declared timepoint boundary")
        self._incremental.push(frames)
        self._next_frame = int(end)
        self._chunks.append(frames.copy())
        self._buffered += int(frames.shape[0])
        self._preview_revision += 1
        if self._buffered == self._per_timepoint:
            assembled = np.concatenate(self._chunks, axis=0)
            self._chunks.clear()
            self._buffered = 0
            self._process_timepoint(assembled)
            self._incremental.reset()
            self._preview_revision += 1

    @property
    def has_completed_timepoint(self) -> bool:
        return self._completed > 0

    @property
    def has_incomplete_preview(self) -> bool:
        return self._buffered > 0

    def preview_revision(self) -> int:
        """Every committed-frame chunk can produce another provisional image."""
        return self._preview_revision

    def provisional_preview(self):
        """Detached, display-only image, never a Session-owned result.

        Workflows can render it without claiming/autosaving it. Cancelling
        before T0 leaves no result; cancelling in T1 retains only T0.
        """
        if not self._buffered or self._incremental is None:
            return None
        from .canonical_result import MonalisaResult
        calibrated = self._incremental.calibrated
        pixels = self._incremental.image()
        if pixels is None:
            return None
        image = MonalisaResult(
            name=f"{self._name}_monalisa_preview",
            image=pixels, output_pixel_nm=calibrated.output_pixel_nm,
            params=deepcopy(self._original_params),
            diagnostics={"placement": self._incremental._placement.method},
            axis_labels=["Y", "X"], scanned_axes=calibrated.scan.scanned_axes,
        )
        image.metadata["monalisa_streaming"] = {
            "algorithm": ALGORITHM_ID, "provisional_preview": True,
            "timepoint": self._completed,
            "received_frames": self._buffered,
            "frames_per_timepoint": self._per_timepoint,
        }
        return image

    def provisional_volume(self, preview):
        """Create a viewer-only T,Y,X buffer from the first incomplete scan.

        This is never the scientific Session result: no completion coverage or
        provenance is claimed until the first full timepoint is reconstructed.
        Subsequent raw chunks transfer just one T slice, not the entire volume.
        """
        from .canonical_result import MonalisaResult

        t = int(preview.metadata["monalisa_streaming"]["timepoint"])
        data = np.full((self._planned, *preview.data.shape), np.nan, dtype=np.float32)
        data[t] = preview.data
        result = MonalisaResult(
            name=f"{self._name}_monalisa", image=data,
            output_pixel_nm=preview.output_pixel_size_nm,
            params=deepcopy(self._original_params),
            diagnostics={"placement": self._incremental._placement.method},
            axis_labels=["T", "Y", "X"],
            scanned_axes=self._incremental.calibrated.scan.scanned_axes,
        )
        result.metadata["monalisa_streaming"] = {
            **preview.metadata["monalisa_streaming"], "provisional_preview": True,
        }
        return result

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
        self._incremental = None
        self._reconstructor = None
