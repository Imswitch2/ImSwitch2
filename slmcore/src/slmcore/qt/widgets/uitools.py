"""Small reusable Qt widgets used by the slmcore Qt projection layer."""

from __future__ import annotations



from qtpy import QtCore,QtGui,QtWidgets


class ElidedLabel(QtWidgets.QLabel):
    """QLabel that elides long text and exposes the full text as a tooltip."""

    def __init__(self,text: str="",parent=None):
        super().__init__(parent)
        self._full_text = ""
        self.setMinimumWidth(50)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Preferred,
        )
        self.set_full_text(text)

    def full_text(self) -> str:
        return self._full_text

    def set_full_text(self,text: str) -> None:
        self._full_text = str(text or "")
        self.setToolTip(self._full_text)
        self._update_elision()

    def resizeEvent(self,event) -> None:
        super().resizeEvent(event)
        self._update_elision()

    def _update_elision(self) -> None:
        width = self.contentsRect().width()
        if width <= 0:
            super().setText(self._full_text)
            return
        super().setText(self.fontMetrics().elidedText(
            self._full_text,QtCore.Qt.ElideRight,width,
        ))


class BetterPushButton(QtWidgets.QPushButton):
    """QPushButton that keeps a usable minimum width under custom styles."""

    def __init__(self,text=None,min_min_width: int=20,*args,**kwargs):
        super().__init__(text,*args,**kwargs)
        self._min_min_width = int(min_min_width)
        self._update_minimum_width(text)

    def setText(self,text,*args,**kwargs):
        super().setText(text,*args,**kwargs)
        self._update_minimum_width(text)

    def _update_minimum_width(self,text=None) -> None:
        text = self.text() if text is None else str(text)
        metrics = QtGui.QFontMetrics(self.font())
        horizontal_advance = getattr(metrics,"horizontalAdvance",None)
        text_width = (
            horizontal_advance(text)
            if callable(horizontal_advance)
            else metrics.width(text)
        )
        self.setMinimumWidth(max(self._min_min_width,text_width + 8))


