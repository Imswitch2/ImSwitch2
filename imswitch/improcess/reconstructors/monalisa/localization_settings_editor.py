"""Automatic localization tuning controls shared with advanced reconstruction settings."""

from __future__ import annotations

from qtpy import QtCore, QtWidgets

from .localization_settings import DEFAULT_LOCALIZATION_SETTINGS, LocalizationSettings, PeriodPrior


class LocalizationSettingsEditor(QtWidgets.QGroupBox):
    """Reusable editor for automatic lattice localization tuning."""

    def __init__(self, settings: LocalizationSettings, parent=None):
        super().__init__("Automatic localization", parent)
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
        self.resetButton.clicked.connect(lambda: self._load(self._defaults))

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.showAdvanced)
        layout.addWidget(advanced)
        layout.addWidget(self.resetButton, alignment=QtCore.Qt.AlignRight)

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




__all__ = ["LocalizationSettingsEditor"]
