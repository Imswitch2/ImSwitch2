"""The start page: four ways into a workflow, the one that runs on real data first.

From the recording open in ImProcess (a source bound to it and *View only*
after it), from what was done to the selected result (its history, replayed
as steps), from a recipe (an installed Python-step snippet as Source → View
only → Python step → Save), or a blank canvas; and below, the workflows used
last and the ones in the workflow folder.
"""

from __future__ import annotations

from pathlib import Path

from qtpy import QtCore, QtWidgets

_STYLE = """
QWidget#startPage { background: palette(window); }
QLabel#startTitle { font-size: 18pt; font-weight: bold; }
QFrame#startCard { border: 1px solid rgba(127,140,155,90); border-radius: 10px; background: rgba(127,140,155,22); }
QFrame#startCard:hover { border: 1px solid #6666cc; background: rgba(102,102,204,40); }
QLabel#cardTitle { font-size: 12pt; font-weight: bold; }
QLabel#cardText { color: palette(mid); }
"""


class _Card(QtWidgets.QFrame):
    clicked = QtCore.Signal()

    def __init__(self, title: str, text: str, colour: str, parent=None):
        super().__init__(parent)
        self.setObjectName("startCard")
        self.setCursor(QtCore.Qt.PointingHandCursor)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        row = QtWidgets.QHBoxLayout()
        dot = QtWidgets.QLabel("●")
        dot.setStyleSheet(f"color: {colour}; font-size: 16pt;")
        row.addWidget(dot)
        self.title = QtWidgets.QLabel(title)
        self.title.setObjectName("cardTitle")
        row.addWidget(self.title, 1)
        layout.addLayout(row)
        self.text = QtWidgets.QLabel(text)
        self.text.setObjectName("cardText")
        self.text.setWordWrap(True)
        layout.addWidget(self.text)
        self.extra = QtWidgets.QVBoxLayout()
        layout.addLayout(self.extra)
        layout.addStretch(1)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == QtCore.Qt.LeftButton and self.isEnabled() and self.rect().contains(event.pos()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class StartPage(QtWidgets.QScrollArea):
    """See the module docstring."""

    sigFromRecording = QtCore.Signal()
    sigFromResult = QtCore.Signal()
    sigRecipe = QtCore.Signal(object)
    sigBlank = QtCore.Signal()
    sigOpenFile = QtCore.Signal(str)
    sigOpenDialog = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        body = QtWidgets.QWidget()
        body.setObjectName("startPage")
        body.setStyleSheet(_STYLE)
        self.setWidget(body)
        outer = QtWidgets.QVBoxLayout(body)
        outer.setContentsMargins(40, 30, 40, 30)
        outer.setSpacing(14)
        title = QtWidgets.QLabel("Start a workflow")
        title.setObjectName("startTitle")
        outer.addWidget(title)
        lead = QtWidgets.QLabel("A workflow is a chain of steps — open a recording, reconstruct, process, save — "
                                "that you can run on this data now and on other recordings later.")
        lead.setWordWrap(True)
        outer.addWidget(lead)

        grid = QtWidgets.QGridLayout()
        grid.setSpacing(14)
        self.recording_card = _Card("From the recording that is open", "", "#3c9a6e")
        self.recording_card.clicked.connect(self.sigFromRecording.emit)
        self.result_card = _Card("From what you did to a result", "", "#3b8bb5")
        self.result_card.clicked.connect(self.sigFromResult.emit)
        self.recipe_card = _Card("From a recipe", "A few lines of Python with a recording in and the outputs "
                                 "saved; pick one:", "#a8905f")
        self.recipes = QtWidgets.QListWidget()
        self.recipes.setMinimumHeight(150)
        self.recipes.itemActivated.connect(lambda item: self.sigRecipe.emit(item.data(QtCore.Qt.UserRole)))
        self.recipes.itemClicked.connect(lambda item: self.sigRecipe.emit(item.data(QtCore.Qt.UserRole)))
        self.recipe_card.extra.addWidget(self.recipes)
        self.blank_card = _Card("A blank canvas", "Drag steps from the list on the left and connect them.", "#6666cc")
        self.blank_card.clicked.connect(self.sigBlank.emit)
        grid.addWidget(self.recording_card, 0, 0)
        grid.addWidget(self.result_card, 0, 1)
        grid.addWidget(self.recipe_card, 1, 0)
        grid.addWidget(self.blank_card, 1, 1)
        outer.addLayout(grid)

        files = QtWidgets.QHBoxLayout()
        recent_box = QtWidgets.QVBoxLayout()
        recent_box.addWidget(QtWidgets.QLabel("<b>Recent workflows</b>"))
        self.recent = QtWidgets.QListWidget()
        self.recent.setMaximumHeight(150)
        self.recent.itemActivated.connect(lambda item: self.sigOpenFile.emit(item.data(QtCore.Qt.UserRole)))
        recent_box.addWidget(self.recent)
        folder_box = QtWidgets.QVBoxLayout()
        self.folder_label = QtWidgets.QLabel("<b>In the workflow folder</b>")
        folder_box.addWidget(self.folder_label)
        self.folder = QtWidgets.QListWidget()
        self.folder.setMaximumHeight(150)
        self.folder.itemActivated.connect(lambda item: self.sigOpenFile.emit(item.data(QtCore.Qt.UserRole)))
        folder_box.addWidget(self.folder)
        files.addLayout(recent_box)
        files.addLayout(folder_box)
        outer.addLayout(files)
        open_button = QtWidgets.QPushButton("Open a workflow file…")
        open_button.clicked.connect(self.sigOpenDialog.emit)
        outer.addWidget(open_button, 0, QtCore.Qt.AlignLeft)
        outer.addStretch(1)

    def set_state(self, *, recording: str | None, result: str | None, recipes, recent, folder, folder_files) -> None:
        self.recording_card.setEnabled(bool(recording))
        self.recording_card.text.setText(
            f"Runs on <b>{Path(recording).name}</b> straight away: a source bound to it, shown as it is "
            "(View only); add steps after it." if recording else
            "Open a recording in ImProcess first; it then appears here.")
        self.result_card.setEnabled(bool(result))
        self.result_card.text.setText(
            f"The steps that made <b>{result}</b>, from its history, ready to change and run again or to save "
            "for other data." if result else "Select a result in the reconstruction list first: everything you "
            "did to it is recorded, and becomes the workflow.")
        self.recipes.clear()
        for recipe in recipes:
            item = QtWidgets.QListWidgetItem(f"{recipe.title} — {recipe.summary}" if recipe.summary else recipe.title)
            item.setData(QtCore.Qt.UserRole, recipe)
            item.setToolTip("\n".join(recipe.about))
            self.recipes.addItem(item)
        if not recipes:
            item = QtWidgets.QListWidgetItem("No snippets installed yet.")
            item.setFlags(QtCore.Qt.NoItemFlags)
            self.recipes.addItem(item)
        self._fill(self.recent, recent, "No recent workflows.")
        self.folder_label.setText(f"<b>In the workflow folder</b> ({folder})")
        self._fill(self.folder, folder_files, "No workflow files there yet.")

    @staticmethod
    def _fill(widget: QtWidgets.QListWidget, paths, empty: str) -> None:
        widget.clear()
        for path in paths:
            item = QtWidgets.QListWidgetItem(Path(path).name)
            item.setData(QtCore.Qt.UserRole, str(path))
            item.setToolTip(str(path))
            widget.addItem(item)
        if not paths:
            item = QtWidgets.QListWidgetItem(empty)
            item.setFlags(QtCore.Qt.NoItemFlags)
            widget.addItem(item)


__all__ = ["StartPage"]
