import time
import os
import numpy as np
from collections.abc import Callable, Sequence
from threading import Lock

from imswitch.imcommon.framework import Thread, Worker, Signal
from ..basecontrollers import ImConWidgetController
from tifffile import imwrite, imread
from imswitch.imcontrol.view import guitools
from imswitch.imcontrol.model import getWidgetStatePersistence
from imswitch.imcontrol.model.bead_recognition import (
    analyze_donut,
    BeadAcquisitionConfig,
    BeadAnalysisParameters,
    BeadRecResultRecord,
    BeadWorkerUpdate,
    ReconstructionUpdate,
    append_roi_means,
    create_reconstruction_buffer,
    find_bead_center,
    fit_bead,
    normalize_roi_bounds,
    reconstruction_image,
    rescale_reconstruction_to_pixel_size,
)

class BeadRecController(ImConWidgetController):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.recIm = None
        self.imDisplay = None
        self.running = False
        self.roiAdded = False
        self.newScan = False
        self.parametersChanged = False
        self.dims = None
        self.stepSizes = None
        self.framesPerPixel = 1
        self.lastDir = None
        self.resultRecords = []
        self.listRecs = []
        self.ongoingScan = False
        self.currentRunImgs = {}
        # Set once we've warned that no generic 'Scan' widget is present
        # (e.g. TriggerScope setups), so we don't spam the log every scan.
        self._warnedNoScanWidget = False

        self.beadWorker = BeadWorker(
            isScanRunning=self._commChannel.isScanRunning,
            getFrames=self._getCurrentDetectorChunk,
            getRoiBounds=self._getBeadRoiBounds,
        )
        self.beadWorker.sigNewChunk.connect(self.update)
        self.beadWorker.sigWarning.connect(self._logger.warning)
        self.beadWorker.sigWarning.connect(self._widget.setStatusText)
        self.beadWorker.sigProgress.connect(self._updateProgress)
        self.thread = Thread()
        self.beadWorker.moveToThread(self.thread)
        self.thread.started.connect(self.beadWorker.run)

        self.yCenter = None
        self.xCenter = None
        self.showCenterState = False
        self.autoAxial=False

        # Connect BeadRecWidget signals
        self._widget.sigROIToggled.connect(self.roiToggled)
        self._widget.sigRunClicked.connect(self.run)
        self._widget.sigScaleClicked.connect(self.updateScaling)
        self._widget.saveRecBtn.clicked.connect(self.saveRec)
        self._widget.loadImgBtn.clicked.connect(self.loadImg)
        self._widget.sigFitRequested.connect(self.runFit)
        self._widget.sigAddCurrentRun.connect(self.addCurrentRun)
        self._widget.sigSelectionChanged.connect(self.selectionChanged)
        self._widget.sigRemoveRecFromList.connect(self.removeRecFromList)
        self._widget.sigClearList.connect(self.clearList)
        self._widget.sigSaveAll.connect(self.saveAll)
        self._widget.sigQueryMousePixelValue.connect(self.updateOnMousePixelValue)


        # Connect comm channel signals
        self._commChannel.sigScanStarted.connect(self.updateParameters)
        self._commChannel.sigScanStarted.connect(self.onNewScan)
        self._commChannel.sigScanStarted.connect(self.OngoingScanStatus)
        self._commChannel.sigScanEnded.connect(self.onEndedScan)
        self._commChannel.beadRecWorkflow.on_query_center_coord(self.centerCoordQuery)
        self._commChannel.beadRecWorkflow.on_update_bead_rec_center(self.updateCenterCross)
        self._commChannel.beadRecWorkflow.on_show_bead_rec_center_cross(self.showStateChanged)
        self._commChannel.beadRecWorkflow.on_auto_axial_toggled(self.onAutoAxialToggled)
        self._commChannel.beadRecWorkflow.on_new_axial_list_buffer(self.onNewAxialListBuffer)
        getWidgetStatePersistence().register('BeadRecController', self)
        

    def __del__(self) -> None:
        self.beadWorker.stop()
        self.thread.quit()
        self.thread.wait()
        if hasattr(super(), '__del__'):
            super().__del__()

    def _getCurrentDetectorChunk(self) -> Sequence[np.ndarray]:
        return self._master.detectorsManager.execOnCurrent(
            lambda c: c.getChunk()
        )

    def _getBeadRoiBounds(self) -> Sequence[int]:
        return self._widget.getROIGraphicsItem().bounds

    def _setResultRecords(self, records: Sequence[BeadRecResultRecord]) -> None:
        self.resultRecords = list(records)
        self.listRecs = [record.image for record in self.resultRecords]

    def _insertResultRecord(self, index: int, record: BeadRecResultRecord) -> None:
        self.resultRecords.insert(index, record)
        self.listRecs.insert(index, record.image)

    def _popResultRecord(self, index: int) -> None:
        self.resultRecords.pop(index)
        self.listRecs.pop(index)

    def _createAcquisitionConfig(self) -> BeadAcquisitionConfig:
        return BeadAcquisitionConfig.from_scan_dims(self.dims, frames_per_pixel=self.framesPerPixel)

    def _updateProgress(self, current: int, total: int) -> None:
        self._widget.updateProgress(current, total)

    def clearList(self):
        self._setResultRecords([])

    def selectionChanged(self,imgListIdx:int=None,currentRun=False,axialName=None):
        if currentRun:
            if axialName is None:
                axialName="XY"
            self.axialName = axialName
            self.imDisplay=self.currentRunImgs.get(axialName)
        else:
            self.axialName = None
            if imgListIdx is not None and imgListIdx<len(self.resultRecords):
                self.imDisplay=self.resultRecords[imgListIdx].image
            else:
                return
        
        if self.imDisplay is not None:
            self.updateScaling() # will update scaling and send final image to be displayed to widget
        else: 
            print("Selection changed, but self.imDisplay = none. current run: ",currentRun,"axialName:",axialName)
        
    def removeRecFromList(self,idx:int=None):
        if idx is not None and idx<len(self.resultRecords):
            self._popResultRecord(idx)

    def addCurrentToWidgetList(self):
        axial=False
        axialName = None
        if self.autoAxial:
            if self._commChannel.getNextAxial() is not None:
                axialName = self._commChannel.getNextAxial()
                axial=True
        if not axial: # clean up currentRunImgs
            self.currentRunImgs={}

        self._widget.addCurrentRunToList(axial,axialName)
        self._widget.imageListWidget.setCurrentRow(0)


    def runFit(self, modelKey: str):
        if self.imDisplay is None:
            self._widget.setStatusText("No image to analyze")
            return
        
        if modelKey == "legacy_donut":
            result = analyze_donut(self.imDisplay, self._widget.analysisPrm)
            self._display_legacy_donut_result(result)
        else:
            from imswitch.imcontrol.model.bead_fits import FIT_MODELS
            try:
                result = fit_bead(self.imDisplay, modelKey, params=self._widget.analysisPrm)
            except ValueError as e:
                self._widget.setStatusText(f"Fit failed: {e}")
                return
            
            model = FIT_MODELS[modelKey]
            height, width = self.imDisplay.shape
            y_coords, x_coords = np.meshgrid(np.arange(height), np.arange(width), indexing='ij')
            param_values = [result.params[name] for name in model.param_names]
            fit_image = model.model((x_coords, y_coords), *param_values)
            residual = self.imDisplay - fit_image
            
            metrics = {**result.params, "r_squared": result.r_squared, **result.summary}
            self._widget.displayFitResult(
                title=f"{modelKey} fit",
                metrics=metrics,
                image=self.imDisplay,
                overlay_center=result.center_px,
                fit_image=fit_image,
                residual=residual
            )
            self._widget.setStatusText(f"Fit complete: R²={result.r_squared:.4f}")
    
    def _display_legacy_donut_result(self, result):
        if not result.accepted:
            metrics = {"status": "rejected", "reason": result.reason or "Unknown"}
            if result.coord is not None:
                metrics["center_y"] = result.coord[0]
                metrics["center_x"] = result.coord[1]
            self._widget.displayFitResult(
                title="Legacy donut analysis (rejected)",
                metrics=metrics,
                image=self.imDisplay,
                overlay_center=result.coord
            )
            self._widget.setStatusText(f"Donut rejected: {result.reason}")
            return
        
        metrics = {
            "min_value": result.min_value,
            "background": result.background,
            "background_std": result.background_std,
            "fill_x": result.fill_x,
            "fill_x_std": result.fill_x_std,
            "fill_y": result.fill_y,
            "fill_y_std": result.fill_y_std,
            "center_y": result.coord[0],
            "center_x": result.coord[1],
        }
        self._widget.displayFitResult(
            title="Legacy donut analysis",
            metrics=metrics,
            image=self.imDisplay,
            overlay_center=result.coord
        )
        self._widget.setStatusText("Donut analysis complete")


    def loadImg(self):
        """Asks users to load one or several images, loads them to the list of saved images and
        calls widget function to add names of files to the list panel"""
        
        paths = guitools.askForFilePath(self._widget, 'Choose one or several tiff image(s)',defaultFolder=self.lastDir,
                                       isSaving=False,nameFilter= "TIFF Files (*.tif *.tiff)",multiFiles=True)
        if paths is None:
            return
        if isinstance(paths,list):
            self.lastDir = os.path.dirname(paths[0])
        else:
            paths = [paths]
        
        for path in paths:
            im = imread(path).astype(np.float64)
            if len(im.shape)!=2:
                print("Loaded images should be 2d")
                return
            filename = os.path.splitext(os.path.basename(path))[0]
            itemName = self._widget.addToList(filename) # adds to list of items in widget
            self._insertResultRecord(
                0,
                BeadRecResultRecord(
                    name=itemName,
                    image=im,
                    source_path=path,
                    timestamp=time.time(),
                ),
            )
        # display last image loaded
        self.imDisplay = im
        self._widget.updateImage(self.imDisplay)
        self._widget.imageListWidget.setCurrentRow(self._widget.getInsertIndexAfterCurrent())

    def addCurrentRun(self,name=None):
        """ Save current run to list of saved images, calls widget to add it
        to list of items and to delete the "current run" item(s), if a scan is not running. 
        NOTE: insert to first position to keep same order as widget items."""
        
        for key, img in self.currentRunImgs.items():
            scaled = False
            if self._widget.scaleButton.isChecked():
                img = self.rescale(img)
                scaled = True
            axialName = key if self.autoAxial else None
            itemName = self._widget.addToList(name,axialName)
            self._insertResultRecord(
                0,
                BeadRecResultRecord(
                    name=itemName,
                    image=img,
                    axial_name=axialName,
                    timestamp=time.time(),
                    scaled=scaled,
                ),
            )
            if not self.ongoingScan:
                self._widget.removeCurrentRunItems()
                            

        # if not self.autoAxial and self.recIm is not None:
        #     self.update()
        #     self.listRecs.insert(0, self.imDisplay)
        #     self._widget.addToList(name)
        #     if not self.ongoingScan:
        #         self._widget.clearCurrentRunItem()
        # else:
        #     print("No current recon to add !")


    def saveRec(self):
        """ Saves currenlty display rec, so self.imDisplay. Suggests the filename if
        it can find name of selected row in the widget list panel"""
        if self.imDisplay is None:
            return

        #for filename suggestion
        if self._widget.isSelectedCurrent():
            suggested = self.lastDir
        else:
            idx = self._widget.imageListWidget.currentRow()
            if idx != -1:
                itemName = self._widget.imageListWidget.item(self._widget.imageListWidget.currentRow()).text()
                if self.lastDir is None:
                    suggested = itemName
                else:
                    suggested = os.path.join(self.lastDir,itemName)
            else:
                suggested = self.lastDir

        path = guitools.askForFilePath(self._widget, 'Save file as',defaultFolder=suggested,isSaving=True)
        if not path:
            return

        self.lastDir = os.path.dirname(path)
        if path.split('.')[-1] not in ['tif', 'tiff']:
            path = path + ".tiff"
        imwrite(path,self.imDisplay)
    
    def saveAll(self):
        """ Saves all images that are in self.listRecs, with file names from the list panel."""
        if not self.resultRecords:
            return
        caption = "Choose folder to save all images"
        folder = guitools.askForFolderPath(self._widget, caption=caption, defaultFolder=self.lastDir)
        if not folder:
            return
        self.lastDir = os.path.dirname(folder)

        name_offset = self._widget.getInsertIndexAfterCurrent()
            
        for idx,record in enumerate(self.resultRecords):
            item = self._widget.imageListWidget.item(idx + name_offset)
            name = item.text() + ".tif"
            path = os.path.join(folder, name)
            imwrite(path, record.image)

    def roiToggled(self, enabled):
        """ Show or hide ROI."""
        if enabled:
            self.addROI()

            ROIsize = (64, 64)
            ROIcenter = self._commChannel.getCenterViewbox()

            ROIpos = (ROIcenter[0] - 0.5 * ROIsize[0],
                      ROIcenter[1] - 0.5 * ROIsize[1])

            self._widget.showROI(ROIpos, ROIsize)
        else:
            self._widget.hideROI()

    def addROI(self):
        """ Adds the ROI to ImageWidget viewbox through the CommunicationChannel. """
        if not self.roiAdded:
            self._commChannel.sigAddItemToVb.emit(self._widget.getROIGraphicsItem())
            self.roiAdded = True

    def run(self):
        # if not self.running:
        if self._widget.runButton.isChecked():
            self.updateParameters()
            config = self._createAcquisitionConfig()
            self.running = True
            self._master.detectorsManager.execOnAll(lambda c: c.flushBuffers())
            self.beadWorker.start(config)
            self._widget.setStatusText("Bead reconstruction running")
            self._widget.updateProgress(0, config.total_pixels)
            self.thread.start()
            if self.ongoingScan:
                self.addCurrentToWidgetList()
        else:
            self.running = False
            self.beadWorker.stop()
            self._widget.setStatusText("Bead reconstruction stopped")
            self.thread.quit()
            self.thread.wait()

    def onNewScan(self):
        self.newScan = True
        if self.autoAxial:
            self.axialName = self._commChannel.getNextAxial()
        else:
            self.axialName = "XY"

        if self._widget.runButton.isChecked():
            self.beadWorker.configure(self._createAcquisitionConfig())
            self.addCurrentToWidgetList() # in case "clear all" made it disappear

    
    def OngoingScanStatus(self):
        self.ongoingScan = True

    def onEndedScan(self):
        self.ongoingScan=False
        if self.recIm is None:
            self._widget.setStatusText("Scan ended without bead reconstruction data")
            return
        self.currentRunImgs[self.axialName] = reconstruction_image(self.recIm, self.dims) # we always store unscaled img
        self._widget.setStatusText("Bead reconstruction scan complete")
        self._widget.updateProgress(self.recIm.size, self.recIm.size)
    
    def onAutoAxialToggled(self,state:bool = False):
        if state:
            self.autoAxial=True
        else:
            self.autoAxial=False
    
    def onNewAxialListBuffer(self, axialList:list):
        """ clean up self.currentRunImgs to not keep previous XZ/YZ and widget list """
        self.currentRunImgs={}
        self._widget.removeCurrentRunItems()

    def updateParameters(self):
        try:
            dims = np.array(self._commChannel.getDimsScan()).astype(int)
            stepSizes = np.array(self._commChannel.getScanStepSizes(), dtype=float)
        except RuntimeError:
            # No generic 'Scan' controller in this setup (e.g. a TriggerScope
            # scanner registers its own scan widget, not 'Scan'). Bead
            # reconstruction needs scan dimensions it cannot obtain here, so
            # skip instead of raising on every scan start. Warn only once.
            if not self._warnedNoScanWidget:
                self._logger.warning(
                    'Bead reconstruction inactive: no scan widget available to '
                    'provide scan dimensions (getDimsScan). Skipping parameter '
                    'update on scan start.'
                )
                self._warnedNoScanWidget = True
            return

        # Use explicit axes 0 and 1 (X, Y) from getDimsScan
        if dims[0] <= 0 or dims[1] <= 0:
            self._logger.warning(
                f'BeadRec: invalid scan dimensions ({dims[0]}, {dims[1]}). '
                f'Keeping previous dims.'
            )
            self.parametersChanged = False
            return

        prior_dims = self.dims
        prior_stepSizes = self.stepSizes
        prior_framesPerPixel = self.framesPerPixel

        self.dims = (int(dims[0]), int(dims[1]))
        self.stepSizes = (float(stepSizes[0]), float(stepSizes[1]))
        self.framesPerPixel = self._commChannel.getFramesPerScanPixel()
        
        if prior_dims is not None and prior_stepSizes is not None:
            if prior_dims != self.dims or prior_stepSizes != self.stepSizes or prior_framesPerPixel != self.framesPerPixel:
                self.parametersChanged = True
    
    def updateOnMousePixelValue(self,x,y):
        """ Updates the pixel value displayed in the widget """
        if self.imDisplay is not None:
            if 0 <= x < self.imDisplay.shape[1] and 0 <= y < self.imDisplay.shape[0]:
                val = self.imDisplay[round(y), round(x)]
                self._widget.updatePixelValue(x,y,val)
            else:
                self._widget.erasePixelValue()

    def updateScaling(self):
        """ Updates scaling factor of displayed image, only if current run
        Note that this will overwrite imDisplay with scaled version, but unscaled still accessble with currentRunImgs[self.axialName]"""
        if not self._commChannel.isScanRunning() and self._widget.isSelectedCurrent():
            if self._widget.scaleButton.isChecked():
                self.imDisplay = self.rescale(self.imDisplay)
            else:
                self.imDisplay = self.currentRunImgs.get(self.axialName)
        self._widget.updateImage(self.imDisplay)

    def rescale(self,im):
        """
        Rescale image to physical scan pixel size if x/y step sizes differ.
        """
        try:
            return rescale_reconstruction_to_pixel_size(im, self.stepSizes)
        except ValueError as exc:
            self._logger.warning("Could not rescale BeadRec image: %s", exc)
            return im
    
    def update(self, recIm=None):
        """"Updates image display with current recorded image self.recIm"""
        if isinstance(recIm, BeadWorkerUpdate):
            self._updateProgress(recIm.filled_pixels, recIm.total_pixels)
            recIm = recIm.buffer
        if recIm is not None:
            self.recIm = recIm
        if self.recIm is None:
            return
        self.imDisplay = reconstruction_image(self.recIm, self.dims)
        if self._widget.scaleButton.isChecked():
            self.imDisplay = self.rescale(self.imDisplay)
        self._widget.updateImage(self.imDisplay)


    def centerCoordQuery(self, mode):
        coord = None
        if self.imDisplay is not None:
            model_key = "gaussian2d" if mode == "Maxima" else "donut_r2_gaussian"
            try:
                result = fit_bead(self.imDisplay, model_key, params=self._widget.analysisPrm)
                coord = (round(result.center_px[0]), round(result.center_px[1]))
            except ValueError:
                result = find_bead_center(self.imDisplay, mode, self._widget.analysisPrm)
                coord = result.coord
        
        self._commChannel.beadRecWorkflow.finish_center_coord_pipeline(coord)
        if coord is not None and self.showCenterState:
            self._widget.displayCenterCoord(coord[0], coord[1])
        else:
            print(f"Center search with '{mode}' method failed. Try manual coordinate")

    def updateCenterCross(self,y,x):    
        self.yCenter = y
        self.xCenter = x
        self.updateCenterCrossWidget()
    
    def showStateChanged(self,state:bool):
        self.showCenterState = state
        self.updateCenterCrossWidget()

    def updateCenterCrossWidget(self):
        if self.showCenterState and self.imDisplay is not None and self.yCenter is not None and self.xCenter is not None:   
            self._widget.displayCenterCoord(self.yCenter,self.xCenter)
        else:
            self._widget.removeCenterCoord()

    def getWidgetState(self) -> dict[str, object]:
        """Return passive BeadRec UI state for persistence."""
        return {
            "analysis_parameters": BeadAnalysisParameters.from_mapping(
                self._widget.analysisPrm
            ).as_dict(),
            "scale_enabled": self._widget.scaleButton.isChecked(),
            "roi_visible": self._widget.roiButton.isChecked(),
            "last_dir": self.lastDir,
            "result_metadata": [
                record.metadata() for record in self.resultRecords
            ],
        }

    def setWidgetState(self, state: dict[str, object]) -> None:
        """Restore passive BeadRec UI state without starting reconstruction."""
        try:
            analysisParameters = state.get("analysis_parameters")
            if isinstance(analysisParameters, dict):
                self._widget.analysisPrm = BeadAnalysisParameters.from_mapping(
                    analysisParameters
                ).as_dict()

            self._widget.scaleButton.setChecked(bool(state.get("scale_enabled", False)))

            lastDir = state.get("last_dir")
            if isinstance(lastDir, str) and os.path.isdir(lastDir):
                self.lastDir = lastDir

            roiVisible = bool(state.get("roi_visible", False))
            signalsBlocked = self._widget.roiButton.blockSignals(True)
            self._widget.roiButton.setChecked(roiVisible)
            self._widget.roiButton.blockSignals(signalsBlocked)
            self.roiToggled(roiVisible)

            self._widget.setStatusText("BeadRec passive state restored")
        except Exception as exc:
            self._logger.warning("Failed to restore BeadRec widget state: %s", exc)

    def getStateSchemaVersion(self) -> int:
        """Return BeadRec widget-state schema version."""
        return 1
        

            

