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
from imswitch.imcontrol.model.managers._scan_execution import (
    EXCLUDED_KEY, PARTICIPANTS_KEY, ScanExecutionCoordinator,
)
from imswitch.imcommon.model import initLogger
from imswitch.imcommon.model.contrast import filled_auto_levels
import numpy as np
import re
from qtpy import QtCore

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
        self.__pendingResultLayers = {}
        self.__resultRenderScheduled = False
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
        if hasattr(self._commChannel, 'sigResultLayersUpdated'):
            self._commChannel.sigResultLayersUpdated.connect(self.resultLayersUpdated)
            self._commChannel.sigResultLayersRemoved.connect(self.resultLayersRemoved)

        # The scan lifecycle: which detectors a scan uses (a detector left out
        # must not keep a stale stack and its slider), when a run starts (its
        # scan-driven detectors are levelled on their first real frame, and
        # the view follows the planes being written again), when it ends.
        self._levelPending = set()
        nidaqBuilt = getattr(getattr(self._master, 'nidaqManager', None), 'sigScanBuilt', None)
        if nidaqBuilt is not None:
            nidaqBuilt.connect(self._onNidaqScanBuilt)
        for signalName, slot in (('sigScanStarting', self._onScanRunStarting),
                                 ('sigScanStarted', self._onScanIterationStarted),
                                 ('sigScanEnded', self._onScanRunEnded)):
            signal = getattr(self._commChannel, signalName, None)
            if signal is not None:
                signal.connect(slot)

        # Connect ModuleCommunicationChannel signals if available
        if self._moduleCommChannel is not None:
            self._moduleCommChannel.sigLiveReconResult.connect(self.liveReconResultAvailable)
            self._moduleCommChannel.sigShowImage.connect(self.showImage)

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
                self._widget.setOverlayPixelScale(display_scale, detectorName)

            if not init or self._shouldResetView:
                self.adjustFrame(shape=display_im.shape, instantResetView=True)

            plane = self._livePlaneOf(detectorName, published_shape, display_im)
            if hasattr(self._widget, 'followLivePlane'):
                self._widget.followLivePlane(detectorName, plane)
            if detectorName in self.__dict__.get('_levelPending', ()):
                self._levelFirstRealFrame(detectorName, display_im, plane)

    # -- scans: the plane being written, the first frame's levels --------------

    def _livePlaneOf(self, detectorName, publishedShape, shownImage):
        """Where the scan writes in the shown image's leading axes (the index
        a scan-driven detector files as ``livePlaneIndex`` for the frame it
        just published), with the summed axes taken out; None when there is
        nothing to follow."""
        try:
            plane = getattr(self._master.detectorsManager[detectorName], 'livePlaneIndex', None)
        except Exception:
            return None
        if plane is None:
            return None
        try:
            plane = tuple(int(v) for v in plane)
        except (TypeError, ValueError):
            return None
        if len(plane) != max(0, len(publishedShape) - 2):
            return None
        told = self.__dict__.get('_toldAxes', {}).get(detectorName)
        labels, summed = told if told is not None else ([], ())
        if summed:
            plane = tuple(i for i, label in zip(plane, labels) if label not in summed)
        if len(plane) != max(0, np.ndim(shownImage) - 2):
            return None
        return plane

    def _levelFirstRealFrame(self, detectorName, image, plane):
        """Level a scan-driven detector once per run, on the first frame with
        pixels in it (the empty frame published at build has none), from the
        filled pixels of the plane being written -- not from the unfilled
        zeros, and not again during the run: the levels are the user's after
        that (Auto levels, the levels box)."""
        sample = image
        if plane:
            try:
                sample = np.asarray(image)[tuple(plane)]
            except IndexError:
                sample = image
        levels = filled_auto_levels(sample)
        if levels is None and sample is not image:
            levels = filled_auto_levels(image)      # the plane is still empty
        if levels is None:
            return
        self._levelPending.discard(detectorName)
        self._widget.setImageDisplayLevels(detectorName, *levels)

    def _scanDrivenDetectorNames(self):
        """The detectors this viewer shows that a scan drives (APD, PMT,
        Time Tagger), asked one by one so one odd detector cannot hide the
        others."""
        names = set()
        for name in list(getattr(self._widget, 'imgLayers', {}) or {}):
            try:
                if getattr(self._master.detectorsManager[name], 'isScanDriven', False) is True:
                    names.add(name)
            except Exception:
                continue
        return names

    def _onScanRunStarting(self):
        self._levelPending = self._scanDrivenDetectorNames()
        if hasattr(self._widget, 'setScanActive'):
            self._widget.setScanActive(True)

    def _onScanRunEnded(self):
        self._levelPending = set()
        if hasattr(self._widget, 'setScanActive'):
            self._widget.setScanActive(False)

    def _onNidaqScanBuilt(self, scanInfoDict, *_args):
        """An NI-DAQ scan is built: its ``scanInfoDict`` names the detectors
        taking part (or, failing that, the scan-driven ones left out)."""
        if not isinstance(scanInfoDict, dict):
            return
        participants = scanInfoDict.get(PARTICIPANTS_KEY)
        if participants is not None:
            self._retireNonParticipants(set(map(str, participants)))
            return
        excluded = scanInfoDict.get(EXCLUDED_KEY)
        if excluded:
            live = set(getattr(self._widget, 'imgLayers', {}) or {})
            self._retireNonParticipants(live - set(map(str, excluded)))

    def _onScanIterationStarted(self):
        """Any scan backend: the coordinator's active iteration knows its
        participants by now. TriggerScope publishes ``sigScanBuilt`` before
        it arms the coordinator (and without a scanInfoDict), so for it this
        is the first moment they are known; for NI-DAQ it repeats the build's
        answer and changes nothing."""
        coordinator = getattr(self._master, 'scanExecutionCoordinator', None)
        if not isinstance(coordinator, ScanExecutionCoordinator):
            return
        token = coordinator.activeToken
        participants = getattr(token, 'participants', None)
        if participants is None:
            return
        self._retireNonParticipants(set(map(str, participants)))

    def _retireNonParticipants(self, participants):
        """Reduce the live layer of every detector not in this scan to the
        plane it shows, so a stack left from an earlier scan no longer keeps
        the viewer's slider. Its display settings are kept; one log line per
        reduced layer says what was dropped."""
        collapse = getattr(self._widget, 'collapseLiveLayerToShownPlane', None)
        if collapse is None:
            return
        for detectorName in list(getattr(self._widget, 'imgLayers', {}) or {}):
            if detectorName in participants:
                continue
            result = collapse(detectorName)
            if result is None:
                continue
            oldShape, index = result
            self.__logger.info(
                f'{detectorName} is not in this scan: its live layer {tuple(oldShape)} '
                f'now shows only the plane it was showing, index {tuple(index)}, as 2-D '
                f'(the rest of that stack is no longer in the viewer)'
            )
            # Its last stack must not come back through a Sum box, and the
            # pixels shown no longer carry that scan's geometry.
            self.__dict__.get('_lastFrames', {}).pop(detectorName, None)
            self.__dict__.get('_toldAxes', {}).pop(detectorName, None)
            if hasattr(self._widget, 'setSummableAxes'):
                self._widget.setSummableAxes(detectorName, [], None)
            if self._shownScanGeometry.get(detectorName) is not None:
                self._shownScanGeometry[detectorName] = None
                self._commChannel.sigScanGeometryShown.emit(detectorName, None)

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
        """A live result from ImProcess (module channel): one layer per name.

        Routed through the result-layer path, so a stream of updates keeps
        replacing the same layer instead of adding one per update.
        """
        kwargs = {'name': str(name)}
        if scale is not None:
            kwargs['scale'] = tuple(float(v) for v in scale)
        self.resultLayersUpdated(f'ImProcess: {name}', [(np.asarray(image), kwargs, 'image')])

    def resultLayersUpdated(self, jobName, layerData):
        """Keep the newest layer data per job and draw once the event loop is free.

        A producer that publishes faster than the viewer redraws must not
        queue up redraws: only the latest data of each job is drawn.
        """
        self.__pendingResultLayers[jobName] = list(layerData or [])
        if not self.__resultRenderScheduled:
            self.__resultRenderScheduled = True
            QtCore.QTimer.singleShot(0, self._renderPendingResultLayers)

    def resultLayersRemoved(self, jobName):
        self.__pendingResultLayers.pop(jobName, None)
        self._widget.removeResultLayers(jobName)

    def _renderPendingResultLayers(self):
        self.__resultRenderScheduled = False
        pending, self.__pendingResultLayers = self.__pendingResultLayers, {}
        for jobName, layerData in pending.items():
            try:
                created = self._widget.setResultLayers(jobName, layerData)
            except Exception:
                self.__logger.error(
                    f'Could not draw the result layers of {jobName!r}', exc_info=True
                )
                continue
            # An empty viewer fits itself to the first thing shown in it, as
            # it does for a snap; a viewer already showing a live view keeps
            # the user's zoom.
            if created and self.__dict__.get('_shouldResetView', False) and layerData:
                shape = getattr(layerData[0][0], 'shape', None)
                if shape is not None:
                    self.adjustFrame(shape, instantResetView=True)

    def showImage(self, name, image, scale):
        """ A script's show(): the array in the layer called ``name``,
        replacing that layer's data when it exists. """
        try:
            isNew = self._widget.setStaticLayer(name, image, scale)
        except Exception:
            self.__logger.error(f'Could not show {name!r}', exc_info=True)
            return
        if isNew and self._shouldResetView:
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
