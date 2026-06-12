"""WFS-shaped facade over ImSwitch hardware managers.

The Widefield-Starss workflows under
``/Users/lenny/PycharmProjects/WidefieldStarss/src/WFS/workflows`` were
written against a custom ``Microscope`` aggregator with attributes
``laser_con``, ``cam``, ``trig``, ``stage_con``, ``z_stage_con``,
``rotator_hwp``, ``rotator_qwp``. To port those workflows to ImSwitch
with minimal rewrites, we expose the same shape here and adapt to
ImSwitch's manager APIs underneath.

Concrete adapters live in this file; a mock variant for headless tests
lives in :mod:`imswitch.imcontrol.model.workflows.mock_facade`.

Construction:

    from imswitch.imcontrol.model.workflows.facade import (
        MicroscopeFacade, build_facade_from_master,
    )
    facade = build_facade_from_master(master_controller, names=...)

`build_facade_from_master` resolves managers by name from the master
controller; callers can also instantiate the sub-facades directly for
tests or partial setups.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

import numpy as np

#: readChunk consumer key for workflow camera reads (see
#: DetectorManager.readChunk — plain getChunk would steal frames from
#: concurrent consumers such as the RecordingManager or BeadRec).
_WORKFLOW_CHUNK_CONSUMER = 'WorkflowFacade'


# ---------------------------------------------------------------------------
# Laser sub-facade
# ---------------------------------------------------------------------------


class LaserConFacade:
    """WFS-shaped wrapper over one or more ImSwitch ``LaserManager`` instances.

    Args:
        lasers: Mapping from logical name (e.g. ``"488"``) to the
            ImSwitch laser manager exposing ``setEnabled``, ``setValue``,
            ``setModulationEnabled`` and ``setScanModeActive``.
    """

    def __init__(self, lasers: dict) -> None:
        self._lasers = lasers

    def _resolve(self, names: Iterable[str]) -> List:
        out = []
        for n in names:
            if n not in self._lasers:
                raise KeyError(f"Laser {n!r} not in facade. Known: {list(self._lasers)}")
            out.append(self._lasers[n])
        return out

    def laser_off(self, names: Iterable[str]) -> None:
        for laser in self._resolve(names):
            laser.setEnabled(False)

    def laser_on(self, names: Iterable[str]) -> None:
        for laser in self._resolve(names):
            laser.setEnabled(True)

    def set_constant_power(self, names: Sequence[str], powers: Sequence[float]) -> None:
        """Continuous-emission mode at the given power for each laser."""
        if len(names) != len(powers):
            raise ValueError(f"names ({len(names)}) and powers ({len(powers)}) length mismatch")
        for laser, power in zip(self._resolve(names), powers):
            laser.setScanModeActive(False)
            laser.setEnabled(True)
            laser.setValue(power)

    def set_triggered_mode(self, names: Sequence[str], powers: Sequence[float]) -> None:
        """Digital-modulation mode — laser fires only on external TTL HIGH."""
        if len(names) != len(powers):
            raise ValueError(f"names ({len(names)}) and powers ({len(powers)}) length mismatch")
        for laser, power in zip(self._resolve(names), powers):
            laser.setScanModeActive(True)
            laser.setModulationEnabled(True)
            try:
                laser.setModulationPower(power)  # type: ignore[attr-defined]
            except AttributeError:
                laser.setValue(power)

    def set_modulation_mode(self, names: Optional[Iterable[str]]) -> None:
        """Exit triggered/constant-power mode for the given lasers, or all.

        Mirrors the WFS ``laser_con.set_modulation_mode`` shape; passing
        ``None`` resets every known laser.
        """
        targets = self._lasers.values() if names is None else self._resolve(names)
        for laser in targets:
            laser.setScanModeActive(False)


# ---------------------------------------------------------------------------
# Camera sub-facade
# ---------------------------------------------------------------------------


class CamFacade:
    """WFS-shaped wrapper over an ImSwitch ``DetectorManager``.

    The WFS camera object exposed ``prepare_acquisition(n)``,
    ``start_acquisition``, ``stop_acquisition``, ``get_data``,
    ``prepare_live``, ``start_live``, ``stop_live``,
    ``wait_for_frame(timeout_s)`` and a mutable ``expo`` attribute. We
    map those onto ImSwitch's ``startAcquisition`` / ``stopAcquisition``
    / ``getLatestFrame`` / ``getChunk`` and the ``Exposure`` parameter.
    """

    def __init__(self, detector) -> None:
        self._detector = detector
        self._n_planned: Optional[int] = None

    # Acquisition lifecycle ------------------------------------------------

    def prepare_acquisition(self, n_frames: int) -> None:
        self._n_planned = int(n_frames)
        # Fresh consumer queue so no stale frames from a previous run leak in
        self._detector.releaseChunkConsumer(_WORKFLOW_CHUNK_CONSUMER)

    def start_acquisition(self) -> None:
        self._detector.startAcquisition()

    def stop_acquisition(self) -> None:
        self._detector.stopAcquisition()
        self._n_planned = None

    def prepare_live(self) -> None:
        self._n_planned = None
        self._detector.releaseChunkConsumer(_WORKFLOW_CHUNK_CONSUMER)

    def start_live(self) -> None:
        self._detector.startAcquisition()

    def stop_live(self) -> None:
        self._detector.stopAcquisition()

    # Data ----------------------------------------------------------------

    def get_data(self):
        """Return all frames acquired so far as an ndarray, or ``None``.

        Reads through DetectorManager.readChunk so concurrent consumers
        (RecordingManager, BeadRec) each still receive every frame.
        """
        frames = self._detector.readChunk(_WORKFLOW_CHUNK_CONSUMER)
        if frames is None or len(frames) == 0:
            return None
        return np.asarray(frames)

    def wait_for_frame(self, timeout_s: float = 2.0) -> bool:
        """Block until at least one frame is available, or ``timeout_s`` elapses."""
        import time
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            frame = self._detector.getLatestFrame()
            if frame is not None and getattr(frame, "size", 0) > 0:
                return True
            time.sleep(0.01)
        return False

    # Parameters ----------------------------------------------------------

    @property
    def expo(self) -> float:
        """Exposure in microseconds (matches WFS attribute name)."""
        params = getattr(self._detector, "parameters", {}) or {}
        if "Exposure" in params:
            return float(params["Exposure"].value)
        return 0.0

    @expo.setter
    def expo(self, value_us: float) -> None:
        self._detector.setParameter("Exposure", float(value_us))


# ---------------------------------------------------------------------------
# Trigger / pulse-generator sub-facade
# ---------------------------------------------------------------------------


class TrigFacade:
    """WFS-shaped wrapper around a Teensy running WFS pulse-generator firmware.

    Two construction modes are supported:

    1. **WFS pass-through (Option A).** Pass ``wfs_serial_port`` (e.g.
       ``"COM7"``) and the facade opens a direct ``pyserial`` connection.
       :meth:`snap_trigger`, :meth:`command`, and :meth:`Sendsignal` then
       speak the WFS protocol — ``Snap,...`` and ``Parameters,...`` lines
       on the wire — as documented in
       ``/Users/lenny/PycharmProjects/WidefieldStarss/src/WFS/module_arduino.py``.
       No ImSwitch ``PulseGeneratorManager`` is used.

    2. **ImSwitch pulse generator.** Pass a ``pulsegen``
       (``TeensyPulseManager``/``PulseStreamerManager``). :meth:`snap_trigger`
       routes to ``pulsegen.snap``. :meth:`command` and :meth:`Sendsignal`
       remain ``NotImplementedError`` in this mode because the WFS arrays
       have not been translated to ``PulseStep`` lists yet (Option B).

    Args:
        pulsegen: An ImSwitch ``PulseGeneratorManager`` (or ``None``).
        wfs_serial_port: Serial port name (``"COM7"``) for the WFS-firmware
            Teensy. When set, takes priority over ``pulsegen``.
        wfs_baudrate: Baud rate for the WFS Teensy (default 115200).
        wfs_timeout: Per-read timeout (s) for the WFS Teensy (default 0.1).
    """

    def __init__(
        self,
        pulsegen=None,
        wfs_serial_port: Optional[str] = None,
        wfs_baudrate: int = 115200,
        wfs_timeout: float = 0.1,
    ) -> None:
        self._pulsegen = pulsegen
        self._wfs = None  # pyserial.Serial when WFS pass-through is active

        if wfs_serial_port:
            self._wfs = self._open_wfs(wfs_serial_port, wfs_baudrate, wfs_timeout)

    # --- WFS serial lifecycle ------------------------------------------

    @staticmethod
    def _open_wfs(port: str, baudrate: int, timeout: float):
        """Open the WFS-firmware Teensy with the DTR reset dance from WFS."""
        import time
        try:
            import serial
        except ImportError:
            raise RuntimeError(
                "pyserial is required for the WFS Teensy pass-through. "
                "Install with `pip install pyserial`."
            )
        ser = serial.Serial(port=port, baudrate=baudrate, timeout=timeout)
        if not ser.is_open:
            raise RuntimeError(f"Could not open WFS Teensy port {port}")
        # Match the WFS Arduino class init sequence so the Teensy boots into
        # a clean state regardless of prior session.
        time.sleep(0.7)
        ser.dtr = False
        time.sleep(0.2)
        ser.dtr = True
        time.sleep(0.5)
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        return ser

    def close(self) -> None:
        if self._wfs is not None:
            try:
                self._wfs.close()
            except Exception:
                pass
            self._wfs = None

    @property
    def connected(self) -> bool:
        if self._wfs is not None:
            return bool(self._wfs.is_open)
        return self._pulsegen is not None and bool(
            getattr(self._pulsegen, "connected", True)
        )

    # --- Low-level WFS helper -----------------------------------------

    def _wfs_write_read(self, payload: str, overall_timeout: float = 2.0) -> list:
        """Send a WFS-protocol line; read response lines until DONE/ERR.

        Mirrors ``WFS.module_arduino.Arduino.write_read`` so workflow
        behaviour matches the reference implementation byte for byte.
        """
        import time
        if self._wfs is None:
            driver = getattr(self._pulsegen, "driver", None)
            send_recv = getattr(driver, "_send_recv", None)
            if callable(send_recv):
                line = send_recv(
                    payload,
                    terminal=("DONE", "ERR"),
                    timeout=overall_timeout,
                )
                return [line] if line else []
            raise RuntimeError(
                "WFS Teensy serial is not connected. Configure TrigFacade "
                "with wfs_serial_port or an ImSwitch Teensy pulse generator "
                "that exposes the legacy serial driver."
            )
        self._wfs.reset_input_buffer()
        self._wfs.reset_output_buffer()
        self._wfs.write(payload.encode())
        self._wfs.flush()

        lines = []
        t0 = time.monotonic()
        while time.monotonic() - t0 < overall_timeout:
            raw = self._wfs.readline()
            if not raw:
                continue
            try:
                line = raw.decode("utf-8", errors="replace").strip()
            except Exception:
                line = str(raw)
            if line:
                lines.append(line)
                if "DONE" in line or "ERR" in line:
                    break
        return lines

    # --- High-level helpers used by every workflow ----------------------

    def snap_trigger(self, laser_pin: int, camera_pin: int, exposure_us: int) -> None:
        """Fire one synchronised laser+camera pulse.

        WFS mode: sends ``"Snap,<laser_pin>,<camera_pin>,<exposure_us>\\n"`` and
        waits for ``DONE``/``ERR`` (or exposure-based timeout) on the Teensy.

        Pulsegen mode: emits a single :class:`PulseStep` pair via
        ``pulsegen.snap``.
        """
        if self._wfs is not None:
            payload = f"Snap,{int(laser_pin)},{int(camera_pin)},{int(exposure_us)}\n"
            timeout = int(exposure_us) * 1e-6 + 1.0
            self._wfs_write_read(payload, overall_timeout=timeout)
            return

        driver = getattr(self._pulsegen, "driver", None)
        send_recv = getattr(driver, "_send_recv", None)
        if callable(send_recv):
            payload = f"Snap,{int(laser_pin)},{int(camera_pin)},{int(exposure_us)}\n"
            timeout = int(exposure_us) * 1e-6 + 1.0
            self._wfs_write_read(payload, overall_timeout=timeout)
            return

        if self._pulsegen is None:
            raise RuntimeError(
                "TrigFacade.snap_trigger called but neither WFS Teensy nor "
                "pulse generator is configured."
            )
        width_ns = int(exposure_us) * 1000
        self._pulsegen.snap(channels=[int(laser_pin), int(camera_pin)], width_ns=width_ns)

    # --- WFS pulse-scheme: numpy translation + serial send -------------

    def command(
        self,
        start488,
        start405,
        start_camera,
        width488,
        width405,
        width_camera,
        dwelltime,
    ):
        """Build the padded ``(tWindowM, laserMod_1, laserMod_2, laserMod_3)``
        arrays that drive the WFS Teensy.

        This is a direct port of ``WFS.module_arduino.Arduino.command`` —
        pure numpy, no hardware interaction. See the source for the
        original implementation and rationale.

        Args:
            start488: Comma-separated string OR scalar; start time(s) for the
                488 nm laser pulse(s) in µs.
            start405: Start time for the 405 nm laser pulse (µs).
            start_camera: Start time for the camera exposure (µs).
            width488: Comma-separated string OR scalar; pulse width(s) for 488 in µs.
            width405: Pulse width for 405 in µs.
            width_camera: Camera exposure width in µs.
            dwelltime: Total cycle duration in µs.

        Returns:
            Tuple ``(tWindowM, laserMod_1, laserMod_2, laserMod_3)``, each
            zero-padded to length 16.
        """
        import numpy as np

        offstart = np.array([int(float(x)) for x in str(start488).split(",")])
        start = np.append(offstart, float(start405))
        start = np.append(start, float(start_camera))

        offend = np.array([int(x) for x in str(width488).split(",") if str(x).strip().isdigit()])
        width = np.append(offend, float(width405))
        width = np.append(width, float(width_camera))

        tWindowM, laserMod = self._pulse_scheme(width, start, int(dwelltime))
        laserMod = laserMod.astype(np.int32)
        tWindowM = tWindowM.astype(np.int32)

        if np.size(start) > 3:
            laserMod[1] = laserMod[0] + laserMod[1]
            laserMod[1] = np.clip(laserMod[1], 0, 1)
            laserMod = np.delete(laserMod, 0, 0)

        return (
            self._pad16(tWindowM),
            self._pad16(laserMod[0, :]),
            self._pad16(laserMod[1, :]),
            self._pad16(laserMod[2, :]),
        )

    @staticmethod
    def _pulse_scheme(pulseWidths, tStart, dwellTime):
        """Pure-numpy pulse-scheme generator — verbatim from WFS."""
        import numpy as np
        pulse = np.zeros((np.size(tStart, 0), 2))
        tEnd = np.add(tStart, pulseWidths)
        for x in range(np.size(tStart)):
            pulse[x] = [tStart[x], tEnd[x]]
        pulse = np.reshape(pulse, (1, np.size(tStart, 0) * 2))

        pulseON = np.zeros(np.size(pulse))
        pulseOFF = np.zeros(np.size(pulse))
        laserN = np.arange(1, (np.size(pulse, 1) / 2) + 1, 1)
        selON = np.arange(0, np.size(pulse), 2)
        selOFF = np.arange(1, np.size(pulse), 2)
        pulseON[selON] = laserN
        pulseOFF[selOFF] = laserN
        pulseSort = np.sort(pulse)
        indxs = np.argsort(pulse)
        pulseONsort = pulseON[indxs]
        pulseOFFsort = pulseOFF[indxs]

        laserMod = np.zeros((np.size(laserN), np.size(pulseSort) + 1))
        for x in range(np.size(pulseSort)):
            laserMod[:, x + 1] = laserMod[:, x]
            pONsBool = int(pulseONsort[0, x])
            pOFFsBool = int(pulseOFFsort[0, x])
            if pONsBool != 0:
                laserMod[pONsBool - 1, x + 1] = 1
            if pOFFsBool != 0:
                laserMod[pOFFsBool - 1, x + 1] = 0

        tLab = np.zeros(np.size(pulseSort) + 2)
        tLab[1:-1] = pulseSort
        tLab[-1] = dwellTime
        tWindowM = np.zeros(np.size(laserMod, 1))
        for x in range(np.size(tLab) - 1):
            tWindowM[x] = tLab[x + 1] - tLab[x]

        cond1 = np.where(tWindowM == 0)
        tWindowM = np.delete(tWindowM, cond1, 0)
        laserMod = np.delete(laserMod, cond1, 1)
        return tWindowM, laserMod

    @staticmethod
    def _pad16(arr):
        import numpy as np
        out = np.zeros(16)
        out[: np.size(arr)] = arr
        return out

    def Sendsignal(
        self,
        pin488,
        pin405,
        camerapin,
        delay_time,
        frame_number,
        tWindowM,
        laserMod_1,
        laserMod_2,
        laserMod_3,
    ) -> None:
        """Stream a pre-computed pulse scheme to the WFS Teensy.

        Direct port of ``WFS.module_arduino.Arduino.Sendsignal``: formats a
        ``"Parameters,...\\n"`` line and writes it to the Teensy, which
        runs the entire frame_number-long sequence on its own.

        Requires WFS pass-through mode (i.e. construction with
        ``wfs_serial_port``).
        """
        import numpy as np

        frame_arr = np.zeros(1)
        frame_arr[0] = int(frame_number)

        payload = (
            "Parameters,"
            + str(np.asarray(tWindowM).astype(np.int32)).replace("\n", "")
            + "," + str(int(pin488))
            + "," + str(np.asarray(laserMod_1).astype(np.int16))
            + "," + str(int(pin405))
            + "," + str(np.asarray(laserMod_2).astype(np.int16))
            + "," + str(int(camerapin))
            + "," + str(np.asarray(laserMod_3).astype(np.int16))
            + "," + str(int(delay_time))
            + "," + str(frame_arr.astype(np.int16))
            + "\n"
        )
        # WFS uses a generous timeout because the Teensy holds the line
        # until the entire frame_number sequence completes.
        self._wfs_write_read(payload, overall_timeout=max(5.0, float(frame_number) * 0.5))


# ---------------------------------------------------------------------------
# Stage sub-facades
# ---------------------------------------------------------------------------


class StageConFacade:
    """WFS-shaped wrapper over a 2-axis XY ``PositionerManager``.

    WFS used raw integer "stage units" via ``move_to(x, y)``. The
    underlying ``KinesisStageManager`` honours
    :attr:`KinesisStageManager._driver_units_per_position_unit` from
    config, so we forward values unchanged and let the manager apply
    the scaling.
    """

    def __init__(self, positioner, x_axis: str = "X", y_axis: str = "Y") -> None:
        self._positioner = positioner
        self._x = x_axis
        self._y = y_axis

    def move_to(self, x: float, y: float) -> None:
        self._positioner.setPosition(x, self._x)
        self._positioner.setPosition(y, self._y)

    def get_position(self) -> tuple:
        pos = self._positioner.position
        return (pos[self._x], pos[self._y])

    def jog_start(self, axis: str, sign: int) -> None:
        if hasattr(self._positioner, "jog_start"):
            self._positioner.jog_start(axis, sign)

    def jog_stop(self, axis: str) -> None:
        if hasattr(self._positioner, "jog_stop"):
            self._positioner.jog_stop(axis)


class ZStageConFacade:
    """WFS-shaped wrapper over a single-axis Z piezo (``JenaPiezoZManager``)."""

    def __init__(self, positioner, axis: str = "Z") -> None:
        self._positioner = positioner
        self._axis = axis

    def read_pos_um(self) -> float:
        return float(self._positioner.position[self._axis])

    def set_pos_um(self, value_um: float) -> None:
        self._positioner.setPosition(float(value_um), self._axis)

    def activate_ext_control(self) -> None:
        if hasattr(self._positioner, "activate_ext_control"):
            self._positioner.activate_ext_control()

    def deactivate_ext_control(self) -> None:
        if hasattr(self._positioner, "deactivate_ext_control"):
            self._positioner.deactivate_ext_control()

    @property
    def pos_range_um(self) -> tuple:
        rng = getattr(self._positioner, "_posRangeUm", (0, 100))
        return (float(rng[0]), float(rng[1]))


# ---------------------------------------------------------------------------
# Rotator sub-facade
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RotatorPresets:
    """H/V angles (degrees) for a polarisation-control rotator."""

    h_deg: float
    v_deg: float


class RotatorFacade:
    """WFS-shaped wrapper over an ``ElliptecRotatorManager`` (HWP or QWP)."""

    def __init__(self, rotator, presets: Optional[RotatorPresets] = None) -> None:
        self._rotator = rotator
        self._presets = presets or RotatorPresets(h_deg=0.0, v_deg=90.0)

    @property
    def presets(self) -> RotatorPresets:
        return self._presets

    def move_abs(self, deg: float) -> None:
        self._rotator.move_abs(float(deg))

    def move_rel(self, deg: float) -> None:
        self._rotator.move_rel(float(deg))

    def position(self) -> float:
        return float(self._rotator.position)

    def move_to_h(self) -> None:
        self.move_abs(self._presets.h_deg)

    def move_to_v(self) -> None:
        self.move_abs(self._presets.v_deg)

    def chained_move_to_h(self, follow_up_callable) -> None:
        """Move this rotator to H, then call ``follow_up_callable`` (the
        partner rotator's ``move_to_h``)."""
        self.move_to_h()
        if follow_up_callable is not None:
            follow_up_callable()

    def chained_move_to_v(self, follow_up_callable) -> None:
        self.move_to_v()
        if follow_up_callable is not None:
            follow_up_callable()


def _rotator_presets_from_manager(rotator) -> Optional[RotatorPresets]:
    """Read workflow H/V presets from a rotator's setup managerProperties."""
    rotator_info = getattr(rotator, "_rotatorInfo", None)
    props = getattr(rotator_info, "managerProperties", None) or {}
    raw = props.get("workflowPresets") or props.get("workflow_presets")
    if raw is None:
        return None
    return RotatorPresets(
        h_deg=float(raw.get("h_deg", raw.get("h", 0.0))),
        v_deg=float(raw.get("v_deg", raw.get("v", 90.0))),
    )


# ---------------------------------------------------------------------------
# Aggregate facade + builder
# ---------------------------------------------------------------------------


@dataclass
class MicroscopeFacade:
    """WFS-shaped aggregator. Pass this single object into a workflow.

    Any field may be ``None`` if a workflow does not need it (tests can
    construct partial facades).
    """

    laser_con: Optional[LaserConFacade] = None
    cam: Optional[CamFacade] = None
    trig: Optional[TrigFacade] = None
    stage_con: Optional[StageConFacade] = None
    z_stage_con: Optional[ZStageConFacade] = None
    rotator_hwp: Optional[RotatorFacade] = None
    rotator_qwp: Optional[RotatorFacade] = None


def build_facade_from_master(
    master,
    *,
    laser_aliases: Optional[dict] = None,
    detector_name: Optional[str] = None,
    pulsegen_name: str = "teensyPulse",
    wfs_teensy_port: Optional[str] = None,
    wfs_teensy_baudrate: int = 115200,
    xy_positioner_name: Optional[str] = None,
    z_positioner_name: Optional[str] = None,
    hwp_name: Optional[str] = "HWP",
    qwp_name: Optional[str] = "QWP",
    hwp_presets: Optional[RotatorPresets] = None,
    qwp_presets: Optional[RotatorPresets] = None,
) -> MicroscopeFacade:
    """Build a :class:`MicroscopeFacade` from an ImSwitch ``MasterController``.

    Each name argument is optional — if omitted the corresponding
    sub-facade is left as ``None``. ``laser_aliases`` lets you map
    logical workflow names (``"488"``) to ImSwitch laser names from the
    setup JSON (e.g. ``"488 (EXC) sn27311"``):

        build_facade_from_master(master, laser_aliases={
            "488": "488 (EXC) sn27311",
            "405": "405 (ACT) sn26647",
        })
    """
    facade = MicroscopeFacade()

    if laser_aliases:
        lasers = {
            logical: master.lasersManager[real]
            for logical, real in laser_aliases.items()
        }
        facade.laser_con = LaserConFacade(lasers)

    if detector_name is not None:
        facade.cam = CamFacade(master.detectorsManager[detector_name])

    # Trigger sub-facade: prefer WFS pass-through when a Teensy port was
    # given (Option A in docs/design/plans/wfs-workflows-port.md). Otherwise
    # fall back to ImSwitch's PulseGeneratorManager if present.
    if wfs_teensy_port is not None:
        facade.trig = TrigFacade(
            pulsegen=None,
            wfs_serial_port=wfs_teensy_port,
            wfs_baudrate=wfs_teensy_baudrate,
        )
    else:
        pulsegen = getattr(master, "pulseGeneratorManager", None) or getattr(
            master, "pulsegenManager", None
        )
        if pulsegen is not None:
            facade.trig = TrigFacade(pulsegen)

    if xy_positioner_name is not None:
        facade.stage_con = StageConFacade(
            master.positionersManager[xy_positioner_name]
        )

    if z_positioner_name is not None:
        facade.z_stage_con = ZStageConFacade(
            master.positionersManager[z_positioner_name]
        )

    # NB: MultiManager (parent of RotatorsManager) defines __getitem__ and
    # __iter__ (yielding `(name, manager)` tuples) but NOT __contains__, so
    # ``name in rotators_manager`` always returns False. We must look up by
    # key via __getitem__ and catch the NoSuchSubManagerError.
    rotators_manager = getattr(master, "rotatorsManager", None)
    if rotators_manager is not None:
        if hwp_name:
            try:
                hwp = rotators_manager[hwp_name]
                facade.rotator_hwp = RotatorFacade(
                    hwp,
                    hwp_presets or _rotator_presets_from_manager(hwp),
                )
            except Exception as exc:
                # Either the rotator is not in the config, or the manager
                # raised. Either way leave facade.rotator_hwp = None and
                # let the workflow fail loudly when it tries to use it.
                import logging
                logging.getLogger(__name__).warning(
                    f"Could not attach HWP rotator named {hwp_name!r}: {exc}"
                )
        if qwp_name:
            try:
                qwp = rotators_manager[qwp_name]
                facade.rotator_qwp = RotatorFacade(
                    qwp,
                    qwp_presets or _rotator_presets_from_manager(qwp),
                )
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning(
                    f"Could not attach QWP rotator named {qwp_name!r}: {exc}"
                )

    return facade


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
