"""The step palette: what can be added, from the catalogue.

Sources and saves are steps of their own; reconstructors, consolidations
and processors are listed by plugin, processors under their category and
drop-in plugins under a root of their own, as the main window's toolbars
keep them apart. A plugin the headless contract refuses is listed greyed
with the reason, so nobody wonders where it went.
"""

from __future__ import annotations

from qtpy import QtCore, QtWidgets

from imswitch.improcess.model.workfloweditor.catalog import PROCESSOR, USER

_KIND_ROLE = QtCore.Qt.UserRole
_PLUGIN_ROLE = QtCore.Qt.UserRole + 1


class PluginPalette(QtWidgets.QTreeWidget):
    """Double-click (or Enter) on a leaf asks the window to add that step."""

    sigAddRequested = QtCore.Signal(str, str)  # step kind, plugin id ("" for source/save)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setColumnCount(1)
        self.itemActivated.connect(self._on_activated)

    def set_catalog(self, catalog) -> None:
        self.clear()
        self._leaf(self, "Source", "source", "", "A recording: a file, or bound when the workflow runs")

        reconstruct = self._root("Reconstruct")
        for entry in catalog.reconstructors:
            if entry.origin != USER:
                self._plugin_leaf(reconstruct, "reconstruct", entry)
        consolidate = self._root("Consolidate")
        for entry in catalog.consolidators():
            self._plugin_leaf(consolidate, "consolidate", entry)

        process = self._root("Process")
        for category in catalog.categories():
            node = QtWidgets.QTreeWidgetItem(process, [category])
            node.setFlags(node.flags() & ~QtCore.Qt.ItemIsSelectable)
            for entry in catalog.processors_in(category):
                if entry.origin != USER:
                    self._plugin_leaf(node, "process", entry)
        process.setExpanded(True)

        dropins = [e for e in catalog.processors + catalog.reconstructors if e.origin == USER]
        if dropins:
            node = self._root("Drop-in plugins")
            for entry in dropins:
                self._plugin_leaf(node, "process" if entry.kind == PROCESSOR else "reconstruct", entry)

        self._leaf(self, "Save", "save", "", "Write one result to a file")
        for index in range(self.topLevelItemCount()):
            self.topLevelItem(index).setExpanded(True)

    def _root(self, title: str) -> QtWidgets.QTreeWidgetItem:
        item = QtWidgets.QTreeWidgetItem(self, [title])
        item.setFlags(item.flags() & ~QtCore.Qt.ItemIsSelectable)
        return item

    @staticmethod
    def _leaf(parent, title: str, kind: str, plugin_id: str, tooltip: str) -> QtWidgets.QTreeWidgetItem:
        item = QtWidgets.QTreeWidgetItem(parent, [title])
        item.setData(0, _KIND_ROLE, kind)
        item.setData(0, _PLUGIN_ROLE, plugin_id)
        item.setToolTip(0, tooltip)
        return item

    def _plugin_leaf(self, parent, kind: str, entry) -> None:
        tooltip = entry.description or entry.name
        if entry.kind == PROCESSOR:
            tooltip += f"\nInputs: {entry.arity}; accepts: {', '.join(entry.accepted_kinds) or 'any'}"
        if entry.gui_only:
            tooltip += f"\nNot available in workflows: {entry.gui_only}"
        item = self._leaf(parent, entry.name, kind, entry.id, tooltip)
        if entry.gui_only:
            item.setFlags(item.flags() & ~QtCore.Qt.ItemIsEnabled)

    def current_choice(self) -> tuple[str, str] | None:
        item = self.currentItem()
        if item is None:
            return None
        kind = item.data(0, _KIND_ROLE)
        if not kind:
            return None
        return str(kind), str(item.data(0, _PLUGIN_ROLE) or "")

    def _on_activated(self, item, _column) -> None:
        kind = item.data(0, _KIND_ROLE)
        if kind and item.flags() & QtCore.Qt.ItemIsEnabled:
            self.sigAddRequested.emit(str(kind), str(item.data(0, _PLUGIN_ROLE) or ""))


__all__ = ["PluginPalette"]


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
