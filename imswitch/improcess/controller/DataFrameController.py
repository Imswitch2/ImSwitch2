import numpy as np

from imswitch.improcess.model.plane_navigation import extract_plane, plane_axes

from .DataEditController import DataEditController
from .basecontrollers import ImProcessWidgetController


def _is_image_source(data_obj):
    return getattr(data_obj, 'sourceKind', 'image') == 'image'


class DataFrameController(ImProcessWidgetController):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.editWindowController = self._factory.createController(
            DataEditController, self._widget.editWdw
        )

        self._dataObj = None
        self._pattern = []
        self._patternGrid = []
        self._patternGridMade = False
        self._patternVisible = False
        self._explicitPatternPoints = None
        self._displayedImage = None

        self._commChannel.sigCurrentDataChanged.connect(self.currentDataChanged)
        self._commChannel.sigPatternUpdated.connect(self.patternUpdated)
        self._commChannel.sigPatternPointsUpdated.connect(self.patternPointsUpdated)
        self._commChannel.sigPatternVisibilityChanged.connect(self.patternVisibilityChanged)
        self._commChannel.sigDetectionPreviewUpdated.connect(self.detectionPreviewUpdated)
        self._commChannel.sigDetectionPreviewVisibilityChanged.connect(self.detectionPreviewVisibilityChanged)

        self._widget.sigShowMeanClicked.connect(self.showMean)
        self._widget.sigAdjustDataClicked.connect(self.adjustData)
        self._widget.sigUnloadDataClicked.connect(self.unloadData)
        self._widget.sigFrameNumberChanged.connect(self.setImgSlice)
        self._widget.sigFrameSliderChanged.connect(self.setImgSlice)

    def patternUpdated(self, pattern):
        changed = list(pattern) != list(self._pattern)
        self._pattern = pattern
        if changed:
            # A genuine edit of the rectangular fields returns the overlay to
            # the grid. Unchanged re-emissions must not wipe explicit lattice
            # points: the Pattern group re-fires sigTreeStateChanged on any
            # interaction — including the Find pattern button, which lives
            # inside that group.
            self._explicitPatternPoints = None
        self._patternGridMade = False
        if self._patternVisible:
            self.makePatternGrid()

    def patternPointsUpdated(self, x, y):
        """Show explicit focus positions (non-rectangular detected lattices)."""
        self._explicitPatternPoints = (np.asarray(x), np.asarray(y))
        self._patternGridMade = False
        self._logger.debug(
            f'Received {self._explicitPatternPoints[0].size} explicit '
            f'pattern points'
        )
        if self._patternVisible:
            self.makePatternGrid()

    def patternVisibilityChanged(self, showPattern):
        self._patternVisible = showPattern
        if showPattern and not self._patternGridMade:
            self.makePatternGrid()

        self._widget.setShowPattern(showPattern)

    def detectionPreviewUpdated(self, x, y):
        self._widget.setDetectionPreviewData(x, y)

    def detectionPreviewVisibilityChanged(self, visible):
        self._widget.setShowDetectionPreview(visible)

    def setImgSlice(self, frame):
        data = self._currentDataArray()
        if data is None:
            return
        img = extract_plane(data, frame, self._currentAxisLabels())
        self._displayedImage = img
        self._widget.setImage(img, autoLevels=False)
        self._commChannel.sigDisplayedFrameChanged.emit()

    def unloadData(self):
        self._dataObj = None
        self._displayedImage = None
        self.showMean()
        self._widget.setNumFrames(0)
        self._widget.setDataName('')
        self._widget.setDatasetName('')
        setter = getattr(self._widget, 'setImageControlsEnabled', None)
        if callable(setter):
            setter(False)

    def adjustData(self):
        self._logger.debug('In adjust data')
        if self._dataObj is not None and _is_image_source(self._dataObj):
            self.editWindowController.setData(self._dataObj)
            self._widget.showEditWindow()
        else:
            self._logger.error('No data to edit')

    def showMean(self):
        img = (
            self._dataObj.getMeanData()
            if self._dataObj is not None and _is_image_source(self._dataObj)
            else np.zeros((1, 1))
        )
        self._displayedImage = img
        self._widget.setImage(img, autoLevels=True)
        self._commChannel.sigDisplayedFrameChanged.emit()

    def getDisplayedImage2D(self):
        """Return the currently displayed 2D image, or None if no data loaded."""
        return self._displayedImage

    def currentDataChanged(self, inDataObj):
        self._dataObj = inDataObj
        is_image = _is_image_source(self._dataObj)
        setter = getattr(self._widget, 'setImageControlsEnabled', None)
        if callable(setter):
            setter(is_image)
        if not is_image:
            self._displayedImage = None
            self._patternGridMade = False
            self._widget.setImage(np.zeros((1, 1)), autoLevels=True)
            pattern_setter = getattr(self._widget, 'setPatternGridData', None)
            if callable(pattern_setter):
                pattern_setter([], [])
            detection_setter = getattr(
                self._widget, 'setDetectionPreviewData', None
            )
            if callable(detection_setter):
                detection_setter([], [])
            self._widget.setNumFrames(0)
            self._widget.setDataName(self._dataObj.name)
            self._widget.setDatasetName(self._dataObj.datasetName)
            self._commChannel.sigDisplayedFrameChanged.emit()
            return
        data = self._currentDataArray()
        self._logger.debug(f'Data shape: {data.shape}')
        self.showMean()
        self._widget.setNumFrames(self._dataObj.numFrames)
        self._widget.setDataName(self._dataObj.name)
        self._widget.setDatasetName(self._dataObj.datasetName)

    def makePatternGrid(self):
        """ Pattern is now [Row-offset, Col-offset, Row-period, Col-period] where
        offset is calculated from the upper left corner (0, 0), while the
        scatter plot plots from lower left corner, so a flip has to be made
        in rows."""
        data = self._currentDataArray()
        if data is None:
            return
        if self._explicitPatternPoints is not None:
            x, y = self._explicitPatternPoints
            self._patternGrid = [x, y]
            self._widget.setPatternGridData(x=x, y=y)
            self._patternGridMade = True
            self._logger.debug('Showing detected lattice foci as pattern overlay')
            return
        shape = data.shape
        # The grid lives on the displayed plane, which is not axes 1/2 once the
        # dataset carries more than one navigation axis.
        plane = plane_axes(shape, self._currentAxisLabels())
        if plane is None:
            self._logger.error(f'Cannot build a pattern grid for shape {shape}')
            return
        numCols = shape[plane[0]]
        numRows = shape[plane[1]]
        numPointsCol = int(1 + np.floor(((numCols - 1) - self._pattern[1]) / self._pattern[3]))
        numPointsRow = int(1 + np.floor(((numRows - 1) - self._pattern[0]) / self._pattern[2]))
        colCoords = np.linspace(self._pattern[1],
                                self._pattern[1] + (numPointsCol - 1) * self._pattern[3],
                                numPointsCol)
        rowCoords = np.linspace(self._pattern[0],
                                self._pattern[0] + (numPointsRow - 1) * self._pattern[2],
                                numPointsRow)
        colCoords = np.repeat(colCoords, numPointsRow)
        rowCoords = np.tile(rowCoords, numPointsCol)

        self._patternGrid = [colCoords, rowCoords]
        self._widget.setPatternGridData(x=self._patternGrid[0], y=self._patternGrid[1])

        self._patternGridMade = True
        self._logger.debug('Made new pattern grid')

    def _currentDataArray(self):
        if self._dataObj is None or not _is_image_source(self._dataObj):
            return None
        handle = getattr(self._dataObj, "data_handle", None)
        if handle is not None and not getattr(self._dataObj, "dataMaterialized", False):
            return handle
        return self._dataObj.data

    def _currentAxisLabels(self):
        """Axis labels of the loaded data, or None to fall back to defaults."""
        return getattr(self._dataObj, "axis_labels", None)


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
