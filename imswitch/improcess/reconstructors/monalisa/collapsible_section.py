"""Reusable compact disclosure / native Qt group-box section for ImProcess.

Collapsed sections are a lightweight text row without a border or hover fill.
Expanded sections are normal QGroupBoxes, matching existing ImProcess theme.
The same grid and controls remain alive in both presentation states.
"""

from __future__ import annotations

from qtpy import QtCore, QtGui, QtWidgets


class _CollapsibleNativeGroupBox(QtWidgets.QGroupBox):
    """Native section frame, with its title acting as the collapse control.

    Expanded sections deliberately use native QGroupBox painting.
    No custom border colors or orphaned header rule.
    """

    collapseRequested = QtCore.Signal()

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self._base_title = str(title)
        self._summary = ""
        self._update_title()

    def setSummary(self, summary: str) -> None:
        self._summary = str(summary)
        self._update_title()

    def _update_title(self) -> None:
        full = f"⌄ {self._base_title}"
        if self._summary:
            full += f" · {self._summary}"
        # Native QGroupBox labels do not reliably elide their title. Keep the
        # disclosure legible even at the narrow ImProcess dock width.
        title = self.fontMetrics().elidedText(
            full, QtCore.Qt.ElideRight, max(60, self.width() - 24)
        )
        if title != self.title():
            self.setTitle(title)
        self.setToolTip(f"Click title to collapse. {full}")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_title()

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            option = QtWidgets.QStyleOptionGroupBox()
            self.initStyleOption(option)
            title_rect = self.style().subControlRect(
                QtWidgets.QStyle.CC_GroupBox,
                option,
                QtWidgets.QStyle.SC_GroupBoxLabel,
                self,
            )
            if title_rect.contains(event.pos()):
                self.collapseRequested.emit()
                event.accept()
                return
        super().mouseReleaseEvent(event)


