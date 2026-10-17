"""Every issue the document reports, one line each; a click selects the step."""

from __future__ import annotations

from qtpy import QtCore, QtWidgets

_STEP_ROLE = QtCore.Qt.UserRole


class IssuePanel(QtWidgets.QListWidget):
    sigIssueSelected = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.itemClicked.connect(self._on_clicked)
        self.itemActivated.connect(self._on_clicked)
        self.set_issues([])

    def set_issues(self, issues) -> None:
        self.clear()
        if not issues:
            item = QtWidgets.QListWidgetItem("No issues: the workflow can run.")
            item.setFlags(item.flags() & ~QtCore.Qt.ItemIsSelectable)
            self.addItem(item)
            return
        for issue in issues:
            item = QtWidgets.QListWidgetItem(f"⚠ {issue.step}: {issue.message}")
            item.setData(_STEP_ROLE, issue.step)
            item.setToolTip(issue.message)
            self.addItem(item)

    def count_issues(self) -> int:
        return sum(1 for index in range(self.count()) if self.item(index).data(_STEP_ROLE))

    def _on_clicked(self, item) -> None:
        step = item.data(_STEP_ROLE)
        if step:
            self.sigIssueSelected.emit(str(step))


__all__ = ["IssuePanel"]


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
