"""Fixed parameter tree for the unified Gaussian MoNaLISA reconstructor."""

from __future__ import annotations

from copy import deepcopy

from pyqtgraph.parametertree import Parameter, ParameterTree
from qtpy import QtCore, QtWidgets

from .localization_settings import DEFAULT_LOCALIZATION_SETTINGS, LocalizationSettings, PeriodPrior
from .params import (
    LOCALIZATION_AUTO,
    LOCALIZATION_MANUAL,
    LOCALIZATION_REFINEMENT_FAST,
    ORIENTATION_AUTO,
    ORIENTATION_MANUAL,
    PLACEMENT_AUTO,
    PLACEMENT_DIRECT,
    PLACEMENT_INTERPOLATE,
    PRESET_CONTROLLED_KEYS,
    PRESET_CUSTOM,
    PRESET_FAST,
    PRESET_FULL,
    SPOT_AUTO,
    SPOT_MANUAL,
    apply_preset,
    default_params,
)
from .scan_frame import ORIENTATIONS


class LocalizationSettingsDialog(QtWidgets.QDialog):
    """Small tuning dialog for automatic lattice localization."""

    def __init__(self, settings: LocalizationSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Localization settings")
        self._defaults = DEFAULT_LOCALIZATION_SETTINGS

        self.usePrior = QtWidgets.QCheckBox("Use expected period")
        self.periodX = QtWidgets.QDoubleSpinBox()
        self.periodY = QtWidgets.QDoubleSpinBox()
        self.tolerance = QtWidgets.QDoubleSpinBox()
        for widget in (self.periodX, self.periodY):
            widget.setRange(0.0, 100000.0)
            widget.setDecimals(3)
            widget.setSuffix(" px")
        self.tolerance.setRange(0.0, 2.0)
        self.tolerance.setDecimals(3)
        self.tolerance.setSingleStep(0.05)

        self.minPeriod = QtWidgets.QDoubleSpinBox()
        self.maxPeriod = QtWidgets.QDoubleSpinBox()
        for widget in (self.minPeriod, self.maxPeriod):
            widget.setRange(0.0, 100000.0)
            widget.setDecimals(3)
            widget.setSuffix(" px")
        self.maxPeriod.setToolTip("0: choose the longest usable period from the frame size")

        self.candidates = QtWidgets.QSpinBox()
        self.candidates.setRange(2, 128)
        self.significance = QtWidgets.QDoubleSpinBox()
        self.significance.setRange(0.1, 1000.0)
        self.significance.setDecimals(2)

        form = QtWidgets.QFormLayout()
        form.addRow(self.usePrior)
        form.addRow("Expected period X", self.periodX)
        form.addRow("Expected period Y", self.periodY)
        form.addRow("Tolerance (fraction)", self.tolerance)

        self.showAdvanced = QtWidgets.QCheckBox("Show advanced parameters")
        advanced = QtWidgets.QGroupBox("Advanced")
        advancedForm = QtWidgets.QFormLayout(advanced)
        advancedForm.addRow("Minimum period", self.minPeriod)
        advancedForm.addRow("Maximum period", self.maxPeriod)
        advancedForm.addRow("Spectral candidates", self.candidates)
        advancedForm.addRow("Peak significance", self.significance)

        self.resetButton = QtWidgets.QPushButton("Reset defaults")
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.resetButton.clicked.connect(lambda: self._load(self._defaults))

        footer = QtWidgets.QHBoxLayout()
        footer.addWidget(self.resetButton)
        footer.addStretch(1)
        footer.addWidget(buttons)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.showAdvanced)
        layout.addWidget(advanced)
        layout.addLayout(footer)

        advanced.setVisible(False)
        self.showAdvanced.toggled.connect(advanced.setVisible)
        self.usePrior.toggled.connect(self._update_prior_enabled)
        self._load(settings)

    def _load(self, settings: LocalizationSettings) -> None:
        prior = settings.period_prior
        self.usePrior.setChecked(bool(prior.enabled))
        self.periodX.setValue(float(prior.expected_x_px))
        self.periodY.setValue(float(prior.expected_y_px))
        self.tolerance.setValue(float(prior.tolerance_fraction))
        self.minPeriod.setValue(float(settings.min_period_px))
        self.maxPeriod.setValue(float(settings.max_period_px))
        self.candidates.setValue(int(settings.num_candidate_peaks))
        self.significance.setValue(float(settings.min_peak_significance))
        self._update_prior_enabled(self.usePrior.isChecked())

    def _update_prior_enabled(self, enabled: bool) -> None:
        self.periodX.setEnabled(enabled)
        self.periodY.setEnabled(enabled)
        self.tolerance.setEnabled(enabled)

    def settings(self) -> LocalizationSettings:
        return LocalizationSettings(
            min_period_px=float(self.minPeriod.value()),
            max_period_px=float(self.maxPeriod.value()),
            num_candidate_peaks=int(self.candidates.value()),
            min_peak_significance=float(self.significance.value()),
            period_prior=PeriodPrior(
                enabled=bool(self.usePrior.isChecked()),
                expected_x_px=float(self.periodX.value()),
                expected_y_px=float(self.periodY.value()),
                tolerance_fraction=float(self.tolerance.value()),
            ),
        )


