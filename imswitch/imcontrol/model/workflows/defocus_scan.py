"""Defocus scan workflow — run WidefieldStarss at multiple Z positions.

Runs a full WidefieldStarssWorkflow at each Z plane to characterise the effect of
defocus on polarisation-resolved measurements.

Ported from WFS DefocusScanWorkflow to ImSwitch model/workflows pattern.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

import numpy as np

if TYPE_CHECKING:
    from imswitch.imcontrol.model.workflows.facade import MicroscopeFacade
    from imswitch.imcontrol.model.workflows.widefield_starss import WidefieldStarssWorkflow

logger = logging.getLogger(__name__)


@dataclass
class DefocusScanParams:
    """Parameters for defocus scan acquisition.

    Attributes:
        n_z_planes: Number of Z planes to acquire.
        z_step_um: Step size between planes in micrometers.
        z_center_um: Center Z position in micrometers. If None, use current position.
        scramble: If True, randomize the Z scan order to reduce systematic artifacts.
    """

    n_z_planes: int
    z_step_um: float
    z_center_um: Optional[float] = None
    scramble: bool = False


class DefocusScanWorkflow:
    """Run a full WidefieldStarss acquisition at each of N Z positions.

    Args:
        facade: WFS-shaped hardware facade exposing z_stage_con.
        widefield_starss_workflow: WidefieldStarssWorkflow instance for acquisitions.
        recording_workflow: Deprecated alias for ``widefield_starss_workflow``.
        params: Defocus scan parameters.
    """

    def __init__(
        self,
        facade: MicroscopeFacade,
        widefield_starss_workflow: Optional[WidefieldStarssWorkflow] = None,
        params: Optional[DefocusScanParams] = None,
        *,
        recording_workflow: Optional[WidefieldStarssWorkflow] = None,
    ) -> None:
        if widefield_starss_workflow is None:
            widefield_starss_workflow = recording_workflow
        if widefield_starss_workflow is None:
            raise TypeError("DefocusScanWorkflow requires a WidefieldStarssWorkflow")
        if params is None:
            raise TypeError("DefocusScanWorkflow requires DefocusScanParams")

        self.facade = facade
        self.widefield_starss = widefield_starss_workflow
        self.recording = widefield_starss_workflow  # Deprecated compatibility attribute
        self.params = params

    def run(self) -> list[float]:
        """Execute defocus scan: WidefieldStarss acquisition at each Z position.

        Returns:
            List of Z positions (in micrometers) actually visited.

        Raises:
            RuntimeError: If z_stage_con is not available in the facade.
        """
        if self.facade.z_stage_con is None:
            raise RuntimeError("Defocus scan requires z_stage_con in facade")

        # Determine center position
        z_center = self.params.z_center_um
        if z_center is None:
            z_center = self.facade.z_stage_con.read_pos_um()

        # Generate Z positions centered around z_center
        half_range = (self.params.n_z_planes - 1) * self.params.z_step_um / 2.0
        z_positions = [
            z_center - half_range + i * self.params.z_step_um
            for i in range(self.params.n_z_planes)
        ]

        # Clamp to piezo's allowed range
        z_min, z_max = self.facade.z_stage_con.pos_range_um
        z_positions = [float(np.clip(z, z_min, z_max)) for z in z_positions]

        # Optionally scramble the order
        if self.params.scramble:
            indices = np.arange(self.params.n_z_planes)
            np.random.shuffle(indices)
            z_positions = [z_positions[i] for i in indices]

        logger.info(
            "Defocus scan: %d planes, step %.2f µm, scramble=%s",
            self.params.n_z_planes,
            self.params.z_step_um,
            self.params.scramble,
        )
        logger.info(
            "Z range: %.2f µm to %.2f µm",
            min(z_positions),
            max(z_positions),
        )

        # Remember starting position for cleanup
        z_start = self.facade.z_stage_con.read_pos_um()

        self.facade.z_stage_con.activate_ext_control()
        try:
            for i, z in enumerate(z_positions):
                logger.info(
                    "Defocus plane %d/%d at %.2f µm",
                    i + 1,
                    self.params.n_z_planes,
                    z,
                )
                self.facade.z_stage_con.set_pos_um(z)
                time.sleep(0.3)  # Allow piezo to settle

                # Update measurement name suffix and run WidefieldStarss.
                self.widefield_starss.params.measurement_name_addition = f"_z{z:.2f}um"
                self.widefield_starss.run()

        finally:
            # Restore original position
            self.facade.z_stage_con.set_pos_um(z_start)
            logger.info("Defocus scan done — returned to %.2f µm", z_start)

        return z_positions


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
