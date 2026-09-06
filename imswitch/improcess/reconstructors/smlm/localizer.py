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
from imswitch.improcess.model.localization_result import LocalizationResult
from imswitch.improcess.model.localization_schema import localizations_from_columns
from imswitch.improcess.reconstructors.base import StreamingReconstructor

from .detection import detect_spots
from .fitting import fit_spots
from .params_widget import SmlmParamsWidget

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

    @classmethod
    def default_params(cls) -> dict:
        return {   'threshold': 500.0,
        'sigma': 1.0,
        'roi': 7,
        'method': 'gausslq',
        'pixel_size_nm': 100.0}

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
        pixel_size_nm = float(params.get("pixel_size_nm", 1.0) or 1.0)
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
            },
        )

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
