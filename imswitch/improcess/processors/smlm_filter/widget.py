"""The SMLM filter panel: one property, its histogram, a band on it.

The property combo offers the columns the current table has values in; the
histogram below shows how that property is distributed; the band -- two
spin boxes, and the shaded region on the histogram, which are two handles
on the same two numbers -- is what the filter keeps (band-pass) or drops
(band-stop). The count line says what the band would keep before anything
is run.

*Copy* decides where the filtered table goes. On, **Apply filter** makes a
new result and the panel follows it; off, the filtered table takes the
current result's place in the list. It starts on and turns itself off after
the first filter applied, so the first step keeps the original and the
following ones refine the copy in place instead of leaving one result per
step; it turns back on when the panel is pointed at a result the filter did
not just make.
"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from qtpy import QtCore, QtWidgets

from imswitch.improcess.analysis.smlm_tables import (
    FILTER_MODES,
    filter_by_property,
    histogram_edges,
    informative_columns,
)
from imswitch.improcess.model.localization_result import LocalizationResult
from imswitch.improcess.model.localization_schema import LOCALIZATION_COLUMNS

#: What the combo calls each column.
PROPERTY_LABELS: dict[str, str] = {
    "frame": "Frame",
    "x_nm": "x (nm)",
    "y_nm": "y (nm)",
    "z_nm": "z (nm)",
    "sigma_x_nm": "PSF sigma x (nm)",
    "sigma_y_nm": "PSF sigma y (nm)",
    "sigma_z_nm": "PSF sigma z (nm)",
    "photons": "Photons",
    "lp_x_nm": "Precision x (nm)",
    "lp_y_nm": "Precision y (nm)",
    "lp_z_nm": "Precision z (nm)",
}

_MODE_LABELS: dict[str, str] = {
    "bandpass": "Band-pass (keep inside the band)",
    "bandstop": "Band-stop (drop inside the band)",
}

#: Shading of the band: what it keeps is green, what it drops is red.
_BAND_BRUSHES = {"bandpass": (80, 200, 120, 70), "bandstop": (230, 90, 90, 70)}

#: Spin-box range. Wide enough for any quantity the table holds; a bound
#: sitting at either end is read as "open on that side".
_BOUND_LIMIT = 1e12

#: How long a resize must rest before the histogram is redrawn for the new
#: width; dragging the dock edge would otherwise recompute it per pixel.
_REDRAW_DELAY_MS = 150


class SmlmFilterWidget(QtWidgets.QWidget):
    """Parameter widget of :class:`SmlmFilterProcessor`."""

    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self._locs: np.recarray | None = None
        self._syncing = False
        self._boundsActive = False
        self._bars = None
        self._lastProduced = None

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        propertyForm = QtWidgets.QFormLayout()
        propertyForm.setContentsMargins(0, 0, 0, 0)
        self.propertyCombo = QtWidgets.QComboBox()
        self.propertyCombo.setToolTip(
            "The property to filter on. Only the columns this table has "
            "values in are offered."
        )
        self._fillProperties(LOCALIZATION_COLUMNS)
        self.propertyCombo.currentIndexChanged.connect(self._propertyChanged)
        propertyForm.addRow("Property:", self.propertyCombo)
        layout.addLayout(propertyForm)

        self.plot = pg.PlotWidget()
        self.plot.setFixedHeight(170)
        self.plot.setMenuEnabled(False)
        self.plot.hideButtons()
        self.plot.setMouseEnabled(x=False, y=False)
        self.plot.getPlotItem().getAxis("left").setLabel("count")
        self.plot.setToolTip("Distribution of the chosen property; drag the band's edges")
        self.region = pg.LinearRegionItem(values=(0.0, 1.0), movable=True)
        self.region.setZValue(10)
        self.region.sigRegionChanged.connect(self._regionDragged)
        self.plot.addItem(self.region)
        self.region.hide()
        layout.addWidget(self.plot)

        bandForm = QtWidgets.QFormLayout()
        bandForm.setContentsMargins(0, 0, 0, 0)
        self.modeCombo = QtWidgets.QComboBox()
        for key in FILTER_MODES:
            self.modeCombo.addItem(_MODE_LABELS[key], key)
        self.modeCombo.setToolTip(
            "Band-pass keeps the localizations inside the band, band-stop "
            "drops them. Both edges are inclusive."
        )
        self.modeCombo.currentIndexChanged.connect(self._modeChanged)
        bandForm.addRow("Mode:", self.modeCombo)

        self.lowerSpin = self._boundSpin("Lower edge of the band (inclusive)")
        self.upperSpin = self._boundSpin("Upper edge of the band (inclusive)")
        bandForm.addRow("Lower:", self.lowerSpin)
        bandForm.addRow("Upper:", self.upperSpin)
        layout.addLayout(bandForm)

        self.copyCheck = QtWidgets.QCheckBox("Copy: filter into a new result")
        self.copyCheck.setChecked(True)
        self.copyCheck.setToolTip(
            "On: Apply filter makes a new result and the panel follows it. "
            "Off: the filtered table replaces the current result in place. "
            "Turns itself off after the first filter applied, so the next ones "
            "refine that copy, and back on when another result is selected."
        )
        layout.addWidget(self.copyCheck)

        self.countLabel = QtWidgets.QLabel("")
        self.countLabel.setStyleSheet("color: palette(mid); font-size: 9pt; padding: 0px 4px;")
        self.countLabel.setWordWrap(True)
        layout.addWidget(self.countLabel)

        self._modeChanged(0)
        self._setBoundsActive(False)

        # The bins follow the width the plot really has, which is not known
        # until it is shown and changes when the dock is resized.
        self._redrawTimer = QtCore.QTimer(self)
        self._redrawTimer.setSingleShot(True)
        self._redrawTimer.setInterval(_REDRAW_DELAY_MS)
        self._redrawTimer.timeout.connect(self._redrawForWidth)

    def _redrawForWidth(self) -> None:
        if self._locs is not None:
            self._refreshHistogram(reset_bounds=False)

    def resizeEvent(self, event) -> None:  # noqa: N802 -- Qt override
        super().resizeEvent(event)
        self._redrawTimer.start()

    def showEvent(self, event) -> None:  # noqa: N802 -- Qt override
        super().showEvent(event)
        self._redrawTimer.start()

    def _boundSpin(self, tip: str) -> QtWidgets.QDoubleSpinBox:
        spin = QtWidgets.QDoubleSpinBox()
        spin.setRange(-_BOUND_LIMIT, _BOUND_LIMIT)
        spin.setDecimals(2)
        spin.setToolTip(tip)
        spin.valueChanged.connect(self._boundsEdited)
        return spin

    # -- what is shown ----------------------------------------------------

    def property(self) -> str:
        return str(self.propertyCombo.currentData() or "photons")

    def mode(self) -> str:
        return str(self.modeCombo.currentData() or "bandpass")

    def setResult(self, result, rois=()) -> None:
        """Show ``result``'s table: its columns, the chosen one's histogram."""
        if result is not self._lastProduced:
            # A result the filter did not just make is somebody's original:
            # the first step on it copies, whatever the box said before.
            self.copyCheck.setChecked(True)
        locs = result.locs if isinstance(result, LocalizationResult) else None
        self._locs = locs if locs is not None and len(locs) else None
        columns = informative_columns(self._locs) if self._locs is not None else LOCALIZATION_COLUMNS
        self._fillProperties(columns)
        self._refreshHistogram(reset_bounds=True)

    def _fillProperties(self, columns) -> None:
        current = self.propertyCombo.currentData()
        self.propertyCombo.blockSignals(True)
        self.propertyCombo.clear()
        for name in columns:
            self.propertyCombo.addItem(PROPERTY_LABELS.get(name, name), name)
        index = self.propertyCombo.findData(current)
        if index < 0:
            index = max(0, self.propertyCombo.findData("photons"))
        self.propertyCombo.setCurrentIndex(index)
        self.propertyCombo.blockSignals(False)

    def _propertyChanged(self, _index: int) -> None:
        self._refreshHistogram(reset_bounds=True)

    def _refreshHistogram(self, reset_bounds: bool) -> None:
        if self._bars is not None:
            self.plot.removeItem(self._bars)
            self._bars = None
        name = self.property()
        self.plot.getPlotItem().getAxis("bottom").setLabel(PROPERTY_LABELS.get(name, name))
        if self._locs is None:
            self.region.hide()
            self._setBoundsActive(False)
            return
        values = np.asarray(self._locs[name], dtype=float)
        finite = values[np.isfinite(values)]
        if finite.size == 0:
            self.region.hide()
            self._setBoundsActive(False)
            self.countLabel.setText("No finite values in this property.")
            return

        low, high = float(finite.min()), float(finite.max())
        span = (low, high) if high > low else (low - 0.5, high + 0.5)
        edges = histogram_edges(finite, max(1, self.plot.width()))
        counts, edges = np.histogram(finite, bins=edges)
        self._bars = self.plot.plot(
            edges, counts, stepMode="center", fillLevel=0,
            brush=(100, 150, 255, 120), pen=pg.mkPen((100, 150, 255)),
        )
        self.plot.setXRange(float(edges[0]), float(edges[-1]), padding=0.02)

        decimals = 0 if name == "frame" else 2
        step = max((high - low) / 100.0, 10.0 ** -decimals)
        for spin in (self.lowerSpin, self.upperSpin):
            spin.setDecimals(decimals)
            spin.setSingleStep(step)
        if reset_bounds or not self._boundsActive:
            self._setBounds(low, high)
        self._setBoundsActive(True)
        self.region.show()
        self._syncRegion()
        self._updateCount()

    # -- the band ---------------------------------------------------------

    def bounds(self) -> tuple[float | None, float | None]:
        """The band's edges, ``None`` for a side that is open or unset."""
        if not self._boundsActive:
            return None, None
        lower = float(self.lowerSpin.value())
        upper = float(self.upperSpin.value())
        return (
            None if lower <= self.lowerSpin.minimum() else lower,
            None if upper >= self.upperSpin.maximum() else upper,
        )

    def _setBounds(self, lower: float, upper: float) -> None:
        self._syncing = True
        try:
            self.lowerSpin.setValue(lower)
            self.upperSpin.setValue(upper)
        finally:
            self._syncing = False

    def _setBoundsActive(self, active: bool) -> None:
        self._boundsActive = bool(active)
        self.lowerSpin.setEnabled(self._boundsActive)
        self.upperSpin.setEnabled(self._boundsActive)
        if not self._boundsActive:
            self.countLabel.setText("")

    def _boundsEdited(self, _value) -> None:
        if self._syncing:
            return
        self._syncRegion()
        self._updateCount()

    def _syncRegion(self) -> None:
        self._syncing = True
        try:
            self.region.setRegion((self.lowerSpin.value(), self.upperSpin.value()))
        finally:
            self._syncing = False

    def _regionDragged(self) -> None:
        if self._syncing:
            return
        low, high = self.region.getRegion()
        self._setBounds(float(low), float(high))
        self._updateCount()

    def _modeChanged(self, _index: int) -> None:
        self.region.setBrush(pg.mkBrush(_BAND_BRUSHES[self.mode()]))
        self._updateCount()

    def _updateCount(self) -> None:
        if self._locs is None or not self._boundsActive:
            self.countLabel.setText("")
            return
        lower, upper = self.bounds()
        try:
            _filtered, mask = filter_by_property(
                self._locs, self.property(), lower=lower, upper=upper, mode=self.mode()
            )
        except ValueError as exc:
            self.countLabel.setText(str(exc))
            return
        self.countLabel.setText(f"Keeps {int(mask.sum())} of {len(mask)} localizations.")

    # -- where the filtered table goes ------------------------------------

    def replaces_input(self) -> bool:
        """Whether the next run replaces the current result rather than copying it."""
        return not self.copyCheck.isChecked()

    def after_run(self, results, failures) -> None:
        """The first applied filter made the copy; the next ones refine it."""
        if not results:
            return
        self._lastProduced = results[-1]
        self.copyCheck.setChecked(False)

    # -- the parameter contract -------------------------------------------

    def get_values(self) -> dict:
        lower, upper = self.bounds()
        return {
            "property": self.property(),
            "mode": self.mode(),
            "lower": lower,
            "upper": upper,
        }

    def set_values(self, values: dict) -> None:
        """Take the keys given; the rest stay as they are."""
        if "property" in values:
            index = self.propertyCombo.findData(str(values["property"]))
            if index >= 0:
                self.propertyCombo.setCurrentIndex(index)
        if "mode" in values:
            index = self.modeCombo.findData(str(values["mode"]))
            if index >= 0:
                self.modeCombo.setCurrentIndex(index)
        if "lower" not in values and "upper" not in values:
            return
        current = self.bounds()
        lower = values.get("lower", current[0])
        upper = values.get("upper", current[1])
        if lower is None and upper is None:
            self._setBoundsActive(False)
            self.region.hide()
            return
        self._setBounds(
            -_BOUND_LIMIT if lower is None else float(lower),
            _BOUND_LIMIT if upper is None else float(upper),
        )
        self._setBoundsActive(True)
        if self._locs is not None:
            self.region.show()
        self._syncRegion()
        self._updateCount()


__all__ = ["PROPERTY_LABELS", "SmlmFilterWidget"]
