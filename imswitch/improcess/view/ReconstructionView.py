import warnings

import numpy as np
from qtpy import QtCore, QtWidgets

from imswitch.imcommon.model import initLogger
from imswitch.imcommon.view.guitools import naparitools
from imswitch.improcess.model.contrast import safe_display_levels
from imswitch.improcess.model.lazy_array import is_dask_array
from . import guitools
from .NapariStormDisplay import NapariStormDisplay


def _spec_kind(spec) -> str:
    return str(getattr(spec, "kind", "image") or "image")


def _spec_role(spec) -> str:
    return str(getattr(spec, "role", "primary") or "primary")


def _spec_component(spec) -> str:
    component = getattr(spec, "component", None)
    if component:
        return str(component)
    metadata = getattr(spec, "metadata", None) or {}
    if metadata.get("component"):
        return str(metadata["component"])
    return str(getattr(spec, "name", "layer"))


#: Keys carrying spatial provenance, written onto every rendered layer.
IDENTITY_KEYS = (
    "result_uid",
    "dataset_uid",
    "coordinate_space_uid",
    "identity_kind",
    "lineage",
    "plane_axes",
    "view_mode",
    "axes",
)


def _applyIdentityMetadata(layer, identity, *, overrides=None) -> None:
    """Write spatial provenance onto a layer, or clear it when unknown.

    Cleared rather than left stale when ``identity`` is None: metadata from a
    previously displayed result would otherwise claim this image is something
    it is not, which is worse than having no provenance at all.
    """
    if layer is None:
        return
    if not identity:
        for key in IDENTITY_KEYS:
            layer.metadata.pop(key, None)
        return
    for key in IDENTITY_KEYS:
        if key in identity:
            layer.metadata[key] = identity[key]
    for key, value in (overrides or {}).items():
        if value is not None:
            layer.metadata[key] = value


