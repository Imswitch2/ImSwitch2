"""Multi-well tiling workflow — automated grid scanning with autofocus.

Iterates over a rectangular grid of well positions, running autofocus and a
full tiling scan at each position. Continues on errors to complete the entire
grid even if individual wells fail.

Ported from WFS MultiWellTilingWorkflow to ImSwitch model/workflows pattern.
"""

from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from imswitch.imcontrol.model.workflows.paths import (
    default_measurements_root,
    resolve_measurements_root,
)

if TYPE_CHECKING:
    from imswitch.imcontrol.model.workflows.facade import MicroscopeFacade
    from imswitch.imcontrol.model.workflows.tiling import TilingWorkflow
    from imswitch.imcontrol.model.workflows.z_stack import ZStackWorkflow

logger = logging.getLogger(__name__)

DEFAULT_MEASUREMENTS_ROOT = default_measurements_root()


@dataclass
class MultiWellTilingParams:
    """Parameters for multi-well tiling with autofocus.

    Attributes:
        n_rows: Number of rows in the well grid.
        n_cols: Number of columns in the well grid.
        well_pitch_x_units: Spacing between wells along X axis in stage units.
        well_pitch_y_units: Spacing between wells along Y axis in stage units.
        autofocus_n_planes: Number of Z planes for autofocus (temporarily overrides
            zstack_workflow params during autofocus).
        autofocus_step_um: Z step size for autofocus in micrometers (temporarily
            overrides zstack_workflow params during autofocus).
        autofocus_z_center_um: Optional starting Z position for autofocus in micrometers.
            If None, autofocus starts from current Z position at each well.
        tiling_n_tiles: Number of tiles to acquire per well (passed to TilingWorkflow).
            Will be rounded up to the nearest perfect square for spiral scanning.
        measurements_root: Root directory for saving measurements. Each well creates
            a subdirectory well_r{row}_c{col}/ under this root.
    """

    n_rows: int
    n_cols: int
    well_pitch_x_units: float
    well_pitch_y_units: float
    autofocus_n_planes: int = 10
    autofocus_step_um: float = 2.0
    autofocus_z_center_um: Optional[float] = None
    tiling_n_tiles: int = 9
    measurements_root: Optional[str | Path] = DEFAULT_MEASUREMENTS_ROOT


