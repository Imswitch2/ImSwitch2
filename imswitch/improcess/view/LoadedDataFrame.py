from __future__ import annotations

from qtpy import QtCore, QtWidgets


class LoadedDataFrame(QtWidgets.QFrame):
    """Ad-hoc Quick Load catalog.

    The catalog is deliberately runtime-only. It records ordinary data brought
    in through Quick Load (or Working Set -> Add data) without turning those
    sources into Session members. One entry can be promoted to Current data by
    activating it in the list.
    """

    sigQuickLoadRequested = QtCore.Signal()
    sigSourceChanged = QtCore.Signal(str)
    sigSetCurrentRequested = QtCore.Signal(object)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setMinimumWidth(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Expanding)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

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
        controls.addStretch(1)
        self.quickLoadBtn = QtWidgets.QPushButton("Quick Load…")
        self.quickLoadBtn.setToolTip("Load one or more datasets into the Quick Load catalog")
        controls.addWidget(self.quickLoadBtn)
        root.addLayout(controls)

        self.dataList = QtWidgets.QTreeWidget()
        self.dataList.setColumnCount(3)
        self.dataList.setHeaderLabels(["Data", "Acquisition", "Status"])
        self.dataList.setRootIsDecorated(False)
        self.dataList.setUniformRowHeights(True)
        self.dataList.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.dataList.setAlternatingRowColors(True)
        self.dataList.header().setStretchLastSection(False)
        self.dataList.header().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        self.dataList.header().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        self.dataList.header().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        root.addWidget(self.dataList, 1)

        self.emptyLabel = QtWidgets.QLabel(
            "Quick Load data appears here. Loading one item makes it Current data; "
            "loading several keeps all of them available and makes the first one current."
        )
        self.emptyLabel.setWordWrap(True)
        self.emptyLabel.setStyleSheet("color: palette(mid);")
        root.addWidget(self.emptyLabel)

        self.quickLoadBtn.clicked.connect(self.sigQuickLoadRequested)
        self.sourceCombo.currentIndexChanged.connect(self._source_changed)
        self.dataList.itemDoubleClicked.connect(self._item_activated)
        self.dataList.itemActivated.connect(self._item_activated)

    def sourceMode(self) -> str:
        return str(self.sourceCombo.currentData() or "files")

    def setSourceMode(self, mode: str) -> None:
        index = self.sourceCombo.findData(str(mode or "files"))
        if index < 0:
            index = 0
        self.sourceCombo.setCurrentIndex(index)

    def _source_changed(self, _index: int) -> None:
        self.sigSourceChanged.emit(self.sourceMode())

    def _item_activated(self, item, _column=0) -> None:
        if item is None:
            return
        data_obj = item.data(0, QtCore.Qt.UserRole)
        if data_obj is not None:
            self.sigSetCurrentRequested.emit(data_obj)

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
        status: str = "Loaded",
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
        item.setText(2, str(status or ""))
        if tooltip:
            for column in range(3):
                item.setToolTip(column, str(tooltip))
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

    def setCurrentDataObj(self, data_obj) -> None:
        for index in range(self.dataList.topLevelItemCount()):
            item = self.dataList.topLevelItem(index)
            existing = item.data(0, QtCore.Qt.UserRole)
            is_current = data_obj is not None and existing is data_obj
            font = item.font(0)
            font.setBold(bool(is_current))
            item.setFont(0, font)
            item.setText(2, "Current" if is_current else "Loaded")
            if is_current:
                self.dataList.setCurrentItem(item)

    def count(self) -> int:
        return self.dataList.topLevelItemCount()
