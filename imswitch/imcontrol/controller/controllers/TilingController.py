import threading
import time
from typing import List, Optional, Tuple

import numpy as np
from qtpy import QtCore

from imswitch.imcommon.model import APIExport
from imswitch.imcontrol.model.workflows import StitchedImage
from imswitch.imcontrol.model.workflows.spiral import spiral_moves
from ..basecontrollers import ImConWidgetController

_SETTLE_S = 0.15  # stage settle time after each move (seconds)


class TilingController(ImConWidgetController):
    """Controls spiral tiling scans with live stitching and click-to-navigate."""

    sigOverviewUpdated = QtCore.Signal(object)   # np.ndarray
    sigProgressUpdated = QtCore.Signal(int, int)  # current, total

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._scanning = False
        self._stopRequested = False
        self._stitcher: Optional[StitchedImage] = None
        self._originXY: Optional[Tuple[float, float]] = None
        self._gridPositions: List[Tuple[int, int]] = []
        self._lastStepUm: float = 100.0

        tilingInfo = self._setupInfo.tiling
        if tilingInfo is None:
            return

        self._widget.setDefaultStep(tilingInfo.defaultTileStepUm)

        self._widget.sigStartTiling.connect(self.startTiling)
        self._widget.sigStopTiling.connect(self.stopTiling)
        self._widget.sigClickOnOverview.connect(self._navigateToPixel)
        self.sigOverviewUpdated.connect(self._widget.updateOverview)
        self.sigProgressUpdated.connect(self._widget.setProgress)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @APIExport()
    def startTiling(self) -> None:
        if self._scanning:
            return
        tilingInfo = self._setupInfo.tiling
        if tilingInfo is None:
            return

        n_tiles = self._widget.getNTiles()
        step_um = self._widget.getTileStepUm()

        self._scanning = True
        self._stopRequested = False
        self._stitcher = None
        self._originXY = None
        self._gridPositions = []
        self._lastStepUm = step_um

        self._widget.setRunning(True)
        self._widget.setProgress(0, n_tiles)

        t = threading.Thread(
            target=self._runScan,
            args=(tilingInfo, n_tiles, step_um),
            daemon=True,
        )
        t.start()

    @APIExport()
    def stopTiling(self) -> None:
        self._stopRequested = True

    @APIExport()
    def setTileLabel(self, label: str) -> None:
        self._widget.setLabel(label)

    # ------------------------------------------------------------------
    # Scan loop (runs in background thread)
    # ------------------------------------------------------------------

    def _runScan(self, tilingInfo, n_tiles: int, step_um: float) -> None:
        try:
            positioner = self._master.positionersManager[tilingInfo.xyPositioner]
            axes = list(self._setupInfo.positioners[tilingInfo.xyPositioner].axes)
            x_axis, y_axis = axes[0], axes[1]

            camera = (tilingInfo.camera
                      if tilingInfo.camera
                      else self._defaultCamera())
            detector = self._master.detectorsManager[camera]

            # Record starting position as grid origin (0, 0)
            start_pos = positioner.position
            self._originXY = (start_pos[x_axis], start_pos[y_axis])

            gx, gy = 0, 0
            for i, (dx, dy) in enumerate(spiral_moves(n_tiles)):
                if self._stopRequested:
                    break

                # Move to next tile (skip move for the very first position)
                if i > 0:
                    positioner.move(dx * step_um, x_axis)
                    positioner.move(dy * step_um, y_axis)
                    time.sleep(_SETTLE_S)

                gx += dx
                gy += dy

                frame = detector.getLatestFrame()

                # Lazy stitcher init after first frame (we need tile_size_px)
                if self._stitcher is None:
                    tile_size_px = max(frame.shape[0], frame.shape[1])
                    self._stitcher = StitchedImage(
                        tile_size_px=tile_size_px,
                        tile_step_um=step_um,
                        px_per_um=tile_size_px / step_um,
                        overlap=0.0,
                    )

                self._stitcher.add_tile(frame, gx, gy)
                self._gridPositions.append((gx, gy))

                overview = self._stitcher.get_overview()
                self.sigOverviewUpdated.emit(overview)
                self.sigProgressUpdated.emit(i + 1, n_tiles)

            # Return to origin
            positioner.setPosition(self._originXY[0], x_axis)
            positioner.setPosition(self._originXY[1], y_axis)

        except Exception as e:
            self._logger.error(f'Tiling scan failed: {e}', exc_info=True)
        finally:
            self._scanning = False
            QtCore.QMetaObject.invokeMethod(
                self._widget, 'setRunning',
                QtCore.Qt.QueuedConnection,
                QtCore.Q_ARG(bool, False),
            )

    # ------------------------------------------------------------------
    # Click-to-navigate
    # ------------------------------------------------------------------

    def _navigateToPixel(self, row: int, col: int) -> None:
        if self._stitcher is None or self._originXY is None or not self._gridPositions:
            return
        tilingInfo = self._setupInfo.tiling
        if tilingInfo is None:
            return

        min_gx = min(g[0] for g in self._gridPositions)
        min_gy = min(g[1] for g in self._gridPositions)
        canvas_origin = (
            self._originXY[0] + min_gx * self._lastStepUm,
            self._originXY[1] + min_gy * self._lastStepUm,
        )

        stage_x, stage_y = self._stitcher.pixel_to_stage(row, col, canvas_origin)

        axes = list(self._setupInfo.positioners[tilingInfo.xyPositioner].axes)
        positioner = self._master.positionersManager[tilingInfo.xyPositioner]
        positioner.setPosition(stage_x, axes[0])
        positioner.setPosition(stage_y, axes[1])

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _defaultCamera(self) -> str:
        for name, info in self._setupInfo.detectors.items():
            if info.forAcquisition:
                return name
        return next(iter(self._setupInfo.detectors))


# Copyright (C) 2020-2021 ImSwitch developers
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
