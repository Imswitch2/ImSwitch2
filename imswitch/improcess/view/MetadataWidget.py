"""Panel that shows the full metadata hierarchy of a measurement file.

The widget renders whatever :mod:`imswitch.improcess.model.metadata_tree`
found — it never assumes a particular file layout, so a recording written by a
future storer (or by another program entirely) displays without changes here.
"""

from __future__ import annotations

import json

from qtpy import QtCore, QtGui, QtWidgets

from imswitch.improcess.model.metadata_tree import (
    DEFAULT_LIMITS,
    KIND_ARRAY,
    KIND_ATTRIBUTE,
    KIND_ERROR,
    KIND_GROUP,
    KIND_INFO,
    KIND_ROOT,
    MetadataNode,
    metadata_tree_rows,
    metadata_tree_to_dict,
)


_NODE_ROLE = QtCore.Qt.UserRole + 1
_MATCH_ROLE = QtCore.Qt.UserRole + 2

#: Full values are also offered as tooltips, but an unbounded tooltip (an
#: embedded OME-XML document, say) is unusable — cap what we put in one.
_TOOLTIP_MAX_CHARS = 2000


class MetadataWidget(QtWidgets.QWidget):
    """Hierarchical, read-only view of a measurement file's metadata."""

    #: Emitted when the user asks to read metadata from a file they pick.
    sigOpenFileRequested = QtCore.Signal()
    #: Emitted when the user asks to re-read the current source.
    sigReloadRequested = QtCore.Signal()
    #: ``(columns, records)`` for the shared Results dock (same contract as
    #: the Profile and ROI stats panels).
    sigResultPushed = QtCore.Signal(object, object)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._tree: MetadataNode | None = None
        self._filter = ""

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(4, 4, 4, 4)
        self.setLayout(layout)

        self._sourceText = ""
        self.sourceLabel = QtWidgets.QLabel("No file loaded")
        self.sourceLabel.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        # A measurement path is long; keep it on one line (elided, full text in
        # the tooltip) so the header never eats three rows of the panel.
        self.sourceLabel.setWordWrap(False)
        self.sourceLabel.setSizePolicy(
            QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred
        )
        self.openButton = QtWidgets.QPushButton("Open file...")
        self.openButton.setToolTip("Read metadata from any supported file, without loading its data")
        self.openButton.clicked.connect(self.sigOpenFileRequested)
        self.reloadButton = QtWidgets.QPushButton("Reload")
        self.reloadButton.setToolTip("Re-read the metadata of the current file")
        self.reloadButton.setEnabled(False)
        self.reloadButton.clicked.connect(self.sigReloadRequested)

        sourceRow = QtWidgets.QHBoxLayout()
        sourceRow.setContentsMargins(0, 0, 0, 0)
        sourceRow.addWidget(self.sourceLabel, 1)
        sourceRow.addWidget(self.openButton)
        sourceRow.addWidget(self.reloadButton)
        layout.addLayout(sourceRow)

        self.filterEdit = QtWidgets.QLineEdit()
        self.filterEdit.setPlaceholderText("Filter by name or value...")
        self.filterEdit.setClearButtonEnabled(True)
        self.filterEdit.textChanged.connect(self.applyFilter)
        layout.addWidget(self.filterEdit)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Name", "Value", "Type"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.tree.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.tree.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._onContextMenu)
        header = self.tree.header()
        # Value takes the slack so Name and Type are always both on screen;
        # values too long for the column are elided, with the full text in the
        # tooltip and in "Copy value".
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        header.setStretchLastSection(False)
        layout.addWidget(self.tree, 1)

        self.expandButton = QtWidgets.QPushButton("Expand all")
        self.expandButton.clicked.connect(self.tree.expandAll)
        self.collapseButton = QtWidgets.QPushButton("Collapse all")
        self.collapseButton.clicked.connect(self._collapseToTop)
        self.copyButton = QtWidgets.QPushButton("Copy")
        self.copyButton.setToolTip("Copy the selected rows as tab-separated text")
        self.copyButton.clicked.connect(self.copySelectionToClipboard)
        self.exportButton = QtWidgets.QPushButton("Export...")
        self.exportButton.setToolTip("Save the whole metadata tree as JSON or CSV")
        self.exportButton.clicked.connect(self._onExport)
        self.pushButton = QtWidgets.QPushButton("To Results")
        self.pushButton.setToolTip("Send the visible metadata entries to the Results table")
        self.pushButton.clicked.connect(self.pushToResults)

        buttonRow = QtWidgets.QHBoxLayout()
        buttonRow.setContentsMargins(0, 0, 0, 0)
        for button in (
            self.expandButton,
            self.collapseButton,
            self.copyButton,
            self.exportButton,
            self.pushButton,
        ):
            buttonRow.addWidget(button)
        layout.addLayout(buttonRow)

        self.statusLabel = QtWidgets.QLabel("")
        self.statusLabel.setWordWrap(True)
        layout.addWidget(self.statusLabel)

        self._setActionsEnabled(False)

    # -- public API ----------------------------------------------------------

    def setMetadataTree(self, node: MetadataNode | None, sourceLabel: str | None = None) -> None:
        """Render ``node`` (or clear the panel when it is None)."""
        self._tree = node
        self.tree.clear()
        if sourceLabel is not None:
            self.setSourceLabel(sourceLabel)
        if node is None:
            self._setActionsEnabled(False)
            self.setStatus("")
            return

        rootItem = self._makeItem(node)
        self.tree.addTopLevelItem(rootItem)
        rootItem.setExpanded(True)
        for child in range(rootItem.childCount()):
            rootItem.child(child).setExpanded(True)
        self._setActionsEnabled(True)
        self.setStatus(f"{node.count()} entries")
        self.applyFilter(self._filter)

    def currentTree(self) -> MetadataNode | None:
        return self._tree

    def setSourceLabel(self, text: str) -> None:
        self._sourceText = str(text or "")
        self.sourceLabel.setToolTip(self._sourceText)
        self._updateSourceLabelText()

    def sourceLabelText(self) -> str:
        """The full (un-elided) source text currently shown in the header."""
        return self._sourceText

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._updateSourceLabelText()

    def _updateSourceLabelText(self) -> None:
        text = self._sourceText or "No file loaded"
        metrics = QtGui.QFontMetrics(self.sourceLabel.font())
        width = max(self.sourceLabel.width(), 80)
        self.sourceLabel.setText(
            metrics.elidedText(text, QtCore.Qt.ElideMiddle, width)
        )

    def setStatus(self, text: str) -> None:
        self.statusLabel.setText(text or "")

    def setReloadEnabled(self, enabled: bool) -> None:
        self.reloadButton.setEnabled(bool(enabled))

    def applyFilter(self, query: str) -> None:
        """Hide rows that neither match ``query`` nor contain a match.

        Ancestors of a match stay visible (and get expanded) so a hit deep in
        the hierarchy is still readable in context.
        """
        self._filter = str(query or "")
        needle = self._filter.strip().lower()
        root = self.tree.topLevelItem(0)
        if root is None:
            return
        matches = self._applyFilterToItem(root, needle)
        if not needle:
            self.setStatus(f"{self._tree.count()} entries" if self._tree else "")
        else:
            self.setStatus(f"{matches} of {self._tree.count()} entries match")

    def visibleRows(self) -> list[dict[str, str]]:
        """``path``/``value``/``type`` rows for everything the filter leaves visible."""
        rows: list[dict[str, str]] = []
        root = self.tree.topLevelItem(0)
        if root is None:
            return rows
        stack = [root]
        while stack:
            item = stack.pop()
            for index in range(item.childCount()):
                stack.append(item.child(index))
            if item.isHidden():
                continue
            node = item.data(0, _NODE_ROLE)
            if node is None or node.kind in (KIND_ROOT, KIND_GROUP):
                continue
            rows.append(
                {
                    "path": node.path,
                    "value": node.display_value(DEFAULT_LIMITS),
                    "type": node.detail,
                }
            )
        rows.sort(key=lambda row: row["path"])
        return rows

    def copySelectionToClipboard(self) -> None:
        items = self.tree.selectedItems()
        if not items:
            root = self.tree.topLevelItem(0)
            items = [root] if root is not None else []
        lines = [
            "\t".join(item.text(column) for column in range(3))
            for item in items
        ]
        if not lines:
            return
        clipboard = QtWidgets.QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText("\n".join(lines))
        self.setStatus(f"Copied {len(lines)} row(s)")

    def pushToResults(self) -> None:
        """Send the currently visible entries to the shared Results table."""
        rows = self.visibleRows()
        if not rows:
            return
        self.sigResultPushed.emit(["path", "value", "type"], rows)

    def toJson(self) -> str:
        if self._tree is None:
            return "{}"
        return json.dumps(metadata_tree_to_dict(self._tree), indent=2, default=str)

    def toCsv(self) -> str:
        from imswitch.improcess.view.ResultsTableWidget import records_to_csv

        if self._tree is None:
            return ""
        return records_to_csv(["path", "value", "type"], metadata_tree_rows(self._tree))

    # -- internals -----------------------------------------------------------

    def _setActionsEnabled(self, enabled: bool) -> None:
        for button in (
            self.expandButton,
            self.collapseButton,
            self.copyButton,
            self.exportButton,
            self.pushButton,
        ):
            button.setEnabled(bool(enabled))

    def _makeItem(self, node: MetadataNode) -> QtWidgets.QTreeWidgetItem:
        value = node.display_value(DEFAULT_LIMITS)
        item = QtWidgets.QTreeWidgetItem([node.name, value, node.detail])
        item.setData(0, _NODE_ROLE, node)
        item.setToolTip(0, node.path or node.name)
        if value:
            item.setToolTip(1, value[:_TOOLTIP_MAX_CHARS])
        if node.kind in (KIND_ROOT, KIND_GROUP, KIND_ARRAY):
            font = item.font(0)
            font.setBold(True)
            item.setFont(0, font)
        if node.kind == KIND_ERROR:
            item.setForeground(0, QtGui.QBrush(QtGui.QColor("#c0392b")))
            item.setForeground(1, QtGui.QBrush(QtGui.QColor("#c0392b")))
        elif node.kind == KIND_INFO:
            item.setForeground(0, QtGui.QBrush(QtGui.QColor("#888888")))
        for child in node.children:
            item.addChild(self._makeItem(child))
        return item

    def _applyFilterToItem(self, item: QtWidgets.QTreeWidgetItem, needle: str) -> int:
        """Hide/show ``item`` and its subtree; return the number of matches."""
        childMatches = 0
        for index in range(item.childCount()):
            childMatches += self._applyFilterToItem(item.child(index), needle)

        selfMatches = bool(needle) and self._itemMatches(item, needle)
        if not needle:
            item.setHidden(False)
            item.setData(0, _MATCH_ROLE, False)
            return 0

        visible = selfMatches or childMatches > 0
        item.setHidden(not visible)
        item.setData(0, _MATCH_ROLE, selfMatches)
        if childMatches:
            item.setExpanded(True)
        return childMatches + (1 if selfMatches else 0)

    @staticmethod
    def _itemMatches(item: QtWidgets.QTreeWidgetItem, needle: str) -> bool:
        return any(needle in item.text(column).lower() for column in range(3))

    def _collapseToTop(self) -> None:
        self.tree.collapseAll()
        root = self.tree.topLevelItem(0)
        if root is not None:
            root.setExpanded(True)

    def _onContextMenu(self, position) -> None:
        item = self.tree.itemAt(position)
        if item is None:
            return
        node = item.data(0, _NODE_ROLE)
        menu = QtWidgets.QMenu(self)
        copyRow = menu.addAction("Copy row")
        copyValue = menu.addAction("Copy value")
        copyPath = menu.addAction("Copy path")
        copySubtree = menu.addAction("Copy subtree as JSON")
        chosen = menu.exec_(self.tree.viewport().mapToGlobal(position))
        if chosen is None:
            return
        if chosen is copyRow:
            self.copySelectionToClipboard()
            return
        if chosen is copyValue:
            text = item.text(1)
        elif chosen is copyPath:
            text = node.path if node is not None else item.text(0)
        else:
            text = (
                json.dumps(metadata_tree_to_dict(node), indent=2, default=str)
                if node is not None
                else ""
            )
        clipboard = QtWidgets.QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)

    def _onExport(self) -> None:
        if self._tree is None:
            return
        path, selected = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Export metadata",
            f"{self._tree.name}_metadata.json",
            "JSON (*.json);;CSV (*.csv)",
        )
        if not path:
            return
        as_csv = path.lower().endswith(".csv") or "csv" in (selected or "").lower()
        try:
            with open(path, "w", encoding="utf-8") as file:
                file.write(self.toCsv() if as_csv else self.toJson())
        except OSError as exc:
            self.setStatus(f"Could not export metadata: {exc}")
            return
        self.setStatus(f"Exported metadata to {path}")


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
