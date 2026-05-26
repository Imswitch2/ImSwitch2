import numpy as np
import pyqtgraph as pg
from qtpy import QtCore, QtWidgets

from .basewidgets import Widget


class FLIMHistWidget(Widget):
    """Histogram view for the FLIM detector with two modes:

    - "Lifetime dist." — histogram of per-pixel fitted lifetimes (ns).
      The red marker is the mean of the displayed distribution.
    - "Decay" — aggregated TCSPC photon-arrival histogram across all valid
      pixels. The red marker is the global single-τ fit using the
      currently selected fit_method on the manager.
    """

    sigShowToggled = QtCore.Signal(bool)
    sigAccumulateToggled = QtCore.Signal(bool)
    sigNBinsChanged = QtCore.Signal(int)
    sigRangeChanged = QtCore.Signal(float, float)
    sigModeChanged = QtCore.Signal(str)  # 'lifetime' or 'decay'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # ---- plot --------------------------------------------------------
        self._plot = pg.PlotWidget()
        self._plot.setLabel('bottom', 'Lifetime (ns)')
        self._plot.setLabel('left', 'Pixel count')
        self._plot.setTitle('FLIM — lifetime histogram')
        self._plot.showGrid(x=True, y=True, alpha=0.3)

        self._bars = pg.BarGraphItem(x=[], height=[], width=0.1,
                                     brush=pg.mkBrush(70, 130, 180, 200),
                                     pen=pg.mkPen(None))
        self._plot.addItem(self._bars)

        self._meanLine = pg.InfiniteLine(
            angle=90, movable=False,
            pen=pg.mkPen('r', width=1.5, style=QtCore.Qt.DashLine)
        )
        self._meanLine.setVisible(False)
        self._plot.addItem(self._meanLine)

        # ---- controls ----------------------------------------------------
        self._showCheck = QtWidgets.QCheckBox('Live update')
        self._showCheck.setChecked(True)

        self._accumCheck = QtWidgets.QCheckBox('Accumulate')
        self._accumCheck.setChecked(False)

        self._modeLabel = QtWidgets.QLabel('Mode:')
        self._modeCombo = QtWidgets.QComboBox()
        self._modeCombo.addItem('Lifetime dist.', userData='lifetime')
        self._modeCombo.addItem('Decay', userData='decay')

        self._nBinsLabel = QtWidgets.QLabel('Bins:')
        self._nBinsEdit = QtWidgets.QLineEdit('50')
        self._nBinsEdit.setFixedWidth(50)

        self._minLabel = QtWidgets.QLabel('Min (ns):')
        self._minEdit = QtWidgets.QLineEdit('0')
        self._minEdit.setFixedWidth(55)

        self._maxLabel = QtWidgets.QLabel('Max (ns):')
        self._maxEdit = QtWidgets.QLineEdit('20')
        self._maxEdit.setFixedWidth(55)

        self._statLabel = QtWidgets.QLabel('Valid pixels: —   Mean: — ns')
        self._statLabel.setStyleSheet('color: grey;')

        # ---- layout ------------------------------------------------------
        ctrl = QtWidgets.QHBoxLayout()
        ctrl.addWidget(self._showCheck)
        ctrl.addWidget(self._accumCheck)
        ctrl.addWidget(self._modeLabel)
        ctrl.addWidget(self._modeCombo)
        ctrl.addWidget(self._nBinsLabel)
        ctrl.addWidget(self._nBinsEdit)
        ctrl.addWidget(self._minLabel)
        ctrl.addWidget(self._minEdit)
        ctrl.addWidget(self._maxLabel)
        ctrl.addWidget(self._maxEdit)
        ctrl.addStretch()
        ctrl.addWidget(self._statLabel)

        vbox = QtWidgets.QVBoxLayout()
        self.setLayout(vbox)
        vbox.addWidget(self._plot, stretch=1)
        vbox.addLayout(ctrl)

        # ---- signal wiring -----------------------------------------------
        self._showCheck.toggled.connect(self.sigShowToggled)
        self._accumCheck.toggled.connect(self.sigAccumulateToggled)
        self._nBinsEdit.editingFinished.connect(self._onNBinsEdited)
        self._minEdit.editingFinished.connect(self._onRangeEdited)
        self._maxEdit.editingFinished.connect(self._onRangeEdited)
        self._modeCombo.currentIndexChanged.connect(self._onModeChanged)

    # ------------------------------------------------------------------ #
    # Getters used by controller                                           #
    # ------------------------------------------------------------------ #

    def isActive(self):
        return self._showCheck.isChecked()

    def isAccumulating(self):
        return self._accumCheck.isChecked()

    def getNBins(self):
        try:
            return max(1, int(self._nBinsEdit.text()))
        except ValueError:
            return 50

    def getMode(self) -> str:
        return str(self._modeCombo.currentData() or 'lifetime')

    def getRange(self):
        try:
            lo = float(self._minEdit.text())
            hi = float(self._maxEdit.text())
            return (lo, hi) if hi > lo else (0.0, 20.0)
        except ValueError:
            return 0.0, 20.0

    # ------------------------------------------------------------------ #
    # Update                                                               #
    # ------------------------------------------------------------------ #

    def updateHistogram(self, valid_lifetimes_ns: np.ndarray):
        """Redraw with a flat array of valid (> 0) lifetime values in ns."""
        self._plot.setLabel('bottom', 'Lifetime (ns)')
        self._plot.setLabel('left', 'Pixel count')
        self._plot.setTitle('FLIM — lifetime distribution')

        n = len(valid_lifetimes_ns)
        if n == 0:
            self._bars.setOpts(x=[], height=[], width=0.1)
            self._meanLine.setVisible(False)
            self._statLabel.setText('Valid pixels: 0   Mean: — ns')
            return

        lo, hi = self.getRange()
        n_bins = self.getNBins()
        counts, edges = np.histogram(valid_lifetimes_ns, bins=n_bins, range=(lo, hi))
        centers = 0.5 * (edges[:-1] + edges[1:])
        width = (edges[1] - edges[0]) * 0.85

        self._bars.setOpts(x=centers, height=counts.astype(float), width=width)
        self._plot.setXRange(lo, hi, padding=0.02)

        mean_ns = float(np.mean(valid_lifetimes_ns))
        self._meanLine.setValue(mean_ns)
        self._meanLine.setVisible(True)

        self._statLabel.setText(
            f'Valid pixels: {n}   Mean: {mean_ns:.2f} ns'
        )
        self._statLabel.setStyleSheet('')

    def updateDecay(self, t_axis_ns: np.ndarray, counts: np.ndarray,
                    global_tau_ns: float, n_valid_pixels: int):
        """Redraw with an aggregated TCSPC decay histogram."""
        self._plot.setLabel('bottom', 'Arrival time (ns)')
        self._plot.setLabel('left', 'Photon count')
        self._plot.setTitle('FLIM — aggregated decay')

        if t_axis_ns is None or counts is None or len(counts) == 0:
            self._bars.setOpts(x=[], height=[], width=0.1)
            self._meanLine.setVisible(False)
            self._statLabel.setText('No decay data')
            return

        # Bar width = bin spacing (assume uniform t_axis).
        if len(t_axis_ns) >= 2:
            width = float(t_axis_ns[1] - t_axis_ns[0]) * 0.85
        else:
            width = 0.1

        self._bars.setOpts(x=np.asarray(t_axis_ns, dtype=float),
                           height=np.asarray(counts, dtype=float),
                           width=width)
        self._plot.setXRange(float(t_axis_ns[0]), float(t_axis_ns[-1]), padding=0.02)

        if global_tau_ns > 0:
            self._meanLine.setValue(float(global_tau_ns))
            self._meanLine.setVisible(True)
            self._statLabel.setText(
                f'Valid pixels: {n_valid_pixels}   '
                f'Global τ: {global_tau_ns:.2f} ns   '
                f'Photons: {int(counts.sum())}'
            )
        else:
            self._meanLine.setVisible(False)
            self._statLabel.setText(
                f'Valid pixels: {n_valid_pixels}   '
                f'Global τ: —   Photons: {int(counts.sum())}'
            )
        self._statLabel.setStyleSheet('')

    # ------------------------------------------------------------------ #
    # Private                                                              #
    # ------------------------------------------------------------------ #

    def _onNBinsEdited(self):
        try:
            self.sigNBinsChanged.emit(max(1, int(self._nBinsEdit.text())))
        except ValueError:
            pass

    def _onModeChanged(self):
        self.sigModeChanged.emit(self.getMode())

    def _onRangeEdited(self):
        try:
            lo = float(self._minEdit.text())
            hi = float(self._maxEdit.text())
            if hi > lo:
                self.sigRangeChanged.emit(lo, hi)
        except ValueError:
            pass
