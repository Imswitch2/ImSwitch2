import copy
import os
from pathlib import Path

import numpy as np
import tifffile as tiff
from qtpy import QtCore, QtGui, QtWidgets

import imswitch.imcommon.view.guitools as guitools
from imswitch.imcommon.controller import PickDatasetsController
from imswitch.imcommon.model import dirtools
from imswitch.improcess.analysis.smlm_import import (
    read_generic_csv,
    read_localizations,
    sniff_localization_format,
)
from imswitch.imcommon.model.measurement_run import MeasurementRunFile, is_measurement_run_file
from imswitch.improcess.model import result_io
from imswitch.improcess.model import DataObj
from imswitch.improcess.model.dataset_sources import (
    LOCALIZATIONS_SPEC,
    LOCATOR_DIRECTORY,
    MEASUREMENT_RUN_SOURCE_KIND,
    TILING_MANIFEST_SPEC,
    file_dialog_filter,
    preferred_source_spec,
    resolve_dataset_source,
    specs_for_reconstructor,
)
from imswitch.improcess.model.folder_preferences import (
    FolderPreferences,
    load_folder_preferences,
    save_folder_preferences,
    load_quick_load_source,
    save_quick_load_source,
    load_quick_load_keep_history,
    save_quick_load_keep_history,
)
from imswitch.improcess.model.lapse_source import (
    TIME_LAPSE_SOURCE_KIND,
    NotATimeLapse,
    discover_time_lapse,
    lapses_in_file,
)
from .MultiDataFrameController import MultiDataFrameController
from .basecontrollers import ImProcessWidgetController

#: What ``_loadTimeLapseAsCurrent`` answers for a file holding several lapses
#: when no item of one was picked: the dataset picker has to ask first.
_PICK_A_LAPSE_ITEM = 'pick-lapse-item'


def _accepts_time_lapse(reconstructor) -> bool:
    return TIME_LAPSE_SOURCE_KIND in tuple(
        getattr(reconstructor, 'accepted_source_kinds', ()) or ()
    )


def _picked_dataset(source) -> str | None:
    """The dataset a path picked *inside* a container names, or None.

    ``lapse.zarr/scan4/Camera`` resolves to the container ``lapse.zarr``; the
    rest is which item the user meant. None when the container itself was
    picked.
    """
    try:
        inside = Path(source.original_path).relative_to(Path(source.path))
    except (TypeError, ValueError):
        return None
    return inside.as_posix() if inside.parts else None


