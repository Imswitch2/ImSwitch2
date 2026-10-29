from __future__ import annotations

from pathlib import Path

from qtpy import QtCore, QtGui, QtWidgets


class _ElidingLabel(QtWidgets.QLabel):
    """Single-line label that never makes the Session dock wider."""

    def __init__(self, text="", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._fullText = ""
        self.setMinimumWidth(0)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Ignored,
            QtWidgets.QSizePolicy.Preferred,
        )
        self.setText(text)

    def setText(self, text):
        self._fullText = str(text or "")
        self.setToolTip(self._fullText)
        self._updateElidedText()

    def fullText(self) -> str:
        return self._fullText

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._updateElidedText()

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        hint.setWidth(0)
        return hint

    def sizeHint(self):
        hint = super().sizeHint()
        hint.setWidth(0)
        return hint

    def _updateElidedText(self):
        shown = self.fontMetrics().elidedText(
            self._fullText,
            QtCore.Qt.ElideRight,
            max(0, self.contentsRect().width()),
        )
        super().setText(shown)


class _CollapsibleSection(QtWidgets.QWidget):
    """Small header + collapsible content used to keep Session list-first."""

    def __init__(self, title: str, *, expanded: bool, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(0)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Ignored,
            QtWidgets.QSizePolicy.Preferred,
        )

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        self.toggle = QtWidgets.QToolButton()
        self.toggle.setText(str(title))
        self.toggle.setCheckable(True)
        self.toggle.setChecked(bool(expanded))
        self.toggle.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.toggle.setAutoRaise(True)
        self.toggle.setSizePolicy(
            QtWidgets.QSizePolicy.Ignored,
            QtWidgets.QSizePolicy.Fixed,
        )
        layout.addWidget(self.toggle)

        self.content = QtWidgets.QWidget()
        self.content.setMinimumWidth(0)
        self.content.setSizePolicy(
            QtWidgets.QSizePolicy.Ignored,
            QtWidgets.QSizePolicy.Preferred,
        )
        layout.addWidget(self.content)

        self.toggle.toggled.connect(self.setExpanded)
        self.setExpanded(bool(expanded))

    def setTitle(self, title: str) -> None:
        self.toggle.setText(str(title))

    def setContentLayout(self, layout) -> None:
        self.content.setLayout(layout)

    def setExpanded(self, expanded: bool) -> None:
        expanded = bool(expanded)
        if self.toggle.isChecked() != expanded:
            self.toggle.blockSignals(True)
            self.toggle.setChecked(expanded)
            self.toggle.blockSignals(False)
        self.toggle.setArrowType(
            QtCore.Qt.DownArrow if expanded else QtCore.Qt.RightArrow
        )
        self.content.setVisible(expanded)

    def isExpanded(self) -> bool:
        return bool(self.toggle.isChecked())


