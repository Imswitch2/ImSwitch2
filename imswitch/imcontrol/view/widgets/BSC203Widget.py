from qtpy import QtCore, QtWidgets

from imswitch.imcontrol.view import guitools
from .basewidgets import Widget


class BSC203Widget(Widget):
    """ Widget for the Thorlabs BSC203 3-axis NanoMax motorised stage. """

    sigHomeAll = QtCore.Signal()
    sigKeyPressed = QtCore.Signal(object)
    sigKeyReleased = QtCore.Signal(object)
    sigFocusLost = QtCore.Signal()  # emitted when widget loses keyboard focus

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setWindowTitle('NanoMax Stepper motor controller')

        self.homeBtn = guitools.BetterPushButton('Home all motors')
        self.homeBtn.clicked.connect(self._homeClicked)

        self.XYVelLabel = QtWidgets.QLabel('X/Y velocity [um/s]')
        self.XYVelEdit = guitools.BetterDoubleSpinBox(allowScrollChanges=False)
        self.XYVelEdit.setMaximum(2000)
        self.XYVelEdit.setMinimum(0)

        self.ZVelLabel = QtWidgets.QLabel('Z velocity [um/s]')
        self.ZVelEdit = guitools.BetterDoubleSpinBox(allowScrollChanges=False)
        self.ZVelEdit.setMaximum(2000)
        self.ZVelEdit.setMinimum(0)

        self.setPosLabel = QtWidgets.QLabel('Set absolute position [um]')

        self.setXLabel = QtWidgets.QLabel('X')
        self.setXEdit = guitools.BetterDoubleSpinBox(allowScrollChanges=False)
        self.setXEdit.setMaximum(8000)
        self.setXEdit.setMinimum(-500)
        self.setYLabel = QtWidgets.QLabel('Y')
        self.setYEdit = guitools.BetterDoubleSpinBox(allowScrollChanges=False)
        self.setYEdit.setMaximum(8000)
        self.setYEdit.setMinimum(-500)
        self.setZLabel = QtWidgets.QLabel('Z')
        self.setZEdit = guitools.BetterDoubleSpinBox(allowScrollChanges=False)
        self.setZEdit.setMaximum(8000)
        self.setZEdit.setMinimum(-500)

        self.moveToBtn = guitools.BetterPushButton('Move to pos')
        self.stopBtn = guitools.BetterPushButton('Stop movement')

        self.pos0Label = QtWidgets.QLabel('X position [µm]')
        self.pos0EditLabel = QtWidgets.QLabel()
        self.pos1Label = QtWidgets.QLabel('Y position [µm]')
        self.pos1EditLabel = QtWidgets.QLabel()
        self.pos2Label = QtWidgets.QLabel('Z position [µm]')
        self.pos2EditLabel = QtWidgets.QLabel()

        grid = QtWidgets.QGridLayout()
        self.setLayout(grid)
        grid.addWidget(self.XYVelLabel,    0,  0, 1, 1)
        grid.addWidget(self.XYVelEdit,     0,  1, 1, 1)
        grid.addWidget(self.ZVelLabel,     1,  0, 1, 1)
        grid.addWidget(self.ZVelEdit,      1,  1, 1, 1)
        grid.addWidget(self.setPosLabel,   2,  0, 1, 2)
        grid.addWidget(self.setXLabel,     3,  0, 1, 1)
        grid.addWidget(self.setXEdit,      3,  1, 1, 1)
        grid.addWidget(self.setYLabel,     4,  0, 1, 1)
        grid.addWidget(self.setYEdit,      4,  1, 1, 1)
        grid.addWidget(self.setZLabel,     5,  0, 1, 1)
        grid.addWidget(self.setZEdit,      5,  1, 1, 1)
        grid.addWidget(self.moveToBtn,     6,  0, 1, 2)
        grid.addWidget(self.stopBtn,       7,  0, 1, 2)
        grid.addWidget(self.pos0Label,     8,  0, 1, 1)
        grid.addWidget(self.pos0EditLabel, 8,  1, 1, 1)
        grid.addWidget(self.pos1Label,     9,  0, 1, 1)
        grid.addWidget(self.pos1EditLabel, 9,  1, 1, 1)
        grid.addWidget(self.pos2Label,     10, 0, 1, 1)
        grid.addWidget(self.pos2EditLabel, 10, 1, 1, 1)
        grid.addWidget(self.homeBtn,       11, 0, 1, 2)

        self._msg = QtWidgets.QMessageBox
        self.setFocusPolicy(QtCore.Qt.StrongFocus)

    def _homeClicked(self):
        answer = self._msg.question(
            self, '', 'Are you sure you want to home all the motors?',
            self._msg.Yes | self._msg.No
        )
        if answer == self._msg.Yes:
            self.sigHomeAll.emit()

    def keyPressEvent(self, event):
        self.sigKeyPressed.emit(event)

    def keyReleaseEvent(self, event):
        self.sigKeyReleased.emit(event)

    def focusOutEvent(self, event):
        """Stop all stage motion when the widget loses keyboard focus.

        Qt does not deliver keyReleaseEvent if a key is held while focus
        moves to another widget.  Without this guard a velocity move started
        by an arrow-key press would keep running until it hits the hardware
        end-stop (~-2500 µm).
        """
        self.sigFocusLost.emit()
        super().focusOutEvent(event)
