"""The Camera → scan calibration window (Hardware → Camera → scan calibration…).

View only: two file fields (camera, scan) with what each file holds, the
options, Fit / Cancel, and the result with its actions. Every decision --
what a file holds, the warnings, the fit, what is saved where -- is
``BeadCalibrationController``'s; this reports clicks and shows what it is
handed. Nothing here touches hardware.
"""
from __future__ import annotations

from qtpy import QtCore, QtGui, QtWidgets

from .basewidgets import Widget

_WARN_STYLE = 'background-color: #fff4e5; color: #663c00; padding: 4px; border-radius: 3px;'
_OK_STYLE = 'background-color: #e8f5e9; color: #1b5e20; padding: 6px; border-radius: 3px; font-weight: bold;'
_BAD_STYLE = 'background-color: #fdecea; color: #611a15; padding: 6px; border-radius: 3px; font-weight: bold;'

FILE_FILTER = 'ImSwitch recordings (*.h5 *.hdf5);;All files (*)'


class _FileField(QtWidgets.QGroupBox):
    """One recording: path, Browse (HDF5 file or Zarr folder), detector,
    what the file holds, a thumbnail and the warnings."""

    sigBrowse = QtCore.Signal(str)             # 'file' | 'zarr'
    sigDetector = QtCore.Signal(str)

    def __init__(self, title, parent=None):
        super().__init__(title, parent)
        self.pathEdit = QtWidgets.QLineEdit()
        self.pathEdit.setReadOnly(True)
        self.pathEdit.setPlaceholderText('No file chosen')
        self.browseButton = QtWidgets.QToolButton()
        self.browseButton.setText('Browse…')
        self.browseButton.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        menu = QtWidgets.QMenu(self.browseButton)
        menu.addAction('HDF5 file…', lambda: self.sigBrowse.emit('file'))
        menu.addAction('Zarr store (folder)…', lambda: self.sigBrowse.emit('zarr'))
        self.browseButton.setMenu(menu)
        self.detectorCombo = QtWidgets.QComboBox()
        self.detectorCombo.setToolTip('The detector of this file to use')
        self.detectorCombo.activated.connect(lambda _: self.sigDetector.emit(self.detectorCombo.currentText()))
        self.detectorCombo.hide()
        self.info = QtWidgets.QLabel('')
        self.info.setTextFormat(QtCore.Qt.RichText)
        self.info.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.info.setAlignment(QtCore.Qt.AlignTop)
        # Never squeezed below its table: the window scrolls instead.
        self.info.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Minimum)
        self.warnings = QtWidgets.QLabel('')
        self.warnings.setWordWrap(True)
        self.warnings.setStyleSheet(_WARN_STYLE)
        self.warnings.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Minimum)
        self.warnings.hide()
        self.thumbnail = QtWidgets.QLabel()
        self.thumbnail.setFixedSize(170, 170)
        self.thumbnail.setAlignment(QtCore.Qt.AlignCenter)
        self.thumbnail.setStyleSheet('background-color: #202020; color: #aaaaaa;')
        top = QtWidgets.QHBoxLayout()
        top.addWidget(self.pathEdit, 1)
        top.addWidget(self.detectorCombo)
        top.addWidget(self.browseButton)
        body = QtWidgets.QHBoxLayout()
        body.addWidget(self.info, 1)
        body.addWidget(self.thumbnail)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(top)
        layout.addLayout(body)
        layout.addWidget(self.warnings)

    def show_summary(self, path, detectors, detector, lines, warnings):
        self.pathEdit.setText(str(path))
        self.pathEdit.setToolTip(str(path))
        self.detectorCombo.blockSignals(True)
        self.detectorCombo.clear()
        self.detectorCombo.addItems(list(detectors))
        if detector in detectors:
            self.detectorCombo.setCurrentText(detector)
        self.detectorCombo.blockSignals(False)
        self.detectorCombo.setVisible(len(detectors) > 1)
        lines = list(lines)
        half = (len(lines) + 1) // 2

        def cell(value):
            value = str(value)
            return value.replace(', ', ',<br>') if len(value) > 40 else value

        def table(part):
            return '<table>' + ''.join(
                f'<tr><td style="padding-right:6px"><b>{label}</b></td><td>{cell(value)}</td></tr>'
                for label, value in part) + '</table>'
        # Two columns side by side: the file's facts at a glance, not a scroll.
        self.info.setText(f'<table><tr><td valign="top" style="padding-right:18px">{table(lines[:half])}</td>'
                          f'<td valign="top">{table(lines[half:])}</td></tr></table>')
        self.info.setMinimumHeight(self.info.sizeHint().height())
        self.set_warnings(warnings)

    def set_warnings(self, warnings):
        self.warnings.setText('\n'.join(f'⚠ {w}' for w in warnings))
        self.warnings.setVisible(bool(warnings))

    def show_error(self, path, message):
        self.pathEdit.setText(str(path))
        self.info.setText('')
        self.thumbnail.clear()
        self.set_warnings([message])

    def set_thumbnail(self, png_bytes, text=''):
        if png_bytes:
            pixmap = QtGui.QPixmap()
            pixmap.loadFromData(png_bytes, 'PNG')
            self.thumbnail.setPixmap(pixmap.scaled(self.thumbnail.size(), QtCore.Qt.KeepAspectRatio,
                                                   QtCore.Qt.SmoothTransformation))
        else:
            self.thumbnail.clear()
            self.thumbnail.setText(text)