def _route_as_current(controller, data_obj) -> str:
    """Make a source that DataObj did not open itself the current one."""
    main = controller._main
    if main._currentDataObj is not None:
        main._currentDataObj.checkAndUnloadData()
    main._currentDataObj = data_obj
    if data_obj.sourceReady:
        controller._commChannel.sigCurrentDataChanged.emit(data_obj)
        controller._widget.raiseCurrentDataDock()
        return 'current'
    return 'empty'


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
        # Kept between sessions in improcess_options.json (Preferences >
        # Default folders…); None means the dialogs start where the system
        # puts them.
        folders = load_folder_preferences()
        self._dataFolder = folders.dataFolder or None
        self._saveFolder = folders.saveFolder or None

        # Multi-dataset controllers are only used by _loadFromPath
        self.multiDataFrameController = self._factory.createController(
            MultiDataFrameController, self._widget.multiDataFrame
        )
        self.multiDataFrameController.setAddDataDelegate(self.addDataToMultiData)
        self._loadedDataObjs = []
        self._replaceLoadedCatalogOnNextRegistration = False
        loaded_frame = getattr(self._widget, "loadedDataFrame", None)
        if loaded_frame is not None:
            loaded_frame.setSourceMode(load_quick_load_source())
            loaded_frame.setKeepHistory(load_quick_load_keep_history())
            loaded_frame.sigSourceChanged.connect(save_quick_load_source)
            loaded_frame.sigKeepHistoryChanged.connect(save_quick_load_keep_history)
            loaded_frame.sigSetCurrentRequested.connect(self.setLoadedAsCurrent)
            loaded_frame.sigOpenViewerRequested.connect(self.openLoadedInViewer)
            loaded_frame.sigRevealRequested.connect(self.revealLoadedSource)
            loaded_frame.sigRemoveRequested.connect(self.removeLoadedDataObj)
            loaded_frame.sigAddToMultiDataRequested.connect(self.addLoadedSelectionToMultiData)
        self._commChannel.sigCurrentDataChanged.connect(self._currentDataChanged)
        self.pickDatasetsController = self._factory.createController(
            PickDatasetsController, self._widget.pickDatasetsDialog
        )
        if self._dataFolder is not None:
            # The add-data dialog starts in the saved folder too.
            self._commChannel.sigDataFolderChanged.emit(self._dataFolder)

    def quickLoadData(self):
        """Quick Load one or many sources into the ad-hoc Loaded catalog.

        One source keeps the historic fluid behaviour and becomes Current data
        immediately. With several sources every selected dataset is catalogued
        and only the first one is promoted to Current data.
        """
        paths = self._requestQuickLoadPaths()
        if not paths:
            return
        frame = getattr(self._widget, 'loadedDataFrame', None)
        if frame is not None:
            set_status = getattr(frame, 'setStatus', None)
            if callable(set_status):
                # A new Quick Load operation supersedes the previous routing
                # status; the dedicated slot itself remains in place.
                set_status("")
        self._replaceLoadedCatalogOnNextRegistration = bool(
            frame is not None and not frame.keepHistory()
        )
        promoted = False
        try:
            for dataPath in paths:
                self._logger.debug(f'Loading data at: {dataPath}')
                outcome = self._loadFromPath(
                    dataPath,
                    prefer_as_current=not promoted,
                    route_to_loaded=True,
                )
                if outcome == 'current':
                    promoted = True
        finally:
            # If no ordinary Quick Load source was registered (for example a
            # special metadata/localization source), preserve the old catalog.
            self._replaceLoadedCatalogOnNextRegistration = False
        if frame is not None:
            # The current load operation is the natural reconstruction scope.
            # This selects every source chosen together while the first one is
            # still the only item promoted to Current data.
            frame.selectSources(paths)
        if hasattr(self._widget, 'raiseLoadedDataDock'):
            self._widget.raiseLoadedDataDock()

    def _requestQuickLoadPaths(self):
        frame = getattr(self._widget, "loadedDataFrame", None)
        mode = frame.sourceMode() if frame is not None else "files"
        if mode == "zarr":
            path = guitools.askForFolderPath(
                self._widget, caption='Quick Load Zarr', defaultFolder=self._dataFolder
            )
            return [path] if path else []

        specs = self._activeSourceSpecs()
        file_specs = [
            spec for spec in (specs or [])
            if spec.locator != LOCATOR_DIRECTORY
        ]
        paths = guitools.askForFilePath(
            self._widget,
            caption='Quick Load data',
            defaultFolder=self._dataFolder,
            nameFilter=file_dialog_filter(file_specs or specs),
            multiFiles=True,
        )
        return list(paths or [])

    def addDataToMultiData(self):
        """Preserve Multi-data -> Add data through the common file loader.

        External sources are also registered in Loaded, but Current data is
        not changed merely because the operator is assembling Multi-data.
        """
        specs = self._activeSourceSpecs()
        file_specs = [
            spec for spec in (specs or []) if spec.locator != LOCATOR_DIRECTORY
        ]
        paths = guitools.askForFilePath(
            self._widget,
            caption='Add data to Multi-data',
            defaultFolder=self._dataFolder,
            nameFilter=file_dialog_filter(file_specs or specs),
            multiFiles=True,
        ) or []
        for dataPath in paths:
            self._loadFromPath(
                dataPath,
                prefer_as_current=False,
                route_to_loaded=True,
                add_to_multidata=True,
            )
        if paths and hasattr(self._widget, 'raiseMultiDataDock'):
            self._widget.raiseMultiDataDock()

    # Compatibility alias for older callers; user-facing terminology is Multi-data.
    addDataToWorkingSet = addDataToMultiData

    def addLoadedSelectionToMultiData(self) -> int:
        frame = getattr(self._widget, 'loadedDataFrame', None)
        selected = frame.selectedDataObjs() if frame is not None else []
        added = 0
        for data_obj in selected:
            before = len(list(self.multiDataFrameController._widget.getAllDataObjs()))
            self._addDataObjToWorkingSet(data_obj)
            after = len(list(self.multiDataFrameController._widget.getAllDataObjs()))
            added += int(after > before)
        if added and hasattr(self._widget, 'raiseMultiDataDock'):
            self._widget.raiseMultiDataDock()
        return added

    def removeLoadedDataObj(self, data_obj) -> None:
        """Remove one source from the lightweight Loaded catalog only."""
        if data_obj is None:
            return
        frame = getattr(self._widget, 'loadedDataFrame', None)
        if frame is not None:
            frame.removeDataObj(data_obj)
        kept = []
        for existing in self._loadedDataObjs:
            same = existing is data_obj
            if not same:
                try:
                    same = bool(existing.describesSameAs(data_obj))
                except Exception:
                    same = False
            if not same:
                kept.append(existing)
        self._loadedDataObjs[:] = kept

    def revealLoadedSource(self, data_obj) -> None:
        path = str(
            getattr(data_obj, 'sourceOriginalPath', None)
            or getattr(data_obj, 'dataPath', '')
            or ''
        )
        if not path:
            return
        source = Path(path)
        target = source if source.is_dir() else source.parent
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(target)))

    def openLoadedInViewer(self, data_obj) -> None:
        """Inspect one Quick Load source without changing the active reconstructor."""
        if data_obj is None or self._main is None:
            return
        manager = getattr(self._main, 'reconstructorManager', None)
        opener = getattr(manager, 'openInspection', None)
        if not callable(opener):
            return
        try:
            if getattr(data_obj, 'sourceKind', 'image') == TIME_LAPSE_SOURCE_KIND:
                index = getattr(data_obj, 'sourceMetadata', None)
                if index is None:
                    return
                inspect_obj = DataObj.fromMetadataSource(
                    index.name,
                    index.anchor.path,
                    TIME_LAPSE_SOURCE_KIND,
                    index,
                    originalPath=(
                        getattr(data_obj, 'sourceOriginalPath', None)
                        or getattr(data_obj, 'dataPath', '')
                    ),
                )
                plugin_id = 'time-lapse'
            else:
                path = str(getattr(data_obj, 'dataPath', '') or '')
                dataset = getattr(data_obj, 'datasetName', None)
                if not path or dataset in (None, ''):
                    return
                inspect_obj = DataObj(Path(path).name, dataset, path=path)
                inspect_obj.checkAndOpenData()
                plugin_id = 'view-only'
            opener(plugin_id, inspect_obj)
        except Exception as exc:
            self._logger.warning(f'Could not open Quick Load source in viewer: {exc}', exc_info=True)

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
        route_to_loaded: bool = False,
        add_to_multidata: bool = False,
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
            route_to_loaded: Register ordinary image sources in the Quick Load
                catalog instead of routing multi-selection to MultiData.
            add_to_multidata: Also reference catalogued sources from the
                Multi-data; does not change Current data.

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

        # A measurement run is HDF5 by name but not an image: recognised by its
        # schema marker and opened as a metadata source for run reconstructors.
        if is_measurement_run_file(dataPath):
            return self._loadMeasurementRunAsCurrent(dataPath)

        try:
            source = resolve_dataset_source(dataPath, allowed_specs=self._activeSourceSpecs())
            dataPath = str(source.path)
            if source.format_id == TILING_MANIFEST_SPEC.id:
                return self._loadMetadataAsCurrent(source)
            lapseItemToPick = False
            accepts_lapse = _accepts_time_lapse(
                getattr(self._main, '_activeReconstructor', None)
            )
            if route_to_loaded:
                # Quick Load is a data-management action, so recognise a
                # recorded logical lapse independently of the active
                # reconstructor. Discovery reads only the selected item's
                # metadata; the potentially expensive sibling/header scan is
                # deferred to Current data's timepoint worker.
                lapse_obj = self._timeLapseDataObj(source, check_multiple=False)
                if lapse_obj is not None:
                    return self._routeCatalogDataObj(
                        lapse_obj,
                        prefer_as_current=prefer_as_current,
                        add_to_multidata=add_to_multidata,
                    )
            elif accepts_lapse:
                outcome = self._loadTimeLapseAsCurrent(source)
                if outcome == _PICK_A_LAPSE_ITEM:
                    lapseItemToPick = True
                elif outcome is not None:
                    return outcome
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

        if lapseItemToPick and len(datasetsToRoute) == 1:
            # The file holds several lapses; the item picked says which one.
            try:
                if route_to_loaded:
                    lapse_obj = self._timeLapseDataObj(source, datasetsToRoute[0])
                    if lapse_obj is not None and lapse_obj != _PICK_A_LAPSE_ITEM:
                        return self._routeCatalogDataObj(
                            lapse_obj,
                            prefer_as_current=prefer_as_current,
                            add_to_multidata=add_to_multidata,
                        )
                    outcome = None
                else:
                    outcome = self._loadTimeLapseAsCurrent(source, datasetsToRoute[0])
            except Exception as exc:
                self._logger.error(
                    f"Could not open the time lapse of {datasetsToRoute[0]}: {exc}"
                )
                outcome = None
            if outcome is not None:
                return outcome

        if route_to_loaded:
            made_current = False
            for datasetName in datasetsToRoute:
                data_obj = DataObj(name, datasetName, path=dataPath)
                outcome = self._routeCatalogDataObj(
                    data_obj,
                    prefer_as_current=bool(prefer_as_current and not made_current),
                    add_to_multidata=add_to_multidata,
                )
                made_current = made_current or outcome == 'current'
            return 'current' if made_current else ('multidata' if add_to_multidata else 'loaded')

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
            result = read_generic_csv(
                dataPath, preview_pixel_size_nm=self._previewPixelSizeNm(), **kwargs
            )
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
            result = read_localizations(
                dataPath, preview_pixel_size_nm=self._previewPixelSizeNm()
            )
        except Exception as exc:
            self._logger.error(f"Could not read localizations from {dataPath}: {exc}")
            return 'empty'
        return self._publishLocalizations(result, dataPath, localizationFormat)

    def _previewPixelSizeNm(self) -> float:
        """The preview bin the SMLM localizer's panel is set to.

        An imported table is shown exactly as a localized one would be, so
        it takes the same *Rendering -> Preview pixel size*; the active
        reconstructor's parameter widget is where that value lives. The
        localizer's default when no panel is up to ask.
        """
        from imswitch.improcess.reconstructors.smlm.localizer import (
            preview_pixel_size_nm,
        )

        # Through __dict__: on a controller whose Qt base __init__ has not run
        # (tests build one without it) a plain getattr raises instead of
        # answering None.
        widget = self.__dict__.get('_widget')
        getter = getattr(getattr(widget, 'parTree', None), 'get_values', None)
        values = {}
        if callable(getter):
            try:
                values = dict(getter() or {})
            except Exception:
                self._logger.debug(
                    'Could not read the preview pixel size from the parameter widget',
                    exc_info=True,
                )
        return preview_pixel_size_nm(values)

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

        # The table is a result with no ImProcess step behind it; its
        # provenance is the foreign file itself, fingerprinted, plus an
        # opaque import node naming the format. A save then says where the
        # localizations came from instead of recording an anonymous origin.
        from imswitch.improcess.model.provenance import record_external_table

        record_external_table(result, dataPath, table_format=localizationFormat)
        reconstructionController.resultProduced(result, result.name)
        self._logger.info(
            f"Loaded {len(result)} localizations from {os.path.basename(dataPath)} "
            f"({localizationFormat})"
        )
        return 'current'

    def _loadMeasurementRunAsCurrent(self, dataPath) -> str:
        """Open a measurement run file as the current, non-image source."""
        try:
            run = MeasurementRunFile.load(dataPath)
            data_obj = DataObj.fromMetadataSource(
                os.path.basename(dataPath), Path(dataPath),
                MEASUREMENT_RUN_SOURCE_KIND, run,
            )
            data_obj.sourceFingerprint = run.run_id
        except Exception as exc:
            self._logger.error(f"Could not open the measurement run {dataPath}: {exc}")
            return 'empty'
        self._logger.info(
            f"Opened measurement run {run.run_id}: {run.n_points} points, "
            f"acquisition {run.acquisition.value}"
        )
        return _route_as_current(self, data_obj)

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

        return _route_as_current(self, data_obj)

    def _timeLapseDataObj(self, source, dataset=None, *, check_multiple=True):
        """Resolve a logical lapse without routing it to Current data.

        ``check_multiple=False`` is the Quick Load fast path: it deliberately
        avoids scanning every sibling header on the GUI thread. Current data
        performs the full plan/header read asynchronously when the first
        timepoint is requested.
        """
        picked = _picked_dataset(source)
        explicit_run = dataset is not None or picked is not None
        if dataset is None:
            dataset = picked
        try:
            index = discover_time_lapse(source.path, dataset)
            if dataset is None and check_multiple:
                lapses = lapses_in_file(index)
                if lapses > 1:
                    self._logger.info(
                        f"{Path(source.path).name} holds {lapses} time lapses; "
                        "pick an item of the one to open."
                    )
                    return _PICK_A_LAPSE_ITEM
        except NotATimeLapse as exc:
            self._logger.info(f"Opening as a single image: {exc}")
            return None
        except Exception as exc:
            self._logger.warning(
                f"Could not read {source.original_path} as a time lapse: {exc}"
            )
            return None
        data_obj = DataObj.fromMetadataSource(
            index.name,
            index.anchor.path,
            TIME_LAPSE_SOURCE_KIND,
            index,
            originalPath=source.original_path,
        )
        data_obj._timeLapseRunExplicit = bool(explicit_run)
        self._logger.info(index.describe())
        return data_obj

    def chooseTimeLapseRun(self, source_obj, dataset_choices):
        """Ask which logical lapse in a shared container Quick Load should open.

        The expensive per-group header scan has already happened in Current
        data's worker.  The GUI thread only presents one representative item
        per detected lapse, then re-runs cheap discovery anchored on the item
        the operator selected.
        """
        choices = [str(choice) for choice in (dataset_choices or ()) if choice]
        if source_obj is None or len(choices) < 2:
            return source_obj

        dialog = getattr(self._widget, 'pickDatasetsDialog', None)
        picker = getattr(dialog, 'datasetsPicker', None)
        old_mode = picker.selectionMode() if picker is not None else None
        try:
            if picker is not None:
                picker.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
            path = str(
                getattr(source_obj, 'dataPath', None)
                or getattr(source_obj, 'sourceOriginalPath', '')
                or ''
            )
            self.pickDatasetsController.setDatasets(path, choices)
            if picker is not None and picker.count():
                picker.clearSelection()
                picker.setCurrentRow(0)
                picker.item(0).setSelected(True)
            if not self._widget.showPickDatasetsDialog(blocking=True):
                return None
            selected = list(self.pickDatasetsController.getSelectedDatasets())
            if len(selected) != 1:
                return None
        finally:
            if picker is not None and old_mode is not None:
                picker.setSelectionMode(old_mode)

        try:
            index = discover_time_lapse(path, selected[0])
        except Exception as exc:
            self._logger.warning(
                f"Could not open the selected time lapse {selected[0]!r}: {exc}"
            )
            return None

        source_obj.name = index.name
        source_obj.dataPath = str(index.anchor.path)
        source_obj.sourceMetadata = index
        source_obj._timeLapseRunExplicit = True
        acquisition, tooltip = self._loadedDataDescription(source_obj)
        frame = getattr(self._widget, 'loadedDataFrame', None)
        if frame is not None:
            frame.addOrUpdateDataObj(
                source_obj, acquisition=acquisition, status='Loaded', tooltip=tooltip
            )
            frame.setCurrentDataObj(source_obj)
        return source_obj

    def _loadTimeLapseAsCurrent(self, source, dataset=None):
        """Open the whole lapse ``source`` belongs to, or return None."""
        data_obj = self._timeLapseDataObj(source, dataset)
        if data_obj in (None, _PICK_A_LAPSE_ITEM):
            return data_obj
        return _route_as_current(self, data_obj)

    @staticmethod
    def _formatBytes(value) -> str:
        try:
            value = int(value)
        except (TypeError, ValueError):
            return ""
        amount = float(max(0, value))
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if amount < 1024.0 or unit == "TB":
                if unit == "B":
                    return f"{int(amount)} {unit}"
                return f"{amount:.1f} {unit}" if amount < 10 else f"{amount:.0f} {unit}"
            amount /= 1024.0
        return ""

    def _loadedDataDescription(self, data_obj) -> tuple[str, str]:
        """Describe a catalog entry without opening an otherwise-idle source.

        Session already performs its richer catalog inspection off the GUI
        thread. Quick Load must stay lightweight too: ordinary entries are
        enriched only after they are actually opened as Current data.
        """
        if getattr(data_obj, 'sourceKind', 'image') == TIME_LAPSE_SOURCE_KIND:
            index = getattr(data_obj, 'sourceMetadata', None)
            count = (
                int(getattr(index, 'planned_count', 0) or 0)
                if index is not None else 0
            )
            shape = (
                tuple(getattr(index, 'anchor_shape', ()) or ())
                if index is not None else ()
            )
            spatial = f"{max(2, len(shape) - 1)}D" if shape else "Time-lapse"
            acquisition = f"{spatial} · T={count}" if count else spatial
            tooltip = (
                index.describe()
                if index is not None and hasattr(index, 'describe')
                else acquisition
            )
            return acquisition, str(tooltip)

        if not bool(getattr(data_obj, 'sourceReady', False)):
            return "", ""
        try:
            handle = data_obj.data_handle
            shape = tuple(int(x) for x in getattr(handle, 'shape', ()) or ())
            labels = tuple(
                str(x) for x in (getattr(data_obj, 'axis_labels', ()) or ())
            )
            nbytes = data_obj.decodedBytes()
        except Exception:
            shape, labels, nbytes = (), (), None
        spatial = ""
        if labels:
            spatial_labels = [
                label.upper()
                for label in labels
                if label.upper() in {"X", "Y", "Z"}
            ]
            spatial = f"{len(spatial_labels)}D" if spatial_labels else ""
        if not spatial and shape:
            spatial = f"{min(3, max(2, len(shape)))}D"
        bits = [bit for bit in (spatial, self._formatBytes(nbytes)) if bit]
        acquisition = " · ".join(bits)
        tooltip = f"Shape: {shape}" if shape else ""
        return acquisition, tooltip

    def _matchingSessionDataset(self, data_obj):
        controller = getattr(self._main, 'processingSessionController', None)
        session = getattr(controller, 'session', None) if controller is not None else None
        if session is None:
            return None
        path = getattr(data_obj, 'dataPath', None)
        dataset_name = getattr(data_obj, 'datasetName', None)
        for dataset in session.datasets:
            if dataset.matches_source(path, dataset_name):
                return dataset
        return None

    def _registerLoadedDataObj(self, data_obj):
        match = self._matchingSessionDataset(data_obj)
        if match is not None:
            frame = getattr(self._widget, 'loadedDataFrame', None)
            set_status = getattr(frame, 'setStatus', None) if frame is not None else None
            if callable(set_status):
                set_status("Already in active Session")
            return data_obj, match
        if self._replaceLoadedCatalogOnNextRegistration:
            frame = getattr(self._widget, 'loadedDataFrame', None)
            if frame is not None:
                frame.clearCatalog()
            # Catalog history is presentation/runtime state only. Queue and
            # Multi-data keep their own references to any older objects.
            self._loadedDataObjs.clear()
            self._replaceLoadedCatalogOnNextRegistration = False
        for existing in self._loadedDataObjs:
            try:
                same = data_obj.describesSameAs(existing)
            except Exception:
                same = False
            if same:
                return existing, None
        self._loadedDataObjs.append(data_obj)
        acquisition, tooltip = self._loadedDataDescription(data_obj)
        frame = getattr(self._widget, 'loadedDataFrame', None)
        if frame is not None:
            frame.addOrUpdateDataObj(
                data_obj, acquisition=acquisition, status='Loaded', tooltip=tooltip
            )
        return data_obj, None

    def _addDataObjToWorkingSet(self, data_obj) -> None:
        """Add a Loaded catalog source to Multi-data.

        Multi-data stores ordinary image ``DataObj`` objects.  A Quick Load
        timelapse, however, is represented by one logical metadata ``DataObj``
        for lazy navigation.  Expand that logical source into its concrete
        per-timepoint image sources, matching the existing Session ->
        Multi-data behavior, rather than silently dropping it.
        """
        source_kind = str(getattr(data_obj, 'sourceKind', 'image') or 'image')
        if source_kind == 'image':
            self.multiDataFrameController.addDataObj(data_obj)
            return
        if source_kind != TIME_LAPSE_SOURCE_KIND:
            return

        index = getattr(data_obj, 'sourceMetadata', None)
        if index is None:
            return
        try:
            candidates = list(index.channel().candidates)
        except Exception:
            self._logger.warning(
                "Could not expand Loaded timelapse into Multi-data",
                exc_info=True,
            )
            return

        for ref in candidates:
            path = str(getattr(ref, 'path', '') or '')
            dataset_name = getattr(ref, 'dataset', None)
            if not path:
                continue
            if dataset_name in (None, ''):
                try:
                    names = list(DataObj.getDatasetNames(path))
                except Exception:
                    names = []
                if len(names) != 1:
                    self._logger.warning(
                        "Could not add Loaded timelapse source to Multi-data "
                        "without guessing a dataset: %s::%s",
                        path,
                        dataset_name,
                    )
                    continue
                dataset_name = names[0]
            self.multiDataFrameController.makeAndAddDataObj(
                Path(path).name, str(dataset_name), path=path
            )

    def _routeCatalogDataObj(
        self, data_obj, *, prefer_as_current: bool, add_to_multidata: bool
    ) -> str:
        data_obj, session_dataset = self._registerLoadedDataObj(data_obj)
        if add_to_multidata:
            if session_dataset is not None:
                controller = getattr(self._main, 'processingSessionController', None)
                helper = getattr(controller, '_multidata_source', None)
                source = helper(
                    session_dataset.primary_path, session_dataset.primary_dataset
                ) if callable(helper) else None
                if source is not None:
                    path, dataset_name = source
                    self.multiDataFrameController.makeAndAddDataObj(
                        os.path.basename(path) or path, dataset_name, path=path
                    )
            else:
                self._addDataObjToWorkingSet(data_obj)
        if prefer_as_current:
            if session_dataset is not None:
                controller = getattr(self._main, 'processingSessionController', None)
                if controller is not None:
                    controller._widget.selectDataset(session_dataset.id)
            return self.setLoadedAsCurrent(data_obj)
        return 'multidata' if add_to_multidata else 'loaded'

    def setLoadedAsCurrent(self, data_obj) -> str:
        if data_obj is None:
            return 'empty'
        if getattr(data_obj, 'sourceKind', 'image') == TIME_LAPSE_SOURCE_KIND:
            # A logical lapse is a catalog/current-data navigation source, not
            # the image object the active reconstructor should see. Let Current
            # data load its first timepoint asynchronously and publish that
            # ordinary image DataObj only when it is ready.
            data_controller = getattr(self._main, 'dataFrameController', None)
            opener = getattr(data_controller, 'openTimeLapseSource', None)
            if not callable(opener):
                return 'empty'
            opener(data_obj)
            frame = getattr(self._widget, 'loadedDataFrame', None)
            if frame is not None:
                frame.setCurrentDataObj(data_obj)
            self._widget.raiseCurrentDataDock()
            return 'current'
        if getattr(data_obj, 'sourceKind', 'image') == 'image':
            if self._main._currentDataObj is not data_obj:
                current = self._main._currentDataObj
                if current is not None:
                    current.checkAndUnloadData()
                self._main._currentDataObj = data_obj
            data_obj.checkAndLoadData()
            ready = data_obj.dataLoaded
        else:
            current = self._main._currentDataObj
            if current is not None and current is not data_obj:
                current.checkAndUnloadData()
            self._main._currentDataObj = data_obj
            ready = bool(getattr(data_obj, 'sourceReady', False))
        if ready:
            self._commChannel.sigCurrentDataChanged.emit(data_obj)
            self._widget.raiseCurrentDataDock()
            return 'current'
        return 'empty'

    def clearCurrentData(self) -> None:
        """Release the authoritative Current Data object and publish None.

        The Current Data view is only a presentation of ``_currentDataObj``;
        clearing the widget alone leaves the DataObj (and any backing source)
        alive.  Keep ownership changes here so every listener observes the
        same transition and resource release.
        """
        current = getattr(self._main, '_currentDataObj', None)
        if current is not None:
            current.checkAndUnloadData()
        self._main._currentDataObj = None
        self._commChannel.sigCurrentDataChanged.emit(None)

    def _currentDataChanged(self, data_obj=None) -> None:
        frame = getattr(self._widget, 'loadedDataFrame', None)
        if frame is None:
            return
        logical = getattr(data_obj, '_timeLapseParent', None)
        catalog_obj = logical if logical is not None else data_obj
        if catalog_obj in self._loadedDataObjs:
            acquisition, tooltip = self._loadedDataDescription(catalog_obj)
            frame.addOrUpdateDataObj(
                catalog_obj,
                acquisition=acquisition,
                status='Loaded',
                tooltip=tooltip,
            )
        frame.setCurrentDataObj(catalog_obj)

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
            # Open decodes the dataset whole, which is what was asked for; a
            # dataset larger than the processing working set is announced
            # before that starts (the DataObj logs the same line), with the
            # lazy alternative named. Not a refusal, and not a switch: which
            # mean the pattern finder receives depends on the open path today,
            # so Open keeps materialising until that is settled.
            dataObj = self._main._currentDataObj
            notice = None
            try:
                # The estimate opens the source handle itself if it must;
                # what it never does is decode anything.
                estimate = getattr(dataObj, 'materializationNotice', None)
                notice = estimate() if callable(estimate) else None
            except Exception as exc:
                # The estimate is a courtesy; the load below reports its own
                # failures the way it always has.
                self._logger.debug(f'No materialisation estimate: {exc!r}')
            if notice and hasattr(self._commChannel, 'sigStatusMessage'):
                self._commChannel.sigStatusMessage.emit(notice)
            dataObj.checkAndLoadData()
        ready = getattr(self._main._currentDataObj, 'sourceReady', None)
        if ready is None:
            ready = (
                self._main._currentDataObj.sourceLoaded
                if virtual else self._main._currentDataObj.dataLoaded
            )
        if ready:
            self._commChannel.sigCurrentDataChanged.emit(self._main._currentDataObj)
            self._widget.raiseCurrentDataDock()

    def releasePaths(self, paths) -> None:
        """Release current/MultiData handles for sources about to be deleted.

        Session raw-data management uses this before moving HDF5/Zarr stores
        to the OS recycle bin.  In particular this avoids Windows refusing a
        move because h5py still owns an open file handle.
        """
        wanted = {
            os.path.normcase(os.path.abspath(str(path)))
            for path in paths
            if path
        }
        if not wanted:
            return

        current = getattr(self._main, '_currentDataObj', None)
        current_path = getattr(current, 'dataPath', None) if current is not None else None
        if current_path and os.path.normcase(os.path.abspath(str(current_path))) in wanted:
            try:
                current.checkAndUnloadData()
            except Exception:
                self._logger.debug('Could not unload current data before deletion', exc_info=True)
            self._main._currentDataObj = None
            self._commChannel.sigCurrentDataChanged.emit(None)

        multi = getattr(self, 'multiDataFrameController', None)
        widget = getattr(multi, '_widget', None)
        if widget is not None:
            for data_obj in list(widget.getAllDataObjs()):
                path = getattr(data_obj, 'dataPath', None)
                if not path or os.path.normcase(os.path.abspath(str(path))) not in wanted:
                    continue
                try:
                    data_obj.checkAndUnloadData()
                except Exception:
                    self._logger.debug('Could not unload MultiData source before deletion', exc_info=True)
                widget.delDataByDataObj(data_obj)
            if multi is not None:
                multi.updateInfo()

    def dataFolderChanged(self, dataFolder):
        self._dataFolder = dataFolder

    def saveFolderChanged(self, saveFolder):
        self._saveFolder = saveFolder

    def openFolderPreferences(self):
        """Show the default-folders editor, seeded with the folders in force."""
        dialog = self._widget.folderPreferencesDialog
        dialog.setValues(FolderPreferences(dataFolder=self._dataFolder or '',
                                           saveFolder=self._saveFolder or ''))
        self._widget.showFolderPreferencesDialog()

    def saveFolderPreferences(self, values):
        """Save the default folders to improcess_options.json and use them now.

        Either may be empty, meaning no default. A folder that cannot be one
        (a relative path, an existing file) keeps the dialog open with the
        reason, as does a file that cannot be written.
        """
        dialog = self._widget.folderPreferencesDialog
        folders = {}
        for field, label in (('dataFolder', 'Open data from'),
                             ('saveFolder', 'Save results to')):
            try:
                folders[field] = dirtools.checkedFolderPath(values.get(field), allowEmpty=True)
            except ValueError as error:
                dialog.setStatus(f'{label}: {error}')
                return
        try:
            save_folder_preferences(FolderPreferences(**folders))
        except OSError as error:
            self._logger.error(f'Could not save the default folders: {error}', exc_info=True)
            dialog.setStatus(f'Could not save the default folders: {error}')
            return
        self._commChannel.sigDataFolderChanged.emit(folders['dataFolder'] or None)
        self._commChannel.sigSaveFolderChanged.emit(folders['saveFolder'] or None)
        dialog.setStatus('')
        dialog.accept()

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
        #
        # The user picked this path in a dialog, so overwriting it is what
        # they asked for; the staged protocol still writes atomically.
        receipt = reconObj.save(
            Path(filePath), result_io.format_for_path(filePath), overwrite=True
        )
        if hasattr(self._commChannel, "sigResultSaved"):
            self._commChannel.sigResultSaved.emit(reconObj, receipt)
        files = getattr(receipt, "files", None)
        if files:
            self._logger.info(
                "Saved %s: %s", getattr(reconObj, "name", "result"),
                ", ".join(str(f) for f in files),
            )
        return receipt

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
