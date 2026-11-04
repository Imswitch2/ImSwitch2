"""The quick-add picker: a search box over the steps that fit.

Opened when a cable is dropped in empty space (only the steps that take what
it carries), by Tab or a double-click on the canvas (everything). Typing
narrows the list; Enter, or a click, adds the step.
"""

from __future__ import annotations

from qtpy import QtCore, QtGui, QtWidgets

from imswitch.improcess.model.workfloweditor.nodes import candidates, matches

from . import style as st

_ROLE = QtCore.Qt.UserRole

_STYLE = (
    "QFrame#quickAdd { background: #17212b; border: 1px solid #3a4c5e; border-radius: 10px; }"
    "QFrame#quickAdd QLabel { color: #93a0ad; background: transparent; }"
    "QFrame#quickAdd QLineEdit { background: #0f161d; border: 1px solid #2e3e4f; border-radius: 7px;"
    " padding: 6px 9px; color: #e3e6ea; }"
    "QFrame#quickAdd QListWidget { background: #17212b; border: none; color: #d7dde3; }"
    "QFrame#quickAdd QListWidget::item { padding: 4px 2px; }"
    "QFrame#quickAdd QListWidget::item:selected { background: #34407a; color: white; border-radius: 5px; }"
)


def _dot(colour: str, size: int = 12) -> QtGui.QIcon:
    pixmap = QtGui.QPixmap(size, size)
    pixmap.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    painter.setPen(QtCore.Qt.NoPen)
    painter.setBrush(QtGui.QColor(colour))
    painter.drawRoundedRect(QtCore.QRectF(1, 1, size - 2, size - 2), 3, 3)
    painter.end()
    return QtGui.QIcon(pixmap)


def candidate_colour(candidate) -> str:
    if candidate.step_kind in st.STEP_COLOURS:
        return st.STEP_COLOURS[candidate.step_kind]
    return st.CATEGORY_COLOURS.get(candidate.category, st.OTHER_COLOUR)


class QuickAddPopup(QtWidgets.QFrame):
    """See the module docstring. ``sigChosen`` carries the chosen candidate."""

    sigChosen = QtCore.Signal(object)

    def __init__(self, catalog, *, kind: str | None = None, producer=None, title: str = "", recent=(), parent=None):
        super().__init__(parent, QtCore.Qt.Popup | QtCore.Qt.FramelessWindowHint)
        self.setObjectName("quickAdd")
        self.setStyleSheet(_STYLE)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        self.resize(400, 420)
        recent = list(recent)
        found = candidates(catalog, kind, producer=producer)
        rank = {key: index for index, key in enumerate(recent)}
        self._candidates = sorted(found, key=lambda c: (rank.get(c.key, len(rank)), 0 if c.step_kind != "process" else 1,
                                                        c.category, c.title.lower()))
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)
        heading = QtWidgets.QLabel(title or "Add a step")
        heading.setWordWrap(True)
        layout.addWidget(heading)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Type to search…")
        self.search.textChanged.connect(self._filter)
        self.search.installEventFilter(self)
        layout.addWidget(self.search)
        self.list = QtWidgets.QListWidget()
        self.list.setIconSize(QtCore.QSize(12, 12))
        self.list.itemActivated.connect(self._activated)
        self.list.itemClicked.connect(self._activated)
        layout.addWidget(self.list, 1)
        self._empty = QtWidgets.QLabel("Nothing fits." if kind is not None else "No match.")
        self._empty.hide()
        layout.addWidget(self._empty)
        self._filter("")

    def eventFilter(self, watched, event) -> bool:
        if watched is self.search and event.type() == QtCore.QEvent.KeyPress:
            key = event.key()
            if key in (QtCore.Qt.Key_Down, QtCore.Qt.Key_Up, QtCore.Qt.Key_PageDown, QtCore.Qt.Key_PageUp):
                QtWidgets.QApplication.sendEvent(self.list, event)
                return True
            if key in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
                self.accept_current()
                return True
            if key == QtCore.Qt.Key_Escape:
                self.close()
                return True
        return super().eventFilter(watched, event)

    def shown_candidates(self) -> list:
        return [self.list.item(row).data(_ROLE) for row in range(self.list.count())]

    def _filter(self, text: str) -> None:
        self.list.clear()
        for candidate in self._candidates:
            if not matches(candidate, text):
                continue
            label = candidate.title
            if candidate.category and candidate.category not in ("Steps",):
                label += f"   ·  {candidate.category}"
            item = QtWidgets.QListWidgetItem(_dot(candidate_colour(candidate)), label)
            item.setData(_ROLE, candidate)
            item.setToolTip(candidate.description or candidate.title)
            self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)
        self._empty.setVisible(self.list.count() == 0)

    def accept_current(self) -> bool:
        item = self.list.currentItem()
        if item is None:
            return False
        self._activated(item)
        return True

    def _activated(self, item) -> None:
        candidate = item.data(_ROLE)
        self.close()
        self.sigChosen.emit(candidate)

    def popup_at(self, global_pos: QtCore.QPoint) -> None:
        screen = QtWidgets.QApplication.screenAt(global_pos) if hasattr(QtWidgets.QApplication, "screenAt") else None
        rect = screen.availableGeometry() if screen is not None else QtCore.QRect(0, 0, 4000, 4000)
        x = min(max(rect.left(), global_pos.x()), rect.right() - self.width())
        y = min(max(rect.top(), global_pos.y()), rect.bottom() - self.height())
        self.move(x, y)
        self.show()
        self.search.setFocus()


__all__ = ["QuickAddPopup", "candidate_colour"]
