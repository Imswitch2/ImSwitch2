import numpy as np
from qtpy import QtWidgets

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
        self.NapariShiftWidget = naparitools.NapariShiftWidget.addToViewer(self.napariViewer)
        self.imgLayers = {}

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
        # Clear the hard minimum so it doesn't force the parent window taller
        # than the available screen area when embedded.
        nw.setMinimumSize(0, 0)
        nw.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Expanding,
        )
        self.viewCtrlLayout.addWidget(nw)
        self.setLayout(self.viewCtrlLayout)

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
            if name not in self.napariViewer.layers:
                try:
                    addImage(name, name.lower())
                except KeyError:
                    addImage(name, 'grayclip')

    def _on_ndisplay_changed(self, event=None):
        """Pad all live-view layers to ndisplay dims when the viewer enters 3D mode."""
        ndisplay = self.napariViewer.dims.ndisplay
        for name, layer in list(self.imgLayers.items()):
            if layer.data.ndim >= ndisplay:
                continue
            data = layer.data
            scale = tuple(layer.scale)
            # Pad with the smallest existing scale so the new singleton axis
            # stays visually negligible — using 1.0 here would distort napari's
            # 3D bounding box when X/Y pixel pitch is << 1 µm and bloat the
            # rendered image.
            pad_scale = min(scale) if scale else 1.0
            while data.ndim < ndisplay:
                data = data[np.newaxis]
                scale = (pad_scale,) + scale
            # ndim change ⇒ must recreate; see comment in setImage().
            self._recreateLiveLayer(name, data, scale)

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
        if index is not None:
            try:
                new_index = layers.index(new)
                if new_index != index:
                    layers.move(new_index, index)
            except (ValueError, IndexError):
                pass
        self.imgLayers[name] = new

    def addStaticLayer(self, name, im, scale=None):
        kwargs = dict(rgb=False, name=name, blending='additive')
        if scale is not None:
            sc = tuple(scale)
            if len(sc) < im.ndim:
                sc = (1.0,) * (im.ndim - len(sc)) + sc
            elif len(sc) > im.ndim:
                sc = sc[-im.ndim:]
            kwargs['scale'] = sc
        self.napariViewer.add_image(im, **kwargs)

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
        """ Returns the center point of the viewbox, as an (x, y) tuple. """
        center = self.napariViewer.camera.center
        return (center[2], center[1])

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

    def removeItem(self, item):
        item.detach()

    @shortcut('Ctrl+U', "Update levels")
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
