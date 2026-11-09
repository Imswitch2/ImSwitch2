"""Expert numerical settings kept out of the normal MoNaLISA workflow UI."""

from __future__ import annotations

from qtpy import QtWidgets

from .params import PLACEMENT_AUTO, PLACEMENT_DIRECT, PLACEMENT_INTERPOLATE
from .footprint_suggestion import suggest_local_footprint_size
from .params import FOOTPRINT_FIXED, FOOTPRINT_FROM_LATTICE
from .footprint_config import SHAPES, MODES, footprint_from_params


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
        fit = footprint_from_params(values, estimator=self.estimator.currentText())
        self.footprintShape = QtWidgets.QComboBox()
        self.footprintShape.addItem("Square", "square")
        self.footprintShape.addItem("Circle", "circle")
        self.footprintShape.addItem("Lattice unit cells (DLL)", "lattice")
        self.footprintShape.setCurrentIndex(self.footprintShape.findData(fit.shape))
        self.footprintShape.setToolTip(
            "Select which original camera pixels contribute to the Gaussian fit. "
            "Lattice cells require an axis-aligned rectangular, single-motif pattern."
        )
        self.footprintMode = QtWidgets.QComboBox()
        self.footprintMode.addItem("From lattice", "from_lattice")
        self.footprintMode.addItem("Fixed", "fixed")
        self.footprintMode.addItem("FWHM-relative", "fwhm")
        self.footprintMode.setCurrentIndex(self.footprintMode.findData(fit.mode))
        self.footprintMode.setToolTip(
            "From lattice selects a conservative footprint size from the detected pattern; "
            "Fixed uses the entered side/diameter; FWHM-relative uses a radius "
            "proportional to the spot sigma. Lattice unit cells ignore sizing."
        )
        self.footprintSize = QtWidgets.QDoubleSpinBox()
        self.footprintSize.setRange(0.1, 255.0)
        self.footprintSize.setDecimals(2)
        self.footprintSize.setSingleStep(1.0)
        self.footprintSize.setSuffix(" px")
        self.footprintSize.setValue(fit.size_px)
        self.footprintSize.setToolTip(
            "Square: odd side in camera pixels. Circle: diameter in camera pixels "
            "(twice the actual fit radius)."
        )
        self.sigmaFactor = QtWidgets.QDoubleSpinBox()
        self.sigmaFactor.setRange(0.1, 100.0)
        self.sigmaFactor.setDecimals(3)
        self.sigmaFactor.setSingleStep(0.25)
        self.sigmaFactor.setSuffix(" σ")
        self.sigmaFactor.setValue(fit.sigma_factor)
        self.sigmaFactor.setToolTip(
            "Fit radius expressed in spot sigmas. Applies to both estimators. "
            "Joint also models neighboring Gaussian tails out to its normal 5.5σ support."
        )
        self.suggestButton = QtWidgets.QPushButton("Suggest from lattice")
        self.suggestButton.setToolTip(
            "Apply the currently suggested size once, switching to Fixed. "
            "From lattice instead recomputes the size on each new localization."
        )
        self.suggestButton.clicked.connect(self._suggest_size)
        self.sampling = QtWidgets.QComboBox()
        self.sampling.addItems(["camera_pixels", "bilinear"])
        self.sampling.setCurrentText(str(values.get("local_sampling", "camera_pixels")))
        self.sampling.setToolTip(
            "Local only: bilinear samples an interpolated stencil rather than "
            "the camera pixels used by Joint. Use camera_pixels for matched fit supports."
        )
        footprintRow = QtWidgets.QHBoxLayout()
        footprintRow.addWidget(self.footprintSize)
        footprintRow.addWidget(self.suggestButton)
        gaussian = QtWidgets.QGroupBox("Gaussian fitting")
        gaussianForm = QtWidgets.QFormLayout(gaussian)
        gaussianForm.addRow("Estimator", self.estimator)
        gaussianForm.addRow("Footprint shape", self.footprintShape)
        gaussianForm.addRow("Footprint sizing", self.footprintMode)
        gaussianForm.addRow("Side / diameter", footprintRow)
        gaussianForm.addRow("Radius / sigma", self.sigmaFactor)
        gaussianForm.addRow("Local sampling", self.sampling)
        self.estimator.currentTextChanged.connect(self._update_footprint_enabled)
        self.footprintShape.currentIndexChanged.connect(self._update_footprint_enabled)
        self.footprintMode.currentIndexChanged.connect(self._update_footprint_enabled)
        self._update_footprint_enabled()

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

    def _update_footprint_enabled(self, *_):
        shape = self.footprintShape.currentData()
        mode = self.footprintMode.currentData()
        cells = shape == "lattice"
        self.footprintMode.setEnabled(not cells)
        self.footprintSize.setEnabled(not cells and mode == "fixed")
        self.sigmaFactor.setEnabled(not cells and mode == "fwhm")
        self.suggestButton.setEnabled(not cells and self._localization is not None)
        if cells and self.sampling.currentText() != "camera_pixels":
            self.sampling.setCurrentText("camera_pixels")
        self.sampling.setEnabled(self.estimator.currentText() == "local" and not cells)

    def _suggest_size(self):
        try:
            size = suggest_local_footprint_size(
                self._localization, str(self.footprintShape.currentData())
            )
        except ValueError as error:
            QtWidgets.QMessageBox.warning(self, "Footprint suggestion", str(error))
        else:
            self.footprintMode.setCurrentIndex(self.footprintMode.findData(FOOTPRINT_FIXED))
            self.footprintSize.setValue(size)

    def accept(self):
        if (self.footprintShape.currentData() == "square"
                and self.footprintMode.currentData() == "fixed"):
            value = self.footprintSize.value()
            if value < 3 or value % 2 != 1:
                QtWidgets.QMessageBox.warning(
                    self, "Invalid footprint", "Fixed square support requires an odd integer side of at least 3 px."
                )
                return
        super().accept()

    def values(self) -> dict:
        return {
            "estimator": self.estimator.currentText(),
            "footprint_shape": str(self.footprintShape.currentData()),
            "footprint_mode": str(self.footprintMode.currentData()),
            "footprint_size_px": float(self.footprintSize.value()),
            "footprint_sigma_factor": float(self.sigmaFactor.value()),
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
