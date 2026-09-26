"""SimplePointScan: a beginner's point-scan panel on the Advanced scan backend.

Two modes (docs/simple-point-scan-plan.md):

* **Overview** scans the largest field the scanners reach, live, with big
  pixels at the shortest safe dwell, aiming for about one frame per second
  (plan D6) and showing the measured rate.
* **Acquisition** scans a region (from the rectangle drawn in the viewer, or
  typed), with the pixel size and dwell chosen on two sliders and a live
  estimate of the scan time.

The rectangle lives in scanner coordinates: it is converted with the scan
geometry of the image the reference detector's layer shows (plan D1, §5.5),
and drawn again whenever that image or the region changes.

The controller owns the model (:mod:`imswitch.imcontrol.model.simple_scan`)
and fills the Advanced controller's two parameter dicts from it, so the run
lifecycle, recording layouts and refusal handling are Advanced's own. Where
Advanced behaves otherwise than this panel needs, this controller overrides
it and leaves Advanced unchanged (plan §6): its own continuation policy (D3),
its own power checks (D5). Scans carry their frame geometry (D1).
"""

from __future__ import annotations

import copy
import dataclasses
import time
import traceback

from qtpy import QtCore

from imswitch.imcontrol.model import ScanDesignRefusedError
from imswitch.imcontrol.model.scan_frame import (
    FRAME_GEOMETRY_KEY,
    frame_geometry_from_scan,
)
from imswitch.imcontrol.model.scan_parameters import pixels_for_length_step
from imswitch.imcontrol.model.simple_scan import (
    AxisRegion,
    PlanNotRepresentable,
    ScanLimits,
    SimpleScanPlan,
    dicts_to_plan,
    normalize_plan,
    plan_overview,
    plan_to_dicts,
    power_refusal,
    snap_dwell_s,
    quantize_step_um,
    snap_length_um,
)
from ..scan_region_mapping import extents_to_rectangle, rectangle_to_extents
from .ScanControllerAdvanced import ScanControllerAdvanced

OVERVIEW = 'overview'
ACQUISITION = 'acquisition'

# Execution modes: fixed by the entry point when a run starts (plan D3).
SINGLE = 'single'          # one iteration; an external driver owns any series
UNBOUNDED = 'unbounded'    # Live: until Stop or Live is unticked

_POINT_DETECTOR_KINDS = {
    'APDManager': 'APD',
    'PMTManager': 'PMT',
    'SwabianTimeTaggerManager': 'Time Tagger',
}


def _placement(shown):
    """Where a shown image's pixels are, without which run it came from."""
    if shown is None:
        return None
    return shown.geometry.axes, shown.display_transform, tuple(shown.raw_shape)


