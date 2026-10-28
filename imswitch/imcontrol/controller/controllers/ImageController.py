from imswitch.imcontrol.view import guitools
from imswitch.imcontrol.model import getWidgetStatePersistence
from ..basecontrollers import (
    ComponentStateApplyMode,
    LiveUpdatedController,
    StatefulComponentMixin,
)
from ..display_transform import (
    apply_display_transform, display_transform_from_properties, sum_axes_for_display,
)
from imswitch.imcontrol.model.scan_frame import DisplayedScanGeometry, frame_geometry_of
from imswitch.imcommon.model import initLogger
import numpy as np
import re

class ImageController(LiveUpdatedController, StatefulComponentMixin):
    """ Linked to ImageWidget."""

    componentName = 'Image'
    stateSchemaVersion = 1
    setupModeDisplayName = 'Image viewer'
    setupModeCategory = 'viewer'
    setupModeHardwareCritical = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.__logger = initLogger(self, tryInheritParent=True)
        getWidgetStatePersistence().register('Image', self)
        self._shownScanGeometry = {}   # detector -> DisplayedScanGeometry or None
        # The view's sum over the frames' extra dimensions (line steps, z,
        # t): which, by name, for every detector whose frames have them; the
        # last such frame per detector, to redraw on a change; what the
        # widget was told, to tell it only on a change.
        self._summedDims = set()       # dimension names
        self._lastFrames = {}          # detector -> (im, scale, isCurrentDetector)
        self._toldAxes = {}            # detector -> (labels, summed)
        if hasattr(self._widget, 'sigSumAxesChanged'):
            self._widget.sigSumAxesChanged.connect(self._onSumAxesChanged)

        if hasattr(self._commChannel, 'sigSetVisibleLayers'):
            self._commChannel.sigSetVisibleLayers.connect(self.setVisibleLayers)

        if not self._master.detectorsManager.hasDevices():
            return

        self._lastShape = self._master.detectorsManager.execOnCurrent(lambda c: c.shape)
        self._shouldResetView = False

        self._widget.setLiveViewLayers(
            self._master.detectorsManager.getAllDeviceNames(lambda c: c.forAcquisition)
        )

        # Connect CommunicationChannel signals
        self._commChannel.sigUpdateImage.connect(self.update)
        self._commChannel.sigAdjustFrame.connect(self.adjustFrame)
        self._commChannel.sigAddItemToVb.connect(self.addItemToVb)
        self._commChannel.sigRemoveItemFromVb.connect(self.removeItemFromVb)
        self._commChannel.sigMemorySnapAvailable.connect(self.memorySnapAvailable)
        self._commChannel.sigUpsertStaticLayer.connect(self.upsertStaticLayer)
        self._commChannel.sigRemoveStaticLayer.connect(self.removeStaticLayer)
        self._commChannel.sigSetExposure.connect(lambda t: self.setExposure(t))

        # Connect ModuleCommunicationChannel signals if available
        if self._moduleCommChannel is not None:
            self._moduleCommChannel.sigLiveReconResult.connect(self.liveReconResultAvailable)

    def autoLevels(self, detectorNames=None, im=None):
        """ Set histogram levels automatically with current detector image."""
        if detectorNames is None:
            detectorNames = self._master.detectorsManager.getAllDeviceNames(
                lambda c: c.forAcquisition
            )

        for detectorName in detectorNames:
            if im is None:
                im = self._widget.getImage(detectorName)

            # self._widget.setImageDisplayLevels(detectorName, *guitools.bestLevels(im))
            self._widget.setImageDisplayLevels(detectorName, *guitools.minmaxLevels(im))

    def addItemToVb(self, item):
        """ Add item from communication channel to viewbox."""
        item.hide()
        self._widget.addItem(item)

    def removeItemFromVb(self, item):
        """ Remove item from communication channel to viewbox."""
        self._widget.removeItem(item)

    def update(self, detectorName, im, init, scale, isCurrentDetector):
        """ Update new image in the viewbox. """
        if np.prod(im.shape)>1:
            # Read before the display transform: a transformed view carries no
            # geometry (scan_frame.ScanFrame), and the geometry describes the
            # untransformed pixels anyway.
            geometry = frame_geometry_of(im)
            published_shape = tuple(np.shape(im))
            # The view's sum over extra dimensions, before the transform (the
            # transform acts on the image axes) and before the levels, so
            # they are those of what is shown.
            im, scale = sum_axes_for_display(
                im, scale, self._summedAxesOf(detectorName, im, scale, isCurrentDetector))
            transform = self._getDisplayTransform(detectorName)
            display_im, display_scale = apply_display_transform(
                im,
                scale,
                transform,
            )

            if not init:
                self.autoLevels([detectorName], display_im)

            shown = (
                DisplayedScanGeometry(geometry, transform, published_shape)
                if geometry is not None else None
            )
            self._widget.setImage(
                detectorName, display_im, display_scale, scanGeometry=shown,
            )
            # Told once per change, after the layer shows it: a panel that maps
            # viewer points to scanner positions re-projects on this.
            if shown != self._shownScanGeometry.get(detectorName):
                self._shownScanGeometry[detectorName] = shown
                self._commChannel.sigScanGeometryShown.emit(detectorName, shown)

            # Keep overlay ROIs aligned to the current detector's pixel scale
            # (and orientation, already baked into display_scale by the swap in
            # apply_display_transform). Without this an ROI drawn in pixel units
            # is mis-sized/shifted on cameras whose pixel size isn't 1 µm.
            if isCurrentDetector:
                self._widget.setOverlayPixelScale(display_scale)

            if not init or self._shouldResetView:
                self.adjustFrame(shape=display_im.shape, instantResetView=True)

    def _axisLabels(self, detectorName, im):
        """What to call each dimension of a frame beyond its image, outermost
        first: the detector's own names when it gives them (an APD or PMT
        scan: line steps, z), else their positions."""
        extra = max(0, np.ndim(im) - 2)
        if not extra:
            return []
        names = None
        try:
            names = getattr(self._master.detectorsManager[detectorName], 'displayAxisNames', None)
        except Exception:
            names = None
        if names and len(names) == np.ndim(im):
            return [str(n) for n in names[:extra]]
        return [f'dimension {i}' for i in range(extra)]

    def _summedAxesOf(self, detectorName, im, scale, isCurrentDetector):
        """Which axes of this frame the view sums: those whose dimension the
        Sum checkboxes name. Tells the widget the frame's dimensions on a
        change, and keeps a frame that has any, to redraw on a toggle.
        (``__dict__``: a controller built without __init__ in a test has none
        of these, and a QObject raises on a missing attribute.)"""
        labels = self._axisLabels(detectorName, im)
        frames = self.__dict__.setdefault('_lastFrames', {})
        if labels:
            frames[detectorName] = (im, scale, isCurrentDetector)
        else:
            frames.pop(detectorName, None)
        summed = self.__dict__.setdefault('_summedDims', set())
        told = self.__dict__.setdefault('_toldAxes', {})
        news = (labels, tuple(sorted(summed)))
        if told.get(detectorName) != news and hasattr(self._widget, 'setSummableAxes'):
            told[detectorName] = news
            self._widget.setSummableAxes(detectorName, labels, news[1])
        return tuple(i for i, label in enumerate(labels) if label in summed)

    def _onSumAxesChanged(self, dimensions):
        """The view's checkboxes: remember which dimensions are summed, and
        redraw the last frame of every detector that has one that changed,
        levelled anew -- a sum is brighter than one slice."""
        before = set(self.__dict__.get('_summedDims', set()))
        now = {str(name) for name in dimensions}
        self.__dict__['_summedDims'] = now
        for detectorName, (im, scale, isCurrent) in list(
                self.__dict__.get('_lastFrames', {}).items()):
            if (before ^ now) & set(self._axisLabels(detectorName, im)):
                self.update(detectorName, im, True, scale, isCurrent)
                self.autoLevels([detectorName])

    def _getDisplayTransform(self, detectorName):
        detector_info = self._setupInfo.detectors.get(detectorName)
        if detector_info is None:
            return display_transform_from_properties(None)
        return display_transform_from_properties(detector_info.managerProperties)

    def adjustFrame(self, shape=None, instantResetView=False):
        """ Adjusts the viewbox to a new width and height. """

        if shape is None:
            shape = self._lastShape

        if instantResetView:
            self._widget.resetView()
            self._shouldResetView = False
        else:
            self._shouldResetView = True

        self._lastShape = shape

    def getCenterViewbox(self):
        """ Returns center of viewbox to center a ROI. """
        return self._widget.getCenterViewbox()

    def memorySnapAvailable(self, name, image, _, __):
        """ Adds captured image to widget. """
        self._widget.addStaticLayer(name, image)
        if self._shouldResetView:
            self.adjustFrame(image.shape, instantResetView=True)

    def upsertStaticLayer(self, name, image, scale=None, options=None):
        """Create or update a named product layer (Lifetime widget)."""
        options = dict(options or {})
        self._widget.upsertStaticLayer(
            name, image, scale=scale, colormap=options.get('colormap'),
            rgb=bool(options.get('rgb', False)),
            contrast_limits=options.get('contrast_limits'),
        )

    def removeStaticLayer(self, name):
        self._widget.removeStaticLayer(name)

    def liveReconResultAvailable(self, name, image, scale):
        """ Adds live reconstruction result to widget. """
        self._widget.addStaticLayer(name, image, scale)
        if self._shouldResetView:
            self.adjustFrame(image.shape, instantResetView=True)

    def setExposure(self, exp):
        detectorName = self._master.detectorsManager.getAllDeviceNames()[0]
        self.__logger.debug(f"Change exposure of {detectorName}, to {str(exp)}")
        #self._master.detectorsManager[detectorName].setParameter('Readout time', exp)

    def setVisibleLayers(self, detectorNames):
        self._widget.setVisibleLayers(detectorNames)

    def getComponentState(self) -> dict:
        """Snapshot passive napari layer visibility only.

        Layer image data, static layer creation, camera acquisition state, and
        viewer camera pose are intentionally excluded.
        """
        state = dict(self._widget.getLayerVisibilityState())
        summed = sorted(self.__dict__.get('_summedDims', set()))
        if summed:
            state['summedDimensions'] = summed
        return state

    def applyComponentState(
        self,
        state: dict,
        *,
        applyMode: ComponentStateApplyMode,
    ) -> list[str]:
        """Restore visibility for layers that already exist in the viewer,
        and which dimensions the view sums over."""
        if isinstance(state, dict):
            # Absent is nothing summed: a state saved with no sum clears one
            # left on by another setup mode (review 2026-10-16).
            summed = state.get('summedDimensions') or []
            if isinstance(summed, (list, tuple)):
                self._onSumAxesChanged(tuple(summed))
                self._toldAxes = {}            # the next frame tells the boxes
        return self._widget.applyLayerVisibilityState(state)

    def describeComponentState(self, state: dict) -> list[str]:
        layers = state.get('layers', {}) if isinstance(state, dict) else {}
        liveLayers = state.get('liveLayers', {}) if isinstance(state, dict) else {}

        summed = state.get('summedDimensions', []) if isinstance(state, dict) else []
        if not layers and not liveLayers and not summed:
            return ['  no napari layer visibility saved']

        summaries = []
        if summed:
            summaries.append('  summed in the view: ' + ', '.join(sorted(map(str, summed))))
        if liveLayers:
            visibleLive = sorted(name for name, visible in liveLayers.items() if visible)
            hiddenLive = sorted(name for name, visible in liveLayers.items() if not visible)
            summaries.append(
                f'  live layers: visible={visibleLive or "none"}, hidden={hiddenLive or "none"}'
            )
        if layers:
            visibleLayers = sorted(name for name, visible in layers.items() if visible)
            hiddenLayers = sorted(name for name, visible in layers.items() if not visible)
            summaries.append(
                f'  napari layers: visible={visibleLayers or "none"}, hidden={hiddenLayers or "none"}'
            )
        return summaries

    def getComponentStateHazards(
        self,
        state: dict,
        *,
        applyMode: ComponentStateApplyMode,
        context: dict | None = None,
    ) -> list[dict]:
        return []


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