class CollapsibleSection(QtWidgets.QWidget):
    """Compact collapsible content container with optional vertical resizing."""

    sigExpandedChanged = QtCore.Signal(bool)
    sigContentHeightChanged = QtCore.Signal(int)

    def __init__(
        self,
        title: str="",
        parent=None,
        target_height: int | None=None,
        frame: bool=True,
        button_height: int | None=None,
        button_width: int | None=None,
        fontsize: int=10,
        expanded: bool=False,
        resizable: bool=False,
        min_content_height: int=0,
    ):
        super().__init__(parent)

        self.target_height = target_height
        self._resizable = bool(resizable)
        self._min_content_height = max(0,int(min_content_height))
        if self._resizable:
            initial_height = None if target_height is None else int(target_height)
            if (
                initial_height is not None
                and initial_height < self._min_content_height
            ):
                raise ValueError(
                    "target_height must be >= min_content_height when resizable"
                )
            self._content_height = initial_height
        else:
            self._content_height = None
        self.contentWidget: QtWidgets.QWidget | None = None

        self.toggleButton = QtWidgets.QToolButton(
            text=title,checkable=True,checked=False,
        )
        if button_height:
            self.toggleButton.setFixedHeight(button_height)
        if button_width:
            self.toggleButton.setFixedWidth(button_width)

        self.toggleButton.setStyleSheet(
            "QToolButton {"
            "border: none;"
            f"font-size: {int(fontsize)}px;"
            "padding: 1px 1px;"
            "}"
        )
        self.toggleButton.setToolButtonStyle(
            QtCore.Qt.ToolButtonTextBesideIcon,
        )
        self.toggleButton.clicked.connect(self._on_pressed)

        self.contentArea = QtWidgets.QScrollArea()
        self.contentArea.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Fixed,
        )
        self.contentArea.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.contentArea.setWidgetResizable(True)
        self.contentArea.setMinimumHeight(0)
        self.contentArea.setMaximumHeight(0)
        if not frame:
            self.contentArea.setStyleSheet("QScrollArea { border: none; }")

        self.headerLayout = QtWidgets.QHBoxLayout()
        self.headerLayout.setContentsMargins(0,0,0,0)
        self.headerLayout.setSpacing(2)
        self.headerLayout.addWidget(self.toggleButton)
        self.headerLayout.addStretch()

        main_layout = QtWidgets.QVBoxLayout(self)
        main_layout.setSpacing(0)
        main_layout.setContentsMargins(0,0,0,0)
        main_layout.addLayout(self.headerLayout)
        main_layout.addWidget(self.contentArea)

        self._resize_handle = _CollapsibleResizeHandle(self)
        self._resize_handle.setVisible(False)
        main_layout.addWidget(self._resize_handle)

        self.toggleAnimation = QtCore.QPropertyAnimation(
            self.contentArea,b"maximumHeight",
        )
        self.toggleAnimation.setDuration(50)
        self.toggleAnimation.finished.connect(self._on_animation_finished)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Preferred,
        )

        self.set_expanded(expanded,animate=False)

    @property
    def expanded(self) -> bool:
        return bool(self.toggleButton.isChecked())

    @property
    def resizable(self) -> bool:
        return self._resizable

    def content_height(self) -> int:
        """Return the retained expanded content height."""
        return self._expanded_height()

    def set_content_height(self,height: int) -> None:
        """Set the retained expanded height of a resizable section."""
        if not self._resizable:
            raise RuntimeError("This CollapsibleSection is not resizable")
        height = max(self._min_content_height,int(height))
        if height == self._content_height:
            return
        self._content_height = height
        self.target_height = height
        if self.expanded:
            self.toggleAnimation.stop()
            self.contentArea.setMinimumHeight(height)
            self.contentArea.setMaximumHeight(height)
            self.updateGeometry()
        self.sigContentHeightChanged.emit(height)

    def set_title(self,title: str) -> None:
        self.toggleButton.setText(str(title))

    def setContentLayout(self,content_layout) -> None:
        """Set or replace the inner content layout."""
        content_widget = QtWidgets.QWidget()
        content_widget.setLayout(content_layout)
        content_widget.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Expanding,
        )
        self.contentWidget = content_widget
        self.contentArea.setWidget(content_widget)
        if self.expanded:
            self.set_expanded(True,animate=False)

    def addHeaderWidget(self,widget,position=None) -> None:
        """Add a widget to the right side of the section header."""
        if position is None:
            position = self.headerLayout.count() - 1
        self.headerLayout.insertWidget(position,widget)

    def set_expanded(self,expanded: bool,*,animate: bool=True) -> None:
        expanded = bool(expanded)
        blocker = QtCore.QSignalBlocker(self.toggleButton)
        try:
            self.toggleButton.setChecked(expanded)
        finally:
            del blocker

        self.toggleButton.setArrowType(
            QtCore.Qt.DownArrow if expanded else QtCore.Qt.RightArrow,
        )
        target = self._expanded_height() if expanded else 0

        self.toggleAnimation.stop()
        self.contentArea.setMinimumHeight(0)
        self._resize_handle.setVisible(self._resizable and expanded)
        if animate:
            self.toggleAnimation.setStartValue(self.contentArea.maximumHeight())
            self.toggleAnimation.setEndValue(target)
            self.toggleAnimation.start()
        else:
            self.contentArea.setMaximumHeight(target)
            self._finish_height_constraints(expanded,target)

        self.sigExpandedChanged.emit(expanded)

    def _on_pressed(self,checked=None) -> None:
        self.set_expanded(
            self.toggleButton.isChecked() if checked is None else bool(checked),
            animate=True,
        )

    def _on_animation_finished(self) -> None:
        target = self._expanded_height() if self.expanded else 0
        self._finish_height_constraints(self.expanded,target)

    def _finish_height_constraints(self,expanded: bool,target: int) -> None:
        if self._resizable:
            self.contentArea.setMinimumHeight(target if expanded else 0)
            self.contentArea.setMaximumHeight(target if expanded else 0)
        else:
            self.contentArea.setMinimumHeight(0)
            self.contentArea.setMaximumHeight(target)
        self.updateGeometry()

    def _expanded_height(self) -> int:
        if self._resizable and self._content_height is not None:
            return int(self._content_height)
        if self.target_height is not None:
            height = int(self.target_height)
        elif self.contentWidget is None or self.contentWidget.layout() is None:
            height = self._min_content_height if self._resizable else 0
        else:
            self.contentWidget.layout().activate()
            self.contentWidget.updateGeometry()
            height = int(self.contentWidget.layout().sizeHint().height())
        if self._resizable:
            height = max(self._min_content_height,height)
            self._content_height = height
        return height


class _CollapsibleResizeHandle(QtWidgets.QWidget):
    """Small vertical drag handle that resizes its CollapsibleSection."""

    def __init__(self,section: CollapsibleSection) -> None:
        super().__init__(section)
        self._section = section
        self._drag_origin_y: int | None = None
        self._drag_origin_height: int | None = None
        self.setFixedHeight(6)
        self.setCursor(QtCore.Qt.SizeVerCursor)
        self.setStyleSheet(
            "QWidget { border-top: 1px solid rgba(128,128,128,80); }"
        )

    @staticmethod
    def _global_y(event) -> int:
        getter = getattr(event,"globalY",None)
        if callable(getter):
            return int(getter())
        position = event.globalPosition()
        return int(position.y())

    def mousePressEvent(self,event) -> None:
        if event.button() != QtCore.Qt.LeftButton:
            super().mousePressEvent(event)
            return
        self._drag_origin_y = self._global_y(event)
        self._drag_origin_height = self._section.content_height()
        event.accept()

    def mouseMoveEvent(self,event) -> None:
        if self._drag_origin_y is None or self._drag_origin_height is None:
            super().mouseMoveEvent(event)
            return
        delta = self._global_y(event) - self._drag_origin_y
        self._section.set_content_height(self._drag_origin_height + delta)
        event.accept()

    def mouseReleaseEvent(self,event) -> None:
        self._drag_origin_y = None
        self._drag_origin_height = None
        event.accept()
