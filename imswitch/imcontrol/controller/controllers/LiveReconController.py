"""Live reconstruction inside ImControl.

An ImProcess reconstructor is run over the acquisition stream as it happens:
frames are read from the detector's chunk queue (the same per-consumer
fan-out the recorder and BeadRec poll), described with exactly the attribute
block a recording would carry, and fed to ImProcess's live runtime
(``LiveReconstructionController`` with its stream and process workers) --
a streaming session for reconstructors that stream, one ``process()`` call
per complete stack for the rest. Results are shown as a layer in the main
viewer or in the panel's own image view.

Ownership, mirroring BeadRec: the detector is leased (``WORKFLOW``) at
``sigScanStarting`` so a trigger-driven camera is armed before any TTL
output, the chunk consumer is armed at the same moment, and both are released
once the run has drained after ``sigScanEnded``. A scan-driven detector
(APD, PMT) delivers its assembled volume once per scan and learns its shape
only when the scan is built, so its stream is set up at ``sigScanStarted``
instead. A run never joins a scan that is already producing frames: its
stacks are counted from the first frame, so it starts at a scan boundary
(``sigScanStarting`` or, for a later iteration, ``sigScanStarted``) or waits
for the next one. ImProcess is used as a
library only (its reconstructors, live runtime and result helpers); nothing
here touches its views or its module controller, and every ImProcess import
is lazy so ImControl starts without it.

After a run the held result can be sent to ImProcess's result list over the
module channel (``sigProcessingResultProduced``), and, when the run kept its
raw frames, the frames can be saved as an ImSwitch HDF5 recording with the
reconstruction next to it. The frames are otherwise gone once reconstructed:
the runtime consumes them, and nothing else records a scan that was not
recorded.
"""

from __future__ import annotations

import copy
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
from qtpy import QtCore

from imswitch.imcommon.model import dirtools
from imswitch.imcommon.model.memory_limits import describeBytes
from imswitch.imcommon.model.ome_metadata import micrometres_per_unit
from imswitch.imcontrol.model import configfiletools, getWidgetStatePersistence
from imswitch.imcontrol.model.managers import LeasePurpose
from imswitch.imcontrol.model.managers.detectors.DetectorManager import ChunkKind
from imswitch.imcontrol.model.managers.recording_metadata import (
    MODE_SCAN as OME_MODE_SCAN,
    MODE_TIMELAPSE as OME_MODE_TIMELAPSE,
    SOURCE_FORMAT_MEMORY,
    RecordingPlan,
    build_recording_attrs,
    completed_recording_attrs,
    expected_frames_for,
    exposure_time_ms_for,
)
from imswitch.imcontrol.view.widgets.LiveReconWidget import (
    DISPLAY_PANEL,
    DISPLAY_VIEWER,
    MODE_FREE,
    MODE_SCAN,
)
from ..basecontrollers import (
    ComponentStateApplyMode,
    ImConWidgetController,
    StatefulComponentMixin,
)
from ..display_transform import apply_display_transform, display_transform_from_properties


_TIME_LABELS = {'t', 'time', 'timepoint', 'timepoints'}


@dataclass
class _LiveRun:
    """One live reconstruction: a leased detector, a source, a name.

    ``source`` is ``None`` while the run holds the lease but has not been
    started: a scan-driven detector's stream is built once the scan is.
    """

    detectorName: str
    leaseHandle: Any
    source: Any
    name: str
    mode: str
    reconstructorId: str
    reconstructor: Any = None
    ended: bool = False
    keepRaw: bool = False
    #: (Nx, Ny, Nz) and the step sizes the recorder would label a scan's
    #: OME axes with; read at the scan start, since they describe that scan.
    scanDims: Any = None
    scanStepSizes: Any = None
    startedAt: float = field(default_factory=time.monotonic)

    @property
    def started(self) -> bool:
        return self.source is not None


@dataclass
class _SaveJob:
    """What one press of *Save raw data and reconstruction* writes."""

    rawPath: Optional[str]
    rawStack: Any
    rawRun: Optional[_LiveRun]
    reconPath: Optional[Path]
    result: Any


def _safeFileName(text: str) -> str:
    cleaned = re.sub(r'[^A-Za-z0-9._-]+', '_', str(text or '')).strip('._-')
    return cleaned or 'live_recon'


