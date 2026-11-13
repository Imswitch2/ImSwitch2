"""The step palette: a search box over everything that can be added.

Recent steps first, then sources, reconstructions, processing by category,
drop-in plugins, and Save; every entry with its category's colour, its
description as a tooltip. Drag an entry onto the canvas, or double-click it
(Enter) to add it next to the selected box. A plugin the headless contract
refuses is listed greyed with the reason, so nobody wonders where it went.
"""

from __future__ import annotations

from qtpy import QtCore, QtGui, QtWidgets

from imswitch.improcess.model.workfloweditor.catalog import PROCESSOR, USER

from .canvas import style as st
from .canvas.view import STEP_MIME

_KIND_ROLE = QtCore.Qt.UserRole
_PLUGIN_ROLE = QtCore.Qt.UserRole + 1
_TEXT_ROLE = QtCore.Qt.UserRole + 2


def _chip(colour: str, size: int = 12) -> QtGui.QIcon:
    pixmap = QtGui.QPixmap(size, size)
    pixmap.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    painter.setPen(QtCore.Qt.NoPen)
    painter.setBrush(QtGui.QColor(colour))
    painter.drawRoundedRect(QtCore.QRectF(1, 1, size - 2, size - 2), 3, 3)
    painter.end()
    return QtGui.QIcon(pixmap)


