"""Compact workflow UI for the unified Gaussian MoNaLISA reconstructor."""

from __future__ import annotations

from copy import deepcopy
import json
import re

from qtpy import QtCore, QtWidgets

from .collapsible_section import CompactSection
from .advanced_settings import AdvancedReconstructionDialog
from .calibration import PatternLocalization, ScanOrientation
from .localization_settings import DEFAULT_LOCALIZATION_SETTINGS, LocalizationSettings, PeriodPrior
from .params import (
    LOCALIZATION_AUTO,
    LOCALIZATION_LOCKED,
    LOCALIZATION_MANUAL,
    LOCALIZATION_REFINEMENT_FAST,
    ORIENTATION_AUTO,
    ORIENTATION_SETUP,
    ORIENTATION_RECORDED,
    ORIENTATION_LOCKED,
    ORIENTATION_MANUAL,
    PRESET_FAST,
    PRESET_FULL,
    PRESET_DLL,
    PRESET_LEGACY,
    FULL_OPTION_DEFAULTS,
    SPOT_AUTO,
    SPOT_MANUAL,
    default_params,
)
from .scan_frame import ORIENTATIONS
from .scanning_settings import ScanningAdvancedDialog
from .session_registry import MonalisaSessionRegistry, detector_identity
from .spot_settings import sigma_px_to_fwhm
from .footprint_suggestion import suggest_local_footprint_size
from .footprint_config import footprint_from_params, migrate_footprint_params
from .setup_defaults import (
    DetectorSetupDefaults, FWHM_KEY, ORIENTATION_KEY, current_setup_path,
    read_detector_defaults, save_detector_default, save_detector_fitting, positive_fwhm,
    startup_detector_id,
)


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


class _WidthEstimateSignals(QtCore.QObject):
    finished = QtCore.Signal(object)


class _WidthEstimateTask(QtCore.QRunnable):
    """One-off background calibration; only the GUI accepts the resulting value."""

    def __init__(self, data_obj, params, serial):
        super().__init__()
        self.signals = _WidthEstimateSignals()
        self.data_obj = data_obj
        self.params = deepcopy(params)
        self.serial = serial

    def run(self):
        try:
            from .reconstructor import MonalisaReconstructor
            sigma, status, localization = MonalisaReconstructor.estimate_spot_fwhm(
                self.data_obj, self.params
            )
            result = (self.serial, self.data_obj, sigma, status, localization, "")
        except Exception as exc:
            result = (self.serial, self.data_obj, None, "", None, str(exc))
        self.signals.finished.emit(result)


class _ISMEstimateSignals(QtCore.QObject):
    finished = QtCore.Signal(object)


class _ISMEstimateTask(QtCore.QRunnable):
    """Read-only raw-frame measurement on a background worker."""

    def __init__(self, data_obj, params, serial):
        super().__init__()
        self.signals = _ISMEstimateSignals()
        self.data_obj = data_obj
        self.params = deepcopy(params)
        self.serial = serial

    def run(self):
        try:
            from .reconstructor import MonalisaReconstructor
            estimate, status, localization = MonalisaReconstructor.estimate_ism_shift(
                self.data_obj, self.params
            )
            result = (self.serial, self.data_obj, estimate, status, localization, "")
        except Exception as exc:
            result = (self.serial, self.data_obj, None, "", None, str(exc))
        self.signals.finished.emit(result)


