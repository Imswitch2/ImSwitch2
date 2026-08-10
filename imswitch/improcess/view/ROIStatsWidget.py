"""ROI statistics panel for ImProcess."""

from __future__ import annotations

import numpy as np
from qtpy import QtCore, QtWidgets

from imswitch.imcommon.view.guitools.viewer_tools import ViewerToolService
from imswitch.improcess.analysis.roi_stats import ROIStats, compute_roi_stats
from imswitch.improcess.layer_selection import active_image_layer
from .ResultsTableWidget import ResultsTableWidget


class ROIStatsWidget(QtWidgets.QWidget):
    """Compute basic statistics for the active image layer or a rectangle ROI."""

    sigResultPushed = QtCore.Signal(object, object)

    #: Stable owner key for the shared drawing tool. Not id(self), so a panel
    #: that is closed and reopened reclaims its own shapes.
    TOOL_OWNER = "improcess.roi-stats"

    def __init__(self, napariViewer, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._viewer = napariViewer
        self._toolService = ViewerToolService.for_viewer(napariViewer)
        self._toolToken = self._toolService.acquire(self.TOOL_OWNER)
        self._current_stats = None

        self.modeCombo = QtWidgets.QComboBox()
        self.modeCombo.addItems(["Full image", "Rectangle ROI"])
        self.runButton = QtWidgets.QPushButton("Update")
        self.clearButton = QtWidgets.QPushButton("Clear ROI")
        self.pushButton = QtWidgets.QPushButton("Push to table")

        self.table = ResultsTableWidget(show_filter=False, show_csv=False)

        controls = QtWidgets.QHBoxLayout()
        controls.addWidget(self.modeCombo)
        controls.addWidget(self.runButton)
        controls.addWidget(self.clearButton)
        controls.addWidget(self.pushButton)
        controls.addStretch()

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(controls)
        layout.addWidget(self.table)
        self.setLayout(layout)

        self.modeCombo.currentIndexChanged.connect(self._mode_changed)
        self.runButton.clicked.connect(self.update_stats)
        self.clearButton.clicked.connect(self._clear_roi)
        self.pushButton.clicked.connect(self._push_to_table)
        self._toolService.add_callback(
            self._toolToken, self._toolService.sigShapesChanged, self.update_stats
        )
        try:
            self._viewer.dims.events.current_step.connect(lambda _event: self.update_stats())
        except Exception:
            pass

        self._mode_changed()
        self._set_message("No statistics computed.")

    def setCurrentResult(self, result) -> None:
        """Recompute against the newly selected result.

        Same ROI, different pixels: statistics computed from the previous
        reconstruction are indistinguishable from fresh ones on screen.
        """
        self.update_stats()

    def update_stats(self) -> None:
        image = self._current_image_2d()
        if image is None:
            self._set_message("No image layer selected.")
            return

        try:
            roi = self._current_rectangle_roi() if self.modeCombo.currentText() == "Rectangle ROI" else None
            stats = compute_roi_stats(image, roi=roi)
        except Exception as exc:
            self._set_message(str(exc))
            return

        self._show_stats(stats)

    def _mode_changed(self) -> None:
        # Re-acquire: taking the drawing tool is what makes newly drawn shapes
        # attributable to this panel rather than whoever held it last.
        self._toolToken = self._toolService.acquire(self.TOOL_OWNER)
        if self.modeCombo.currentText() == "Rectangle ROI":
            self._toolService.set_mode(self._toolToken, "rectangle")
        else:
            self._toolService.set_mode(self._toolToken, "pan")
        self.update_stats()

    def _clear_roi(self) -> None:
        # Clears only this panel's shapes; the Profile panel's line survives.
        self._toolService.clear(self._toolToken)
        self.update_stats()

    def closeEvent(self, event):  # noqa: N802 - Qt naming
        """Give up the drawing tool and disconnect our viewer callbacks.

        There is no dock-close signal to hang teardown on, so without this the
        handlers this panel installed keep firing after it is gone.
        """
        try:
            self._toolService.release(self._toolToken)
        except Exception:
            pass
        super().closeEvent(event)

    def _push_to_table(self) -> None:
        if self._current_stats is None:
            return
        stats = self._current_stats
        record = {
            # Which result these numbers came from. The Results table is an
            # accumulating log, so rows pushed from two reconstructions are
            # otherwise indistinguishable apart from the values themselves.
            "source": self._source_name(),
            "kind": "roi-stats",
            "area_px": float(stats.area_pixels),
            "finite_px": float(stats.finite_pixels),
            "mean": float(stats.mean),
            "median": float(stats.median),
            "std": float(stats.std),
            "min": float(stats.minimum),
            "max": float(stats.maximum),
            "sum": float(stats.total),
        }
        columns = list(record.keys())
        self.sigResultPushed.emit(columns, [record])

    def _show_stats(self, stats: ROIStats) -> None:
        self._current_stats = stats
        rows = [
            {"Metric": "Area px", "Value": stats.area_pixels},
            {"Metric": "Finite px", "Value": stats.finite_pixels},
            {"Metric": "Mean", "Value": stats.mean},
            {"Metric": "Median", "Value": stats.median},
            {"Metric": "Std", "Value": stats.std},
            {"Metric": "Min", "Value": stats.minimum},
            {"Metric": "Max", "Value": stats.maximum},
            {"Metric": "Sum", "Value": stats.total},
        ]
        self.table.set_records(["Metric", "Value"], rows)

    def _set_message(self, message: str) -> None:
        self._current_stats = None
        self.table.set_records(["Metric", "Value"], [{"Metric": "Status", "Value": message}])

    def _current_rectangle_roi(self) -> tuple[int, int, int, int] | None:
        # Only this panel's shapes: a rectangle drawn for the Profile panel is
        # not ours to measure.
        for index, shape_type, _vertices in self._toolService.shapes(self._toolToken):
            if shape_type == "rectangle":
                r0, c0, r1, c1 = self._toolService.get_rectangle_bounds(index)
                # Bounds come from the Shapes layer in world coordinates; convert
                # to pixel indices via the image scale before cropping (no-op
                # when scale == 1, but required for scaled reconstructions).
                row_scale, col_scale = self._visible_pixel_scales()
                r0, r1 = r0 / row_scale, r1 / row_scale
                c0, c1 = c0 / col_scale, c1 / col_scale
                return int(round(r0)), int(round(r1)), int(round(c0)), int(round(c1))
        return None

    def _visible_pixel_scales(self) -> tuple[float, float]:
        """Return the active image layer's (row, col) scale (1.0 fallback)."""
        layer = self._active_image_layer()
        if layer is None:
            return 1.0, 1.0
        try:
            scale = tuple(float(v) for v in layer.scale)
        except Exception:
            return 1.0, 1.0
        if len(scale) < 2:
            return 1.0, 1.0
        return scale[-2], scale[-1]

    def _source_name(self) -> str:
        """Name of the layer the statistics were measured on."""
        layer = self._active_image_layer()
        return str(getattr(layer, "name", "") or "image")

    def _current_image_2d(self):
        layer = self._active_image_layer()
        if layer is None:
            return None
        data = np.asarray(layer.data)
        if data.ndim < 2:
            return None
        if data.ndim == 2:
            return data
        step = self._current_step(data.ndim)
        indexer = []
        for axis, size in enumerate(data.shape):
            if axis >= data.ndim - 2:
                indexer.append(slice(None))
            else:
                indexer.append(min(max(step[axis], 0), size - 1))
        return np.asarray(data[tuple(indexer)])

    def _current_step(self, ndim: int) -> tuple[int, ...]:
        try:
            step = tuple(int(v) for v in self._viewer.dims.current_step)
        except Exception:
            step = ()
        if len(step) < ndim:
            step = (*step, *(0 for _ in range(ndim - len(step))))
        return step

    def _active_image_layer(self):
        return active_image_layer(self._viewer)
