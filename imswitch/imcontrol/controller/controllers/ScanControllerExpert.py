"""The Expert scan panel's controller (docs/design/plans/
scan-engines-and-expert-scan.md §4, M3).

The panel edits a scan program; this controller turns it into what the
setup's scan engine runs and reports what the engine makes of it:

- on a streaming engine (the NI-DAQ), the program is compiled into sampled
  waveforms and runs through the same lifecycle as every other scan panel;
- on a step engine (a pulse generator, a step board), the engine compiles
  and runs the program itself, and its end, stop and failure end the run.

The report beside the tables is the engine's feasibility report and the
realized schedule: the same compile the run uses, so what the panel shows is
what the run refuses (one authority, plan §3.3).

Saved states hold the program (``scan-program/1``) and declare the
``scan-program`` feature, so the other scan panels refuse them with the
reason. A legacy Advanced scan file opens here as a program; while it is
unchanged it is saved back in the Advanced format too, so it still opens on
the Advanced panel.
"""

from __future__ import annotations

import copy
import math
import traceback
import numpy as np
from qtpy import QtCore

from imswitch.imcommon.model.acquisition_layout import LOOP_KIND_ADVANCES_POSITION
from imswitch.imcontrol.model import getWidgetStatePersistence
from imswitch.imcontrol.model.errors import ScanDesignRefusedError
from imswitch.imcontrol.model.scan_program import (
    ProgramError, RunContext, expand, presets, realize,
)
from imswitch.imcontrol.model.scan_program.advanced import (
    AdvancedConversionError, is_legacy_advanced_state, program_from_advanced,
)
from imswitch.imcontrol.model.scan_program.densify import densify
from imswitch.imcontrol.model.scan_program.engine import ScanEngineRefused, setup_channels
from imswitch.imcontrol.model.scan_program.policy import PUBLISHED
from imswitch.imcontrol.model.scan_program.preparation import resolve_preparation
from imswitch.imcontrol.model.scan_program.scan_info import (
    RASTER_KEY, RASTER_REASON_KEY, TRAJECTORY_KEY, scan_info_for,
)
from imswitch.imcontrol.model.scan_program.trajectory import RealizedTrajectory, US_PER_S
from imswitch.imcontrol.model.scan_program.schedule import format_seconds
from imswitch.imcontrol.model.scan_program.serialize import (
    FORMAT, program_from_dict, program_to_dict,
)
from ..basecontrollers import SuperScanController
from ._acquisition_layout_source import (
    build_program_layouts,
    build_trajectory_layouts,
    scan_driven_detector_names,
    validate_detector_edge_counts,
)

#: The saved-state feature an Expert program needs (plan §4): the other scan
#: panels do not declare it, so they refuse an Expert state with the reason.
PROGRAM_FEATURE = 'scan-program'
PROGRAM_FEATURE_REASON = 'an Expert scan program (scan-program/1)'

#: How long edits settle before the report is compiled again.
REPORT_SETTLE_MS = 300


