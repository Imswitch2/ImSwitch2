"""Polarisation calibration and segmentation-parameter workflows.

Ported from WidefieldStarss/src/WFS/workflows/calibration.py. Provides
hardware-triggered QWP/HWP sweep for polarisation camera calibration and
a simplified segmentation sanity-check routine.
"""

from __future__ import annotations

import csv
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import numpy as np

if TYPE_CHECKING:
    from imswitch.imcontrol.model.workflows.facade import MicroscopeFacade

logger = logging.getLogger(__name__)

DEFAULT_MEASUREMENTS_ROOT = Path.home() / "ImSwitchMeasurements"


@dataclass
class CalibrationParams:
    """Configuration for calibration workflows.

    Attributes:
        n_steps_qwp: Number of QWP angles in [0, 180] deg sweep.
        n_steps_hwp: Number of HWP angles in [0, 180] deg sweep.
        laser_power_488_mw: Power setting for 488 nm laser (mW).
        exposure_us: Camera exposure in microseconds.
        laser_pin: Pulse-generator pin for laser modulation.
        camera_pin: Pulse-generator pin for camera trigger.
        pulsed: If True, use hardware-triggered acquisition; if False, live mode.
        measurements_root: Base folder for saving calibration data.
    """

    n_steps_qwp: int = 10
    n_steps_hwp: int = 10
    laser_power_488_mw: float = 5.0
    exposure_us: int = 50000
    laser_pin: int = 1
    camera_pin: int = 0
    pulsed: bool = True
    measurements_root: Optional[str | Path] = DEFAULT_MEASUREMENTS_ROOT


