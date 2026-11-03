"""The v1 action catalogue: the three actions a pipeline may name, with the
parameters the mock and the live backend both honour.

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

from imswitch.imcommon.model.smartpipeline import ActionCatalog, ActionSpec

MOVE_TO = ActionSpec(
    "move-to", label="Move to", accepts=("points",),
    params={"positioner": "", "axes": ["Y", "X"], "frame": "", "relative": False, "deadline_s": 10.0},
    frame_param="frame", hardware=True,
    description="Move positioner axes to the first point (one axis per coordinate column, see 'axes').",
)
SNAP = ActionSpec(
    "snap", label="Snap", accepts=("event",), params={"detector": ""}, hardware=True,
    description="Take one image with a detector when the input event fired.",
)
LOG = ActionSpec(
    "log", label="Log", accepts=("any",), params={"message": ""}, hardware=False,
    description="Write the input to the event log.",
)

RUN_SCAN = ActionSpec(
    "run-scan", label="Run scan", accepts=("any",),
    params={"source": "Scan", "mode": "acquisition", "field_um": 60.0, "length_um": 8.0, "pixel_um": 0.1,
            "dwell_s": 0.0, "lasers": [], "axes": ["Y", "X"], "frame": "", "points": "all", "max_points": 0},
    frame_param="frame", hardware=True,
    description="Run a point scan: 'overview' of field_um around the current centre, or 'acquisition' of "
                "length_um at pixel_um centred on the input points (their columns mapped to the scan axes by "
                "'axes'): 'points' all (one scan after the other; max_points 0 = no limit) or first; 'lasers' "
                "are the lasers of its one channel; dwell_s 0 keeps the plan's dwell.",
)

ACTION_SPECS = (MOVE_TO, SNAP, LOG, RUN_SCAN)


def catalog() -> ActionCatalog:
    """A fresh catalogue of the v1 actions."""
    return ActionCatalog(ACTION_SPECS)


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


__all__ = ["ACTION_SPECS", "LOG", "MOVE_TO", "RUN_SCAN", "SNAP", "axis_targets", "catalog"]


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