class LiveReconController(ImConWidgetController, StatefulComponentMixin):
    """ Linked to LiveReconWidget. """

    componentName = 'LiveRecon'
    stateSchemaVersion = 1
    setupModeDisplayName = 'Live reconstruction'
    setupModeCategory = 'analysis'
    setupModeHardwareCritical = False

    #: Chunk-consumer key registered on the detector.
    CONSUMER_KEY = 'LiveRecon'
    STATUS_INTERVAL_MS = 500
    #: What ImProcess registers when the setup names no reconstructors.
    DEFAULT_RECONSTRUCTOR_IDS = ('view-only',)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._specs: dict[str, tuple[str, str]] = {}
        self._offered: list[str] = []
        self._registry = None
        self._reconstructors: dict[str, Any] = {}
        self._reconstructorErrors: dict[str, str] = {}
        self._activeId: Optional[str] = None
        self._paramWidget = None
        self._liveEnabled = False
        self._syncingLiveButton = False
        self._run: Optional[_LiveRun] = None
        self._lastJobName: Optional[str] = None
        self._pendingRestart = False
        self._bus = None
        self._live = None
        self._heldResult = None
        self._renderScheduled = False
        self._closed = False
        # The raw frames the last finished run kept, and that run (its
        # attribute block and scan geometry describe the frames).
        self._rawStack = None
        self._rawRun: Optional[_LiveRun] = None
        self._saveThread: Optional[threading.Thread] = None

        self._statusTimer = QtCore.QTimer(self)
        self._statusTimer.setInterval(self.STATUS_INTERVAL_MS)
        self._statusTimer.timeout.connect(self._refreshStatus)

        self._populateDetectors()
        self._populateReconstructors()

        widget = self._widget
        widget.sigLiveToggled.connect(self.setLiveEnabled)
        widget.sigReconstructorChanged.connect(self.selectReconstructor)
        widget.sigLoadReconstructorRequested.connect(self.loadReconstructor)
        widget.sigModeChanged.connect(self._onModeChanged)
        widget.sigDisplayTargetChanged.connect(self._onDisplayTargetChanged)
        widget.sigFullLayerToggled.connect(lambda _checked: self._scheduleRender())
        widget.sigClearRequested.connect(self.clearResult)
        widget.sigSendToImProcessRequested.connect(self.sendResultToImProcess)
        widget.sigSaveRequested.connect(self.saveRawAndResult)

        self._commChannel.sigScanStarting.connect(self.onScanStarting)
        self._commChannel.sigScanStarted.connect(self.onScanStarted)
        self._commChannel.sigScanEnded.connect(self.onScanEnded)

        getWidgetStatePersistence().register('LiveRecon', self)

    # ------------------------------------------------------------------
    # Population
    # ------------------------------------------------------------------

    def _populateDetectors(self) -> None:
        manager = self._master.detectorsManager
        names, current = [], None
        try:
            names = list(manager.getAllDeviceNames(lambda c: c.forAcquisition))
        except Exception:
            try:
                names = list(manager.getAllDeviceNames())
            except Exception:
                names = []
        try:
            current = manager.getCurrentDetectorName()
        except Exception:
            current = None
        self._widget.setDetectors(names, current)

    def _configuredReconstructorIds(self) -> list[str]:
        """The setup's ``processing.reconstructors`` list, else ImProcess's default."""
        catchAll = getattr(self._setupInfo, '_catchAll', None) or {}
        processing = catchAll.get('processing') if isinstance(catchAll, dict) else None
        ids = processing.get('reconstructors') if isinstance(processing, dict) else None
        if isinstance(ids, (list, tuple)) and ids:
            return [str(item) for item in ids]
        return list(self.DEFAULT_RECONSTRUCTOR_IDS)

    def _populateReconstructors(self) -> None:
        try:
            from imswitch.improcess.reconstructors import available_reconstructor_specs
            specs = available_reconstructor_specs()
        except Exception as exc:
            self._logger.error(f'ImProcess reconstructors are unavailable: {exc}', exc_info=True)
            self._widget.setStatusText(f'ImProcess reconstructors are unavailable: {exc}')
            self._widget.setLiveEnabled(False)
            return
        self._specs = {plugin_id: (name, description) for plugin_id, name, description in specs}
        offered = [plugin_id for plugin_id in self._configuredReconstructorIds()
                   if plugin_id in self._specs]
        if not offered:
            offered = sorted(self._specs)
        self._offered = offered
        self._refreshReconstructorLists()
        if offered:
            self.selectReconstructor(offered[0])

    def _refreshReconstructorLists(self) -> None:
        self._widget.setReconstructors(
            [(plugin_id, *self._specs[plugin_id]) for plugin_id in self._offered],
            selected=self._activeId,
        )
        self._widget.setLoadableReconstructors(
            [(plugin_id, *self._specs[plugin_id]) for plugin_id in sorted(self._specs)
             if plugin_id not in self._offered]
        )

    # ------------------------------------------------------------------
    # Reconstructor selection
    # ------------------------------------------------------------------

    @property
    def reconstructorIds(self) -> list[str]:
        return list(self._offered)

    @property
    def activeReconstructorId(self) -> Optional[str]:
        return self._activeId

    def activeReconstructor(self):
        return self._reconstructors.get(self._activeId) if self._activeId else None

    def loadReconstructor(self, plugin_id: str) -> bool:
        """Offer one more of the reconstructors ImProcess knows, and select it."""
        plugin_id = str(plugin_id or '')
        if plugin_id not in self._specs:
            self._widget.setStatusText(f'Unknown reconstructor {plugin_id!r}')
            return False
        if plugin_id not in self._offered:
            self._offered.append(plugin_id)
            self._refreshReconstructorLists()
        return self.selectReconstructor(plugin_id)

    def selectReconstructor(self, plugin_id: str) -> bool:
        plugin_id = str(plugin_id or '')
        if not plugin_id:
            return False
        if plugin_id == self._activeId:
            self._widget.setSelectedReconstructor(plugin_id)
            return True
        reconstructor = self._reconstructorFor(plugin_id)
        if reconstructor is None:
            self._widget.setStatusText(
                f'Could not load reconstructor {plugin_id!r}: '
                f'{self._reconstructorErrors.get(plugin_id, "unknown error")}'
            )
            return False
        self._activeId = plugin_id
        self._widget.setSelectedReconstructor(plugin_id)
        self._installParamWidget(reconstructor)
        if not self._liveEnabled:
            self._widget.setStatusText(f'Idle: {getattr(reconstructor, "name", plugin_id)}')
        return True

    def _reconstructorFor(self, plugin_id: str):
        instance = self._reconstructors.get(plugin_id)
        if instance is not None:
            return instance
        try:
            from imswitch.improcess.reconstructors import register_reconstructor_by_id
            from imswitch.improcess.reconstructors.registry import PluginRegistry
            if self._registry is None:
                self._registry = PluginRegistry()
            instance = register_reconstructor_by_id(self._registry, plugin_id)
        except Exception as exc:
            self._logger.error(f'Could not load reconstructor {plugin_id!r}: {exc}', exc_info=True)
            self._reconstructorErrors[plugin_id] = str(exc)
            return None
        self._reconstructors[plugin_id] = instance
        return instance

    def _installParamWidget(self, reconstructor) -> None:
        widget = None
        try:
            widget = reconstructor.make_param_widget(self._widget)
        except Exception as exc:
            self._logger.warning(
                f'{getattr(reconstructor, "id", "?")}: parameter widget could not be '
                f'built ({exc}); using its default parameters'
            )
            widget = None
        self._paramWidget = widget
        self._widget.setParameterWidget(widget)

    def currentParams(self) -> dict:
        """The active reconstructor's defaults, overridden by its widget's values."""
        reconstructor = self.activeReconstructor()
        params: dict = {}
        if reconstructor is None:
            return params
        try:
            defaults = reconstructor.default_params()
            if isinstance(defaults, dict):
                params.update(defaults)
        except Exception:
            pass
        getter = getattr(self._paramWidget, 'get_values', None)
        if callable(getter):
            try:
                values = getter()
                if isinstance(values, dict):
                    params.update(values)
            except Exception as exc:
                self._logger.warning(f'Could not read reconstruction parameters: {exc}')
        return params

    # ------------------------------------------------------------------
    # Live toggle and scan lifecycle
    # ------------------------------------------------------------------

    @property
    def isLive(self) -> bool:
        return self._liveEnabled

    @property
    def hasActiveRun(self) -> bool:
        return self._run is not None

    def setLiveEnabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self._closed:
            return
        self._liveEnabled = enabled
        if self._widget.isLiveChecked() != enabled and not self._syncingLiveButton:
            self._syncingLiveButton = True
            try:
                self._widget.setLiveChecked(enabled)
            finally:
                self._syncingLiveButton = False
        if not enabled:
            self._pendingRestart = False
            if self._run is not None:
                self._stopRun(graceful=True)
            else:
                self._widget.setStatusText('Idle')
            return
        if self._activeId is None:
            self._widget.setStatusText('Select a reconstructor first')
            return
        mode = self._widget.getMode()
        if mode == MODE_FREE:
            self._beginRun(MODE_FREE)
        elif self._commChannel.isScanRunning():
            # A scan already producing frames cannot be joined: the stacks are
            # counted from the first frame the run sees. The next iteration
            # (sigScanStarted) or the next scan (sigScanStarting) starts it.
            self._widget.setStatusText(
                'Live: a scan is running; starting at its next iteration or the next scan'
            )
        else:
            self._widget.setStatusText('Live: waiting for the next scan')

    def onScanStarting(self) -> None:
        """A scan run is about to start: arm now, before any TTL output."""
        if self._closed or not self._liveEnabled or self._widget.getMode() != MODE_SCAN:
            return
        run = self._run
        if run is not None:
            if run.mode == MODE_SCAN and not run.ended:
                # A repeat or lapse iteration of the run already in progress.
                return
            # The previous run is still draining; start again at a boundary
            # once it is gone.
            self._pendingRestart = True
            return
        self._beginRun(MODE_SCAN)

    def onScanStarted(self) -> None:
        """An iteration's hardware is running: complete a deferred run, or start one.

        This is also a stack boundary, so a run that could not begin at
        ``sigScanStarting`` (live switched on during a lapse, the previous run
        still draining then) starts here.
        """
        if self._closed or not self._liveEnabled or self._widget.getMode() != MODE_SCAN:
            return
        run = self._run
        if run is not None:
            if not run.started and not run.ended:
                self._startRun(run)
            return
        self._pendingRestart = False
        self._beginRun(MODE_SCAN, atScanStarted=True)

    def onScanEnded(self) -> None:
        run = self._run
        if run is None or run.mode != MODE_SCAN or run.ended:
            return
        run.ended = True
        if not run.started:
            # Leased but never fed (the scan ended before it was built).
            self._onRunFinished()
            return
        run.source.mark_stream_ended()
        self._widget.setStatusText('Scan ended: finishing the reconstruction')

    def _onModeChanged(self, mode: str) -> None:
        if self._liveEnabled:
            # The run in progress keeps its mode; a new one needs a new arm.
            self.setLiveEnabled(False)
            self._widget.setStatusText('Mode changed: press Live to start again')

    def _onDisplayTargetChanged(self, target: str) -> None:
        if target == DISPLAY_PANEL:
            self._removeResultLayers(self._lastJobName)
        self._scheduleRender()

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    def _beginRun(self, mode: str, *, atScanStarted: bool = False) -> bool:
        """Lease the detector and start the run, or defer the start to the scan build."""
        if self._run is not None:
            return False
        manager = self._master.detectorsManager
        detectorName = self._widget.selectedDetector()
        if not detectorName:
            try:
                detectorName = manager.getCurrentDetectorName()
            except Exception:
                detectorName = None
        if not detectorName:
            self._widget.setStatusText('No detector to reconstruct from')
            return False
        reconstructor = self._reconstructorFor(self._activeId) if self._activeId else None
        if reconstructor is None:
            self._widget.setStatusText('Select a reconstructor first')
            return False
        if mode == MODE_FREE and getattr(reconstructor, 'requires_frame_stacks', False):
            self._widget.setStatusText(
                f'{getattr(reconstructor, "name", self._activeId)} needs whole scan '
                f'stacks; use the "During scans" mode'
            )
            return False

        try:
            handle = manager.acquire([detectorName], LeasePurpose.WORKFLOW)
        except Exception as exc:
            self._logger.error(
                f'Live reconstruction could not arm detector "{detectorName}": {exc}',
                exc_info=True,
            )
            self._widget.setStatusText(
                f'Could not arm "{detectorName}"; this scan will not be reconstructed'
            )
            return False

        name = f'{getattr(reconstructor, "name", self._activeId)} ({detectorName})'
        run = _LiveRun(
            detectorName=detectorName, leaseHandle=handle, source=None,
            name=name, mode=mode, reconstructorId=str(self._activeId),
            reconstructor=reconstructor, keepRaw=bool(self._widget.getKeepRaw()),
        )
        if mode == MODE_SCAN:
            run.scanDims, run.scanStepSizes = self._scanGeometry()
        self._run = run
        self._lastJobName = name
        self._heldResult = None
        self._dropRawStack()
        self._updateActions()

        scanDriven = bool(getattr(manager[detectorName], 'isScanDriven', False))
        if mode == MODE_SCAN and scanDriven and not atScanStarted:
            # The volume's shape is set when the scan is built; the frame
            # itself arrives at the scan's end, so nothing is missed by
            # waiting for sigScanStarted.
            self._widget.setStatusText('Armed: waiting for the scan to be built')
            return True
        return self._startRun(run)

    def _startRun(self, run: _LiveRun) -> bool:
        """Build and arm the source and start the ImProcess runtime on it."""
        try:
            source = self._buildSource(run.mode, run.detectorName, retain=run.keepRaw)
            source.arm()
        except Exception as exc:
            self._logger.error(
                f'Live reconstruction could not be prepared for "{run.detectorName}": {exc}',
                exc_info=True,
            )
            self._run = None
            self._releaseLease(run.leaseHandle)
            self._widget.setStatusText(f'Could not prepare the live reconstruction: {exc}')
            return False
        run.source = source

        live = self._liveController()
        params = self.currentParams()
        try:
            started = bool(live.start(
                run.reconstructor, source, params, source_arg=None, name=run.name,
            ))
        except Exception as exc:
            self._logger.error(f'Live reconstruction did not start: {exc}', exc_info=True)
            started = False
        if not started:
            self._run = None
            try:
                source.close()
            finally:
                self._releaseLease(run.leaseHandle)
            self._widget.setStatusText('The live reconstruction did not start')
            return False

        self._statusTimer.start()
        self._updateActions()
        self._widget.setStatusText('Armed: waiting for frames')
        return True

    def _buildSource(self, mode: str, detectorName: str, *, retain: bool = False):
        from imswitch.imcontrol.model.liverecon import (
            DetectorChunkLiveSource,
            build_live_stack_info,
            frame_shape_for_detector,
        )

        manager = self._master.detectorsManager
        detector = manager[detectorName]
        isScanDriven = bool(getattr(detector, 'isScanDriven', False))
        transform = self._frameTransformFor(detectorName)

        if mode == MODE_SCAN:
            plan = self._scanPlan(detectorName)
            framesPerStack = expected_frames_for(plan, detectorName, is_scan_driven=isScanDriven)
        else:
            plan = RecordingPlan('UntilStop', source_format=SOURCE_FORMAT_MEMORY)
            framesPerStack = max(1, int(self._widget.getFramesPerUpdate()))

        try:
            sharedAttrs = self._commChannel.sharedAttrs.getHDF5Attributes()
        except Exception:
            sharedAttrs = {}
        attrs = build_recording_attrs(
            plan, detectorName, sharedAttrs,
            expected_frames=framesPerStack,
            exposure_time_ms=exposure_time_ms_for(detector),
            start_time=datetime.now(timezone.utc).isoformat(),
        )
        dtype = np.dtype(getattr(detector, 'dtype', np.uint16))
        frameShape = frame_shape_for_detector(detector, transform, dtype)
        stackInfo = build_live_stack_info(
            detectorName, frameShape, dtype, attrs,
            frames_per_stack=framesPerStack, num_timepoints=None,
        )
        return DetectorChunkLiveSource(
            manager, detectorName, stackInfo,
            consumer_key=self.CONSUMER_KEY, chunk_kind=ChunkKind.RAW,
            frame_transform=transform, retain_frames=retain,
        )

    def _scanPlan(self, detectorName: str) -> RecordingPlan:
        """The plan a recording of this scan would run on, for this detector."""
        channel = self._commChannel
        recFrames = int(channel.getNumScanPositions())
        numCamTTL = dict(channel.getNumCamTTL() or {})
        layouts = {}
        source = channel.getActiveScanSource()
        accessor = getattr(source, 'getAcquisitionLayouts', None) if source is not None else None
        if callable(accessor):
            try:
                produced = accessor((detectorName,)) or {}
                layout = produced.get(detectorName)
                if layout is not None:
                    layouts[detectorName] = layout
            except Exception as exc:
                self._logger.warning(
                    f'The scan source declares no usable acquisition layout for '
                    f'"{detectorName}" ({exc}); reconstructing from the scan geometry'
                )
        return RecordingPlan(
            'ScanOnce', rec_frames=recFrames, num_cam_ttl=numCamTTL,
            acquisition_layouts=layouts, source_format=SOURCE_FORMAT_MEMORY,
        )

    def _scanGeometry(self) -> tuple:
        """``(scanDims, scanStepSizes)`` as the recorder reads them, or Nones.

        Asked of the active scan source first and of the channel second, so
        a rig with several scanners answers for the one about to run.
        """
        dims = steps = None
        owners = [self._commChannel]
        try:
            source = self._commChannel.getActiveScanSource()
        except Exception:
            source = None
        if source is not None:
            owners.insert(0, source)
        for owner in owners:
            getDims = getattr(owner, 'getDimsScan', None)
            getSteps = getattr(owner, 'getScanStepSizes', None)
            if not callable(getDims) or not callable(getSteps):
                continue
            try:
                dims = tuple(int(v) for v in getDims())
                steps = tuple(float(v) for v in getSteps())
            except Exception:
                dims = steps = None
                continue
            break
        return dims, steps

    def _frameTransformFor(self, detectorName: str):
        if not self._widget.getUseDisplayed():
            return None
        detectors = getattr(self._setupInfo, 'detectors', None) or {}
        info = detectors.get(detectorName) if hasattr(detectors, 'get') else None
        transform = display_transform_from_properties(
            getattr(info, 'managerProperties', None) if info is not None else None
        )
        if transform.is_identity:
            return None
        return lambda frame: apply_display_transform(np.asarray(frame), None, transform)[0]

    def _liveController(self):
        if self._live is None:
            from imswitch.improcess.controller.CommunicationChannel import (
                CommunicationChannel as ProcessingBus,
            )
            from imswitch.improcess.controller.LiveReconstructionController import (
                LiveReconstructionController,
            )
            self._bus = ProcessingBus()
            self._bus.sigLiveResultUpdated.connect(self._onLiveResult)
            self._bus.sigLiveTimepointUpdated.connect(self._onLiveTimepoint)
            self._live = LiveReconstructionController(self._bus)
            self._live.sigFinished.connect(self._onRunFinished)
        return self._live

    def _stopRun(self, *, graceful: bool) -> None:
        run = self._run
        if run is None:
            return
        run.ended = True
        if run.started:
            try:
                run.source.mark_stream_ended()
            except Exception:
                pass
        live = self._live
        if run.started and live is not None and live.is_running:
            live.stop(graceful=graceful, notify_finished=True)
        else:
            self._onRunFinished()

    def _releaseLease(self, handle) -> None:
        if handle is None:
            return
        try:
            self._master.detectorsManager.release(handle)
        except Exception as exc:
            self._logger.error(f'Live reconstruction failed to release its detector lease: {exc}',
                               exc_info=True)

    def _onRunFinished(self) -> None:
        run = self._run
        self._run = None
        self._statusTimer.stop()
        if run is not None:
            if run.started:
                try:
                    run.source.close()
                except Exception:
                    pass
            self._releaseLease(run.leaseHandle)
            if run.started:
                stats = run.source.stats
                text = f'Finished: {stats.frames_received} frames'
                if stats.frames_expected:
                    text += f' of {stats.frames_expected}'
                if stats.incomplete:
                    text += ' (incomplete)'
                if stats.frames_discarded:
                    text += f', {stats.frames_discarded} beyond the plan discarded'
                text += self._keepRawStack(run)
            else:
                text = 'Finished: the scan ended before any frame was expected'
            if not self._widget.getKeepLayer() and self._widget.getDisplayTarget() == DISPLAY_VIEWER:
                self._heldResult = None
                self._removeResultLayers(run.name)
            self._widget.setStatusText(text)
        self._updateActions()
        if self._pendingRestart and self._liveEnabled:
            # Started at the next boundary (onScanStarted / onScanStarting),
            # never in the middle of a scan already producing frames.
            self._widget.setStatusText(text + '; the next scan iteration starts a new run')

    def _refreshStatus(self) -> None:
        run = self._run
        if run is None or not run.started:
            return
        stats = run.source.stats
        text = f'Live: {stats.frames_received} frames received'
        if stats.frames_expected:
            text += f' of {stats.frames_expected}'
        session = getattr(self._live, '_session', None)
        completed = getattr(session, 'stacks_completed', None)
        if completed is not None:
            text += f', {completed} stack(s) reconstructed'
        if stats.frames_discarded:
            text += f', {stats.frames_discarded} discarded'
        if stats.overflow_events:
            text += f', {stats.overflow_events} overflow(s): incomplete'
        if run.keepRaw:
            text += f', keeping raw frames ({describeBytes(run.source.retained_bytes())})'
        self._widget.setStatusText(text)

    def _keepRawStack(self, run: _LiveRun) -> str:
        """Take the finished run's kept frames; returns the status note for them."""
        if not run.keepRaw:
            return ''
        try:
            stack = run.source.retained_stack()
        except Exception as exc:
            self._logger.error(f'Could not take the kept raw frames: {exc}', exc_info=True)
            return '; the raw frames could not be kept'
        finally:
            try:
                run.source.release_retained()
            except Exception:
                pass
        if stack is None:
            return '; no raw frames were received'
        self._rawStack = stack
        self._rawRun = run
        note = f'; raw frames kept: {stack.frames} ({describeBytes(stack.nbytes)})'
        if not stack.complete:
            note += ' (partial stack)'
        return note

    # ------------------------------------------------------------------
    # Results and display
    # ------------------------------------------------------------------

    def _onLiveResult(self, result) -> None:
        if result is None:
            return
        first = self._heldResult is None
        self._heldResult = result
        if first:
            self._updateActions()
        self._scheduleRender()

    def _onLiveTimepoint(self, index: int, plane) -> None:
        """Write one reconstructed timepoint into the held result's own buffer."""
        result = self._heldResult
        if result is None or plane is None:
            return
        try:
            axis = [str(label) for label in result.axis_labels].index('T')
        except (AttributeError, ValueError):
            return
        target = [slice(None)] * result.data.ndim
        target[axis] = slice(int(index), int(index) + 1)
        try:
            result.data[tuple(target)] = plane
        except Exception as exc:
            self._logger.debug(f'Could not write live timepoint {index}: {exc}')
            return
        self._scheduleRender()

    def _scheduleRender(self) -> None:
        if self._renderScheduled or self._closed:
            return
        self._renderScheduled = True
        QtCore.QTimer.singleShot(0, self._render)

    def _render(self) -> None:
        self._renderScheduled = False
        result = self._heldResult
        if result is None or self._closed:
            return
        jobName = self._lastJobName or getattr(result, 'name', 'Live reconstruction')
        try:
            if self._widget.getDisplayTarget() == DISPLAY_PANEL:
                plane = self._displayPlane(result)
                if plane is not None:
                    self._widget.setImage(plane)
            else:
                layers = self._layerDataFor(result, jobName)
                if layers:
                    self._emitResultLayers(jobName, layers)
        except Exception as exc:
            self._logger.error(f'Could not display the live reconstruction: {exc}', exc_info=True)

    def _layerDataFor(self, result, jobName: str) -> list:
        """napari ``(data, kwargs, layer_type)`` tuples for the main viewer, in µm."""
        from imswitch.improcess.model.napari_layers import NotLayerable, result_to_layer_data

        try:
            layers = result_to_layer_data(result)
        except NotLayerable as exc:
            self._logger.debug(f'Live result cannot be shown as a layer: {exc}')
            return []
        fullLayer = self._widget.getFullLayer()
        prepared = []
        for data, kwargs, kind in layers:
            kwargs = dict(kwargs)
            kwargs.pop('units', None)
            metadata = kwargs.get('metadata') or {}
            labels = [str(label) for label in (metadata.get('axis_labels') or [])]
            unit = str(metadata.get('scale_unit') or 'px')
            scale = kwargs.get('scale')
            if kind in ('image', 'labels'):
                data = np.asarray(data)
                if not fullLayer and data.ndim > 2:
                    data = data[self._planeIndex(data.shape, labels)]
                    if scale is not None:
                        scale = tuple(scale)[-data.ndim:]
            if scale is not None:
                factor = micrometres_per_unit(unit)
                if factor is not None:
                    scale = tuple(float(value) * factor for value in scale)
                kwargs['scale'] = tuple(float(value) for value in scale)
            baseName = kwargs.get('name') or jobName
            kwargs['name'] = f'Recon: {baseName}'
            prepared.append((data, kwargs, kind))
        return prepared

    @staticmethod
    def _planeIndex(shape, labels) -> tuple:
        """Index the latest timepoint and the middle of every other leading axis."""
        index = []
        for axis in range(len(shape) - 2):
            label = labels[axis].lower() if axis < len(labels) else ''
            if label in _TIME_LABELS or 'time' in label:
                index.append(max(0, int(shape[axis]) - 1))
            else:
                index.append(int(shape[axis]) // 2)
        return tuple(index)

    def _displayPlane(self, result):
        data = getattr(result, 'data', None)
        if data is None:
            return None
        data = np.asarray(data)
        if data.ndim < 2:
            return None
        if data.ndim == 2:
            return data
        labels = [str(label) for label in (getattr(result, 'axis_labels', None) or [])]
        return data[self._planeIndex(data.shape, labels)]

    def _emitResultLayers(self, jobName: str, layers: list) -> None:
        signal = getattr(self._commChannel, 'sigResultLayersUpdated', None)
        if signal is not None:
            signal.emit(jobName, layers)

    def _removeResultLayers(self, jobName: Optional[str]) -> None:
        if not jobName:
            return
        signal = getattr(self._commChannel, 'sigResultLayersRemoved', None)
        if signal is not None:
            signal.emit(jobName)

    def clearResult(self) -> None:
        self._heldResult = None
        self._dropRawStack()
        self._widget.clearImage()
        self._removeResultLayers(self._lastJobName)
        self._updateActions()

    # ------------------------------------------------------------------
    # Actions on the held result: send to ImProcess, save with raw frames
    # ------------------------------------------------------------------

    @property
    def rawStack(self):
        """The raw frames kept from the last finished run (``RetainedStack``), or None."""
        return self._rawStack

    def _dropRawStack(self) -> None:
        self._rawStack = None
        self._rawRun = None

    def _updateActions(self) -> None:
        widget = self._widget
        widget.setSendToImProcessEnabled(self._heldResult is not None)
        widget.setSaveEnabled(
            (self._heldResult is not None or self._rawStack is not None)
            and self._run is None and self._saveThread is None
        )

    def _imProcessSignal(self):
        """The module-channel signal into ImProcess, when ImProcess is loaded."""
        channel = self._moduleCommChannel
        signal = getattr(channel, 'sigProcessingResultProduced', None)
        if signal is None:
            return None
        try:
            if not channel.isModuleRegistered('improcess'):
                return None
        except Exception:
            return None
        return signal

    def sendResultToImProcess(self) -> bool:
        """Offer the held result to ImProcess's result list.

        Once the run has finished the object itself crosses (the modules
        share a process, so nothing is copied). While a run is still
        writing into it, a snapshot of the data goes instead, so the entry
        in ImProcess does not keep changing under the user.
        """
        result = self._heldResult
        if result is None:
            self._widget.setStatusText('No reconstruction to send yet')
            return False
        signal = self._imProcessSignal()
        if signal is None:
            self._widget.setStatusText('ImProcess is not loaded; there is nothing to send to')
            return False
        name = self._lastJobName or str(getattr(result, 'name', '') or 'Live reconstruction')
        if self._run is not None and self._run.started:
            snapshot = self._snapshotResult(result)
            if snapshot is None:
                self._widget.setStatusText(
                    'The reconstruction is still being written; try again when the run has ended'
                )
                return False
            result = snapshot
        signal.emit(result, name)
        self._widget.setStatusText(f'Sent to ImProcess: {name}')
        return True

    @staticmethod
    def _snapshotResult(result):
        """A copy of ``result`` with its own data buffer and identity, or None."""
        try:
            snapshot = copy.copy(result)
            snapshot.data = np.array(result.data, copy=True)
        except Exception:
            return None
        try:
            from imswitch.imcommon.algorithms.spatial_frame import mint_uid
            snapshot.result_uid = mint_uid('result')
        except Exception:
            pass
        metadata = getattr(result, 'metadata', None)
        if isinstance(metadata, dict):
            try:
                snapshot.metadata = copy.deepcopy(metadata)
            except Exception:
                snapshot.metadata = dict(metadata)
        if hasattr(snapshot, 'artifacts'):
            snapshot.artifacts = []
        return snapshot

    def saveRawAndResult(self) -> bool:
        """Write the kept raw frames and the held reconstruction to disk.

        The raw frames become an ImSwitch HDF5 recording (the detector group
        with the same attribute block a recording of the scan would carry),
        at the path chosen; the reconstruction is saved next to it with a
        ``_recon`` suffix in the first format the result supports (OME-TIFF
        when it does). Writing runs on a worker thread; the status line
        reports the outcome.
        """
        if self._run is not None:
            self._widget.setStatusText('Wait for the run to finish before saving')
            return False
        if self._saveThread is not None:
            self._widget.setStatusText('A save is still in progress')
            return False
        result, stack = self._heldResult, self._rawStack
        if result is None and stack is None:
            self._widget.setStatusText('Nothing to save: no reconstruction and no raw frames kept')
            return False
        path = self._widget.askForSavePath(self._suggestedSavePath())
        if not path:
            return False
        rawPath = str(path)
        if not rawPath.lower().endswith(('.h5', '.hdf5')):
            rawPath += '.h5'
        stem = os.path.splitext(rawPath)[0]
        job = _SaveJob(
            rawPath=rawPath if stack is not None else None,
            rawStack=stack, rawRun=self._rawRun,
            reconPath=Path(f'{stem}_recon') if result is not None else None,
            result=result,
        )
        self._saveThread = threading.Thread(
            target=self._runSave, args=(job,), name='LiveReconSave', daemon=True,
        )
        self._saveThread.start()
        self._updateActions()
        self._widget.setStatusText(f'Saving to {os.path.dirname(rawPath) or "."}…')
        return True

    def _runSave(self, job: _SaveJob) -> None:
        """Worker thread: write what the job names, then report on the controller thread."""
        written: list[str] = []
        errors: list[str] = []
        if job.rawPath and job.rawStack is not None:
            try:
                self._writeRawStack(job.rawStack, job.rawRun, job.rawPath)
                written.append(job.rawPath)
            except Exception as exc:
                self._logger.error(f'Could not save the raw frames: {exc}', exc_info=True)
                errors.append(f'raw frames: {exc}')
        if job.reconPath is not None and job.result is not None:
            try:
                written.append(self._writeResult(job.result, job.reconPath))
            except Exception as exc:
                self._logger.error(f'Could not save the reconstruction: {exc}', exc_info=True)
                errors.append(f'reconstruction: {exc}')
        self._invokeOnControllerThread(lambda: self._onSaveFinished(written, errors))

    def _writeRawStack(self, stack, run: Optional[_LiveRun], rawPath: str) -> None:
        from imswitch.imcontrol.model.managers.RecordingManager import (
            HDF5Storer,
            annotationsFromAttrs,
        )

        detectorName = run.detectorName if run is not None else str(self._widget.selectedDetector())
        planned = dict(run.source.stack_info.attrs) if run is not None and run.started else {}
        planned['recording:lapse_index'] = int(stack.stack_index)
        stats = run.source.stats if run is not None and run.started else None
        attrs = completed_recording_attrs(
            planned, stack.frames,
            discarded_frames=int(getattr(stats, 'frames_discarded', 0) or 0),
            frames_missing=(not stack.complete) or bool(getattr(stats, 'overflow_events', 0)),
        )
        manager = self._master.detectorsManager
        storer = HDF5Storer(os.path.splitext(rawPath)[0], manager)
        omeMeta = self._omeMetaFor(run, detectorName, stack.frames, annotationsFromAttrs(attrs))
        if omeMeta is not None:
            storer.omeMeta = {detectorName: omeMeta}
        if os.path.exists(rawPath):
            # The file dialog already asked about overwriting.
            os.remove(rawPath)
        storer.writeStack(rawPath, detectorName, stack.data, attrs)

    def _omeMetaFor(self, run: Optional[_LiveRun], detectorName: str, nFrames: int, annotations):
        """The recorder's OME description of the stack, or None when it cannot be built."""
        recordingManager = getattr(self._master, 'recordingManager', None)
        build = getattr(recordingManager, 'buildOmeMeta', None)
        if not callable(build):
            return None
        mode = OME_MODE_SCAN if run is not None and run.mode == MODE_SCAN else OME_MODE_TIMELAPSE
        try:
            return build(
                detectorName, mode, max(1, int(nFrames)),
                scanDims=run.scanDims if run is not None else None,
                scanStepSizes=run.scanStepSizes if run is not None else None,
                annotations=annotations,
            )
        except Exception as exc:
            self._logger.warning(f'No OME metadata for the raw frames: {exc}')
            return None

    @staticmethod
    def _writeResult(result, reconPath: Path) -> str:
        formats = tuple(getattr(result, 'supported_formats', ()) or ('tiff',))
        fmt = 'tiff' if 'tiff' in formats else formats[0]
        try:
            receipt = result.save(reconPath, fmt, overwrite=True)
        except TypeError:
            receipt = result.save(reconPath, fmt)
        primary = getattr(receipt, 'primary', None)
        return str(primary) if primary is not None else str(reconPath)

    def _onSaveFinished(self, written: list, errors: list) -> None:
        self._saveThread = None
        if self._closed:
            return
        parts = []
        if written:
            parts.append('Saved ' + ', '.join(os.path.basename(path) for path in written))
        if errors:
            parts.append('not saved: ' + '; '.join(errors))
        self._widget.setStatusText('; '.join(parts) or 'Nothing was saved')
        self._updateActions()

    def _suggestedSavePath(self) -> str:
        run = self._rawRun or self._run
        detector = run.detectorName if run is not None else (self._widget.selectedDetector() or 'raw')
        name = _safeFileName(self._lastJobName or 'live_recon')
        stamp = time.strftime('%Y%m%d-%H%M%S')
        return os.path.join(self._defaultSaveFolder(), f'{name}_{stamp}_{detector}.h5')

    def _defaultSaveFolder(self) -> str:
        """Today's recordings folder, as the Recording widget uses it."""
        try:
            options, _ = configfiletools.loadOptions()
            folder = options.recording.folderFor()
        except Exception:
            folder = os.path.join(dirtools.UserFileDirs.Root, 'recordings')
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError:
            pass
        return folder

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def getComponentState(self) -> dict:
        return {
            'reconstructor': self._activeId,
            'detector': self._widget.selectedDetector(),
            'mode': self._widget.getMode(),
            'display': self._widget.getDisplayTarget(),
            'framesPerUpdate': self._widget.getFramesPerUpdate(),
            'useDisplayed': self._widget.getUseDisplayed(),
            'fullLayer': self._widget.getFullLayer(),
            'keepLayer': self._widget.getKeepLayer(),
            'keepRaw': self._widget.getKeepRaw(),
        }

    def applyComponentState(self, state: dict, *, applyMode: ComponentStateApplyMode) -> list[str]:
        warnings: list[str] = []
        if not isinstance(state, dict):
            return ['Live reconstruction state is not a mapping; skipped.']
        reconstructor = state.get('reconstructor')
        if reconstructor:
            if reconstructor in self._specs:
                if not self.loadReconstructor(str(reconstructor)):
                    warnings.append(f'Reconstructor {reconstructor!r} could not be loaded.')
            else:
                warnings.append(f'Reconstructor {reconstructor!r} is not available.')
        detector = state.get('detector')
        if detector and not self._widget.setSelectedDetector(str(detector)):
            warnings.append(f'Detector {detector!r} is not available.')
        mode = state.get('mode')
        if mode in (MODE_SCAN, MODE_FREE):
            self._widget.setMode(mode)
        display = state.get('display')
        if display in (DISPLAY_VIEWER, DISPLAY_PANEL):
            self._widget.setDisplayTarget(display)
        frames = state.get('framesPerUpdate')
        if isinstance(frames, int) and frames > 0:
            self._widget.setFramesPerUpdate(frames)
        for key, setter in (
            ('useDisplayed', self._widget.setUseDisplayed),
            ('fullLayer', self._widget.setFullLayer),
            ('keepLayer', self._widget.setKeepLayer),
            ('keepRaw', self._widget.setKeepRaw),
        ):
            if key in state:
                setter(bool(state[key]))
        return warnings

    def describeComponentState(self, state: dict) -> list[str]:
        if not isinstance(state, dict):
            return []
        mode = 'during scans' if state.get('mode') == MODE_SCAN else 'free-running'
        return [
            f"Live reconstruction: {state.get('reconstructor') or 'none'} on "
            f"{state.get('detector') or 'current detector'}, {mode}, shown in "
            f"{'the main viewer' if state.get('display') != DISPLAY_PANEL else 'the panel'}"
        ]

    def getComponentStateHazards(self, state: dict, *, applyMode, context=None) -> list[dict]:
        return []

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def closeEvent(self) -> None:
        self._closed = True
        self._pendingRestart = False
        self._statusTimer.stop()
        try:
            self._stopRun(graceful=False)
        except Exception as exc:
            self._logger.error(f'Live reconstruction did not stop cleanly: {exc}', exc_info=True)
        thread = self._saveThread
        if thread is not None and thread.is_alive():
            thread.join(timeout=10.0)
        self._dropRawStack()
        try:
            getWidgetStatePersistence().unregister('LiveRecon')
        except Exception:
            pass
        super().closeEvent()
