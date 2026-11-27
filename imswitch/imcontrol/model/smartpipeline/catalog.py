"""The action catalogue: the actions a pipeline may name, with the
parameters the mock and the live backend both honour, by category (Scan,
Motion, Light, Devices, Recording, Other: how the designer sorts them).

Always there: ``run-scan``, ``move-to``, ``snap``, ``wait``, ``log``. Only
when the setup has the device (:func:`catalog` with a :class:`SetupDevices`):
``flip-mirror`` (a flip mirror, filter wheel or light-path switch to a
position), ``laser`` (a laser on or off, optionally at a power) and
``setup-mode`` (apply a saved setup mode, as etSNOUTY switches to its event
configuration on a trigger).

The specs are static (what the document checks against); the mock backend
performs them in memory and the live backend through hand-written adapters
(:mod:`.live_actions`). Phase 2 generates no action from the scripting API:
the review's finding 5 keeps execution curated; the forms come from these
specs' parameter defaults.

``move-to`` takes the first point of its input and drives one positioner
axis per coordinate column: ``axes`` lists, per column, an axis of
``positioner`` (``"Y"``) or ``"<positioner>:<axis>"`` for a column that
belongs to another positioner (two single-axis stages: ``["Mock Y:Y",
"Mock X:X"]``). ``relative`` moves by the value instead of to it;
``deadline_s`` bounds the move, after which the stage is quarantined and the
action has not ended.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Mapping

from imswitch.imcommon.model.param_spec import ParamField
from imswitch.imcommon.model.smartpipeline import ActionCatalog, ActionSpec

MOVE_TO = ActionSpec(
    "move-to", label="Move to", accepts=("points",),
    params={"positioner": "", "axes": ["Y", "X"], "frame": "", "relative": False, "deadline_s": 10.0},
    frame_param="frame", hardware=True, category="Motion",
    description="Move positioner axes to the first point (one axis per coordinate column, see 'axes').",
)
SNAP = ActionSpec(
    "snap", label="Snap", accepts=("event",), params={"detector": ""}, hardware=True, category="Recording",
    description="Take one image with a detector when the input event fired.",
)
LOG = ActionSpec(
    "log", label="Log", accepts=("any",), params={"message": ""}, hardware=False,
    description="Write the input to the event log.",
)

RUN_SCAN = ActionSpec(
    "run-scan", label="Run scan", accepts=("any",),
    params={"source": "Scan", "mode": "acquisition", "field_um": 60.0, "length_um": 8.0, "pixel_um": 0.1,
            "dwell_s": 0.0, "lasers": [], "axes": ["Y", "X"], "frame": "", "points": "all", "max_points": 0,
            "overview_pixel_um": 0.25, "box": "auto", "margin_um": 0.0, "margin_frac": 0.0, "min_size_um": 0.0,
            "max_size_um": 0.0, "min_overlap": 0.5, "range_um": {}, "range_inset_um": 0.0, "restore": True,
            "save": True},
    frame_param="frame", hardware=True, category="Scan",
    description="Run a point scan: 'overview' of field_um around the centre at overview_pixel_um (the Advanced "
                "panel; the simple page plans its own), or 'acquisition' of "
                "length_um at pixel_um centred on the input points (their columns mapped to the scan axes by "
                "'axes'): 'points' all (one scan after the other; max_points 0 = no limit) or first; 'lasers' "
                "are the lasers of its one channel; dwell_s 0 keeps the plan's dwell. On the Advanced panel, input points that "
                "carry bbox_y0/bbox_x0/bbox_y1/bbox_x1 (scan um, after a transform) scan their box: padded by "
                "margin_um + margin_frac, limited to min/max_size_um, a whole number of steps (the panel's step, or "
                "pixel_um), fitted to the scanner range (range_um per axis, else the positioner's volts x "
                "conversionFactor, inset by range_inset_um) or skipped with a reason when its centre is outside it or "
                "less than min_overlap of it is inside; the panel's own parameters are put back afterwards (restore). "
                "In Experiment, 'save' (on by default) records every scan as a Scan-once recording of the setup's "
                "scan-driven detectors into the run's folder under the Recording panel's folder, one file per "
                "event with the trigger's metadata; a scan that cannot be saved stops the pipeline.",
)

WAIT = ActionSpec(
    "wait", label="Wait", accepts=("any",), params={"seconds": 1.0}, hardware=False,
    description="Do nothing for 'seconds' (the chain goes on afterwards; the pipeline is busy meanwhile).",
)

FLIP_MIRROR = ActionSpec(
    "flip-mirror", label="Flip mirror", accepts=("any",), params={"mirror": "", "position": 1},
    hardware=True, category="Devices",
    description="Move a flip mirror (or a filter wheel, turret, light-path switch) to a position, 0..N-1, "
                "with its linked followers, through the Flip Mirrors panel; fails when it does not move "
                "(disconnected, a follower, an unknown position).",
    fields=(ParamField("position", "int", 1, label="Position", min=0, help="0..N-1; 0 and 1 for a two-state mirror"),),
)
LASER = ActionSpec(
    "laser", label="Laser", accepts=("any",), params={"laser": "", "enabled": True, "set_power": False,
                                                      "power": 0.0},
    hardware=True, category="Light",
    description="Switch a laser's emission on or off (as its row in the Laser panel does), and with set_power "
                "also set its live power to 'power' (in the laser's units) first; fails when the laser refuses.",
)
SETUP_MODE = ActionSpec(
    "setup-mode", label="Apply setup mode", accepts=("any",), params={"mode": ""}, hardware=True,
    category="Devices",
    description="Apply a saved setup mode (beam path, lasers, devices) as the Setup Modes panel does: the "
                "configuration etSNOUTY switches to on an event; fails when a component does not apply.",
)

ACTION_SPECS = (MOVE_TO, SNAP, LOG, RUN_SCAN, WAIT)
#: The actions that need a device of the setup; :func:`catalog` adds each when it is there.
DEVICE_ACTION_SPECS = (FLIP_MIRROR, LASER, SETUP_MODE)


@dataclass(frozen=True)
class SetupDevices:
    """What the running setup has, for the device actions: ``flip_mirrors``
    maps a mirror to its position names (index = position), ``lasers`` a
    laser to whether it is binary (on/off only), ``setup_modes`` the saved
    setup modes, or ``None`` when setup modes are not available at all."""

    flip_mirrors: Mapping[str, tuple] = field(default_factory=dict)
    lasers: Mapping[str, bool] = field(default_factory=dict)
    setup_modes: tuple | None = None


def _choice(key: str, names, label: str, help: str = "") -> ParamField:
    names = tuple(str(n) for n in names)
    return ParamField(key, "select", names[0], label=label, help=help, options=names)


def catalog(devices: SetupDevices | None = None) -> ActionCatalog:
    """A fresh catalogue: the actions every setup has, and -- with
    ``devices`` -- the device actions whose devices the setup has, their
    device parameter a choice of the setup's names."""
    specs = list(ACTION_SPECS)
    if devices is not None:
        mirrors = dict(devices.flip_mirrors or {})
        if mirrors:
            most = max((len(p) for p in mirrors.values()), default=2) or 2
            names = "; ".join(f"{m}: " + ", ".join(f"{i}={n}" for i, n in enumerate(p)) for m, p in mirrors.items())
            specs.append(replace(
                FLIP_MIRROR, params={**FLIP_MIRROR.params, "mirror": next(iter(mirrors))},
                fields=(_choice("mirror", mirrors, "Mirror"),
                        ParamField("position", "int", 1, label="Position", min=0, max=most - 1,
                                   help=f"0..N-1 ({names})"))))
        lasers = dict(devices.lasers or {})
        if lasers:
            specs.append(replace(LASER, params={**LASER.params, "laser": next(iter(lasers))},
                                 fields=(_choice("laser", lasers, "Laser"),
                                         ParamField("enabled", "bool", True, label="Emission on"),
                                         ParamField("set_power", "bool", False, label="Set power",
                                                    help="Also set the live power (not for an on/off laser)"),
                                         ParamField("power", "float", 0.0, label="Power", min=0.0,
                                                    help="In the laser's units, when 'Set power' is on"))))
        if devices.setup_modes is not None:
            modes = tuple(devices.setup_modes)
            fields = (_choice("mode", modes, "Setup mode"),) if modes else (
                ParamField("mode", "text", "", label="Setup mode", help="The name of a saved setup mode"),)
            specs.append(replace(SETUP_MODE, params={"mode": modes[0] if modes else ""}, fields=fields))
    return ActionCatalog(specs)


