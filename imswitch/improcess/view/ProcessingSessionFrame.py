from __future__ import annotations

import json
from pathlib import Path

from qtpy import QtCore, QtGui, QtWidgets


_FAVORITE_COL_W = 24
_ACQUISITION_COL_W = 118
_STATUS_COL_W = 80
_RESULT_ICON_COL_W = 16


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


class _WatchingSwitch(QtWidgets.QAbstractButton):
    """Compact, accessible Qt toggle for the Session directory observer.

    The status dot and text are separate widgets: only this switch is the
    interaction target, and its visual state follows the controller.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setAccessibleName("Monitor session folder")
        self.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)

    def sizeHint(self):
        return QtCore.QSize(40, 22)

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        painter.setPen(QtCore.Qt.NoPen)
        track = QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if not self.isEnabled():
            color = self.palette().color(QtGui.QPalette.Mid)
        elif self.isChecked():
            color = QtGui.QColor("#27865d")
        else:
            color = self.palette().color(QtGui.QPalette.Mid)
        painter.setBrush(color)
        painter.drawRoundedRect(track, track.height() / 2, track.height() / 2)
        diameter = track.height() - 4
        x = track.right() - diameter - 2 if self.isChecked() else track.left() + 2
        thumb = QtCore.QRectF(x, track.top() + 2, diameter, diameter)
        painter.setBrush(self.palette().color(QtGui.QPalette.Base))
        painter.drawEllipse(thumb)
        if self.hasFocus():
            painter.setBrush(QtCore.Qt.NoBrush)
            painter.setPen(QtGui.QPen(self.palette().color(QtGui.QPalette.Highlight), 1))
            painter.drawRoundedRect(track.adjusted(-0.5, -0.5, 0.5, 0.5),
                                    track.height() / 2, track.height() / 2)


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
        self.setColumnCount(5)
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
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.Fixed)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.Fixed)
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.Fixed)
        header.setSectionResizeMode(4, QtWidgets.QHeaderView.Fixed)
        header.resizeSection(0, _FAVORITE_COL_W)
        header.resizeSection(2, _ACQUISITION_COL_W)
        header.resizeSection(3, _STATUS_COL_W)
        header.resizeSection(4, _RESULT_ICON_COL_W)
        self.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)

    def fitAuxiliaryColumns(self) -> None:
        """Give spare width to the dataset name, not empty status/icon columns."""
        header = self.header()
        metrics = self.fontMetrics()
        statuses = [
            self.topLevelItem(index).text(3)
            for index in range(self.topLevelItemCount())
        ]
        widest_status = max(
            (metrics.horizontalAdvance(text) for text in statuses if text),
            default=metrics.horizontalAdvance("No result"),
        )
        # Keep normal states readable but never let transient long status text
        # steal substantial space from the (more useful) dataset name.
        status_width = max(54, min(86, widest_status + 12))
        header.resizeSection(3, status_width)

        has_panel_link = any(
            bool(self.topLevelItem(index).text(4))
            for index in range(self.topLevelItemCount())
        )
        self.setColumnHidden(4, not has_panel_link)
        if has_panel_link:
            header.resizeSection(4, _RESULT_ICON_COL_W)

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


class _DatasetFilterBar(QtWidgets.QWidget):
    """Filter controls aligned to the dataset tree's actual header geometry.

    The tree is the source of truth for column widths.  This avoids drift from
    frame widths, DPI/style differences, and vertical-scrollbar visibility.
    """

    _WATCHED_EVENTS = {
        QtCore.QEvent.Resize,
        QtCore.QEvent.Show,
        QtCore.QEvent.LayoutRequest,
        QtCore.QEvent.StyleChange,
        QtCore.QEvent.FontChange,
    }

    def __init__(
        self,
        tree: QtWidgets.QTreeWidget,
        *,
        favorite_widget: QtWidgets.QWidget,
        name_widget: QtWidgets.QWidget,
        acquisition_widget: QtWidgets.QWidget,
        status_widget: QtWidgets.QWidget,
        parent=None,
    ):
        super().__init__(parent)
        self._tree = tree
        self._header = tree.header()
        self._bindings = (
            (favorite_widget, (0,)),
            (name_widget, (1,)),
            (acquisition_widget, (2,)),
            (status_widget, (3, 4)),
        )
        for widget, _columns in self._bindings:
            widget.setParent(self)
            widget.setMinimumWidth(0)

        self.setMinimumWidth(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        self.setFixedHeight(
            max(widget.sizeHint().height() for widget, _columns in self._bindings)
        )

        self._header.sectionResized.connect(self._scheduleSync)
        self._header.geometriesChanged.connect(self._scheduleSync)
        tree.verticalScrollBar().rangeChanged.connect(self._scheduleSync)

        for watched in (tree, tree.viewport(), self._header, tree.verticalScrollBar()):
            watched.installEventFilter(self)

        QtCore.QTimer.singleShot(0, self._syncGeometry)

    def eventFilter(self, watched, event):
        if event.type() in self._WATCHED_EVENTS:
            self._scheduleSync()
        return super().eventFilter(watched, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._syncGeometry()

    @QtCore.Slot()
    def _scheduleSync(self, *args):
        QtCore.QTimer.singleShot(0, self._syncGeometry)

    def _syncGeometry(self):
        if self._tree is None or self._header is None:
            return
        viewport = self._tree.viewport()
        if viewport is None:
            return

        # Header section positions are in the tree viewport coordinate system.
        # Map that viewport origin into this widget so the filter controls also
        # account for the tree frame and platform-specific style metrics.
        origin = self.mapFromGlobal(viewport.mapToGlobal(QtCore.QPoint(0, 0)))
        height = self.height()
        for widget, columns in self._bindings:
            visible_columns = [
                col for col in columns if not self._header.isSectionHidden(col)
            ]
            if not visible_columns:
                widget.hide()
                continue
            first = visible_columns[0]
            x = origin.x() + self._header.sectionViewportPosition(first)
            width = sum(self._header.sectionSize(col) for col in visible_columns)
            widget.setGeometry(x, 0, max(0, width), height)
            widget.show()


class _SessionSplitterHandle(QtWidgets.QSplitterHandle):
    """Lightweight splitter boundary that also summarizes reconstruction state.

    The handle keeps a normal-sized mouse target for dragging, but deliberately
    paints no native splitter chrome.  Visually it is only a compact disclosure
    label followed by a one-pixel separator line.
    """

    sigExpandedChanged = QtCore.Signal(bool)

    def __init__(self, orientation, parent):
        super().__init__(orientation, parent)
        self.setCursor(QtCore.Qt.SplitVCursor)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        # The handle remains a normal-sized drag target, but contributes no
        # visible panel/header background of its own.
        self.setStyleSheet("background: transparent; border: none;")

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.toggle = QtWidgets.QToolButton(self)
        self.toggle.setCheckable(True)
        self.toggle.setChecked(False)
        self.toggle.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
        self.toggle.setAutoRaise(True)
        self.toggle.setText("› Results (0)")
        self.toggle.setCursor(QtCore.Qt.ArrowCursor)
        self.toggle.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
        self.toggle.setStyleSheet(
            "QToolButton { border: none; background: transparent; padding: 0 2px 0 0; }"
            "QToolButton:hover { border: none; background: transparent; }"
            "QToolButton:pressed { border: none; background: transparent; }"
        )
        self.toggle.toggled.connect(self._toggle_changed)
        layout.addWidget(self.toggle)
        layout.addStretch(1)

    def sizeHint(self):
        hint = super().sizeHint()
        hint.setHeight(self.fontMetrics().height() + 2)
        return hint

    def paintEvent(self, event) -> None:
        # Do not call QSplitterHandle.paintEvent(): native styles otherwise draw
        # a raised/sunken handle background that makes this boundary look bulky.
        painter = QtGui.QPainter(self)
        color = self.palette().color(QtGui.QPalette.Mid)
        color.setAlpha(150)
        painter.setPen(QtGui.QPen(color, 1))
        y = self.rect().center().y()
        start_x = self.toggle.geometry().right() + 5
        if start_x < self.width():
            painter.drawLine(start_x, y, self.width() - 1, y)

    def _update_toggle_text(self) -> None:
        marker = "⌄" if self.toggle.isChecked() else "›"
        summary = str(self.toggle.property("summaryText") or "Results (0)")
        self.toggle.setText(f"{marker} {summary}")

    def _toggle_changed(self, expanded: bool) -> None:
        self._update_toggle_text()
        self.sigExpandedChanged.emit(bool(expanded))

    def setExpanded(self, expanded: bool) -> None:
        expanded = bool(expanded)
        if self.toggle.isChecked() != expanded:
            self.toggle.blockSignals(True)
            self.toggle.setChecked(expanded)
            self.toggle.blockSignals(False)
        self._update_toggle_text()

    def setSummary(self, total: int, in_panel: int, *, tooltip: str = "") -> None:
        total = max(0, int(total))
        in_panel = max(0, int(in_panel))
        text = f"Results ({total})"
        if total:
            text += f" · {in_panel} in panel"
        self.toggle.setProperty("summaryText", text)
        self._update_toggle_text()
        if tooltip:
            self.toggle.setToolTip(str(tooltip))


class _SessionSplitter(QtWidgets.QSplitter):
    """Vertical splitter whose boundary doubles as the reconstruction header."""

    def createHandle(self):
        return _SessionSplitterHandle(self.orientation(), self)


class _ReconstructionTree(QtWidgets.QTreeWidget):
    """Compact result list; all result actions live in the context menu."""

    sigWorkspaceToggleRequested = QtCore.Signal(object)
    sigDiscardRequested = QtCore.Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(1)
        self.setHeaderHidden(True)
        self.setRootIsDecorated(False)
        self.setItemsExpandable(False)
        self.setIndentation(0)
        self.setUniformRowHeights(True)
        self.setTextElideMode(QtCore.Qt.ElideRight)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.setMinimumWidth(0)
        self._preferredHeight = 68
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Ignored,
            QtWidgets.QSizePolicy.Preferred,
        )
        self.header().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)

    def _heightForRows(self, rows: int) -> int:
        row_height = self.sizeHintForRow(0) if self.topLevelItemCount() else -1
        if row_height <= 0:
            row_height = max(
                self.fontMetrics().height() + 8,
                self.style().pixelMetric(QtWidgets.QStyle.PM_SmallIconSize) + 8,
            )
        return self.frameWidth() * 2 + max(1, int(rows)) * row_height + 2

    def _updateAdaptiveHeight(self) -> None:
        self.doItemsLayout()
        count = self.topLevelItemCount()
        visible_rows = max(1, min(count, 3))
        self._preferredHeight = self._heightForRows(visible_rows)
        self.setMinimumHeight(self._heightForRows(1))
        self.setMaximumHeight(16777215)
        self.updateGeometry()

    def preferredHeight(self) -> int:
        return int(self._preferredHeight)

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        hint.setWidth(0)
        hint.setHeight(self._heightForRows(1))
        return hint

    def sizeHint(self):
        hint = super().sizeHint()
        hint.setWidth(0)
        hint.setHeight(self._preferredHeight)
        return hint

    def selectedKeys(self) -> list[str]:
        keys = []
        for item in self.selectedItems():
            key = item.data(0, QtCore.Qt.UserRole)
            if key:
                keys.append(str(key))
        return keys

    def selectedEntries(self) -> list[dict]:
        return [
            dict(item.data(0, QtCore.Qt.UserRole + 1) or {})
            for item in self.selectedItems()
        ]

    def setEntries(self, entries: list[dict], selected_key: str | None = None) -> None:
        selected_before = set(self.selectedKeys())
        self.blockSignals(True)
        self.clear()
        restore_current = None
        normal = self.palette().color(QtGui.QPalette.Text)

        for entry in entries:
            key = str(entry.get("key", ""))
            name = str(entry.get("name", "reconstruction"))
            item = QtWidgets.QTreeWidgetItem([name])
            item.setData(0, QtCore.Qt.UserRole, key)
            item.setData(0, QtCore.Qt.UserRole + 1, dict(entry))
            item.setToolTip(0, str(entry.get("tooltip") or name))
            item.setForeground(0, normal)
            self.addTopLevelItem(item)
            if key in selected_before:
                item.setSelected(True)
            if selected_key and key == selected_key:
                restore_current = item

        if restore_current is not None:
            self.setCurrentItem(restore_current)
            restore_current.setSelected(True)
        self.blockSignals(False)
        self._updateAdaptiveHeight()


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
        self.autoSaveCheck = QtWidgets.QCheckBox("Auto-save new results")
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
        self.resumeReconCombo.addItem("Don't add results", "none")
        self.resumeReconCombo.addItem("Add results from favorite datasets", "favorites")
        self.resumeReconCombo.addItem("Add all saved results", "all")
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
            "Choose results folder",
            self._start_dir(self.resultsEdit.text() or self.rawEdit.text()),
        )
        if folder:
            self.resultsEdit.setText(folder)
            self._updateState()

    @staticmethod
    def _manifest_counts(manifest: Path) -> tuple[int, int, int, int]:
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return 0, 0, 0, 0
        datasets = payload.get("datasets", []) if isinstance(payload, dict) else []
        dataset_count = 0
        saved_count = 0
        favorite_count = 0
        favorite_saved_count = 0
        for dataset in datasets if isinstance(datasets, list) else []:
            if not isinstance(dataset, dict):
                continue
            dataset_count += 1
            favorite = bool(dataset.get("favorite", False))
            favorite_count += int(favorite)
            results = dataset.get("saved_reconstructions", [])
            count = sum(1 for result in results if isinstance(result, dict)) if isinstance(results, list) else 0
            saved_count += count
            if favorite:
                favorite_saved_count += count
        return dataset_count, saved_count, favorite_count, favorite_saved_count

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
            dataset_count, saved_count, favorite_count, favorite_saved_count = self._manifest_counts(manifest)
            dataset_word = "datasets" if dataset_count != 1 else "dataset"
            recon_word = "results" if saved_count != 1 else "result"
            favorite_text = (
                f" · {favorite_count} favorite{'s' if favorite_count != 1 else ''}"
                if favorite_count else ""
            )
            self.infoLabel.setText(
                f"Existing Session found · {dataset_count} {dataset_word} · "
                f"{saved_count} saved {recon_word}{favorite_text}"
            )
            self.resumeOptions.setVisible(not active_same and bool(saved_count))
            self.resumeReconCombo.setItemText(
                1, f"Add results from favorite datasets ({favorite_saved_count})"
            )
            self.resumeReconCombo.setItemText(
                2, f"Add all saved results ({saved_count})"
            )
            model_item = self.resumeReconCombo.model().item(1)
            if model_item is not None:
                model_item.setEnabled(bool(favorite_saved_count))
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
    sigOpenViewerRequested = QtCore.Signal()
    sigLoadSelectedAsMultiDataRequested = QtCore.Signal()
    sigProcessSelectedRequested = QtCore.Signal(str)
    sigSelectionCountChanged = QtCore.Signal(int)
    sigSessionActiveChanged = QtCore.Signal(bool)
    sigSessionModeChanged = QtCore.Signal(str)
    sigAutoProcessingChanged = QtCore.Signal(bool)
    sigPauseProcessingRequested = QtCore.Signal()
    sigResumeProcessingRequested = QtCore.Signal()
    sigSkipCurrentProcessingRequested = QtCore.Signal()
    sigRevealRawRequested = QtCore.Signal()
    sigCopyRawPathRequested = QtCore.Signal()
    sigDeleteRawRequested = QtCore.Signal()
    sigFavoriteDatasetRequested = QtCore.Signal(object, bool)
    sigSetSelectedFavoriteRequested = QtCore.Signal(bool)
    sigSetSelectedAutoIgnoredRequested = QtCore.Signal(bool)
    sigReconstructionWorkspaceToggleRequested = QtCore.Signal(object)
    sigDiscardReconstructionRequested = QtCore.Signal(object)
    sigDiscardSelectedRequested = QtCore.Signal()
    sigReuseCalibrationRequested = QtCore.Signal(object, str)
    sigAddSelectedReconstructionsRequested = QtCore.Signal()
    sigCloseSelectedDatasetReconstructionsRequested = QtCore.Signal()
    sigDeleteSelectedDatasetReconstructionsRequested = QtCore.Signal()
    sigFollowActiveReconstructionChanged = QtCore.Signal(bool)
    sigCancelAddingRequested = QtCore.Signal()
    sigCloseSessionReconstructionsRequested = QtCore.Signal()
    sigCloseNonFavoriteReconstructionsRequested = QtCore.Signal()
    sigCleanupNonFavoritesRequested = QtCore.Signal()
    sigCloseSessionRequested = QtCore.Signal()
    sigEmptyTrashRequested = QtCore.Signal()
    sigRevealRawFolderRequested = QtCore.Signal()
    sigRevealResultsFolderRequested = QtCore.Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setMinimumWidth(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Expanding)
        self._datasetEntries: list[dict] = []
        self._sessionActive = False
        self._sessionMode = "review"
        self._processingAvailable = True
        self._autoProcessingAvailable = False
        self._lastQueueSnapshot: dict = {}

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
        self.statusLabel = _ElidingLabel("")
        self.statusLabel.setMinimumWidth(0)
        self.statusLabel.setMaximumWidth(150)
        self.statusLabel.setStyleSheet("color: #d08b28;")
        self.statusLabel.hide()
        self._statusGeneration = 0
        self.sessionNameLabel = _ElidingLabel("")
        self.startSessionBtn = QtWidgets.QPushButton("Start session…")
        # Review has no folder watcher, so Refresh remains a manual snapshot.
        self.refreshBtn = QtWidgets.QToolButton()
        self.refreshBtn.setText("↻")
        self.refreshBtn.setToolTip("Rescan Session folder")
        self.refreshBtn.setAutoRaise(True)
        self.refreshBtn.hide()
        # Status and interaction are distinct: the dot describes the actual
        # observer state, the switch activates/deactivates Watching.
        self.watchingControls = QtWidgets.QWidget()
        watching_layout = QtWidgets.QHBoxLayout(self.watchingControls)
        watching_layout.setContentsMargins(0, 0, 0, 0)
        watching_layout.setSpacing(5)
        self.watchStatusDot = QtWidgets.QLabel("●")
        self.watchStatusDot.setStyleSheet("font-size: 9px; color: palette(mid);")
        self.watchStatusLabel = QtWidgets.QLabel("Monitor")
        self.watchToggle = _WatchingSwitch(self.watchingControls)
        self.watchToggle.setToolTip("Start monitoring for new recordings")
        watching_layout.addWidget(self.watchStatusDot)
        watching_layout.addWidget(self.watchStatusLabel)
        watching_layout.addWidget(self.watchToggle)
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
        self.rescanFolderAction = self.actionsMenu.addAction("Rescan folder")
        self.actionsMenu.addSeparator()
        self.closeReconstructionsAction = self.actionsMenu.addAction("Close session results")
        self.closeNonFavoriteReconstructionsAction = self.actionsMenu.addAction(
            "Close results from non-favorites"
        )
        self.cleanupNonFavoritesAction = self.actionsMenu.addAction("Clean up non-favorites…")
        self.closeSessionAction = self.actionsMenu.addAction("Close Session…")
        self.actionsMenu.addSeparator()
        self.emptyTrashAction = self.actionsMenu.addAction("Empty Session trash…")
        self.actionsMenu.addSeparator()
        self.revealRawFolderAction = self.actionsMenu.addAction("Reveal raw folder")
        self.revealResultsFolderAction = self.actionsMenu.addAction("Reveal results folder")
        self.actionsBtn.setMenu(self.actionsMenu)

        header.addWidget(self.sessionTitleLabel)
        header.addWidget(self.statusLabel)
        header.addWidget(self.sessionNameLabel, 1)
        header.addWidget(self.startSessionBtn)
        header.addWidget(self.watchingControls)
        header.addWidget(self.refreshBtn)
        header.addWidget(self.actionsBtn)
        header.addWidget(self.settingsBtn)
        root.addLayout(header)

        self.addingRow = QtWidgets.QWidget()
        adding_layout = QtWidgets.QHBoxLayout(self.addingRow)
        adding_layout.setContentsMargins(0, 0, 0, 0)
        adding_layout.setSpacing(4)
        self.addingLabel = QtWidgets.QLabel("Adding results…")
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

        # Keep the dataset browser visually flat inside the Session tab.  The
        # DATA tab already provides the enclosing context; an additional
        # "Raw datasets" group title/frame only consumes vertical space.
        datasets_box = QtWidgets.QWidget()
        datasets_box.setMinimumWidth(0)
        datasets_box.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Expanding)
        datasets_layout = QtWidgets.QVBoxLayout(datasets_box)
        datasets_layout.setContentsMargins(0, 0, 0, 0)
        datasets_layout.setSpacing(4)

        self.favoriteOnlyBtn = QtWidgets.QToolButton()
        self.favoriteOnlyBtn.setText("★")
        self.favoriteOnlyBtn.setCheckable(True)
        self.favoriteOnlyBtn.setAutoRaise(True)
        self.favoriteOnlyBtn.setMinimumWidth(0)
        self.favoriteOnlyBtn.setToolTip("Show favorites only")

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

        self.dimensionFilterCombo = QtWidgets.QComboBox()
        self.dimensionFilterCombo.addItem("All", "all")
        self.dimensionFilterCombo.setToolTip("Filter by acquisition dimensions")
        self.dimensionFilterCombo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon
        )
        self.dimensionFilterCombo.setMinimumContentsLength(4)
        self.dimensionFilterCombo.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed
        )

        status_controls = QtWidgets.QHBoxLayout()
        status_controls.setContentsMargins(0, 0, 0, 0)
        status_controls.setSpacing(3)
        self.datasetShowCombo = QtWidgets.QComboBox()
        self.datasetShowCombo.addItem("All", "all")
        self.datasetShowCombo.addItem("No result", "raw")
        self.datasetShowCombo.addItem("Has results", "reconstructed")
        self.datasetShowCombo.setToolTip("Filter by result status")
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
            ("No result first", "raw"),
            ("Has results first", "reconstructed"),
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
        dimension_controls_widget = QtWidgets.QWidget()
        dimension_layout = QtWidgets.QHBoxLayout(dimension_controls_widget)
        dimension_layout.setContentsMargins(0, 0, 0, 0)
        dimension_layout.addWidget(self.dimensionFilterCombo)
        # Match the fixed acquisition-info column below; no additional header
        # label is needed because placement makes the filter's meaning clear.
        dimension_controls_widget.setMinimumWidth(0)
        dimension_controls_widget.setSizePolicy(
            QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed
        )
        status_controls_widget = QtWidgets.QWidget()
        status_controls_widget.setLayout(status_controls)
        status_controls_widget.setMinimumWidth(0)
        status_controls_widget.setSizePolicy(
            QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed
        )

        self.datasetList = _CompactDatasetTree()
        self.datasetList.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.datasetFilterBar = _DatasetFilterBar(
            self.datasetList,
            favorite_widget=self.favoriteOnlyBtn,
            name_widget=dataset_controls_widget,
            acquisition_widget=dimension_controls_widget,
            status_widget=status_controls_widget,
        )
        datasets_layout.addWidget(self.datasetFilterBar)
        datasets_layout.addWidget(self.datasetList)


        # Legacy selection controls are still referenced by controller compatibility
        # paths, but selection/reconstruction actions now live in the shared DATA
        # footer. Keep the controls owned by this widget so Qt can never expose a
        # parentless button as a top-level window.
        selection_compat_box = QtWidgets.QWidget(self)
        selection_row = QtWidgets.QHBoxLayout(selection_compat_box)
        selection_row.setContentsMargins(0, 0, 0, 0)
        selection_row.setSpacing(4)
        self.selectionSummaryLabel = QtWidgets.QLabel("No dataset selected")
        self.selectionSummaryLabel.setStyleSheet('color: palette(mid);')
        self.processSelectedBtn = QtWidgets.QToolButton()
        self.processSelectedBtn.setText("Reconstruct ▾")
        self.processSelectedBtn.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.processSelectedBtn.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
        self.processSelectedMenu = QtWidgets.QMenu(self.processSelectedBtn)
        self.processNextAction = self.processSelectedMenu.addAction("Reconstruct next")
        self.processEndAction = self.processSelectedMenu.addAction("Add to end of queue")
        self.processSelectedBtn.setMenu(self.processSelectedMenu)
        self.addWorkingSetBtn = QtWidgets.QPushButton('+ Multi-data')
        self.addWorkingSetBtn.setToolTip('Add the selected Session datasets to Multi-data')
        self.addWorkingSetBtn.clicked.connect(self.sigLoadSelectedAsMultiDataRequested)
        selection_row.addWidget(self.selectionSummaryLabel, 1)
        selection_row.addWidget(self.processSelectedBtn)
        selection_row.addWidget(self.addWorkingSetBtn)
        # Selection actions now live in the shared DATA footer. Keep this hidden
        # compatibility container parented for existing controller paths, but do
        # not consume vertical space inside the Session tab.
        self._selectionCompatibilityBox = selection_compat_box
        root.addWidget(selection_compat_box)
        selection_compat_box.hide()

        # The Session boundary itself carries the reconstruction summary.  This
        # avoids a separate selected-data row and a separate reconstruction title.
        top_pane = QtWidgets.QWidget()
        top_pane.setMinimumWidth(0)
        top_layout = QtWidgets.QVBoxLayout(top_pane)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(0)
        top_layout.addWidget(datasets_box, 1)

        self.reconPane = QtWidgets.QWidget()
        self.reconPane.setMinimumWidth(0)
        recon_layout = QtWidgets.QVBoxLayout(self.reconPane)
        recon_layout.setContentsMargins(0, 0, 0, 0)
        recon_layout.setSpacing(0)

        self.savedList = _ReconstructionTree()
        self.savedList.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.savedList.setVisible(False)
        recon_layout.addWidget(self.savedList, 1)

        self.sessionSplitter = _SessionSplitter(QtCore.Qt.Vertical)
        self.sessionSplitter.setChildrenCollapsible(True)
        self.sessionSplitter.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.sessionSplitter.setAutoFillBackground(False)
        self.sessionSplitter.setStyleSheet(
            "QSplitter { background: transparent; border: none; }"
            "QSplitter::handle { background: transparent; border: none; }"
        )
        self.sessionSplitter.addWidget(top_pane)
        self.sessionSplitter.addWidget(self.reconPane)
        self.sessionSplitter.setStretchFactor(0, 1)
        self.sessionSplitter.setStretchFactor(1, 0)
        self.reconHandle = self.sessionSplitter.handle(1)
        self.sessionSplitter.setHandleWidth(self.reconHandle.sizeHint().height())
        root.addWidget(self.sessionSplitter, 1)

        self._reconUserExpanded = False
        self._reconLastExpandedHeight = 0
        self._programmaticSplitterMove = False
        self._selectedDatasetTitle = ""

        processing_box = QtWidgets.QWidget()
        processing_layout = QtWidgets.QVBoxLayout(processing_box)
        processing_layout.setContentsMargins(0, 0, 0, 0)
        processing_layout.setSpacing(3)

        auto_row = QtWidgets.QHBoxLayout()
        auto_row.setContentsMargins(0, 0, 0, 0)
        self.autoProcessCheck = QtWidgets.QCheckBox("Auto-reconstruct")
        self.autoProcessCheck.setToolTip(
            "Automatically reconstruct new recordings while Watching"
        )
        self.queueSummaryLabel = _ElidingLabel("Queue idle")
        self.queueSummaryLabel.setStyleSheet('color: palette(mid);')
        auto_row.addWidget(self.autoProcessCheck)
        auto_row.addStretch(1)
        processing_layout.addLayout(auto_row)
        self.queueSummaryLabel.hide()

        queue_actions = QtWidgets.QHBoxLayout()
        queue_actions.setContentsMargins(0, 0, 0, 0)
        queue_actions.setSpacing(4)
        self.pauseResumeBtn = QtWidgets.QPushButton("Pause")
        self.skipCurrentBtn = QtWidgets.QPushButton("Skip current")
        self.queueToggleBtn = QtWidgets.QToolButton()
        self.queueToggleBtn.setText("Queue ▸")
        self.queueToggleBtn.setCheckable(True)
        self.queueToggleBtn.setAutoRaise(True)
        queue_actions.addWidget(self.pauseResumeBtn)
        queue_actions.addWidget(self.skipCurrentBtn)
        queue_actions.addStretch(1)
        queue_actions.addWidget(self.queueToggleBtn)
        processing_layout.addLayout(queue_actions)
        self.pauseResumeBtn.hide()
        self.skipCurrentBtn.hide()
        self.queueToggleBtn.hide()

        self.queueList = QtWidgets.QTreeWidget()
        self.queueList.setColumnCount(3)
        self.queueList.setHeaderLabels(["Dataset", "State", "Origin"])
        self.queueList.setRootIsDecorated(False)
        self.queueList.setUniformRowHeights(True)
        self.queueList.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.queueList.setFocusPolicy(QtCore.Qt.NoFocus)
        self.queueList.setMaximumHeight(130)
        self.queueList.setVisible(False)
        processing_layout.addWidget(self.queueList)
        self.queueList.hide()
        # Auto/queue execution controls now live in the shared DATA footer and
        # Queue tab. Keep this hidden compatibility container parented to the
        # Session widget because existing controller state updates still touch
        # its controls (auto checkbox, pause/skip, queue summary).
        self._processingCompatibilityBox = processing_box
        root.addWidget(processing_box)
        processing_box.hide()

        self.startSessionBtn.clicked.connect(self.sigStartSessionRequested)
        self.refreshBtn.clicked.connect(self.sigRefresh)
        self.watchToggle.toggled.connect(self._mode_changed)
        self.rescanFolderAction.triggered.connect(self.sigRefresh)
        self.settingsBtn.clicked.connect(self.sigSettingsRequested)
        self.cancelAddingBtn.clicked.connect(self.sigCancelAddingRequested)
        self.followActiveReconstructionAction.toggled.connect(
            self.sigFollowActiveReconstructionChanged
        )
        self.closeReconstructionsAction.triggered.connect(self.sigCloseSessionReconstructionsRequested)
        self.closeNonFavoriteReconstructionsAction.triggered.connect(
            self.sigCloseNonFavoriteReconstructionsRequested
        )
        self.cleanupNonFavoritesAction.triggered.connect(self.sigCleanupNonFavoritesRequested)
        self.closeSessionAction.triggered.connect(self.sigCloseSessionRequested)
        self.emptyTrashAction.triggered.connect(self.sigEmptyTrashRequested)
        self.revealRawFolderAction.triggered.connect(self.sigRevealRawFolderRequested)
        self.revealResultsFolderAction.triggered.connect(self.sigRevealResultsFolderRequested)
        self.datasetList.currentItemChanged.connect(self._dataset_changed)
        self.datasetList.itemClicked.connect(self._dataset_clicked)
        self.datasetList.itemDoubleClicked.connect(self._dataset_double_clicked)
        self.datasetList.customContextMenuRequested.connect(self._dataset_context_menu)
        self.datasetSearchEdit.textChanged.connect(self._applyDatasetView)
        self.favoriteOnlyBtn.toggled.connect(self._applyDatasetView)
        self.datasetOrderGroup.triggered.connect(self._datasetOrderChanged)
        self.dimensionFilterCombo.currentIndexChanged.connect(self._applyDatasetView)
        self.datasetShowCombo.currentIndexChanged.connect(self._applyDatasetView)
        self.statusOrderGroup.triggered.connect(self._statusOrderChanged)
        self.savedList.currentItemChanged.connect(self._saved_changed)
        self.savedList.sigWorkspaceToggleRequested.connect(self.sigReconstructionWorkspaceToggleRequested)
        self.savedList.sigDiscardRequested.connect(self.sigDiscardReconstructionRequested)
        self.savedList.customContextMenuRequested.connect(self._reconstruction_context_menu)
        self.reconHandle.sigExpandedChanged.connect(self._recon_header_toggled)
        self.sessionSplitter.splitterMoved.connect(self._recon_splitter_moved)
        self.datasetList.itemSelectionChanged.connect(self._selection_changed)
        self.processNextAction.triggered.connect(
            lambda _checked=False: self.sigProcessSelectedRequested.emit("next")
        )
        self.processEndAction.triggered.connect(
            lambda _checked=False: self.sigProcessSelectedRequested.emit("end")
        )
        self.autoProcessCheck.toggled.connect(self.sigAutoProcessingChanged)
        self.pauseResumeBtn.clicked.connect(self._pause_resume_clicked)
        self.skipCurrentBtn.clicked.connect(self.sigSkipCurrentProcessingRequested)
        self.queueToggleBtn.toggled.connect(self._queue_toggled)

        self.setSessionActive(False)
        self.reconHandle.toggle.setEnabled(False)
        QtCore.QTimer.singleShot(0, lambda: self._apply_recon_expanded(False, remember=False))

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

    def requestSessionCloseChoice(self, reconstruction_count: int, *, switching: bool = False) -> str:
        count = max(0, int(reconstruction_count or 0))
        if count <= 0:
            return "keep"

        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Switch Session" if switching else "Close Session")
        box.setIcon(QtWidgets.QMessageBox.Question)
        action = "switch Sessions" if switching else "close this Session"
        noun = "object" if count == 1 else "objects"
        box.setText(
            f"{count} result {noun} from the current Session "
            f"{'are' if count != 1 else 'is'} still open in the Reconstruction panel."
        )
        box.setInformativeText(
            f"You can keep {'them' if count != 1 else 'it'} open as independent result "
            f"{'objects' if count != 1 else 'object'} when you {action}, or close "
            f"{'them' if count != 1 else 'it'} first."
        )
        keep_btn = box.addButton("Keep open", QtWidgets.QMessageBox.AcceptRole)
        close_btn = box.addButton("Close them" if count != 1 else "Close it", QtWidgets.QMessageBox.ActionRole)
        cancel_btn = box.addButton(QtWidgets.QMessageBox.Cancel)
        box.setDefaultButton(keep_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is close_btn:
            return "close"
        if clicked is keep_btn:
            return "keep"
        if clicked is cancel_btn or clicked is None:
            return "cancel"
        return "cancel"

    def requestNonFavoriteCleanupChoice(
        self, dataset_count: int, reconstruction_count: int
    ) -> str:
        dataset_count = max(0, int(dataset_count or 0))
        reconstruction_count = max(0, int(reconstruction_count or 0))
        if dataset_count <= 0:
            return "cancel"
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Clean up non-favorites")
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setText(
            f"Clean up {dataset_count} non-favorite dataset"
            f"{'s' if dataset_count != 1 else ''}?"
        )
        box.setInformativeText(
            f"{reconstruction_count} associated result"
            f"{'s' if reconstruction_count != 1 else ''} can be moved to Session trash. "
            "Favorite datasets will not be touched."
        )
        recons = box.addButton("Results only", QtWidgets.QMessageBox.ActionRole)
        raw = box.addButton("Raw data + results", QtWidgets.QMessageBox.DestructiveRole)
        cancel = box.addButton(QtWidgets.QMessageBox.Cancel)
        box.setDefaultButton(recons)
        box.exec()
        clicked = box.clickedButton()
        if clicked is recons:
            return "reconstructions"
        if clicked is raw:
            return "raw"
        if clicked is cancel or clicked is None:
            return "cancel"
        return "cancel"

    def requestRawCleanupConfirmation(
        self, *, dataset_count: int, raw_file_count: int, skipped_count: int = 0
    ) -> bool:
        dataset_count = max(0, int(dataset_count or 0))
        raw_file_count = max(0, int(raw_file_count or 0))
        skipped_count = max(0, int(skipped_count or 0))
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Move non-favorite raw data to Trash?")
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setText(
            f"Move raw acquisition data for {dataset_count} non-favorite dataset"
            f"{'s' if dataset_count != 1 else ''} to the operating-system Trash?"
        )
        details = (
            f"This affects {raw_file_count} physical raw file"
            f"{'s' if raw_file_count != 1 else ''}. Saved results from non-favorites "
            "will be moved to Session trash. Favorite datasets will not be touched."
        )
        if skipped_count:
            details += (
                f" {skipped_count} non-favorite dataset"
                f"{'s' if skipped_count != 1 else ''} share raw files with kept datasets and "
                "will be left on disk."
            )
        box.setInformativeText(details)
        remove = box.addButton("Move non-favorites to Trash", QtWidgets.QMessageBox.DestructiveRole)
        cancel = box.addButton(QtWidgets.QMessageBox.Cancel)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is remove

    def requestDeleteSelectedResultsConfirmation(self, result_count: int) -> bool:
        count = max(0, int(result_count or 0))
        if count <= 0:
            return False
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Delete Session results?")
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setText(
            f"Move {count} result{'s' if count != 1 else ''} to Session trash?"
        )
        box.setInformativeText(
            "Loaded result objects will be closed and saved Session results will be "
            "moved to Session trash. Raw data is not changed."
        )
        remove = box.addButton("Delete results", QtWidgets.QMessageBox.DestructiveRole)
        cancel = box.addButton(QtWidgets.QMessageBox.Cancel)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is remove

    def requestSessionFinishChoice(
        self, *, favorite_count: int, nonfavorite_count: int
    ) -> str:
        favorite_count = max(0, int(favorite_count or 0))
        nonfavorite_count = max(0, int(nonfavorite_count or 0))
        if nonfavorite_count <= 0:
            return "none"
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Close Session")
        box.setIcon(QtWidgets.QMessageBox.Question)
        box.setText(
            f"{favorite_count} of {favorite_count + nonfavorite_count} dataset"
            f"{'s' if favorite_count + nonfavorite_count != 1 else ''} "
            f"{'are' if favorite_count != 1 else 'is'} marked as favorite."
        )
        box.setInformativeText(
            "You can close without changing files, remove results from non-favorites, "
            "or keep only favorite acquisitions. Raw-data cleanup always asks for a second confirmation."
        )
        plain = box.addButton("Close without cleaning up", QtWidgets.QMessageBox.AcceptRole)
        recons = box.addButton(
            "Clean up non-favorite results", QtWidgets.QMessageBox.ActionRole
        )
        raw = box.addButton("Keep favorite datasets only", QtWidgets.QMessageBox.DestructiveRole)
        cancel = box.addButton(QtWidgets.QMessageBox.Cancel)

        plain.setToolTip(
            "Close the Session without moving or deleting raw data or saved results."
        )
        recons.setToolTip(
            "Remove result objects from non-favorite datasets and move their saved "
            "results to Session trash. Raw data is kept."
        )
        raw.setToolTip(
            "Keep favorite datasets only. Non-favorite results are cleaned up and "
            "their raw data can be moved to the operating-system Trash after confirmation."
        )

        # QMessageBox's standard button box may compress long custom labels. Keep each
        # action at its natural text width and enlarge the dialog enough for one clear row.
        choice_buttons = (plain, recons, raw)
        for button in choice_buttons:
            button.setMinimumWidth(button.sizeHint().width() + 16)
        required_button_width = sum(button.minimumWidth() for button in choice_buttons)
        required_button_width += cancel.sizeHint().width() + 64
        box.setMinimumWidth(max(box.sizeHint().width(), required_button_width))

        box.setDefaultButton(plain)
        box.exec()
        clicked = box.clickedButton()
        if clicked is plain:
            return "none"
        if clicked is recons:
            return "reconstructions"
        if clicked is raw:
            return "raw"
        if clicked is cancel or clicked is None:
            return "cancel"
        return "cancel"

    def _mode_changed(self, checked: bool) -> None:
        if self._sessionActive:
            self.sigSessionModeChanged.emit("follow" if checked else "review")

    def setSessionMode(self, mode: str) -> None:
        """Reflect the runtime watching state without re-emitting its signal."""
        mode = str(mode or "review").lower()
        self._sessionMode = mode
        self.watchToggle.blockSignals(True)
        try:
            self.watchToggle.setChecked(mode == "follow")
        finally:
            self.watchToggle.blockSignals(False)
        # Do not show a green status merely because Follow was requested:
        # the controller reports success after the observer actually starts.
        self.setWatchingActive(False)
        self.rescanFolderAction.setText("Refresh folder")
        self.refreshBtn.setVisible(self._sessionActive and mode != "follow")

    def setWatchingActive(self, running: bool) -> None:
        """Green status means the directory observer is actually running."""
        active = bool(running and self._sessionMode == "follow")
        self.watchStatusDot.setStyleSheet(
            "font-size: 9px; color: #27865d;" if active
            else "font-size: 9px; color: palette(mid);"
        )
        # Keep the label fixed; the status dot and switch communicate state.
        self.watchToggle.setToolTip(
            "Stop monitoring the folder (queued jobs continue)" if active
            else "Start monitoring for new recordings"
        )

    def setSessionActive(self, active: bool, name: str = "") -> None:
        self._sessionActive = bool(active)
        self._statusGeneration += 1
        self.statusLabel.clear()
        self.statusLabel.setMinimumWidth(0)
        self.statusLabel.hide()
        self.sigSessionActiveChanged.emit(self._sessionActive)
        self.sessionTitleLabel.setText("SESSION:" if active else "SESSION")
        self.sessionNameLabel.setText(str(name or ""))
        self.sessionNameLabel.setVisible(bool(active))
        self.startSessionBtn.setVisible(not active)
        self.refreshBtn.setVisible(bool(active and self._sessionMode == "review"))
        self.watchToggle.setEnabled(bool(active))
        for widget in (self.watchingControls, self.actionsBtn, self.settingsBtn):
            widget.setVisible(bool(active))
        self.datasetList.setEnabled(bool(active))
        self.datasetSearchEdit.setEnabled(bool(active))
        self.datasetOrderBtn.setEnabled(bool(active))
        self.dimensionFilterCombo.setEnabled(bool(active))
        self.favoriteOnlyBtn.setEnabled(bool(active))
        self.datasetShowCombo.setEnabled(bool(active))
        self.statusOrderBtn.setEnabled(bool(active))
        has_selection = bool(self.selectedDatasetIds())
        self.processSelectedBtn.setEnabled(
            bool(active and self._processingAvailable and has_selection)
        )
        self.addWorkingSetBtn.setEnabled(bool(active and has_selection))
        self.autoProcessCheck.setEnabled(bool(active and self._autoProcessingAvailable))
        self._update_selection_summary()
        if not active:
            self.pauseResumeBtn.setEnabled(False)
            self.skipCurrentBtn.setEnabled(False)

    def setAddingProgress(self, active: bool, current: int = 0, total: int = 0) -> None:
        active = bool(active)
        total = max(0, int(total or 0))
        current = max(0, min(int(current or 0), total)) if total else 0
        self.addingRow.setVisible(active)
        for widget in (self.actionsBtn, self.settingsBtn):
            widget.setEnabled(bool(self._sessionActive and not active))
        self.rescanFolderAction.setEnabled(bool(self._sessionActive and not active))
        if active:
            self.addingLabel.setText("Adding results…")
            self.addingProgress.setRange(0, max(1, total))
            self.addingProgress.setValue(current)
            self.addingProgress.setFormat(f"{current} / {total}")
            self.cancelAddingBtn.setEnabled(True)

    def setStatus(self, text: str) -> None:
        """Surface only important Session notices in the compact header.

        Routine progress/status is already visible in dedicated controls and
        should not permanently consume vertical space. Errors and warnings are
        shown transiently beside SESSION; routine informational chatter stays
        out of the layout.
        """
        text = str(text or "").strip()
        lowered = text.lower()
        error = any(token in lowered for token in (
            "could not", "failed", "error", "does not exist"
        ))
        warning = (
            lowered.startswith("no ")
            or lowered.startswith("already ")
            or lowered.startswith("cancelling")
        )
        main_info = lowered.startswith("session closed")
        if not (error or warning or main_info):
            return
        self._statusGeneration += 1
        generation = self._statusGeneration
        self.statusLabel.setStyleSheet(
            "color: #d34a4a;" if error else "color: #d08b28;"
        )
        self.statusLabel.setMinimumWidth(48)
        self.statusLabel.setText(text)
        self.statusLabel.show()
        QtCore.QTimer.singleShot(
            7000, lambda: self._clearHeaderStatus(generation)
        )

    def _clearHeaderStatus(self, generation: int) -> None:
        if int(generation) != self._statusGeneration:
            return
        self.statusLabel.clear()
        self.statusLabel.setMinimumWidth(0)
        self.statusLabel.hide()

    def _dataset_changed(self, current, _previous):
        value = current.data(0, QtCore.Qt.UserRole) if current is not None else None
        self.sigDatasetSelectionChanged.emit(value)

    def _dataset_clicked(self, item, column):
        if item is None or int(column) != 0:
            return
        dataset_id = item.data(0, QtCore.Qt.UserRole)
        entry = item.data(0, QtCore.Qt.UserRole + 1) or {}
        if dataset_id:
            self.sigFavoriteDatasetRequested.emit(
                str(dataset_id), not bool(entry.get("favorite", False))
            )

    def _dataset_double_clicked(self, item, column):
        if (
            item is None
            or int(column) == 0
            or bool(item.data(0, QtCore.Qt.UserRole + 2))
        ):
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
        selected_entries = [item.data(0, QtCore.Qt.UserRole + 1) or {} for item in selected_items]
        if (
            len(selected_items) == 1
            and not bool(selected_items[0].data(0, QtCore.Qt.UserRole + 2))
        ):
            open_viewer = menu.addAction("Open in viewer")
            open_viewer.triggered.connect(self.sigOpenViewerRequested)
            menu.addSeparator()
        if any(not bool(entry.get("favorite", False)) for entry in selected_entries):
            favorite = menu.addAction(
                "Favorite selected" if len(selected_items) > 1 else "Favorite"
            )
            favorite.triggered.connect(
                lambda _checked=False: self.sigSetSelectedFavoriteRequested.emit(True)
            )
        if any(bool(entry.get("favorite", False)) for entry in selected_entries):
            unfavorite = menu.addAction(
                "Remove favorite from selected" if len(selected_items) > 1 else "Remove favorite"
            )
            unfavorite.triggered.connect(
                lambda _checked=False: self.sigSetSelectedFavoriteRequested.emit(False)
            )
        if selected_entries:
            reconstruct_next = menu.addAction(
                "Reconstruct selected next" if len(selected_items) > 1 else "Reconstruct next"
            )
            reconstruct_next.triggered.connect(
                lambda _checked=False: self.sigProcessSelectedRequested.emit("next")
            )
            reconstruct_end = menu.addAction("Add to end of reconstruction queue")
            reconstruct_end.triggered.connect(
                lambda _checked=False: self.sigProcessSelectedRequested.emit("end")
            )
            menu.addSeparator()

        saved_count = sum(
            int((item.data(0, QtCore.Qt.UserRole + 1) or {}).get("saved_count", 0))
            for item in selected_items
        )
        panel_count = sum(
            int((item.data(0, QtCore.Qt.UserRole + 1) or {}).get("panel_count", 0))
            for item in selected_items
        )
        result_count = sum(
            int((item.data(0, QtCore.Qt.UserRole + 1) or {}).get("result_count", 0))
            for item in selected_items
        )
        if saved_count:
            add_saved = menu.addAction(
                f"Add results ({saved_count})"
            )
            add_saved.triggered.connect(
                lambda _checked=False: self.sigAddSelectedReconstructionsRequested.emit()
            )
            menu.addSeparator()

        if panel_count:
            close = menu.addAction(
                f"Close result{'s' if panel_count != 1 else ''} ({panel_count})"
            )
            close.triggered.connect(self.sigCloseSelectedDatasetReconstructionsRequested)
        if saved_count or panel_count:
            delete_results = menu.addAction(
                f"Delete result{'s' if result_count != 1 else ''}…"
            )
            delete_results.triggered.connect(
                self.sigDeleteSelectedDatasetReconstructionsRequested
            )
            menu.addSeparator()

        if selected:
            action = menu.addAction(
                "Add selected to Multi-data" if len(selected) > 1 else "Add to Multi-data"
            )
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

    def _selection_changed(self) -> None:
        self._update_selection_summary()

    def _update_selection_summary(self) -> None:
        count = len(self.selectedDatasetIds())
        if count <= 0:
            text = "No dataset selected"
        elif count == 1:
            text = "1 dataset selected"
        else:
            text = f"{count} datasets selected"
        self.selectionSummaryLabel.setText(text)
        self.sigSelectionCountChanged.emit(count)
        self.processSelectedBtn.setText(
            "Reconstruct ▾" if count <= 1 else f"Reconstruct {count} ▾"
        )
        self.processSelectedBtn.setEnabled(
            bool(self._sessionActive and self._processingAvailable and count)
        )
        self.addWorkingSetBtn.setEnabled(bool(self._sessionActive and count))

    def _pause_resume_clicked(self) -> None:
        if bool(self.pauseResumeBtn.property("queuePaused")):
            self.sigResumeProcessingRequested.emit()
        else:
            self.sigPauseProcessingRequested.emit()

    def _queue_toggled(self, expanded: bool) -> None:
        self.queueList.setVisible(bool(expanded))
        self.queueToggleBtn.setText("Queue ⌄" if expanded else "Queue ▸")
        if expanded:
            self._populateQueueList(self._lastQueueSnapshot)

    def setProcessingAvailable(self, available: bool) -> None:
        available = bool(available)
        self._processingAvailable = available
        # This is a compatibility-only control. Never make it visible: a visible
        # legacy button would duplicate the DATA footer and, if ever detached,
        # could become a top-level floating window.
        self._selectionCompatibilityBox.hide()
        self.processSelectedBtn.setEnabled(
            bool(available and self._sessionActive and self.selectedDatasetIds())
        )

    def setAutoProcessingAvailable(self, available: bool, tooltip: str = "") -> None:
        self._autoProcessingAvailable = bool(available)
        self.autoProcessCheck.setEnabled(bool(available and self._sessionActive))
        self.autoProcessCheck.setToolTip(str(tooltip or (
            "Automatically reconstruct new recordings while Watching"
        )))

    def _populateQueueList(self, snapshot: dict | None) -> None:
        snapshot = dict(snapshot or {})
        current = snapshot.get("current")
        waiting = list(snapshot.get("waiting", ()) or ())
        waiting_count = int(snapshot.get("waiting_count", len(waiting)) or 0)
        self.queueList.clear()
        jobs = ([current] if current is not None else []) + waiting
        for job in jobs:
            state = str(getattr(getattr(job, "state", None), "value", getattr(job, "state", "")))
            origin = str(getattr(getattr(job, "origin", None), "value", getattr(job, "origin", "")))
            pending = str(getattr(job, "readiness_status", "") or "")
            item = QtWidgets.QTreeWidgetItem([
                str(getattr(job, "label", "")),
                "WAITING FOR ACQUISITION" if state == "waiting" and pending else state.upper(),
                origin.upper(),
            ])
            if pending:
                item.setToolTip(1, pending)
            self.queueList.addTopLevelItem(item)
        hidden = max(0, waiting_count - len(waiting))
        if hidden:
            self.queueList.addTopLevelItem(
                QtWidgets.QTreeWidgetItem([f"… {hidden} more", "", ""])
            )
        # Resizing columns is relatively expensive for long queues; only do it
        # when the details are actually visible.
        for column in range(3):
            self.queueList.resizeColumnToContents(column)

    def setProcessingQueue(self, snapshot: dict | None) -> None:
        snapshot = dict(snapshot or {})
        self._lastQueueSnapshot = snapshot
        paused = bool(snapshot.get("paused", False))
        auto_enabled = bool(snapshot.get("auto_enabled", False))
        current = snapshot.get("current")
        waiting = list(snapshot.get("waiting", ()) or ())
        waiting_count = int(snapshot.get("waiting_count", len(waiting)) or 0)

        self.autoProcessCheck.blockSignals(True)
        self.autoProcessCheck.setChecked(auto_enabled)
        self.autoProcessCheck.blockSignals(False)
        self.pauseResumeBtn.setProperty("queuePaused", paused)
        self.pauseResumeBtn.setText("Resume" if paused else "Pause")
        self.pauseResumeBtn.setEnabled(
            bool(self._sessionActive and (current or waiting_count or paused))
        )
        self.skipCurrentBtn.setEnabled(bool(self._sessionActive and current))

        if current is not None:
            label = str(getattr(current, "label", "processing"))
            summary = f"▶ {label} · reconstructing"
            if waiting_count:
                summary += f" · {waiting_count} waiting"
        elif waiting_count:
            summary = f"{waiting_count} waiting"
            if paused:
                summary += " · paused"
        else:
            summary = "Queue paused" if paused else "Queue idle"
        self.queueSummaryLabel.setText(summary)

        if not self.queueList.isHidden():
            self._populateQueueList(snapshot)

    def selectedDatasetIds(self) -> list[str]:
        ids = []
        for item in self.datasetList.selectedItems():
            value = item.data(0, QtCore.Qt.UserRole)
            if value:
                ids.append(str(value))
        return list(dict.fromkeys(ids))

    def selectedTimeLapseDescriptor(self) -> dict | None:
        items = self.datasetList.selectedItems()
        if len(items) != 1:
            return None
        entry = items[0].data(0, QtCore.Qt.UserRole + 1) or {}
        if str(entry.get("kind") or "") != "time-lapse":
            return None
        identity = entry.get("time_lapse_identity")
        total = int(entry.get("planned_timepoints") or 0)
        if not identity or total <= 0:
            return None
        return {"identity": tuple(identity), "total": total}

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

    def _preferred_recon_pane_height(self) -> int:
        return int(self.savedList.preferredHeight())

    def _set_splitter_recon_height(self, height: int) -> None:
        sizes = list(self.sessionSplitter.sizes())
        total = sum(sizes)
        if total <= 0:
            total = max(1, int(self.sessionSplitter.height()))
        target = max(0, int(height))
        top_min = max(1, int(self.sessionSplitter.widget(0).minimumSizeHint().height()))
        if total > top_min:
            target = min(target, max(0, total - top_min))
        self._programmaticSplitterMove = True
        try:
            self.sessionSplitter.setSizes([max(1, total - target), target])
        finally:
            self._programmaticSplitterMove = False

    def _apply_recon_expanded(self, expanded: bool, *, remember: bool) -> None:
        # Expansion is a user preference, not a consequence of result count.
        # Keep an empty Recon (0) pane open when the user left it open so the
        # Session layout does not jump while navigating raw datasets.
        expanded = bool(expanded)
        if remember:
            self._reconUserExpanded = expanded
        self.reconHandle.setExpanded(expanded)

        if expanded:
            self.savedList.setVisible(True)
            target = self._reconLastExpandedHeight or self._preferred_recon_pane_height()
            QtCore.QTimer.singleShot(0, lambda h=target: self._set_splitter_recon_height(h))
        else:
            sizes = self.sessionSplitter.sizes()
            if self.savedList.isVisible() and len(sizes) > 1 and int(sizes[1]) > 0:
                self._reconLastExpandedHeight = int(sizes[1])
            self.savedList.setVisible(False)
            QtCore.QTimer.singleShot(0, lambda: self._set_splitter_recon_height(0))

    def _recon_header_toggled(self, expanded: bool) -> None:
        self._apply_recon_expanded(bool(expanded), remember=True)

    def _recon_splitter_moved(self, _pos: int, _index: int) -> None:
        if self._programmaticSplitterMove or not self.savedList.isVisible():
            return
        sizes = self.sessionSplitter.sizes()
        if len(sizes) > 1 and sizes[1] > 0:
            self._reconLastExpandedHeight = int(sizes[1])

    def setReconstructionsExpanded(self, expanded: bool = True) -> None:
        self._apply_recon_expanded(bool(expanded), remember=True)

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
            return state == "reconstructed"
        if mode == "raw":
            return state != "reconstructed"
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
            return 0 if state != "reconstructed" else 1
        if mode == "reconstructed":
            return 0 if state == "reconstructed" else 1
        return 0

    def _dataset_colors(self):
        palette = self.palette()
        normal = palette.color(QtGui.QPalette.Text)
        try:
            muted = palette.color(QtGui.QPalette.PlaceholderText)
        except AttributeError:
            muted = palette.color(QtGui.QPalette.Mid)
        dark = palette.color(QtGui.QPalette.Base).lightness() < 128
        reconstructed = QtGui.QColor("#72c98c" if dark else "#2e7d32")
        active = palette.color(QtGui.QPalette.Highlight)
        return normal, muted, reconstructed, active

    def _styleDatasetItem(self, item, state: str) -> None:
        state = str(state or "raw").lower()
        normal, muted, reconstructed, active = self._dataset_colors()
        name_font = item.font(1)
        status_font = item.font(3)
        name_font.setBold(False)
        status_font.setBold(True)
        item.setForeground(0, normal)
        if state == "reconstructed":
            item.setForeground(1, muted)
            item.setForeground(3, reconstructed)
        elif state in {"running", "processing", "queued", "loading"}:
            name_font.setBold(True)
            item.setForeground(1, normal)
            item.setForeground(3, active)
        elif state == "failed":
            name_font.setBold(True)
            item.setForeground(1, normal)
            item.setForeground(3, normal)
        else:
            item.setForeground(1, normal)
            item.setForeground(3, normal)
        item.setForeground(2, muted)
        link_font = item.font(4)
        if item.text(4):
            item.setForeground(4, active)
            link_font.setBold(True)
        else:
            item.setForeground(4, muted)
            link_font.setBold(False)
        item.setFont(4, link_font)
        item.setFont(1, name_font)
        item.setFont(3, status_font)

    @staticmethod
    def _dimension_sort_key(tag: str):
        tag = str(tag or "").upper()
        timed = tag.endswith("T")
        spatial = tag[:-1] if timed else tag
        order = {
            "X": 0, "Y": 1, "Z": 2,
            "XY": 3, "XZ": 4, "YZ": 5,
            "XYZ": 6, "": 99,
        }
        return (order.get(spatial, 98), 1 if timed else 0, tag)

    def _refreshDimensionFilter(self) -> None:
        current = str(self.dimensionFilterCombo.currentData() or "all")
        dimension_labels = {
            str(entry.get("dimension") or ""): str(
                entry.get("dimension_label") or entry.get("dimension") or ""
            )
            for entry in self._datasetEntries
            if str(entry.get("dimension") or "")
        }
        dimensions = sorted(dimension_labels, key=self._dimension_sort_key)
        self.dimensionFilterCombo.blockSignals(True)
        try:
            self.dimensionFilterCombo.clear()
            self.dimensionFilterCombo.addItem("All", "all")
            for tag in dimensions:
                self.dimensionFilterCombo.addItem(dimension_labels[tag], tag)
            index = self.dimensionFilterCombo.findData(current)
            self.dimensionFilterCombo.setCurrentIndex(index if index >= 0 else 0)
        finally:
            self.dimensionFilterCombo.blockSignals(False)

    def _applyDatasetView(self, *_args) -> None:
        selected_ids = set(self.selectedDatasetIds())
        current_id = self.currentDatasetId()
        query = self.datasetSearchEdit.text().strip().casefold()
        dimension = str(self.dimensionFilterCombo.currentData() or "all")
        show_mode = str(self.datasetShowCombo.currentData() or "all")
        order_mode = str(self._datasetOrderMode or "latest")
        status_order_mode = str(self._statusOrderMode or "none")
        favorites_only = bool(self.favoriteOnlyBtn.isChecked())
        entries = [
            dict(entry) for entry in self._datasetEntries
            if (not query or query in str(entry.get("search", "")).casefold())
            and (dimension == "all" or str(entry.get("dimension") or "") == dimension)
            and (not favorites_only or bool(entry.get("favorite", False)))
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
            favorite = bool(entry.get("favorite", False))
            item = QtWidgets.QTreeWidgetItem([
                "★" if favorite else "☆",
                label,
                str(entry.get("info", "")),
                str(entry.get("status", "")),
                "●" if entry.get("panel_link") else "",
            ])
            item.setData(0, QtCore.Qt.UserRole, dataset_id)
            item.setData(0, QtCore.Qt.UserRole + 1, dict(entry))
            item.setData(0, QtCore.Qt.UserRole + 2, bool(entry.get("raw_deleted", False)))
            item.setToolTip(0, "Remove from favorites" if favorite else "Mark this acquisition as favorite")
            item.setToolTip(1, str(entry.get("tooltip") or entry.get("search", label)))
            item.setToolTip(2, str(entry.get("info_tooltip") or entry.get("info", "")))
            item.setToolTip(3, str(entry.get("status", "")))
            item.setTextAlignment(0, QtCore.Qt.AlignCenter)
            panel_count = int(entry.get("panel_count", 0) or 0)
            if panel_count:
                item.setToolTip(
                    4,
                    f"{panel_count} result object{'s' if panel_count != 1 else ''} "
                    "from this dataset are in the Results panel.",
                )
                item.setTextAlignment(4, QtCore.Qt.AlignCenter)
            self._styleDatasetItem(item, entry.get("state", "raw"))
            self.datasetList.addTopLevelItem(item)
            if dataset_id in selected_ids:
                item.setSelected(True)
            if dataset_id == current_id:
                restore_current = item
        # Restoring the current row after a catalog rebuild is presentation
        # state, not a new user navigation intent. Keep signals blocked for that
        # operation so it cannot select a reconstruction on the right.
        if restore_current is not None:
            self.datasetList.setCurrentItem(restore_current)
            self.datasetList.blockSignals(False)
        else:
            self.datasetList.blockSignals(False)
            if self.datasetList.topLevelItemCount() and self.datasetList.currentItem() is None:
                # Initial selection is intentional and should update controller
                # context exactly like a user click.
                self.datasetList.setCurrentItem(self.datasetList.topLevelItem(0))
            elif not self.datasetList.topLevelItemCount():
                self.sigDatasetSelectionChanged.emit(None)
        self.datasetList.fitAuxiliaryColumns()
        self._update_selection_summary()

    def _updateDatasetItem(self, item, entry: dict) -> None:
        dataset_id = entry.get("id")
        label = str(entry.get("label", ""))
        if entry.get("current"):
            label = f"▶ {label}"
        favorite = bool(entry.get("favorite", False))
        item.setText(0, "★" if favorite else "☆")
        item.setText(1, label)
        item.setText(2, str(entry.get("info", "")))
        item.setText(3, str(entry.get("status", "")))
        item.setText(4, "●" if entry.get("panel_link") else "")
        item.setData(0, QtCore.Qt.UserRole, dataset_id)
        item.setData(0, QtCore.Qt.UserRole + 1, dict(entry))
        item.setData(0, QtCore.Qt.UserRole + 2, bool(entry.get("raw_deleted", False)))
        item.setToolTip(0, "Remove from favorites" if favorite else "Mark this acquisition as favorite")
        item.setToolTip(1, str(entry.get("tooltip") or entry.get("search", label)))
        item.setToolTip(2, str(entry.get("info_tooltip") or entry.get("info", "")))
        item.setToolTip(3, str(entry.get("status", "")))
        item.setTextAlignment(0, QtCore.Qt.AlignCenter)
        panel_count = int(entry.get("panel_count", 0) or 0)
        if panel_count:
            item.setToolTip(
                4,
                f"{panel_count} result object{'s' if panel_count != 1 else ''} "
                "from this dataset are in the Results panel.",
            )
            item.setTextAlignment(4, QtCore.Qt.AlignCenter)
        else:
            item.setToolTip(4, "")
        self._styleDatasetItem(item, entry.get("state", "raw"))

    def updateDataset(self, entry: dict) -> None:
        """Update one Session row without rebuilding the complete dataset tree."""
        entry = dict(entry)
        dataset_id = str(entry.get("id") or "")
        if not dataset_id:
            return
        replaced = False
        for index, existing in enumerate(self._datasetEntries):
            if str(existing.get("id") or "") == dataset_id:
                self._datasetEntries[index] = entry
                replaced = True
                break
        if not replaced:
            self._datasetEntries.append(entry)
            self._refreshDimensionFilter()
            self._applyDatasetView()
            return

        # State-filtering or state-ordering can change row membership/position,
        # so those explicit views still rebuild. The normal Session view updates
        # the affected QTreeWidgetItem in place.
        if (
            str(self.datasetShowCombo.currentData() or "all") != "all"
            or str(self._statusOrderMode or "none") != "none"
        ):
            self._applyDatasetView()
            return

        for index in range(self.datasetList.topLevelItemCount()):
            item = self.datasetList.topLevelItem(index)
            if str(item.data(0, QtCore.Qt.UserRole) or "") == dataset_id:
                self._updateDatasetItem(item, entry)
                break
        self.datasetList.fitAuxiliaryColumns()
        self._update_selection_summary()

    def setDatasets(self, entries: list[dict]) -> None:
        self._datasetEntries = [dict(entry) for entry in entries]
        self._refreshDimensionFilter()
        self._applyDatasetView()

    def setSelectedDataset(self, title: str, _summary: str) -> None:
        self._selectedDatasetTitle = str(title or "")
        has_dataset = bool(self._selectedDatasetTitle)
        self.reconHandle.toggle.setEnabled(has_dataset)
        self.reconHandle.setVisible(has_dataset)
        self.reconPane.setVisible(has_dataset)
        if not has_dataset:
            self._apply_recon_expanded(False, remember=False)

    def setSavedReconstructions(self, entries: list[dict], selected_path: str | None = None) -> None:
        count = len(entries)
        panel_count = sum(1 for entry in entries if bool(entry.get("loaded", False)))
        self.savedList.setEntries(entries, selected_path)
        tooltip = self._selectedDatasetTitle
        if count:
            tooltip = (tooltip + "\n" if tooltip else "") + (
                f"{count} result{'s' if count != 1 else ''} associated · "
                f"{panel_count} in panel"
            )
        self.reconHandle.setSummary(count, panel_count, tooltip=tooltip)
        has_dataset = bool(self._selectedDatasetTitle)
        self.reconPane.setVisible(has_dataset)
        self.reconHandle.setVisible(has_dataset)
        self.reconHandle.toggle.setEnabled(has_dataset)
        if has_dataset:
            if self._reconUserExpanded:
                self._apply_recon_expanded(True, remember=False)
            elif not self.savedList.isVisible():
                self._apply_recon_expanded(False, remember=False)
        else:
            self._apply_recon_expanded(False, remember=False)
        if selected_path is None:
            self.sigSavedSelectionChanged.emit(None)

    def selectedReconstructionKeys(self) -> list[str]:
        return self.savedList.selectedKeys()

    def clearReconstructionSelection(self) -> None:
        """Clear reconstruction-list selection without feeding a navigation signal back."""
        self.savedList.blockSignals(True)
        try:
            self.savedList.clearSelection()
        finally:
            self.savedList.blockSignals(False)

    def _reconstruction_context_menu(self, point) -> None:
        item = self.savedList.itemAt(point)
        if item is None:
            return
        if not item.isSelected():
            self.savedList.clearSelection()
            item.setSelected(True)
            self.savedList.setCurrentItem(item)
        entries = self.savedList.selectedEntries()
        count = len(entries)
        if count <= 0:
            return
        menu = QtWidgets.QMenu(self)
        if count == 1:
            entry = entries[0]
            key = str(entry.get("key") or "")
            if key:
                if bool(entry.get("loaded", False)):
                    workspace = menu.addAction("Close result")
                    workspace.triggered.connect(
                        lambda _checked=False, k=key:
                            self.sigReconstructionWorkspaceToggleRequested.emit(k)
                    )
                elif bool(entry.get("saved", False)):
                    workspace = menu.addAction("Add result")
                    workspace.triggered.connect(
                        lambda _checked=False, k=key:
                            self.sigReconstructionWorkspaceToggleRequested.emit(k)
                    )
                reuse = menu.addMenu("Reuse MoNaLISA calibration")
                for label, mode in (
                    ("Lock localization…", "localization"),
                    ("Lock scan orientation", "orientation"),
                    ("Lock both…", "both"),
                ):
                    action = reuse.addAction(label)
                    action.triggered.connect(
                        lambda _checked=False, k=key, m=mode:
                            self.sigReuseCalibrationRequested.emit(k, m)
                    )
                menu.addSeparator()
        label = "Delete result…" if count == 1 else f"Delete selected results ({count})…"
        discard = menu.addAction(label)
        discard.triggered.connect(self.sigDiscardSelectedRequested)
        menu.exec(self.savedList.viewport().mapToGlobal(point))

__all__ = ["ProcessingSessionFrame"]
