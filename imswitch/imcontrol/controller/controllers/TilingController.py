import threading
import time
from typing import Callable, List, Optional, Tuple

import numpy as np
from qtpy import QtCore

from imswitch.imcommon.model import APIExport
from imswitch.imcontrol.model.managers import LeasePurpose
from imswitch.imcontrol.model.workflows import StitchedImage
from imswitch.imcontrol.model.workflows.spiral import spiral_moves
from ..basecontrollers import ImConWidgetController

_SETTLE_S = 0.15  # stage settle time after each move (seconds)
_CLOSE_JOIN_TIMEOUT_S = 2.0
CellFeatureCallback = Callable[[int, dict, Tuple[float, float]], None]


class TilingController(ImConWidgetController):
    """Controls spiral tiling scans with live stitching and click-to-navigate."""

    sigOverviewUpdated = QtCore.Signal(object)   # np.ndarray
    sigProgressUpdated = QtCore.Signal(int, int)  # current, total
    sigRunningChanged = QtCore.Signal(bool)       # routes setRunning across threads
    sigShowCellMarkers = QtCore.Signal(object)    # (N, 2) row/col array
    sigHighlightCell = QtCore.Signal(int)
    sigCellTargetingEnabled = QtCore.Signal(bool)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._scanning = False
        self._stopRequested = False
        self._stitcher: Optional[StitchedImage] = None
        self._originXY: Optional[Tuple[float, float]] = None
        self._gridPositions: List[Tuple[int, int]] = []
        self._lastStepUm: float = 100.0
        self._segParams: dict = {}
        self._cellPositionsRC: Optional[np.ndarray] = None  # (N, 2) row/col in overview
        self._cellProps: Optional[dict[str, np.ndarray]] = None
        self._cellTargetingRunning = False
        self._cellTargetCancel = threading.Event()
        self._activityLock = threading.Lock()
        self._scanThread: Optional[threading.Thread] = None
        self._cellTargetThread: Optional[threading.Thread] = None
        self._scanAcqHandle = None
        self._closed = False

        tilingInfo = self._setupInfo.tiling
        if tilingInfo is None:
            return

        self._widget.setDefaultStep(tilingInfo.defaultTileStepUm)

        self._widget.sigStartTiling.connect(self.startTiling)
        self._widget.sigStopTiling.connect(self.stopTiling)
        self._widget.sigClickOnOverview.connect(self._navigateToPixel)
        self._widget.sigTuneSegmentation.connect(self._onTuneSegmentation)
        self._widget.sigRunCellTargeting.connect(self.detectCellTargets)
        self.sigOverviewUpdated.connect(self._widget.updateOverview)
        self.sigProgressUpdated.connect(self._widget.setProgress)
        self.sigRunningChanged.connect(self._widget.setRunning)
        self.sigShowCellMarkers.connect(self._widget.showCellMarkers)
        self.sigHighlightCell.connect(self._widget.highlightCurrentCell)
        self.sigCellTargetingEnabled.connect(self._widget.setCellTargetingEnabled)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @APIExport(runOnUIThread=True)
    def startTiling(self) -> None:
        tilingInfo = self._setupInfo.tiling
        if tilingInfo is None:
            return
        with self._getActivityLock():
            busyWithCells = self._cellTargetingRunning
            busy = (
                self._scanning
                or busyWithCells
                or getattr(self, '_closed', False)
            )
            if not busy:
                self._scanning = True
                self._stopRequested = False
        if busy:
            if busyWithCells:
                self._logger.warning(
                    'Tiling cannot start while cell targeting owns the stage'
                )
            return
        try:
            n_tiles = self._widget.getNTiles()
            step_um = self._widget.getTileStepUm()
            blend_overlaps = self._widget.getBlendOverlaps()
            intensity_correction = self._widget.getIntensityCorrection()

            self._stitcher = None
            self._originXY = None
            self._gridPositions = []
            self._lastStepUm = step_um

            self._widget.setRunning(True)
            self._widget.setProgress(0, n_tiles)
            self._widget.clearCellMarkers()
            self._widget.setCellTargetingEnabled(False)
            self._cellPositionsRC = None
            self._cellProps = None

            self._scanThread = threading.Thread(
                target=self._runScan,
                args=(
                    tilingInfo, n_tiles, step_um, blend_overlaps,
                    intensity_correction,
                ),
                daemon=True,
            )
            self._scanThread.start()
        except Exception as e:
            with self._getActivityLock():
                self._scanning = False
                self._scanThread = None
            try:
                self._widget.setRunning(False)
            except Exception:
                pass
            self._logger.error(
                f'Tiling worker could not start: {e}', exc_info=True
            )

    @APIExport()
    def stopTiling(self) -> None:
        self._stopRequested = True
        self._cellCancelEvent().set()

    def closeEvent(self) -> bool:
        """Request cancellation and wait briefly for owned workers.

        The acquisition handle remains owned by ``_runScan`` until that worker
        actually exits. This avoids disarming the camera underneath a blocked
        ``getLatestFrame`` call when a hardware backend is slow to cancel.
        """
        with self._getActivityLock():
            self._closed = True
            self._stopRequested = True
        self._cellCancelEvent().set()
        for name in ('_scanThread', '_cellTargetThread'):
            thread = getattr(self, name, None)
            if thread is None or thread is threading.current_thread():
                continue
            thread.join(_CLOSE_JOIN_TIMEOUT_S)
            if thread.is_alive():
                self._logger.warning(
                    f'{name[1:]} is still stopping after widget close; '
                    'owned hardware will be released when it exits.'
                )
        super().closeEvent()
        return self.shutdownComplete()

    @staticmethod
    def _threadIsAlive(thread) -> bool:
        return bool(thread is not None and thread.is_alive())

    def shutdownComplete(self) -> bool:
        """Return whether all stage/camera workers have left their finally."""
        with self._getActivityLock():
            return (
                not self._threadIsAlive(self.__dict__.get('_scanThread'))
                and not self._threadIsAlive(
                    self.__dict__.get('_cellTargetThread')
                )
                and self.__dict__.get('_scanAcqHandle') is None
                and not self.__dict__.get('_scanning', False)
                and not self.__dict__.get('_cellTargetingRunning', False)
            )

    def _getActivityLock(self):
        lock = self.__dict__.get('_activityLock')
        if lock is None:
            lock = threading.Lock()
            self._activityLock = lock
        return lock

    def _cellCancelEvent(self):
        event = self.__dict__.get('_cellTargetCancel')
        if event is None:
            event = threading.Event()
            self._cellTargetCancel = event
        return event

    @APIExport(runOnUIThread=True)
    def setTileLabel(self, label: str) -> None:
        self._widget.setLabel(label)

    @APIExport()
    def getStitchedImage(self) -> Optional[np.ndarray]:
        if self._stitcher is None:
            return None
        return self._stitcher.get_overview()

    # ------------------------------------------------------------------
    # Scan loop (runs in background thread)
    # ------------------------------------------------------------------

    def _runScan(
        self,
        tilingInfo,
        n_tiles: int,
        step_um: float,
        blend_overlaps: bool,
        intensity_correction: bool,
    ) -> None:
        acqHandle = None
        positioner = None
        x_axis = None
        y_axis = None
        origin_xy = None
        scan_completed = False
        try:
            positioner = self._master.positionersManager[tilingInfo.xyPositioner]
            axes = list(self._setupInfo.positioners[tilingInfo.xyPositioner].axes)
            x_axis, y_axis = axes[0], axes[1]

            camera = (tilingInfo.camera
                      if tilingInfo.camera
                      else self._defaultCamera())
            detector = self._master.detectorsManager[camera]

            # Tiling used to arm nothing at all and silently depend on live
            # view being on: with live view off, getLatestFrame() below
            # returned stale or empty frames. A WORKFLOW lease over the whole
            # loop arms the camera for the scan's duration and releases it in
            # the finally, whatever path the loop exits by.
            acqHandle = self._master.detectorsManager.acquire(
                [camera], LeasePurpose.WORKFLOW
            )
            with self._getActivityLock():
                self._scanAcqHandle = acqHandle

            # Give a newly armed camera one normal settle interval before the
            # first tile. Subsequent tiles already wait after each stage move;
            # previously only the first tile could be the pre-acquisition
            # cached frame when live view had been off.
            time.sleep(_SETTLE_S)
            if self._stopRequested:
                return

            # Record starting position as grid origin (0, 0)
            start_pos = positioner.position
            origin_xy = (start_pos[x_axis], start_pos[y_axis])
            self._originXY = origin_xy

            gx, gy = 0, 0
            for i, (dx, dy) in enumerate(spiral_moves(n_tiles)):
                if self._stopRequested:
                    break

                # Move to next tile (skip move for the very first position)
                if i > 0:
                    positioner.move(dx * step_um, x_axis)
                    positioner.move(dy * step_um, y_axis)
                    time.sleep(_SETTLE_S)
                    if self._stopRequested:
                        break

                gx += dx
                gy += dy

                frame = detector.getLatestFrameShared()

                # Lazy stitcher init after first frame (we need tile_size_px)
                if self._stitcher is None:
                    pixel_size_um = self._detectorPixelSizeUm(detector)
                    self._stitcher = StitchedImage(
                        tile_size_px=None,
                        tile_step_um=step_um,
                        px_per_um=None,
                        tile_shape_px=frame.shape[:2],
                        pixel_size_um=pixel_size_um,
                        blend_overlaps=blend_overlaps,
                        intensity_correction=intensity_correction,
                    )

                self._stitcher.add_tile(frame, gx, gy)
                self._gridPositions.append((gx, gy))

                overview = self._stitcher.get_overview()
                if not getattr(self, '_closed', False):
                    self.sigOverviewUpdated.emit(overview)
                    self.sigProgressUpdated.emit(i + 1, n_tiles)

            scan_completed = not self._stopRequested

        except Exception as e:
            self._logger.error(f'Tiling scan failed: {e}', exc_info=True)
        finally:
            # Once the origin is known, every exit path owns returning the
            # stage: normal completion, user cancellation, camera/stitching
            # failure, and close while the worker is active.
            origin_restored = origin_xy is not None
            if positioner is not None and origin_xy is not None:
                for value, axis in (
                    (origin_xy[0], x_axis),
                    (origin_xy[1], y_axis),
                ):
                    try:
                        positioner.setPosition(value, axis)
                    except Exception as e:
                        origin_restored = False
                        self._logger.error(
                            f'Failed to restore tiling origin on axis '
                            f'"{axis}": {e}',
                            exc_info=True,
                        )

            if (
                scan_completed
                and origin_restored
                and self._stitcher is not None
                and not getattr(self, '_closed', False)
            ):
                self.sigCellTargetingEnabled.emit(True)

            if acqHandle is not None:
                try:
                    self._master.detectorsManager.release(acqHandle)
                except Exception as e:
                    self._logger.error(
                        f'Failed to release tiling detector lease: {e}',
                        exc_info=True,
                    )
            with self._getActivityLock():
                self._scanAcqHandle = None
                self._scanning = False
                self._scanThread = None
            # Cross-thread emit — Qt's AutoConnection becomes QueuedConnection
            # because the sender (this background thread) lives in a different
            # thread than the receiver (widget on the GUI thread).  Previously
            # this used QMetaObject.invokeMethod which silently fails when the
            # target slot isn't registered with the Qt meta-object system
            # (setRunning is a plain Python method, not @Slot-decorated).
            if not getattr(self, '_closed', False):
                self.sigRunningChanged.emit(False)

    # ------------------------------------------------------------------
    # Click-to-navigate
    # ------------------------------------------------------------------

    def _navigateToPixel(self, row: int, col: int) -> None:
        # Reject clicks while the scan thread is still active.  The acquisition
        # loop exits as soon as the last tile is captured (line ~134), but the
        # thread then performs a blocking return-to-origin (lines ~137-138)
        # before reaching the ``finally`` block that clears ``_scanning`` and
        # restores button state.  A click during that window issues a second
        # ``setPosition`` on the same positioner from the UI thread; on most
        # backends that aborts the scan thread's pending hardware ACK and
        # leaves its ``setPosition`` call hung forever — ``finally`` never runs,
        # ``_scanning`` stays True, ``setRunning(False)`` never fires, Start
        # remains disabled and Stop becomes a no-op (the scan loop has already
        # exited, so flipping ``_stopRequested`` has nothing to react to).
        if self._scanning or self._cellTargetingRunning:
            self._logger.info(
                'Tiling click-to-navigate ignored: automated stage movement '
                'is still in progress. Wait for it to finish.'
            )
            return
        self._moveStageToPixel(row, col)

    def _canvasOrigin(self) -> Optional[Tuple[float, float]]:
        if self._stitcher is None or self._originXY is None or not self._gridPositions:
            return None
        min_gx = min(g[0] for g in self._gridPositions)
        min_gy = min(g[1] for g in self._gridPositions)
        return (
            self._originXY[0] + min_gx * self._lastStepUm
            - (self._stitcher.tile_shape_px[1] / 2) / self._stitcher.px_per_um_x,
            self._originXY[1] + min_gy * self._lastStepUm
            - (self._stitcher.tile_shape_px[0] / 2) / self._stitcher.px_per_um_y,
        )

    def _moveStageToPixel(self, row: int, col: int) -> Optional[Tuple[float, float]]:
        canvas_origin = self._canvasOrigin()
        if canvas_origin is None:
            return None
        tilingInfo = self._setupInfo.tiling
        if tilingInfo is None:
            return None
        stage_x, stage_y = self._stitcher.pixel_to_stage(row, col, canvas_origin)
        axes = list(self._setupInfo.positioners[tilingInfo.xyPositioner].axes)
        positioner = self._master.positionersManager[tilingInfo.xyPositioner]
        positioner.setPosition(stage_x, axes[0])
        positioner.setPosition(stage_y, axes[1])
        return stage_x, stage_y

    # ------------------------------------------------------------------
    # Cell targeting
    # ------------------------------------------------------------------

    def _onTuneSegmentation(self) -> None:
        if self._stitcher is None:
            return
        from imswitch.imcontrol.view.widgets.SegmentationParamsWidget import (
            SegmentationParamsWidget,
        )
        overview = self._stitcher.get_overview()
        pixel_size_um = 1.0 / self._stitcher.px_per_um_y
        dlg = SegmentationParamsWidget(
            image=np.asarray(overview, dtype=np.float32),
            pixel_size_um=pixel_size_um,
            initial_params=self._segParams or None,
            parent=self._widget,
        )
        dlg.sigParamsAccepted.connect(self._onSegParamsAccepted)
        dlg.exec_()

    def _onSegParamsAccepted(self, params: dict) -> None:
        self._segParams = params
        self._logger.info(f'Segmentation parameters updated: {params}')

    @APIExport()
    def detectCellTargets(self) -> np.ndarray:
        """Segment cells in the current overview and show target markers.

        This GUI-safe path does not move the stage. It only updates the cached
        target list and the overview marker overlay.
        """
        if self.__dict__.get('_scanning', False) or getattr(self, '_closed', False):
            self._logger.warning(
                'Cell targeting cannot start while tiling owns the stage'
            )
            return np.empty((0, 2))

        positions, props = self._detectCellTargets()
        self._cellPositionsRC = positions
        self._cellProps = props
        self.sigShowCellMarkers.emit(positions)
        return positions

    @APIExport()
    def runCellTargeting(
        self,
        feature_callback: Optional[CellFeatureCallback] = None,
        move_only: bool = False,
    ) -> None:
        """Move through detected cells and optionally run a per-cell workflow.

        Args:
            feature_callback: Optional ``f(idx, props_for_cell, stage_xy)``
                invoked after moving to each accepted cell.
            move_only: If True, move through targets even without a callback.

        If neither ``feature_callback`` nor ``move_only`` is supplied this
        method behaves like ``detectCellTargets()`` and does not move hardware.
        """
        if self.__dict__.get('_scanning', False) or getattr(self, '_closed', False):
            self._logger.warning(
                'Cell targeting cannot start while tiling owns the stage'
            )
            return
        positions, props = self._detectCellTargets()
        self._cellPositionsRC = positions
        self._cellProps = props
        self.sigShowCellMarkers.emit(positions)

        if len(positions) == 0:
            return
        if feature_callback is None and not move_only:
            self._logger.info(
                'Cell targets detected; no per-cell workflow requested, so stage iteration is skipped'
            )
            return
        self._cellCancelEvent().clear()
        with self._getActivityLock():
            busy = (
                self._cellTargetingRunning
                or self.__dict__.get('_scanning', False)
                or getattr(self, '_closed', False)
            )
            if not busy:
                self._cellTargetingRunning = True
        if busy:
            self._logger.warning('Cell targeting already running; ignoring duplicate request')
            return

        try:
            self._cellTargetThread = threading.Thread(
                target=self._iterateCells,
                args=(positions, props, feature_callback),
                daemon=True,
            )
            self._cellTargetThread.start()
        except Exception as e:
            with self._getActivityLock():
                self._cellTargetingRunning = False
                self._cellTargetThread = None
            self._logger.error(
                f'Cell-target worker could not start: {e}', exc_info=True
            )

    def _detectCellTargets(self) -> Tuple[np.ndarray, dict[str, np.ndarray]]:
        if self._stitcher is None:
            self._logger.warning('Cell targeting requested but no stitcher exists')
            return np.empty((0, 2)), {}

        from imswitch.imcontrol.model.workflows.segmentation import detect_cell_targets
        overview = np.asarray(self._stitcher.get_overview(), dtype=np.float32)
        pixel_size_um = 1.0 / self._stitcher.px_per_um_y

        params = self._segParams
        targets = detect_cell_targets(overview, pixel_size_um, params)
        if not targets.props:
            self._logger.info('No cells found in overview')
            return np.empty((0, 2)), {}

        self._logger.info(
            f'Cell targeting: {targets.n_valid} / {targets.n_total} cells pass filters'
        )

        if targets.n_valid == 0:
            return np.empty((0, 2)), targets.props

        return targets.positions, targets.filtered_props

    def _iterateCells(
        self,
        positions: np.ndarray,
        props: dict[str, np.ndarray],
        feature_callback: Optional[CellFeatureCallback],
    ) -> None:
        cancel = self._cellCancelEvent()
        try:
            for i, (row, col) in enumerate(positions):
                if cancel.is_set() or getattr(self, '_closed', False):
                    break
                self.sigHighlightCell.emit(i)
                stage_xy = self._moveStageToPixel(int(row), int(col))
                if stage_xy is None:
                    self._logger.warning(f'Cell {i}: stage coordinate conversion failed')
                    continue
                if cancel.wait(_SETTLE_S) or getattr(self, '_closed', False):
                    break
                if feature_callback is not None:
                    try:
                        cell_props = {key: value[i] for key, value in props.items()}
                        feature_callback(i, cell_props, stage_xy)
                    except Exception as exc:
                        self._logger.warning(f'Cell {i}: feature_callback failed: {exc}')
        finally:
            with self._getActivityLock():
                self._cellTargetingRunning = False
                self._cellTargetThread = None
            if not getattr(self, '_closed', False):
                self.sigHighlightCell.emit(-1)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _defaultCamera(self) -> str:
        for name, info in self._setupInfo.detectors.items():
            if info.forAcquisition:
                return name
        return next(iter(self._setupInfo.detectors))

    def _detectorPixelSizeUm(self, detector) -> Tuple[float, float]:
        """Return detector pixel size as ``(y, x)`` in micrometres."""
        pixel_size_um = detector.pixelSizeUm
        if len(pixel_size_um) >= 3:
            return float(pixel_size_um[-2]), float(pixel_size_um[-1])
        if len(pixel_size_um) == 2:
            return float(pixel_size_um[0]), float(pixel_size_um[1])
        if len(pixel_size_um) == 1:
            return float(pixel_size_um[0]), float(pixel_size_um[0])
        raise ValueError(f'Detector {detector.name} has invalid pixelSizeUm: {pixel_size_um}')


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
