"""SMLM localizer reconstructor.

Iterates a blinking stack frame-by-frame, running the pure-function
``detect_spots`` + ``fit_spots`` core, and concatenates the per-frame fits into
a canonical :class:`LocalizationResult`. Reference-quality (unvectorized); a
throughput implementation can later replace ``detection``/``fitting`` behind
the same contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterator

import numpy as np
from qtpy import QtWidgets

from imswitch.imcommon.model import initLogger
from imswitch.imcommon.model.acquisition_layout import (
    PAYLOAD_DETECTOR_FRAME_STREAM,
    iter_recorded_coordinates,
)
from imswitch.improcess.model.localization_result import LocalizationResult
from imswitch.improcess.model.localization_schema import localizations_from_columns
from imswitch.improcess.reconstructors.base import StreamingReconstructor

from .detection import detect_spots
from .fitting import fit_spots
from .params_widget import SmlmParamsWidget

#: Loop kinds that advance a chronological frame stream, which is what SMLM
#: localizes. Every other kind is a second physical dimension, and flattening
#: it into the frame index mixes unrelated states into one blinking trace.
FRAME_LOOP_KINDS = frozenset({"time", "repeat", "frame"})

_UNIT_TO_NM = {"nm": 1.0, "um": 1000.0, "µm": 1000.0, "micron": 1000.0, "mm": 1e6}


def source_pixel_size_nm(data_obj: Any) -> float | None:
    """Camera pixel pitch in nm from the source's own calibration, or ``None``."""
    labels = list(getattr(data_obj, "axis_labels", None) or [])
    scales = list(getattr(data_obj, "axis_scales", None) or [])
    factor = _UNIT_TO_NM.get(str(getattr(data_obj, "scale_unit", "") or "").lower())
    if factor is None or "X" not in labels:
        return None
    index = labels.index("X")
    if index >= len(scales):
        return None
    try:
        value = float(scales[index])
    except (TypeError, ValueError):
        return None
    return value * factor if value > 0 else None


def non_frame_loops(layout: Any) -> tuple:
    """Acquisition loops that are not a chronological frame axis."""
    if layout is None or layout.payload_kind != PAYLOAD_DETECTOR_FRAME_STREAM:
        return ()
    return tuple(
        loop for loop in layout.event_loops if loop.kind not in FRAME_LOOP_KINDS
    )


def frame_indices_for_selection(layout: Any, selection: dict) -> list[int]:
    """Stored-frame indices whose coordinates match ``selection``."""
    return [
        index
        for index, coordinates in enumerate(iter_recorded_coordinates(layout))
        if all(coordinates.get(loop_id) == value for loop_id, value in selection.items())
    ]

if TYPE_CHECKING:
    from imswitch.improcess.model import DataObj


def iter_frames(data: np.ndarray) -> Iterator[tuple[int, np.ndarray]]:
    """Yield ``(frame_index, 2D frame)`` from an N-d stack.

    The last two axes are treated as ``(Y, X)``; all leading axes are
    flattened into a single frame index. A bare 2D array is one frame.
    """
    array = np.asarray(data)
    if array.ndim < 2:
        raise ValueError(f"SMLM data must be at least 2D, got ndim={array.ndim}")
    if array.ndim == 2:
        yield 0, array
        return
    height, width = array.shape[-2:]
    flat = array.reshape(-1, height, width)
    for index in range(flat.shape[0]):
        yield index, flat[index]


def localize_stack(
    data: np.ndarray,
    *,
    threshold: float,
    roi: int = 7,
    sigma: float = 1.0,
    method: str = "gausslq",
    pixel_size_nm: float = 1.0,
) -> np.recarray:
    """Localize a whole stack into a canonical (nm) localization recarray.

    Pure function over an ndarray; used by both the batch reconstructor and
    the streaming session so their outputs are identical.
    """
    frames: list[int] = []
    xs: list[float] = []
    ys: list[float] = []
    sxs: list[float] = []
    sys: list[float] = []
    photons: list[float] = []

    for frame_index, frame in iter_frames(data):
        coords = detect_spots(frame, threshold=threshold, roi=roi, sigma=sigma)
        for fit in fit_spots(frame, coords, roi=roi, method=method):
            frames.append(frame_index)
            xs.append(fit["x"])
            ys.append(fit["y"])
            sxs.append(fit["sigma_x"])
            sys.append(fit["sigma_y"])
            photons.append(fit["intensity"])

    if not frames:
        from imswitch.improcess.model.localization_schema import empty_localizations

        return empty_localizations(0)

    return localizations_from_columns(
        {
            "frame": np.asarray(frames, dtype=np.int32),
            "x_nm": np.asarray(xs, dtype=np.float32) * pixel_size_nm,
            "y_nm": np.asarray(ys, dtype=np.float32) * pixel_size_nm,
            "sigma_x_nm": np.asarray(sxs, dtype=np.float32) * pixel_size_nm,
            "sigma_y_nm": np.asarray(sys, dtype=np.float32) * pixel_size_nm,
            "photons": np.asarray(photons, dtype=np.float32),
        }
    )


