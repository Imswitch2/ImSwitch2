"""
View-only reconstructor.

The default fallback for any modality that doesn't need real reconstruction:
STED, FLIM, confocal, widefield, anything where the user just wants to inspect
raw frames with the same data-edit / multi-data / scan-params tooling that
ImProcess already provides.

`process()` does no signal processing — it returns the raw DataObj data or
lazy data handle wrapped as a ProcessingResult so the rest of ImProcess
(ReconstructionView, WatcherFrame save, etc.) can handle it uniformly.
"""

from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import tifffile as tiff
from qtpy import QtWidgets

from imswitch.imcommon.algorithms.spatial_frame import content_digest_uid

from imswitch.improcess.model.result import ProcessingResult, ViewMode
from imswitch.improcess.reconstructors.base import Reconstructor

if TYPE_CHECKING:
    from imswitch.improcess.model import DataObj


# Default axis labels for trailing dims. Truncated to data.ndim.
_DEFAULT_AXIS_LABELS = ["T", "Z", "C", "Y", "X"]


def _content_fingerprint(data) -> str:
    """A cheap, stable fingerprint of an array's contents.

    Samples rather than hashing everything: this runs on load, and a full hash
    of a multi-gigabyte stack would be paid for on every open. Corners plus a
    strided sample is enough to tell two different datasets apart, and it is
    deterministic, so the same file always yields the same id. It is never
    used as proof two datasets are identical — a fingerprint match still only
    produces a "derived" identity, which cannot claim an exact match.
    """
    try:
        flat = np.asarray(data).reshape(-1)
        if flat.size == 0:
            return "empty"
        step = max(1, flat.size // 512)
        sample = np.asarray(flat[::step][:512])
        return f"{flat.size}:{float(np.nansum(sample.astype('float64'))):.8g}"
    except Exception:
        # A lazy/virtual handle that will not sample: fall back to shape only.
        return "unsampled"


class ViewOnlyResult(ProcessingResult):
    """Raw frame stack wrapped as a ProcessingResult."""

    def save(self, path: Path, fmt: str = "tiff") -> None:
        path = Path(path)
        if fmt == "tiff":
            tiff.imwrite(str(path), np.asarray(self.data))
        else:
            raise ValueError(f'ViewOnlyResult only supports fmt="tiff", got "{fmt}"')


class _NoParamsWidget(QtWidgets.QWidget):
    """Trivial empty widget — view-only takes no parameters."""

    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel(
            "View-only: no parameters. Drop a file or load from disk."
        ))
        layout.addStretch()

    def get_values(self) -> dict:
        return {}


class ViewOnlyReconstructor(Reconstructor):
    """
    Pass-through 'reconstructor' that loads the data and exposes it for viewing.

    Used as the standalone-mode default and as the fallback for any dataset
    whose modality has no dedicated plugin.
    """

    name = "View only"
    id = "view-only"
    file_extensions = ["hdf5", "tiff", "tif", "zarr"]
    description = "Display raw frames without any reconstruction"
    is_pass_through = True

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        return _NoParamsWidget(parent)

    def make_metadata_dialog(self, parent: QtWidgets.QWidget) -> QtWidgets.QDialog | None:
        return None

    def process(self, data_obj: "DataObj", params: dict) -> ViewOnlyResult:
        virtual = (
            getattr(data_obj, "sourceLoaded", False)
            and not getattr(data_obj, "dataLoaded", False)
            and getattr(data_obj, "data_handle", None) is not None
        )
        if virtual:
            data = data_obj.data_handle
            source_axis_labels = data_obj.axis_labels
            source_axis_scales = data_obj.axis_scales
            source_scale_unit = data_obj.scale_unit
        else:
            preloaded = data_obj.dataLoaded
            try:
                data_obj.checkAndLoadData()
                data = np.asarray(data_obj.data)
                source_axis_labels = data_obj.axis_labels
                source_axis_scales = data_obj.axis_scales
                source_scale_unit = data_obj.scale_unit
            finally:
                if not preloaded:
                    data_obj.checkAndUnloadData()

        ndim = data.ndim
        if source_axis_labels and len(source_axis_labels) == ndim:
            axis_labels = list(source_axis_labels)
        elif ndim <= len(_DEFAULT_AXIS_LABELS):
            axis_labels = _DEFAULT_AXIS_LABELS[-ndim:]
        else:
            # More dims than we have default labels for — pad the front.
            extra = ndim - len(_DEFAULT_AXIS_LABELS)
            axis_labels = [f"D{i}" for i in range(extra)] + _DEFAULT_AXIS_LABELS

        axis_scales = (
            list(source_axis_scales)
            if source_axis_scales and len(source_axis_scales) == ndim
            else None
        )
        view_modes = [ViewMode("Standard", tuple(range(ndim)))]

        # Data loaded from disk carries no recorded identity, so one is
        # inferred from its content: the same file loaded twice gets the same
        # ids (an ROI set saved against it still lines up), while
        # identity_kind="derived" stops an inferred identity ever claiming an
        # exact match — see imcommon.algorithms.spatial_frame.
        dataset_uid = content_digest_uid(
            "data", data.shape, str(data.dtype), _content_fingerprint(data)
        )
        return ViewOnlyResult(
            name=data_obj.name,
            data=data,
            axis_labels=axis_labels,
            view_modes=view_modes,
            display_levels=None,
            axis_scales=axis_scales,
            scale_unit=source_scale_unit or "px",
            dataset_uid=dataset_uid,
            result_uid=content_digest_uid("result", dataset_uid, tuple(axis_labels)),
            coordinate_space_uid=content_digest_uid("space", dataset_uid, data.shape[-2:]),
            identity_kind="derived",
        )
