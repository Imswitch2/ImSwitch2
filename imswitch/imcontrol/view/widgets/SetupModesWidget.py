from qtpy import QtCore, QtGui, QtWidgets

from imswitch.imcontrol.view.guitools import BetterPushButton
from .basewidgets import Widget


class SetupModesWidget(Widget):
    """Compact widget for saving and applying named setup modes."""

    _transparentToolButtonStyle = """
        QToolButton {
            border: none;
            background: transparent;
        }
        QToolButton:hover {
            background: transparent;
        }
        QToolButton:pressed {
            background: transparent;
        }
    """

    inspectDialogFontPointSize = 10

    sigUpdateMode = QtCore.Signal()
    sigSaveAsMode = QtCore.Signal()
    sigModeSelected = QtCore.Signal(str)
    sigReloadMode = QtCore.Signal()
    sigInspectModes = QtCore.Signal()
    sigManagerLoadMode = QtCore.Signal(str)
    sigManagerComponentsMode = QtCore.Signal(str)
    sigManagerEditDescription = QtCore.Signal(str)
    sigManagerRenameMode = QtCore.Signal(str)
    sigManagerDuplicateMode = QtCore.Signal(str)
    sigManagerSetShortcut = QtCore.Signal(str)
    sigManagerDeleteMode = QtCore.Signal(str)
    sigRenameMode = QtCore.Signal()
    sigDuplicateMode = QtCore.Signal()
    sigSetShortcut = QtCore.Signal()
    sigSafetySettings = QtCore.Signal()
    sigDeleteMode = QtCore.Signal()
    sigRevealFolder = QtCore.Signal()
    sigSmartMicroscopyRoles = QtCore.Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._modeSummaries = {}
        self._backendAvailable = False
        self._setupModeManagerDialog = None
        self._setupModeManagerWidgets = {}
        self._setupModeManagerDetails = {}
        self._setupModeManagerActiveName = None

        self.setMinimumWidth(260)

        mainLayout = QtWidgets.QVBoxLayout(self)
        mainLayout.setContentsMargins(6, 6, 6, 6)
        mainLayout.setSpacing(6)

        title = QtWidgets.QLabel("General Setup Mode")
        title.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        font = title.font()
        font.setBold(True)
        font.setPointSize(font.pointSize() + 4)
        title.setFont(font)
        title.setContentsMargins(0, 6, 0, 2)

        self.modeWarningButton = QtWidgets.QToolButton()
        self.modeWarningButton.setIcon(
            self.style().standardIcon(QtWidgets.QStyle.SP_MessageBoxWarning)
        )
        self.modeWarningButton.setStyleSheet(self._transparentToolButtonStyle)
        self.modeWarningButton.setAutoRaise(True)
        self.modeWarningButton.setFixedSize(20, 20)
        self.modeWarningButton.setToolTip(
            "Selecting a mode immediately applies saved hardware settings."
        )

        titleLayout = QtWidgets.QHBoxLayout()
        titleLayout.setContentsMargins(0, 0, 0, 0)
        titleLayout.addWidget(title)
        titleLayout.addWidget(self.modeWarningButton)
        titleLayout.addStretch(1)

        separator = QtWidgets.QFrame()
        separator.setFrameShape(QtWidgets.QFrame.HLine)
        separator.setFrameShadow(QtWidgets.QFrame.Sunken)
        separator.setMaximumHeight(12)

        controlsWidget = QtWidgets.QWidget()
        layout = QtWidgets.QGridLayout(controlsWidget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(4)
        layout.setVerticalSpacing(0)

        modeLabel = QtWidgets.QLabel("Mode:")
        self.modeCombo = QtWidgets.QComboBox()
        self.modeCombo.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Fixed
        )
        self.modeCombo.view().setMouseTracking(True)
        self.modeCombo.wheelEvent = lambda event: None

        self.infoButton = QtWidgets.QToolButton()
        self.infoButton.setIcon(
            self.style().standardIcon(QtWidgets.QStyle.SP_MessageBoxInformation)
        )
        self.infoButton.setAutoRaise(True)
        self.infoButton.setFixedSize(18, 18)
        self.infoButton.setToolTip("Open setup mode manager")
        self.infoButton.setStyleSheet(self._transparentToolButtonStyle)
        self.reloadButton = QtWidgets.QToolButton()
        self.reloadButton.setIcon(
            self.style().standardIcon(
                getattr(QtWidgets.QStyle, "SP_BrowserReload", QtWidgets.QStyle.SP_ArrowRight)
            )
        )
        self.reloadButton.setAutoRaise(True)
        self.reloadButton.setFixedSize(22, 22)
        self.reloadButton.setToolTip("Reload selected mode")
        self.reloadButton.setStyleSheet(self._transparentToolButtonStyle)
        self.updateButton = BetterPushButton("Update Mode")
        self.moreButton = QtWidgets.QToolButton()
        self.moreButton.setText("More")
        self.moreButton.setPopupMode(QtWidgets.QToolButton.InstantPopup)

        self.moreMenu = QtWidgets.QMenu(self.moreButton)
        self.saveAsAction = self.moreMenu.addAction("Save as...")
        self.moreMenu.addSeparator()
        self.renameAction = self.moreMenu.addAction("Rename mode...")
        self.duplicateAction = self.moreMenu.addAction("Duplicate mode...")
        self.shortcutAction = self.moreMenu.addAction("Set shortcut...")
        self.moreMenu.addSeparator()
        self.smartMicroscopyRolesAction = self.moreMenu.addAction("Smart microscopy roles...")
        self.safetySettingsAction = self.moreMenu.addAction("Safety settings...")
        self.revealFolderAction = self.moreMenu.addAction("Open modes folder")
        self.moreMenu.addSeparator()
        self.deleteAction = self.moreMenu.addAction("Delete mode")
        self.moreButton.setMenu(self.moreMenu)

        layout.addWidget(modeLabel, 0, 0)
        layout.addWidget(self.modeCombo, 0, 1)
        layout.addWidget(self.infoButton, 0, 2)
        layout.addWidget(self.reloadButton, 0, 3)
        layout.addWidget(self.updateButton, 0, 4)
        layout.addWidget(self.moreButton, 0, 5)
        layout.setColumnStretch(1, 1)

        mainLayout.addLayout(titleLayout)
        mainLayout.addWidget(separator)
        mainLayout.addWidget(controlsWidget)
        mainLayout.addStretch(1)

        self.modeCombo.currentIndexChanged.connect(self._onModeIndexChanged)
        self.modeCombo.activated.connect(self._onModeActivated)
        self.infoButton.clicked.connect(self.sigInspectModes)
        self.reloadButton.clicked.connect(self.sigReloadMode)
        self.updateButton.clicked.connect(self.sigUpdateMode)
        self.saveAsAction.triggered.connect(self.sigSaveAsMode)
        self.renameAction.triggered.connect(self.sigRenameMode)
        self.duplicateAction.triggered.connect(self.sigDuplicateMode)
        self.shortcutAction.triggered.connect(self.sigSetShortcut)
        self.smartMicroscopyRolesAction.triggered.connect(self.sigSmartMicroscopyRoles)
        self.safetySettingsAction.triggered.connect(self.sigSafetySettings)
        self.deleteAction.triggered.connect(self.sigDeleteMode)
        self.revealFolderAction.triggered.connect(self.sigRevealFolder)

        self.setBackendAvailable(False)
        self.setModes([])

        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Maximum)
        self.setMaximumHeight(105)

    def setBackendAvailable(self, available):
        self._backendAvailable = bool(available)
        self.saveAsAction.setEnabled(self._backendAvailable)
        self.revealFolderAction.setEnabled(self._backendAvailable)
        self._updateModeControls()

    def setModes(self, modeSummaries, selectedName=None):
        self._modeSummaries = {
            summary["name"]: summary for summary in modeSummaries
            if summary.get("name")
        }

        self.modeCombo.blockSignals(True)
        self.modeCombo.clear()

        if modeSummaries:
            self.modeCombo.addItem("", None)
            for summary in modeSummaries:
                self.modeCombo.addItem(summary.get("displayName", summary["name"]), summary["name"])
                self.modeCombo.setItemData(
                    self.modeCombo.count() - 1,
                    self._modeTooltipText(summary),
                    QtCore.Qt.ToolTipRole
                )
        else:
            self.modeCombo.addItem("No saved modes", None)

        if selectedName:
            index = self.modeCombo.findData(selectedName)
            if index >= 0:
                self.modeCombo.setCurrentIndex(index)

        self.modeCombo.blockSignals(False)
        self._updateInfoButton()
        self._updateModeControls()

    def getSelectedModeName(self):
        return self.modeCombo.currentData()

    def showSetupModeManagerDialog(self, modeDetails, selectedName=None, activeName=None):
        if self._setupModeManagerDialog is not None:
            self.updateSetupModeManager(modeDetails, selectedName, activeName)
            self._setupModeManagerDialog.raise_()
            self._setupModeManagerDialog.activateWindow()
            return

        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Setup Modes")
        dialog.resize(820, 470)
        dialogFont = self._applyInspectDialogFont(dialog)
        self._setupModeManagerDialog = dialog

        mainLayout = QtWidgets.QHBoxLayout(dialog)

        modeList = QtWidgets.QListWidget()
        modeList.setMinimumWidth(220)
        mainLayout.addWidget(modeList, 0)

        detailWidget = QtWidgets.QWidget()
        detailLayout = QtWidgets.QVBoxLayout(detailWidget)
        detailLayout.setContentsMargins(6, 0, 0, 0)

        headerRow = QtWidgets.QHBoxLayout()
        nameLabel = QtWidgets.QLabel()
        nameFont = QtGui.QFont(dialogFont)
        nameFont.setBold(True)
        nameLabel.setFont(nameFont)
        activeLabel = QtWidgets.QLabel("Active")
        activeFont = QtGui.QFont(dialogFont)
        activeFont.setBold(True)
        activeLabel.setFont(activeFont)
        activeLabel.setVisible(False)
        headerRow.addWidget(nameLabel)
        headerRow.addWidget(activeLabel)
        headerRow.addStretch(1)
        detailLayout.addLayout(headerRow)

        form = QtWidgets.QFormLayout()
        shortcutLabel = QtWidgets.QLabel()
        updatedLabel = QtWidgets.QLabel()
        createdLabel = QtWidgets.QLabel()
        componentsLabel = QtWidgets.QLabel()
        componentsLabel.setWordWrap(True)
        inactiveLabel = QtWidgets.QLabel()
        inactiveLabel.setWordWrap(True)
        form.addRow("Shortcut:", shortcutLabel)
        form.addRow("Updated:", updatedLabel)
        form.addRow("Created:", createdLabel)
        form.addRow("Uses:", componentsLabel)
        form.addRow("Saved but inactive:", inactiveLabel)
        detailLayout.addLayout(form)

        descriptionHeader = QtWidgets.QHBoxLayout()
        descriptionLabel = QtWidgets.QLabel("Description")
        descriptionFont = QtGui.QFont(dialogFont)
        descriptionFont.setBold(True)
        descriptionLabel.setFont(descriptionFont)
        editDescriptionButton = BetterPushButton("Edit…")
        descriptionHeader.addWidget(descriptionLabel)
        descriptionHeader.addStretch(1)
        descriptionHeader.addWidget(editDescriptionButton)
        descriptionEdit = QtWidgets.QPlainTextEdit()
        descriptionEdit.setReadOnly(True)
        descriptionEdit.setMaximumHeight(80)
        descriptionEdit.setPlaceholderText("No description")
        self._applyPlainTextEditFont(descriptionEdit, dialogFont)
        detailLayout.addLayout(descriptionHeader)
        detailLayout.addWidget(descriptionEdit)

        smartRolesLabel = QtWidgets.QLabel("Smart microscopy roles")
        smartRolesFont = QtGui.QFont(dialogFont)
        smartRolesFont.setBold(True)
        smartRolesLabel.setFont(smartRolesFont)
        smartRolesEdit = QtWidgets.QPlainTextEdit()
        smartRolesEdit.setReadOnly(True)
        smartRolesEdit.setMaximumHeight(55)
        smartRolesEdit.setPlaceholderText("Not used in any workflow")
        self._applyPlainTextEditFont(smartRolesEdit, dialogFont)
        detailLayout.addWidget(smartRolesLabel)
        detailLayout.addWidget(smartRolesEdit)

        savedStateLabel = QtWidgets.QLabel("Saved state summary")
        savedStateFont = QtGui.QFont(dialogFont)
        savedStateFont.setBold(True)
        savedStateLabel.setFont(savedStateFont)
        stateSummary = QtWidgets.QPlainTextEdit()
        stateSummary.setReadOnly(True)
        self._applyPlainTextEditFont(stateSummary, dialogFont)
        detailLayout.addWidget(savedStateLabel)
        detailLayout.addWidget(stateSummary, 1)

        actionRow = QtWidgets.QHBoxLayout()
        loadButton = BetterPushButton("Load")
        componentsButton = BetterPushButton("Components…")
        renameButton = BetterPushButton("Rename…")
        duplicateButton = BetterPushButton("Duplicate…")
        shortcutButton = BetterPushButton("Shortcut…")
        deleteButton = BetterPushButton("Delete")
        closeButton = BetterPushButton("Close")
        for button in (
            loadButton, componentsButton, renameButton, duplicateButton,
            shortcutButton, deleteButton
        ):
            actionRow.addWidget(button)
        actionRow.addStretch(1)
        actionRow.addWidget(closeButton)
        detailLayout.addLayout(actionRow)

        mainLayout.addWidget(detailWidget, 1)

        self._setupModeManagerWidgets = {
            "modeList": modeList,
            "nameLabel": nameLabel,
            "activeLabel": activeLabel,
            "shortcutLabel": shortcutLabel,
            "updatedLabel": updatedLabel,
            "createdLabel": createdLabel,
            "componentsLabel": componentsLabel,
            "inactiveLabel": inactiveLabel,
            "descriptionEdit": descriptionEdit,
            "editDescriptionButton": editDescriptionButton,
            "smartRolesEdit": smartRolesEdit,
            "stateSummary": stateSummary,
            "actionButtons": (
                loadButton, componentsButton, renameButton, duplicateButton,
                shortcutButton, deleteButton
            ),
        }

        modeList.currentRowChanged.connect(self._updateSetupModeManagerDetails)
        loadButton.clicked.connect(
            lambda: self._emitManagerAction(self.sigManagerLoadMode)
        )
        componentsButton.clicked.connect(
            lambda: self._emitManagerAction(self.sigManagerComponentsMode)
        )
        editDescriptionButton.clicked.connect(
            lambda: self._emitManagerAction(self.sigManagerEditDescription)
        )
        renameButton.clicked.connect(
            lambda: self._emitManagerAction(self.sigManagerRenameMode)
        )
        duplicateButton.clicked.connect(
            lambda: self._emitManagerAction(self.sigManagerDuplicateMode)
        )
        shortcutButton.clicked.connect(
            lambda: self._emitManagerAction(self.sigManagerSetShortcut)
        )
        deleteButton.clicked.connect(
            lambda: self._emitManagerAction(self.sigManagerDeleteMode)
        )
        closeButton.clicked.connect(dialog.accept)

        self.updateSetupModeManager(modeDetails, selectedName, activeName)
        try:
            self._execDialog(dialog)
        finally:
            self._setupModeManagerDialog = None
            self._setupModeManagerWidgets = {}
            self._setupModeManagerDetails = {}
            self._setupModeManagerActiveName = None

    def updateSetupModeManager(self, modeDetails, selectedName=None, activeName=None):
        if self._setupModeManagerDialog is None:
            return

        widgets = self._setupModeManagerWidgets
        modeList = widgets.get("modeList")
        if modeList is None:
            return

        currentName = self.getSetupModeManagerSelectedName()
        targetName = selectedName if selectedName is not None else currentName
        self._setupModeManagerDetails = {
            details["name"]: details for details in modeDetails if details.get("name")
        }
        self._setupModeManagerActiveName = activeName

        modeList.blockSignals(True)
        modeList.clear()
        targetRow = -1
        for row, details in enumerate(modeDetails):
            item = QtWidgets.QListWidgetItem(details.get("displayName") or details["name"])
            item.setData(QtCore.Qt.UserRole, details["name"])
            if details["name"] == activeName:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
                item.setToolTip("Currently active setup mode")
            modeList.addItem(item)
            if details["name"] == targetName:
                targetRow = row

        if targetRow < 0 and modeList.count() > 0:
            targetRow = 0
        modeList.setCurrentRow(targetRow)
        modeList.blockSignals(False)
        self._updateSetupModeManagerDetails(targetRow)

    def getSetupModeManagerSelectedName(self):
        modeList = self._setupModeManagerWidgets.get("modeList")
        if modeList is None:
            return None
        item = modeList.currentItem()
        if item is None:
            return None
        return item.data(QtCore.Qt.UserRole)

    def _emitManagerAction(self, signal):
        modeName = self.getSetupModeManagerSelectedName()
        if modeName:
            signal.emit(modeName)

    def _updateSetupModeManagerDetails(self, row):
        widgets = self._setupModeManagerWidgets
        modeList = widgets.get("modeList")
        item = modeList.item(row) if modeList is not None and row >= 0 else None
        details = (
            self._setupModeManagerDetails.get(item.data(QtCore.Qt.UserRole), {})
            if item is not None else {}
        )
        hasMode = bool(details)
        for button in widgets.get("actionButtons", ()):
            button.setEnabled(hasMode)
        widgets.get("editDescriptionButton").setEnabled(hasMode)

        widgets.get("nameLabel").setText(details.get("name", "No setup modes"))
        widgets.get("activeLabel").setVisible(
            hasMode and details.get("name") == self._setupModeManagerActiveName
        )
        widgets.get("shortcutLabel").setText(details.get("shortcut") or "None")
        widgets.get("updatedLabel").setText(details.get("updatedAt") or "Unknown")
        widgets.get("createdLabel").setText(details.get("createdAt") or "Unknown")

        included = (
            details.get("includedComponentLabels")
            or details.get("includedComponents")
            or []
        )
        inactive = (
            details.get("inactiveComponentLabels")
            or details.get("inactiveComponents")
            or []
        )
        widgets.get("componentsLabel").setText(
            ", ".join(included) if included else "None"
        )
        widgets.get("inactiveLabel").setText(
            ", ".join(inactive) if inactive else "None"
        )
        widgets.get("descriptionEdit").setPlainText(details.get("description") or "")
        widgets.get("smartRolesEdit").setPlainText(
            "\n".join(details.get("smartModeRoles") or [])
        )
        widgets.get("stateSummary").setPlainText(
            "\n".join(details.get("stateSummary") or [])
        )

    def showDescriptionDialog(self, modeName, currentDescription):
        description, accepted = QtWidgets.QInputDialog.getMultiLineText(
            self,
            f"Edit description — {modeName}",
            "Description:",
            currentDescription or "",
        )
        if not accepted:
            return None
        return description.strip()

    def showModeComponentsDialog(self, modeName, components, includedComponents):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(f'Components — {modeName}')
        layout = QtWidgets.QVBoxLayout(dialog)

        explanation = QtWidgets.QLabel(
            "Choose which saved component states are used when this mode is loaded. "
            "Unchecked components remain saved and can be re-enabled later."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        group = QtWidgets.QGroupBox("Use when loading this mode")
        groupLayout = QtWidgets.QVBoxLayout(group)
        includedSet = set(includedComponents or [])
        checks = {}
        for component in components:
            componentName = component.get("name")
            if not componentName:
                continue
            check = QtWidgets.QCheckBox(component.get("label") or componentName)
            check.setChecked(componentName in includedSet)
            checks[componentName] = check
            groupLayout.addWidget(check)
        layout.addWidget(group)

        buttonBox = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel
        )
        layout.addWidget(buttonBox)

        result = {"components": None}

        def acceptIfValid():
            selected = [
                componentName
                for componentName, check in checks.items()
                if check.isChecked()
            ]
            if not selected:
                QtWidgets.QMessageBox.warning(
                    dialog,
                    "Setup mode components",
                    "Select at least one component to use when loading this mode.",
                )
                return
            result["components"] = selected
            dialog.accept()

        buttonBox.accepted.connect(acceptIfValid)
        buttonBox.rejected.connect(dialog.reject)
        if self._execDialog(dialog) != QtWidgets.QDialog.Accepted:
            return None
        return result["components"]

    def _modeTooltipText(self, summary):
        lines = []

        description = summary.get("description")
        if description:
            lines.append(description)

        shortcut = summary.get("shortcut")
        if shortcut:
            lines.append(f"Shortcut: {shortcut}")

        components = (
            summary.get("includedComponentLabels")
            or summary.get("includedComponents")
            or []
        )
        if components:
            lines.append("Includes: " + ", ".join(components))

        return "\n\n".join(lines)

    def _applyInspectDialogFont(self, dialog):
        font = QtGui.QFont(dialog.font())
        font.setPointSize(self.inspectDialogFontPointSize)
        dialog.setFont(font)
        dialog.setStyleSheet(
            f"QDialog QWidget {{ font-size: {self.inspectDialogFontPointSize}pt; }}"
        )
        return font

    def _applyPlainTextEditFont(self, edit, font):
        edit.setFont(font)
        edit.document().setDefaultFont(font)

    def showSaveModeDialog(
            self, defaultName, defaultDescription, defaultShortcut,
            components, selectedComponents, componentCaptureOptions=None):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Save setup mode")

        layout = QtWidgets.QVBoxLayout(dialog)
        form = QtWidgets.QFormLayout()

        nameEdit = QtWidgets.QLineEdit(defaultName or "")
        descriptionEdit = QtWidgets.QPlainTextEdit(defaultDescription or "")
        descriptionEdit.setFixedHeight(70)

        shortcutRow = QtWidgets.QWidget()
        shortcutLayout = QtWidgets.QHBoxLayout(shortcutRow)
        shortcutLayout.setContentsMargins(0, 0, 0, 0)
        shortcutEdit = self._makeShortcutEditor(defaultShortcut)
        clearShortcutButton = BetterPushButton("Clear")
        clearShortcutButton.clicked.connect(lambda: self._clearShortcutEditor(shortcutEdit))
        shortcutLayout.addWidget(shortcutEdit, 1)
        shortcutLayout.addWidget(clearShortcutButton)

        form.addRow("Name:", nameEdit)
        form.addRow("Description:", descriptionEdit)
        form.addRow("Shortcut:", shortcutRow)
        layout.addLayout(form)

        includeGroup = QtWidgets.QGroupBox("Include in snapshot")
        includeLayout = QtWidgets.QVBoxLayout(includeGroup)
        includeLayout.setContentsMargins(6, 6, 6, 6)
        includeLayout.setSpacing(4)

        selectedComponents = set(selectedComponents or [])
        componentCaptureOptions = componentCaptureOptions or {}
        componentChecks = {}
        captureControls = {}

        for component in components:
            componentWidget = QtWidgets.QWidget()
            componentLayout = QtWidgets.QVBoxLayout(componentWidget)
            componentLayout.setContentsMargins(0, 0, 0, 0)
            componentLayout.setSpacing(2)

            check = QtWidgets.QCheckBox(component)
            check.setChecked(component in selectedComponents)
            componentChecks[component] = check
            componentLayout.addWidget(check)

            options = componentCaptureOptions.get(component) or {}
            if options.get('kind') == 'power_bindings':
                optionsWidget = QtWidgets.QWidget()
                optionsLayout = QtWidgets.QVBoxLayout(optionsWidget)
                optionsLayout.setContentsMargins(22, 0, 0, 4)
                optionsLayout.setSpacing(3)
                sectionControls = {}

                for section in options.get('sections', []):
                    sectionId = section.get('id')
                    if not sectionId:
                        continue
                    sectionCheck = None
                    if section.get('checkable'):
                        sectionCheck = QtWidgets.QCheckBox(section.get('label') or sectionId)
                        sectionCheck.setChecked(bool(section.get('included', True)))
                        optionsLayout.addWidget(sectionCheck)

                    sourceRow = QtWidgets.QWidget()
                    sourceLayout = QtWidgets.QHBoxLayout(sourceRow)
                    sourceLayout.setContentsMargins(
                        18 if sectionCheck is not None else 0, 0, 0, 0
                    )
                    sourceLayout.setSpacing(5)
                    sourceLayout.addWidget(QtWidgets.QLabel(
                        'Source:' if sectionCheck is not None else 'Power source:'
                    ))
                    sourceCombo = QtWidgets.QComboBox()
                    sourceCombo.addItem('Snapshot current powers', 'snapshot')
                    sourceCombo.addItem('Follow preset', 'preset')
                    requestedKind = section.get('bindingKind', 'snapshot')
                    sourceIndex = sourceCombo.findData(requestedKind)
                    sourceCombo.setCurrentIndex(max(0, sourceIndex))
                    sourceLayout.addWidget(sourceCombo)

                    presetCombo = QtWidgets.QComboBox()
                    presetCombo.addItem('', None)
                    for name in section.get('presetNames') or []:
                        presetCombo.addItem(str(name), str(name))
                    requestedPreset = section.get('presetName')
                    if requestedPreset:
                        presetIndex = presetCombo.findData(requestedPreset)
                        if presetIndex < 0:
                            presetCombo.addItem(str(requestedPreset), str(requestedPreset))
                            presetIndex = presetCombo.count() - 1
                        presetCombo.setCurrentIndex(presetIndex)
                    sourceLayout.addWidget(presetCombo, 1)
                    optionsLayout.addWidget(sourceRow)

                    def updatePresetVisibility(
                        _index=None, *, source=sourceCombo, preset=presetCombo,
                        sectionCheck=sectionCheck, row=sourceRow
                    ):
                        included = sectionCheck is None or sectionCheck.isChecked()
                        row.setEnabled(included)
                        preset.setVisible(source.currentData() == 'preset')

                    sourceCombo.currentIndexChanged.connect(updatePresetVisibility)
                    if sectionCheck is not None:
                        sectionCheck.toggled.connect(updatePresetVisibility)
                    updatePresetVisibility()

                    sectionControls[sectionId] = {
                        'check': sectionCheck,
                        'source': sourceCombo,
                        'preset': presetCombo,
                    }

                optionsWidget.setEnabled(check.isChecked())
                check.toggled.connect(optionsWidget.setEnabled)
                componentLayout.addWidget(optionsWidget)
                captureControls[component] = {
                    'metadata': options,
                    'sections': sectionControls,
                }

            includeLayout.addWidget(componentWidget)

        layout.addWidget(includeGroup)

        buttonBox = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel
        )
        layout.addWidget(buttonBox)

        def collectCaptureOptions(validate=False):
            collected = {}
            for component, controls in captureControls.items():
                if not componentChecks[component].isChecked():
                    continue
                metadata = controls['metadata']
                sections = []
                anyIncluded = False
                for sectionMeta in metadata.get('sections', []):
                    sectionId = sectionMeta.get('id')
                    control = controls['sections'].get(sectionId)
                    if control is None:
                        continue
                    included = (
                        control['check'] is None or control['check'].isChecked()
                    )
                    anyIncluded = anyIncluded or included
                    bindingKind = control['source'].currentData()
                    presetName = control['preset'].currentData()
                    if validate and included and bindingKind == 'preset' and not presetName:
                        QtWidgets.QMessageBox.warning(
                            dialog, 'Save setup mode',
                            f'Select a preset for {component}'
                            + (f' / {sectionMeta.get("label")}' if sectionMeta.get('label') else '')
                            + '.'
                        )
                        return None
                    sections.append({
                        'id': sectionId,
                        'included': included,
                        'bindingKind': bindingKind,
                        'presetName': presetName,
                        'fallbackPowers': sectionMeta.get('fallbackPowers') or {},
                    })
                if validate and metadata.get('powerMode') == 'separated' and not anyIncluded:
                    QtWidgets.QMessageBox.warning(
                        dialog, 'Save setup mode',
                        f'Select Live, Scan, or both for {component}.'
                    )
                    return None
                collected[component] = {
                    'kind': metadata.get('kind'),
                    'powerMode': metadata.get('powerMode'),
                    'sections': sections,
                }
            return collected

        def acceptIfValid():
            if not nameEdit.text().strip():
                QtWidgets.QMessageBox.warning(dialog, "Save setup mode", "Mode name is required.")
                return

            included = [
                component for component, check in componentChecks.items()
                if check.isChecked()
            ]
            if not included:
                QtWidgets.QMessageBox.warning(
                    dialog, "Save setup mode", "Select at least one component."
                )
                return
            if collectCaptureOptions(validate=True) is None:
                return
            dialog.accept()

        buttonBox.accepted.connect(acceptIfValid)
        buttonBox.rejected.connect(dialog.reject)

        if self._execDialog(dialog) != QtWidgets.QDialog.Accepted:
            return None

        return {
            "name": nameEdit.text().strip(),
            "description": descriptionEdit.toPlainText().strip(),
            "shortcut": self._shortcutEditorText(shortcutEdit),
            "components": [
                component for component, check in componentChecks.items()
                if check.isChecked()
            ],
            "captureOptions": collectCaptureOptions(validate=False),
        }

    def showUpdateModeDialog(self, modeName, summaries, warnings):
        """Show a structured, selective setup-mode Update dialog.

        Returns ``None`` on cancel or a dict containing selected components,
        component subsection selections, and component capture options.
        """
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Update setup mode")
        dialog.resize(680, 520)

        layout = QtWidgets.QVBoxLayout(dialog)
        intro = QtWidgets.QLabel(
            f'Choose what to update in "{modeName}". Unchecked state is kept unchanged.'
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        content = QtWidgets.QWidget()
        contentLayout = QtWidgets.QVBoxLayout(content)
        contentLayout.setContentsMargins(6, 6, 6, 6)
        contentLayout.setSpacing(8)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        componentChecks = {}
        partChecks = {}
        captureControls = {}

        def addDiffLines(parentLayout, lines, changed):
            if not lines:
                text = (
                    "(state changed; no detailed difference available)"
                    if changed else "(no changes)"
                )
                label = QtWidgets.QLabel(text)
                label.setEnabled(False)
                parentLayout.addWidget(label)
                return
            for line in lines:
                label = QtWidgets.QLabel(str(line))
                label.setWordWrap(True)
                if not changed:
                    label.setEnabled(False)
                parentLayout.addWidget(label)

        for summary in summaries:
            componentName = summary.get('name')
            if not componentName:
                continue
            available = bool(summary.get('available', True))
            diff = summary.get('diff') or {}
            options = summary.get('captureOptions') or {}

            frame = QtWidgets.QFrame()
            frame.setFrameShape(QtWidgets.QFrame.StyledPanel)
            frameLayout = QtWidgets.QVBoxLayout(frame)
            frameLayout.setContentsMargins(8, 6, 8, 6)
            frameLayout.setSpacing(4)

            check = QtWidgets.QCheckBox(summary.get('label') or componentName)
            check.setChecked(bool(summary.get('selected', True)) and available)
            check.setEnabled(available)
            font = check.font()
            font.setBold(True)
            check.setFont(font)
            componentChecks[componentName] = check
            frameLayout.addWidget(check)

            body = QtWidgets.QWidget()
            bodyLayout = QtWidgets.QVBoxLayout(body)
            bodyLayout.setContentsMargins(22, 0, 0, 2)
            bodyLayout.setSpacing(3)
            body.setEnabled(check.isChecked() and available)
            check.toggled.connect(lambda checked, body=body: body.setEnabled(checked))

            notice = diff.get('notice') or options.get('notice')
            if notice:
                noticeLabel = QtWidgets.QLabel(str(notice))
                noticeLabel.setWordWrap(True)
                bodyLayout.addWidget(noticeLabel)

            if options.get('kind') == 'power_bindings':
                powerMode = options.get('powerMode')
                sectionDiffs = {
                    section.get('id'): section
                    for section in diff.get('sections') or []
                    if isinstance(section, dict) and section.get('id')
                }
                sectionControls = {}
                partChecks[componentName] = {}

                for section in options.get('sections') or []:
                    sectionId = section.get('id')
                    if not sectionId:
                        continue
                    separated = powerMode == 'separated'
                    sectionCheck = None
                    sectionContainer = QtWidgets.QWidget()
                    sectionLayout = QtWidgets.QVBoxLayout(sectionContainer)
                    sectionLayout.setContentsMargins(0, 0, 0, 3)
                    sectionLayout.setSpacing(2)

                    if separated:
                        sectionCheck = QtWidgets.QCheckBox(section.get('label') or sectionId)
                        sectionCheck.setChecked(bool(section.get('updateSelected', True)))
                        sectionLayout.addWidget(sectionCheck)
                        partChecks[componentName][sectionId] = sectionCheck

                    sectionDiff = sectionDiffs.get(sectionId) or {}
                    diffHolder = QtWidgets.QWidget()
                    diffLayout = QtWidgets.QVBoxLayout(diffHolder)
                    diffLayout.setContentsMargins(18 if separated else 0, 0, 0, 0)
                    diffLayout.setSpacing(1)
                    addDiffLines(
                        diffLayout,
                        sectionDiff.get('lines') if separated else diff.get('lines'),
                        bool(sectionDiff.get('changed') if separated else diff.get('changed')),
                    )
                    sectionLayout.addWidget(diffHolder)

                    sourceRow = QtWidgets.QWidget()
                    sourceLayout = QtWidgets.QHBoxLayout(sourceRow)
                    sourceLayout.setContentsMargins(18 if separated else 0, 0, 0, 0)
                    sourceLayout.setSpacing(5)
                    sourceLayout.addWidget(QtWidgets.QLabel(
                        'Source:' if separated else 'Power source:'
                    ))
                    sourceCombo = QtWidgets.QComboBox()
                    sourceCombo.addItem('Snapshot current powers', 'snapshot')
                    sourceCombo.addItem('Follow preset', 'preset')
                    requestedKind = section.get('bindingKind', 'snapshot')
                    sourceIndex = sourceCombo.findData(requestedKind)
                    sourceCombo.setCurrentIndex(max(0, sourceIndex))
                    sourceLayout.addWidget(sourceCombo)

                    presetCombo = QtWidgets.QComboBox()
                    presetCombo.addItem('', None)
                    for name in section.get('presetNames') or []:
                        presetCombo.addItem(str(name), str(name))
                    requestedPreset = section.get('presetName')
                    if requestedPreset:
                        presetIndex = presetCombo.findData(requestedPreset)
                        if presetIndex < 0:
                            presetCombo.addItem(str(requestedPreset), str(requestedPreset))
                            presetIndex = presetCombo.count() - 1
                        presetCombo.setCurrentIndex(presetIndex)
                    sourceLayout.addWidget(presetCombo, 1)
                    sectionLayout.addWidget(sourceRow)

                    def updateSourceState(
                        _value=None, *, source=sourceCombo, preset=presetCombo,
                        row=sourceRow, sectionCheck=sectionCheck
                    ):
                        selected = sectionCheck is None or sectionCheck.isChecked()
                        row.setEnabled(selected)
                        preset.setVisible(source.currentData() == 'preset')

                    sourceCombo.currentIndexChanged.connect(updateSourceState)
                    if sectionCheck is not None:
                        sectionCheck.toggled.connect(updateSourceState)
                    updateSourceState()

                    sectionControls[sectionId] = {
                        'check': sectionCheck,
                        'source': sourceCombo,
                        'preset': presetCombo,
                    }
                    bodyLayout.addWidget(sectionContainer)

                captureControls[componentName] = {
                    'metadata': options,
                    'sections': sectionControls,
                }
            else:
                addDiffLines(
                    bodyLayout, diff.get('lines') or [], bool(diff.get('changed'))
                )

            frameLayout.addWidget(body)
            contentLayout.addWidget(frame)

        contentLayout.addStretch(1)

        if warnings:
            warningLabel = QtWidgets.QLabel(
                "Snapshot warnings:\n" + "\n".join(str(w) for w in warnings)
            )
            warningLabel.setWordWrap(True)
            layout.addWidget(warningLabel)

        buttonBox = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel
        )
        saveButton = buttonBox.button(QtWidgets.QDialogButtonBox.Save)
        if saveButton is not None:
            saveButton.setText("Update selected")
        layout.addWidget(buttonBox)

        def collectResult(validate=False):
            selectedComponents = [
                name for name, check in componentChecks.items() if check.isChecked()
            ]
            if validate and not selectedComponents:
                QtWidgets.QMessageBox.warning(
                    dialog, "Update setup mode", "Select at least one component to update."
                )
                return None

            selectedParts = {}
            captureOptions = {}
            for componentName, controls in captureControls.items():
                if componentName not in selectedComponents:
                    continue
                metadata = controls['metadata']
                sections = []
                selectedSectionIds = []
                for sectionMeta in metadata.get('sections') or []:
                    sectionId = sectionMeta.get('id')
                    control = controls['sections'].get(sectionId)
                    if control is None:
                        continue
                    selected = control['check'] is None or control['check'].isChecked()
                    if selected:
                        selectedSectionIds.append(sectionId)
                    bindingKind = control['source'].currentData()
                    presetName = control['preset'].currentData()
                    if validate and selected and bindingKind == 'preset' and not presetName:
                        QtWidgets.QMessageBox.warning(
                            dialog, "Update setup mode",
                            f'Select a preset for {componentName}'
                            + (f' / {sectionMeta.get("label")}' if sectionMeta.get('label') else '')
                            + '.'
                        )
                        return None
                    sections.append({
                        'id': sectionId,
                        # Update candidates contain the current subsection; the
                        # merge hook decides whether an unchecked subsection is
                        # kept from the old payload.
                        'included': True,
                        'bindingKind': bindingKind,
                        'presetName': presetName,
                        'fallbackPowers': sectionMeta.get('fallbackPowers') or {},
                    })

                if metadata.get('powerMode') == 'separated':
                    if validate and not selectedSectionIds:
                        QtWidgets.QMessageBox.warning(
                            dialog, "Update setup mode",
                            f'Select Live, Scan, or both for {componentName}, '
                            'or uncheck this component.'
                        )
                        return None
                    selectedParts[componentName] = selectedSectionIds

                captureOptions[componentName] = {
                    'kind': metadata.get('kind'),
                    'powerMode': metadata.get('powerMode'),
                    'sections': sections,
                }

            return {
                'components': selectedComponents,
                'selectedParts': selectedParts,
                'captureOptions': captureOptions,
            }

        def acceptIfValid():
            if collectResult(validate=True) is None:
                return
            dialog.accept()

        buttonBox.accepted.connect(acceptIfValid)
        buttonBox.rejected.connect(dialog.reject)

        if self._execDialog(dialog) != QtWidgets.QDialog.Accepted:
            return None
        return collectResult(validate=False)

    def showShortcutDialog(self, modeName, currentShortcut):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Set setup mode shortcut")

        layout = QtWidgets.QVBoxLayout(dialog)
        layout.addWidget(QtWidgets.QLabel(modeName))

        row = QtWidgets.QWidget()
        rowLayout = QtWidgets.QHBoxLayout(row)
        rowLayout.setContentsMargins(0, 0, 0, 0)
        shortcutEdit = self._makeShortcutEditor(currentShortcut)
        clearButton = BetterPushButton("Clear")
        clearButton.clicked.connect(lambda: self._clearShortcutEditor(shortcutEdit))
        rowLayout.addWidget(shortcutEdit, 1)
        rowLayout.addWidget(clearButton)
        layout.addWidget(row)

        buttonBox = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        layout.addWidget(buttonBox)
        buttonBox.accepted.connect(dialog.accept)
        buttonBox.rejected.connect(dialog.reject)

        if self._execDialog(dialog) != QtWidgets.QDialog.Accepted:
            return None

        return self._shortcutEditorText(shortcutEdit)

    def showSafetySettingsDialog(self, settings):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Setup mode safety")

        layout = QtWidgets.QVBoxLayout(dialog)

        warnCheck = QtWidgets.QCheckBox("Warn above laser power threshold")
        warnCheck.setChecked(bool(settings.get("warnAboveLaserPowerThreshold", True)))
        layout.addWidget(warnCheck)

        thresholdSpin = QtWidgets.QDoubleSpinBox()
        thresholdSpin.setRange(0.0, 1000000.0)
        thresholdSpin.setDecimals(3)
        thresholdSpin.setSuffix(" mW")
        try:
            thresholdValue = float(settings.get("laserPowerThresholdMw", 50.0))
        except (TypeError, ValueError):
            thresholdValue = 50.0
        thresholdSpin.setValue(thresholdValue)

        form = QtWidgets.QFormLayout()
        form.addRow("Threshold:", thresholdSpin)
        layout.addLayout(form)

        shortcutCheck = QtWidgets.QCheckBox("Confirm shortcut applies when warning is triggered")
        shortcutCheck.setChecked(bool(settings.get("confirmHighPowerShortcutApply", True)))
        layout.addWidget(shortcutCheck)

        suppressedCount = len(settings.get("suppressedWarnings", []) or [])
        clearSuppressedCheck = QtWidgets.QCheckBox(
            f"Show hidden warnings again ({suppressedCount} hidden)"
        )
        clearSuppressedCheck.setEnabled(suppressedCount > 0)
        layout.addWidget(clearSuppressedCheck)

        buttonBox = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        layout.addWidget(buttonBox)
        buttonBox.accepted.connect(dialog.accept)
        buttonBox.rejected.connect(dialog.reject)

        if self._execDialog(dialog) != QtWidgets.QDialog.Accepted:
            return None

        return {
            "warnAboveLaserPowerThreshold": warnCheck.isChecked(),
            "laserPowerThresholdMw": thresholdSpin.value(),
            "confirmHighPowerShortcutApply": shortcutCheck.isChecked(),
            "clearSuppressedWarnings": clearSuppressedCheck.isChecked(),
        }

    def showSmartMicroscopyRolesDialog(self, workflowNames, availableModes, currentConfig):
        """Show dialog for configuring smart microscopy role-to-mode mappings.

        Args:
            workflowNames: List of workflow names (from controllers + setupInfo)
            availableModes: List of available setup mode names
            currentConfig: Dict with 'modes', 'policies', 'enabled' keys containing
                current configuration from setupInfo

        Returns:
            Dict with 'modes', 'policies', 'enabled' keys, or None if cancelled
        """
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Smart Microscopy Roles")
        dialog.setMinimumWidth(600)

        layout = QtWidgets.QVBoxLayout(dialog)

        label = QtWidgets.QLabel(
            "Assign setup modes to workflow roles. Each workflow can define modes for "
            "scouting, event, resume, idle, and validation phases."
        )
        label.setWordWrap(True)
        layout.addWidget(label)

        layout.addSpacing(8)

        currentModes = currentConfig.get("modes", {})
        currentPolicies = currentConfig.get("policies", {})
        currentEnabled = currentConfig.get("enabled", {})

        roleNames = ("scouting", "event", "resume", "idle", "validation")
        policyNames = ("blockOnHazard", "warnOnly", "allow")

        roleWidgets = {}
        missingAssignments = []

        scrollArea = QtWidgets.QScrollArea()
        scrollArea.setWidgetResizable(True)
        scrollWidget = QtWidgets.QWidget()
        scrollLayout = QtWidgets.QVBoxLayout(scrollWidget)

        for workflowName in sorted(workflowNames):
            groupBox = QtWidgets.QGroupBox(workflowName)
            groupLayout = QtWidgets.QVBoxLayout(groupBox)

            enabledCheck = QtWidgets.QCheckBox("Enable smart mode switching for this workflow")
            enabledCheck.setChecked(currentEnabled.get(workflowName, False))
            groupLayout.addWidget(enabledCheck)

            rolesForm = QtWidgets.QFormLayout()
            workflowRoleCombos = {}
            workflowModes = currentModes.get(workflowName, {})

            for role in roleNames:
                combo = QtWidgets.QComboBox()
                combo.addItem("", "")
                for modeName in availableModes:
                    combo.addItem(modeName, modeName)

                currentMode = workflowModes.get(role, "")
                if currentMode and currentMode not in availableModes:
                    combo.addItem(f"{currentMode} (missing)", currentMode)
                    missingAssignments.append(f"{workflowName}.{role}: {currentMode}")

                index = combo.findData(currentMode)
                combo.setCurrentIndex(index if index >= 0 else 0)
                rolesForm.addRow(f"{role.capitalize()}:", combo)
                workflowRoleCombos[role] = combo

            groupLayout.addLayout(rolesForm)

            policyForm = QtWidgets.QFormLayout()
            policyCombo = QtWidgets.QComboBox()
            policyCombo.addItems(policyNames)
            currentPolicy = currentPolicies.get(workflowName, 'blockOnHazard')
            if currentPolicy in policyNames:
                policyCombo.setCurrentText(currentPolicy)
            else:
                policyCombo.setCurrentText('blockOnHazard')
            policyForm.addRow("Hazard policy:", policyCombo)
            groupLayout.addLayout(policyForm)

            roleWidgets[workflowName] = {
                'enabled': enabledCheck,
                'roles': workflowRoleCombos,
                'policy': policyCombo,
            }

            scrollLayout.addWidget(groupBox)

        scrollLayout.addStretch(1)
        scrollArea.setWidget(scrollWidget)
        layout.addWidget(scrollArea)

        statusLabel = QtWidgets.QLabel("")
        statusLabel.setWordWrap(True)
        if missingAssignments:
            statusLabel.setText(
                "Missing configured setup modes: " + "; ".join(missingAssignments)
            )
        layout.addWidget(statusLabel)

        buttonBox = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        layout.addWidget(buttonBox)
        buttonBox.accepted.connect(dialog.accept)
        buttonBox.rejected.connect(dialog.reject)

        if self._execDialog(dialog) != QtWidgets.QDialog.Accepted:
            return None

        resultModes = {}
        resultPolicies = {}
        resultEnabled = {}

        for workflowName, widgets in roleWidgets.items():
            enabled = widgets['enabled'].isChecked()
            resultEnabled[workflowName] = enabled

            workflowModes = {}
            for role, combo in widgets['roles'].items():
                modeName = combo.currentData()
                modeName = str(modeName).strip() if modeName is not None else ""
                if modeName:
                    workflowModes[role] = modeName

            if workflowModes:
                resultModes[workflowName] = workflowModes

            policy = widgets['policy'].currentText()
            if policy and policy != 'blockOnHazard':
                resultPolicies[workflowName] = policy

        return {
            'modes': resultModes,
            'policies': resultPolicies,
            'enabled': resultEnabled,
        }

    def showNameDialog(self, title, label, suggested=""):
        result, okClicked = QtWidgets.QInputDialog.getText(
            self, title, label,
            flags=QtCore.Qt.WindowSystemMenuHint | QtCore.Qt.WindowTitleHint,
            text=suggested or ""
        )
        return result.strip() if okClicked and result.strip() else None

    def askOverwriteMode(self, modeName):
        return self._askQuestion(
            "Overwrite setup mode",
            f'Setup mode "{modeName}" already exists. Overwrite it?'
        )

    def askDeleteMode(self, modeName):
        return self._askQuestion(
            "Delete setup mode",
            f'Delete setup mode "{modeName}"?'
        )

    def askShortcutConflict(self, shortcut, otherModeName):
        return self._askQuestion(
            "Shortcut already used",
            f'Shortcut "{shortcut}" is already assigned to "{otherModeName}". Replace it?'
        )

    def confirmHighPowerApply(self, modeName, laserEntries, thresholdMw):
        details = "\n".join(
            f'- {entry["laserName"]}: {entry["value"]:.3g} {entry["units"]}'
            for entry in laserEntries
        )

        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setWindowTitle("Laser power warning")
        box.setText(
            f'Setup mode "{modeName}" turns on laser power above {thresholdMw:.3g} mW.'
        )
        box.setInformativeText(details)

        applyButton = box.addButton("Apply", QtWidgets.QMessageBox.AcceptRole)
        cancelButton = box.addButton(QtWidgets.QMessageBox.Cancel)
        box.setDefaultButton(cancelButton)
        self._execDialog(box)
        return box.clickedButton() == applyButton

    def showWarnings(self, title, warnings, allowSuppress=False):
        if not warnings:
            return False

        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setWindowTitle(title)
        box.setText(title)
        box.setInformativeText("\n".join(warnings))
        box.addButton(QtWidgets.QMessageBox.Ok)

        suppressCheck = None
        if allowSuppress:
            suppressCheck = QtWidgets.QCheckBox("Don't show these warnings again")
            if hasattr(box, "setCheckBox"):
                box.setCheckBox(suppressCheck)

        if allowSuppress and suppressCheck is not None and not hasattr(box, "setCheckBox"):
            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle(title)
            layout = QtWidgets.QVBoxLayout(dialog)
            label = QtWidgets.QLabel("\n".join(warnings))
            label.setWordWrap(True)
            layout.addWidget(label)
            layout.addWidget(suppressCheck)
            buttonBox = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok)
            buttonBox.accepted.connect(dialog.accept)
            layout.addWidget(buttonBox)
            self._execDialog(dialog)
            return suppressCheck.isChecked()

        self._execDialog(box)
        return bool(suppressCheck is not None and suppressCheck.isChecked())

    def showError(self, title, message):
        QtWidgets.QMessageBox.critical(self, title, message)

    def showInformation(self, title, message):
        QtWidgets.QMessageBox.information(self, title, message)

    def _onModeIndexChanged(self, _index):
        self._updateInfoButton()
        self._updateModeControls()

    def _onModeActivated(self, _index):
        modeName = self.getSelectedModeName()
        if modeName:
            self.sigModeSelected.emit(modeName)

    def _updateInfoButton(self):
        self.infoButton.setToolTip("Open setup mode manager")
        self.infoButton.setEnabled(self._backendAvailable and bool(self._modeSummaries))

    def _updateModeControls(self):
        hasMode = self.getSelectedModeName() is not None
        hasModes = bool(self._modeSummaries)
        hasUsableMode = self._backendAvailable and hasMode
        self.modeCombo.setEnabled(self._backendAvailable and hasModes)
        self.reloadButton.setEnabled(hasUsableMode)
        self.updateButton.setEnabled(hasUsableMode)
        self.moreButton.setEnabled(self._backendAvailable)
        self.renameAction.setEnabled(hasUsableMode)
        self.duplicateAction.setEnabled(hasUsableMode)
        self.shortcutAction.setEnabled(hasUsableMode)
        self.smartMicroscopyRolesAction.setEnabled(self._backendAvailable)
        self.deleteAction.setEnabled(hasUsableMode)

    def _makeShortcutEditor(self, shortcut):
        if hasattr(QtWidgets, "QKeySequenceEdit"):
            editor = QtWidgets.QKeySequenceEdit()
            if shortcut:
                editor.setKeySequence(QtGui.QKeySequence(shortcut))
        else:
            editor = QtWidgets.QLineEdit(shortcut or "")
            editor.setPlaceholderText("F3")
        return editor

    def _clearShortcutEditor(self, editor):
        if hasattr(QtWidgets, "QKeySequenceEdit") and isinstance(editor, QtWidgets.QKeySequenceEdit):
            editor.setKeySequence(QtGui.QKeySequence())
        else:
            editor.clear()

    def _shortcutEditorText(self, editor):
        if hasattr(QtWidgets, "QKeySequenceEdit") and isinstance(editor, QtWidgets.QKeySequenceEdit):
            sequence = editor.keySequence()
            try:
                return sequence.toString(QtGui.QKeySequence.NativeText).strip()
            except TypeError:
                return sequence.toString().strip()

        return editor.text().strip()

    def _askQuestion(self, title, question):
        result = QtWidgets.QMessageBox.question(
            self, title, question,
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
        )
        return result == QtWidgets.QMessageBox.Yes

    def _execDialog(self, dialog):
        if hasattr(dialog, "exec_"):
            return dialog.exec_()
        return dialog.exec()


# Copyright (C) 2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
