import copy
import os
from pathlib import Path

import numpy as np
import tifffile as tiff
from qtpy import QtWidgets

import imswitch.imcommon.view.guitools as guitools
from imswitch.imcommon.controller import PickDatasetsController
from imswitch.improcess.analysis.smlm_import import (
    read_generic_csv,
    read_localizations,
    sniff_localization_format,
)
from imswitch.improcess.model import result_io
from imswitch.improcess.model import DataObj
from imswitch.improcess.model.dataset_sources import (
    LOCALIZATIONS_SPEC,
    LOCATOR_DIRECTORY,
    TILING_MANIFEST_SPEC,
    file_dialog_filter,
    preferred_source_spec,
    resolve_dataset_source,
    specs_for_reconstructor,
)
from .MultiDataFrameController import MultiDataFrameController
from .basecontrollers import ImProcessWidgetController


class FileIOController(ImProcessWidgetController):
    """Owns all file-I/O and save logic: data-folder / save-folder state, the
    QuickLoad / drag-drop / picker flow, and the save-reconstruction /
    save-coefficients dialogs.

    Extracted from ``ImProcessMainViewController``; the coordinator passes
    itself as ``mainController`` so this controller can reach
    ``_currentDataObj`` and ``reconstructionController``. The multi-dataset
    controllers (multi-data frame and picker) are now owned here since they're
    only used by the load paths.
    """

    def __init__(self, *args, mainController=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._main = mainController
        self._dataFolder = None
        self._saveFolder = None

        # Multi-dataset controllers are only used by _loadFromPath
        self.multiDataFrameController = self._factory.createController(
            MultiDataFrameController, self._widget.multiDataFrame
        )
        self.pickDatasetsController = self._factory.createController(
            PickDatasetsController, self._widget.pickDatasetsDialog
        )

    def quickLoadData(self):
        dataPath = self._requestLoadPath()
        if dataPath:
            self._logger.debug(f'Loading data at: {dataPath}')
            self._loadFromPath(dataPath, prefer_as_current=True)

    def quickLoadVirtualData(self):
        dataPath = self._requestLoadPath(caption_prefix='Open virtual')
        if dataPath:
            self._logger.debug(f'Opening virtual data at: {dataPath}')
            self._loadFromPath(
                dataPath,
                prefer_as_current=True,
                virtual_current=True,
            )

    def _requestLoadPath(self, *, caption_prefix: str = 'Open'):
        specs = self._activeSourceSpecs()
        extensionParam = getattr(self._widget, 'extension', None)
        extension = extensionParam.value() if extensionParam is not None else None
        sourceSpec = preferred_source_spec(specs, extension)
        selectedSpecs = specs
        captionLabel = sourceSpec.label
        if extension is None and self._needsSourceFamilyChoice(specs):
            sourceSpec, selectedSpecs, captionLabel = self._requestSourceFamily(
                specs,
                caption_prefix=caption_prefix,
            )
            if sourceSpec is None:
                return None
        if sourceSpec.locator == LOCATOR_DIRECTORY:
            return guitools.askForFolderPath(
                self._widget,
                caption=f'{caption_prefix} {captionLabel}',
                defaultFolder=self._dataFolder,
            )
        return guitools.askForFilePath(
            self._widget,
            caption=f'{caption_prefix} {captionLabel}',
            defaultFolder=self._dataFolder,
            nameFilter=file_dialog_filter(selectedSpecs),
        )

    def _activeSourceSpecs(self):
        active = getattr(self._main, '_activeReconstructor', None)
        specs = specs_for_reconstructor(active) if active is not None else []
        return specs or None

    @staticmethod
    def _needsSourceFamilyChoice(specs) -> bool:
        if not specs:
            return False
        locators = {spec.locator for spec in specs}
        return LOCATOR_DIRECTORY in locators and len(locators) > 1

    def _requestSourceFamily(self, specs, *, caption_prefix: str):
        fileSpecs = [spec for spec in specs if spec.locator != LOCATOR_DIRECTORY]
        directorySpecs = [spec for spec in specs if spec.locator == LOCATOR_DIRECTORY]
        choices = []
        if fileSpecs:
            fileLabels = " / ".join(spec.label for spec in fileSpecs)
            choices.append((f'{fileLabels} file', fileSpecs[0], fileSpecs))
        for spec in directorySpecs:
            choices.append((f'{spec.label} folder', spec, [spec]))

        labels = [label for label, _sourceSpec, _selectedSpecs in choices]
        selected, accepted = QtWidgets.QInputDialog.getItem(
            self._widget,
            f'{caption_prefix} data source',
            'Source type',
            labels,
            0,
            False,
        )
        if not accepted:
            return None, [], None
        for label, sourceSpec, selectedSpecs in choices:
            if label == selected:
                return sourceSpec, selectedSpecs, label
        return None, [], None

    def handleDroppedFiles(self, paths):
        """Process files dropped onto the main view via drag-and-drop.

        Per dropped path, routes through ``_loadFromPath``. When exactly one
        file is dropped and the active reconstructor is pass-through, that
        single path is promoted to the current DataObj so the auto-route in
        ``currentDataChanged`` puts it straight onto the napari viewer.
        """
        prefer_as_current = (
            len(paths) == 1
            and self._main._activeReconstructor is not None
            and getattr(self._main._activeReconstructor, 'is_pass_through', False)
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

    def _loadFromPath(
        self,
        dataPath,
        *,
        prefer_as_current: bool = False,
        virtual_current: bool = False,
    ) -> str:
        """Unified loader for one file path.

        Handles dataset enumeration, the multi-dataset picker dialog and the
        routing decision in one place. Reused by ``quickLoadData`` and
        ``handleDroppedFiles`` so all entry points share the same pick UX.

        Args:
            dataPath: Absolute path to the file or zarr group.
            prefer_as_current: When True, a single (or single-picked) dataset
                is promoted to the current DataObj. When False, every dataset
                is added to the multi-data list.
            virtual_current: When promoting a single dataset, open only the
                lazy source handle instead of materializing the full data array.

        Returns:
            ``'current'``     — routed to the current DataObj (and raised),
            ``'multidata'``   — routed only into the multi-data list,
            ``'cancelled'``   — user dismissed the picker dialog,
            ``'empty'``       — no datasets in the file or none selected.
        """
        # Recognised by content, before the suffix-based resolver runs: a
        # Picasso .hdf5 and an image .hdf5 are the same name, and a
        # localization .csv is not something DataObj can open at all.
        localizationFormat = sniff_localization_format(dataPath)
        if localizationFormat == 'generic-csv':
            # An unrecognised table: only a localization file if the user says
            # so, and only readable once they name the columns. Offered rather
            # than assumed, and only where a table cannot be image data anyway.
            if self._localizationSpecActive():
                return self._loadMappedLocalizationsAsResult(dataPath)
        elif localizationFormat is not None:
            return self._loadLocalizationsAsResult(dataPath, localizationFormat)

        try:
            source = resolve_dataset_source(dataPath, allowed_specs=self._activeSourceSpecs())
            dataPath = str(source.path)
            if source.format_id == TILING_MANIFEST_SPEC.id:
                return self._loadMetadataAsCurrent(source)
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
                self._loadAsCurrent(
                    name,
                    datasetsToRoute[0],
                    dataPath,
                    virtual=virtual_current,
                )
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

    def _localizationSpecActive(self) -> bool:
        """Whether the active reconstructor offers localization tables."""
        specs = self._activeSourceSpecs() or []
        return any(spec.id == LOCALIZATIONS_SPEC.id for spec in specs)

    def _loadMappedLocalizationsAsResult(self, dataPath) -> str:
        """Ask what the columns mean, then read the table as a result."""
        from imswitch.improcess.view.LocalizationImportDialog import (
            LocalizationImportDialog,
        )

        try:
            kwargs = LocalizationImportDialog.get_import_kwargs(
                dataPath, parent=self._widget
            )
        except Exception as exc:
            self._logger.error(f"Could not inspect {dataPath}: {exc}")
            return 'empty'
        if kwargs is None:
            return 'cancelled'

        try:
            result = read_generic_csv(dataPath, **kwargs)
        except Exception as exc:
            self._logger.error(f"Could not read localizations from {dataPath}: {exc}")
            return 'empty'
        return self._publishLocalizations(result, dataPath, 'generic-csv')

    def _loadLocalizationsAsResult(self, dataPath, localizationFormat) -> str:
        """Open a coordinate table straight into the reconstruction list.

        A localization table is a *result*, not data to reconstruct, so it
        bypasses DataObj entirely and is published the same way a reconstructor
        publishes its output.
        """
        try:
            result = read_localizations(dataPath)
        except Exception as exc:
            self._logger.error(f"Could not read localizations from {dataPath}: {exc}")
            return 'empty'
        return self._publishLocalizations(result, dataPath, localizationFormat)

    def _publishLocalizations(self, result, dataPath, localizationFormat) -> str:
        """Hand a freshly read localization table to the reconstruction list."""
        if result.metadata.get('pixel_size_assumed'):
            # The coordinates are exact; only the preview bin floor and any
            # later pixel-native export depend on this. Say so rather than let
            # a guess be mistaken for a measurement.
            self._logger.warning(
                f"{os.path.basename(dataPath)} declares no pixel size; assuming "
                f"{result.pixel_size_nm:g} nm for preview and export"
            )

        reconstructionController = getattr(self._main, 'reconstructionController', None)
        if reconstructionController is None:
            self._logger.error(
                "No reconstruction controller available to receive localizations"
            )
            return 'empty'

        reconstructionController.resultProduced(result, result.name)
        self._logger.info(
            f"Loaded {len(result)} localizations from {os.path.basename(dataPath)} "
            f"({localizationFormat})"
        )
        return 'current'

    def _loadMetadataAsCurrent(self, source) -> str:
        """Inspect and route a non-array source without asking DataObj to open it."""
        from imswitch.imcommon.algorithms.tile_mosaic import (
            inspect_dataset,
            manifest_fingerprint,
        )

        try:
            index, completeness = inspect_dataset(source.path)
            data_obj = DataObj.fromMetadataSource(
                source.path.parent.name,
                source.path,
                source.format_id,
                index,
                originalPath=source.original_path,
            )
            data_obj.sourceSummary = completeness
            data_obj.sourceFingerprint = manifest_fingerprint(source.path)
        except Exception as exc:
            self._logger.error(
                f"Could not inspect tiling source {source.original_path}: {exc}"
            )
            return 'empty'

        if self._main._currentDataObj is not None:
            self._main._currentDataObj.checkAndUnloadData()
        self._main._currentDataObj = data_obj
        if data_obj.sourceReady:
            self._commChannel.sigCurrentDataChanged.emit(data_obj)
            self._widget.raiseCurrentDataDock()
            return 'current'
        return 'empty'

    def _loadAsCurrent(self, name, datasetName, dataPath, *, virtual: bool = False):
        """Promote a dataset to the current DataObj and emit sigCurrentDataChanged.

        Extracted so ``_loadFromPath`` (used by drag-drop and quickLoadData)
        shares the routing with any future single-dataset entry points.
        """
        if self._main._currentDataObj is not None:
            self._main._currentDataObj.checkAndUnloadData()
        self._main._currentDataObj = DataObj(name, datasetName, path=dataPath)
        if virtual:
            self._main._currentDataObj.checkAndOpenData()
        else:
            self._main._currentDataObj.checkAndLoadData()
        ready = getattr(self._main._currentDataObj, 'sourceReady', None)
        if ready is None:
            ready = (
                self._main._currentDataObj.sourceLoaded
                if virtual else self._main._currentDataObj.dataLoaded
            )
        if ready:
            self._commChannel.sigCurrentDataChanged.emit(self._main._currentDataObj)
            self._widget.raiseCurrentDataDock()

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

    def saveCurrent(self, dataType):
        """ Saves the reconstructed image or coefficients from the current
        result to a user-specified destination. """

        # Coefficients are a MoNaLISA-specific TIFF; a reconstruction can go
        # into any container the shared writer supports, and the chosen
        # filter's suffix is what decides which.
        nameFilter = (
            result_io.file_dialog_filter() if dataType == 'reconstruction'
            else '*.tiff'
        )
        filePath = guitools.askForFilePath(self._widget,
                                           caption=f'Save {dataType}',
                                           defaultFolder=self._saveFolder or self._dataFolder,
                                           nameFilter=nameFilter, isSaving=True)

        if filePath:
            reconObj = self._main.reconstructionController.getActiveResult()
            if dataType == 'reconstruction':
                self.saveReconstruction(reconObj, filePath)
            elif dataType == 'coefficients':
                if getattr(reconObj, 'getCoeffs', lambda: None)() is None:
                    self._logger.error(
                        "Coefficient export is only available for MoNaLISA reconstructions"
                    )
                    return
                self.saveCoefficients(reconObj, filePath)
            else:
                raise ValueError(f'Invalid save data type "{dataType}"')

    def saveAll(self, dataType):
        """ Saves the reconstructed image or coefficients from all available
        results to a user-specified directory. """

        dirPath = guitools.askForFolderPath(self._widget,
                                            caption=f'Save all {dataType}',
                                            defaultFolder=self._saveFolder or self._dataFolder)

        if dirPath:
            for name, reconObj in self._main.reconstructionController.getAllResults():
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
                    if getattr(reconObj, 'getCoeffs', lambda: None)() is None:
                        self._logger.error(
                            f"Skipping coefficient export for {name}; "
                            "not a MoNaLISA reconstruction"
                        )
                        continue
                    self.saveCoefficients(reconObj, filePath)
                else:
                    raise ValueError(f'Invalid save data type "{dataType}"')

    def saveReconstruction(self, reconObj, filePath):
        # Two-part suffixes (.ome.tif, .ome.zarr) are why this asks the writer
        # rather than reading Path.suffix, which sees only the last part and
        # would call an OME-Zarr directory a TIFF.
        reconObj.save(Path(filePath), result_io.format_for_path(filePath))

    def saveCoefficients(self, reconObj, filePath):
        coeffs = copy.deepcopy(reconObj.getCoeffs())
        self._logger.debug(f'Shape of coeffs: {coeffs.shape}')
        coeffs = np.swapaxes(coeffs, 1, 2)
        tiff.imwrite(filePath, coeffs,
                     imagej=True, resolution=(1, 1),
                     metadata={'spacing': 1, 'unit': 'px', 'axes': 'TZCYX'})


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
