"""ImageJ's *Set Measurements*, over the shared registry.

The dialog does not know what a measurement is — it renders whatever the
registry holds, grouped as the registry groups it. Adding a measurement
therefore adds a checkbox here without anyone editing this file.
"""

from __future__ import annotations

from qtpy import QtWidgets

from imswitch.imcommon.algorithms.roi_set import MeasurementConfig
from imswitch.improcess.analysis.roi_measurements import default_selection, groups


class ROIMeasurementsDialog(QtWidgets.QDialog):
    """Choose which measurements are taken, and how they are reported."""

    def __init__(self, config: MeasurementConfig, parent=None, *, threshold_source=None):
        """``threshold_source`` supplies a ``(low, high)`` to seed *Limit to
        threshold* — the Segmentation panel's current window, when there is
        one. Seeded, never applied: the measurement threshold is recorded with
        the set, so it must be the user's explicit choice rather than whatever
        another panel happens to be showing."""
        super().__init__(parent)
        self.setWindowTitle("Set Measurements")
        self._checks: dict[str, QtWidgets.QCheckBox] = {}
        self._thresholdSource = threshold_source

        selected = set(
            default_selection() if config.selected is None else config.selected
        )
        columns = QtWidgets.QHBoxLayout()
        for group, entries in groups().items():
            box = QtWidgets.QGroupBox(group)
            layout = QtWidgets.QVBoxLayout(box)
            for entry in entries:
                check = QtWidgets.QCheckBox(entry.label)
                check.setChecked(entry.id in selected)
                if entry.note:
                    # The definitions that are easy to assume wrong — the
                    # perimeter estimator, the n-1 standard deviation — are
                    # stated where the choice is made.
                    check.setToolTip(entry.note)
                layout.addWidget(check)
                self._checks[entry.id] = check
            layout.addStretch()
            columns.addWidget(box)

        self.lineWidthSpin = QtWidgets.QSpinBox()
        self.lineWidthSpin.setRange(1, 99)
        self.lineWidthSpin.setValue(int(config.line_width))
        self.lineWidthSpin.setToolTip(
            "Perpendicular samples averaged along a line ROI, as in ImageJ. "
            "1 samples the line itself."
        )
        self.displayLabelCheck = QtWidgets.QCheckBox("Display label")
        self.displayLabelCheck.setChecked(bool(config.display_label))
        self.displayLabelCheck.setToolTip(
            "Add a readable \"image:roi\" label column to pushed rows. The "
            "identity columns are always present either way."
        )

        self.decimalsSpin = QtWidgets.QSpinBox()
        self.decimalsSpin.setRange(0, 9)
        self.decimalsSpin.setValue(int(config.decimals))
        self.scientificCheck = QtWidgets.QCheckBox("Scientific notation")
        self.scientificCheck.setChecked(bool(config.scientific))

        self.thresholdCheck = QtWidgets.QCheckBox("Limit to threshold")
        self.thresholdCheck.setToolTip(
            "Restrict intensity statistics to pixels inside this range. "
            "Recorded with the set, so a measurement can be reproduced from "
            "the set alone."
        )
        self.lowSpin = QtWidgets.QDoubleSpinBox()
        self.highSpin = QtWidgets.QDoubleSpinBox()
        for spin in (self.lowSpin, self.highSpin):
            spin.setRange(-1e12, 1e12)
            spin.setDecimals(4)
        if config.threshold is not None:
            self.thresholdCheck.setChecked(True)
            self.lowSpin.setValue(float(config.threshold[0]))
            self.highSpin.setValue(float(config.threshold[1]))
        elif threshold_source:
            # Seeded from the Segmentation panel, unticked: the window is
            # offered, not imposed.
            try:
                low, high = threshold_source
                self.lowSpin.setValue(float(low))
                self.highSpin.setValue(float(high))
                self.thresholdCheck.setToolTip(
                    self.thresholdCheck.toolTip()
                    + "\nSeeded from the Segmentation panel's current threshold."
                )
            except Exception:
                pass
        self._thresholdToggled(self.thresholdCheck.isChecked())
        self.thresholdCheck.toggled.connect(self._thresholdToggled)

        options = QtWidgets.QFormLayout()
        options.addRow("Decimal places:", self.decimalsSpin)
        options.addRow("Line width:", self.lineWidthSpin)
        options.addRow("", self.scientificCheck)
        options.addRow("", self.displayLabelCheck)
        options.addRow("", self.thresholdCheck)
        thresholdRow = QtWidgets.QHBoxLayout()
        thresholdRow.addWidget(self.lowSpin)
        thresholdRow.addWidget(QtWidgets.QLabel("to"))
        thresholdRow.addWidget(self.highSpin)
        options.addRow("Threshold:", thresholdRow)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(columns)
        layout.addLayout(options)
        layout.addWidget(buttons)

    def _thresholdToggled(self, enabled: bool) -> None:
        self.lowSpin.setEnabled(enabled)
        self.highSpin.setEnabled(enabled)

    def config(self) -> MeasurementConfig:
        """The configuration the dialog now describes."""
        selected = tuple(
            key for key, check in self._checks.items() if check.isChecked()
        )
        threshold = None
        if self.thresholdCheck.isChecked():
            low, high = self.lowSpin.value(), self.highSpin.value()
            threshold = (min(low, high), max(low, high))
        return MeasurementConfig(
            selected=selected,
            decimals=self.decimalsSpin.value(),
            scientific=self.scientificCheck.isChecked(),
            threshold=threshold,
            line_width=self.lineWidthSpin.value(),
            display_label=self.displayLabelCheck.isChecked(),
        )

    @classmethod
    def edit(
        cls, config: MeasurementConfig, parent=None, *, threshold_source=None
    ) -> MeasurementConfig | None:
        dialog = cls(config, parent, threshold_source=threshold_source)
        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            return dialog.config()
        return None


__all__ = ["ROIMeasurementsDialog"]
