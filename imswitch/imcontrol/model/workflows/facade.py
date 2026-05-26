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

    def start_acquisition(self) -> None:
        self._detector.startAcquisition()

    def stop_acquisition(self) -> None:
        self._detector.stopAcquisition()
        self._n_planned = None

    def prepare_live(self) -> None:
        self._n_planned = None

    def start_live(self) -> None:
        self._detector.startAcquisition()

    def stop_live(self) -> None:
        self._detector.stopAcquisition()

    # Data ----------------------------------------------------------------

    def get_data(self):
        """Return all frames acquired so far as an ndarray, or ``None``."""
        return self._detector.getChunk()

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
    """WFS-shaped wrapper over a ``PulseGeneratorManager`` (e.g. Teensy).

    Phase 1 will flesh out :meth:`command` and :meth:`Sendsignal` to
    translate WFS-style pulse arrays (``tWindowM``, ``laserMod_1..3``)
    into ``PulseStep`` lists. For now :meth:`snap_trigger` is provided
    because the simple Z-stack and tiling paths use it directly.

    Args:
        pulsegen: A ``PulseGeneratorManager`` (e.g. ``TeensyPulseManager``)
            or ``None`` to make the facade a no-op in software-only paths.
    """

    def __init__(self, pulsegen=None) -> None:
        self._pulsegen = pulsegen

    @property
    def connected(self) -> bool:
        return self._pulsegen is not None and bool(getattr(self._pulsegen, "connected", True))

    # --- High-level helpers used directly by Z-stack / tiling -----------

    def snap_trigger(self, laser_pin: int, camera_pin: int, exposure_us: int) -> None:
        """Fire a single synchronised laser+camera pulse.

        Camera pin goes HIGH for ``exposure_us`` microseconds; laser pin
        is held HIGH for the same duration. Both drop together.
        """
        if self._pulsegen is None:
            raise RuntimeError(
                "TrigFacade.snap_trigger called but no pulse generator is configured."
            )
        width_ns = int(exposure_us) * 1000
        self._pulsegen.snap(channels=[int(laser_pin), int(camera_pin)], width_ns=width_ns)

    # --- WFS pulse-scheme entry points (to be completed in Phase 1) ----

    def command(self, *args, **kwargs):
        """Pre-compute a pulse scheme. Phase 1 will translate WFS arrays
        into :class:`PulseStep` sequences for ImSwitch's pulse generator.
        """
        raise NotImplementedError(
            "TrigFacade.command — Phase 1 will translate WFS pulse arrays "
            "(tWindowM, laserMod_*) into PulseStep lists. See plan in "
            "docs/design/plans/wfs-workflows-port.md."
        )

    def Sendsignal(self, *args, **kwargs):
        """Run a pre-computed pulse scheme. See :meth:`command`."""
        raise NotImplementedError(
            "TrigFacade.Sendsignal — Phase 1 will program a PulseStep list "
            "via pulsegen.program_sequence() and start it with pulsegen.run()."
        )


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

    rotators_manager = getattr(master, "rotatorsManager", None)
    if rotators_manager is not None:
        if hwp_name and hwp_name in rotators_manager:
            facade.rotator_hwp = RotatorFacade(rotators_manager[hwp_name], hwp_presets)
        if qwp_name and qwp_name in rotators_manager:
            facade.rotator_qwp = RotatorFacade(rotators_manager[qwp_name], qwp_presets)

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
