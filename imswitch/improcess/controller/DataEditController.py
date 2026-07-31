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
        self._dataObj = inDataObj
        self._meanData = self._dataObj.getMeanData()
        self.showMean()
        self._widget.updateDataProperties(self._dataObj.name, self._dataObj.datasetName,
                                          self._dataObj.numFrames)

    def setImgSlice(self, frameNumber):
        if self._dataObj is None:
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
