import copy
import os
from pathlib import Path

import numpy as np
import tifffile as tiff

import imswitch.improcess.view.guitools as guitools
from imswitch.imcommon.controller import PickDatasetsController
from imswitch.improcess.model import DataObj, ReconObj
# NOTE: PatternFinder and SignalExtractor live in the MoNaLISA plugin.
# The controller still uses them directly during the Phase B.1 transition;
# Phase B.2 will replace the direct calls with registry dispatch.
from imswitch.improcess.reconstructors.monalisa.pattern_finder import PatternFinder
from imswitch.improcess.reconstructors.monalisa.signal_extractor import SignalExtractor
from .DataFrameController import DataFrameController
from .MultiDataFrameController import MultiDataFrameController
from .WatcherFrameController import WatcherFrameController
from .ReconstructionViewController import ReconstructionViewController
from .GraphController import GraphController
from .ScanParamsController import ScanParamsController
from .basecontrollers import ImProcessWidgetController


class ImProcessMainViewController(ImProcessWidgetController):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._commChannel.extension = self._widget.extension
        
        self.dataFrameController = self._factory.createController(
            DataFrameController, self._widget.dataFrame
        )
        self.multiDataFrameController = self._factory.createController(
            MultiDataFrameController, self._widget.multiDataFrame
        )
        self.watcherFrameController = self._factory.createController(
            WatcherFrameController, self._widget.watcherFrame
        )
        self.reconstructionController = self._factory.createController(
            ReconstructionViewController, self._widget.reconstructionWidget
        )
        self.graphController = None
        if self._widget.graphWidget is not None:
            self.graphController = self._factory.createController(
                GraphController, self._widget.graphWidget
            )
        self.scanParamsController = self._factory.createController(
            ScanParamsController, self._widget.scanParamsDialog
        )
        self.pickDatasetsController = self._factory.createController(
            PickDatasetsController, self._widget.pickDatasetsDialog
        )

        # SignalExtractor is MoNaLISA-only and Windows-only (CUDA DLL). Defer
        # construction until the user actually triggers a MoNaLISA reconstruction;
        # otherwise the module fails to launch on macOS/Linux even when the user
        # only wants view-only / drag-and-drop.
        self._signalExtractor = None
        self._patternFinder = PatternFinder()

        self._activeReconstructor = self._select_reconstructor()
        if self._activeReconstructor is not None:
            self._install_reconstructor_params(self._activeReconstructor)

        self._currentDataObj = None
        self._pattern = self._widget.getPatternParams()
        self._settingPatternParams = False
        self._scanParDict = {
            'dimensions': [self._widget.u_d_text, self._widget.r_l_text, self._widget.b_f_text,
                           self._widget.timepoints_text],
            'directions': [self._widget.p_text, self._widget.p_text, self._widget.p_text],
            'steps': ['35', '35', '1', '1'],
            'step_sizes': ['35', '35', '35', '1'],
            'unidirectional': True
        }
        self._dataFolder = None
        self._saveFolder = None

        self._commChannel.sigDataFolderChanged.connect(self.dataFolderChanged)
        self._commChannel.sigSaveFolderChanged.connect(self.saveFolderChanged)
        self._commChannel.sigCurrentDataChanged.connect(self.currentDataChanged)
        self._commChannel.sigScanParamsUpdated.connect(self.scanParamsUpdated)
        self._commChannel.sigReconstruct.connect(self.reconstruct)


        self._widget.sigSaveReconstruction.connect(lambda: self.saveCurrent('reconstruction'))
        self._widget.sigSaveReconstructionAll.connect(lambda: self.saveAll('reconstruction'))
        self._widget.sigSaveCoeffs.connect(lambda: self.saveCurrent('coefficients'))
        self._widget.sigSaveCoeffsAll.connect(lambda: self.saveAll('coefficients'))
        self._widget.sigSetDataFolder.connect(self.setDataFolder)
        self._widget.sigSetSaveFolder.connect(self.setSaveFolder)

        self._widget.sigReconstuctCurrent.connect(self.reconstructCurrent)
        self._widget.sigReconstructMultiConsolidated.connect(
            lambda: self.reconstructMulti(consolidate=True)
        )
        self._widget.sigReconstructMultiIndividual.connect(
            lambda: self.reconstructMulti(consolidate=False)
        )
        self._widget.sigQuickLoadData.connect(self.quickLoadData)
        self._widget.sigUpdate.connect(lambda: self.updateScanParams(applyOnCurrentRecon=True))

        self._widget.sigShowPatternChanged.connect(self.togglePattern)
        self._widget.sigFindPattern.connect(self.findPattern)
        self._widget.sigShowScanParamsClicked.connect(self.showScanParamsDialog)
        self._widget.sigPatternParamsChanged.connect(self.updatePattern)
        self._widget.sigFilesDropped.connect(self.handleDroppedFiles)
        self.updatePattern()
        self.updateScanParams()

    def _select_reconstructor(self):
        from imswitch.improcess.reconstructors.registry import get_registry

        reconstructors = get_registry().reconstructors()
        if not reconstructors:
            self._logger.warning("No ImProcess reconstructors registered")
            return None
        reconstructor = reconstructors[0]
        self._logger.info(
            f"Using active reconstructor: {reconstructor.id} ({reconstructor.name})"
        )
        return reconstructor

    def _install_reconstructor_params(self, reconstructor):
        # Always reflect the active reconstructor in the Parameters dock so
        # the user can tell at a glance which plugin's parameters they are
        # editing — even for plugins that keep the legacy parameter tree.
        try:
            self._widget.setActiveReconstructorName(reconstructor.name)
        except Exception:
            pass

        # Gate the modality-specific Actions buttons:
        # - 'Reconstruct current' is ceremonial for pass-through plugins
        #   (process() is a no-op wrap), so hide it; currentDataChanged
        #   auto-routes the data to the viewer in that case.
        # - 'Update reconstruction' re-applies MoNaLISA scan parameters and
        #   only makes sense for the MoNaLISA plugin.
        try:
            self._widget.setReconstructionActionsVisible(
                reconstruct_current=not getattr(reconstructor, 'is_pass_through', False),
                update_reconstruction=(reconstructor.id == 'monalisa'),
            )
        except Exception:
            pass

        if reconstructor.id == "monalisa":
            # Keep the legacy MoNaLISA parameter tree until the whole
            # scan-params/find-pattern path is migrated to plugin widgets.
            return
        widget = reconstructor.make_param_widget(self._widget)
        self._widget.setParameterWidget(widget)
    
    def dataFolderChanged(self, dataFolder):
        self._dataFolder = dataFolder

    def saveFolderChanged(self, saveFolder):
        self._saveFolder = saveFolder

    def setDataFolder(self):
        dataFolder = guitools.askForFolderPath(self._widget)
        if dataFolder:
            self._commChannel.sigDataFolderChanged.emit(dataFolder)

    def setSaveFolder(self):
        saveFolder = guitools.askForFolderPath(self._widget)
        if saveFolder:
            self._commChannel.sigSaveFolderChanged.emit(saveFolder)

    def findPattern(self):
        self._logger.debug('Find pattern clicked')
        if self._currentDataObj is None:
            return

        meanData = self._currentDataObj.getMeanData()
        if len(meanData) < 1:
            return

        self._logger.debug('Finding pattern')
        pattern = self._patternFinder.findPattern(meanData)
        self._logger.debug(f'Pattern found as: {self._pattern}')
        self.setPatternParams(pattern)
        self.updatePattern()

    def togglePattern(self, enabled):
        self._logger.debug('Toggling pattern')
        self._commChannel.sigPatternVisibilityChanged.emit(enabled)

    def updatePattern(self):
        if self._settingPatternParams:
            return

        self._logger.debug('Updating pattern')
        self._pattern = self._widget.getPatternParams()
        self._commChannel.sigPatternUpdated.emit(self._pattern)

    def setPatternParams(self, pattern):
        try:
            self._settingPatternParams = True
            self._widget.setPatternParams(*pattern)
        finally:
            self._settingPatternParams = False

    def updateScanParams(self, applyOnCurrentRecon=False):
        self._commChannel.sigScanParamsUpdated.emit(copy.deepcopy(self._scanParDict),
                                                    applyOnCurrentRecon)

    def scanParamsUpdated(self, scanParDict):
        self._scanParDict = scanParDict

    def showScanParamsDialog(self):
        self.updateScanParams()
        self._widget.showScanParamsDialog()

    def quickLoadData(self):
        extension = self._widget.extension.value() if self._widget.extension is not None else 'hdf5'
        if extension == 'zarr':
            dataPath = guitools.askForFolderPath(self._widget, defaultFolder=self._dataFolder)
        elif extension == 'hdf5':
            dataPath = guitools.askForFilePath(self._widget, defaultFolder=self._dataFolder)
        else:
            dataPath = guitools.askForFilePath(self._widget, defaultFolder=self._dataFolder)

        if dataPath:
            self._logger.debug(f'Loading data at: {dataPath}')
            self._loadFromPath(dataPath, prefer_as_current=True)

    def currentDataChanged(self, dataObj):
        self._currentDataObj = dataObj
        if hasattr(self._widget.parTree, "load_from_attrs"):
            try:
                self._widget.parTree.load_from_attrs(dataObj.attrs or {})
            except Exception as exc:
                self._logger.warning(f"Could not load reconstructor params from metadata: {exc}")

        # Update scan params based on new data
        # TODO: What if the attribute names change in imcontrol?
        dimensionMap = {
            b'X': self._widget.r_l_text,
            b'Y': self._widget.u_d_text,
            b'Z': self._widget.b_f_text
        }
        try:
            targetsAttr = dataObj.attrs['ScanStage:target_device']
            for i in range(0, min(3, len(targetsAttr))):
                self._scanParDict['dimensions'][i] = dimensionMap[targetsAttr[i]]
        except KeyError:
            pass

        try:
            positiveDirectionAttr = dataObj.attrs['ScanStage:positive_direction']
            for i in range(0, min(3, len(positiveDirectionAttr))):
                self._scanParDict['directions'][i] = (
                    self._widget.p_text if positiveDirectionAttr[i]
                    else self._widget.n_text
                )
        except KeyError:
            pass

        for i in range(0, 2):
            self._scanParDict['steps'][i] = str(int(np.sqrt(dataObj.numFrames)))

        try:
            stepSizesAttr = dataObj.attrs['ScanStage:axis_step_size']
        except KeyError:
            pass
        else:
            for i in range(0, min(4, len(stepSizesAttr))):
                self._scanParDict['step_sizes'][i] = str(stepSizesAttr[i] * 1000)  # convert um->nm

        self.updateScanParams()

        # Pass-through reconstructors don't require an explicit click — the
        # data is the result. Route to the viewer the moment a current
        # DataObj is set, so 'view-only' and similar plugins display files
        # in a single user action instead of three.
        if (
            self._activeReconstructor is not None
            and getattr(self._activeReconstructor, 'is_pass_through', False)
            and dataObj is not None
        ):
            try:
                self.reconstruct([dataObj], consolidate=False)
            except Exception as exc:
                self._logger.warning(
                    f"Pass-through auto-route failed for {self._activeReconstructor.id}: {exc}"
                )

    def extractData(self, data):
        fwhmNm = self._widget.getFwhmNm()
        bgModelling = self._widget.getBgModelling()
        if bgModelling == 'Constant':
            fwhmNm = np.append(fwhmNm, 9999)  # Code for constant bg
        elif bgModelling == 'No background':
            fwhmNm = np.append(fwhmNm, 0)  # Code for zero bg
        elif bgModelling == 'Gaussian':
            self._logger.debug('In Gaussian version')
            fwhmNm = np.append(fwhmNm, self._widget.getBgGaussianSize())
            self._logger.debug('Appended to sigmas')
        else:
            raise ValueError(f'Invalid BG modelling "{bgModelling}" specified; must be either'
                             f' "Constant", "Gaussian" or "No background".')

        sigmas = np.divide(fwhmNm, 2.355 * self._widget.getPixelSizeNm())

        device = self._widget.getComputeDevice()
        pattern = self._pattern
        if device == 'CPU' or device == 'GPU':
            if self._signalExtractor is None:
                self._signalExtractor = SignalExtractor()
            coeffs = self._signalExtractor.extractSignal(data, sigmas, pattern, device.lower())
        else:
            raise ValueError(f'Invalid device "{device}" specified; must be either "CPU" or "GPU"')

        return coeffs

    def reconstructCurrent(self):
        if self._currentDataObj is None:
            return

        self.reconstruct([self._currentDataObj], consolidate=False)

    def reconstructMulti(self, consolidate):
        self.reconstruct(self._widget.getMultiDatas(), consolidate)

    def reconstruct(self, dataObjs, consolidate):
        if self._activeReconstructor is not None and self._activeReconstructor.id != "monalisa":
            self._reconstruct_with_plugin(dataObjs, consolidate)
            return

        reconObj = None
        for index, dataObj in enumerate(dataObjs):
            preloaded = dataObj.dataLoaded
            try:
                dataObj.checkAndLoadData()

                if np.prod(np.array(self._scanParDict['steps'], dtype=int)) < dataObj.numFrames:
                    self._logger.error('Too many frames in data')
                    return

                if not consolidate or index == 0:
                    reconObj = ReconObj(dataObj.name,
                                        self._scanParDict,
                                        self._widget.r_l_text,
                                        self._widget.u_d_text,
                                        self._widget.b_f_text,
                                        self._widget.timepoints_text,
                                        self._widget.p_text,
                                        self._widget.n_text)

                data = dataObj.data
                if self._widget.bleachBool.value():
                    data = self.bleachingCorrection(data)

                coeffs = self.extractData(data)
            finally:
                if not preloaded:
                    dataObj.checkAndUnloadData()

            reconObj.addCoeffsTP(coeffs)
            if not consolidate:
                reconObj.updateImages()
                self._commChannel.sigResultProduced.emit(reconObj, reconObj.name)

        if consolidate and reconObj is not None:
            reconObj.updateImages()
            self._commChannel.sigResultProduced.emit(reconObj, f'{reconObj.name}_multi')
            self._commChannel.sigExecutionFinished.emit(self.reconstructionController.getImage())

    def _reconstruct_with_plugin(self, dataObjs, consolidate):
        if self._activeReconstructor is None:
            return
        if consolidate:
            self._logger.warning(
                f"{self._activeReconstructor.name} does not support consolidated "
                "multi-data reconstruction yet; processing items individually."
            )
        for dataObj in dataObjs:
            params = self._widget.getReconstructionParams()
            self._logger.info(
                f"Running {self._activeReconstructor.id} reconstruction for {dataObj.name}"
            )
            result = self._activeReconstructor.process(dataObj, params)
            self._commChannel.sigResultProduced.emit(result, result.name)
            self._commChannel.sigCurrentResultChanged.emit(result)

    def bleachingCorrection(self, data):
        correctedData = data.copy()
        energy = np.sum(data, axis=(1, 2))
        for i in range(data.shape[0]):
            c = (energy[0] / energy[i]) ** 4
            correctedData[i, :, :] = data[i, :, :] * c
        return correctedData

    def saveCurrent(self, dataType):
        """ Saves the reconstructed image or coefficeints from the current
        ReconObj to a user-specified destination. """

        filePath = guitools.askForFilePath(self._widget,
                                           caption=f'Save {dataType}',
                                           defaultFolder=self._saveFolder or self._dataFolder,
                                           nameFilter='*.tiff', isSaving=True)

        if filePath:
            reconObj = self.reconstructionController.getActiveReconObj()
            if dataType == 'reconstruction':
                self.saveReconstruction(reconObj, filePath)
            elif dataType == 'coefficients':
                if hasattr(reconObj, "data") and hasattr(reconObj, "save"):
                    self._logger.error("Coefficient export is only available for legacy MoNaLISA results")
                    return
                self.saveCoefficients(reconObj, filePath)
            else:
                raise ValueError(f'Invalid save data type "{dataType}"')

    def saveAll(self, dataType):
        """ Saves the reconstructed image or coefficeints from all available
        ReconObj objects to a user-specified directory. """

        dirPath = guitools.askForFolderPath(self._widget,
                                            caption=f'Save all {dataType}',
                                            defaultFolder=self._saveFolder or self._dataFolder)

        if dirPath:
            for name, reconObj in self.reconstructionController.getAllReconObjs():
                # Avoid overwriting
                filePath = os.path.join(dirPath, f'{name}.{dataType}.tiff')
                filePathNew = filePath
                numExisting = 0
                while os.path.exists(filePathNew):
                    numExisting += 1
                    pathWithoutExt, pathExt = os.path.splitext(filePath)
                    filePathNew = f'{pathWithoutExt}_{numExisting}{pathExt}'
                filePath = filePathNew

                # Save
                if dataType == 'reconstruction':
                    self.saveReconstruction(reconObj, filePath)
                elif dataType == 'coefficients':
                    if hasattr(reconObj, "data") and hasattr(reconObj, "save"):
                        self._logger.error(
                            f"Skipping coefficient export for {name}; not a legacy MoNaLISA result"
                        )
                        continue
                    self.saveCoefficients(reconObj, filePath)
                else:
                    raise ValueError(f'Invalid save data type "{dataType}"')

    def saveReconstruction(self, reconObj, filePath):
        if hasattr(reconObj, "data") and hasattr(reconObj, "save"):
            suffix = Path(filePath).suffix.lower().lstrip(".") or "tiff"
            fmt = "tiff" if suffix in ("tif", "tiff") else suffix
            reconObj.save(Path(filePath), fmt)
            return

        scanParDict = reconObj.getScanParams()
        vxsizec = int(float(
            scanParDict['step_sizes'][scanParDict['dimensions'].index(
                self._widget.r_l_text
            )]
        ))
        vxsizer = int(float(
            scanParDict['step_sizes'][scanParDict['dimensions'].index(
                self._widget.u_d_text
            )]
        ))
        vxsizez = int(float(
            reconObj.scanParDict['step_sizes'][scanParDict['dimensions'].index(
                self._widget.b_f_text
            )]
        ))
        dt = int(float(
            scanParDict['step_sizes'][scanParDict['dimensions'].index(
                self._widget.timepoints_text
            )]
        ))

        self._logger.debug(f'Trying to save to: {filePath}, Vx size: {vxsizec, vxsizer, vxsizez},'
                           f' dt: {dt}')
        # Reconstructed image
        reconstrData = copy.deepcopy(reconObj.getReconstruction())
        reconstrData = reconstrData[:, 0, :, :, :, :]
        reconstrData = np.swapaxes(reconstrData, 1, 2)
        tiff.imwrite(filePath, reconstrData,
                     imagej=True, resolution=(1 / vxsizec, 1 / vxsizer),
                     metadata={'spacing': vxsizez, 'unit': 'nm', 'axes': 'TZCYX'})

    def saveCoefficients(self, reconObj, filePath):
        coeffs = copy.deepcopy(reconObj.getCoeffs())
        self._logger.debug(f'Shape of coeffs: {coeffs.shape}')
        coeffs = np.swapaxes(coeffs, 1, 2)
        tiff.imwrite(filePath, coeffs,
                     imagej=True, resolution=(1, 1),
                     metadata={'spacing': 1, 'unit': 'px', 'axes': 'TZCYX'})

    def handleDroppedFiles(self, paths):
        """Process files dropped onto the main view via drag-and-drop.

        Per dropped path, routes through ``_loadFromPath``. When exactly one
        file is dropped and the active reconstructor is pass-through, that
        single path is promoted to the current DataObj so the auto-route in
        ``currentDataChanged`` puts it straight onto the napari viewer.
        """
        prefer_as_current = (
            len(paths) == 1
            and self._activeReconstructor is not None
            and getattr(self._activeReconstructor, 'is_pass_through', False)
        )
        any_routed_to_current = False
        any_routed_to_multidata = False

        for path in paths:
            outcome = self._loadFromPath(str(path), prefer_as_current=prefer_as_current)
            if outcome == 'current':
                any_routed_to_current = True
            elif outcome == 'multidata':
                any_routed_to_multidata = True

        # Single pass-through drop already raised the Current data dock via
        # _loadAsCurrent. Otherwise, surface the MultiData dock if anything
        # landed there.
        if not any_routed_to_current and any_routed_to_multidata:
            self._widget.raiseMultiDataDock()

    def _loadFromPath(self, dataPath, *, prefer_as_current: bool = False) -> str:
        """Unified loader for one file path.

        Handles dataset enumeration, the multi-dataset picker dialog and the
        routing decision in one place. Reused by ``quickLoadData`` and
        ``handleDroppedFiles`` so all entry points share the same pick UX.

        Args:
            dataPath: Absolute path to the file or zarr group.
            prefer_as_current: When True, a single (or single-picked) dataset
                is promoted to the current DataObj. When False, every dataset
                is added to the multi-data list.

        Returns:
            ``'current'``     — routed to the current DataObj (and raised),
            ``'multidata'``   — routed only into the multi-data list,
            ``'cancelled'``   — user dismissed the picker dialog,
            ``'empty'``       — no datasets in the file or none selected.
        """
        try:
            datasetsInFile = DataObj.getDatasetNames(dataPath)
        except Exception as exc:
            self._logger.error(f"Could not read datasets from {dataPath}: {exc}")
            return 'empty'

        if not datasetsInFile:
            return 'empty'

        name = os.path.basename(dataPath) or dataPath
        datasetsToRoute = list(datasetsInFile)

        if len(datasetsInFile) > 1:
            self.pickDatasetsController.setDatasets(dataPath, datasetsInFile)
            if not self._widget.showPickDatasetsDialog(blocking=True):
                return 'cancelled'
            datasetsToRoute = list(self.pickDatasetsController.getSelectedDatasets())
            if not datasetsToRoute:
                return 'empty'

        if prefer_as_current and len(datasetsToRoute) == 1:
            try:
                self._loadAsCurrent(name, datasetsToRoute[0], dataPath)
                return 'current'
            except Exception as exc:
                self._logger.error(
                    f"Could not promote {name}::{datasetsToRoute[0]} to current: {exc}"
                )
                # Fall through to multidata routing so the data isn't silently
                # dropped on the floor.

        for datasetName in datasetsToRoute:
            self.multiDataFrameController.makeAndAddDataObj(
                name, datasetName, path=dataPath
            )
        return 'multidata'

    def _loadAsCurrent(self, name, datasetName, dataPath):
        """Promote a dataset to the current DataObj and emit sigCurrentDataChanged.

        Extracted so ``_loadFromPath`` (used by drag-drop and quickLoadData)
        shares the routing with any future single-dataset entry points.
        """
        if self._currentDataObj is not None:
            self._currentDataObj.checkAndUnloadData()
        self._currentDataObj = DataObj(name, datasetName, path=dataPath)
        self._currentDataObj.checkAndLoadData()
        if self._currentDataObj.dataLoaded:
            self._commChannel.sigCurrentDataChanged.emit(self._currentDataObj)
            self._widget.raiseCurrentDataDock()


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
