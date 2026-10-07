"""The measurement-run engine: set controls → settle → window → sample → commit.

Design: ``docs/design/plans/transient-instruments-step-scans.md`` §8. The
runner reserves its controls, instruments and ``waveform-output`` for the
whole run. It writes every point through one journal writer
(``commit_point``) and reports three separate outcomes:

- **acquisition**: complete / stopped / failed;
- **cleanup**: return to start; never dispatched to a quarantined backend;
- **lifecycle**: active → cleaning up → finished. A run is finished only once
  every resource is released or explicitly left quarantined.

Data never depends on cleanup. The journal is finished when acquisition
ends; cleanup only amends its end record before the run file is written.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import logging
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from imswitch.imcommon.model.measurement_run import (
    AcquisitionOutcome,
    CleanupOutcome,
    ControlResult,
    PointStatus,
    RunJournalWriter,
    RunLifecycle,
    Verification,
    WindowCause,
    WindowResult,
    MeasurementRunFile,
    finalize_journal,
)

from ..resources import (
    WAVEFORM_OUTPUT,
    ResourceRegistry,
    ResourceReservedError,
    get_resource_registry,
    instrument_key,
)
from .controls import READBACK_FRESH, ControlExecutor, RunControl, get_control_executor
from .generators import PointSequence
from .instrument import InstrumentSession, WindowFault, WindowRefused

_logger = logging.getLogger(__name__)

#: Window causes that end a run (the instrument or the user ended it);
#: others (timeout, invalid samples) fail only the point.
_RUN_ENDING_CAUSES = {WindowCause.TRANSPORT_FAULT, WindowCause.CANCELLED}


class RunRefused(RuntimeError):
    """The run cannot start; nothing was moved or written."""


@dataclass
class RunSettings:
    samples_per_point: int = 3
    #: Extra settle time on top of the controls' own settle time.
    settle_s: float = 0.0
    move_deadline_s: float = 30.0
    #: How long to wait for commands already running on the run's devices.
    reserve_deadline_s: float = 5.0
    position_deadline_s: float = 5.0
    window_deadline_s: float = 10.0
    #: Consecutive point-level failures (timeouts, invalid samples) that fail the run.
    max_consecutive_failed_points: int = 3
    return_to_start: bool = True
    cleanup_deadline_s: float = 30.0
    allow_unverified_timing: bool = False
    plane_label: str = ''
    #: Declared illumination: ``{'source': <laser entry>, 'wavelength_nm': float}``.
    illumination: Optional[Mapping[str, Any]] = None
    notes: str = ''
    fsync: bool = True
    keep_journal: bool = False


@dataclass(frozen=True)
class PrepareStep:
    """Runs inside the reservation before the first point (e.g. dark zero).

    ``fn(token)`` may return a mapping, recorded in the run metadata under
    ``preparation.<label>``. An exception fails the run before any point.
    """

    label: str
    fn: Callable[[Optional[str]], Optional[Mapping[str, Any]]]


@dataclass(frozen=True)
class CleanupStep:
    """Runs inside the reservation after acquisition (e.g. restore a laser).

    Never dispatched while ``resource`` is quarantined; its outcome is part
    of the cleanup outcome, never of the data.
    """

    label: str
    resource: str
    fn: Callable[[Optional[str]], None]
    #: Also run after a movement failure (restoring a safe state usually should).
    after_failure: bool = True


class _PrepareFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class ProgressEvent:
    point: int
    total: int
    status: PointStatus
    setpoints: Mapping[str, float]


@dataclass
class RunReport:
    run_id: str
    run_file: Optional[Path]
    journal_dir: Path
    acquisition: AcquisitionOutcome
    cleanup: CleanupOutcome
    lifecycle: RunLifecycle
    points_total: int
    points_committed: int
    points_failed: int
    detail: str = ''
    cleanup_detail: str = ''
    quarantined: List[str] = field(default_factory=list)
    finalize_error: str = ''


class MeasurementRunner:
    def __init__(
        self,
        *,
        sequence: PointSequence,
        controls: Sequence[RunControl],
        instruments: Mapping[str, InstrumentSession],
        folder: Path,
        settings: Optional[RunSettings] = None,
        run_id: Optional[str] = None,
        owner: str = 'measurement run',
        executor: Optional[ControlExecutor] = None,
        progress: Optional[Callable[[ProgressEvent], None]] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        prepare: Sequence[PrepareStep] = (),
        cleanup_steps: Sequence[CleanupStep] = (),
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.sequence = sequence
        self.controls = {c.name: c for c in controls}
        self.instruments = dict(instruments)
        self.folder = Path(folder)
        self.settings = settings or RunSettings()
        self.run_id = run_id or _new_run_id()
        self.owner = owner
        # One registry per process: managers and instruments admit through it too.
        self.registry = get_resource_registry()
        self.executor = executor if executor is not None else get_control_executor()
        self._progress = progress
        self._prepare = tuple(prepare)
        self._cleanup_steps = tuple(cleanup_steps)
        self._extra_metadata = dict(metadata or {})
        self._clock = clock
        self._wall = wall_clock
        self._cancel = threading.Event()
        self._cancel_reason = ''
        self._fault: Optional[str] = None
        self.acquisition = AcquisitionOutcome.RUNNING
        self.cleanup = CleanupOutcome.PENDING
        self.lifecycle = RunLifecycle.ACTIVE
        self._reservation = None
        self._finished = threading.Event()

    # --------------------------------------------------------------- control
    def stop(self, reason: str = 'stopped by user') -> None:
        if not self._cancel.is_set():
            self._cancel_reason = reason
            self._cancel.set()

    def wait_finished(self, timeout_s: Optional[float] = None) -> bool:
        return self._finished.wait(timeout_s)

    @property
    def _token(self) -> Optional[str]:
        return self._reservation.token if self._reservation is not None else None

    @property
    def journal_dir(self) -> Path:
        return self.folder / f'{self.run_id}.journal'

    # ------------------------------------------------------------------ run
    def run(self) -> RunReport:
        """Acquire, clean up, write the run file, release — on every path.

        A script cancellation (``OperationCancelled``, a ``BaseException``) or
        any other interrupt stops acquisition like Stop does; cleanup, the
        run file and the release of every resource still happen, and the
        interrupt is re-raised afterwards.
        """
        self._preflight()
        try:
            self._reservation = self.registry.reserve(
                self._resources(), self.owner, deadline_s=self.settings.reserve_deadline_s)
        except ResourceReservedError as exc:
            raise RunRefused(str(exc)) from None

        interrupt: Optional[BaseException] = None
        writer = None
        starts: Dict[str, Optional[float]] = {}
        committed = failed = 0
        detail = ''
        movement_failed = False
        cleanup_detail = ''
        quarantined: List[str] = []
        run_file = None
        finalize_error = ''
        try:
            for session in self.instruments.values():
                session.add_fault_listener(self._on_fault)
            try:
                starts = self._start_positions()
                prepared = self._run_prepare()
                self.folder.mkdir(parents=True, exist_ok=True)
                metadata = self._metadata(starts)
                if prepared:
                    metadata['preparation'] = prepared
                writer = RunJournalWriter(
                    self.journal_dir,
                    metadata=metadata,
                    instruments={name: s.quantities for name, s in self.instruments.items()},
                    controls=list(self.controls),
                    fsync=self.settings.fsync,
                )
                consecutive_failed = 0
                for index in range(len(self.sequence)):
                    if self._cancel.is_set():
                        break
                    status, controls_ok, run_ending = self._measure_point(writer, index)
                    if status is PointStatus.COMMITTED:
                        committed += 1
                        consecutive_failed = 0
                    else:
                        failed += 1
                        consecutive_failed += 1
                    if not controls_ok:
                        movement_failed = True
                        detail = detail or f'point {index}: a control did not apply'
                        break
                    if run_ending:
                        break
                    if consecutive_failed >= self.settings.max_consecutive_failed_points:
                        detail = f'{consecutive_failed} consecutive points failed'
                        break
                self.acquisition = self._acquisition_outcome(
                    index_done=committed + failed, movement_failed=movement_failed,
                    detail=detail)
                if self._fault:
                    detail = f'instrument fault: {self._fault}'
                elif self._cancel.is_set() and not detail:
                    detail = self._cancel_reason
            except _PrepareFailed as exc:
                self.acquisition = AcquisitionOutcome.FAILED
                detail = f'preparation failed: {exc}'
            except Exception as exc:
                _logger.exception('measurement run %s failed', self.run_id)
                self.acquisition = AcquisitionOutcome.FAILED
                detail = f'{type(exc).__name__}: {exc}'
            except BaseException as exc:  # script cancellation, KeyboardInterrupt
                interrupt = exc
                self.stop(f'interrupted ({type(exc).__name__})')
                self.acquisition = AcquisitionOutcome.STOPPED
                detail = self._cancel_reason
            finally:
                for session in self.instruments.values():
                    session.remove_fault_listener(self._on_fault)
            if writer is not None:
                try:
                    writer.finish(self.acquisition, cleanup=CleanupOutcome.RUNNING,
                                  detail=detail)
                except Exception:
                    _logger.exception('could not finish the journal')

            # -------------------------------------------------------- cleanup
            self.lifecycle = RunLifecycle.CLEANING_UP
            self.cleanup = CleanupOutcome.RUNNING
            try:
                cleanup_outcome, cleanup_detail, quarantined = self._cleanup(
                    starts, movement_failed)
            except BaseException as exc:
                _logger.exception('cleanup of measurement run %s failed', self.run_id)
                cleanup_outcome = CleanupOutcome.FAILED
                cleanup_detail = f'cleanup interrupted: {type(exc).__name__}: {exc}'
                quarantined = self.executor.quarantined()
                if interrupt is None and not isinstance(exc, Exception):
                    interrupt = exc
            self.cleanup = cleanup_outcome

            if writer is not None:
                try:
                    writer.update_end(cleanup=cleanup_outcome.value,
                                      cleanup_detail=cleanup_detail, quarantined=quarantined)
                    run_file = finalize_journal(self.journal_dir, fsync=self.settings.fsync)
                    recovered = MeasurementRunFile.load(run_file)
                    if recovered.acquisition is AcquisitionOutcome.CORRUPT:
                        # Points were lost between journal and file: keep the
                        # journal for inspection and say so.
                        finalize_error = ('the journal was damaged; the run file holds '
                                          'only the points that validated, journal kept')
                    elif not self.settings.keep_journal:
                        shutil.rmtree(self.journal_dir, ignore_errors=True)
                except Exception as exc:
                    finalize_error = f'{type(exc).__name__}: {exc}'
                    _logger.exception('could not write the run file; journal kept at %s',
                                      self.journal_dir)
        finally:
            self._release(quarantined)

        if interrupt is not None:
            raise interrupt
        return RunReport(
            run_id=self.run_id, run_file=run_file, journal_dir=self.journal_dir,
            acquisition=self.acquisition, cleanup=self.cleanup, lifecycle=self.lifecycle,
            points_total=len(self.sequence), points_committed=committed,
            points_failed=failed, detail=detail, cleanup_detail=cleanup_detail,
            quarantined=quarantined, finalize_error=finalize_error,
        )

    # ------------------------------------------------------------- preflight
    def _preflight(self) -> None:
        missing = [c for c in self.sequence.controls if c not in self.controls]
        if missing:
            raise RunRefused(f'the point sequence uses unknown controls: {missing}')
        unused = [c for c in self.controls if c not in self.sequence.controls]
        if unused:
            raise RunRefused(f'controls not in the point sequence: {unused}')
        if not self.instruments:
            raise RunRefused('a measurement run needs at least one instrument')
        if self.settings.samples_per_point < 1:
            raise RunRefused('samples_per_point must be at least 1')
        for name, session in self.instruments.items():
            if not session.connected:
                raise RunRefused(f'instrument {name} is not connected')
            if session.faulted:
                raise RunRefused(f'instrument {name} is faulted: {session.faulted}')
            if session.profile is None:
                raise RunRefused(f'instrument {name}: no timing profile matches its settings')
            if (session.verification is Verification.UNVERIFIED
                    and not self.settings.allow_unverified_timing):
                raise RunRefused(
                    f'instrument {name}: timing profile {session.profile.id!r} is not '
                    f'verified; quantitative runs need verified timing '
                    f'(allow_unverified_timing is for characterisation only)')
        for control in self.controls.values():
            if self.executor.is_quarantined(control.resource):
                raise RunRefused(f'{control.resource} is busy (quarantined)')

    def _resources(self) -> List[str]:
        keys = {c.resource for c in self.controls.values()}
        keys |= {instrument_key(name) for name in self.instruments}
        # A cleanup step restores a device: the run must own it too.
        keys |= {step.resource for step in self._cleanup_steps}
        keys.add(WAVEFORM_OUTPUT)
        return sorted(keys)

    def _start_positions(self) -> Dict[str, Optional[float]]:
        starts = {}
        for name, control in self.controls.items():
            if control.capabilities.readback == READBACK_FRESH:
                starts[name] = self.executor.read_position(control, self.settings.position_deadline_s)
            else:
                starts[name] = None
        return starts

    def _metadata(self, starts) -> Dict[str, Any]:
        seq = self.sequence
        grid = None
        if seq.grid is not None:
            grid = {
                'axes': [{'name': n, 'values': list(v)} for n, v in seq.grid.axes],
                'traversal': seq.grid.traversal,
                'shape': list(seq.grid.shape),
                'index': [list(i) for i in seq.grid.index],
            }
        controls = []
        for name in self.controls:
            control = self.controls[name]
            caps = control.capabilities
            controls.append({
                'name': name, 'resource': control.resource, 'unit': caps.unit,
                'readback': caps.readback, 'acknowledges': caps.acknowledges,
                'can_stop': caps.can_stop, 'settle_s': caps.settle_s,
                'tolerance': caps.tolerance, 'start_position': starts.get(name),
                'zero_reference': getattr(control, 'zero_reference', {'state': 'unknown'}),
            })
        instruments = {}
        for name, session in self.instruments.items():
            profile = session.profile
            instruments[name] = {
                'identity': session.identity.as_dict() if session.identity else {},
                'settings': session.settings(),
                'timing_profile': None if profile is None else profile.id,
                'timing_rule': None if profile is None else profile.rule.value,
                'verification': session.verification.value,
            }
        meta = {
            'schema': 'imswitch-measurement-run',
            'run_id': self.run_id,
            'owner': self.owner,
            'created': _dt.datetime.fromtimestamp(self._wall(), _dt.timezone.utc).isoformat(),
            'imswitch_version': _imswitch_version(),
            'generator': seq.describe(),
            'grid': grid,
            'points': [list(v) for v in seq.values],
            'controls': controls,
            'instruments': instruments,
            'settings': {
                'samples_per_point': self.settings.samples_per_point,
                'settle_s': self.settings.settle_s,
                'move_deadline_s': self.settings.move_deadline_s,
                'window_deadline_s': self.settings.window_deadline_s,
                'allow_unverified_timing': self.settings.allow_unverified_timing,
                'return_to_start': self.settings.return_to_start,
            },
            'plane_label': self.settings.plane_label,
            'illumination': dict(self.settings.illumination) if self.settings.illumination else None,
            'notes': self.settings.notes,
        }
        meta.update(self._extra_metadata)
        return meta

    # ------------------------------------------------------------- one point
    def _measure_point(self, writer: RunJournalWriter, index: int):
        setpoints = self.sequence.setpoints(index)
        t_start = self._wall()
        results: Dict[str, ControlResult] = {}
        causes: List[str] = []
        controls_ok = True

        for name, value in setpoints.items():
            result = self.executor.apply(self.controls[name], value,
                                         self.settings.move_deadline_s, self._token)
            results[name] = result
            if not result.ok:
                controls_ok = False
                causes.append(f'{name}: {result.cause}')
                break
            if self._cancel.is_set():
                break

        windows: Dict[str, WindowResult] = {}
        if controls_ok and not self._cancel.is_set():
            settle = max([c.capabilities.settle_s for c in self.controls.values()] + [0.0])
            self._wait(settle + self.settings.settle_s)
            controls_ok = self._settle_within_tolerance(results, causes)

        if controls_ok and not self._cancel.is_set():
            for name, session in self.instruments.items():
                windows[name] = self._window(session)
                if windows[name].cause is not None:
                    causes.append(f'{name}: {windows[name].cause.value}'
                                  + (f' ({windows[name].detail})' if windows[name].detail else ''))
                if self._cancel.is_set():
                    break
            # Fresh positions after sampling: the recorded ``measured`` value.
            for name, control in self.controls.items():
                if name in results and control.capabilities.readback == READBACK_FRESH:
                    position = self.executor.read_position(
                        control, self.settings.position_deadline_s)
                    if position is not None:
                        results[name] = dataclasses.replace(results[name], measured=position)

        status = self._point_status(setpoints, results, windows)
        if self._cancel.is_set() and not causes:
            causes.append(self._cancel_reason or 'cancelled')
        writer.commit_point(
            point=index, status=status, t_start=t_start, t_end=self._wall(),
            controls=[results[n] for n in setpoints if n in results],
            windows=windows, grid_index=self.sequence.grid_index(index), causes=causes,
        )
        if self._progress is not None:
            try:
                self._progress(ProgressEvent(index, len(self.sequence), status, setpoints))
            except Exception:
                _logger.exception('progress callback failed')
        run_ending = self._cancel.is_set() or any(
            w.cause in _RUN_ENDING_CAUSES for w in windows.values())
        return status, controls_ok, run_ending

    def _settle_within_tolerance(self, results, causes) -> bool:
        ok = True
        for name, control in self.controls.items():
            caps = control.capabilities
            if caps.readback != READBACK_FRESH or caps.tolerance is None:
                continue
            target = results[name].requested
            deadline = self._clock() + self.settings.position_deadline_s
            position = None
            while True:
                position = self.executor.read_position(control, self.settings.position_deadline_s)
                if position is not None and abs(position - target) <= caps.tolerance:
                    break
                if self._clock() > deadline or self._cancel.is_set():
                    results[name] = dataclasses.replace(
                        results[name], measured=position, ok=False,
                        cause=f'not within ±{caps.tolerance:g} {caps.unit} of {target:g}'
                              f' (at {position!r})')
                    causes.append(f'{name}: {results[name].cause}')
                    ok = False
                    break
                self._wait(0.002)
            if not ok:
                break
        return ok

    def _window(self, session: InstrumentSession) -> WindowResult:
        try:
            boundary = session.open_window(
                allow_unverified=self.settings.allow_unverified_timing, owner=self._token)
        except WindowRefused as exc:
            return WindowResult(
                samples=(), invalid=(), discarded=0, complete=False,
                cause=(WindowCause.TRANSPORT_FAULT if isinstance(exc, WindowFault)
                       else WindowCause.REFUSED), verification=session.verification,
                profile_id=session.profile.id if session.profile else None,
                boundary_t=self._clock(), requested=self.settings.samples_per_point,
                detail=str(exc))
        return session.sample_window(boundary, self.settings.samples_per_point,
                                     self.settings.window_deadline_s, self._cancel,
                                     owner=self._token)

    def _point_status(self, setpoints, results, windows) -> PointStatus:
        all_controls = all(n in results and results[n].ok for n in setpoints)
        all_windows = (set(windows) == set(self.instruments)
                       and all(w.complete for w in windows.values()))
        if all_controls and all_windows:
            return PointStatus.COMMITTED
        if any(w.samples for w in windows.values()):
            return PointStatus.FAILED_PARTIAL
        return PointStatus.FAILED

    def _acquisition_outcome(self, *, index_done: int, movement_failed: bool,
                             detail: str) -> AcquisitionOutcome:
        if self._fault or movement_failed or detail:
            return AcquisitionOutcome.FAILED
        if self._cancel.is_set():
            return AcquisitionOutcome.STOPPED
        if index_done == len(self.sequence):
            return AcquisitionOutcome.COMPLETE
        return AcquisitionOutcome.FAILED

    def _on_fault(self, instrument: str, cause: str) -> None:
        self._fault = f'{instrument}: {cause}'
        self.stop(f'instrument fault: {self._fault}')

    def _wait(self, seconds: float) -> None:
        if seconds > 0:
            self._cancel.wait(seconds)

    def _run_prepare(self) -> Dict[str, Any]:
        prepared: Dict[str, Any] = {}
        for step in self._prepare:
            if self._cancel.is_set():
                raise _PrepareFailed(f'{step.label}: cancelled')
            try:
                result = step.fn(self._token)
            except Exception as exc:
                raise _PrepareFailed(f'{step.label}: {exc}') from exc
            prepared[step.label] = dict(result or {})
        return prepared

    # --------------------------------------------------------------- cleanup
    def _cleanup(self, starts, movement_failed):
        quarantined: List[str] = []
        failures: List[str] = []
        notes: List[str] = []
        wants_return = (
            self.settings.return_to_start
            and not movement_failed
            and self.acquisition in (AcquisitionOutcome.COMPLETE, AcquisitionOutcome.STOPPED)
        )
        for name, control in self.controls.items():
            resource = control.resource
            if self.executor.is_quarantined(resource):
                # Never dispatch to a backend whose earlier command still runs.
                if not self.executor.wait_released(resource, self.settings.cleanup_deadline_s):
                    quarantined.append(resource)
                    notes.append(f'{name}: backend {resource} still busy; not restored')
                    continue
            if not wants_return:
                continue
            start = starts.get(name)
            if start is None:
                notes.append(f'{name}: no start position (no fresh readback); left in place')
                continue
            result = self.executor.apply(control, start, self.settings.cleanup_deadline_s,
                                         self._token)
            if not result.ok:
                if self.executor.is_quarantined(resource):
                    quarantined.append(resource)
                failures.append(f'{name}: return to {start:g} failed: {result.cause}')
        if movement_failed and self.settings.return_to_start:
            notes.append('no return to start after a movement failure')
        for step in self._cleanup_steps:
            if movement_failed and not step.after_failure:
                notes.append(f'{step.label}: skipped after a movement failure')
                continue
            if self.executor.is_quarantined(step.resource):
                if not self.executor.wait_released(step.resource,
                                                   self.settings.cleanup_deadline_s):
                    quarantined.append(step.resource)
                    notes.append(f'{step.label}: {step.resource} still busy; not done')
                    continue
            # Bounded like every hardware command: a stuck restore must not
            # hang the run; its resource stays quarantined (and reserved).
            ok, cause = self.executor.run_step(
                step.resource, step.label, lambda step=step: step.fn(self._token),
                self.settings.cleanup_deadline_s)
            if not ok:
                if self.executor.is_quarantined(step.resource):
                    quarantined.append(step.resource)
                failures.append(f'{step.label} failed: {cause}')
        if quarantined:
            outcome = CleanupOutcome.QUARANTINED
        elif failures:
            outcome = CleanupOutcome.FAILED
        else:
            outcome = CleanupOutcome.DONE
        return outcome, '; '.join(failures + notes), sorted(set(quarantined))

    def _release(self, quarantined: List[str]) -> None:
        if self._reservation is None:
            self.lifecycle = RunLifecycle.FINISHED
            self._finished.set()
            return
        token = self._reservation.token
        held = set(quarantined)
        self.registry.release(token, [r for r in self._reservation.resources if r not in held])
        if held:
            # Quarantined resources stay reserved until their worker returns;
            # that is an explicit state, so the run is finished.
            def on_release(resource: str, is_quarantined: bool) -> None:
                if not is_quarantined and resource in held:
                    self.registry.release(token, [resource])
            self.executor.add_listener(on_release)
            for resource in list(held):
                if not self.executor.is_quarantined(resource):
                    self.registry.release(token, [resource])
        self.lifecycle = RunLifecycle.FINISHED
        self._finished.set()


def _new_run_id() -> str:
    stamp = _dt.datetime.now().strftime('%Y%m%d-%H%M%S')
    return f'run-{stamp}-{uuid.uuid4().hex[:6]}'


def _imswitch_version() -> str:
    try:
        from imswitch import __version__
        return str(__version__)
    except Exception:
        return 'unknown'