class ReconstructionView(QtWidgets.QFrame):
    """ Frame for showing the reconstructed image"""

    # Signals
    sigItemSelected = QtCore.Signal()
    sigSelectionChanged = QtCore.Signal()
    sigResultsRemoved = QtCore.Signal()
    sigAxisStepChanged = QtCore.Signal(tuple)
    sigViewChanged = QtCore.Signal()
    #: (layer metadata, (min, max)) -- a rendered layer's contrast changed,
    #: however it was changed: our toolbar, napari's own slider, or a render.
    sigImageLevelsChanged = QtCore.Signal(object, object)

    # Methods
    def __init__(
        self,
        *args,
        showLayerControls: bool = True,
        useNapariStormViewer: bool = False,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._logger = initLogger(self)

        # Image Widget
        naparitools.addNapariGrayclipColormap()
        self.napariViewer = naparitools.EmbeddedNapari()
        self.napariViewer.dims.events.connect(self.dimsChanged)
        naparitools.NapariUpdateLevelsWidget.addToViewer(self.napariViewer)

        self.imgLayer = self.napariViewer.add_image(
            np.zeros((1, 1)), rgb=False, name='Reconstruction', colormap='grayclip', protected=True
        )
        self._watchLevels(self.imgLayer)
        self.setNapariLayerControlsVisible(showLayerControls)
        self._displayLayers = []
        # Optional GPU point-cloud backend for localization results. Retained
        # across selections, so it deliberately sits outside _displayLayers,
        # which is cleared and rebuilt on every result change. None unless the
        # config asks for it; the adapter then gates itself again on the
        # optional package actually being installed.
        self.napariStormDisplay = (
            NapariStormDisplay(self.napariViewer) if useNapariStormViewer else None
        )
        # Tracks which managed/protected layer is the selected result's canonical
        # output (may be a labels/points layer, not imgLayer) so the toolbar and
        # active-image accessors can target it by role rather than by identity.
        self._primaryLayer = self.imgLayer
        self._primaryComponent = None

        # Button group for choosing view
        self.chooseViewGroup = QtWidgets.QButtonGroup()
        self.chooseViewBox = QtWidgets.QGroupBox('Choose view')
        self.viewLayout = QtWidgets.QVBoxLayout()

        self.standardView = QtWidgets.QRadioButton('Standard view')
        self.standardView.viewName = 'standard'
        self.chooseViewGroup.addButton(self.standardView)
        self.viewLayout.addWidget(self.standardView)

        self.bottomView = QtWidgets.QRadioButton('Bottom side view')
        self.bottomView.viewName = 'bottom'
        self.chooseViewGroup.addButton(self.bottomView)
        self.viewLayout.addWidget(self.bottomView)

        self.leftView = QtWidgets.QRadioButton('Left side view')
        self.leftView.viewName = 'left'
        self.chooseViewGroup.addButton(self.leftView)
        self.viewLayout.addWidget(self.leftView)

        self.chooseViewBox.setLayout(self.viewLayout)
        self.chooseViewGroup.buttonClicked.connect(self.sigViewChanged)

        # List for storing sevral data sets
        self.reconList = QtWidgets.QListWidget()
        self.reconList.currentItemChanged.connect(self.sigItemSelected)
        # Selection can change without the current item moving (Ctrl+A,
        # Ctrl-click deselect), and multi-input toolbar actions gate on the
        # selection — so it needs its own signal.
        self.reconList.itemSelectionChanged.connect(self.sigSelectionChanged)
        self.reconList.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        removeReconBtn = guitools.BetterPushButton('Remove current')
        removeReconBtn.clicked.connect(self.removeRecon)
        removeAllReconBtn = guitools.BetterPushButton('Remove all')
        removeAllReconBtn.clicked.connect(self.removeAllRecon)

        # Set initial states
        self.standardView.setChecked(True)
        self._defaultViewLabels = [
            (self.standardView, 'Standard view', 'standard'),
            (self.bottomView, 'Bottom side view', 'bottom'),
            (self.leftView, 'Left side view', 'left'),
        ]

        # --- Left pane: napari viewer + the view-mode chooser --------------
        leftPane = QtWidgets.QWidget()
        leftLayout = QtWidgets.QGridLayout(leftPane)
        leftLayout.setContentsMargins(0, 0, 0, 0)
        leftLayout.addWidget(self.napariViewer.get_widget(), 0, 0, 4, 1)
        leftLayout.addWidget(self.chooseViewBox, 0, 1, 1, 2)
        leftLayout.setRowStretch(1, 1)
        leftLayout.setColumnStretch(0, 100)
        leftLayout.setColumnStretch(2, 5)

        # --- Right pane: reconstruction list + its two buttons -------------
        rightPane = QtWidgets.QWidget()
        rightLayout = QtWidgets.QVBoxLayout(rightPane)
        rightLayout.setContentsMargins(0, 0, 0, 0)
        rightLayout.setSpacing(2)
        rightLayout.addWidget(self.reconList, 1)
        rightLayout.addWidget(removeReconBtn)
        rightLayout.addWidget(removeAllReconBtn)
        # Keep the pane from being squeezed below readable width while still
        # allowing the user to drag it down to the handle (collapsed state).
        rightPane.setMinimumWidth(0)
        self.reconList.setMinimumWidth(80)
        self._reconListPane = rightPane

        # --- QSplitter: drag the handle left/right to resize the list, ----
        # drag fully right to snap it shut to a thin band that can be
        # dragged back to expand.
        self._reconSplitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self._reconSplitter.addWidget(leftPane)
        self._reconSplitter.addWidget(rightPane)
        self._reconSplitter.setStretchFactor(0, 1)
        self._reconSplitter.setStretchFactor(1, 0)
        self._reconSplitter.setCollapsible(0, False)
        self._reconSplitter.setCollapsible(1, True)
        self._reconSplitter.setHandleWidth(8)
        # Initial size hint: ~80% viewer / 20% list. setSizes uses pixels
        # but Qt scales them to the widget's actual width on first show.
        self._reconSplitter.setSizes([800, 200])

        outerLayout = QtWidgets.QVBoxLayout(self)
        outerLayout.setContentsMargins(0, 0, 0, 0)
        outerLayout.addWidget(self._reconSplitter)

    def setNapariLayerControlsVisible(self, visible: bool) -> None:
        """Show/hide napari's built-in layer controls dock."""
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', FutureWarning)
                warnings.simplefilter('ignore', DeprecationWarning)
                qt_viewer = self.napariViewer.window.qt_viewer
            dock = getattr(qt_viewer, 'dockLayerControls', None)
            if dock is not None:
                dock.setVisible(bool(visible))
        except Exception as exc:
            self._logger.debug(
                "Could not set napari layer-controls visibility: %s",
                exc,
            )

    # --- Recon list pane controls -----------------------------------------

    def toggleReconListPane(self) -> None:
        """Collapse the recon-list pane to a thin band, or restore it.

        Bound to the ``View > Reconstructions list`` menu action.  Remembers
        the last expanded width so the user gets back the size they had.
        """
        sizes = self._reconSplitter.sizes()
        if len(sizes) < 2:
            return
        if sizes[1] == 0:
            restore_to = getattr(self, '_reconListLastSize', 0) or 200
            total = sum(sizes) or (restore_to + 800)
            self._reconSplitter.setSizes([total - restore_to, restore_to])
        else:
            self._reconListLastSize = sizes[1]
            total = sum(sizes)
            self._reconSplitter.setSizes([total, 0])

    def isReconListPaneCollapsed(self) -> bool:
        sizes = self._reconSplitter.sizes()
        return len(sizes) >= 2 and sizes[1] == 0

    def dimsChanged(self, event):
        if event.type == 'current_step':
            self.sigAxisStepChanged.emit(event.value)

    def addNewData(self, reconObj, name):
        ind = 0
        for i in range(self.reconList.count()):
            if name + '.' + str(ind) == self.reconList.item(i).data(0):
                ind += 1
        name = name + '.' + str(ind)

        listItem = QtWidgets.QListWidgetItem(name)
        listItem.setData(1, reconObj)
        self.reconList.addItem(listItem)
        self.reconList.setCurrentItem(listItem)

    def getCurrentItemIndex(self):
        currentItem = self.reconList.currentItem()
        if currentItem is None:
            return None
        return self.reconList.indexFromItem(currentItem).row()

    def getDataAtIndex(self, index):
        if index is None or index < 0 or index >= self.reconList.count():
            return None
        item = self.reconList.item(index)
        return item.data(1) if item is not None else None

    def getCurrentItemData(self):
        currentItem = self.reconList.currentItem()
        return currentItem.data(1) if currentItem is not None else None

    def getAllItemDatas(self):
        for i in range(self.reconList.count()):
            item = self.reconList.item(i)
            yield item.text(), item.data(1)

    def getSelectedItemDatas(self):
        for item in self.reconList.selectedItems():
            yield item.text(), item.data(1)

    def getViewName(self):
        return self.chooseViewGroup.checkedButton().viewName

    def setViewModes(self, viewModes):
        if not viewModes:
            for button, text, view_name in self._defaultViewLabels:
                button.setText(text)
                button.viewName = view_name
                button.setVisible(True)
            self.standardView.setChecked(True)
            return

        buttons = [self.standardView, self.bottomView, self.leftView]
        for index, button in enumerate(buttons):
            if index < len(viewModes):
                mode = viewModes[index]
                button.setText(mode.name)
                button.viewName = mode.name
                button.setVisible(True)
            else:
                button.setVisible(False)
        buttons[0].setChecked(True)

    def getImage(self):
        return self.imgLayer.data

    def setImage(self, im, axisLabels, axisScales=None, scaleUnit="px", colormap="grayclip",
                 name=None, identity=None):
        self._clearDisplayLayers()
        # A prior labels/points-primary result may have hidden imgLayer; a plain
        # image result restores it as the (visible) primary again.
        self.imgLayer.visible = True
        self._primaryLayer = self.imgLayer
        self._primaryComponent = None
        # Name the layer after the result (single source of truth for "current image").
        # When name is given, tools operating on the active layer will see this result's name.
        if name is not None:
            self.imgLayer.name = str(name)
        else:
            self.imgLayer.name = 'Reconstruction'
        self.imgLayer.colormap = colormap
        if not is_dask_array(im):
            # A dask array stays one: napari reads the plane on screen from it,
            # where np.asarray would read every plane of a lazy result first.
            im = np.asarray(im)
        if im.ndim < 2:
            # napari's image layer holds planes; a lower-rank array leaves its
            # transform and units disagreeing about the rank and every later
            # cursor move raises inside napari. Every caller should have
            # refused already; this is the last gate before the layer.
            self._logger.warning(
                "setImage: %r has shape %s and cannot be shown as an image",
                name or 'Reconstruction', im.shape,
            )
            self.clearImage()
            return
        old_ndim = self.imgLayer.data.ndim
        new_ndim = im.ndim
        if axisScales is None:
            axisScales = [1.0] * new_ndim

        self._logger.debug(
            "setImage: shape=%s  ndim %d→%d  labels=%s  scales=%s  unit=%s  name=%s",
            im.shape, old_ndim, new_ndim,
            list(axisLabels), [f"{s:.4g}" for s in axisScales], scaleUnit, name or 'Reconstruction',
        )

        self._patchLayerForNdimChange(self.imgLayer, old_ndim, new_ndim, "setImage")

        # Set data — fires set_data → _on_matrix_change (safe now)
        self.imgLayer.data = im

        # Set scale after data so layer.ndim is already correct
        try:
            self.imgLayer.scale = tuple(axisScales)
        except Exception as exc:
            self._logger.warning("setImage: could not set layer scale %s: %s", axisScales, exc)

        try:
            self.napariViewer.dims.axis_labels = tuple(axisLabels)
        except Exception as exc:
            self._logger.warning("setImage: could not set axis_labels %s: %s", axisLabels, exc)

        try:
            self.imgLayer.metadata["axis_labels"] = list(axisLabels)
            self.imgLayer.metadata["scale_unit"] = scaleUnit
            # Store source_result for per-result display settings persistence (levels, colormap).
            # Persistence code (_storeDisplayLayerState, _activeDisplayLayerId) compares this
            # against result.name to ensure settings round-trip correctly even after renaming.
            if name is not None:
                self.imgLayer.metadata["source_result"] = str(name)
            else:
                self.imgLayer.metadata.pop("source_result", None)
            # A single-image result has no components. Leaving the previous
            # result's component behind made this layer claim to be one, and
            # its display settings were then filed under a component name
            # nothing would ever look for again.
            self.imgLayer.metadata.pop("component", None)
            # Spatial provenance travels with the layer, beside the scale and
            # unit that were already written here, so anything measuring this
            # image can read what it is measuring from one object.
            _applyIdentityMetadata(self.imgLayer, identity)
            self.napariViewer.scale_bar.unit = "µm" if scaleUnit == "um" else scaleUnit
        except Exception as exc:
            self._logger.debug("setImage: could not set scale_bar unit: %s", exc)

    def setDisplayLayers(self, layerSpecs, identity=None):
        self._clearDisplayLayers()
        specs = []
        for spec in list(layerSpecs or []):
            data = spec.data
            ndim = int(getattr(data, "ndim", np.ndim(data)))
            if _spec_kind(spec) in ("image", "labels") and ndim < 2:
                self._logger.warning(
                    "setDisplayLayers: skipping %r (shape %s): an image layer needs two axes",
                    spec.name, tuple(getattr(data, "shape", ()) or ()),
                )
                continue
            specs.append(spec)
        if not specs:
            self.clearImage()
            return

        # The protected imgLayer is permanently an Image layer (a napari layer's
        # type is fixed at creation), so route the FIRST image-kind spec through
        # it (stable reference the contrast toolbar/active-image accessors rely
        # on) and render every other spec as a fresh managed layer of its
        # declared kind. When no spec is an image, imgLayer is hidden — a
        # labels/points-primary result renders purely through managed layers.
        img_spec_index = next(
            (i for i, spec in enumerate(specs)
             if _spec_kind(spec) == "image"),
            None,
        )
        self._primaryComponent = None
        self._primaryLayer = None

        for index, spec in enumerate(specs):
            if index == img_spec_index:
                layer = self._applyImageSpecToImgLayer(spec)
            else:
                layer = self._addManagedLayer(spec)
            # A display layer can sit on its own pixel grid, so its own
            # coordinate space wins over the parent result's.
            _applyIdentityMetadata(
                layer,
                identity,
                overrides={
                    'coordinate_space_uid': getattr(spec, 'coordinate_space_uid', None),
                    'component': _spec_component(spec),
                },
            )
            if layer is None:
                continue
            if _spec_role(spec) == "primary":
                self._primaryComponent = _spec_component(spec)
                self._primaryLayer = layer

        if img_spec_index is None:
            # No image to anchor the protected layer — keep it present but out
            # of the way; the primary is a managed labels/points/shapes layer.
            self.imgLayer.visible = False
            self.imgLayer.name = 'Reconstruction'
            self.imgLayer.metadata.pop("source_result", None)

        first = specs[0]
        try:
            self.napariViewer.dims.axis_labels = tuple(first.axis_labels)
        except Exception as exc:
            self._logger.warning("setDisplayLayers: could not set axis_labels %s: %s",
                                 first.axis_labels, exc)

        try:
            self.napariViewer.scale_bar.unit = (
                "µm" if first.scale_unit == "um" else first.scale_unit
            )
        except Exception as exc:
            self._logger.debug("setDisplayLayers: could not set scale_bar unit: %s", exc)

        # Re-activate the image anchor when there is one (keeps the contrast
        # toolbar targeting an intensity layer); otherwise activate the primary
        # managed layer so tools operate on the result the list shows.
        active = self.imgLayer if img_spec_index is not None else self._primaryLayer
        if active is not None:
            try:
                self.napariViewer.layers.selection.active = active
            except Exception as exc:
                self._logger.debug(
                    "setDisplayLayers: could not select active layer: %s", exc)

    def _applyImageSpecToImgLayer(self, spec):
        """Render an image-kind spec into the reused protected imgLayer."""
        data = np.asarray(spec.data)
        axisScales = spec.axis_scales if spec.axis_scales is not None else [1.0] * data.ndim
        metadata = dict(spec.metadata or {})
        metadata.setdefault("axis_labels", list(spec.axis_labels))
        metadata.setdefault("scale_unit", spec.scale_unit)
        metadata.setdefault("component", _spec_component(spec))

        layer = self.imgLayer
        old_ndim = layer.data.ndim
        layer.visible = True
        layer.name = spec.name
        layer.colormap = spec.colormap
        try:
            layer.rgb = bool(spec.rgb)
        except Exception:
            pass
        self._patchLayerForNdimChange(layer, old_ndim, data.ndim, "setDisplayLayers")
        layer.data = data
        layer.scale = tuple(axisScales)
        layer.metadata.update(metadata)
        layer.visible = bool(spec.visible)
        if spec.display_levels is not None:
            safe_levels = safe_display_levels(*spec.display_levels)
            layer.contrast_limits_range = safe_levels
            layer.contrast_limits = safe_levels
        return layer

    def _addManagedLayer(self, spec):
        """Add a fresh managed napari layer of the spec's declared kind.

        Image/context layers become ``add_image``; labels/points/shapes use the
        matching napari constructor. Managed layers are removable (unlike the
        protected imgLayer) and are tracked in ``_displayLayers`` so the next
        result render clears them.
        """
        kind = _spec_kind(spec)
        data = np.asarray(spec.data)
        axisScales = spec.axis_scales if spec.axis_scales is not None else [1.0] * data.ndim
        metadata = dict(spec.metadata or {})
        metadata.setdefault("axis_labels", list(spec.axis_labels))
        metadata.setdefault("scale_unit", spec.scale_unit)
        metadata.setdefault("component", _spec_component(spec))
        extra = dict(spec.layer_kwargs or {})

        try:
            if kind == "labels":
                layer = self.napariViewer.add_labels(
                    data.astype(np.int32, copy=False),
                    name=spec.name,
                    scale=tuple(axisScales),
                    metadata=metadata,
                    visible=bool(spec.visible),
                    **extra,
                )
            elif kind == "points":
                # points scale must match coordinate dimensionality (N, D).
                point_scale = (
                    tuple(axisScales)
                    if data.ndim == 2 and data.shape[1] == len(axisScales)
                    else None
                )
                layer = self.napariViewer.add_points(
                    data,
                    name=spec.name,
                    scale=point_scale,
                    metadata=metadata,
                    visible=bool(spec.visible),
                    **extra,
                )
            elif kind == "shapes":
                layer = self.napariViewer.add_shapes(
                    data,
                    name=spec.name,
                    scale=tuple(axisScales),
                    metadata=metadata,
                    visible=bool(spec.visible),
                    **extra,
                )
            else:  # "image" (context/overlay image) or unknown -> image
                layer = self.napariViewer.add_image(
                    data,
                    rgb=bool(spec.rgb),
                    name=spec.name,
                    colormap=spec.colormap,
                    scale=tuple(axisScales),
                    metadata=metadata,
                    visible=bool(spec.visible),
                    **extra,
                )
                if spec.display_levels is not None:
                    safe_levels = safe_display_levels(*spec.display_levels)
                    layer.contrast_limits_range = safe_levels
                    layer.contrast_limits = safe_levels
        except Exception as exc:
            self._logger.warning(
                "setDisplayLayers: could not add %s layer %r: %s",
                kind, spec.name, exc,
            )
            return None

        self._watchLevels(layer)
        self._displayLayers.append(layer)
        return layer

    def _patchLayerForNdimChange(self, layer, old_ndim: int, new_ndim: int, context: str) -> None:
        """Pre-size napari's vispy units tuple before changing layer dimensionality.

        VispyBaseLayer._world_to_layer_units_scale is created for the layer's
        original ndim and can lag behind when an existing image layer receives
        data with a different ndim. The set_data event then indexes that stale
        tuple with the new displayed dims and can raise IndexError. Patch it
        before assigning layer.data.
        """
        if old_ndim == new_ndim:
            return

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                canvas = self.napariViewer.window._qt_viewer.canvas
            vispy_layer = canvas.layer_to_visual[layer]
            old_wts = vispy_layer._world_to_layer_units_scale
            vispy_layer._world_to_layer_units_scale = (1,) * new_ndim
            self._logger.debug(
                "%s: patched vispy _world_to_layer_units_scale len %d -> len %d "
                "(was %s)",
                context, len(old_wts), new_ndim, old_wts,
            )
        except Exception as exc:
            self._logger.warning(
                "%s: could not patch vispy _world_to_layer_units_scale "
                "(ndim %d->%d): %s - IndexError may still fire in napari callback",
                context, old_ndim, new_ndim, exc,
            )

    def _watchLevels(self, layer) -> None:
        """Announce this layer's contrast whenever it changes.

        Contrast is changed from three places -- our toolbar, napari's own
        slider, and a render that applies a result's remembered levels -- and
        only the first of those used to be noticed. Listening to the layer
        itself catches all three, so what is on screen is recorded the moment
        it changes rather than at some later moment that has to be arranged.
        """
        events = getattr(getattr(layer, 'events', None), 'contrast_limits', None)
        if events is None:
            # Not silent: a layer nobody can watch is a layer whose contrast
            # is quietly not remembered, and that is the failure this exists
            # to end.
            self._logger.warning(
                "Layer %r has no contrast_limits event; contrast changes made "
                "on it cannot be remembered per result.",
                getattr(layer, 'name', layer),
            )
            return
        try:
            events.connect(
                lambda event, layer=layer: self._onLevelsChanged(layer)
            )
        except Exception as exc:
            self._logger.warning("Could not watch contrast limits: %s", exc)

    def _onLevelsChanged(self, layer) -> None:
        metadata = dict(getattr(layer, 'metadata', None) or {})
        levels = getattr(layer, 'contrast_limits', None)
        if levels is None:
            return
        self.sigImageLevelsChanged.emit(metadata, tuple(float(v) for v in levels))

    def _clearDisplayLayers(self):
        for layer in list(getattr(self, "_displayLayers", [])):
            try:
                self.napariViewer.layers.remove(layer)
            except Exception:
                pass
        self._displayLayers = []

    def clearImage(self):
        self._clearDisplayLayers()
        self.imgLayer.visible = True
        self.imgLayer.name = 'Reconstruction'
        self.imgLayer.metadata.pop("source_result", None)
        self.imgLayer.metadata.pop("component", None)
        self.imgLayer.data = np.zeros((1, 1))
        self._primaryLayer = self.imgLayer
        self._primaryComponent = None

    def getImageDisplayLevels(self):
        return self.imgLayer.contrast_limits

    def setImageDisplayLevels(self, minimum, maximum):
        self.imgLayer.contrast_limits = safe_display_levels(minimum, maximum)

    def setImageDisplayLevelsRange(self, minimum, maximum):
        self.imgLayer.contrast_limits_range = safe_display_levels(minimum, maximum)

    def getActiveImageLayer(self):
        """Return the active image-like Napari layer, falling back to imgLayer.

        "Image-like" means the data really is an array, not merely that the
        layer has the two attributes. A point-cloud layer has ``contrast_limits``
        *and* a ``data`` holding a tuple of geometry arrays, so attribute
        presence alone let it through to every contrast tool built on this
        accessor, where the tuple then failed whatever tried to take its min
        and max.
        """
        layer = None
        try:
            layer = self.napariViewer.layers.selection.active
        except Exception:
            layer = None
        if (
            layer is not None
            and hasattr(layer, "contrast_limits")
            and isinstance(getattr(layer, "data", None), np.ndarray)
        ):
            return layer
        return self.imgLayer

    def getActiveImage(self):
        return self.getActiveImageLayer().data

    def getActiveImageLayerMetadata(self):
        metadata = getattr(self.getActiveImageLayer(), "metadata", None)
        return dict(metadata or {})

    def getActiveImageLayerName(self) -> str:
        return str(getattr(self.getActiveImageLayer(), "name", ""))

    def getImageLayerStates(self) -> list[dict]:
        states = []
        for layer in self._imageLayers():
            layer_id = self._imageLayerId(layer)
            if layer_id is None:
                continue
            levels = getattr(layer, "contrast_limits", None)
            states.append(
                {
                    "id": layer_id,
                    "name": str(getattr(layer, "name", layer_id)),
                    "visible": bool(getattr(layer, "visible", True)),
                    "colormap": self._colormapName(layer),
                    # Every layer's contrast, not only the active one's. The
                    # two used to disagree: switching results remembered the
                    # colormap of each layer and the levels of one.
                    "display_levels": (
                        tuple(float(v) for v in levels)
                        if levels is not None else None
                    ),
                    "metadata": dict(getattr(layer, "metadata", {}) or {}),
                }
            )
        return states

    def getActiveImageDisplayLevels(self):
        return self.getActiveImageLayer().contrast_limits

    def setActiveImageDisplayLevels(self, minimum, maximum):
        self.getActiveImageLayer().contrast_limits = safe_display_levels(minimum, maximum)

    def getActiveImageColormap(self) -> str:
        return self._colormapName(self.getActiveImageLayer())

    def setActiveImageColormap(self, colormap: str) -> None:
        self.getActiveImageLayer().colormap = str(colormap)

    def setImageLayerVisible(self, layer_id: str, visible: bool) -> None:
        layer = self._imageLayerById(layer_id)
        if layer is not None:
            layer.visible = bool(visible)

    def setImageLayerColormap(self, layer_id: str, colormap: str) -> None:
        layer = self._imageLayerById(layer_id)
        if layer is not None:
            layer.colormap = str(colormap)

    def getActiveImageDisplayLevelsRange(self):
        layer = self.getActiveImageLayer()
        return getattr(layer, "contrast_limits_range", None)

    def setActiveImageDisplayLevelsRange(self, minimum, maximum):
        layer = self.getActiveImageLayer()
        if hasattr(layer, "contrast_limits_range"):
            layer.contrast_limits_range = safe_display_levels(minimum, maximum)

    def removeRecon(self):
        rows = sorted(
            {index.row() for index in self.reconList.selectedIndexes()},
            reverse=True,
        )
        if not rows and self.reconList.currentRow() >= 0:
            rows = [self.reconList.currentRow()]
        for row in rows:
            self.reconList.takeItem(row)
        # Explicit rather than relying on the selection signal: consumers
        # listing the loaded results must never keep offering a removed one.
        self.sigResultsRemoved.emit()

    def removeAllRecon(self):
        self.reconList.clear()
        self.sigResultsRemoved.emit()

    def resetView(self):
        self.napariViewer.reset_view()

    @staticmethod
    def _colormapName(layer) -> str:
        colormap = getattr(layer, "colormap", "grayclip")
        return str(getattr(colormap, "name", colormap))

    def _imageLayers(self) -> list:
        return [self.imgLayer, *list(getattr(self, "_displayLayers", []))]

    def _imageLayerById(self, layer_id: str):
        requested = str(layer_id)
        for layer in self._imageLayers():
            if self._imageLayerId(layer) == requested:
                return layer
        return None

    @staticmethod
    def _imageLayerId(layer):
        metadata = dict(getattr(layer, "metadata", {}) or {})
        component = metadata.get("component")
        if component:
            return str(component)
        name = getattr(layer, "name", None)
        return str(name) if name else None


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
