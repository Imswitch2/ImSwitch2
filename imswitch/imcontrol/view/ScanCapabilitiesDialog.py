"""Hardware → What this setup can scan…: the setup's scan engines and what
each can do, before anyone builds a scan (docs/design/plans/
scan-engines-and-expert-scan.md §3.3).

Read-only. The text is the report a run decides by (``capability_report``),
refreshed each time the dialog is opened: a board that had not answered may
have since.
"""

from qtpy import QtCore, QtGui, QtWidgets


class ScanCapabilitiesDialog(QtWidgets.QDialog):
    """Modeless: it is something to read beside the scan panel."""

    def __init__(self, parent=None, *args, **kwargs):
        super().__init__(parent, *args, **kwargs)
        self.setWindowTitle('What this setup can scan')
        self.setModal(False)

        self.informationLabel = QtWidgets.QLabel(
            'Every scan engine of this setup: what the scan panel does with it, each\n'
            'feature (native, emulated, impossible, or unknown when the engine does\n'
            'not declare what it needs), what it declares, and the devices on it.'
        )

        self.reportEdit = QtWidgets.QPlainTextEdit()
        self.reportEdit.setReadOnly(True)
        self.reportEdit.setLineWrapMode(QtWidgets.QPlainTextEdit.WidgetWidth)

        self.buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Close, QtCore.Qt.Horizontal, self
        )
        self.copyButton = self.buttons.addButton(
            'Copy', QtWidgets.QDialogButtonBox.ActionRole
        )
        self.copyButton.setToolTip('Copy the report to the clipboard')
        self.copyButton.clicked.connect(self._copy)
        self.buttons.rejected.connect(self.close)

        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(self.informationLabel)
        layout.addWidget(self.reportEdit)
        layout.addWidget(self.buttons)
        self.setLayout(layout)

        self.resize(720, 640)

    def setReport(self, text: str) -> None:
        self.reportEdit.setPlainText(text)

    def reportText(self) -> str:
        return self.reportEdit.toPlainText()

    def _copy(self):
        QtGui.QGuiApplication.clipboard().setText(self.reportText())


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
