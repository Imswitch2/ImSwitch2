"""The canvas widget: zoom, pan, overlays, and drops from the palette.

Wheel zooms around the pointer; the middle button, or Space with the left
button, pans; a left drag on empty canvas selects with a rubber band. The
kind legend sits bottom left, the zoom / fit / tidy controls bottom right,
and an empty canvas says how to begin.
"""

from __future__ import annotations

import math

from qtpy import QtCore, QtGui, QtWidgets

from . import style as st
from .items import EdgeItem, NodeItem

#: What the palette puts on a drag: "<step kind>\n<plugin id>".
STEP_MIME = "application/x-imswitch-workflow-step"

_OVERLAY_STYLE = (
    "QFrame#canvasOverlay { background: rgba(23,33,43,230); border: 1px solid #2a3846; border-radius: 9px; }"
    "QFrame#canvasOverlay QLabel { color: #c9d1d9; background: transparent; font-size: 8.5pt; }"
    "QFrame#canvasOverlay QToolButton { color: #dfe4ea; background: transparent; border: none;"
    " padding: 2px 6px; font-size: 9pt; }"
    "QFrame#canvasOverlay QToolButton:hover { background: #2a3846; border-radius: 5px; }"
)


class CanvasView(QtWidgets.QGraphicsView):
    """See the module docstring."""

    #: step kind, plugin id, scene position (a palette drop)
    sigDropStep = QtCore.Signal(str, str, QtCore.QPointF)
    #: local file paths dropped, scene position
    sigDropFiles = QtCore.Signal(list, QtCore.QPointF)
    #: scene position (Tab, or a double-click on empty canvas)
    sigQuickAddHere = QtCore.Signal(QtCore.QPointF)
    #: what was right-clicked (a NodeItem, an EdgeItem or None), scene and screen positions
    sigContextMenu = QtCore.Signal(object, QtCore.QPointF, QtCore.QPoint)
    sigTidyRequested = QtCore.Signal()
    sigZoomChanged = QtCore.Signal(float)

    MIN_ZOOM, MAX_ZOOM = 0.2, 3.0

    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self.setRenderHints(QtGui.QPainter.Antialiasing | QtGui.QPainter.SmoothPixmapTransform
                            | QtGui.QPainter.TextAntialiasing)
        self.setViewportUpdateMode(QtWidgets.QGraphicsView.FullViewportUpdate)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setDragMode(QtWidgets.QGraphicsView.RubberBandDrag)
        self.setTransformationAnchor(QtWidgets.QGraphicsView.AnchorUnderMouse)
        self.setAcceptDrops(True)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self._panning = None
        self._space = False
        self._build_overlays()

    # -- overlays ----------------------------------------------------------------------

    def _build_overlays(self) -> None:
        self.setStyleSheet(_OVERLAY_STYLE)
        self._legend = QtWidgets.QFrame(self)
        self._legend.setObjectName("canvasOverlay")
        legend = QtWidgets.QHBoxLayout(self._legend)
        legend.setContentsMargins(10, 4, 10, 4)
        legend.setSpacing(10)
        for kind in st.LEGEND_KINDS:
            label = QtWidgets.QLabel(
                f"<span style='color:{st.KIND_COLOURS[kind]}; font-size:11pt'>●</span>&nbsp;{st.KIND_WORDS[kind]}")
            label.setTextFormat(QtCore.Qt.RichText)
            legend.addWidget(label)
        self._legend.setToolTip("What each cable carries")

        self._controls = QtWidgets.QFrame(self)
        self._controls.setObjectName("canvasOverlay")
        controls = QtWidgets.QHBoxLayout(self._controls)
        controls.setContentsMargins(6, 2, 6, 2)
        controls.setSpacing(2)

        def button(text, tip, slot):
            widget = QtWidgets.QToolButton()
            widget.setText(text)
            widget.setToolTip(tip)
            widget.clicked.connect(lambda _checked=False: slot())
            controls.addWidget(widget)
            return widget

        button("−", "Zoom out (Ctrl+−)", lambda: self.zoom_by(1 / 1.2))
        self._zoom_button = button("100%", "Actual size", self.reset_zoom)
        button("+", "Zoom in (Ctrl++)", lambda: self.zoom_by(1.2))
        button("⊡ Fit", "Show everything (F)", self.fit)
        button("⇱ Tidy up", "Lay the boxes out left to right (H)", self.sigTidyRequested.emit)

        self._hint = QtWidgets.QLabel(self)
        self._hint.setTextFormat(QtCore.Qt.RichText)
        self._hint.setAlignment(QtCore.Qt.AlignCenter)
        self._hint.setStyleSheet("color: #93a0ad; background: transparent; font-size: 11pt;")
        self._hint.setText(
            "<b style='color:#e3e6ea'>An empty workflow</b><br><br>"
            "Drag a step here from the list on the left,<br>"
            "or double-click / press <b>Tab</b> to search for one.<br><br>"
            "Then drag from a box's right side to another box's left side to connect them."
        )
        self._hint.hide()
        self._place_overlays()

    def set_empty_hint(self, visible: bool) -> None:
        self._hint.setVisible(bool(visible))
        self._place_overlays()

    def _place_overlays(self) -> None:
        for widget in (self._legend, self._controls, self._hint):
            widget.adjustSize()
        self._legend.move(14, self.height() - self._legend.height() - 14)
        self._controls.move(self.width() - self._controls.width() - 14, self.height() - self._controls.height() - 14)
        self._hint.move((self.width() - self._hint.width()) // 2, (self.height() - self._hint.height()) // 2)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place_overlays()

    # -- zoom and pan ------------------------------------------------------------------

    @property
    def zoom(self) -> float:
        return float(self.transform().m11())

    def zoom_by(self, factor: float, anchor: QtCore.QPointF | None = None) -> None:
        target = min(self.MAX_ZOOM, max(self.MIN_ZOOM, self.zoom * factor))
        factor = target / self.zoom if self.zoom else 1.0
        if abs(factor - 1.0) < 1e-6:
            return
        if anchor is None:
            self.setTransformationAnchor(QtWidgets.QGraphicsView.AnchorViewCenter)
        self.scale(factor, factor)
        self.setTransformationAnchor(QtWidgets.QGraphicsView.AnchorUnderMouse)
        self._zoom_changed()

    def reset_zoom(self) -> None:
        self.zoom_by(1.0 / self.zoom if self.zoom else 1.0)

    def _zoom_changed(self) -> None:
        self._zoom_button.setText(f"{self.zoom * 100:.0f}%")
        self._place_overlays()
        self.sigZoomChanged.emit(self.zoom)

    def items_rect(self) -> QtCore.QRectF:
        rect = QtCore.QRectF()
        for item in self.scene().items():
            if isinstance(item, NodeItem):
                rect = rect.united(item.sceneBoundingRect())
        return rect

    def fit(self) -> None:
        rect = self.items_rect()
        if rect.isNull():
            self.resetTransform()
            self.centerOn(0, 0)
            self._zoom_changed()
            return
        rect = rect.adjusted(-60, -60, 60, 90)
        self.fitInView(rect, QtCore.Qt.KeepAspectRatio)
        if self.zoom > 1.0:
            self.resetTransform()
            self.centerOn(rect.center())
        elif self.zoom < self.MIN_ZOOM:
            factor = self.MIN_ZOOM / self.zoom
            self.scale(factor, factor)
        self._zoom_changed()

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if delta == 0:
            super().wheelEvent(event)
            return
        self.zoom_by(math.pow(1.0015, delta), anchor=QtCore.QPointF(event.pos()) if hasattr(event, "pos") else None)
        event.accept()

    def keyPressEvent(self, event) -> None:
        if event.key() == QtCore.Qt.Key_Space and not event.isAutoRepeat():
            self._space = True
            self.viewport().setCursor(QtCore.Qt.OpenHandCursor)
            event.accept()
            return
        if event.key() == QtCore.Qt.Key_Tab:
            self.sigQuickAddHere.emit(self.mapToScene(self.mapFromGlobal(QtGui.QCursor.pos())))
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:
        if event.key() == QtCore.Qt.Key_Space and not event.isAutoRepeat():
            self._space = False
            self.viewport().unsetCursor()
            event.accept()
            return
        super().keyReleaseEvent(event)

    def focusNextPrevChild(self, _next: bool) -> bool:
        return False        # Tab opens the quick-add picker here

    def mousePressEvent(self, event) -> None:
        if event.button() == QtCore.Qt.MiddleButton or (event.button() == QtCore.Qt.LeftButton and self._space):
            self._panning = event.pos()
            self.viewport().setCursor(QtCore.Qt.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._panning is not None:
            delta = event.pos() - self._panning
            self._panning = event.pos()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._panning is not None:
            self._panning = None
            if self._space:
                self.viewport().setCursor(QtCore.Qt.OpenHandCursor)
            else:
                self.viewport().unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        pos = self.mapToScene(event.pos())
        if self.scene().node_at(pos) is None and self.scene().edge_at(pos) is None:
            self.sigQuickAddHere.emit(pos)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:
        pos = self.mapToScene(event.pos())
        target = self.scene().node_at(pos) or self.scene().edge_at(pos)
        if isinstance(target, NodeItem) and not target.isSelected():
            self.scene().select_steps([target.step_id])
        elif isinstance(target, EdgeItem):
            for item in self.scene().selectedItems():
                item.setSelected(False)
            target.setSelected(True)
        self.sigContextMenu.emit(target, pos, event.globalPos())
        event.accept()

    def drawBackground(self, painter, rect) -> None:
        painter.fillRect(rect, st.colour(st.CANVAS_BG))
        if self.zoom < 0.45:
            return
        step = 26
        left = int(math.floor(rect.left() / step)) * step
        top = int(math.floor(rect.top() / step)) * step
        painter.setPen(QtGui.QPen(st.colour(st.GRID_DOT), 1.6))
        points = []
        y = top
        while y < rect.bottom():
            x = left
            while x < rect.right():
                points.append(QtCore.QPointF(x, y))
                x += step
            y += step
        if points:
            painter.drawPoints(QtGui.QPolygonF(points))

    # -- drops ---------------------------------------------------------------------------

    def dragEnterEvent(self, event) -> None:
        mime = event.mimeData()
        if mime.hasFormat(STEP_MIME) or mime.hasUrls():
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        mime = event.mimeData()
        if mime.hasFormat(STEP_MIME) or mime.hasUrls():
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        mime = event.mimeData()
        pos = self.mapToScene(event.pos())
        if mime.hasFormat(STEP_MIME):
            kind, _newline, plugin = bytes(mime.data(STEP_MIME)).decode("utf-8").partition("\n")
            self.sigDropStep.emit(kind, plugin, pos)
            event.acceptProposedAction()
            return
        if mime.hasUrls():
            paths = [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]
            if paths:
                self.sigDropFiles.emit(paths, pos)
                event.acceptProposedAction()
                return
        super().dropEvent(event)


__all__ = ["CanvasView", "STEP_MIME"]
