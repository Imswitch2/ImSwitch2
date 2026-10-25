"""The things on the canvas: a box per step, its ports, and the cables.

Items only draw and report gestures to their scene
(:class:`~.scene.WorkflowScene`); they never touch the document. What a box
shows comes from a Qt-free :class:`~imswitch.improcess.model.workfloweditor.nodes.NodeInfo`
and, after a run, a :class:`RunView` (status, thumbnail, timing, error).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from qtpy import QtCore, QtGui, QtWidgets

from . import style as st

#: Run states a box can show; "" is "never run in this session".
STATES = ("", "done", "outdated", "running", "waiting", "failed", "would-write")
_BADGE_GLYPHS = {"done": "✓", "outdated": "↻", "failed": "✕", "waiting": "…", "would-write": "⤓"}
_BADGE_COLOURS = {"done": st.DONE, "outdated": st.OUTDATED, "failed": st.ERROR, "running": st.RUNNING,
                  "waiting": st.TEXT_DIM, "would-write": st.TEXT_DIM}


@dataclass
class RunView:
    """What a run says about one box (Qt side: the pixmap is made on the GUI thread)."""

    status: str = ""
    thumbnail: QtGui.QPixmap | None = None
    footer: str = ""
    error: str = ""
    summary: tuple[str, ...] | None = None
    #: The kind each port actually carried (``port -> kind``), over the declared one.
    kinds: dict = field(default_factory=dict)
    #: Extra tooltip text per port (shape, axes, dtype).
    tips: dict = field(default_factory=dict)
    #: The error's line in the step's code, for a Python step.
    line: int | None = None


def _elide(painter: QtGui.QPainter, text: str, width: float) -> str:
    return QtGui.QFontMetrics(painter.font()).elidedText(str(text), QtCore.Qt.ElideRight, int(max(width, 8)))


class PortItem(QtWidgets.QGraphicsItem):
    """One input or output connector. Pressing it starts a cable drag."""

    def __init__(self, node: "NodeItem", side: str, slot, position: int):
        super().__init__(node)
        self.node = node
        self.side = side            # "in" | "out"
        self.slot = slot
        self.position = position    # row on the box
        self.highlight = ""         # "" | "fit" | "nofit" | "hover"
        self.setAcceptHoverEvents(True)
        self.setCursor(QtCore.Qt.CrossCursor)
        self.setZValue(3)
        self.setToolTip(self._tooltip())

    # -- what it is ------------------------------------------------------------

    @property
    def kind(self) -> str:
        run = self.node.run
        if self.side == "out" and run is not None and self.slot.port in run.kinds:
            return run.kinds[self.slot.port]
        return self.slot.kind

    @property
    def hollow(self) -> bool:
        return bool(getattr(self.slot, "adds", False) or getattr(self.slot, "placeholder", False))

    @property
    def empty(self) -> bool:
        return self.side == "in" and getattr(self.slot, "ref", None) is None

    def _tooltip(self) -> str:
        words = st.KIND_WORDS.get(self.kind, self.kind)
        if self.side == "out":
            name = self.slot.label or self.slot.port
            if getattr(self.slot, "placeholder", False):
                text = f"{name} · {words}: drag from here to use the next {name.rstrip('0123456789')}… output"
            else:
                text = f"{name} · {words}\nDrag to an input to connect; into empty space to add a step that takes it"
        elif self.slot.adds:
            text = "Add an input: drop a cable here"
        elif self.slot.ref is None:
            text = f"Not connected: needs a {words}" if self.slot.required else f"Not connected ({words})"
        else:
            text = f"Reads {self.slot.ref} · {words}\nDrag the end away to disconnect it"
        run = self.node.run
        extra = run.tips.get(self.slot.port if self.side == "out" else f"in{self.slot.index}") if run else None
        return f"{text}\n{extra}" if extra else text

    def centre(self) -> QtCore.QPointF:
        return self.mapToScene(QtCore.QPointF(0, 0))

    # -- drawing ---------------------------------------------------------------

    def boundingRect(self) -> QtCore.QRectF:
        if self.side == "out" and self.slot.label:
            return QtCore.QRectF(-84, -11, 96, 22)
        return QtCore.QRectF(-11, -11, 22, 22)

    def shape(self) -> QtGui.QPainterPath:
        path = QtGui.QPainterPath()
        path.addEllipse(QtCore.QPointF(0, 0), 11, 11)
        return path

    def paint(self, painter, option, widget=None) -> None:
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        colour = st.kind_colour(self.kind)
        radius = st.PORT_R + (2 if self.highlight in ("fit", "hover") else 0)
        if self.highlight == "nofit":
            painter.setOpacity(0.3)
        if self.highlight == "fit":
            glow = QtGui.QColor(colour)
            glow.setAlpha(70)
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(glow)
            painter.drawEllipse(QtCore.QPointF(0, 0), radius + 5, radius + 5)
        if self.hollow:
            painter.setPen(QtGui.QPen(colour, 1.6, QtCore.Qt.DashLine))
            painter.setBrush(st.colour(st.NODE_BODY))
            painter.drawEllipse(QtCore.QPointF(0, 0), radius - 1, radius - 1)
        elif self.empty:
            ring = st.colour(st.ERROR) if self.slot.required else colour
            painter.setPen(QtGui.QPen(ring, 2.0))
            painter.setBrush(st.colour(st.NODE_BODY))
            painter.drawEllipse(QtCore.QPointF(0, 0), radius - 0.5, radius - 0.5)
        else:
            painter.setPen(QtGui.QPen(QtGui.QColor("#0b1117"), 2.2))
            painter.setBrush(colour)
            painter.drawEllipse(QtCore.QPointF(0, 0), radius, radius)
        if self.side == "out" and self.slot.label:
            painter.setFont(st.font(7.8, bold=True))
            painter.setPen(colour.lighter(115))
            painter.drawText(QtCore.QRectF(-84, -8, 72, 16), QtCore.Qt.AlignVCenter | QtCore.Qt.AlignRight,
                             _elide(painter, self.slot.label, 72))
        painter.setOpacity(1.0)

    def set_highlight(self, value: str) -> None:
        if value != self.highlight:
            self.highlight = value
            self.update()

    # -- gestures ----------------------------------------------------------------

    def hoverEnterEvent(self, event) -> None:
        if not self.highlight:
            self.set_highlight("hover")
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:
        if self.highlight == "hover":
            self.set_highlight("")
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event) -> None:
        scene = self.scene()
        if event.button() == QtCore.Qt.LeftButton and scene is not None and scene.begin_cable(self, event.scenePos()):
            event.accept()
            return
        event.ignore()

    def mouseMoveEvent(self, event) -> None:
        if self.scene() is not None:
            self.scene().move_cable(event.scenePos())

    def mouseReleaseEvent(self, event) -> None:
        if self.scene() is not None:
            self.scene().end_cable(event.scenePos(), event.modifiers())


class NodeItem(QtWidgets.QGraphicsItem):
    """One step as a box: header, what the file sets, ports, and run state."""

    def __init__(self, info, run: RunView | None = None):
        super().__init__()
        self.info = info
        self.run = run
        self.splice_target = None
        self.setFlags(QtWidgets.QGraphicsItem.ItemIsSelectable | QtWidgets.QGraphicsItem.ItemIsMovable
                      | QtWidgets.QGraphicsItem.ItemSendsGeometryChanges)
        self.setAcceptHoverEvents(True)
        self.setZValue(2)
        self.setToolTip(self._tooltip())
        self.inputs: list[PortItem] = []
        self.outputs: list[PortItem] = []
        self._lay = self._layout()
        self._make_ports()

    @property
    def step_id(self) -> str:
        return self.info.id

    def _tooltip(self) -> str:
        lines = [f"{self.info.title} ({self.info.id})"]
        if self.info.description:
            lines.append(self.info.description)
        for issue in self.info.issues:
            lines.append(f"⚠ {issue}")
        return "\n".join(lines)

    def summary(self) -> tuple[str, ...]:
        if self.run is not None and self.run.summary is not None:
            return self.run.summary
        return self.info.summary

    def _layout(self) -> dict:
        info = self.info
        rows = max(len(info.inputs), len(info.outputs), 1)
        ports_bottom = st.PORT_TOP + (rows - 1) * st.PORT_GAP + 14
        summary = self.summary()
        y = st.HEADER_H + 10
        summary_y = y
        y += 17 * len(summary)
        code_rect = None
        if info.code:
            top = max(y + 2, ports_bottom if len(info.outputs) > 1 else y + 2)
            code_rect = QtCore.QRectF(14, top, st.NODE_W - 28, 15 * len(info.code) + 6)
            y = code_rect.bottom() + 6
        y = max(y, ports_bottom)
        thumb_rect = None
        if self.run is not None and self.run.thumbnail is not None:
            thumb_rect = QtCore.QRectF((st.NODE_W - st.THUMB_W) / 2, y + 2, st.THUMB_W, st.THUMB_H)
            y = thumb_rect.bottom() + 6
        footer_y = None
        if self.run is not None and self.run.footer:
            footer_y = y
            y += 18
        height = y + 6
        error_h = 0
        if self.run is not None and self.run.error:
            error_h = 18 + 15 * min(4, 1 + len(self.run.error) // 34)
        labelled = any(slot.label for slot in info.outputs)
        return {"summary_y": summary_y, "code": code_rect, "thumb": thumb_rect, "footer_y": footer_y,
                "height": height, "error_h": error_h, "labelled": labelled}

    def _make_ports(self) -> None:
        for item in self.inputs + self.outputs:
            item.setParentItem(None)
            if item.scene() is not None:
                item.scene().removeItem(item)
        self.inputs, self.outputs = [], []
        for row, slot in enumerate(self.info.inputs):
            port = PortItem(self, "in", slot, row)
            port.setPos(0, st.PORT_TOP + row * st.PORT_GAP)
            self.inputs.append(port)
        for row, slot in enumerate(self.info.outputs):
            port = PortItem(self, "out", slot, row)
            port.setPos(st.NODE_W, st.PORT_TOP + row * st.PORT_GAP)
            self.outputs.append(port)

    def set_run(self, run: RunView | None) -> None:
        """Show what a run said, without rebuilding the scene."""
        self.prepareGeometryChange()
        self.run = run
        self._lay = self._layout()
        for port in self.inputs + self.outputs:
            port.setToolTip(port._tooltip())
            port.update()
        self.update()

    @property
    def height(self) -> float:
        return self._lay["height"]

    def node_rect(self) -> QtCore.QRectF:
        return QtCore.QRectF(0, 0, st.NODE_W, self.height)

    def boundingRect(self) -> QtCore.QRectF:
        return QtCore.QRectF(-14, -10, st.NODE_W + 28, self.height + 24 + self._lay["error_h"])

    def shape(self) -> QtGui.QPainterPath:
        path = QtGui.QPainterPath()
        path.addRoundedRect(self.node_rect(), st.RADIUS, st.RADIUS)
        return path

    def output_port(self, ref: str) -> PortItem | None:
        for port in self.outputs:
            if port.slot.ref == ref or f"{self.step_id}.{port.slot.port}" == ref:
                return port
        # A reference without a port reads the first output.
        if ref == self.step_id and self.outputs:
            return self.outputs[0]
        return None

    def input_port(self, index: int) -> PortItem | None:
        for port in self.inputs:
            if port.slot.index == index:
                return port
        return None

    # -- drawing -------------------------------------------------------------------

    def paint(self, painter, option, widget=None) -> None:
        info, lay = self.info, self._lay
        status = self.run.status if self.run is not None else ""
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        rect = self.node_rect()
        radius = st.RADIUS
        painter.setPen(QtCore.Qt.NoPen)
        for i, alpha in enumerate((40, 26, 14, 6)):
            painter.setBrush(QtGui.QColor(0, 0, 0, alpha))
            painter.drawRoundedRect(rect.adjusted(-i, -i + 4, i, i + 6), radius + i, radius + i)
        body = QtGui.QPainterPath()
        body.addRoundedRect(rect, radius, radius)
        painter.fillPath(body, st.colour(st.NODE_BODY))
        header = QtGui.QPainterPath()
        header.addRoundedRect(QtCore.QRectF(0, 0, st.NODE_W, st.HEADER_H + radius), radius, radius)
        clip = QtGui.QPainterPath()
        clip.addRect(QtCore.QRectF(0, 0, st.NODE_W, st.HEADER_H))
        head = st.step_colour(info.step_kind, info.category)
        gradient = QtGui.QLinearGradient(0, 0, st.NODE_W, 0)
        gradient.setColorAt(0, head)
        gradient.setColorAt(1, head.darker(135))
        painter.fillPath(header.intersected(clip), gradient)
        badges = 26 + (24 if info.issues else 0)
        painter.setPen(QtGui.QColor("#ffffff"))
        painter.setFont(st.font(9.8, bold=True))
        painter.drawText(QtCore.QRectF(12, 5, st.NODE_W - 12 - badges, 18), QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft,
                         _elide(painter, info.title, st.NODE_W - 12 - badges))
        painter.setFont(st.font(7.8, mono=True))
        painter.setPen(QtGui.QColor(255, 255, 255, 175))
        painter.drawText(QtCore.QRectF(12, 24, st.NODE_W - 12 - badges, 14), QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft,
                         _elide(painter, info.id, st.NODE_W - 12 - badges))

        painter.setFont(st.font(8.6))
        width = st.NODE_W - (100 if lay["labelled"] else 36)
        y = lay["summary_y"]
        for line in self.summary():
            dim = line.startswith(("bound when run", "defaults", "would write"))
            painter.setPen(st.colour(st.TEXT_DIM if dim else st.TEXT))
            painter.drawText(QtCore.QRectF(18, y, width, 16), QtCore.Qt.AlignLeft, _elide(painter, line, width))
            y += 17
        if lay["code"] is not None:
            box = lay["code"]
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(st.colour(st.CODE_BG))
            painter.drawRoundedRect(box, 4, 4)
            painter.setFont(st.font(7.4, mono=True))
            painter.setPen(st.colour(st.CODE_TEXT))
            for i, line in enumerate(info.code):
                painter.drawText(QtCore.QRectF(box.x() + 6, box.y() + 3 + 15 * i, box.width() - 10, 15),
                                 QtCore.Qt.AlignLeft, _elide(painter, line, box.width() - 10))
        if lay["thumb"] is not None:
            trect = lay["thumb"]
            clip_path = QtGui.QPainterPath()
            clip_path.addRoundedRect(trect, 5, 5)
            painter.save()
            painter.setClipPath(clip_path)
            pixmap = self.run.thumbnail
            scaled = pixmap.scaled(trect.size().toSize(), QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
            painter.fillRect(trect, QtGui.QColor("#0c1218"))
            painter.drawPixmap(QtCore.QPointF(trect.x() + (trect.width() - scaled.width()) / 2,
                                              trect.y() + (trect.height() - scaled.height()) / 2), scaled)
            if status == "outdated":
                painter.fillRect(trect, QtGui.QColor(17, 25, 33, 150))
                painter.setPen(st.colour(st.OUTDATED))
                painter.setFont(st.font(8.6, bold=True))
                painter.drawText(trect, QtCore.Qt.AlignCenter, "outdated")
            painter.restore()
            painter.setPen(QtGui.QPen(QtGui.QColor("#2b3a49"), 1))
            painter.setBrush(QtCore.Qt.NoBrush)
            painter.drawRoundedRect(trect, 5, 5)
        if lay["footer_y"] is not None:
            painter.setFont(st.font(7.8))
            painter.setPen(st.colour(st.TEXT_DIM))
            painter.drawText(QtCore.QRectF(14, lay["footer_y"], st.NODE_W - 28, 14), QtCore.Qt.AlignLeft,
                             _elide(painter, self.run.footer, st.NODE_W - 28))

        border, pen_width = st.colour(st.NODE_BORDER), 1.2
        if info.issues:
            border, pen_width = QtGui.QColor(st.ERROR).darker(115), 1.6
        if status == "outdated":
            border, pen_width = st.colour(st.OUTDATED), 1.6
        elif status == "failed":
            border, pen_width = st.colour(st.ERROR), 2.0
        elif status == "running":
            border, pen_width = st.colour(st.RUNNING), 2.0
        if self.isSelected():
            border, pen_width = st.colour(st.NODE_SELECTED), 2.2
        pen = QtGui.QPen(border, pen_width)
        if status in ("outdated", "waiting") and not self.isSelected():
            pen.setStyle(QtCore.Qt.DashLine)
        painter.setPen(pen)
        painter.setBrush(QtCore.Qt.NoBrush)
        painter.drawRoundedRect(rect, radius, radius)
        self._paint_badge(painter, QtCore.QPointF(st.NODE_W - 20, st.HEADER_H / 2), status)
        if info.issues:
            centre = QtCore.QPointF(st.NODE_W - 20 - (24 if status else 0), st.HEADER_H / 2)
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(QtGui.QColor("#3d1a1c"))
            painter.drawEllipse(centre, 9, 9)
            painter.setPen(QtGui.QColor("#ffb3ae"))
            painter.setFont(st.font(9, bold=True))
            painter.drawText(QtCore.QRectF(centre.x() - 9, centre.y() - 9.5, 18, 18), QtCore.Qt.AlignCenter, "!")
        if self.run is not None and self.run.error:
            box = QtCore.QRectF(0, self.height + 8, st.NODE_W, lay["error_h"])
            painter.setPen(QtGui.QPen(st.colour(st.ERROR), 1.2))
            painter.setBrush(QtGui.QColor(70, 22, 24, 235))
            painter.drawRoundedRect(box, 6, 6)
            painter.setPen(QtGui.QColor("#ffd6d4"))
            painter.setFont(st.font(8.0))
            painter.drawText(box.adjusted(8, 3, -8, -3), QtCore.Qt.AlignVCenter | QtCore.Qt.TextWordWrap, self.run.error)

    @staticmethod
    def _paint_badge(painter, centre, status: str) -> None:
        if not status:
            return
        colour = st.colour(_BADGE_COLOURS.get(status, st.TEXT_DIM))
        painter.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0, 90), 1))
        painter.setBrush(colour if status != "running" else QtGui.QColor("#13202b"))
        painter.drawEllipse(centre, 9, 9)
        if status == "running":
            painter.setPen(QtGui.QPen(colour, 2.6, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap))
            painter.drawArc(QtCore.QRectF(centre.x() - 6, centre.y() - 6, 12, 12), 90 * 16, -250 * 16)
            return
        painter.setPen(QtGui.QColor("#ffffff") if status == "failed" else QtGui.QColor("#0d1419"))
        painter.setFont(st.font(9.5, bold=True))
        painter.drawText(QtCore.QRectF(centre.x() - 9, centre.y() - 9.5, 18, 18), QtCore.Qt.AlignCenter,
                         _BADGE_GLYPHS.get(status, ""))

    # -- moving --------------------------------------------------------------------

    def itemChange(self, change, value):
        if change == QtWidgets.QGraphicsItem.ItemPositionHasChanged and self.scene() is not None:
            self.scene().node_moved(self)
        return super().itemChange(change, value)

    def mouseDoubleClickEvent(self, event) -> None:
        if self.scene() is not None:
            self.scene().sigActivated.emit(self.step_id)
        event.accept()


class EdgeItem(QtWidgets.QGraphicsPathItem):
    """A cable from one output to one input; selectable, so it can be deleted."""

    def __init__(self, source: NodeItem, ref: str, target: NodeItem, index: int):
        super().__init__()
        self.source = source
        self.ref = ref
        self.target = target
        self.index = index
        self.hot = False
        self.setZValue(1)
        self.setFlag(QtWidgets.QGraphicsItem.ItemIsSelectable)
        self.setAcceptHoverEvents(True)
        self.update_path()

    @property
    def kind(self) -> str:
        port = self.source.output_port(self.ref)
        return port.kind if port is not None else "unknown"

    def endpoints(self) -> tuple[QtCore.QPointF, QtCore.QPointF]:
        out = self.source.output_port(self.ref)
        into = self.target.input_port(self.index)
        start = out.centre() if out is not None else self.source.mapToScene(QtCore.QPointF(st.NODE_W, st.PORT_TOP))
        end = into.centre() if into is not None else self.target.mapToScene(QtCore.QPointF(0, st.PORT_TOP))
        return start, end

    def update_path(self) -> None:
        start, end = self.endpoints()
        self.setPath(cable_path(start, end))

    def shape(self) -> QtGui.QPainterPath:
        stroker = QtGui.QPainterPathStroker()
        stroker.setWidth(12)
        return stroker.createStroke(self.path())

    def set_hot(self, hot: bool) -> None:
        if hot != self.hot:
            self.hot = hot
            self.update()

    def hoverEnterEvent(self, event) -> None:
        self.set_hot(True)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:
        self.set_hot(False)
        super().hoverLeaveEvent(event)

    def paint(self, painter, option, widget=None) -> None:
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        colour = st.kind_colour(self.kind)
        run = self.target.run
        dim = run is not None and run.status in ("outdated", "waiting")
        if dim:
            colour.setAlpha(120)
        width = 2.6
        if self.isSelected() or self.hot:
            width = 3.6
            colour = colour.lighter(130)
        painter.setPen(QtGui.QPen(QtGui.QColor(5, 8, 12, 200), width + 3.4, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap))
        painter.drawPath(self.path())
        style = QtCore.Qt.DashLine if dim else QtCore.Qt.SolidLine
        painter.setPen(QtGui.QPen(colour, width, style, QtCore.Qt.RoundCap))
        painter.drawPath(self.path())
        if self.isSelected():
            painter.setPen(QtGui.QPen(st.colour(st.NODE_SELECTED), 1.0, QtCore.Qt.DotLine))
            painter.drawPath(self.path())


class FrameItem(QtWidgets.QGraphicsItem):
    """A labelled area behind a group of boxes; moving it moves what it holds.

    Its bottom-right corner resizes it; a double-click on its title renames it.
    """

    GRIP = 16.0
    TITLE_H = 26.0

    def __init__(self, index: int, frame: dict):
        super().__init__()
        self.index = index
        self.title = frame.get("title", "")
        self.colour = QtGui.QColor(frame.get("colour") or "#6c7c8c")
        x, y, w, h = frame["rect"]
        self.size = QtCore.QSizeF(w, h)
        self.setPos(x, y)
        self.setZValue(-2)
        self.setFlags(QtWidgets.QGraphicsItem.ItemIsSelectable | QtWidgets.QGraphicsItem.ItemIsMovable
                      | QtWidgets.QGraphicsItem.ItemSendsGeometryChanges)
        self.setAcceptHoverEvents(True)
        self.setToolTip("A frame: drag its title to move it with the steps inside, its corner to resize it, "
                        "double-click the title to rename it")
        self._carried: list = []
        self._resizing = None

    def rect(self) -> list[float]:
        return [float(self.pos().x()), float(self.pos().y()), float(self.size.width()), float(self.size.height())]

    def boundingRect(self) -> QtCore.QRectF:
        return QtCore.QRectF(-2, -2, self.size.width() + 4, self.size.height() + 4)

    def shape(self) -> QtGui.QPainterPath:
        # Only the title bar and the grip take the mouse: a press inside the
        # frame belongs to the canvas (rubber band) or the boxes on it.
        path = QtGui.QPainterPath()
        path.addRect(QtCore.QRectF(0, 0, self.size.width(), self.TITLE_H))
        path.addRect(QtCore.QRectF(self.size.width() - self.GRIP, self.size.height() - self.GRIP, self.GRIP, self.GRIP))
        return path

    def paint(self, painter, option, widget=None) -> None:
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        rect = QtCore.QRectF(0, 0, self.size.width(), self.size.height())
        fill = QtGui.QColor(self.colour)
        fill.setAlpha(28)
        border = QtGui.QColor(self.colour)
        border.setAlpha(150 if not self.isSelected() else 255)
        painter.setPen(QtGui.QPen(st.colour(st.NODE_SELECTED) if self.isSelected() else border, 1.4,
                                  QtCore.Qt.SolidLine))
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, 10, 10)
        bar = QtGui.QColor(self.colour)
        bar.setAlpha(70)
        title = QtGui.QPainterPath()
        title.addRoundedRect(QtCore.QRectF(0, 0, rect.width(), self.TITLE_H + 10), 10, 10)
        clip = QtGui.QPainterPath()
        clip.addRect(QtCore.QRectF(0, 0, rect.width(), self.TITLE_H))
        painter.fillPath(title.intersected(clip), bar)
        painter.setPen(st.colour(st.TEXT))
        painter.setFont(st.font(9.5, bold=True))
        painter.drawText(QtCore.QRectF(12, 0, rect.width() - 24, self.TITLE_H), QtCore.Qt.AlignVCenter,
                         _elide(painter, self.title or "Frame", rect.width() - 24))
        painter.setPen(QtGui.QPen(border, 1.2))
        corner = rect.bottomRight()
        for offset in (5, 10):
            painter.drawLine(QtCore.QPointF(corner.x() - offset, corner.y() - 3),
                             QtCore.QPointF(corner.x() - 3, corner.y() - offset))

    def _in_grip(self, pos: QtCore.QPointF) -> bool:
        return pos.x() > self.size.width() - self.GRIP and pos.y() > self.size.height() - self.GRIP

    def mousePressEvent(self, event) -> None:
        if event.button() == QtCore.Qt.LeftButton and self._in_grip(event.pos()):
            self._resizing = (event.scenePos(), QtCore.QSizeF(self.size))
            event.accept()
            return
        scene = self.scene()
        area = self.sceneBoundingRect()
        self._carried = [node for node in getattr(scene, "nodes", {}).values()
                         if area.contains(node.sceneBoundingRect().center()) and not node.isSelected()]
        self._last = QtCore.QPointF(self.pos())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._resizing is not None:
            start, size = self._resizing
            delta = event.scenePos() - start
            self.prepareGeometryChange()
            self.size = QtCore.QSizeF(max(120.0, size.width() + delta.x()), max(80.0, size.height() + delta.y()))
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._resizing = None
        self._carried = []
        super().mouseReleaseEvent(event)

    def itemChange(self, change, value):
        if change == QtWidgets.QGraphicsItem.ItemPositionHasChanged and self._carried:
            delta = self.pos() - self._last
            self._last = QtCore.QPointF(self.pos())
            for node in self._carried:
                node.setPos(node.pos() + delta)
        return super().itemChange(change, value)

    def mouseDoubleClickEvent(self, event) -> None:
        scene = self.scene()
        if scene is not None and event.pos().y() <= self.TITLE_H:
            scene.sigFrameRename.emit(self.index)
        event.accept()


class CableItem(QtWidgets.QGraphicsPathItem):
    """The cable being dragged, from a port to the pointer."""

    def __init__(self, kind: str, start: QtCore.QPointF, *, reverse: bool = False):
        super().__init__()
        self.kind = kind
        self.start = QtCore.QPointF(start)
        self.reverse = reverse
        self.setZValue(5)
        colour = st.kind_colour(kind)
        self.setPen(QtGui.QPen(colour, 2.6, QtCore.Qt.DashLine, QtCore.Qt.RoundCap))
        self.set_end(start)

    def set_end(self, end: QtCore.QPointF) -> None:
        self.end = QtCore.QPointF(end)
        if self.reverse:
            self.setPath(cable_path(self.end, self.start))
        else:
            self.setPath(cable_path(self.start, self.end))


def cable_path(start: QtCore.QPointF, end: QtCore.QPointF) -> QtGui.QPainterPath:
    dx = max(60.0, abs(end.x() - start.x()) * 0.5)
    path = QtGui.QPainterPath(start)
    path.cubicTo(start + QtCore.QPointF(dx, 0), end - QtCore.QPointF(dx, 0), end)
    return path


__all__ = ["CableItem", "EdgeItem", "FrameItem", "NodeItem", "PortItem", "RunView", "STATES", "cable_path"]
