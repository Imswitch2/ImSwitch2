from __future__ import annotations

import os
from typing import Any,Mapping,Sequence

from qtpy import QtCore,QtWidgets

from ..widgets.uitools import BetterPushButton


class ConfigControls(QtWidgets.QWidget):
    """Reusable compact selector/actions for complete SLM configs."""

    sigLoadRequested = QtCore.Signal(str)
    sigSaveAsRequested = QtCore.Signal()
    sigUpdateRequested = QtCore.Signal(str)
    sigRenameRequested = QtCore.Signal(str)
    sigDuplicateRequested = QtCore.Signal(str)
    sigDeleteRequested = QtCore.Signal(str)
    sigSetStartupRequested = QtCore.Signal(str)
    sigOpenFolderRequested = QtCore.Signal()
    sigInspectRequested = QtCore.Signal()

    _transparent_style = """
        QToolButton { border: none; background: transparent; }
        QToolButton:hover { background: transparent; }
        QToolButton:pressed { background: transparent; }
    """

    def __init__(self,parent=None) -> None:
        super().__init__(parent)
        self._current_config = {}
        self._selection_override: str | None = None
        self._editing_actions_enabled = True

        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0,0,0,0)
        row.setSpacing(4)
        row.addWidget(QtWidgets.QLabel("Config:"))

        self.combo = QtWidgets.QComboBox()
        self.combo.setMinimumWidth(180)
        self.combo.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,QtWidgets.QSizePolicy.Fixed,
        )
        self.combo.view().setMouseTracking(True)
        self.combo.view().viewport().setMouseTracking(True)
        self.combo.wheelEvent = lambda event:None
        row.addWidget(self.combo)

        self.info_button = QtWidgets.QToolButton()
        self.info_button.setIcon(
            self.style().standardIcon(QtWidgets.QStyle.SP_MessageBoxInformation)
        )
        self.info_button.setAutoRaise(True)
        self.info_button.setFixedSize(18,18)
        self.info_button.setToolTip("Open config inspector")
        self.info_button.setStyleSheet(self._transparent_style)
        row.addWidget(self.info_button)

        self.reload_button = QtWidgets.QToolButton()
        self.reload_button.setIcon(self.style().standardIcon(
            getattr(QtWidgets.QStyle,"SP_BrowserReload",QtWidgets.QStyle.SP_ArrowRight)
        ))
        self.reload_button.setAutoRaise(True)
        self.reload_button.setFixedSize(22,22)
        self.reload_button.setToolTip("Reload selected config")
        self.reload_button.setStyleSheet(self._transparent_style)
        row.addWidget(self.reload_button)

        self.update_button = BetterPushButton("Update Config")
        self.update_button.setFixedHeight(20)
        row.addWidget(self.update_button)

        self.more_button = QtWidgets.QToolButton()
        self.more_button.setText("More")
        self.more_button.setFixedHeight(20)
        self.more_button.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        row.addWidget(self.more_button)

        menu = QtWidgets.QMenu(self.more_button)
        self.save_as_action = menu.addAction("Save as...")
        menu.addSeparator()
        self.rename_action = menu.addAction("Rename config...")
        self.duplicate_action = menu.addAction("Duplicate config...")
        self.delete_action = menu.addAction("Delete config")
        menu.addSeparator()
        self.startup_action = menu.addAction("Set as startup config")
        self.open_folder_action = menu.addAction("Open config folder")
        self.more_button.setMenu(menu)

        self.combo.currentIndexChanged.connect(self._on_index_changed)
        self.combo.activated.connect(lambda _index:self._emit_load())
        self.reload_button.clicked.connect(lambda _checked=False:self._emit_load())
        self.info_button.clicked.connect(lambda _checked=False:self.sigInspectRequested.emit())
        self.update_button.clicked.connect(lambda _checked=False:self._emit_path(self.sigUpdateRequested))
        self.save_as_action.triggered.connect(
            lambda _checked=False:self.sigSaveAsRequested.emit()
        )
        self.rename_action.triggered.connect(
            lambda _checked=False:self._emit_path(self.sigRenameRequested)
        )
        self.duplicate_action.triggered.connect(
            lambda _checked=False:self._emit_path(self.sigDuplicateRequested)
        )
        self.delete_action.triggered.connect(
            lambda _checked=False:self._emit_path(self.sigDeleteRequested)
        )
        self.startup_action.triggered.connect(
            lambda _checked=False:self._emit_path(self.sigSetStartupRequested)
        )
        self.open_folder_action.triggered.connect(
            lambda _checked=False:self.sigOpenFolderRequested.emit()
        )
        self._update_enabled()

    def set_available_configs(self,entries: Sequence[Sequence[Any]]) -> None:
        current = (
            self._selection_override
            if self._selection_override is not None
            else self._current_config.get("path")
        )
        current_path = os.path.abspath(str(current)) if current else None
        current_index = -1
        blocker = QtCore.QSignalBlocker(self.combo)
        try:
            self.combo.clear()
            for index,entry in enumerate(entries):
                name,path = str(entry[0]),str(entry[1])
                tooltip = str(entry[2]) if len(entry) > 2 else name
                self.combo.addItem(name,path)
                self.combo.setItemData(index,tooltip,QtCore.Qt.ToolTipRole)
                if current_path and _same_path(path,current_path):
                    current_index = index
            self.combo.setCurrentIndex(current_index)
        finally:
            del blocker
        self._on_index_changed(current_index)

    def set_current_config(self,config: Any | None) -> None:
        self._selection_override = None
        if config is None:
            self._current_config = {}
        elif isinstance(config,Mapping):
            self._current_config = dict(config)
        else:
            self._current_config = {"path":str(config)}
        self._sync_current_index()

    def set_selected_path(self,path: str | None) -> None:
        """Select a config for presentation without changing current config."""
        self._selection_override = None if path is None else str(path)
        target = self._selection_override
        index = -1
        if target:
            for candidate in range(self.combo.count()):
                item = self.combo.itemData(candidate)
                if item and _same_path(item,target):
                    index = candidate
                    break
        blocker = QtCore.QSignalBlocker(self.combo)
        try:
            self.combo.setCurrentIndex(index)
        finally:
            del blocker
        self._on_index_changed(index)

    def clear_selected_path_override(self) -> None:
        self._selection_override = None
        self._sync_current_index()

    def set_editing_actions_enabled(self,enabled: bool) -> None:
        self._editing_actions_enabled = bool(enabled)
        self._update_enabled()

    def current_config(self):
        return dict(self._current_config)

    def selected_path(self) -> str | None:
        value = self.combo.currentData()
        return None if value is None else str(value)

    def selected_name(self) -> str:
        return str(self.combo.currentText() or "")

    def existing_stems(self):
        return tuple(
            os.path.splitext(self.combo.itemText(index))[0]
            for index in range(self.combo.count())
        )

    def _sync_current_index(self) -> None:
        path = self._current_config.get("path")
        index = -1
        if path:
            for candidate in range(self.combo.count()):
                item = self.combo.itemData(candidate)
                if item and _same_path(item,path):
                    index = candidate
                    break
        blocker = QtCore.QSignalBlocker(self.combo)
        try:
            self.combo.setCurrentIndex(index)
        finally:
            del blocker
        self._on_index_changed(index)

    def _on_index_changed(self,_index: int) -> None:
        tooltip = self.combo.itemData(
            self.combo.currentIndex(),QtCore.Qt.ToolTipRole,
        ) or ""
        self.combo.setToolTip(str(tooltip))
        self._update_enabled()

    def _update_enabled(self) -> None:
        has_config = self.selected_path() is not None
        self.reload_button.setEnabled(has_config)
        editable = self._editing_actions_enabled
        self.update_button.setEnabled(has_config and editable)
        self.save_as_action.setEnabled(editable)
        for action in (
            self.rename_action,self.duplicate_action,self.delete_action,
            self.startup_action,
        ):
            action.setEnabled(has_config and editable)

    def _emit_load(self) -> None:
        self._emit_path(self.sigLoadRequested)

    def _emit_path(self,signal) -> None:
        path = self.selected_path()
        if path:
            signal.emit(path)


def _same_path(first: Any,second: Any) -> bool:
    return os.path.normcase(os.path.abspath(str(first))) == os.path.normcase(
        os.path.abspath(str(second))
    )
