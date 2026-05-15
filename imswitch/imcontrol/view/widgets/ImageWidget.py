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
        try:
            self.napariViewer.dims.events.ndisplay.connect(self._on_ndisplay_changed)
        except Exception:
            pass

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

    def setLiveViewLayers(self, names):
        for name, img in self.imgLayers.items():
            if name not in names:
                self.napariViewer.layers.remove(img, force=True)

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
        try:
            ndisplay = self.napariViewer.dims.ndisplay
        except Exception:
            return
        for name, layer in list(self.imgLayers.items()):
            try:
                data = layer.data
                scale = tuple(layer.scale)
                changed = False
                while data.ndim < ndisplay:
                    data = data[np.newaxis]
                    scale = (1.0,) + scale
                    changed = True
                if changed:
                    try:
                        layer.scale = scale
                    except Exception:
                        pass
                    try:
                        layer.data = data
                    except Exception:
                        pass
            except Exception:
                pass

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

        # When the viewer is in 3D display mode (ndisplay=3) every layer must
        # have at least 3 dimensions; otherwise napari indexes displayed_axes
        # into extent.data[:, displayed_axes] (shape (2, ndim)) and raises
        # IndexError when ndim < ndisplay.  Add singleton axes at the front.
        try:
            ndisplay = self.napariViewer.dims.ndisplay
            while im.ndim < ndisplay:
                im = im[np.newaxis]
                scale = (1.0,) + scale
        except Exception:
            pass

        # Scale MUST be set before data: napari rebuilds _world_to_layer_units_scale
        # from the current scale when data is assigned; if the old scale has a
        # different length dims_displayed indexes beyond it → IndexError.
        try:
            layer.scale = scale
        except Exception:
            pass
        try:
            layer.data = im
        except (IndexError, ValueError):
            pass

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
