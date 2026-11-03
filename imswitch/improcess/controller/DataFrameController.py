from pathlib import Path

import numpy as np
from qtpy import QtCore

from imswitch.improcess.model import DataObj
from imswitch.improcess.model.lapse_source import (
    TIME_LAPSE_SOURCE_KIND,
    LazyTimeLapseArray,
    lapse_run_representatives,
    plan_time_lapse,
    read_lapse_headers,
)
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


class _TimepointLoadWorker(QtCore.QObject):
    """Prepare one logical-lapse timepoint away from the GUI thread."""

    sigLoaded = QtCore.Signal(object)
    sigAmbiguous = QtCore.Signal(object)
    sigFailed = QtCore.Signal(str)
    sigDone = QtCore.Signal()

    def __init__(self, source_obj, position, plan=None):
        super().__init__()
        self._sourceObj = source_obj
        self._position = int(position)
        self._plan = plan

    @QtCore.Slot()
    def run(self):
        try:
            plan = self._plan
            if plan is None:
                index = getattr(self._sourceObj, 'sourceMetadata', None)
                if index is None:
                    raise ValueError('Time-lapse source has no discovery metadata')
                headers = read_lapse_headers(index)
                if not bool(getattr(self._sourceObj, '_timeLapseRunExplicit', False)):
                    choices = lapse_run_representatives(index, headers)
                    if len(choices) > 1:
                        self.sigAmbiguous.emit(choices)
                        return
                plan = plan_time_lapse(index, headers)

            if not 0 <= self._position < len(plan.slots):
                raise IndexError(
                    f'Timepoint {self._position + 1} is outside this '
                    f'{len(plan.slots)}-point lapse'
                )

            slot = plan.slots[self._position]
            if slot.readable:
                dataset = slot.dataset or getattr(slot.ref, 'dataset', None)
                data_obj = DataObj(
                    Path(slot.ref.path).name,
                    str(dataset or ''),
                    path=str(slot.ref.path),
                )
                data_obj.checkAndLoadData()
                if not data_obj.dataLoaded:
                    raise RuntimeError(
                        f'Could not load timepoint {self._position + 1}'
                    )
            else:
                # Missing logical points remain navigable. Match the lapse
                # stack semantics by presenting the same zero-filled point
                # that LazyTimeLapseArray would return.
                lazy = LazyTimeLapseArray(plan)
                try:
                    array = lazy.timepoint(self._position)
                finally:
                    lazy.close()
                data_obj = DataObj(
                    getattr(self._sourceObj, 'name', 'time-lapse'),
                    f'T{self._position + 1}',
                )
                data_obj._data = np.asarray(array)
                data_obj._axis_labels = list(plan.item_axis_labels)
                data_obj._axis_scales = list(plan.item_axis_scales)
                data_obj._scale_unit = str(plan.scale_unit)
                data_obj._attrs = {}

            # Keep the logical source beside the ordinary image DataObj. The
            # rest of ImProcess continues to see a normal image source while
            # Current data can keep its T navigation and Loaded can keep the
            # logical lapse selected.
            data_obj._timeLapseParent = self._sourceObj
            data_obj._timeLapsePlan = plan
            data_obj._timeLapsePosition = self._position
            data_obj.sourceOriginalPath = getattr(
                self._sourceObj, 'sourceOriginalPath',
                getattr(self._sourceObj, 'dataPath', None),
            )
            self.sigLoaded.emit((plan, self._position, data_obj))
        except Exception as exc:
            self.sigFailed.emit(str(exc) or type(exc).__name__)
        finally:
            self.sigDone.emit()