class ScanControllerSimplePointScan(ScanControllerAdvanced):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        state = self._simple()
        self._widget.configureSimpleScan(
            axes=[(a.name, a.label, a.smooth) for a in state['limits'].axes],
            gates=[(g.name, g.wavelength_nm, g.power_capable)
                   for g in state['limits'].gates],
            overviewAxes=state['limits'].overview_axes,
            detectorNote=self._detectorNote(),
            detectors=self._pointDetectorNames(),
        )
        if state['reference'] is not None:
            self._widget.setReferenceDetector(state['reference'])
        self._widget.sigModeRequested.connect(self.setSimpleScanMode)
        self._widget.sigAcquisitionEdited.connect(self._onAcquisitionEdited)
        self._widget.sigOverviewChannelChanged.connect(self._onOverviewChannelChanged)
        self._widget.sigStopClicked.connect(self._onStopClicked)
        self._widget.sigRegionDrawn.connect(self._onRegionDrawn)
        self._widget.sigReferenceDetectorChanged.connect(self.setReferenceDetector)
        self._commChannel.sigScanRequestRejected.connect(self._onScanRejected)
        self._commChannel.sigScanGeometryShown.connect(self._onScanGeometryShown)

        self._estimateTimer = QtCore.QTimer()
        self._estimateTimer.setSingleShot(True)
        self._estimateTimer.setInterval(100)
        self._estimateTimer.timeout.connect(self._refreshEstimate)

        self._pushToWidget()
        self._refreshEstimate()

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def _simple(self) -> dict:
        """The panel's model, created on first use.

        Advanced's constructor already reads the parameters (and so this
        controller's plan) before this class's own constructor body runs.
        """
        state = self.__dict__.get('_simpleState')
        if state is None:
            limits = ScanLimits.from_setup(self._setupInfo)
            state = self.__dict__['_simpleState'] = {
                'limits': limits,
                'mode': OVERVIEW,
                'overviewChannel': 0,
                'overheadS': 0.0,
                'overview': None,
                'acquisition': None,
                'framesDone': 0,
                'lastFrameEnd': None,
                'periods': [],
                # The layer the rectangle is drawn on, and the scan geometry
                # each detector's layer shows (plan D1 point 6).
                'reference': next(iter(self._pointDetectorNames()), None),
                'shown': {},
            }
            state['overview'] = self._planOverview()
            state['acquisition'] = self._defaultAcquisition()
        return state

    def _scanLimits(self) -> ScanLimits:
        # A method, not a property: generateAPI reads every attribute of a
        # controller, and this builds the panel's model on first use.
        return self._simple()['limits']

    def _pointDetectorNames(self) -> list:
        return [
            name for name, info in self._setupInfo.detectors.items()
            if getattr(info, 'managerName', '') in _POINT_DETECTOR_KINDS
        ]

    def _defaultAcquisition(self) -> SimpleScanPlan:
        state = self._simple()
        limits = state['limits']
        overview = state['overview']
        coarse, fine = self._pixelRange(channels=None)
        step = quantize_step_um((coarse * fine) ** 0.5)
        regions = {
            axis: AxisRegion(centre, snap_length_um(overview.field_um / 4.0, step), step)
            for axis, centre in zip(overview.axes, overview.centres_um)
        }
        dims = tuple(overview.axes)
        return normalize_plan(SimpleScanPlan(
            dims=dims,
            regions=regions,
            dwell_s=limits.min_dwell_s(dims[0], step),
            channels=((limits.gates[0].name,),) if limits.gates else (),
            phase_delay_us=float(self._designerParam('phase_delay', 0.0)),
            d3step_delay_us=float(self._designerParam('d3step_delay', 0.0)),
        ))

    def _designerParam(self, key, default):
        params = getattr(self._setupInfo.scan, 'scanDesignerParams', None) or {}
        return params.get(key, default)

    def _pixelRange(self, channels):
        """(overview pixel, finest useful pixel) for the pixel slider."""
        state = self._simple()
        limits = state['limits']
        coarse = state['overview'].step_um
        lasers = [laser for lane in (channels or ()) for laser in lane]
        wavelengths = [
            limits.gate(name).wavelength_nm for name in lasers
            if name in {g.name for g in limits.gates}
        ] or [g.wavelength_nm for g in limits.gates if g.wavelength_nm]
        fine, _ = limits.nyquist_um(wavelengths)
        if fine is None or fine >= coarse:
            fine = coarse / 4.0
        return coarse, fine

    def _currentPositions(self) -> dict:
        positions = {}
        for name in self._scanLimits().axis_names:
            try:
                position = self._master.positionersManager[name].position
                positions[name] = float(next(iter(position.values())))
            except Exception:
                positions[name] = self._scanLimits().axis(name).centre_um
        return positions

    def _withCurrentPark(self, plan: SimpleScanPlan) -> SimpleScanPlan:
        """Unscanned positioners stay where they are, unless the plan parks
        them somewhere on purpose (e.g. Y at the rectangle for an XZ scan)."""
        park = {
            name: value for name, value in self._currentPositions().items()
            if name not in plan.dims
        }
        park.update({k: v for k, v in plan.park.items() if k not in plan.dims})
        return normalize_plan(dataclasses.replace(plan, park=park))

    def _overviewScanPlan(self) -> SimpleScanPlan:
        state = self._simple()
        overview = state['overview']
        acquisition = state['acquisition']
        channels = acquisition.channels
        index = min(state['overviewChannel'], max(0, len(channels) - 1))
        lane = channels[index] if channels else ()
        regions = {
            axis: AxisRegion(centre, overview.pixels * overview.step_um, overview.step_um)
            for axis, centre in zip(overview.axes, overview.centres_um)
        }
        power = {
            name: (values[index],) if index < len(values) else (100.0,)
            for name, values in acquisition.channel_power.items()
        }
        return SimpleScanPlan(
            dims=tuple(overview.axes),
            regions=regions,
            dwell_s=overview.dwell_s,
            channels=(lane,) if lane else (),
            channel_power_on=acquisition.channel_power_on,
            channel_power=power,
            channel_power_enabled=dict(acquisition.channel_power_enabled),
            phase_delay_us=acquisition.phase_delay_us,
            d3step_delay_us=acquisition.d3step_delay_us,
        )

    def currentPlan(self) -> SimpleScanPlan:
        """The plan the next scan runs: the overview or the acquisition."""
        state = self._simple()
        plan = (self._overviewScanPlan() if state['mode'] == OVERVIEW
                else state['acquisition'])
        return self._withCurrentPark(plan)

    # ------------------------------------------------------------------
    # Advanced's parameter seam (plan F2)
    # ------------------------------------------------------------------

    def _buildAnalogParameterDict(self):
        analog, digital = plan_to_dicts(self.currentPlan(), self._scanLimits())
        self.__dict__['_pendingDigital'] = digital
        return analog, list(analog['scan_dim_target_device'])

    def _buildDigitalParameterDict(self, analogParameterDict):
        digital = self.__dict__.pop('_pendingDigital', None)
        if digital is None:
            digital = plan_to_dicts(self.currentPlan(), self._scanLimits())[1]
        return digital

    def setParameters(self):
        """Dicts from a saved state or file -> the acquisition plan (D4)."""
        self.settingParameters = True
        try:
            plan = dicts_to_plan(self._analogParameterDict,
                                 self._digitalParameterDict, self._scanLimits())
        except PlanNotRepresentable as refusal:
            self._logger.warning(f'Scan not loaded: {refusal}')
            self._widget.showMessage(str(refusal), error=True)
            self.__dict__['_stateApplied'] = False
            return
        finally:
            self.settingParameters = False
        self._simple()['acquisition'] = plan
        self.__dict__['_stateApplied'] = True
        self._pushToWidget()
        self._scheduleEstimate()

    def updatePixels(self):
        if self.__dict__.get('_simpleState') is not None and hasattr(self, '_estimateTimer'):
            self._scheduleEstimate()

    def plotSignalGraph(self):
        pass

    # ------------------------------------------------------------------
    # Building: channel power (D5) and frame geometry (D1)
    # ------------------------------------------------------------------

    def _buildScanSignals(self):
        """A run's signals, refused first if the panel's channel power cannot
        be built (plan D5). The check reads the plan, not the dicts: the dicts
        cannot even express power for a laser without an analog channel."""
        refusal = power_refusal(self.currentPlan(), self._scanLimits())
        if refusal:
            raise ScanDesignRefusedError(refusal)
        return super()._buildScanSignals()

    def _make_full_scan(self, scanParameters, TTLParameters):
        signalDict, scanInfoDict = super()._make_full_scan(scanParameters, TTLParameters)

        # Advanced swallows a failed power injection and scans without it;
        # this panel refuses instead (plan D5, review 3).
        if TTLParameters.get('advanced_mode'):
            enabled = TTLParameters.get('linestep_power_enabled') or {}
            fired = set(TTLParameters.get('target_device') or ())
            for gate in self._scanLimits().gates:
                if (gate.power_capable and gate.name in fired
                        and gate.name in (TTLParameters.get('linestep_power_percent') or {})
                        and enabled.get(gate.name, True)
                        and gate.name not in signalDict['scanSignalsDict']):
                    raise ScanDesignRefusedError(
                        f'Channel power for {gate.name} could not be built; '
                        'the scan was not started.'
                    )

        scanInfoDict[FRAME_GEOMETRY_KEY] = frame_geometry_from_scan(
            scanParameters, scanInfoDict
        ).to_dict()
        return signalDict, scanInfoDict

    # ------------------------------------------------------------------
    # Running: one iteration owner per run (D3)
    # ------------------------------------------------------------------

    def runScan(self) -> None:
        """The panel's Start. Live runs until Stop; otherwise one frame."""
        live = self._simple()['mode'] == OVERVIEW or bool(self._widget.repeatEnabled())
        if self._simple()['mode'] == OVERVIEW:
            self._widget.setRepeatEnabled(True)
        self.__dict__['_executionMode'] = UNBOUNDED if live else SINGLE
        self._resetFrameClock()
        super().runScan()

    def runScanExternal(self, recalculateSignals, isNonFinalPartOfSequence):
        """A recording, script or workflow start: always one iteration. The
        external driver owns any series (a recorded T series is a lapse)."""
        self.__dict__['_executionMode'] = SINGLE
        self._resetFrameClock()
        return super().runScanExternal(recalculateSignals, isNonFinalPartOfSequence)

    def _wantsAnotherIteration(self) -> bool:
        if self.__dict__.get('_scanStopRequested', False):
            return False
        if self.__dict__.get('_executionMode', SINGLE) == UNBOUNDED:
            return bool(self._widget.repeatEnabled())
        return False

    def _shouldContinueRepeat(self) -> bool:
        return self._wantsAnotherIteration()

    def scanDone(self):
        """Advanced's scanDone, deciding on this panel's execution mode
        instead of the widget's Repeat box (plan D3)."""
        self.isRunning = False
        self._recordFramePeriod()
        try:
            if not self._wantsAnotherIteration():
                isFinalPart = not getattr(self, 'doingNonFinalPartOfSequence', False)
                self._restoreScanPositioners()
                if isFinalPart:
                    try:
                        self._widget.setScanButtonChecked(False)
                    except Exception:
                        self._logger.error(
                            'Failed to reset the scan widget after completion',
                            exc_info=True,
                        )
                self._publishScanDone(isFinalPart=isFinalPart)
            else:
                self._armRepeatScan()
        except Exception:
            self._logger.error(traceback.format_exc())
            self.scanFailed()

    def _onStopClicked(self):
        """Stop: no further frame. A running frame completes (NI-DAQ cannot
        be interrupted mid-iteration; decided 2026-09-25)."""
        self._widget.setRepeatEnabled(False)
        running = (
            getattr(self, 'isRunning', False)
            or self.__dict__.get('_repeatPending', False)
            or self.__dict__.get('_scanRunToken') is not None
        )
        if running:
            remaining = self._remainingFrameS()
            self._widget.showMessage(
                'Stopping after this frame'
                + (f' (about {remaining:.0f} s)' if remaining and remaining >= 1 else '')
                + '.'
            )
            self.abortScan()

    def _onScanRejected(self, reason):
        self._widget.showMessage(f'Not started: {reason}', error=True)

    # ------------------------------------------------------------------
    # Measured frame rate; learning the re-arm overhead (D6)
    # ------------------------------------------------------------------

    def _resetFrameClock(self):
        state = self._simple()
        state['lastFrameEnd'] = None
        state['periods'] = []
        state['frameStart'] = time.monotonic()

    def _remainingFrameS(self):
        state = self._simple()
        estimate = state.get('frameEstimateS')
        started = state.get('frameStart')
        if not estimate or started is None:
            return None
        return max(0.0, estimate - (time.monotonic() - started))

    def _recordFramePeriod(self):
        state = self._simple()
        now = time.monotonic()
        last = state.get('lastFrameEnd')
        state['lastFrameEnd'] = now
        state['frameStart'] = now
        if last is None:
            return
        periods = (state['periods'] + [now - last])[-3:]
        state['periods'] = periods
        measured = sum(periods) / len(periods)
        self._widget.setMeasuredRate(1.0 / measured if measured > 0 else None)
        if state['mode'] != OVERVIEW or len(periods) < 3:
            return
        target = float(state['limits'].config['overviewFrameTimeS'])
        overview = state['overview']
        if measured > 1.1 * target and overview.met:
            overhead = max(0.0, measured - overview.estimate_s)
            if overhead > state['overheadS'] + 0.01:
                state['overheadS'] = overhead
                state['overview'] = self._planOverview()
                state['periods'] = []
                self._pushToWidget()

    # ------------------------------------------------------------------
    # Overview planning (D6)
    # ------------------------------------------------------------------

    def _planOverview(self):
        state = self._simple()
        limits = state['limits']
        axes = limits.overview_axes
        acquisition = state.get('acquisition')
        lane = ()
        if acquisition is not None and acquisition.channels:
            index = min(state['overviewChannel'], len(acquisition.channels) - 1)
            lane = acquisition.channels[index]

        def planFor(field, pixels, step, dwell):
            regions = {axis: AxisRegion(centre, pixels * step, step)
                       for axis, centre in zip(axes, (limits.axis(a).centre_um for a in axes))}
            plan = SimpleScanPlan(dims=tuple(axes), regions=regions, dwell_s=dwell,
                                  channels=(lane,) if lane else ())
            return plan_to_dicts(self._withCurrentPark(plan), limits)

        def estimate(field, pixels, step, dwell):
            analog, digital = planFor(field, pixels, step, dwell)
            return self._estimateFrameS(analog, digital)

        def fits(field, pixels, step, dwell):
            analog, digital = planFor(field, pixels, step, dwell)
            return not self._voltageRefusal(analog, digital)

        budget = float(limits.config['overviewFrameTimeS']) - state['overheadS']
        return plan_overview(limits, estimate=estimate, fits=fits,
                             budget_s=max(0.05, budget))

    def _estimateFrameS(self, analog, digital):
        designer = self._get_scan_designer()
        estimated = designer.estimateScanTime(
            self._stage_parameters(analog, digital), self._setupInfo
        )
        return float('inf') if estimated is None else float(estimated)

    def _voltageRefusal(self, analog, digital) -> str:
        """Whether the waveform stays inside the scanners' voltage ranges,
        turnaround included, without building it at full size: the fast axis
        from a two-line proxy (its extremes equal the full build's), stepped
        axes from their pixel positions."""
        designer = self._get_scan_designer()
        proxy = copy.deepcopy(analog)
        dims = [d for d in analog['scan_dim_target_device'] if d != 'None']
        for dim in dims[1:]:
            index = proxy['target_device'].index(dim)
            proxy['axis_length'][index] = 2 * proxy['axis_step_size'][index]
        try:
            _, _, info = designer.make_signal(
                self._stage_parameters(proxy, digital), self._setupInfo
            )
        except ScanDesignRefusedError as error:
            return str(error)
        except Exception as error:
            return str(error)
        for name, (low, high) in zip(info.get('axis_names', []), info.get('minmaxes', [])):
            props = self._setupInfo.positioners[name].managerProperties or {}
            conversion = float(props.get('conversionFactor', 1) or 1)
            index = analog['target_device'].index(name)
            if name != dims[0]:
                # the proxy shortened this axis: use its real pixel positions
                count = pixels_for_length_step(analog['axis_length'][index],
                                               analog['axis_step_size'][index])
                half = (count - 1) / 2.0 * float(analog['axis_step_size'][index])
                centre = float(analog['axis_centerpos'][index])
                low, high = (centre - half) / conversion, (centre + half) / conversion
            minVolt, maxVolt = props.get('minVolt'), props.get('maxVolt')
            if (minVolt is not None and low < minVolt) or (maxVolt is not None and high > maxVolt):
                return (f'{name} would need {low:.3g} to {high:.3g} V, outside its '
                        f'{minVolt:g} to {maxVolt:g} V: choose a smaller region, '
                        'smaller pixels or a longer dwell.')
        return ''

    # ------------------------------------------------------------------
    # Panel edits
    # ------------------------------------------------------------------

    def _isBusy(self) -> bool:
        return bool(getattr(self, 'isRunning', False)
                    or self.__dict__.get('_scanRunToken') is not None)

    def setSimpleScanMode(self, mode: str):
        if mode not in (OVERVIEW, ACQUISITION):
            raise ValueError(f'Unknown mode {mode!r}')
        if self._isBusy():
            self._widget.showMessage('Stop the scan before switching modes.', error=True)
            self._pushToWidget()
            return
        state = self._simple()
        state['mode'] = mode
        self._widget.setRepeatEnabled(mode == OVERVIEW)
        self._pushToWidget()
        self._scheduleEstimate()

    def _onOverviewChannelChanged(self, index: int):
        state = self._simple()
        state['overviewChannel'] = max(0, int(index))
        state['overview'] = self._planOverview()
        self._pushToWidget()
        self._scheduleEstimate()

    def _onAcquisitionEdited(self, plan: SimpleScanPlan):
        state = self._simple()
        limits = state['limits']
        plan = normalize_plan(plan)
        if plan.dims:
            fast = plan.dims[0]
            step = plan.regions[fast].step_um
            shortest = limits.min_dwell_s(fast, step)
            dwell = snap_dwell_s(min(max(plan.dwell_s, shortest), limits.max_dwell_s),
                                 limits.sample_rate, minimum=shortest)
            plan = dataclasses.replace(plan, dwell_s=dwell)
        channelsChanged = plan.channels != state['acquisition'].channels
        state['acquisition'] = plan
        if channelsChanged:
            state['overview'] = self._planOverview()
        self._pushToWidget()
        self._scheduleEstimate()
        try:
            self.updateScanStageAttrs()
            self.updateScanTTLAttrs()
        except Exception:
            self._logger.debug('Updating shared scan attributes failed', exc_info=True)

    # ------------------------------------------------------------------
    # The rectangle in the viewer (plan §5.5)
    # ------------------------------------------------------------------

    def setReferenceDetector(self, name: str):
        """Draw and show the rectangle on this detector's live image."""
        if name not in self._pointDetectorNames():
            raise ValueError(f'{name!r} is not a point detector of this setup')
        self._simple()['reference'] = name
        self._widget.setReferenceDetector(name)
        self._pushRegion()

    def _onScanGeometryShown(self, detectorName, shown):
        state = self._simple()
        previous = state['shown'].get(detectorName)
        state['shown'][detectorName] = shown
        # Every Live frame is a new iteration; the rectangle moves only when
        # the pixels land somewhere else.
        if detectorName == state['reference'] and _placement(shown) != _placement(previous):
            self._pushRegion()

    def _shownReference(self):
        state = self._simple()
        return state['shown'].get(state['reference'])

    def _regionExtents(self, shown) -> dict:
        """The acquisition's (low, high) per axis the shown image spans; a
        parked axis is a zero-width extent. None if one of them is neither."""
        plan = self._withCurrentPark(self._simple()['acquisition'])
        extents = {}
        for axis in shown.geometry.axes:
            if axis.device in plan.dims:
                region = plan.regions[axis.device]
                half = region.length_um / 2.0
                extents[axis.device] = (region.center_um - half, region.center_um + half)
            elif axis.device in plan.park:
                extents[axis.device] = (plan.park[axis.device],) * 2
            else:
                return None
        return extents

    def _pushRegion(self):
        state = self._simple()
        reference = state['reference']
        shown = self._shownReference()
        if not self._widget.hasViewer():
            self._widget.setDrawing(False, 'This setup has no image viewer.')
        elif reference is None:
            self._widget.setDrawing(False, 'No point detector is configured.')
        elif shown is None:
            self._widget.setDrawing(
                False, f'{reference} shows no scanned image yet: start the overview first.')
        else:
            self._widget.setDrawing(True)
        extents = self._regionExtents(shown) if shown is not None else None
        self._widget.showRegion(
            extents_to_rectangle(extents, shown) if extents is not None else None)

    def _onRegionDrawn(self, vertices):
        """A rectangle drawn, moved or resized on the reference layer.

        Every shown axis the acquisition scans takes the rectangle's extent,
        snapped to whole steps; a shown axis it does not scan is parked at
        the rectangle's centre (an XZ scan drawn on an XY image, plan §5.4).
        """
        state = self._simple()
        limits = state['limits']
        shown = self._shownReference()
        if shown is None:
            self._widget.showMessage(
                'The live image has no scan geometry to draw on: start the overview '
                'first.', error=True)
            self._pushRegion()
            return
        plan = state['acquisition']
        regions = dict(plan.regions)
        park = dict(plan.park)
        for device, (low, high) in rectangle_to_extents(vertices, shown).items():
            if device not in limits.axis_names:
                continue
            reach = limits.axis(device).range_um
            if reach is not None:
                low, high = (min(max(v, reach[0]), reach[1]) for v in (low, high))
            centre = (low + high) / 2.0
            if device in plan.dims:
                step = regions[device].step_um
                regions[device] = AxisRegion(centre, snap_length_um(high - low, step), step)
            else:
                park[device] = centre
        self._onAcquisitionEdited(dataclasses.replace(plan, regions=regions, park=park))
        if self._isBusy():
            self._widget.showMessage(
                'Region set. Stop the overview, then Start to acquire it.')
        else:
            self.setSimpleScanMode(ACQUISITION)
            self._widget.showMessage('Region set.')

    # ------------------------------------------------------------------
    # Readouts
    # ------------------------------------------------------------------

    def _scheduleEstimate(self):
        timer = self.__dict__.get('_estimateTimer')
        if timer is not None:
            timer.start()

    def _refreshEstimate(self):
        state = self._simple()
        plan = self.currentPlan()
        limits = state['limits']
        refusal = power_refusal(plan, limits)
        estimate = None
        if not refusal and not plan.channels:
            refusal = 'Choose at least one laser.'
        analog, digital = plan_to_dicts(plan, limits)
        if not refusal:
            refusal = self._get_scan_designer().signalLengthRefusal(analog, self._setupInfo)
        if not refusal:
            try:
                estimate = self._estimateFrameS(analog, digital)
            except ScanDesignRefusedError as error:
                refusal = str(error)
        if not refusal:
            refusal = self._voltageRefusal(analog, digital)
        state['frameEstimateS'] = estimate
        steps = len(plan.channels)
        note = (f'{steps} line passes per line, one per channel.'
                if steps > 1 else '')
        self._widget.setEstimate(estimate, None, note, refusal)

    def _pushToWidget(self):
        state = self._simple()
        limits = state['limits']
        acquisition = state['acquisition']
        coarse, fine = self._pixelRange(acquisition.channels)
        fast = acquisition.dims[0] if acquisition.dims else limits.axis_names[0]
        step = acquisition.regions[fast].step_um if acquisition.dims else coarse
        self._widget.setSimpleScanState(
            mode=state['mode'],
            overview=state['overview'],
            overviewChannel=state['overviewChannel'],
            acquisition=acquisition,
            pixelRange=(coarse, fine),
            dwellRange=(limits.min_dwell_s(fast, step), limits.max_dwell_s),
        )
        self._pushRegion()

    def _detectorNote(self) -> str:
        kinds = {}
        for name, info in self._setupInfo.detectors.items():
            kind = _POINT_DETECTOR_KINDS.get(getattr(info, 'managerName', ''))
            if kind:
                kinds[name] = kind
        if not kinds:
            return 'No point detector is configured.'
        note = 'Every point detector records every channel'
        if 'PMT' in kinds.values():
            note += '; a PMT adds the channels into one image'
        return note + '.'

    # ------------------------------------------------------------------
    # Saved state (D4)
    # ------------------------------------------------------------------

    def getComponentState(self) -> dict:
        state = super().getComponentState()
        simple = self._simple()
        acquisition = self._withCurrentPark(simple['acquisition'])
        analog, digital = plan_to_dicts(acquisition, simple['limits'])
        # A file's executable dicts are the acquisition (plan D4).
        state['analogParameterDict'] = analog
        state['digitalParameterDict'] = digital
        state['positionersScan'] = list(analog['scan_dim_target_device'])
        state['simplePlan'] = {
            'mode': simple['mode'],
            'overviewChannel': simple['overviewChannel'],
            'reference': simple['reference'],
            'acquisition': simple['acquisition'].to_dict(),
        }
        return state

    def applyComponentState(self, state, *, applyMode):
        self.__dict__['_stateApplied'] = False
        # Refuse what the panel cannot represent before anything is applied
        # (plan D4): nothing changes, and the reason is the warning.
        if isinstance(state, dict) and not getattr(self, 'isRunning', False):
            analog = state.get('analogParameterDict')
            digital = state.get('digitalParameterDict')
            if isinstance(analog, dict) and isinstance(digital, dict) and analog:
                try:
                    dicts_to_plan(analog, digital, self._scanLimits())
                except PlanNotRepresentable as refusal:
                    return [f'{refusal} Scan state was not applied.']
                except (KeyError, IndexError, TypeError, ValueError):
                    pass  # malformed dicts: the base class says what is wrong
        warnings = super().applyComponentState(state, applyMode=applyMode)
        simple = state.get('simplePlan') if isinstance(state, dict) else None
        if simple and self.__dict__.get('_stateApplied'):
            try:
                stored = self._simple()
                stored['acquisition'] = SimpleScanPlan.from_dict(simple['acquisition'])
                stored['overviewChannel'] = int(simple.get('overviewChannel', 0))
                if simple.get('mode') in (OVERVIEW, ACQUISITION):
                    stored['mode'] = simple['mode']
                if simple.get('reference') in self._pointDetectorNames():
                    stored['reference'] = simple['reference']
                    self._widget.setReferenceDetector(stored['reference'])
                stored['overview'] = self._planOverview()
                self._pushToWidget()
                self._scheduleEstimate()
            except Exception as error:
                warnings.append(f'Point-scan panel settings were not restored: {error}')
        return warnings


# Copyright (C) 2020-2026 ImSwitch developers
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
