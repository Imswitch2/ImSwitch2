import numpy as np
from qtpy import QtCore, QtWidgets

from imswitch.imcommon.model import shortcut
from imswitch.imcommon.view.guitools import naparitools


class ImageWidget(QtWidgets.QWidget):
    """ Widget containing viewbox that displays the new detector frames. """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        naparitools.addNapariGrayclipColormap()
        self.napariViewer = naparitools.EmbeddedNapari()
        self.updateLevelsWidget = naparitools.NapariUpdateLevelsWidget.addToViewer(
            self.napariViewer
        )
        #LR: The next to widgets are etSTED specific and i.m.o. not needed --> at least for now commented
        #self.NapariResetViewWidget = naparitools.NapariResetViewWidget.addToViewer(self.napariViewer, 'right')
        #self.NapariSumImageWidget = naparitools.NapariSumImageWidget.addToViewer(self.napariViewer, 'right')
        self.imgLayers = {}
        # Result layers other controllers keep in this viewer, updated in
        # place: {jobName: {layerName: layer}} -- see setResultLayers -- and
        # the napari layer type each was created as, which decides whether
        # it is padded when the viewer enters 3D mode.
        self.resultLayers = {}
        self.resultLayerTypes = {}

        # ViewerToolManager for napari Shape-based tools (ROI, line, etc.)
        self.toolManager = naparitools.ViewerToolManager(self.napariViewer)

        # When the viewer switches to 3D display mode all live-view layers
        # must have at least 3 dimensions; otherwise napari's extent
        # computation (extent.data[:, displayed_axes]) raises IndexError for
        # 2D layers while the user slides through a 3D scan result.
        self.napariViewer.dims.events.ndisplay.connect(self._on_ndisplay_changed)

        self.viewCtrlLayout = QtWidgets.QVBoxLayout()
        self.viewCtrlLayout.setContentsMargins(0, 0, 0, 0)

        nw = self.napariViewer.get_widget()
        # Napari's _qt_window is a QMainWindow that restores its own saved size
        # from preferences and has a large minimumSizeHint() from its dock layout.
        # setMinimumSize(0, 0) only clears an *explicit* minimum -- Qt still
        # asks minimumSizeHint() afterwards -- so the hint itself has to go
        # (see minimumSizeHint() below), or the viewer forces the ImSwitch
        # window bigger than the screen.
        nw.setMinimumSize(0, 0)
        nw.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Expanding,
        )
        self.viewCtrlLayout.addWidget(nw)
        self.setLayout(self.viewCtrlLayout)

    #: The smallest viewer worth showing. Napari's embedded window adds up the
    #: minimums of its own docks, which on a populated viewer is larger than
    #: the space ImSwitch has left for it, and reporting that upwards is part
    #: of what makes the application window open taller than the screen.
    #: Capped rather than dropped to zero: a previous attempt to report no
    #: minimum at all collapsed the dock layout during startup, and this keeps
    #: the viewer weighing something while still fitting any screen.
    minimumViewerSize = (240, 180)

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        width, height = self.minimumViewerSize
        return QtCore.QSize(min(hint.width(), width), min(hint.height(), height))

    def _removeProtectedLayer(self, layer):
        """Remove a layer from the viewer, bypassing the protected-layer guard.

        ``EmbeddedNapari`` monkey-patches the layer list's ``_delitem_indices``
        to filter out layers marked ``protected=True``.  Older napari versions
        accepted a ``force=True`` kwarg on ``LayerList.remove`` to override
        that, but the keyword no longer exists in current napari — passing it
        raises TypeError, and dropping it leaves protected layers in place
        forever (the next ``add_image`` then auto-suffixes the new layer to
        ``Live: Camera [1]``).  Clear the flag first instead.
        """
        try:
            layer.protected = False
        except AttributeError:
            pass
        try:
            self.napariViewer.layers.remove(layer)
        except ValueError:
            # Already gone from the layer list — fine.
            pass

    def setLiveViewLayers(self, names):
        for name, img in self.imgLayers.items():
            if name not in names:
                self._removeProtectedLayer(img)

        def addImage(name, colormap=None):
            self.imgLayers[name] = self.napariViewer.add_image(
                np.zeros((1, 1)), rgb=False, name=f'Live: {name}', blending='additive',
                colormap=colormap, protected=True
            )

        for name in names:
            if (name not in self.imgLayers
                    or self.imgLayers[name] not in self.napariViewer.layers):
                try:
                    addImage(name, name.lower())
                except KeyError:
                    addImage(name, 'grayclip')

    def _on_ndisplay_changed(self, event=None):
        """Pad every layer of ours to ndisplay dims when the viewer enters 3D mode.

        Live-view layers and result layers alike: a result layer showing the
        latest plane of a reconstruction is 2D, and it used to be the one
        layer left out, so switching to 3D with a live reconstruction shown
        failed the way the live view once did.
        """
        ndisplay = self.napariViewer.dims.ndisplay
        for name, layer in list(self.imgLayers.items()):
            if layer.data.ndim >= ndisplay:
                continue
            data, scale = self._padToNdisplay(layer.data, tuple(layer.scale), ndisplay)
            # ndim change ⇒ must recreate; see comment in setImage().
            self._recreateLiveLayer(name, data, scale)
        self._padResultLayersToNdisplay(ndisplay)

    @staticmethod
    def _padToNdisplay(data, scale, ndisplay):
        """``data`` with singleton axes prepended until it has ``ndisplay``
        dims, and ``scale`` (fitted to the data first) padded to match.

        The new axes take the smallest existing scale so they stay visually
        negligible: 1.0 would distort napari's 3D bounding box when the X/Y
        pixel pitch is << 1 µm and bloat the rendered image.
        """
        if not hasattr(data, 'ndim'):
            data = np.asarray(data)
        ndim = int(data.ndim)
        if scale is not None:
            scale = ImageWidget._fitScale(scale, ndim)
        if ndim >= int(ndisplay):
            return data, scale
        pad_scale = min(scale) if scale else 1.0
        while data.ndim < int(ndisplay):
            data = data[np.newaxis]
            if scale is not None:
                scale = (pad_scale,) + scale
        return data, scale

    def _recreateLiveLayer(self, name, im, scale):
        """Replace a live-view layer in place, preserving display properties.

        Mandatory when the layer's ndim has to change.  napari has no API to
        atomically grow/shrink a layer's ndim:

          * ``layer.scale = (longer_tuple)`` raises ValueError inside
            transform_utils.py — the scale setter materialises into
            ``np.ones(layer.ndim)`` and broadcasts the new tuple into it,
            so any scale longer than the current ndim fails immediately.
          * ``layer.data = different_ndim_array`` triggers ``_update_dims``,
            which fires ``events.set_data`` synchronously; downstream
            handlers index the still-mismatched affine and raise
            IndexError on ``_world_to_layer_units_scale[displayed_axes]``.

        Recreating preserves contrast limits, colormap, blending and
        list-position; the cost is losing gamma/opacity/visibility tweaks
        the user may have applied, and a brief flicker.  Both are
        acceptable for an ndim transition (rare event — happens once when
        the first 3D scan arrives after startup).
        """
        old = self.imgLayers[name]
        layers = self.napariViewer.layers
        try:
            index = layers.index(old)
        except ValueError:
            index = None

        properties = dict(
            name=old.name,
            blending=old.blending,
            colormap=old.colormap.name,
            contrast_limits=tuple(old.contrast_limits),
            rgb=False,
            scale=scale,
            protected=True,
        )

        self._removeProtectedLayer(old)
        new = self.napariViewer.add_image(im, **properties)
        try:
            new.visible = old.visible
        except AttributeError:
            pass
        if index is not None:
            try:
                new_index = layers.index(new)
                if new_index != index:
                    layers.move(new_index, index)
            except (ValueError, IndexError):
                pass
        self.imgLayers[name] = new

    def addStaticLayer(self, name, im, scale=None):
        """Add one more image layer; every call adds a new one (a snap)."""
        kwargs = dict(rgb=False, name=name, blending='additive')
        if scale is not None:
            kwargs['scale'] = self._fitScale(scale, im.ndim)
        self.napariViewer.add_image(im, **kwargs)

    @staticmethod
    def _fitScale(scale, ndim):
        """``scale`` padded (with 1.0) or trimmed to ``ndim`` entries."""
        sc = tuple(float(v) for v in scale)
        if len(sc) < ndim:
            sc = (1.0,) * (ndim - len(sc)) + sc
        elif len(sc) > ndim:
            sc = sc[-ndim:]
        return sc

    # -- result layers ---------------------------------------------------

    _RESULT_LAYER_ADDERS = {
        'image': 'add_image',
        'labels': 'add_labels',
        'points': 'add_points',
        'shapes': 'add_shapes',
    }
    #: Layer types whose data is an image array, padded to the displayed
    #: dims like the live view (points and shapes are coordinate lists).
    _PADDED_RESULT_LAYER_TYPES = ('image', 'labels')

    def _ndisplay(self):
        try:
            return int(self.napariViewer.dims.ndisplay)
        except AttributeError:
            return 2

    def setResultLayers(self, jobName, layerData):
        """Create or update the result layers of ``jobName`` in place.

        ``layerData`` is a list of napari ``(data, kwargs, layerType)`` tuples
        in display order. A layer is created once, keyed by job and layer
        name, and afterwards only its data (and scale) are swapped, so a live
        update costs a copy rather than a layer rebuild and keeps whatever
        contrast, colormap and visibility the user set. A layer whose ndim
        changes is recreated (napari cannot grow a layer's dims in place, see
        :meth:`_recreateLiveLayer`), and one the user deleted from the layer
        list comes back on the next update. Layers of the job that are absent
        from ``layerData`` are removed. Image data is padded to the viewer's
        displayed dims (see :meth:`_on_ndisplay_changed`), so a 2D plane
        arriving while the viewer is in 3D mode keeps updating the padded
        layer in place. Returns whether any layer was created (rather than
        updated), so a caller can fit the view once.
        """
        existing = self.resultLayers.setdefault(jobName, {})
        types = self.resultLayerTypes.setdefault(jobName, {})
        ndisplay = self._ndisplay()
        seen = set()
        created = False
        for data, kwargs, layerType in layerData:
            kwargs = dict(kwargs or {})
            layerType = str(layerType or 'image')
            name = str(kwargs.get('name') or jobName)
            kwargs['name'] = name
            seen.add(name)
            if layerType in self._PADDED_RESULT_LAYER_TYPES:
                data, scale = self._padToNdisplay(data, kwargs.get('scale'), ndisplay)
                if scale is not None:
                    kwargs['scale'] = scale
            layer = existing.get(name)
            if layer is not None and layer not in self.napariViewer.layers:
                layer = None
            if layer is not None and self._canUpdateInPlace(layer, data, layerType):
                scale = kwargs.get('scale')
                if scale is not None:
                    layer.scale = self._fitScale(scale, layer.ndim)
                layer.data = data
                existing[name] = layer
                types[name] = layerType
                continue
            if layer is not None:
                self._removeResultLayer(layer)
            existing[name] = self._addResultLayer(data, kwargs, layerType)
            types[name] = layerType
            created = True
        for name in list(existing):
            if name not in seen:
                self._removeResultLayer(existing.pop(name))
                types.pop(name, None)
        return created

    def removeResultLayers(self, jobName):
        """Remove every layer :meth:`setResultLayers` created for ``jobName``."""
        self.resultLayerTypes.pop(jobName, None)
        for layer in self.resultLayers.pop(jobName, {}).values():
            self._removeResultLayer(layer)

    def resultLayerNames(self, jobName):
        """The names of the layers currently held for ``jobName``."""
        return list(self.resultLayers.get(jobName, {}))

    def _padResultLayersToNdisplay(self, ndisplay):
        """Recreate image-like result layers with fewer than ``ndisplay`` dims.

        Napari cannot grow a layer's dims in place (:meth:`_recreateLiveLayer`
        says why), so the layer is rebuilt with its display properties and
        its place in the layer list carried over, exactly as a live-view
        layer is.
        """
        for jobName, layers in self.resultLayers.items():
            types = self.resultLayerTypes.get(jobName, {})
            for name, layer in list(layers.items()):
                layerType = types.get(name, 'image')
                if layerType not in self._PADDED_RESULT_LAYER_TYPES:
                    continue
                if layer not in self.napariViewer.layers:
                    continue
                data = layer.data
                if int(getattr(data, 'ndim', np.ndim(data))) >= int(ndisplay):
                    continue
                data, scale = self._padToNdisplay(data, tuple(layer.scale), ndisplay)
                kwargs = self._carriedLayerProperties(layer, layerType)
                kwargs['scale'] = scale
                viewerLayers = self.napariViewer.layers
                try:
                    index = viewerLayers.index(layer)
                except ValueError:
                    index = None
                self._removeResultLayer(layer)
                new = self._addResultLayer(data, kwargs, layerType)
                if index is not None:
                    try:
                        newIndex = viewerLayers.index(new)
                        if newIndex != index:
                            viewerLayers.move(newIndex, index)
                    except (AttributeError, ValueError, IndexError):
                        pass
                layers[name] = new

    @staticmethod
    def _carriedLayerProperties(layer, layerType):
        """The display properties a rebuilt result layer keeps."""
        props = {'name': layer.name}
        for key in ('blending', 'opacity', 'visible'):
            value = getattr(layer, key, None)
            if value is not None:
                props[key] = value
        if layerType == 'image':
            colormap = getattr(getattr(layer, 'colormap', None), 'name', None)
            if colormap:
                props['colormap'] = colormap
            limits = getattr(layer, 'contrast_limits', None)
            if limits is not None:
                props['contrast_limits'] = tuple(limits)
            gamma = getattr(layer, 'gamma', None)
            if gamma is not None:
                props['gamma'] = gamma
        return props

    @staticmethod
    def _canUpdateInPlace(layer, data, layerType):
        if layerType not in ('image', 'labels', 'points'):
            return False
        ndim = int(getattr(data, 'ndim', np.ndim(data)))
        if layerType == 'points':
            return ndim == 2 and int(data.shape[1]) == int(layer.ndim)
        return ndim == int(layer.data.ndim)

    def _addResultLayer(self, data, kwargs, layerType):
        adder = getattr(self.napariViewer, self._RESULT_LAYER_ADDERS.get(layerType, 'add_image'))
        if layerType == 'image':
            kwargs.setdefault('blending', 'additive')
            kwargs.setdefault('rgb', False)
        scale = kwargs.get('scale')
        if scale is not None and layerType != 'points':
            kwargs['scale'] = self._fitScale(scale, int(getattr(data, 'ndim', np.ndim(data))))
        return adder(data, **kwargs)

    def _removeResultLayer(self, layer):
        try:
            layer.protected = False
        except AttributeError:
            pass
        try:
            self.napariViewer.layers.remove(layer)
        except ValueError:
            pass

    def getCurrentImageName(self):
        return self.napariViewer.active_layer.name

    def getImage(self, name):
        return self.imgLayers[name].data

    def setImage(self, name, im, scale):
        layer = self.imgLayers[name]
        scale = tuple(scale)

        # Normalise scale length to match im.ndim.
        if len(scale) < im.ndim:
            scale = (1.0,) * (im.ndim - len(scale)) + scale
        elif len(scale) > im.ndim:
            scale = scale[-im.ndim:]

        # Every layer must have at least ndisplay dimensions, otherwise napari
        # indexes displayed_axes into extent.data[:, displayed_axes] (shape
        # (2, ndim)) and raises IndexError.  Pad with the smallest existing
        # scale so the new singleton axes stay visually negligible (using 1.0
        # distorts the 3D bounding box and bloats the rendered image when the
        # real X/Y pixel pitch is << 1 µm).
        ndisplay = self.napariViewer.dims.ndisplay
        if im.ndim < ndisplay:
            pad_scale = min(scale) if scale else 1.0
            while im.ndim < ndisplay:
                im = im[np.newaxis]
                scale = (pad_scale,) + scale

        # If the layer's current ndim differs from the incoming image, in-place
        # mutation is not possible in this napari version — see the docstring
        # of _recreateLiveLayer for the full explanation.  Briefly:
        #   * scale-first fails because the scale setter validates
        #     len(scale) <= layer.ndim and raises ValueError otherwise
        #     (transform_utils.py:138, "could not broadcast … into shape").
        #   * data-first fails because the data setter synchronously fires
        #     events.set_data with a still-mismatched affine.
        # Recreate is the only path that lets napari rebuild its dims from
        # scratch.  Only hits on the first frame of an ndim transition.
        if layer.data.ndim != im.ndim:
            self._recreateLiveLayer(name, im, scale)
            return

        # Same-ndim update: scale-before-data is safe because the scale setter
        # accepts a tuple matching the current ndim, and the data setter then
        # uses the already-correct scale length when rebuilding the world↔layer
        # transform.
        layer.scale = scale
        layer.data = im

    def clearImage(self, name):
        self.setImage(name, np.zeros((1, 1)))

    def getImageDisplayLevels(self, name):
        return self.imgLayers[name].contrast_limits

    def setImageDisplayLevels(self, name, minimum, maximum):
        self.imgLayers[name].contrast_limits = (minimum, maximum)

    def getCenterViewbox(self):
        """ Returns the center point of the viewbox in DATA-PIXEL coordinates,
        as an (x, y) tuple.

        ``camera.center`` is in world units (the image layer is drawn scaled by
        the detector pixel size), so divide by the current overlay pixel scale.
        Overlay ROIs keep their geometry in data pixels, so a center in the same
        units lands them correctly regardless of pixel size. """
        center = self.napariViewer.camera.center
        sx, sy = getattr(self, '_lastOverlayScale', (1.0, 1.0))
        return (center[2] / sx, center[1] / sy)

    def setOverlayPixelScale(self, scale):
        """ Push the current image layer's pixel scale to overlay ROIs so they
        render in data-pixel units aligned to the (scaled) image.

        ``scale`` is the layer scale in (..., row/Y, col/X) order; ROI axes are
        (x=col, y=row). Stored so a ROI added later (overlays attach on demand)
        picks up the right scale immediately. """
        if scale is None or len(scale) < 2:
            return
        sx, sy = float(scale[-1]), float(scale[-2])
        self._lastOverlayScale = (sx, sy)
        for item in getattr(self, '_overlayItems', []):
            setter = getattr(item, 'setPixelScale', None)
            if setter is not None:
                setter((sx, sy))

    def resetView(self):
        self.napariViewer.reset_view()

    def addItem(self, item):
        try:
            _canvas = self.napariViewer.window.qt_viewer.canvas
            _view = (getattr(_canvas, 'view', None)
                     or getattr(self.napariViewer.window.qt_viewer, 'view', None))
            _parent = _view.scene if _view else None
        except AttributeError:
            _canvas = None
            _view = None
            _parent = None
        item.attach(self.napariViewer,
                    canvas=_canvas,
                    view=_view,
                    parent=_parent,
                    order=1e6 + 8000)
        # Track overlays and apply the last known pixel scale right away, so a
        # ROI added after the image is already showing renders aligned without
        # waiting for the next frame.
        overlays = getattr(self, '_overlayItems', None)
        if overlays is None:
            overlays = self._overlayItems = []
        if item not in overlays:
            overlays.append(item)
        scale = getattr(self, '_lastOverlayScale', None)
        setter = getattr(item, 'setPixelScale', None)
        if scale is not None and setter is not None:
            setter(scale)

    def removeItem(self, item):
        item.detach()
        overlays = getattr(self, '_overlayItems', None)
        if overlays is not None and item in overlays:
            overlays.remove(item)

    def getLayerVisibilityState(self):
        """Return JSON-safe napari layer visibility without storing layer data."""
        layers = {}
        for layer in self.napariViewer.layers:
            name = getattr(layer, 'name', None)
            if name:
                layers[name] = bool(getattr(layer, 'visible', True))

        liveLayers = {}
        for detectorName, layer in self.imgLayers.items():
            liveLayers[detectorName] = bool(getattr(layer, 'visible', True))

        return {
            'layers': layers,
            'liveLayers': liveLayers,
        }

    def applyLayerVisibilityState(self, state):
        """Apply saved napari visibility to layers that currently exist."""
        warnings = []
        if not isinstance(state, dict):
            return ['Layer visibility state is not a mapping; skipped.']

        liveLayers = state.get('liveLayers', {})
        if isinstance(liveLayers, dict):
            for detectorName, visible in liveLayers.items():
                layer = self.imgLayers.get(detectorName)
                if layer is not None:
                    self._setLayerVisible(layer, visible)
        else:
            warnings.append('Saved live layer visibility is not a mapping; skipped.')

        layers = state.get('layers', {})
        if isinstance(layers, dict):
            for layerName, visible in layers.items():
                layer = self._findLayerByName(layerName)
                if layer is not None:
                    self._setLayerVisible(layer, visible)
        else:
            warnings.append('Saved layer visibility is not a mapping; skipped.')

        return warnings

    def setVisibleLayers(self, detectorNames):
        """Show only the named live detector layers."""
        visibleDetectorNames = set(detectorNames or ())
        for detectorName, layer in self.imgLayers.items():
            self._setLayerVisible(layer, detectorName in visibleDetectorNames)

    def _findLayerByName(self, layerName):
        for layer in self.napariViewer.layers:
            if getattr(layer, 'name', None) == layerName:
                return layer
        return None

    @staticmethod
    def _setLayerVisible(layer, visible):
        try:
            layer.visible = bool(visible)
        except AttributeError:
            pass

    @shortcut(actionId="image.updateLevels", defaultKey="Ctrl+U",
              displayName="Update levels", initiallyBound=True)
    def updateLevelsButton(self):
        self.updateLevelsWidget.updateLevelsButton.click()


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
