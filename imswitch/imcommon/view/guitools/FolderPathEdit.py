import os

from qtpy import QtCore, QtWidgets


class FolderPathEdit(QtWidgets.QWidget):
    """ A folder path field with a Browse… button, for settings dialogs.

    The path can be typed or picked. The picker starts at the typed folder,
    or at its nearest parent that exists, so a folder that has not been
    created yet (a recordings folder usually hasn't) still opens somewhere
    near it rather than wherever the system last was. """

    #: (text) -- the path in the field changed, typed or picked.
    textChanged = QtCore.Signal(str)

    def __init__(self, parent=None, *, caption='Choose folder', fallback='',
                 clearable=False):
        super().__init__(parent)
        self._caption = caption
        self._fallback = fallback

        self.lineEdit = QtWidgets.QLineEdit()
        self.lineEdit.setClearButtonEnabled(clearable)
        self.lineEdit.textChanged.connect(self.textChanged)
        self.browseButton = QtWidgets.QPushButton('Browse…')
        self.browseButton.clicked.connect(self.browse)

        layout = QtWidgets.QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.lineEdit, 1)
        layout.addWidget(self.browseButton)
        self.setLayout(layout)

    def text(self) -> str:
        return self.lineEdit.text()

    def setText(self, text) -> None:
        self.lineEdit.setText(str(text or ''))

    def setPlaceholderText(self, text) -> None:
        self.lineEdit.setPlaceholderText(str(text or ''))

    def browseStart(self) -> str:
        """ Where the picker opens: the typed folder or its nearest existing
        parent, else the fallback folder, else the home folder. """
        for candidate in (self.text().strip(), self._fallback):
            start = os.path.expanduser(candidate or '')
            while start and not os.path.isdir(start):
                parent = os.path.dirname(start)
                if parent == start:
                    break
                start = parent
            if start and os.path.isdir(start):
                return start
        return os.path.expanduser('~')

    def browse(self) -> None:
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, self._caption, self.browseStart()
        )
        if folder:
            self.setText(folder)
