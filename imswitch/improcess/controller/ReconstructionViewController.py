import numpy as np

from imswitch.imcommon.model import initLogger
from .basecontrollers import ImProcessWidgetController


class ReconstructionViewController(ImProcessWidgetController):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._logger = initLogger(self)

        self._currItemInd = None
        self._prevViewId = None

        self._transposeOrder = [0, 1, 2, 3, 4, 5]
        self._axisStep = (0, 0, 0, 0, 0, 0)

        self._commChannel.sigScanParamsUpdated.connect(self.scanParamsUpdated)
        self._commChannel.sigResultProduced.connect(self.resultProduced)

        self._widget.sigItemSelected.connect(self.listItemChanged)
        self._widget.sigAxisStepChanged.connect(self.axisStepChanged)
        self._widget.sigViewChanged.connect(lambda: self.fullUpdate(levels=None))

    def getActiveReconObj(self):
        return self._widget.getCurrentItemData()

    def getAllReconObjs(self):
        return self._widget.getAllItemDatas()

    def listItemChanged(self):
        if self._currItemInd is not None:
            currHistLevels = self._widget.getImageDisplayLevels()
            prevItem = self._widget.getDataAtIndex(self._currItemInd)
            prevItem.setDispLevels(currHistLevels)

            currItem = self._widget.getCurrentItemData()
            self._syncViewModes(currItem)
            retrievedLevels = \
                self._widget.getCurrentItemData().getDispLevels() if currItem is not None else None
            self.fullUpdate(levels=retrievedLevels)
            if retrievedLevels is not None:
                self._widget.setImageDisplayLevels(retrievedLevels[0], retrievedLevels[1])
        else:
            self._syncViewModes(self._widget.getCurrentItemData())
            self.fullUpdate(autoLevels=True,
                            levels=self._widget.getCurrentItemData().getDispLevels())

        self._currItemInd = self._widget.getCurrentItemIndex()
        self._commChannel.sigCurrentResultChanged.emit(self._widget.getCurrentItemData())

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
        if hasattr(current, "data") and hasattr(current, "view_modes"):
            self._setProcessingResultSlice(current, autoLevels=autoLevels, levels=levels)
            return

        data = current.reconstructed

        if self.getViewId() == 3:
            transposeOrder = [0, 1, 2, 3, 4, 5]
        elif self.getViewId() == 4:
            transposeOrder = [0, 1, 2, 4, 3, 5]
        else:
            transposeOrder = [0, 1, 2, 5, 4, 3]

        im = data.transpose(*transposeOrder)
        axisLabels = np.array(['Dataset', 'Base', 'Time point', 'Slice', 'X', 'Y'])[transposeOrder]
        self._transposeOrder = transposeOrder

        self._widget.setImage(im, axisLabels)
        if autoLevels:
            self.updateLevelsRange()
        elif levels is not None:
            self._widget.setImageDisplayLevels(*levels)

    def _setProcessingResultSlice(self, result, autoLevels=False, levels=None):
        mode = self._processingViewMode(result)
        im = result.data.transpose(*mode.transpose)
        axisLabels = np.array(result.axis_labels)[list(mode.transpose)]
        axisScales = np.array(result.axis_scales, dtype=float)[list(mode.transpose)]
        self._transposeOrder = list(mode.transpose)

        self._logger.debug(
            "_setProcessingResultSlice: result=%s  view_mode=%s  "
            "data.shape=%s  transposed.shape=%s  "
            "axis_labels=%s  axis_scales=%s  scale_unit=%s",
            type(result).__name__, mode.name,
            result.data.shape, im.shape,
            list(axisLabels), [f"{s:.4g}" for s in axisScales], result.scale_unit,
        )

        self._widget.setImage(im, axisLabels, axisScales, result.scale_unit)
        if levels is not None:
            self._widget.setImageDisplayLevels(*levels)
        elif autoLevels:
            self.updateLevelsRange(base=None)

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
            # Return the name itself — getViewId() is only compared for equality in fullUpdate.
            self._logger.debug("getViewId: custom view mode %r → using name as ID", viewName)
            return viewName

    def axisStepChanged(self, newAxisStep):
        baseAxisIndex = self._transposeOrder.index(1)
        newBase = newAxisStep[baseAxisIndex]
        if newBase != self._axisStep[baseAxisIndex]:
            # Base changed, update levels range
            self.updateLevelsRange(newBase)

        self._axisStep = newAxisStep

    def updateLevelsRange(self, base=None):
        baseAxisIndex = self._transposeOrder.index(1)
        if base is None:
            base = self._axisStep[baseAxisIndex]

        # Find image at current base
        im = self._widget.getImage()
        indexForImage = [slice(None) for _ in range(len(im.shape))]
        indexForImage[baseAxisIndex] = base
        imAtBase = im[tuple(indexForImage)]

        # Update levels
        levels = imAtBase.min(), imAtBase.max()
        self._widget.setImageDisplayLevelsRange(*levels)
        self._widget.setImageDisplayLevels(*levels)

    def updateRecon(self):
        reconObj = self._widget.getCurrentItemData()
        if reconObj is not None:
            reconObj.updateImages()
            self.fullUpdate(levels=None)

    def scanParamsUpdated(self, scanParDict, applyOnCurrentRecon):
        if not applyOnCurrentRecon:
            return

        reconObj = self._widget.getCurrentItemData()
        if reconObj is not None:
            reconObj.updateScanParams(scanParDict)
            self.updateRecon()

    def getImage(self):
        return self._widget.getImage()

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
