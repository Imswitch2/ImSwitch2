"""Generic graph panel for ImProcess results."""

import numpy as np
import pyqtgraph as pg
from qtpy import QtCore, QtWidgets

from imswitch.improcess.model import PlotPayload, PlotSeries


class GraphWidget(QtWidgets.QWidget):
    """Display plot payloads published by ImProcess results."""

    sigExportClicked = QtCore.Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._payloads: list[PlotPayload] = []

        self.plotSelector = QtWidgets.QComboBox()
        self.plotSelector.currentIndexChanged.connect(self._plotSelected)

        self.exportButton = QtWidgets.QPushButton("Export")
        self.exportButton.clicked.connect(self.sigExportClicked)

        toolbar = QtWidgets.QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.addWidget(self.plotSelector, 1)
        toolbar.addWidget(self.exportButton, 0)

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
        """Replace available plots and render the first one."""
        self._payloads = payloads

        self.plotSelector.blockSignals(True)
        self.plotSelector.clear()
        for payload in payloads:
            self.plotSelector.addItem(payload.title)
        self.plotSelector.blockSignals(False)

        has_payloads = len(payloads) > 0
        self.plotSelector.setEnabled(has_payloads)
        self.exportButton.setEnabled(has_payloads)

        if has_payloads:
            self.plotSelector.setCurrentIndex(0)
            self._renderPayload(payloads[0])
        else:
            self._showEmpty()

    def clear(self) -> None:
        """Clear all graph data."""
        self._payloads = []
        self.plotSelector.clear()
        self.plotSelector.setEnabled(False)
        self.exportButton.setEnabled(False)
        self._showEmpty()

    def _plotSelected(self, index: int) -> None:
        if 0 <= index < len(self._payloads):
            self._renderPayload(self._payloads[index])

    def _showEmpty(self) -> None:
        self.plot.clear()
        self.stack.setCurrentWidget(self.emptyLabel)

    def _renderPayload(self, payload: PlotPayload) -> None:
        self.plot.clear()
        self.plot.setTitle(payload.title)
        self.plot.setLabel("bottom", payload.x_label)
        self.plot.setLabel("left", payload.y_label)

        for index, series in enumerate(payload.series):
            self._renderSeries(series, index)

        self.stack.setCurrentWidget(self.plot)

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
