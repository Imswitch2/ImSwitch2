"""The Expert scan panel's controller (docs/design/plans/
scan-engines-and-expert-scan.md §4, M3).

The panel edits a scan program; this controller turns it into what a scan
engine runs and reports what the engine makes of it. The program's devices
choose the engine (§7.10): the one their channels are on, the leader with
its followers when they spread over those, ``scan.engine`` when none is on
an engine.

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

from typing import Dict

import copy
import math
import time
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
from imswitch.imcontrol.model.scan_program.bindings import (
    binding_lines, binding_problems, frames_per_step,
)
from imswitch.imcontrol.model.scan_program.densify import densify
from imswitch.imcontrol.model.managers.scan_engines.loader import EngineChoice
from imswitch.imcontrol.model.scan_program.engine import ScanEngineRefused, setup_channels
from imswitch.imcontrol.model.scan_program.pattern_plugins import load_user_patterns
from imswitch.imcontrol.model.scan_program.policy import PUBLISHED
from imswitch.imcontrol.model.scan_program.preparation import resolve_preparation
from imswitch.imcontrol.model.scan_program.script_approval import (
    approvals, unapproved_script,
)
from imswitch.imcontrol.model.scan_program.scan_info import (
    RASTER_KEY, RASTER_REASON_KEY, TRAJECTORY_KEY, scan_info_for,
)
from imswitch.imcontrol.model.scan_program.trajectory import RealizedTrajectory, US_PER_S
from imswitch.imcontrol.model.scan_program.schedule import approach_span, format_seconds
from imswitch.imcontrol.model.scan_program.serialize import (
    FORMAT, program_from_dict, program_to_dict,
)
from ..basecontrollers import SuperScanController, same_run_signals
from .ScanControllerAdvanced import SCAN_PROGRESS_INTERVAL_MS, scan_progress
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


def _run_parts(engine, compiled):
    """The engines a compile ran on: a multi-engine run's leader and
    followers, else the engine itself."""
    if getattr(compiled, 'followers', None) and hasattr(engine, 'followers'):
        return [engine.leader] + [e for e, _ in engine.followers.values()]
    return [getattr(engine, 'leader', engine)]


def _run_outcomes(engine, compiled):
    """``{engine: (kind, reason)}`` of the run that ended; an engine that
    keeps no outcome (the NI-DAQ: its iteration always runs to its end) is
    done."""
    if getattr(compiled, 'followers', None) and callable(getattr(engine, 'partsOutcome', None)):
        return dict(engine.partsOutcome() or {})
    out = {}
    for part in _run_parts(engine, compiled):
        outcome = getattr(part, 'outcome', None)
        out[part.name] = outcome if outcome is not None else ('done', '')
    return out


def _end_positions(schedule):
    """Where a finished run leaves each device it moves, in µm."""
    positions = getattr(schedule, 'positions_um', None)
    if positions is not None:           # a trajectory: its last sample
        return {device: float(values[-1]) for device, values in positions.items()}
    return dict(schedule.expansion.restore_um)


def _preview_windows(schedule, program):
    """Where the preview's zooms look, in s on the drawn (first) segment: the
    first step (a trajectory: its first window), the first pass of the
    innermost loop (the path), the whole of it."""
    rate = schedule.rate_hz
    if isinstance(schedule, RealizedTrajectory):
        first = int(schedule.window_start[0])
        last = int(schedule.window_start[-1] + schedule.window_ticks[-1])
        return ((first / rate, (first + int(schedule.window_ticks[0])) / rate),
                (first / rate, last / rate), None)
    steps = [s for s in schedule.step_spans() if s.segment == 0]
    if not steps:
        return None
    start = steps[0].start_tick
    step = (start / rate, (start + steps[0].ticks) / rate)
    returns = [s for s in schedule.spans
               if s.segment == 0 and s.span.kind == 'return' and s.start_tick >= start]
    if returns:
        end = returns[0].start_tick + returns[0].ticks
    else:
        inner = int(program.loops[-1].count) if program.loops else 1
        last = steps[min(len(steps), inner) - 1]
        end = last.start_tick + last.ticks
    return step, (start / rate, end / rate), None


def _logged_positions(parts):
    """Each device's last write in the engines' logs, in µm (a step board's
    log comes back in the program's terms); an engine whose log cannot be
    read says nothing."""
    positions = {}
    for part in parts:
        try:
            log = part.log
        except Exception:
            continue
        for entry in log.of('dac'):
            positions[entry[3]] = float(entry[4])
    return positions


class ScanControllerExpert(SuperScanController):
    supportedStateFeatures = frozenset({PROGRAM_FEATURE})

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._program = None
        self._programError = ''
        self._advancedOrigin = None     # (program, analog, digital) of an opened Advanced file
        self._compiled = None
        self._compiledFor = None
        self._compiledEngine = None     # the engine the compile is for
        # The program is authoritative; the tables are read back only after
        # an edit, so an opened file's request is never re-rounded by them.
        self._edited = False

        # Drop-in trajectory patterns join the Make… presets; a file that
        # does not load is skipped with the reason.
        loaded = load_user_patterns()
        for problem in loaded.errors:
            self._logger.warning(f'Scan pattern {problem.path} skipped: {problem.message}.')
        if loaded.ids:
            self._logger.info(f'Drop-in scan patterns: {", ".join(loaded.ids)}.')

        detectors = list(self._setupInfo.detectors)
        lasers = [name for name in self.TTLDevices if name not in detectors]
        engines = getattr(self._master, 'scanEngines', None)
        used = sorted({name for name in (engines.owners.values() if engines else ())
                       if name is not None})
        # Said under the toolbar only when it is not obvious: with one
        # engine the header's chip names it.
        where = ''
        if len(used) > 1:
            where = (f'Runs on the scan engine its devices are on ({", ".join(used)}); '
                     f'without devices on {engines.selected}.')
        wavelengths = {name: getattr(info, 'wavelength', None)
                       for name, info in (self._setupInfo.lasers or {}).items()}
        self._titleBase = None          # the template the program was made from
        self._templateValues: Dict[str, Dict] = {}   # the last values used per template
        self._widget.initControls(
            list(self.positioners), lasers, 'ms', detectorNames=detectors,
            presets=[(p.id, p.title) for p in presets.PRESETS.values()],
            policies=sorted(PUBLISHED), engine=where, wavelengths=wavelengths,
            pointDetectorNames=self._pointDetectors(detectors))

        self._reportTimer = QtCore.QTimer(self)
        self._reportTimer.setSingleShot(True)
        self._reportTimer.setInterval(REPORT_SETTLE_MS)
        self._reportTimer.timeout.connect(self.updateReport)
        self._widget.sigProgramEdited.connect(self._onEdited)
        self._widget.sigPresetRequested.connect(self.makeFromPreset)
        self._widget.sigPlotClicked.connect(self.plotProgram)

        # Progress while a run of this panel's lasts: a step engine counts its
        # steps (waits included), the rest by time against the duration.
        self._progressSteps = None
        self._progressStarted = None
        self._progressDuration = None
        self._progressTimer = QtCore.QTimer(self)
        self._progressTimer.setInterval(SCAN_PROGRESS_INTERVAL_MS)
        self._progressTimer.timeout.connect(self._updateScanProgress)

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
        made_from = presets.preset_of(program)[0] if program is not None else ''
        self._titleBase = presets.PRESETS[made_from].title if made_from in presets.PRESETS \
            else None
        self._advancedOrigin = advancedOrigin
        self._programError = '' if program is not None else self._EMPTY
        self.setParameters()
        self.updateReport()

    _EMPTY = 'The scan has no dimensions yet: start from a template, or add a dimension.'

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
        # The form opens with the values this scan was made from, else the
        # last values used for the template; what is still empty is filled
        # from the rig (tracked positions, declared setup times), never
        # guessed (§7.12).
        initial = params if made_from == preset_id else dict(
            self._templateValues.get(preset_id) or {})

        def validate(candidate):
            try:
                presets.make(preset_id, candidate)
            except (presets.PresetError, ProgramError, ValueError, TypeError) as error:
                return str(error)
            return ''

        params = self._widget.askPresetParams(
            preset, initial, validate, positions=self._trackedPositions(),
            setup=self._declaredSetupTimes())
        if params is not None:
            # Code written or read in the form has been seen here (§7.10).
            for field in preset.fields:
                if field.kind == 'code' and params.get(field.name):
                    approvals().approve(params[field.name])
            self._templateValues[preset_id] = copy.deepcopy(params)
            self.setProgram(presets.make(preset_id, params))

    def _trackedPositions(self) -> Dict[str, float]:
        """Each positioner's tracked position on its scan axis, in µm, where
        it is known."""
        positions = {}
        for device, info in (self._setupInfo.positioners or {}).items():
            try:
                manager = self._master.positionersManager[device]
                axes = list(getattr(info, 'axes', None) or manager.axes)
                positions[device] = float(manager.position[axes[0]])
            except (KeyError, IndexError, TypeError, ValueError, AttributeError):
                continue
        return positions

    def _declaredSetupTimes(self) -> Dict[str, float]:
        """The move, settle and return times the setup declares for stepped
        scans (``scanDesignerParams``), in seconds -- only what it declares."""
        declared = getattr(self._setupInfo.scan, 'scanDesignerParams', None) or {}
        times = {}
        for key in ('move_time', 'settle_time', 'return_time'):
            try:
                if declared.get(key) is not None:
                    times[key] = float(declared[key])
            except (TypeError, ValueError):
                continue
        return times

    def _approveScript(self):
        """Whether the program may run its trajectory script: approved
        already, or shown now and approved (§7.10). Asked only where someone
        acts on the panel -- Load, Plot, Start; a restore or an API call
        never asks, and the report says the script is held."""
        self.getParameters()
        code = unapproved_script(self._program)
        if code is None:
            return True
        if not self._widget.askApproveScript(code):
            text = ('The trajectory script was not approved, so it did not run. Draw or Run '
                    'asks again.')
            self._widget.setReport(text)
            self._widget.showNotice(text)
            return False
        approvals().approve(code)
        self.updateReport()
        return True

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

    def _engineSignalSlots(self):
        # A step engine ends its run by sigScanStopped and sigScanFailed too
        # and counts its steps; the NI-DAQ has none of these.
        return super()._engineSignalSlots() + (
            ('sigScanStopped', self._SuperScanController__onNidaqScanDone),
            ('sigScanFailed', self._onEngineFailed),
            ('sigScanStarted', self._onRunStarted),
            ('sigScanProgress', self._onStepProgress))

    def _choice(self, program) -> EngineChoice:
        """Where ``program`` runs: the engine its devices are on (§7.10)."""
        engines = getattr(self._master, 'scanEngines', None)
        if engines is None:        # a master without engines (test doubles)
            return EngineChoice(self._engineForScans(), {})
        return engines.for_program(program, setup_channels(self._setupInfo))

    def _compile(self):
        """The compile of the current program on the engine its devices
        choose, or ScanDesignRefusedError with every reason."""
        self.getParameters()
        if self._program is None:
            raise ScanDesignRefusedError(self._programError)
        choice = self._choice(self._program)
        if choice.engine is None:
            raise ScanDesignRefusedError(choice.refusal)
        engine = choice.engine
        captured = self._capturedPositions(self._program)
        key = (engine.name, program_to_dict(self._program), tuple(sorted(captured.items())))
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
        # What each detector records is its binding's, checked against its
        # line before anything runs (§2.5, §7.10).
        problems = binding_problems(
            self._program, self._schedulesOf(compiled),
            point_detectors=self._pointDetectors([b.detector for b in self._program.detectors]),
            known_detectors=list(self._setupInfo.detectors))
        if problems:
            raise ScanDesignRefusedError('The detector bindings do not hold:\n- '
                                         + '\n- '.join(problems))
        self._compiled, self._compiledFor, self._compiledEngine = compiled, key, engine
        return compiled

    @staticmethod
    def _schedulesOf(compiled):
        """The run's schedule; on several engines, the leader's and each
        follower's (a detector's line is on one of them)."""
        schedules = [compiled.schedule]
        for part in (getattr(compiled, 'followers', None) or {}).values():
            if getattr(part, 'schedule', None) is not None:
                schedules.append(part.schedule)
        return schedules

    def _pointDetectors(self, names):
        return scan_driven_detector_names(self, names)

    def _scheduleOf(self, compiled):
        """The schedule the engine compiled the program to -- every engine
        keeps it (one authority: the panel shows what the run plays)."""
        schedule = getattr(compiled, 'schedule', None)
        if schedule is None:
            raise ScanDesignRefusedError(
                f'{self._compiledEngine.name} kept no schedule of the program, so its timing '
                'cannot be shown.')
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
        # The run is on the engine the program chose (bound as the run
        # began): a continuation of it cannot move to another one.
        engine = self._compiledEngine
        if not same_run_signals(engine, self._engineForScans()):
            raise ScanDesignRefusedError(
                f'This program runs on {engine.name}, and the scan in progress on '
                f'{self._engineForScans().name}; stop it first.')
        self._bindScanEngine(engine)
        # The file says what ran (§7.9): the realized timing beside the
        # program as requested.
        self.setSharedAttr('ScanStage', 'scan_schedule', self._scheduleOf(compiled).summary())
        info = copy.deepcopy(self._scanInfoOf(compiled))
        refusal = self._pointDetectorRefusal(info)
        if refusal:
            raise ScanDesignRefusedError(refusal)
        signals = getattr(compiled, 'signalDict', None)
        if signals is None:   # a step engine runs the program itself
            signals = {'scanSignalsDict': {}, 'TTLCycleSignalsDict': {}, 'compiled': compiled}
        return signals, info

    def _title(self, program):
        """What the panel calls the program: its template, or ``…, edited``."""
        if program is not None and program.trajectory is not None:
            preset = presets.PRESETS.get(f'trajectory-{program.trajectory.pattern}')
            return preset.title if preset else f'Trajectory: {program.trajectory.pattern}'
        made_from = presets.preset_of(program)[0] if program is not None else ''
        if made_from in presets.PRESETS:
            return presets.PRESETS[made_from].title
        return f'{self._titleBase}, edited' if self.__dict__.get('_titleBase') else \
            'New scan'

    def _presetOf(self, program) -> str:
        """The template a program was made from (its path's for a
        trajectory), or the one it was made from before it was edited."""
        if program is not None and program.trajectory is not None:
            preset = f'trajectory-{program.trajectory.pattern}'
            return preset if preset in presets.PRESETS else ''
        made_from = presets.preset_of(program)[0] if program is not None else ''
        if made_from in presets.PRESETS:
            return made_from
        base = self.__dict__.get('_titleBase')
        return next((p.id for p in presets.PRESETS.values() if p.title == base), '') \
            if base else ''

    @staticmethod
    def _trajectorySummary(program):
        """A path in words, for the panel (a path is not loops)."""
        if program is None or program.trajectory is None:
            return None
        path = program.trajectory
        lasers = sorted({e.device for e in program.timeline})
        text = (f'{" and ".join(path.devices)} follow a {path.pattern} for '
                f'{format_seconds(path.duration_s)}, measured every '
                f'{format_seconds(program.step_s)} and gridded at {path.pixel_um:g} µm; '
                f'{format_seconds(path.approach_s)} onto the path and back.')
        return {'text': text, 'preset': f'trajectory-{path.pattern}'
                if f'trajectory-{path.pattern}' in presets.PRESETS else '',
                'events': (f'On along the path: {", ".join(lasers)}.' if lasers else '')}

    def updateReport(self):
        """Compile what the panel shows and say what the engine makes of it:
        as text (the report) and as data (the header and the check)."""
        self.getParameters()
        choice = self._choice(self._program)
        title = self._title(self._program)
        check = {'title': title,
                 'engine': choice.engine.name if choice.engine is not None else '',
                 'preset': self._presetOf(self._program),
                 'edited': title.endswith(', edited'),
                 'trajectory': self._trajectorySummary(self._program)}
        try:
            compiled = self._compile()
            schedule = self._scheduleOf(compiled)
        except ScanDesignRefusedError as error:
            where = f' on {choice.engine.name}' if choice.engine is not None else ''
            self._widget.setReport(f'Not runnable{where}.\n\n{error}')
            refused = getattr(error.__cause__, 'report', None)
            # each reason the once: the engine's, where it gave them
            problems = list(refused.reasons()) if refused is not None else [str(error)]
            facts = {}
            caps = getattr(choice.engine, 'capabilities', None)
            if callable(caps):
                try:
                    facts['grid_s'] = caps().timing_grid_s
                except Exception:
                    pass
            if self._program is not None:
                facts['policy'] = self._program.timing_policy
            check.update(runnable=False, problems=problems, facts=facts, verdicts=[
                (v.verdict, v.feature, v.reason or v.cost) for v in
                (refused.verdicts if refused is not None else ())])
            self._widget.setCheck(check)
            return
        lines = [f'Runnable on {choice.engine.name}.']
        if len(choice.devices) > 1:
            lines += [f'{name}: {", ".join(found)}' for name, found in sorted(choice.devices.items())]
        lines.append('')
        for verdict in compiled.report.verdicts:
            text = f'{verdict.verdict}: {verdict.feature}'
            if verdict.reason or verdict.cost:
                text += f' -- {verdict.reason or verdict.cost}'
            lines.append(text)
        check.update(runnable=True, problems=[], verdicts=[
            (v.verdict, v.feature, v.reason or v.cost) for v in compiled.report.verdicts])
        facts = {'grid_s': schedule.grid_s, 'policy': schedule.policy.id,
                 'duration_s': self._duration_s(schedule)}
        notes = []
        if len(choice.devices) > 1:
            notes.append('Runs on ' + '; '.join(f'{name} ({", ".join(found)})' for name, found
                                                in sorted(choice.devices.items())) + '.')
        recorded = []
        if not isinstance(schedule, RealizedTrajectory):
            detectors = list(self._setupInfo.detectors)
            point = self._pointDetectors(detectors)
            schedules = self._schedulesOf(compiled)
            recorded = binding_lines(compiled.program, schedules, detectors,
                                     point_detectors=point)
            if recorded:
                lines += [''] + recorded
            facts['steps'] = len(schedule.step_spans())
            facts['frames'] = {
                detector: total for detector in detectors if detector not in point
                for total in [sum(frames_per_step(compiled.program, schedules, detector))]
                if total}
        lines += ['', f'Timing policy {schedule.policy.id}, '
                      f'{format_seconds(schedule.grid_s)} grid.']
        if isinstance(schedule, RealizedTrajectory):
            path = self._trajectoryReport(schedule)
            lines += path
            notes += path
            refusal = self._durationRefusal(compiled)
            if refusal:
                lines.append(refusal)
                check.update(runnable=False, problems=[refusal])
            self._widget.setReport('\n'.join(lines))
            self._widget.setCheck({**check, 'facts': facts, 'notes': notes,
                                   'recorded': recorded})
            return
        lines.append(f'Duration {format_seconds(self._duration_s(schedule))}, '
                     f'{len(schedule.step_spans())} steps.')
        approach = approach_span(schedule)
        if approach is not None:
            text = (f'Starts with {format_seconds(approach.ticks / schedule.rate_hz)} for '
                    'the drives to reach their first positions and settle.')
            lines.append(text)
            notes.append(text)
        elif not compiled.program.approach:
            text = ('Starts at its first positions, without an approach (an Advanced '
                    'scan, as its designer plays it).')
            lines.append(text)
            notes.append(text)
        for change in schedule.changes:
            text = (f'Runs differently: {change.quantity} at {change.where}, '
                    f'{format_seconds(change.requested_s)} as '
                    f'{format_seconds(change.realized_s)}.')
            lines.append(text)
            notes.append(text)
        info = self._scanInfoOf(compiled)
        if not info.get(RASTER_KEY):
            text = f'Point detectors cannot assemble it: {info.get(RASTER_REASON_KEY)}'
            lines.append(text)
            # on the panel only where a point detector is bound to take part
            point = set(self._pointDetectors([b.detector for b in compiled.program.detectors]))
            if point:
                notes.append(text)
        refusal = self._durationRefusal(compiled)
        if refusal:
            lines.append(refusal)
            check.update(runnable=False, problems=[refusal])
        self._widget.setReport('\n'.join(lines))
        self._widget.setCheck({**check, 'facts': facts, 'notes': notes, 'recorded': recorded})

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
        if not self._approveScript():
            self._widget.showPreviewProblem('Nothing drawn: the trajectory script was not '
                                            'approved.')
            return
        try:
            compiled = self._compile()
        except ScanDesignRefusedError as error:
            self._widget.setReport(str(error))
            self._widget.showPreviewProblem(f'Nothing to draw: {error}')
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
        segments = len(getattr(schedule, 'segments', ()) or ())
        self._widget.showTraces(
            np.arange(n) / rate, traces, analog=sorted(dense['analog']),
            windows=_preview_windows(schedule, compiled.program),
            note=('Drawn up to the first wait for a trigger: what follows depends on when it '
                  'comes.') if segments > 1 else '')

    # ------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------

    def runScan(self):
        # The Start button: someone is at the panel to approve a script.
        if self._approveScript():
            super().runScan()

    def loadScan(self):
        before = self._program
        super().loadScan()
        if self._program is not before:
            self._approveScript()

    def _beginScanRunWithDesign(self, **kwargs):
        # A new run runs on the engine its program's devices choose, heard
        # from and stopped by it; a run in progress keeps its engine.
        if self._scanCoordinator.runForOwner(self) is None:
            self.getParameters()
            engine = self._choice(self._program).engine
            if engine is not None:
                self._bindScanEngine(engine)
        return super()._beginScanRunWithDesign(**kwargs)

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

    def _onRunStarted(self):
        if self._scanCoordinator.tokenForOwner(self) is None:
            return
        # The compile that is running: where it leaves the positioners is
        # told to them when it ends (_trackRunEndPositions).
        self._runCompiled = self._compiled
        self._progressSteps = None
        self._progressStarted = time.monotonic()
        schedule = getattr(self._compiled, 'schedule', None)
        segments = getattr(schedule, 'segments', None)
        self._progressDuration = (self._duration_s(schedule)
                                  if schedule is not None and len(segments or (0,)) == 1
                                  else None)
        self._updateScanProgress()
        self._progressTimer.start()

    def _onStepProgress(self, step, steps):
        self._progressSteps = (int(step), int(steps))

    def _updateScanProgress(self):
        if self._scanCoordinator.runForOwner(self) is None or self._progressStarted is None:
            self._progressTimer.stop()
            self._progressStarted = None
            self._widget.hideScanProgress()
            return
        if self._progressSteps is not None:
            step, steps = self._progressSteps
            self._widget.showScanProgress(step / steps if steps else None,
                                          f'Step {step + 1} of {steps}')
            return
        fraction, text = scan_progress(time.monotonic() - self._progressStarted,
                                       self._progressDuration, 1, show_frame=False)
        self._widget.showScanProgress(fraction, text)

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

    def _trackRunEndPositions(self):
        """Tell each positioner the run moved where the run left it (§7.9):
        a finished run, where its program ends -- the restore position, or a
        trajectory's last sample; a stopped or failed one, a step board's
        last write (its log). The next run captures its origin there, and
        a move by hand starts from there. Where it is not known, that is
        said and the tracked position is left as it was."""
        compiled = self.__dict__.get('_runCompiled')
        self._runCompiled = None
        if compiled is None:
            return
        engine = self._engineForScans()
        ended = _run_outcomes(engine, compiled)
        if all(kind == 'done' for kind, _ in ended.values()):
            positions, how = _end_positions(compiled.schedule), 'finished'
        else:
            positions = _logged_positions(_run_parts(engine, compiled))
            how = 'stopped' if all(kind != 'failed' for kind, _ in ended.values()) else 'failed'
        for device in self._movedDevices(compiled.program):
            if device not in positions:
                self._logger.warning(f'Where the {how} run left {device} is not known; its '
                                     'tracked position is left as it was.')
                continue
            manager = self._master.positionersManager[device]
            info = self._setupInfo.positioners[device]
            axes = list(getattr(info, 'axes', None) or manager.axes)
            manager.updateTrackedPosition({axes[0]: float(positions[device])})

    def scanFailed(self, **kwargs):
        self._trackRunEndPositions()
        super().scanFailed(**kwargs)

    def scanDone(self):
        self.isRunning = False
        self._trackRunEndPositions()
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
        schedules = self._schedulesOf(compiled)
        for detector in self._setupInfo.detectors:
            # the binding's frames, or its line's (§2.5): what the layout says
            edges = sum(frames_per_step(compiled.program, schedules, detector))
            if edges and positions and edges % positions == 0:
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
            compiled.program, self._schedulesOf(compiled), detectorNames,
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
            'templateValues': copy.deepcopy(self._templateValues),
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
        remembered = state.get('templateValues')
        if isinstance(remembered, dict):
            self._templateValues = {str(k): dict(v) for k, v in remembered.items()
                                    if isinstance(v, dict)}
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
