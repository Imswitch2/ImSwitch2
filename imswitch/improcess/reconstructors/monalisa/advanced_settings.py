"""Expert numerical settings kept out of the normal MoNaLISA workflow UI."""

from __future__ import annotations

from qtpy import QtWidgets

from .params import PLACEMENT_AUTO, PLACEMENT_DIRECT, PLACEMENT_INTERPOLATE
from .footprint_suggestion import suggest_local_footprint_size
from .params import FOOTPRINT_FIXED, FOOTPRINT_FROM_LATTICE


class AdvancedReconstructionDialog(QtWidgets.QDialog):
    """Edit implementation-level reconstruction settings.

    These values stay part of the stable parameter contract for replay and
    headless workflows, but they are intentionally not peers of localization,
    scanning or the spot model in the interactive panel.
    """

    def __init__(self, values: dict, parent=None, *, localization=None):
        super().__init__(parent)
        self.setWindowTitle("Expert reconstruction parameters")
        self._localization = localization

        self.device = QtWidgets.QComboBox()
        self.device.addItems(["GPU", "CPU"])
        self.device.setCurrentText(str(values.get("device", "GPU")))
        self.device.setToolTip(
            "Choose where Gaussian extraction runs. GPU is normally much faster; "
            "CPU is useful for compatibility or debugging."
        )

        self.pixelNm = QtWidgets.QDoubleSpinBox()
        self.pixelNm.setRange(0.0, 1e7)
        self.pixelNm.setDecimals(3)
        self.pixelNm.setSuffix(" nm")
        self.pixelNm.setSpecialValueText("from data")
        self.pixelNm.setValue(float(values.get("pixel_size_nm", 0.0) or 0.0))
        self.pixelNm.setToolTip(
            "Camera-plane pixel size used to convert between detector pixels and nanometres. "
            "Leave at 0 to use the value stored with the dataset."
        )

        execution = QtWidgets.QGroupBox("Execution")
        executionForm = QtWidgets.QFormLayout(execution)
        executionForm.addRow("CPU/GPU", self.device)
        executionForm.addRow("Camera pixel", self.pixelNm)

        self.placement = QtWidgets.QComboBox()
        self.placement.addItems([PLACEMENT_AUTO, PLACEMENT_DIRECT, PLACEMENT_INTERPOLATE])
        self.placement.setCurrentText(str(values.get("placement_mode", PLACEMENT_AUTO)))
        self.placement.setToolTip(
            "Auto uses direct placement when samples fall on the reconstruction raster and "
            "interpolates only when the geometry is genuinely off-grid."
        )
        self.tolerance = QtWidgets.QDoubleSpinBox()
        self.tolerance.setRange(0.0, 1.0)
        self.tolerance.setDecimals(3)
        self.tolerance.setSingleStep(0.01)
        self.tolerance.setSuffix(" px")
        self.tolerance.setValue(float(values.get("commensurate_tolerance", 0.05)))
        self.tolerance.setToolTip(
            "Maximum off-grid residual, in output-raster pixels, still accepted for direct placement."
        )
        self.variance = QtWidgets.QCheckBox("Variance-weight duplicate samples")
        self.variance.setChecked(bool(values.get("variance_weighting", False)))
        self.variance.setToolTip(
            "When several measurements land on the same output pixel, combine them using "
            "inverse-variance weighting when variance estimates are available."
        )

        placement = QtWidgets.QGroupBox("Placement")
        placementForm = QtWidgets.QFormLayout(placement)
        placementForm.addRow("Mode", self.placement)
        placementForm.addRow("Direct tolerance", self.tolerance)
        placementForm.addRow(self.variance)

        self.sharpenSigma = QtWidgets.QDoubleSpinBox()
        self.sharpenSigma.setRange(0.1, 100.0)
        self.sharpenSigma.setDecimals(2)
        self.sharpenSigma.setSuffix(" px")
        self.sharpenSigma.setValue(float(values.get("sharpen_sigma_px", 1.5)))
        self.sharpenSigma.setToolTip(
            "Spatial scale of the blur model used by the optional sharpening post-processing."
        )
        self.sharpenRegularization = QtWidgets.QDoubleSpinBox()
        self.sharpenRegularization.setRange(0.001, 100.0)
        self.sharpenRegularization.setDecimals(3)
        self.sharpenRegularization.setValue(float(values.get("sharpen_regularization", 0.1)))
        self.sharpenRegularization.setToolTip(
            "Regularization strength for sharpening. Higher values suppress noise amplification "
            "but reduce the sharpening effect."
        )

        sharpening = QtWidgets.QGroupBox("Sharpening")
        sharpeningForm = QtWidgets.QFormLayout(sharpening)
        sharpeningForm.addRow("Blur", self.sharpenSigma)
        sharpeningForm.addRow("Regularization", self.sharpenRegularization)

        # One small section in the existing Expert dialog; no new footprint UI.
        self.estimator = QtWidgets.QComboBox()
        self.estimator.addItems(["local", "joint"])
        self.estimator.setCurrentText(str(values.get("estimator", "local")))
        self.estimator.setToolTip("Local fits spots independently; Joint fits neighboring Gaussian signals together.")
        self.footprintMode = QtWidgets.QComboBox()
        self.footprintMode.addItem("From lattice", FOOTPRINT_FROM_LATTICE)
        self.footprintMode.addItem("Fixed", FOOTPRINT_FIXED)
        mode = str(values.get("local_footprint_mode", FOOTPRINT_FIXED))
        self.footprintMode.setCurrentIndex(max(0, self.footprintMode.findData(mode)))
        self.footprintMode.setToolTip(
            "From lattice chooses a safe odd size when building the extraction operator. "
            "Fixed uses the entered size. Both choices can be saved as setup defaults."
        )
        self.footprintShape = QtWidgets.QComboBox()
        self.footprintShape.addItems(["square", "circle"])
        self.footprintShape.setCurrentText(str(values.get("local_footprint_shape", "square")))
        self.footprintSize = QtWidgets.QSpinBox()
        self.footprintSize.setRange(3, 255)
        self.footprintSize.setSingleStep(2)
        size = int(values.get("local_footprint_size", 9))
        self.footprintSize.setValue(size if size % 2 else max(3, size - 1))
        self.footprintSize.setToolTip("Odd camera-pixel side length (Square) or diameter (Circle).")
        self.suggestButton = QtWidgets.QPushButton("Suggest from lattice")
        self.suggestButton.setToolTip("Apply a conservative size from the current localized pattern once; never resizes during reconstruction.")
        self.suggestButton.setEnabled(localization is not None)
        if localization is None:
            self.suggestButton.setToolTip("Localize or select a pattern first to suggest a footprint size.")
        self.suggestButton.clicked.connect(self._suggest_size)
        self.sampling = QtWidgets.QComboBox()
        self.sampling.addItems(["camera_pixels", "bilinear"])
        self.sampling.setCurrentText(str(values.get("local_sampling", "camera_pixels")))
        self.sampling.setToolTip("Fit original camera pixels or the WIP bilinear shifted stencil.")
        footprintRow = QtWidgets.QHBoxLayout()
        footprintRow.addWidget(self.footprintSize)
        footprintRow.addWidget(self.suggestButton)
        gaussian = QtWidgets.QGroupBox("Gaussian fitting")
        gaussianForm = QtWidgets.QFormLayout(gaussian)
        gaussianForm.addRow("Estimator", self.estimator)
        gaussianForm.addRow("Local shape", self.footprintShape)
        gaussianForm.addRow("Local size mode", self.footprintMode)
        gaussianForm.addRow("Local side/diameter", footprintRow)
        gaussianForm.addRow("Local sampling", self.sampling)
        self.estimator.currentTextChanged.connect(self._update_local_enabled)
        self.footprintMode.currentIndexChanged.connect(self._update_local_enabled)
        self.footprintSize.valueChanged.connect(self._odd_footprint_size)
        self._update_local_enabled()

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(gaussian)
        layout.addWidget(execution)
        layout.addWidget(placement)
        layout.addWidget(sharpening)
        layout.addWidget(buttons)

    def _odd_footprint_size(self, size: int) -> None:
        if size % 2 == 0:
            old = self.footprintSize.blockSignals(True)
            self.footprintSize.setValue(min(255, size + 1))
            self.footprintSize.blockSignals(old)

    def _update_local_enabled(self, *_):
        local = self.estimator.currentText() == "local"
        for widget in (self.footprintShape, self.footprintMode, self.sampling):
            widget.setEnabled(local)
        self.footprintSize.setEnabled(local and self.footprintMode.currentData() == FOOTPRINT_FIXED)
        self.suggestButton.setEnabled(local and self._localization is not None)

    def _suggest_size(self):
        try:
            size = suggest_local_footprint_size(
                self._localization, self.footprintShape.currentText()
            )
        except ValueError as error:
            QtWidgets.QMessageBox.warning(self, "Footprint suggestion", str(error))
        else:
            self.footprintMode.setCurrentIndex(self.footprintMode.findData(FOOTPRINT_FIXED))
            self.footprintSize.setValue(size)

    def values(self) -> dict:
        return {
            "estimator": self.estimator.currentText(),
            "local_footprint_mode": str(self.footprintMode.currentData()),
            "local_footprint_shape": self.footprintShape.currentText(),
            "local_footprint_size": int(self.footprintSize.value()),
            "local_sampling": self.sampling.currentText(),
            "device": self.device.currentText(),
            "pixel_size_nm": float(self.pixelNm.value()),
            "placement_mode": self.placement.currentText(),
            "commensurate_tolerance": float(self.tolerance.value()),
            "variance_weighting": bool(self.variance.isChecked()),
            "sharpen_sigma_px": float(self.sharpenSigma.value()),
            "sharpen_regularization": float(self.sharpenRegularization.value()),
        }


__all__ = ["AdvancedReconstructionDialog"]
