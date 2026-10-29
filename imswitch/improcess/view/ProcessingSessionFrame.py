from __future__ import annotations

from qtpy import QtCore, QtWidgets


class ProcessingSessionFrame(QtWidgets.QFrame):
    """Prototype Session UI kept deliberately separate from Directory watcher."""

    sigOpenSession = QtCore.Signal()
    sigRefresh = QtCore.Signal()
    sigDatasetSelectionChanged = QtCore.Signal(object)
    sigSavedSelectionChanged = QtCore.Signal(object)
    sigLoadRawRequested = QtCore.Signal()
    sigAddToReconstructionsRequested = QtCore.Signal()
    sigSetFinalRequested = QtCore.Signal()
    sigAutoSaveChanged = QtCore.Signal(bool)
    sigSaveFormatChanged = QtCore.Signal(str)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        path_grid = QtWidgets.QGridLayout()
        self.rawRootEdit = QtWidgets.QLineEdit()
        self.rawRootEdit.setPlaceholderText("Raw data folder")
        self.resultsRootEdit = QtWidgets.QLineEdit()
        self.resultsRootEdit.setPlaceholderText("Results folder (defaults to <raw>_recon)")
        self.openBtn = QtWidgets.QPushButton("Open session")
        self.refreshBtn = QtWidgets.QPushButton("Refresh")
        path_grid.addWidget(QtWidgets.QLabel("Raw"), 0, 0)
        path_grid.addWidget(self.rawRootEdit, 0, 1, 1, 3)
        path_grid.addWidget(QtWidgets.QLabel("Results"), 1, 0)
        path_grid.addWidget(self.resultsRootEdit, 1, 1, 1, 3)
        path_grid.addWidget(self.openBtn, 2, 2)
        path_grid.addWidget(self.refreshBtn, 2, 3)
        root.addLayout(path_grid)

        save_row = QtWidgets.QHBoxLayout()
        self.autoSaveCheck = QtWidgets.QCheckBox("Auto-save new reconstructions")
        self.autoSaveCheck.setChecked(True)
        self.saveFormatCombo = QtWidgets.QComboBox()
        self.saveFormatCombo.addItem("OME-TIFF", "tiff")
        self.saveFormatCombo.addItem("HDF5", "hdf5")
        self.saveFormatCombo.addItem("OME-Zarr", "zarr")
        save_row.addWidget(self.autoSaveCheck)
        save_row.addStretch(1)
        save_row.addWidget(QtWidgets.QLabel("Format"))
        save_row.addWidget(self.saveFormatCombo)
        root.addLayout(save_row)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)

        datasets_box = QtWidgets.QGroupBox("Raw datasets")
        datasets_layout = QtWidgets.QVBoxLayout(datasets_box)
        self.datasetList = QtWidgets.QListWidget()
        datasets_layout.addWidget(self.datasetList)
        splitter.addWidget(datasets_box)

        selected_box = QtWidgets.QGroupBox("Selected dataset")
        selected_layout = QtWidgets.QVBoxLayout(selected_box)
        self.selectedLabel = QtWidgets.QLabel("No dataset selected")
        self.selectedLabel.setWordWrap(True)
        self.summaryLabel = QtWidgets.QLabel("")
        self.summaryLabel.setWordWrap(True)
        selected_layout.addWidget(self.selectedLabel)
        selected_layout.addWidget(self.summaryLabel)
        selected_layout.addWidget(QtWidgets.QLabel("Reconstructions for selected dataset"))
        self.savedList = QtWidgets.QListWidget()
        selected_layout.addWidget(self.savedList, 1)

        buttons = QtWidgets.QHBoxLayout()
        self.loadRawBtn = QtWidgets.QPushButton("Load raw data")
        self.addReconBtn = QtWidgets.QPushButton("Add to reconstructions")
        self.setFinalBtn = QtWidgets.QPushButton("Set final")
        buttons.addWidget(self.loadRawBtn)
        buttons.addWidget(self.addReconBtn)
        buttons.addWidget(self.setFinalBtn)
        selected_layout.addLayout(buttons)
        splitter.addWidget(selected_box)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)

        self.statusLabel = QtWidgets.QLabel("No session open")
        self.statusLabel.setWordWrap(True)
        root.addWidget(self.statusLabel)

        self.openBtn.clicked.connect(self.sigOpenSession)
        self.refreshBtn.clicked.connect(self.sigRefresh)
        self.datasetList.currentItemChanged.connect(self._dataset_changed)
        self.savedList.currentItemChanged.connect(self._saved_changed)
        self.loadRawBtn.clicked.connect(self.sigLoadRawRequested)
        self.addReconBtn.clicked.connect(self.sigAddToReconstructionsRequested)
        self.setFinalBtn.clicked.connect(self.sigSetFinalRequested)
        self.autoSaveCheck.toggled.connect(self.sigAutoSaveChanged)
        self.saveFormatCombo.currentIndexChanged.connect(
            lambda _index: self.sigSaveFormatChanged.emit(
                str(self.saveFormatCombo.currentData() or "tiff")
            )
        )
        self.setActionsEnabled(False, False, False)

    def _dataset_changed(self, current, _previous):
        value = current.data(QtCore.Qt.UserRole) if current is not None else None
        self.sigDatasetSelectionChanged.emit(value)

    def _saved_changed(self, current, _previous):
        value = current.data(QtCore.Qt.UserRole) if current is not None else None
        self.sigSavedSelectionChanged.emit(value)

    def rawRoot(self) -> str:
        return self.rawRootEdit.text().strip()

    def resultsRoot(self) -> str:
        return self.resultsRootEdit.text().strip()

    def setPaths(self, raw_root: str, results_root: str) -> None:
        self.rawRootEdit.setText(raw_root)
        self.resultsRootEdit.setText(results_root)

    def setStatus(self, text: str) -> None:
        self.statusLabel.setText(str(text))

    def setDatasets(self, entries: list[tuple[str, str, str]]) -> None:
        """Entries are ``(dataset_id, label, status_text)``."""
        current_id = None
        current = self.datasetList.currentItem()
        if current is not None:
            current_id = current.data(QtCore.Qt.UserRole)
        self.datasetList.blockSignals(True)
        self.datasetList.clear()
        restore = None
        for dataset_id, label, status in entries:
            item = QtWidgets.QListWidgetItem(f"{label}    {status}")
            item.setData(QtCore.Qt.UserRole, dataset_id)
            self.datasetList.addItem(item)
            if dataset_id == current_id:
                restore = item
        self.datasetList.blockSignals(False)
        if restore is not None:
            self.datasetList.setCurrentItem(restore)
        elif self.datasetList.count():
            self.datasetList.setCurrentRow(0)
        else:
            self.sigDatasetSelectionChanged.emit(None)

    def setSelectedDataset(self, title: str, summary: str) -> None:
        self.selectedLabel.setText(title or "No dataset selected")
        self.summaryLabel.setText(summary or "")

    def setSavedReconstructions(
        self,
        entries: list[tuple[str, str]],
        selected_path: str | None = None,
    ) -> None:
        self.savedList.blockSignals(True)
        self.savedList.clear()
        restore = None
        for path, label in entries:
            item = QtWidgets.QListWidgetItem(label)
            item.setData(QtCore.Qt.UserRole, path)
            self.savedList.addItem(item)
            if selected_path and path == selected_path:
                restore = item
        self.savedList.blockSignals(False)
        if restore is not None:
            self.savedList.setCurrentItem(restore)
        else:
            # Dataset selection must stay Session-local. Do not implicitly
            # select the first saved reconstruction: if that one is already in
            # the reconstruction list, doing so would also switch the active
            # result on the right.
            self.savedList.setCurrentRow(-1)
            self.sigSavedSelectionChanged.emit(None)

    def setActionsEnabled(self, load_raw: bool, add_recon: bool, set_final: bool) -> None:
        self.loadRawBtn.setEnabled(bool(load_raw))
        self.addReconBtn.setEnabled(bool(add_recon))
        self.setFinalBtn.setEnabled(bool(set_final))


__all__ = ["ProcessingSessionFrame"]
