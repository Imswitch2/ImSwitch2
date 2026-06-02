from qtpy import QtCore

from imswitch.imcommon.model import initLogger
from ..basecontrollers import ImConWidgetController

STEPS_PER_REV = 409600
REV_PER_MM = 2

Xchan = 1
Ychan = 0
Zchan = 2


class BSC203Controller(ImConWidgetController):
    """ Linked to BSC203Widget. Controls the Thorlabs BSC203 NanoMax stage. """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__logger = initLogger(self)

        try:
            self._stageManager = self._master.positionersManager['BSC203']
            self.dev = self._stageManager.dev
        except KeyError:
            self.__logger.error('BSC203 positioner not found in setup — widget disabled')
            self._widget.setEnabled(False)
            return

        if self.dev is None:
            self.__logger.warning('BSC203 device not available — widget disabled')
            self._widget.setEnabled(False)
            return

        self.initVelXY, self.initVelZ = 300, 300
        self.initAccXY, self.initAccZ = 4000, 4000
        self.initialize()

        self._widget.XYVelEdit.setValue(self.initVelXY)
        self._widget.ZVelEdit.setValue(self.initVelZ)

        self._widget.moveToBtn.clicked.connect(self.moveTo)
        self._widget.stopBtn.clicked.connect(self.stopAll)
        self._widget.XYVelEdit.editingFinished.connect(self.setXYVelocity)
        self._widget.ZVelEdit.editingFinished.connect(self.setZVelocity)
        self._widget.sigHomeAll.connect(self.homeAll)
        self._widget.sigKeyPressed.connect(self.keyPressed)
        self._widget.sigKeyReleased.connect(self.keyReleased)

        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.getPosition_mm)
        self.timer.start(100)

    # ------------------------------------------------------------------
    # Unit helpers
    # ------------------------------------------------------------------

    def to_enc_steps(self, mm):
        return int(mm * REV_PER_MM * STEPS_PER_REV)

    def to_mm(self, steps):
        return steps / (REV_PER_MM * STEPS_PER_REV)

    # ------------------------------------------------------------------
    # Initialisation / velocity
    # ------------------------------------------------------------------

    def initialize(self):
        self.__logger.debug('Setting initial position')
        for bay in range(3):
            self.dev.set_velocity_params(acceleration=4506, max_velocity=21987328 * 5,
                                         bay=bay, channel=0)
        self.setInitialVelocity()

    def setInitialVelocity(self):
        self.__logger.debug('Setting initial velocity')
        self.dev.set_velocity_params(
            acceleration=int(self.initAccXY / 1000 * 4506),
            max_velocity=int(self.initVelXY / 1000 * 21987328),
            bay=0, channel=0)
        self.dev.set_velocity_params(
            acceleration=int(self.initAccXY / 1000 * 4506),
            max_velocity=int(self.initVelXY / 1000 * 21987328),
            bay=1, channel=0)
        self.dev.set_velocity_params(
            acceleration=int(self.initAccZ / 1000 * 4506),
            max_velocity=int(self.initVelZ / 1000 * 21987328),
            bay=2, channel=0)

    def setVelocity(self, um_per_s, axis):
        self.dev.set_velocity_params(
            acceleration=4506,
            max_velocity=int((um_per_s * 21987328) / 1000),
            bay=axis, channel=0)

    def setXYVelocity(self):
        um_per_s = self._widget.XYVelEdit.value()
        self.setVelocity(um_per_s, 0)
        self.setVelocity(um_per_s, 1)

    def setZVelocity(self):
        um_per_s = self._widget.ZVelEdit.value()
        self.setVelocity(um_per_s, 2)

    # ------------------------------------------------------------------
    # Movement
    # ------------------------------------------------------------------

    def moveTo(self):
        self.move_absolute_mm(self._widget.setXEdit.value() / 1000, Xchan)
        self.move_absolute_mm(self._widget.setYEdit.value() / 1000, Ychan)
        self.move_absolute_mm(self._widget.setZEdit.value() / 1000, Zchan)

    def move_absolute_mm(self, position_mm, axis):
        self.dev.move_absolute(self.to_enc_steps(position_mm), now=True, bay=axis, channel=0)

    def move_constant(self, direction, axis):
        self.dev.move_velocity(direction=direction, bay=axis, channel=0)

    def stopAll(self):
        for axis in range(3):
            self.stop(axis)

    def stop(self, axis):
        self.dev.stop(bay=axis)

    def homeAll(self):
        self.dev.home(bay=0)
        self.dev.home(bay=1)
        self.dev.home(bay=2)
        while not all(self.dev.status_[b][0]['homed'] for b in range(3)):
            pass

    # ------------------------------------------------------------------
    # Position readback
    # ------------------------------------------------------------------

    def getPosition_mm(self):
        x = self.to_mm(self.dev.status_[Xchan][0]['position'])
        y = self.to_mm(self.dev.status_[Ychan][0]['position'])
        z = self.to_mm(self.dev.status_[Zchan][0]['position'])
        self._widget.pos0EditLabel.setText(str(x * 1000))
        self._widget.pos1EditLabel.setText(str(y * 1000))
        self._widget.pos2EditLabel.setText(str(z * 1000))
        return [x, y, z]

    # ------------------------------------------------------------------
    # Keyboard control (arrow = XY, Q/A = Z)
    # ------------------------------------------------------------------

    def keyPressed(self, event):
        if event.isAutoRepeat():
            return
        key = event.key()
        if key == QtCore.Qt.Key_Right:
            self.move_constant(False, Xchan)
        elif key == QtCore.Qt.Key_Left:
            self.move_constant(True, Xchan)
        elif key == QtCore.Qt.Key_Up:
            self.move_constant(True, Ychan)
        elif key == QtCore.Qt.Key_Down:
            self.move_constant(False, Ychan)
        elif key == QtCore.Qt.Key_Q:
            self.move_constant(True, Zchan)
        elif key == QtCore.Qt.Key_A:
            self.move_constant(False, Zchan)

    def keyReleased(self, event):
        if event.isAutoRepeat():
            return
        key = event.key()
        if key in (QtCore.Qt.Key_Right, QtCore.Qt.Key_Left):
            self.stop(Xchan)
        if key in (QtCore.Qt.Key_Up, QtCore.Qt.Key_Down):
            self.stop(Ychan)
        if key in (QtCore.Qt.Key_Q, QtCore.Qt.Key_A):
            self.stop(Zchan)

    def closeEvent(self):
        self.timer.stop()
