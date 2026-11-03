"""Ready-made pipelines for the designer's start page.

A recipe is what the editor's start page lists (``title``, ``summary``,
``about``) and builds on a click (``workflow()``); ``select`` names the step
the editor should select afterwards and ``hint`` what the status bar says.
The host passes what the recipe needs to be concrete -- the detector, the
transform id, the positioner axes -- so the pipeline it makes runs as it is
once a calibration exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from imswitch.imcommon.model.smartpipeline.steps import Action, Gate, Live, Sink, Transform
from imswitch.imcommon.model.workflow.steps import Process, Workflow


@dataclass(frozen=True)
class PipelineRecipe:
    name: str
    title: str
    summary: str
    about: tuple[str, ...]
    build: Callable[[], Workflow] = field(compare=False)
    select: str = ""
    hint: str = ""

    def workflow(self) -> Workflow:
        return self.build()


def threshold_move_recipe(*, detector: str = "", transform: str = "identity", axes=("Y", "X"),
                          positioner: str = "", frame: str = "") -> PipelineRecipe:
    """Threshold the live frames, pick the largest region, move the stage there."""
    detector = str(detector or "")
    axes = [str(a) for a in axes]

    def build() -> Workflow:
        return Workflow("threshold and move", [
            Live("cam", detector=detector),
            Process("thr", "threshold", {"method": "absolute", "value": 250.0, "min_area": 20}, inputs=["cam"]),
            Process("rp", "regionprops", {"frame": f"detector:{detector}" if detector else "unknown"},
                    inputs=["thr", "cam"]),
            Process("pk", "pick", {"rule": "largest"}, inputs=["rp"]),
            Gate("found", "condition", inputs=["pk"]),
            Gate("settle", "min-interval", {"seconds": 2.0}, inputs=["found"]),
            Transform("tf", transform, inputs=["settle"]),
            Action("mv", "move-to", {"axes": axes, "positioner": positioner, "frame": frame}, inputs=["tf"]),
            Sink("log", "event-log", inputs=["pk"]),
        ], description="Threshold each frame, pick the largest region, move the stage to it (at most "
                       "every two seconds).")

    return PipelineRecipe(
        name="threshold_move",
        title="Threshold → move",
        summary="Detect the largest bright region in the live frames and move the stage to it.",
        about=(
            "Threshold (absolute, 250 counts) → region properties → pick the largest → a gate that opens when a region was "
            "found, at most every two seconds → the transform into stage coordinates → move-to.",
            "Set the transform to a calibration of your camera and stage, and the action's frame to the "
            "frame that calibration writes.",
        ),
        build=build,
        select="mv",
        hint="Recipe 'Threshold → move': set the transform and the action's axes, save, then Arm from the panel.",
    )


def pipeline_recipes(*, detector: str = "", transform: str = "identity", axes=("Y", "X"), positioner: str = "",
                     frame: str = "") -> list:
    return [threshold_move_recipe(detector=detector, transform=transform, axes=axes, positioner=positioner,
                                  frame=frame)]


__all__ = ["PipelineRecipe", "pipeline_recipes", "threshold_move_recipe"]


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