class CalibrationWorkflow:
    """Polarisation calibration and segmentation parameter check.

    Args:
        facade: Hardware aggregator (:class:`MicroscopeFacade`).
        params: Calibration configuration.
    """

    def __init__(self, facade: MicroscopeFacade, params: CalibrationParams) -> None:
        self.facade = facade
        self.params = params

    def run(self, save_folder: Optional[str | Path] = None) -> None:
        """Run the default polarisation calibration workflow."""
        self.run_polarisation_calibration(save_folder=save_folder)

    def _snap_triggered(self, laser_pin: int, camera_pin: int, exposure_us: int) -> np.ndarray:
        """Grab one hardware-triggered frame with a single laser pulse."""
        if self.facade.cam is None:
            raise RuntimeError("CalibrationWorkflow: camera facade is None")
        if self.facade.trig is None:
            raise RuntimeError("CalibrationWorkflow: trigger facade is None")

        self.facade.cam.prepare_acquisition(1)
        self.facade.cam.start_acquisition()
        logger.debug("Camera ready for triggered snap")

        self.facade.trig.snap_trigger(
            laser_pin=laser_pin,
            camera_pin=camera_pin,
            exposure_us=exposure_us,
        )

        arrived = self.facade.cam.wait_for_frame(timeout_s=2.0)
        if not arrived:
            logger.warning("Hardware-triggered frame did not arrive within 2 s")

        self.facade.cam.stop_acquisition()
        data = self.facade.cam.get_data()

        if isinstance(data, np.ndarray) and data.ndim >= 2:
            return data[0] if data.ndim == 3 else data

        logger.warning("Camera returned no data during triggered snap — using zeros")
        return np.zeros((1804, 1804), dtype=np.uint16)

    def run_polarisation_calibration(self, save_folder: Optional[str | Path] = None) -> None:
        """Sweep QWP/HWP angles and record camera quad-pixel means.

        Args:
            save_folder: Where to save the CSV. If ``None``, defaults to
                         ``<measurements_root>/<YYYY_MM_DD>/polcal_<HHMMSS>.csv``.
        """
        if self.facade.rotator_hwp is None or self.facade.rotator_qwp is None:
            raise RuntimeError(
                "CalibrationWorkflow: HWP or QWP rotator facade is None. "
                "Both are required for polarisation calibration."
            )

        ang_qwp = np.linspace(0, 180, self.params.n_steps_qwp)
        ang_hwp = np.linspace(0, 180, self.params.n_steps_hwp)

        rows: list[dict] = []

        logger.info(
            "Starting polarisation calibration: %d QWP × %d HWP = %d measurements",
            len(ang_qwp),
            len(ang_hwp),
            len(ang_qwp) * len(ang_hwp),
        )

        for i, hwp_deg in enumerate(ang_hwp):
            logger.info("Calibration sweep %d/%d (HWP=%.2f°)", i + 1, len(ang_hwp), hwp_deg)
            self.facade.rotator_hwp.move_abs(hwp_deg)

            for qwp_deg in ang_qwp:
                try:
                    self.facade.rotator_qwp.move_abs(qwp_deg)

                    if self.facade.laser_con is not None:
                        self.facade.laser_con.set_triggered_mode(
                            ["488"], [self.params.laser_power_488_mw]
                        )

                    time.sleep(0.5)

                    data = self._snap_triggered(
                        self.params.laser_pin,
                        self.params.camera_pin,
                        self.params.exposure_us,
                    )

                    if not isinstance(data, np.ndarray):
                        continue

                    # Quad-pixel means: polarisation camera convention
                    p_0 = float(np.mean(data[1::2, 1::2]))
                    p_45 = float(np.mean(data[1::2, ::2]))
                    p_90 = float(np.mean(data[::2, ::2]))
                    p_135 = float(np.mean(data[::2, 1::2]))
                    mean_intensity = float(np.mean([p_0, p_45, p_90, p_135]))

                    rows.append(
                        {
                            "qwp_angle": qwp_deg,
                            "hwp_angle": hwp_deg,
                            "p_0": p_0,
                            "p_45": p_45,
                            "p_90": p_90,
                            "p_135": p_135,
                            "mean_intensity": mean_intensity,
                        }
                    )

                except Exception as e:
                    logger.exception(
                        "Error at HWP=%.2f°, QWP=%.2f°: %s", hwp_deg, qwp_deg, e
                    )

        if not rows:
            logger.warning("Calibration produced no valid measurements")
            return

        # Default save path: measurements_root/{YYYY_MM_DD}/polcal_{HHMMSS}.csv
        if save_folder is None:
            now = datetime.now()
            date_folder = now.strftime("%Y_%m_%d")
            time_str = now.strftime("%H%M%S")
            measurements_root = self.params.measurements_root or DEFAULT_MEASUREMENTS_ROOT
            folder = Path(measurements_root).expanduser() / date_folder
            folder.mkdir(parents=True, exist_ok=True)
            out_path = folder / f"polcal_{time_str}.csv"
        else:
            out_path = Path(save_folder) / "pol_calibration_cam.csv"
            out_path.parent.mkdir(parents=True, exist_ok=True)

        fieldnames = [
            "qwp_angle",
            "hwp_angle",
            "p_0",
            "p_45",
            "p_90",
            "p_135",
            "mean_intensity",
        ]
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        logger.info("Calibration saved to %s (%d rows)", out_path, len(rows))

    def run_segmentation_param_check(self) -> None:
        """Snap a test tile and run segmentation for sanity checking.

        This is a simplified placeholder. In the full WFS workflow this
        delegates to :class:`~WFS.workflows.tiling.TilingWorkflow` and
        launches an interactive parameter-tuning UI. For ImSwitch we
        provide only the basic snap-and-log path.
        """
        if self.facade.cam is None:
            raise RuntimeError("CalibrationWorkflow: camera facade is None")

        logger.info("Segmentation parameter check: snapping test tile")

        if self.facade.laser_con is not None:
            self.facade.laser_con.set_constant_power(
                ["488"], [self.params.laser_power_488_mw]
            )
        time.sleep(0.5)

        self.facade.cam.prepare_live()
        self.facade.cam.start_live()
        time.sleep(0.1)
        self.facade.cam.stop_live()
        data = self.facade.cam.get_data()

        if self.facade.laser_con is not None:
            self.facade.laser_con.set_modulation_mode(["488"])

        if not isinstance(data, np.ndarray):
            logger.warning("No data returned from camera during segmentation check")
            return

        img = data[0] if data.ndim == 3 else data
        logger.info(
            "Segmentation check: acquired %s image, mean=%.1f, max=%d",
            img.shape,
            float(np.mean(img)),
            int(np.max(img)),
        )
        # In a full implementation this would call a segmentation module and
        # provide interactive parameter tuning. For now we just log success.


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
