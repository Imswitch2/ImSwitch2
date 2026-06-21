"""CWSTARSS acquisition workflow.

Sequence (per polarisation H then V):
  a) Pre-bleach: single 2 ms 488 nm pulse at maximum power, no acquisition.
  b) Stream 488 nm only at specified power for the experiment duration.
  c) Stream 488 + 405 nm at specified powers for the experiment duration.

Ported from WidefieldStarss/src/WFS/workflows/cwstarss.py to ImSwitch.
"""

from __future__ import annotations

import logging
import os
import re
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

DEFAULT_MEASUREMENTS_ROOT = default_measurements_root()


@dataclass
class CWSTARSSParams:
    """Parameters for CW-STARSS photoselection experiment.

    Args:
        fps: Target camera frame rate used to calculate number of frames.
        duration_s: Duration of each streaming phase in seconds.
        power_488_mw: 488 nm CW power for streaming phases (mW).
        power_405_mw: 405 nm power for the combined streaming phase (mW).
        measurements_root: Base directory for saving data.
        laser_488_name: Facade laser key used for the 488 nm role.
        laser_405_name: Facade laser key used for the 405 nm role.
    """

    fps: float
    duration_s: float
    power_488_mw: float
    power_405_mw: float
    measurements_root: Optional[Path | str] = DEFAULT_MEASUREMENTS_ROOT
    laser_488_name: str = "488"
    laser_405_name: str = "405"


