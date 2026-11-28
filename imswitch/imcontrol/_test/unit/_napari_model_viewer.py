"""The image viewer without a canvas, for headless tests.

napari's Qt viewer needs an OpenGL context, which the offscreen platform the
suite runs on does not have (``get_gl_extensions`` returns None). The model
underneath it does not: :class:`ModelViewer` is a
``napari.components.ViewerModel`` with the layer protection of the real
:class:`~imswitch.imcommon.view.guitools.naparitools.EmbeddedNapari` (the same
mixin), and :class:`HeadlessImageWidget` is the shipping
:class:`~imswitch.imcontrol.view.widgets.ImageWidget.ImageWidget` built on it
through the same ``_initViewerModel`` -- every method under test is the real
one; only the docks and the canvas are missing.

What this cannot show: rendering, mouse interaction on the canvas, the vispy
overlays, and napari's Qt layer controls.
"""
from napari.components import ViewerModel
from qtpy import QtWidgets

from imswitch.imcommon.view.guitools import naparitools
from imswitch.imcontrol.view.widgets.ImageWidget import ImageWidget


class ModelViewer(naparitools.ProtectedLayersMixin, ViewerModel):
    """A canvas-less viewer that protects layers like the embedded one."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._installLayerProtection()

    @property
    def active_layer(self):
        return self.layers.selection.active


class HeadlessImageWidget(ImageWidget):
    """The shipping image widget on a :class:`ModelViewer`."""

    def __init__(self):
        QtWidgets.QWidget.__init__(self)
        naparitools.addNapariGrayclipColormap()
        self.napariViewer = ModelViewer()
        # Undocked: there is no window to dock it in. Its dock-visibility
        # handling is a no-op without one; its boxes and signals are real.
        self.sumAxesWidget = naparitools.NapariSumAxesWidget(self.napariViewer)
        self._initViewerModel()

    def close(self):
        super().close()
        try:
            self.napariViewer.layers.clear()
        except Exception:
            pass


def layerNames(viewer):
    return [layer.name for layer in viewer.layers]


def activeName(viewer):
    active = viewer.layers.selection.active
    return None if active is None else active.name
