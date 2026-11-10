"""Expert numerical settings kept out of the normal MoNaLISA workflow UI."""

from __future__ import annotations

from qtpy import QtWidgets

from .params import PLACEMENT_AUTO, PLACEMENT_DIRECT, PLACEMENT_INTERPOLATE


class AdvancedReconstructionDialog(QtWidgets.QDialog):
    """Edit implementation-level reconstruction settings.

    These values stay part of the stable parameter contract for replay and
    headless workflows, but they are intentionally not peers of localization,
    scanning or the spot model in the interactive panel.
    """

    def __init__(self, values: dict, parent=None, *, localization=None, dll_equivalent=False):
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

        # Everyday estimator and footprint settings now live in Spot model.
        # Keep only specialist numerical controls here to avoid two editors.
        self.background = QtWidgets.QComboBox()
        self.background.addItems(["constant", "none", "auto"])
        self.background.setCurrentText(str(values.get("background", "constant")))
        self.background.setToolTip(
            "Background handling for Gaussian extraction, independent of spot calibration."
        )
        self.sampling = QtWidgets.QComboBox()
        self.sampling.addItems(["camera_pixels", "bilinear"])
        self.sampling.setCurrentText(str(values.get("local_sampling", "camera_pixels")))
        self.sampling.setToolTip(
            "Used by Local fitting only. Camera pixels allow direct comparison with Joint."
        )
        gaussian = QtWidgets.QGroupBox("Gaussian numerics")
        gaussianForm = QtWidgets.QFormLayout(gaussian)
        gaussianForm.addRow("Background", self.background)
        gaussianForm.addRow("Local sampling", self.sampling)

        if dll_equivalent:
            # The DLL-equivalent mode fixes the Gaussian numerical model and
            # does not perform amplitude-variance fitting or sharpening.
            # Placement and camera-pixel calibration remain configurable.
            for group in (gaussian, sharpening):
                group.setEnabled(False)
                group.setToolTip("Not used by DLL-equivalent reconstruction")
            self.variance.setEnabled(False)
            self.variance.setToolTip("DLL-equivalent has no amplitude-variance solve")

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

    def values(self) -> dict:
        return {
            "background": self.background.currentText(),
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
