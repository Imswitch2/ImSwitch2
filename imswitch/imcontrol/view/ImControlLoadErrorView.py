import dataclasses
from pathlib import Path

from qtpy import QtCore, QtWidgets

from imswitch.imcommon.model import ostools
from imswitch.imcommon.view import ModuleLoadErrorView
from imswitch.imcontrol.model import configfiletools
from imswitch.imcontrol.view.PickSetupDialog import PickSetupDialog


class ImControlLoadErrorView(ModuleLoadErrorView):
    """Load-error view with a hardware-setup recovery path.

    This view deliberately depends only on configuration/UI code, not on the
    ImControl controller or hardware managers. It therefore remains usable when
    ImControl failed part-way through constructing those objects.
    """

    def __init__(self, error, *args, **kwargs):
        super().__init__(error, *args, **kwargs)

        self._configEditor = None
        self._activeSetupPath = self._resolveActiveSetupPath()

        # Make recovery the primary content of the page. The raw exception is
        # still available underneath, but it should not visually dominate the
        # action the user can take to recover from a bad hardware setup.
        headerLabel = QtWidgets.QLabel('Hardware Control could not start')
        headerLabel.setObjectName('recoveryHeader')
        headerLabel.setAlignment(QtCore.Qt.AlignCenter)

        helpLabel = QtWidgets.QLabel(
            'ImSwitch could not start Hardware Control with the selected hardware setup. '
            'If the setup is incompatible or incomplete, choose another setup or edit the '
            'current hardware configuration below, then restart ImSwitch.'
        )
        helpLabel.setWordWrap(True)
        helpLabel.setAlignment(QtCore.Qt.AlignCenter)
        helpLabel.setStyleSheet('font-size: 13pt')

        pickButton = QtWidgets.QPushButton('Pick hardware setup…')
        pickButton.setObjectName('recoveryButton')
        pickButton.clicked.connect(self.pickSetup)
        pickButton.setMinimumSize(150, 50)
        pickButton.setDefault(True)

        editButton = QtWidgets.QPushButton('Edit hardware configuration…')
        editButton.setObjectName('recoveryButton')
        editButton.clicked.connect(self.openConfigEditor)
        editButton.setMinimumSize(220, 50)

        buttonsLayout = QtWidgets.QHBoxLayout()
        buttonsLayout.setSpacing(12)
        buttonsLayout.addStretch(1)
        buttonsLayout.addWidget(pickButton)
        buttonsLayout.addWidget(editButton)
        buttonsLayout.addStretch(1)

        errorHeaderLabel = QtWidgets.QLabel('Startup error details')
        errorHeaderLabel.setAlignment(QtCore.Qt.AlignCenter)
        errorHeaderLabel.setStyleSheet('font-size: 11pt; font-weight: 600')
        errorMessage = QtWidgets.QTextEdit(f'{type(error).__name__}: {error}')
        errorMessage.setReadOnly(True)
        errorMessage.setStyleSheet('font-size: 11pt')
        errorMessage.setAlignment(QtCore.Qt.AlignCenter)
        errorMessage.setFixedHeight(110)

        layout = QtWidgets.QVBoxLayout()
        layout.setSpacing(16)
        layout.setContentsMargins(48, 32, 48, 40)
        layout.addStretch(1)
        layout.addWidget(headerLabel)
        layout.addWidget(helpLabel)
        layout.addLayout(buttonsLayout)
        layout.addSpacing(24)
        layout.addWidget(errorHeaderLabel)
        layout.addWidget(errorMessage)
        layout.addStretch(1)

        container = QtWidgets.QWidget()
        container.setLayout(layout)
        # ImSwitch's global stylesheet sets QWidget font-size explicitly, which
        # overrides QFont sizes set with setFont(). 
        container.setStyleSheet('''
            QLabel#recoveryHeader {
                font-size: 15pt;
                font-weight: bold;
            }
            QPushButton#recoveryButton {
                font-size: 9pt;
                font-weight: bold;
            }
        ''')
        self.setCentralWidget(container)

        # Named attributes make the intended hierarchy explicit and keep tests
        # independent of layout-item indexes.
        self.recoveryHeaderLabel = headerLabel
        self.recoveryHelpLabel = helpLabel
        self.pickSetupButton = pickButton
        self.configEditorButton = editButton
        self.errorHeaderLabel = errorHeaderLabel
        self.errorMessage = errorMessage

        hardwareMenu = self.menuBar().addMenu('&Hardware')
        hardwareMenu.setToolTipsVisible(True)

        pickAction = QtWidgets.QAction('Pick hardware setup…', self)
        pickAction.setToolTip('Choose another hardware setup and restart ImSwitch.')
        pickAction.triggered.connect(self.pickSetup)
        hardwareMenu.addAction(pickAction)

        editAction = QtWidgets.QAction('Edit hardware configuration…', self)
        editAction.setToolTip('Open the Config Studio on the hardware setup files.')
        editAction.triggered.connect(self.openConfigEditor)
        hardwareMenu.addAction(editAction)

        # Kept as attributes for tests and for parity with the normal ImControl view.
        self.pickSetupAction = pickAction
        self.configEditorAction = editAction

    def _resolveActiveSetupPath(self):
        try:
            options, _ = configfiletools.loadOptions()
            return str(Path(configfiletools.getSetupFilePath(options.setupFileName)).resolve())
        except Exception:  # noqa: BLE001 - recovery UI must survive damaged config.
            return None

    def pickSetup(self):
        """Choose a setup without requiring the failed ImControl controller."""
        try:
            options, _ = configfiletools.loadOptions()
            setups = configfiletools.getSetupList()
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.critical(
                self,
                'Could not list hardware setups',
                f'ImSwitch could not read the hardware setup list:\n{exc}',
            )
            return

        dialog = PickSetupDialog(self)
        dialog.setSetups(setups)
        dialog.setSelectedSetup(options.setupFileName)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return

        setupFileName = dialog.getSelectedSetup()
        if not setupFileName:
            return

        proceed = QtWidgets.QMessageBox.question(
            self,
            'Restart ImSwitch?',
            f'ImSwitch will restart using "{setupFileName}". Continue?',
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.Yes,
        )
        if proceed != QtWidgets.QMessageBox.Yes:
            return

        try:
            configfiletools.saveOptions(
                dataclasses.replace(options, setupFileName=setupFileName)
            )
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.critical(
                self,
                'Could not change hardware setup',
                f'ImSwitch could not save the selected hardware setup:\n{exc}',
            )
            return

        self._restartAfterShutdown()

    def openConfigEditor(self):
        """Open Config Studio even though the normal ImControl controller failed."""
        if self._configEditor is not None:
            self._configEditor.show()
            self._configEditor.raise_()
            self._configEditor.activateWindow()
            return

        try:
            from imswitch.imcontrol.view.configeditor import MainWindow as ConfigEditor
            editor = ConfigEditor(
                start_folder=configfiletools.getSetupFilesDir(),
                parent=self,
            )
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.critical(
                self,
                'Could not open the config editor',
                f'The hardware configuration editor failed to start:\n{exc}',
            )
            return

        editor.sig_closed.connect(self._onConfigEditorClosed)
        self._configEditor = editor
        editor.show()

    def _onConfigEditorClosed(self):
        editor = self._configEditor
        if editor is None:
            return
        self._configEditor = None

        needsRestart = (
            editor.active_config_changed
            or (
                self._activeSetupPath is not None
                and self._activeSetupPath in editor.saved_files()
            )
        )
        editor.deleteLater()

        if needsRestart:
            QtCore.QTimer.singleShot(0, self._promptRestartAfterConfigEdit)

    def _promptRestartAfterConfigEdit(self):
        proceed = QtWidgets.QMessageBox.question(
            self,
            'Restart ImSwitch?',
            'The hardware configuration used at startup has changed.\n\n'
            'Restart ImSwitch now?',
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.Yes,
        )
        if proceed == QtWidgets.QMessageBox.Yes:
            self._restartAfterShutdown()

    def _restartAfterShutdown(self):
        # Use the same deferred-restart path as the normal ImControl UI so
        # every successfully loaded module is shut down before re-exec.
        ostools.restartAfterShutdown(self.window().close)
