from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np
import pyqtgraph as pg
from qtpy import QtCore, QtGui, QtWidgets

from imswitch.imcommon.view.guitools.imagetools import bestLevels
from imswitch.imcontrol.model.workflows.spiral import SERPENTINE, SPIRAL

from imswitch.imcontrol.view import guitools
from .basewidgets import Widget

#: Acquisition timing models offered by the widget. The mechanism behind
#: 'triggered' depends on the detector, which the controller works out.
MODE_FREE_RUNNING = 'free-running'
MODE_TRIGGERED = 'triggered'

#: Auto contrast reads a strided subsample of the overview, strided the way
#: pyqtgraph's own histogram is (``ImageItem.getHistogram``'s default
#: ``targetImageSize``): a mosaic of 2048 px tiles runs to hundreds of
#: megapixels, and the levels are a statistic of the whole, not of each pixel.
_AUTO_CONTRAST_SAMPLES_PER_SIDE = 200
#: The range ``StitchedImage.get_overview`` returns values in, and so the
#: levels when an overview gives nothing to fit (blank, or uniform).
_OVERVIEW_RANGE = (0.0, 1.0)

#: What a click on the overview does. The overview maps pixels back to stage
#: positions, so a click can either go there or remember the place.
CLICK_NONE = 'none'
CLICK_NAVIGATE = 'navigate'
CLICK_ADD_TARGET = 'add-target'

_TARGET_FILE_FILTER = 'Target list (*.json);;All files (*)'


class TiltCalibrationDetailsDialog(QtWidgets.QDialog):
    """Resizable inspection of a saved calibration and its existing PNG fit."""

    def __init__(self, fit_path, data, name, rename_callback, parent=None):
        super().__init__(parent)
        self._rename_callback = rename_callback
        self.setWindowTitle('Stage tilt calibration')
        self.resize(900, 650)
        self.setMinimumSize(620, 460)
        fit_path = Path(fit_path)
        layout = QtWidgets.QVBoxLayout(self)

        header = QtWidgets.QHBoxLayout()
        self.nameLabel = QtWidgets.QLabel(name)
        title_font = self.nameLabel.font()
        title_font.setBold(True)
        self.nameLabel.setFont(title_font)
        self.nameLabel.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        header.addWidget(self.nameLabel, 1)
        rename_button = QtWidgets.QPushButton('Rename…')
        rename_button.clicked.connect(self._rename)
        header.addWidget(rename_button)
        layout.addLayout(header)

        fit = data.get('fit') or {}
        rms = fit.get('rms_residual_um')
        z_range = fit.get('measured_z_range_um')
        points = data.get('n_valid_tiles', fit.get('n_fit_points', '?'))
        total = data.get('n_recorded_tiles', data.get('n_requested_tiles', '?'))
        summary = QtWidgets.QLabel(
            f'Fit RMS: {float(rms)*1000:.1f} nm' if rms is not None else 'Fit RMS: —'
        )
        if z_range is not None:
            summary.setText(summary.text() + f'     Z range: {float(z_range):.3f} µm')
        summary.setText(summary.text() + f'     Valid readings: {points}/{total}')
        drift = data.get('origin_drift') or {}
        if drift.get('available'):
            summary.setText(summary.text() +
                            f"     Origin drift: {float(drift['delta_z_um'])*1000:+.1f} nm")
        elif 'origin_drift' in data:
            summary.setText(summary.text() + '     Origin drift: unavailable')
        bidir = data.get('bidirectional_diagnostics') or {}
        if bidir:
            difference = bidir.get('rms_reverse_minus_forward_um')
            if difference is not None:
                summary.setText(summary.text() +
                                f'     Pass difference RMS: {float(difference)*1000:.1f} nm')
        summary.setWordWrap(True)
        layout.addWidget(summary)

        plot_path = fit_path.with_name('tilt_fit.png')
        self.plot = QtWidgets.QLabel()
        self.plot.setAlignment(QtCore.Qt.AlignCenter)
        self.plot.setSizePolicy(
            QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Ignored
        )
        self._originalPixmap = QtGui.QPixmap(str(plot_path))
        if self._originalPixmap.isNull():
            self.plot.setText('No saved fit visualization is available.')
        self.plotArea = QtWidgets.QScrollArea()
        self.plotArea.setWidgetResizable(True)
        self.plotArea.setWidget(self.plot)
        layout.addWidget(self.plotArea, 1)

        bounds = fit.get('valid_xy_bounds_um', {})
        form = QtWidgets.QFormLayout()
        values = (
            ('Z actuator:', data.get('focus_positioner', '—')),
            ('XY positioner:', data.get('xy_positioner', '—')),
            ('X / Y slope:',
             f"{float(fit.get('slope_x_um_per_um', 0))*1000:+.3f} / "
             f"{float(fit.get('slope_y_um_per_um', 0))*1000:+.3f} nm/µm"),
            ('Calibrated XY area:',
             f"X {bounds.get('x', '—')}, Y {bounds.get('y', '—')} µm"),
            ('Sampling grid:', data.get('sampling_grid_xy') or '—'),
            ('Area span (µm):', data.get('area_span_um') or '—'),
            ('Drift interval (s):',
             round(float((data.get('origin_drift') or {}).get('duration_s')), 1)
             if (data.get('origin_drift') or {}).get('duration_s') is not None else '—'),
            ('Recording passes:', 2 if data.get('bidirectional') else 1),
            ('Recorded:', data.get('created_utc', '—')),
        )
        for label, value in values:
            item = QtWidgets.QLabel(str(value))
            item.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            form.addRow(label, item)
        path_label = QtWidgets.QLabel(str(fit_path))
        path_label.setToolTip(str(fit_path))
        path_label.setWordWrap(True)
        path_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        form.addRow('File:', path_label)
        layout.addLayout(form)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._resizePlot()

    def _rename(self):
        new_name, accepted = QtWidgets.QInputDialog.getText(
            self, 'Rename tilt calibration', 'Calibration name:',
            QtWidgets.QLineEdit.Normal, self.nameLabel.text(),
        )
        if not accepted:
            return
        try:
            displayed = self._rename_callback(new_name)
        except (OSError, ValueError, TypeError) as error:
            QtWidgets.QMessageBox.warning(self, 'Cannot rename calibration', str(error))
            return
        self.nameLabel.setText(displayed)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._resizePlot()

    def _resizePlot(self):
        if hasattr(self, 'plotArea') and not self._originalPixmap.isNull():
            width = max(1, self.plotArea.viewport().width() - 4)
            self.plot.setPixmap(self._originalPixmap.scaled(
                width, max(1, self.plotArea.viewport().height()),
                QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation,
            ))


