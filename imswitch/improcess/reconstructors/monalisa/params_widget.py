"""Fixed parameter tree for the unified Gaussian MoNaLISA reconstructor."""

from __future__ import annotations

from copy import deepcopy
import json

from pyqtgraph.parametertree import Parameter, ParameterTree
from qtpy import QtCore, QtWidgets

from .calibration import PatternLocalization, ScanOrientation
from .localization_settings import DEFAULT_LOCALIZATION_SETTINGS, LocalizationSettings, PeriodPrior
from .params import (
    LOCALIZATION_AUTO,
    LOCALIZATION_LOCKED,
    LOCALIZATION_MANUAL,
    LOCALIZATION_REFINEMENT_FAST,
    ORIENTATION_AUTO,
    ORIENTATION_LOCKED,
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
from .session_registry import MonalisaSessionRegistry, detector_identity
from .spot_settings import sigma_px_to_fwhm


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
    sigLocalizationModeChanged = QtCore.Signal(str)

    def __init__(self, parent=None, *, session_registry: MonalisaSessionRegistry | None = None):
        super().__init__(parent)
        values = default_params()
        self._registry = session_registry
        self._detector_id = "unknown"
        self._current_data_obj = None
        self._selected_locked_localization_id = ""
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
                {"name": "Mode", "type": "list",
                 "values": [LOCALIZATION_AUTO, LOCALIZATION_LOCKED, LOCALIZATION_MANUAL],
                 "value": values["localization_mode"]},
                {"name": "Locked localization", "type": "list", "values": {"—": ""}, "value": "",
                 "tip": "Explicit session localization for the current detector."},
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
                {"name": "Lock current…", "type": "action"},
                {"name": "Rename locked…", "type": "action"},
                {"name": "Delete locked", "type": "action"},
                # Keep the explicit Auto action at the bottom beside its settings.
                {"name": "Localization settings…", "type": "action"},
                {"name": "Localize", "type": "action"},
            ]},
            {"name": "Orientation", "type": "group", "children": [
                {"name": "Mode", "type": "list",
                 "values": [ORIENTATION_AUTO, ORIENTATION_LOCKED, ORIENTATION_MANUAL],
                 "value": values["orientation_mode"]},
                {"name": "Orientation", "type": "list", "values": list(ORIENTATIONS),
                 "value": values["orientation"]},
                {"name": "Locked value", "type": "str", "value": "—", "readonly": True},
                {"name": "Lock current", "type": "action"},
                {"name": "Clear locked", "type": "action"},
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
            ]},
            {"name": "ISM", "type": "group", "children": [
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
        localization = self.p.param("Localization")
        localization.param("Localize").sigActivated.connect(self.sigLocalizeRequested.emit)
        localization.param("Localization settings…").sigActivated.connect(self._open_localization_settings)
        localization.param("Lock current…").sigActivated.connect(self._lock_current_localization)
        localization.param("Rename locked…").sigActivated.connect(self._rename_locked_localization)
        localization.param("Delete locked").sigActivated.connect(self._delete_locked_localization)
        localization.param("Locked localization").sigValueChanged.connect(self._locked_localization_changed)
        orientation = self.p.param("Orientation")
        orientation.param("Lock current").sigActivated.connect(self._lock_current_orientation)
        orientation.param("Clear locked").sigActivated.connect(self._clear_locked_orientation)

        for key, parameter in self._preset_controlled_parameters().items():
            parameter.sigValueChanged.connect(lambda _p, _v, key=key: self._controlled_changed(key))
        for parameter in self._localization_preview_parameters():
            parameter.sigValueChanged.connect(lambda *_: self.sigDetectionParamsChanged.emit())

        localization.param("Mode").sigValueChanged.connect(self._localization_mode_changed)
        orientation.param("Mode").sigValueChanged.connect(self._orientation_mode_changed)
        self.p.param("Spot model").param("Mode").sigValueChanged.connect(lambda *_: self._update_enabled())
        self.p.param("Spot model").param("FWHM unit").sigValueChanged.connect(self._spot_unit_changed)
        self.p.param("ISM").param("Reassignment").sigValueChanged.connect(lambda *_: self._update_enabled())
        self.p.param("Outputs").param("Sharpened image").sigValueChanged.connect(lambda *_: self._update_enabled())

        if self._registry is not None:
            self._registry.add_listener(self._registry_changed)
            self.destroyed.connect(lambda *_: self._registry.remove_listener(self._registry_changed))
        self._refresh_registry_controls(clear_selection=True)
        self._update_enabled()
        QtCore.QTimer.singleShot(0, self._offer_recovery_if_needed)

    def _preset_controlled_parameters(self):
        return {
            "device": self.p.param("CPU/GPU"),
            "intensity_correction": self.p.param("Corrections").param("Intensity correction"),
            "cell_offsets": self.p.param("Corrections").param("Cell offsets"),
            "spot_width_mode": self.p.param("Spot model").param("Mode"),
            "reassignment": self.p.param("ISM").param("Reassignment"),
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
            group.param("Locked localization"),
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
        mode = str(localization.param("Mode").value())
        manual = mode == LOCALIZATION_MANUAL
        locked = mode == LOCALIZATION_LOCKED
        localization.param("Localize").setOpts(enabled=mode == LOCALIZATION_AUTO)
        localization.param("Locked localization").setOpts(enabled=locked)
        for name in ("Offset X", "Offset Y", "Period X", "Period Y", "Stagger axis", "Stagger"):
            localization.param(name).setOpts(enabled=manual)
        localization.param("Localization settings…").setOpts(enabled=mode == LOCALIZATION_AUTO)
        has_lockable = self._lockable_localization() is not None
        localization.param("Lock current…").setOpts(enabled=has_lockable and mode != LOCALIZATION_LOCKED)
        selected = bool(self._selected_locked_localization_id)
        localization.param("Rename locked…").setOpts(enabled=selected)
        localization.param("Delete locked").setOpts(enabled=selected)

        orientation = self.p.param("Orientation")
        orient_mode = str(orientation.param("Mode").value())
        orientation.param("Orientation").setOpts(enabled=orient_mode == ORIENTATION_MANUAL)
        orientation.param("Lock current").setOpts(enabled=self._lockable_orientation() is not None)
        orientation.param("Clear locked").setOpts(
            enabled=self._registry is not None and self._registry.locked_orientation(self._detector_id) is not None
        )

        spot = self.p.param("Spot model")
        spot_manual = spot.param("Mode").value() == SPOT_MANUAL
        spot.param("FWHM").setOpts(enabled=spot_manual)
        spot.param("FWHM unit").setOpts(enabled=spot_manual)

        ism = self.p.param("ISM")
        ism.param("Shift factor").setOpts(
            enabled=ism.param("Reassignment").value() == "fixed"
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
        self.sigLocalizationModeChanged.emit(current)
        self.sigDetectionParamsChanged.emit()

    def _orientation_mode_changed(self, *_):
        self._update_enabled()

    def _seed_manual_from_auto(self) -> None:
        localization = PatternLocalization.from_dict(self._auto_localization)
        if localization is None and self._registry is not None:
            transient = self._registry.current_localization(self._detector_id)
            localization = transient.localization if transient is not None else None
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
        payload = localization.to_dict() if hasattr(localization, "to_dict") else dict(localization)
        provenance = dict(payload.get("provenance") or {})
        if status:
            provenance["status"] = str(status)
        payload["provenance"] = provenance
        self._auto_localization = payload
        if self._registry is not None:
            self._registry.set_current_localization(
                self._detector_id,
                PatternLocalization.from_dict(payload),
                source=str(provenance.get("source") or "auto"),
                source_dataset=str(provenance.get("dataset") or getattr(self._current_data_obj, "name", "") or ""),
                source_path=str(getattr(self._current_data_obj, "dataPath", "") or ""),
                diagnostics={"status": status} if status else {},
            )
        self.setPreviewStatus(status or "Auto localization ready")
        self._update_enabled()
        self.sigDetectionParamsChanged.emit()

    def clear_auto_localization(self, status: str = "Auto: press Localize") -> None:
        self._auto_localization = {}
        self.setPreviewStatus(status)
        self._update_enabled()
        self.sigDetectionParamsChanged.emit()

    # ----------------------------------------------------- session registry UI
    def _registry_changed(self):
        self._refresh_registry_controls()

    def sync_current_transient(self):
        """Pull Auto values resolved by a completed worker for this dataset."""
        if self._registry is None:
            return
        transient = self._registry.current_localization(self._detector_id)
        current_name = str(getattr(self._current_data_obj, "name", "") or "")
        if (
            transient is not None
            and str(self.p.param("Localization").param("Mode").value()) == LOCALIZATION_AUTO
            and (not transient.source_dataset or transient.source_dataset == current_name)
        ):
            payload = transient.localization.to_dict()
            provenance = dict(payload.get("provenance") or {})
            provenance.setdefault("status", "Auto localization resolved during reconstruction")
            payload["provenance"] = provenance
            self._auto_localization = payload
            self.setPreviewStatus(str(provenance["status"]))
        self._refresh_registry_controls()
        self.sigDetectionParamsChanged.emit()

    def set_auto_spot_sigma_px(self, sigma_px: float) -> None:
        """Show an Auto-estimated width without changing Spot model mode."""
        if str(self.p.param("Spot model").param("Mode").value()) != SPOT_AUTO:
            return
        try:
            sigma_px = float(sigma_px)
        except (TypeError, ValueError):
            return
        if sigma_px <= 0:
            return
        spot = self.p.param("Spot model")
        unit = str(spot.param("FWHM unit").value())
        pixel_nm = float(self.p.param("Camera pixel").value())
        try:
            value = sigma_px_to_fwhm(
                sigma_px, unit, pixel_nm if unit == "nm" else None
            )
        except ValueError:
            return
        spot.param("FWHM").setValue(float(value))

    def sync_reconstruction_results(self, results) -> None:
        """Reflect an Auto-estimated spot width from the current reconstruction.

        This updates only the displayed FWHM value.  The mode remains Auto;
        switching Spot model to Manual/default then freezes that resolved value.
        """
        if str(self.p.param("Spot model").param("Mode").value()) != SPOT_AUTO:
            return
        results = tuple(results or ())
        if not results:
            return
        current_name = str(getattr(self._current_data_obj, "name", "") or "")
        selected = None
        for result in results:
            if current_name and str(getattr(result, "name", "")).startswith(f"{current_name}_monalisa"):
                selected = result
                break
        if selected is None and len(results) == 1:
            selected = results[0]
        if selected is None:
            return
        metadata = dict(getattr(selected, "metadata", {}) or {})
        snapshot = metadata.get("monalisa_calibration")
        if not isinstance(snapshot, dict):
            params = dict(getattr(selected, "params", {}) or {})
            snapshot = params.get("calibration_snapshot")
        if not isinstance(snapshot, dict):
            return
        try:
            sigma_px = float(snapshot.get("effective_sigma_px"))
        except (TypeError, ValueError):
            sigma_px = 0.0
        if sigma_px > 0:
            self.set_auto_spot_sigma_px(sigma_px)
            return
        spot = self.p.param("Spot model")
        unit = str(spot.param("FWHM unit").value())
        key = "resolved_spot_fwhm_nm" if unit == "nm" else "resolved_spot_fwhm_px"
        try:
            value = float(snapshot.get(key))
        except (TypeError, ValueError):
            return
        if value > 0:
            spot.param("FWHM").setValue(value)

    def _refresh_registry_controls(self, *, clear_selection: bool = False):
        localization = self.p.param("Localization")
        current = "" if clear_selection else str(self._selected_locked_localization_id or "")
        choices = {"— choose locked localization —": ""}
        if self._registry is not None:
            for profile in self._registry.localization_profiles(self._detector_id):
                label = profile.name
                if label in choices:
                    label = f"{profile.name} ({profile.id[:6]})"
                choices[label] = profile.id
        selector = localization.param("Locked localization")
        selector.setLimits(choices)
        if current and self._registry is not None:
            profile = self._registry.localization_profile(current)
            if profile is None or profile.detector != self._detector_id:
                current = ""
        self._selected_locked_localization_id = current
        selector.setValue(current)

        orientation = self.p.param("Orientation")
        lock = self._registry.locked_orientation(self._detector_id) if self._registry else None
        orientation.param("Locked value").setValue(
            lock.orientation.to_literal() if lock is not None else "—"
        )
        self._update_enabled()

    def _locked_localization_changed(self, _parameter, value):
        self._selected_locked_localization_id = str(value or "")
        self._update_enabled()
        self.sigDetectionParamsChanged.emit()

    def _manual_localization(self) -> PatternLocalization:
        group = self.p.param("Localization")
        axis = str(group.param("Stagger axis").value())
        return PatternLocalization.manual_grid(
            period_x=float(group.param("Period X").value()),
            period_y=float(group.param("Period Y").value()),
            offset_x=float(group.param("Offset X").value()),
            offset_y=float(group.param("Offset Y").value()),
            stagger_axis=None if axis.lower() == "none" else axis.lower(),
            stagger=float(group.param("Stagger").value()),
            source="manual",
            dataset=str(getattr(self._current_data_obj, "name", "") or ""),
        )

    def _lockable_localization(self):
        mode = str(self.p.param("Localization").param("Mode").value())
        if mode == LOCALIZATION_MANUAL:
            return self._manual_localization(), "manual"
        if mode == LOCALIZATION_AUTO:
            localization = PatternLocalization.from_dict(self._auto_localization)
            source = "auto"
            if localization is None and self._registry is not None:
                transient = self._registry.current_localization(self._detector_id)
                if transient is not None:
                    localization, source = transient.localization, transient.source
            if localization is not None:
                return localization, source
        return None

    def _lock_current_localization(self):
        if self._registry is None:
            self.setPreviewStatus("Session localization registry is unavailable")
            return
        lockable = self._lockable_localization()
        if lockable is None:
            self.setPreviewStatus("Localize or reconstruct a good dataset before locking")
            return
        localization, source = lockable
        profiles = self._registry.localization_profiles(self._detector_id)
        default_name = f"Localization {len(profiles) + 1}"
        name, ok = QtWidgets.QInputDialog.getText(
            self, "Lock localization", "Name", text=default_name
        )
        if not ok:
            return
        profile = self._registry.lock_localization(
            name=str(name), detector=self._detector_id, localization=localization,
            source=source,
            source_dataset=str(getattr(self._current_data_obj, "name", "") or ""),
            source_path=str(getattr(self._current_data_obj, "dataPath", "") or ""),
            diagnostics=dict(localization.provenance),
        )
        self._selected_locked_localization_id = profile.id
        self._refresh_registry_controls()
        self.setPreviewStatus(f"Locked localization as {profile.name}")

    def _rename_locked_localization(self):
        if self._registry is None or not self._selected_locked_localization_id:
            return
        profile = self._registry.localization_profile(self._selected_locked_localization_id)
        if profile is None:
            return
        name, ok = QtWidgets.QInputDialog.getText(
            self, "Rename locked localization", "Name", text=profile.name
        )
        if ok:
            self._registry.rename_localization(profile.id, str(name))

    def _delete_locked_localization(self):
        if self._registry is None or not self._selected_locked_localization_id:
            return
        profile = self._registry.localization_profile(self._selected_locked_localization_id)
        if profile is None:
            return
        answer = QtWidgets.QMessageBox.question(
            self, "Delete locked localization?",
            f"Delete '{profile.name}' from this session?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if answer == QtWidgets.QMessageBox.Yes:
            self._registry.delete_localization(profile.id)
            self._selected_locked_localization_id = ""
            self._refresh_registry_controls(clear_selection=True)

    def _lockable_orientation(self):
        mode = str(self.p.param("Orientation").param("Mode").value())
        if mode == ORIENTATION_MANUAL:
            return ScanOrientation.from_literal(
                str(self.p.param("Orientation").param("Orientation").value())
            ), "manual"
        if mode == ORIENTATION_AUTO and self._registry is not None:
            transient = self._registry.current_orientation(self._detector_id)
            if transient is not None:
                return transient.orientation, transient.source
        return None

    def _lock_current_orientation(self):
        if self._registry is None:
            return
        lockable = self._lockable_orientation()
        if lockable is None:
            self.setPreviewStatus("Reconstruct once to resolve Auto orientation before locking it")
            return
        orientation, source = lockable
        self._registry.lock_orientation(
            self._detector_id, orientation, source=source,
            source_dataset=str(getattr(self._current_data_obj, "name", "") or ""),
            source_path=str(getattr(self._current_data_obj, "dataPath", "") or ""),
        )
        self.setPreviewStatus(f"Locked orientation {orientation.to_literal()} for {self._detector_id}")

    def _clear_locked_orientation(self):
        if self._registry is None:
            return
        self._registry.clear_orientation(self._detector_id)

    def _offer_recovery_if_needed(self):
        if self._registry is None or self._registry.recovery_prompted:
            return
        self._registry.mark_recovery_prompted()
        candidates = self._registry.recovery_candidates()
        if not candidates:
            return
        candidate = candidates[0]
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Restore MoNaLISA session calibration?")
        box.setText(
            "An unfinished MoNaLISA calibration session was found "
            f"({candidate.localization_count} localization profile(s), "
            f"{candidate.orientation_count} orientation lock(s))."
        )
        box.setInformativeText("Restore it into this session, or discard the recovery journal.")
        restore = box.addButton("Restore", QtWidgets.QMessageBox.AcceptRole)
        discard = box.addButton("Discard", QtWidgets.QMessageBox.DestructiveRole)
        box.addButton(QtWidgets.QMessageBox.Cancel)
        box.exec_()
        if box.clickedButton() is restore:
            self._registry.restore(candidate)
            self.setPreviewStatus("Recovered MoNaLISA session calibration")
        elif box.clickedButton() is discard:
            self._registry.discard(candidate)

    # --------------------------------------------------------------- values
    def get_values(self) -> dict:
        loc = self.p.param("Localization")
        orient = self.p.param("Orientation")
        spot = self.p.param("Spot model")
        corrections = self.p.param("Corrections")
        ism = self.p.param("ISM")
        placement = self.p.param("Placement")
        outputs = self.p.param("Outputs")
        scan = self.p.param("Scan overrides")

        locked_profile = None
        if self._registry is not None and self._selected_locked_localization_id:
            locked_profile = self._registry.localization_profile(self._selected_locked_localization_id)
        locked_orientation = self._registry.locked_orientation(self._detector_id) if self._registry else None
        return {
            "preset": str(self.p.param("Preset").value()),
            "device": str(self.p.param("CPU/GPU").value()),
            "pixel_size_nm": float(self.p.param("Camera pixel").value()),
            "localization_mode": str(loc.param("Mode").value()),
            "auto_localization": deepcopy(self._auto_localization),
            "locked_localization_id": locked_profile.id if locked_profile else "",
            "locked_localization_name": locked_profile.name if locked_profile else "",
            "locked_localization": locked_profile.localization.to_dict() if locked_profile else {},
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
            "locked_orientation": locked_orientation.orientation.to_literal() if locked_orientation else "",
            "spot_width_mode": str(spot.param("Mode").value()),
            "spot_fwhm": float(spot.param("FWHM").value()),
            "spot_fwhm_unit": str(spot.param("FWHM unit").value()),
            "background": str(spot.param("Background").value()),
            "intensity_correction": bool(corrections.param("Intensity correction").value()),
            "cell_offsets": bool(corrections.param("Cell offsets").value()),
            "reassignment": str(ism.param("Reassignment").value()),
            "shift_factor": float(ism.param("Shift factor").value()),
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
        attrs = dict(attrs or {})
        old_detector = self._detector_id
        self._current_data_obj = data_obj
        self._detector_id = detector_identity(data_obj, attrs)
        if old_detector != self._detector_id:
            self._selected_locked_localization_id = ""
            self._refresh_registry_controls(clear_selection=True)

        if self._auto_localization and data_obj is not None:
            source = str((self._auto_localization.get("provenance") or {}).get("dataset", ""))
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

        # A saved reconstruction can carry the exact calibration values used.
        # Treat those as transient values belonging to this exact data object:
        # never guess a matching profile and never promote them to Locked.
        embedded = attrs.get("monalisa_calibration")
        if isinstance(embedded, str):
            try:
                embedded = json.loads(embedded)
            except (TypeError, ValueError, json.JSONDecodeError):
                embedded = None
        if isinstance(embedded, dict) and "per_data_item" not in embedded:
            try:
                resolved = PatternLocalization.from_dict(embedded.get("resolved_localization"))
            except (KeyError, TypeError, ValueError):
                resolved = None
            if resolved is not None:
                payload = resolved.to_dict()
                provenance = dict(payload.get("provenance") or {})
                provenance.update({
                    "source": "embedded",
                    "dataset": str(getattr(data_obj, "name", "") or ""),
                    "status": "Embedded localization loaded from this data",
                })
                payload["provenance"] = provenance
                self._auto_localization = payload
                if self._registry is not None:
                    self._registry.set_current_localization(
                        self._detector_id, PatternLocalization.from_dict(payload),
                        source="embedded",
                        source_dataset=str(getattr(data_obj, "name", "") or ""),
                        source_path=str(getattr(data_obj, "dataPath", "") or ""),
                        diagnostics={"status": provenance["status"]},
                    )
                self.setPreviewStatus(provenance["status"])
            resolved_orientation = str(embedded.get("resolved_orientation") or "")
            if resolved_orientation and self._registry is not None:
                try:
                    self._registry.set_current_orientation(
                        self._detector_id, resolved_orientation, source="embedded",
                        source_dataset=str(getattr(data_obj, "name", "") or ""),
                        source_path=str(getattr(data_obj, "dataPath", "") or ""),
                    )
                except ValueError:
                    pass

        spot = self.p.param("Spot model")
        embedded_fwhm_nm = None
        embedded_fwhm_px = None
        if isinstance(embedded, dict) and "per_data_item" not in embedded:
            try:
                embedded_fwhm_nm = float(embedded.get("resolved_spot_fwhm_nm"))
            except (TypeError, ValueError):
                embedded_fwhm_nm = None
            try:
                embedded_fwhm_px = float(embedded.get("resolved_spot_fwhm_px"))
            except (TypeError, ValueError):
                embedded_fwhm_px = None
        if embedded_fwhm_nm is not None and embedded_fwhm_nm > 0:
            spot.param("FWHM unit").setValue("nm")
            spot.param("FWHM").setValue(embedded_fwhm_nm)
        elif embedded_fwhm_px is not None and embedded_fwhm_px > 0:
            spot.param("FWHM unit").setValue("px")
            spot.param("FWHM").setValue(embedded_fwhm_px)
        else:
            for key in ("monalisa:spot_fwhm_nm", "monalisaSpotFwhmNm"):
                try:
                    fwhm = float(attrs.get(key, 0))
                except (TypeError, ValueError):
                    continue
                if fwhm > 0:
                    spot.param("FWHM unit").setValue("nm")
                    spot.param("FWHM").setValue(fwhm)
                    break
        self._refresh_registry_controls()

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
