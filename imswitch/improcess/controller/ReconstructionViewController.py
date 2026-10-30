from dataclasses import replace
from pathlib import Path
import weakref

import numpy as np
from qtpy import QtCore

from imswitch.imcommon.model import initLogger
from imswitch.improcess.model.contrast import auto_levels, finite_range
from imswitch.improcess.model.lazy_array import display_array
from imswitch.improcess.model.result import ViewMode, result_kind
from .basecontrollers import ImProcessWidgetController


#: Total percentage of pixels allowed to saturate when a result is scaled
#: automatically -- 2% is 1% off each tail, which is the rule a result's own
#: display layers already use for their per-component levels. The two have to
#: agree: a MoNaLISA reconstruction renders through the display-layer path and
#: a duplicate of it through the plain-image path, and clipping the outliers in
#: one while spanning the full range in the other made the same picture look
#: like two different pictures depending on which was clicked.
AUTO_SATURATED_PERCENT = 2.0
_PRESERVE_DIMENSION_ORDER = object()


class ReconstructionViewController(ImProcessWidgetController):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._logger = initLogger(self)

        self._currItemInd = None
        self._prevViewId = None
        # Viewer state is session-local and belongs to the result, not to the
        # global napari canvas.  In particular, a user may inspect one volume
        # from YZ and another from XY and expect both choices to survive when
        # toggling between results.
        self._viewerStateByResult = weakref.WeakKeyDictionary()
        # Spatial slider memory is intentionally separate from camera/view
        # state. Per-result locking remembers an exact discrete slice index.
        # Normal comparison mode instead remembers a physical/world position,
        # but only reapplies it when the sliced spatial axis disappeared in the
        # intervening view (for example YZ -> 2-D XY -> YZ). Compatible YZ->YZ
        # transitions are deliberately left to napari, which can preserve the
        # physical point correctly when the two stacks have different spacing.
        self._sharedSlicePositionByPlane = {}
        # Camera state follows the same two-level policy as slices. By default
        # the most recent camera is shared by physical viewing plane (XY/YZ/XZ)
        # so repeated measurements can be compared at identical framing. The
        # optional viewer lock makes restoration result-local instead.
        self._sharedCameraStateByPlane = {}
        self._restoringSliceState = False
        # The result currently on screen, held by reference rather than by
        # list index: removing a result shifts every index after it, and the
        # index was what decided whose display settings were being saved.
        self._displayedResult = None

        # Live-update coalescing: streaming reconstruction can emit results
        # faster than a full napari redraw completes. Keep only the newest and
        # render it once the event loop is free, so the GUI never falls behind.
        self._pendingLiveResult = None
        self._liveRenderScheduled = False
        # Set once the current live result has had one full render (which
        # establishes the layers); subsequent updates take the fast in-place
        # data-swap path unless the layer structure changes.
        self._liveEstablished = False
        # Newest completed timepoint from the streaming session; the slider is
        # advanced to it inside the coalesced render.
        self._liveLatestTimepoint = None
        # The list entry owned by the live run currently streaming, and its
        # name. Held by reference so a job's updates keep landing in its own
        # data object even if the user selects a different entry mid-run.
        self._liveItem = None
        self._liveName = None

        self._transposeOrder = [0, 1, 2, 3, 4, 5]
        self._axisStep = (0, 0, 0, 0, 0, 0)
        # Axis labels of the image currently shown in the viewer, in displayed
        # (transposed) order. Used to locate a "Base" axis for per-base contrast
        # rescaling without hard-coding its position.
        self._displayedAxisLabels = []

        self._commChannel.sigScanParamsUpdated.connect(self.scanParamsUpdated)
        self._commChannel.sigResultProduced.connect(self.resultProduced)
        self._commChannel.sigResultReplaced.connect(self.resultReplaced)
        self._commChannel.sigLiveResultUpdated.connect(self.liveResultUpdated)
        self._commChannel.sigLiveTimepointUpdated.connect(self.liveTimepointUpdated)
        self._commChannel.sigSaveLiveResult.connect(self.saveLiveResult)
        self._commChannel.sigLiveTimepointDone.connect(self._onLiveTimepointDone)
        # Results that have ever arrived as a live update. Their pixels can be
        # rewritten between render passes, so they never get a mutation token
        # and nothing downstream caches a measurement of them.
        self._liveResultUids: set = set()
        # The list widget owns the loaded results; registering here is what
        # lets any panel enumerate them through the channel instead of
        # reaching for this controller.
        self._commChannel.setResultProvider(self)
        # Keep object references so removal can release resources owned by a
        # result (notably lazy Session-restored DataObj handles) even though the
        # view's removal signal itself carries no payload.
        self._knownResultObjects = {
            id(result): result for _name, result in self.getAllResults()
        }

        self._widget.sigItemSelected.connect(self.listItemChanged)
        self._widget.sigAxisStepChanged.connect(self.axisStepChanged)
        if hasattr(self._widget, "sigImageLevelsChanged"):
            self._widget.sigImageLevelsChanged.connect(self.imageLevelsChanged)
        self._widget.sigViewChanged.connect(self._viewChanged)
        if hasattr(self._widget, 'sigLockSlicePerResultChanged'):
            self._widget.sigLockSlicePerResultChanged.connect(self._sliceLockChanged)
        if hasattr(self._widget, 'sigLockZoomPerResultChanged'):
            self._widget.sigLockZoomPerResultChanged.connect(self._zoomLockChanged)
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

    def selectResult(self, result) -> bool:
        """Select an already-loaded result without knowing list-widget details."""
        selector = getattr(self._widget, "selectData", None)
        return bool(selector(result)) if callable(selector) else False

    def removeResult(self, result) -> bool:
        """Remove one loaded result by identity without exposing widget details."""
        remover = getattr(self._widget, "removeData", None)
        return bool(remover(result)) if callable(remover) else False

    def getNapariViewer(self):
        """The embedded napari viewer, for code that adds layers of its own
        (napari endpoint sessions). ``None`` when the view has no viewer."""
        return getattr(self._widget, "napariViewer", None)

    @staticmethod
    def _releaseResultResources(result) -> None:
        """Release file-backed resources owned by a result leaving the list."""
        backing = getattr(result, "_session_backing_data_obj", None)
        closer = getattr(backing, "checkAndUnloadData", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass
        if backing is not None:
            try:
                result._session_backing_data_obj = None
            except Exception:
                pass

    def _resultsChanged(self) -> None:
        """Announce selection/membership changes and release removed resources."""
        getter = getattr(self, "getAllResults", None)
        try:
            rows = getter() if callable(getter) else []
        except Exception:
            rows = []
        current = {id(result): result for _name, result in rows}
        known = self.__dict__.get("_knownResultObjects", {})
        for key, result in tuple(known.items()):
            if key not in current:
                self._releaseResultResources(result)
        self._knownResultObjects = current
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
        # Read through __dict__: on a controller whose base __init__ has not
        # run, plain getattr raises rather than falling back to the default.
        displayed = self.__dict__.get("_displayedResult")
        if displayed is not None and displayed is not currItem:
            self._persistActiveViewerSettings(displayed)
            self._rememberViewerState(displayed)
        self._syncViewModes(currItem)

        if currItem is None:
            self.__dict__["_displayedResult"] = None
            self.fullUpdate(levels=None)
            self._currItemInd = self._widget.getCurrentItemIndex()
            self._commChannel.sigCurrentResultChanged.emit(None)
            return

        # Set before rendering: the render itself moves the layers' contrast,
        # and what that reports has to be filed against the result it belongs
        # to rather than the one that was on screen a moment ago.
        self.__dict__["_displayedResult"] = currItem

        # A result nobody has set a contrast on yet is scaled to its own data.
        # Inheriting the previous result's levels is worse than useless when
        # the two are being compared *because* their ranges differ -- which is
        # the reason for having them both in the list.
        retrievedLevels = currItem.getDispLevels() if hasattr(currItem, "getDispLevels") else None
        remembered = self._hasRememberedLevels(currItem)

        # The render applies the levels, and nothing may be applied after it.
        # A second, unconditional apply here used to write the whole-result
        # default over the per-component contrast a display-layer render had
        # just restored -- which is how a reconstruction came back at the
        # levels its reconstructor measured at construction, every time it was
        # clicked, however often its contrast had been set.
        # Restore this result's own raw napari dimension order after rendering.
        # A first-time result has no saved order and is normalised instead, so
        # a YZ roll from the previous result cannot poison a new 2D/XY view.
        saved_order = self._viewerState(currItem).get("dimension_order")
        self.fullUpdate(
            autoLevels=not remembered,
            levels=retrievedLevels,
            dimensionOrder=saved_order,
        )

        self._currItemInd = self._widget.getCurrentItemIndex()
        self._commChannel.sigCurrentResultChanged.emit(currItem)

    @staticmethod
    def _hasRememberedLevels(result) -> bool:
        """Whether this result has a contrast of its own waiting to be restored.

        A result that renders display layers keeps its contrast per component,
        so asking ``getDispLevels()`` alone answers "never set" on every visit,
        however many times its contrast has been adjusted. That answer is what
        decides whether to rescale automatically, and a reconstruction that
        gets rescaled every time it is clicked is the whole complaint.
        """
        if result is None:
            return False
        try:
            if result.getDispLevels() is not None:
                return True
        except AttributeError:
            pass
        try:
            settings = result.displayLayerSettings()
        except AttributeError:
            return False
        return any("display_levels" in (entry or {})
                   for entry in (settings or {}).values())

    def imageLevelsChanged(self, layerMetadata, levels) -> None:
        """Remember a contrast the moment it changes.

        The alternative -- reading the levels back when the selection moves --
        only works if a selection move is the next thing that happens. It is
        not: re-running a reconstruction, switching the view, or any toolbar
        operation re-renders first and overwrites what was never saved. This
        runs on the change itself, so there is no window to lose it in.
        """
        result = self.getActiveResult()
        if result is None or levels is None:
            return
        self._storeDisplayLevels(result, layerMetadata, levels)

    def fullUpdate(
        self,
        autoLevels=False,
        levels=None,
        *,
        resetView=False,
        dimensionOrder=_PRESERVE_DIMENSION_ORDER,
    ):
        reconObj = self._widget.getCurrentItemData()
        if reconObj is not None:
            # Capture viewer state *before* replacing the layer data. A camera
            # is reusable across another XY result, but the same zoom/centre has
            # no useful meaning when the displayed plane turns into YZ/XZ. For
            # slices, knowing which semantic spatial axes existed before the
            # redraw is what lets us avoid overriding napari on compatible
            # YZ->YZ/XZ->XZ transitions.
            source_plane = self._currentDisplayedPlaneAxes()
            source_camera = self._currentCameraState()
            source_slice_axes = set(self._currentSpatialSliceState())

            # Ordinary redraws preserve any raw napari axis roll the user made
            # on the result.  Selection changes pass the saved order explicitly
            # (or None for a first display).
            if dimensionOrder is _PRESERVE_DIMENSION_ORDER:
                dimensionOrder = self._currentDimensionOrder()
            self.__dict__["_restoringSliceState"] = True
            try:
                self.setImgSlice(autoLevels=autoLevels, levels=levels)
                self._restoreDimensionOrder(reconObj, dimensionOrder)
                self._restoreSliceState(
                    reconObj, sourceSpatialAxes=source_slice_axes
                )
            finally:
                self.__dict__["_restoringSliceState"] = False
            # Rendering can move a slider without emitting a useful persistence
            # callback while _restoringSliceState is set. Capture the final
            # napari-resolved position once the redraw is complete.
            self._rememberSliceState(reconObj, updateShared=True)

            target_plane = self._currentDisplayedPlaneAxes()
            state = self._viewerState(reconObj)
            target_key = self._cameraPlaneKey(target_plane)
            per_result_camera = (
                state.setdefault("camera_by_plane", {}).get(target_key)
                if target_key is not None else None
            )
            shared_camera = (
                self.__dict__.setdefault("_sharedCameraStateByPlane", {}).get(target_key)
                if target_key is not None else None
            )

            if self._zoomLockedPerResult():
                desired_camera = per_result_camera
            else:
                # Shared-by-plane is the normal comparison workflow. Fall back
                # to this result's own camera only for older/session state where
                # no shared entry has been established yet.
                desired_camera = shared_camera or per_result_camera

            if desired_camera is not None:
                self._restoreCameraState(desired_camera)
            elif (
                not self._zoomLockedPerResult()
                and source_plane
                and target_plane
                and self._cameraPlaneKey(source_plane) == target_key
                and source_camera is not None
            ):
                # Defensive fallback for a first transition before shared state
                # exists. Normally listItemChanged() has already stored source
                # camera into the shared plane entry.
                self._restoreCameraState(source_camera)
            else:
                # No memory for this policy/plane yet. This includes a
                # first-time result while per-result lock is enabled and any
                # XY<->YZ/XZ transition whose target plane has never been seen.
                self._widget.resetView()

            if target_key is not None:
                state["display_plane"] = target_plane
                current_camera = self._currentCameraState()
                if current_camera is not None:
                    self._storeCameraState(
                        reconObj, target_plane, current_camera, updateShared=True
                    )
        else:
            self._widget.clearImage()

        self._prevViewId = self.getViewId()

    def setImgSlice(self, autoLevels=False, levels=None):
        current = self._widget.getCurrentItemData()
        # TODO: I would argue that all of the logic of '_setProcessingResultSlice'
        #   should be placed in this method, since this method is barely doing anything
        self._setProcessingResultSlice(current, autoLevels=autoLevels, levels=levels)

    def _setProcessingResultSlice(self, result, autoLevels=False, levels=None):
        if self._showWithNapariStorm(result):
            # The point cloud is the display; the preview histogram it would
            # otherwise fall back to would only sit behind it.
            self._transposeOrder = []
            self._displayedAxisLabels = []
            self._widget.clearImage()
            return

        # display_layers = list(DisplayLayerSpec_0, DisplayLayerSpec_1, ...)
        # DisplayLayerSpec_i(name=..., data=array(...))
        display_layers = result.display_layers() if hasattr(result, "display_layers") else []
        if display_layers:
            if hasattr(result, "applyDisplayLayerSettings"):
                display_layers = result.applyDisplayLayerSettings(display_layers)
            mode = self._processingViewMode(result)
            display_layers, layer_order = self._transposeDisplayLayers(
                result, display_layers, mode
            )
            self._transposeOrder = list(layer_order)
            # Display layers carry their own per-layer contrast and have no
            # shared sliced axis, so there is no "Base" axis to rescale against.
            self._displayedAxisLabels = list(display_layers[0].axis_labels)
            first = display_layers[0]
            self._widget.setDisplayLayers(
                display_layers,
                identity=self._resultIdentity(
                    result,
                    first.data,
                    list(first.axis_labels),
                    first.axis_scales,
                    first.scale_unit,
                    view_mode=mode.name,
                ),
            )
            return

        if result_kind(result) in ("curve", "table"):
            self._transposeOrder = []
            self._displayedAxisLabels = []
            self._widget.clearImage()
            return

        # A lazy view over a file it does not own (a workflow's view-only
        # reconstruction, a duplicate of one) is read here, the same moment the
        # GUI's own loader reads a file it opens: the handle it borrows closes
        # when another file is loaded. One that owns its files -- a time lapse
        # -- reaches napari as a dask array instead, which transposes without
        # reading and is read a plane at a time as the sliders move.
        data = display_array(result.data)
        if getattr(data, "ndim", 0) < 2:
            # napari's image layer holds planes. A 1D result pushed into it
            # leaves the layer's transform and units disagreeing about the
            # rank, and every later cursor move or redraw raises from inside
            # napari. Show nothing rather than a viewer that cannot draw.
            self._logger.warning(
                "Result %r has %d axis/axes and cannot be shown as an image",
                getattr(result, "name", result), getattr(data, "ndim", 0),
            )
            self._transposeOrder = []
            self._displayedAxisLabels = []
            self._widget.clearImage()
            return
        mode = self._processingViewMode(result)
        im = data.transpose(*mode.transpose)
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
        # Sizes only: the array may be lazy, and reading it to learn its shape
        # read the whole of it.
        shape = tuple(
            int(size) for size in (
                data.shape if hasattr(data, "shape") else np.shape(data)
            )
        )
        labels = [str(label) for label in axis_labels]
        scales = list(axis_scales or [1.0] * len(shape))
        axes = [
            {
                "label": labels[axis] if axis < len(labels) else str(axis),
                "size": int(shape[axis]),
                "scale": float(scales[axis]) if axis < len(scales) else 1.0,
                "unit": scale_unit,
            }
            for axis in range(len(shape))
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

    @staticmethod
    def _transposeDisplayLayers(result, layers, mode):
        """Apply a result view-mode permutation to image display layers.

        Display layers may omit semantic axes from ``result.data`` (MoNaLISA
        removes ``Base``).  Derive the layer permutation by axis label rather
        than assuming the layer rank matches the parent result rank.
        """
        result_labels = list(getattr(result, "axis_labels", ()))
        try:
            ordered_labels = [result_labels[i] for i in mode.transpose]
        except (IndexError, TypeError):
            ordered_labels = result_labels

        first_order = None
        transposed = []
        for spec in layers:
            labels = list(getattr(spec, "axis_labels", ()))
            kind = str(getattr(spec, "kind", "image") or "image")
            order = tuple(range(len(labels)))
            if kind in ("image", "labels") and len(labels) >= 2:
                desired = [label for label in ordered_labels if label in labels]
                extra = [label for label in labels if label not in desired]
                # A display layer may introduce a slider-only axis absent from
                # the canonical result (e.g. the lattice pinhole stack). Keep
                # such axes in front of the final two displayed spatial axes.
                if extra and len(desired) >= 2:
                    desired = [*desired[:-2], *extra, *desired[-2:]]
                else:
                    desired.extend(extra)
                if (
                    len(desired) == len(labels)
                    and len(set(labels)) == len(labels)
                    and set(desired) == set(labels)
                ):
                    order = tuple(labels.index(label) for label in desired)

            if first_order is None:
                first_order = order
            if order == tuple(range(len(labels))):
                transposed.append(spec)
                continue

            data = spec.data.transpose(*order)
            scales = (
                [spec.axis_scales[i] for i in order]
                if spec.axis_scales is not None else None
            )
            transposed.append(replace(
                spec,
                data=data,
                axis_labels=[labels[i] for i in order],
                axis_scales=scales,
            ))

        return transposed, (first_order or ())

    @staticmethod
    def _viewModesForResult(result):
        """Declared view modes, with orthogonal XYZ views for semantic volumes.

        A few image results (notably the lattice reconstructor and files opened
        through View only) expose only the default identity mode even though
        their axes are explicitly labelled Z/Y/X.  Hiding Bottom/Left for those
        volumes is an implementation accident, not a meaningful limitation.
        Strictly 2-D Y/X results still expose Standard only.
        """
        modes = list(getattr(result, "view_modes", ()) or ())
        labels = list(getattr(result, "axis_labels", ()) or ())
        if (
            len(modes) == 1
            and str(modes[0].name).lower() == "standard"
            and all(axis in labels for axis in ("X", "Y", "Z"))
        ):
            leading = [i for i, label in enumerate(labels) if label not in ("X", "Y", "Z")]
            x, y, z = (labels.index(axis) for axis in ("X", "Y", "Z"))
            return [
                ViewMode("Standard", tuple([*leading, z, y, x])),
                ViewMode("Bottom", tuple([*leading, y, z, x])),
                ViewMode("Left", tuple([*leading, x, z, y])),
            ]
        return modes

    def _processingViewMode(self, result):
        modes = self._viewModesForResult(result)
        view_name = self._widget.getViewName()
        for mode in modes:
            if mode.name == view_name:
                return mode
        return modes[0]

    @staticmethod
    def _resultViewKey(result):
        # Viewer interaction state belongs to this in-memory reconstruction
        # object, not to its provenance/content identity.  Some result types
        # intentionally derive a stable result_uid when the same source is
        # reconstructed again; using that UID here made a genuinely new
        # reconstruction inherit an older object's rolled axes/view choice.
        return result

    def _viewerState(self, result):
        if result is None:
            return {}
        states = self.__dict__.setdefault("_viewerStateByResult", {})
        return states.setdefault(self._resultViewKey(result), {})

    def _currentDimensionOrder(self):
        getter = getattr(self._widget, "getDimensionOrder", None)
        if not callable(getter):
            return None
        try:
            return tuple(getter())
        except Exception:
            return None

    def _currentDisplayedPlaneAxes(self):
        getter = getattr(self._widget, "getDisplayedPlaneAxes", None)
        if not callable(getter):
            return ()
        try:
            return tuple(getter())
        except Exception:
            return ()

    def _currentCameraState(self):
        getter = getattr(self._widget, "getCameraState", None)
        if not callable(getter):
            return None
        try:
            return getter()
        except Exception:
            return None

    def _restoreCameraState(self, state):
        setter = getattr(self._widget, "setCameraState", None)
        if not callable(setter) or state is None:
            return
        try:
            setter(state)
        except Exception as exc:
            self._logger.debug("Could not restore camera state: %s", exc)

    def _rememberViewerState(self, result):
        if result is None:
            return
        self._rememberSliceState(result)
        state = self._viewerState(result)
        try:
            state["view_mode"] = self._widget.getViewName()
        except Exception:
            pass
        order = self._currentDimensionOrder()
        if order is not None:
            state["dimension_order"] = order
        plane = self._currentDisplayedPlaneAxes()
        camera = self._currentCameraState()
        if plane:
            state["display_plane"] = plane
            if camera is not None:
                self._storeCameraState(result, plane, camera, updateShared=True)

    @staticmethod
    def _cameraPlaneKey(plane):
        """Canonical physical viewing plane used for shared camera memory."""
        return ReconstructionViewController._slicePlaneKey(plane)

    def _zoomLockedPerResult(self):
        getter = getattr(self._widget, "isZoomLockedPerResult", None)
        if not callable(getter):
            return False
        try:
            return bool(getter())
        except Exception:
            return False

    def _storeCameraState(self, result, plane, camera, *, updateShared=True):
        """Record camera in both result-local and optional shared stores."""
        if result is None or camera is None:
            return
        plane_key = self._cameraPlaneKey(plane)
        if plane_key is None:
            return
        state = self._viewerState(result)
        state.setdefault("camera_by_plane", {})[plane_key] = camera
        if updateShared:
            self.__dict__.setdefault("_sharedCameraStateByPlane", {})[plane_key] = camera

    def _zoomLockChanged(self, _locked):
        """Anchor both camera stores to the framing visible at toggle time."""
        result = self._widget.getCurrentItemData()
        if result is None:
            return
        plane = self._currentDisplayedPlaneAxes()
        camera = self._currentCameraState()
        if plane and camera is not None:
            self._storeCameraState(result, plane, camera, updateShared=True)

    @staticmethod
    def _slicePlaneKey(plane):
        """Canonical physical plane key, independent of on-screen axis order."""
        spatial = []
        for label in plane or ():
            axis = str(label).upper()
            if axis in ("X", "Y", "Z") and axis not in spatial:
                spatial.append(axis)
        if len(spatial) != 2:
            return None
        rank = {"X": 0, "Y": 1, "Z": 2}
        return tuple(sorted(spatial, key=rank.__getitem__))

    def _currentSpatialSliceState(self):
        """Discrete spatial slider indices not currently drawn on the canvas."""
        getter = getattr(self._widget, "getDimensionStepState", None)
        if not callable(getter):
            return {}
        try:
            steps = getter() or {}
        except Exception:
            return {}
        plane = {str(label).upper() for label in self._currentDisplayedPlaneAxes()}
        return {
            str(label).upper(): int(step)
            for label, step in steps.items()
            if str(label).upper() in ("X", "Y", "Z")
            and str(label).upper() not in plane
        }

    def _currentSpatialSlicePositionState(self):
        """World positions of spatial slider axes not drawn on the canvas."""
        getter = getattr(self._widget, "getDimensionPositionState", None)
        if not callable(getter):
            return {}
        try:
            positions = getter() or {}
        except Exception:
            return {}
        plane = {str(label).upper() for label in self._currentDisplayedPlaneAxes()}
        return {
            str(label).upper(): float(position)
            for label, position in positions.items()
            if str(label).upper() in ("X", "Y", "Z")
            and str(label).upper() not in plane
        }

    def _sliceLockedPerResult(self):
        getter = getattr(self._widget, "isSliceLockedPerResult", None)
        if not callable(getter):
            return False
        try:
            return bool(getter())
        except Exception:
            return False

    def _rememberSliceState(self, result, *, updateShared=True):
        """Remember exact local index plus the shared physical position.

        The two stores intentionally have different semantics. The result-local
        store backs ``Lock slice per result`` and therefore remembers exactly
        which discrete slice was selected. The shared store is a world
        coordinate so a denser/sparser reconstruction can map to the same
        physical sample position.
        """
        if result is None or self.__dict__.get("_restoringSliceState", False):
            return
        plane_key = self._slicePlaneKey(self._currentDisplayedPlaneAxes())
        slice_state = self._currentSpatialSliceState()
        if plane_key is None or not slice_state:
            return
        state = self._viewerState(result)
        state.setdefault("slice_by_plane", {})[plane_key] = dict(slice_state)

        if updateShared:
            positions = self._currentSpatialSlicePositionState()
            if positions:
                shared = self.__dict__.setdefault(
                    "_sharedSlicePositionByPlane", {}
                )
                shared.setdefault(plane_key, {}).update(positions)

    def _restoreSliceState(self, result, *, sourceSpatialAxes=()):
        """Restore only the slice state that ImProcess actually owns.

        In normal comparison mode, a compatible transition such as YZ A -> YZ
        B is left untouched: napari already carries the current world point
        through a scale change, which correctly maps different multifoci
        densities onto different discrete X indices. ImProcess intervenes only
        when that sliced semantic axis was absent immediately beforehand (for
        example YZ -> 2-D XY -> YZ), restoring the remembered world position.

        Per-result lock is intentionally different: it restores the exact
        discrete index saved for that result/view.
        """
        if result is None:
            return
        plane_key = self._slicePlaneKey(self._currentDisplayedPlaneAxes())
        if plane_key is None:
            return

        state = self._viewerState(result)
        per_result = state.get("slice_by_plane", {}).get(plane_key, {})
        if self._sliceLockedPerResult():
            if not per_result:
                return
            setter = getattr(self._widget, "setDimensionStepState", None)
            if not callable(setter):
                return
            try:
                setter(per_result)
            except Exception as exc:
                self._logger.debug(
                    "Could not restore per-result spatial slice state: %s", exc
                )
            return

        current_axes = set(self._currentSpatialSliceState())
        reappeared_axes = current_axes.difference(
            str(axis).upper() for axis in (sourceSpatialAxes or ())
        )
        if not reappeared_axes:
            # Most important path: same semantic sliced axis before/after. Do
            # not replace napari's physically meaningful world-coordinate
            # mapping with one of our own indices.
            return

        shared = self.__dict__.setdefault(
            "_sharedSlicePositionByPlane", {}
        ).get(plane_key, {})
        desired = {
            axis: shared[axis]
            for axis in reappeared_axes
            if axis in shared
        }
        if not desired:
            return
        setter = getattr(self._widget, "setDimensionPositionState", None)
        if not callable(setter):
            return
        try:
            setter(desired)
        except Exception as exc:
            self._logger.debug(
                "Could not restore shared spatial position: %s", exc
            )

    def _sliceLockChanged(self, _locked):
        """Anchor shared mode to the slice visible when the policy changes."""
        result = self._widget.getCurrentItemData()
        if result is not None:
            self._rememberSliceState(result, updateShared=True)

    def _restoreDimensionOrder(self, result, order):
        setter = getattr(self._widget, "setDimensionOrder", None)
        if callable(setter):
            try:
                # None means a first-time result: force natural order instead
                # of inheriting whatever the previous result left in napari.
                setter(order)
            except Exception as exc:
                self._logger.debug("Could not restore dimension order: %s", exc)
        current = self._currentDimensionOrder()
        if current is not None:
            self._viewerState(result)["dimension_order"] = current

    def _initialViewName(self, item):
        chooser = getattr(item, "initial_view_mode_name", None)
        if callable(chooser):
            try:
                preferred = chooser()
                if preferred:
                    return str(preferred)
            except Exception as exc:
                self._logger.debug("Could not determine initial view mode: %s", exc)
        modes = self._viewModesForResult(item)
        return modes[0].name if modes else None

    def _syncViewModes(self, item):
        if hasattr(item, "view_modes"):
            state = self._viewerState(item)
            modes = self._viewModesForResult(item)
            selected = state.get("view_mode") or self._initialViewName(item)
            self._widget.setViewModes(modes, selectedName=selected)
            state["view_mode"] = self._widget.getViewName()
        else:
            self._widget.setViewModes(None)

    def _viewChanged(self):
        """Remember a named view choice and restore that plane's camera policy.

        Raw napari dimension rolls remain result-local. Named plane changes
        use the same shared/per-result camera memory as result selection; an
        unseen plane is fitted once and then remembered.
        """
        result = self._widget.getCurrentItemData()
        if result is not None:
            self._rememberSliceState(result)
            state = self._viewerState(result)
            old_plane = self._currentDisplayedPlaneAxes()
            old_camera = self._currentCameraState()
            if old_plane and old_camera is not None:
                self._storeCameraState(
                    result, old_plane, old_camera, updateShared=True
                )
            state["view_mode"] = self._widget.getViewName()
            state.pop("dimension_order", None)
        self.fullUpdate(levels=None, resetView=True, dimensionOrder=None)

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
        result = self._widget.getCurrentItemData()
        if result is not None:
            self._rememberSliceState(result)

    def updateLevelsRange(self, base=None):
        """Scale the contrast to the data currently on screen.

        The slider spans the data's full range; the view clips the outermost
        1% at each end, which is the rule a result's own display layers
        already use, so the same picture looks the same whichever path renders
        it. Both go through the contrast helpers rather than a plain
        ``min()``/``max()``: this runs whenever a result is shown for the first
        time, not only for the first result of a session, so it meets lazy
        HDF5/Zarr arrays it must not materialize in full, and results holding
        NaN, whose raw minimum is NaN and renders as nothing at all.
        """
        im = self._widget.getImage()
        baseAxisIndex = self._baseAxisIndex()

        if baseAxisIndex is not None:
            # Components of a Base axis have their own ranges, so the one on
            # screen is what gets measured rather than the whole stack.
            if base is None:
                base = (self._axisStep[baseAxisIndex]
                        if baseAxisIndex < len(self._axisStep) else 0)
            indexForImage = [slice(None) for _ in range(im.ndim)]
            indexForImage[baseAxisIndex] = base
            im = im[tuple(indexForImage)]

        self._widget.setImageDisplayLevelsRange(*finite_range(im))
        self._widget.setImageDisplayLevels(
            *auto_levels(im, saturated_percent=AUTO_SATURATED_PERCENT)
        )

    def updateRecon(self):
        reconObj = self._widget.getCurrentItemData()
        if reconObj is not None and hasattr(reconObj, "updateImages"):
            self._rememberSliceState(reconObj)
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
        metadata = {}
        if hasattr(self._widget, "getActiveImageLayerMetadata"):
            metadata = self._widget.getActiveImageLayerMetadata()
        self._storeDisplayLevels(result, metadata, levels)

    def _storeDisplayLevels(self, result, layerMetadata, levels) -> None:
        """File ``levels`` under whichever part of ``result`` the layer shows.

        A layer naming a component belongs to a result that renders several of
        them and keeps their contrast apart; a layer naming none is the whole
        result. The layer's own metadata is what decides, so a levels change
        reported by any layer lands where that layer's own render will read it
        back from.
        """
        if result is None or levels is None:
            return
        layer_id = self._displayLayerId(result, layerMetadata)
        if layer_id is not None and hasattr(result, "setDisplayLayerLevels"):
            result.setDisplayLayerLevels(layer_id, levels)
        elif hasattr(result, "setDispLevels"):
            result.setDispLevels(levels)

    def _storeActiveColormap(self, result, colormap: str) -> None:
        if result is None:
            return
        layer_id = self._activeDisplayLayerId(result)
        # (resolved from the active layer; colormap has only ever had one)
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
        # Levels for every layer, not just the active one. Saving a layer's
        # colormap while dropping its contrast is the sort of split that reads
        # as "the viewer forgot" from the outside.
        levels = state.get("display_levels")
        if levels is not None and metadata.get("component") \
                and hasattr(result, "setDisplayLayerLevels"):
            result.setDisplayLayerLevels(layer_id, levels)

    def _activeDisplayLayerId(self, result):
        metadata = {}
        if hasattr(self._widget, "getActiveImageLayerMetadata"):
            metadata = self._widget.getActiveImageLayerMetadata()
        return self._displayLayerId(result, metadata)

    @staticmethod
    def _displayLayerId(result, layerMetadata):
        """The component id a layer's settings belong under, or None.

        None means "the result as a whole", which is both the single-image
        case and the case where the layer turns out to belong to a different
        result than the one being written to.
        """
        if result is None:
            return None
        metadata = dict(layerMetadata or {})
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

    def resultReplaced(self, old, new, displayName):
        """Put ``new`` in ``old``'s list entry; redraw if it is on screen.

        A processor whose panel asked for in-place refinement (the SMLM
        filter with *Copy* off) publishes this way. An ``old`` that is no
        longer listed makes ``new`` an ordinary new entry.
        """
        if new is None:
            return
        name = displayName or getattr(new, 'name', '') or 'result'
        replacer = getattr(self._widget, 'replaceData', None)
        if not callable(replacer) or not replacer(old, new, name):
            self.resultProduced(new, name)
            return
        if self._widget.getCurrentItemData() is new:
            # The entry on screen now holds other data: render it, and tell
            # everyone following the current result that it changed.
            self.listItemChanged()
        self._resultsChanged()

    def liveResultUpdated(self, result):
        """Queue a live reconstruction result for rendering.

        Coalescing: a fast streaming session can emit several results while one
        full redraw is still running. We keep only the newest and render it via
        a 0-delay timer once the event loop is free, so the viewer always shows
        near-latest data and the GUI never accumulates a backlog.
        """
        if result is None:
            return

        # Registered here rather than in the render path: coalescing keeps
        # only the newest result, so a result that never reaches a render
        # pass must still be known as live.
        uid = getattr(result, "result_uid", None)
        if uid:
            self._liveResultUids.add(uid)

        self._pendingLiveResult = result
        if not self._liveRenderScheduled:
            self._liveRenderScheduled = True
            QtCore.QTimer.singleShot(0, self._renderPendingLiveResult)

    @QtCore.Slot(str, str)
    def saveLiveResult(self, name: str, path: str) -> None:
        """Write the named live data object to ``path`` as a TIFF.

        The viewer holds the accumulated buffer, so it is what can write it.

        Any pending live render is flushed first: :meth:`liveResultUpdated`
        only *schedules* the newest result, and a run's final result is emitted
        immediately before ``sigFinished`` -- so without this the file could be
        written one update stale.

        The name is checked rather than trusted: a run whose entry the user has
        since deleted or replaced is skipped, instead of writing whatever object
        happens to be live now.
        """
        if self._pendingLiveResult is not None:
            self._renderPendingLiveResult()

        result = self._liveItem.data(1) if self._liveItemIsAlive() else None
        if result is None or getattr(result, 'name', None) != name:
            self._logger.warning(
                "Not saving %r: it is no longer the live data object", name
            )
            return

        try:
            result.save(Path(path), "tiff")
        except Exception as exc:
            self._logger.error("Could not save %r to %s: %s", name, path, exc)
            return
        self._logger.info("Saved %s", path)

    @QtCore.Slot(int, object)
    def liveTimepointUpdated(self, index: int, plane) -> None:
        """Write one reconstructed timepoint into the live result's own buffer.

        The result the viewer holds owns its array (the session handed over a
        copy), so writing into it here is a GUI-thread-only operation -- the
        process thread never touches this memory.

        Unlike :meth:`liveResultUpdated`, the data is applied **immediately**
        rather than coalesced: successive planes carry different timepoints, so
        keeping only the newest would silently drop reconstructed data. Only
        the redraw is coalesced, which is the expensive part.
        """
        if not self._liveItemIsAlive():
            return
        result = self._liveItem.data(1)
        if result is None or plane is None:
            return

        try:
            axis = list(result.axis_labels).index("T")
        except (AttributeError, ValueError):
            self._logger.debug("liveTimepointUpdated: result has no T axis; ignoring")
            return

        target = [slice(None)] * result.data.ndim
        target[axis] = slice(index, index + 1)
        try:
            result.data[tuple(target)] = plane
        except Exception as exc:
            self._logger.debug(
                "liveTimepointUpdated: could not write timepoint %s (%s)", index, exc
            )
            return

        self._pendingLiveResult = result
        if not self._liveRenderScheduled:
            self._liveRenderScheduled = True
            QtCore.QTimer.singleShot(0, self._renderPendingLiveResult)

    def _liveItemIsAlive(self) -> bool:
        """Whether the live run's entry still exists in the list.

        The user can delete entries while a run streams -- ``removeRecon``
        takes the item out of the list, ``removeAllRecon`` deletes it outright
        (leaving a dangling wrapper that raises on access). Either way the run
        should just create a fresh entry on its next result rather than fail.
        """
        if self._liveItem is None:
            return False
        try:
            return self._widget.reconList.row(self._liveItem) >= 0
        except RuntimeError:  # underlying C++ item already deleted
            return False

    def _renderPendingLiveResult(self):
        """Render the newest live result into the data object owned by its job.

        Every job follows the same two-step sequence: its first result creates
        one data object named after the job, and every later result updates
        that same object. The job is identified by the result's name (the
        timelapse folder), which is unique per job -- so no de-duplicating
        suffix is needed and none is added.
        """
        self._liveRenderScheduled = False
        result = self._pendingLiveResult
        self._pendingLiveResult = None
        if result is None:
            return

        name = getattr(result, 'name', '') or 'Live'
        if not self._liveItemIsAlive() or self._liveName != name:
            self._liveItem = self._widget.addNamedData(result, name)
            self._liveName = name
            self._liveEstablished = False
            self._liveLatestTimepoint = None
            return

        # Update this job's own entry, not whatever is selected -- otherwise a
        # click elsewhere mid-run would overwrite that other entry's data.
        self._liveItem.setData(1, result)
        if self._widget.reconList.currentItem() is not self._liveItem:
            # The user is inspecting a different entry; leave their view alone.
            # Re-selecting the live entry redraws it via sigItemSelected.
            return

        # Fast path: after the first full render the layers exist, so an update
        # is just an in-place pixel swap -- no layer rebuild. Calling
        # tryFastLiveUpdate IS the update; it reports False (having changed
        # nothing) if the layer structure no longer matches, and we rebuild.
        rendered = (
            self._liveEstablished
            and self._widget.tryFastLiveUpdate(result, self._transposeOrder)
        )
        if rendered:
            self._advanceLiveTimeSlider()
            return

        self.fullUpdate(levels=None)
        self._liveEstablished = True
        self._advanceLiveTimeSlider()

    @QtCore.Slot(int)
    def _onLiveTimepointDone(self, timepoint: int) -> None:
        self._liveLatestTimepoint = timepoint

    def _advanceLiveTimeSlider(self) -> None:
        """Move the viewer's timepoint slider to the newest completed stack."""
        t = self._liveLatestTimepoint
        if t is None:
            return
        labels = self._displayedAxisLabels
        for name in ("T", "Timepoints", "Time"):
            if name in labels:
                self._widget.moveDimStep(labels.index(name), t)
                return

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
