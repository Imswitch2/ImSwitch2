"""Tiling workflow — spiral scan with optional cell segmentation and targeting.

Ported from WFS TilingWorkflow. Executes a spiral XY scan, snaps one frame per
tile (software or hardware-triggered), saves tiles to disk and H5, stitches the
overview, and optionally segments cells then drives per-cell RecordingWorkflow
acquisitions.

Copyright (C) 2020-2026 ImSwitch developers
This file is part of ImSwitch.

ImSwitch is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

ImSwitch is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

import numpy as np

from imswitch.imcontrol.model.workflows.spiral import spiral_moves
from imswitch.imcontrol.model.workflows.paths import (
    default_measurements_root,
    resolve_measurements_root,
)
from imswitch.imcontrol.model.workflows.stitched_image import StitchedImage

if TYPE_CHECKING:
    from imswitch.imcontrol.model.workflows.facade import MicroscopeFacade
    from imswitch.imcontrol.model.workflows.recording import RecordingWorkflow

logger = logging.getLogger(__name__)

# Default base folder when none is provided.
DEFAULT_MEASUREMENTS_ROOT = default_measurements_root()


@dataclass
class TilingParams:
    """Parameters for a tiling scan.
    
    Attributes:
        n_tiles: Number of tiles to acquire (rounded up to perfect square).
        step_units: Stage step size in stage units between tile centers (default 1560).
        laser_pin: Teensy pin number for laser TTL output (for hardware triggering).
        camera_pin: Teensy pin number for camera trigger output (for hardware triggering).
        pulsed: Whether to use pulsed/triggered acquisition mode.
        laser_power_488_mw: Laser power in mW for 488 nm.
        exposure_us: Camera exposure time in microseconds.
        tile_display_size: Target tile size for downsampled stitching preview (default 256).
            Set to 0 for full resolution (required for cell segmentation).
        save_individual: Whether to save individual .npy tile files (default True).
        skip_cell_targeting: If True, skip automatic cell segmentation/targeting.
        measurements_root: Base directory for saving tiles. Defaults to
            ``~/ImSwitchMeasurements`` if not provided.
        save_folder: Pre-set save folder path. If None, run() creates a timestamped
            folder under measurements_root.
    """
    
    n_tiles: int
    step_units: int = 1560
    laser_pin: int = 0
    camera_pin: int = 1
    pulsed: bool = False
    laser_power_488_mw: float = 5.0
    exposure_us: int = 50000
    tile_display_size: int = 256
    save_individual: bool = True
    skip_cell_targeting: bool = False
    measurements_root: Optional[Path | str] = field(default_factory=lambda: DEFAULT_MEASUREMENTS_ROOT)
    save_folder: Optional[Path] = None


class TilingWorkflow:
    """Orchestrate a spiral tiling scan with optional cell targeting.
    
    Args:
        facade: MicroscopeFacade providing access to hardware managers.
        recording_workflow: Optional RecordingWorkflow instance for per-cell acquisitions.
            For overview-only tiling, callers may use ``TilingWorkflow(facade, params)``.
        params: TilingParams configuration for this scan.
        seg_filter: Optional dict of cell segmentation filter parameters.
            Example: {
                "area_um2_min": 100.0,
                "area_um2_max": 1000.0,
                "area_enabled": True,
                "mean_intensity": 0.1,
                "mean_intensity_enabled": True,
                "eccentricity": 0.95,
                "eccentricity_enabled": True,
                "max_intensity": 0.2,
                "max_intensity_enabled": False,
            }
    """
    
    def __init__(
        self,
        facade: MicroscopeFacade,
        recording_workflow: Optional[RecordingWorkflow | TilingParams] = None,
        params: Optional[TilingParams] = None,
        seg_filter: Optional[dict] = None,
    ) -> None:
        if isinstance(recording_workflow, TilingParams):
            if params is not None:
                if seg_filter is not None:
                    raise TypeError(
                        "TilingWorkflow received both positional params and seg_filter"
                    )
                seg_filter = params  # Backward-compatible TilingWorkflow(facade, params, seg_filter)
            params = recording_workflow
            recording_workflow = None

        if params is None:
            raise TypeError("TilingWorkflow requires TilingParams")

        self.facade = facade
        self._recording = recording_workflow
        self.params = params
        self.seg_filter = seg_filter or {}
        
        # Populated during a scan
        self.stitched_image: Optional[StitchedImage] = None
        self._tile_positions_stage: list[tuple[float, float]] = []
        self._origin_stage_xy: Optional[tuple[float, float]] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        save_folder: Optional[Path] = None,
        tile_callback: Optional[Callable] = None,
    ) -> None:
        """Execute the full tiling scan.
        
        Args:
            save_folder: Directory to save tiles and H5 file. If None, uses params.save_folder
                or creates a timestamped folder under params.measurements_root.
            tile_callback: Optional callback ``f(image, grid_x, grid_y, tile_idx, n_total)``
                called after each tile is acquired — use for live preview updates.
        """
        save_folder = save_folder or self.params.save_folder or self._default_save_folder()
        
        save_folder = Path(save_folder)
        save_folder.mkdir(parents=True, exist_ok=True)
        self.params.save_folder = save_folder
        
        # Round n_tiles up to perfect square for spiral
        n_steps = int(np.ceil(np.sqrt(self.params.n_tiles)) ** 2)
        logger.info("Starting tiling scan: %d tiles (rounded from %d)", n_steps, self.params.n_tiles)
        
        self._prepare_h5(save_folder)
        
        # Lazy import h5py
        try:
            import h5py
        except ImportError:
            h5py = None
            logger.warning("h5py not available; H5 master file will not be created")
        
        if h5py is not None:
            h5_file = h5py.File(str(save_folder / "Tiling_measurement.h5"), "a")
            h5_file["n_steps"] = n_steps
            h5_file["folder"] = str(save_folder)
        else:
            h5_file = None
        
        # Get initial stage position
        initial_pos = self._get_stage_position()
        curr_pos = np.zeros(2, dtype=float)
        self._tile_positions_stage = []
        
        # Decide acquisition strategy
        hw_trigger = (
            self.params.pulsed
            and self.params.laser_pin is not None
            and self.params.camera_pin is not None
            and self.facade.trig.connected
        )
        
        # Configure laser
        if hw_trigger:
            self.facade.laser_con.set_triggered_mode(["488"], [self.params.laser_power_488_mw])
            time.sleep(0.5)
            logger.info("Tiling: hardware-triggered mode (laser_pin=%d, camera_pin=%d)",
                       self.params.laser_pin, self.params.camera_pin)
        else:
            self.facade.laser_con.set_constant_power(["488"], [self.params.laser_power_488_mw])
            time.sleep(2)
            if self.params.pulsed:
                self.facade.laser_con.laser_off(["488"])
        
        # Execute spiral scan
        for ctr, pos in enumerate(spiral_moves(n_steps)):
            logger.info("Acquiring tile %d/%d", ctr + 1, n_steps)
            curr_pos += pos
            curr_pos_move = (
                int(initial_pos[0] + self.params.step_units * curr_pos[0]),
                int(initial_pos[1] + self.params.step_units * curr_pos[1]),
            )
            self.facade.stage_con.move_to(curr_pos_move[0], curr_pos_move[1])
            
            # Record actual position
            curr_pos_is = self._get_stage_position()
            self._tile_positions_stage.append(curr_pos_is)
            
            if h5_file is not None:
                h5_file[f"{int(curr_pos[0])}_{int(curr_pos[1])}_is_pos"] = curr_pos_is
                h5_file[f"{int(curr_pos[0])}_{int(curr_pos[1])}_should_pos"] = curr_pos_move
            
            # Acquire tile
            save_path = save_folder / f"img_new_{int(curr_pos[0])}_{int(curr_pos[1])}.npy"
            if hw_trigger:
                tmp_im = self._grab_image_hw(
                    laser_pin=self.params.laser_pin,
                    camera_pin=self.params.camera_pin,
                    savepath=save_path if self.params.save_individual else None,
                    filehandle=h5_file,
                )
            else:
                tmp_im = self._grab_image(
                    savepath=save_path if self.params.save_individual else None,
                    filehandle=h5_file,
                    pulsed_laser=self.params.pulsed,
                )
            
            if tile_callback is not None:
                tile_callback(tmp_im, int(curr_pos[0]), int(curr_pos[1]), ctr, n_steps)
        
        # Clean up laser
        self.facade.laser_con.set_modulation_mode(["488"])
        
        if h5_file is not None:
            h5_file.close()
        
        # Move to origin (minimum x, y)
        if self._tile_positions_stage:
            pos_arr = np.array(self._tile_positions_stage)
            min_x = int(np.min(pos_arr[:, 0]))
            min_y = int(np.min(pos_arr[:, 1]))
            self._origin_stage_xy = (float(min_x), float(min_y))
            self.facade.stage_con.move_to(min_x, min_y)
        
        logger.info("Tiling scan complete")

    def _default_save_folder(self) -> Path:
        """Return a timestamped tiling folder under the configured measurements root."""
        root = resolve_measurements_root(self.params.measurements_root)
        now = datetime.now()
        return root / now.strftime("%Y_%m_%d") / f"tiling_{now.strftime('%H%M%S')}"

    def run_cell_targeting(
        self,
        stitched: StitchedImage,
        pixel_size_um: float,
        canvas_origin_stage: tuple[float, float],
        for_each_feature: Optional[Callable] = None,
        cells_found_cb: Optional[Callable] = None,
        cell_started_cb: Optional[Callable] = None,
        cell_done_cb: Optional[Callable] = None,
    ) -> None:
        """Segment cells in a live stitched overview and iterate over them.

        Args:
            stitched: A live ``StitchedImage`` containing the overview to segment.
            pixel_size_um: Pixel size in µm for the overview (matches ``stitched``).
            canvas_origin_stage: ``(stage_x, stage_y)`` corresponding to overview
                pixel (0, 0). Used to convert centroid coords to stage moves.
            for_each_feature: Optional ``f(idx, props_for_cell, stage_xy) -> None``
                callable invoked once per accepted cell after the stage has moved
                to it. Plug in a sub-workflow (recording, z-stack, etc.) here.
            cells_found_cb: ``f(positions, n_cells)`` — (N, 2) row/col positions.
            cell_started_cb: ``f(cell_index)`` called before each cell.
            cell_done_cb: ``f(cell_index, success)`` called after each cell.
        """
        from imswitch.imcontrol.model.workflows.segmentation import Segmenter

        overview = np.asarray(stitched.get_overview(), dtype=np.float32)
        if overview.size == 0:
            logger.warning("Empty overview — nothing to segment")
            return

        params = self.seg_filter or {}
        seg = Segmenter(
            blur_sigma_px=params.get("blur_sigma_px", 3.0),
            threshold=params.get("threshold"),
        )
        props = seg.segment(overview, pixel_size_um)
        if not props:
            logger.info("No cells found in overview")
            if cells_found_cb is not None:
                cells_found_cb(np.empty((0, 2)), 0)
            return

        keep = Segmenter.apply_filters(props, params)
        idx_valid = np.where(keep)[0]
        logger.info("Found %d valid target cells (filtered from %d)",
                    len(idx_valid), len(props["label"]))

        if len(idx_valid) == 0:
            if cells_found_cb is not None:
                cells_found_cb(np.empty((0, 2)), 0)
            return

        positions = np.column_stack([
            props["centroid_row"][idx_valid],
            props["centroid_col"][idx_valid],
        ])
        if cells_found_cb is not None:
            cells_found_cb(positions, len(idx_valid))

        for i, cell_idx in enumerate(idx_valid):
            if cell_started_cb is not None:
                cell_started_cb(i)
            success = False
            try:
                row = int(props["centroid_row"][cell_idx])
                col = int(props["centroid_col"][cell_idx])
                stage_x, stage_y = stitched.pixel_to_stage(row, col, canvas_origin_stage)
                self.facade.stage_con.move_to(stage_x, stage_y)
                logger.info("Cell %d/%d at stage (%.2f, %.2f)",
                            i + 1, len(idx_valid), stage_x, stage_y)

                if for_each_feature is not None:
                    cell_props = {k: v[cell_idx] for k, v in props.items()}
                    for_each_feature(i, cell_props, (stage_x, stage_y))

                success = True
            except Exception as exc:
                logger.warning("Cell %d/%d failed: %s — skipping",
                               i + 1, len(idx_valid), exc)
            if cell_done_cb is not None:
                cell_done_cb(i, success)

        logger.info("Cell targeting complete")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _grab_image_hw(
        self,
        laser_pin: int,
        camera_pin: int,
        savepath: Optional[Path] = None,
        filehandle = None,
    ) -> np.ndarray:
        """Acquire one frame using Teensy Snap hardware synchronization.
        
        The Teensy fires laser_pin and camera_pin simultaneously for exactly
        exposure_us microseconds. The camera is armed in HARDWARE_TRIGGER mode
        so it captures exactly on the rising edge. The laser is in digital-
        modulation mode so it responds to the TTL immediately with no ramp-up.
        """
        self.facade.cam.prepare_acquisition(1)
        self.facade.cam.start_acquisition()
        self.facade.trig.snap_trigger(
            laser_pin=laser_pin,
            camera_pin=camera_pin,
            exposure_us=self.params.exposure_us,
        )
        
        # Wait for frame to arrive
        arrived = self.facade.cam.wait_for_frame(timeout_s=2.0)
        if not arrived:
            logger.warning("Hardware snap: no frame within 2 s, falling back to software")
            self.facade.cam.stop_acquisition()
            return self._grab_image(savepath=savepath, filehandle=filehandle, pulsed_laser=False)
        
        self.facade.cam.stop_acquisition()
        data = self.facade.cam.get_data()
        
        if not isinstance(data, np.ndarray) or data.shape[0] == 0:
            logger.warning("Hardware snap: empty data, falling back to software")
            return self._grab_image(savepath=savepath, filehandle=filehandle, pulsed_laser=False)
        
        img = data[:, :]
        
        if savepath is not None:
            np.save(str(savepath).replace(".npy", "") + ".npy", img)
        
        if filehandle is not None and savepath is not None:
            key = savepath.name.split("new_")[-1].replace(".npy", "")
            filehandle[key] = data
        
        return img

    def _grab_image(
        self,
        savepath: Optional[Path] = None,
        filehandle = None,
        pulsed_laser: bool = False,
        recursion_ctr: int = 0,
    ) -> np.ndarray:
        """Acquire a single image from the camera using software control.
        
        When pulsed_laser is True, the laser is enabled immediately before
        the camera snap and disabled right after.
        """
        if pulsed_laser:
            self.facade.laser_con.laser_on(["488"])
        
        self.facade.cam.prepare_live()
        self.facade.cam.start_live()
        time.sleep(0.05)  # Brief delay for frame capture
        self.facade.cam.stop_live()
        data = self.facade.cam.get_data()
        
        if pulsed_laser:
            self.facade.laser_con.laser_off(["488"])
        
        if not isinstance(data, np.ndarray):
            if recursion_ctr > 10:
                raise RuntimeError("Camera failed to acquire image after 10 retries")
            logger.warning("No image acquired, retrying...")
            return self._grab_image(savepath, filehandle, pulsed_laser, recursion_ctr + 1)
        
        img = data[0, :, :]
        
        if savepath is not None:
            np.save(str(savepath).replace(".npy", "") + ".npy", img)
        
        if filehandle is not None and savepath is not None:
            key = savepath.name.split("new_")[-1].replace(".npy", "")
            filehandle[key] = data
        
        return img

    def _get_stage_position(self) -> tuple[float, float]:
        """Return current (x, y) stage position."""
        return self.facade.stage_con.get_position()

    def _prepare_h5(self, save_folder: Path) -> None:
        """Remove old H5 and npy files before starting a new scan."""
        h5_path = save_folder / "Tiling_measurement.h5"
        if h5_path.exists():
            h5_path.unlink()
            logger.info("Removed existing %s", h5_path)
        
        for old_npy in save_folder.glob("img_new_*.npy"):
            old_npy.unlink()
