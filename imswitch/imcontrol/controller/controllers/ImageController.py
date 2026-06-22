from imswitch.imcontrol.view import guitools
from imswitch.imcontrol.model import getWidgetStatePersistence
from ..basecontrollers import (
    ComponentStateApplyMode,
    LiveUpdatedController,
    StatefulComponentMixin,
)
from ..display_transform import apply_display_transform, display_transform_from_properties
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
        self._commChannel.sigSetExposure.connect(lambda t: self.setExposure(t))

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
            display_im, display_scale = apply_display_transform(
                im,
                scale,
                self._getDisplayTransform(detectorName),
            )

            if not init:
                self.autoLevels([detectorName], display_im)

            self._widget.setImage(detectorName, display_im, display_scale)

            # Keep overlay ROIs aligned to the current detector's pixel scale
            # (and orientation, already baked into display_scale by the swap in
            # apply_display_transform). Without this an ROI drawn in pixel units
            # is mis-sized/shifted on cameras whose pixel size isn't 1 µm.
            if isCurrentDetector:
                self._widget.setOverlayPixelScale(display_scale)

            if not init or self._shouldResetView:
                self.adjustFrame(shape=display_im.shape, instantResetView=True)

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
        return self._widget.getLayerVisibilityState()

    def applyComponentState(
        self,
        state: dict,
        *,
        applyMode: ComponentStateApplyMode,
    ) -> list[str]:
        """Restore visibility for layers that already exist in the viewer."""
        return self._widget.applyLayerVisibilityState(state)

    def describeComponentState(self, state: dict) -> list[str]:
        layers = state.get('layers', {}) if isinstance(state, dict) else {}
        liveLayers = state.get('liveLayers', {}) if isinstance(state, dict) else {}

        if not layers and not liveLayers:
            return ['  no napari layer visibility saved']

        summaries = []
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