def axis_targets(params: dict) -> list[tuple[str, str]]:
    """``(positioner, axis)`` per coordinate column of a ``move-to``.

    Raises :class:`ValueError` when a column names no positioner (no
    ``positioner`` parameter and no ``"<positioner>:<axis>"`` entry) or an
    entry is empty.
    """
    default = str(params.get("positioner") or "")
    axes = params.get("axes") or MOVE_TO.params["axes"]
    if isinstance(axes, str):
        axes = [axes]
    targets = []
    for column, entry in enumerate(axes):
        entry = str(entry or "").strip()
        if not entry:
            raise ValueError(f"move-to: axes entry {column} is empty")
        if ":" in entry:
            positioner, axis = entry.rsplit(":", 1)
            positioner, axis = positioner.strip(), axis.strip()
        else:
            positioner, axis = default, entry
        if not positioner or not axis:
            raise ValueError(
                f"move-to: column {column} ({entry!r}) names no positioner; set 'positioner' or write "
                "'<positioner>:<axis>'"
            )
        targets.append((positioner, axis))
    return targets


__all__ = ["ACTION_SPECS", "DEVICE_ACTION_SPECS", "FLIP_MIRROR", "LASER", "LOG", "MOVE_TO", "RUN_SCAN", "SETUP_MODE",
           "SNAP", "SetupDevices", "WAIT", "axis_targets", "catalog"]


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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
