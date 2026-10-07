"""Run controls: devices a measurement run sets at every point.

A returned setter is no proof that an output changed. Run controls therefore
report an observable :class:`ControlResult` (requested / acknowledged /
measured, ``ok`` and ``cause``) and never go through the UI setters, which may
swallow errors (``docs/design/plans/transient-instruments-step-scans.md`` §7.4).

:class:`ControlExecutor` runs each command on a worker thread with a
deadline. A deadline ends the *wait*, not the hardware operation. A backend
whose worker has not physically returned stays **quarantined**: no further
command (cleanup included) is dispatched to it, and it is not released,
until the worker returns (§7.3).
"""
from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from imswitch.imcommon.model.measurement_run import ControlResult

_logger = logging.getLogger(__name__)

READBACK_FRESH = 'fresh'
READBACK_CACHED = 'cached'
READBACK_NONE = 'none'


@dataclass(frozen=True)
class ControlCapabilities:
    unit: str
    can_stop: bool
    #: ``fresh`` (queries the hardware), ``cached`` or ``none``.
    readback: str
    #: Whether the device confirms the value it applied.
    acknowledges: bool
    settle_s: float = 0.0
    #: Position tolerance for settling; only meaningful with fresh readback.
    tolerance: Optional[float] = None


class RunControl(ABC):
    """One audited control. Every method is called by the executor only."""

    #: Unique name within a run; becomes a dataset name.
    name: str
    #: The physical resource this control commands (e.g. a controller).
    resource: str
    capabilities: ControlCapabilities

    @abstractmethod
    def apply(self, value: float, token: Optional[str] = None) -> ControlResult:
        """Set the control; blocks until the device reports completion.

        ``token`` is the run's reservation token, passed to the manager
        (``owner=token``) so the resource registry admits the command. Must
        return ``ok=False`` with a cause, or raise, on any failure — never a
        silent success.
        """

    def stop(self) -> None:
        """Ask the device to stop the current operation (if ``can_stop``)."""

    def read_position(self) -> Optional[float]:
        """Fresh readback, or ``None`` if the control cannot read back."""
        return None


class ControlExecutor:
    """Runs control commands with deadlines and keeps stuck backends quarantined."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy: Dict[str, threading.Thread] = {}
        self._released: Dict[str, threading.Event] = {}
        self._listeners: List[Callable[[str, bool], None]] = []

    def add_listener(self, callback: Callable[[str, bool], None]) -> None:
        """``callback(resource, quarantined)`` when quarantine starts / ends."""
        self._listeners.append(callback)

    def is_quarantined(self, resource: str) -> bool:
        with self._lock:
            return resource in self._busy

    def quarantined(self) -> List[str]:
        with self._lock:
            return sorted(self._busy)

    def wait_released(self, resource: str, timeout_s: float) -> bool:
        with self._lock:
            event = self._released.get(resource)
        if event is None:
            return True
        return event.wait(timeout_s)

    def apply(self, control: RunControl, value: float, deadline_s: float,
              token: Optional[str] = None) -> ControlResult:
        return self._run(control, f'apply {value!r}', lambda: control.apply(value, token),
                         deadline_s, requested=value)

    def read_position(self, control: RunControl, deadline_s: float) -> Optional[float]:
        if control.capabilities.readback == READBACK_NONE:
            return None
        result = self._run(control, 'read position',
                           lambda: _position_result(control), deadline_s,
                           requested=float('nan'))
        return result.measured if result.ok else None

    def _run(self, control: RunControl, what: str, call, deadline_s: float,
             *, requested: float) -> ControlResult:
        resource = control.resource
        with self._lock:
            if resource in self._busy:
                return ControlResult(
                    control.name, requested=requested, ok=False,
                    cause=f'backend {resource} is busy: an earlier operation is '
                          f'still running (quarantined)')
        box: Dict[str, object] = {}

        def work():
            try:
                box['result'] = call()
            except Exception as exc:  # reported, never swallowed
                box['error'] = exc

        worker = threading.Thread(target=work, name=f'run-control-{control.name}', daemon=True)
        worker.start()
        worker.join(max(0.0, float(deadline_s)))
        if worker.is_alive():
            if control.capabilities.can_stop:
                try:
                    control.stop()
                except Exception:
                    _logger.exception('stop of %s failed', control.name)
            # Stop issued or not: the backend is held until the worker returns.
            self._quarantine(resource, worker)
            return ControlResult(
                control.name, requested=requested, ok=False,
                cause=f'{what} did not finish within {deadline_s:g} s'
                      + (' (stop issued)' if control.capabilities.can_stop else ''))
        if 'error' in box:
            exc = box['error']
            return ControlResult(control.name, requested=requested, ok=False,
                                 cause=f'{type(exc).__name__}: {exc}')
        result = box.get('result')
        if not isinstance(result, ControlResult):
            return ControlResult(control.name, requested=requested, ok=False,
                                 cause=f'{what} returned no ControlResult')
        return result

    def _quarantine(self, resource: str, worker: threading.Thread) -> None:
        event = threading.Event()
        with self._lock:
            self._busy[resource] = worker
            self._released[resource] = event
        _logger.warning('Backend %s quarantined: operation still running', resource)
        self._notify(resource, True)

        def watch():
            worker.join()
            with self._lock:
                self._busy.pop(resource, None)
                self._released.pop(resource, None)
            event.set()
            _logger.info('Backend %s released: operation returned', resource)
            self._notify(resource, False)

        threading.Thread(target=watch, name=f'quarantine-{resource}', daemon=True).start()

    def live_workers(self) -> List[threading.Thread]:
        with self._lock:
            return list(self._busy.values())

    def _notify(self, resource: str, quarantined: bool) -> None:
        for callback in list(self._listeners):
            try:
                callback(resource, quarantined)
            except Exception:
                _logger.exception('quarantine listener failed')


def _position_result(control: RunControl) -> ControlResult:
    position = control.read_position()
    return ControlResult(control.name, requested=float('nan'), measured=position,
                         ok=position is not None,
                         cause='' if position is not None else 'no readback')


_executor = ControlExecutor()


def get_control_executor() -> ControlExecutor:
    """The process-wide executor: one quarantine view for runs and scripts."""
    return _executor
