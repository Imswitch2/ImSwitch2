"""ROI statistics panel for ImProcess."""

from __future__ import annotations

import numpy as np
from qtpy import QtWidgets

from imswitch.imcommon.view.guitools.naparitools import ViewerToolManager
from imswitch.improcess.analysis.roi_stats import ROIStats, compute_roi_stats


class ROIStatsWidget(QtWidgets.QWidget):
    """Compute basic statistics for the active image layer or a rectangle ROI."""

    def __init__(self, napariViewer, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._viewer = napariViewer
        self._toolManager = ViewerToolManager(napariViewer)

        self.modeCombo = QtWidgets.QComboBox()
        self.modeCombo.addItems(["Full image", "Rectangle ROI"])
        self.runButton = QtWidgets.QPushButton("Update")
        self.clearButton = QtWidgets.QPushButton("Clear ROI")

        self.table = QtWidgets.QTableWidget(8, 2)
        self.table.setHorizontalHeaderLabels(["Metric", "Value"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)

        controls = QtWidgets.QHBoxLayout()
        controls.addWidget(self.modeCombo)
        controls.addWidget(self.runButton)
        controls.addWidget(self.clearButton)
        controls.addStretch()

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(controls)
        layout.addWidget(self.table)
        self.setLayout(layout)

        self.modeCombo.currentIndexChanged.connect(self._mode_changed)
        self.runButton.clicked.connect(self.update_stats)
        self.clearButton.clicked.connect(self._clear_roi)
        self._toolManager.sigShapesChanged.connect(self.update_stats)
        try:
            self._viewer.dims.events.current_step.connect(lambda _event: self.update_stats())
        except Exception:
            pass

        self._mode_changed()
        self._set_message("No statistics computed.")

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
        if self.modeCombo.currentText() == "Rectangle ROI":
            self._toolManager.set_mode("rectangle")
        else:
            self._toolManager.set_mode("pan")
        self.update_stats()

    def _clear_roi(self) -> None:
        self._toolManager.clear_shapes()
        self.update_stats()

    def _show_stats(self, stats: ROIStats) -> None:
        rows = [
            ("Area px", stats.area_pixels),
            ("Finite px", stats.finite_pixels),
            ("Mean", stats.mean),
            ("Median", stats.median),
            ("Std", stats.std),
            ("Min", stats.minimum),
            ("Max", stats.maximum),
            ("Sum", stats.total),
        ]
        self.table.setRowCount(len(rows))
        for row, (name, value) in enumerate(rows):
            self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(str(name)))
            if isinstance(value, float):
                text = f"{value:.6g}" if np.isfinite(value) else "nan"
            else:
                text = str(value)
            self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(text))

    def _set_message(self, message: str) -> None:
        self.table.setRowCount(1)
        self.table.setItem(0, 0, QtWidgets.QTableWidgetItem("Status"))
        self.table.setItem(0, 1, QtWidgets.QTableWidgetItem(message))

    def _current_rectangle_roi(self) -> tuple[int, int, int, int] | None:
        for index, shape_type in enumerate(self._toolManager.get_shape_types()):
            if shape_type == "rectangle":
                r0, c0, r1, c1 = self._toolManager.get_rectangle_bounds(index)
                return int(round(r0)), int(round(r1)), int(round(c0)), int(round(c1))
        return None

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
        try:
            active = self._viewer.layers.selection.active
        except Exception:
            active = None
        if self._is_image_layer(active):
            return active
        for layer in self._viewer.layers:
            if self._is_image_layer(layer):
                return layer
        return None

    @staticmethod
    def _is_image_layer(layer) -> bool:
        return (
            layer is not None
            and hasattr(layer, "data")
            and isinstance(layer.data, np.ndarray)
            and layer.data.ndim >= 2
            and getattr(layer, "visible", True)
            and not str(getattr(layer, "name", "")).startswith("_")
            and getattr(layer, "name", "") != "Viewer Tools"
        )
