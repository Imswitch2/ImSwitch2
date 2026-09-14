import numpy as np

from imswitch.imcommon.model import initLogger
from imswitch.improcess.model.result import result_kind
from .basecontrollers import ImProcessWidgetController


class ReconstructionViewController(ImProcessWidgetController):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._logger = initLogger(self)

        self._currItemInd = None
        self._prevViewId = None

        self._transposeOrder = [0, 1, 2, 3, 4, 5]
        self._axisStep = (0, 0, 0, 0, 0, 0)
        # Axis labels of the image currently shown in the viewer, in displayed
        # (transposed) order. Used to locate a "Base" axis for per-base contrast
        # rescaling without hard-coding its position.
        self._displayedAxisLabels = []

        self._commChannel.sigScanParamsUpdated.connect(self.scanParamsUpdated)
        self._commChannel.sigResultProduced.connect(self.resultProduced)
        self._commChannel.sigLiveResultUpdated.connect(self.liveResultUpdated)
        # Results that have ever arrived as a live update. Their pixels can be
        # rewritten between render passes, so they never get a mutation token
        # and nothing downstream caches a measurement of them.
        self._liveResultUids: set = set()
        # The list widget owns the loaded results; registering here is what
        # lets any panel enumerate them through the channel instead of
        # reaching for this controller.
        self._commChannel.setResultProvider(self)

        self._widget.sigItemSelected.connect(self.listItemChanged)
        self._widget.sigAxisStepChanged.connect(self.axisStepChanged)
        self._widget.sigViewChanged.connect(lambda: self.fullUpdate(levels=None))
        if hasattr(self._widget, "sigSelectionChanged"):
            self._widget.sigSelectionChanged.connect(self._resultsChanged)
        if hasattr(self._widget, "sigResultsRemoved"):
            self._widget.sigResultsRemoved.connect(self._resultsChanged)
        # The render-controls panel emits intent; the renderer lives here.
        self._commChannel.sigSmlmRenderSettingsChanged.connect(
            self.applyNapariStormSettings)
        self._commChannel.sigSmlmRenderAppearanceChanged.connect(
            self.applyNapariStormAppearance)

    def getActiveResult(self):
        return self._widget.getCurrentItemData()

    def getAllResults(self):
        return self._widget.getAllItemDatas()

    def getSelectedResults(self):
        if hasattr(self._widget, "getSelectedItemDatas"):
            return list(self._widget.getSelectedItemDatas())
        current = self.getActiveResult()
        if current is None:
            return []
        return [(getattr(current, "name", "result"), current)]

    def getNapariViewer(self):
        """The embedded napari viewer, for code that adds layers of its own
        (napari endpoint sessions). ``None`` when the view has no viewer."""
        return getattr(self._widget, "napariViewer", None)

    def _resultsChanged(self) -> None:
        """Announce that the loaded set or the selection moved."""
        self._retainNapariStormDatasets()
        self._commChannel.sigResultsChanged.emit()

    def applyNapariStormSettings(self, result, overrides, renderRange) -> None:
        """Redraw a point cloud with new Gaussian settings and render range."""
        display = self._napariStormDisplay()
        if display is None:
            return
        display.apply_settings(result, overrides=overrides, render_range=renderRange)

    def applyNapariStormAppearance(self, result, appearance) -> None:
        """Recolour a point cloud, which rebuilds no geometry."""
        display = self._napariStormDisplay()
        if display is None:
            return
        display.set_appearance(result, **appearance)

    def _napariStormDisplay(self):
        """The point-cloud backend, if there is one and it can draw.

        Two independent gates, either of which leaves localization results on
        their built-in preview: the view only builds a display when the config
        asks for it, and the display only reports itself importable when the
        optional package is installed.
        """
        display = getattr(self._widget, "napariStormDisplay", None)
        if display is None or not display.importable:
            return None
        return display

    def _showWithNapariStorm(self, result) -> bool:
        """Draw a localization result as a point cloud, if we can."""
        if result_kind(result) != "localization":
            # Anything else takes the ordinary path, so whatever the point
            # cloud was showing must stop competing with it for the canvas.
            display = self._napariStormDisplay()
            if display is not None:
                display.hide()
            return False

        display = self._napariStormDisplay()
        if display is None:
            return False
        return display.show(result)

    def _retainNapariStormDatasets(self) -> None:
        """Close point clouds whose result has left the list."""
        display = self._napariStormDisplay()
        if display is None:
            return
        try:
            results = [data for _name, data in self._widget.getAllItemDatas()]
        except Exception as exc:
            self._logger.debug("Could not enumerate results: %s", exc)
            return
        display.retain_only(results)

    def listItemChanged(self):
        currItem = self._widget.getCurrentItemData()
        if self._currItemInd is not None:
            prevItem = self._widget.getDataAtIndex(self._currItemInd)
            self._persistActiveViewerSettings(prevItem)
        self._syncViewModes(currItem)

        if currItem is None:
            self.fullUpdate(levels=None)
            self._currItemInd = self._widget.getCurrentItemIndex()
            self._commChannel.sigCurrentResultChanged.emit(None)
            return

        retrievedLevels = currItem.getDispLevels() if hasattr(currItem, "getDispLevels") else None
        self.fullUpdate(autoLevels=self._currItemInd is None, levels=retrievedLevels)
        if retrievedLevels is not None:
            self._widget.setImageDisplayLevels(retrievedLevels[0], retrievedLevels[1])

        self._currItemInd = self._widget.getCurrentItemIndex()
        self._commChannel.sigCurrentResultChanged.emit(currItem)

    def fullUpdate(self, autoLevels=False, levels=None):
        reconObj = self._widget.getCurrentItemData()
        if reconObj is not None:
            self.setImgSlice(autoLevels=autoLevels, levels=levels)
            if (self._currItemInd is None or self._prevViewId is None or
                    self.getViewId() != self._prevViewId):
                self._widget.resetView()
        else:
            self._widget.clearImage()

        self._prevViewId = self.getViewId()

    def setImgSlice(self, autoLevels=False, levels=None):
        current = self._widget.getCurrentItemData()
        self._setProcessingResultSlice(current, autoLevels=autoLevels, levels=levels)

    def _setProcessingResultSlice(self, result, autoLevels=False, levels=None):
        if self._showWithNapariStorm(result):
            # The point cloud is the display; the preview histogram it would
            # otherwise fall back to would only sit behind it.
            self._transposeOrder = []
            self._displayedAxisLabels = []
            self._widget.clearImage()
            return

        display_layers = result.display_layers() if hasattr(result, "display_layers") else []
        if display_layers:
            if hasattr(result, "applyDisplayLayerSettings"):
                display_layers = result.applyDisplayLayerSettings(display_layers)
            self._transposeOrder = list(range(np.asarray(display_layers[0].data).ndim))
            # Display layers carry their own per-layer contrast and have no
            # shared sliced axis, so there is no "Base" axis to rescale against.
            self._displayedAxisLabels = list(display_layers[0].axis_labels)
            first = display_layers[0]
            self._widget.setDisplayLayers(
                display_layers,
                identity=self._resultIdentity(
                    result,
                    np.asarray(first.data),
                    list(first.axis_labels),
                    first.axis_scales,
                    first.scale_unit,
                    view_mode=None,
                ),
            )
            return

        if result_kind(result) in ("curve", "table"):
            self._transposeOrder = []
            self._displayedAxisLabels = []
            self._widget.clearImage()
            return

        mode = self._processingViewMode(result)
        im = result.data.transpose(*mode.transpose)
        axisLabels = np.array(result.axis_labels)[list(mode.transpose)]
        axisScales = np.array(result.axis_scales, dtype=float)[list(mode.transpose)]
        self._transposeOrder = list(mode.transpose)
        self._displayedAxisLabels = list(axisLabels)

        self._logger.debug(
            "_setProcessingResultSlice: result=%s  view_mode=%s  "
            "data.shape=%s  transposed.shape=%s  "
            "axis_labels=%s  axis_scales=%s  scale_unit=%s",
            type(result).__name__, mode.name,
            result.data.shape, im.shape,
            list(axisLabels), [f"{s:.4g}" for s in axisScales], result.scale_unit,
        )

        colormap = (
            result.getDisplayColormap()
            if hasattr(result, "getDisplayColormap")
            else "grayclip"
        )
        result_name = getattr(result, 'name', None)
        identity = self._resultIdentity(
            result, im, list(axisLabels), list(axisScales), result.scale_unit,
            view_mode=mode.name,
        )
        self._widget.setImage(
            im,
            axisLabels,
            axisScales,
            result.scale_unit,
            colormap=colormap,
            name=result_name,
            identity=identity,
        )
        # Re-activate the main layer so tools operate on the selected result.
        # (setDisplayLayers already does this; here we match that behavior for the setImage path.)
        try:
            self._widget.napariViewer.layers.selection.active = self._widget.imgLayer
        except Exception as exc:
            self._logger.debug("_setProcessingResultSlice: could not re-activate imgLayer: %s", exc)
        
        if levels is not None:
            self._widget.setImageDisplayLevels(*levels)
        elif autoLevels:
            self.updateLevelsRange(base=None)

    def _mutationToken(self, result) -> str | None:
        """A token that changes whenever this layer's pixels can have changed.

        Minted per render pass, which is exactly when the array behind the
        layer is replaced — and *withheld* for live results, whose array can be
        rewritten in place between passes. A token there would certify data
        that had changed, so the honest answer is None: consumers that cache on
        it simply do not cache.
        """
        uid = getattr(result, "result_uid", None)
        # Read through __dict__: on a controller whose base __init__ has not
        # run, plain getattr raises rather than falling back to the default.
        if uid and uid in self.__dict__.get("_liveResultUids", ()):
            return None
        generation = self.__dict__.get("_renderGeneration", 0) + 1
        self.__dict__["_renderGeneration"] = generation
        return f"render:{uid or id(result)}:{generation}"

    def _resultIdentity(self, result, data, axis_labels, axis_scales, scale_unit, *, view_mode):
        """Spatial provenance for the layer about to be rendered.

        Assembled here because this is the only place that knows all of it at
        once: the result supplies the identities, the view mode decides which
        two axes are on screen, and the transposed array supplies the sizes.
        Anything measuring the layer later reads it back off the layer itself.
        """
        data = np.asarray(data)
        labels = [str(label) for label in axis_labels]
        scales = list(axis_scales or [1.0] * data.ndim)
        axes = [
            {
                "label": labels[axis] if axis < len(labels) else str(axis),
                "size": int(data.shape[axis]),
                "scale": float(scales[axis]) if axis < len(scales) else 1.0,
                "unit": scale_unit,
            }
            for axis in range(data.ndim)
        ]
        return {
            "result_uid": getattr(result, "result_uid", None),
            "dataset_uid": getattr(result, "dataset_uid", None),
            "coordinate_space_uid": getattr(result, "coordinate_space_uid", None),
            "identity_kind": getattr(result, "identity_kind", "minted"),
            "lineage": tuple(getattr(result, "lineage", ()) or ()),
            # The displayed plane is always the last two axes after the view
            # mode's transposition.
            "plane_axes": tuple(labels[-2:]) if len(labels) >= 2 else tuple(labels),
            "view_mode": view_mode,
            "axes": axes,
            "mutation_token": self._mutationToken(result),
        }

    def _processingViewMode(self, result):
        view_name = self._widget.getViewName()
        for mode in result.view_modes:
            if mode.name == view_name:
                return mode
        return result.view_modes[0]

    def _syncViewModes(self, item):
        if hasattr(item, "view_modes"):
            self._widget.setViewModes(item.view_modes)
        else:
            self._widget.setViewModes(None)

    def getViewId(self):
        """Return a hashable ID for the current view, used only for change detection."""
        viewName = self._widget.getViewName()
        # Legacy numeric IDs for the three hard-coded standard views.
        if viewName == 'standard':
            return 3
        elif viewName == 'bottom':
            return 4
        elif viewName == 'left':
            return 5
        else:
            # Custom view mode from ProcessingResult.view_modes (e.g. "XY", "XZ", "YZ").
            # Return the name itself; getViewId() is only compared for equality in fullUpdate.
            return viewName

    def _baseAxisIndex(self):
        """Index of the displayed "Base" axis, or None if the current result
        has no Base axis (e.g. it renders as per-base display layers, or is a
        non-MoNaLISA result)."""
        labels = self._displayedAxisLabels
        if labels and "Base" in labels:
            return labels.index("Base")
        return None

    def axisStepChanged(self, newAxisStep):
        baseAxisIndex = self._baseAxisIndex()
        if baseAxisIndex is not None and baseAxisIndex < len(newAxisStep):
            newBase = newAxisStep[baseAxisIndex]
            prevBase = (self._axisStep[baseAxisIndex]
                        if baseAxisIndex < len(self._axisStep) else None)
            if newBase != prevBase:
                # Base changed; rescale contrast to that component's range.
                self.updateLevelsRange(newBase)

        self._axisStep = newAxisStep

    def updateLevelsRange(self, base=None):
        im = self._widget.getImage()
        baseAxisIndex = self._baseAxisIndex()

        if baseAxisIndex is None:
            # No Base axis — rescale to the whole displayed image's range.
            levels = im.min(), im.max()
        else:
            if base is None:
                base = (self._axisStep[baseAxisIndex]
                        if baseAxisIndex < len(self._axisStep) else 0)
            indexForImage = [slice(None) for _ in range(im.ndim)]
            indexForImage[baseAxisIndex] = base
            imAtBase = im[tuple(indexForImage)]
            levels = imAtBase.min(), imAtBase.max()

        self._widget.setImageDisplayLevelsRange(*levels)
        self._widget.setImageDisplayLevels(*levels)

    def updateRecon(self):
        reconObj = self._widget.getCurrentItemData()
        if reconObj is not None and hasattr(reconObj, "updateImages"):
            reconObj.updateImages()
            self.fullUpdate(levels=None)

    def scanParamsUpdated(self, scanParDict, applyOnCurrentRecon):
        if not applyOnCurrentRecon:
            return

        reconObj = self._widget.getCurrentItemData()
        if reconObj is not None and hasattr(reconObj, "updateScanParams"):
            reconObj.updateScanParams(scanParDict)
            self.updateRecon()

    def getImage(self):
        return self._widget.getImage()

    def getActiveImage(self):
        if hasattr(self._widget, "getActiveImage"):
            return self._widget.getActiveImage()
        return self._widget.getImage()

    def getActiveImageCurrentView(self):
        image = self.getActiveImage()
        if getattr(image, "ndim", 0) <= 2:
            return image
        index = []
        for axis in range(image.ndim - 2):
            step = self._axisStep[axis] if axis < len(self._axisStep) else 0
            index.append(max(0, min(int(step), image.shape[axis] - 1)))
        index.extend([slice(None), slice(None)])
        return image[tuple(index)]

    def getActiveImageDisplayLevels(self):
        if hasattr(self._widget, "getActiveImageDisplayLevels"):
            return self._widget.getActiveImageDisplayLevels()
        return self._widget.getImageDisplayLevels()

    def setActiveImageDisplayLevels(self, minimum, maximum):
        if hasattr(self._widget, "setActiveImageDisplayLevels"):
            self._widget.setActiveImageDisplayLevels(minimum, maximum)
        else:
            self._widget.setImageDisplayLevels(minimum, maximum)

        result = self.getActiveResult()
        self._storeActiveDisplayLevels(result, (minimum, maximum))

    def getActiveImageColormap(self) -> str:
        if hasattr(self._widget, "getActiveImageColormap"):
            return self._widget.getActiveImageColormap()
        result = self.getActiveResult()
        if result is not None and hasattr(result, "getDisplayColormap"):
            return result.getDisplayColormap()
        return "grayclip"

    def setActiveImageColormap(self, colormap: str):
        if hasattr(self._widget, "setActiveImageColormap"):
            self._widget.setActiveImageColormap(colormap)

        result = self.getActiveResult()
        self._storeActiveColormap(result, colormap)

    def getDisplayLayerStates(self) -> list[dict]:
        if hasattr(self._widget, "getImageLayerStates"):
            return self._widget.getImageLayerStates()
        return []

    def setDisplayLayerVisible(self, layer_id: str, visible: bool) -> None:
        if hasattr(self._widget, "setImageLayerVisible"):
            self._widget.setImageLayerVisible(layer_id, visible)
        result = self.getActiveResult()
        if result is not None and hasattr(result, "setDisplayLayerVisible"):
            result.setDisplayLayerVisible(layer_id, visible)

    def setDisplayLayerColormap(self, layer_id: str, colormap: str) -> None:
        if hasattr(self._widget, "setImageLayerColormap"):
            self._widget.setImageLayerColormap(layer_id, colormap)
        result = self.getActiveResult()
        if result is not None and hasattr(result, "setDisplayLayerColormap"):
            result.setDisplayLayerColormap(layer_id, colormap)

    def setActiveImageDisplayLevelsRange(self, minimum, maximum):
        if hasattr(self._widget, "setActiveImageDisplayLevelsRange"):
            self._widget.setActiveImageDisplayLevelsRange(minimum, maximum)
        else:
            self._widget.setImageDisplayLevelsRange(minimum, maximum)

    def _persistActiveViewerSettings(self, result) -> None:
        if result is None:
            return
        try:
            levels = self.getActiveImageDisplayLevels()
        except Exception:
            levels = None
        if levels is not None:
            self._storeActiveDisplayLevels(result, levels)
        try:
            colormap = self.getActiveImageColormap()
        except Exception:
            colormap = None
        if colormap:
            self._storeActiveColormap(result, colormap)
        if hasattr(self._widget, "getImageLayerStates"):
            for state in self._widget.getImageLayerStates():
                self._storeDisplayLayerState(result, state)

    def _storeActiveDisplayLevels(self, result, levels) -> None:
        if result is None:
            return
        layer_id = self._activeDisplayLayerId(result)
        if layer_id is not None and hasattr(result, "setDisplayLayerLevels"):
            result.setDisplayLayerLevels(layer_id, levels)
        elif hasattr(result, "setDispLevels"):
            result.setDispLevels(levels)

    def _storeActiveColormap(self, result, colormap: str) -> None:
        if result is None:
            return
        layer_id = self._activeDisplayLayerId(result)
        if layer_id is not None and hasattr(result, "setDisplayLayerColormap"):
            result.setDisplayLayerColormap(layer_id, colormap)
        elif hasattr(result, "setDisplayColormap"):
            result.setDisplayColormap(colormap)

    def _storeDisplayLayerState(self, result, state: dict) -> None:
        if result is None:
            return
        metadata = dict(state.get("metadata", {}) or {})
        if metadata.get("source_result") not in (None, getattr(result, "name", None)):
            return
        layer_id = state.get("id")
        if not layer_id:
            return
        if hasattr(result, "setDisplayLayerVisible"):
            result.setDisplayLayerVisible(layer_id, bool(state.get("visible", True)))
        if state.get("colormap") and hasattr(result, "setDisplayLayerColormap"):
            result.setDisplayLayerColormap(layer_id, str(state["colormap"]))

    def _activeDisplayLayerId(self, result):
        if result is None:
            return None
        metadata = {}
        if hasattr(self._widget, "getActiveImageLayerMetadata"):
            metadata = self._widget.getActiveImageLayerMetadata()
        if metadata.get("source_result") not in (None, getattr(result, "name", None)):
            return None
        component = metadata.get("component")
        if component:
            return str(component)
        return None

    def resultProduced(self, result, displayName):
        """Add a freshly-produced result to the reconstruction list.

        Decouples the producer (any reconstructor or processor) from this
        viewer-side widget so future runners — processor chains, batch
        watchers, scripted entry points — can publish without reaching into
        the main view.
        """
        if result is None:
            return
        name = displayName or getattr(result, 'name', '') or 'result'
        self._widget.addNewData(result, name)
        self._resultsChanged()

    def liveResultUpdated(self, result):
        """Update the view with a live reconstruction result.
        
        Refreshes the current display with the latest partial result from
        a streaming reconstruction session.
        """
        if result is None:
            return
        
        uid = getattr(result, "result_uid", None)
        if uid:
            self._liveResultUids.add(uid)

        current = self._widget.getCurrentItemData()
        if current is None or getattr(current, 'name', '') != getattr(result, 'name', ''):
            self._widget.addNewData(result, getattr(result, 'name', 'Live'))
        else:
            currentItem = self._widget.reconList.currentItem()
            if currentItem is not None:
                currentItem.setData(1, result)
                self.fullUpdate(levels=None)

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
