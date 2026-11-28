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
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
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


# -- the image controller on the headless widget ----------------------------------

class FakeDetectors:
    """The few DetectorsManager calls ImageController makes."""

    def __init__(self, detectors):
        self._detectors = detectors

    def __getitem__(self, name):
        return self._detectors[name]

    def hasDevices(self):
        return True

    def execOnCurrent(self, fn):
        return (64, 64)

    def getAllDeviceNames(self, condition=None):
        return [name for name, d in self._detectors.items()
                if condition is None or condition(d)]


def fakeDetector(scanDriven):
    return SimpleNamespace(forAcquisition=True, isScanDriven=scanDriven,
                           livePlaneIndex=None, displayAxisNames=None)


def imageControllerRig():
    """A real ImageController on a HeadlessImageWidget, with two scan-driven
    detectors (APDred, APDgreen) and a camera. ``publish`` delivers a frame
    as a detector does, with the plane it is writing."""
    from imswitch.imcontrol.controller.controllers.ImageController import ImageController

    widget = HeadlessImageWidget()
    detectors = {'APDred': fakeDetector(True), 'APDgreen': fakeDetector(True),
                 'Camera': fakeDetector(False)}
    master = SimpleNamespace(detectorsManager=FakeDetectors(detectors),
                             nidaqManager=Mock(), scanExecutionCoordinator=None)
    with patch('imswitch.imcontrol.controller.controllers.ImageController.'
               'getWidgetStatePersistence', return_value=Mock()):
        controller = ImageController(Mock(), Mock(), master, widget=widget,
                                     factory=Mock(), moduleCommChannel=Mock())
    controller._setupInfo = SimpleNamespace(detectors={})
    rig = SimpleNamespace(widget=widget, controller=controller, detectors=detectors,
                          master=master, viewer=widget.napariViewer)

    def publish(name, frame, plane=None, scale=(0.5, 0.1, 0.1)):
        detectors[name].livePlaneIndex = plane
        controller.update(name, frame, True, list(scale[-np.ndim(frame):]),
                          name == 'APDred')

    rig.publish = publish
    return rig


def stack(planes=8, filled_planes=0, value=50):
    frames = np.zeros((planes, 32, 32), np.uint16)
    frames[:filled_planes] = value
    return frames
