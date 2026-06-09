"""Interactive FRC analysis panel for ImProcess."""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from qtpy import QtWidgets

from imswitch.improcess.analysis.frc import frc_two_image, single_image_frc


class FRCWidget(QtWidgets.QWidget):
    """Compute FRC or single-image FRC from the active napari image layer."""

    def __init__(self, napariViewer, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._viewer = napariViewer

        self.modeCombo = QtWidgets.QComboBox()
        self.modeCombo.addItems(["single-image", "two-image"])

        self.axisCombo = QtWidgets.QComboBox()
        self.axisCombo.addItems(["Auto", "T", "C", "Z", "D0", "D1", "D2"])

        self.indexASpin = QtWidgets.QSpinBox()
        self.indexASpin.setRange(0, 999999)
        self.indexASpin.setValue(0)

        self.indexBSpin = QtWidgets.QSpinBox()
        self.indexBSpin.setRange(0, 999999)
        self.indexBSpin.setValue(1)

        self.splitCombo = QtWidgets.QComboBox()
        self.splitCombo.addItems(["checkerboard", "odd-even"])

        self.windowCombo = QtWidgets.QComboBox()
        self.windowCombo.addItems(["hann", "none"])

        self.pixelSizeSpin = QtWidgets.QDoubleSpinBox()
        self.pixelSizeSpin.setDecimals(6)
        self.pixelSizeSpin.setRange(1e-9, 1e12)
        self.pixelSizeSpin.setValue(1.0)

        self.unitCombo = QtWidgets.QComboBox()
        self.unitCombo.addItems(["px", "nm", "um"])

        self.runButton = QtWidgets.QPushButton("Run")
        self.summaryLabel = QtWidgets.QLabel("")
        self.summaryLabel.setWordWrap(True)
        self.summaryLabel.setStyleSheet("color:#888; font-size:8pt;")

        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.addLegend()

        form = QtWidgets.QFormLayout()
        form.addRow("Mode", self.modeCombo)
        form.addRow("Compare axis", self.axisCombo)
        form.addRow("Index A", self.indexASpin)
        form.addRow("Index B", self.indexBSpin)
        form.addRow("Single split", self.splitCombo)
        form.addRow("Window", self.windowCombo)
        form.addRow("Pixel size", self.pixelSizeSpin)
        form.addRow("Unit", self.unitCombo)

        controls = QtWidgets.QHBoxLayout()
        controls.addLayout(form)
        controls.addWidget(self.runButton)

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(controls)
        layout.addWidget(self.plot, 1)
        layout.addWidget(self.summaryLabel)
        self.setLayout(layout)

        self.runButton.clicked.connect(self.run)
        self._draw_empty()

    def run(self) -> None:
        layer = self._active_image_layer()
        if layer is None:
            self._draw_empty("No image layer selected.")
            return

        try:
            data = np.asarray(layer.data)
            unit = self.unitCombo.currentText()
            common = {
                "pixel_size": float(self.pixelSizeSpin.value()),
                "frequency_unit": f"cycles/{unit}",
                "resolution_unit": unit,
                "window": self.windowCombo.currentText(),
            }
            if self.modeCombo.currentText() == "two-image":
                axis = self._resolve_compare_axis(layer, data)
                image_a = self._extract_2d(layer, data, axis, self.indexASpin.value())
                image_b = self._extract_2d(layer, data, axis, self.indexBSpin.value())
                analysis = frc_two_image(image_a, image_b, **common)
            else:
                image = self._extract_2d(layer, data, None, self.indexASpin.value())
                analysis = single_image_frc(
                    image,
                    split=self.splitCombo.currentText(),
                    **common,
                )
        except Exception as exc:
            self._draw_empty(str(exc))
            return

        self.plot.clear()
        self.plot.setTitle("Fourier Ring Correlation")
        self.plot.setLabel("bottom", f"Spatial frequency ({analysis.frequency_unit})")
        self.plot.setLabel("left", "Correlation")
        self.plot.plot(analysis.frequency, analysis.frc, pen=pg.mkPen("c", width=2), name="FRC")
        self.plot.plot(
            analysis.frequency,
            analysis.threshold,
            pen=pg.mkPen("y", width=1),
            name="1/7 threshold",
        )
        if np.isfinite(analysis.cutoff_frequency):
            self.plot.plot(
                [analysis.cutoff_frequency, analysis.cutoff_frequency],
                [0.0, 1.0],
                pen=pg.mkPen("r", width=1),
                name="cutoff",
            )

        if np.isfinite(analysis.resolution):
            self.summaryLabel.setText(
                f"Resolution {analysis.resolution:.4g} {analysis.resolution_unit}; "
                f"cutoff {analysis.cutoff_frequency:.4g} {analysis.frequency_unit}"
            )
        else:
            self.summaryLabel.setText("No threshold crossing found.")

    def _draw_empty(self, message: str = "Run FRC on the active image layer.") -> None:
        self.plot.clear()
        self.plot.setTitle("Fourier Ring Correlation")
        self.plot.setLabel("bottom", "Spatial frequency")
        self.plot.setLabel("left", "Correlation")
        self.summaryLabel.setText(message)

    def _resolve_compare_axis(self, layer, data: np.ndarray) -> int:
        requested = self.axisCombo.currentText()
        labels = self._axis_labels(layer, data)
        if requested != "Auto":
            if requested not in labels:
                raise ValueError(f"Axis {requested!r} is not available for this layer")
            axis = labels.index(requested)
            if data.shape[axis] < 2:
                raise ValueError(f"Axis {requested!r} needs at least two planes")
            return axis
        for candidate in ("T", "C", "Z"):
            if candidate in labels and data.shape[labels.index(candidate)] >= 2:
                return labels.index(candidate)
        for axis, size in enumerate(data.shape[:-2]):
            if size >= 2:
                return axis
        raise ValueError("Two-image FRC needs an axis with at least two planes")

    def _extract_2d(self, layer, data: np.ndarray, compare_axis: int | None, index: int):
        if data.ndim == 2:
            return data
        step = self._current_step(data.ndim)
        indexer = []
        for axis, size in enumerate(data.shape):
            if axis >= data.ndim - 2:
                indexer.append(slice(None))
            elif compare_axis is not None and axis == compare_axis:
                if index < 0 or index >= size:
                    raise ValueError(f"Index {index} is out of range for axis size {size}")
                indexer.append(index)
            else:
                indexer.append(min(max(step[axis], 0), size - 1))
        image = np.asarray(data[tuple(indexer)])
        if image.ndim != 2:
            labels = self._axis_labels(layer, data)
            raise ValueError(f"Could not extract a 2D image from axes {labels}")
        return image

    def _current_step(self, ndim: int) -> tuple[int, ...]:
        try:
            step = tuple(int(v) for v in self._viewer.dims.current_step)
        except Exception:
            step = ()
        if len(step) < ndim:
            step = (*step, *(0 for _ in range(ndim - len(step))))
        return step

    def _axis_labels(self, layer, data: np.ndarray) -> list[str]:
        try:
            labels = list(layer.metadata.get("axis_labels", []))
        except Exception:
            labels = []
        if len(labels) == data.ndim:
            return [str(label) for label in labels]
        defaults = ["T", "Z", "C", "Y", "X"]
        if data.ndim <= len(defaults):
            return defaults[-data.ndim:]
        extra = [f"D{i}" for i in range(data.ndim - len(defaults))]
        return [*extra, *defaults]

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
        )
