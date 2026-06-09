"""
View-only reconstructor.

The default fallback for any modality that doesn't need real reconstruction:
STED, FLIM, confocal, widefield, anything where the user just wants to inspect
raw frames with the same data-edit / multi-data / scan-params tooling that
ImProcess already provides.

`process()` does no signal processing — it returns the raw DataObj.data
wrapped as a ProcessingResult so the rest of ImProcess (ReconstructionView,
WatcherFrame save, etc.) can handle it uniformly.
"""

from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import tifffile as tiff
from qtpy import QtWidgets

from imswitch.improcess.model.result import ProcessingResult, ViewMode
from imswitch.improcess.reconstructors.base import Reconstructor

if TYPE_CHECKING:
    from imswitch.improcess.model import DataObj


# Default axis labels for trailing dims. Truncated to data.ndim.
_DEFAULT_AXIS_LABELS = ["T", "Z", "C", "Y", "X"]


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
        preloaded = data_obj.dataLoaded
        try:
            data_obj.checkAndLoadData()
            data = np.asarray(data_obj.data)
        finally:
            if not preloaded:
                data_obj.checkAndUnloadData()

        # Infer axis labels from ndim — last 2 dims are always Y, X.
        ndim = data.ndim
        if ndim <= len(_DEFAULT_AXIS_LABELS):
            axis_labels = _DEFAULT_AXIS_LABELS[-ndim:]
        else:
            # More dims than we have default labels for — pad the front.
            extra = ndim - len(_DEFAULT_AXIS_LABELS)
            axis_labels = [f"D{i}" for i in range(extra)] + _DEFAULT_AXIS_LABELS

        view_modes = [ViewMode("Standard", tuple(range(ndim)))]

        return ViewOnlyResult(
            name=data_obj.name,
            data=data,
            axis_labels=axis_labels,
            view_modes=view_modes,
            display_levels=None,
        )
