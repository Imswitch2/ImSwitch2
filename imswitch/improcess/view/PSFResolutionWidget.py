"""Interactive PSF / bead resolution panel for ImProcess.

Producing panel: the Fit button runs PSFResolutionProcessor on the selected
result via the generic run->publish pipeline (sigRunRequested ->
ResultProcessorController -> sigResultProduced). The published
PSFResolutionResult renders in the results table dock (with CSV export);
this panel only holds the inputs the generic parameter widget cannot offer,
most importantly ROI Manager sourcing.
"""

from __future__ import annotations

from qtpy import QtCore, QtWidgets

from imswitch.improcess.processors import PSFResolutionProcessor


class PSFResolutionWidget(QtWidgets.QWidget):
    """Fit 2D Gaussian PSFs on the selected result, full-frame or per ROI."""

    sigRunRequested = QtCore.Signal(object, dict)

    def __init__(self, napariViewer, roiManagerWidget=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._viewer = napariViewer
        self._roiManagerWidget = roiManagerWidget
        self._currentResult = None
        self.processor = PSFResolutionProcessor()

        self.sourceCombo = QtWidgets.QComboBox()
        self.sourceCombo.addItems(["Full image", "ROI Manager"])

        self.pixelSizeSpin = QtWidgets.QDoubleSpinBox()
        self.pixelSizeSpin.setDecimals(6)
        self.pixelSizeSpin.setRange(1e-9, 1e12)
        self.pixelSizeSpin.setValue(1.0)

        self.unitCombo = QtWidgets.QComboBox()
        self.unitCombo.addItems(["px", "nm", "um"])

        self.fitButton = QtWidgets.QPushButton("Fit")
        self.fitButton.setEnabled(False)

        self.summaryLabel = QtWidgets.QLabel(
            "Select a result, then fit PSF resolution. "
            "The fits appear in the results list and table."
        )
        self.summaryLabel.setWordWrap(True)
        self.summaryLabel.setStyleSheet("color:#888; font-size:8pt;")

        form = QtWidgets.QFormLayout()
        form.addRow("Source", self.sourceCombo)
        form.addRow("Pixel size", self.pixelSizeSpin)
        form.addRow("Unit", self.unitCombo)

        controls = QtWidgets.QHBoxLayout()
        controls.addLayout(form)
        controls.addWidget(self.fitButton)
        controls.addStretch()

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(controls)
        layout.addWidget(self.summaryLabel)
        layout.addStretch()
        self.setLayout(layout)

        self.fitButton.clicked.connect(self.run)

    def run(self) -> None:
        if self._currentResult is None:
            self.summaryLabel.setText(
                "No result selected. Load or create a result first."
            )
            return
        try:
            self.sigRunRequested.emit(self._currentResult, self.parameterValues())
        except Exception as exc:
            self.summaryLabel.setText(str(exc))

    def parameterValues(self) -> dict:
        """Conform to result-processor widget contract: return current params.

        Keys MUST match PSFResolutionProcessor.apply()'s param contract
        (pixel_size/unit/rois)."""
        params = {
            "pixel_size": float(self.pixelSizeSpin.value()),
            "unit": self.unitCombo.currentText(),
        }
        if self.sourceCombo.currentText() == "ROI Manager":
            if self._roiManagerWidget is None:
                raise ValueError("ROI Manager panel is not enabled.")
            rois = self._roiManagerWidget.rois()
            if not rois:
                raise ValueError("ROI Manager has no ROIs.")
            params["rois"] = rois
        return params

    def setCurrentResult(self, result) -> None:
        """Conform to result-processor widget contract: store the current result."""
        self._currentResult = result
        has_image = (
            result is not None
            and getattr(result, "data", None) is not None
        )
        self.fitButton.setEnabled(has_image)

    def setStatusText(self, text: str) -> None:
        """Conform to result-processor widget contract: forward to summaryLabel."""
        self.summaryLabel.setText(text)

    def setRoiManagerWidget(self, roiManagerWidget) -> None:
        """Wire (or rewire) the ROI Manager dependency at runtime."""
        self._roiManagerWidget = roiManagerWidget
