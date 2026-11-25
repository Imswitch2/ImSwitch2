"""The Calibrations window (Hardware → Calibrations…): view only.

Shows what ``CalibrationsController`` hands it -- per laser the calibrations
that match it and which one is active, the calibrations in the global store
that belong to other devices, and the details and curve of the selected one
-- and reports clicks. It decides nothing and never talks to a manager.
"""
from __future__ import annotations

from qtpy import QtCore, QtWidgets

from .basewidgets import Widget

_ROLE = QtCore.Qt.UserRole


class CalibrationsWidget(Widget):
    """Calibrations per laser: view, swap, import, export, annotate."""

    sigRefreshRequested = QtCore.Signal()
    sigItemSelected = QtCore.Signal(object)          # ('laser', name) | ('calibration', id, laser)
    sigActivateRequested = QtCore.Signal()
    sigRawRequested = QtCore.Signal()
    sigImportRequested = QtCore.Signal()
    sigImportReferencedRequested = QtCore.Signal()
    sigExportRequested = QtCore.Signal()
    sigSaveAnnotationsRequested = QtCore.Signal(str, str)   # (measuredAt, comment)
    sigSaveToSetupRequested = QtCore.Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setWindowTitle('Calibrations')
        self.resize(980, 640)

        self.setupBanner = QtWidgets.QLabel('')
        self.setupBanner.setWordWrap(True)
        self.setupBanner.setStyleSheet(
            'background-color: #fff4e5; color: #663c00; padding: 6px; border-radius: 3px;')
        self.setupBanner.hide()

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(['Laser / calibration', 'Status'])
        self.tree.setColumnWidth(0, 300)
        self.tree.setMinimumWidth(380)
        self.tree.itemSelectionChanged.connect(self._selectionChanged)

        self.detailsText = QtWidgets.QPlainTextEdit()
        self.detailsText.setReadOnly(True)
        self.detailsText.setMinimumHeight(170)

        self.plot = None
        try:
            import pyqtgraph as pg

            self.plot = pg.PlotWidget()
            self.plot.setMinimumHeight(160)
            self.plot.showGrid(x=True, y=True, alpha=0.3)
            self._curve = self.plot.plot([], [], pen=pg.mkPen('#1565c0', width=2),
                                         symbol='o', symbolSize=5, symbolBrush='#1565c0')
        except Exception:  # noqa: BLE001 - the details stay usable without a plot
            self.plot = None

        self.measuredAtEdit = QtWidgets.QLineEdit()
        self.measuredAtEdit.setPlaceholderText('e.g. back focal plane, sample plane, fibre output')
        self.commentEdit = QtWidgets.QLineEdit()
        self.commentEdit.setPlaceholderText('e.g. with ND 1.0 in the path')
        self.saveAnnotationsButton = QtWidgets.QPushButton('Save location and comment')
        self.saveAnnotationsButton.clicked.connect(
            lambda: self.sigSaveAnnotationsRequested.emit(
                self.measuredAtEdit.text(), self.commentEdit.text()))
        annotations = QtWidgets.QFormLayout()
        annotations.addRow('Measured at:', self.measuredAtEdit)
        annotations.addRow('Comment:', self.commentEdit)
        annotations.addRow('', self.saveAnnotationsButton)

        self.activateButton = QtWidgets.QPushButton('Activate')
        self.activateButton.clicked.connect(self.sigActivateRequested)
        self.rawButton = QtWidgets.QPushButton('Run on raw units')
        self.rawButton.clicked.connect(self.sigRawRequested)
        self.importButton = QtWidgets.QPushButton('Import CSV…')
        self.importButton.clicked.connect(self.sigImportRequested)
        self.importReferencedButton = QtWidgets.QPushButton('Import referenced file into store')
        self.importReferencedButton.setToolTip(
            'Copy the calibCsvPath file this laser uses into the global calibration store. '
            'The file and the setup stay as they are.')
        self.importReferencedButton.clicked.connect(self.sigImportReferencedRequested)
        self.exportButton = QtWidgets.QPushButton('Export CSV…')
        self.exportButton.setToolTip('Write the calibration as a calibCsvPath file with its '
                                     'header, e.g. to take it to another computer.')
        self.exportButton.clicked.connect(self.sigExportRequested)
        self.saveToSetupButton = QtWidgets.QPushButton('Save selection to setup file')
        self.saveToSetupButton.setToolTip(
            'Write which calibration each laser uses into the current setup file '
            '(managerProperties.calibrationId only).')
        self.saveToSetupButton.clicked.connect(self.sigSaveToSetupRequested)
        self.refreshButton = QtWidgets.QPushButton('Refresh')
        self.refreshButton.clicked.connect(self.sigRefreshRequested)

        laserActions = QtWidgets.QHBoxLayout()
        laserActions.addWidget(self.activateButton)
        laserActions.addWidget(self.rawButton)
        laserActions.addStretch(1)
        fileActions = QtWidgets.QHBoxLayout()
        fileActions.addWidget(self.importButton)
        fileActions.addWidget(self.importReferencedButton)
        fileActions.addWidget(self.exportButton)
        fileActions.addStretch(1)

        self.blockedLabel = QtWidgets.QLabel('')
        self.blockedLabel.setWordWrap(True)
        self.blockedLabel.setStyleSheet('color: #757575;')

        right = QtWidgets.QWidget()
        rightLayout = QtWidgets.QVBoxLayout(right)
        rightLayout.setContentsMargins(0, 0, 0, 0)
        rightLayout.addWidget(self.detailsText, 3)
        if self.plot is not None:
            rightLayout.addWidget(self.plot, 2)
        rightLayout.addLayout(annotations)
        rightLayout.addLayout(laserActions)
        rightLayout.addWidget(self.blockedLabel)
        rightLayout.addLayout(fileActions)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        splitter.addWidget(self.tree)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)

        self.storeLabel = QtWidgets.QLabel('')
        self.storeLabel.setStyleSheet('color: #757575;')
        self.storeLabel.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.statusLabel = QtWidgets.QLabel('')
        self.statusLabel.setWordWrap(True)

        bottom = QtWidgets.QHBoxLayout()
        bottom.addWidget(self.storeLabel, 1)
        bottom.addWidget(self.refreshButton)
        bottom.addWidget(self.saveToSetupButton)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.setupBanner)
        layout.addWidget(splitter, 1)
        layout.addWidget(self.statusLabel)
        layout.addLayout(bottom)

        self._restoring = False
        self._selected = None
        self._targetLaser = None

    # -- filled by the controller ------------------------------------------------

    def setModel(self, model):
        self._restoring = True
        try:
            selected = self._selected
            self.tree.clear()
            reselect = None
            for laser in model.get('lasers', ()):
                status = {'calibrated': 'calibrated', 'failed': 'CALIBRATION NOT LOADED',
                          'raw': f'raw units ({laser["units"]})'}.get(laser['state'], '')
                if laser.get('differs'):
                    status += ' · differs from setup file'
                wavelength = laser.get('wavelength')
                label = laser['name'] + (f'  ({wavelength:g} nm)' if wavelength else '')
                item = QtWidgets.QTreeWidgetItem([label, status])
                item.setData(0, _ROLE, ('laser', laser['name']))
                if laser['state'] == 'failed':
                    item.setForeground(1, QtCore.Qt.red)
                    item.setToolTip(1, laser.get('error', ''))
                self.tree.addTopLevelItem(item)
                if selected == ('laser', laser['name']):
                    reselect = item
                if laser.get('csvInForce') or (laser['state'] == 'failed' and laser.get('csvPath')):
                    csv = QtWidgets.QTreeWidgetItem(
                        [f'calibCsvPath file: {laser["csvPath"]}',
                         'active (not in the store)' if laser.get('csvInForce') else 'not loaded'])
                    csv.setData(0, _ROLE, ('csv', laser['name']))
                    csv.setToolTip(0, laser.get('csvResolved') or laser.get('csvPath', ''))
                    item.addChild(csv)
                    if selected == ('csv', laser['name']):
                        reselect = csv
                for entry in laser.get('entries', ()):
                    child = QtWidgets.QTreeWidgetItem(
                        [entry['title'], 'ACTIVE' if entry['active'] else ''])
                    child.setData(0, _ROLE, ('calibration', entry['id'], laser['name']))
                    child.setToolTip(0, f'{entry["id"]}\n{entry["path"]}')
                    item.addChild(child)
                    if selected == ('calibration', entry['id'], laser['name']):
                        reselect = child
                item.setExpanded(True)
            others = model.get('others', ())
            if others:
                group = QtWidgets.QTreeWidgetItem(['Other devices (not offered by default)', ''])
                group.setData(0, _ROLE, None)
                self.tree.addTopLevelItem(group)
                for entry in others:
                    wl = entry.get('wavelength')
                    child = QtWidgets.QTreeWidgetItem(
                        [entry['title'], f'made for {entry["madeFor"]}'
                                         + (f', {wl:g} nm' if wl else '')])
                    # The laser it would be used for is the one selected last.
                    child.setData(0, _ROLE, ('calibration', entry['id'], None))
                    child.setToolTip(0, f'{entry["id"]}\n{entry["path"]}')
                    group.addChild(child)
                    if selected and selected[0] == 'calibration' and selected[1] == entry['id']:
                        reselect = child
                group.setExpanded(True)
            unreadable = model.get('unreadable', ())
            if unreadable:
                group = QtWidgets.QTreeWidgetItem(['Unreadable files in the store', ''])
                group.setData(0, _ROLE, None)
                self.tree.addTopLevelItem(group)
                for entry in unreadable:
                    child = QtWidgets.QTreeWidgetItem([entry['path'], 'unreadable'])
                    child.setToolTip(0, entry['error'])
                    child.setData(0, _ROLE, None)
                    child.setForeground(1, QtCore.Qt.red)
                    group.addChild(child)
                group.setExpanded(True)
            self.storeLabel.setText(f'Store: {model.get("storeRoot", "")}    '
                                    f'Setup file: {model.get("setupFile", "")}')
            if reselect is not None:
                self.tree.setCurrentItem(reselect)
        finally:
            self._restoring = False

    def setDetails(self, info):
        self.detailsText.setPlainText('\n'.join(info.get('lines') or ['Select a laser or a '
                                                                      'calibration.']))
        self.measuredAtEdit.setText(info.get('measuredAt') or '')
        self.commentEdit.setText(info.get('comment') or '')
        editable = bool(info.get('editable'))
        for widget in (self.measuredAtEdit, self.commentEdit):
            widget.setEnabled(editable)
        if self.plot is not None:
            plot = info.get('plot')
            if plot:
                order = sorted(range(len(plot['drive'])), key=lambda i: plot['drive'][i])
                self._curve.setData([plot['drive'][i] for i in order],
                                    [plot['power'][i] for i in order])
                self.plot.setLabel('bottom', 'drive', units=plot.get('driveUnit') or None)
                self.plot.setLabel('left', 'power', units=None)
                self.plot.setTitle(f'power [{plot.get("powerUnit") or "relative"}] vs drive')
            else:
                self._curve.setData([], [])
                self.plot.setTitle('')

    def setActions(self, actions):
        self.activateButton.setEnabled(bool(actions.get('activate')))
        self.activateButton.setText(actions.get('activateLabel', 'Activate'))
        self.rawButton.setEnabled(bool(actions.get('raw')))
        self.rawButton.setText(actions.get('rawLabel', 'Run on raw units'))
        self.exportButton.setEnabled(bool(actions.get('export')))
        self.importReferencedButton.setEnabled(bool(actions.get('importReferenced')))
        self.saveAnnotationsButton.setEnabled(bool(actions.get('saveAnnotations')))
        self.saveToSetupButton.setEnabled(bool(actions.get('saveToSetup')))
        blocked = actions.get('blockedReason') or ''
        self.blockedLabel.setText(blocked)
        self.blockedLabel.setVisible(bool(blocked))

    def setStatus(self, text, error=False):
        self.statusLabel.setText(text)
        self.statusLabel.setStyleSheet('color: #b3261e;' if error else '')

    def setSetupBanner(self, text):
        self.setupBanner.setText(text)
        self.setupBanner.setVisible(bool(text))

    # -- questions (patched in tests) ------------------------------------------------

    def confirm(self, title, text) -> bool:
        answer = QtWidgets.QMessageBox.question(
            self, title, text, QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        return answer == QtWidgets.QMessageBox.Yes

    def askOpenFile(self) -> str:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, 'Import calibration CSV', '', 'Calibration files (*.csv *.txt *.dat);;'
                                                'All files (*)')
        return path

    def askSaveFile(self, defaultName) -> str:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, 'Export calibration CSV', defaultName, 'CSV (*.csv);;All files (*)')
        return path

    def askLaser(self, question, names, default=None):
        names = list(names)
        if not names:
            return None
        index = names.index(default) if default in names else 0
        name, ok = QtWidgets.QInputDialog.getItem(self, 'Which laser?', question, names,
                                                  index, False)
        return name if ok else None

    # -- internals ----------------------------------------------------------------------

    def _selectionChanged(self):
        if self._restoring:
            return
        items = self.tree.selectedItems()
        data = items[0].data(0, _ROLE) if items else None
        data = tuple(data) if data else None
        if data and data[0] in ('laser', 'csv'):
            self._targetLaser = data[1]
        elif data and data[0] == 'calibration':
            if data[2] is None:
                # A calibration of another device: offered for the laser
                # selected last (the details say for which).
                data = (data[0], data[1], self._targetLaser)
            else:
                self._targetLaser = data[2]
        self._selected = data
        self.sigItemSelected.emit(self._selected)
