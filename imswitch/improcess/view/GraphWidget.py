"""Generic graph panel for ImProcess results."""

import numpy as np
import pyqtgraph as pg
from qtpy import QtCore, QtWidgets

from imswitch.improcess.model import PlotPayload, PlotSeries
from imswitch.improcess.model.plotting import build_graph_delta_x_record


class GraphWidget(QtWidgets.QWidget):
    """Display plot payloads published by ImProcess results."""

    sigResultPushed = QtCore.Signal(object, object)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._payloads: list[PlotPayload] = []
        # Payloads sent here deliberately (a pushed line profile, a table
        # plot) outlive the result they came from: the panel otherwise
        # re-renders from the current result on every switch, which is exactly
        # what makes comparing a curve from result A with one from result B
        # impossible.
        self._pinnedPayloads: list[PlotPayload] = []
        self._measurementRegion: pg.LinearRegionItem | None = None

        self.plotSelector = QtWidgets.QComboBox()
        self.plotSelector.currentIndexChanged.connect(self._plotSelected)

        self.overlayButton = QtWidgets.QPushButton("Overlay")
        self.overlayButton.setCheckable(True)
        self.overlayButton.setToolTip(
            "Draw every pushed curve in one plot instead of showing them one "
            "at a time — this is how two profiles get compared"
        )
        self.overlayButton.toggled.connect(lambda _checked: self._renderCurrent())

        self.unpinButton = QtWidgets.QPushButton("Clear pushed")
        self.unpinButton.setToolTip("Forget the curves pushed into this panel")
        self.unpinButton.clicked.connect(self.clearPinnedPayloads)

        self.measureButton = QtWidgets.QPushButton("Measure Δx")
        self.measureButton.setCheckable(True)
        self.measureButton.setToolTip(
            "Show two draggable vertical markers and measure their horizontal distance"
        )
        self.measureButton.toggled.connect(self._measurementToggled)

        self.pushMeasurementButton = QtWidgets.QPushButton("Push to table")
        self.pushMeasurementButton.setToolTip(
            "Append the current Δx measurement to the Results table for CSV export"
        )
        self.pushMeasurementButton.clicked.connect(self._pushMeasurement)

        self.measurementLabel = QtWidgets.QLabel("")
        self.measurementLabel.setToolTip(
            "Marker positions and their horizontal peak-to-peak distance"
        )

        toolbar = QtWidgets.QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.addWidget(self.plotSelector, 1)
        toolbar.addWidget(self.overlayButton, 0)
        toolbar.addWidget(self.unpinButton, 0)
        toolbar.addWidget(self.measureButton, 0)
        toolbar.addWidget(self.pushMeasurementButton, 0)
        toolbar.addWidget(self.measurementLabel, 0)

        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.addLegend()

        self.emptyLabel = QtWidgets.QLabel("No graph data for the selected result")
        self.emptyLabel.setAlignment(QtCore.Qt.AlignCenter)

        self.stack = QtWidgets.QStackedLayout()
        self.stack.addWidget(self.emptyLabel)
        self.stack.addWidget(self.plot)

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(toolbar)
        layout.addLayout(self.stack, 1)
        self.setLayout(layout)

        self.clear()

    def setPlotPayloads(self, payloads: list[PlotPayload]) -> None:
        """Replace the current result's plots, keeping the pushed ones."""
        self._payloads = list(payloads)
        self._rebuild(select=0 if payloads else None)

    def addPlotPayload(self, payload: PlotPayload) -> None:
        """Add a payload pushed here by another panel, and show it.

        Pushed payloads accumulate: pushing a line profile from one
        reconstruction and another from the next is how two curves get
        compared, so a later result switch must not throw the first away.
        A repeated title replaces its earlier version rather than growing the
        list with duplicates.
        """
        self._pinnedPayloads = [
            pinned for pinned in self._pinnedPayloads if pinned.title != payload.title
        ]
        self._pinnedPayloads.append(payload)
        self._rebuild(select=len(self._payloads) + len(self._pinnedPayloads) - 1)

    def clearPinnedPayloads(self) -> None:
        """Forget the pushed payloads, keeping the current result's own."""
        self._pinnedPayloads = []
        self._rebuild(select=0 if self._payloads else None)

    def pinnedPayloads(self) -> list[PlotPayload]:
        return list(self._pinnedPayloads)

    def clear(self) -> None:
        """Clear all graph data, pushed curves included."""
        self._payloads = []
        self._pinnedPayloads = []
        self._rebuild(select=None)

    def _allPayloads(self) -> list[PlotPayload]:
        return [*self._payloads, *self._pinnedPayloads]

    def _rebuild(self, *, select: int | None) -> None:
        payloads = self._allPayloads()
        self.plotSelector.blockSignals(True)
        self.plotSelector.clear()
        for index, payload in enumerate(payloads):
            pushed = index >= len(self._payloads)
            self.plotSelector.addItem(
                f"{payload.title} (pushed)" if pushed else payload.title
            )
        self.plotSelector.blockSignals(False)

        has_payloads = bool(payloads)
        self.plotSelector.setEnabled(has_payloads)
        self.measureButton.setEnabled(has_payloads)
        self.overlayButton.setEnabled(len(payloads) > 1)
        self.unpinButton.setEnabled(bool(self._pinnedPayloads))

        if not has_payloads:
            self.measureButton.setChecked(False)
            self.overlayButton.setChecked(False)
            self._showEmpty()
            return

        index = 0 if select is None else max(0, min(int(select), len(payloads) - 1))
        self.plotSelector.blockSignals(True)
        self.plotSelector.setCurrentIndex(index)
        self.plotSelector.blockSignals(False)
        self._renderCurrent()

    def _renderCurrent(self) -> None:
        payloads = self._allPayloads()
        if not payloads:
            self._showEmpty()
            return
        if self.overlayButton.isChecked() and len(payloads) > 1:
            self._renderOverlay(payloads)
            return
        index = max(0, min(self.plotSelector.currentIndex(), len(payloads) - 1))
        self._renderPayload(payloads[index])

    def _plotSelected(self, index: int) -> None:
        self._renderCurrent()

    def _showEmpty(self) -> None:
        self.plot.clear()
        self._measurementRegion = None
        self.measurementLabel.clear()
        self.pushMeasurementButton.setEnabled(False)
        self.stack.setCurrentWidget(self.emptyLabel)

    def _renderPayload(self, payload: PlotPayload) -> None:
        self.plot.clear()
        self.plot.setTitle(payload.title)
        self.plot.setLabel("bottom", payload.x_label)
        self.plot.setLabel("left", payload.y_label)

        for index, series in enumerate(payload.series):
            self._renderSeries(series, index)

        self._measurementRegion = None
        if self.measureButton.isChecked():
            self._addMeasurementRegion(payload)

        self.stack.setCurrentWidget(self.plot)

    def _renderOverlay(self, payloads: list[PlotPayload]) -> None:
        """Draw every payload in one plot, series names prefixed by payload.

        The axes come from the first payload; curves measured in different
        units can still be overlaid, which is the user's call to make, so
        this warns in the title rather than refusing.
        """
        self.plot.clear()
        first = payloads[0]
        units = {payload.x_label for payload in payloads}
        title = f"Overlay of {len(payloads)} plots"
        if len(units) > 1:
            title += " (mixed x units)"
        self.plot.setTitle(title)
        self.plot.setLabel("bottom", first.x_label)
        self.plot.setLabel("left", first.y_label)

        index = 0
        for payload in payloads:
            for series in payload.series:
                if series.kind == "image":
                    continue  # an image has no meaning in an overlay of curves
                self._renderSeries(
                    PlotSeries(
                        name=f"{payload.title}: {series.name}",
                        y=series.y,
                        x=series.x,
                        kind=series.kind,
                        style=series.style,
                    ),
                    index,
                )
                index += 1

        self._measurementRegion = None
        if self.measureButton.isChecked():
            self._addMeasurementRegion(first)
        self.stack.setCurrentWidget(self.plot)

    def _measurementToggled(self, enabled: bool) -> None:
        payload = self._currentPayload()
        if enabled and payload is not None:
            self._addMeasurementRegion(payload)
            return
        self._removeMeasurementRegion()

    def _addMeasurementRegion(self, payload: PlotPayload) -> None:
        self._removeMeasurementRegion()
        x_min, x_max = self._payloadXRange(payload)
        span = x_max - x_min
        region = pg.LinearRegionItem(
            values=(x_min + span / 3.0, x_min + 2.0 * span / 3.0),
            orientation="vertical",
            brush=pg.mkBrush(255, 215, 0, 45),
            pen=pg.mkPen(255, 190, 0, width=2),
            hoverBrush=pg.mkBrush(255, 215, 0, 75),
            hoverPen=pg.mkPen(255, 225, 80, width=2),
            swapMode="sort",
        )
        region.setZValue(20)
        region.sigRegionChanged.connect(self._measurementChanged)
        self.plot.addItem(region)
        self._measurementRegion = region
        self._measurementChanged()

    def _removeMeasurementRegion(self) -> None:
        region = self._measurementRegion
        self._measurementRegion = None
        if region is not None:
            try:
                self.plot.removeItem(region)
            except Exception:
                pass
        self.measurementLabel.clear()
        self.pushMeasurementButton.setEnabled(False)

    def _measurementChanged(self) -> None:
        region = self._measurementRegion
        if region is None:
            return
        x_1, x_2 = sorted(float(value) for value in region.getRegion())
        delta_x = x_2 - x_1
        self.measurementLabel.setText(
            f"x₁={x_1:.6g}  x₂={x_2:.6g}  Δx={delta_x:.6g}"
        )
        self.pushMeasurementButton.setEnabled(self._currentPayload() is not None)

    def _pushMeasurement(self) -> None:
        payload = self._currentPayload()
        region = self._measurementRegion
        if payload is None or region is None:
            return
        record = build_graph_delta_x_record(payload, *region.getRegion())
        self.sigResultPushed.emit(list(record.keys()), [record])

    def _currentPayload(self) -> PlotPayload | None:
        payloads = self._allPayloads()
        index = self.plotSelector.currentIndex()
        if 0 <= index < len(payloads):
            return payloads[index]
        return None

    @staticmethod
    def _payloadXRange(payload: PlotPayload) -> tuple[float, float]:
        """Return a finite horizontal range for initial measurement markers."""
        ranges = []
        for series in payload.series:
            if series.kind == "image":
                edges = np.asarray(series.style.get("x_edges", []), dtype=float)
                finite = edges[np.isfinite(edges)]
            elif series.kind == "histogram":
                values = np.asarray(series.y, dtype=float).ravel()
                finite = values[np.isfinite(values)]
            else:
                y = np.asarray(series.y)
                x = np.arange(y.size) if series.x is None else np.asarray(series.x)
                try:
                    x = np.asarray(x, dtype=float).ravel()
                except (TypeError, ValueError):
                    continue
                finite = x[np.isfinite(x)]
            if finite.size:
                ranges.append((float(np.min(finite)), float(np.max(finite))))

        if not ranges:
            return 0.0, 1.0
        x_min = min(start for start, _end in ranges)
        x_max = max(end for _start, end in ranges)
        if not np.isfinite(x_min) or not np.isfinite(x_max):
            return 0.0, 1.0
        if x_max <= x_min:
            padding = max(abs(x_min) * 0.5, 0.5)
            return x_min - padding, x_max + padding
        return x_min, x_max

    def _renderSeries(self, series: PlotSeries, index: int) -> None:
        y = np.asarray(series.y)
        x = np.arange(y.size) if series.x is None else np.asarray(series.x)
        pen = series.style.get("pen", pg.intColor(index))
        symbol = series.style.get("symbol", "o")

        if series.kind == "image":
            self._renderImage(series)
            return

        if series.kind == "histogram":
            bins = int(series.style.get("bins", min(50, max(10, int(np.sqrt(max(y.size, 1)))))))
            finite = y[np.isfinite(y)]
            if finite.size == 0:
                return
            counts, edges = np.histogram(finite, bins=bins)
            centers = 0.5 * (edges[:-1] + edges[1:])
            self.plot.plot(
                centers,
                counts,
                fillLevel=0,
                brush=series.style.get("brush", pg.intColor(index, alpha=80)),
                pen=pen,
                name=series.name,
            )
        elif series.kind == "scatter":
            self.plot.plot(
                x,
                y,
                pen=None,
                symbol=symbol,
                symbolBrush=series.style.get("symbolBrush", pg.intColor(index)),
                name=series.name,
            )
            self._renderErrorBars(series, x, y, index)
        else:
            self.plot.plot(x, y, pen=pen, name=series.name)
            self._renderErrorBars(series, x, y, index)

    def _renderImage(self, series: PlotSeries) -> None:
        """Render a 2D-histogram-style ``image`` series as a heatmap.

        ``series.y`` is the ``(n_x, n_y)`` count grid (col-major: y[i, j] is x-bin
        i, y-bin j). Bin edges in ``series.style`` map the grid into world
        coordinates so the heatmap lines up with the axes.
        """
        grid = np.asarray(series.y, dtype=float)
        if grid.ndim != 2 or grid.size == 0:
            return
        item = pg.ImageItem(grid)
        x_edges = np.asarray(series.style.get("x_edges", [0, grid.shape[0]]), dtype=float)
        y_edges = np.asarray(series.style.get("y_edges", [0, grid.shape[1]]), dtype=float)
        x0, x1 = float(x_edges[0]), float(x_edges[-1])
        y0, y1 = float(y_edges[0]), float(y_edges[-1])
        item.setRect(QtCore.QRectF(x0, y0, x1 - x0, y1 - y0))
        try:
            item.setColorMap(pg.colormap.get("viridis"))
        except Exception:
            pass
        self.plot.addItem(item)

    def _renderErrorBars(self, series: PlotSeries, x: np.ndarray, y: np.ndarray, index: int) -> None:
        y_err = series.style.get("y_err")
        if y_err is None:
            return
        err = np.asarray(y_err, dtype=float)
        x = np.asarray(x, dtype=float)
        y = np.asarray(y, dtype=float)
        valid = np.isfinite(x) & np.isfinite(y) & np.isfinite(err) & (err > 0)
        if not np.any(valid):
            return
        self.plot.addItem(
            pg.ErrorBarItem(
                x=x[valid],
                y=y[valid],
                height=2.0 * err[valid],
                pen=series.style.get("pen", pg.intColor(index)),
                beam=series.style.get("beam", 0.2),
            )
        )