class DataFrameController(ImProcessWidgetController):
    def __init__(self, *args, mainController=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._main = mainController

        self.editWindowController = self._factory.createController(
            DataEditController, self._widget.editWdw
        )

        self._dataObj = None
        self._pattern = []
        self._patternGrid = []
        self._patternGridMade = False
        self._patternVisible = False
        self._displayedImage = None
        self._timeLapseSource = None
        self._timeLapsePlan = None
        self._timepointPosition = 0
        self._timepointThread = None
        self._timepointWorker = None
        self._timepointOutcome = None
        self._timepointPendingRequest = None
        self._timepointRequestCounter = 0
        self._timepointExpectedRequestId = None

        self._commChannel.sigCurrentDataChanged.connect(self.currentDataChanged)
        self._commChannel.sigPatternUpdated.connect(self.patternUpdated)
        self._commChannel.sigPatternVisibilityChanged.connect(self.patternVisibilityChanged)
        self._commChannel.sigDetectionPreviewStyleChanged.connect(self.detectionPreviewStyleChanged)
        self._commChannel.sigDetectionPreviewUpdated.connect(self.detectionPreviewUpdated)
        self._commChannel.sigDetectionPreviewVisibilityChanged.connect(self.detectionPreviewVisibilityChanged)

        self._widget.sigShowMeanClicked.connect(lambda *_: self.showMean(explicit=True))
        self._widget.sigAdjustDataClicked.connect(self.adjustData)
        self._widget.sigUnloadDataClicked.connect(self.unloadData)
        self._widget.sigFrameNumberChanged.connect(self.setImgSlice)
        self._widget.sigFrameSliderChanged.connect(self.setImgSlice)
        self._widget.sigTimepointChanged.connect(self.setTimepoint)
        self._setCurrentDataProcessingEnabled(False)

    def _setCurrentDataProcessingEnabled(self, enabled):
        """Keep Actions consistent with the image actually shown as Current.

        A lazy timepoint request deliberately leaves the previous pixels on
        screen while disk I/O runs.  During that interval processing must not
        be allowed to act on the previous ``_currentDataObj`` under the new T
        value displayed in the navigation control.
        """
        if self._main is None:
            return
        view = getattr(self._main, '_widget', None)
        setter = getattr(view, 'setCurrentDataProcessingEnabled', None)
        if callable(setter):
            setter(bool(enabled))

    def currentDataReadyForProcessing(self):
        return (
            self._timepointThread is None
            and self._timepointPendingRequest is None
            and not self._widget.timepointLoading.isVisible()
        )

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

    def detectionPreviewStyleChanged(self, marker):
        self._widget.setDetectionPreviewStyle(marker)

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
        self._invalidateTimepointRequests(clear_context=True)
        self._dataObj = None
        self._displayedImage = None
        self.showMean()
        self._widget.setNumFrames(0)
        self._widget.setDataName('')
        self._widget.setDatasetName('')
        self._widget.setTimepointControls(0)
        setter = getattr(self._widget, 'setImageControlsEnabled', None)
        if callable(setter):
            setter(False)
        self._setCurrentDataProcessingEnabled(False)

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
        if inDataObj is None:
            self._invalidateTimepointRequests(clear_context=True)
            self._dataObj = None
            self._displayedImage = None
            self._widget.setTimepointControls(0)
            self._widget.setImage(np.zeros((1, 1)), autoLevels=True)
            self._widget.setNumFrames(0)
            self._widget.setDataName('')
            self._widget.setDatasetName('')
            setter = getattr(self._widget, 'setImageControlsEnabled', None)
            if callable(setter):
                setter(False)
            self._setCurrentDataProcessingEnabled(False)
            self._commChannel.sigDisplayedFrameChanged.emit()
            return

        source_kind = getattr(inDataObj, 'sourceKind', 'image')
        if source_kind == TIME_LAPSE_SOURCE_KIND:
            self._beginTimeLapse(inDataObj)
            return

        parent = getattr(inDataObj, '_timeLapseParent', None)
        if parent is not None:
            self._timeLapseSource = parent
            self._timeLapsePlan = getattr(inDataObj, '_timeLapsePlan', None)
            self._timepointPosition = int(
                getattr(inDataObj, '_timeLapsePosition', 0) or 0
            )
            total = (
                len(self._timeLapsePlan.slots)
                if self._timeLapsePlan is not None else
                int(getattr(getattr(parent, 'sourceMetadata', None), 'planned_count', 0) or 0)
            )
            self._widget.setTimepointControls(
                total, self._timepointPosition, loading=False
            )
        else:
            self._invalidateTimepointRequests(clear_context=True)
            self._widget.setTimepointControls(0)

        self._showImageDataObj(inDataObj)
        self._setCurrentDataProcessingEnabled(self.currentDataReadyForProcessing())

    def _showImageDataObj(self, inDataObj):
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
            self._widget.setDataName(getattr(self._dataObj, "name", ""))
            self._widget.setDatasetName(getattr(self._dataObj, "datasetName", ""))
            self._commChannel.sigDisplayedFrameChanged.emit()
            return
        data = self._currentDataArray()
        self._logger.debug(f'Data shape: {data.shape}')
        self.showMean()
        self._widget.setNumFrames(self._dataObj.numFrames)
        logical = getattr(self._dataObj, '_timeLapseParent', None)
        self._widget.setDataName(
            getattr(logical, 'name', self._dataObj.name) if logical is not None
            else self._dataObj.name
        )
        self._widget.setDatasetName(self._dataObj.datasetName)

    def openTimeLapseSource(self, source_obj):
        """Open a catalogued logical lapse without publishing metadata as Current."""
        if getattr(source_obj, 'sourceKind', None) != TIME_LAPSE_SOURCE_KIND:
            return
        self._beginTimeLapse(source_obj)

    def _beginTimeLapse(self, source_obj):
        """Keep a logical lapse current while one image point is loaded."""
        if self._timeLapseSource is not source_obj:
            self._invalidateTimepointRequests(clear_context=False)
            self._timeLapsePlan = None
            self._timepointPosition = 0
        self._timeLapseSource = source_obj
        self._dataObj = None
        index = getattr(source_obj, 'sourceMetadata', None)
        total = int(getattr(index, 'planned_count', 0) or 0)
        self._widget.setDataName(getattr(source_obj, 'name', ''))
        self._widget.setDatasetName('Time lapse')
        self._widget.setNumFrames(0)
        self._widget.setTimepointControls(total, 0, loading=True)
        setter = getattr(self._widget, 'setImageControlsEnabled', None)
        if callable(setter):
            setter(False)
        self._setCurrentDataProcessingEnabled(False)
        self._requestTimepoint(source_obj, 0)

    @QtCore.Slot(int)
    def setTimepoint(self, position):
        source = self._timeLapseSource
        if source is None:
            return
        total = (
            len(self._timeLapsePlan.slots)
            if self._timeLapsePlan is not None else
            int(getattr(getattr(source, 'sourceMetadata', None), 'planned_count', 0) or 0)
        )
        if total <= 0:
            return
        position = max(0, min(int(position), total - 1))
        if (
            self._timeLapsePlan is not None
            and position == self._timepointPosition
            and getattr(self._dataObj, '_timeLapseParent', None) is source
        ):
            return
        self._widget.setTimepointControls(total, position, loading=True)
        self._setCurrentDataProcessingEnabled(False)
        self._requestTimepoint(source, position)

    def _requestTimepoint(self, source_obj, position):
        self._timepointRequestCounter += 1
        request_id = self._timepointRequestCounter
        self._timepointExpectedRequestId = request_id
        self._timepointPendingRequest = (
            request_id, source_obj, int(position), self._timeLapsePlan
        )
        self._widget.setTimepointLoading(True)
        if self._timepointThread is None:
            self._startPendingTimepointRequest()

    def _startPendingTimepointRequest(self):
        request = self._timepointPendingRequest
        if request is None or self._timepointThread is not None:
            return
        self._timepointPendingRequest = None
        request_id, source_obj, position, plan = request
        thread = QtCore.QThread()
        worker = _TimepointLoadWorker(source_obj, position, plan)
        worker.moveToThread(thread)
        self._timepointOutcome = None
        self._timepointActiveRequest = request
        thread.started.connect(worker.run)
        worker.sigLoaded.connect(
            lambda result, rid=request_id: self._recordTimepointLoaded(rid, result)
        )
        worker.sigAmbiguous.connect(
            lambda choices, rid=request_id: self._recordTimepointAmbiguous(rid, choices)
        )
        worker.sigFailed.connect(
            lambda message, rid=request_id: self._recordTimepointFailed(rid, message)
        )
        worker.sigDone.connect(worker.deleteLater)
        worker.sigDone.connect(thread.quit)
        thread.finished.connect(lambda: self._timepointThreadFinished(thread))
        thread.finished.connect(thread.deleteLater)
        self._timepointThread = thread
        self._timepointWorker = worker
        thread.start()

    def _recordTimepointLoaded(self, request_id, result):
        self._timepointOutcome = ('loaded', int(request_id), result)

    def _recordTimepointAmbiguous(self, request_id, choices):
        self._timepointOutcome = ('ambiguous', int(request_id), tuple(choices or ()))

    def _recordTimepointFailed(self, request_id, message):
        self._timepointOutcome = ('failed', int(request_id), str(message or ''))

    def _timepointThreadFinished(self, thread):
        if self._timepointThread is not thread:
            return
        outcome = self._timepointOutcome
        self._timepointThread = None
        self._timepointWorker = None
        self._timepointOutcome = None
        self._timepointActiveRequest = None

        if outcome is not None:
            state, request_id, payload = outcome
            if request_id == self._timepointExpectedRequestId:
                if state == 'loaded':
                    self._applyLoadedTimepoint(payload)
                elif state == 'ambiguous':
                    self._resolveTimeLapseAmbiguity(payload)
                else:
                    total = (
                        len(self._timeLapsePlan.slots)
                        if self._timeLapsePlan is not None else
                        int(getattr(
                            getattr(self._timeLapseSource, 'sourceMetadata', None),
                            'planned_count', 0,
                        ) or 0)
                    )
                    self._widget.setTimepointControls(
                        total, self._timepointPosition, loading=False
                    )
                    self._setCurrentDataProcessingEnabled(
                        getattr(self._main, '_currentDataObj', None) is not None
                    )
                    _say(self, f'Could not load timepoint: {payload}')
                    self._logger.warning(f'Could not load timepoint: {payload}')
            elif state == 'loaded':
                # The operator selected another dataset while this worker was
                # finishing. Its pixels are no longer current, but the worker
                # must still keep them alive until QThread.finished.
                _plan, _position, stale_obj = payload
                stale_obj.checkAndUnloadData()

        if self._timepointPendingRequest is not None:
            self._startPendingTimepointRequest()
        elif self._timeLapseSource is None:
            # A stale worker may finish after the operator has switched to an
            # ordinary dataset. That dataset is already Current and should not
            # remain disabled just because the old worker had to finish safely.
            self._setCurrentDataProcessingEnabled(
                getattr(self._main, '_currentDataObj', None) is not None
            )

    def _resolveTimeLapseAmbiguity(self, choices):
        source = self._timeLapseSource
        file_io = getattr(self._main, 'fileIOController', None) if self._main else None
        chooser = getattr(file_io, 'chooseTimeLapseRun', None)
        selected = chooser(source, choices) if callable(chooser) and source is not None else None
        if selected is None:
            # The previous Current object was intentionally left untouched while
            # probing this lapse. Put its inspector state back if the operator
            # cancels the run choice.
            previous = getattr(self._main, '_currentDataObj', None) if self._main else None
            self._invalidateTimepointRequests(clear_context=True)
            self._widget.setTimepointControls(0)
            self._setCurrentDataProcessingEnabled(previous is not None)
            self._commChannel.sigCurrentDataChanged.emit(previous)
            return
        self._timeLapseSource = selected
        self._timeLapsePlan = None
        self._timepointPosition = 0
        total = int(getattr(getattr(selected, 'sourceMetadata', None), 'planned_count', 0) or 0)
        self._widget.setDataName(getattr(selected, 'name', ''))
        self._widget.setDatasetName('Time lapse')
        self._widget.setTimepointControls(total, 0, loading=True)
        self._setCurrentDataProcessingEnabled(False)
        self._requestTimepoint(selected, 0)

    def _applyLoadedTimepoint(self, result):
        plan, position, data_obj = result
        source = getattr(data_obj, '_timeLapseParent', None)
        if source is None or source is not self._timeLapseSource:
            data_obj.checkAndUnloadData()
            return

        self._timeLapsePlan = plan
        self._timepointPosition = int(position)
        total = len(plan.slots)
        self._widget.setTimepointControls(total, position, loading=False)

        if self._main is None:
            self._showImageDataObj(data_obj)
            return

        previous = getattr(self._main, '_currentDataObj', None)
        self._main._currentDataObj = data_obj
        self._commChannel.sigCurrentDataChanged.emit(data_obj)
        if previous is not None and previous is not source and previous is not data_obj:
            # Match ordinary Current-data replacement: keep the previous image
            # visible while disk I/O runs, then release it only after the new
            # timepoint has been published successfully.
            previous.checkAndUnloadData()

    def _invalidateTimepointRequests(self, *, clear_context):
        self._timepointRequestCounter += 1
        self._timepointExpectedRequestId = self._timepointRequestCounter
        self._timepointPendingRequest = None
        if clear_context:
            self._timeLapseSource = None
            self._timeLapsePlan = None
            self._timepointPosition = 0

    def closeEvent(self):
        # A timepoint worker owns open file handles and a QThread. Invalidate
        # its result, but keep the strong references until QThread.finished;
        # the controller factory's shutdown barrier waits on shutdownComplete.
        self._invalidateTimepointRequests(clear_context=True)
        return self.shutdownComplete()

    def shutdownComplete(self):
        return self._timepointThread is None

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