class ScanControllerExpert(SuperScanController):
    supportedStateFeatures = frozenset({PROGRAM_FEATURE})

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._program = None
        self._programError = ''
        self._advancedOrigin = None     # (program, analog, digital) of an opened Advanced file
        self._compiled = None
        self._compiledFor = None
        # The program is authoritative; the tables are read back only after
        # an edit, so an opened file's request is never re-rounded by them.
        self._edited = False

        detectors = list(self._setupInfo.detectors)
        lasers = [name for name in self.TTLDevices if name not in detectors]
        engine = self._engineForScans()
        self._widget.initControls(
            list(self.positioners), lasers, 'ms', detectorNames=detectors,
            presets=[(p.id, p.title) for p in presets.PRESETS.values()],
            policies=sorted(PUBLISHED),
            engine=f'Runs on {engine.name} ({type(engine).__name__}).')

        self._reportTimer = QtCore.QTimer(self)
        self._reportTimer.setSingleShot(True)
        self._reportTimer.setInterval(REPORT_SETTLE_MS)
        self._reportTimer.timeout.connect(self.updateReport)
        self._widget.sigProgramEdited.connect(self._onEdited)
        self._widget.sigPresetRequested.connect(self.makeFromPreset)
        self._widget.sigPlotClicked.connect(self.plotProgram)

        # A step engine ends its run by these too; the NI-DAQ has neither.
        for name, slot in (('sigScanStopped', self._SuperScanController__onNidaqScanDone),
                           ('sigScanFailed', self._onEngineFailed)):
            signal = getattr(self._scanEngine, name, None)
            if signal is not None:
                signal.connect(slot)

        self.updateReport()
        getWidgetStatePersistence().register('Scan', self)

    # ------------------------------------------------------------------
    # The program
    # ------------------------------------------------------------------

    def program(self):
        return self._program

    def _onEdited(self):
        if not getattr(self, 'settingParameters', False):
            self._edited = True
            self._reportTimer.start()

    def setProgram(self, program, *, advancedOrigin=None):
        """Show ``program`` in the panel (``None`` for an empty one)."""
        self._program = program
        self._advancedOrigin = advancedOrigin
        self._programError = '' if program is not None else self._EMPTY
        self.setParameters()
        self.updateReport()

    _EMPTY = 'The program has no loops yet: choose a preset, or add a loop.'

    def getParameters(self):
        if getattr(self, 'settingParameters', False):
            return
        if self._edited:
            carried = self._carriedFields()
            try:
                data = self._widget.programDict()
                if not data.get('loops') and not carried.get('trajectory'):
                    self._program, self._programError = None, self._EMPTY
                else:
                    self._program = program_from_dict({'format': FORMAT, **data, **carried})
                    self._programError = ''
            except (ProgramError, ValueError) as error:
                self._program = None
                self._programError = str(error)
            self._edited = False
        elif self._program is None and not self._programError:
            self._programError = self._EMPTY
        program = self._program
        devices = self._movedDevices(program)
        # What the base reads: the actuators and the positions to capture.
        self._analogParameterDict = {'target_device': devices}
        self._digitalParameterDict = {
            'target_device': sorted({e.device for e in program.timeline}) if program else []}
        self._positionersScan = list(devices)

    def _carriedFields(self):
        """What the tables do not show but the program keeps. ``requested_as``
        is not carried: after an edit the tables are the request."""
        program = self._program or (self._advancedOrigin[0] if self._advancedOrigin else None)
        if program is None:
            return {}
        # A trajectory is not in the tables either: Make… edits its path.
        return {'park': dict(program.park), 'requested_as': None,
                'trajectory': program_to_dict(program)['trajectory'],
                'separations': [{'after': list(s.after), 'before': list(s.before),
                                 'tolerance_s': s.tolerance_s} for s in program.separations]}

    def setParameters(self):
        self.settingParameters = True
        try:
            if self._program is None:
                self._widget.setProgramDict(None)
            else:
                self._widget.setProgramDict(program_to_dict(self._program))
        finally:
            self.settingParameters = False

    def updatePixels(self):
        self._reportTimer.start()

    def emitScanSignal(self, signal, *args):
        signal.emit(*args)

    def makeFromPreset(self, preset_id):
        preset = presets.PRESETS.get(preset_id)
        if preset is None:
            return
        made_from, params = presets.preset_of(self._program) if self._program else ('', {})
        initial = params if made_from == preset_id else {}

        def validate(candidate):
            try:
                presets.make(preset_id, candidate)
            except (presets.PresetError, ProgramError, ValueError, TypeError) as error:
                return str(error)
            return ''

        params = self._widget.askPresetParams(preset, initial, validate)
        if params is not None:
            self.setProgram(presets.make(preset_id, params))

    # ------------------------------------------------------------------
    # Compiling: one compile for the report, the run and the recording
    # ------------------------------------------------------------------

    @staticmethod
    def _movedDevices(program):
        if program is None:
            return []
        if program.trajectory is not None:
            return list(program.trajectory.devices)
        return [d.device for d in program.drives]

    def _capturedPositions(self, program):
        captured = {}
        for device in self._movedDevices(program):
            manager = self._master.positionersManager[device]
            info = self._setupInfo.positioners[device]
            axes = list(getattr(info, 'axes', None) or manager.axes)
            captured[device] = float(manager.position[axes[0]])
        return captured

    def _deviceFacts(self):
        has_engines = bool(getattr(self._setupInfo, 'scanEngines', None))
        facts = {}
        for kind, section in (('laser', 'lasers'), ('detector', 'detectors'),
                              ('positioner', 'positioners')):
            for name, info in (getattr(self._setupInfo, section, None) or {}).items():
                entry = {'preparation': getattr(info, 'preparation', None)}
                facts[name] = resolve_preparation(name, kind, entry,
                                                  setup_has_scan_engines=has_engines)
        return facts

    def _compile(self):
        """The engine's compile of the current program, or
        ScanDesignRefusedError with every reason."""
        self.getParameters()
        if self._program is None:
            raise ScanDesignRefusedError(self._programError)
        engine = self._engineForScans()
        captured = self._capturedPositions(self._program)
        key = (program_to_dict(self._program), tuple(sorted(captured.items())))
        if self._compiledFor == key and self._compiled is not None:
            return self._compiled
        try:
            compiled = engine.compile(self._program, setup_channels(self._setupInfo),
                                      captured_um=captured, devices=self._deviceFacts())
        except ScanEngineRefused as error:
            raise ScanDesignRefusedError(
                f'{engine.name} cannot run this program:\n- '
                + '\n- '.join(error.report.reasons())) from error
        except (ProgramError, AdvancedConversionError, ValueError) as error:
            raise ScanDesignRefusedError(str(error)) from error
        self._compiled, self._compiledFor = compiled, key
        return compiled

    def _scheduleOf(self, compiled):
        schedule = getattr(compiled, 'schedule', None)
        if schedule is None:
            # A step engine that keeps only its own plan: the interpreter's
            # schedule on the engine's grid says the same (conformance).
            engine = self._engineForScans()
            schedule = realize(
                expand(compiled.program, RunContext(
                    captured_um=self._capturedPositions(compiled.program),
                    devices=self._deviceFacts())),
                1.0 / engine.capabilities().timing_grid_s, rate_source=engine.name)
            compiled.schedule = schedule
        return schedule

    def _scanInfoOf(self, compiled):
        info = getattr(compiled, 'scanInfoDict', None)
        if info is None:
            schedule = self._scheduleOf(compiled)
            info = scan_info_for(compiled.program, schedule, schedule.rate_hz)
            compiled.scanInfoDict = info
        return info

    @staticmethod
    def _duration_s(schedule):
        if isinstance(schedule, RealizedTrajectory):
            return schedule.total_ticks / schedule.rate_hz
        return sum(segment.ticks for segment in schedule.segments) / schedule.rate_hz

    def _durationRefusal(self, compiled):
        cap = getattr(self._setupInfo.scan, 'maxScanTimeMin', None)
        if not cap:
            return ''
        seconds = self._duration_s(self._scheduleOf(compiled))
        if seconds > float(cap) * 60:
            return (f'Scan would take {format_seconds(seconds)}, above the '
                    f'{float(cap):g} min cap (scan.maxScanTimeMin).')
        return ''

    def _buildScanSignals(self):
        """What the run arms: refused, with the reason, before the run is
        reserved."""
        compiled = self._compile()
        refusal = self._durationRefusal(compiled)
        if refusal:
            raise ScanDesignRefusedError(refusal)
        info = copy.deepcopy(self._scanInfoOf(compiled))
        refusal = self._pointDetectorRefusal(info)
        if refusal:
            raise ScanDesignRefusedError(refusal)
        signals = getattr(compiled, 'signalDict', None)
        if signals is None:   # a step engine runs the program itself
            signals = {'scanSignalsDict': {}, 'TTLCycleSignalsDict': {}, 'compiled': compiled}
        return signals, info

    def updateReport(self):
        """Compile what the panel shows and say what the engine makes of it."""
        engine = self._engineForScans()
        try:
            compiled = self._compile()
        except ScanDesignRefusedError as error:
            self._widget.setReport(f'Not runnable on {engine.name}.\n\n{error}')
            return
        lines = [f'Runnable on {engine.name}.', '']
        for verdict in compiled.report.verdicts:
            text = f'{verdict.verdict}: {verdict.feature}'
            if verdict.reason or verdict.cost:
                text += f' -- {verdict.reason or verdict.cost}'
            lines.append(text)
        schedule = self._scheduleOf(compiled)
        lines += ['', f'Timing policy {schedule.policy.id}, '
                      f'{format_seconds(schedule.grid_s)} grid.']
        if isinstance(schedule, RealizedTrajectory):
            lines += self._trajectoryReport(schedule)
            refusal = self._durationRefusal(compiled)
            self._widget.setReport('\n'.join(lines + ([refusal] if refusal else [])))
            return
        lines.append(f'Duration {format_seconds(self._duration_s(schedule))}, '
                     f'{len(schedule.step_spans())} steps.')
        for change in schedule.changes:
            lines.append(f'Runs differently: {change.quantity} at {change.where}, '
                         f'{format_seconds(change.requested_s)} as '
                         f'{format_seconds(change.realized_s)}.')
        info = self._scanInfoOf(compiled)
        if not info.get(RASTER_KEY):
            lines.append(f'Point detectors cannot assemble it: {info.get(RASTER_REASON_KEY)}')
        refusal = self._durationRefusal(compiled)
        if refusal:
            lines.append(refusal)
        self._widget.setReport('\n'.join(lines))

    def _trajectoryReport(self, realized):
        """The path, its windows and grid, and the limits readout: each
        device's peak speed and acceleration against its own limits."""
        trajectory = realized.program.trajectory
        seconds = realized.window_seconds()
        grid = realized.grid
        last = (f' (the last {format_seconds(seconds[-1])})'
                if seconds[-1] != seconds.max() else '')
        lines = [f'Trajectory {trajectory.pattern}: {format_seconds(trajectory.duration_s)} on '
                 f'the path, {format_seconds(trajectory.approach_s)} onto it and back; '
                 f'{format_seconds(self._duration_s(realized))} in all.',
                 f'{len(seconds)} measurement windows of {format_seconds(seconds.max())}{last}.',
                 f'Gridded onto {" x ".join(str(n) for n in grid.shape)} bins of '
                 f'{grid.pixel_um:g} µm: APDs as counts/s, PMTs as V, unvisited bins '
                 'empty (NaN); the coverage (s per bin) is recorded beside the image.']
        rate = realized.rate_hz
        for device, positions in realized.positions_um.items():
            props = self._setupInfo.positioners[device].managerProperties or {}
            speed = np.max(np.abs(np.diff(positions))) * rate / US_PER_S
            accel = np.max(np.abs(np.diff(positions, 2))) * (rate / US_PER_S) ** 2
            lines.append(f'{device}: peak {speed:.3g} µm/µs (vel_max {props.get("vel_max")}), '
                         f'{accel:.3g} µm/µs² (acc_max {props.get("acc_max")}); '
                         f'{positions.min():.4g} to {positions.max():.4g} µm.')
        return lines

    def plotProgram(self):
        try:
            compiled = self._compile()
        except ScanDesignRefusedError as error:
            self._widget.setReport(str(error))
            return
        schedule = self._scheduleOf(compiled)
        if isinstance(schedule, RealizedTrajectory):
            dense = {'analog': schedule.positions_um, 'digital': schedule.digital}
        else:
            dense = densify(schedule)
        rate = schedule.rate_hz
        traces = dict(dense['analog'])
        for name, line in sorted(dense['digital'].items()):
            traces[name] = line.astype(float)
        n = max((len(v) for v in traces.values()), default=0)
        self._widget.showTraces(np.arange(n) / rate, traces)

    # ------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------

    def runScanAdvanced(self, *, recalculateSignals=True, isNonFinalPartOfSequence=False,
                        sigScanStartingEmitted=False):
        try:
            if self._beginScanRunWithDesign(
                sigScanStartingEmitted=sigScanStartingEmitted,
                recalculateSignals=recalculateSignals,
            ) is None:
                return
            self._widget.setScanButtonChecked(True)
            self.doingNonFinalPartOfSequence = isNonFinalPartOfSequence
            self._armScanIteration(self.signalDict, self.scanInfoDict)
        except ScanDesignRefusedError as error:
            self._logger.error(str(error))
            self.scanFailed(message=str(error))
        except Exception:
            self._logger.error(traceback.format_exc())
            self.scanFailed()

    def _pointDetectorRefusal(self, scanInfoDict):
        # A trajectory is what point detectors measure window by window.
        if scanInfoDict.get(RASTER_KEY, True) or scanInfoDict.get(TRAJECTORY_KEY):
            return ''
        participants = self._scanCoordinator.composeParticipants()
        if not participants:
            return ''
        return (f'{", ".join(participants)} cannot assemble this scan: '
                f'{scanInfoDict.get(RASTER_REASON_KEY)} Deselect them for this scan.')

    def _armScanIteration(self, signalDict, scanInfoDict):
        compiled = signalDict.get('compiled')
        if compiled is None:
            return super()._armScanIteration(signalDict, scanInfoDict)
        # A step engine: it runs the compiled program itself.
        engine = self._engineForScans()
        self._scanPositionersRestored = False
        self._publishScanActuators()
        self.emitScanSignal(self._commChannel.sigScanDevicesResolved,
                            sorted({e.device for e in compiled.program.timeline}))
        recordingManager = getattr(self._master, 'recordingManager', None)
        markScanStarted = getattr(recordingManager, 'markScanStarted', None)
        if callable(markScanStarted):
            try:
                markScanStarted(scanInfoDict)
            except Exception:
                self._logger.error('Could not initialize the scan-recording watchdog',
                                   exc_info=True)

        def start():
            engine.prepare(compiled)
            engine.run(compiled, blocking=False)

        return self._scanCoordinator.armWithStarter(start, owner=self,
                                                    scanInfoDict=scanInfoDict)

    def _onEngineFailed(self, reason):
        # Why; sigScanBuildFailed, emitted right after, ends the run.
        if self._scanCoordinator.tokenForOwner(self) is not None:
            message = f'{self._engineForScans().name}: {reason}'
            self._externalScanRequestFailureMessage = message
            self._logger.error(f'The scan failed: {message}')

    def abortScan(self):
        running = self.isRunning
        super().abortScan()
        engine = self._engineForScans()
        if running and getattr(engine, 'run_signals', None) is None:
            try:
                engine.abort()
            except Exception:
                self._logger.error('The scan engine did not stop', exc_info=True)

    def scanDone(self):
        self.isRunning = False
        try:
            if not self._widget.repeatEnabled():
                isFinalPart = not getattr(self, 'doingNonFinalPartOfSequence', False)
                self._restoreScanPositioners()
                if isFinalPart:
                    self._widget.setScanButtonChecked(False)
                self._publishScanDone(isFinalPart=isFinalPart)
            else:
                self._armRepeatScan()
        except Exception:
            self._logger.error(traceback.format_exc())
            self.scanFailed()

    # ------------------------------------------------------------------
    # What recording asks
    # ------------------------------------------------------------------

    def _positionalLoops(self):
        program = self._program
        if program is None:
            return []
        return [loop for loop in program.loops
                if LOOP_KIND_ADVANCES_POSITION.get(loop.kind, True)]

    def _trajectoryGrid(self):
        """The grid a trajectory is binned onto, or None (not a trajectory, or
        not compilable)."""
        self.getParameters()
        if self._program is None or self._program.trajectory is None:
            return None
        try:
            return self._scheduleOf(self._compile()).grid
        except ScanDesignRefusedError:
            return None

    def getNumScanPositions(self):
        grid = self._trajectoryGrid()
        if grid is not None:
            return int(math.prod(grid.shape))     # one assembled image of the grid
        self.getParameters()
        return int(math.prod(loop.count for loop in self._positionalLoops())) \
            if self._program else 0

    def getDimsScan(self):
        grid = self._trajectoryGrid()
        if grid is not None:
            return tuple(grid.shape)
        self.getParameters()
        if self._program is None:
            return ()
        return tuple(loop.count for loop in reversed(self._program.loops)
                     if loop.kind != 'condition')

    def getScanStepSizes(self):
        grid = self._trajectoryGrid()
        if grid is not None:
            return tuple(grid.pixel_um for _ in grid.shape)
        self.getParameters()
        if self._program is None:
            return ()
        sizes = []
        for loop in reversed(self._program.loops):
            if loop.kind == 'condition':
                continue
            weights = [abs(float(d.weights_um[loop.id])) for d in self._program.drives
                       if loop.id in d.weights_um]
            sizes.append(weights[0] if len(weights) == 1 else None)
        return tuple(sizes)

    def getNumLineSteps(self):
        self.getParameters()
        return self._program.phase_count if self._program else 1

    def getNumCamTTL(self):
        """Frames per scan position, for each detector the program triggers."""
        try:
            compiled = self._compile()
        except ScanDesignRefusedError:
            return {}
        positions = self.getNumScanPositions()
        schedule = self._scheduleOf(compiled)
        if isinstance(schedule, RealizedTrajectory):
            return {}          # point detectors only: no camera frames
        out = {}
        for detector in self._setupInfo.detectors:
            events = [e for e in schedule.events if e.device == detector]
            if not events or not positions:
                continue
            edges = 0
            for step in {e.step_index for e in events}:
                end = None
                for e in sorted((e for e in events if e.step_index == step),
                                key=lambda e: e.start_tick):
                    if end is None or e.start_tick > end:
                        edges += 1
                    end = e.end_tick if end is None else max(end, e.end_tick)
            if edges % positions == 0:
                out[detector] = edges // positions
        return out

    def getFramesPerScanPixel(self):
        counts = self.getNumCamTTL()
        return max(counts.values()) if counts else 1

    def getAcquisitionLayouts(self, detectorNames):
        compiled = self._compile()
        info = self._scanInfoOf(compiled)
        schedule = self._scheduleOf(compiled)
        if isinstance(schedule, RealizedTrajectory):
            return build_trajectory_layouts(
                schedule.grid, info['img_axes_phys'], detectorNames,
                scan_source=type(self).__name__,
                scan_driven_detectors=scan_driven_detector_names(self, detectorNames))
        layouts = build_program_layouts(
            compiled.program, self._scheduleOf(compiled), detectorNames,
            scan_source=type(self).__name__,
            scan_driven_detectors=scan_driven_detector_names(self, detectorNames),
            raster_refusal=info.get(RASTER_REASON_KEY, ''),
        )
        signals = getattr(compiled, 'signalDict', None)
        if signals is not None:
            validate_detector_edge_counts(layouts, signals.get('TTLCycleSignalsDict', {}))
        return layouts

    # ------------------------------------------------------------------
    # Recording attributes and saved state
    # ------------------------------------------------------------------

    def updateScanStageAttrs(self):
        self.getParameters()
        if self._program is not None:
            self.setSharedAttr('ScanStage', 'scan_program', program_to_dict(self._program))

    def updateScanTTLAttrs(self):
        self.getParameters()

    def getComponentState(self) -> dict:
        self.getParameters()
        program = self._program
        state = {
            'controller': type(self).__name__,
            'scanWidgetType': getattr(self._setupInfo.scan, 'scanWidgetType', None),
            'program': program_to_dict(program) if program is not None else None,
            'timingPolicy': program.timing_policy if program is not None else None,
            'mode': {'repeatEnabled': self._widget.repeatEnabled()},
            'requiredFeatures': {PROGRAM_FEATURE: PROGRAM_FEATURE_REASON},
        }
        origin = self._advancedOrigin
        if origin is not None and program is not None and program == origin[0]:
            # Unchanged since it was opened: the Advanced panel can still run it.
            state.pop('requiredFeatures')
            state['analogParameterDict'] = copy.deepcopy(origin[1])
            state['digitalParameterDict'] = copy.deepcopy(origin[2])
        return state

    def applyComponentState(self, state, *, applyMode):
        if self.isRunning:
            return ['Scan is currently running; scan parameters were not changed.']
        if not isinstance(state, dict):
            return ['Saved scan state is not a dictionary.']
        refusal = self._unsupportedStateFeatureRefusal(state)
        if refusal:
            return [refusal]
        warnings = []
        try:
            if state.get('program'):
                program = program_from_dict(state['program'])
                origin = None
                if state.get('analogParameterDict'):
                    origin = (program, state['analogParameterDict'],
                              state['digitalParameterDict'])
            elif is_legacy_advanced_state(state):
                program, origin = self._programFromAdvanced(state)
            else:
                return ['Saved scan state holds no scan program; it was not applied.']
        except (ProgramError, AdvancedConversionError, KeyError, ValueError) as error:
            return [f'{error} Scan state was not applied.']
        missing = self._missingDevices(program)
        if missing:
            return [f'This setup has no {", ".join(missing)}. Scan state was not applied.']
        mode = state.get('mode') or {}
        if mode.get('repeatEnabled') is not None:
            self._widget.setRepeatEnabled(bool(mode['repeatEnabled']))
        self.setProgram(program, advancedOrigin=origin)
        self.signalDict = None
        self.scanInfoDict = None
        return warnings

    def _programFromAdvanced(self, state):
        """An Advanced scan file as a program, as the Advanced panel would
        build it here: its designer parameters, line steps and line program."""
        analog = copy.deepcopy(state['analogParameterDict'])
        digital = copy.deepcopy(state['digitalParameterDict'])
        stage = copy.deepcopy(getattr(self._setupInfo.scan, 'scanDesignerParams', None) or {})
        stage.update(analog)
        stage['n_linesteps'] = int(digital.get('n_linesteps', 1))
        for key in ('intra_pixel_positioner_movement', 'positioner_target_device',
                    'positioner_linestep_enable', 'positioner_movement_starts_s',
                    'positioner_movement_ends_s', 'positioner_step_size_um'):
            if key in digital:
                stage[key] = copy.deepcopy(digital[key])
        # Beta's positions are relative to where the stage is when it starts.
        stage['axis_position_before_scan'] = [[0.0] for _ in stage['target_device']]
        program, _context = program_from_advanced(stage, digital, self._setupInfo)
        return program, (program, state['analogParameterDict'], state['digitalParameterDict'])

    def _missingDevices(self, program):
        known = set(self._setupInfo.positioners) | set(self._setupInfo.lasers or {}) \
            | set(self._setupInfo.detectors)
        clocks = {'line_clock', 'frame_start_clock', 'frame_end_clock'}
        names = {d.device for d in program.drives} | {
            e.device for e in program.timeline if e.device not in clocks}
        return sorted(names - known)


__all__ = ['PROGRAM_FEATURE', 'ScanControllerExpert']


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
