"""Interactive projection panel for ImProcess."""

from __future__ import annotations

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.analysis.projections import (
    axis_index_from_label,
    axis_labels_for_shape,
    project_array,
)


class ProjectionWidget(QtWidgets.QWidget):
    """Create projections from the active napari image layer."""

    def __init__(self, napariViewer, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._viewer = napariViewer

        self.axisCombo = QtWidgets.QComboBox()
        self.axisCombo.addItems(["Auto", "T", "Z", "C", "Y", "X", "D0", "D1", "D2"])

        self.modeCombo = QtWidgets.QComboBox()
        self.modeCombo.addItems(["max", "mean", "sum", "median", "std"])

        self.outputNameEdit = QtWidgets.QLineEdit()
        self.outputNameEdit.setPlaceholderText("Auto")

        self.runButton = QtWidgets.QPushButton("Project")
        self.summaryLabel = QtWidgets.QLabel("Create a projection from the active image layer.")
        self.summaryLabel.setWordWrap(True)
        self.summaryLabel.setStyleSheet("color:#888; font-size:8pt;")

        form = QtWidgets.QFormLayout()
        form.addRow("Axis", self.axisCombo)
        form.addRow("Mode", self.modeCombo)
        form.addRow("Output name", self.outputNameEdit)

        controls = QtWidgets.QHBoxLayout()
        controls.addLayout(form)
        controls.addWidget(self.runButton)

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(controls)
        layout.addWidget(self.summaryLabel)
        self.setLayout(layout)

        self.runButton.clicked.connect(self.run)

    def run(self) -> None:
        layer = self._active_image_layer()
        if layer is None:
            self.summaryLabel.setText("No image layer selected.")
            return
        try:
            data = np.asarray(layer.data)
            labels = self._axis_labels(layer, data)
            axis = self._resolve_axis(data, labels)
            analysis = project_array(
                data,
                axis=axis,
                mode=self.modeCombo.currentText(),
                axis_labels=labels,
                axis_scales=self._axis_scales(layer, data),
            )
            name = self.outputNameEdit.text().strip()
            if not name:
                name = f"{getattr(layer, 'name', 'image')} {analysis.mode} {analysis.axis_label}-projection"
            self._viewer.add_image(
                analysis.data,
                name=name,
                metadata={
                    "axis_labels": analysis.output_axis_labels,
                    "projection": analysis.metadata,
                },
            )
            self.summaryLabel.setText(
                f"Created {analysis.mode} projection along {analysis.axis_label}: "
                f"{analysis.input_shape} -> {analysis.data.shape}"
            )
        except Exception as exc:
            self.summaryLabel.setText(str(exc))

    def _resolve_axis(self, data: np.ndarray, labels: list[str]) -> int:
        requested = self.axisCombo.currentText()
        if requested != "Auto":
            return axis_index_from_label(requested, labels, data.ndim)
        for label in ("Z", "T", "C"):
            if label in labels and data.shape[labels.index(label)] > 1:
                return labels.index(label)
        if data.ndim > 2:
            return 0
        return data.ndim - 1

    def _axis_labels(self, layer, data: np.ndarray) -> list[str]:
        try:
            labels = list(layer.metadata.get("axis_labels", []))
        except Exception:
            labels = []
        return axis_labels_for_shape(data, labels)

    @staticmethod
    def _axis_scales(layer, data: np.ndarray) -> list[float]:
        try:
            scale = list(getattr(layer, "scale", []))
        except Exception:
            scale = []
        if len(scale) == data.ndim:
            return [float(value) for value in scale]
        return [1.0 for _ in range(data.ndim)]

    def _active_image_layer(self):
        """Return the active image-like Napari layer, falling back to the first valid one.
        
        Aligned with ReconstructionView.getActiveImageLayer() semantics: prefer the active
        layer when it's image-like, otherwise scan for a valid layer.
        """
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