class _CompactDatasetTree(QtWidgets.QTreeWidget):
    """Compact two-column raw-data list with a shrinkable filename column."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(2)
        self.setHeaderHidden(True)
        self.setRootIsDecorated(False)
        self.setItemsExpandable(False)
        self.setIndentation(0)
        self.setUniformRowHeights(True)
        self.setTextElideMode(QtCore.Qt.ElideRight)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setMinimumWidth(0)
        self.setMinimumHeight(72)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Ignored,
            QtWidgets.QSizePolicy.Expanding,
        )

        header = self.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.Fixed)
        header.resizeSection(1, 132)

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        hint.setWidth(0)
        hint.setHeight(72)
        return hint

    def sizeHint(self):
        hint = super().sizeHint()
        hint.setWidth(0)
        hint.setHeight(145)
        return hint


class _ReconstructionReviewTree(QtWidgets.QTreeWidget):
    """Compact multi-select reconstruction review list with inline actions."""

    sigFinalToggleRequested = QtCore.Signal(object)
    sigWorkspaceToggleRequested = QtCore.Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(3)
        self.setHeaderHidden(True)
        self.setRootIsDecorated(False)
        self.setItemsExpandable(False)
        self.setIndentation(0)
        self.setUniformRowHeights(True)
        self.setTextElideMode(QtCore.Qt.ElideRight)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.setMinimumWidth(0)
        self.setMinimumHeight(68)
        self.setMaximumHeight(155)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Ignored,
            QtWidgets.QSizePolicy.Preferred,
        )

        header = self.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.Fixed)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.Fixed)
        header.resizeSection(1, 28)
        header.resizeSection(2, 82)

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        hint.setWidth(0)
        hint.setHeight(68)
        return hint

    def sizeHint(self):
        hint = super().sizeHint()
        hint.setWidth(0)
        # Roughly 3--4 candidates by default; longer lists scroll but can use
        # a little more height when the Session dock itself has room.
        hint.setHeight(102)
        return hint

    def selectedKeys(self) -> list[str]:
        keys = []
        for item in self.selectedItems():
            key = item.data(0, QtCore.Qt.UserRole)
            if key:
                keys.append(str(key))
        return keys

    def setEntries(self, entries: list[dict], selected_key: str | None = None) -> None:
        selected_before = set(self.selectedKeys())
        self.blockSignals(True)
        self.clear()
        restore_current = None
        palette = self.palette()
        normal = palette.color(QtGui.QPalette.Text)
        try:
            muted = palette.color(QtGui.QPalette.PlaceholderText)
        except AttributeError:
            muted = palette.color(QtGui.QPalette.Mid)
        dark = palette.color(QtGui.QPalette.Base).lightness() < 128
        final_color = QtGui.QColor("#72c98c" if dark else "#2e7d32")
        candidate_color = QtGui.QColor("#ffb457" if dark else "#b56a00")

        for entry in entries:
            key = str(entry.get("key", ""))
            name = str(entry.get("name", "reconstruction"))
            final = bool(entry.get("final", False))
            loaded = bool(entry.get("loaded", False))
            saved = bool(entry.get("saved", False))

            item = QtWidgets.QTreeWidgetItem([name, "", ""])
            item.setData(0, QtCore.Qt.UserRole, key)
            item.setToolTip(0, str(entry.get("tooltip") or name))
            item.setForeground(0, final_color if final else candidate_color)
            font = item.font(0)
            font.setBold(final)
            item.setFont(0, font)
            self.addTopLevelItem(item)

            star = QtWidgets.QToolButton()
            star.setText("★" if final else "☆")
            star.setAutoRaise(True)
            star.setFixedWidth(26)
            star.setToolTip(
                "Final/kept reconstruction — click to make candidate"
                if final
                else "Candidate reconstruction — click to keep as final"
            )
            # Emit only the stable Session key.  Never capture the QTreeWidgetItem
            # here: action signals can synchronously trigger a Session rerender,
            # which clears the tree and deletes the underlying C++ item.
            star.clicked.connect(
                lambda _checked=False, k=key: self.sigFinalToggleRequested.emit(k)
            )
            self.setItemWidget(item, 1, star)

            workspace = QtWidgets.QPushButton("Close" if loaded else "Add")
            workspace.setMinimumWidth(0)
            workspace.setMaximumWidth(78)
            workspace.setSizePolicy(
                QtWidgets.QSizePolicy.Expanding,
                QtWidgets.QSizePolicy.Fixed,
            )
            if loaded:
                workspace.setToolTip(
                    "Close this reconstruction object from the right-hand list; saved Session data is preserved"
                )
            elif saved:
                workspace.setToolTip("Add this saved result to the Reconstruction objects list")
            else:
                workspace.setToolTip("Reconstruction is not currently available")
                workspace.setEnabled(False)
            workspace.clicked.connect(
                lambda _checked=False, k=key: self.sigWorkspaceToggleRequested.emit(k)
            )
            self.setItemWidget(item, 2, workspace)

            if key in selected_before:
                item.setSelected(True)
            if selected_key and key == selected_key:
                restore_current = item

        if restore_current is not None:
            self.setCurrentItem(restore_current)
            restore_current.setSelected(True)
        self.blockSignals(False)


class ProcessingSessionFrame(QtWidgets.QFrame):
    """Prototype Session UI kept deliberately separate from Directory watcher."""

    sigOpenSession = QtCore.Signal()
    sigRefresh = QtCore.Signal()
    sigDatasetSelectionChanged = QtCore.Signal(object)
    sigSavedSelectionChanged = QtCore.Signal(object)
    sigLoadRawRequested = QtCore.Signal()
    sigReconstructionFinalToggleRequested = QtCore.Signal(object)
    sigReconstructionWorkspaceToggleRequested = QtCore.Signal(object)
    sigDiscardSelectedRequested = QtCore.Signal()
    sigDiscardCandidatesRequested = QtCore.Signal()
    sigAutoSaveChanged = QtCore.Signal(bool)
    sigSaveFormatChanged = QtCore.Signal(str)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setMinimumWidth(0)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Ignored,
            QtWidgets.QSizePolicy.Expanding,
        )

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # Keep the everyday Session chrome to one compact row. Setup is opened
        # on demand from the gear button and collapses automatically once a
        # Session is active.
        header = QtWidgets.QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(4)
        title = QtWidgets.QLabel("SESSION")
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        self.sessionNameLabel = _ElidingLabel("No session")
        self.setupBtn = QtWidgets.QToolButton()
        self.setupBtn.setText("⚙")
        self.setupBtn.setToolTip("Session setup")
        self.setupBtn.setCheckable(True)
        self.setupBtn.setChecked(True)
        self.setupBtn.setAutoRaise(True)
        self.refreshBtn = QtWidgets.QToolButton()
        self.refreshBtn.setText("↻")
        self.refreshBtn.setToolTip("Refresh session")
        self.refreshBtn.setAutoRaise(True)
        header.addWidget(title)
        header.addWidget(self.sessionNameLabel, 1)
        header.addWidget(self.setupBtn)
        header.addWidget(self.refreshBtn)
        root.addLayout(header)

        self.setupSection = _CollapsibleSection(
            "Session setup", expanded=True
        )
        # The persistent SESSION header replaces the section's own header.
        self.setupSection.toggle.hide()
        setup_layout = QtWidgets.QVBoxLayout()
        setup_layout.setContentsMargins(0, 0, 0, 0)
        setup_layout.setSpacing(4)

        path_grid = QtWidgets.QGridLayout()
        path_grid.setContentsMargins(0, 0, 0, 0)
        path_grid.setColumnStretch(1, 1)

        self.rawRootEdit = QtWidgets.QLineEdit()
        self.rawRootEdit.setPlaceholderText("Raw data folder")
        self.rawRootEdit.setReadOnly(True)
        self.rawRootEdit.setMinimumWidth(0)
        self.rawRootEdit.setSizePolicy(
            QtWidgets.QSizePolicy.Ignored,
            QtWidgets.QSizePolicy.Fixed,
        )
        self.rawBrowseBtn = QtWidgets.QPushButton("Browse…")

        self.resultsRootEdit = QtWidgets.QLineEdit()
        self.resultsRootEdit.setPlaceholderText("Results folder (defaults to <raw>_recon)")
        self.resultsRootEdit.setReadOnly(True)
        self.resultsRootEdit.setMinimumWidth(0)
        self.resultsRootEdit.setSizePolicy(
            QtWidgets.QSizePolicy.Ignored,
            QtWidgets.QSizePolicy.Fixed,
        )
        self.resultsBrowseBtn = QtWidgets.QPushButton("Browse…")

        self.openBtn = QtWidgets.QPushButton("Open session")
        path_grid.addWidget(QtWidgets.QLabel("Raw"), 0, 0)
        path_grid.addWidget(self.rawRootEdit, 0, 1)
        path_grid.addWidget(self.rawBrowseBtn, 0, 2)
        path_grid.addWidget(QtWidgets.QLabel("Results"), 1, 0)
        path_grid.addWidget(self.resultsRootEdit, 1, 1)
        path_grid.addWidget(self.resultsBrowseBtn, 1, 2)
        path_grid.addWidget(self.openBtn, 2, 1, 1, 2)
        setup_layout.addLayout(path_grid)

        save_row = QtWidgets.QHBoxLayout()
        save_row.setContentsMargins(0, 0, 0, 0)
        self.autoSaveCheck = QtWidgets.QCheckBox("Auto-save new reconstructions")
        self.autoSaveCheck.setChecked(True)
        self.saveFormatCombo = QtWidgets.QComboBox()
        self.saveFormatCombo.addItem("OME-TIFF", "tiff")
        self.saveFormatCombo.addItem("HDF5", "hdf5")
        self.saveFormatCombo.addItem("OME-Zarr", "zarr")
        self.saveFormatCombo.setMinimumContentsLength(6)
        self.saveFormatCombo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon
        )
        save_row.addWidget(self.autoSaveCheck)
        save_row.addStretch(1)
        save_row.addWidget(QtWidgets.QLabel("Format"))
        save_row.addWidget(self.saveFormatCombo)
        setup_layout.addLayout(save_row)
        self.setupSection.setContentLayout(setup_layout)
        root.addWidget(self.setupSection)

        # The raw dataset list is the main Session surface and receives nearly
        # all spare vertical space in the compact state.  Search, ordering and
        # workflow-status filtering intentionally share one compact row so the
        # dock remains usable when narrow.
        datasets_box = QtWidgets.QGroupBox("Raw datasets")
        datasets_box.setMinimumWidth(0)
        datasets_box.setSizePolicy(
            QtWidgets.QSizePolicy.Ignored,
            QtWidgets.QSizePolicy.Expanding,
        )
        datasets_layout = QtWidgets.QVBoxLayout(datasets_box)
        datasets_layout.setSpacing(4)

        # Compact per-column controls: dataset search/order on the left and
        # status filter/order on the right.  They intentionally double as the
        # table header so no extra vertical row is needed.
        filter_row = QtWidgets.QHBoxLayout()
        filter_row.setContentsMargins(0, 0, 0, 0)
        filter_row.setSpacing(4)

        dataset_controls = QtWidgets.QHBoxLayout()
        dataset_controls.setContentsMargins(0, 0, 0, 0)
        dataset_controls.setSpacing(3)
        self.datasetSearchEdit = QtWidgets.QLineEdit()
        self.datasetSearchEdit.setPlaceholderText("Filter datasets…")
        self.datasetSearchEdit.setClearButtonEnabled(True)
        self.datasetSearchEdit.setToolTip(
            "Filter datasets by name or source path"
        )
        self.datasetSearchEdit.setMinimumWidth(48)
        self.datasetSearchEdit.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Fixed,
        )

        self.datasetOrderBtn = QtWidgets.QToolButton()
        self.datasetOrderBtn.setText("↕")
        self.datasetOrderBtn.setAutoRaise(True)
        self.datasetOrderBtn.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.datasetOrderBtn.setFixedWidth(26)
        self.datasetOrderMenu = QtWidgets.QMenu(self.datasetOrderBtn)
        self.datasetOrderGroup = QtWidgets.QActionGroup(self.datasetOrderMenu)
        self.datasetOrderGroup.setExclusive(True)
        self._datasetOrderMode = "latest"
        for label, mode in (
            ("Latest first", "latest"),
            ("Oldest first", "oldest"),
            ("Name A → Z", "name_asc"),
            ("Name Z → A", "name_desc"),
        ):
            action = self.datasetOrderMenu.addAction(label)
            action.setCheckable(True)
            action.setData(mode)
            action.setChecked(mode == self._datasetOrderMode)
            self.datasetOrderGroup.addAction(action)
        self.datasetOrderBtn.setMenu(self.datasetOrderMenu)
        self.datasetOrderBtn.setToolTip("Dataset order: Latest first")
        dataset_controls.addWidget(self.datasetSearchEdit, 1)
        dataset_controls.addWidget(self.datasetOrderBtn)

        status_controls = QtWidgets.QHBoxLayout()
        status_controls.setContentsMargins(0, 0, 0, 0)
        status_controls.setSpacing(3)
        self.datasetShowCombo = QtWidgets.QComboBox()
        self.datasetShowCombo.addItem("All", "all")
        self.datasetShowCombo.addItem("Raw only", "raw")
        self.datasetShowCombo.addItem("Reconstructed", "reconstructed")
        self.datasetShowCombo.addItem("Review only", "review")
        self.datasetShowCombo.addItem("Final only", "final")
        self.datasetShowCombo.setToolTip("Filter by reconstruction status")
        self.datasetShowCombo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon
        )
        self.datasetShowCombo.setMinimumWidth(100)
        self.datasetShowCombo.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Fixed,
        )

        self.statusOrderBtn = QtWidgets.QToolButton()
        self.statusOrderBtn.setText("↕")
        self.statusOrderBtn.setAutoRaise(True)
        self.statusOrderBtn.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.statusOrderBtn.setFixedWidth(26)
        self.statusOrderMenu = QtWidgets.QMenu(self.statusOrderBtn)
        self.statusOrderGroup = QtWidgets.QActionGroup(self.statusOrderMenu)
        self.statusOrderGroup.setExclusive(True)
        self._statusOrderMode = "none"
        for label, mode in (
            ("No status grouping", "none"),
            ("Raw first", "raw"),
            ("Reconstructed first", "reconstructed"),
        ):
            action = self.statusOrderMenu.addAction(label)
            action.setCheckable(True)
            action.setData(mode)
            action.setChecked(mode == self._statusOrderMode)
            self.statusOrderGroup.addAction(action)
        self.statusOrderBtn.setMenu(self.statusOrderMenu)
        self.statusOrderBtn.setToolTip("Status order: No status grouping")
        status_controls.addWidget(self.datasetShowCombo, 1)
        status_controls.addWidget(self.statusOrderBtn)

        dataset_controls_widget = QtWidgets.QWidget()
        dataset_controls_widget.setLayout(dataset_controls)
        dataset_controls_widget.setMinimumWidth(0)
        dataset_controls_widget.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed
        )
        status_controls_widget = QtWidgets.QWidget()
        status_controls_widget.setLayout(status_controls)
        status_controls_widget.setFixedWidth(132)

        filter_row.addWidget(dataset_controls_widget, 1)
        filter_row.addWidget(status_controls_widget)
        datasets_layout.addLayout(filter_row)

        self.datasetList = _CompactDatasetTree()
        datasets_layout.addWidget(self.datasetList)
        root.addWidget(datasets_box, 1)
        self._datasetEntries = []

        # Keep selected-dataset state to one line. The filename is already
        # visible in the raw list; this row is reserved for reconstruction
        # state plus the explicit raw-load action.
        selected_summary = QtWidgets.QHBoxLayout()
        selected_summary.setContentsMargins(0, 0, 0, 0)
        selected_summary.setSpacing(4)
        self.summaryLabel = _ElidingLabel("No dataset selected")
        self.loadRawBtn = QtWidgets.QPushButton("Load raw")
        self.loadRawBtn.setToolTip("Load the selected raw dataset")
        # The action must always reserve its natural width.  The summary label
        # is the shrinkable/elided part of this row, not the button.
        self.loadRawBtn.setSizePolicy(
            QtWidgets.QSizePolicy.Fixed,
            QtWidgets.QSizePolicy.Fixed,
        )
        selected_summary.addWidget(self.summaryLabel, 1)
        selected_summary.addWidget(self.loadRawBtn, 0)
        root.addLayout(selected_summary)

        self.reviewSection = _CollapsibleSection(
            "Review reconstructions (0)", expanded=False
        )
        review_layout = QtWidgets.QVBoxLayout()
        review_layout.setContentsMargins(0, 0, 0, 0)
        review_layout.setSpacing(4)

        self.savedList = _ReconstructionReviewTree()
        review_layout.addWidget(self.savedList)

        cleanup_row = QtWidgets.QHBoxLayout()
        cleanup_row.setContentsMargins(0, 0, 0, 0)
        cleanup_row.setSpacing(4)
        self.discardSelectedBtn = QtWidgets.QPushButton("Discard selected")
        self.discardCandidatesBtn = QtWidgets.QPushButton("Discard candidates")
        self.discardSelectedBtn.setToolTip(
            "Move the selected reconstruction(s) to Session trash"
        )
        self.discardCandidatesBtn.setToolTip(
            "Discard every non-final reconstruction for the selected raw dataset"
        )
        for button in (self.discardSelectedBtn, self.discardCandidatesBtn):
            button.setMinimumWidth(0)
            button.setSizePolicy(
                QtWidgets.QSizePolicy.Expanding,
                QtWidgets.QSizePolicy.Fixed,
            )
        cleanup_row.addWidget(self.discardSelectedBtn, 1)
        cleanup_row.addWidget(self.discardCandidatesBtn, 1)
        review_layout.addLayout(cleanup_row)
        self.reviewSection.setContentLayout(review_layout)
        root.addWidget(self.reviewSection)

        self.statusLabel = _ElidingLabel("No session open")
        root.addWidget(self.statusLabel)

        self.rawBrowseBtn.clicked.connect(self._browse_raw_folder)
        self.resultsBrowseBtn.clicked.connect(self._browse_results_folder)
        self.openBtn.clicked.connect(self.sigOpenSession)
        self.refreshBtn.clicked.connect(self.sigRefresh)
        self.setupBtn.toggled.connect(self.setSetupExpanded)
        self.datasetList.currentItemChanged.connect(self._dataset_changed)
        self.datasetList.itemDoubleClicked.connect(self._dataset_double_clicked)
        self.datasetSearchEdit.textChanged.connect(self._applyDatasetView)
        self.datasetOrderGroup.triggered.connect(self._datasetOrderChanged)
        self.datasetShowCombo.currentIndexChanged.connect(self._applyDatasetView)
        self.statusOrderGroup.triggered.connect(self._statusOrderChanged)
        self.savedList.currentItemChanged.connect(self._saved_changed)
        self.savedList.sigFinalToggleRequested.connect(
            self.sigReconstructionFinalToggleRequested
        )
        self.savedList.sigWorkspaceToggleRequested.connect(
            self.sigReconstructionWorkspaceToggleRequested
        )
        self.savedList.itemSelectionChanged.connect(self._review_selection_changed)
        self.loadRawBtn.clicked.connect(self.sigLoadRawRequested)
        self.discardSelectedBtn.clicked.connect(self.sigDiscardSelectedRequested)
        self.discardCandidatesBtn.clicked.connect(self.sigDiscardCandidatesRequested)
        self.autoSaveCheck.toggled.connect(self.sigAutoSaveChanged)
        self.saveFormatCombo.currentIndexChanged.connect(
            lambda _index: self.sigSaveFormatChanged.emit(
                str(self.saveFormatCombo.currentData() or "tiff")
            )
        )
        self.setActionsEnabled(False, False, False)
        self.reviewSection.toggle.setEnabled(False)

    @staticmethod
    def _browse_start(path_text: str) -> str:
        text = str(path_text or "").strip()
        if not text:
            return str(Path.home())
        path = Path(text).expanduser()
        while str(path) and not path.is_dir():
            parent = path.parent
            if parent == path:
                break
            path = parent
        if path.is_dir():
            return str(path)
        return str(Path.home())

    def _browse_raw_folder(self) -> None:
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Choose raw-data folder",
            self._browse_start(self.rawRoot()),
        )
        if not folder:
            return
        self.rawRootEdit.setText(folder)
        self.setSessionName(Path(folder).name)
        if not self.resultsRoot():
            raw = Path(folder)
            self.resultsRootEdit.setText(str(raw.with_name(raw.name + "_recon")))

    def _browse_results_folder(self) -> None:
        start = self.resultsRoot() or self.rawRoot()
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Choose reconstruction-results folder",
            self._browse_start(start),
        )
        if folder:
            self.resultsRootEdit.setText(folder)

    def _dataset_changed(self, current, _previous):
        value = current.data(0, QtCore.Qt.UserRole) if current is not None else None
        self.sigDatasetSelectionChanged.emit(value)

    def _dataset_double_clicked(self, item, _column):
        if item is None:
            return
        if self.datasetList.currentItem() is not item:
            self.datasetList.setCurrentItem(item)
        self.sigLoadRawRequested.emit()

    def _saved_changed(self, current, _previous):
        value = current.data(0, QtCore.Qt.UserRole) if current is not None else None
        self.sigSavedSelectionChanged.emit(value)

    def rawRoot(self) -> str:
        return self.rawRootEdit.text().strip()

    def resultsRoot(self) -> str:
        return self.resultsRootEdit.text().strip()

    def setPaths(self, raw_root: str, results_root: str) -> None:
        self.rawRootEdit.setText(raw_root)
        self.resultsRootEdit.setText(results_root)
        self.setSessionName(Path(raw_root).name if raw_root else "")

    def setSessionName(self, name: str) -> None:
        self.sessionNameLabel.setText(str(name or "No session"))

    def setSetupExpanded(self, expanded: bool) -> None:
        expanded = bool(expanded)
        self.setupSection.setExpanded(expanded)
        if self.setupBtn.isChecked() != expanded:
            self.setupBtn.blockSignals(True)
            self.setupBtn.setChecked(expanded)
            self.setupBtn.blockSignals(False)

    def setReviewExpanded(self, expanded: bool) -> None:
        self.reviewSection.setExpanded(expanded)

    def setStatus(self, text: str) -> None:
        self.statusLabel.setText(str(text))

    @staticmethod
    def _dataset_state_visible(state: str, mode: str) -> bool:
        state = str(state or "raw").lower()
        mode = str(mode or "all").lower()
        if mode == "all":
            return True
        if mode == "reconstructed":
            return state in {"review", "final"}
        return state == mode

    def _datasetOrderChanged(self, action) -> None:
        mode = str(action.data() or "latest")
        self._datasetOrderMode = mode
        self.datasetOrderBtn.setToolTip(
            f"Dataset order: {action.text()}"
        )
        self._applyDatasetView()

    def _statusOrderChanged(self, action) -> None:
        mode = str(action.data() or "none")
        self._statusOrderMode = mode
        self.statusOrderBtn.setToolTip(
            f"Status order: {action.text()}"
        )
        self._applyDatasetView()

    @staticmethod
    def _status_order_rank(state: str, mode: str) -> int:
        state = str(state or "raw").lower()
        mode = str(mode or "none").lower()
        if mode == "raw":
            return 0 if state == "raw" else 1
        if mode == "reconstructed":
            return 0 if state in {"review", "final"} else 1
        return 0

    def _dataset_colors(self):
        palette = self.palette()
        normal = palette.color(QtGui.QPalette.Text)
        try:
            muted = palette.color(QtGui.QPalette.PlaceholderText)
        except AttributeError:
            muted = palette.color(QtGui.QPalette.Mid)
        dark = palette.color(QtGui.QPalette.Base).lightness() < 128
        final = QtGui.QColor("#72c98c" if dark else "#2e7d32")
        review = QtGui.QColor("#ffb457" if dark else "#b56a00")
        active = palette.color(QtGui.QPalette.Highlight)
        return normal, muted, final, review, active

    def _styleDatasetItem(self, item, state: str) -> None:
        state = str(state or "raw").lower()
        normal, muted, final, review, active = self._dataset_colors()
        name_font = item.font(0)
        status_font = item.font(1)
        name_font.setBold(False)
        status_font.setBold(True)

        if state == "final":
            item.setForeground(0, muted)
            item.setForeground(1, final)
        elif state == "review":
            name_font.setBold(True)
            item.setForeground(0, normal)
            item.setForeground(1, review)
        elif state in {"running", "queued"}:
            name_font.setBold(True)
            item.setForeground(0, normal)
            item.setForeground(1, active)
        elif state == "failed":
            name_font.setBold(True)
            item.setForeground(0, normal)
            item.setForeground(1, review)
        else:  # RAW/new work should remain clearly visible during acquisition.
            item.setForeground(0, normal)
            item.setForeground(1, normal)

        item.setFont(0, name_font)
        item.setFont(1, status_font)

    def _applyDatasetView(self, *_args) -> None:
        current_id = None
        current = self.datasetList.currentItem()
        if current is not None:
            current_id = current.data(0, QtCore.Qt.UserRole)

        query = self.datasetSearchEdit.text().strip().casefold()
        show_mode = str(self.datasetShowCombo.currentData() or "all")
        order_mode = str(self._datasetOrderMode or "latest")
        status_order_mode = str(self._statusOrderMode or "none")
        entries = [
            dict(entry)
            for entry in self._datasetEntries
            if (
                not query
                or query in str(entry.get("search", "")).casefold()
            )
            and self._dataset_state_visible(entry.get("state", "raw"), show_mode)
        ]

        if order_mode == "name_asc":
            entries.sort(key=lambda entry: str(entry.get("label", "")).casefold())
        elif order_mode == "name_desc":
            entries.sort(
                key=lambda entry: str(entry.get("label", "")).casefold(),
                reverse=True,
            )
        elif order_mode == "oldest":
            entries.sort(
                key=lambda entry: (
                    entry.get("timestamp") is None,
                    float(entry.get("timestamp") or 0.0),
                    str(entry.get("label", "")).casefold(),
                )
            )
        else:  # latest first
            entries.sort(
                key=lambda entry: (
                    entry.get("timestamp") is None,
                    -float(entry.get("timestamp") or 0.0),
                    str(entry.get("label", "")).casefold(),
                )
            )

        # Stable second pass groups by status while preserving the chosen
        # dataset ordering within each status group.
        if status_order_mode != "none":
            entries.sort(
                key=lambda entry: self._status_order_rank(
                    entry.get("state", "raw"), status_order_mode
                )
            )

        self.datasetList.blockSignals(True)
        self.datasetList.clear()
        restore = None
        for entry in entries:
            dataset_id = entry.get("id")
            label = str(entry.get("label", ""))
            status = str(entry.get("status", ""))
            item = QtWidgets.QTreeWidgetItem([label, status])
            item.setData(0, QtCore.Qt.UserRole, dataset_id)
            item.setToolTip(0, str(entry.get("search", label)))
            item.setToolTip(1, status)
            self._styleDatasetItem(item, entry.get("state", "raw"))
            self.datasetList.addTopLevelItem(item)
            if dataset_id == current_id:
                restore = item
        self.datasetList.blockSignals(False)

        if restore is not None:
            self.datasetList.setCurrentItem(restore)
        elif self.datasetList.topLevelItemCount():
            self.datasetList.setCurrentItem(self.datasetList.topLevelItem(0))
        else:
            self.sigDatasetSelectionChanged.emit(None)

    def setDatasets(self, entries: list[dict]) -> None:
        """Replace the lightweight dataset catalog displayed by Session."""
        self._datasetEntries = [dict(entry) for entry in entries]
        self._applyDatasetView()

    def setSelectedDataset(self, title: str, summary: str) -> None:
        title = str(title or "")
        # The raw filename remains visible in the dataset list. Use the compact
        # row for state only, while keeping the selected filename in a tooltip.
        compact = str(summary or "").replace("\n", " · ")
        self.summaryLabel.setText(
            f"Selected: {compact}" if title and compact else "No dataset selected"
        )
        if title:
            self.summaryLabel.setToolTip(f"{title}\n{compact}" if compact else title)
        else:
            self.summaryLabel.setToolTip("No dataset selected")
        has_dataset = bool(title)
        self.reviewSection.toggle.setEnabled(has_dataset)
        if not has_dataset:
            self.reviewSection.setExpanded(False)

    def setSavedReconstructions(
        self,
        entries: list[dict],
        selected_path: str | None = None,
    ) -> None:
        self.reviewSection.setTitle(f"Review reconstructions ({len(entries)})")
        self.savedList.setEntries(entries, selected_path)
        if selected_path is None:
            self.sigSavedSelectionChanged.emit(None)
        self._review_selection_changed()

    def selectedReconstructionKeys(self) -> list[str]:
        return self.savedList.selectedKeys()

    def _review_selection_changed(self) -> None:
        self.discardSelectedBtn.setEnabled(bool(self.savedList.selectedItems()))

    def setActionsEnabled(
        self,
        load_raw: bool,
        discard_selected: bool,
        discard_candidates: bool,
    ) -> None:
        self.loadRawBtn.setEnabled(bool(load_raw))
        self.discardSelectedBtn.setEnabled(bool(discard_selected))
        self.discardCandidatesBtn.setEnabled(bool(discard_candidates))


__all__ = ["ProcessingSessionFrame"]
