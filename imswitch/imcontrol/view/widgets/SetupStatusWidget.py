from qtpy import QtCore, QtGui, QtWidgets
from .basewidgets import Widget
from imswitch.imcontrol.view import guitools


class SetupStatusWidget(Widget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setWindowTitle('Setup status')

        self.illuminationLabel = QtWidgets.QLabel('Current illumination config:')
        self.illuminationLabel.setFont(QtGui.QFont('Calibri', 14))
        self.illuminationLabel.setStyleSheet('font-weight: bold')
        self.illuminationStatusLabel = QtWidgets.QLabel('')
        self.illuminationStatusLabel.setFont(QtGui.QFont('Calibri', 14))
        self.illuminationStatusLabel.setStyleSheet('font-weight: bold')

        self.detectionLabel = QtWidgets.QLabel('Current detection config:')
        self.detectionLabel.setFont(QtGui.QFont('Calibri', 14))
        self.detectionLabel.setStyleSheet('font-weight: bold')
        self.detectionStatusLabel = QtWidgets.QLabel('')
        self.detectionStatusLabel.setFont(QtGui.QFont('Calibri', 14))
        self.detectionStatusLabel.setStyleSheet('font-weight: bold')

        self.rotationStageHeader = QtWidgets.QLabel('Rotation stage')
        self.rotationStageHeader.setFont(QtGui.QFont('Calibri', 14))
        self.rotationStageHeader.setStyleSheet('font-weight: bold')

        self.rotationStagePosLabel = QtWidgets.QLabel('Set position (deg) of rotation stage')
        self.rotationStagePosEdit = guitools.BetterDoubleSpinBox()
        self.rotationStagePosEdit.setDecimals(4)
        self.rotationStagePosEdit.setMinimum(-360)
        self.rotationStagePosEdit.setMaximum(360)
        self.rotationStagePosEdit.setSingleStep(0.01)

        self.jogStepSizeLabel = QtWidgets.QLabel('Set jog step size (deg)')
        self.jogStepSizeEdit = guitools.BetterDoubleSpinBox()
        self.jogStepSizeEdit.setDecimals(4)
        self.jogStepSizeEdit.setMaximum(360)
        self.jogStepSizeEdit.setSingleStep(0.01)

        self.jogPositiveButton = guitools.BetterPushButton('>>')
        self.jogNegativeButton = guitools.BetterPushButton('<<')

        self.currentPosOfRotationStageLabel = QtWidgets.QLabel(
            'Current position (deg) of rotation stage'
        )
        self.currentPosOfRotationStageDisp = QtWidgets.QLabel('')

        self.hotKeyInfo = QtWidgets.QLabel('Hot keys for setting status:')
        self.hotKeyInfo.setFont(QtGui.QFont('Calibri', 14))
        self.hotKeyInfo.setStyleSheet('font-weight: bold')

        self.illDetHK1info = QtWidgets.QLabel(
            'Set illumination and detection to straight widefield:'
        )
        self.illDetHK2info = QtWidgets.QLabel(
            'Set illumination and detection to tilted light sheet:'
        )
        self.illHK1info = QtWidgets.QLabel('Set illumination to widefield:')
        self.illHK2info = QtWidgets.QLabel('Set illumination to light sheet:')
        self.detHK1info = QtWidgets.QLabel('Set detection to straight:')
        self.detHK2info = QtWidgets.QLabel('Set detection to tilted:')

        self.illDetHK1 = QtWidgets.QLabel('F1')
        self.illDetHK2 = QtWidgets.QLabel('F2')
        self.illHK1 = QtWidgets.QLabel('F5')
        self.illHK2 = QtWidgets.QLabel('F6')
        self.detHK1 = QtWidgets.QLabel('F9')
        self.detHK2 = QtWidgets.QLabel('F10')

        grid = QtWidgets.QGridLayout()
        self.setLayout(grid)

        grid.addWidget(self.illuminationLabel, 0, 0, 1, 1)
        grid.addWidget(self.illuminationStatusLabel, 0, 1, 1, 1)
        grid.addWidget(self.detectionLabel, 0, 2, 1, 1)
        grid.addWidget(self.detectionStatusLabel, 0, 3, 1, 1)
        grid.addItem(
            QtWidgets.QSpacerItem(
                40, 20, QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Expanding
            ),
            1, 0,
        )

        currentRow = 2
        grid.addWidget(self.rotationStageHeader, currentRow, 0)
        currentRow += 1
        grid.addWidget(self.rotationStagePosLabel, currentRow, 0, 1, 1)
        grid.addWidget(self.rotationStagePosEdit, currentRow, 1, 1, 1)
        currentRow += 1
        grid.addWidget(self.jogStepSizeLabel, currentRow, 0, 1, 1)
        grid.addWidget(self.jogStepSizeEdit, currentRow, 1, 1, 1)
        currentRow += 1
        grid.addWidget(self.jogNegativeButton, currentRow, 0, 1, 1)
        grid.addWidget(self.jogPositiveButton, currentRow, 1, 1, 1)
        currentRow += 1
        grid.addWidget(self.currentPosOfRotationStageLabel, currentRow, 0, 1, 1)
        grid.addWidget(self.currentPosOfRotationStageDisp, currentRow, 1, 1, 1)
        currentRow += 1

        grid.addWidget(self.hotKeyInfo, currentRow, 0, 1, 2)
        currentRow += 1
        grid.addWidget(self.illDetHK1info, currentRow, 0, 1, 1)
        grid.addWidget(self.illDetHK1, currentRow, 1, 1, 1)
        currentRow += 1
        grid.addWidget(self.illDetHK2info, currentRow, 0, 1, 1)
        grid.addWidget(self.illDetHK2, currentRow, 1, 1, 1)
        currentRow += 1
        grid.addWidget(self.illHK1info, currentRow, 0, 1, 1)
        grid.addWidget(self.illHK1, currentRow, 1, 1, 1)
        currentRow += 1
        grid.addWidget(self.illHK2info, currentRow, 0, 1, 1)
        grid.addWidget(self.illHK2, currentRow, 1, 1, 1)
        currentRow += 1
        grid.addWidget(self.detHK1info, currentRow, 0, 1, 1)
        grid.addWidget(self.detHK1, currentRow, 1, 1, 1)
        currentRow += 1
        grid.addWidget(self.detHK2info, currentRow, 0, 1, 1)
        grid.addWidget(self.detHK2, currentRow, 1, 1, 1)
