import numpy as np

from imswitch.improcess.model.plane_navigation import extract_plane, plane_axes

from .DataEditController import DataEditController
from .basecontrollers import ImProcessWidgetController


def _is_image_source(data_obj):
    return getattr(data_obj, 'sourceKind', 'image') == 'image'


def _mean_preview_notice(data_obj):
    """Why the mean preview is worth saying first, or None (fits, or unknown)."""
    notice = getattr(data_obj, 'meanPreviewNotice', None)
    if not callable(notice):
        return None
    try:
        return notice()
    except Exception:
        return None


def _plane_read_is_bounded(data_obj):
    """False only when the object says a plane read decodes the whole series."""
    bounded = getattr(data_obj, 'planeReadIsBounded', None)
    if not callable(bounded):
        return True
    try:
        return bool(bounded())
    except Exception:
        return True


def _say(controller, message):
    """A line where the operator is looking, if the channel has a status bar."""
    comm_channel = getattr(controller, '_commChannel', None)
    if hasattr(comm_channel, 'sigStatusMessage'):
        comm_channel.sigStatusMessage.emit(message)


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
        self._displayedImage = None

        self._commChannel.sigCurrentDataChanged.connect(self.currentDataChanged)
        self._commChannel.sigPatternUpdated.connect(self.patternUpdated)
        self._commChannel.sigPatternVisibilityChanged.connect(self.patternVisibilityChanged)
        self._commChannel.sigDetectionPreviewUpdated.connect(self.detectionPreviewUpdated)
        self._commChannel.sigDetectionPreviewVisibilityChanged.connect(self.detectionPreviewVisibilityChanged)

        self._widget.sigShowMeanClicked.connect(lambda *_: self.showMean(explicit=True))
        self._widget.sigAdjustDataClicked.connect(self.adjustData)
        self._widget.sigUnloadDataClicked.connect(self.unloadData)
        self._widget.sigFrameNumberChanged.connect(self.setImgSlice)
        self._widget.sigFrameSliderChanged.connect(self.setImgSlice)

    def patternUpdated(self, pattern):
        self._pattern = pattern
        self._patternGridMade = False
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

    def showMean(self, explicit=False):
        """Display the mean plane -- automatically on load, or on request.

        The mean is estimated before it is computed: a float64 accumulator and
        a float32 result of one plane, which the plane-count bound on the
        preview says nothing about. Above the processing working set the
        *automatic* preview is skipped in favour of the first plane, with the
        estimate and the setting said where the operator is looking; the
        *explicit* one (the Show mean button) is what they asked for, so it is
        computed after the same line. The preview keeps its native coordinates
        either way: the pattern finder and the detection preview take
        pixel-unit parameters from this image, so it is never approximated in
        plane.
        """
        if self._dataObj is None or not _is_image_source(self._dataObj):
            img = np.zeros((1, 1))
        else:
            notice = _mean_preview_notice(self._dataObj)
            if notice is not None and not explicit:
                data = self._currentDataArray()
                if data is not None and _plane_read_is_bounded(self._dataObj):
                    self._logger.info(f'{notice} Showing the first plane instead; '
                                      f'Show mean computes it on request.')
                    _say(self, f'{notice} Showing the first plane; Show mean computes it.')
                    self.setImgSlice(0)
                    return
                # A plane read that decodes the whole series is the very cost
                # being avoided, so nothing is read until asked.
                self._logger.info(f'{notice} Nothing shown until asked; Show mean '
                                  f'computes it on request.')
                _say(self, f'{notice} Nothing shown until asked; Show mean computes it.')
                img = np.zeros((1, 1))
            else:
                if notice is not None:
                    self._logger.warning(f'{notice} Computing it as requested.')
                    _say(self, f'{notice} Computing it as requested.')
                img = self._dataObj.getMeanData()
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
