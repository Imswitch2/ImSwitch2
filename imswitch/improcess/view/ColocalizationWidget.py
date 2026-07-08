"""Interactive colocalization panel for ImProcess.

Producing panel: the Run button runs ColocalizationProcessor on the selected
result via the generic run->publish pipeline (sigRunRequested ->
ResultProcessorController -> sigResultProduced). The published
ColocalizationResult renders in the results table dock (with CSV export) and
its intensity scatter in the graph dock; this panel only holds the inputs
the generic parameter widget cannot offer, most importantly ROI Manager
sourcing.
"""

from __future__ import annotations

from qtpy import QtCore, QtWidgets

from imswitch.improcess.processors import ColocalizationProcessor


class ColocalizationWidget(QtWidgets.QWidget):
    """Compute channel colocalization on the selected result."""

    sigRunRequested = QtCore.Signal(object, dict)

    def __init__(self, napariViewer, roiManagerWidget=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._viewer = napariViewer
        self._roiManagerWidget = roiManagerWidget
        self._currentResult = None
        self.processor = ColocalizationProcessor()

        self.sourceCombo = QtWidgets.QComboBox()
        self.sourceCombo.addItems(["Full image", "ROI Manager"])

        self.axisCombo = QtWidgets.QComboBox()
        self.axisCombo.addItems(["Auto", "C", "T", "Z", "D0", "D1", "D2"])

        self.indexASpin = QtWidgets.QSpinBox()
        self.indexASpin.setRange(0, 999999)
        self.indexASpin.setValue(0)

        self.indexBSpin = QtWidgets.QSpinBox()
        self.indexBSpin.setRange(0, 999999)
        self.indexBSpin.setValue(1)

        self.thresholdASpin = QtWidgets.QDoubleSpinBox()
        self.thresholdASpin.setDecimals(6)
        self.thresholdASpin.setRange(-1e12, 1e12)
        self.thresholdASpin.setValue(0.0)

        self.thresholdBSpin = QtWidgets.QDoubleSpinBox()
        self.thresholdBSpin.setDecimals(6)
        self.thresholdBSpin.setRange(-1e12, 1e12)
        self.thresholdBSpin.setValue(0.0)

        self.runButton = QtWidgets.QPushButton("Run")
        self.runButton.setEnabled(False)

        self.summaryLabel = QtWidgets.QLabel(
            "Select a result stack, then run colocalization. "
            "The metrics appear in the results list and table."
        )
        self.summaryLabel.setWordWrap(True)
        self.summaryLabel.setStyleSheet("color:#888; font-size:8pt;")

        form = QtWidgets.QFormLayout()
        form.addRow("Source", self.sourceCombo)
        form.addRow("Compare axis", self.axisCombo)
        form.addRow("Index A", self.indexASpin)
        form.addRow("Index B", self.indexBSpin)
        form.addRow("Threshold A", self.thresholdASpin)
        form.addRow("Threshold B", self.thresholdBSpin)

        controls = QtWidgets.QHBoxLayout()
        controls.addLayout(form)
        controls.addWidget(self.runButton)
        controls.addStretch()

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(controls)
        layout.addWidget(self.summaryLabel)
        layout.addStretch()
        self.setLayout(layout)

        self.runButton.clicked.connect(self.run)

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

        Keys MUST match ColocalizationProcessor.apply()'s param contract
        (compare_axis/index_a/index_b/threshold_a/threshold_b/rois)."""
        params = {
            "compare_axis": self.axisCombo.currentText(),
            "index_a": self.indexASpin.value(),
            "index_b": self.indexBSpin.value(),
            "threshold_a": self.thresholdASpin.value(),
            "threshold_b": self.thresholdBSpin.value(),
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
        self.runButton.setEnabled(has_image)

    def setStatusText(self, text: str) -> None:
        """Conform to result-processor widget contract: forward to summaryLabel."""
        self.summaryLabel.setText(text)

    def setRoiManagerWidget(self, roiManagerWidget) -> None:
        """Wire (or rewire) the ROI Manager dependency at runtime."""
        self._roiManagerWidget = roiManagerWidget