class MultiWellTilingWorkflow:
    """Run tiling experiments at multiple grid positions with per-well autofocus.

    Iterates over a rectangular grid of well positions in row-major order. At each
    well position:
    1. Moves the XY stage to the well center
    2. Runs autofocus to find optimal Z position
    3. Executes a full tiling scan

    If autofocus or tiling fails at a well, logs a warning with full traceback and
    continues to the next well to ensure the entire grid is processed.

    Args:
        facade: Hardware facade exposing stage_con, z_stage_con, cam, laser_con.
        tiling_workflow: Pre-configured TilingWorkflow instance to execute at each well.
        zstack_workflow: Pre-configured ZStackWorkflow instance for autofocus.
            Its n_planes and step_um params will be temporarily overridden during
            autofocus calls using values from MultiWellTilingParams.
        params: Multi-well tiling parameters including grid dimensions and autofocus settings.
    """

    def __init__(
        self,
        facade: MicroscopeFacade,
        tiling_workflow: TilingWorkflow,
        zstack_workflow: ZStackWorkflow,
        params: MultiWellTilingParams,
    ) -> None:
        self.facade = facade
        self.tiling_workflow = tiling_workflow
        self.zstack_workflow = zstack_workflow
        self.params = params

        # Validate required facade components
        if self.facade.stage_con is None:
            raise ValueError("MultiWellTilingWorkflow requires facade.stage_con for XY positioning")
        if self.facade.z_stage_con is None:
            raise ValueError("MultiWellTilingWorkflow requires facade.z_stage_con for autofocus")

    def run(self) -> None:
        """Execute multi-well tiling scan across the full grid.

        Records the current XY stage position as the grid origin (position of well
        r0_c0). Iterates over all wells in row-major order, performing autofocus
        and tiling at each position. Returns the stage to the origin when complete.

        Individual well failures are logged but do not stop the workflow — all wells
        in the grid will be attempted regardless of individual failures.
        """
        # Record origin position (current stage position = well [0,0])
        origin_x, origin_y = self.facade.stage_con.get_position()
        logger.info(
            "Multi-well tiling: %dx%d grid, pitch=(%.1f, %.1f) units, %d tiles/well",
            self.params.n_rows,
            self.params.n_cols,
            self.params.well_pitch_x_units,
            self.params.well_pitch_y_units,
            self.params.tiling_n_tiles,
        )
        logger.info(
            "Grid origin (well r0_c0) at stage position: (%.2f, %.2f)",
            origin_x,
            origin_y,
        )

        # Create base measurement folder
        base_folder = resolve_measurements_root(self.params.measurements_root)
        base_folder.mkdir(parents=True, exist_ok=True)

        # Iterate over grid in row-major order
        for row in range(self.params.n_rows):
            for col in range(self.params.n_cols):
                well_label = f"well_r{row}_c{col}"
                logger.info("=== Processing %s ===", well_label)

                # Calculate well position
                target_x = origin_x + col * self.params.well_pitch_x_units
                target_y = origin_y + row * self.params.well_pitch_y_units

                try:
                    # Move to well position
                    self.facade.stage_con.move_to(target_x, target_y)
                    actual_x, actual_y = self.facade.stage_con.get_position()
                    logger.info(
                        "%s: moved to (%.2f, %.2f) (target was %.2f, %.2f)",
                        well_label,
                        actual_x,
                        actual_y,
                        target_x,
                        target_y,
                    )

                    # Run autofocus at this well
                    self._run_autofocus_for_well(well_label)

                    # Run tiling at this well
                    well_folder = base_folder / well_label
                    well_folder.mkdir(parents=True, exist_ok=True)
                    logger.info("%s: starting tiling scan", well_label)
                    self.tiling_workflow.run(save_folder=well_folder)
                    logger.info("%s: tiling complete", well_label)

                except Exception as exc:
                    # Log full traceback but continue to next well
                    logger.warning(
                        "%s: FAILED with exception: %s\n%s",
                        well_label,
                        exc,
                        traceback.format_exc(),
                    )
                    logger.warning("%s: continuing to next well despite failure", well_label)

        # Return stage to origin
        self.facade.stage_con.move_to(origin_x, origin_y)
        final_x, final_y = self.facade.stage_con.get_position()
        logger.info(
            "Multi-well tiling complete — returned to origin (%.2f, %.2f)",
            final_x,
            final_y,
        )

    def _run_autofocus_for_well(self, well_label: str) -> None:
        """Run autofocus at the current well position.

        Temporarily overrides the zstack_workflow params with autofocus-specific
        values from MultiWellTilingParams, then calls run_autofocus.

        Args:
            well_label: Well identifier for logging (e.g. "well_r0_c1").

        Raises:
            Any exception from run_autofocus (caught by caller).
        """
        # Save original Z-stack params
        original_n_planes = self.zstack_workflow.params.n_planes
        original_step_um = self.zstack_workflow.params.step_um

        try:
            # Temporarily override for autofocus
            self.zstack_workflow.params.n_planes = self.params.autofocus_n_planes
            self.zstack_workflow.params.step_um = self.params.autofocus_step_um

            logger.info(
                "%s: running autofocus (n_planes=%d, step_um=%.1f)",
                well_label,
                self.params.autofocus_n_planes,
                self.params.autofocus_step_um,
            )

            # Run autofocus (uses current Z position if z_center is None)
            self.zstack_workflow.run_autofocus(z_start=self.params.autofocus_z_center_um)
            logger.info("%s: autofocus succeeded", well_label)

        finally:
            # Restore original Z-stack params
            self.zstack_workflow.params.n_planes = original_n_planes
            self.zstack_workflow.params.step_um = original_step_um
