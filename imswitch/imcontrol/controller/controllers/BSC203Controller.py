from qtpy import QtCore

from imswitch.imcommon.model import initLogger
from ..basecontrollers import ImConWidgetController

STEPS_PER_REV = 409600
REV_PER_MM = 2

Xchan = 0   # physical X motor is on bay 0
Ychan = 1   # physical Y motor is on bay 1 (APT-forward = physical-negative on this bay)
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
        # Route absolute moves through the manager so the single clamp authority
        # (0..travelRange, unsigned-underflow guard) applies and the tracked
        # position stays in sync. Widget values are µm and already bounded ≥ 0.
        self._stageManager.setPosition(self._widget.setXEdit.value(), 'X')
        self._stageManager.setPosition(self._widget.setYEdit.value(), 'Y')
        self._stageManager.setPosition(self._widget.setZEdit.value(), 'Z')

    _BAY_TO_AXIS = {0: 'X', 1: 'Y', 2: 'Z'}

    def stopAll(self):
        # Delegate to the manager (single source of truth), as homeAll does.
        # The bare dev.stop() this used to issue was a *profiled* stop, which
        # decelerates along the bay's velocity curve and so cannot stop a bay
        # whose acceleration has been zeroed — the one state the button is
        # pressed in. The manager's version stops immediately and restores the
        # motion parameters that got the axis stuck.
        self._stageManager.stopAll()

    def stop(self, axis):
        """Stop one bay (0/1/2 — kept as bay indices for existing callers)."""
        self._stageManager.stopAxis(self._BAY_TO_AXIS[axis])

    def homeAll(self):
        # Delegate to the manager (single source of truth). Homing parks each
        # axis at its end-stop (position 0); the manager waits for completion.
        self._stageManager.homeAll()

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
        # Keep the manager's tracked position in sync with hardware so the
        # Positioner widget shows the real position, not a stale tracked value.
        self._stageManager.updateTrackedPosition(
            {'X': x * 1000, 'Y': y * 1000, 'Z': z * 1000}
        )
        return [x, y, z]

    def closeEvent(self):
        if hasattr(self, 'dev') and self.dev is not None:
            self.stopAll()
        if hasattr(self, 'timer'):
            self.timer.stop()
