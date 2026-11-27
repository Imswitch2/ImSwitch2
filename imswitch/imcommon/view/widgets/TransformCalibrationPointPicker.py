"""Point picker for the *Manual point pairs* method of Transform Calibration.

The Moving and Reference images side by side (pyqtgraph, like the Inspect /
tune workspace); a click in one image adds a point to the open pair, a
click in the other completes it. Pairs are numbered in both images; once
the chosen model can be fitted, the transformed Moving points (×) and the
residual vectors are drawn on the Reference image and the table shows each
pair's residual and leave-one-out residual, so a bad click stands out.

The window only draws and forwards clicks: the pairs, the snapping, the fit
and the refusals are the owning :class:`TransformCalibrationWidget`'s (and,
below it, the Qt-free
:mod:`imswitch.imcommon.algorithms.transforms.correspondence.manual`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np
import pyqtgraph as pg
from qtpy import QtCore, QtGui, QtWidgets

if TYPE_CHECKING:
    from .TransformCalibrationWidget import TransformCalibrationWidget

__all__ = ["PointPickerDialog"]

_PAIR = "#55dd55"
_OPEN = "#ffcc33"
_WORST = "#ff6666"


class _ImagePane(QtWidgets.QWidget):
    """One image with the pair markers and numbers; emits clicks as (row, col)."""

    sigClicked = QtCore.Signal(float, float)

    def __init__(self, title: str, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.title = QtWidgets.QLabel(f"<b>{title}</b>")
        layout.addWidget(self.title)
        self.plot = pg.PlotWidget()
        self.plot.setAspectLocked(True)
        self.plot.getViewBox().invertY(True)
        self.plot.hideAxis("left")
        self.plot.hideAxis("bottom")
        self.plot.setMenuEnabled(False)
        self.image_item = pg.ImageItem(axisOrder="row-major")
        self.points = pg.ScatterPlotItem(symbol="o", size=13, pen=pg.mkPen(_PAIR, width=1.6), brush=None)
        self.open = pg.ScatterPlotItem(symbol="o", size=15, pen=pg.mkPen(_OPEN, width=2.0), brush=None)
        self.worst = pg.ScatterPlotItem(symbol="o", size=17, pen=pg.mkPen(_WORST, width=2.0), brush=None)
        self.mapped = pg.ScatterPlotItem(symbol="x", size=10, pen=pg.mkPen("#4c8bd8", width=1.5))
        self.residuals = pg.PlotDataItem(pen=pg.mkPen(_WORST, width=1.2))
        for item in (self.image_item, self.residuals, self.points, self.open, self.worst, self.mapped):
            self.plot.addItem(item)
        self._labels: list[pg.TextItem] = []
        self._shape: tuple[int, int] | None = None
        layout.addWidget(self.plot, 1)
        self.plot.scene().sigMouseClicked.connect(self._clicked)

    def setImage(self, image: np.ndarray | None) -> None:
        if image is None:
            self.image_item.clear()
            self._shape = None
            return
        image = np.asarray(image, dtype=np.float64)
        lo, hi = np.percentile(image, (1, 99.7))
        self.image_item.setImage(image, levels=(lo, hi if hi > lo else lo + 1))
        if self._shape != image.shape:
            self._shape = tuple(image.shape)
            self.plot.autoRange()

    def setPoints(self, points: list, open_index: int | None, worst: int | None) -> None:
        for label in self._labels:
            self.plot.removeItem(label)
        self._labels = []
        done = [(k, p) for k, p in enumerate(points) if p is not None and k != open_index]
        self.points.setData(x=[p[1] for _, p in done], y=[p[0] for _, p in done])
        pending = points[open_index] if open_index is not None and open_index < len(points) else None
        self.open.setData(x=[pending[1]] if pending else [], y=[pending[0]] if pending else [])
        bad = points[worst] if worst is not None and worst < len(points) and points[worst] is not None else None
        self.worst.setData(x=[bad[1]] if bad else [], y=[bad[0]] if bad else [])
        for k, point in enumerate(points):
            if point is None:
                continue
            label = pg.TextItem(str(k + 1), color=_OPEN if k == open_index else "#ffff66", anchor=(-0.3, 1.2))
            label.setPos(point[1], point[0])
            self.plot.addItem(label)
            self._labels.append(label)

    def setFit(self, mapped: np.ndarray | None, targets: np.ndarray | None) -> None:
        if mapped is None or targets is None or len(mapped) == 0:
            self.mapped.setData([], [])
            self.residuals.setData([], [])
            return
        self.mapped.setData(x=mapped[:, 1], y=mapped[:, 0])
        xs, ys = [], []
        for target, predicted in zip(targets, mapped):
            xs += [target[1], predicted[1], np.nan]
            ys += [target[0], predicted[0], np.nan]
        self.residuals.setData(xs, ys)

    def scenePositionOf(self, row: float, col: float) -> QtCore.QPointF:
        return self.plot.getViewBox().mapViewToScene(QtCore.QPointF(float(col), float(row)))

    def widgetPositionOf(self, row: float, col: float) -> QtCore.QPoint:
        """Where ``(row, col)`` is on the plot's viewport (to click it)."""
        return self.plot.mapFromScene(self.scenePositionOf(row, col))

    def _clicked(self, event: Any) -> None:
        if event.button() != QtCore.Qt.LeftButton or event.double() or self._shape is None:
            return
        view = self.plot.getViewBox()
        if not view.sceneBoundingRect().contains(event.scenePos()):
            return
        point = view.mapSceneToView(event.scenePos())
        row, col = float(point.y()), float(point.x())
        if -0.5 <= row < self._shape[0] - 0.5 and -0.5 <= col < self._shape[1] - 0.5:
            event.accept()
            self.sigClicked.emit(row, col)


