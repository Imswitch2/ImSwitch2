"""Presentation host for retained SLM section views."""

from __future__ import annotations

from typing import Mapping

from qtpy import QtCore,QtGui,QtWidgets

from .display import SectionsDisplayMode
from .collection import SectionsCollectionView
from ..widgets.uitools import ElidedLabel


_HORIZONTAL_TITLE_HEIGHT = 28
_SECTION_SEPARATOR_COLOR = "#455364"
_SECTION_SEPARATOR_WIDTH = 2


class SectionsViewHost(QtWidgets.QWidget):
    """Mount retained ``SectionView`` widgets using a selectable UI layout.

    The host owns section titles and presentation chrome. It does not own
    backend runtime state, and replacing its collection detaches the previous
    ``SectionView`` widgets without deleting them. The caller that replaces a
    collection remains responsible for disposing the previous collection/views.
    """

    sigSettingsRequested = QtCore.Signal()
    sigSectionTitleChanged = QtCore.Signal(str,str)
    sigCurrentSectionChanged = QtCore.Signal(str)
    sigSectionsChanged = QtCore.Signal()
    sigDisplayModeChanged = QtCore.Signal(object)

    def __init__(
        self,
        collection: SectionsCollectionView,
        *,
        display_mode: SectionsDisplayMode=SectionsDisplayMode.TABS,
        section_titles: Mapping[str, str] | None=None,
        show_settings: bool=True,
        parent: QtWidgets.QWidget | None=None,
    ) -> None:
        super().__init__(parent)

        self._collection: SectionsCollectionView | None = None
        self._display_mode = SectionsDisplayMode.normalize(display_mode)
        self._section_titles: dict[str, str] = {}
        self._current_section_key: str | None = None

        self._presentation_widget: QtWidgets.QWidget | None = None
        self._tabs: QtWidgets.QTabWidget | None = None
        self._splitter: QtWidgets.QSplitter | None = None
        self._pane_widgets: dict[str, QtWidgets.QWidget] = {}
        self._pane_scrolls: dict[str, QtWidgets.QScrollArea] = {}
        self._title_labels: dict[str, _SectionTitleLabel] = {}
        self._tab_filter: QtCore.QObject | None = None

        self._show_settings = bool(show_settings)
        self._settings_button = self._create_settings_button()

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(0)

        self.set_collection(collection,section_titles=section_titles)

    @property
    def display_mode(self) -> SectionsDisplayMode:
        return self._display_mode

    def section_titles(self) -> dict[str, str]:
        return {
            key:self.section_title(key)
            for key in self.section_keys
        }

    @property
    def collection(self) -> SectionsCollectionView:
        if self._collection is None:
            raise RuntimeError("SectionsViewHost has no section collection")
        return self._collection

    @property
    def section_keys(self):
        if self._collection is None:
            return ()
        return self._collection.section_keys

    def section_geometries(self):
        """Return current physical section geometries keyed by section id."""
        if self._collection is None:
            return {}
        return {
            key:self._collection.get_section_snapshot(key).geometry
            for key in self.section_keys
        }

    def section_title(self,section_key: str) -> str:
        if section_key in self._section_titles:
            return self._section_titles[section_key]
        return self._default_title(section_key)

    def set_section_title(self,section_key: str,title) -> None:
        if self._collection is not None and section_key not in self.section_keys:
            raise KeyError(f"Unknown SLM section '{section_key}'")

        text = self._normalized_title(section_key,title)
        self._section_titles[section_key] = text

        if self._tabs is not None and self._collection is not None:
            try:
                index = self.section_keys.index(section_key)
            except ValueError:
                index = -1
            if index >= 0:
                self._tabs.setTabText(index,text)

        label = self._title_labels.get(section_key)
        if label is not None:
            label.set_full_text(text)

    def current_section_key(self) -> str | None:
        if self._tabs is not None and self._collection is not None:
            index = self._tabs.currentIndex()
            keys = self.section_keys
            if 0 <= index < len(keys):
                return keys[index]
        if self._current_section_key in self.section_keys:
            return self._current_section_key
        keys = self.section_keys
        return None if not keys else keys[0]

    def set_current_section_key(self,section_key: str | None) -> None:
        if section_key is None:
            return
        if section_key not in self.section_keys:
            raise KeyError(f"Unknown SLM section '{section_key}'")
        self._current_section_key = section_key
        if self._tabs is not None:
            index = self.section_keys.index(section_key)
            if self._tabs.currentIndex() != index:
                self._tabs.setCurrentIndex(index)

    def set_display_mode(self,display_mode) -> None:
        mode = SectionsDisplayMode.normalize(display_mode)
        if mode == self._display_mode:
            return
        current_key = self.current_section_key()
        self._display_mode = mode
        self._remount(current_key=current_key)
        self.sigDisplayModeChanged.emit(mode)

    def set_collection(
        self,
        collection: SectionsCollectionView,
        *,
        section_titles: Mapping[str, str] | None=None,
    ) -> None:
        if not isinstance(collection,SectionsCollectionView):
            raise TypeError(
                "collection must be a SectionsCollectionView"
            )

        current_key = self.current_section_key()
        previous_titles = dict(self._section_titles)
        provided_titles = dict(section_titles or {})

        self._clear_presentation()
        self._collection = collection
        self._section_titles = self._initial_titles(
            previous_titles=previous_titles,
            provided_titles=provided_titles,
        )
        if current_key not in self.section_keys:
            current_key = None
        self._current_section_key = current_key
        self._remount(current_key=current_key)
        self.sigSectionsChanged.emit()

    def rename_section(self,section_key: str) -> None:
        if section_key not in self.section_keys:
            return

        old_title = self.section_title(section_key)
        new_title,ok = QtWidgets.QInputDialog.getText(
            self,
            "Rename Section",
            "Section name:",
            text=old_title,
        )
        if not ok:
            return

        normalized = str(new_title or "").strip()
        if not normalized or normalized == old_title:
            return

        self.set_section_title(section_key,normalized)
        self.sigSectionTitleChanged.emit(section_key,normalized)

    def _initial_titles(
        self,
        *,
        previous_titles: Mapping[str,str],
        provided_titles: Mapping[str,str],
    ) -> dict[str, str]:
        titles = {}
        assert self._collection is not None
        for index,section_key in enumerate(self._collection.section_keys):
            title = provided_titles.get(section_key)
            if not title:
                title = self._snapshot_title(section_key)
            if not title:
                title = previous_titles.get(section_key)
            if not title:
                title = f"Section {index + 1}"
            titles[section_key] = str(title).strip() or f"Section {index + 1}"
        return titles

    def _remount(self,*,current_key: str | None) -> None:
        self._clear_presentation()
        if self._collection is None:
            return

        if self._display_mode == SectionsDisplayMode.HORIZONTAL:
            self._mount_horizontal()
        else:
            self._mount_tabs()

        if current_key in self.section_keys:
            self.set_current_section_key(current_key)
        elif self.section_keys:
            self.set_current_section_key(self.section_keys[0])

    def _mount_tabs(self) -> None:
        assert self._collection is not None

        tabs = QtWidgets.QTabWidget(self)
        tabs.setDocumentMode(True)
        tabs.currentChanged.connect(self._on_tab_current_changed)

        tab_bar = tabs.tabBar()
        tab_bar.tabBarDoubleClicked.connect(self._rename_tab_index)
        tab_bar.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        tab_bar.customContextMenuRequested.connect(
            self._open_tab_context_menu,
        )

        for section_key in self._collection.section_keys:
            tabs.addTab(
                self._collection.section_view(section_key),
                self.section_title(section_key),
            )

        self.layout().addWidget(tabs)
        self._presentation_widget = tabs
        self._tabs = tabs

        if self._show_settings:
            self._settings_button.setParent(tab_bar)
            self._settings_button.show()
            self._position_tab_settings_button()
            QtCore.QTimer.singleShot(0,self._position_tab_settings_button)
            self._tab_filter = _TabSettingsPositionFilter(
                tab_bar,self._position_tab_settings_button,
            )
            tab_bar.installEventFilter(self._tab_filter)

    def _mount_horizontal(self) -> None:
        assert self._collection is not None

        splitter = _SectionSplitter(QtCore.Qt.Horizontal,self)
        splitter.setObjectName("SectionsViewHostSplitter")
        splitter.set_separator_offset(_HORIZONTAL_TITLE_HEIGHT + 2)
        splitter.setChildrenCollapsible(True)
        splitter.setHandleWidth(7)
        splitter.setStyleSheet("""
            QSplitter#SectionsViewHostSplitter {
                background: transparent;
                border: none;
                padding: 0px;
                margin: 0px;
            }
            QSplitter#SectionsViewHostSplitter::handle {
                background: transparent;
                border: none;
                image: none;
                padding: 0px;
                margin: 0px;
            }
            QSplitter#SectionsViewHostSplitter::handle:horizontal {
                width: 7px;
            }
        """)

        last_key = (
            None if not self._collection.section_keys
            else self._collection.section_keys[-1]
        )
        for section_key in self._collection.section_keys:
            pane = self._create_horizontal_pane(
                section_key,
                include_settings=section_key == last_key,
            )
            splitter.addWidget(pane)
            splitter.setCollapsible(splitter.indexOf(pane),True)

        if self._collection.section_keys:
            splitter.setSizes(
                [1 for _key in self._collection.section_keys]
            )

        self.layout().addWidget(splitter)
        self._presentation_widget = splitter
        self._splitter = splitter

    def _create_horizontal_pane(
        self,
        section_key: str,
        *,
        include_settings: bool,
    ) -> QtWidgets.QWidget:
        assert self._collection is not None

        pane = QtWidgets.QWidget()
        pane.setMinimumWidth(0)
        pane_layout = QtWidgets.QVBoxLayout(pane)
        pane_layout.setContentsMargins(0,0,0,0)
        pane_layout.setSpacing(0)

        title_row = QtWidgets.QWidget(pane)
        title_row.setFixedHeight(_HORIZONTAL_TITLE_HEIGHT)
        title_layout = QtWidgets.QHBoxLayout(title_row)
        title_layout.setContentsMargins(6,4,6,4)
        title_layout.setSpacing(4)

        label = _SectionTitleLabel(self.section_title(section_key),title_row)
        label.setMinimumWidth(0)
        label.setSizePolicy(
            QtWidgets.QSizePolicy.Ignored,
            QtWidgets.QSizePolicy.Preferred,
        )
        label.doubleClicked.connect(
            lambda key=section_key:self.rename_section(key)
        )
        label.contextMenuRequested.connect(
            lambda global_pos,key=section_key:
                self._open_title_context_menu(key,global_pos)
        )
        title_layout.addWidget(label,1)
        self._title_labels[section_key] = label

        if include_settings and self._show_settings:
            self._settings_button.setParent(title_row)
            title_layout.addWidget(self._settings_button)
            self._settings_button.show()

        line = QtWidgets.QFrame(pane)
        line.setFrameShape(QtWidgets.QFrame.HLine)
        line.setFrameShadow(QtWidgets.QFrame.Sunken)

        pane_layout.addWidget(title_row)
        pane_layout.addWidget(line)
        view = self._collection.section_view(section_key)
        scroll = _SectionContentScrollArea(pane)
        scroll.setWidget(view)
        pane_layout.addWidget(scroll,1)
        view.show()
        self._pane_scrolls[section_key] = scroll
        self._pane_widgets[section_key] = pane
        return pane

    def _clear_presentation(self) -> None:
        self._settings_button.setParent(None)
        self._settings_button.hide()
        self._tab_filter = None

        if self._tabs is not None:
            while self._tabs.count():
                widget = self._tabs.widget(0)
                self._tabs.removeTab(0)
                if widget is not None:
                    widget.setParent(None)
            self._tabs = None

        for scroll in self._pane_scrolls.values():
            widget = scroll.takeWidget()
            if widget is not None:
                widget.setParent(None)

        if self._collection is not None:
            for section_key in self._collection.section_keys:
                try:
                    view = self._collection.section_view(section_key)
                except KeyError:
                    continue
                if view.parent() is not None:
                    view.setParent(None)

        widget = self._presentation_widget
        self._presentation_widget = None
        self._splitter = None
        self._pane_widgets.clear()
        self._pane_scrolls.clear()
        self._title_labels.clear()

        if widget is not None:
            self.layout().removeWidget(widget)
            widget.deleteLater()

    def _create_settings_button(self) -> QtWidgets.QToolButton:
        button = QtWidgets.QToolButton(self)
        button.setText(chr(0x2699))
        button.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
        button.setToolTip("SLM section settings")
        button.setAutoRaise(True)
        button.setFocusPolicy(QtCore.Qt.NoFocus)
        button.setFixedSize(22,22)
        button.clicked.connect(self.sigSettingsRequested.emit)
        button.hide()
        button.setStyleSheet("""
            QToolButton {
                border: none;
                background: transparent;
                padding: 0px;
                margin: 0px;
                font-size: 16px;
            }
            QToolButton:hover {
                background: rgba(255, 255, 255, 20);
            }
            QToolButton:pressed {
                background: rgba(255, 255, 255, 35);
            }
        """)
        return button

    def _position_tab_settings_button(self) -> None:
        if self._tabs is None or not self._show_settings:
            return
        tab_bar = self._tabs.tabBar()
        if tab_bar.count() == 0:
            self._settings_button.hide()
            return

        last_rect = tab_bar.tabRect(tab_bar.count() - 1)
        x = min(
            last_rect.right() + 5,
            max(0,tab_bar.width() - self._settings_button.width()),
        )
        y = (tab_bar.height() - self._settings_button.height()) // 2
        self._settings_button.move(x,max(0,y))
        self._settings_button.raise_()
        self._settings_button.show()

    def _rename_tab_index(self,index: int) -> None:
        if index < 0 or index >= len(self.section_keys):
            return
        self.rename_section(self.section_keys[index])

    def _open_tab_context_menu(self,pos) -> None:
        if self._tabs is None:
            return
        index = self._tabs.tabBar().tabAt(pos)
        if index < 0 or index >= len(self.section_keys):
            return
        global_pos = self._tabs.tabBar().mapToGlobal(pos)
        self._open_title_context_menu(self.section_keys[index],global_pos)

    def _open_title_context_menu(self,section_key: str,global_pos) -> None:
        menu = QtWidgets.QMenu(self)
        menu.addAction(
            "Rename section...",
            lambda key=section_key:self.rename_section(key),
        )
        if hasattr(menu,"exec_"):
            menu.exec_(global_pos)
        else:
            menu.exec(global_pos)

    def _on_tab_current_changed(self,index: int) -> None:
        keys = self.section_keys
        if 0 <= index < len(keys):
            self._current_section_key = keys[index]
            self.sigCurrentSectionChanged.emit(keys[index])

    def _snapshot_title(self,section_key: str) -> str | None:
        if self._collection is None:
            return None
        try:
            snapshot = self._collection.get_section_snapshot(section_key)
        except Exception:
            return None
        presentation = getattr(snapshot,"presentation",None)
        title = getattr(presentation,"title",None)
        if title is None:
            return None
        title = str(title).strip()
        return title or None

    def _default_title(self,section_key: str) -> str:
        try:
            index = self.section_keys.index(section_key)
        except ValueError:
            index = 0
        return f"Section {index + 1}"

    def _normalized_title(self,section_key: str,title) -> str:
        text = "" if title is None else str(title).strip()
        return text or self._default_title(section_key)


