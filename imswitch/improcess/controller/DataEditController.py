import numpy as np

from imswitch.improcess.model.plane_navigation import extract_plane, plane_count

from .basecontrollers import ImProcessWidgetController


class DataEditController(ImProcessWidgetController):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._dataObj = None
        self._meanData = None

        self._widget.sigImageSliceChanged.connect(self.setImgSlice)
        self._widget.sigShowMeanClicked.connect(self.showMean)
        self._widget.sigSetDarkFrameClicked.connect(self.setDarkFrame)

    def setData(self, inDataObj):
        if getattr(inDataObj, 'sourceKind', 'image') != 'image':
            return
        self._dataObj = inDataObj
        # Opening the edit window is not a request for the mean; above the
        # processing working set the window opens on the first plane (or on
        # nothing, where a plane read decodes the whole series) and the mean
        # waits for the Show mean button, which computes it as asked -- the
        # same split as the current-data panel.
        notice = getattr(self._dataObj, 'meanPreviewNotice', None)
        notice = notice() if callable(notice) else None
        self._meanData = None
        if notice is None:
            self._meanData = self._dataObj.getMeanData()
            self._displayMean()
        else:
            bounded = getattr(self._dataObj, 'planeReadIsBounded', None)
            bounded = bool(bounded()) if callable(bounded) else True
            if bounded:
                self._logger.info(f'{notice} Opening on the first plane; Show mean '
                                  f'computes it on request.')
                self.setImgSlice(0)
            else:
                self._logger.info(f'{notice} Nothing shown until asked; Show mean '
                                  f'computes it on request.')
                self._widget.setImage(np.zeros((1, 1)), autoLevels=True)
        self._widget.updateDataProperties(self._dataObj.name, self._dataObj.datasetName,
                                          self._dataObj.numFrames)

    def setImgSlice(self, frameNumber):
        if (
            self._dataObj is None
            or getattr(self._dataObj, 'sourceKind', 'image') != 'image'
        ):
            return

        data = self._dataObj.data
        labels = getattr(self._dataObj, "axis_labels", None)
        if frameNumber >= plane_count(np.shape(data), labels):
            return

        self._widget.setImage(extract_plane(data, frameNumber, labels), autoLevels=False)

    def setDarkFrame(self):
        # self.dataObj.data = self.dataObj.data[0:100]
        pass

    def showMean(self):
        """The Show mean button: compute the mean if it was deferred, then show it."""
        if self._meanData is None and self._dataObj is not None:
            notice = getattr(self._dataObj, 'meanPreviewNotice', None)
            notice = notice() if callable(notice) else None
            if notice is not None:
                self._logger.warning(f'{notice} Computing it as requested.')
            self._meanData = self._dataObj.getMeanData()
        self._displayMean()

    def _displayMean(self):
        if self._meanData is None:
            return
        self._widget.setImage(self._meanData, autoLevels=True)


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
