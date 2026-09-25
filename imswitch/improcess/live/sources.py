"""Format-agnostic live source contracts and the in-memory stack wrapper."""

import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import h5py
import numpy as np
import zarr

from imswitch.imcommon.model.logging import initLogger
from imswitch.imcommon.model.acquisition_metadata import (
    RecordingLifecycleMarkers,
    normalize_recording_lifecycle,
)
from imswitch.imcommon.model import initLogger
from imswitch.improcess.model.acquisition_layout_resolver import (
    ResolvedAcquisitionLayout,
    resolve_acquisition_layout,
)
from imswitch.improcess.model.image_sources import (
    axis_scales_from_element_size,
    dataset_names,
    decode_layout_attrs,
    default_axis_labels,
    flatten_metadata_attrs,
    is_zarr_array as _is_zarr_array,
    is_zarr_group as _is_zarr_group,
    resolve_image,
)
from imswitch.improcess.reconstructors.base import Chunk, StackInfo


def _axis_labels_from_attrs(attrs: dict[str, Any], ndim: int) -> tuple[str, ...] | None:
    value = attrs.get(
        "ngff:axes",
        attrs.get("axes", attrs.get("_ARRAY_DIMENSIONS", attrs.get("tiff:axes"))),
    )
    if isinstance(value, str):
        value = tuple(value) if len(value) == ndim else None
    if not isinstance(value, (list, tuple)) or len(value) != ndim:
        return None
    labels = []
    for item in value:
        if isinstance(item, dict):
            item = item.get("name")
        if item is None:
            return None
        labels.append(str(item))
    return tuple(labels)


def _resolve_stack_layout(
    attrs: dict[str, Any],
    *,
    shape: tuple[int, ...],
    detector: str | None,
    source_path: str | None,
    dataset_path: str | None,
    axis_labels: tuple[str, ...] | None = None,
) -> ResolvedAcquisitionLayout:
    labels = axis_labels or _axis_labels_from_attrs(attrs, len(shape))
    resolution_shape = shape
    if shape and _optional_bool(attrs.get("writing")) is True:
        planned = (
            _optional_int(attrs.get("recording:planned_frames"))
            or _optional_int(attrs.get("recording:expected_frames"))
            or _optional_int(attrs.get("recording:frames_per_stack"))
        )
        resolution_shape = (max(1, int(planned or shape[0] or 1)), *shape[1:])
    return resolve_acquisition_layout(
        attrs,
        shape=resolution_shape,
        detector=str(detector or attrs.get("detector_name") or "unknown"),
        axis_labels=labels,
        axis_metadata_explicit=labels is not None,
        source_path=source_path,
        dataset_path=dataset_path,
    )


