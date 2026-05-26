"""Z-stack acquisition workflow.

Steps the Jena Z-piezo through a user-defined range and snaps one frame at
each position. The resulting 3D stack (Z, H, W) is saved as a TIFF and
returned to the caller.

Ported from WFS ZStackWorkflow to ImSwitch model/workflows pattern.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Tuple

import numpy as np
import skimage.filters
import tifffile as tf

if TYPE_CHECKING:
    from imswitch.imcontrol.model.workflows.facade import MicroscopeFacade

logger = logging.getLogger(__name__)


@dataclass
class ZStackParams:
    """Parameters for Z-stack acquisition.

    Attributes:
        n_planes: Number of Z planes to acquire.
        step_um: Step size between planes in micrometers.
        laser_pin: Teensy digital pin for laser trigger (used only if pulsed=True).
        camera_pin: Teensy digital pin for camera trigger (used only if pulsed=True).
        pulsed: If True, use hardware-triggered acquisition via Teensy.
                If False, use software live-mode snaps.
        laser_power_488_mw: Laser power in milliwatts for 488 nm laser.
        exposure_us: Camera exposure time in microseconds.
        measurements_root: Root directory for saving measurements.
    """

    n_planes: int
    step_um: float
    laser_pin: Optional[int] = None
    camera_pin: Optional[int] = None
    pulsed: bool = False
    laser_power_488_mw: float = 50.0
    exposure_us: float = 50000.0
    measurements_root: str = "~/Measurements"


class ZStackWorkflow:
    """Acquire a Z-stack by stepping the piezo and snapping one frame per plane.

    Args:
        facade: WFS-shaped hardware facade exposing z_stage_con, cam, laser_con,
                and optionally trig for hardware-triggered acquisition.
        params: Z-stack acquisition parameters.
    """

    def __init__(self, facade: MicroscopeFacade, params: ZStackParams) -> None:
        self.facade = facade
        self.params = params

    def run(
        self,
        save_stack: bool = True,
        z_start: Optional[float] = None,
    ) -> Tuple[np.ndarray, list]:
        """Acquire the Z-stack and optionally save it.

        Args:
            save_stack: If True, save the stack as TIFF to measurements_root.
            z_start: Starting Z position in micrometers. If None, use current position.

        Returns:
            Tuple of (stack, z_positions) where stack is a 3-D array of shape
            (n_planes, H, W) and z_positions is a list of Z coordinates in µm.
        """
        if self.facade.z_stage_con is None:
            raise RuntimeError("Z-stack workflow requires z_stage_con in facade")
        if self.facade.cam is None:
            raise RuntimeError("Z-stack workflow requires cam in facade")
        if self.facade.laser_con is None:
            raise RuntimeError("Z-stack workflow requires laser_con in facade")

        # Centre the stack around the current piezo position
        if z_start is None:
            z_start = self.facade.z_stage_con.read_pos_um()

        self.facade.z_stage_con.activate_ext_control()
        self.facade.z_stage_con.set_pos_um(z_start)

        half_range = (self.params.n_planes - 1) * self.params.step_um / 2.0
        z_positions = [
            z_start - half_range + i * self.params.step_um
            for i in range(self.params.n_planes)
        ]

        # Clamp to the piezo's allowed range
        z_min, z_max = self.facade.z_stage_con.pos_range_um
        z_positions = [float(np.clip(z, z_min, z_max)) for z in z_positions]

        logger.info(
            "Z-stack: %d planes from %.2f µm to %.2f µm (step %.2f µm)",
            self.params.n_planes,
            z_positions[0],
            z_positions[-1],
            self.params.step_um,
        )

        frames: list[np.ndarray] = []

        # Configure laser mode
        if self.params.pulsed:
            if self.facade.trig is None:
                raise RuntimeError(
                    "Hardware-triggered mode requested but trig facade is not configured"
                )
            if self.params.laser_pin is None or self.params.camera_pin is None:
                raise ValueError(
                    "pulsed=True requires laser_pin and camera_pin to be set"
                )
            self.facade.laser_con.set_triggered_mode(
                ["488"], [self.params.laser_power_488_mw]
            )
            time.sleep(0.5)
        else:
            self.facade.laser_con.set_constant_power(
                ["488"], [self.params.laser_power_488_mw]
            )
            time.sleep(1.0)

        try:
            for i, z in enumerate(z_positions):
                logger.debug(
                    "Z-stack plane %d/%d at %.2f µm", i + 1, self.params.n_planes, z
                )
                self.facade.z_stage_con.set_pos_um(z)
                time.sleep(0.3)

                if self.params.pulsed:
                    frames.append(
                        self._snap_triggered(
                            self.params.laser_pin,
                            self.params.camera_pin,
                            int(self.params.exposure_us),
                        )
                    )
                else:
                    frames.append(self._snap())

            # Restore laser and Z position
            self.facade.laser_con.set_modulation_mode(["488"])
            self.facade.z_stage_con.set_pos_um(z_start)
            logger.info("Z-stack done — returned to %.2f µm", z_start)

            stack = np.stack(frames, axis=0)

            if save_stack:
                self._save(stack)

            return stack, z_positions

        except Exception as e:
            logger.exception("Z-stack acquisition failed")
            # Attempt cleanup
            try:
                self.facade.laser_con.set_modulation_mode(["488"])
                self.facade.z_stage_con.set_pos_um(z_start)
            except Exception:
                pass
            raise

    def run_autofocus(
        self, z_start: Optional[float] = None
    ) -> Tuple[np.ndarray, list]:
        """Run Z-stack acquisition and move stage to computed focus position.

        Computes a gradient-energy profile across the Z-stack, fits a quadratic,
        and moves the stage to the vertex (best focus). Raises if focus falls
        outside the scanned range.

        Args:
            z_start: Starting Z position in micrometers. If None, use current position.

        Returns:
            Tuple of (stack, z_positions) after the stage has been moved to focus.

        Raises:
            RuntimeError: If the computed focus position falls outside the scanned range.
        """
        # Acquire Z-stack without saving
        img, z_positions = self.run(save_stack=False, z_start=z_start)

        # Binning-style downsample: sum 2×2 pixel blocks
        img_binned = (
            img[:, 1::2, 1::2]
            + img[:, 0::2, 0::2]
            + img[:, 1::2, 0::2]
            + img[:, 0::2, 1::2]
        )

        # Gaussian smoothing per plane
        img_smooth = []
        for i in range(img_binned.shape[0]):
            img_smooth.append(skimage.filters.gaussian(img_binned[i, :, :], sigma=3))
        img_proc = np.asarray(img_smooth)

        # Normalize
        img_proc = img_proc - img_proc.min()
        median_val = np.median(img_proc)
        if median_val > 0:
            img_proc = img_proc / median_val

        # Gradient energy metric (Sobel-ish)
        z_profile = np.sqrt(
            np.mean(np.mean(np.diff(img_proc, axis=-1) ** 2, axis=-1), axis=-1)
            + np.mean(np.mean(np.diff(img_proc, axis=-2) ** 2, axis=-1), axis=-1)
        )

        # Fit quadratic
        coeffs = np.polyfit(z_positions, z_profile, 2)
        if len(coeffs) < 3 or coeffs[0] == 0:
            raise RuntimeError(
                "Autofocus failed: gradient profile is too flat or linear for quadratic fit"
            )
        
        fit = np.poly1d(coeffs)
        z_focus = -fit.c[1] / (2 * fit.c[0])

        logger.info("Autofocus done. Found focus pos at %.2f µm", z_focus)

        # Validate focus is within scanned range
        if z_focus < min(z_positions) or z_focus > max(z_positions):
            raise RuntimeError(
                f"Autofocus failed: computed focus {z_focus:.2f} µm is outside "
                f"scanned range [{min(z_positions):.2f}, {max(z_positions):.2f}] µm"
            )

        # Move stage to focus
        if self.facade.z_stage_con is not None:
            try:
                self.facade.z_stage_con.set_pos_um(z_focus)
            except Exception as e:
                logger.exception("Failed to move stage to focus position")
                raise

        return img, z_positions

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _snap(self) -> np.ndarray:
        """Grab one frame from the camera in live mode."""
        self.facade.cam.prepare_live()
        self.facade.cam.start_live()
        time.sleep(0.05)
        self.facade.cam.stop_live()
        data = self.facade.cam.get_data()
        if isinstance(data, np.ndarray) and data.size > 0:
            return data[0] if data.ndim > 2 else data
        logger.warning("Camera returned no data during Z-stack snap — using zeros")
        return np.zeros((512, 512), dtype=np.uint16)

    def _snap_triggered(
        self, laser_pin: int, camera_pin: int, exposure_us: int
    ) -> np.ndarray:
        """Grab one hardware-triggered frame with a single laser pulse."""
        self.facade.cam.prepare_acquisition(1)
        self.facade.cam.start_acquisition()
        self.facade.trig.snap_trigger(
            laser_pin=laser_pin,
            camera_pin=camera_pin,
            exposure_us=exposure_us,
        )
        arrived = self.facade.cam.wait_for_frame(timeout_s=2.0)
        if not arrived:
            logger.warning(
                "Z-stack: hardware-triggered frame did not arrive within 2 s"
            )
        self.facade.cam.stop_acquisition()
        data = self.facade.cam.get_data()
        if isinstance(data, np.ndarray) and data.size > 0:
            return data[0] if data.ndim > 2 else data
        logger.warning(
            "Camera returned no data during triggered Z-stack snap — using zeros"
        )
        return np.zeros((512, 512), dtype=np.uint16)

    def _save(self, stack: np.ndarray) -> None:
        """Save the stack as a TIFF to measurements_root/{YYYY_MM_DD}/zstack_{HHMMSS}.tif."""
        root = Path(self.params.measurements_root).expanduser()
        date_folder = root / time.strftime("%Y_%m_%d")
        date_folder.mkdir(parents=True, exist_ok=True)

        timestamp = time.strftime("%H%M%S")
        out_path = date_folder / f"zstack_{timestamp}.tif"

        tf.imsave(out_path, stack)
        logger.info("Z-stack saved to %s", out_path)


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