class CWSTARSSWorkflow:
    """CW-STARSS photoselection experiment.

    Args:
        facade: Hardware aggregator providing access to lasers, camera, and rotators.
        params: Experiment parameters.
    """

    def __init__(
        self,
        facade: MicroscopeFacade,
        params: CWSTARSSParams,
    ) -> None:
        self.facade = facade
        self.params = params

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Execute the full CWSTARSS experiment."""
        logger.info(
            "CWSTARSS starting — fps=%.1f  duration=%.1f s  "
            "power_488=%.1f mW  power_405=%.1f mW  lasers=(%s, %s)",
            self.params.fps,
            self.params.duration_s,
            self.params.power_488_mw,
            self.params.power_405_mw,
            self._laser_488_name,
            self._laser_405_name,
        )
        try:
            self._run_experiment()
        finally:
            self._cleanup()

    # ------------------------------------------------------------------
    # Experiment sequence
    # ------------------------------------------------------------------

    def _run_experiment(self) -> None:
        for pol in ("H", "V"):
            self._move_rotators(pol)
            self._prebleach_488(pol)
            self._stream_488(pol)
            self._stream_488_405(pol)

        logger.info("CWSTARSS experiment complete")

    # ------------------------------------------------------------------
    # Phase implementations
    # ------------------------------------------------------------------

    def _move_rotators(self, pol: str) -> None:
        """Move rotators to H or V polarisation position."""
        logger.info("CWSTARSS: Moving rotators to %s position …", pol)
        if pol == "H":
            self.facade.rotator_qwp.chained_move_to_h(self.facade.rotator_hwp.move_to_h)
        else:
            self.facade.rotator_qwp.chained_move_to_v(self.facade.rotator_hwp.move_to_v)
        logger.info("CWSTARSS: Rotators at %s — done", pol)

    def _prebleach_488(self, pol: str) -> None:
        """Pre-bleach pulse: 2 ms 488 nm at high power, camera NOT armed.

        Uses constant-power mode for a short pulse without camera acquisition.
        This bleaches the sample before the streaming phases begin.
        """
        logger.info("CWSTARSS: Pre-bleach pulse 2 ms 488 nm — pol=%s …", pol)

        # Turn off all lasers first
        self.facade.laser_con.laser_off(self._all_laser_names)

        # Fire 488 at high power (use power_488_mw as the pre-bleach power)
        self.facade.laser_con.set_constant_power(
            [self._laser_488_name], [self.params.power_488_mw]
        )
        time.sleep(0.002)  # 2 ms pulse
        self.facade.laser_con.laser_off([self._laser_488_name])

        logger.info("CWSTARSS: Pre-bleach done — pol=%s", pol)

    def _stream_488(self, pol: str) -> None:
        """488 nm CW streaming acquisition, save as separate dataset."""
        n_frames = int(self.params.fps * self.params.duration_s)
        logger.info(
            "CWSTARSS: Streaming 488 nm only — pol=%s  fps=%.1f  "
            "duration=%.1f s  n_frames=%d  power=%.1f mW …",
            pol,
            self.params.fps,
            self.params.duration_s,
            n_frames,
            self.params.power_488_mw,
        )

        # Ensure lasers off before starting
        self.facade.laser_con.laser_off(self._all_laser_names)

        # Prepare camera
        self.facade.cam.prepare_acquisition(n_frames)
        self.facade.cam.start_acquisition()

        # Turn on 488 laser
        self.facade.laser_con.set_constant_power(
            [self._laser_488_name], [self.params.power_488_mw]
        )

        # Wait for acquisition to complete
        time.sleep(self.params.duration_s)

        # Turn off laser and stop acquisition
        self.facade.laser_con.laser_off([self._laser_488_name])
        self.facade.cam.stop_acquisition()

        # Retrieve and save data
        frames = self.facade.cam.get_data()
        if frames is not None:
            self._save(frames, label=self._phase_label(pol, self._laser_488_name))
        else:
            logger.warning("CWSTARSS: No data acquired for 488-only phase — pol=%s", pol)

        logger.info("CWSTARSS: 488-only stream done — pol=%s", pol)

    def _stream_488_405(self, pol: str) -> None:
        """488 + 405 nm CW streaming acquisition, save as separate dataset."""
        n_frames = int(self.params.fps * self.params.duration_s)
        logger.info(
            "CWSTARSS: Streaming 488+405 nm — pol=%s  fps=%.1f  "
            "duration=%.1f s  n_frames=%d  power_488=%.1f mW  power_405=%.1f mW …",
            pol,
            self.params.fps,
            self.params.duration_s,
            n_frames,
            self.params.power_488_mw,
            self.params.power_405_mw,
        )

        # Ensure lasers off before starting
        self.facade.laser_con.laser_off(self._all_laser_names)

        # Prepare camera
        self.facade.cam.prepare_acquisition(n_frames)
        self.facade.cam.start_acquisition()

        # Turn on both lasers
        self.facade.laser_con.set_constant_power(
            [self._laser_488_name, self._laser_405_name],
            [self.params.power_488_mw, self.params.power_405_mw],
        )

        # Wait for acquisition to complete
        time.sleep(self.params.duration_s)

        # Turn off lasers and stop acquisition
        self.facade.laser_con.laser_off(self._all_laser_names)
        self.facade.cam.stop_acquisition()

        # Retrieve and save data
        frames = self.facade.cam.get_data()
        if frames is not None:
            self._save(
                frames,
                label=self._phase_label(
                    pol,
                    self._laser_488_name,
                    self._laser_405_name,
                ),
            )
        else:
            logger.warning("CWSTARSS: No data acquired for 488+405 phase — pol=%s", pol)

        logger.info("CWSTARSS: 488+405 stream done — pol=%s", pol)

    def _cleanup(self) -> None:
        """Ensure all lasers are off (safety cleanup, always called in finally)."""
        logger.info("CWSTARSS: Cleanup — lasers off")
        try:
            self.facade.laser_con.laser_off(self._all_laser_names)
        except Exception as e:
            logger.error("CWSTARSS: Cleanup failed — %s", e)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def _laser_488_name(self) -> str:
        return str(self.params.laser_488_name)

    @property
    def _laser_405_name(self) -> str:
        return str(self.params.laser_405_name)

    @property
    def _all_laser_names(self) -> list[str]:
        return list(dict.fromkeys([self._laser_488_name, self._laser_405_name]))

    def _phase_label(self, pol: str, *laser_names: str) -> str:
        tokens = [pol, *(self._safe_label_token(name) for name in laser_names)]
        return "_".join(tokens)

    @staticmethod
    def _safe_label_token(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("_") or "laser"

    def _save(self, frames: np.ndarray, label: str) -> None:
        """Save acquired frames to TIFF under <measurements_root>/<date>/."""
        t_date = time.strftime("%Y_%m_%d")
        t = time.strftime("%H%M%S")
        folder = resolve_measurements_root(self.params.measurements_root) / t_date
        os.makedirs(folder, exist_ok=True)
        filename = folder / f"cwstarss_{label}_{t}.tif"
        tf.imwrite(str(filename), frames)
        logger.info("Saved %s", filename)


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
