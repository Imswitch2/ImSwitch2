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

import os
from typing import TYPE_CHECKING

import numpy as np
from qtpy import QtWidgets

from imswitch.imcommon.algorithms.spatial_frame import content_digest_uid

from imswitch.improcess.model.result import ProcessingResult, ViewMode
from imswitch.improcess.reconstructors.base import Reconstructor
from imswitch.improcess.model.result_io import save_image_result

if TYPE_CHECKING:
    from imswitch.improcess.model import DataObj


# Default axis labels for trailing dims. Truncated to data.ndim.
_DEFAULT_AXIS_LABELS = ["T", "Z", "C", "Y", "X"]


def _source_identity(data_obj) -> str | None:
    """A stable identity for the file this result was loaded from, if any.

    Uses the *source*, never the pixels. Sampling content was both unsound and
    expensive: a strided sum collides easily (two acquisitions of the same
    static field, or any two all-zero arrays), and calling ``np.asarray`` on a
    lazily-loaded stack materialises gigabytes purely to compute an id.

    A file path plus its size and mtime identifies the data without reading
    it. When there is no path — data handed over in memory — there is nothing
    trustworthy to derive an identity from, and the caller mints a fresh one
    instead of inventing an equivalence.
    """
    path = getattr(data_obj, "dataPath", None)
    if not path:
        return None
    try:
        stat = os.stat(path)
        return content_digest_uid("data", path, stat.st_size, int(stat.st_mtime))
    except OSError:
        return content_digest_uid("data", path)


class ViewOnlyResult(ProcessingResult):
    """Raw frame stack wrapped as a ProcessingResult."""

    def write_files(self, plan, document) -> None:
        save_image_result(self, plan.primary, plan.fmt, document=document)


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

    @classmethod
    def default_params(cls) -> dict:
        return {}

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        return _NoParamsWidget(parent)

    def make_metadata_dialog(self, parent: QtWidgets.QWidget) -> QtWidgets.QDialog | None:
        return None

    def process(
        self, data_obj: "DataObj", params: dict, context=None
    ) -> ViewOnlyResult:
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

        # Loaded data carries no recorded identity. When it came from a file we
        # can identify the *source*, so the same file reopened lines up with an
        # ROI set saved against it. With no path there is nothing sound to
        # derive from, so the ids are minted: two unidentifiable datasets must
        # come out unrelated rather than accidentally equal.
        dataset_uid = _source_identity(data_obj)
        if dataset_uid is None:
            return ViewOnlyResult(
                name=data_obj.name,
                data=data,
                axis_labels=axis_labels,
                view_modes=view_modes,
                display_levels=None,
                axis_scales=axis_scales,
                scale_unit=source_scale_unit or "px",
                identity_kind="derived",
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