class _Tree(QtWidgets.QTreeWidget):
    """The list itself; a drag carries the step as :data:`STEP_MIME`."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setColumnCount(1)
        self.setIndentation(14)
        self.setDragEnabled(True)
        self.setDragDropMode(QtWidgets.QAbstractItemView.DragOnly)
        self.setIconSize(QtCore.QSize(12, 12))

    def mimeTypes(self):
        return [STEP_MIME]

    def mimeData(self, items):
        mime = QtCore.QMimeData()
        for item in items:
            kind = item.data(0, _KIND_ROLE)
            if kind and item.flags() & QtCore.Qt.ItemIsEnabled:
                mime.setData(STEP_MIME, f"{kind}\n{item.data(0, _PLUGIN_ROLE) or ''}".encode("utf-8"))
                break
        return mime


class StepPalette(QtWidgets.QWidget):
    """See the module docstring."""

    sigAddRequested = QtCore.Signal(str, str)  # step kind, plugin id ("" for source/save)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Search steps…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        self.search.returnPressed.connect(self._add_first_match)
        layout.addWidget(self.search)
        hint = QtWidgets.QLabel("Drag onto the canvas, or double-click to add next to the selected step.")
        hint.setWordWrap(True)
        hint.setEnabled(False)
        layout.addWidget(hint)
        self.tree = _Tree()
        self.tree.itemActivated.connect(self._on_activated)
        layout.addWidget(self.tree, 1)
        self._catalog = None
        self._recent: list[tuple[str, str]] = []

    # -- content -----------------------------------------------------------------------

    def set_catalog(self, catalog, recent=()) -> None:
        self._catalog = catalog
        self._recent = list(recent)
        self._build()

    def set_recent(self, recent) -> None:
        self._recent = list(recent)
        self._build()

    def _build(self) -> None:
        tree = self.tree
        tree.clear()
        catalog = self._catalog
        if catalog is None:
            return
        entries = {}
        for entry in catalog.reconstructors:
            entries[("reconstruct", entry.id)] = entry
        for entry in catalog.processors:
            entries[("process", entry.id)] = entry

        recent = [key for key in self._recent if key in entries or key in (("source", ""), ("save", ""))]
        if recent:
            node = self._root("Recent")
            for key in recent[:6]:
                if key == ("source", ""):
                    self._source(node)
                elif key == ("save", ""):
                    self._save(node)
                else:
                    self._plugin_leaf(node, key[0], entries[key])

        steps = self._root("Steps")
        self._source(steps)
        self._save(steps)
        reconstruct = self._root("Reconstruct")
        for entry in catalog.reconstructors:
            if entry.origin != USER:
                self._plugin_leaf(reconstruct, "reconstruct", entry)
        for entry in catalog.consolidators():
            self._plugin_leaf(reconstruct, "consolidate", entry, title=f"Consolidate · {entry.name}")
        for category in catalog.categories():
            node = self._root(category, colour=st.CATEGORY_COLOURS.get(category, st.OTHER_COLOUR))
            for entry in catalog.processors_in(category):
                if entry.origin != USER:
                    self._plugin_leaf(node, "process", entry)
        dropins = [e for e in catalog.processors + catalog.reconstructors if e.origin == USER]
        if dropins:
            node = self._root("Drop-in plugins")
            for entry in dropins:
                self._plugin_leaf(node, "process" if entry.kind == PROCESSOR else "reconstruct", entry)
        tree.expandAll()
        self._filter(self.search.text())

    def _root(self, title: str, colour: str | None = None) -> QtWidgets.QTreeWidgetItem:
        item = QtWidgets.QTreeWidgetItem(self.tree, [title])
        item.setFlags(item.flags() & ~QtCore.Qt.ItemIsSelectable & ~QtCore.Qt.ItemIsDragEnabled)
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        if colour:
            item.setIcon(0, _chip(colour))
        return item

    def _leaf(self, parent, title: str, kind: str, plugin_id: str, tooltip: str, colour: str, words: str = ""):
        item = QtWidgets.QTreeWidgetItem(parent, [title])
        item.setData(0, _KIND_ROLE, kind)
        item.setData(0, _PLUGIN_ROLE, plugin_id)
        item.setData(0, _TEXT_ROLE, " ".join((title, plugin_id, tooltip, words)).lower())
        item.setToolTip(0, tooltip)
        item.setIcon(0, _chip(colour))
        return item

    def _source(self, parent) -> None:
        self._leaf(parent, "Recording (source)", "source", "",
                   "A recording: a file, or the one bound when the workflow runs", st.STEP_COLOURS["source"], "source")

    def _save(self, parent) -> None:
        self._leaf(parent, "Save to a file", "save", "", "Write one result to a file", st.STEP_COLOURS["save"], "save")

    def _plugin_leaf(self, parent, kind: str, entry, title: str | None = None) -> None:
        tooltip = entry.description or entry.name
        if entry.kind == PROCESSOR:
            tooltip += f"\nInputs: {entry.arity}; takes: {', '.join(entry.accepted_kinds) or 'anything'}"
        if entry.gui_only:
            tooltip += f"\nNot available in workflows: {entry.gui_only}"
        colour = st.STEP_COLOURS.get(kind) or st.CATEGORY_COLOURS.get(entry.category, st.OTHER_COLOUR)
        item = self._leaf(parent, title or entry.name, kind, entry.id, tooltip, colour, entry.category)
        if entry.gui_only:
            item.setFlags(item.flags() & ~QtCore.Qt.ItemIsEnabled)

    # -- search ---------------------------------------------------------------------------

    def _filter(self, text: str) -> None:
        words = str(text).lower().split()
        for index in range(self.tree.topLevelItemCount()):
            root = self.tree.topLevelItem(index)
            shown = 0
            for row in range(root.childCount()):
                child = root.child(row)
                haystack = str(child.data(0, _TEXT_ROLE) or "")
                visible = all(word in haystack for word in words)
                child.setHidden(not visible)
                shown += visible
            root.setHidden(shown == 0)

    def visible_choices(self) -> list[tuple[str, str]]:
        found = []
        for index in range(self.tree.topLevelItemCount()):
            root = self.tree.topLevelItem(index)
            if root.isHidden():
                continue
            for row in range(root.childCount()):
                child = root.child(row)
                if not child.isHidden() and child.flags() & QtCore.Qt.ItemIsEnabled:
                    found.append((str(child.data(0, _KIND_ROLE)), str(child.data(0, _PLUGIN_ROLE) or "")))
        return found

    def all_choices(self) -> set[tuple[str, str]]:
        found = set()
        for index in range(self.tree.topLevelItemCount()):
            root = self.tree.topLevelItem(index)
            for row in range(root.childCount()):
                child = root.child(row)
                found.add((str(child.data(0, _KIND_ROLE)), str(child.data(0, _PLUGIN_ROLE) or "")))
        return found

    def _add_first_match(self) -> None:
        choices = self.visible_choices()
        if choices:
            self.sigAddRequested.emit(*choices[0])

    def current_choice(self) -> tuple[str, str] | None:
        item = self.tree.currentItem()
        if item is None or not item.data(0, _KIND_ROLE):
            return None
        return str(item.data(0, _KIND_ROLE)), str(item.data(0, _PLUGIN_ROLE) or "")

    def _on_activated(self, item, _column) -> None:
        kind = item.data(0, _KIND_ROLE)
        if kind and item.flags() & QtCore.Qt.ItemIsEnabled:
            self.sigAddRequested.emit(str(kind), str(item.data(0, _PLUGIN_ROLE) or ""))


__all__ = ["StepPalette"]
