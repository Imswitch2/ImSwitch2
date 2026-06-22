from imswitch.imcommon.model import initLogger
from .PositionerManager import PositionerManager

STEPS_PER_REV = 409600
REV_PER_MM = 2


move_step_mm = 1


class BSC203StageManager(PositionerManager):

    def __init__(self, positionerInfo, name, **lowLevelManagers):
        super().__init__(positionerInfo, name, initialPosition={
            axis: 0 for axis in positionerInfo.axes
        })
        self.__logger = initLogger(self, instanceName=name)
        props = positionerInfo.managerProperties
        home = props.get('home', False)
        port = props.get('port', 'COM9')
        # Full travel per axis in µm (DRV208 actuators = 8 mm). Used to compute
        # mid-travel when recentring after homing. Absolute coordinates stay
        # 0..travelRangeUm; the centre is simply travelRangeUm / 2.
        self._travelRangeUm = props.get('travelRangeUm', 8000)
        # Axes recentred to mid-travel after homing. APT homing always drives to
        # a physical end-stop (position 0), an awkward place to leave an XY
        # sample stage, so X/Y are moved to the centre by default. Z is left at
        # its homed edge — the natural reference for a focus axis. Set to [] in
        # managerProperties to disable recentring entirely.
        self._centerAxesOnHome = props.get('centerAxesOnHome', ['X', 'Y'])

        try:
            from thorlabs_apt_device.devices.bsc import BSC
            from serial.serialutil import SerialException
            self.dev = BSC(serial_port=port, vid=None, pid=None, manufacturer=None, product=None, serial_number=None,
                           location=None, home=home, x=3, invert_direction_logic=False, swap_limit_switches=True)
        except ImportError as e:
            self.__logger.warning(
                f'Failed to import thorlabs_apt_device: {e}. '
                'Install with: pip install thorlabs_apt_device'
            )
            self.dev = None
        except Exception as e:
            self.__logger.debug(f'Could not initialize BSC203 motorized stage: {e}')
            self.dev = None
        if home:
            self.__logger.debug('Is homing')
            while self.homing():
                pass
            self.__logger.debug('Finished homing')
            # We just homed to the edge; recentre X/Y so the operator starts at
            # mid-travel. This sets the tracked position to the centre target,
            # so no controller read-back is needed (or wanted — the move may
            # still be in flight).
            self.centerAxes()
        else:
            # Not homing this session — show the controller's actual (absolute)
            # position at startup instead of resetting to 0. The BSC203 tracks
            # position in encoder steps and retains it while powered, so reading
            # it back here keeps positions reproducible across ImSwitch restarts
            # without having to re-home every time.
            self._syncPositionFromController()

    def _syncPositionFromController(self, timeout=3.0):
        """Set ``self._position`` from the controller's reported encoder position.

        The BSC polls status continuously (thorlabs_apt_device uses
        ``status_updates="polled"`` for this controller), filling
        ``dev.status_[bay][channel]`` from a background thread. The catch: that
        dict is pre-initialised with ``position=0`` and ``msg=''`` *before* any
        reply arrives, so reading ``position`` immediately just echoes 0. We
        therefore wait until ``msg`` is non-empty (a real status reply has
        landed) before trusting ``position``, then convert steps -> micron (the
        unit this manager works in). If the controller never reports (e.g. no
        hardware), the position is left at the initial 0 so a missing status
        neither blocks nor crashes startup.
        """
        if self.dev is None:
            return
        import time
        # X is wired to bay 0, Y to bay 1 — matches BSC203Controller (Xchan=0, Ychan=1).
        axisToBay = {'X': 0, 'Y': 1, 'Z': 2}
        deadline = time.time() + timeout
        for axis in self._position:
            bay = axisToBay.get(axis)
            if bay is None:
                continue
            status = None
            while True:
                try:
                    candidate = self.dev.status_[bay][0]
                    # 'msg' stays '' until a real polled status reply updates it.
                    if candidate.get('msg'):
                        status = candidate
                except (IndexError, KeyError, TypeError):
                    status = None
                if status is not None or time.time() >= deadline:
                    break
                time.sleep(0.05)
            if status is None:
                self.__logger.warning(
                    f'{axis}: BSC203 controller sent no status update within '
                    f'{timeout:.1f}s; leaving position at 0 (relative until '
                    f'homed/moved).'
                )
                continue
            steps = status.get('position', 0)
            self._position[axis] = self.to_mm(steps) * 1000
            self.__logger.info(
                f'{axis}: startup position {self._position[axis]:.3f} um '
                f'({steps} steps, homed={status.get("homed")}) read from '
                f'BSC203 controller'
            )

    def initialize(self):
        self.dev.set_velocity_params(acceleration=4506, max_velocity=21987328 * 5, bay=0, channel=0)
        self.dev.set_velocity_params(acceleration=4506, max_velocity=21987328 * 5, bay=1, channel=0)
        self.dev.set_velocity_params(acceleration=4506, max_velocity=21987328 * 5, bay=2, channel=0)

    def homeAll(self):
        self.dev.home(bay=0)
        self.dev.home(bay=1)
        self.dev.home(bay=2)
        while self.homing():
            pass
        self.centerAxes()

    def centerAxes(self):
        """Move the configured axes to mid-travel after homing.

        APT homing drives each axis to a physical end-stop (position 0). For an
        XY sample stage that is an awkward corner to start from, so the axes in
        ``_centerAxesOnHome`` (X/Y by default) are moved to ``travelRangeUm / 2``
        so the operator can jog or Go-To in both directions immediately. Z is
        left at its homed edge. Absolute coordinates are unchanged: the centre is
        just a positive position (``travelRangeUm / 2``), not a new origin.
        """
        if self.dev is None:
            return
        centre = self._travelRangeUm / 2
        for axis in self._centerAxesOnHome:
            if axis in self._position:
                self.__logger.info(
                    f'{axis}: recentring to {centre:.0f} µm (mid-travel) after homing'
                )
                self.setPosition(centre, axis)

    def homing(self):
        return not all([self.dev.status_[0][0]['homed'],
                        self.dev.status_[1][0]['homed'],
                        self.dev.status_[2][0]['homed']])

    def to_enc_steps(self, mm):
        steps = mm * REV_PER_MM * STEPS_PER_REV
        return int(steps)

    def to_mm(self, steps):
        mm = steps / (REV_PER_MM * STEPS_PER_REV)
        return mm

    def move(self, dist, axis):
        self._position[axis] = self._position[axis] + dist
        if axis == "X":
            channel = 0  # X is wired to bay 0
        elif axis == "Y":
            channel = 1  # Y is wired to bay 1
        elif axis == "Z":
            channel = 2
        self.move_relative_mm(dist, channel)

    def setPosition(self, value, axis):
        if axis == "X":
            channel = 0  # X is wired to bay 0
        elif axis == "Y":
            channel = 1  # Y is wired to bay 1
        elif axis == "Z":
            channel = 2
        self._position[axis] = value
        pos = self.to_enc_steps(value / 1000)
        # BSC203 firmware interprets the absolute-position field as an unsigned
        # 32-bit integer.  A negative Python int is packed as a signed two's-
        # complement value by the APT library, but the firmware reads it as a
        # huge positive number and drives the motor continuously toward an
        # unreachable target.  Clamp to 0 (home end) instead.
        if pos < 0:
            self.__logger.warning(
                f'{axis}: requested absolute position {value:.1f} µm maps to '
                f'{pos} encoder steps (< 0); clamping to 0. '
                f'BSC203 firmware uses unsigned positions — check your target.'
            )
            pos = 0
        self.dev.move_absolute(pos, now=True, bay=channel, channel=0)

    # Bay number whose APT "forward" direction is the physical NEGATIVE direction.
    # Determined by the velocity key mapping in BSC203Controller:
    #   bay 0 (X): Right key → move_velocity(True/forward) → physical positive  (normal)
    #   bay 1 (Y): Up   key → move_velocity(False/reverse) → physical positive  (inverted)
    #   bay 2 (Z): Q    key → move_velocity(True/forward)  → physical positive  (normal)
    # Bay 1 (Y) is the only bay where APT-forward maps to the physical-negative direction.
    _APT_FWD_IS_NEGATIVE_BAYS = frozenset({1})  # bay 1 = Y axis

    def move_relative_mm(self, value, axis):
        """Move `value` µm on hardware bay `axis` using the jog mechanism.

        The BSC203 firmware does not handle negative distances in
        MGMSG_MOT_MOVE_RELATIVE correctly — a negative signed value is
        interpreted as a huge unsigned count, firing the motor at full
        speed until the hardware end-stop.  The old code worked around this
        by always using a positive jog-step size and an explicit direction
        flag.  We do the same here, using the velocity parameters already
        configured on that bay so the speed is consistent with normal moves.
        """
        if value == 0:
            return
        steps = self.to_enc_steps(abs(value) / 1000)
        if steps == 0:
            return
        # Determine the APT jog direction that produces the desired physical
        # direction.  For bay 1 (X) APT-forward = physical-negative, so a
        # negative value needs direction=True; for Y/Z it is the opposite.
        if axis in self._APT_FWD_IS_NEGATIVE_BAYS:
            direction = value < 0   # True → APT forward → physical negative (X)
        else:
            direction = value > 0   # True → APT forward → physical positive (Y/Z)
        # Use the move-velocity parameters already set on this bay so jog
        # speed is consistent with what the user configured.
        vel = self.dev.velparams_[axis][0].get('max_velocity',
                                               int(300 / 1000 * 21987328))
        acc = self.dev.velparams_[axis][0].get('acceleration',
                                               int(4000 / 1000 * 4506))
        self.dev.set_jog_params(steps, acc, vel,
                                continuous=False, immediate_stop=False,
                                bay=axis, channel=0)
        self.dev.move_jog(direction=direction, bay=axis, channel=0)

    def get_abs(self, axis):
        return self._position[axis]

    def closeEvent(self):
        pass


# Copyright (C) 2020, 2021 The imswitch developers
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
