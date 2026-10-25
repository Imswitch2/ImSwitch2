"""The workflow as a graph: boxes, cables, and the gestures that edit it.

The scene draws a :class:`WorkflowDocument` and turns gestures into
*requests* (signals); the window makes the edit, inside one undoable
``history.edit``, and asks the scene to :meth:`refresh`. Requests are sent
after the mouse event that made them has finished, because the refresh
replaces the items that event is still being delivered to.

Cable rules (the document enforces them again; the scene only says early):

- drag from an output to an input, or onto a box: connect (Shift: add the
  cable rather than replace the input, so a single-input step runs once per
  cable);
- drag from an output into empty space: ask for a step that takes it;
- drag a connected input's end away: disconnect (a Save keeps its input), or
  drop it on another input to move the cable there;
- drop a box that is connected to nothing on a cable: put it in between.
"""

from __future__ import annotations

from qtpy import QtCore, QtGui, QtWidgets

from imswitch.improcess.model.workfloweditor.graph import layered_positions, place_missing
from imswitch.improcess.model.workfloweditor.nodes import can_feed, kind_fits, kinds_of, node_infos
from imswitch.improcess.workflows.steps import Save

from . import style as st
from .items import CableItem, EdgeItem, FrameItem, NodeItem, PortItem, RunView

_SNAP = 18.0     # how near a port a cable end counts as on it