class BeadCalibrationWidget(Widget):
    """Camera → scan calibration from two bead recordings."""

    sigBrowse = QtCore.Signal(str, str)           # role, 'file' | 'zarr'
    sigDetectorChosen = QtCore.Signal(str, str)   # role, detector
    sigFit = QtCore.Signal()
    sigCancel = QtCore.Signal()
    sigOpenFigure = QtCore.Signal()
    sigOpenFolder = QtCore.Signal()
    sigCopyReport = QtCore.Signal()
    sigVerify = QtCore.Signal()
    sigPredict = QtCore.Signal(float, float)

    MODELS = (('auto', 'Auto (similarity; affine only if clearly better)'),
              ('similarity_reflect', 'Similarity (rotation, scale, mirror allowed)'),
              ('affine', 'Affine (6 parameters)'))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setWindowTitle('Camera → scan calibration')
        self.resize(1120, 900)
        intro = QtWidgets.QLabel(
            'Pick a camera recording and an APD scan recording (Advanced scan, "Scan once") of the SAME sparse, '
            'random bead field, then Fit. The result maps camera pixels to Advanced scan centres (µm).')
        intro.setWordWrap(True)
        self.cameraField = _FileField('Camera recording')
        self.scanField = _FileField('Scan (APD) recording')
        self.cameraField.sigBrowse.connect(lambda kind: self.sigBrowse.emit('camera', kind))
        self.scanField.sigBrowse.connect(lambda kind: self.sigBrowse.emit('scan', kind))
        self.cameraField.sigDetector.connect(lambda name: self.sigDetectorChosen.emit('camera', name))
        self.scanField.sigDetector.connect(lambda name: self.sigDetectorChosen.emit('scan', name))
        self.pairWarnings = QtWidgets.QLabel('')
        self.pairWarnings.setWordWrap(True)
        self.pairWarnings.setStyleSheet(_WARN_STYLE)
        self.pairWarnings.hide()

        self.idEdit = QtWidgets.QLineEdit('cam_to_scan')
        self.idEdit.setToolTip('The id the pipeline\'s transform step names')
        self.modelCombo = QtWidgets.QComboBox()
        for key, label in self.MODELS:
            self.modelCombo.addItem(label, key)
        self.advancedToggle = QtWidgets.QToolButton()
        self.advancedToggle.setText('Advanced ▸')
        self.advancedToggle.setCheckable(True)
        self.advancedToggle.setAutoRaise(True)
        self.advancedBox = QtWidgets.QWidget()
        self.advancedToggle.toggled.connect(self._toggleAdvanced)
        self.cameraThreshold = self._spin(6.0, 1.0, 100.0, 0.5)
        self.scanThreshold = self._spin(6.0, 1.0, 100.0, 0.5)
        self.cameraSigma = self._spin(0.0, 0.0, 20.0, 0.1, special='measure')
        self.scanSigma = self._spin(0.0, 0.0, 20.0, 0.1, special='measure')
        self.maxResidual = self._spin(0.25, 0.01, 5.0, 0.05, suffix=' µm')
        self.tolerance = self._spin(0.0, 0.0, 10.0, 0.05, suffix=' µm', special='auto')
        self.allowMirror = QtWidgets.QCheckBox('Allow a mirrored camera')
        self.allowMirror.setChecked(True)
        self.fastAxis = QtWidgets.QComboBox()
        for label, value in (('Auto (as this setup scans it)', None), ('Swept (galvo)', True),
                             ('Stepped (stage, piezo)', False)):
            self.fastAxis.addItem(label, value)
        self.fastAxis.setToolTip('How the fast scan axis moved. It only matters when the scan length is not a '
                                 'whole number of steps: it then decides where the first pixel sits.')
        advanced = QtWidgets.QFormLayout(self.advancedBox)
        advanced.addRow('Camera detection threshold (noise units)', self.cameraThreshold)
        advanced.addRow('Scan detection threshold (noise units)', self.scanThreshold)
        advanced.addRow('Camera PSF sigma (px)', self.cameraSigma)
        advanced.addRow('Scan PSF sigma (px)', self.scanSigma)
        advanced.addRow('Pairing tolerance', self.tolerance)
        advanced.addRow('Fast axis', self.fastAxis)
        advanced.addRow('Largest accepted residual', self.maxResidual)
        advanced.addRow('', self.allowMirror)
        self.advancedBox.hide()
        options = QtWidgets.QFormLayout()
        options.addRow('Calibration id', self.idEdit)
        options.addRow('Model', self.modelCombo)
        options.addRow(self.advancedToggle, self.advancedBox)

        self.fitButton = QtWidgets.QPushButton('Fit')
        self.fitButton.setDefault(True)
        self.fitButton.clicked.connect(self.sigFit)
        self.cancelButton = QtWidgets.QPushButton('Cancel')
        self.cancelButton.clicked.connect(self.sigCancel)
        self.cancelButton.setEnabled(False)
        self.busy = QtWidgets.QProgressBar()
        self.busy.setRange(0, 0)
        self.busy.setMaximumWidth(160)
        self.busy.hide()
        self.status = QtWidgets.QLabel('')
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self.fitButton)
        buttons.addWidget(self.cancelButton)
        buttons.addWidget(self.busy)
        buttons.addWidget(self.status, 1)

        self.verdict = QtWidgets.QLabel('')
        self.verdict.setWordWrap(True)
        self.verdict.hide()
        self.resultText = QtWidgets.QPlainTextEdit()
        self.resultText.setReadOnly(True)
        self.resultText.setMinimumHeight(230)
        font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        self.resultText.setFont(font)
        self.openFigureButton = QtWidgets.QPushButton('Open QC figure')
        self.openFigureButton.clicked.connect(self.sigOpenFigure)
        self.openFolderButton = QtWidgets.QPushButton('Open folder')
        self.openFolderButton.clicked.connect(self.sigOpenFolder)
        self.copyButton = QtWidgets.QPushButton('Copy report')
        self.copyButton.clicked.connect(self.sigCopyReport)
        self.verifyButton = QtWidgets.QPushButton('Verify with another pair of files…')
        self.verifyButton.clicked.connect(self.sigVerify)
        actions = QtWidgets.QHBoxLayout()
        for button in (self.openFigureButton, self.openFolderButton, self.copyButton, self.verifyButton):
            actions.addWidget(button)
        actions.addStretch(1)
        self.rowSpin = self._spin(0.0, -1e5, 1e5, 1.0, decimals=1)
        self.colSpin = self._spin(0.0, -1e5, 1e5, 1.0, decimals=1)
        self.predictButton = QtWidgets.QPushButton('Predict scan position')
        self.predictButton.clicked.connect(lambda: self.sigPredict.emit(self.rowSpin.value(), self.colSpin.value()))
        self.predictResult = QtWidgets.QLabel('')
        self.predictResult.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        predict = QtWidgets.QHBoxLayout()
        predict.addWidget(QtWidgets.QLabel('Camera pixel row'))
        predict.addWidget(self.rowSpin)
        predict.addWidget(QtWidgets.QLabel('col'))
        predict.addWidget(self.colSpin)
        predict.addWidget(self.predictButton)
        predict.addWidget(self.predictResult, 1)
        self.resultBox = QtWidgets.QGroupBox('Result')
        result = QtWidgets.QVBoxLayout(self.resultBox)
        result.addWidget(self.verdict)
        result.addWidget(self.resultText, 1)
        result.addLayout(actions)
        result.addLayout(predict)
        self.setResultActionsEnabled(False)

        files = QtWidgets.QVBoxLayout()
        files.addWidget(self.cameraField)
        files.addWidget(self.scanField)
        files.addWidget(self.pairWarnings)
        inner = QtWidgets.QWidget()
        column = QtWidgets.QVBoxLayout(inner)
        column.addWidget(intro)
        column.addLayout(files)
        column.addLayout(options)
        column.addLayout(buttons)
        column.addWidget(self.resultBox, 1)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)

    @staticmethod
    def _spin(value, low, high, step, suffix='', special=None, decimals=2):
        spin = QtWidgets.QDoubleSpinBox()
        spin.setRange(low, high)
        spin.setDecimals(decimals)
        spin.setSingleStep(step)
        spin.setValue(value)
        if suffix:
            spin.setSuffix(suffix)
        if special:
            spin.setSpecialValueText(special)
        return spin

    def _toggleAdvanced(self, checked):
        self.advancedBox.setVisible(checked)
        self.advancedToggle.setText('Advanced ▾' if checked else 'Advanced ▸')

    # -- what the controller reads -------------------------------------------
    def field(self, role):
        return self.cameraField if role == 'camera' else self.scanField

    def options(self) -> dict:
        return {'calibration_id': self.idEdit.text().strip(), 'model': self.modelCombo.currentData(),
                'camera_threshold': self.cameraThreshold.value(), 'scan_threshold': self.scanThreshold.value(),
                'camera_sigma_px': self.cameraSigma.value(), 'scan_sigma_px': self.scanSigma.value(),
                'tolerance_um': self.tolerance.value(), 'max_residual_um': self.maxResidual.value(),
                'allow_mirror': self.allowMirror.isChecked(),
                'fast_axis_swept': self.fastAxis.currentData()}

    # -- what the controller shows -------------------------------------------
    def setPairWarnings(self, warnings):
        self.pairWarnings.setText('\n'.join(f'⚠ {w}' for w in warnings))
        self.pairWarnings.setVisible(bool(warnings))

    def setBusy(self, busy, text=''):
        self.fitButton.setEnabled(not busy)
        self.cancelButton.setEnabled(busy)
        self.busy.setVisible(busy)
        self.status.setText(text)
        for field in (self.cameraField, self.scanField):
            field.browseButton.setEnabled(not busy)
            field.detectorCombo.setEnabled(not busy)

    def setFitEnabled(self, enabled):
        self.fitButton.setEnabled(enabled)

    def setResult(self, accepted, verdict, text):
        self.verdict.setText(verdict)
        self.verdict.setStyleSheet(_OK_STYLE if accepted else _BAD_STYLE)
        self.verdict.setVisible(bool(verdict))
        self.resultText.setPlainText(text)
        self.setResultActionsEnabled(accepted)
        self.copyButton.setEnabled(bool(text))

    def clearResult(self):
        self.setResult(False, '', '')
        self.predictResult.setText('')

    def setResultActionsEnabled(self, enabled):
        for widget in (self.openFigureButton, self.openFolderButton, self.verifyButton, self.predictButton,
                       self.rowSpin, self.colSpin):
            widget.setEnabled(enabled)

    def setPrediction(self, text):
        self.predictResult.setText(text)

    # -- dialogs (overridden in tests) ---------------------------------------
    def askOpenFile(self, title, folder):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, title, str(folder or ''), FILE_FILTER)
        return path or None

    def askOpenFolder(self, title, folder):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, title, str(folder or ''))
        return path or None

    def askOverwrite(self, calibration_id, archived_id):
        """'replace', 'keep' (keep the old one as ``archived_id``) or 'cancel'."""
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Question)
        box.setWindowTitle('Calibration exists')
        box.setText(f'A calibration "{calibration_id}" exists already.')
        box.setInformativeText(f'Keep the old one as "{archived_id}", replace it, or cancel?')
        keep = box.addButton('Keep old as dated copy', QtWidgets.QMessageBox.AcceptRole)
        replace = box.addButton('Replace', QtWidgets.QMessageBox.DestructiveRole)
        box.addButton(QtWidgets.QMessageBox.Cancel)
        box.exec_()
        clicked = box.clickedButton()
        return 'keep' if clicked is keep else 'replace' if clicked is replace else 'cancel'

    def askVerificationFiles(self, folder):
        """Two paths (camera, scan) for the verification, or None."""
        camera = self.askOpenFile('Verification: camera recording', folder)
        if not camera:
            return None
        scan = self.askOpenFile('Verification: scan recording', folder)
        return (camera, scan) if scan else None

    def showMessage(self, title, text):
        QtWidgets.QMessageBox.information(self, title, text)

    def openPath(self, path):
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path)))

    def copyText(self, text):
        QtWidgets.QApplication.clipboard().setText(text)