def _coerce_positive_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple, np.ndarray)):
        array = np.asarray(value).flatten()
        if array.size != 1:
            return None
        value = array[0]
    if isinstance(value, (bytes, np.bytes_)):
        value = value.decode(errors="ignore")
    if isinstance(value, str):
        text = value.strip()
        if text.lower() in {"", "null", "none", "nan", "n/a", "na"}:
            return None
        value = text
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(number) or number <= 0:
        return None
    return max(1, int(number))


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, (bytes, np.bytes_)):
        value = value.decode(errors="ignore")
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return None
    return bool(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _numeric_vector(value: Any, min_len: int) -> np.ndarray | None:
    try:
        vector = np.asarray(value, dtype=float).flatten()
    except (TypeError, ValueError):
        return None
    if vector.size < min_len or not np.all(np.isfinite(vector[:min_len])):
        return None
    return vector


def _meta_lookup(attrs: dict[str, Any], key: str) -> Any:
    """Read a metadata key from flat attrs or a nested ``ImswitchData`` block.

    ImSwitch2's structured storer flattens scan metadata to top-level keys
    (``ScanStage:axis_length`` …); legacy ImSwitch-1 Zarr nests the same keys
    under an ``ImswitchData`` attr. Prefer the flat key, then the nested one.
    """
    if key in attrs:
        return attrs[key]
    nested = attrs.get("ImswitchData")
    if isinstance(nested, dict) and key in nested:
        return nested[key]
    return None


def _writing_flag_complete(value: Any) -> bool:
    """Interpret a ``writing`` attribute: absent => complete (legacy/external)."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {'0', 'false', 'no', 'off'}
    return not bool(value)


def _zarr_store_write_complete(path: Any) -> bool:
    """True iff a Zarr store's first detector data array is done being written.

    Used to gate advancing onto the NEXT timepoint file of a per-file
    timelapse: ZarrStorer resizes the array BEFORE writing the frame data, so
    a reader that opens a mid-write store trusts a shape that is ahead of the
    committed data and permanently bakes zero frames into the stream (root
    cause 1 in docs/live_reconstruction_audit.md). A store with no dataset yet
    is not complete. Absent ``writing`` attr means complete (legacy/external
    stores never carry it).
    """
    try:
        root = zarr.open(str(path), mode='r')
        if _is_zarr_array(root):
            return _writing_flag_complete(root.attrs.get('writing'))
        names = dataset_names(root)
        if not names:
            return False
        image = resolve_image(root, names[0])
        return _writing_flag_complete(image.array.attrs.get('writing'))
    except Exception:
        return False


def _zarr_store_streamable(path: Any) -> bool:
    """True iff a Zarr store can be safely opened by a live reader NOW.

    Either the store is write-complete, or it carries the
    ``recording:frames_committed`` barrier — with the barrier,
    ``ZarrLiveSource._readable_length`` never reads past the flushed data,
    so mid-write following is safe. Legacy stores (no barrier) must wait for
    completion.
    """
    if _zarr_store_write_complete(path):
        return True
    try:
        root = zarr.open(str(path), mode='r')
        if _is_zarr_array(root):
            return root.attrs.get('recording:frames_committed') is not None
        names = dataset_names(root)
        if not names:
            return False
        image = resolve_image(root, names[0])
        return image.array.attrs.get('recording:frames_committed') is not None
    except Exception:
        return False


def _positions_for_length_step(length: float, step: float) -> int:
    """Canonical ImSwitch position count: ``round(length / step)``, at least 1.

    ImControl unified every axis-count computation onto ``round`` precisely
    because a mix of ``round``/``int``/``ceil`` made the GUI pixel count, the
    recorded dimensions and the real number of scanned lines disagree for
    non-divisible ratios. This module kept ``ceil``: a 0.52 um axis at 0.05 um
    made the live reader wait for 11 positions where the scan produced 10, so
    the stack never completed.
    """
    step = abs(float(step))
    if step == 0:
        return 1
    return max(1, int(round(abs(float(length)) / step)))


def _frames_per_stack_from_layout(resolved: Any) -> int | None:
    """Frames per scan stack according to the resolved layout, or ``None``.

    The resolver that gives :class:`StackInfo` its layout also decides how many
    frames one stack holds, rather than this module re-deriving it from the
    same attributes with its own arithmetic.
    """
    from imswitch.imcommon.model.acquisition_layout import (
        recorded_frames_per_time_point,
    )

    layout = getattr(resolved, "layout", None)
    if layout is None or not resolved.is_usable:
        return None
    kinds = {loop.kind for loop in layout.event_loops}
    if "scan_x" not in kinds or "scan_y" not in kinds:
        return None
    # A stack is what one time point produces -- conditions, a Z axis, a
    # repeat loop and a gated detector's spans included. ``scan_x * scan_y``
    # dropped all of those, so the reader waited for the wrong frame count.
    try:
        return recorded_frames_per_time_point(layout)
    except ValueError as error:
        # A layout that has no single stack size -- time repeated inside a
        # spatial axis, or a gated selection that differs between time points
        # -- is not something this reader can follow, and guessing a number
        # would make it slice frames from several time points into one stack.
        initLogger("LiveSource").warning(
            f"This recording has no single stack size, so the live reader "
            f"cannot follow it from its layout: {error}"
        )
        return None


def _derive_scan_frames_per_stack(attrs: dict[str, Any]) -> int | None:
    """Frames per MoNaLISA scan stack for a source with no resolvable layout.

    Prefer :func:`_frames_per_stack_from_layout`; this is the fallback for
    sources the resolver cannot describe. ``recording:frames_per_stack`` wins,
    then explicit ``ScanTTL:Nx``/``Ny``, then the canonical position count from
    ``axis_length``/``axis_step_size``. Metadata is read from flat attrs or a
    nested ``ImswitchData`` block so both the ImSwitch2 structured layout and
    legacy ImSwitch-1 Zarr work.
    """
    explicit = _coerce_positive_int(_meta_lookup(attrs, "recording:frames_per_stack"))
    if explicit is not None:
        return explicit

    nx_ttl = _coerce_positive_int(_meta_lookup(attrs, "ScanTTL:Nx"))
    ny_ttl = _coerce_positive_int(_meta_lookup(attrs, "ScanTTL:Ny"))
    if nx_ttl is not None and ny_ttl is not None:
        return nx_ttl * ny_ttl

    lengths = _numeric_vector(_meta_lookup(attrs, "ScanStage:axis_length"), 2)
    step_sizes = _numeric_vector(_meta_lookup(attrs, "ScanStage:axis_step_size"), 2)
    if lengths is None or step_sizes is None:
        return None
    if step_sizes[0] == 0 or step_sizes[1] == 0:
        return None

    return (
        _positions_for_length_step(lengths[0], step_sizes[0])
        * _positions_for_length_step(lengths[1], step_sizes[1])
    )


#: How much frame data one ``poll()`` may hand back. A poll used to return
#: every unread frame in one call, so a live source pointed at a recording
#: that had already finished materialised the whole recording before the
#: first chunk was processed -- and the lapse sources walked every timepoint
#: in the same call. The streaming path exists so that memory stays bounded;
#: this is the read-side twin of the detector queue's byte budget, and like
#: it the frame count follows from the frame. The cursor stays where the
#: budget ran out, so the next poll continues from there.
LIVE_POLL_MAX_BYTES = 64 * 1024 * 1024


def _chunk_nbytes(data: Any) -> int:
    nbytes = getattr(data, "nbytes", None)
    if nbytes is None:
        nbytes = np.asarray(data).nbytes
    return int(nbytes)


class LiveSource(ABC):
    """Polls a growing source and yields new raw-frame chunks."""

    #: Whether the stream legitimately goes quiet between logical stacks. The
    #: live stall watchdog (300 s by default) finalises a quiet stream as a
    #: crashed writer, so it is only right for a source that streams
    #: continuously -- and a source that streams continuously has to say so.
    #: The default used to be the other way round, switched off by a flag each
    #: new source author had to remember; forgetting it turned a slow
    #: timelapse into a silently short reconstruction.
    idles_between_stacks: bool = True

    @abstractmethod
    def open(self, path_or_handle: Any) -> StackInfo:
        """Open the source and return its stack metadata."""
        ...

    @abstractmethod
    def poll(self) -> list[Chunk]:
        """Return newly available chunks since the previous poll."""
        ...

    @abstractmethod
    def is_complete(self) -> bool:
        """Return whether the source has no more frames to yield."""
        ...

    def close(self) -> None:
        """Release source resources."""
        return None


class InMemoryStackWrapper:
    """Minimal DataObj-compatible wrapper for buffered live stacks."""

    def __init__(
        self,
        name: str,
        dataset_name: str,
        data: np.ndarray,
        attrs: dict[str, Any] | None = None,
        *,
        axis_labels: list[str] | None = None,
        axis_scales: list[float] | None = None,
        scale_unit: str | None = None,
    ):
        self.name = name
        self._datasetName = dataset_name
        self._data = np.asarray(data)
        self._attrs = dict(attrs or {})
        self.dataPath = None
        self._meanData = None

        # Mirror the DataObj axis/scale metadata contract so pass-through
        # reconstructors (e.g. View-only) get the same labels/calibration on the
        # in-memory wrapper path as on a batch load. Without these, their
        # process() raises AttributeError on the in-memory wrapper.
        ndim = self._data.ndim
        fallback_scales, fallback_unit = axis_scales_from_element_size(self._attrs, ndim)
        # Decode eagerly so a self-contradictory schema fails at construction
        # with a precise message, then publish the same ResolvedAcquisitionLayout
        # contract DataObj exposes. Handing plugins a bare AcquisitionLayout here
        # broke every consumer that reads .source/.confidence off the attribute.
        declared_layout = decode_layout_attrs(self._attrs)
        layout_axes = (
            list(declared_layout.storage_axes)
            if declared_layout is not None
            and len(declared_layout.storage_axes) == ndim
            else None
        )
        self._acquisitionLayout = None
        self._acquisitionLayoutAxes = tuple(axis_labels) if axis_labels else None
        self._axis_labels = list(axis_labels or layout_axes or default_axis_labels(ndim))
        self._axis_scales = list(axis_scales or fallback_scales or [1.0] * ndim)
        self._scale_unit = scale_unit or fallback_unit or "px"
        self.recording_lifecycle = normalize_recording_lifecycle(
            self._attrs,
            RecordingLifecycleMarkers(
                writing=_optional_bool(self._attrs.get("writing")),
                frames_committed=_optional_int(self._attrs.get("recording:frames_committed")),
            ),
        )
        self._source_info = {
            "dataset_name": dataset_name,
            "dataset_path": self._attrs.get("recording:dataset_path"),
            "source_format": self._attrs.get("recording:source_format"),
        }

    @property
    def acquisition_layout(self) -> ResolvedAcquisitionLayout:
        """Resolve lazily and cache, exactly as ``DataObj`` does.

        Resolution is deferred so a layout that conflicts with the array
        surfaces where a plugin asks for it, not while the live pipeline is
        wrapping a buffer.
        """
        if self._acquisitionLayout is None:
            self._acquisitionLayout = _resolve_stack_layout(
                self._attrs,
                shape=tuple(self._data.shape),
                detector=self._datasetName,
                source_path=None,
                dataset_path=self._attrs.get("recording:dataset_path"),
                axis_labels=self._acquisitionLayoutAxes,
            )
        return self._acquisitionLayout

    @property
    def datasetName(self) -> str:
        return self._datasetName

    @property
    def axis_labels(self) -> list[str]:
        return self._axis_labels

    @property
    def axis_scales(self) -> list[float]:
        return self._axis_scales

    @property
    def scale_unit(self) -> str:
        return self._scale_unit

    @property
    def source_info(self) -> dict[str, Any]:
        return self._source_info

    @property
    def data(self) -> np.ndarray:
        return self._data

    @property
    def attrs(self) -> dict[str, Any]:
        return self._attrs

    @property
    def dataLoaded(self) -> bool:
        return True

    @property
    def numFrames(self) -> int | None:
        return self._data.shape[0] if self._data.ndim > 0 else None

    def checkAndLoadData(self) -> None:
        return None

    def checkAndUnloadData(self) -> None:
        return None

    def getMeanData(self) -> np.ndarray:
        if self._meanData is None:
            self._meanData = np.array(np.mean(self._data, 0), dtype=np.float32)
        return self._meanData


class ZarrLiveSource(LiveSource):
    """Polls a growing Zarr array for new frames."""

    idles_between_stacks = False  # one growing array; quiet means the writer stopped

    def __init__(self, detector_name: str | None = None, chunk_size: int | None = None):
        """
        Args:
            detector_name: Detector name for structured layout. If None, auto-detect.
            chunk_size: Override chunk size for polling. If None, use array.chunks[0].
        """
        self._detector_name = detector_name
        self._chunk_size_override = chunk_size
        self._path: str | None = None
        self._root = None
        self._array = None
        self._array_path: tuple[str, ...] = ()
        self._cursor = 0
        self._chunk_size = 1
        self._expected_frames: int | None = None
        self._frames_committed: int | None = None
        self._writing = True

    def open(self, path_or_handle: Any) -> StackInfo:
        """Open the Zarr store and return stack metadata."""
        self._cursor = 0
        self._root = self._open_root(path_or_handle)

        if _is_zarr_array(self._root):
            self._array = self._root
            self._array_path = ()
            attrs = self._read_attrs()
            detector_name = (
                self._detector_name
                or attrs.get('recording:detector_name')
                or attrs.get('detector_name')
            )
        else:
            detector_name = self._detector_name or self._auto_detect_detector()
            self._array, attrs = self._open_array(detector_name)

        if self._array.ndim != 3:
            raise ValueError(f"Expected 3D array (T, Y, X), got shape {self._array.shape}")

        if self._chunk_size_override is not None:
            self._chunk_size = max(1, int(self._chunk_size_override))
        elif self._array.chunks is not None and len(self._array.chunks) > 0:
            self._chunk_size = max(1, int(self._array.chunks[0]))
        else:
            self._chunk_size = 1

        frame_shape = self._array.shape[-2:]
        self._refresh_state_from_attrs(attrs)

        # Legacy / metadata-less stores carry no recording:expected_frames. A
        # completed store (not writing) is one whole stack, so its length is the
        # expected frame count — without this, downstream startup would treat
        # frames_per_stack as unknown and grab only the first chunk.
        if self._expected_frames is None and not self._writing:
            self._expected_frames = int(self._array.shape[0])

        # frames_per_stack: an explicit recording attr wins; for a completed
        # single-stack store the exact array length is more reliable than the
        # scan-size guess (which is sensitive to the size-vs-endpoint length
        # convention); only fall back to the ScanStage-derived count for a
        # still-writing store with no explicit metadata.
        dataset_path = attrs.get('recording:dataset_path') or self._default_dataset_path()
        resolved = _resolve_stack_layout(
            attrs,
            shape=tuple(self._array.shape),
            detector=attrs.get('recording:detector_name') or attrs.get('detector_name') or detector_name,
            source_path=self._path,
            dataset_path=dataset_path,
        )

        # The resolved layout decides the stack size; the older attribute
        # parses below remain only for sources it cannot describe.
        recorded_fps = _frames_per_stack_from_layout(resolved)
        explicit_fps = _coerce_positive_int(_meta_lookup(attrs, "recording:frames_per_stack"))
        if recorded_fps is not None:
            frames_per_stack = recorded_fps
        elif explicit_fps is not None:
            frames_per_stack = explicit_fps
        elif not self._writing and self._expected_frames is not None:
            frames_per_stack = self._expected_frames
        else:
            frames_per_stack = _derive_scan_frames_per_stack(attrs)

        return StackInfo(
            frame_shape=frame_shape,
            dtype=self._array.dtype,
            attrs=dict(attrs),
            expected_frames=self._expected_frames,
            frames_per_stack=frames_per_stack,
            detector_name=attrs.get('recording:detector_name') or attrs.get('detector_name') or detector_name,
            dataset_path=dataset_path,
            source_format=attrs.get('recording:source_format') or 'ZARR',
            acquisition_layout=resolved,
        )

    def poll(self) -> list[Chunk]:
        """Return newly available chunks since the previous poll."""
        self._refresh_array()
        if self._array is None:
            return []

        self._refresh_state_from_attrs(self._read_attrs())
        readable_length = self._readable_length()
        if self._cursor >= readable_length:
            return []

        chunks = []
        queued = 0
        while self._cursor < readable_length:
            start = self._cursor
            end = min(start + self._chunk_size, readable_length)

            data = self._array[start:end]
            chunks.append(Chunk(data=data, start=start, end=end))

            self._cursor = end
            queued += _chunk_nbytes(data)
            if queued >= LIVE_POLL_MAX_BYTES:
                break

        return chunks

    def is_complete(self) -> bool:
        """Return whether the source has no more frames to yield."""
        self._refresh_array()
        if self._array is None:
            return True

        self._refresh_state_from_attrs(self._read_attrs())
        current_length = self._array.shape[0]

        if self._expected_frames is not None:
            if self._cursor >= self._expected_frames:
                return True

        if not self._writing and self._cursor >= current_length:
            return True

        return False

    def close(self) -> None:
        """Release source resources."""
        self._path = None
        self._root = None
        self._array = None
        self._array_path = ()

    def _auto_detect_detector(self) -> str:
        """Auto-detect detector name from root contents."""
        if self._root is None or _is_zarr_array(self._root):
            raise ValueError("Root not opened")

        names = dataset_names(self._root)
        if names:
            return names[0]

        raise ValueError("No detector array found in Zarr store")

    def _open_array(self, detector_name: str) -> tuple[Any, dict[str, Any]]:
        """Open array and collect attributes from structured or legacy layout."""
        if self._root is None or _is_zarr_array(self._root):
            raise ValueError("Root not opened")

        names = dataset_names(self._root)
        if detector_name not in names:
            if detector_name in self._root and _is_zarr_group(self._root[detector_name]):
                raise ValueError(f"Detector group '{detector_name}' has no 'data' array")
            raise ValueError(f"Detector '{detector_name}' not found in Zarr store")

        image = resolve_image(
            self._root,
            detector_name,
            validate_layout_metadata=False,
        )
        self._array = image.array
        self._array_path = tuple(image.array_path.split('/')) if image.array_path else ()
        attrs = self._read_attrs()
        attrs.update(image.attrs)
        return image.array, attrs

    def _open_root(self, path_or_handle: Any) -> Any:
        if _is_zarr_group(path_or_handle) or _is_zarr_array(path_or_handle):
            self._path = None
            return path_or_handle

        path = Path(path_or_handle)
        self._path = str(path)
        return zarr.open(str(path), mode='r')

    def _refresh_array(self) -> None:
        if self._path is None or self._array is None:
            return

        self._root = zarr.open(self._path, mode='r')
        node = self._root
        for part in self._array_path:
            node = node[part]
        self._array = node

    def _read_attrs(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        if self._array is None:
            return attrs

        if _is_zarr_group(self._root):
            attrs.update(dict(self._root.attrs))

            if self._array_path:
                detector_group = self._root
                for component in self._array_path[:-1]:
                    detector_group = detector_group[component]
                attrs.update(dict(self._array.attrs))
                if _is_zarr_group(detector_group) and "metadata" in detector_group:
                    attrs.update(flatten_metadata_attrs(detector_group["metadata"]))
                return attrs

        attrs.update(dict(self._array.attrs))
        return attrs

    def _refresh_state_from_attrs(self, attrs: dict[str, Any]) -> None:
        expected_frames = self._coerce_int(attrs.get("recording:planned_frames"))
        if expected_frames is None:
            expected_frames = self._coerce_int(attrs.get("recording:expected_frames"))
        if expected_frames is not None:
            self._expected_frames = expected_frames

        lifecycle = normalize_recording_lifecycle(
            attrs,
            RecordingLifecycleMarkers(
                writing=_optional_bool(attrs.get("writing")),
                frames_committed=self._coerce_int(attrs.get("recording:frames_committed")),
                live_writer_attached=True,
            ),
        )
        self._frames_committed = lifecycle.frames_committed
        self._writing = lifecycle.writer_state == "writing"

    def _readable_length(self) -> int:
        if self._array is None:
            return 0

        # frames_committed is the write barrier: ZarrStorer resizes the array
        # BEFORE writing frame data, so shape alone runs ahead of the data on
        # disk and a shape-trusting reader bakes zero frames into the stream
        # (root cause 1 in docs/live_reconstruction_audit.md). Stores without
        # the barrier (legacy/external) keep the shape behavior - they are
        # only opened once complete, so no writer is racing them.
        current_length = int(self._array.shape[0])
        if self._frames_committed is not None:
            current_length = min(current_length, self._frames_committed)
        if self._expected_frames is None:
            return current_length
        return min(current_length, self._expected_frames)

    def _default_dataset_path(self) -> str | None:
        if not self._array_path:
            return None
        return '/' + '/'.join(self._array_path)

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _coerce_bool(value: Any, *, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, str):
            return value.strip().lower() not in {'0', 'false', 'no', 'off'}
        return bool(value)


def _lapse_index_template(path: str):
    """Derive how to build sibling per-file-lapse paths from one file name.

    Matches the scan index in names like ``..._scan__00__CAM.zarr`` (legacy)
    or ``..._scan0.zarr`` (ImSwitch2). Returns ``(folder, prefix, width,
    suffix, index)`` so ``prefix + str(i).zfill(width) + suffix`` reconstructs
    timepoint ``i``'s file, or ``None`` if the name carries no scan index.
    """
    folder = os.path.dirname(path)
    name = os.path.basename(path)
    match = re.search(r"(?i)scan[_]*(\d+)", name)
    if match is None:
        return None
    start, end = match.span(1)
    width = end - start
    return folder, name[:start], width, name[end:], int(match.group(1))


def _highest_lapse_index_span(template, first_index: int) -> int:
    """How many timepoint slots the files on disk span, gaps included.

    Returns ``highest_index - first_index + 1`` by scanning the folder for
    names matching the template, so a skipped timepoint does not truncate the
    range. Falls back to ``1`` when there is no template or the folder cannot
    be listed.
    """
    if template is None:
        return 1
    folder, prefix, _width, suffix, _ = template
    try:
        names = os.listdir(folder)
    except OSError:
        return 1

    highest = first_index
    for name in names:
        if not name.startswith(prefix) or not name.endswith(suffix):
            continue
        digits = name[len(prefix):len(name) - len(suffix)]
        if digits.isdigit():
            highest = max(highest, int(digits))
    return highest - first_index + 1


class ZarrMultiFileLapseSource(LiveSource):
    """Stream a per-file timelapse (one ``.zarr`` per timepoint) as one stream.

    Each ``<base>_scan<NN>_<...>.zarr`` store holds a single stack (one
    timepoint). Files are streamed in index order and frames are emitted with
    GLOBAL indices (``position * frames_per_stack + local``) so a streaming
    session's ``time_index = start // frames_per_stack`` routes each file into
    its own timepoint, accumulating one multi-timepoint result. Later timepoint
    files (live recording) are picked up by re-deriving the next index path.

    ``position`` is the timepoint's **true** index (relative to the first
    file), not a count of files streamed. An acquisition can skip a timepoint
    -- t0, t1, t2, t4 -- and keeping the true index is what leaves t3's slot
    untouched in the session's pre-zeroed output, so it shows as blank and the
    viewer's timepoint slider jumps straight from t2 to t4.
    """

    # Multiple timepoints stream through one worker run; between timepoints the
    # worker idles while the recorder writes the next _scanNN.zarr store, so the
    # stall watchdog must not treat that gap as a crashed writer.
    idles_between_stacks = True

    def __init__(
        self,
        first_path,
        detector_name=None,
        chunk_size=None,
        num_timepoints=None,
    ) -> None:
        self._first_path = str(first_path)
        self._detector_name = detector_name
        self._chunk_size = chunk_size
        self._num_timepoints = num_timepoints
        self._inner: ZarrLiveSource | None = None
        self._template = None
        self._first_index = 0
        self._position = 0  # 0-based TRUE timepoint index (gaps preserved)
        self._frames_per_stack = 1
        self._logger = initLogger(self, tryInheritParent=False)

    def open(self, path_or_handle: Any) -> StackInfo:
        first = str(path_or_handle) if path_or_handle is not None else self._first_path
        self._first_path = first
        self._template = _lapse_index_template(first)
        self._first_index = self._template[4] if self._template else 0
        self._position = 0

        self._inner = ZarrLiveSource(self._detector_name, self._chunk_size)
        info = self._inner.open(first)
        self._frames_per_stack = int(info.frames_per_stack or info.expected_frames or 1)

        num_tp = (
            self._num_timepoints
            or _coerce_positive_int(_meta_lookup(info.attrs, "recording:num_timepoints"))
            or _coerce_positive_int(_meta_lookup(info.attrs, "Rec:LapseTime"))
            or self._count_present_timepoints()
        )
        self._num_timepoints = max(1, int(num_tp or 1))
        expected_total = self._frames_per_stack * self._num_timepoints

        return StackInfo(
            frame_shape=info.frame_shape,
            dtype=info.dtype,
            attrs=dict(info.attrs),
            expected_frames=expected_total,
            frames_per_stack=self._frames_per_stack,
            detector_name=info.detector_name,
            dataset_path=info.dataset_path,
            source_format=info.source_format or "ZARR",
            acquisition_layout=info.acquisition_layout,
        )

    def poll(self) -> list[Chunk]:
        if self._inner is None:
            return []

        offset = self._position * self._frames_per_stack
        chunks = [Chunk(c.data, c.start + offset, c.end + offset) for c in self._inner.poll()]

        # Current timepoint fully read: advance to the next file once it is
        # safely readable — write-complete, or mid-write WITH the
        # frames_committed barrier (the inner reader then never runs ahead of
        # flushed data). Plain existence is not enough: Zarr resizes the array
        # before writing data, so opening a barrier-less mid-write store reads
        # uninitialised (zero) frames that get baked into the stream
        # permanently — the HDF5 sibling can advance on existence because SWMR
        # flush ordering makes shape-visible imply data-visible, but Zarr
        # cannot.
        if self._inner.is_complete() and self._position + 1 < self._num_timepoints:
            nxt = self._next_available_position()
            if nxt is not None:
                if nxt != self._position + 1:
                    missing = ", ".join(str(p) for p in range(self._position + 1, nxt))
                    self._logger.warning(
                        f"Timepoint(s) {missing} never appeared but timepoint {nxt} "
                        f"did; leaving the gap blank and continuing at {nxt}"
                    )
                self._inner.close()
                self._position = nxt
                self._inner = ZarrLiveSource(self._detector_name, self._chunk_size)
                self._inner.open(self._build_path(nxt))
        return chunks

    def _next_available_position(self) -> int | None:
        """The next timepoint to stream, or ``None`` to keep waiting.

        Normally that is simply the next index. When the next index is absent
        but a LATER one already exists, the acquisition skipped that timepoint
        -- the recorder writes in order, so a later file existing means the
        earlier one is never coming -- and we jump to the first index present.
        The skipped position is never streamed, so the session's pre-zeroed
        output keeps a blank slot there.

        If the first present store exists but is not safe to stream yet
        (mid-write without a commit barrier), wait for it rather than looking
        further ahead: it is the next real timepoint, just not ready.
        """
        for position in range(self._position + 1, self._num_timepoints):
            path = self._build_path(position)
            if path is None or not os.path.exists(path):
                continue
            if not _zarr_store_streamable(path):
                return None
            return position
        return None

    def is_complete(self) -> bool:
        if self._inner is None:
            return True
        if not self._inner.is_complete():
            return False
        # On the last expected timepoint with its file fully read -> done.
        if self._position + 1 >= self._num_timepoints:
            return True
        # Otherwise wait for the next timepoint file (live). If it isn't going
        # to appear, the user stops live mode; we don't guess completion here.
        return False

    def close(self) -> None:
        if self._inner is not None:
            self._inner.close()
        self._inner = None

    def _build_path(self, position: int) -> str | None:
        if self._template is None:
            return None
        folder, prefix, width, suffix, _ = self._template
        index = self._first_index + position
        return os.path.join(folder, f"{prefix}{index:0{width}d}{suffix}")

    def _count_present_timepoints(self) -> int:
        """Timepoint slots to allocate when the metadata does not say.

        Taken from the HIGHEST index on disk rather than a contiguous walk: a
        skipped timepoint would end that walk early and cap the range, so every
        later file would be silently dropped.
        """
        return _highest_lapse_index_span(self._template, self._first_index)


class ZarrLapseSource(LiveSource):
    """Polls a single-file scan{N} timelapse store as one continuous global frame stream."""

    idles_between_stacks = True  # Single file with multiple scan{N} groups; worker idles while recorder prepares next timepoint.

    def __init__(self, detector_name: str | None = None, chunk_size: int | None = None):
        """
        Args:
            detector_name: Detector name inside scan groups. If None, auto-detect.
            chunk_size: Override chunk size for polling. If None, use array.chunks[0].
        """
        self._detector_name = detector_name
        self._chunk_size_override = chunk_size
        self._path: str | None = None
        self._root = None
        self._scan_groups: list[str] = []
        self._current_group_index = 0
        self._global_cursor = 0
        self._local_cursor = 0
        self._chunk_size = 1
        self._frames_per_stack: int | None = None
        self._num_timepoints: int | None = None
        self._current_array = None
        self._current_array_path: tuple[str, ...] = ()
        self._resolved_detector_name: str | None = None
        self._writing = True

    def open(self, path_or_handle: Any) -> StackInfo:
        """Open the Zarr store and return stack metadata."""
        self._global_cursor = 0
        self._local_cursor = 0
        self._current_group_index = 0
        
        self._root = self._open_root(path_or_handle)
        
        if _is_zarr_array(self._root):
            raise ValueError("ZarrLapseSource expects a group with scan{N} structure, not a raw array")
        
        # Enumerate scan{N} groups
        self._scan_groups = sorted(
            [key for key in self._root.keys() if key.startswith('scan') and key[4:].isdigit()],
            key=lambda x: int(x[4:])
        )
        
        if not self._scan_groups:
            raise ValueError("No scan{N} groups found in Zarr store")
        
        # Open scan0 to detect detector and read metadata
        scan0 = self._root[self._scan_groups[0]]
        self._resolved_detector_name = self._detector_name or self._auto_detect_detector(scan0)
        
        # Open the detector array in scan0
        self._current_array, attrs = self._open_detector_array(scan0, self._resolved_detector_name)
        self._current_array_path = (self._scan_groups[0], self._resolved_detector_name, 'data')
        
        if self._current_array.ndim != 3:
            raise ValueError(f"Expected 3D array (T, Y, X), got shape {self._current_array.shape}")
        
        # Determine chunk size
        if self._chunk_size_override is not None:
            self._chunk_size = max(1, int(self._chunk_size_override))
        elif self._current_array.chunks is not None and len(self._current_array.chunks) > 0:
            self._chunk_size = max(1, int(self._current_array.chunks[0]))
        else:
            self._chunk_size = 1
        
        # Extract frames_per_stack and num_timepoints
        self._frames_per_stack = self._coerce_int(attrs.get('recording:frames_per_stack'))
        if self._frames_per_stack is None:
            self._frames_per_stack = self._current_array.shape[0]
        
        self._num_timepoints = self._coerce_int(attrs.get('recording:num_timepoints'))
        self._refresh_state_from_attrs(attrs)
        
        # Calculate expected_frames
        expected_frames = None
        if self._num_timepoints is not None and self._frames_per_stack is not None:
            expected_frames = self._num_timepoints * self._frames_per_stack
        
        frame_shape = self._current_array.shape[-2:]
        
        dataset_path = attrs.get('recording:dataset_path')
        return StackInfo(
            frame_shape=frame_shape,
            dtype=self._current_array.dtype,
            attrs=dict(attrs),
            expected_frames=expected_frames,
            frames_per_stack=self._frames_per_stack,
            detector_name=attrs.get('recording:detector_name') or attrs.get('detector_name') or self._resolved_detector_name,
            dataset_path=dataset_path,
            source_format=attrs.get('recording:source_format') or 'ZARR',
            acquisition_layout=_resolve_stack_layout(
                attrs,
                shape=tuple(self._current_array.shape),
                detector=(
                    attrs.get('recording:detector_name')
                    or attrs.get('detector_name')
                    or self._resolved_detector_name
                ),
                source_path=self._path,
                dataset_path=dataset_path,
            ),
        )

    def poll(self) -> list[Chunk]:
        """Return newly available chunks with GLOBAL indices across all scan groups."""
        if self._current_array is None:
            return []
        
        chunks = []
        queued = 0

        while True:
            # Refresh current array and re-enumerate scan groups to detect new ones
            self._refresh_current_array()
            self._refresh_scan_groups()
            
            # Read current group's attributes
            current_group = self._root[self._scan_groups[self._current_group_index]]
            _, attrs = self._open_detector_array(current_group, self._resolved_detector_name)
            self._refresh_state_from_attrs(attrs)
            
            # Determine how many frames we can read from current group
            readable_length = min(self._current_array.shape[0], self._frames_per_stack or self._current_array.shape[0])
            
            if self._local_cursor >= readable_length:
                # Current group is fully read, try to advance to next
                if self._current_group_index + 1 < len(self._scan_groups):
                    # Check if next group exists
                    next_group_name = self._scan_groups[self._current_group_index + 1]
                    if next_group_name in self._root:
                        # Advance to next group
                        self._current_group_index += 1
                        self._local_cursor = 0
                        next_group = self._root[next_group_name]
                        self._current_array, _ = self._open_detector_array(next_group, self._resolved_detector_name)
                        self._current_array_path = (next_group_name, self._resolved_detector_name, 'data')
                        continue
                    else:
                        # Next group doesn't exist yet, wait
                        break
                else:
                    # No more groups expected
                    break
            
            # Read chunks from current group
            start_local = self._local_cursor
            end_local = min(start_local + self._chunk_size, readable_length)
            
            if start_local >= end_local:
                break
            
            # Calculate GLOBAL indices
            start_global = self._current_group_index * (self._frames_per_stack or 0) + start_local
            end_global = self._current_group_index * (self._frames_per_stack or 0) + end_local
            
            data = self._current_array[start_local:end_local]
            chunks.append(Chunk(data=data, start=start_global, end=end_global))
            
            self._local_cursor = end_local
            self._global_cursor = end_global
            queued += _chunk_nbytes(data)
            if queued >= LIVE_POLL_MAX_BYTES:
                break

            # If we haven't filled a full chunk, stop polling (wait for more data)
            if end_local - start_local < self._chunk_size and end_local < readable_length:
                break
        
        return chunks

    def is_complete(self) -> bool:
        """Return whether all timepoints have been fully consumed."""
        if self._current_array is None:
            return True
        
        # If we know the total expected frames, check against global cursor
        if self._num_timepoints is not None and self._frames_per_stack is not None:
            expected_total = self._num_timepoints * self._frames_per_stack
            if self._global_cursor >= expected_total:
                return True
        
        # Check if we're on the last expected group
        if self._num_timepoints is not None:
            if self._current_group_index >= self._num_timepoints - 1:
                # We're on or past the last expected group
                self._refresh_current_array()
                current_group = self._root[self._scan_groups[self._current_group_index]]
                _, attrs = self._open_detector_array(current_group, self._resolved_detector_name)
                self._refresh_state_from_attrs(attrs)
                
                readable_length = min(self._current_array.shape[0], self._frames_per_stack or self._current_array.shape[0])
                
                if not self._writing and self._local_cursor >= readable_length:
                    return True
        
        return False

    def close(self) -> None:
        """Release source resources."""
        self._path = None
        self._root = None
        self._current_array = None
        self._current_array_path = ()
        self._scan_groups = []

    def _auto_detect_detector(self, scan_group: Any) -> str:
        """Auto-detect detector name from scan group contents."""
        for key in scan_group.keys():
            item = scan_group[key]
            if _is_zarr_group(item) and 'data' in item:
                return key
        
        raise ValueError("No detector group with 'data' array found in scan group")

    def _open_detector_array(self, scan_group: Any, detector_name: str) -> tuple[Any, dict[str, Any]]:
        """Open detector array and collect flattened attributes."""
        if detector_name not in scan_group:
            raise ValueError(f"Detector '{detector_name}' not found in scan group")
        
        det_group = scan_group[detector_name]
        if not _is_zarr_group(det_group):
            raise ValueError(f"'{detector_name}' is not a group")
        
        if 'data' not in det_group:
            raise ValueError(f"Detector group '{detector_name}' has no 'data' array")
        
        array = det_group['data']
        
        # Flatten attributes from root, detector group, and metadata
        attrs: dict[str, Any] = {}
        
        # Root attrs
        if _is_zarr_group(self._root):
            attrs.update(dict(self._root.attrs))
        
        # Array attrs
        attrs.update(dict(array.attrs))
        
        # Metadata group
        if 'metadata' in det_group:
            self._flatten_metadata(det_group['metadata'], attrs, prefix='')
        
        return array, attrs

    def _flatten_metadata(self, group: Any, attrs: dict[str, Any], prefix: str) -> None:
        """Recursively flatten metadata group into attrs dict with category prefixes."""
        for key in group.attrs.keys():
            flat_key = f"{prefix}{key}" if prefix else key
            attrs[flat_key] = group.attrs[key]
        
        for subgroup_name in group.keys():
            subgroup = group[subgroup_name]
            if _is_zarr_group(subgroup):
                new_prefix = f"{subgroup_name}:" if not prefix else f"{prefix}{subgroup_name}:"
                self._flatten_metadata(subgroup, attrs, new_prefix)

    def _open_root(self, path_or_handle: Any) -> Any:
        if _is_zarr_group(path_or_handle):
            self._path = None
            return path_or_handle
        
        path = Path(path_or_handle)
        self._path = str(path)
        return zarr.open(str(path), mode='r')

    def _refresh_current_array(self) -> None:
        """Re-open current array to see new frames."""
        if self._path is None or self._current_array is None:
            return
        
        self._root = zarr.open(self._path, mode='r')
        current_group = self._root[self._scan_groups[self._current_group_index]]
        det_group = current_group[self._resolved_detector_name]
        self._current_array = det_group['data']

    def _refresh_scan_groups(self) -> None:
        """Re-enumerate scan groups to detect newly added ones."""
        if self._path is None:
            return
        
        self._scan_groups = sorted(
            [key for key in self._root.keys() if key.startswith('scan') and key[4:].isdigit()],
            key=lambda x: int(x[4:])
        )

    def _refresh_state_from_attrs(self, attrs: dict[str, Any]) -> None:
        """Update internal state from attributes."""
        self._writing = self._coerce_bool(_meta_lookup(attrs, 'writing'), default=self._writing)

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _coerce_bool(value: Any, *, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, str):
            return value.strip().lower() not in {'0', 'false', 'no', 'off'}
        return bool(value)


class Hdf5LiveSource(LiveSource):
    """Polls a growing HDF5 dataset for new frames (SWMR protocol)."""

    idles_between_stacks = False  # one growing array; quiet means the writer stopped

    def __init__(self, detector_name: str | None = None, chunk_size: int | None = None):
        """
        Args:
            detector_name: Detector name for structured layout. If None, auto-detect.
            chunk_size: Override chunk size for polling. If None, use dataset.chunks[0].
        """
        self._detector_name = detector_name
        self._chunk_size_override = chunk_size
        self._path: str | None = None
        self._file = None
        self._dataset = None
        self._dataset_path: str | None = None
        self._cursor = 0
        self._chunk_size = 1
        self._expected_frames: int | None = None
        self._frames_committed: int | None = None
        self._stream_complete: bool | None = None
        self._committed_ds = None
        self._complete_ds = None
        self._writing = True
        self._owns_file = False

    def open(self, path_or_handle: Any) -> StackInfo:
        """Open the HDF5 file in SWMR mode and return stack metadata."""
        self._cursor = 0

        if isinstance(path_or_handle, h5py.File):
            self._file = path_or_handle
            self._path = None
            self._owns_file = False
        else:
            path = Path(path_or_handle)
            self._path = str(path)
            self._file = h5py.File(self._path, 'r', libver='latest', swmr=True)
            self._owns_file = True

        attrs = self._read_root_attrs()
        detector_name = self._detector_name or attrs.get('recording:detector_name')

        if detector_name is None:
            detector_name = self._auto_detect_detector()

        self._dataset, dataset_attrs = self._open_dataset(detector_name, attrs)

        if self._dataset.ndim != 3:
            raise ValueError(f"Expected 3D dataset (T, Y, X), got shape {self._dataset.shape}")

        # Committed-frames barrier + live completion marker: 1-element side
        # datasets next to 'data' (SWMR forbids attr writes, so the recorder
        # cannot use attrs for these; and the final writing=False attr is
        # rewritten only AFTER close, invisible to this live SWMR handle).
        # Absent on legacy/external files -> both stay None.
        parent = self._dataset.parent
        self._committed_ds = parent.get('frames_committed') \
            if isinstance(parent, h5py.Group) else None
        self._complete_ds = parent.get('stream_complete') \
            if isinstance(parent, h5py.Group) else None

        if self._chunk_size_override is not None:
            self._chunk_size = max(1, int(self._chunk_size_override))
        elif self._dataset.chunks is not None and len(self._dataset.chunks) > 0:
            self._chunk_size = max(1, int(self._dataset.chunks[0]))
        else:
            self._chunk_size = 1

        frame_shape = self._dataset.shape[-2:]
        all_attrs = {**attrs, **dataset_attrs}
        self._refresh_state_from_attrs(all_attrs)
        self._refresh_barrier_state()
        self._refresh_state_from_attrs(all_attrs)
        dataset_path = all_attrs.get('recording:dataset_path') or self._dataset_path
        resolved = _resolve_stack_layout(
            all_attrs,
            shape=tuple(self._dataset.shape),
            detector=all_attrs.get('recording:detector_name') or detector_name,
            source_path=self._path,
            dataset_path=dataset_path,
        )
        # The resolved layout decides the stack size; the older attribute parse
        # remains only for sources it cannot describe.
        frames_per_stack = _frames_per_stack_from_layout(resolved)
        if frames_per_stack is None:
            frames_per_stack = _derive_scan_frames_per_stack(all_attrs)

        return StackInfo(
            frame_shape=frame_shape,
            dtype=self._dataset.dtype,
            attrs=dict(all_attrs),
            expected_frames=self._expected_frames,
            frames_per_stack=frames_per_stack,
            detector_name=all_attrs.get('recording:detector_name') or detector_name,
            dataset_path=dataset_path,
            source_format=all_attrs.get('recording:source_format') or 'HDF5',
            acquisition_layout=resolved,
        )

    def poll(self) -> list[Chunk]:
        """Return newly available chunks since the previous poll."""
        if self._dataset is None:
            return []

        self._dataset.refresh()
        self._refresh_state_from_attrs(self._read_dataset_attrs())
        self._refresh_barrier_state()
        self._refresh_state_from_attrs(self._read_dataset_attrs())
        readable_length = self._readable_length()

        if self._cursor >= readable_length:
            return []

        chunks = []
        queued = 0
        while self._cursor < readable_length:
            start = self._cursor
            end = min(start + self._chunk_size, readable_length)

            data = self._dataset[start:end]
            chunks.append(Chunk(data=data, start=start, end=end))

            self._cursor = end
            queued += _chunk_nbytes(data)
            if queued >= LIVE_POLL_MAX_BYTES:
                break

        return chunks

    def is_complete(self) -> bool:
        """Return whether the source has no more frames to yield."""
        if self._dataset is None:
            return True

        self._dataset.refresh()
        self._refresh_state_from_attrs(self._read_dataset_attrs())
        self._refresh_barrier_state()
        self._refresh_state_from_attrs(self._read_dataset_attrs())
        current_length = self._dataset.shape[0]

        if self._expected_frames is not None:
            if self._cursor >= self._expected_frames:
                return True

        # Live completion: the recorder sets the stream_complete marker while
        # its SWMR handle is still open, so a live reader terminates even for
        # recordings with no expected_frames (SpecTime/UntilStop). The
        # writing-attr path below only works for readers that (re)opened the
        # file after the recorder closed it.
        if self._stream_complete and self._cursor >= self._readable_length():
            return True

        if not self._writing and self._cursor >= current_length:
            return True

        return False

    def _refresh_barrier_state(self) -> None:
        """Re-read the frames_committed / stream_complete side datasets."""
        try:
            if self._committed_ds is not None:
                self._committed_ds.refresh()
                self._frames_committed = int(self._committed_ds[0])
            if self._complete_ds is not None:
                self._complete_ds.refresh()
                self._stream_complete = bool(self._complete_ds[0])
        except Exception:
            # RAM/BytesIO handles have no SWMR refresh; fall back to a plain
            # read (values are in-process there anyway).
            try:
                if self._committed_ds is not None:
                    self._frames_committed = int(self._committed_ds[0])
                if self._complete_ds is not None:
                    self._stream_complete = bool(self._complete_ds[0])
            except Exception:
                pass

    def close(self) -> None:
        """Release source resources."""
        if self._owns_file and self._file is not None:
            self._file.close()
        self._path = None
        self._file = None
        self._dataset = None
        self._dataset_path = None
        self._committed_ds = None
        self._complete_ds = None

    def _auto_detect_detector(self) -> str:
        """Auto-detect detector name from file contents."""
        if self._file is None:
            raise ValueError("File not opened")

        for key in self._file.keys():
            item = self._file[key]
            if isinstance(item, h5py.Group):
                if 'data' in item:
                    return key
            elif isinstance(item, h5py.Dataset) and item.ndim == 3:
                return key

        raise ValueError("No detector dataset found in HDF5 file")

    def _open_dataset(self, detector_name: str, root_attrs: dict[str, Any]) -> tuple[h5py.Dataset, dict[str, Any]]:
        """Open dataset and collect attributes from structured or legacy layout."""
        if self._file is None:
            raise ValueError("File not opened")

        dataset_path = root_attrs.get('recording:dataset_path')
        if dataset_path:
            if dataset_path.startswith('/'):
                dataset_path = dataset_path[1:]
            if dataset_path in self._file:
                self._dataset_path = '/' + dataset_path
                dataset = self._file[dataset_path]
                return dataset, self._read_dataset_attrs_at(dataset.name)

        if detector_name in self._file:
            item = self._file[detector_name]

            if isinstance(item, h5py.Group):
                if 'data' not in item:
                    raise ValueError(f"Detector group '{detector_name}' has no 'data' dataset")
                dataset = item['data']
                self._dataset_path = f'/{detector_name}/data'

            elif isinstance(item, h5py.Dataset):
                dataset = item
                self._dataset_path = f'/{detector_name}'

            else:
                raise ValueError(f"'{detector_name}' is neither a group nor a dataset")
        else:
            raise ValueError(f"Detector '{detector_name}' not found in HDF5 file")

        return dataset, self._read_dataset_attrs_at(dataset.name)

    def _read_root_attrs(self) -> dict[str, Any]:
        """Read and flatten root-level attributes."""
        attrs: dict[str, Any] = {}
        if self._file is None:
            return attrs

        attrs.update(dict(self._file.attrs))
        return attrs

    def _read_dataset_attrs(self) -> dict[str, Any]:
        """Read and flatten dataset-level attributes."""
        if self._dataset is None:
            return {}
        return self._read_dataset_attrs_at(self._dataset.name)

    def _read_dataset_attrs_at(self, path: str) -> dict[str, Any]:
        """Read attributes at a specific path, flattening recording: prefixes and metadata groups."""
        attrs: dict[str, Any] = {}
        if self._file is None:
            return attrs

        dataset = self._file[path]
        for key, value in dataset.attrs.items():
            attrs[key] = value

        # Find parent group path and check for metadata group
        path_parts = path.strip('/').split('/')
        if len(path_parts) > 1:
            parent_path = '/' + '/'.join(path_parts[:-1])
            if parent_path in self._file:
                parent = self._file[parent_path]
                if isinstance(parent, h5py.Group) and 'metadata' in parent:
                    attrs.update(flatten_metadata_attrs(parent["metadata"]))

        return attrs

    def _flatten_metadata(self, group: h5py.Group, attrs: dict[str, Any], prefix: str) -> None:
        """Recursively flatten metadata group into attrs dict with category prefixes."""
        for key in group.attrs.keys():
            flat_key = f"{prefix}{key}" if prefix else key
            attrs[flat_key] = group.attrs[key]

        for subgroup_name in group.keys():
            subgroup = group[subgroup_name]
            if isinstance(subgroup, h5py.Group):
                new_prefix = f"{subgroup_name}:" if not prefix else f"{prefix}{subgroup_name}:"
                self._flatten_metadata(subgroup, attrs, new_prefix)

    def _refresh_state_from_attrs(self, attrs: dict[str, Any]) -> None:
        """Update internal state from attributes."""
        expected_frames = self._coerce_int(attrs.get("recording:planned_frames"))
        if expected_frames is None:
            expected_frames = self._coerce_int(attrs.get("recording:expected_frames"))
        if expected_frames is not None:
            self._expected_frames = expected_frames

        lifecycle = normalize_recording_lifecycle(
            attrs,
            RecordingLifecycleMarkers(
                writing=_optional_bool(attrs.get("writing")),
                frames_committed=self._frames_committed,
                stream_complete=self._stream_complete,
                live_writer_attached=True,
            ),
        )
        self._frames_committed = lifecycle.frames_committed
        self._writing = lifecycle.writer_state == "writing"

    def _readable_length(self) -> int:
        """Return the number of frames that can be read.

        Capped by the frames_committed barrier when present, so a live reader
        never runs ahead of the data the recorder has actually flushed.
        """
        if self._dataset is None:
            return 0

        current_length = int(self._dataset.shape[0])
        if self._frames_committed is not None:
            current_length = min(current_length, self._frames_committed)
        if self._expected_frames is None:
            return current_length
        return min(current_length, self._expected_frames)

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _coerce_bool(value: Any, *, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, str):
            return value.strip().lower() not in {'0', 'false', 'no', 'off'}
        return bool(value)


class Hdf5MultiFileLapseSource(LiveSource):
    """Stream a per-file HDF5 timelapse (one ``.h5`` per timepoint) as one stream.

    Each ``<base>_scan<NN>_<...>.h5`` file holds a single stack (one
    timepoint). Files are streamed in index order and frames are emitted with
    GLOBAL indices (``position * frames_per_stack + local``) so a streaming
    session's ``time_index = start // frames_per_stack`` routes each file into
    its own timepoint, accumulating one multi-timepoint result. Later timepoint
    files (live recording) are picked up by re-deriving the next index path.
    """

    # Multiple timepoints stream through one worker run; between timepoints the
    # worker idles while the recorder writes the next _scanNN.h5 file, so the
    # stall watchdog must not treat that gap as a crashed writer.
    idles_between_stacks = True

    def __init__(self, first_path, detector_name=None, chunk_size=None,
                 num_timepoints=None):
        self._first_path = str(first_path)
        self._detector_name = detector_name
        self._chunk_size = chunk_size
        self._num_timepoints = num_timepoints
        self._inner: Hdf5LiveSource | None = None
        self._template = None
        self._first_index = 0
        self._position = 0  # 0-based TRUE timepoint index (gaps preserved)
        self._frames_per_stack = 1
        self._logger = initLogger(self, tryInheritParent=False)

    def open(self, path_or_handle: Any) -> StackInfo:
        first = str(path_or_handle) if path_or_handle is not None else self._first_path
        self._first_path = first
        self._template = _lapse_index_template(first)
        self._first_index = self._template[4] if self._template else 0
        self._position = 0

        self._inner = Hdf5LiveSource(self._detector_name, self._chunk_size)
        info = self._inner.open(first)
        self._frames_per_stack = int(info.frames_per_stack or info.expected_frames or 1)

        num_tp = (
            self._num_timepoints
            or _coerce_positive_int(_meta_lookup(info.attrs, "recording:num_timepoints"))
            or _coerce_positive_int(_meta_lookup(info.attrs, "Rec:LapseTime"))
            or self._count_present_timepoints()
        )
        self._num_timepoints = max(1, int(num_tp or 1))
        expected_total = self._frames_per_stack * self._num_timepoints

        return StackInfo(
            frame_shape=info.frame_shape,
            dtype=info.dtype,
            attrs=dict(info.attrs),
            expected_frames=expected_total,
            frames_per_stack=self._frames_per_stack,
            detector_name=info.detector_name,
            dataset_path=info.dataset_path,
            source_format=info.source_format or "HDF5",
            acquisition_layout=info.acquisition_layout,
        )

    def poll(self) -> list[Chunk]:
        if self._inner is None:
            return []

        offset = self._position * self._frames_per_stack
        chunks = [Chunk(c.data, c.start + offset, c.end + offset) for c in self._inner.poll()]

        # Current timepoint fully read: advance to the next file that exists.
        # A skipped timepoint is jumped over (see the Zarr sibling); its slot
        # stays blank because its true index is never streamed.
        if self._inner.is_complete() and self._position + 1 < self._num_timepoints:
            nxt = self._next_available_position()
            if nxt is not None:
                if nxt != self._position + 1:
                    missing = ", ".join(str(p) for p in range(self._position + 1, nxt))
                    self._logger.warning(
                        f"Timepoint(s) {missing} never appeared but timepoint {nxt} "
                        f"did; leaving the gap blank and continuing at {nxt}"
                    )
                self._inner.close()
                self._position = nxt
                self._inner = Hdf5LiveSource(self._detector_name, self._chunk_size)
                self._inner.open(self._build_path(nxt))
        return chunks

    def _next_available_position(self) -> int | None:
        """The next timepoint to stream, or ``None`` to keep waiting.

        As in the Zarr sibling, but advancing on plain existence: SWMR flush
        ordering makes a shape-visible HDF5 dataset data-visible too, so there
        is no separate streamability gate to clear.
        """
        for position in range(self._position + 1, self._num_timepoints):
            path = self._build_path(position)
            if path is not None and os.path.exists(path):
                return position
        return None

    def is_complete(self) -> bool:
        if self._inner is None:
            return True
        if not self._inner.is_complete():
            return False
        # On the last expected timepoint with its file fully read -> done.
        if self._position + 1 >= self._num_timepoints:
            return True
        # Otherwise wait for the next timepoint file (live). If it isn't going
        # to appear, the user stops live mode; we don't guess completion here.
        return False

    def close(self) -> None:
        if self._inner is not None:
            self._inner.close()
        self._inner = None

    def _build_path(self, position: int) -> str | None:
        if self._template is None:
            return None
        folder, prefix, width, suffix, _ = self._template
        index = self._first_index + position
        return os.path.join(folder, f"{prefix}{index:0{width}d}{suffix}")

    def _count_present_timepoints(self) -> int:
        """Timepoint slots to allocate when the metadata does not say.

        See :meth:`ZarrMultiFileLapseSource._count_present_timepoints`.
        """
        return _highest_lapse_index_span(self._template, self._first_index)


class Hdf5LapseSource(LiveSource):
    """Polls a single-file scan{N} timelapse HDF5 as one continuous global frame stream."""

    idles_between_stacks = True  # Single file with multiple scan{N} groups; worker idles while recorder prepares next timepoint.

    def __init__(self, detector_name: str | None = None, chunk_size: int | None = None):
        """
        Args:
            detector_name: Detector name inside scan groups. If None, auto-detect.
            chunk_size: Override chunk size for polling. If None, use dataset.chunks[0].
        """
        self._detector_name = detector_name
        self._chunk_size_override = chunk_size
        self._path: str | None = None
        self._file = None
        self._scan_groups: list[str] = []
        self._current_group_index = 0
        self._global_cursor = 0
        self._local_cursor = 0
        self._chunk_size = 1
        self._frames_per_stack: int | None = None
        self._num_timepoints: int | None = None
        self._current_dataset = None
        self._current_dataset_path: str | None = None
        self._resolved_detector_name: str | None = None
        self._writing = True
        self._owns_file = False

    def open(self, path_or_handle: Any) -> StackInfo:
        """Open the HDF5 file in SWMR mode and return stack metadata."""
        self._global_cursor = 0
        self._local_cursor = 0
        self._current_group_index = 0
        
        if isinstance(path_or_handle, h5py.File):
            self._file = path_or_handle
            self._path = None
            self._owns_file = False
        else:
            path = Path(path_or_handle)
            self._path = str(path)
            self._file = h5py.File(self._path, 'r', libver='latest', swmr=True)
            self._owns_file = True
        
        # Enumerate scan{N} groups
        self._scan_groups = sorted(
            [key for key in self._file.keys() if key.startswith('scan') and key[4:].isdigit()],
            key=lambda x: int(x[4:])
        )
        
        if not self._scan_groups:
            raise ValueError("No scan{N} groups found in HDF5 file")
        
        # Open scan0 to detect detector and read metadata
        scan0 = self._file[self._scan_groups[0]]
        self._resolved_detector_name = self._detector_name or self._auto_detect_detector(scan0)
        
        # Open the detector dataset in scan0
        self._current_dataset, attrs = self._open_detector_dataset(scan0, self._resolved_detector_name)
        self._current_dataset_path = f'/{self._scan_groups[0]}/{self._resolved_detector_name}/data'
        
        if self._current_dataset.ndim != 3:
            raise ValueError(f"Expected 3D dataset (T, Y, X), got shape {self._current_dataset.shape}")
        
        # Determine chunk size
        if self._chunk_size_override is not None:
            self._chunk_size = max(1, int(self._chunk_size_override))
        elif self._current_dataset.chunks is not None and len(self._current_dataset.chunks) > 0:
            self._chunk_size = max(1, int(self._current_dataset.chunks[0]))
        else:
            self._chunk_size = 1
        
        # Extract frames_per_stack and num_timepoints
        self._frames_per_stack = self._coerce_int(attrs.get('recording:frames_per_stack'))
        if self._frames_per_stack is None:
            self._frames_per_stack = self._current_dataset.shape[0]
        
        self._num_timepoints = self._coerce_int(attrs.get('recording:num_timepoints'))
        self._refresh_state_from_attrs(attrs)
        
        # Calculate expected_frames
        expected_frames = None
        if self._num_timepoints is not None and self._frames_per_stack is not None:
            expected_frames = self._num_timepoints * self._frames_per_stack
        
        frame_shape = self._current_dataset.shape[-2:]
        
        dataset_path = attrs.get('recording:dataset_path') or self._current_dataset_path
        return StackInfo(
            frame_shape=frame_shape,
            dtype=self._current_dataset.dtype,
            attrs=dict(attrs),
            expected_frames=expected_frames,
            frames_per_stack=self._frames_per_stack,
            detector_name=attrs.get('recording:detector_name') or attrs.get('detector_name') or self._resolved_detector_name,
            dataset_path=dataset_path,
            source_format=attrs.get('recording:source_format') or 'HDF5',
            acquisition_layout=_resolve_stack_layout(
                attrs,
                shape=tuple(self._current_dataset.shape),
                detector=(
                    attrs.get('recording:detector_name')
                    or attrs.get('detector_name')
                    or self._resolved_detector_name
                ),
                source_path=self._path,
                dataset_path=dataset_path,
            ),
        )

    def poll(self) -> list[Chunk]:
        """Return newly available chunks with GLOBAL indices across all scan groups."""
        if self._current_dataset is None:
            return []
        
        chunks = []
        queued = 0

        while True:
            # Refresh current dataset and re-enumerate scan groups to detect new ones
            self._refresh_current_dataset()
            self._refresh_scan_groups()
            
            # Read current group's attributes
            current_group = self._file[self._scan_groups[self._current_group_index]]
            _, attrs = self._open_detector_dataset(current_group, self._resolved_detector_name)
            self._refresh_state_from_attrs(attrs)
            
            # Determine how many frames we can read from current group
            readable_length = min(self._current_dataset.shape[0], self._frames_per_stack or self._current_dataset.shape[0])
            
            if self._local_cursor >= readable_length:
                # Current group is fully read, try to advance to next
                if self._current_group_index + 1 < len(self._scan_groups):
                    # Check if next group exists
                    next_group_name = self._scan_groups[self._current_group_index + 1]
                    if next_group_name in self._file:
                        # Advance to next group
                        self._current_group_index += 1
                        self._local_cursor = 0
                        next_group = self._file[next_group_name]
                        self._current_dataset, _ = self._open_detector_dataset(next_group, self._resolved_detector_name)
                        self._current_dataset_path = f'/{next_group_name}/{self._resolved_detector_name}/data'
                        continue
                    else:
                        # Next group doesn't exist yet, wait
                        break
                else:
                    # No more groups expected
                    break
            
            # Read chunks from current group
            start_local = self._local_cursor
            end_local = min(start_local + self._chunk_size, readable_length)
            
            if start_local >= end_local:
                break
            
            # Calculate GLOBAL indices
            start_global = self._current_group_index * (self._frames_per_stack or 0) + start_local
            end_global = self._current_group_index * (self._frames_per_stack or 0) + end_local
            
            data = self._current_dataset[start_local:end_local]
            chunks.append(Chunk(data=data, start=start_global, end=end_global))
            
            self._local_cursor = end_local
            self._global_cursor = end_global
            queued += _chunk_nbytes(data)
            if queued >= LIVE_POLL_MAX_BYTES:
                break

            # If we haven't filled a full chunk, stop polling (wait for more data)
            if end_local - start_local < self._chunk_size and end_local < readable_length:
                break
        
        return chunks

    def is_complete(self) -> bool:
        """Return whether all timepoints have been fully consumed."""
        if self._current_dataset is None:
            return True
        
        # If we know the total expected frames, check against global cursor
        if self._num_timepoints is not None and self._frames_per_stack is not None:
            expected_total = self._num_timepoints * self._frames_per_stack
            if self._global_cursor >= expected_total:
                return True
        
        # Check if we're on the last expected group
        if self._num_timepoints is not None:
            if self._current_group_index >= self._num_timepoints - 1:
                # We're on or past the last expected group
                self._refresh_current_dataset()
                current_group = self._file[self._scan_groups[self._current_group_index]]
                _, attrs = self._open_detector_dataset(current_group, self._resolved_detector_name)
                self._refresh_state_from_attrs(attrs)
                
                readable_length = min(self._current_dataset.shape[0], self._frames_per_stack or self._current_dataset.shape[0])
                
                if not self._writing and self._local_cursor >= readable_length:
                    return True

        # A timepoint that finalized as stopped_early is the last one: an
        # interrupted acquisition writes no further groups. Without this the
        # source waits for timepoints that will never be written, and the only
        # way out was the stall timeout, which reports a deliberate stop as a
        # crashed writer after a delay.
        if self._stopped_early_group_is_drained():
            return True

        return False

    def _stopped_early_group_is_drained(self) -> bool:
        """Whether the group being read was cut short and has been consumed."""
        if not self._scan_groups or self._current_group_index >= len(self._scan_groups):
            return False
        try:
            self._refresh_current_dataset()
            group = self._file[self._scan_groups[self._current_group_index]]
            _, attrs = self._open_detector_dataset(group, self._resolved_detector_name)
        except Exception:
            return False
        outcome = attrs.get("recording:completion_outcome")
        if isinstance(outcome, bytes):
            outcome = outcome.decode()
        if str(outcome) != "stopped_early":
            return False
        self._refresh_state_from_attrs(attrs)
        if self._writing:
            return False
        return self._local_cursor >= int(self._current_dataset.shape[0])

    def close(self) -> None:
        """Release source resources."""
        if self._owns_file and self._file is not None:
            self._file.close()
        self._path = None
        self._file = None
        self._current_dataset = None
        self._current_dataset_path = None
        self._scan_groups = []

    def _auto_detect_detector(self, scan_group: h5py.Group) -> str:
        """Auto-detect detector name from scan group contents."""
        for key in scan_group.keys():
            item = scan_group[key]
            if isinstance(item, h5py.Group) and 'data' in item:
                return key
        
        raise ValueError("No detector group with 'data' dataset found in scan group")

    def _open_detector_dataset(self, scan_group: h5py.Group, detector_name: str) -> tuple[h5py.Dataset, dict[str, Any]]:
        """Open detector dataset and collect flattened attributes."""
        if detector_name not in scan_group:
            raise ValueError(f"Detector '{detector_name}' not found in scan group")
        
        det_group = scan_group[detector_name]
        if not isinstance(det_group, h5py.Group):
            raise ValueError(f"'{detector_name}' is not a group")
        
        if 'data' not in det_group:
            raise ValueError(f"Detector group '{detector_name}' has no 'data' dataset")
        
        dataset = det_group['data']
        
        # Flatten attributes from root, detector group, and metadata
        attrs: dict[str, Any] = {}
        
        # Root attrs
        attrs.update(dict(self._file.attrs))
        
        # Dataset attrs
        attrs.update(dict(dataset.attrs))
        
        # Metadata group
        if 'metadata' in det_group:
            self._flatten_metadata(det_group['metadata'], attrs, prefix='')
        
        return dataset, attrs

    def _flatten_metadata(self, group: h5py.Group, attrs: dict[str, Any], prefix: str) -> None:
        """Recursively flatten metadata group into attrs dict with category prefixes."""
        for key in group.attrs.keys():
            flat_key = f"{prefix}{key}" if prefix else key
            attrs[flat_key] = group.attrs[key]
        
        for subgroup_name in group.keys():
            subgroup = group[subgroup_name]
            if isinstance(subgroup, h5py.Group):
                new_prefix = f"{subgroup_name}:" if not prefix else f"{prefix}{subgroup_name}:"
                self._flatten_metadata(subgroup, attrs, new_prefix)

    def _refresh_current_dataset(self) -> None:
        """Re-open current dataset to see new frames."""
        if self._path is None or self._current_dataset is None:
            return
        
        # Close and re-open file to refresh SWMR
        if self._owns_file and self._file is not None:
            self._file.close()
            self._file = h5py.File(self._path, 'r', libver='latest', swmr=True)
        
        current_group = self._file[self._scan_groups[self._current_group_index]]
        det_group = current_group[self._resolved_detector_name]
        self._current_dataset = det_group['data']
        self._current_dataset.refresh()

    def _refresh_scan_groups(self) -> None:
        """Re-enumerate scan groups to detect newly added ones."""
        if self._path is None:
            return
        
        self._scan_groups = sorted(
            [key for key in self._file.keys() if key.startswith('scan') and key[4:].isdigit()],
            key=lambda x: int(x[4:])
        )

    def _refresh_state_from_attrs(self, attrs: dict[str, Any]) -> None:
        """Update internal state from attributes."""
        self._writing = self._coerce_bool(_meta_lookup(attrs, 'writing'), default=self._writing)

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _coerce_bool(value: Any, *, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, str):
            return value.strip().lower() not in {'0', 'false', 'no', 'off'}
        return bool(value)