class _ThinDisclosureHeader(QtWidgets.QWidget):
    """Results-splitter-style disclosure row, shared by compact sections.

    Mirrors ``_SessionSplitterHandle`` in ProcessingSessionFrame: a text-only
    transparent toggle and one hairline to the right of its text. There is no
    native section chrome or hover background.
    """

    summaryChanged = QtCore.Signal(str)

    def __init__(self, parent=None, *, title="Fitting", show_rule=True, bold_title=False):
        super().__init__(parent)
        self._title = str(title)
        self._show_rule = bool(show_rule)
        self._bold_title = bool(bold_title)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self.setStyleSheet("background: transparent; border: none;")
        self.setMinimumWidth(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        self._summary = ""
        self._status = ""
        self._status_tooltip = ""

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.toggle = QtWidgets.QToolButton(self)
        self.toggle.setCheckable(True)
        self.toggle.setChecked(False)
        self.toggle.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
        self.toggle.setAutoRaise(True)
        # Start from the application font rather than inheriting section chrome.
        title_font = QtGui.QFont(QtWidgets.QApplication.font())
        if self._bold_title:
            title_font.setBold(True)
        self.toggle.setFont(title_font)
        self.toggle.setCursor(QtCore.Qt.ArrowCursor)
        self.toggle.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
        # These rules match the Session Results splitter toggle. In particular,
        # hover and pressed MUST remain transparent even with global Qt themes.
        self.toggle.setStyleSheet(
            "QToolButton { border: none; background: transparent; padding: 0 2px 0 0; }"
            "QToolButton:hover { border: none; background: transparent; }"
            "QToolButton:pressed { border: none; background: transparent; }"
            "QToolButton:checked { border: none; background: transparent; }"
        )
        height = self.toggle.fontMetrics().height() + 2
        self.toggle.setFixedHeight(height)
        self.setFixedHeight(height)
        self.toggle.toggled.connect(self._update_toggle_text)
        layout.addWidget(self.toggle)
        self._summary_label = None
        if self._bold_title:
            # Only the section name is emphasized; the status stays subtle.
            self._summary_label = QtWidgets.QLabel(self)
            self._summary_label.setFont(QtWidgets.QApplication.font())
            self._summary_label.setStyleSheet(
                "QLabel { color: palette(mid); border: none; background: transparent; padding: 0px; }"
            )
            layout.addWidget(self._summary_label)
        layout.addStretch(1)
        self._update_toggle_text()

    def sizeHint(self):
        hint = super().sizeHint()
        hint.setHeight(self.fontMetrics().height() + 2)
        return hint

    def paintEvent(self, event):
        if not self._show_rule:
            return
        # As with the Results splitter, paint only the thin divider. Never call
        # a native frame/handle paint method (which could draw a raised surface).
        painter = QtGui.QPainter(self)
        color = self.palette().color(QtGui.QPalette.Mid)
        color.setAlpha(150)
        painter.setPen(QtGui.QPen(color, 1))
        y = self.rect().center().y()
        x = self.toggle.geometry().right() + 5
        if x < self.width():
            painter.drawLine(x, y, self.width() - 1, y)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_toggle_text()

    def setSummary(self, summary: str) -> None:
        self._summary = str(summary)
        self._update_toggle_text()
        self.summaryChanged.emit(self._summary)

    def setStatus(self, status: str, tooltip: str = "") -> None:
        self._status = str(status)
        self._status_tooltip = str(tooltip)
        self._update_toggle_text()

    def _update_toggle_text(self, *_):
        marker = "⌄" if self.toggle.isChecked() else "›"
        summary = f"{self._title} · {self._summary}" if self._summary else self._title
        # All MoNaLISA top-level sections separate the bold title from the
        # muted summary. Combined text remains available for other consumers.
        if self._summary_label is None:
            full = f"{marker} {summary}"
        else:
            full = f"{marker} {self._title}"
        available = max(55, self.width() - 28)
        displayed = self.toggle.fontMetrics().elidedText(
            full, QtCore.Qt.ElideRight, available
        )
        if self.toggle.text() != displayed:
            self.toggle.setText(displayed)
        if self._summary_label is not None:
            remaining = max(0, self.width() - self.toggle.sizeHint().width() - 16)
            if self._summary and remaining >= 20:
                label_text = self._summary_label.fontMetrics().elidedText(
                    f" · {self._summary}", QtCore.Qt.ElideRight, remaining
                )
                self._summary_label.setText(label_text)
                self._summary_label.setFixedWidth(
                    min(remaining, self._summary_label.fontMetrics().horizontalAdvance(label_text) + 2)
                )
                self._summary_label.show()
            else:
                self._summary_label.hide()
        self.toggle.setToolTip(
            f"Expand/collapse {self._title}. " + summary
            + ((". " + self._status_tooltip) if self._status_tooltip else "")
        )
        self.update()


class CompactSection(QtWidgets.QWidget):
    """Reusable native-framed section with an unobtrusive collapsed summary.

    ``grid`` owns all the client controls, ``header`` is the collapsed view,
    and ``details`` is the expanded native QGroupBox.  ``expandedChanged``
    signals only presentation changes, never scientific parameter changes.
    """

    expandedChanged = QtCore.Signal(bool)

    def __init__(self, title: str, parent=None, *, expanded: bool = False):
        super().__init__(parent)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.header = _ThinDisclosureHeader(
            self, title=title, show_rule=False, bold_title=True
        )
        self.details = _CollapsibleNativeGroupBox(title, self)
        self.toggle = self.header.toggle
        self.grid = QtWidgets.QGridLayout(self.details)
        self.grid.setContentsMargins(6, 6, 6, 6)
        self.grid.setHorizontalSpacing(6)
        self.grid.setVerticalSpacing(4)
        outer.addWidget(self.header)
        outer.addWidget(self.details)

        self.header.summaryChanged.connect(self.details.setSummary)
        self.details.collapseRequested.connect(lambda: self.toggle.setChecked(False))
        self.toggle.toggled.connect(self._set_expanded_widgets)
        self.toggle.setChecked(bool(expanded))
        self._set_expanded_widgets(self.toggle.isChecked(), notify=False)

    def setSummary(self, summary: str) -> None:
        self.header.setSummary(summary)

    def setStatus(self, status: str, tooltip: str = "") -> None:
        self.header.setStatus(status, tooltip)

    def setExpanded(self, expanded: bool) -> None:
        self.toggle.setChecked(bool(expanded))

    def isExpanded(self) -> bool:
        return self.toggle.isChecked()

    def _set_expanded_widgets(self, expanded: bool, *, notify: bool = True) -> None:
        self.header.setVisible(not expanded)
        self.details.setVisible(bool(expanded))
        if notify:
            self.expandedChanged.emit(bool(expanded))
