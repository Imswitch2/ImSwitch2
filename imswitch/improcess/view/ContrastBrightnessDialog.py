"""Brightness/contrast display-level dialog for ImProcess."""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from qtpy import QtCore, QtWidgets


class ContrastBrightnessDialog(QtWidgets.QDialog):
    """ImageJ-like display range editor for the active image layer."""

    sigLevelsChanged = QtCore.Signal(float, float)
    sigAutoRequested = QtCore.Signal(float)
    sigResetRequested = QtCore.Signal()

    _SLIDER_STEPS = 1000

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Brightness/Contrast")
        self.setModal(False)
        self._range = (0.0, 1.0)
        self._updating = False

        self.histogramPlot = pg.PlotWidget()
        self.histogramPlot.setMinimumHeight(140)
        self.histogramPlot.setLabel("bottom", "Intensity")
        self.histogramPlot.setLabel("left", "Count")
        self.histogramPlot.showGrid(x=True, y=True, alpha=0.2)

        self.minSpin = QtWidgets.QDoubleSpinBox()
        self.maxSpin = QtWidgets.QDoubleSpinBox()
        for spin in (self.minSpin, self.maxSpin):
            spin.setDecimals(6)
            spin.setRange(-1e30, 1e30)
            spin.setKeyboardTracking(False)

        self.minSlider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.maxSlider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        for slider in (self.minSlider, self.maxSlider):
            slider.setRange(0, self._SLIDER_STEPS)

        self.saturatedSpin = QtWidgets.QDoubleSpinBox()
        self.saturatedSpin.setDecimals(3)
        self.saturatedSpin.setRange(0.0, 100.0)
        self.saturatedSpin.setSingleStep(0.05)
        self.saturatedSpin.setSuffix(" %")
        self.saturatedSpin.setValue(0.35)

        self.scopeCombo = QtWidgets.QComboBox()
        self.scopeCombo.addItems(["Whole stack", "Current view"])
        self.scopeCombo.setToolTip("Histogram source for auto contrast")

        self.autoButton = QtWidgets.QPushButton("Auto")
        self.resetButton = QtWidgets.QPushButton("Reset")
        self.closeButton = QtWidgets.QPushButton("Close")

        form = QtWidgets.QFormLayout()
        form.addRow("Minimum", self.minSpin)
        form.addRow("", self.minSlider)
        form.addRow("Maximum", self.maxSpin)
        form.addRow("", self.maxSlider)
        form.addRow("Saturated", self.saturatedSpin)
        form.addRow("Histogram", self.scopeCombo)

        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self.autoButton)
        buttons.addWidget(self.resetButton)
        buttons.addStretch()
        buttons.addWidget(self.closeButton)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.histogramPlot)
        layout.addLayout(form)
        layout.addLayout(buttons)

        self.minSpin.valueChanged.connect(self._spinLevelsChanged)
        self.maxSpin.valueChanged.connect(self._spinLevelsChanged)
        self.minSlider.valueChanged.connect(self._minSliderChanged)
        self.maxSlider.valueChanged.connect(self._maxSliderChanged)
        self.autoButton.clicked.connect(
            lambda: self.sigAutoRequested.emit(float(self.saturatedSpin.value()))
        )
        self.resetButton.clicked.connect(self.sigResetRequested)
        self.closeButton.clicked.connect(self.close)

    def setDataRange(self, minimum: float, maximum: float) -> None:
        minimum, maximum = self._normalized(minimum, maximum)
        self._range = (minimum, maximum)
        self._updating = True
        try:
            self.minSpin.setRange(minimum, maximum)
            self.maxSpin.setRange(minimum, maximum)
            self._syncSlidersFromSpins()
        finally:
            self._updating = False

    def setLevels(self, minimum: float, maximum: float) -> None:
        minimum, maximum = self._normalized(minimum, maximum)
        self._updating = True
        try:
            self.minSpin.setValue(minimum)
            self.maxSpin.setValue(maximum)
            self._syncSlidersFromSpins()
        finally:
            self._updating = False

    def setHistogram(self, counts: np.ndarray, edges: np.ndarray) -> None:
        self.histogramPlot.clear()
        counts = np.asarray(counts)
        edges = np.asarray(edges, dtype=float)
        if counts.size == 0 or edges.size < 2:
            return
        centers = (edges[:-1] + edges[1:]) / 2.0
        self.histogramPlot.plot(
            centers,
            counts,
            stepMode=False,
            fillLevel=0,
            brush=(120, 160, 220, 80),
            pen=pg.mkPen((120, 160, 220), width=1),
        )

    def histogramScope(self) -> str:
        return "view" if self.scopeCombo.currentText() == "Current view" else "stack"

    def _spinLevelsChanged(self, *_args) -> None:
        if self._updating:
            return
        minimum, maximum = self._normalized(self.minSpin.value(), self.maxSpin.value())
        self._updating = True
        try:
            if self.minSpin.value() != minimum:
                self.minSpin.setValue(minimum)
            if self.maxSpin.value() != maximum:
                self.maxSpin.setValue(maximum)
            self._syncSlidersFromSpins()
        finally:
            self._updating = False
        self.sigLevelsChanged.emit(minimum, maximum)

    def _minSliderChanged(self, value: int) -> None:
        if self._updating:
            return
        self._updating = True
        try:
            self.minSpin.setValue(self._sliderToValue(value))
        finally:
            self._updating = False
        self._spinLevelsChanged()

    def _maxSliderChanged(self, value: int) -> None:
        if self._updating:
            return
        self._updating = True
        try:
            self.maxSpin.setValue(self._sliderToValue(value))
        finally:
            self._updating = False
        self._spinLevelsChanged()

    def _syncSlidersFromSpins(self) -> None:
        self.minSlider.setValue(self._valueToSlider(self.minSpin.value()))
        self.maxSlider.setValue(self._valueToSlider(self.maxSpin.value()))

    def _valueToSlider(self, value: float) -> int:
        minimum, maximum = self._range
        if maximum <= minimum:
            return 0
        fraction = (float(value) - minimum) / (maximum - minimum)
        return int(round(max(0.0, min(1.0, fraction)) * self._SLIDER_STEPS))

    def _sliderToValue(self, value: int) -> float:
        minimum, maximum = self._range
        fraction = max(0.0, min(1.0, float(value) / self._SLIDER_STEPS))
        return minimum + fraction * (maximum - minimum)

    @staticmethod
    def _normalized(minimum: float, maximum: float) -> tuple[float, float]:
        minimum = float(minimum)
        maximum = float(maximum)
        if not np.isfinite(minimum) or not np.isfinite(maximum):
            return 0.0, 1.0
        if minimum > maximum:
            minimum, maximum = maximum, minimum
        if minimum == maximum:
            pad = max(abs(minimum) * 0.005, 1.0)
            return minimum - pad, maximum + pad
        return minimum, maximum


__all__ = ["ContrastBrightnessDialog"]