class MonalisaParamsWidget(QtWidgets.QWidget):
    """Compact workflow-oriented controls for unified Gaussian MoNaLISA.

    The widget intentionally does not mirror the parameter dictionary one row
    at a time.  Normal reconstruction decisions stay visible; implementation
    details remain in small advanced dialogs.  ``get_values`` is still the one
    adapter to the stable backend/headless parameter contract.
    """

    sigPreviewToggled = QtCore.Signal(bool)
    sigDetectionParamsChanged = QtCore.Signal()
    sigLocalizeRequested = QtCore.Signal()
    sigLocalizationModeChanged = QtCore.Signal(str)
    sigModeChanged = QtCore.Signal(str)

    _MODE_LABELS = (
        ("Normal", PRESET_FAST),
        ("Full", PRESET_FULL),
        ("DLL-equivalent", PRESET_DLL),
    )
    _ISM_LABELS = (("Off", "off"), ("Auto", "auto"), ("Manual", "fixed"))

    def __init__(self, parent=None, *, session_registry: MonalisaSessionRegistry | None = None):
        super().__init__(parent)
        values = default_params()
        self._registry = session_registry
        # A dataset is not needed to identify the active setup's sole
        # configured MoNaLISA detector. Refuse ambiguous multi-camera setups.
        self._detector_id = startup_detector_id()
        self._setup_defaults = read_detector_defaults(self._detector_id)
        if self._setup_defaults.fwhm_nm is not None:
            values["spot_fwhm"] = self._setup_defaults.fwhm_nm
            values["spot_width_source"] = "Setup default"
        self._current_data_obj = None
        self._selected_locked_localization_id = ""
        self._localization_settings = LocalizationSettings.from_dict(values["localization_settings"])
        self._localization_refinement = str(values["localization_refinement"])
        self._localization_mode = str(values["localization_mode"])
        self._auto_localization = deepcopy(values["auto_localization"])
        self._localization_busy = False
        self._spot_unit = str(values["spot_fwhm_unit"])
        self._recorded_orientation = None
        self._orientation_user_override = False
        self._loading_setup_orientation = False
        self._fwhm_source = str(values["spot_width_source"])
        self._programmatic_fwhm = False
        self._footprint_user_override = False
        self._fwhm_estimate_serial = 0
        self._fwhm_estimate_task = None
        self._fwhm_estimate_busy = False
        self._fwhm_data_revision = 0
        self._fwhm_estimate_revision = None
        self._fwhm_estimate_signature = None
        self._ism_estimate_serial = 0
        self._ism_estimate_task = None
        self._ism_estimate_busy = False
        self._ism_estimate_revision = None
        self._ism_estimate_signature = None
        self._applying_preset = False
        self._last_gaussian_mode = PRESET_FAST
        self._recorded_scan_summary = ""
        self._recorded_scan_axes: dict[str, dict[str, float | int | None]] = {}
        self._scan_result_summary: dict[str, object] = {}
        self._scanned_lateral_axes = {"x", "y"}
        self._scan_overrides = {
            "scan_steps_fast": int(values["scan_steps_fast"]),
            "scan_steps_slow": int(values["scan_steps_slow"]),
            "scan_step_nm": float(values["scan_step_nm"]),
        }
        self._advanced_values = {
            "device": str(values["device"]),
            "pixel_size_nm": float(values["pixel_size_nm"]),
            "placement_mode": str(values["placement_mode"]),
            "commensurate_tolerance": float(values["commensurate_tolerance"]),
            "variance_weighting": bool(values["variance_weighting"]),
            "sharpen_sigma_px": float(values["sharpen_sigma_px"]),
            "sharpen_regularization": float(values["sharpen_regularization"]),
            "estimator": str(values["estimator"]),
            "footprint_shape": str(values["footprint_shape"]),
            "footprint_mode": str(values["footprint_mode"]),
            "footprint_size_px": float(values["footprint_size_px"]),
            "footprint_sigma_factor": float(values["footprint_sigma_factor"]),
            "local_sampling": str(values["local_sampling"]),
            "background": str(values["background"]),
        }

        self._build_ui(values)
        self._connect_signals()
        if self._registry is not None:
            self._registry.add_listener(self._registry_changed)
            self.destroyed.connect(lambda *_: self._registry.remove_listener(self._registry_changed))
        self._refresh_registry_controls(clear_selection=True)
        self._update_ui()
        QtCore.QTimer.singleShot(0, self._offer_recovery_if_needed)

    # --------------------------------------------------------------- builders
    def _section(self, title: str, *, expanded: bool = True):
        section = CompactSection(title, expanded=expanded)
        section.expandedChanged.connect(lambda _expanded: self._refresh_scroll_minimum())
        return section, section.grid

    @staticmethod
    def _double_spin(value, minimum=-1e9, maximum=1e9, decimals=3, suffix=""):
        spin = QtWidgets.QDoubleSpinBox()
        spin.setRange(float(minimum), float(maximum))
        spin.setDecimals(int(decimals))
        spin.setValue(float(value))
        if suffix:
            spin.setSuffix(str(suffix))
        return spin

    @staticmethod
    def _combo(items, value=None):
        combo = QtWidgets.QComboBox()
        for item in items:
            if isinstance(item, tuple):
                combo.addItem(str(item[0]), item[1])
            else:
                combo.addItem(str(item), item)
        if value is not None:
            MonalisaParamsWidget._set_combo_data(combo, value)
        return combo

    @staticmethod
    def _set_combo_data(combo, value):
        for index in range(combo.count()):
            if combo.itemData(index) == value or combo.itemText(index) == str(value):
                combo.setCurrentIndex(index)
                return True
        return False

    @staticmethod
    def _combo_data(combo):
        value = combo.currentData()
        return combo.currentText() if value is None else value

    @staticmethod
    def _set_combo_item_enabled(combo, value, enabled: bool, tooltip: str = "") -> None:
        """Enable/disable one semantic combo-box choice without removing it."""
        for index in range(combo.count()):
            if combo.itemData(index) == value or combo.itemText(index) == str(value):
                model = combo.model()
                item = model.item(index) if hasattr(model, "item") else None
                if item is not None:
                    item.setEnabled(bool(enabled))
                    item.setToolTip(str(tooltip or ""))
                return

    def _compact_section(self, title: str, *, expanded: bool):
        section, grid = self._section(title, expanded=expanded)
        return section, grid, section.header, section.details

    def _build_ui(self, values: dict) -> None:
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.scrollArea = QtWidgets.QScrollArea(self)
        self.scrollArea.setWidgetResizable(True)
        self.scrollArea.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.scrollArea.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.scrollArea.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        outer.addWidget(self.scrollArea)

        self._scrollContent = QtWidgets.QWidget()
        self.scrollArea.setWidget(self._scrollContent)
        root = QtWidgets.QVBoxLayout(self._scrollContent)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(6)
        self._contentLayout = root

        modeRow = QtWidgets.QHBoxLayout()
        modeRow.addWidget(QtWidgets.QLabel("Mode"))
        self.modeCombo = self._combo(self._MODE_LABELS, values["preset"])
        self.modeCombo.setToolTip(
            "Normal and Full share localization, FWHM, fitting and corrections. "
            "Full additionally enables ISM (Auto by default), pinhole stacks "
            "and optional sharpening. Returning to Normal remembers Full settings. "
            "DLL-equivalent uses the recovered CPU unit-cell extraction model and "
            "the common scan/calibration pipeline, but disables ISM, sharpening "
            "and variance weighting. For the original DLL use the separate "
            "Legacy DLL reconstructor."
        )
        modeRow.addWidget(self.modeCombo, 1)
        self.advancedButton = QtWidgets.QPushButton("Expert…")
        self.advancedButton.setToolTip(
            "Open existing advanced parameters, including Gaussian estimator and footprint."
        )
        modeRow.addWidget(self.advancedButton)
        root.addLayout(modeRow)

        # Localization -------------------------------------------------------
        self.localizationBox, loc, self.localizationHeader, self.localizationDetailsWidget = (
            self._compact_section(
                "Localization", expanded=values["localization_mode"] != LOCALIZATION_LOCKED
            )
        )
        self.localizationToggle = self.localizationHeader.toggle
        self.localizationModeCombo = self._combo(
            [LOCALIZATION_AUTO, LOCALIZATION_LOCKED, LOCALIZATION_MANUAL],
            values["localization_mode"],
        )
        loc.addWidget(QtWidgets.QLabel("Source"), 0, 0)
        loc.addWidget(self.localizationModeCombo, 0, 1)

        self.lockedLocalizationLabel = QtWidgets.QLabel("Profile")
        self.lockedLocalizationCombo = QtWidgets.QComboBox()
        self.renameLocalizationButton = QtWidgets.QPushButton("Rename…")
        self.deleteLocalizationButton = QtWidgets.QPushButton("Delete")
        loc.addWidget(self.lockedLocalizationLabel, 0, 2)
        loc.addWidget(self.lockedLocalizationCombo, 0, 3)
        loc.addWidget(self.renameLocalizationButton, 0, 4)
        loc.addWidget(self.deleteLocalizationButton, 0, 5)

        self.manualLocalizationWidget = QtWidgets.QWidget()
        manual = QtWidgets.QGridLayout(self.manualLocalizationWidget)
        manual.setContentsMargins(0, 0, 0, 0)
        manual.setHorizontalSpacing(6)
        manual.setVerticalSpacing(3)
        self.periodXSpin = self._double_spin(values["manual_period_x_px"], 0.001, 100000, 3, " px")
        self.periodYSpin = self._double_spin(values["manual_period_y_px"], 0.001, 100000, 3, " px")
        self.offsetXSpin = self._double_spin(values["manual_offset_x_px"], -100000, 100000, 3, " px")
        self.offsetYSpin = self._double_spin(values["manual_offset_y_px"], -100000, 100000, 3, " px")
        self.staggerAxisCombo = self._combo(["None", "X", "Y"], values["stagger_axis"])
        self.staggerSpin = self._double_spin(values["stagger"], -10.0, 10.0, 3)
        self.staggerSpin.setSingleStep(0.05)
        manual.addWidget(QtWidgets.QLabel("Period"), 0, 0)
        manual.addWidget(QtWidgets.QLabel("X"), 0, 1)
        manual.addWidget(self.periodXSpin, 0, 2)
        manual.addWidget(QtWidgets.QLabel("Y"), 0, 3)
        manual.addWidget(self.periodYSpin, 0, 4)
        manual.addWidget(QtWidgets.QLabel("Offset"), 1, 0)
        manual.addWidget(QtWidgets.QLabel("X"), 1, 1)
        manual.addWidget(self.offsetXSpin, 1, 2)
        manual.addWidget(QtWidgets.QLabel("Y"), 1, 3)
        manual.addWidget(self.offsetYSpin, 1, 4)
        manual.addWidget(QtWidgets.QLabel("Stagger"), 2, 0)
        manual.addWidget(self.staggerAxisCombo, 2, 1, 1, 2)
        manual.addWidget(self.staggerSpin, 2, 3, 1, 2)
        loc.addWidget(self.manualLocalizationWidget, 1, 0, 1, 6)

        actionRow = QtWidgets.QHBoxLayout()
        actionRow.setSpacing(8)
        self.previewCheckbox = QtWidgets.QCheckBox("Show")
        self.previewCheckbox.setChecked(True)
        self.previewCheckbox.setToolTip(
            "Overlay the localization currently used by reconstruction."
        )
        self.localizeButton = QtWidgets.QPushButton("Localize")
        self.localizeButton.setToolTip(
            "Run automatic localization on the current dataset."
        )
        self.lockLocalizationButton = QtWidgets.QPushButton("Lock current")
        self.lockLocalizationButton.setToolTip(
            "Save the currently resolved localization in this session and use it immediately."
        )
        self.localizationSettingsButton = QtWidgets.QPushButton("Settings…")
        self.localizationSettingsButton.setToolTip(
            "Configure automatic localization parameters."
        )
        actionRow.addWidget(self.previewCheckbox)
        actionRow.addWidget(self.localizeButton)
        actionRow.addWidget(self.lockLocalizationButton)
        actionRow.addWidget(self.localizationSettingsButton)
        actionRow.addStretch()
        loc.addLayout(actionRow, 2, 0, 1, 6)

        self.previewStatusLabel = QtWidgets.QLabel("Auto: waiting for data")
        self.previewStatusLabel.setWordWrap(True)
        self.previewStatusLabel.setSizePolicy(
            QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Minimum
        )
        self.previewStatusLabel.setMinimumHeight(
            self.previewStatusLabel.fontMetrics().lineSpacing() + 4
        )
        self.previewStatusLabel.setStyleSheet("color: palette(mid);")
        loc.addWidget(self.previewStatusLabel, 3, 0, 1, 6)
        root.addWidget(self.localizationBox)

        # Scanning -----------------------------------------------------------
        self.scanningBox, scan, self.scanningHeader, self.scanningDetailsWidget = (
            self._compact_section("Scanning", expanded=False)
        )
        self.scanningToggle = self.scanningHeader.toggle
        self.orientationModeCombo = self._combo(
            [ORIENTATION_AUTO, ORIENTATION_RECORDED, ORIENTATION_SETUP, ORIENTATION_LOCKED, ORIENTATION_MANUAL],
            values["orientation_mode"],
        )
        self.orientationCombo = self._combo(list(ORIENTATIONS), values["orientation"])
        self.orientationResultLabel = QtWidgets.QLabel("—")
        self.orientationResultLabel.setStyleSheet("color: palette(mid);")
        self.lockOrientationButton = QtWidgets.QPushButton("Lock current")
        self.lockOrientationButton.setToolTip(
            "Save the currently resolved orientation for this detector and use it immediately."
        )
        self.clearOrientationButton = QtWidgets.QPushButton("Clear lock")
        self.saveOrientationDefaultButton = QtWidgets.QPushButton("Save default…")
        self.saveOrientationDefaultButton.setToolTip(
            "Explicitly save the resolved orientation for this detector in the active hardware setup."
        )
        self.scanningAdvancedButton = QtWidgets.QPushButton("Edit…")
        self.scanningAdvancedButton.setToolTip(
            "Review or override scan metadata used only for reconstruction."
        )
        scan.addWidget(QtWidgets.QLabel("Orientation"), 0, 0)
        scan.addWidget(self.orientationModeCombo, 0, 1)
        scan.addWidget(self.orientationCombo, 0, 2)
        scan.addWidget(self.orientationResultLabel, 0, 2)
        scan.addWidget(self.lockOrientationButton, 0, 3)
        scan.addWidget(self.clearOrientationButton, 0, 4)
        scan.addWidget(self.saveOrientationDefaultButton, 0, 5)
        scan.setColumnStretch(6, 1)

        self.scanSummaryLabel = QtWidgets.QLabel("Scan metadata: —")
        self.scanSummaryLabel.setWordWrap(True)
        self.scanSummaryLabel.setTextFormat(QtCore.Qt.RichText)
        scan.addWidget(self.scanSummaryLabel, 1, 0, 1, 5)
        scan.addWidget(self.scanningAdvancedButton, 1, 5)

        self.scanStatusLabel = QtWidgets.QLabel("")
        self.scanStatusLabel.setStyleSheet("color: palette(mid);")
        self.scanStatusLabel.setWordWrap(True)
        self.scanStatusLabel.setSizePolicy(
            QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Minimum
        )
        self.scanStatusLabel.setMinimumHeight(
            self.scanStatusLabel.fontMetrics().lineSpacing() + 4
        )
        scan.addWidget(self.scanStatusLabel, 2, 0, 1, 6)
        root.addWidget(self.scanningBox)

        # Spot width ---------------------------------------------------------
        # Keep the FWHM estimate/manual/setup workflow visible by default.
        self.spotBox, spot = self._section("Spot width", expanded=True)
        self.fwhmLabel = QtWidgets.QLabel("FWHM")
        self.spotModeCombo = self._combo(
            [("Manual", SPOT_MANUAL), ("Auto", SPOT_AUTO)], values["spot_width_mode"]
        )
        self.spotModeCombo.setToolTip(
            "Manual uses the entered FWHM; Auto estimates it during each reconstruction."
        )
        self.fwhmSpin = self._double_spin(values["spot_fwhm"], 0.001, 100000, 3)
        self.fwhmSpin.setMaximumWidth(112)
        self.fwhmUnitCombo = self._combo(["nm", "px"], values["spot_fwhm_unit"])
        self.fwhmUnitCombo.setMaximumWidth(72)
        self.estimateFwhmButton = QtWidgets.QPushButton("Estimate")
        self.estimateFwhmButton.setToolTip(
            "Estimate FWHM from the selected dataset without reconstructing. "
            "The estimate becomes an editable Manual value."
        )
        self.fwhmMenuButton = QtWidgets.QToolButton()
        self.fwhmMenuButton.setText("⋯")
        self.fwhmMenuButton.setToolTip("Restore or save the detector FWHM default")
        self.fwhmMenu = QtWidgets.QMenu(self.fwhmMenuButton)
        self.useEstimatedFwhmButton = self.fwhmMenu.addAction("Use last Auto estimate")
        self.useSetupFwhmButton = self.fwhmMenu.addAction("Restore setup default")
        self.fwhmMenu.addSeparator()
        self.saveFwhmDefaultButton = self.fwhmMenu.addAction("Save as setup default…")
        self.fwhmMenuButton.setMenu(self.fwhmMenu)
        self.fwhmMenuButton.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        # Keep the source for existing calibration logic without consuming a
        # second visible row; it is available from the FWHM menu tooltip.
        self.fwhmSourceLabel = QtWidgets.QLabel("Application default", self.spotBox)
        self.fwhmSourceLabel.hide()
        spot.addWidget(self.fwhmLabel, 0, 0)
        spot.addWidget(self.spotModeCombo, 0, 1)
        spot.addWidget(self.fwhmSpin, 0, 2)
        spot.addWidget(self.fwhmUnitCombo, 0, 3)
        spot.addWidget(self.estimateFwhmButton, 0, 4)
        spot.addWidget(self.fwhmMenuButton, 0, 5)

        # Fitting is an independent top-level section, not nested inside FWHM.
        self._fitting_expanded = False
        self.fittingBox, fitting = self._section("Fitting", expanded=False)
        self.fittingHeader = self.fittingBox.header
        self.fittingToggle = self.fittingBox.toggle
        self.fittingDetailsWidget = self.fittingBox.details
        self.estimatorCombo = self._combo(
            [("Local", "local"), ("Joint", "joint")], values["estimator"]
        )
        self.estimatorCombo.setToolTip(
            "Local fits each spot independently; Joint models neighboring Gaussian signals together."
        )
        self.footprintShapeCombo = self._combo(
            [("Square", "square"), ("Circle", "circle"), ("Lattice cells", "lattice")],
            values["footprint_shape"],
        )
        self.footprintShapeCombo.setToolTip(
            "Footprint defines which camera pixels participate in the fit. "
            "Lattice cells require axis-aligned rectangular, single-motif geometry."
        )
        self.footprintModeCombo = self._combo(
            [("From lattice", "from_lattice"), ("Fixed", "fixed"),
             ("Spot-width relative", "fwhm")], values["footprint_mode"]
        )
        self.footprintModeCombo.setToolTip(
            "From lattice adapts to the localized period; Fixed uses a numeric side/diameter; "
            "Spot-width relative uses an editable fit radius in multiples of spot sigma "
            "(the original Joint Auto support is Circle with radius 2.5σ)."
        )
        self.footprintSizeSpin = self._double_spin(
            values["footprint_size_px"], 0.1, 255.0, 2, " px"
        )
        self.footprintSigmaSpin = self._double_spin(
            values["footprint_sigma_factor"], 0.1, 100.0, 3, " σ"
        )
        self.footprintSizeSpin.setMaximumWidth(110)
        self.footprintSigmaSpin.setMaximumWidth(110)
        self.footprintSizeSpin.setToolTip(
            "Fixed size: square side (odd integer) or circle diameter in camera pixels."
        )
        self.footprintSigmaSpin.setToolTip("Fitting radius in multiples of spot sigma.")
        self.fitCalculatedSizeLabel = QtWidgets.QLabel("Auto size")
        self.fitCalculatedSizeLabel.setStyleSheet("color: palette(mid);")
        self.fitCalculatedSizeLabel.setToolTip("Size will be resolved from the selected localization")
        self.footprintValueStack = QtWidgets.QStackedWidget()
        self.footprintValueStack.addWidget(self.footprintSizeSpin)
        self.footprintValueStack.addWidget(self.footprintSigmaSpin)
        self.footprintValueStack.addWidget(self.fitCalculatedSizeLabel)
        self.footprintValueStack.addWidget(QtWidgets.QWidget())  # lattice cells: no size
        self.fittingMenuButton = QtWidgets.QToolButton()
        self.fittingMenuButton.setText("⋯")
        self.fittingMenuButton.setToolTip("Restore or save detector Gaussian fitting defaults")
        self.fittingMenu = QtWidgets.QMenu(self.fittingMenuButton)
        self.useSetupFittingAction = self.fittingMenu.addAction("Restore setup default")
        self.fittingMenu.addSeparator()
        self.saveFootprintDefaultButton = self.fittingMenu.addAction("Save fitting as setup default…")
        self.fittingMenuButton.setMenu(self.fittingMenu)
        self.fittingMenuButton.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        # Internal status only; the detailed status remains in the Fitting
        # header tooltip rather than occupying a separate row or suffix.
        self.footprintSourceLabel = QtWidgets.QLabel("Application default", self.fittingDetailsWidget)
        self.footprintSourceLabel.hide()
        fitting.addWidget(self.estimatorCombo, 0, 0)
        fitting.addWidget(self.footprintShapeCombo, 0, 1)
        fitting.addWidget(self.footprintModeCombo, 0, 2)
        fitting.addWidget(self.footprintValueStack, 0, 3)
        fitting.addWidget(self.fittingMenuButton, 0, 4)
        fitting.setColumnStretch(0, 1)
        fitting.setColumnStretch(1, 1)
        fitting.setColumnStretch(2, 1)
        spot.setColumnStretch(1, 1)
        spot.setColumnStretch(2, 1)
        spot.setColumnStretch(3, 1)
        root.addWidget(self.spotBox)
        root.addWidget(self.fittingBox)

        # Classic DLL -------------------------------------------------------
        self.legacyBox, legacy = self._section("Legacy DLL", expanded=True)
        self.legacyBleachingCheck = QtWidgets.QCheckBox("Bleaching correction")
        self.legacyBleachingCheck.setChecked(bool(values["legacy_bleaching_correction"]))
        self.legacyBleachingCheck.setToolTip(
            "Normalize each frame to the total intensity of the first frame before "
            "the classic SignalExtractor reconstruction."
        )
        self.legacyBackgroundCombo = self._combo(
            ["Constant", "Gaussian", "No background"], values["legacy_bg_modelling"]
        )
        self.legacyBackgroundCombo.setToolTip(
            "Background basis used by the classic SignalExtractor DLL."
        )
        self.legacyGaussianSizeSpin = self._double_spin(
            values["legacy_bg_gaussian_size_nm"], 0.001, 1e7, 1, " nm"
        )
        self.legacyGaussianSizeSpin.setToolTip(
            "FWHM of the Gaussian background basis. Used only when Background is Gaussian."
        )
        self.legacyDeviceCombo = self._combo(["CPU", "GPU"], values["device"])
        self.legacyDeviceCombo.setToolTip(
            "Select the CPU or GPU implementation inside GPU_acc_recon.dll."
        )
        self.legacyPixelSizeSpin = self._double_spin(
            values["pixel_size_nm"], 0.001, 1e7, 3, " nm"
        )
        self.legacyPixelSizeSpin.setToolTip(
            "Camera pixel size used to convert the PSF FWHM to DLL pixel units."
        )
        legacy.addWidget(self.legacyBleachingCheck, 0, 0, 1, 2)
        legacy.addWidget(QtWidgets.QLabel("Background"), 1, 0)
        legacy.addWidget(self.legacyBackgroundCombo, 1, 1)
        self.legacyGaussianSizeLabel = QtWidgets.QLabel("Gaussian size")
        legacy.addWidget(self.legacyGaussianSizeLabel, 2, 0)
        legacy.addWidget(self.legacyGaussianSizeSpin, 2, 1)
        legacy.addWidget(QtWidgets.QLabel("Device"), 3, 0)
        legacy.addWidget(self.legacyDeviceCombo, 3, 1)
        legacy.addWidget(QtWidgets.QLabel("Camera pixel"), 4, 0)
        legacy.addWidget(self.legacyPixelSizeSpin, 4, 1)
        legacy.setColumnStretch(2, 1)
        root.addWidget(self.legacyBox)

        # ISM ---------------------------------------------------------------
        self.ismBox, ism = self._section("ISM", expanded=True)
        full = values.get("full_options") or FULL_OPTION_DEFAULTS
        self.reassignmentCombo = self._combo(self._ISM_LABELS, full["reassignment"])
        self.shiftFactorSpin = self._double_spin(full["shift_factor"], 0.0, 1.0, 3)
        self.shiftFactorSpin.setSingleStep(0.05)
        self.asymmetricCheck = QtWidgets.QCheckBox("Asymmetric")
        self.asymmetricCheck.setChecked(bool(full.get("reassignment_asymmetric", False)))
        self.pinholeCheck = QtWidgets.QCheckBox("Pinhole stack")
        self.pinholeCheck.setChecked(bool(full["pinhole_stack"]))
        ism.addWidget(QtWidgets.QLabel("Reassignment"), 0, 0)
        ism.addWidget(self.reassignmentCombo, 0, 1)
        ism.addWidget(QtWidgets.QLabel("Shift"), 0, 2)
        ism.addWidget(self.shiftFactorSpin, 0, 3)
        ism.addWidget(self.asymmetricCheck, 1, 0)
        ism.addWidget(self.pinholeCheck, 1, 1)
        self.estimateISMButton = QtWidgets.QPushButton("Estimate")
        self.estimateISMButton.setToolTip(
            "Measure the ISM shift from raw camera frames of the selected dataset, "
            "without Gaussian fitting or reconstruction. Uses the selected "
            "localization and resolved scan orientation; applies the result as Fixed."
        )
        ism.addWidget(self.estimateISMButton, 0, 4)

        self.asymmetricWidget = QtWidgets.QWidget()
        asym = QtWidgets.QHBoxLayout(self.asymmetricWidget)
        asym.setContentsMargins(0, 0, 0, 0)
        self.shiftXLabel = QtWidgets.QLabel("X")
        self.shiftYLabel = QtWidgets.QLabel("Y")
        self.shiftXSpin = self._double_spin(full.get("shift_factor_x", full["shift_factor"]), 0.0, 1.0, 3)
        self.shiftYSpin = self._double_spin(full.get("shift_factor_y", full["shift_factor"]), 0.0, 1.0, 3)
        self.shiftXSpin.setSingleStep(0.05)
        self.shiftYSpin.setSingleStep(0.05)
        asym.addWidget(self.shiftXLabel)
        asym.addWidget(self.shiftXSpin)
        asym.addWidget(self.shiftYLabel)
        asym.addWidget(self.shiftYSpin)
        ism.addWidget(self.asymmetricWidget, 1, 2, 1, 4)
        root.addWidget(self.ismBox)

        # Corrections --------------------------------------------------------
        self.correctionsBox, corrections = self._section("Corrections", expanded=True)
        self.intensityCheck = QtWidgets.QCheckBox("Intensity correction")
        self.intensityCheck.setChecked(bool(values["intensity_correction"]))
        self.intensityCheck.setToolTip(
            "Correct smooth frame-to-frame intensity changes using the already extracted amplitudes."
        )
        self.cellOffsetsCheck = QtWidgets.QCheckBox("Cell offsets")
        self.cellOffsetsCheck.setChecked(bool(values["cell_offsets"]))
        self.cellOffsetsCheck.setToolTip(
            "Estimate and correct cell-to-cell offsets after extraction. This does not rerun Gaussian fitting."
        )
        corrections.addWidget(self.intensityCheck, 0, 0)
        corrections.addWidget(self.cellOffsetsCheck, 0, 1)
        corrections.setColumnStretch(2, 1)
        root.addWidget(self.correctionsBox)

        # Output -------------------------------------------------------------
        self.outputBox, output = self._section("Output", expanded=True)
        self.sharpenCheck = QtWidgets.QCheckBox("Sharpened image")
        self.sharpenCheck.setChecked(bool(full["sharpen"]))
        output.addWidget(self.sharpenCheck, 0, 0)
        output.setColumnStretch(1, 1)
        root.addWidget(self.outputBox)

        self._outputPixelSizeLabel = QtWidgets.QLabel("Output pixel size: — (set after reconstruction)")
        self._outputPixelSizeLabel.setStyleSheet("color: palette(mid);")
        root.addWidget(self._outputPixelSizeLabel)
        root.addStretch(1)
        QtCore.QTimer.singleShot(0, self._refresh_scroll_minimum)

    def _connect_signals(self) -> None:
        self.modeCombo.currentIndexChanged.connect(self._mode_changed)
        self.localizationModeCombo.currentIndexChanged.connect(self._localization_mode_changed)
        self.lockedLocalizationCombo.currentIndexChanged.connect(self._locked_localization_changed)
        self.orientationModeCombo.currentIndexChanged.connect(self._orientation_mode_changed)
        self.spotModeCombo.currentIndexChanged.connect(self._controlled_changed)
        self.reassignmentCombo.currentIndexChanged.connect(self._controlled_changed)
        self.asymmetricCheck.toggled.connect(self._controlled_changed)
        self.intensityCheck.toggled.connect(self._correction_changed)
        self.cellOffsetsCheck.toggled.connect(self._correction_changed)
        self.sharpenCheck.toggled.connect(self._controlled_changed)
        self.pinholeCheck.toggled.connect(self._controlled_changed)
        for spin in (self.shiftFactorSpin, self.shiftXSpin, self.shiftYSpin):
            spin.valueChanged.connect(self._controlled_changed)
        self.legacyBackgroundCombo.currentIndexChanged.connect(lambda *_: self._update_ui())
        self.legacyDeviceCombo.currentIndexChanged.connect(self._legacy_device_changed)
        self.legacyPixelSizeSpin.valueChanged.connect(self._legacy_pixel_size_changed)

        self.previewCheckbox.toggled.connect(self.sigPreviewToggled)
        self.localizeButton.clicked.connect(self.sigLocalizeRequested.emit)
        self.localizationSettingsButton.clicked.connect(self._open_localization_settings)
        self.lockLocalizationButton.clicked.connect(self._lock_current_localization)
        self.renameLocalizationButton.clicked.connect(self._rename_locked_localization)
        self.deleteLocalizationButton.clicked.connect(self._delete_locked_localization)
        self.lockOrientationButton.clicked.connect(self._lock_current_orientation)
        self.clearOrientationButton.clicked.connect(self._clear_locked_orientation)
        self.saveOrientationDefaultButton.clicked.connect(self._save_orientation_default)
        self.estimateFwhmButton.clicked.connect(self._estimate_fwhm)
        self.estimateISMButton.clicked.connect(self._estimate_ism_shift)
        self.useEstimatedFwhmButton.triggered.connect(self._use_estimated_fwhm)
        self.saveFootprintDefaultButton.triggered.connect(self._save_footprint_default)
        self.useSetupFittingAction.triggered.connect(self._restore_fitting_default)
        self.useSetupFwhmButton.triggered.connect(self._use_setup_fwhm)
        self.saveFwhmDefaultButton.triggered.connect(self._save_fwhm_default)
        self.fittingToggle.toggled.connect(self._set_fitting_expanded)
        for control in (self.shiftFactorSpin, self.shiftXSpin, self.shiftYSpin, self.legacyGaussianSizeSpin):
            control.valueChanged.connect(self._update_section_summaries)
        for control in (self.legacyBleachingCheck, self.pinholeCheck):
            control.toggled.connect(self._update_section_summaries)
        self.orientationCombo.currentIndexChanged.connect(self._refresh_orientation_display)
        for combo in (self.estimatorCombo, self.footprintShapeCombo):
            combo.currentIndexChanged.connect(self._fitting_changed)
        self.footprintModeCombo.currentIndexChanged.connect(self._fitting_changed)
        self.footprintSizeSpin.valueChanged.connect(self._fitting_changed)
        self.footprintSigmaSpin.valueChanged.connect(self._fitting_changed)
        self.fwhmSpin.valueChanged.connect(self._fwhm_changed)
        self.scanningAdvancedButton.clicked.connect(self._open_scanning_advanced)
        self.advancedButton.clicked.connect(self._open_advanced_reconstruction)

        for control in (
            self.periodXSpin, self.periodYSpin, self.offsetXSpin, self.offsetYSpin,
            self.staggerSpin,
        ):
            control.valueChanged.connect(lambda *_: self.sigDetectionParamsChanged.emit())
            control.valueChanged.connect(self._update_localization_summary)
        self.staggerAxisCombo.currentIndexChanged.connect(
            lambda *_: self.sigDetectionParamsChanged.emit()
        )
        self.staggerAxisCombo.currentIndexChanged.connect(self._update_localization_summary)
        self.fwhmUnitCombo.currentIndexChanged.connect(self._spot_unit_changed)
        self.asymmetricCheck.toggled.connect(lambda *_: self._update_ui())
        self.reassignmentCombo.currentIndexChanged.connect(lambda *_: self._update_ui())

    # --------------------------------------------------------------- mode/UI
    def _mode_value(self) -> str:
        return str(self._combo_data(self.modeCombo))

    def _set_mode_value(self, value: str) -> None:
        self._set_combo_data(self.modeCombo, value)

    def _mode_changed(self, *_):
        """Preserve the Gaussian profile while entering DLL-equivalent mode."""
        if self._applying_preset:
            return
        selected = self._mode_value()
        if selected in (PRESET_FAST, PRESET_FULL):
            self._last_gaussian_mode = selected
        self._update_ui()
        self.sigModeChanged.emit(str(self._mode_value()))

    def _controlled_changed(self, *_):
        """Edits remain in the selected profile (there is no Custom mode)."""
        if self._applying_preset:
            return
        self._update_ui()
        self.sigModeChanged.emit(str(self._mode_value()))

    def _correction_changed(self, *_):
        # Corrections are shared between Normal and Full.
        self._controlled_changed()

    def _legacy_device_changed(self, *_):
        if self._applying_preset:
            return
        self._advanced_values["device"] = str(self._combo_data(self.legacyDeviceCombo))

    def _legacy_pixel_size_changed(self, value):
        if self._applying_preset:
            return
        self._advanced_values["pixel_size_nm"] = float(value)

    def _update_ui(self) -> None:
        mode = self._mode_value()
        fast = mode == PRESET_FAST
        legacy = mode == PRESET_LEGACY
        dll = mode == PRESET_DLL
        self.advancedButton.setVisible(not legacy)
        self.ismBox.setVisible(not fast and not legacy and not dll)
        self.correctionsBox.setVisible(not legacy)
        self.outputBox.setVisible(not fast and not legacy and not dll)
        self.legacyBox.setVisible(legacy)
        # Spot width is useful for Legacy too; fitting is Gaussian-only.
        self.spotModeCombo.setVisible(not legacy)
        self.fittingBox.setVisible(not legacy and not dll)
        self.advancedButton.setToolTip(
            "DLL-equivalent uses a fixed CPU unit-cell Gaussian/background model; "
            "other expert settings remain stored for Normal/Full."
            if dll else "Advanced reconstruction settings"
        )
        self.fwhmUnitCombo.setEnabled(True)
        gaussian_bg = str(self._combo_data(self.legacyBackgroundCombo)) == "Gaussian"
        self.legacyGaussianSizeLabel.setEnabled(gaussian_bg)
        self.legacyGaussianSizeSpin.setEnabled(gaussian_bg)

        # The classic DLL consumes scan geometry from the recording/legacy scan
        # parameters, not the unified orientation transform. Keep the compact
        # scan summary visible, but do not imply that manual/locked orientation
        # choices affect this engine.
        self.orientationModeCombo.setEnabled(not legacy)
        self.lockOrientationButton.setEnabled((not legacy) and self._lockable_orientation() is not None)
        self.scanningAdvancedButton.setEnabled(not legacy)
        self.scanningAdvancedButton.setToolTip(
            "Legacy DLL uses recorded scan metadata; use the standalone classic "
            "reconstructor for manual legacy scan-parameter recovery." if legacy else
            "Inspect or override scan counts and step size used for reconstruction."
        )

        localization_mode = str(self._combo_data(self.localizationModeCombo))
        manual = localization_mode == LOCALIZATION_MANUAL
        locked = localization_mode == LOCALIZATION_LOCKED
        self.manualLocalizationWidget.setVisible(manual)
        for widget in (
            self.lockedLocalizationLabel, self.lockedLocalizationCombo,
            self.renameLocalizationButton, self.deleteLocalizationButton,
        ):
            widget.setVisible(locked)
        auto = localization_mode == LOCALIZATION_AUTO
        self.localizeButton.setEnabled(auto and not self._localization_busy)
        self.localizationSettingsButton.setEnabled(auto)
        self.lockLocalizationButton.setEnabled(
            self._lockable_localization() is not None
            and not locked
            and not (auto and self._localization_busy)
        )
        selected = bool(self._selected_locked_localization_id)
        self.renameLocalizationButton.setEnabled(selected)
        self.deleteLocalizationButton.setEnabled(selected)

        orient_mode = str(self._combo_data(self.orientationModeCombo))
        manual_orientation = (not legacy) and orient_mode == ORIENTATION_MANUAL
        self.orientationCombo.setVisible(manual_orientation)
        self.orientationCombo.setEnabled(manual_orientation)
        self.orientationResultLabel.setVisible(not manual_orientation)
        self.lockOrientationButton.setEnabled(
            (not legacy) and self._lockable_orientation() is not None
        )
        has_locked_orientation = (
            self._registry is not None
            and self._registry.locked_orientation(self._detector_id) is not None
        )
        self.clearOrientationButton.setVisible(has_locked_orientation and not legacy)
        self.clearOrientationButton.setEnabled(has_locked_orientation and not legacy)

        spot_auto = str(self._combo_data(self.spotModeCombo)) == SPOT_AUTO
        self.fwhmSpin.setEnabled(not spot_auto)
        self.estimateFwhmButton.setEnabled(
            not legacy and self._current_data_obj is not None and not self._fwhm_estimate_busy
        )
        self.estimateFwhmButton.setText(
            "Estimating…" if self._fwhm_estimate_busy else "Estimate"
        )
        self.estimateISMButton.setEnabled(
            not legacy and not dll and self._current_data_obj is not None and not self._ism_estimate_busy
        )
        self.estimateISMButton.setText(
            "Estimating…" if self._ism_estimate_busy else "Estimate"
        )
        has_detector_setup = (
            self._detector_id != "unknown" and current_setup_path() is not None
        )
        self.useEstimatedFwhmButton.setVisible(spot_auto and not legacy)
        self.useEstimatedFwhmButton.setEnabled(
            spot_auto and self._fwhm_source == "Auto estimate"
        )
        self.useSetupFwhmButton.setEnabled(self._setup_defaults.fwhm_nm is not None)
        self.saveFwhmDefaultButton.setEnabled(
            not spot_auto and has_detector_setup and not legacy
        )
        self.saveFootprintDefaultButton.setEnabled(not legacy and has_detector_setup)
        self.useSetupFittingAction.setEnabled(
            not legacy and (self._setup_defaults.footprint_mode is not None
                            or self._setup_defaults.estimator is not None)
        )
        self.saveOrientationDefaultButton.setEnabled(
            not legacy and has_detector_setup and self._lockable_orientation() is not None
        )
        self.fwhmSourceLabel.setText(self._fwhm_source)
        self.fwhmMenuButton.setToolTip(
            f"FWHM source: {self._fwhm_source}. Restore or save the detector FWHM default."
        )
        self._sync_fitting_controls()
        self._update_footprint_summary()
        self._update_fitting_summary()
        self._update_localization_summary()
        self._update_scanning_header_summary()
        self._update_section_summaries()
        # The unit remains editable in Auto: changing units changes only how
        # the resolved value is displayed, not how it was estimated.
        self.fwhmUnitCombo.setEnabled(True)

        reassignment = str(self._combo_data(self.reassignmentCombo))
        can_be_asymmetric = len(self._scanned_lateral_axes) > 1
        asymmetric = (
            bool(self.asymmetricCheck.isChecked())
            and reassignment != "off"
            and can_be_asymmetric
        )
        manual_ism = reassignment == "fixed"
        self.shiftFactorSpin.setEnabled(manual_ism and not asymmetric)
        self.asymmetricCheck.setEnabled(reassignment != "off" and can_be_asymmetric)
        self.asymmetricCheck.setToolTip(
            "Use separate camera X/Y reassignment factors."
            if can_be_asymmetric else
            "Only one lateral scan axis is present; reassignment is applied only along that sampled direction."
        )
        self.asymmetricWidget.setVisible(asymmetric)
        self.shiftXSpin.setEnabled(manual_ism)
        self.shiftYSpin.setEnabled(manual_ism)
        self.shiftXLabel.setEnabled(True)
        self.shiftYLabel.setEnabled(True)
        QtCore.QTimer.singleShot(0, self._refresh_scroll_minimum)

    def _update_localization_summary(self, *_):
        mode = str(self._combo_data(self.localizationModeCombo))
        if mode == LOCALIZATION_LOCKED:
            profile = (
                self._registry.localization_profile(self._selected_locked_localization_id)
                if self._registry is not None and self._selected_locked_localization_id
                else None
            )
            if profile is not None and profile.detector == self._detector_id:
                description = f"Locked · {profile.name}"
            else:
                description = "Locked · Select profile"
        elif mode == LOCALIZATION_MANUAL:
            description = (
                f"Manual · {self.periodXSpin.value():g} × "
                f"{self.periodYSpin.value():g} px"
            )
        elif self._localization_busy:
            description = "Auto · Localizing…"
        elif self.has_auto_localization_for_current_data():
            description = "Auto · Ready"
        else:
            description = "Auto · Pending"
        self.localizationHeader.setSummary(description)
        self.localizationHeader.setStatus("", self.previewStatusLabel.text())

    def _update_scanning_header_summary(self) -> None:
        # Display the *selected* source. Do not advertise a stale orientation
        # from a previously reconstructed dataset when the current one is Auto.
        mode = str(self._combo_data(self.orientationModeCombo))
        legacy = self._mode_value() == PRESET_LEGACY
        if legacy:
            summary = "Legacy DLL · Recorded scan"
        else:
            orientation = None
            if mode == ORIENTATION_MANUAL:
                orientation = str(self._combo_data(self.orientationCombo))
            elif mode == ORIENTATION_SETUP:
                orientation = self._setup_defaults.orientation
            elif mode == ORIENTATION_RECORDED:
                orientation = self._recorded_orientation
            elif mode == ORIENTATION_LOCKED and self._registry is not None:
                item = self._registry.locked_orientation(self._detector_id)
                orientation = item.orientation.to_literal() if item is not None else None
            elif mode == ORIENTATION_AUTO:
                if self._registry is not None:
                    item = self._registry.current_orientation(self._detector_id)
                    current_name = str(getattr(self._current_data_obj, "name", "") or "")
                    if item is not None and (
                        not item.source_dataset or not current_name
                        or item.source_dataset == current_name
                    ):
                        orientation = item.orientation.to_literal()
                if not orientation:
                    orientation = str(self._scan_result_summary.get("orientation") or "") or None
            source = "Setup" if mode == ORIENTATION_SETUP else mode
            summary = f"{orientation} · {source}" if orientation else f"{source} · Pending"
        axes = "".join(
            axis.upper() for axis in "xyz"
            if int((self._recorded_scan_axes.get(axis) or {}).get("count") or 0) > 1
        )
        if axes:
            summary += f" · {axes}"
        self.scanningHeader.setSummary(summary)
        detail = " ".join(part for part in (
            self._recorded_scan_summary,
            self.scanStatusLabel.text(),
        ) if part)
        self.scanningHeader.setStatus("", detail)

    def _update_section_summaries(self, *_):
        """Summaries describe actual settings without changing their values."""
        width_mode = str(self._combo_data(self.spotModeCombo))
        width_unit = str(self._combo_data(self.fwhmUnitCombo))
        width = f"{self.fwhmSpin.value():g} {width_unit}"
        self.spotBox.setSummary(f"{width} · {width_mode}")

        legacy_parts = [str(self._combo_data(self.legacyBackgroundCombo)) + " background"]
        if self.legacyBleachingCheck.isChecked():
            legacy_parts.append("Bleaching")
        self.legacyBox.setSummary(" · ".join(legacy_parts))

        reassignment = str(self._combo_data(self.reassignmentCombo))
        if reassignment == "fixed":
            if self.asymmetricCheck.isChecked() and len(self._scanned_lateral_axes) > 1:
                reassignment = (
                    f"Manual · X {self.shiftXSpin.value():g}"
                    f" / Y {self.shiftYSpin.value():g}"
                )
            else:
                reassignment = f"Manual · {self.shiftFactorSpin.value():g}"
        elif reassignment == "auto":
            reassignment = "Auto"
        else:
            reassignment = "Off"
        if self.pinholeCheck.isChecked():
            reassignment += " · Pinhole stack"
        self.ismBox.setSummary(reassignment)

        corrections = []
        if self.intensityCheck.isChecked():
            corrections.append("Intensity")
        if self.cellOffsetsCheck.isChecked():
            corrections.append("Cell offsets")
        self.correctionsBox.setSummary(" · ".join(corrections) if corrections else "Off")
        self.outputBox.setSummary("Sharpening On" if self.sharpenCheck.isChecked() else "Sharpening Off")

    def _refresh_scroll_minimum(self) -> None:
        """Keep the full workflow reachable instead of vertically compressing labels."""
        if not hasattr(self, "_scrollContent") or not hasattr(self, "_contentLayout"):
            return
        self._scrollContent.setMinimumHeight(self._contentLayout.minimumSize().height())

    # -------------------------------------------------------------- dialogs
    def _open_localization_settings(self):
        dialog = LocalizationSettingsDialog(self._localization_settings, self)
        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            self._localization_settings = dialog.settings()
            self.clear_auto_localization(
                "Localization settings changed — localizing…"
            )
            if str(self._combo_data(self.localizationModeCombo)) == LOCALIZATION_AUTO:
                self.sigLocalizeRequested.emit()

    def _open_scanning_advanced(self):
        dialog = ScanningAdvancedDialog(
            self._scan_overrides,
            recorded_summary=self._recorded_scan_summary,
            parent=self,
        )
        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            self._scan_overrides = dialog.values()

    def _open_advanced_reconstruction(self):
        before = dict(self._advanced_values)
        localization = self._lockable_localization()
        if localization is not None:
            localization = localization[0]
        elif str(self._combo_data(self.localizationModeCombo)) == LOCALIZATION_LOCKED:
            profile = (self._registry.localization_profile(self._selected_locked_localization_id)
                       if self._registry is not None and self._selected_locked_localization_id else None)
            localization = profile.localization if profile is not None else None
        dialog = AdvancedReconstructionDialog(
            self._advanced_values, self, localization=localization,
            dll_equivalent=self._mode_value() == PRESET_DLL,
        )
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return
        self._advanced_values.update(dialog.values())
        self._update_ui()
        controlled = ("device", "placement_mode", "variance_weighting")
        if any(self._advanced_values[key] != before[key] for key in controlled):
            self._controlled_changed()

    # --------------------------------------------------------- localization
    def _localization_mode_changed(self, *_):
        previous = self._localization_mode
        current = str(self._combo_data(self.localizationModeCombo))
        if (
            current == LOCALIZATION_LOCKED
            and not self._selected_locked_localization_id
            and self._registry is not None
        ):
            profiles = self._registry.localization_profiles(self._detector_id)
            if profiles:
                self._selected_locked_localization_id = profiles[0].id
                old = self.lockedLocalizationCombo.blockSignals(True)
                try:
                    self._set_combo_data(
                        self.lockedLocalizationCombo, self._selected_locked_localization_id
                    )
                finally:
                    self.lockedLocalizationCombo.blockSignals(old)
        self._localization_mode = current
        if previous == LOCALIZATION_AUTO and current == LOCALIZATION_MANUAL:
            self._seed_manual_from_auto()
        self._update_ui()
        self.sigLocalizationModeChanged.emit(current)
        self.sigDetectionParamsChanged.emit()

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
        self.offsetXSpin.setValue(values["offset_x"])
        self.offsetYSpin.setValue(values["offset_y"])
        self.periodXSpin.setValue(values["period_x"])
        self.periodYSpin.setValue(values["period_y"])
        self._set_combo_data(self.staggerAxisCombo, values["stagger_axis"])
        self.staggerSpin.setValue(values["stagger"])

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
        self._update_ui()
        self.sigDetectionParamsChanged.emit()

    def clear_auto_localization(self, status: str = "Auto localization pending…") -> None:
        self._auto_localization = {}
        self.setPreviewStatus(status)
        self._update_ui()
        self.sigDetectionParamsChanged.emit()

    def _manual_localization(self) -> PatternLocalization:
        axis = str(self._combo_data(self.staggerAxisCombo))
        return PatternLocalization.manual_grid(
            period_x=float(self.periodXSpin.value()),
            period_y=float(self.periodYSpin.value()),
            offset_x=float(self.offsetXSpin.value()),
            offset_y=float(self.offsetYSpin.value()),
            stagger_axis=None if axis.lower() == "none" else axis.lower(),
            stagger=float(self.staggerSpin.value()),
            source="manual",
            dataset=str(getattr(self._current_data_obj, "name", "") or ""),
        )

    def _lockable_localization(self):
        mode = str(self._combo_data(self.localizationModeCombo))
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
        self._set_combo_data(self.localizationModeCombo, LOCALIZATION_LOCKED)
        self.setPreviewStatus(f"Locked localization as {profile.name}")
        self.localizationToggle.setChecked(False)

    def _locked_localization_changed(self, *_):
        self._selected_locked_localization_id = str(self._combo_data(self.lockedLocalizationCombo) or "")
        self._update_ui()
        self.sigDetectionParamsChanged.emit()

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

    # ----------------------------------------------------------- orientation
    def _orientation_mode_changed(self, *_):
        if not self._loading_setup_orientation:
            self._orientation_user_override = True
        self._refresh_orientation_display()
        self._update_ui()

    def _lockable_orientation(self):
        mode = str(self._combo_data(self.orientationModeCombo))
        if mode == ORIENTATION_RECORDED and self._recorded_orientation:
            return ScanOrientation.from_literal(self._recorded_orientation), "recorded"
        if mode == ORIENTATION_SETUP and self._setup_defaults.orientation:
            return ScanOrientation.from_literal(self._setup_defaults.orientation), "setup"
        if mode == ORIENTATION_MANUAL:
            return ScanOrientation.from_literal(str(self._combo_data(self.orientationCombo))), "manual"
        if mode == ORIENTATION_LOCKED and self._registry is not None:
            item = self._registry.locked_orientation(self._detector_id)
            return (item.orientation, "locked") if item is not None else None
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
        self._set_combo_data(self.orientationModeCombo, ORIENTATION_LOCKED)
        self.setPreviewStatus(f"Locked orientation {orientation.to_literal()} for {self._detector_id}")

    def _clear_locked_orientation(self):
        if self._registry is not None:
            self._registry.clear_orientation(self._detector_id)

    def _refresh_orientation_display(self) -> None:
        mode = str(self._combo_data(self.orientationModeCombo))
        value = None
        if mode == ORIENTATION_MANUAL:
            value = str(self._combo_data(self.orientationCombo))
        elif mode == ORIENTATION_SETUP:
            value = self._setup_defaults.orientation
        elif mode == ORIENTATION_RECORDED:
            value = self._recorded_orientation
        elif self._registry is not None:
            if mode == ORIENTATION_LOCKED:
                item = self._registry.locked_orientation(self._detector_id)
                value = item.orientation.to_literal() if item is not None else None
            elif mode == ORIENTATION_AUTO:
                item = self._registry.current_orientation(self._detector_id)
                value = item.orientation.to_literal() if item is not None else None
        if not value:
            value = str(self._scan_result_summary.get("orientation") or "") or None
        if value:
            old = self.orientationCombo.blockSignals(True)
            try:
                self._set_combo_data(self.orientationCombo, value)
            finally:
                self.orientationCombo.blockSignals(old)
        self.orientationResultLabel.setText(value if value else "—")
        self._update_scan_summary()
        self._update_scanning_header_summary()

    @staticmethod
    def _format_nm(value: float | None) -> str:
        if value is None:
            return "—"
        value = float(value)
        return f"{value:.1f}" if abs(value) >= 1.0 else f"{value:.3g}"

    def _effective_scan_step_nm(self, axis: str, orientation: str | None) -> float | None:
        summary = self._scan_result_summary
        if axis == "z":
            value = summary.get("plane_step_nm")
            return None if value is None else float(value)
        pixel = summary.get("output_pixel_nm")
        if not isinstance(pixel, tuple) or len(pixel) != 2 or not orientation:
            return None
        if orientation not in ORIENTATIONS:
            return None
        camera_axis = orientation[1] if axis == "x" else orientation[3]
        y_nm, x_nm = pixel
        value = x_nm if camera_axis == "x" else y_nm
        return None if value is None else float(value)

    def _update_scan_summary(self) -> None:
        axes = self._recorded_scan_axes
        if not axes:
            self.scanSummaryLabel.setText("Scan metadata: —")
            self.scanSummaryLabel.setToolTip(self._recorded_scan_summary)
            self.scanStatusLabel.clear()
            self.scanStatusLabel.setVisible(False)
            self._update_scanning_header_summary()
            return

        orientation = str(self._scan_result_summary.get("orientation") or "") or None
        if orientation is None:
            mode = str(self._combo_data(self.orientationModeCombo))
            if mode == ORIENTATION_MANUAL:
                orientation = str(self._combo_data(self.orientationCombo))
            elif mode == ORIENTATION_SETUP:
                orientation = self._setup_defaults.orientation
            elif mode == ORIENTATION_RECORDED:
                orientation = self._recorded_orientation
            elif self._registry is not None:
                item = (
                    self._registry.locked_orientation(self._detector_id)
                    if mode == ORIENTATION_LOCKED
                    else self._registry.current_orientation(self._detector_id)
                )
                orientation = item.orientation.to_literal() if item is not None else None

        lines = []
        deltas = []
        for axis in "xyz":
            info = axes.get(axis)
            if not info or int(info.get("count") or 0) <= 1:
                continue
            count = int(info["count"])
            recorded = info.get("step_nm")
            recorded = None if recorded is None else float(recorded)
            effective = self._effective_scan_step_nm(axis, orientation)
            if recorded is not None and effective is not None and recorded > 0:
                delta = 100.0 * (effective - recorded) / recorded
                if abs(delta) >= 0.05:
                    step = (
                        f"{self._format_nm(recorded)} → {self._format_nm(effective)} nm"
                    )
                    deltas.append((axis.upper(), delta))
                else:
                    step = f"{self._format_nm(recorded)} nm"
            elif recorded is not None:
                step = f"{self._format_nm(recorded)} nm"
            elif effective is not None:
                step = f"{self._format_nm(effective)} nm"
            else:
                step = "step —"
            lines.append(
                f"<b>{axis.upper()} scan</b>&nbsp;&nbsp;{count} pos.&nbsp;&nbsp;{step}"
            )

        self.scanSummaryLabel.setText("<br>".join(lines) if lines else "Scan metadata: —")
        self.scanSummaryLabel.setToolTip(self._recorded_scan_summary)

        summary = self._scan_result_summary
        holes = summary.get("coverage_holes")
        overlaps = summary.get("coverage_overlaps")
        total = summary.get("coverage_cell_pixels")
        status = []
        if holes is not None and overlaps is not None and total is not None:
            acquired = 1
            for axis in "xy":
                acquired *= max(1, int((axes.get(axis) or {}).get("count") or 1))
            exact = int(holes) == 0 and int(overlaps) == 0
            if exact:
                status.append(f"✓ {acquired}/{int(total)}")
            else:
                status.append(f"⚠ {max(0, int(total) - int(holes))}/{int(total)} covered")
            status.append(f"{int(holes)} holes")
            if int(overlaps) or not exact:
                status.append(f"{int(overlaps)} overlaps")
        if deltas:
            if len(deltas) == 1:
                status.append(f"adjusted {deltas[0][1]:+.1f}%")
            else:
                rounded = {round(delta, 1) for _axis, delta in deltas}
                if len(rounded) == 1:
                    status.append(f"adjusted {deltas[0][1]:+.1f}%")
                else:
                    status.append(
                        "adjusted " + ", ".join(
                            f"{axis} {delta:+.1f}%" for axis, delta in deltas
                        )
                    )
        self.scanStatusLabel.setText(" • ".join(status))
        self.scanStatusLabel.setVisible(bool(status))
        tooltip = str(summary.get("diagnostic_tooltip") or "")
        self.scanStatusLabel.setToolTip(tooltip)
        self._update_scanning_header_summary()
        QtCore.QTimer.singleShot(0, self._refresh_scroll_minimum)

    # ----------------------------------------------------------- spot / ISM
    def _sync_fitting_controls(self) -> None:
        """Reflect setup and fitting changes without recursively updating values."""
        for widget, value in (
            (self.estimatorCombo, self._advanced_values["estimator"]),
            (self.footprintShapeCombo, self._advanced_values["footprint_shape"]),
            (self.footprintModeCombo, self._advanced_values["footprint_mode"]),
        ):
            blocked = widget.blockSignals(True)
            try:
                self._set_combo_data(widget, value)
            finally:
                widget.blockSignals(blocked)
        for widget, value in (
            (self.footprintSizeSpin, self._advanced_values["footprint_size_px"]),
            (self.footprintSigmaSpin, self._advanced_values["footprint_sigma_factor"]),
        ):
            blocked = widget.blockSignals(True)
            try:
                widget.setValue(float(value))
            finally:
                widget.blockSignals(blocked)
        shape = str(self._advanced_values["footprint_shape"])
        mode = str(self._advanced_values["footprint_mode"])
        if shape == "square":
            self.footprintSizeSpin.setDecimals(0)
            self.footprintSizeSpin.setSingleStep(2.0)
        else:
            self.footprintSizeSpin.setDecimals(2)
            self.footprintSizeSpin.setSingleStep(1.0)
        active = shape != "lattice"
        self.footprintModeCombo.setEnabled(active)
        self.footprintValueStack.setCurrentIndex(
            3 if not active else
            0 if mode == "fixed" else 1 if mode == "fwhm" else 2
        )
        if active and mode == "from_lattice":
            loc = self._lockable_localization()
            try:
                proposed = suggest_local_footprint_size(loc[0], shape) if loc else None
            except (ValueError, TypeError):
                proposed = None
            self.footprintModeCombo.setToolTip(
                f"Resolved footprint size: {proposed} px" if proposed else
                "Size is calculated from localization when reconstruction starts."
            )
            self.fitCalculatedSizeLabel.setText(f"{proposed} px" if proposed else "Auto size")

    def _set_fitting_expanded(self, expanded: bool) -> None:
        """Keep the inline fitting editor collapsed unless the user requests it."""
        self._fitting_expanded = bool(expanded)
        # CompactSection performs the header/frame swap itself. This callback
        # retains the existing flag for compatibility with the widget state.
        QtCore.QTimer.singleShot(0, self._refresh_scroll_minimum)

    def _fitting_changed(self, *_):
        """Update the existing scientific configuration directly from the main panel."""
        if self._applying_preset:
            return
        shape = str(self._combo_data(self.footprintShapeCombo))
        mode = str(self._combo_data(self.footprintModeCombo))
        size = float(self.footprintSizeSpin.value())
        if shape == "square" and mode == "fixed":
            # The extraction backend requires odd square sides. Quantize before
            # updating shared params so interactive edits never create invalid jobs.
            odd = max(3, min(255, int(round(size))))
            if odd % 2 == 0:
                odd = odd + 1 if odd < 255 else odd - 1
            size = float(odd)
        if shape == "lattice":
            mode = "from_lattice"  # sizing has no meaning for unit-cell ownership
        candidate = dict(self._advanced_values)
        candidate.update({
            "estimator": str(self._combo_data(self.estimatorCombo)),
            "footprint_shape": shape,
            "footprint_mode": mode,
            "footprint_size_px": size,
            "footprint_sigma_factor": float(self.footprintSigmaSpin.value()),
        })
        try:
            footprint_from_params(candidate)
        except ValueError:
            self._update_ui()
            return
        if any(candidate[key] != self._advanced_values[key] for key in (
            "estimator", "footprint_shape", "footprint_mode", "footprint_size_px",
            "footprint_sigma_factor",
        )):
            self._advanced_values.update(candidate)
            self._footprint_user_override = True
            self._controlled_changed()
        else:
            self._update_ui()

    def _update_footprint_summary(self):
        fit = footprint_from_params(self._advanced_values)
        saved = self._setup_defaults
        if saved.footprint_mode is None and saved.estimator is None:
            status = ("Modified · not saved" if self._footprint_user_override
                      else "Application default")
        else:
            same = (fit.shape == saved.footprint_shape and
                    (fit.shape == "lattice" or fit.mode == saved.footprint_mode))
            if same and fit.shape != "lattice":
                if fit.mode == "fixed":
                    same = fit.size_px == saved.footprint_size
                elif fit.mode == "fwhm":
                    same = fit.sigma_factor == saved.footprint_sigma_factor
            if saved.estimator is not None:
                same = same and self._advanced_values["estimator"] == saved.estimator
            status = "Setup default" if same else "Modified from setup"
        self.footprintSourceLabel.setText(status)
        self.footprintSourceLabel.setToolTip(
            "Save fitting default stores estimator and footprint together for this detector."
        )
        compact_status = (
            "Setup" if status == "Setup default" else
            "Modified" if status.startswith("Modified") else ""
        )
        self.fittingHeader.setStatus(
            compact_status, status + ". " + self.footprintSourceLabel.toolTip()
        )

    def _update_fitting_summary(self) -> None:
        """Show the actual scientific selection even while the editor is closed."""
        fit = footprint_from_params(self._advanced_values)
        estimator = str(self._advanced_values["estimator"]).capitalize()
        if fit.shape == "lattice":
            description = "Lattice cells"
        else:
            shape = fit.shape.capitalize()
            if fit.mode == "from_lattice":
                resolved = self.fitCalculatedSizeLabel.text()
                size = f" ({resolved})" if resolved and resolved != "Auto size" else ""
                description = f"{shape} · From lattice{size}"
            elif fit.mode == "fixed":
                description = f"{shape} · {fit.size_px:g} px"
            else:
                description = f"{shape} · {fit.sigma_factor:g}σ radius"
        self.fittingHeader.setSummary(f"{estimator} · {description}")

    def _restore_fitting_default(self):
        saved = self._setup_defaults
        if saved.footprint_mode is None and saved.estimator is None:
            return
        if saved.footprint_mode is not None:
            self._advanced_values.update({
                "footprint_shape": saved.footprint_shape,
                "footprint_mode": saved.footprint_mode,
                "footprint_size_px": saved.footprint_size,
                "footprint_sigma_factor": saved.footprint_sigma_factor,
            })
        if saved.estimator is not None:
            self._advanced_values["estimator"] = saved.estimator
        self._footprint_user_override = False
        self._controlled_changed()

    def _spot_unit_changed(self, *_):
        new_unit = str(self._combo_data(self.fwhmUnitCombo))
        old_unit = self._spot_unit
        if new_unit == old_unit:
            return
        width = float(self.fwhmSpin.value())
        pixel_nm = float(self._advanced_values.get("pixel_size_nm", 0.0))
        if pixel_nm > 0:
            if old_unit == "nm" and new_unit == "px":
                width /= pixel_nm
            elif old_unit == "px" and new_unit == "nm":
                width *= pixel_nm
            old = self.fwhmSpin.blockSignals(True)
            try:
                self.fwhmSpin.setValue(width)
            finally:
                self.fwhmSpin.blockSignals(old)
        self._spot_unit = new_unit
        self._update_section_summaries()

    def set_auto_spot_sigma_px(self, sigma_px: float) -> None:
        if str(self._combo_data(self.spotModeCombo)) != SPOT_AUTO:
            return
        try:
            sigma_px = float(sigma_px)
        except (TypeError, ValueError):
            return
        if sigma_px <= 0:
            return
        unit = str(self._combo_data(self.fwhmUnitCombo))
        pixel_nm = float(self._advanced_values.get("pixel_size_nm", 0.0))
        try:
            value = sigma_px_to_fwhm(sigma_px, unit, pixel_nm if unit == "nm" else None)
        except ValueError:
            return
        self._set_fwhm_value(float(value), "Auto estimate")

    def _fwhm_changed(self, *_):
        if not self._programmatic_fwhm and str(self._combo_data(self.spotModeCombo)) != SPOT_AUTO:
            self._fwhm_source = "Manual"
            self.fwhmSourceLabel.setText(self._fwhm_source)
        self._update_section_summaries()

    def _set_fwhm_value(self, value: float, source: str) -> None:
        self._programmatic_fwhm = True
        try:
            self.fwhmSpin.setValue(float(value))
        finally:
            self._programmatic_fwhm = False
        self._fwhm_source = source
        self.fwhmSourceLabel.setText(source)
        self._update_ui()

    @staticmethod
    def _fwhm_calibration_signature(params: dict) -> str:
        keys = (
            "localization_mode", "auto_localization", "locked_localization",
            "manual_offset_x_px", "manual_offset_y_px", "manual_period_x_px",
            "manual_period_y_px", "stagger_axis", "stagger", "localization_settings",
            "scan_steps_fast", "scan_steps_slow", "scan_step_nm",
        )
        return json.dumps({key: params.get(key) for key in keys}, sort_keys=True, default=str)

    def _estimate_fwhm(self):
        if self._current_data_obj is None or self._fwhm_estimate_busy:
            return
        self._fwhm_estimate_busy = True
        self._fwhm_estimate_serial += 1
        # Snapshot parameters and data identity: never apply a stale result
        # after switching datasets, localizations or the active detector.
        params = self.get_values()
        self._fwhm_estimate_revision = self._fwhm_data_revision
        self._fwhm_estimate_signature = self._fwhm_calibration_signature(params)
        task = _WidthEstimateTask(self._current_data_obj, params, self._fwhm_estimate_serial)
        task.signals.finished.connect(self._finish_fwhm_estimate)
        self._fwhm_estimate_task = task
        self._update_ui()
        QtCore.QThreadPool.globalInstance().start(task)

    @QtCore.Slot(object)
    def _finish_fwhm_estimate(self, result):
        serial, data_obj, sigma, status, localization, error = result
        if serial != self._fwhm_estimate_serial:
            return
        self._fwhm_estimate_busy = False
        self._fwhm_estimate_task = None
        if (
            data_obj is not self._current_data_obj
            or self._fwhm_estimate_revision != self._fwhm_data_revision
            or self._fwhm_estimate_signature != self._fwhm_calibration_signature(self.get_values())
        ):
            self.setPreviewStatus("Discarded FWHM estimate: source or localization changed")
            self._update_ui()
            return
        if error:
            QtWidgets.QMessageBox.warning(self, "FWHM estimation failed", error)
            self._update_ui()
            return
        unit = str(self._combo_data(self.fwhmUnitCombo))
        pixel_nm = float(self._advanced_values.get("pixel_size_nm", 0.0))
        try:
            value = sigma_px_to_fwhm(float(sigma), unit, pixel_nm if unit == "nm" else None)
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, "FWHM estimation failed", str(exc))
            self._update_ui()
            return
        self._set_combo_data(self.spotModeCombo, SPOT_MANUAL)
        self._set_fwhm_value(value, "Calibrated estimate")
        # Auto localization is shared only if a new one had to be detected.
        if localization is not None and str(self._combo_data(self.localizationModeCombo)) == LOCALIZATION_AUTO:
            self.set_auto_localization(localization, status)
        self.setPreviewStatus(f"FWHM estimated: {value:.3f} {unit}. {status}")
        self._update_ui()

    @staticmethod
    def _ism_calibration_signature(params: dict) -> str:
        # Only geometry, localization and source selection matter. ISM mode,
        # sharpening and Gaussian fitting do not affect the raw measurement.
        keys = (
            "preset", "pixel_size_nm", "localization_mode", "auto_localization",
            "locked_localization", "manual_offset_x_px", "manual_offset_y_px",
            "manual_period_x_px", "manual_period_y_px", "stagger_axis", "stagger",
            "localization_settings", "orientation_mode", "orientation",
            "locked_orientation", "_resolved_auto_orientation", "scan_steps_fast",
            "scan_steps_slow", "scan_step_nm",
        )
        return json.dumps({key: params.get(key) for key in keys}, sort_keys=True, default=str)

    def _ism_estimate_params(self) -> dict:
        params = self.get_values()
        if params["orientation_mode"] == ORIENTATION_AUTO:
            # A resolved Auto orientation is only reusable for the same data.
            # The visible combo is not authoritative: it could retain an old
            # manual/setup orientation while Auto is still pending.
            orientation = str(self._scan_result_summary.get("orientation") or "")
            if not orientation and self._registry is not None:
                item = self._registry.current_orientation(self._detector_id)
                current = str(getattr(self._current_data_obj, "name", "") or "")
                if item is not None and (
                    not item.source_dataset or not current or item.source_dataset == current
                ):
                    orientation = item.orientation.to_literal()
            params["_resolved_auto_orientation"] = orientation
        return params

    def _estimate_ism_shift(self):
        if self._current_data_obj is None or self._ism_estimate_busy:
            return
        params = self._ism_estimate_params()
        self._ism_estimate_serial += 1
        self._ism_estimate_busy = True
        self._ism_estimate_revision = self._fwhm_data_revision
        self._ism_estimate_signature = self._ism_calibration_signature(params)
        task = _ISMEstimateTask(self._current_data_obj, params, self._ism_estimate_serial)
        task.signals.finished.connect(self._finish_ism_estimate)
        self._ism_estimate_task = task
        self._update_ui()
        QtCore.QThreadPool.globalInstance().start(task)

    @QtCore.Slot(object)
    def _finish_ism_estimate(self, result):
        serial, data_obj, measurement, status, localization, error = result
        if serial != self._ism_estimate_serial:
            return
        self._ism_estimate_busy = False
        self._ism_estimate_task = None
        if (
            data_obj is not self._current_data_obj
            or self._ism_estimate_revision != self._fwhm_data_revision
            or self._ism_estimate_signature != self._ism_calibration_signature(
                self._ism_estimate_params()
            )
        ):
            self.setPreviewStatus("Discarded ISM estimate: source or scan geometry changed")
            self._update_ui()
            return
        if error:
            QtWidgets.QMessageBox.warning(self, "ISM shift estimation failed", error)
            self._update_ui()
            return
        factor = measurement.factor
        axis_values = {
            "x": float(factor.alpha_x),
            "y": float(factor.alpha_y),
        }
        axes = measurement.scanned_axes
        scalar = (
            axis_values[axes[0]] if len(axes) == 1
            else float(factor.alpha)
        )
        # Never let QDoubleSpinBox silently clip an unphysical estimate.
        relevant = [scalar] + [axis_values[a] for a in axes]
        if not all(0.0 <= value <= 1.0 for value in relevant):
            QtWidgets.QMessageBox.warning(
                self, "ISM shift estimation failed",
                "Measured shift factor is outside the physical range [0, 1]. "
                "Check orientation, localization and recorded scan geometry.",
            )
            self._update_ui()
            return
        self.shiftFactorSpin.setValue(scalar)
        self.shiftXSpin.setValue(axis_values["x"] if "x" in axes else 0.0)
        self.shiftYSpin.setValue(axis_values["y"] if "y" in axes else 0.0)
        self._set_combo_data(self.reassignmentCombo, "fixed")
        # Like FWHM Estimate, the acceptance is user-driven and doesn't lock
        # the detected localization or change its source mode.
        if localization is not None and str(self._combo_data(self.localizationModeCombo)) == LOCALIZATION_AUTO:
            self.set_auto_localization(localization, status)
        self.setPreviewStatus(
            f"ISM shift {scalar:.3f} ({'+'.join(a.upper() for a in axes)} scanned); "
            f"X {factor.alpha_x:.3f}, Y {factor.alpha_y:.3f}; "
            f"spread {factor.spread:.3f}. {status}"
        )
        self._update_ui()

    def _use_estimated_fwhm(self):
        if self._fwhm_source != "Auto estimate":
            return
        self._set_combo_data(self.spotModeCombo, SPOT_MANUAL)
        self._fwhm_source = "Calibrated estimate"
        self._update_ui()

    def _use_setup_fwhm(self):
        value = self._setup_defaults.fwhm_nm
        if value is None:
            return
        self._set_combo_data(self.spotModeCombo, SPOT_MANUAL)
        self._set_combo_data(self.fwhmUnitCombo, "nm")
        self._spot_unit = "nm"
        self._set_fwhm_value(value, "Setup default")

    def _save_fwhm_default(self):
        if str(self._combo_data(self.spotModeCombo)) == SPOT_AUTO:
            return
        unit = str(self._combo_data(self.fwhmUnitCombo))
        pixel_nm = float(self._advanced_values.get("pixel_size_nm", 0.0))
        fwhm_nm = float(self.fwhmSpin.value()) * (pixel_nm if unit == "px" else 1.0)
        if positive_fwhm(fwhm_nm) is None or (unit == "px" and pixel_nm <= 0):
            QtWidgets.QMessageBox.warning(self, "Invalid FWHM", "A positive FWHM and detector pixel calibration are required.")
            return
        self._confirm_save_setup(FWHM_KEY, fwhm_nm, f"FWHM = {fwhm_nm:g} nm")

    def _save_footprint_default(self):
        """Save *both* estimator and footprint; separate from FWHM defaults."""
        path = current_setup_path()
        if path is None or self._detector_id == "unknown":
            QtWidgets.QMessageBox.warning(
                self, "No setup", "Select data from a known detector and an active ImControl setup."
            )
            return
        fit = footprint_from_params(self._advanced_values)
        estimator = str(self._advanced_values["estimator"])
        detail = (
            f"{estimator.capitalize()} Gaussian, {fit.shape}, {fit.mode}"
            + (f", {fit.size_px:g} px" if fit.mode == "fixed" and fit.shape != "lattice" else "")
            + (f", {fit.sigma_factor:g}σ radius" if fit.mode == "fwhm" and fit.shape != "lattice" else "")
        )
        answer = QtWidgets.QMessageBox.question(
            self, "Save Gaussian fitting setup default?",
            f"Save {detail} for detector '{self._detector_id}' in:\n{path}\n\n"
            "Only the Gaussian estimator and fit-footprint properties will change.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return
        try:
            save_detector_fitting(self._detector_id, estimator, fit.mode, fit.shape,
                                  fit.size_px, sigma_factor=fit.sigma_factor, path=path)
        except (OSError, ValueError) as exc:
            QtWidgets.QMessageBox.warning(self, "Could not save fitting defaults", str(exc))
            return
        self._setup_defaults = read_detector_defaults(self._detector_id, path=path)
        self._footprint_user_override = False
        self.setPreviewStatus("Saved Gaussian estimator and footprint to active setup")
        self._update_ui()

    def _save_orientation_default(self):
        lockable = self._lockable_orientation()
        if lockable is None:
            return
        self._confirm_save_setup(
            ORIENTATION_KEY, lockable[0].to_literal(),
            f"orientation = {lockable[0].to_literal()}",
        )

    def _confirm_save_setup(self, key: str, value, detail: str) -> None:
        path = current_setup_path()
        if path is None or self._detector_id == "unknown":
            QtWidgets.QMessageBox.warning(self, "No setup", "Select data from a known detector and an active ImControl setup.")
            return
        answer = QtWidgets.QMessageBox.question(
            self, "Save MoNaLISA setup default?",
            f"Save {detail} for detector '{self._detector_id}' in:\n{path}\n\n"
            "Only this setup property is changed. ImControl may require a restart "
            "before new recordings snapshot the updated default.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return
        try:
            save_detector_default(self._detector_id, key, value, path=path)
        except (OSError, ValueError) as exc:
            QtWidgets.QMessageBox.warning(self, "Could not save setup default", str(exc))
            return
        self._setup_defaults = read_detector_defaults(self._detector_id, path=path)
        self._sync_locked_mode_availability()
        if key == FWHM_KEY:
            self._fwhm_source = "Setup default"
            self._update_ui()
        else:
            self._orientation_user_override = False
            self._loading_setup_orientation = True
            try:
                self._set_combo_data(self.orientationModeCombo, ORIENTATION_SETUP)
            finally:
                self._loading_setup_orientation = False
            self._refresh_orientation_display()
            self._update_ui()
        self.setPreviewStatus(f"Saved {detail} to active setup ({path.name})")

    @staticmethod
    def _result_for_current_data(results, current_name: str):
        results = tuple(results or ())
        for result in results:
            if current_name and str(getattr(result, "name", "")).startswith(f"{current_name}_monalisa"):
                return result
        return results[0] if len(results) == 1 else None

    def sync_reconstruction_results(self, results) -> None:
        selected = self._result_for_current_data(
            results, str(getattr(self._current_data_obj, "name", "") or "")
        )
        if selected is None:
            return
        metadata = dict(getattr(selected, "metadata", {}) or {})
        snapshot = metadata.get("monalisa_calibration")
        if not isinstance(snapshot, dict):
            params = dict(getattr(selected, "params", {}) or {})
            snapshot = params.get("calibration_snapshot")
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        self._sync_scan_result(selected, snapshot)
        if not snapshot:
            return

        if str(self._combo_data(self.spotModeCombo)) == SPOT_AUTO:
            try:
                sigma_px = float(snapshot.get("effective_sigma_px"))
            except (TypeError, ValueError):
                sigma_px = 0.0
            if sigma_px > 0:
                self.set_auto_spot_sigma_px(sigma_px)
            else:
                unit = str(self._combo_data(self.fwhmUnitCombo))
                key = "resolved_spot_fwhm_nm" if unit == "nm" else "resolved_spot_fwhm_px"
                try:
                    value = float(snapshot.get(key))
                except (TypeError, ValueError):
                    value = 0.0
                if value > 0:
                    self.fwhmSpin.setValue(value)

        if str(self._combo_data(self.reassignmentCombo)) == "auto":
            try:
                scalar = float(snapshot.get("resolved_shift_factor"))
            except (TypeError, ValueError):
                scalar = 0.0
            xy = snapshot.get("resolved_shift_factor_xy")
            if scalar > 0:
                self.shiftFactorSpin.setValue(scalar)
            if isinstance(xy, (list, tuple)) and len(xy) == 2:
                try:
                    self.shiftXSpin.setValue(float(xy[0]))
                    self.shiftYSpin.setValue(float(xy[1]))
                except (TypeError, ValueError):
                    pass

    def _sync_scan_result(self, result, snapshot: dict) -> None:
        diagnostics = dict(getattr(result, "diagnostics", {}) or {})
        orientation = str(
            snapshot.get("resolved_orientation")
            or diagnostics.get("resolved_orientation")
            or diagnostics.get("orientation")
            or ""
        )

        pixel = getattr(result, "output_pixel_size_nm", None)
        if pixel is None:
            output_pixel_nm = None
        elif isinstance(pixel, (tuple, list)):
            values = tuple(float(value) for value in pixel)
            output_pixel_nm = values if len(values) == 2 else None
        else:
            value = float(pixel)
            output_pixel_nm = (value, value)

        holes = diagnostics.get("coverage_holes")
        overlaps = diagnostics.get("coverage_overlaps")
        cell_pixels = diagnostics.get("coverage_cell_pixels")
        coverage_text = str(diagnostics.get("coverage of the cell") or "")
        match = re.search(
            r"(\d+) holes,\s*(\d+) overlaps of\s*(\d+) positions",
            coverage_text,
        )
        if match is not None:
            if holes is None:
                holes = int(match.group(1))
            if overlaps is None:
                overlaps = int(match.group(2))
            if cell_pixels is None:
                cell_pixels = int(match.group(3))

        self._scan_result_summary = {
            "orientation": orientation,
            "output_pixel_nm": output_pixel_nm,
            "plane_step_nm": getattr(result, "plane_step_nm", None),
            "coverage_holes": holes,
            "coverage_overlaps": overlaps,
            "coverage_cell_pixels": cell_pixels,
            "diagnostic_tooltip": "\n".join(
                text for text in (
                    str(diagnostics.get("scan step") or ""),
                    coverage_text,
                ) if text
            ),
        }
        self._refresh_orientation_display()

    # ------------------------------------------------------ session registry
    def _sync_locked_mode_availability(self) -> None:
        localization_profiles = (
            self._registry.localization_profiles(self._detector_id)
            if self._registry is not None else ()
        )
        has_localization = bool(localization_profiles)
        localization_tip = (
            "Use a localization saved for this detector."
            if has_localization else
            "No locked localization available. Localize a pattern and choose 'Lock current' first."
        )
        self._set_combo_item_enabled(
            self.localizationModeCombo, LOCALIZATION_LOCKED, has_localization, localization_tip
        )
        self.localizationModeCombo.setToolTip(localization_tip if not has_localization else "")

        has_orientation = (
            self._registry is not None
            and self._registry.locked_orientation(self._detector_id) is not None
        )
        orientation_tip = (
            "Use the orientation locked for this detector."
            if has_orientation else
            "No locked orientation available. Resolve an orientation and choose 'Lock current' first."
        )
        self._set_combo_item_enabled(
            self.orientationModeCombo, ORIENTATION_LOCKED, has_orientation, orientation_tip
        )
        self.orientationModeCombo.setToolTip(orientation_tip if not has_orientation else "")
        has_recorded_orientation = self._recorded_orientation is not None
        self._set_combo_item_enabled(
            self.orientationModeCombo, ORIENTATION_RECORDED, has_recorded_orientation,
            "Use orientation embedded with the recording."
            if has_recorded_orientation else "No recorded orientation available for this dataset.",
        )
        has_setup_orientation = self._setup_defaults.orientation is not None
        self._set_combo_item_enabled(
            self.orientationModeCombo, ORIENTATION_SETUP, has_setup_orientation,
            "Use detector orientation configured in the active hardware setup."
            if has_setup_orientation else "No orientation default for this detector in the active setup.",
        )

        if (
            str(self._combo_data(self.localizationModeCombo)) == LOCALIZATION_LOCKED
            and not has_localization
        ):
            self._set_combo_data(self.localizationModeCombo, LOCALIZATION_AUTO)
        if (
            str(self._combo_data(self.orientationModeCombo)) == ORIENTATION_LOCKED
            and not has_orientation
        ):
            self._set_combo_data(self.orientationModeCombo, ORIENTATION_AUTO)
        if (
            str(self._combo_data(self.orientationModeCombo)) == ORIENTATION_RECORDED
            and not has_recorded_orientation
        ):
            previous = self._loading_setup_orientation
            self._loading_setup_orientation = True
            try:
                self._set_combo_data(self.orientationModeCombo, ORIENTATION_AUTO)
            finally:
                self._loading_setup_orientation = previous
        if (
            str(self._combo_data(self.orientationModeCombo)) == ORIENTATION_SETUP
            and not has_setup_orientation
        ):
            previous = self._loading_setup_orientation
            self._loading_setup_orientation = True
            try:
                self._set_combo_data(self.orientationModeCombo, ORIENTATION_AUTO)
            finally:
                self._loading_setup_orientation = previous

    def _registry_changed(self):
        self._refresh_registry_controls()

    def _refresh_registry_controls(self, *, clear_selection: bool = False):
        current = "" if clear_selection else str(self._selected_locked_localization_id or "")
        old = self.lockedLocalizationCombo.blockSignals(True)
        try:
            self.lockedLocalizationCombo.clear()
            self.lockedLocalizationCombo.addItem("— choose locked localization —", "")
            if self._registry is not None:
                names = set()
                for profile in self._registry.localization_profiles(self._detector_id):
                    label = profile.name
                    if label in names:
                        label = f"{profile.name} ({profile.id[:6]})"
                    names.add(label)
                    self.lockedLocalizationCombo.addItem(label, profile.id)
            if current and self._registry is not None:
                profile = self._registry.localization_profile(current)
                if profile is None or profile.detector != self._detector_id:
                    current = ""
            self._selected_locked_localization_id = current
            self._set_combo_data(self.lockedLocalizationCombo, current)
        finally:
            self.lockedLocalizationCombo.blockSignals(old)
        self._sync_locked_mode_availability()
        self._refresh_orientation_display()
        self._update_ui()

    def sync_current_transient(self):
        if self._registry is None:
            return
        transient = self._registry.current_localization(self._detector_id)
        current_name = str(getattr(self._current_data_obj, "name", "") or "")
        if (
            transient is not None
            and str(self._combo_data(self.localizationModeCombo)) == LOCALIZATION_AUTO
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

    # --------------------------------------------------------------- values
    def get_values(self) -> dict:
        locked_profile = None
        if self._registry is not None and self._selected_locked_localization_id:
            locked_profile = self._registry.localization_profile(self._selected_locked_localization_id)
        locked_orientation = self._registry.locked_orientation(self._detector_id) if self._registry else None
        values = {
            "preset": self._mode_value(),
            "gaussian_mode": self._last_gaussian_mode,
            "device": str(self._advanced_values["device"]),
            "pixel_size_nm": float(self._advanced_values["pixel_size_nm"]),
            "localization_mode": str(self._combo_data(self.localizationModeCombo)),
            "auto_localization": deepcopy(self._auto_localization),
            "locked_localization_id": locked_profile.id if locked_profile else "",
            "locked_localization_name": locked_profile.name if locked_profile else "",
            "locked_localization": locked_profile.localization.to_dict() if locked_profile else {},
            "localization_refinement": str(self._localization_refinement),
            "manual_offset_x_px": float(self.offsetXSpin.value()),
            "manual_offset_y_px": float(self.offsetYSpin.value()),
            "manual_period_x_px": float(self.periodXSpin.value()),
            "manual_period_y_px": float(self.periodYSpin.value()),
            "stagger_axis": str(self._combo_data(self.staggerAxisCombo)),
            "stagger": float(self.staggerSpin.value()),
            "localization_settings": deepcopy(self._localization_settings.to_dict()),
            "orientation_mode": str(self._combo_data(self.orientationModeCombo)),
            "orientation": (
                self._setup_defaults.orientation
                if str(self._combo_data(self.orientationModeCombo)) == ORIENTATION_SETUP
                else (
                    self._recorded_orientation
                    if str(self._combo_data(self.orientationModeCombo)) == ORIENTATION_RECORDED
                    else str(self._combo_data(self.orientationCombo))
                )
            ),
            "locked_orientation": locked_orientation.orientation.to_literal() if locked_orientation else "",
            "spot_width_mode": str(self._combo_data(self.spotModeCombo)),
            "spot_width_source": str(self._fwhm_source),
            "spot_fwhm": float(self.fwhmSpin.value()),
            "spot_fwhm_unit": str(self._combo_data(self.fwhmUnitCombo)),
            "background": str(self._advanced_values["background"]),
            "estimator": ("dll_python" if self._mode_value() == PRESET_DLL
                          else str(self._advanced_values["estimator"])),
            "gaussian_estimator": str(self._advanced_values["estimator"]),
            "footprint_shape": str(self._advanced_values["footprint_shape"]),
            "footprint_mode": str(self._advanced_values["footprint_mode"]),
            "footprint_size_px": float(self._advanced_values["footprint_size_px"]),
            "footprint_sigma_factor": float(self._advanced_values["footprint_sigma_factor"]),
            "local_sampling": str(self._advanced_values["local_sampling"]),
            "legacy_bleaching_correction": bool(self.legacyBleachingCheck.isChecked()),
            "legacy_bg_modelling": str(self._combo_data(self.legacyBackgroundCombo)),
            "legacy_bg_gaussian_size_nm": float(self.legacyGaussianSizeSpin.value()),
            "intensity_correction": bool(self.intensityCheck.isChecked()),
            "cell_offsets": bool(self.cellOffsetsCheck.isChecked()),
            "reassignment": str(self._combo_data(self.reassignmentCombo)),
            "shift_factor": float(self.shiftFactorSpin.value()),
            "reassignment_asymmetric": bool(
                self.asymmetricCheck.isChecked() and len(self._scanned_lateral_axes) > 1
            ),
            "shift_factor_x": float(self.shiftXSpin.value()),
            "shift_factor_y": float(self.shiftYSpin.value()),
            "placement_mode": str(self._advanced_values["placement_mode"]),
            "commensurate_tolerance": float(self._advanced_values["commensurate_tolerance"]),
            "variance_weighting": bool(self._advanced_values["variance_weighting"]),
            "sharpen": bool(self.sharpenCheck.isChecked()),
            "sharpen_sigma_px": float(self._advanced_values["sharpen_sigma_px"]),
            "sharpen_regularization": float(self._advanced_values["sharpen_regularization"]),
            "pinhole_stack": bool(self.pinholeCheck.isChecked()),
            **self._scan_overrides,
        }
        # Full-only processing is retained while Normal is selected, but
        # disabled in the effective parameter snapshot sent to reconstruction.
        full = {key: values[key] for key in FULL_OPTION_DEFAULTS}
        values["full_options"] = full
        if values["preset"] != PRESET_FULL:
            values.update(reassignment="off", pinhole_stack=False, sharpen=False)
        if values["preset"] == PRESET_DLL:
            values.update(background="constant", variance_weighting=False)
        return values

    def set_pattern_params(self, row_offset: float, col_offset: float, row_period: float, col_period: float):
        self._set_combo_data(self.localizationModeCombo, LOCALIZATION_MANUAL)
        self.offsetXSpin.setValue(float(col_offset))
        self.offsetYSpin.setValue(float(row_offset))
        self.periodXSpin.setValue(float(col_period))
        self.periodYSpin.setValue(float(row_period))
        self._set_combo_data(self.staggerAxisCombo, "None")
        self.staggerSpin.setValue(0.0)

    def setPreviewStatus(self, text: str) -> None:
        self.previewStatusLabel.setText(str(text))
        self._update_localization_summary()
        QtCore.QTimer.singleShot(0, self._refresh_scroll_minimum)

    def set_localization_job_state(self, running: bool, status: str = "") -> None:
        """Reflect background localization without changing reconstruction state."""
        self._localization_busy = bool(running)
        if status:
            self.setPreviewStatus(status)
        self._update_ui()

    def has_auto_localization_for_current_data(self) -> bool:
        """Whether Auto already has a resolved localization for this source."""
        localization = PatternLocalization.from_dict(self._auto_localization)
        if localization is None:
            return False
        current = str(getattr(self._current_data_obj, "name", "") or "")
        source = str((self._auto_localization.get("provenance") or {}).get("dataset", "") or "")
        return not source or not current or source == current

    # ----------------------------------------------------------- data source
    @staticmethod
    def _step_to_nm(step, unit) -> float | None:
        if step is None:
            return None
        try:
            step = float(step)
        except (TypeError, ValueError):
            return None
        if step <= 0:
            return None
        key = str(unit).lower() if unit is not None else None
        factors = {None: 1000.0, "um": 1000.0, "µm": 1000.0, "nm": 1.0, "mm": 1e6, "m": 1e9}
        factor = factors.get(key)
        return None if factor is None else step * factor

    @staticmethod
    def _source_scan_info(attrs: dict, data_obj) -> tuple[str, set[str], dict]:
        resolved = getattr(data_obj, "acquisition_layout", None)
        layout = getattr(resolved, "layout", None)
        if layout is not None:
            loops = {
                loop.kind: loop for loop in getattr(layout, "event_loops", ())
                if loop.kind in ("scan_x", "scan_y", "scan_z") and int(loop.count) > 1
            }
            lateral = {axis for axis in "xy" if f"scan_{axis}" in loops}
            parts = []
            axes = {}
            for axis in "xyz":
                loop = loops.get(f"scan_{axis}")
                if loop is None:
                    continue
                step_nm = MonalisaParamsWidget._step_to_nm(loop.step, loop.unit)
                axes[axis] = {"count": int(loop.count), "step_nm": step_nm}
                step = "" if step_nm is None else f", step {step_nm:g} nm"
                parts.append(f"{axis.upper()} {int(loop.count)}{step}")
            if parts:
                return "Recorded: " + ", ".join(parts), lateral or {"x", "y"}, axes

        lateral = set()
        axes = {}
        try:
            nx = int(float(attrs.get("ScanTTL:Nx", 0)))
            ny = int(float(attrs.get("ScanTTL:Ny", 0)))
        except (TypeError, ValueError):
            nx = ny = 0

        step_sizes = attrs.get("ScanStage:axis_step_size")
        if hasattr(step_sizes, "tolist"):
            step_sizes = step_sizes.tolist()
        if not isinstance(step_sizes, (list, tuple)):
            step_sizes = ()
        step_unit = attrs.get("ScanStage:axis_step_size_unit", "um")
        for index, (axis, count) in enumerate((("x", nx), ("y", ny))):
            if count <= 1:
                continue
            lateral.add(axis)
            step = step_sizes[index] if index < len(step_sizes) else None
            axes[axis] = {
                "count": count,
                "step_nm": MonalisaParamsWidget._step_to_nm(step, step_unit),
            }
        if axes:
            parts = []
            for axis in "xy":
                info = axes.get(axis)
                if not info:
                    continue
                step = info.get("step_nm")
                suffix = "" if step is None else f", step {float(step):g} nm"
                parts.append(f"{axis.upper()} {int(info['count'])}{suffix}")
            return "Recorded: " + ", ".join(parts), lateral or {"x", "y"}, axes
        return "Recorded metadata: used by default", {"x", "y"}, {}

    def load_from_attrs(self, attrs: dict, data_obj=None) -> None:
        attrs = dict(attrs or {})
        old_detector = self._detector_id
        self._current_data_obj = data_obj
        self._fwhm_data_revision += 1
        self._detector_id = detector_identity(data_obj, attrs)
        self._setup_defaults = read_detector_defaults(self._detector_id)
        if old_detector != self._detector_id:
            self._footprint_user_override = False
        if not self._footprint_user_override and self._setup_defaults.footprint_mode is not None:
            self._advanced_values.update({
                "footprint_mode": self._setup_defaults.footprint_mode,
                "footprint_shape": self._setup_defaults.footprint_shape,
                "footprint_size_px": self._setup_defaults.footprint_size,
                "footprint_sigma_factor": self._setup_defaults.footprint_sigma_factor,
            })
        if not self._footprint_user_override and self._setup_defaults.estimator is not None:
            self._advanced_values["estimator"] = self._setup_defaults.estimator
        from .setup_defaults import valid_orientation
        self._recorded_orientation = valid_orientation(attrs.get("monalisa:scan_orientation"))
        self._sync_locked_mode_availability()
        if not self._orientation_user_override:
            self._loading_setup_orientation = True
            try:
                self._set_combo_data(
                    self.orientationModeCombo,
                    (ORIENTATION_RECORDED if self._recorded_orientation else
                     ORIENTATION_SETUP if self._setup_defaults.orientation else ORIENTATION_AUTO),
                )
            finally:
                self._loading_setup_orientation = False
        (
            self._recorded_scan_summary,
            self._scanned_lateral_axes,
            self._recorded_scan_axes,
        ) = self._source_scan_info(attrs, data_obj)
        self._scan_result_summary = {}
        self._update_scan_summary()
        if old_detector != self._detector_id:
            self._selected_locked_localization_id = ""
            self._refresh_registry_controls(clear_selection=True)

        if self._auto_localization and data_obj is not None:
            source = str((self._auto_localization.get("provenance") or {}).get("dataset", ""))
            current = str(getattr(data_obj, "name", "") or "")
            if source and current and source != current:
                self.clear_auto_localization("Auto localization pending…")

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
            self._advanced_values["pixel_size_nm"] = float(pixel_nm)

        embedded = attrs.get("monalisa_calibration")
        if isinstance(embedded, str):
            try:
                embedded = json.loads(embedded)
            except (TypeError, ValueError, json.JSONDecodeError):
                embedded = None
        if isinstance(embedded, dict) and "per_data_item" not in embedded:
            if not self._footprint_user_override:
                # Old results stored per-estimator fields.  Convert according
                # to the estimator actually used, never to today's UI default.
                if ("footprint_shape" in embedded or
                        "local_footprint_shape" in embedded or
                        "joint_footprint_mode" in embedded):
                    try:
                        migrated = migrate_footprint_params(
                            embedded, estimator=str(embedded.get("estimator", "joint"))
                        )
                        self._advanced_values.update({key: migrated[key] for key in (
                            "footprint_shape", "footprint_mode", "footprint_size_px",
                            "footprint_sigma_factor",
                        )})
                    except (TypeError, ValueError):
                        pass
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
            if valid_orientation(resolved_orientation) and not self._orientation_user_override:
                self._recorded_orientation = resolved_orientation
                self._loading_setup_orientation = True
                try:
                    self._set_combo_data(self.orientationModeCombo, ORIENTATION_RECORDED)
                finally:
                    self._loading_setup_orientation = False
            if resolved_orientation and self._registry is not None:
                try:
                    self._registry.set_current_orientation(
                        self._detector_id, resolved_orientation, source="embedded",
                        source_dataset=str(getattr(data_obj, "name", "") or ""),
                        source_path=str(getattr(data_obj, "dataPath", "") or ""),
                    )
                except ValueError:
                    pass

        embedded_fwhm_nm = embedded_fwhm_px = None
        if isinstance(embedded, dict) and "per_data_item" not in embedded:
            try:
                embedded_fwhm_nm = float(embedded.get("resolved_spot_fwhm_nm"))
            except (TypeError, ValueError):
                pass
            try:
                embedded_fwhm_px = float(embedded.get("resolved_spot_fwhm_px"))
            except (TypeError, ValueError):
                pass
        if embedded_fwhm_nm is not None and embedded_fwhm_nm > 0:
            self._set_combo_data(self.fwhmUnitCombo, "nm")
            self._set_fwhm_value(embedded_fwhm_nm, "Embedded result")
        elif embedded_fwhm_px is not None and embedded_fwhm_px > 0:
            self._set_combo_data(self.fwhmUnitCombo, "px")
            self._set_fwhm_value(embedded_fwhm_px, "Embedded result")
        else:
            recorded_fwhm = next(
                (value for key in ("monalisa:spot_fwhm_nm", "monalisaSpotFwhmNm")
                 if (value := positive_fwhm(attrs.get(key))) is not None), None
            )
            chosen = recorded_fwhm if recorded_fwhm is not None else self._setup_defaults.fwhm_nm
            if chosen is not None:
                self._set_combo_data(self.fwhmUnitCombo, "nm")
                self._set_fwhm_value(
                    chosen, "Recording" if recorded_fwhm is not None else "Setup default"
                )
        self._refresh_registry_controls()
        self._update_ui()

    # ------------------------------------------------------------- recovery
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