class SmlmLocalizer(StreamingReconstructor):
    """Localize a blinking stack into a coordinate table."""

    name = "SMLM localizer"
    id = "smlm-localizer"
    file_extensions = ["hdf5", "tiff", "tif", "zarr"]
    description = "Single-molecule localization (net-gradient detect + fit)"
    default_save_subdir = "smlm"

    def __init__(self):
        self._logger = initLogger('SmlmLocalizer')

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        return SmlmParamsWidget(parent)

    def make_metadata_dialog(self, parent: QtWidgets.QWidget) -> QtWidgets.QDialog | None:
        return None
    
    def make_session(self):
        """Create a fresh streaming session for live localization."""
        from .live_session import SmlmLiveSession
        return SmlmLiveSession()

    def process(
        self, data_obj: "DataObj", params: dict, context=None
    ) -> LocalizationResult:
        data, source_shape = self._load_frames(data_obj)
        data, selection = self._select_frame_stream(data_obj, data, params)
        pixel_size_nm, calibration = self._pixel_size_nm(data_obj, params)
        locs = localize_stack(
            data,
            threshold=float(params.get("threshold", 500.0)),
            roi=int(params.get("roi", 7)),
            sigma=float(params.get("sigma", 1.0)),
            method=str(params.get("method", "gausslq")),
            pixel_size_nm=pixel_size_nm,
        )
        if len(locs) == 0:
            self._logger.warning(
                "0 localizations found. Tune the detection threshold with the "
                "Preview detection toggle in the SMLM parameters panel."
            )
        return LocalizationResult(
            name=f"{data_obj.name} localizations",
            locs=locs,
            pixel_size_nm=pixel_size_nm,
            dims="2D",
            source_name=data_obj.name,
            source_shape=source_shape,
            metadata={
                "threshold": float(params.get("threshold", 500.0)),
                "roi": int(params.get("roi", 7)),
                "fit_method": str(params.get("method", "gausslq")),
                **calibration,
                **({"loop_selection": selection} if selection else {}),
            },
        )

    def _select_frame_stream(
        self, data_obj: "DataObj", data: np.ndarray, params: dict
    ) -> tuple[np.ndarray, dict]:
        """Reduce a recorded multi-loop acquisition to one frame stream.

        SMLM treats every leading axis as chronological. When a recording says
        an axis is a condition, a channel or a scan coordinate, flattening it
        into the frame index mixes unrelated states into one blinking trace, so
        the caller has to say which one to localize.
        """
        resolved = getattr(data_obj, "acquisition_layout", None)
        # Refusing the user's data needs a statement, not an inference.
        if resolved is None or not resolved.is_authoritative:
            return data, {}
        layout = resolved.layout
        extra = non_frame_loops(layout)
        if not extra:
            return data, {}

        selection = dict(params.get("loop_selection") or {})
        unresolved = [loop for loop in extra if loop.id not in selection]
        if unresolved:
            names = ", ".join(f"{loop.id!r} ({loop.kind}, {loop.count})" for loop in extra)
            raise ValueError(
                f"This recording is not a plain frame stream: it also has "
                f"{names}. Localizing it as-is would flatten those into time "
                f"and mix unrelated states into one trace. Choose one index "
                f"per loop (loop_selection), or use a reconstructor that "
                f"understands them."
            )
        for loop in extra:
            index = int(selection[loop.id])
            if not 0 <= index < loop.count:
                raise ValueError(
                    f"Selection {index} for loop {loop.id!r} is outside "
                    f"0..{loop.count - 1}"
                )
        indices = frame_indices_for_selection(layout, selection)
        if not indices:
            raise ValueError(f"No frames match the selection {selection}")
        flat = np.asarray(data)
        flat = flat.reshape(-1, *flat.shape[-2:])
        return flat[indices], selection

    @staticmethod
    def _pixel_size_nm(data_obj: "DataObj", params: dict) -> tuple[float, dict]:
        """Prefer the source's own calibration; record which one was used."""
        recorded = source_pixel_size_nm(data_obj)
        manual = params.get("pixel_size_nm")
        manual = float(manual) if manual else None
        if recorded is not None:
            calibration = {"pixel_size_source": "source"}
            if manual is not None and abs(manual - recorded) > 1e-9:
                calibration["pixel_size_nm_manual"] = manual
            return recorded, calibration
        return (manual or 1.0), {"pixel_size_source": "manual"}

    @staticmethod
    def _load_frames(data_obj: "DataObj") -> tuple[np.ndarray, tuple[int, int] | None]:
        preloaded = data_obj.dataLoaded
        try:
            data_obj.checkAndLoadData()
            data = np.asarray(data_obj.data)
        finally:
            if not preloaded:
                data_obj.checkAndUnloadData()
        source_shape = tuple(data.shape[-2:]) if data.ndim >= 2 else None
        return data, source_shape


__all__ = ["SmlmLocalizer", "localize_stack", "iter_frames"]
