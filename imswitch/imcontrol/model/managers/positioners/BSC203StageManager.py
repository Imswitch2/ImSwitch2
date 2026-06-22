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
        # Full travel per axis in µm (DRV208 actuators = 8 mm). Used as the upper
        # bound when clamping moves. Coordinates run 0..travelRangeUm.
        self._travelRangeUm = props.get('travelRangeUm', 8000)
        # Axes whose encoder counts DOWN when the APT 'forward' jog runs. By
        # default a forward jog is assumed to INCREASE the encoder (so +target
        # increases the displayed position) on every axis. If an axis physically
        # moves the wrong way / the displayed value changes opposite to the
        # command, add its label here to flip the jog direction. Confirm per rig
        # with utility_scripts/bsc203_diag.py calibrate.
        self._invertJogAxes = set(props.get('invertJogAxes', []))

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
            # The BSC constructor (home=True) already initiated homing; wait for
            # it to finish. Homing parks each axis at its end-stop (position 0).
            self._waitHomed()
            self.__logger.debug('Finished homing')
        # Show the controller's actual (absolute) position at startup. After a
        # home this reads ~0 (the end-stop); without a home it recovers the
        # retained encoder position so positions survive an ImSwitch restart.
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

    # Safety bound (s) for how long to wait for the controller to ENTER homing
    # before giving up on observing the in-progress state. Not timing-critical:
    # if homing is never observed in-progress we still confirm via 'homed' in
    # phase 2. See _waitHomed.
    _HOMING_START_TIMEOUT_S = 5.0

    def homeAll(self):
        if self.dev is None:
            return
        self.dev.home(bay=0)
        self.dev.home(bay=1)
        self.dev.home(bay=2)
        self._waitHomed()

    def _homingInProgress(self):
        """True if any bay currently reports it is homing.

        Uses the 'homing' status bit, or a cleared 'homed' flag, as the
        in-progress signal — either indicates the controller has actually started
        a home (as opposed to sitting idle and still reporting the previous
        ``homed=True``).
        """
        try:
            return any(self.dev.status_[b][0].get('homing')
                       or not self.dev.status_[b][0].get('homed')
                       for b in range(3))
        except (IndexError, KeyError, TypeError):
            return False

    def _waitHomed(self, timeout=120.0):
        """Block until all three bays finish homing, or ``timeout`` elapses.

        ``dev.home()`` only *queues* the command on the device's own I/O thread,
        and for a short while afterwards the controller is still idle and keeps
        reporting its previous ``homed=True``. The library polls status every
        ~10 ms, so a wait that trusts ``homed`` immediately is satisfied by a
        stale "idle, homed" reply and returns before homing has even started.

        Rather than betting on a fixed delay (which races the other way if homing
        is slow to *start*), we OBSERVE the controller: phase 1 waits for the
        in-progress state to appear, phase 2 waits for it to clear with ``homed``
        set. Because homing involves real motion (≫ the 10 ms poll interval), the
        in-progress state is reliably observed whether homing is fast or slow. If
        it is never seen within ``_HOMING_START_TIMEOUT_S`` (a device that does
        not signal in-progress, or a home that somehow completed before we
        looked), we fall through to phase 2, which confirms completion via
        ``homed`` — so an already-finished home is handled correctly, never
        prematurely.

        The device updates ``status_`` from its own thread, so this works even
        when called from the GUI thread (e.g. the Home button); the sleeps avoid
        a 100%-CPU busy-spin and the timeout prevents a permanent hang.
        """
        if self.dev is None:
            return
        import time
        # Phase 1 — wait for homing to actually begin.
        startDeadline = time.time() + min(self._HOMING_START_TIMEOUT_S, timeout)
        while time.time() < startDeadline:
            if self._homingInProgress():
                break
            time.sleep(0.02)
        # Phase 2 — wait for homing to complete on all bays.
        deadline = time.time() + timeout
        while self.homing():
            if time.time() >= deadline:
                self.__logger.warning(
                    f'Homing did not complete within {timeout:.0f}s; continuing.'
                )
                return
            time.sleep(0.05)

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
        """Relative move, implemented as a clamped *absolute* move.

        Implemented via :meth:`setPosition`, which jogs by the displacement.
        """
        self.setPosition(self._position[axis] + dist, axis)

    def _clampSteps(self, steps, axis):
        """Clamp an absolute encoder-step target to the reachable travel range.

        Single clamp authority for every absolute move, whatever the caller
        (Go-To button, tiling, scan return-to-origin, recentre …). Two failure
        modes are guarded:

        * **Negative target** — the BSC203 firmware reads the absolute-position
          field as an *unsigned* 32-bit integer, so a negative count underflows
          to a huge positive value and drives the motor continuously toward an
          unreachable point (the runaway). Clamp to 0.
        * **Past the far end** — a target beyond ``travelRangeUm`` just grinds the
          stage into the limit switch. Clamp to the max.
        """
        maxSteps = self.to_enc_steps(self._travelRangeUm / 1000)
        if steps < 0:
            self.__logger.warning(
                f'{axis}: absolute target {steps} steps < 0; clamping to 0 '
                f'(firmware uses unsigned positions — a negative target runs away).'
            )
            return 0
        if steps > maxSteps:
            self.__logger.warning(
                f'{axis}: absolute target {steps} steps exceeds travel '
                f'({maxSteps} steps = {self._travelRangeUm} µm); clamping to max.'
            )
            return maxSteps
        return steps

    # Set True to log every commanded move at INFO level. Leave False in
    # production; flip on to capture a "goes crazy" reproduction.
    LOG_MOVES = True

    _AXIS_TO_BAY = {"X": 0, "Y": 1, "Z": 2}

    def _currentSteps(self, channel, axis):
        """Live encoder position (steps) for a bay, with a tracked-position
        fallback if the controller has not reported yet."""
        try:
            pos = self.dev.status_[channel][0].get('position')
        except (IndexError, KeyError, TypeError):
            pos = None
        if pos is None:
            pos = self.to_enc_steps(self._position[axis] / 1000)
        return int(pos)

    def setPosition(self, value, axis):
        channel = self._AXIS_TO_BAY[axis]
        targetSteps = self._clampSteps(self.to_enc_steps(value / 1000), axis)
        self._jogToSteps(targetSteps, channel, axis)

    def _jogToSteps(self, targetSteps, channel, axis):
        """Move to absolute encoder position ``targetSteps`` using a bounded JOG.

        Why not ``move_absolute`` / ``move_relative``: on the BSC203 a move whose
        DISPLACEMENT is negative (target *below* the current position) runs away —
        the firmware reads the signed displacement as unsigned and drives the
        motor at full speed to the end-stop. This is the long-standing "negative
        direction accelerates" bug, and it fires for any downward move, even when
        the target itself is a perfectly valid positive position. Clamping the
        target to ≥0 never fixed it.

        The JOG command instead takes a POSITIVE step size plus a direction flag,
        which the firmware handles correctly in both directions. So every move —
        relative or absolute — becomes: jog ``|target - current|`` steps, forward
        when the target is higher. The target is pre-clamped to ``[0, travel]`` so
        the jog can never drive past an end-stop, and the displacement is taken
        from the *live* encoder so repeated moves self-correct.
        """
        current = self._currentSteps(channel, axis)
        delta = int(targetSteps) - current
        # Track the (clamped) target so the display reflects what we commanded;
        # the live read-back keeps it honest thereafter.
        self._position[axis] = self.to_mm(targetSteps) * 1000
        direction = delta > 0          # forward jog increases the encoder count
        if axis in self._invertJogAxes:
            direction = not direction
        if self.LOG_MOVES:
            self.__logger.info(
                f'MOVE {axis}/bay{channel}: target={targetSteps}st '
                f'current={current}st delta={delta}st '
                f'dir={"fwd" if direction else "rev"} size={abs(delta)}st (jog)'
            )
        if delta == 0:
            return
        size = abs(delta)
        # Use the bay's configured velocity so jog speed matches normal moves.
        vel = self.dev.velparams_[channel][0].get('max_velocity',
                                                   int(300 / 1000 * 21987328))
        acc = self.dev.velparams_[channel][0].get('acceleration',
                                                   int(4000 / 1000 * 4506))
        # Single-step (continuous=False) jog of exactly `size` steps, then stop.
        self.dev.set_jog_params(size, acc, vel, continuous=False,
                                immediate_stop=False, bay=channel, channel=0)
        self.dev.move_jog(direction=direction, bay=channel, channel=0)

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
