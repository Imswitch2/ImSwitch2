"""Recording workflow — acquire polarisation-resolved image stacks.

Ported from WFS RecordingWorkflow. Acquires H/V polarisation-resolved stacks
by moving HWP/QWP rotators, triggering Teensy pulse sequences, and grabbing
frames from the camera. Saves stacks to TIFF and computes a quick Stokes/
anisotropy summary.

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
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import numpy as np
import tifffile as tf

from imswitch.imcontrol.model.workflows.paths import (
    default_measurements_root,
    resolve_measurements_root,
)

if TYPE_CHECKING:
    from imswitch.imcontrol.model.workflows.facade import MicroscopeFacade

logger = logging.getLogger(__name__)

# Default base folder when none is provided.
DEFAULT_MEASUREMENTS_ROOT = default_measurements_root()


@dataclass
class RecordingParams:
    """Parameters for a single recording sequence.
    
    Attributes:
        pin488: Teensy pin number for 488 nm laser TTL output.
        pin405: Teensy pin number for 405 nm laser TTL output.
        camerapin: Teensy pin number for camera trigger output.
        start488: Start time (µs) for 488 nm pulse within each frame window.
        start405: Start time (µs) for 405 nm pulse within each frame window.
        start_camera: Start time (µs) for camera trigger pulse.
        width488: Pulse width (µs) for 488 nm laser.
        width405: Pulse width (µs) for 405 nm laser.
        width_camera: Pulse width (µs) for camera trigger (exposure time).
        dwelltime: Dwell time (µs) per frame — total frame period.
        delay_time: Inter-frame delay time (µs).
        frame_number: Number of signal/background frame pairs to acquire.
        move_waveplate: Whether to move QWP/HWP before each polarisation.
        record_h: Record horizontal polarisation stack.
        record_v: Record vertical polarisation stack.
        measurements_root: Base directory for saving stacks. Defaults to
            ``~/ImSwitchMeasurements`` if not provided.
        measurement_name_addition: Suffix to append to TIFF filenames.
    """
    
    pin488: int
    pin405: int
    camerapin: int
    start488: int
    start405: int
    start_camera: int
    width488: int
    width405: int
    width_camera: int
    dwelltime: int
    delay_time: int
    frame_number: int
    move_waveplate: bool = True
    record_h: bool = True
    record_v: bool = True
    measurements_root: Optional[Path | str] = None
    measurement_name_addition: str = ""


class RecordingWorkflow:
    """Encapsulates a single H/V recording sequence.
    
    Moves HWP/QWP rotators to H or V presets, fires Teensy pulse sequence,
    grabs N frames from camera, saves stack to TIFF, computes a quick
    Stokes/anisotropy summary.
    
    Args:
        facade: WFS-shaped hardware aggregator exposing ``laser_con``, ``cam``,
            ``trig``, ``rotator_hwp``, ``rotator_qwp``.
        params: Recording parameters (trigger timings, frame count, etc.).
        measurements_root: Override base directory for saving stacks. If
            ``None``, uses ``params.measurements_root`` or the default.
    """

    def __init__(
        self,
        facade: MicroscopeFacade,
        params: RecordingParams,
        measurements_root: Optional[Path | str] = None,
    ) -> None:
        self.facade = facade
        self.params = params
        self.measurements_root = resolve_measurements_root(
            measurements_root or params.measurements_root
        )

        # Populated during a recording run
        self.datastack: Optional[np.ndarray] = None
        self.datastack_h: Optional[np.ndarray] = None  # H-polarisation stack
        self.datastack_v: Optional[np.ndarray] = None  # V-polarisation stack

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Execute a full H/V recording sequence.
        
        For each requested polarisation (H and/or V):
        
        1. Move QWP/HWP to the corresponding preset (if enabled).
        2. Prepare camera acquisition for ``2 × frame_number`` frames.
        3. Start camera acquisition.
        4. Fire Teensy pulse sequence to trigger laser/camera.
        5. Wait for all frames to arrive.
        6. Save stack to TIFF under ``measurements_root/{YYYY_MM_DD}/``.
        7. Compute and log a quick Stokes/anisotropy summary.
        
        Raises:
            ValueError: If both ``record_h`` and ``record_v`` are False.
        """
        if not (self.params.record_h or self.params.record_v):
            logger.warning("Both record_h and record_v are False; nothing to do.")
            return
        
        # Disable any existing modulation mode on all lasers
        self.facade.laser_con.set_modulation_mode(None)

        # Pre-compute the pulse scheme once (shared between H and V)
        tWindowM, laserMod_1, laserMod_2, laserMod_3 = self.facade.trig.command(
            self.params.start488,
            self.params.start405,
            self.params.start_camera,
            self.params.width488,
            self.params.width405,
            self.params.width_camera,
            self.params.dwelltime,
        )

        if self.params.record_h:
            self._acquire_polarisation(
                pol="h",
                tWindowM=tWindowM,
                laserMod_1=laserMod_1,
                laserMod_2=laserMod_2,
                laserMod_3=laserMod_3,
            )

        if self.params.record_v:
            self._acquire_polarisation(
                pol="v",
                tWindowM=tWindowM,
                laserMod_1=laserMod_1,
                laserMod_2=laserMod_2,
                laserMod_3=laserMod_3,
            )

        logger.info("Recording sequence finished")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _acquire_polarisation(
        self,
        pol: str,
        tWindowM: np.ndarray,
        laserMod_1: np.ndarray,
        laserMod_2: np.ndarray,
        laserMod_3: np.ndarray,
    ) -> None:
        """Acquire a single polarisation (H or V) stack.
        
        Args:
            pol: Polarisation label — ``"h"`` or ``"v"``.
            tWindowM: Pre-computed time window array from ``trig.command()``.
            laserMod_1: Pre-computed laser modulation array (488 nm).
            laserMod_2: Pre-computed laser modulation array (405 nm).
            laserMod_3: Pre-computed laser modulation array (camera).
        """
        # Move waveplates to the target polarisation preset
        if self.params.move_waveplate:
            if pol == "h":
                self.facade.rotator_qwp.chained_move_to_h(
                    self.facade.rotator_hwp.move_to_h
                )
            else:
                self.facade.rotator_qwp.chained_move_to_v(
                    self.facade.rotator_hwp.move_to_v
                )

        # Prepare camera for 2 × frame_number frames (signal + background pairs)
        n_frames = self.params.frame_number * 2
        self.facade.cam.prepare_acquisition(n_frames)
        self.facade.cam.start_acquisition()
        time.sleep(0.1)
        logger.info("Start recording polarisation %s", pol)

        # Fire the pulse sequence
        self.facade.trig.Sendsignal(
            self.params.pin488,
            self.params.pin405,
            self.params.camerapin,
            self.params.delay_time,
            self.params.frame_number,
            tWindowM,
            laserMod_1,
            laserMod_2,
            laserMod_3,
        )
        
        # Wait for acquisition, save, and compute anisotropy
        self._finish_acquisition(pol=pol)
        
        # Store per-polarisation reference for downstream anisotropy analysis
        if pol == "h":
            self.datastack_h = self.datastack
        else:
            self.datastack_v = self.datastack

    def _finish_acquisition(self, pol: str) -> None:
        """Wait for all frames, save stack to TIFF, compute quick anisotropy.
        
        Args:
            pol: Polarisation label — ``"h"`` or ``"v"`` — used in filename.
        """
        n_expected = 2 * self.params.frame_number

        # Poll camera until all frames have arrived
        n_images_grabbed = 0
        while n_images_grabbed < n_expected:
            images = self.facade.cam.get_data()
            n_images_grabbed = len(images) if images is not None else 0
            time.sleep(1)

        self.datastack = images
        self.facade.cam.stop_acquisition()
        self._calculate_r(self.datastack)

        # Save to TIFF under measurements_root/{YYYY_MM_DD}/data_stack_{name}_{pol}.tif
        t_date = time.strftime("%Y_%m_%d")
        folder = self.measurements_root / t_date
        os.makedirs(folder, exist_ok=True)

        if isinstance(self.datastack, np.ndarray):
            suffix = self.params.measurement_name_addition
            out_path = folder / f"data_stack{suffix}_{pol}.tif"
            tf.imwrite(out_path, self.datastack)
            logger.info("Saved %s-polarisation stack to %s", pol, out_path)

        # TODO: napari display path — deferred to future widget task.
        # The WFS version had a live napari viewer integration for immediate
        # feedback; that requires a Qt widget context. For now, workflows are
        # model-only and headless.

    @staticmethod
    def _calculate_r(img: np.ndarray) -> None:
        """Compute and log a quick Stokes / anisotropy summary.
        
        Extracts 0°, 45°, 90°, 135° detector channels from the micro-polariser
        array (spatial subsampling), computes Stokes parameters S0, S1, S2,
        angle of linear polarisation (AoLP), and anisotropy ``r``.
        
        Args:
            img: Raw camera stack of shape ``(2·N, H, W)`` where even frames
                are signal and odd frames are background.
        """
        img = img[0::2]  # Keep only signal frames
        img_0degree = img[1::2, 1::2]
        img_45degree = img[1::2, ::2]
        img_90degree = img[::2, ::2]
        img_135degree = img[::2, 1::2]

        p_0 = np.sum(np.mean(img_0degree, axis=0)) / 250_000
        p_45 = np.sum(np.mean(img_45degree, axis=0)) / 250_000
        p_90 = np.sum(np.mean(img_90degree, axis=0)) / 250_000
        p_135 = np.sum(np.mean(img_135degree, axis=0)) / 250_000

        S0 = p_0 + p_90
        S1 = p_0 - p_90
        S2 = p_45 - p_135
        AoLP = np.arctan2(S2, S1)
        r = (p_0 - p_90) / (p_0 + 2 * p_90)

        logger.info(
            "p_0=%.4f  p_90=%.4f  p_45=%.4f  p_135=%.4f  g=%.4f  AoLP=%.4f  r=%.4f",
            p_0, p_90, p_45, p_135, p_0 / p_90 if p_90 else float("nan"),
            AoLP, r,
        )

    # ------------------------------------------------------------------
    # Per-pixel anisotropy maps (for histogram display)
    # ------------------------------------------------------------------

    @staticmethod
    def compute_anisotropy_map(stack: np.ndarray) -> np.ndarray:
        """Per-superpixel anisotropy from a *single* polarisation stack.

        Uses the 0° and 90° detector pixels (spatial subsampling in the
        micro-polariser array): ``r = (I_0 - I_90) / (I_0 + 2·I_90)``.

        Args:
            stack: Raw camera stack of shape ``(2·N, H, W)``.  Even frames
                   are signal, odd frames are background.

        Returns:
            2-D float32 ``r`` map.  Pixels where the denominator is ≤ 0 are
            set to ``NaN``.
        """
        sig = stack[0::2].astype(np.float64)
        p0  = np.mean(sig[:, 1::2, 1::2], axis=0)   # 0° detector pixels
        p90 = np.mean(sig[:, ::2,  ::2],  axis=0)   # 90° detector pixels
        denom = p0 + 2.0 * p90
        r_map = np.where(denom > 0, (p0 - p90) / denom, np.nan)
        return r_map.astype(np.float32)

    @staticmethod
    def compute_full_anisotropy_map(
        stack_h: np.ndarray,
        stack_v: np.ndarray,
    ) -> np.ndarray:
        """G-factor–corrected per-superpixel anisotropy from H and V stacks.

        Follows the two-channel correction scheme used in WFS module_analysis:

        .. code-block:: text

            G  = sqrt(I_hv · I_vv / (I_hh · I_vh))
            K2 = sqrt(I_vh · I_vv / (I_hh · I_hv))
            I_∥  = I_hh + I_vv / (G · K2)
            I_⊥  = I_hv / G  +  I_vh / K2
            r    = (I_∥ − I_⊥) / (I_∥ + 2·I_⊥)

        where the subscript notation is (excitation, detection) and the
        detector channels are extracted by spatial subsampling.

        Args:
            stack_h: H-polarisation camera stack ``(2·N, H, W)``.
            stack_v: V-polarisation camera stack ``(2·N, H, W)``.

        Returns:
            2-D float32 ``r`` map; NaN where the signal is too weak.
        """
        def _sig(stack: np.ndarray) -> np.ndarray:
            """Mean of signal frames."""
            return np.mean(stack[0::2].astype(np.float64), axis=0)

        h = _sig(stack_h)
        v = _sig(stack_v)

        ihh = h[1::2, 1::2]   # H excitation, 0°  detection
        ihv = h[::2,  ::2]    # H excitation, 90° detection
        ivh = v[1::2, 1::2]   # V excitation, 0°  detection
        ivv = v[::2,  ::2]    # V excitation, 90° detection

        with np.errstate(invalid="ignore", divide="ignore"):
            G  = np.where(ihh * ivh > 0, np.sqrt(np.abs(ihv * ivv / (ihh * ivh))), 1.0)
            K2 = np.where(ihh * ihv > 0, np.sqrt(np.abs(ivh * ivv / (ihh * ihv))), 1.0)
            il = ihh + np.where(G * K2 > 0, ivv / (G * K2), 0.0)
            ip = np.where(G > 0, ihv / G, 0.0) + np.where(K2 > 0, ivh / K2, 0.0)
            denom = il + 2.0 * ip
            r_map = np.where(denom > 0, (il - ip) / denom, np.nan)

        return r_map.astype(np.float32)
