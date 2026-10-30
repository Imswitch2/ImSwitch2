from __future__ import annotations

import json
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
    """Compact raw-data table: filename, acquisition info, recon state, panel presence."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(4)
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
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.Fixed)
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.Fixed)
        header.resizeSection(1, 118)
        header.resizeSection(2, 92)
        header.resizeSection(3, 24)
        self.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)

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


class _SessionConfigurationDialog(QtWidgets.QDialog):
    """Configure a Session before starting/resuming it."""

    def __init__(
        self,
        *,
        raw_root: str = "",
        results_root: str = "",
        auto_save: bool = True,
        save_format: str = "tiff",
        active_raw_root: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Session configuration")
        self.setModal(True)
        self.setMinimumWidth(520)
        self._activeRawRoot = str(active_raw_root or "")

        root = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QGridLayout()
        form.setColumnStretch(1, 1)

        self.rawEdit = QtWidgets.QLineEdit(str(raw_root or ""))
        self.rawEdit.setReadOnly(True)
        self.rawBrowse = QtWidgets.QPushButton("Browse…")
        self.resultsEdit = QtWidgets.QLineEdit(str(results_root or ""))
        self.resultsEdit.setReadOnly(True)
        self.resultsBrowse = QtWidgets.QPushButton("Browse…")
        form.addWidget(QtWidgets.QLabel("Raw data"), 0, 0)
        form.addWidget(self.rawEdit, 0, 1)
        form.addWidget(self.rawBrowse, 0, 2)
        form.addWidget(QtWidgets.QLabel("Results"), 1, 0)
        form.addWidget(self.resultsEdit, 1, 1)
        form.addWidget(self.resultsBrowse, 1, 2)
        root.addLayout(form)

        options = QtWidgets.QHBoxLayout()
        self.autoSaveCheck = QtWidgets.QCheckBox("Auto-save new reconstructions")
        self.autoSaveCheck.setChecked(bool(auto_save))
        self.formatCombo = QtWidgets.QComboBox()
        self.formatCombo.addItem("OME-TIFF", "tiff")
        self.formatCombo.addItem("HDF5", "hdf5")
        self.formatCombo.addItem("OME-Zarr", "zarr")
        wanted = self.formatCombo.findData(str(save_format or "tiff"))
        self.formatCombo.setCurrentIndex(max(0, wanted))
        options.addWidget(self.autoSaveCheck)
        options.addStretch(1)
        options.addWidget(QtWidgets.QLabel("Result format"))
        options.addWidget(self.formatCombo)
        root.addLayout(options)

        self.infoLabel = QtWidgets.QLabel()
        self.infoLabel.setWordWrap(True)
        root.addWidget(self.infoLabel)

        self.resumeOptions = QtWidgets.QWidget()
        resume_layout = QtWidgets.QHBoxLayout(self.resumeOptions)
        resume_layout.setContentsMargins(0, 0, 0, 0)
        resume_layout.addWidget(QtWidgets.QLabel("On resume"))
        self.resumeReconCombo = QtWidgets.QComboBox()
        self.resumeReconCombo.addItem("Don't add reconstructions", "none")
        self.resumeReconCombo.addItem("Add final reconstructions", "final")
        self.resumeReconCombo.addItem("Add all saved reconstructions", "all")
        resume_layout.addWidget(self.resumeReconCombo, 1)
        self.resumeOptions.setVisible(False)
        root.addWidget(self.resumeOptions)

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Cancel)
        self.primaryBtn = buttons.addButton("Start session", QtWidgets.QDialogButtonBox.AcceptRole)
        self.primaryBtn.clicked.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.rawBrowse.clicked.connect(self._browseRaw)
        self.resultsBrowse.clicked.connect(self._browseResults)
        self._updateState()

    @staticmethod
    def _start_dir(text: str) -> str:
        path = Path(str(text or "")).expanduser()
        while str(path) and not path.is_dir():
            parent = path.parent
            if parent == path:
                break
            path = parent
        return str(path if path.is_dir() else Path.home())

    def _browseRaw(self) -> None:
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Choose raw-data folder", self._start_dir(self.rawEdit.text())
        )
        if not folder:
            return
        previous = self.rawEdit.text().strip()
        self.rawEdit.setText(folder)
        # Raw root defines Session identity: an actual raw-folder change resets
        # the result root to the new Session-local default. Re-selecting the
        # same folder while editing settings keeps a custom results location.
        try:
            changed = Path(previous).resolve() != Path(folder).resolve() if previous else True
        except Exception:
            changed = previous != folder
        if changed:
            self.resultsEdit.setText(str(Path(folder) / "results"))
        self._updateState()

    def _browseResults(self) -> None:
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Choose reconstruction-results folder",
            self._start_dir(self.resultsEdit.text() or self.rawEdit.text()),
        )
        if folder:
            self.resultsEdit.setText(folder)
            self._updateState()

    @staticmethod
    def _manifest_counts(manifest: Path) -> tuple[int, int, int]:
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return 0, 0, 0
        datasets = payload.get("datasets", []) if isinstance(payload, dict) else []
        dataset_count = 0
        final_count = 0
        saved_count = 0
        for dataset in datasets if isinstance(datasets, list) else []:
            if not isinstance(dataset, dict):
                continue
            dataset_count += 1
            results = dataset.get("saved_reconstructions", [])
            for result in results if isinstance(results, list) else []:
                if not isinstance(result, dict):
                    continue
                saved_count += 1
                if bool(result.get("final")):
                    final_count += 1
        return dataset_count, final_count, saved_count

    def _updateState(self) -> None:
        raw = Path(self.rawEdit.text()).expanduser() if self.rawEdit.text() else None
        results = Path(self.resultsEdit.text()).expanduser() if self.resultsEdit.text() else None
        manifest = results / "session.improcess.json" if results else None
        active_same = bool(
            raw
            and self._activeRawRoot
            and str(raw.resolve()) == str(Path(self._activeRawRoot).expanduser().resolve())
        )
        if manifest is not None and manifest.exists():
            self.primaryBtn.setText("Resume session" if not active_same else "Apply")
            dataset_count, final_count, saved_count = self._manifest_counts(manifest)
            candidate_count = max(0, saved_count - final_count)
            dataset_word = "datasets" if dataset_count != 1 else "dataset"
            final_word = "reconstructions" if final_count != 1 else "reconstruction"
            details = (
                f"Existing Session found · {dataset_count} {dataset_word} · "
                f"{final_count} final {final_word}"
            )
            if candidate_count:
                candidate_word = "candidates" if candidate_count != 1 else "candidate"
                details += f" · {candidate_count} additional saved {candidate_word}"
            self.infoLabel.setText(details)
            self.resumeOptions.setVisible(not active_same and bool(saved_count))
            final_text = (
                f"Add final reconstructions ({final_count})"
                if final_count
                else "Add final reconstructions"
            )
            self.resumeReconCombo.setItemText(1, final_text)
            final_item = self.resumeReconCombo.model().item(1)
            if final_item is not None:
                final_item.setEnabled(bool(final_count))
            all_text = (
                f"Add all saved reconstructions ({saved_count})"
                if saved_count
                else "Add all saved reconstructions"
            )
            self.resumeReconCombo.setItemText(2, all_text)
            if not final_count and self.resumeReconCombo.currentData() == "final":
                self.resumeReconCombo.setCurrentIndex(0)
        elif active_same:
            self.primaryBtn.setText("Apply")
            self.infoLabel.setText("Update settings for the active Session.")
            self.resumeOptions.setVisible(False)
        else:
            self.primaryBtn.setText("Start session")
            self.infoLabel.setText("")
            self.resumeOptions.setVisible(False)
        self.primaryBtn.setEnabled(bool(raw and raw.is_dir() and results))

    def configuration(self) -> dict:
        return {
            "raw_root": self.rawEdit.text().strip(),
            "results_root": self.resultsEdit.text().strip(),
            "auto_save": bool(self.autoSaveCheck.isChecked()),
            "save_format": str(self.formatCombo.currentData() or "tiff"),
            # ``configuration()`` is read after ``exec()`` returns.  At that
            # point the dialog itself is hidden, so QWidget.isVisible() is
            # false for every child even when the resume controls were active.
            # Use the widget's explicit hidden state instead.
            "restore_mode": str(self.resumeReconCombo.currentData() or "none")
            if not self.resumeOptions.isHidden()
            else "none",
        }



class ProcessingSessionFrame(QtWidgets.QFrame):
    """Persistent Session UI, deliberately separate from Directory watcher."""

    sigStartSessionRequested = QtCore.Signal()
    sigSettingsRequested = QtCore.Signal()
    sigRefresh = QtCore.Signal()
    sigDatasetSelectionChanged = QtCore.Signal(object)
    sigSavedSelectionChanged = QtCore.Signal(object)
    sigLoadRawRequested = QtCore.Signal()
    sigLoadSelectedAsMultiDataRequested = QtCore.Signal()
    sigRevealRawRequested = QtCore.Signal()
    sigCopyRawPathRequested = QtCore.Signal()
    sigDeleteRawRequested = QtCore.Signal()
    sigReconstructionFinalToggleRequested = QtCore.Signal(object)
    sigReconstructionWorkspaceToggleRequested = QtCore.Signal(object)
    sigDiscardSelectedRequested = QtCore.Signal()
    sigDiscardCandidatesRequested = QtCore.Signal()
    sigAddSelectedReconstructionsRequested = QtCore.Signal(object)
    sigCloseSelectedDatasetReconstructionsRequested = QtCore.Signal()
    sigFollowActiveReconstructionChanged = QtCore.Signal(bool)
    sigCancelAddingRequested = QtCore.Signal()
    sigCloseSessionReconstructionsRequested = QtCore.Signal()
    sigCleanUpSessionRequested = QtCore.Signal()
    sigEmptyTrashRequested = QtCore.Signal()
    sigRevealRawFolderRequested = QtCore.Signal()
    sigRevealResultsFolderRequested = QtCore.Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setMinimumWidth(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Expanding)
        self._datasetEntries: list[dict] = []
        self._sessionActive = False

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        header = QtWidgets.QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(4)
        self.sessionTitleLabel = QtWidgets.QLabel("SESSION")
        font = self.sessionTitleLabel.font()
        font.setBold(True)
        self.sessionTitleLabel.setFont(font)
        self.sessionNameLabel = _ElidingLabel("")
        self.startSessionBtn = QtWidgets.QPushButton("Start session…")
        self.refreshBtn = QtWidgets.QToolButton()
        self.refreshBtn.setText("↻")
        self.refreshBtn.setToolTip("Refresh Session")
        self.refreshBtn.setAutoRaise(True)
        self.livePlaceholderBtn = QtWidgets.QToolButton()
        self.livePlaceholderBtn.setText("LIVE")
        self.livePlaceholderBtn.setToolTip("Live watcher integration will live here")
        self.livePlaceholderBtn.setEnabled(False)
        self.livePlaceholderBtn.setAutoRaise(True)
        self.actionsBtn = QtWidgets.QToolButton()
        self.actionsBtn.setText("Actions")
        self.actionsBtn.setToolTip("Session actions")
        self.actionsBtn.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.actionsBtn.setAutoRaise(True)
        self.settingsBtn = QtWidgets.QToolButton()
        self.settingsBtn.setText("⚙")
        self.settingsBtn.setToolTip("Session settings")
        self.settingsBtn.setAutoRaise(True)

        self.actionsMenu = QtWidgets.QMenu(self.actionsBtn)
        self.followActiveReconstructionAction = self.actionsMenu.addAction("Follow active reconstruction")
        self.followActiveReconstructionAction.setCheckable(True)
        self.followActiveReconstructionAction.setChecked(True)
        self.followActiveReconstructionAction.setToolTip(
            "Select the source Session dataset when the active reconstruction changes"
        )
        self.actionsMenu.addSeparator()
        self.closeReconstructionsAction = self.actionsMenu.addAction("Close session reconstructions")
        self.actionsMenu.addSeparator()
        self.cleanupSessionAction = self.actionsMenu.addAction("Clean up session…")
        self.emptyTrashAction = self.actionsMenu.addAction("Empty Session trash…")
        self.actionsMenu.addSeparator()
        self.revealRawFolderAction = self.actionsMenu.addAction("Reveal raw folder")
        self.revealResultsFolderAction = self.actionsMenu.addAction("Reveal results folder")
        self.actionsBtn.setMenu(self.actionsMenu)

        header.addWidget(self.sessionTitleLabel)
        header.addWidget(self.sessionNameLabel, 1)
        header.addWidget(self.startSessionBtn)
        header.addWidget(self.refreshBtn)
        header.addWidget(self.livePlaceholderBtn)
        header.addWidget(self.actionsBtn)
        header.addWidget(self.settingsBtn)
        root.addLayout(header)

        self.addingRow = QtWidgets.QWidget()
        adding_layout = QtWidgets.QHBoxLayout(self.addingRow)
        adding_layout.setContentsMargins(0, 0, 0, 0)
        adding_layout.setSpacing(4)
        self.addingLabel = QtWidgets.QLabel("Adding reconstructions…")
        self.addingProgress = QtWidgets.QProgressBar()
        self.addingProgress.setTextVisible(True)
        self.addingProgress.setMinimumWidth(80)
        self.addingProgress.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed
        )
        self.cancelAddingBtn = QtWidgets.QPushButton("Cancel")
        self.cancelAddingBtn.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
        adding_layout.addWidget(self.addingLabel)
        adding_layout.addWidget(self.addingProgress, 1)
        adding_layout.addWidget(self.cancelAddingBtn)
        self.addingRow.setVisible(False)
        root.addWidget(self.addingRow)

        datasets_box = QtWidgets.QGroupBox("Raw datasets")
        datasets_box.setMinimumWidth(0)
        datasets_box.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Expanding)
        datasets_layout = QtWidgets.QVBoxLayout(datasets_box)
        datasets_layout.setSpacing(4)

        filter_row = QtWidgets.QHBoxLayout()
        filter_row.setContentsMargins(0, 0, 0, 0)
        filter_row.setSpacing(4)
        dataset_controls = QtWidgets.QHBoxLayout()
        dataset_controls.setContentsMargins(0, 0, 0, 0)
        dataset_controls.setSpacing(3)
        self.datasetSearchEdit = QtWidgets.QLineEdit()
        self.datasetSearchEdit.setPlaceholderText("Filter datasets…")
        self.datasetSearchEdit.setClearButtonEnabled(True)
        self.datasetSearchEdit.setToolTip("Filter datasets by name or source path")
        self.datasetSearchEdit.setMinimumWidth(48)
        self.datasetSearchEdit.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
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
        self.datasetShowCombo.addItem("No recon", "raw")
        self.datasetShowCombo.addItem("Reconstructed", "reconstructed")
        self.datasetShowCombo.addItem("To review", "review")
        self.datasetShowCombo.addItem("Resolved", "final")
        self.datasetShowCombo.setToolTip("Filter by reconstruction status")
        self.datasetShowCombo.setMinimumWidth(100)
        self.datasetShowCombo.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
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
            ("No reconstruction first", "raw"),
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
        dataset_controls_widget.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        status_controls_widget = QtWidgets.QWidget()
        status_controls_widget.setLayout(status_controls)
        status_controls_widget.setFixedWidth(132)
        filter_row.addWidget(dataset_controls_widget, 1)
        filter_row.addWidget(status_controls_widget)
        datasets_layout.addLayout(filter_row)

        self.datasetList = _CompactDatasetTree()
        self.datasetList.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        datasets_layout.addWidget(self.datasetList)
        root.addWidget(datasets_box, 1)

        selected_summary = QtWidgets.QHBoxLayout()
        selected_summary.setContentsMargins(0, 0, 0, 0)
        selected_summary.setSpacing(4)
        self.summaryLabel = _ElidingLabel("No dataset selected")
        self.loadRawBtn = QtWidgets.QPushButton("Load raw")
        self.loadRawBtn.setToolTip("Load the selected raw dataset")
        self.loadRawBtn.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
        selected_summary.addWidget(self.summaryLabel, 1)
        selected_summary.addWidget(self.loadRawBtn, 0)
        root.addLayout(selected_summary)

        self.reviewSection = _CollapsibleSection("Reconstructions (0)", expanded=False)
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
        self.discardSelectedBtn.setToolTip("Move the selected reconstruction(s) to Session trash")
        self.discardCandidatesBtn.setToolTip("Discard every non-final reconstruction for the selected raw dataset")
        for button in (self.discardSelectedBtn, self.discardCandidatesBtn):
            button.setMinimumWidth(0)
            button.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        cleanup_row.addWidget(self.discardSelectedBtn, 1)
        cleanup_row.addWidget(self.discardCandidatesBtn, 1)
        review_layout.addLayout(cleanup_row)
        self.reviewSection.setContentLayout(review_layout)
        root.addWidget(self.reviewSection)

        self.statusLabel = _ElidingLabel("No session active")
        root.addWidget(self.statusLabel)

        self.startSessionBtn.clicked.connect(self.sigStartSessionRequested)
        self.refreshBtn.clicked.connect(self.sigRefresh)
        self.settingsBtn.clicked.connect(self.sigSettingsRequested)
        self.cancelAddingBtn.clicked.connect(self.sigCancelAddingRequested)
        self.followActiveReconstructionAction.toggled.connect(
            self.sigFollowActiveReconstructionChanged
        )
        self.closeReconstructionsAction.triggered.connect(self.sigCloseSessionReconstructionsRequested)
        self.cleanupSessionAction.triggered.connect(self.sigCleanUpSessionRequested)
        self.emptyTrashAction.triggered.connect(self.sigEmptyTrashRequested)
        self.revealRawFolderAction.triggered.connect(self.sigRevealRawFolderRequested)
        self.revealResultsFolderAction.triggered.connect(self.sigRevealResultsFolderRequested)
        self.datasetList.currentItemChanged.connect(self._dataset_changed)
        self.datasetList.itemDoubleClicked.connect(self._dataset_double_clicked)
        self.datasetList.customContextMenuRequested.connect(self._dataset_context_menu)
        self.datasetSearchEdit.textChanged.connect(self._applyDatasetView)
        self.datasetOrderGroup.triggered.connect(self._datasetOrderChanged)
        self.datasetShowCombo.currentIndexChanged.connect(self._applyDatasetView)
        self.statusOrderGroup.triggered.connect(self._statusOrderChanged)
        self.savedList.currentItemChanged.connect(self._saved_changed)
        self.savedList.sigFinalToggleRequested.connect(self.sigReconstructionFinalToggleRequested)
        self.savedList.sigWorkspaceToggleRequested.connect(self.sigReconstructionWorkspaceToggleRequested)
        self.savedList.itemSelectionChanged.connect(self._review_selection_changed)
        self.loadRawBtn.clicked.connect(self.sigLoadRawRequested)
        self.discardSelectedBtn.clicked.connect(self.sigDiscardSelectedRequested)
        self.discardCandidatesBtn.clicked.connect(self.sigDiscardCandidatesRequested)

        self.setSessionActive(False)
        self.setActionsEnabled(False, False, False)
        self.reviewSection.toggle.setEnabled(False)

    def requestSessionConfiguration(
        self,
        *,
        raw_root: str = "",
        results_root: str = "",
        auto_save: bool = True,
        save_format: str = "tiff",
        active_raw_root: str = "",
    ) -> dict | None:
        if raw_root and not results_root:
            results_root = str(Path(raw_root) / "results")
        dialog = _SessionConfigurationDialog(
            raw_root=raw_root,
            results_root=results_root,
            auto_save=auto_save,
            save_format=save_format,
            active_raw_root=active_raw_root,
            parent=self,
        )
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return None
        return dialog.configuration()

    def setSessionActive(self, active: bool, name: str = "") -> None:
        self._sessionActive = bool(active)
        self.sessionTitleLabel.setText("SESSION:" if active else "SESSION")
        self.sessionNameLabel.setText(str(name or ""))
        self.sessionNameLabel.setVisible(bool(active))
        self.startSessionBtn.setVisible(not active)
        for widget in (self.refreshBtn, self.livePlaceholderBtn, self.actionsBtn, self.settingsBtn):
            widget.setVisible(bool(active))
        self.datasetList.setEnabled(bool(active))
        self.datasetSearchEdit.setEnabled(bool(active))
        self.datasetOrderBtn.setEnabled(bool(active))
        self.datasetShowCombo.setEnabled(bool(active))
        self.statusOrderBtn.setEnabled(bool(active))

    def setAddingProgress(self, active: bool, current: int = 0, total: int = 0) -> None:
        active = bool(active)
        total = max(0, int(total or 0))
        current = max(0, min(int(current or 0), total)) if total else 0
        self.addingRow.setVisible(active)
        for widget in (self.refreshBtn, self.actionsBtn, self.settingsBtn):
            widget.setEnabled(bool(self._sessionActive and not active))
        if active:
            self.addingLabel.setText("Adding reconstructions…")
            self.addingProgress.setRange(0, max(1, total))
            self.addingProgress.setValue(current)
            self.addingProgress.setFormat(f"{current} / {total}")
            self.cancelAddingBtn.setEnabled(True)

    def setStatus(self, text: str) -> None:
        self.statusLabel.setText(str(text))

    def _dataset_changed(self, current, _previous):
        value = current.data(0, QtCore.Qt.UserRole) if current is not None else None
        self.sigDatasetSelectionChanged.emit(value)

    def _dataset_double_clicked(self, item, _column):
        if item is None or bool(item.data(0, QtCore.Qt.UserRole + 2)):
            return
        if self.datasetList.currentItem() is not item:
            self.datasetList.setCurrentItem(item)
        self.sigLoadRawRequested.emit()

    def _dataset_context_menu(self, point) -> None:
        item = self.datasetList.itemAt(point)
        if item is None:
            return
        if not item.isSelected():
            self.datasetList.clearSelection()
            item.setSelected(True)
            self.datasetList.setCurrentItem(item)
        selected = self.selectedDatasetIds()
        menu = QtWidgets.QMenu(self)

        selected_items = list(self.datasetList.selectedItems())
        saved_count = sum(
            int((item.data(0, QtCore.Qt.UserRole + 1) or {}).get("saved_count", 0))
            for item in selected_items
        )
        final_count = sum(
            int((item.data(0, QtCore.Qt.UserRole + 1) or {}).get("final_count", 0))
            for item in selected_items
        )
        panel_count = sum(
            int((item.data(0, QtCore.Qt.UserRole + 1) or {}).get("panel_count", 0))
            for item in selected_items
        )
        if saved_count:
            add_menu = menu.addMenu("Add reconstructions")
            finals = add_menu.addAction(
                f"Final only ({final_count})" if final_count else "Final only"
            )
            finals.setEnabled(bool(final_count))
            finals.triggered.connect(
                lambda _checked=False: self.sigAddSelectedReconstructionsRequested.emit("final")
            )
            all_saved = add_menu.addAction(f"All saved ({saved_count})")
            all_saved.triggered.connect(
                lambda _checked=False: self.sigAddSelectedReconstructionsRequested.emit("all")
            )
            menu.addSeparator()

        if panel_count:
            close = menu.addAction(
                f"Close reconstruction{'s' if panel_count != 1 else ''} ({panel_count})"
            )
            close.triggered.connect(self.sigCloseSelectedDatasetReconstructionsRequested)
            menu.addSeparator()

        if len(selected) > 1:
            action = menu.addAction("Load selected as MultiData")
            action.triggered.connect(self.sigLoadSelectedAsMultiDataRequested)
            menu.addSeparator()
        reveal = menu.addAction("Reveal in Explorer")
        reveal.triggered.connect(self.sigRevealRawRequested)
        copy = menu.addAction("Copy path")
        copy.triggered.connect(self.sigCopyRawPathRequested)
        menu.addSeparator()
        delete = menu.addAction("Delete raw data…")
        delete.triggered.connect(self.sigDeleteRawRequested)
        delete.setEnabled(not all(bool(it.data(0, QtCore.Qt.UserRole + 2)) for it in self.datasetList.selectedItems()))
        menu.exec(self.datasetList.viewport().mapToGlobal(point))

    def _saved_changed(self, current, _previous):
        value = current.data(0, QtCore.Qt.UserRole) if current is not None else None
        self.sigSavedSelectionChanged.emit(value)

    def selectedDatasetIds(self) -> list[str]:
        ids = []
        for item in self.datasetList.selectedItems():
            value = item.data(0, QtCore.Qt.UserRole)
            if value:
                ids.append(str(value))
        return list(dict.fromkeys(ids))

    def currentDatasetId(self) -> str | None:
        item = self.datasetList.currentItem()
        value = item.data(0, QtCore.Qt.UserRole) if item is not None else None
        return str(value) if value else None

    def selectDataset(self, dataset_id: str) -> bool:
        """Select and reveal one visible Session dataset, like a user click."""
        dataset_id = str(dataset_id or "")
        for index in range(self.datasetList.topLevelItemCount()):
            item = self.datasetList.topLevelItem(index)
            if str(item.data(0, QtCore.Qt.UserRole) or "") != dataset_id:
                continue
            self.datasetList.clearSelection()
            item.setSelected(True)
            self.datasetList.setCurrentItem(item)
            self.datasetList.scrollToItem(item, QtWidgets.QAbstractItemView.PositionAtCenter)
            return True
        return False

    def setReviewExpanded(self, expanded: bool = True) -> None:
        self.reviewSection.setExpanded(bool(expanded))

    def setFollowActiveReconstruction(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self.followActiveReconstructionAction.isChecked() == enabled:
            return
        self.followActiveReconstructionAction.blockSignals(True)
        self.followActiveReconstructionAction.setChecked(enabled)
        self.followActiveReconstructionAction.blockSignals(False)

    def copyText(self, text: str) -> None:
        QtWidgets.QApplication.clipboard().setText(str(text or ""))

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
        self._datasetOrderMode = str(action.data() or "latest")
        self.datasetOrderBtn.setToolTip(f"Dataset order: {action.text()}")
        self._applyDatasetView()

    def _statusOrderChanged(self, action) -> None:
        self._statusOrderMode = str(action.data() or "none")
        self.statusOrderBtn.setToolTip(f"Status order: {action.text()}")
        self._applyDatasetView()

    @staticmethod
    def _status_order_rank(state: str, mode: str) -> int:
        state = str(state or "raw").lower()
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
        status_font = item.font(2)
        name_font.setBold(False)
        status_font.setBold(True)
        if state == "final":
            item.setForeground(0, muted)
            item.setForeground(2, final)
        elif state == "review":
            name_font.setBold(True)
            item.setForeground(0, normal)
            item.setForeground(2, review)
        elif state in {"running", "queued", "loading"}:
            name_font.setBold(True)
            item.setForeground(0, normal)
            item.setForeground(2, active)
        elif state == "failed":
            name_font.setBold(True)
            item.setForeground(0, normal)
            item.setForeground(2, review)
        else:
            item.setForeground(0, normal)
            item.setForeground(2, normal)
        item.setForeground(1, muted)
        if item.text(3):
            item.setForeground(3, active)
            link_font = item.font(3)
            link_font.setBold(True)
            item.setFont(3, link_font)
        item.setFont(0, name_font)
        item.setFont(2, status_font)

    def _applyDatasetView(self, *_args) -> None:
        selected_ids = set(self.selectedDatasetIds())
        current_id = self.currentDatasetId()
        query = self.datasetSearchEdit.text().strip().casefold()
        show_mode = str(self.datasetShowCombo.currentData() or "all")
        order_mode = str(self._datasetOrderMode or "latest")
        status_order_mode = str(self._statusOrderMode or "none")
        entries = [
            dict(entry) for entry in self._datasetEntries
            if (not query or query in str(entry.get("search", "")).casefold())
            and self._dataset_state_visible(entry.get("state", "raw"), show_mode)
        ]
        if order_mode == "name_asc":
            entries.sort(key=lambda e: str(e.get("label", "")).casefold())
        elif order_mode == "name_desc":
            entries.sort(key=lambda e: str(e.get("label", "")).casefold(), reverse=True)
        elif order_mode == "oldest":
            entries.sort(key=lambda e: (e.get("timestamp") is None, float(e.get("timestamp") or 0), str(e.get("label", "")).casefold()))
        else:
            entries.sort(key=lambda e: (e.get("timestamp") is None, -float(e.get("timestamp") or 0), str(e.get("label", "")).casefold()))
        if status_order_mode != "none":
            entries.sort(key=lambda e: self._status_order_rank(e.get("state", "raw"), status_order_mode))

        self.datasetList.blockSignals(True)
        self.datasetList.clear()
        restore_current = None
        for entry in entries:
            dataset_id = entry.get("id")
            label = str(entry.get("label", ""))
            if entry.get("current"):
                label = f"▶ {label}"
            item = QtWidgets.QTreeWidgetItem([
                label,
                str(entry.get("info", "")),
                str(entry.get("status", "")),
                "●" if entry.get("panel_link") else "",
            ])
            item.setData(0, QtCore.Qt.UserRole, dataset_id)
            item.setData(0, QtCore.Qt.UserRole + 1, dict(entry))
            item.setData(0, QtCore.Qt.UserRole + 2, bool(entry.get("raw_deleted", False)))
            item.setToolTip(0, str(entry.get("tooltip") or entry.get("search", label)))
            item.setToolTip(1, str(entry.get("info_tooltip") or entry.get("info", "")))
            item.setToolTip(2, str(entry.get("status", "")))
            panel_count = int(entry.get("panel_count", 0) or 0)
            if panel_count:
                item.setToolTip(
                    3,
                    f"{panel_count} reconstruction object{'s' if panel_count != 1 else ''} "
                    "from this dataset are in the Reconstruction panel.",
                )
                item.setTextAlignment(3, QtCore.Qt.AlignCenter)
            self._styleDatasetItem(item, entry.get("state", "raw"))
            self.datasetList.addTopLevelItem(item)
            if dataset_id in selected_ids:
                item.setSelected(True)
            if dataset_id == current_id:
                restore_current = item
        self.datasetList.blockSignals(False)
        if restore_current is not None:
            self.datasetList.setCurrentItem(restore_current)
        elif self.datasetList.topLevelItemCount() and self.datasetList.currentItem() is None:
            self.datasetList.setCurrentItem(self.datasetList.topLevelItem(0))
        elif not self.datasetList.topLevelItemCount():
            self.sigDatasetSelectionChanged.emit(None)

    def setDatasets(self, entries: list[dict]) -> None:
        self._datasetEntries = [dict(entry) for entry in entries]
        self._applyDatasetView()

    def setSelectedDataset(self, title: str, summary: str) -> None:
        title = str(title or "")
        compact = str(summary or "").replace("\n", " · ")
        self.summaryLabel.setText(f"Selected: {compact}" if title and compact else "No dataset selected")
        self.summaryLabel.setToolTip(f"{title}\n{compact}" if title and compact else (title or "No dataset selected"))
        has_dataset = bool(title)
        self.reviewSection.toggle.setEnabled(has_dataset)
        if not has_dataset:
            self.reviewSection.setExpanded(False)

    def setSavedReconstructions(self, entries: list[dict], selected_path: str | None = None) -> None:
        self.reviewSection.setTitle(f"Reconstructions ({len(entries)})")
        self.savedList.setEntries(entries, selected_path)
        if selected_path is None:
            self.sigSavedSelectionChanged.emit(None)
        self._review_selection_changed()

    def selectedReconstructionKeys(self) -> list[str]:
        return self.savedList.selectedKeys()

    def clearReconstructionSelection(self) -> None:
        """Clear review-list selection without feeding a navigation signal back."""
        self.savedList.blockSignals(True)
        try:
            self.savedList.clearSelection()
        finally:
            self.savedList.blockSignals(False)
        self._review_selection_changed()

    def _review_selection_changed(self) -> None:
        self.discardSelectedBtn.setEnabled(bool(self.savedList.selectedItems()))

    def setActionsEnabled(self, load_raw: bool, discard_selected: bool, discard_candidates: bool) -> None:
        self.loadRawBtn.setEnabled(bool(load_raw))
        self.discardSelectedBtn.setEnabled(bool(discard_selected))
        self.discardCandidatesBtn.setEnabled(bool(discard_candidates))


__all__ = ["ProcessingSessionFrame"]
