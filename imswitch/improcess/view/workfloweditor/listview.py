"""List mode: the same workflow as a pipeline of cards, in run order.

One card per step -- its colour, name and id, what it sets (or a Python
step's first lines), what it reads, its outputs as chips in their kinds'
colours, how long it took and a thumbnail -- and, in the gutter on the left,
who feeds whom drawn like a git graph. Selection, the inspector and undo are
the window's, shared with the graph; this only draws.
"""

from __future__ import annotations

from qtpy import QtCore, QtGui, QtWidgets

from imswitch.improcess.model.workfloweditor.nodes import node_infos

from .canvas import style as st
from .canvas.items import _BADGE_COLOURS, _BADGE_GLYPHS, _elide

CARD_H = 96
CARD_GAP = 12
GUTTER = 64
LANE_W = 16
THUMB_W, THUMB_H = 132, 76


class CardItem(QtWidgets.QGraphicsItem):
    """One step as a row."""

    def __init__(self, info, run, lane: int, width: float):
        super().__init__()
        self.info = info
        self.run = run
        self.lane = lane
        self.width = width
        self.setFlags(QtWidgets.QGraphicsItem.ItemIsSelectable)
        self.setAcceptHoverEvents(True)
        self.setToolTip("\n".join([f"{info.title} ({info.id})", info.description, *(f"⚠ {i}" for i in info.issues)]))
        self.height = CARD_H + (26 if run is not None and run.error else 0)

    @property
    def step_id(self) -> str:
        return self.info.id

    def dot(self) -> QtCore.QPointF:
        return self.mapToScene(QtCore.QPointF(-GUTTER + 18 + self.lane * LANE_W, CARD_H / 2))

    def boundingRect(self) -> QtCore.QRectF:
        return QtCore.QRectF(-GUTTER, -6, self.width + GUTTER + 8, self.height + 12)

    def paint(self, painter, option, widget=None) -> None:
        info, run = self.info, self.run
        status = run.status if run is not None else ""
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        rect = QtCore.QRectF(0, 0, self.width, self.height)
        painter.setPen(QtCore.Qt.NoPen)
        for i, alpha in enumerate((30, 16, 8)):
            painter.setBrush(QtGui.QColor(0, 0, 0, alpha))
            painter.drawRoundedRect(rect.adjusted(-i, -i + 3, i, i + 5), 9 + i, 9 + i)
        painter.setOpacity(0.55 if status == "waiting" else 1.0)
        body = QtGui.QPainterPath()
        body.addRoundedRect(rect, 9, 9)
        painter.fillPath(body, st.colour(st.NODE_BODY))
        bar = QtGui.QPainterPath()
        bar.addRoundedRect(QtCore.QRectF(0, 0, 14, self.height), 9, 9)
        clip = QtGui.QPainterPath()
        clip.addRect(QtCore.QRectF(0, 0, 7, self.height))
        painter.fillPath(bar.intersected(clip), st.step_colour(info.step_kind, info.category))

        right = self.width - (THUMB_W + 24 if run is not None and run.thumbnail is not None else 16)
        middle = max(240.0, min(right * 0.5, 360.0))
        painter.setPen(st.colour(st.TEXT))
        painter.setFont(st.font(10.2, bold=True))
        painter.drawText(QtCore.QRectF(22, 9, middle - 30, 20), QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
                         _elide(painter, info.title, middle - 30))
        painter.setFont(st.font(8.0, mono=True))
        painter.setPen(st.colour(st.TEXT_DIM))
        painter.drawText(QtCore.QRectF(22, 30, middle - 30, 15), QtCore.Qt.AlignLeft, info.id)
        lines = list(info.code[:2]) or list((run.summary if run is not None and run.summary else info.summary)[:2])
        painter.setFont(st.font(7.8, mono=True) if info.code else st.font(8.6))
        painter.setPen(st.colour(st.CODE_TEXT) if info.code else QtGui.QColor("#c9d1d9"))
        for i, line in enumerate(lines):
            painter.drawText(QtCore.QRectF(22, 50 + 17 * i, middle - 30, 16), QtCore.Qt.AlignLeft,
                             _elide(painter, line, middle - 30))

        x = middle
        reads = ", ".join(str(slot.ref) for slot in info.inputs if slot.ref is not None)
        painter.setFont(st.font(8.2))
        painter.setPen(st.colour(st.TEXT_DIM))
        if reads:
            painter.drawText(QtCore.QRectF(x, 9, right - x, 16), QtCore.Qt.AlignLeft, _elide(painter, "← " + reads, right - x))
        elif any(slot.required for slot in info.inputs):
            painter.setPen(st.colour(st.ERROR))
            painter.drawText(QtCore.QRectF(x, 9, right - x, 16), QtCore.Qt.AlignLeft, "← not connected")
        chip_x = x
        painter.setFont(st.font(7.8, bold=True))
        for slot in info.outputs:
            if slot.placeholder:
                continue
            kind = run.kinds.get(slot.port, slot.kind) if run is not None else slot.kind
            colour = st.kind_colour(kind)
            label = f"{slot.port} · {st.KIND_WORDS.get(kind, kind)}"
            width = QtGui.QFontMetrics(painter.font()).horizontalAdvance(label) + 16
            if chip_x + width > right:
                break
            chip = QtCore.QRectF(chip_x, 34, width, 19)
            fill = QtGui.QColor(colour)
            fill.setAlpha(38)
            painter.setPen(QtGui.QPen(colour, 1))
            painter.setBrush(fill)
            painter.drawRoundedRect(chip, 9, 9)
            painter.setPen(colour.lighter(120))
            painter.drawText(chip, QtCore.Qt.AlignCenter, label)
            chip_x += width + 6
        if run is not None and run.footer:
            painter.setFont(st.font(7.8))
            painter.setPen(st.colour(st.TEXT_DIM))
            painter.drawText(QtCore.QRectF(x, 62, right - x, 15), QtCore.Qt.AlignLeft, _elide(painter, run.footer, right - x))
        elif status == "waiting":
            painter.setFont(st.font(8.2))
            painter.setPen(st.colour(st.TEXT_DIM))
            painter.drawText(QtCore.QRectF(x, 62, right - x, 15), QtCore.Qt.AlignLeft, "waits for the steps before it")
        if run is not None and run.thumbnail is not None:
            trect = QtCore.QRectF(self.width - THUMB_W - 12, 10, THUMB_W, THUMB_H)
            scaled = run.thumbnail.scaled(trect.size().toSize(), QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
            painter.fillRect(trect, QtGui.QColor("#0c1218"))
            painter.drawPixmap(QtCore.QPointF(trect.x() + (trect.width() - scaled.width()) / 2,
                                              trect.y() + (trect.height() - scaled.height()) / 2), scaled)
            if status == "outdated":
                painter.fillRect(trect, QtGui.QColor(17, 25, 33, 150))
                painter.setPen(st.colour(st.OUTDATED))
                painter.setFont(st.font(8.4, bold=True))
                painter.drawText(trect, QtCore.Qt.AlignCenter, "outdated")
        border, width = st.colour(st.NODE_BORDER), 1.2
        if info.issues:
            border, width = QtGui.QColor(st.ERROR).darker(115), 1.6
        if status == "failed":
            border, width = st.colour(st.ERROR), 2.0
        elif status == "outdated":
            border, width = st.colour(st.OUTDATED), 1.6
        if self.isSelected():
            border, width = st.colour(st.NODE_SELECTED), 2.2
        painter.setPen(QtGui.QPen(border, width))
        painter.setBrush(QtCore.Qt.NoBrush)
        painter.drawRoundedRect(rect, 9, 9)
        if run is not None and run.error:
            strip = QtCore.QRectF(16, CARD_H - 6, self.width - 32, 26)
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(QtGui.QColor(70, 22, 24, 235))
            painter.drawRoundedRect(strip, 5, 5)
            painter.setPen(QtGui.QColor("#ffd6d4"))
            painter.setFont(st.font(8.4))
            painter.drawText(strip.adjusted(10, 0, -10, 0), QtCore.Qt.AlignVCenter,
                             _elide(painter, "✕  " + run.error, strip.width() - 20))
        painter.setOpacity(1.0)
        centre = QtCore.QPointF(-GUTTER + 18 + self.lane * LANE_W, CARD_H / 2)
        colour = st.colour(_BADGE_COLOURS.get(status, "#4a5a6a")) if status else QtGui.QColor("#4a5a6a")
        painter.setPen(QtGui.QPen(QtGui.QColor("#0b1117"), 3))
        painter.setBrush(colour)
        painter.drawEllipse(centre, 9, 9)
        glyph = _BADGE_GLYPHS.get(status, "")
        if glyph:
            painter.setPen(QtGui.QColor("#ffffff") if status == "failed" else QtGui.QColor("#0d1419"))
            painter.setFont(st.font(8.8, bold=True))
            painter.drawText(QtCore.QRectF(centre.x() - 9, centre.y() - 9.5, 18, 18), QtCore.Qt.AlignCenter, glyph)

    def mouseDoubleClickEvent(self, event) -> None:
        view = self.scene().views()[0] if self.scene() is not None and self.scene().views() else None
        if isinstance(view, StepListView):
            view.sigActivated.emit(self.step_id)
        event.accept()


def lanes(document) -> dict[str, int]:
    """A lane per step for the gutter: a step continues its first input's lane
    unless an earlier step already continued it, then it opens the next one."""
    from imswitch.improcess.model.workfloweditor.graph import refs_of

    lane_of: dict[str, int] = {}
    taken: dict[str, int] = {}
    for step in document.steps:
        refs = refs_of(step)
        if refs and refs[0].step in lane_of:
            producer = refs[0].step
            lane_of[step.id] = lane_of[producer] + taken.get(producer, 0)
            taken[producer] = taken.get(producer, 0) + 1
        else:
            lane_of[step.id] = 0
    return {key: min(value, 2) for key, value in lane_of.items()}


class StepListView(QtWidgets.QGraphicsView):
    """See the module docstring. The window calls :meth:`refresh`."""

    sigStepSelected = QtCore.Signal(str)        # "" when nothing is selected
    sigActivated = QtCore.Signal(str)
    sigContextMenu = QtCore.Signal(str, QtCore.QPoint)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QtWidgets.QGraphicsScene(self))
        self.setRenderHints(QtGui.QPainter.Antialiasing | QtGui.QPainter.SmoothPixmapTransform
                            | QtGui.QPainter.TextAntialiasing)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setBackgroundBrush(st.colour(st.CANVAS_BG))
        self.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.cards: dict[str, CardItem] = {}
        self._document = None
        self._issues = None
        self._runs: dict = {}
        self.scene().selectionChanged.connect(self._selection_changed)

    def refresh(self, document, issues=None, runs=None) -> None:
        self._document, self._issues = document, issues
        self._runs = dict(runs or {})
        selected = self.current_step_id()
        scene = self.scene()
        scene.blockSignals(True)
        try:
            scene.clear()
            self.cards = {}
            infos = node_infos(document, issues)
            lane_of = lanes(document)
            width = max(520.0, self.viewport().width() - GUTTER - 28)
            y = 12.0
            for step in document.steps:
                card = CardItem(infos[step.id], self._runs.get(step.id), lane_of.get(step.id, 0), width)
                card.setPos(GUTTER + 12, y)
                scene.addItem(card)
                self.cards[step.id] = card
                y += card.height + CARD_GAP
            self._draw_lanes(document)
            scene.setSceneRect(QtCore.QRectF(0, 0, width + GUTTER + 24, y + 12))
            if selected in self.cards:
                self.cards[selected].setSelected(True)
        finally:
            scene.blockSignals(False)

    def _draw_lanes(self, document) -> None:
        from imswitch.improcess.model.workfloweditor.graph import refs_of

        for step in document.steps:
            target = self.cards.get(step.id)
            for ref in refs_of(step):
                source = self.cards.get(ref.step)
                if source is None or target is None:
                    continue
                slot = source.info.output(str(ref)) or (source.info.outputs[0] if source.info.outputs else None)
                kind = slot.kind if slot is not None else "unknown"
                if source.run is not None and slot is not None:
                    kind = source.run.kinds.get(slot.port, kind)
                start, end = source.dot(), target.dot()
                path = QtGui.QPainterPath(start)
                bend = end.y() - 28
                path.lineTo(start.x(), max(start.y(), bend - 10))
                path.cubicTo(QtCore.QPointF(start.x(), bend + 12), QtCore.QPointF(end.x(), bend + 12), end)
                line = QtWidgets.QGraphicsPathItem(path)
                line.setPen(QtGui.QPen(st.kind_colour(kind), 2.6, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap))
                line.setZValue(-1)
                self.scene().addItem(line)

    def set_runs(self, runs) -> None:
        if self._document is not None:
            self.refresh(self._document, self._issues, runs)

    def current_step_id(self) -> str | None:
        try:
            selected = [item for item in self.scene().selectedItems() if isinstance(item, CardItem)]
        except RuntimeError:
            return None
        return selected[0].step_id if selected else None

    def select_step(self, step_id: str) -> None:
        card = self.cards.get(step_id)
        if card is None:
            return
        if card.isSelected() and len(self.scene().selectedItems()) == 1:
            return
        self.scene().blockSignals(True)
        for item in self.scene().selectedItems():
            item.setSelected(False)
        card.setSelected(True)
        self.scene().blockSignals(False)
        self.ensureVisible(card, 20, 20)
        self.sigStepSelected.emit(step_id)

    def _selection_changed(self) -> None:
        self.sigStepSelected.emit(self.current_step_id() or "")

    def keyPressEvent(self, event) -> None:
        if event.key() in (QtCore.Qt.Key_Up, QtCore.Qt.Key_Down) and self._document is not None:
            ids = [step.id for step in self._document.steps]
            current = self.current_step_id()
            index = ids.index(current) if current in ids else -1
            index = max(0, min(len(ids) - 1, index + (1 if event.key() == QtCore.Qt.Key_Down else -1)))
            if ids:
                self.select_step(ids[index])
            event.accept()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event) -> None:
        item = self.itemAt(event.pos())
        while item is not None and not isinstance(item, CardItem):
            item = item.parentItem()
        if isinstance(item, CardItem):
            self.select_step(item.step_id)
            self.sigContextMenu.emit(item.step_id, event.globalPos())
            event.accept()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._document is not None and abs(event.size().width() - event.oldSize().width()) > 20:
            self.refresh(self._document, self._issues, self._runs)


__all__ = ["CardItem", "StepListView", "lanes"]
