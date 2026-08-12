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

    def inspect_source(self, data_obj: "DataObj"):
        """Show how the acquisition was interpreted, without gating on it.

        View-only never refuses data, so it declares no requirements; but it is
        often the first place a user opens an unfamiliar file, which makes it
        the right place to say that the axis names are a guess.
        """
        from imswitch.improcess.reconstructors.base import SourceInspection

        try:
            resolved = data_obj.acquisition_layout
        except Exception as error:
            return SourceInspection(
                source_kind=getattr(data_obj, "sourceKind", "image"),
                issues=tuple(getattr(error, "issues", ())),
                warning=str(error),
            )
        if resolved is None:
            return None
        return SourceInspection(
            source_kind=getattr(data_obj, "sourceKind", "image"),
            metadata={
                "acquisition_layout_source": resolved.source,
                "acquisition_layout_confidence": resolved.confidence,
                "acquisition_payload_kind": resolved.layout.payload_kind,
            },
            issues=tuple(resolved.issues),
        )

    #: Provenance values that mean the layout was inferred rather than read.
    #: The container declared nothing, so its axis names are a rank guess.
    _INFERRED_PROVENANCE = frozenset({"shape-inference", "generic-fallback"})

    #: Canonical storage roles to the names shown on the viewer's sliders.
    _AXIS_DISPLAY_NAMES = {
        "frame": "Frame",
        "detector_y": "Y",
        "detector_x": "X",
        "scan_x": "X",
        "scan_y": "Y",
        "scan_z": "Z",
        "channel": "C",
        "condition": "Condition",
        "time": "T",
    }

    def _axis_labels_for(
        self, data_obj: "DataObj", source_axis_labels, ndim: int
    ) -> list[str]:
        """Name the axes from evidence, falling back to Frame rather than T/C.

        A source that declares its own axes keeps them. A source that declares
        nothing used to be labelled from rank alone, which called a plain 3D
        camera stack ``C, Y, X`` -- channel data, on no evidence at all.
        """
        resolved = getattr(data_obj, "acquisition_layout", None)
        layout = getattr(resolved, "layout", None)
        inferred = layout is None or layout.provenance in self._INFERRED_PROVENANCE

        if source_axis_labels and len(source_axis_labels) == ndim and not inferred:
            return list(source_axis_labels)
        if layout is not None and len(layout.storage_axes) == ndim:
            return [
                self._AXIS_DISPLAY_NAMES.get(axis, axis.capitalize())
                for axis in layout.storage_axes
            ]
        if ndim >= 2:
            # Only the trailing two axes are known to be the detector plane.
            leading = ndim - 2
            names = ["Frame"] if leading == 1 else [f"Frame{i}" for i in range(leading)]
            return names + ["Y", "X"]
        return _DEFAULT_AXIS_LABELS[-ndim:]

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
        axis_labels = self._axis_labels_for(data_obj, source_axis_labels, ndim)

        axis_scales = (
            list(source_axis_scales)
            if source_axis_scales and len(source_axis_scales) == ndim
            else None
        )
        view_modes = [ViewMode("Standard", tuple(range(ndim)))]

        return ViewOnlyResult(
            name=data_obj.name,
            data=data,
            axis_labels=axis_labels,
            view_modes=view_modes,
            display_levels=None,
            axis_scales=axis_scales,
            scale_unit=source_scale_unit or "px",
        )