class BeadWorker(Worker):
    sigNewChunk = Signal(object)
    sigWarning = Signal(str)
    sigProgress = Signal(int, int)

    def __init__(
        self,
        isScanRunning: Callable[[], bool],
        getFrames: Callable[[], Sequence[np.ndarray]],
        getRoiBounds: Callable[[], Sequence[int]],
    ) -> None:
        super().__init__()
        self._isScanRunning = isScanRunning
        self._getFrames = getFrames
        self._getRoiBounds = getRoiBounds
        self._lock = Lock()
        self._running = False
        self._config = None
        self._recIm = None
        self._nextIndex = 0
        self._filledPixels = 0
        self._resetRequested = False
        self._lineStepPhase = 0

    def start(self, config: BeadAcquisitionConfig) -> None:
        self.configure(config)
        with self._lock:
            self._running = True

    def stop(self) -> None:
        with self._lock:
            self._running = False

    def configure(self, config: BeadAcquisitionConfig) -> None:
        with self._lock:
            self._config = config
            self._resetRequested = True
            self._lineStepPhase = 0

    def _isRunning(self) -> bool:
        with self._lock:
            return self._running

    def _getBufferAndIndex(
        self,
    ) -> tuple[BeadAcquisitionConfig | None, np.ndarray | None, int | None]:
        with self._lock:
            if self._config is None:
                return None, None, None
            if self._recIm is None or self._resetRequested:
                self._recIm = create_reconstruction_buffer(self._config.scan_dims)
                self._nextIndex = 0
                self._filledPixels = 0
                self._resetRequested = False
            return self._config, self._recIm, self._nextIndex
    
    def _storeUpdate(
        self,
        sourceBuffer: np.ndarray,
        update: ReconstructionUpdate,
    ) -> BeadWorkerUpdate | None:
        with self._lock:
            if self._recIm is not sourceBuffer:
                return None
            self._recIm = update.buffer
            self._nextIndex = update.next_index
            if update.wrapped:
                self._filledPixels = self._recIm.size
            else:
                self._filledPixels = min(self._filledPixels + update.frames_written, self._recIm.size)
            return BeadWorkerUpdate(
                buffer=self._recIm,
                filled_pixels=self._filledPixels,
                total_pixels=self._recIm.size,
                frames_written=update.frames_written,
                wrapped=update.wrapped,
            )

    def run(self) -> None:
        while self._isRunning():
            config, recIm, nextIndex = self._getBufferAndIndex()
            if config is None or recIm is None or nextIndex is None:
                time.sleep(0.0001)
                continue

            if self._isScanRunning():
                newImages = self._getFrames()
                n = len(newImages)
                if n > 0:
                    # With C = frames_per_pixel > 1 the camera fires in C
                    # linesteps per line, so frames arrive in per-line blocks
                    # of Nx per linestep: [line0 step0: Nx][line0 step1: Nx]...
                    # Keep only the first block (linestep) of each physical
                    # line: frames whose index modulo Nx*C falls in [0, Nx).
                    C = config.frames_per_pixel
                    if C > 1:
                        Nx = config.scan_dims[0]
                        period = Nx * C
                        keptFrames = [
                            newImages[i]
                            for i in range(n)
                            if (self._lineStepPhase + i) % period < Nx
                        ]
                        self._lineStepPhase = (self._lineStepPhase + n) % period
                    else:
                        keptFrames = newImages
                    
                    if len(keptFrames) > 0:
                        try:
                            roi = normalize_roi_bounds(self._getRoiBounds(), keptFrames[0].shape)
                            update = append_roi_means(
                                recIm,
                                nextIndex,
                                keptFrames,
                                roi,
                                wrap=config.wrap,
                            )
                        except ValueError as exc:
                            self.sigWarning.emit(f"Skipping BeadRec chunk: {exc}")
                            continue

                        workerUpdate = self._storeUpdate(recIm, update)
                        if workerUpdate is not None:
                            self.sigNewChunk.emit(workerUpdate)
                            self.sigProgress.emit(workerUpdate.filled_pixels, workerUpdate.total_pixels)

            time.sleep(config.poll_interval_s)  # Prevents freezing











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