class _SectionContentScrollArea(QtWidgets.QScrollArea):
    """Horizontal overflow wrapper that does not constrain splitter width."""

    def __init__(self,parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setMinimumWidth(0)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Ignored,
            QtWidgets.QSizePolicy.Expanding,
        )
        self.setStyleSheet("QScrollArea { border: none; }")

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        return QtCore.QSize(0,hint.height())

    def sizeHint(self):
        widget = self.widget()
        if widget is None:
            return super().sizeHint()
        hint = widget.sizeHint()
        return QtCore.QSize(hint.width(),hint.height())


class _SectionTitleLabel(ElidedLabel):
    doubleClicked = QtCore.Signal()
    contextMenuRequested = QtCore.Signal(object)

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        return QtCore.QSize(0,hint.height())

    def mouseDoubleClickEvent(self,event) -> None:
        if event.button() == QtCore.Qt.LeftButton:
            self.doubleClicked.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self,event) -> None:
        self.contextMenuRequested.emit(event.globalPos())
        event.accept()


class _TabSettingsPositionFilter(QtCore.QObject):
    """Keep the section-settings button near the last tab."""

    def __init__(self,parent,reposition):
        super().__init__(parent)
        self._reposition = reposition

    def eventFilter(self,obj,event):
        if event.type() in (
            QtCore.QEvent.Resize,
            QtCore.QEvent.Show,
            QtCore.QEvent.LayoutRequest,
        ):
            QtCore.QTimer.singleShot(0,self._reposition)
        return False


class _SectionSplitter(QtWidgets.QSplitter):
    def __init__(self,orientation,parent=None):
        super().__init__(orientation,parent)
        self._separator_offset = 0

    def set_separator_offset(self,offset: int) -> None:
        self._separator_offset = max(0,int(offset))

    def separator_offset(self) -> int:
        return self._separator_offset

    def createHandle(self):
        return _SectionSplitterHandle(self.orientation(),self)


class _SectionSplitterHandle(QtWidgets.QSplitterHandle):
    def paintEvent(self,event) -> None:
        splitter = self.splitter()
        offset = (
            splitter.separator_offset()
            if hasattr(splitter,"separator_offset") else 0
        )

        painter = QtGui.QPainter(self)
        try:
            background = splitter.palette().color(QtGui.QPalette.Window)
            separator = QtGui.QColor(_SECTION_SEPARATOR_COLOR)
            painter.fillRect(self.rect(),background)
            if offset < self.height():
                painter.setPen(
                    QtGui.QPen(separator,_SECTION_SEPARATOR_WIDTH)
                )
                x = self.width() // 2
                painter.drawLine(x,offset,x,self.height())
        finally:
            painter.end()
