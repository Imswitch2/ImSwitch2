"""Serial CWSTARSS workflow.

Performs one CWSTARSS experiment per (488 nm, 405 nm) power combination from
the Cartesian product of two user-supplied power lists, moving the stage to a
new spiral position for each combination.

Example: powers_488=[40, 50] × powers_405=[10, 20, 30] → 6 positions,
one per unique combination.

Ported from WidefieldStarss/src/WFS/workflows/serial_cwstarss.py to ImSwitch.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import numpy as np

from imswitch.imcontrol.model.workflows.spiral import spiral_moves
from imswitch.imcontrol.model.workflows.paths import (
    default_measurements_root,
    resolve_measurements_root,
)

if TYPE_CHECKING:
    from imswitch.imcontrol.model.workflows.cwstarss import CWSTARSSWorkflow
    from imswitch.imcontrol.model.workflows.facade import MicroscopeFacade

logger = logging.getLogger(__name__)

DEFAULT_MEASUREMENTS_ROOT = default_measurements_root()


@dataclass
class SerialCWSTARSSParams:
    """Parameters for serial CWSTARSS power sweep experiment.

    Args:
        powers_488_mw: List of 488 nm powers to sweep (mW).
        powers_405_mw: List of 405 nm powers to sweep (mW).
        fps: Target camera frame rate (used by CWSTARSS).
        duration_s: Duration of each streaming phase in seconds (used by CWSTARSS).
        step_units: Raw stage units between adjacent spiral positions.
        measurements_root: Base directory for saving data.
    """

    powers_488_mw: list[float]
    powers_405_mw: list[float]
    fps: float
    duration_s: float
    step_units: float = 1560.0
    measurements_root: Optional[Path | str] = DEFAULT_MEASUREMENTS_ROOT


class SerialCWSTARSSWorkflow:
    """Run one CWSTARSS experiment per (488, 405) power combination on a spiral grid.

    The total number of stage positions equals
    ``len(powers_488) × len(powers_405)``.  No rounding is applied.

    Args:
        facade: Hardware aggregator providing access to stage controller.
        cwstarss_workflow: A configured :class:`CWSTARSSWorkflow` instance.
        params: Experiment parameters.
    """

    def __init__(
        self,
        facade: MicroscopeFacade,
        cwstarss_workflow: CWSTARSSWorkflow,
        params: SerialCWSTARSSParams,
    ) -> None:
        self.facade = facade
        self.cwstarss = cwstarss_workflow
        self.params = params

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Execute the serial CWSTARSS power sweep."""
        # Validate parameters
        if not self.params.powers_488_mw:
            raise ValueError("powers_488_mw must not be empty")
        if not self.params.powers_405_mw:
            raise ValueError("powers_405_mw must not be empty")

        combinations = list(
            itertools.product(self.params.powers_488_mw, self.params.powers_405_mw)
        )
        n_positions = len(combinations)

        logger.info(
            "Serial CWSTARSS starting — %d positions (%d × %d power combinations), "
            "fps=%.1f, duration=%.1f s, step_units=%.1f",
            n_positions,
            len(self.params.powers_488_mw),
            len(self.params.powers_405_mw),
            self.params.fps,
            self.params.duration_s,
            self.params.step_units,
        )
        logger.info(
            "488 powers: %s mW  |  405 powers: %s mW",
            self.params.powers_488_mw,
            self.params.powers_405_mw,
        )

        initial_pos = self.facade.stage_con.get_position()
        logger.info("Initial stage position: (%.1f, %.1f)", initial_pos[0], initial_pos[1])

        try:
            self._run_serial_experiment(combinations, n_positions, initial_pos)
        finally:
            # Return stage to initial position
            logger.info(
                "Serial CWSTARSS: returning stage to initial position (%.1f, %.1f)",
                initial_pos[0],
                initial_pos[1],
            )
            try:
                self.facade.stage_con.move_to(initial_pos[0], initial_pos[1])
            except Exception as e:
                logger.error("Failed to return stage to initial position: %s", e)

        logger.info("Serial CWSTARSS complete — %d positions done", n_positions)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _run_serial_experiment(
        self,
        combinations: list[tuple[float, float]],
        n_positions: int,
        initial_pos: tuple[float, float],
    ) -> None:
        """Execute the main loop: move to spiral positions and run CWSTARSS."""
        curr_pos = np.zeros(2)

        for pos_idx, (move, (p488, p405)) in enumerate(
            zip(spiral_moves(n_positions), combinations)
        ):
            # Accumulate spiral offset
            curr_pos += np.array(move)

            # Calculate absolute target position
            target = (
                initial_pos[0] + self.params.step_units * curr_pos[0],
                initial_pos[1] + self.params.step_units * curr_pos[1],
            )

            # Move stage
            self.facade.stage_con.move_to(target[0], target[1])
            logger.info(
                "Serial CWSTARSS: position %d/%d  grid=(%.1f,%.1f)  "
                "stage=(%.1f,%.1f)  488=%.1f mW  405=%.1f mW",
                pos_idx + 1,
                n_positions,
                curr_pos[0],
                curr_pos[1],
                target[0],
                target[1],
                p488,
                p405,
            )

            # Update CWSTARSS workflow parameters for this position
            self.cwstarss.params.fps = self.params.fps
            self.cwstarss.params.duration_s = self.params.duration_s
            self.cwstarss.params.power_488_mw = p488
            self.cwstarss.params.power_405_mw = p405
            self.cwstarss.params.measurements_root = resolve_measurements_root(
                self.params.measurements_root
            )

            # Run CWSTARSS experiment at this position
            self.cwstarss.run()


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