class WorkflowScene(QtWidgets.QGraphicsScene):
    """See the module docstring."""

    #: source ref, target step id, input index (None: append / first free), add (fan out)
    sigConnect = QtCore.Signal(str, str, object, bool)
    #: step id, input index, new target step id, new input index (or None)
    sigReconnect = QtCore.Signal(str, int, str, object)
    sigDisconnect = QtCore.Signal(str, int)
    #: source ref (or None for "anything"), scene position
    sigQuickAdd = QtCore.Signal(object, QtCore.QPointF)
    #: {step id: (x, y)} of boxes the user moved, a splice (step, target, index) or None,
    #: and {frame index: [x, y, w, h]} of frames moved or resized
    sigMoved = QtCore.Signal(dict, object, dict)
    sigFrameRename = QtCore.Signal(int)
    sigActivated = QtCore.Signal(str)
    sigMessage = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.document = None
        self.nodes: dict[str, NodeItem] = {}
        self.edges: list[EdgeItem] = []
        self.frames: list[FrameItem] = []
        self._pressed_frames: dict[int, list] = {}
        self.infos = {}
        self.runs: dict[str, RunView] = {}
        self._kinds: dict[str, str] = {}
        self._cable: CableItem | None = None
        self._drag = None
        self._pressed: dict[str, QtCore.QPointF] = {}
        self.setItemIndexMethod(QtWidgets.QGraphicsScene.NoIndex)
        self.setSceneRect(QtCore.QRectF(-20000, -20000, 40000, 40000))

    # -- building ------------------------------------------------------------------

    def set_document(self, document) -> None:
        self.document = document
        self.runs = {}
        self.refresh()

    def refresh(self, issues=None) -> None:
        """Rebuild every box and cable from the document; keeps the selection."""
        self.cancel_cable()
        selected = set(self.selected_step_ids())
        self.blockSignals(True)
        try:
            self.clear()
            self.nodes, self.edges, self.frames = {}, [], []
            if self.document is None:
                return
            for index, frame in enumerate(self.document.frames):
                item = FrameItem(index, frame)
                self.frames.append(item)
                self.addItem(item)
            self.infos = node_infos(self.document, issues)
            self._kinds = kinds_of(self.document)
            for step_id, info in self.infos.items():
                node = NodeItem(info, self.runs.get(step_id))
                self.nodes[step_id] = node
                self.addItem(node)
            positions = self.wanted_positions()
            for step_id, node in self.nodes.items():
                x, y = positions.get(step_id, (0.0, 0.0))
                node.setPos(x, y)
            for step in self.document.steps:
                target = self.nodes[step.id]
                refs = [step.input] if isinstance(step, Save) else list(getattr(step, "inputs", []) or [])
                for index, ref in enumerate(refs):
                    source = self.nodes.get(ref.step)
                    if source is None or source.output_port(str(ref)) is None:
                        continue
                    edge = EdgeItem(source, str(ref), target, index)
                    self.edges.append(edge)
                    self.addItem(edge)
            for step_id in selected:
                if step_id in self.nodes:
                    self.nodes[step_id].setSelected(True)
        finally:
            self.blockSignals(False)
        self.selectionChanged.emit()

    def heights(self) -> dict[str, float]:
        return {step_id: node.height for step_id, node in self.nodes.items()}

    def wanted_positions(self) -> dict[str, tuple[float, float]]:
        """Where each box goes: its stored place, or one made up for it."""
        stored = {key: value for key, value in self.document.positions.items() if key in self.nodes}
        return place_missing(self.document.steps, stored, self.heights(),
                             width=st.NODE_W, h_gap=st.H_GAP, v_gap=st.V_GAP)

    def tidy_positions(self) -> dict[str, tuple[float, float]]:
        """A fresh left-to-right layout of everything (*Tidy up*)."""
        return layered_positions(self.document.steps, self.heights(), width=st.NODE_W, h_gap=st.H_GAP,
                                 v_gap=st.V_GAP)

    def shown_positions(self) -> dict[str, tuple[float, float]]:
        return {step_id: (float(node.pos().x()), float(node.pos().y())) for step_id, node in self.nodes.items()}

    def set_runs(self, runs: dict[str, RunView]) -> None:
        """Show run states on the boxes, without rebuilding."""
        self.runs = dict(runs)
        for step_id, node in self.nodes.items():
            run = self.runs.get(step_id)
            if run is not node.run:
                node.set_run(run)
        for edge in self.edges:
            edge.update_path()
            edge.update()

    def set_run(self, step_id: str, run: RunView | None) -> None:
        if run is None:
            self.runs.pop(step_id, None)
        else:
            self.runs[step_id] = run
        node = self.nodes.get(step_id)
        if node is not None:
            node.set_run(run)
            self.node_moved(node)

    # -- queries -------------------------------------------------------------------

    def node(self, step_id: str) -> NodeItem | None:
        return self.nodes.get(step_id)

    def selected_step_ids(self) -> list[str]:
        try:
            items = self.selectedItems()
        except RuntimeError:
            return []
        return [item.step_id for item in items if isinstance(item, NodeItem)]

    def selected_edges(self) -> list[EdgeItem]:
        return [item for item in self.selectedItems() if isinstance(item, EdgeItem)]

    def selected_frames(self) -> list[int]:
        return sorted(item.index for item in self.selectedItems() if isinstance(item, FrameItem))

    def select_steps(self, step_ids, *, only: bool = True) -> None:
        wanted = set(step_ids)
        self.blockSignals(True)
        try:
            if only:
                for item in self.selectedItems():
                    item.setSelected(False)
            for step_id in wanted:
                if step_id in self.nodes:
                    self.nodes[step_id].setSelected(True)
        finally:
            self.blockSignals(False)
        self.selectionChanged.emit()

    def node_at(self, pos: QtCore.QPointF) -> NodeItem | None:
        for item in self.items(pos):
            while item is not None and not isinstance(item, NodeItem):
                item = item.parentItem()
            if isinstance(item, NodeItem):
                return item
        return None

    def edge_at(self, pos: QtCore.QPointF) -> EdgeItem | None:
        for item in self.items(pos):
            if isinstance(item, EdgeItem):
                return item
        return None

    def port_near(self, pos: QtCore.QPointF, side: str) -> PortItem | None:
        best, best_distance = None, _SNAP
        for node in self.nodes.values():
            for port in (node.inputs if side == "in" else node.outputs):
                delta = port.centre() - pos
                distance = (delta.x() ** 2 + delta.y() ** 2) ** 0.5
                if distance <= best_distance:
                    best, best_distance = port, distance
        return best

    def edge_into(self, step_id: str, index: int) -> EdgeItem | None:
        for edge in self.edges:
            if edge.target.step_id == step_id and edge.index == index:
                return edge
        return None

    def fits(self, producer_id: str, target_id: str) -> bool:
        return can_feed(self.document, producer_id, target_id, self._kinds)

    # -- cables ----------------------------------------------------------------------

    def begin_cable(self, port: PortItem, pos: QtCore.QPointF) -> bool:
        """A press on a port: start dragging a cable from it."""
        if self.document is None:
            return False
        node = port.node
        if port.side == "out":
            self._drag = {"mode": "out", "ref": port.slot.ref, "producer": node.step_id, "port": port}
            self._cable = CableItem(port.kind, port.centre())
            self._mark_inputs(node.step_id)
        elif port.slot.ref is not None:
            edge = self.edge_into(node.step_id, port.slot.index)
            ref = str(port.slot.ref)
            producer = port.slot.ref.step
            source = self.nodes.get(producer)
            out = source.output_port(ref) if source is not None else None
            if out is None:
                return False
            if edge is not None:
                edge.hide()
            self._drag = {"mode": "detach", "ref": ref, "producer": producer, "port": port, "edge": edge,
                          "step": node.step_id, "index": port.slot.index}
            self._cable = CableItem(out.kind, out.centre())
            self._mark_inputs(producer)
        else:
            self._drag = {"mode": "in", "step": node.step_id, "index": port.slot.index, "port": port}
            self._cable = CableItem(port.kind, port.centre(), reverse=True)
            self._mark_outputs(node.step_id)
        self.addItem(self._cable)
        self._cable.set_end(pos)
        return True

    def move_cable(self, pos: QtCore.QPointF) -> None:
        if self._cable is None:
            return
        side = "out" if self._drag["mode"] == "in" else "in"
        near = self.port_near(pos, side)
        for node in self.nodes.values():
            for port in (node.inputs if side == "in" else node.outputs):
                if port is near and port.highlight == "fit":
                    port.set_highlight("hover")
                elif port.highlight == "hover":
                    port.set_highlight("fit")
        self._cable.set_end(near.centre() if near is not None and near.highlight in ("fit", "hover") else pos)

    def end_cable(self, pos: QtCore.QPointF, modifiers=QtCore.Qt.NoModifier) -> None:
        drag = self._drag
        if drag is None:
            return
        add = bool(modifiers & QtCore.Qt.ShiftModifier)
        mode = drag["mode"]
        if mode == "in":
            port = self.port_near(pos, "out")
            node = port.node if port is not None else self.node_at(pos)
            self.cancel_cable()
            if node is None or node.step_id == drag["step"]:
                return
            if not self.fits(node.step_id, drag["step"]):
                self._say(f"{self.infos[drag['step']].title} cannot read from {node.step_id!r}")
                return
            ref = port.slot.ref if port is not None else (node.outputs[0].slot.ref if node.outputs else None)
            if ref is not None:
                self._later(self.sigConnect.emit, ref, drag["step"], drag["index"], add)
            return
        port = self.port_near(pos, "in")
        node = port.node if port is not None else self.node_at(pos)
        producer, ref = drag["producer"], drag["ref"]
        self.cancel_cable()
        if mode == "detach":
            if port is drag["port"]:
                return
            if node is None or node.step_id == producer:
                step = self.document.step(drag["step"])
                if isinstance(step, Save):
                    self._say("A Save needs an input: delete the Save instead")
                    return
                self._later(self.sigDisconnect.emit, drag["step"], drag["index"])
                return
            if not self.fits(producer, node.step_id):
                self._say(self._refusal(producer, node.step_id))
                return
            index = self._drop_index(node, port)
            self._later(self.sigReconnect.emit, drag["step"], drag["index"], node.step_id, index)
            return
        if node is None:
            self._later(self.sigQuickAdd.emit, ref, QtCore.QPointF(pos))
            return
        if node.step_id == producer:
            return
        if not self.fits(producer, node.step_id):
            self._say(self._refusal(producer, node.step_id))
            return
        self._later(self.sigConnect.emit, ref, node.step_id, self._drop_index(node, port), add)

    def cancel_cable(self) -> None:
        drag, self._drag = self._drag, None
        if self._cable is not None:
            if self._cable.scene() is self:
                self.removeItem(self._cable)
            self._cable = None
        if drag is not None and drag.get("edge") is not None:
            try:
                drag["edge"].show()
            except RuntimeError:
                pass
        for node in self.nodes.values():
            for port in node.inputs + node.outputs:
                port.set_highlight("")

    @staticmethod
    def _drop_index(node: NodeItem, port: PortItem | None):
        """Which input a drop lands on: the port's, or the box's first free one."""
        if port is not None:
            return port.slot.index
        for slot in node.info.inputs:
            if slot.ref is None and slot.index is not None:
                return slot.index
        return None

    def _refusal(self, producer: str, target: str) -> str:
        kind = self._kinds.get(producer, "unknown")
        title = self.infos[target].title if target in self.infos else target
        if target in self.infos and self.infos[target].step_kind == "reconstruct":
            return f"{title} reads a recording (a Source step)"
        return f"{title} does not take {st.KIND_WORDS.get(kind, kind)} results"

    def _mark_inputs(self, producer_id: str) -> None:
        for step_id, node in self.nodes.items():
            fits = step_id != producer_id and self.fits(producer_id, step_id)
            for port in node.inputs:
                port.set_highlight("fit" if fits else "nofit")

    def _mark_outputs(self, target_id: str) -> None:
        for step_id, node in self.nodes.items():
            fits = step_id != target_id and self.fits(step_id, target_id)
            for port in node.outputs:
                port.set_highlight("fit" if fits else "nofit")

    def _say(self, message: str) -> None:
        self._later(self.sigMessage.emit, message)

    @staticmethod
    def _later(function, *args) -> None:
        QtCore.QTimer.singleShot(0, lambda: function(*args))

    # -- moving --------------------------------------------------------------------------

    def node_moved(self, node: NodeItem) -> None:
        for edge in self.edges:
            if edge.source is node or edge.target is node:
                edge.update_path()
        if self._pressed and node.isSelected() and len(self.selected_step_ids()) == 1:
            self._mark_splice(node)

    def _can_splice(self, node: NodeItem) -> bool:
        info = node.info
        if info.step_kind != "process" or any(slot.ref is not None for slot in info.inputs):
            return False
        return not any(edge.source is node for edge in self.edges)

    def _mark_splice(self, node: NodeItem) -> None:
        target = None
        if self._can_splice(node):
            centre = node.mapToScene(QtCore.QPointF(st.NODE_W / 2, st.HEADER_H / 2))
            entry = self.document.entry_for(self.document.step(node.step_id))
            for edge in self.edges:
                if edge.target is node or edge.source is node:
                    continue
                if edge.shape().contains(centre) or edge.shape().contains(node.mapToScene(QtCore.QPointF(0, st.PORT_TOP))):
                    if kind_fits(edge.kind, entry) and edge.target.info.step_kind != "reconstruct":
                        target = edge
                        break
        old = node.splice_target
        if old is not target:
            if old is not None:
                try:
                    old.set_hot(False)
                except RuntimeError:
                    pass
            if target is not None:
                target.set_hot(True)
            node.splice_target = target

    def mousePressEvent(self, event) -> None:
        super().mousePressEvent(event)
        if event.button() == QtCore.Qt.LeftButton:
            self._pressed = {step_id: QtCore.QPointF(node.pos()) for step_id, node in self.nodes.items()}
            self._pressed_frames = {frame.index: frame.rect() for frame in self.frames}

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        pressed, self._pressed = self._pressed, {}
        pressed_frames, self._pressed_frames = self._pressed_frames, {}
        if not pressed or event.button() != QtCore.Qt.LeftButton:
            return
        frames = {frame.index: frame.rect() for frame in self.frames
                  if frame.index in pressed_frames and any(abs(a - b) > 0.5 for a, b in
                                                           zip(frame.rect(), pressed_frames[frame.index]))}
        moved = {}
        for step_id, node in self.nodes.items():
            before = pressed.get(step_id)
            if before is not None and (node.pos() - before).manhattanLength() > 0.5:
                moved[step_id] = (float(node.pos().x()), float(node.pos().y()))
        splice = None
        for node in self.nodes.values():
            if node.splice_target is not None:
                edge = node.splice_target
                splice = (node.step_id, edge.target.step_id, edge.index)
                edge.set_hot(False)
                node.splice_target = None
        if moved or splice or frames:
            self._later(self.sigMoved.emit, moved, splice, frames)

    def keyPressEvent(self, event) -> None:
        if event.key() == QtCore.Qt.Key_Escape and self._drag is not None:
            self.cancel_cable()
            event.accept()
            return
        super().keyPressEvent(event)


__all__ = ["WorkflowScene"]