class MonalisaParamsWidget(QtWidgets.QWidget):
    """One fixed parameter tree for every Gaussian MoNaLISA configuration."""

    sigPreviewToggled = QtCore.Signal(bool)
    sigDetectionParamsChanged = QtCore.Signal()
    sigLocalizeRequested = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        values = default_params()
        self._localization_settings = LocalizationSettings.from_dict(
            values["localization_settings"]
        )
        self._applying_preset = False
        self._spot_unit = str(values["spot_fwhm_unit"])
        self._auto_localization = deepcopy(values["auto_localization"])
        self._localization_refinement = str(values["localization_refinement"])
        self._localization_mode = str(values["localization_mode"])

        params = [
            {"name": "Preset", "type": "list", "values": [PRESET_FAST, PRESET_FULL, PRESET_CUSTOM],
             "value": values["preset"],
             "tip": "Fast prioritizes speed; Full enables the additional reconstruction corrections. "
                    "Changing a preset-controlled option switches to Custom."},
            {"name": "CPU/GPU", "type": "list", "values": ["GPU", "CPU"],
             "value": values["device"]},
            {"name": "Camera pixel", "type": "float", "suffix": "nm",
             "limits": (0.0, 100000.0), "value": values["pixel_size_nm"],
             "tip": "Effective sample-plane detector pixel size."},
            {"name": "Localization", "type": "group", "children": [
                {"name": "Mode", "type": "list", "values": [LOCALIZATION_AUTO, LOCALIZATION_MANUAL],
                 "value": values["localization_mode"]},
                {"name": "Localize", "type": "action"},
                {"name": "Relocalize each data item", "type": "bool",
                 "value": values["relocalize_each_data_item"],
                 "tip": "Multi-data only. Off reuses one Auto localization across all data items."},
                {"name": "Offset X", "type": "float", "suffix": "px", "value": values["manual_offset_x_px"]},
                {"name": "Offset Y", "type": "float", "suffix": "px", "value": values["manual_offset_y_px"]},
                {"name": "Period X", "type": "float", "suffix": "px", "limits": (0.001, 100000.0),
                 "value": values["manual_period_x_px"]},
                {"name": "Period Y", "type": "float", "suffix": "px", "limits": (0.001, 100000.0),
                 "value": values["manual_period_y_px"]},
                {"name": "Stagger axis", "type": "list", "values": ["None", "X", "Y"],
                 "value": values["stagger_axis"]},
                {"name": "Stagger", "type": "float", "value": values["stagger"], "step": 0.05,
                 "tip": "Signed fraction of the period along the stagger axis."},
                {"name": "Localization settings…", "type": "action"},
            ]},
            {"name": "Orientation", "type": "group", "children": [
                {"name": "Mode", "type": "list", "values": [ORIENTATION_AUTO, ORIENTATION_MANUAL],
                 "value": values["orientation_mode"]},
                {"name": "Orientation", "type": "list", "values": list(ORIENTATIONS),
                 "value": values["orientation"]},
            ]},
            {"name": "Spot model", "type": "group", "children": [
                {"name": "Mode", "type": "list", "values": [SPOT_MANUAL, SPOT_AUTO],
                 "value": values["spot_width_mode"]},
                {"name": "FWHM", "type": "float", "limits": (0.001, 100000.0),
                 "value": values["spot_fwhm"]},
                {"name": "FWHM unit", "type": "list", "values": ["nm", "px"],
                 "value": values["spot_fwhm_unit"]},
                {"name": "Background", "type": "list", "values": ["constant", "none", "auto"],
                 "value": values["background"]},
            ]},
            {"name": "Corrections", "type": "group", "children": [
                {"name": "Intensity correction", "type": "bool", "value": values["intensity_correction"],
                 "tip": "Corrects smooth frame-to-frame intensity variations, for example from bleaching, "
                        "illumination drift, or laser-power fluctuations."},
                {"name": "Cell offsets", "type": "bool", "value": values["cell_offsets"]},
                {"name": "Reassignment", "type": "list", "values": ["off", "auto", "fixed"],
                 "value": values["reassignment"]},
                {"name": "Shift factor", "type": "float", "limits": (0.0, 1.0), "step": 0.05,
                 "value": values["shift_factor"]},
            ]},
            {"name": "Placement", "type": "group", "children": [
                {"name": "Mode", "type": "list", "values": [PLACEMENT_AUTO, PLACEMENT_DIRECT, PLACEMENT_INTERPOLATE],
                 "value": values["placement_mode"]},
                {"name": "Direct tolerance", "type": "float", "suffix": " px", "limits": (0.0, 1.0),
                 "step": 0.01, "value": values["commensurate_tolerance"],
                 "tip": "Maximum off-grid residual accepted for direct placement; exposed temporarily for validation."},
                {"name": "Variance weighting", "type": "bool", "value": values["variance_weighting"],
                 "tip": "Use extraction variance when duplicate samples land on one direct-placement pixel."},
            ]},
            {"name": "Outputs", "type": "group", "children": [
                {"name": "Sharpened image", "type": "bool", "value": values["sharpen"]},
                {"name": "Sharpening blur", "type": "float", "suffix": "px", "limits": (0.1, 10.0),
                 "value": values["sharpen_sigma_px"]},
                {"name": "Sharpening regularization", "type": "float", "limits": (0.001, 10.0),
                 "value": values["sharpen_regularization"]},
                {"name": "Pinhole stack", "type": "bool", "value": values["pinhole_stack"]},
            ]},
            {"name": "Scan overrides", "type": "group", "children": [
                {"name": "Steps, fast axis", "type": "int", "limits": (0, 10000), "value": values["scan_steps_fast"],
                 "tip": "0: use the recording"},
                {"name": "Steps, slow axis", "type": "int", "limits": (0, 10000), "value": values["scan_steps_slow"],
                 "tip": "0: use the recording"},
                {"name": "Step", "type": "float", "suffix": "nm", "limits": (0.0, 100000.0),
                 "value": values["scan_step_nm"], "tip": "0: use the recording"},
            ]},
        ]

        self.p = Parameter.create(name="params", type="group", children=params)
        self.tree = ParameterTree(showHeader=False)
        self.tree.setParameters(self.p, showTop=False)

        self.previewCheckbox = QtWidgets.QCheckBox("Show pattern")
        self.previewCheckbox.setToolTip(
            "Overlay the localization currently used by reconstruction. In Manual mode, editing the grid moves this overlay."
        )
        self.previewCheckbox.toggled.connect(self.sigPreviewToggled)
        self.previewStatusLabel = QtWidgets.QLabel("")
        self.previewStatusLabel.setWordWrap(True)
        self.previewStatusLabel.setStyleSheet("color: palette(mid); font-size: 9pt; padding: 0px 4px;")
        self._outputPixelSizeLabel = QtWidgets.QLabel("Output pixel size: — (set after reconstruction)")
        self._outputPixelSizeLabel.setStyleSheet("color: palette(mid); font-size: 9pt; padding: 2px 4px;")

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.tree)
        layout.addWidget(self.previewCheckbox)
        layout.addWidget(self.previewStatusLabel)
        layout.addWidget(self._outputPixelSizeLabel)
        layout.setContentsMargins(0, 0, 0, 0)

        self.p.param("Preset").sigValueChanged.connect(self._preset_changed)
        self.p.param("Localization").param("Localize").sigActivated.connect(
            self.sigLocalizeRequested.emit
        )
        self.p.param("Localization").param("Localization settings…").sigActivated.connect(
            self._open_localization_settings
        )
        for key, parameter in self._preset_controlled_parameters().items():
            parameter.sigValueChanged.connect(lambda _p, _v, key=key: self._controlled_changed(key))

        for parameter in self._localization_preview_parameters():
            parameter.sigValueChanged.connect(lambda *_: self.sigDetectionParamsChanged.emit())

        self.p.param("Localization").param("Mode").sigValueChanged.connect(
            self._localization_mode_changed
        )
        self.p.param("Orientation").param("Mode").sigValueChanged.connect(lambda *_: self._update_enabled())
        self.p.param("Spot model").param("Mode").sigValueChanged.connect(lambda *_: self._update_enabled())
        self.p.param("Spot model").param("FWHM unit").sigValueChanged.connect(self._spot_unit_changed)
        self.p.param("Corrections").param("Reassignment").sigValueChanged.connect(lambda *_: self._update_enabled())
        self.p.param("Outputs").param("Sharpened image").sigValueChanged.connect(lambda *_: self._update_enabled())
        self._update_enabled()

    def _preset_controlled_parameters(self):
        return {
            "device": self.p.param("CPU/GPU"),
            "intensity_correction": self.p.param("Corrections").param("Intensity correction"),
            "cell_offsets": self.p.param("Corrections").param("Cell offsets"),
            "reassignment": self.p.param("Corrections").param("Reassignment"),
            "placement_mode": self.p.param("Placement").param("Mode"),
            "variance_weighting": self.p.param("Placement").param("Variance weighting"),
            "sharpen": self.p.param("Outputs").param("Sharpened image"),
        }

    def _localization_preview_parameters(self):
        group = self.p.param("Localization")
        return [
            group.param("Offset X"), group.param("Offset Y"),
            group.param("Period X"), group.param("Period Y"),
            group.param("Stagger axis"), group.param("Stagger"),
        ]

    def _preset_changed(self, _parameter, value):
        if self._applying_preset or value == PRESET_CUSTOM:
            return
        values = apply_preset(self.get_values(), str(value))
        self._applying_preset = True
        try:
            for key, parameter in self._preset_controlled_parameters().items():
                parameter.setValue(values[key])
            self._localization_refinement = str(values["localization_refinement"])
        finally:
            self._applying_preset = False
        self._update_enabled()

    def _controlled_changed(self, _key):
        if self._applying_preset:
            return
        preset = self.p.param("Preset")
        if preset.value() != PRESET_CUSTOM:
            self._applying_preset = True
            try:
                preset.setValue(PRESET_CUSTOM)
            finally:
                self._applying_preset = False
        self._update_enabled()

    def _spot_unit_changed(self, _parameter, value):
        """Change display units without changing the represented physical width."""
        new_unit = str(value)
        old_unit = self._spot_unit
        if new_unit == old_unit:
            return
        spot = self.p.param("Spot model")
        width = float(spot.param("FWHM").value())
        pixel_nm = float(self.p.param("Camera pixel").value())
        if pixel_nm > 0:
            if old_unit == "nm" and new_unit == "px":
                width = width / pixel_nm
            elif old_unit == "px" and new_unit == "nm":
                width = width * pixel_nm
            spot.param("FWHM").setValue(width)
        self._spot_unit = new_unit

    def _update_enabled(self):
        localization = self.p.param("Localization")
        manual = localization.param("Mode").value() == LOCALIZATION_MANUAL
        localization.param("Localize").setOpts(enabled=not manual)
        localization.param("Relocalize each data item").setOpts(enabled=not manual)
        for name in ("Offset X", "Offset Y", "Period X", "Period Y", "Stagger axis", "Stagger"):
            localization.param(name).setOpts(enabled=manual)
        localization.param("Localization settings…").setOpts(enabled=not manual)

        orientation = self.p.param("Orientation")
        orientation.param("Orientation").setOpts(
            enabled=orientation.param("Mode").value() == ORIENTATION_MANUAL
        )

        spot = self.p.param("Spot model")
        spot_manual = spot.param("Mode").value() == SPOT_MANUAL
        spot.param("FWHM").setOpts(enabled=spot_manual)
        spot.param("FWHM unit").setOpts(enabled=spot_manual)

        corrections = self.p.param("Corrections")
        corrections.param("Shift factor").setOpts(
            enabled=corrections.param("Reassignment").value() == "fixed"
        )
        outputs = self.p.param("Outputs")
        sharpen = bool(outputs.param("Sharpened image").value())
        outputs.param("Sharpening blur").setOpts(enabled=sharpen)
        outputs.param("Sharpening regularization").setOpts(enabled=sharpen)

    def _open_localization_settings(self):
        dialog = LocalizationSettingsDialog(self._localization_settings, self)
        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            self._localization_settings = dialog.settings()
            self.clear_auto_localization(
                "Localization settings changed — press Localize to update"
            )

    def _localization_mode_changed(self, _parameter, value):
        previous = self._localization_mode
        current = str(value)
        self._localization_mode = current
        if previous == LOCALIZATION_AUTO and current == LOCALIZATION_MANUAL:
            self._seed_manual_from_auto()
        self._update_enabled()
        self.sigDetectionParamsChanged.emit()

    def _seed_manual_from_auto(self) -> None:
        from .calibration import PatternLocalization

        localization = PatternLocalization.from_dict(self._auto_localization)
        if localization is None:
            return
        values = localization.manual_grid_parameters()
        if values is None:
            self.setPreviewStatus(
                "The Auto localization is rotated/general and cannot be represented "
                "exactly by the Manual period/offset/stagger controls."
            )
            return
        group = self.p.param("Localization")
        group.param("Offset X").setValue(values["offset_x"])
        group.param("Offset Y").setValue(values["offset_y"])
        group.param("Period X").setValue(values["period_x"])
        group.param("Period Y").setValue(values["period_y"])
        group.param("Stagger axis").setValue(values["stagger_axis"])
        group.param("Stagger").setValue(values["stagger"])

    def set_auto_localization(self, localization, status: str = "") -> None:
        """Install an explicitly resolved Auto localization without locking it."""
        payload = localization.to_dict() if hasattr(localization, "to_dict") else dict(localization)
        provenance = dict(payload.get("provenance") or {})
        if status:
            provenance["status"] = str(status)
        payload["provenance"] = provenance
        self._auto_localization = payload
        self.setPreviewStatus(status or "Auto localization ready")
        self.sigDetectionParamsChanged.emit()

    def clear_auto_localization(self, status: str = "Auto: press Localize") -> None:
        self._auto_localization = {}
        self.setPreviewStatus(status)
        self.sigDetectionParamsChanged.emit()

    def get_values(self) -> dict:
        loc = self.p.param("Localization")
        orient = self.p.param("Orientation")
        spot = self.p.param("Spot model")
        corrections = self.p.param("Corrections")
        placement = self.p.param("Placement")
        outputs = self.p.param("Outputs")
        scan = self.p.param("Scan overrides")
        return {
            "preset": str(self.p.param("Preset").value()),
            "device": str(self.p.param("CPU/GPU").value()),
            "pixel_size_nm": float(self.p.param("Camera pixel").value()),
            "localization_mode": str(loc.param("Mode").value()),
            "auto_localization": deepcopy(self._auto_localization),
            "relocalize_each_data_item": bool(loc.param("Relocalize each data item").value()),
            "localization_refinement": str(self._localization_refinement),
            "manual_offset_x_px": float(loc.param("Offset X").value()),
            "manual_offset_y_px": float(loc.param("Offset Y").value()),
            "manual_period_x_px": float(loc.param("Period X").value()),
            "manual_period_y_px": float(loc.param("Period Y").value()),
            "stagger_axis": str(loc.param("Stagger axis").value()),
            "stagger": float(loc.param("Stagger").value()),
            "localization_settings": deepcopy(self._localization_settings.to_dict()),
            "orientation_mode": str(orient.param("Mode").value()),
            "orientation": str(orient.param("Orientation").value()),
            "spot_width_mode": str(spot.param("Mode").value()),
            "spot_fwhm": float(spot.param("FWHM").value()),
            "spot_fwhm_unit": str(spot.param("FWHM unit").value()),
            "background": str(spot.param("Background").value()),
            "intensity_correction": bool(corrections.param("Intensity correction").value()),
            "cell_offsets": bool(corrections.param("Cell offsets").value()),
            "reassignment": str(corrections.param("Reassignment").value()),
            "shift_factor": float(corrections.param("Shift factor").value()),
            "placement_mode": str(placement.param("Mode").value()),
            "commensurate_tolerance": float(placement.param("Direct tolerance").value()),
            "variance_weighting": bool(placement.param("Variance weighting").value()),
            "sharpen": bool(outputs.param("Sharpened image").value()),
            "sharpen_sigma_px": float(outputs.param("Sharpening blur").value()),
            "sharpen_regularization": float(outputs.param("Sharpening regularization").value()),
            "pinhole_stack": bool(outputs.param("Pinhole stack").value()),
            "scan_steps_fast": int(scan.param("Steps, fast axis").value()),
            "scan_steps_slow": int(scan.param("Steps, slow axis").value()),
            "scan_step_nm": float(scan.param("Step").value()),
        }

    def set_pattern_params(self, row_offset: float, col_offset: float, row_period: float, col_period: float):
        """Compatibility helper: put the old row/column grid into Manual mode."""
        loc = self.p.param("Localization")
        loc.param("Mode").setValue(LOCALIZATION_MANUAL)
        loc.param("Offset X").setValue(float(col_offset))
        loc.param("Offset Y").setValue(float(row_offset))
        loc.param("Period X").setValue(float(col_period))
        loc.param("Period Y").setValue(float(row_period))
        loc.param("Stagger axis").setValue("None")
        loc.param("Stagger").setValue(0.0)

    def setPreviewStatus(self, text: str) -> None:
        self.previewStatusLabel.setText(str(text))

    def load_from_attrs(self, attrs: dict, data_obj=None) -> None:
        """Seed detector-dependent defaults when recordings carry them."""
        attrs = dict(attrs or {})
        if self._auto_localization and data_obj is not None:
            source = str(
                (self._auto_localization.get("provenance") or {}).get("dataset", "")
            )
            current = str(getattr(data_obj, "name", "") or "")
            if source and current and source != current:
                self.clear_auto_localization("Auto: press Localize")
        pixel_nm = None
        for key in ("monalisa:camera_pixel_nm", "camera_pixel_nm"):
            try:
                value = float(attrs.get(key, 0))
            except (TypeError, ValueError):
                continue
            if value > 0:
                pixel_nm = value
                break
        if pixel_nm is None:
            try:
                physical_um = float(attrs.get("ome:PhysicalSizeX", 0))
            except (TypeError, ValueError):
                physical_um = 0.0
            if physical_um > 0:
                pixel_nm = 1000.0 * physical_um
        if pixel_nm is not None:
            self.p.param("Camera pixel").setValue(pixel_nm)

        for key in ("monalisa:spot_fwhm_nm", "monalisaSpotFwhmNm"):
            try:
                fwhm = float(attrs.get(key, 0))
            except (TypeError, ValueError):
                continue
            if fwhm > 0:
                spot = self.p.param("Spot model")
                spot.param("FWHM unit").setValue("nm")
                spot.param("FWHM").setValue(fwhm)
                break

    def setOutputPixelSize(self, output_pixel_size_nm) -> None:
        label = self._outputPixelSizeLabel
        if output_pixel_size_nm is None:
            label.setText("Output pixel size: — (set after reconstruction)")
            return
        import numpy as np
        sizes = np.atleast_1d(np.asarray(output_pixel_size_nm, dtype=float))
        if sizes.size == 0 or not np.all(sizes > 0):
            label.setText("Output pixel size: — (set after reconstruction)")
        elif sizes.size > 1 and abs(sizes[0] - sizes[-1]) >= 0.01:
            label.setText(f"Output pixel size: Y {sizes[0]:.3g} / X {sizes[-1]:.3g} nm/px")
        else:
            label.setText(f"Output pixel size: {sizes[0]:.3g} nm/px")


__all__ = ["LocalizationSettingsDialog", "MonalisaParamsWidget"]
