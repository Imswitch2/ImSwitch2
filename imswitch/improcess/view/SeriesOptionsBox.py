"""The Series box: which timepoints of a series a reconstruction covers.

Shown in the reconstruction panel, under the active plugin's own parameters,
for every reconstructor that supports series and a source that is one
timepoint of one (see :mod:`imswitch.improcess.model.series`). It is the same
for every plugin: the options are the reserved ``_series*`` parameters, which
``getReconstructionParams`` adds to the plugin's own.

Copyright (C) 2020-2026 ImSwitch developers
This file is part of ImSwitch.

ImSwitch is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

ImSwitch is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

from qtpy import QtWidgets

from imswitch.improcess.model import series as series_model


class SeriesOptionsBox(QtWidgets.QGroupBox):
    """This timepoint / all timepoints / a range, and what to do about gaps."""

    def __init__(self, parent=None):
        super().__init__("Timepoints", parent)
        form = QtWidgets.QFormLayout(self)
        self.summary = QtWidgets.QLabel()
        self.summary.setWordWrap(True)
        form.addRow(self.summary)

        self.modeCombo = QtWidgets.QComboBox()
        for label, value in (
            ("This timepoint only", "this"), ("All timepoints", "all"), ("Range", "range"),
        ):
            self.modeCombo.addItem(label, value)
        form.addRow("Reconstruct:", self.modeCombo)

        self.firstSpin = QtWidgets.QSpinBox()
        self.firstSpin.setRange(0, 1_000_000)
        self.lastSpin = QtWidgets.QSpinBox()
        self.lastSpin.setRange(-1, 1_000_000)
        self.lastSpin.setSpecialValueText("last")
        self.lastSpin.setValue(-1)
        self.stepSpin = QtWidgets.QSpinBox()
        self.stepSpin.setRange(1, 100_000)
        self.firstSpin.setPrefix("from ")
        self.lastSpin.setPrefix("to ")
        self.stepSpin.setPrefix("every ")
        rangeRow = QtWidgets.QHBoxLayout()
        for spin in (self.firstSpin, self.lastSpin, self.stepSpin):
            rangeRow.addWidget(spin)
        self.rangeWidget = QtWidgets.QWidget()
        self.rangeWidget.setLayout(rangeRow)
        rangeRow.setContentsMargins(0, 0, 0, 0)
        form.addRow("Range:", self.rangeWidget)

        self.missingCombo = QtWidgets.QComboBox()
        for label, value in (("Skip", "skip"), ("Fill with zeros", "zeros"), ("Fill with NaN", "nan")):
            self.missingCombo.addItem(label, value)
        self.missingCombo.setToolTip(
            "A missing or incomplete timepoint is reported either way. Skip leaves it\n"
            "out; zeros/NaN keep T regular so plane n stays timepoint n."
        )
        form.addRow("Missing timepoints:", self.missingCombo)

        self.shapesCombo = QtWidgets.QComboBox()
        for label, value in (("Refuse", "refuse"), ("Pad (top-left)", "pad_origin"), ("Pad (centred)", "pad_center")):
            self.shapesCombo.addItem(label, value)
        self.shapesCombo.setToolTip(
            "When the timepoints' outputs differ in size. Nothing is ever cropped:\n"
            "refuse names the sizes; padding uses the largest canvas."
        )
        form.addRow("Different sizes:", self.shapesCombo)
        self.shapesRow = form.labelForField(self.shapesCombo)

        self.modeCombo.currentIndexChanged.connect(self._refreshEnabled)
        self._refreshEnabled()
        self.hide()

    def _refreshEnabled(self, *_args) -> None:
        mode = self.modeCombo.currentData()
        self.rangeWidget.setEnabled(mode == "range")
        for control in (self.missingCombo, self.shapesCombo):
            control.setEnabled(mode != "this")

    def set_series(self, info, default: str = "this", opened_as_series=None) -> None:
        """Show for ``info`` (a ``SeriesInfo``) or hide for ``None``.

        ``default`` is the reconstructor's ``series_default``; opening the
        series' own manifest selects *all* whatever it says.
        """
        if info is None or len(info.members) < 2:
            self.hide()
            return
        as_series = info.opened_as_series if opened_as_series is None else opened_as_series
        chosen = "all" if as_series else (default if default in ("this", "all") else "this")
        self.modeCombo.setCurrentIndex(max(0, self.modeCombo.findData(chosen)))
        self.lastSpin.setMaximum(max(0, len(info.members) - 1))
        self.summary.setText(f"This source is one timepoint of a series. {info.describe()}.")
        self.show()

    def get_values(self) -> dict:
        """The reserved parameters, or ``{}`` while the box is hidden."""
        if self.isHidden():
            return {}
        last = self.lastSpin.value()
        return {
            series_model.SERIES_PARAM: self.modeCombo.currentData(),
            series_model.SERIES_FIRST_PARAM: self.firstSpin.value(),
            series_model.SERIES_LAST_PARAM: None if last < 0 else last,
            series_model.SERIES_STEP_PARAM: self.stepSpin.value(),
            series_model.SERIES_MISSING_PARAM: self.missingCombo.currentData(),
            series_model.SERIES_SHAPES_PARAM: self.shapesCombo.currentData(),
        }
