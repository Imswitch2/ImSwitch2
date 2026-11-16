from __future__ import annotations

from pathlib import Path

from qtpy import QtCore, QtGui, QtWidgets

from imswitch.improcess.model.lapse_source import TIME_LAPSE_SOURCE_KIND
from imswitch.improcess.model.timepoint_scope import logical_time_lapse_identity

class LoadedDataFrame(QtWidgets.QFrame):
    """Lightweight runtime-only Quick Load catalog.

    Quick Load is deliberately simpler than Session: the catalog only shows
    the source name and useful acquisition information.  Selection defines the
    reconstruction scope; double-clicking promotes one entry to Current data.
    """

    sigQuickLoadRequested = QtCore.Signal()
    sigSourceChanged = QtCore.Signal(str)
    sigKeepHistoryChanged = QtCore.Signal(bool)
    sigSetCurrentRequested = QtCore.Signal(object)
    sigOpenViewerRequested = QtCore.Signal(object)
    sigRevealRequested = QtCore.Signal(object)
    sigRemoveRequested = QtCore.Signal(object)
    sigAddToMultiDataRequested = QtCore.Signal()
    sigSelectionCountChanged = QtCore.Signal(int)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setMinimumWidth(0)
        self.setMinimumHeight(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Expanding)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(6, 4, 6, 4)
        root.setSpacing(4)

        controls = QtWidgets.QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(4)
        controls.addWidget(QtWidgets.QLabel("Source:"))
        self.sourceCombo = QtWidgets.QComboBox()
        self.sourceCombo.addItem("Files", "files")
        self.sourceCombo.addItem("Zarr", "zarr")
        self.sourceCombo.setToolTip(
            "Choose which operating-system picker Quick Load opens. "
            "Files supports multiple selection."
        )
        controls.addWidget(self.sourceCombo)
        # Keep a permanent status slot in the toolbar so informational text
        # never changes the geometry of the Source / Quick Load controls.
        self.statusLabel = QtWidgets.QLabel("")
        self.statusLabel.setMinimumWidth(0)
        self.statusLabel.setSizePolicy(
            QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred
        )
        self.statusLabel.setStyleSheet("color: palette(mid);")
        controls.addWidget(self.statusLabel, 1)
        self.keepHistoryCheck = QtWidgets.QCheckBox("Keep history")
        self.keepHistoryCheck.setToolTip(
            "Keep earlier Quick Load groups in this lightweight catalog. "
            "When disabled, each new Quick Load replaces the previous group."
        )
        controls.addWidget(self.keepHistoryCheck)
        self.quickLoadBtn = QtWidgets.QPushButton("Quick Load…")
        self.quickLoadBtn.setToolTip("Load one or more datasets")
        controls.addWidget(self.quickLoadBtn)
        root.addLayout(controls)

        self.dataList = QtWidgets.QTreeWidget()
        self.dataList.setColumnCount(2)
        self.dataList.setHeaderLabels(["Data", "Acquisition"])
        # QHeaderView section text can be overridden by the active Qt theme,
        # so style the visible sections explicitly rather than only changing
        # the parent header font.
        self.dataList.header().setStyleSheet(
            "QHeaderView::section { font-size: 7pt; font-weight: normal; "
            "padding: 0px 3px; }"
        )
        self.dataList.setRootIsDecorated(False)
        self.dataList.setUniformRowHeights(True)
        self.dataList.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.dataList.setAlternatingRowColors(True)
        self.dataList.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.dataList.setMinimumHeight(24)
        self.dataList.setSizePolicy(
            QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Expanding
        )
        self.dataList.header().setStretchLastSection(False)
        self.dataList.header().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        self.dataList.header().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        root.addWidget(self.dataList, 1)

        self.emptyLabel = QtWidgets.QLabel("Quick Load data appears here.")
        self.emptyLabel.setStyleSheet("color: palette(mid);")
        root.addWidget(self.emptyLabel)

        self.quickLoadBtn.clicked.connect(self.sigQuickLoadRequested)
        self.sourceCombo.currentIndexChanged.connect(self._source_changed)
        self.keepHistoryCheck.toggled.connect(self.sigKeepHistoryChanged)
        self.dataList.itemDoubleClicked.connect(self._item_activated)
        self.dataList.itemActivated.connect(self._item_activated)
        self.dataList.itemSelectionChanged.connect(self._selection_changed)
        self.dataList.customContextMenuRequested.connect(self._context_menu)

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        hint.setWidth(0)
        # Keep Quick Load usable even when DATA is collapsed to roughly one row.
        hint.setHeight(64)
        return hint

    def sizeHint(self):
        hint = super().sizeHint()
        hint.setWidth(0)
        return hint

    def _selection_changed(self) -> None:
        self.sigSelectionCountChanged.emit(len(self.selectedDataObjs()))

    def selectedDataObjs(self) -> list:
        out = []
        for item in self.dataList.selectedItems():
            data_obj = item.data(0, QtCore.Qt.UserRole)
            if data_obj is not None:
                out.append(data_obj)
        return out

    def selectedTimeLapseDescriptor(self) -> dict | None:
        selected = self.selectedDataObjs()
        if len(selected) != 1:
            return None
        data_obj = selected[0]
        if str(getattr(data_obj, "sourceKind", "image") or "image") != TIME_LAPSE_SOURCE_KIND:
            return None
        metadata = getattr(data_obj, "sourceMetadata", None)
        total = int(getattr(metadata, "planned_count", 0) or 0)
        identity = logical_time_lapse_identity(data_obj)
        if identity is None or total <= 0:
            return None
        return {"identity": identity, "total": total}

    def setStatus(self, text: str) -> None:
        """Set the persistent Quick Load toolbar status.

        The slot is always present, even when empty, so status changes never
        shift the Source / Keep history / Quick Load controls.
        """
        text = str(text or "").strip()
        self.statusLabel.setText(text)
        self.statusLabel.setToolTip(text)

    def showNotice(self, text: str, *, timeout_ms: int = 0) -> None:
        """Compatibility alias for older callers; notices are persistent."""
        self.setStatus(text)

    def sourceMode(self) -> str:
        return str(self.sourceCombo.currentData() or "files")

    def setSourceMode(self, mode: str) -> None:
        index = self.sourceCombo.findData(str(mode or "files"))
        if index < 0:
            index = 0
        self.sourceCombo.setCurrentIndex(index)

    def keepHistory(self) -> bool:
        return bool(self.keepHistoryCheck.isChecked())

    def setKeepHistory(self, enabled: bool) -> None:
        self.keepHistoryCheck.setChecked(bool(enabled))

    def _source_changed(self, _index: int) -> None:
        self.sigSourceChanged.emit(self.sourceMode())

    def _item_activated(self, item, _column=0) -> None:
        if item is None:
            return
        data_obj = item.data(0, QtCore.Qt.UserRole)
        if data_obj is not None:
            self.sigSetCurrentRequested.emit(data_obj)

    def _context_menu(self, point) -> None:
        item = self.dataList.itemAt(point)
        if item is None:
            return
        if not item.isSelected():
            self.dataList.clearSelection()
            item.setSelected(True)
            self.dataList.setCurrentItem(item)
        selected = self.selectedDataObjs()
        if not selected:
            return

        menu = QtWidgets.QMenu(self)
        if len(selected) == 1:
            data_obj = selected[0]
            open_viewer = menu.addAction("Open in viewer")
            open_viewer.triggered.connect(
                lambda _checked=False, obj=data_obj: self.sigOpenViewerRequested.emit(obj)
            )
            reveal = menu.addAction("Show in Explorer")
            reveal.triggered.connect(
                lambda _checked=False, obj=data_obj: self.sigRevealRequested.emit(obj)
            )
            menu.addSeparator()
        add_multi = menu.addAction(
            "Add selected to Multi-data" if len(selected) > 1 else "Add to Multi-data"
        )
        add_multi.triggered.connect(self.sigAddToMultiDataRequested)
        menu.addSeparator()
        remove = menu.addAction(
            "Remove selected from Loaded" if len(selected) > 1 else "Remove from Loaded"
        )
        remove.triggered.connect(
            lambda _checked=False, objs=list(selected): [
                self.sigRemoveRequested.emit(obj) for obj in objs
            ]
        )
        menu.exec(self.dataList.viewport().mapToGlobal(point))

    @staticmethod
    def _identity(data_obj) -> tuple:
        return (
            str(getattr(data_obj, "sourceKind", "image") or "image"),
            str(getattr(data_obj, "dataPath", "") or ""),
            str(getattr(data_obj, "datasetName", "") or ""),
            str(getattr(data_obj, "name", "") or ""),
        )

    def _findItem(self, data_obj):
        wanted = self._identity(data_obj)
        for index in range(self.dataList.topLevelItemCount()):
            item = self.dataList.topLevelItem(index)
            existing = item.data(0, QtCore.Qt.UserRole)
            if existing is data_obj or (
                existing is not None and self._identity(existing) == wanted
            ):
                return item
        return None

    def addOrUpdateDataObj(
        self,
        data_obj,
        *,
        acquisition: str = "",
        status: str = "",  # compatibility with older callers; intentionally hidden
        tooltip: str = "",
    ):
        item = self._findItem(data_obj)
        if item is None:
            item = QtWidgets.QTreeWidgetItem()
            self.dataList.addTopLevelItem(item)
        item.setData(0, QtCore.Qt.UserRole, data_obj)
        name = str(getattr(data_obj, "name", "") or "")
        dataset = str(getattr(data_obj, "datasetName", "") or "")
        if dataset and dataset not in {"time-lapse", name}:
            label = f"{name} · {dataset}"
        else:
            label = name or dataset or "Data"
        item.setText(0, label)
        item.setText(1, str(acquisition or ""))
        if tooltip:
            item.setToolTip(0, str(tooltip))
            item.setToolTip(1, str(tooltip))
        self.emptyLabel.setVisible(self.dataList.topLevelItemCount() == 0)
        return item

    def removeDataObj(self, data_obj) -> None:
        item = self._findItem(data_obj)
        if item is None:
            return
        index = self.dataList.indexOfTopLevelItem(item)
        if index >= 0:
            self.dataList.takeTopLevelItem(index)
        self.emptyLabel.setVisible(self.dataList.topLevelItemCount() == 0)
        self._selection_changed()

    def clearCatalog(self) -> None:
        self.dataList.clear()
        self.emptyLabel.setVisible(True)
        self._selection_changed()

    def selectSources(self, paths) -> None:
        wanted = {
            str(Path(path).resolve())
            for path in (paths or ())
            if str(path or "").strip()
        }
        if not wanted:
            return
        self.dataList.clearSelection()
        matched_items = []
        for index in range(self.dataList.topLevelItemCount()):
            item = self.dataList.topLevelItem(index)
            data_obj = item.data(0, QtCore.Qt.UserRole)
            if data_obj is None:
                continue
            source_paths = {
                str(getattr(data_obj, "dataPath", "") or ""),
                str(getattr(data_obj, "sourceOriginalPath", "") or ""),
            }
            resolved = set()
            for value in source_paths:
                if not value:
                    continue
                try:
                    resolved.add(str(Path(value).resolve()))
                except Exception:
                    resolved.add(value)
            if wanted.intersection(resolved):
                matched_items.append(item)
        if matched_items:
            # Qt may reset extended selection when the current row changes.
            # Focus the first match *before* selecting the entire requested set.
            self.dataList.setCurrentItem(matched_items[0])
            for item in matched_items:
                item.setSelected(True)
        self._selection_changed()

    def setCurrentDataObj(self, data_obj) -> None:
        # Current is inspection state, not reconstruction selection.  Keep the
        # current row bold without changing the user's selected reconstruction scope.
        for index in range(self.dataList.topLevelItemCount()):
            item = self.dataList.topLevelItem(index)
            existing = item.data(0, QtCore.Qt.UserRole)
            is_current = data_obj is not None and existing is data_obj
            font = item.font(0)
            font.setBold(bool(is_current))
            item.setFont(0, font)

    def count(self) -> int:
        return self.dataList.topLevelItemCount()