class TilingWidget(Widget):
    """Widget for controlling spiral tiling scans."""

    sigStartTiling = QtCore.Signal()
    sigStopTiling = QtCore.Signal()
    sigParamsChanged = QtCore.Signal()
    sigClickOnOverview = QtCore.Signal(int, int)  # row, col in stitched canvas
    sigModeChanged = QtCore.Signal(str)
    sigDetectorChanged = QtCore.Signal()
    # Target list actions. Each carries a target id, never a row index: the
    # controller owns the list and the table is only a view of it.
    sigGoToTarget = QtCore.Signal(int)
    sigRemoveTarget = QtCore.Signal(int)
    sigClearTargets = QtCore.Signal()
    sigTargetEnabledChanged = QtCore.Signal(int, bool)
    sigSaveTargets = QtCore.Signal(str)  # path
    sigLoadTargets = QtCore.Signal(str)  # path
    sigLoadTiltCalibration = QtCore.Signal()
    sigShowTiltCalibration = QtCore.Signal()
    sigOpenTiltCalibrationFolder = QtCore.Signal()
    sigSetDefaultTiltCalibration = QtCore.Signal()
    sigTiltRecordModeSelected = QtCore.Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # One column of labelled blocks over a full-width overview. The
        # controls are grouped by the question they answer — how far to scan,
        # how to acquire, how to lay the tiles down, how to draw them — so a
        # setting can be found by what it does rather than by where it landed.
        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        self.setLayout(layout)

        layout.addWidget(self._buildScanGroup())
        layout.addWidget(self._buildAcquisitionGroup())
        layout.addWidget(self._buildTiltGroup())
        layout.addWidget(self._buildAlignmentGroup())
        layout.addWidget(self._buildDisplayGroup())
        layout.addLayout(self._buildRunRow())
        layout.addWidget(self._buildStatusBlock())
        # The overview is the point of the widget, so it takes every pixel the
        # controls above it do not need, and all of the width.
        layout.addWidget(self._buildOverview(), 1)
        layout.addWidget(self._buildTargetsGroup())

        # Wire signals
        self.startButton.clicked.connect(self.sigStartTiling)
        self.stopButton.clicked.connect(self.sigStopTiling)
        self.nTilesXSpinbox.valueChanged.connect(self.sigParamsChanged)
        self.nTilesYSpinbox.valueChanged.connect(self.sigParamsChanged)
        self.patternCombo.currentIndexChanged.connect(self.sigParamsChanged)
        self.tileStepSpinbox.valueChanged.connect(self.sigParamsChanged)
        self.blendOverlapsCheck.stateChanged.connect(self.sigParamsChanged)
        self.intensityCorrectionCheck.stateChanged.connect(self.sigParamsChanged)
        self.buildOverviewCheck.stateChanged.connect(self.sigParamsChanged)
        self.buildOverviewCheck.toggled.connect(self._updateOverviewAvailability)
        self.settleTimeSpinbox.valueChanged.connect(self.sigParamsChanged)
        self.registerTilesCheck.stateChanged.connect(self.sigParamsChanged)
        self.saveTilesCheck.stateChanged.connect(self.sigParamsChanged)
        self.tiltModeCombo.currentIndexChanged.connect(self._updateTiltMode)
        self.tiltCalibrationOnlyCheck.toggled.connect(self._updateTiltSamplingEnabled)
        self.tiltCalibrationOnlyCheck.toggled.connect(self.sigParamsChanged)
        self.tiltBidirectionalCheck.toggled.connect(self.sigParamsChanged)
        self.tiltSampleXSpin.valueChanged.connect(self.sigParamsChanged)
        self.tiltSampleYSpin.valueChanged.connect(self.sigParamsChanged)
        self.tiltModeCombo.currentIndexChanged.connect(self.sigParamsChanged)
        self.tiltCalibrationButton.clicked.connect(
            lambda *_: self.sigLoadTiltCalibration.emit()
        )
        self.tiltSeeAction.triggered.connect(
            lambda *_: self.sigShowTiltCalibration.emit()
        )
        self.tiltOpenFolderAction.triggered.connect(
            lambda *_: self.sigOpenTiltCalibrationFolder.emit()
        )
        self.tiltDefaultAction.triggered.connect(
            lambda *_: self.sigSetDefaultTiltCalibration.emit()
        )
        self.modeCombo.currentIndexChanged.connect(self._onModeChanged)
        self.scanSourceCombo.currentIndexChanged.connect(self.sigParamsChanged)
        self.detectorCombo.currentIndexChanged.connect(self.sigParamsChanged)
        self.detectorCombo.currentIndexChanged.connect(self.sigDetectorChanged)
        self.flipXCheck.stateChanged.connect(self.sigParamsChanged)
        self.flipYCheck.stateChanged.connect(self.sigParamsChanged)
        self.swapAxesCheck.stateChanged.connect(self.sigParamsChanged)
        self.overviewItem.scene().sigMouseClicked.connect(self._onSceneClicked)
        # Display only: contrast changes nothing the run records.
        self.autoContrastCheck.toggled.connect(self._onAutoContrastToggled)
        self.overviewHist.sigLevelsChanged.connect(self._onOverviewLevelsChanged)

        self.goToTargetButton.clicked.connect(self._onGoToTarget)
        self.removeTargetButton.clicked.connect(self._onRemoveTarget)
        self.clearTargetsButton.clicked.connect(self.sigClearTargets)
        self.saveTargetsButton.clicked.connect(self._onSaveTargets)
        self.loadTargetsButton.clicked.connect(self._onLoadTargets)
        self.targetsTable.itemSelectionChanged.connect(
            self._onTargetSelectionChanged
        )
        self.targetsTable.itemChanged.connect(self._onTargetItemChanged)
        self._updateOverviewAvailability(self.buildOverviewCheck.isChecked())
        self._updateTiltMode()
        self._updateTiltSamplingEnabled()
        self._updateTiltCalibrationLabel()

    # ------------------------------------------------------------------
    # Layout blocks
    # ------------------------------------------------------------------

    @staticmethod
    def _group(title: str, inner: QtWidgets.QLayout) -> QtWidgets.QGroupBox:
        box = QtWidgets.QGroupBox(title)
        inner.setContentsMargins(8, 4, 8, 6)
        inner.setSpacing(6)
        box.setLayout(inner)
        return box

    def _buildScanGroup(self) -> QtWidgets.QGroupBox:
        """How much ground to cover, and how settled the stage must be."""
        grid = QtWidgets.QGridLayout()

        grid.addWidget(QtWidgets.QLabel('Tiles X x Y:'), 0, 0)
        tilesRow = QtWidgets.QHBoxLayout()
        self.nTilesXSpinbox = QtWidgets.QSpinBox()
        self.nTilesXSpinbox.setMinimum(1)
        self.nTilesXSpinbox.setMaximum(1000)
        self.nTilesXSpinbox.setValue(3)
        tilesRow.addWidget(self.nTilesXSpinbox)
        self.nTilesYSpinbox = QtWidgets.QSpinBox()
        self.nTilesYSpinbox.setMinimum(1)
        self.nTilesYSpinbox.setMaximum(1000)
        self.nTilesYSpinbox.setValue(3)
        tilesRow.addWidget(self.nTilesYSpinbox)
        self.squareLockCheck = QtWidgets.QCheckBox('Square')
        self.squareLockCheck.setChecked(True)
        self.squareLockCheck.setToolTip(
            'Keep the two counts equal. Unlock for a rectangular area.'
        )
        tilesRow.addWidget(self.squareLockCheck)
        grid.addLayout(tilesRow, 0, 1)
        # The lock ties the two spinboxes rather than hiding one, so the grid
        # stays readable as N x N and unlocking never changes the area.
        self.nTilesXSpinbox.valueChanged.connect(self._matchTileCounts)
        self.nTilesYSpinbox.valueChanged.connect(self._matchTileCounts)
        self.squareLockCheck.toggled.connect(self._onSquareLockToggled)

        grid.addWidget(QtWidgets.QLabel('Step (µm):'), 0, 2)
        self.tileStepSpinbox = QtWidgets.QDoubleSpinBox()
        self.tileStepSpinbox.setMinimum(1.0)
        self.tileStepSpinbox.setMaximum(50000.0)
        self.tileStepSpinbox.setSingleStep(10.0)
        self.tileStepSpinbox.setDecimals(1)
        self.tileStepSpinbox.setValue(100.0)
        grid.addWidget(self.tileStepSpinbox, 0, 3)

        # The knob to reach for when the mosaic does not line up: too short and
        # tiles are grabbed while the stage is still ringing.
        grid.addWidget(QtWidgets.QLabel('Settle (ms):'), 1, 0)
        self.settleTimeSpinbox = QtWidgets.QDoubleSpinBox()
        self.settleTimeSpinbox.setMinimum(0.0)
        self.settleTimeSpinbox.setMaximum(10000.0)
        self.settleTimeSpinbox.setSingleStep(25.0)
        self.settleTimeSpinbox.setDecimals(0)
        self.settleTimeSpinbox.setValue(150.0)
        self.settleTimeSpinbox.setToolTip(
            'Wait after each stage move before capturing a tile.\n'
            'Increase this first if tiles do not overlap cleanly.'
        )
        grid.addWidget(self.settleTimeSpinbox, 1, 1)

        grid.addWidget(QtWidgets.QLabel('Pattern:'), 1, 2)
        self.patternCombo = QtWidgets.QComboBox()
        self.patternCombo.addItem('Spiral (centred)', SPIRAL)
        self.patternCombo.addItem('Serpentine (from here)', SERPENTINE)
        self.patternCombo.setToolTip(
            'The order tiles are visited in, and where the grid sits.\n\n'
            'Spiral grows outward from the current position, so the mosaic is\n'
            'centred here and a run stopped early still leaves a filled,\n'
            'centred area. Choose it when the extent is open-ended.\n\n'
            'Serpentine rasters from the current position in +X and +Y, one\n'
            'tile step at a time with no jumps, so every settle follows a\n'
            'single step. Choose it when you know the area and will finish it:\n'
            'frame the stage at one corner of what you want.'
        )
        grid.addWidget(self.patternCombo, 1, 3)
        grid.setColumnStretch(4, 1)
        return self._group('Scan', grid)

    def _buildAcquisitionGroup(self) -> QtWidgets.QGroupBox:
        """Where each tile's signal comes from, and on whose clock."""
        grid = QtWidgets.QGridLayout()

        # The operator chooses the timing model; which mechanism implements it
        # follows from the detector, which the controller works out.
        grid.addWidget(QtWidgets.QLabel('Mode:'), 0, 0)
        self.modeCombo = QtWidgets.QComboBox()
        self.modeCombo.addItem('Free-running', MODE_FREE_RUNNING)
        self.modeCombo.addItem('Triggered', MODE_TRIGGERED)
        self.modeCombo.setToolTip(
            'Free-running: grab a frame from a continuously running camera.\n'
            'Triggered: run one scan per tile — this drives a scanned\n'
            'detector (APD/PMT) directly, and clocks a camera wired to the\n'
            'scan trigger.'
        )
        grid.addWidget(self.modeCombo, 0, 1)

        self.scanSourceLabel = QtWidgets.QLabel('Scan:')
        self.scanSourceCombo = QtWidgets.QComboBox()
        self.scanSourceCombo.setToolTip(
            'Which scan controller to trigger. Only shown when the setup has\n'
            'more than one; the scan itself is configured in its own widget.'
        )
        grid.addWidget(self.scanSourceLabel, 0, 2)
        grid.addWidget(self.scanSourceCombo, 0, 3)
        self.scanSourceLabel.setVisible(False)
        self.scanSourceCombo.setVisible(False)

        self.detectorLabel = QtWidgets.QLabel('Align on:')
        self.detectorCombo = QtWidgets.QComboBox()
        self.detectorCombo.setToolTip(
            'Which detector the mosaic is built and aligned from. The setup\n'
            'file chooses the default; this overrides it for the session.\n'
            'Only shown when the setup has more than one to pick from.\n'
            '\n'
            'This is not the only detector saved: a run also saves whatever\n'
            'the Recording widget is set to capture, at every tile position,\n'
            'so long as each one declares how its pixels relate to this one.\n'
            '\n'
            'Changing it discards the current overview: a different detector\n'
            'means a different pixel size, so the existing mosaic no longer\n'
            'maps to stage coordinates.'
        )
        grid.addWidget(self.detectorLabel, 1, 0)
        grid.addWidget(self.detectorCombo, 1, 1)
        self.detectorLabel.setVisible(False)
        self.detectorCombo.setVisible(False)

        self.saveTilesCheck = QtWidgets.QCheckBox('Save tiles')
        self.saveTilesCheck.setToolTip(
            'Save each tile as an OME image as it is acquired, plus the\n'
            'TileConfiguration.txt for Fiji/BigStitcher and a tiles.json manifest.\n'
            'A stitched mosaic is saved only when Build mosaic preview is enabled.\n'
            'Every tile carries its stage position in OME metadata.'
        )
        grid.addWidget(self.saveTilesCheck, 1, 2, 1, 2)
        self.focusPauseCheck = QtWidgets.QCheckBox('Pause focus lock during moves')
        self.focusPauseCheck.setChecked(False)
        self.focusPauseCheck.setToolTip(
            'Free-running tiling: pause the focus lock (Z held) for each XY move,\n'
            'then relock to the kept setpoint in bounded steps before the tile is\n'
            'taken. A tile whose relock fails is taken at the last good Z and\n'
            'marked in tiles.json; the next tile tries again. Off: the lock runs\n'
            'through the moves as before.'
        )
        grid.addWidget(self.focusPauseCheck, 2, 2, 1, 2)
        grid.setColumnStretch(4, 1)
        return self._group('Acquisition', grid)

    def _buildTiltGroup(self) -> QtWidgets.QGroupBox:
        """Two-row stage tilt controls; the Z identity is read-only here."""
        grid = QtWidgets.QGridLayout()
        grid.addWidget(QtWidgets.QLabel('Mode:'), 0, 0)
        self.tiltModeCombo = QtWidgets.QComboBox()
        self.tiltModeCombo.addItem('Off', 'off')
        self.tiltModeCombo.addItem('Record', 'record')
        self.tiltModeCombo.addItem('Apply', 'apply')
        self.tiltModeCombo.setToolTip(
            'Off: regular tiling. Record: measure the stage tilt using the current\n'
            'Focus Lock Z actuator. Apply: compensate with the actuator stored\n'
            'in the selected calibration.'
        )
        grid.addWidget(self.tiltModeCombo, 0, 1)
        self.precheckTiltCheck = QtWidgets.QCheckBox('Pre-check')
        self.precheckTiltCheck.setToolTip(
            'Optional full-route check of calibrated XY limits, Z output limits\n'
            'and actuator reference before moving. Basic validation always applies.'
        )
        grid.addWidget(self.precheckTiltCheck, 0, 3, QtCore.Qt.AlignRight)

        self.tiltRecordRow = QtWidgets.QWidget()
        record_row = QtWidgets.QHBoxLayout(self.tiltRecordRow)
        record_row.setContentsMargins(0, 0, 0, 0)
        record_row.setSpacing(6)
        record_row.addWidget(QtWidgets.QLabel('Name:'))
        self.tiltNameEdit = QtWidgets.QLineEdit()
        self.tiltNameEdit.setMaxLength(160)
        self.tiltNameEdit.setPlaceholderText('Optional calibration name')
        self.tiltNameEdit.setToolTip('Name for the new calibration (editable later).')
        record_row.addWidget(self.tiltNameEdit, 1)
        record_row.addWidget(QtWidgets.QLabel('Z actuator:'))
        self.tiltRecordZLabel = QtWidgets.QLabel('—')
        self.tiltRecordZLabel.setToolTip('Currently selected Focus Lock Z actuator')
        record_row.addWidget(self.tiltRecordZLabel)
        self.tiltRecordActionsButton = QtWidgets.QToolButton()
        self.tiltRecordActionsButton.setText('…')
        self.tiltRecordActionsButton.setToolTip('Actions for the last recorded calibration')
        self.tiltRecordActionsButton.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        record_row.addWidget(self.tiltRecordActionsButton)
        grid.addWidget(self.tiltRecordRow, 1, 0, 1, 4)

        self.tiltOptionsRow = QtWidgets.QWidget()
        options_row = QtWidgets.QHBoxLayout(self.tiltOptionsRow)
        options_row.setContentsMargins(0, 0, 0, 0)
        options_row.setSpacing(8)
        self.tiltCalibrationOnlyCheck = QtWidgets.QCheckBox('Calibration only')
        self.tiltCalibrationOnlyCheck.setChecked(True)
        self.tiltCalibrationOnlyCheck.setToolTip(
            'Visit calibration points without arming the tile camera, saving\n'
            'images, registering tiles or constructing a mosaic.'
        )
        options_row.addWidget(self.tiltCalibrationOnlyCheck)
        self.tiltBidirectionalCheck = QtWidgets.QCheckBox('Bidirectional')
        self.tiltBidirectionalCheck.setToolTip(
            'Retrace the same positions in reverse order, starting from the\n'
            'last forward position. Pair and average Z readings for the plane fit.'
        )
        options_row.addWidget(self.tiltBidirectionalCheck)
        options_row.addWidget(QtWidgets.QLabel('Samples:'))
        self.tiltSampleXSpin = QtWidgets.QSpinBox()
        self.tiltSampleYSpin = QtWidgets.QSpinBox()
        for spin in (self.tiltSampleXSpin, self.tiltSampleYSpin):
            spin.setRange(2, 1000)
            spin.setValue(5)
            spin.setMaximumWidth(64)
            spin.setToolTip(
                'Sampling positions over the full XY area defined by Scan.\n'
                'For example, a 21×21 area sampled at 5×5 still covers the\n'
                'same edge-to-edge span, but needs only 25 readings per pass.'
            )
        options_row.addWidget(self.tiltSampleXSpin)
        options_row.addWidget(QtWidgets.QLabel('×'))
        options_row.addWidget(self.tiltSampleYSpin)
        options_row.addStretch(1)
        grid.addWidget(self.tiltOptionsRow, 2, 0, 1, 4)

        self.tiltApplyRow = QtWidgets.QWidget()
        apply_row = QtWidgets.QHBoxLayout(self.tiltApplyRow)
        apply_row.setContentsMargins(0, 0, 0, 0)
        apply_row.setSpacing(6)
        apply_row.addWidget(QtWidgets.QLabel('Calibration:'))
        self.tiltCalibrationButton = QtWidgets.QPushButton('Choose calibration…')
        self.tiltCalibrationButton.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed
        )
        self.tiltCalibrationButton.setToolTip('Click to select a tilt_fit.json file')
        self.tiltCalibrationButton.setMaximumWidth(280)
        apply_row.addWidget(self.tiltCalibrationButton, 1)
        apply_row.addWidget(QtWidgets.QLabel('Z actuator:'))
        self.tiltApplyZLabel = QtWidgets.QLabel('—')
        apply_row.addWidget(self.tiltApplyZLabel)
        self.tiltActionsButton = QtWidgets.QToolButton()
        self.tiltActionsButton.setText('…')
        self.tiltActionsButton.setToolTip('Calibration actions')
        self.tiltActionsButton.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.tiltActionsMenu = QtWidgets.QMenu(self.tiltActionsButton)
        self.tiltSeeAction = self.tiltActionsMenu.addAction('See calibration')
        self.tiltOpenFolderAction = self.tiltActionsMenu.addAction('Open calibration folder')
        self.tiltActionsMenu.addSeparator()
        self.tiltDefaultAction = self.tiltActionsMenu.addAction('Set as default')
        self.tiltActionsButton.setMenu(self.tiltActionsMenu)
        # Both buttons use the same actions; the controller chooses the
        # correct file based on the current Stage tilt mode.
        self.tiltRecordActionsButton.setMenu(self.tiltActionsMenu)
        apply_row.addWidget(self.tiltActionsButton)
        grid.addWidget(self.tiltApplyRow, 1, 0, 1, 4)

        self._tiltCalibrationPath = ''
        self._tiltCalibrationName = ''
        self._tiltDefaultPath = ''
        self._tiltLastRecordedPath = ''
        grid.setColumnStretch(2, 1)
        return self._group('Stage tilt', grid)

    def _updateTiltMode(self, *_):
        mode = self.tiltModeCombo.currentData()
        self.tiltRecordRow.setVisible(mode == 'record')
        self.tiltOptionsRow.setVisible(mode == 'record')
        self.tiltApplyRow.setVisible(mode == 'apply')
        self.precheckTiltCheck.setVisible(mode == 'apply')
        self.precheckTiltCheck.setEnabled(
            mode == 'apply' and self.startButton.isEnabled()
        )
        self._updateTiltSamplingEnabled()
        self._updateTiltActionMenu()
        if mode == 'record':
            self.sigTiltRecordModeSelected.emit()

    def _updateTiltSamplingEnabled(self, *_):
        enabled = (self.tiltCalibrationOnlyCheck.isChecked()
                   and self.tiltModeCombo.currentData() == 'record'
                   and self.startButton.isEnabled())
        for spin in (self.tiltSampleXSpin, self.tiltSampleYSpin):
            spin.setEnabled(enabled)

    def getTiltCalibrationOnly(self) -> bool:
        return self.tiltCalibrationOnlyCheck.isChecked()

    def getTiltBidirectional(self) -> bool:
        return self.tiltBidirectionalCheck.isChecked()

    def getTiltSamplingCounts(self):
        return self.tiltSampleXSpin.value(), self.tiltSampleYSpin.value()

    def setTiltRecordZActuator(self, actuator: str):
        self.tiltRecordZLabel.setText(actuator or 'Unavailable')

    def getTiltRecordName(self) -> str:
        return self.tiltNameEdit.text().strip()

    def setDefaultTiltCalibration(self, path):
        self._tiltDefaultPath = str(Path(path).resolve()) if path else ''
        self._updateTiltCalibrationLabel()

    def getLastRecordedTiltCalibrationPath(self) -> str:
        return self._tiltLastRecordedPath

    def setLastRecordedTiltCalibration(self, path):
        """Expose the latest recording in Record mode without switching modes."""
        self._tiltLastRecordedPath = str(Path(path).resolve()) if path else ''
        if self._tiltLastRecordedPath:
            self.tiltRecordActionsButton.setToolTip(
                f'Last recording: {self._tiltLastRecordedPath}'
            )
        self._updateTiltActionMenu()

    def getTiltActionCalibrationPath(self) -> str:
        mode = self.tiltModeCombo.currentData()
        if mode == 'record':
            return self._tiltLastRecordedPath
        if mode == 'apply':
            return self._tiltCalibrationPath
        return ''

    def _updateTiltActionMenu(self):
        """The shared actions target the file shown by the current mode."""
        active_path = self.getTiltActionCalibrationPath()
        idle = self.startButton.isEnabled()
        self.tiltRecordActionsButton.setVisible(bool(self._tiltLastRecordedPath))
        self.tiltRecordActionsButton.setEnabled(
            idle and bool(self._tiltLastRecordedPath)
        )
        self.tiltActionsButton.setEnabled(idle and bool(self._tiltCalibrationPath))
        self.tiltSeeAction.setEnabled(idle and bool(active_path))
        self.tiltOpenFolderAction.setEnabled(idle and bool(active_path))
        self.tiltDefaultAction.setEnabled(
            idle and bool(active_path) and active_path != self._tiltDefaultPath
        )

    def setTiltCalibrationDisplayName(self, path, name: str):
        """Refresh the visible label after a rename; leave the fit unchanged."""
        if self._tiltCalibrationPath == str(Path(path).resolve()):
            self._tiltCalibrationName = name
            self._updateTiltCalibrationLabel()

    def _updateTiltCalibrationLabel(self):
        if not self._tiltCalibrationPath:
            self.tiltCalibrationButton.setText('Choose calibration…')
        else:
            default = self._tiltCalibrationPath == self._tiltDefaultPath
            name = self._tiltCalibrationName or Path(self._tiltCalibrationPath).parent.name
            label = ('★ ' if default else '') + name
            self.tiltCalibrationButton.setText(
                self.tiltCalibrationButton.fontMetrics().elidedText(
                    label, QtCore.Qt.ElideRight, 260
                )
            )
            self.tiltCalibrationButton.setToolTip(
                f'{name}\n{self._tiltCalibrationPath}\nClick to choose a different calibration.'
            )
        self._updateTiltActionMenu()

    def showEvent(self, event):
        super().showEvent(event)
        if self.tiltModeCombo.currentData() == 'record':
            self.sigTiltRecordModeSelected.emit()

    def _buildAlignmentGroup(self) -> QtWidgets.QGroupBox:
        """Where each tile is laid down, and which way the mosaic grows."""
        outer = QtWidgets.QVBoxLayout()

        row = QtWidgets.QHBoxLayout()
        self.registerTilesCheck = QtWidgets.QCheckBox('Align tiles')
        self.registerTilesCheck.setToolTip(
            'Refine each tile by cross-correlating it with its already-placed\n'
            'neighbours instead of trusting the stage position alone.\n'
            'Also reports how far off the commanded positions were.'
        )
        row.addWidget(self.registerTilesCheck)

        row.addStretch(1)
        outer.addLayout(row)

        # Which way the overview grows depends on the camera mounting and the
        # stage sign convention, so it has to be settable per rig.
        orientation = QtWidgets.QHBoxLayout()
        orientation.addWidget(QtWidgets.QLabel('Orientation:'))
        self.flipXCheck = QtWidgets.QCheckBox('Flip X')
        self.flipXCheck.setToolTip(
            'Mirror the mosaic left/right.\n'
            'Use when tiles build in the opposite direction to the stage.'
        )
        self.flipYCheck = QtWidgets.QCheckBox('Flip Y')
        self.flipYCheck.setToolTip('Mirror the mosaic up/down.')
        self.swapAxesCheck = QtWidgets.QCheckBox('Swap X/Y')
        self.swapAxesCheck.setToolTip(
            'Exchange the mosaic axes, for a camera mounted at 90° to the stage.'
        )
        for check in (self.flipXCheck, self.flipYCheck, self.swapAxesCheck):
            orientation.addWidget(check)
        orientation.addStretch(1)
        outer.addLayout(orientation)
        return self._group('Alignment', outer)

    def _buildDisplayGroup(self) -> QtWidgets.QGroupBox:
        """How the placed tiles are drawn into one image."""
        row = QtWidgets.QHBoxLayout()
        self.buildOverviewCheck = QtWidgets.QCheckBox('Build mosaic preview')
        self.buildOverviewCheck.setChecked(True)
        self.buildOverviewCheck.setToolTip(
            'Uncheck for large tiling runs: acquire and save individual tiles and\n'
            'their position/sidecar metadata without constructing an in-memory\n'
            'mosaic or displaying it. Image registration and viewer-based cell\n'
            'detection are unavailable, and no stitched mosaic file is saved.'
        )
        row.addWidget(self.buildOverviewCheck)
        self.blendOverlapsCheck = QtWidgets.QCheckBox('Mean overlaps')
        self.blendOverlapsCheck.setChecked(True)
        self.blendOverlapsCheck.setToolTip(
            'Average overlapping pixels instead of letting the newer tile win.'
        )
        self.intensityCorrectionCheck = QtWidgets.QCheckBox('Intensity correction')
        self.intensityCorrectionCheck.setToolTip(
            'Match each tile\'s brightness to its neighbours\' where they overlap.\n'
            'Corrects what changes from tile to tile (flicker, fading light);\n'
            'the vignetting every tile shares is left out of the comparison.'
        )
        row.addWidget(self.blendOverlapsCheck)
        row.addWidget(self.intensityCorrectionCheck)
        row.addStretch(1)
        return self._group('Stitching', row)

    def _buildRunRow(self) -> QtWidgets.QHBoxLayout:
        row = QtWidgets.QHBoxLayout()
        self.startButton = guitools.BetterPushButton('Start Tiling')
        self.stopButton = guitools.BetterPushButton('Stop')
        self.stopButton.setEnabled(False)
        # One chooser rather than two checkboxes: a click cannot both drive
        # the stage and mark a target, so the choices are exclusive.
        self.clickModeCombo = QtWidgets.QComboBox()
        self.clickModeCombo.addItem('does nothing', CLICK_NONE)
        self.clickModeCombo.addItem('moves the stage', CLICK_NAVIGATE)
        self.clickModeCombo.addItem('adds a target', CLICK_ADD_TARGET)
        self.clickModeCombo.setToolTip(
            'What a click on the overview does.\n'
            'Moves the stage: drive the XY positioner to the clicked spot.\n'
            'Adds a target: remember the clicked spot in the target list\n'
            'below, without moving anything.'
        )
        self.autoContrastCheck = QtWidgets.QCheckBox('Auto contrast')
        self.autoContrastCheck.setChecked(True)
        self.autoContrastCheck.setToolTip(
            "Fit the overview's contrast to each update (ImageJ's Auto).\n"
            'Drag the levels beside the overview to set your own: they then\n'
            'hold while tiles arrive, until this is ticked again.\n'
            'Right-click the colour bar for other colour maps.\n'
            'Display only: saved tiles and the mosaic are not changed.'
        )
        row.addWidget(self.startButton, 2)
        row.addWidget(self.stopButton, 1)
        row.addWidget(QtWidgets.QLabel('Click:'))
        row.addWidget(self.clickModeCombo)
        row.addWidget(self.autoContrastCheck)
        return row

    def _buildStatusBlock(self) -> QtWidgets.QWidget:
        holder = QtWidgets.QWidget()
        column = QtWidgets.QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)

        self.progressLabel = QtWidgets.QLabel('')
        self.progressLabel.setAlignment(QtCore.Qt.AlignCenter)
        column.addWidget(self.progressLabel)

        # Filled after a run: how far the commanded positions were off, and
        # whether the layout held together.
        self.registrationLabel = QtWidgets.QLabel('')
        self.registrationLabel.setWordWrap(True)
        self.registrationLabel.setAlignment(QtCore.Qt.AlignLeft)
        column.addWidget(self.registrationLabel)
        return holder

    def _buildOverview(self) -> QtWidgets.QWidget:
        self.overviewView = pg.GraphicsLayoutWidget()
        self.overviewView.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding
        )
        self.overviewView.setMinimumHeight(240)
        self.overviewItem = pg.ImageItem()
        self.overviewItem.setImage(np.zeros((64, 64), dtype=np.float32))
        self._overviewVB = self.overviewView.addViewBox()
        self._overviewVB.setAspectLocked(True)
        self._overviewVB.invertY(True)
        self._overviewVB.addItem(self.overviewItem)

        # Levels and colour map beside the overview. Every repaint used to
        # fit the levels to the image's own minimum and maximum, so there was
        # no way to set a contrast and keep it while tiles arrived.
        self._overviewImage = None
        self._settingOverviewLevels = False
        self.overviewHist = pg.HistogramLUTItem(image=self.overviewItem)
        self.overviewView.addItem(self.overviewHist)

        # Target markers: one cross per target that lies on the overview, and
        # a ring around the one selected in the table.
        self._targetPenOn = pg.mkPen('y', width=2)
        self._targetPenOff = pg.mkPen((160, 160, 160), width=1)
        self.targetMarkers = pg.ScatterPlotItem(symbol='+', size=14, brush=None)
        self.selectedTargetMarker = pg.ScatterPlotItem(
            symbol='o', size=24, pen=pg.mkPen('r', width=3), brush=None
        )
        self._overviewVB.addItem(self.targetMarkers)
        self._overviewVB.addItem(self.selectedTargetMarker)
        self._targetMarkers: Dict[int, Tuple[float, float]] = {}
        self._targetEnabled: Dict[int, bool] = {}
        return self.overviewView

    def _buildTargetsGroup(self) -> QtWidgets.QGroupBox:
        """Stage positions to come back to, listed under the overview.

        The list is the controller's; the table shows it and sends back what
        the operator asked for. It is sized to stay a strip under the
        overview rather than compete with it for height.
        """
        outer = QtWidgets.QVBoxLayout()

        self.targetsTable = QtWidgets.QTableWidget(0, 5)
        self.targetsTable.setHorizontalHeaderLabels(
            ['On', '#', 'X (µm)', 'Y (µm)', 'Source']
        )
        self.targetsTable.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectRows
        )
        self.targetsTable.setSelectionMode(
            QtWidgets.QAbstractItemView.SingleSelection
        )
        self.targetsTable.setEditTriggers(
            QtWidgets.QAbstractItemView.NoEditTriggers
        )
        self.targetsTable.verticalHeader().setVisible(False)
        header = self.targetsTable.horizontalHeader()
        header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        header.setStretchLastSection(True)
        self.targetsTable.setMaximumHeight(160)
        self.targetsTable.setToolTip(
            'Stage positions to return to. Tick a target off to keep it in\n'
            'the list but skip it; select one to see where it is on the\n'
            'overview. Scripts read the same list through\n'
            'api.imcontrol.getTargets().'
        )
        outer.addWidget(self.targetsTable)

        buttons = QtWidgets.QHBoxLayout()
        self.goToTargetButton = guitools.BetterPushButton('Go to')
        self.goToTargetButton.setToolTip(
            'Drive the stage to the selected target.'
        )
        self.removeTargetButton = guitools.BetterPushButton('Remove')
        self.clearTargetsButton = guitools.BetterPushButton('Clear')
        self.saveTargetsButton = guitools.BetterPushButton('Save…')
        self.loadTargetsButton = guitools.BetterPushButton('Load…')
        self.saveTargetsButton.setToolTip(
            'Write the list to a JSON file a script or a later session can\n'
            'load again.'
        )
        self.goToTargetButton.setEnabled(False)
        self.removeTargetButton.setEnabled(False)
        for button in (
            self.goToTargetButton, self.removeTargetButton,
            self.clearTargetsButton, self.saveTargetsButton,
            self.loadTargetsButton,
        ):
            buttons.addWidget(button)
        self.targetsCountLabel = QtWidgets.QLabel('')
        buttons.addStretch(1)
        buttons.addWidget(self.targetsCountLabel)
        outer.addLayout(buttons)
        self._updatingTargets = False
        return self._group('Targets', outer)

    def _onSceneClicked(self, event):
        if self.getClickMode() == CLICK_NONE:
            return
        pos = self._overviewVB.mapSceneToView(event.scenePos())
        col = int(pos.x())
        row = int(pos.y())
        if col >= 0 and row >= 0:
            self.sigClickOnOverview.emit(row, col)

    def _updateOverviewAvailability(self, enabled: bool) -> None:
        """Avoid showing a stale overview when a run skips mosaic assembly."""
        self.overviewView.setVisible(enabled)
        self.blendOverlapsCheck.setEnabled(enabled)
        self.intensityCorrectionCheck.setEnabled(enabled)
        self.registerTilesCheck.setEnabled(enabled)
        # Saving and stage targets remain available without a mosaic, but
        # clicking an invisible/old overview must not navigate or add targets.
        self.clickModeCombo.setEnabled(enabled)
        if not enabled:
            self.setClickMode(CLICK_NONE)
            self._overviewImage = None
            self.overviewItem.setImage(np.zeros((64, 64), dtype=np.float32))
            self._targetMarkers.clear()
            self._redrawTargetMarkers()

    def updateOverview(self, image: np.ndarray) -> None:
        """Display a new stitched overview (float32 H×W array, 0 to 1).

        With Auto contrast ticked the levels are fitted to it; otherwise the
        operator's levels stay where they are.
        """
        self._overviewImage = image
        if self.autoContrastCheck.isChecked():
            levels = self._autoContrastLevels(image)
        else:
            levels = tuple(self.overviewHist.getLevels())
        self._showOverview(image, levels)

    def _showOverview(self, image: np.ndarray, levels) -> None:
        # The histogram follows the image's levels on every update, and says
        # so through the same signal a drag does; this flag tells them apart.
        self._settingOverviewLevels = True
        try:
            self.overviewItem.setImage(image.T, autoLevels=False, levels=levels)
        finally:
            self._settingOverviewLevels = False

    @staticmethod
    def _autoContrastLevels(image: np.ndarray):
        """ImageJ's Auto levels for an overview, or its full range if blank.

        The canvas outside the tiles placed so far is zero; ImageJ's rule
        already ignores a bin holding over a tenth of the pixels, so it does
        not drag the levels down while the mosaic is still small.
        """
        stride = [
            max(1, int(np.ceil(extent / _AUTO_CONTRAST_SAMPLES_PER_SIDE)))
            for extent in image.shape[:2]
        ]
        sample = np.asarray(image[::stride[0], ::stride[1]])
        if sample.size == 0:
            return _OVERVIEW_RANGE
        low, high = (float(level) for level in bestLevels(sample))
        if not (np.isfinite(low) and np.isfinite(high)) or high <= low:
            return _OVERVIEW_RANGE
        return low, high

    def _onOverviewLevelsChanged(self, *_args) -> None:
        if self._settingOverviewLevels:
            return
        # The operator moved the levels: theirs from now on.
        if self.autoContrastCheck.isChecked():
            self.autoContrastCheck.setChecked(False)

    def _onAutoContrastToggled(self, checked: bool) -> None:
        if checked and self._overviewImage is not None:
            self._showOverview(
                self._overviewImage, self._autoContrastLevels(self._overviewImage),
            )

    def setProgress(self, current: int, total: int) -> None:
        self.progressLabel.setText(f'{current} / {total}')

    def setRunning(self, running: bool) -> None:
        self.startButton.setEnabled(not running)
        self.stopButton.setEnabled(running)
        self.tiltModeCombo.setEnabled(not running)
        self.tiltCalibrationButton.setEnabled(not running)
        self._updateTiltActionMenu()
        self.tiltNameEdit.setEnabled(not running)
        self.tiltCalibrationOnlyCheck.setEnabled(not running)
        self.tiltBidirectionalCheck.setEnabled(not running)
        self._updateTiltSamplingEnabled()
        self.precheckTiltCheck.setEnabled(
            not running and self.tiltModeCombo.currentData() == 'apply'
        )
        self.buildOverviewCheck.setEnabled(not running)
        self.focusPauseCheck.setEnabled(not running)

    def _matchTileCounts(self, value: int) -> None:
        """Hold the counts equal while the lock is on, without recursing."""
        if not self.squareLockCheck.isChecked():
            return
        for spinbox in (self.nTilesXSpinbox, self.nTilesYSpinbox):
            if spinbox.value() == value:
                continue
            spinbox.blockSignals(True)
            spinbox.setValue(value)
            spinbox.blockSignals(False)

    def _onSquareLockToggled(self, locked: bool) -> None:
        if locked:
            self._matchTileCounts(self.nTilesXSpinbox.value())

    def getNTilesX(self) -> int:
        return self.nTilesXSpinbox.value()

    def getNTilesY(self) -> int:
        return self.nTilesYSpinbox.value()

    def getNTiles(self) -> int:
        """Total positions in the grid — what the progress bar counts."""
        return self.nTilesXSpinbox.value() * self.nTilesYSpinbox.value()

    def getPattern(self) -> str:
        return self.patternCombo.currentData()

    def setDefaultTileCounts(self, nx: int, ny: int) -> None:
        self.squareLockCheck.setChecked(nx == ny)
        self.nTilesXSpinbox.setValue(int(nx))
        self.nTilesYSpinbox.setValue(int(ny))

    def setDefaultPattern(self, pattern: str) -> None:
        index = self.patternCombo.findData(pattern)
        if index >= 0:
            self.patternCombo.setCurrentIndex(index)

    def getTileStepUm(self) -> float:
        return self.tileStepSpinbox.value()

    def getBuildOverview(self) -> bool:
        return self.buildOverviewCheck.isChecked()

    def getBlendOverlaps(self) -> bool:
        return self.blendOverlapsCheck.isChecked()

    def getIntensityCorrection(self) -> bool:
        return self.intensityCorrectionCheck.isChecked()

    def getSettleTimeMs(self) -> float:
        return self.settleTimeSpinbox.value()

    def getRegisterTiles(self) -> bool:
        return self.registerTilesCheck.isChecked()

    def getPauseFocusLock(self) -> bool:
        return self.focusPauseCheck.isChecked()

    def getSaveTiles(self) -> bool:
        return self.saveTilesCheck.isChecked()

    def getRecordTiltCalibration(self) -> bool:
        return self.tiltModeCombo.currentData() == 'record'

    def getApplyTiltCalibration(self) -> bool:
        return self.tiltModeCombo.currentData() == 'apply'

    def getPrecheckTiltCompensation(self) -> bool:
        return self.precheckTiltCheck.isChecked()

    def getTiltCalibrationPath(self) -> str:
        return self._tiltCalibrationPath

    def setLoadedTiltCalibration(self, path, plane, display_name: str = '') -> None:
        self._tiltCalibrationPath = str(Path(path).resolve())
        self._tiltCalibrationName = display_name
        self.tiltApplyZLabel.setText(plane.focus_positioner)
        self.tiltApplyZLabel.setToolTip(
            f'Z actuator saved in {self._tiltCalibrationPath}; '
            'independent of the current Focus Lock actuator.'
        )
        self._updateTiltCalibrationLabel()

    def _onModeChanged(self) -> None:
        self.sigParamsChanged.emit()
        self.sigModeChanged.emit(self.getMode())

    def getMode(self) -> str:
        return self.modeCombo.currentData() or MODE_FREE_RUNNING

    def setMode(self, mode: str) -> None:
        index = self.modeCombo.findData(mode)
        if index >= 0:
            self.modeCombo.setCurrentIndex(index)

    def getClickMode(self) -> str:
        """What a click on the overview does: one of the ``CLICK_*`` values."""
        return self.clickModeCombo.currentData() or CLICK_NONE

    def setClickMode(self, mode: str) -> None:
        index = self.clickModeCombo.findData(mode)
        if index >= 0:
            self.clickModeCombo.setCurrentIndex(index)

    def getScanSource(self):
        """Selected scan-source widget key, or None to let the app resolve it.

        None when the setup offers no choice. Asked with ``isHidden()``, not
        ``isVisible()``: the latter is also False while this panel is a tab
        behind another one, and a run started then -- from a timelapse or a
        script -- silently used the default instead of the selection.
        """
        if self.scanSourceCombo.isHidden():
            return None
        return self.scanSourceCombo.currentData()

    def setScanSource(self, key: str) -> None:
        index = self.scanSourceCombo.findData(key)
        if index >= 0:
            self.scanSourceCombo.setCurrentIndex(index)

    def setScanSources(self, keys) -> None:
        """Offer a scan-source choice, but only when there is one to make.

        A rig with a single scanner is never asked to pick it.
        """
        keys = list(keys or [])
        self.scanSourceCombo.blockSignals(True)
        self.scanSourceCombo.clear()
        for key in keys:
            self.scanSourceCombo.addItem(key, key)
        self.scanSourceCombo.blockSignals(False)

        showChoice = len(keys) > 1
        self.scanSourceLabel.setVisible(showChoice)
        self.scanSourceCombo.setVisible(showChoice)

    def getDetector(self):
        """Selected detector name, or None to let the controller resolve it.

        None when the setup offers no choice, so the setup-file value (and its
        fallback) stays the single authority on rigs with one camera.
        """
        # isHidden(), not isVisible(): see getScanSource. A tiling timelapse
        # started from the Timelapse tab aligned on the setup's default
        # detector (a scan-driven APD) instead of the selected camera.
        if self.detectorCombo.isHidden():
            return None
        return self.detectorCombo.currentData()

    def setDetector(self, name: str) -> None:
        index = self.detectorCombo.findData(name)
        if index >= 0:
            self.detectorCombo.setCurrentIndex(index)

    def setDetectors(self, names) -> None:
        """Offer a detector choice, but only when there is one to make."""
        names = list(names or [])
        self.detectorCombo.blockSignals(True)
        self.detectorCombo.clear()
        for name in names:
            self.detectorCombo.addItem(name, name)
        self.detectorCombo.blockSignals(False)

        showChoice = len(names) > 1
        self.detectorLabel.setVisible(showChoice)
        self.detectorCombo.setVisible(showChoice)

    def getTileOrientation(self) -> tuple:
        """Return ``(flip_x, flip_y, swap_axes)`` for mosaic assembly."""
        return (
            self.flipXCheck.isChecked(),
            self.flipYCheck.isChecked(),
            self.swapAxesCheck.isChecked(),
        )

    def setDefaultStep(self, step_um: float) -> None:
        self.tileStepSpinbox.setValue(step_um)

    def setDefaultSettleTimeMs(self, settle_ms: float) -> None:
        self.settleTimeSpinbox.setValue(settle_ms)

    def setDefaultRegisterTiles(self, enabled: bool) -> None:
        self.registerTilesCheck.setChecked(bool(enabled))

    def setDefaultSaveTiles(self, enabled: bool) -> None:
        self.saveTilesCheck.setChecked(bool(enabled))

    def setDefaultTileOrientation(
        self, flip_x: bool, flip_y: bool, swap_axes: bool
    ) -> None:
        self.flipXCheck.setChecked(bool(flip_x))
        self.flipYCheck.setChecked(bool(flip_y))
        self.swapAxesCheck.setChecked(bool(swap_axes))

    def setRegistrationSummary(self, summary: str) -> None:
        """Show the post-run registration diagnostics, or clear them."""
        self.registrationLabel.setText(summary or '')

    def setLabel(self, label: str) -> None:
        self.progressLabel.setText(label)

    # ------------------------------------------------------------------
    # Targets
    # ------------------------------------------------------------------

    def setTargets(self, records) -> None:
        """Show the target list.

        ``records`` are the controller's target dicts (``id``, ``stage_xy``,
        ``enabled``, ``source``). The selection survives a rebuild when its
        target is still there, so ticking one target does not drop the
        selection on another.
        """
        records = list(records or [])
        selected = self.selectedTargetId()
        self._updatingTargets = True
        try:
            self.targetsTable.setRowCount(0)
            self.targetsTable.setRowCount(len(records))
            self._targetEnabled = {}
            for rowIndex, record in enumerate(records):
                target_id = int(record['id'])
                x_um, y_um = record['stage_xy']
                enabled = bool(record.get('enabled', True))
                self._targetEnabled[target_id] = enabled

                onItem = QtWidgets.QTableWidgetItem()
                onItem.setFlags(
                    QtCore.Qt.ItemIsUserCheckable | QtCore.Qt.ItemIsEnabled
                    | QtCore.Qt.ItemIsSelectable
                )
                onItem.setCheckState(
                    QtCore.Qt.Checked if enabled else QtCore.Qt.Unchecked
                )
                onItem.setData(QtCore.Qt.UserRole, target_id)
                self.targetsTable.setItem(rowIndex, 0, onItem)
                for column, text in enumerate(
                    (
                        str(target_id),
                        f'{float(x_um):.1f}',
                        f'{float(y_um):.1f}',
                        str(record.get('source', '')),
                    ),
                    start=1,
                ):
                    item = QtWidgets.QTableWidgetItem(text)
                    item.setData(QtCore.Qt.UserRole, target_id)
                    self.targetsTable.setItem(rowIndex, column, item)
                if target_id == selected:
                    self.targetsTable.selectRow(rowIndex)
        finally:
            self._updatingTargets = False

        enabledCount = sum(1 for on in self._targetEnabled.values() if on)
        if not records:
            self.targetsCountLabel.setText('')
        elif enabledCount == len(records):
            self.targetsCountLabel.setText(f'{len(records)} targets')
        else:
            self.targetsCountLabel.setText(
                f'{enabledCount} of {len(records)} targets on'
            )
        self._redrawTargetMarkers()
        self._onTargetSelectionChanged()

    def setTargetMarkers(self, markers) -> None:
        """Place targets on the overview.

        ``markers`` maps a target id to its ``(row, col)`` overview pixel.
        Targets that are not in it — because there is no overview, or they
        lie outside it — are listed but not drawn.
        """
        self._targetMarkers = {
            int(target_id): (float(rc[0]), float(rc[1]))
            for target_id, rc in dict(markers or {}).items()
        }
        self._redrawTargetMarkers()

    def selectedTargetId(self) -> Optional[int]:
        items = self.targetsTable.selectedItems()
        if not items:
            return None
        value = items[0].data(QtCore.Qt.UserRole)
        return None if value is None else int(value)

    def _redrawTargetMarkers(self) -> None:
        spots = []
        for target_id, (row, col) in self._targetMarkers.items():
            on = self._targetEnabled.get(target_id, True)
            spots.append({
                'pos': (col, row),
                'pen': self._targetPenOn if on else self._targetPenOff,
                'data': target_id,
            })
        self.targetMarkers.setData(spots=spots)
        self._highlightSelectedTarget()

    def _highlightSelectedTarget(self) -> None:
        marker = self._targetMarkers.get(self.selectedTargetId())
        if marker is None:
            self.selectedTargetMarker.clear()
            return
        row, col = marker
        self.selectedTargetMarker.setData(x=[col], y=[row])

    def _onTargetSelectionChanged(self) -> None:
        hasSelection = self.selectedTargetId() is not None
        self.goToTargetButton.setEnabled(hasSelection)
        self.removeTargetButton.setEnabled(hasSelection)
        self._highlightSelectedTarget()

    def _onTargetItemChanged(self, item) -> None:
        if self._updatingTargets or item.column() != 0:
            return
        target_id = item.data(QtCore.Qt.UserRole)
        if target_id is None:
            return
        self.sigTargetEnabledChanged.emit(
            int(target_id), item.checkState() == QtCore.Qt.Checked
        )

    def _onGoToTarget(self) -> None:
        target_id = self.selectedTargetId()
        if target_id is not None:
            self.sigGoToTarget.emit(target_id)

    def _onRemoveTarget(self) -> None:
        target_id = self.selectedTargetId()
        if target_id is not None:
            self.sigRemoveTarget.emit(target_id)

    def _onSaveTargets(self) -> None:
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(
            self, 'Save targets', '', _TARGET_FILE_FILTER
        )
        if path:
            self.sigSaveTargets.emit(path)

    def _onLoadTargets(self) -> None:
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(
            self, 'Load targets', '', _TARGET_FILE_FILTER
        )
        if path:
            self.sigLoadTargets.emit(path)


# Copyright (C) 2020-2021 ImSwitch developers
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