class PointPickerDialog(QtWidgets.QDialog):
    """Click the same features in both images; see the module docstring."""

    COLUMNS = ("#", "Moving (row, col)", "Reference (row, col)", "Residual", "Leave-one-out")

    def __init__(self, owner: "TransformCalibrationWidget") -> None:
        super().__init__(owner)
        self.owner = owner
        self.setWindowTitle("Transform Calibration — Pick point pairs")
        self.setWindowModality(QtCore.Qt.NonModal)
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowMinimizeButtonHint
                            | QtCore.Qt.WindowMaximizeButtonHint)
        self.resize(1200, 780)
        self.setMinimumSize(760, 520)

        self.instructions = QtWidgets.QLabel("")
        self.instructions.setWordWrap(True)
        self.moving = _ImagePane("Moving")
        self.reference = _ImagePane("Reference")
        self.moving.sigClicked.connect(lambda r, c: self.owner.addManualPoint("source", (r, c)))
        self.reference.sigClicked.connect(lambda r, c: self.owner.addManualPoint("target", (r, c)))
        images = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        images.addWidget(self.moving)
        images.addWidget(self.reference)
        images.setSizes([600, 600])

        self.table = QtWidgets.QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        self.table.setMinimumHeight(150)
        self.table.horizontalHeaderItem(4).setToolTip(
            "Where this pair's Moving point lands when the transform is fitted to the OTHER pairs, "
            "against where it was clicked: a bad click stands out here (red).")

        self.snapCheck = QtWidgets.QCheckBox("Snap to spot centre within")
        self.snapCheck.setToolTip("Move each click onto the brightness centre of the spot under it.")
        self.snapRadius = QtWidgets.QDoubleSpinBox()
        self.snapRadius.setRange(0.5, 50.0)
        self.snapRadius.setDecimals(1)
        self.snapRadius.setSuffix(" px")
        self.deleteButton = QtWidgets.QPushButton("Delete")
        self.upButton = QtWidgets.QPushButton("Up")
        self.downButton = QtWidgets.QPushButton("Down")
        self.undoButton = QtWidgets.QPushButton("Undo")
        self.undoButton.setShortcut(QtGui.QKeySequence.Undo)
        self.clearButton = QtWidgets.QPushButton("Clear all")
        self.status = QtWidgets.QLabel("")
        self.status.setWordWrap(True)
        close = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        close.rejected.connect(self.hide)

        tools = QtWidgets.QHBoxLayout()
        tools.addWidget(self.snapCheck)
        tools.addWidget(self.snapRadius)
        tools.addStretch(1)
        for button in (self.deleteButton, self.upButton, self.downButton, self.undoButton, self.clearButton):
            tools.addWidget(button)
        lower = QtWidgets.QWidget()
        lower_layout = QtWidgets.QVBoxLayout(lower)
        lower_layout.setContentsMargins(0, 0, 0, 0)
        lower_layout.addLayout(tools)
        lower_layout.addWidget(self.table, 1)
        lower_layout.addWidget(self.status)
        split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        split.addWidget(images)
        split.addWidget(lower)
        split.setSizes([500, 260])
        root = QtWidgets.QVBoxLayout(self)
        root.addWidget(self.instructions)
        root.addWidget(split, 1)
        root.addWidget(close)

        self.snapCheck.toggled.connect(self._snapChanged)
        self.snapRadius.valueChanged.connect(self._snapChanged)
        self.deleteButton.clicked.connect(self._delete)
        self.upButton.clicked.connect(lambda: self._move(-1))
        self.downButton.clicked.connect(lambda: self._move(+1))
        self.undoButton.clicked.connect(lambda: self.owner.undoManualPairs())
        self.clearButton.clicked.connect(lambda: self.owner.clearManualPairs())
        self.table.itemSelectionChanged.connect(self._updateButtons)
        owner.sigStateChanged.connect(self.refreshFromState)
        self._images: tuple[int, int] | None = None
        self.refreshFromState()

    # -- state -> view -------------------------------------------------------
    def refreshFromState(self) -> None:
        owner = self.owner
        enabled, radius = owner.snapSettings()
        for widget, value in ((self.snapCheck, enabled), (self.snapRadius, radius)):
            widget.blockSignals(True)
            widget.setChecked(value) if widget is self.snapCheck else widget.setValue(value)
            widget.blockSignals(False)
        self.snapRadius.setEnabled(enabled)
        source, target = owner.image("source"), owner.image("target")
        key = (id(source), id(target))
        if key != self._images:
            self._images = key
            self.moving.setImage(source)
            self.reference.setImage(target)
        editor = owner.pairEditor
        pair_fit = owner.manualPairFit
        worst = None
        _, _, complete = editor.complete_pairs()
        suspicious = pair_fit.suspicious() if pair_fit is not None else None
        if suspicious is not None and suspicious < len(complete):
            worst = int(complete[suspicious])
        open_index = editor.open_pair
        self.moving.setPoints([p.source for p in editor.pairs], open_index, worst)
        self.reference.setPoints([p.target for p in editor.pairs], open_index, worst)
        mapped = owner.mappedReferencePixels() if pair_fit is not None else None
        result = owner.correspondenceResult
        self.reference.setFit(mapped, None if result is None else result.target_points)
        self._fillTable(editor, pair_fit, complete, worst)
        role = editor.next_role()
        which = "Moving" if role == "source" else "Reference"
        if open_index is None:
            text = (f"Click a feature in either image to start pair {len(editor) + 1} "
                    "(Moving first is usual), then the same feature in the other image.")
        else:
            text = f"Pair {open_index + 1}: now click the same feature in the <b>{which}</b> image."
        self.instructions.setText(text + " Scroll to zoom, drag to pan.")
        self.status.setText(owner.manualStatusLabel.text().replace("\n", " — "))
        self._updateButtons()

    def _fillTable(self, editor: Any, pair_fit: Any, complete: np.ndarray, worst: int | None) -> None:
        unit = pair_fit.fit.residuals.unit if pair_fit is not None else "px"
        position = {int(k): i for i, k in enumerate(complete)}
        selected = self.selectedPair()
        self.table.setRowCount(len(editor.pairs))
        for k, pair in enumerate(editor.pairs):
            cells = [str(k + 1), _point_text(pair.source), _point_text(pair.target), "", ""]
            if pair_fit is not None and k in position:
                i = position[k]
                cells[3] = _length(pair_fit.residuals[i], unit)
                if pair_fit.loo is not None and np.isfinite(pair_fit.loo[i]):
                    cells[4] = _length(pair_fit.loo[i], unit)
            for column, text in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(text)
                if k == worst:
                    item.setForeground(QtGui.QBrush(QtGui.QColor(_WORST)))
                elif not pair.complete:
                    item.setForeground(QtGui.QBrush(QtGui.QColor("#b08000")))
                self.table.setItem(k, column, item)
        if selected is not None and selected < len(editor.pairs):
            self.table.selectRow(selected)

    def selectedPair(self) -> int | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return rows[0].row() if rows else None

    def _updateButtons(self) -> None:
        selected = self.selectedPair()
        count = len(self.owner.pairEditor)
        self.deleteButton.setEnabled(selected is not None)
        self.upButton.setEnabled(selected is not None and selected > 0)
        self.downButton.setEnabled(selected is not None and selected < count - 1)
        self.undoButton.setEnabled(self.owner.pairEditor.can_undo)
        self.clearButton.setEnabled(count > 0)

    # -- view -> owner -------------------------------------------------------
    def _snapChanged(self, *_args: Any) -> None:
        self.owner.setSnap(self.snapCheck.isChecked(), self.snapRadius.value())

    def _delete(self) -> None:
        selected = self.selectedPair()
        if selected is not None:
            self.owner.deleteManualPair(selected)

    def _move(self, offset: int) -> None:
        selected = self.selectedPair()
        if selected is not None:
            new = self.owner.moveManualPair(selected, offset)
            self.table.selectRow(new)


def _point_text(point: Any) -> str:
    return "—" if point is None else f"{point[0]:.2f}, {point[1]:.2f}"


def _length(value: float, unit: str) -> str:
    if unit in ("um", "µm"):
        return f"{value * 1000:.0f} nm"
    return f"{value:.2f} {unit}"
