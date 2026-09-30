"""Settable memory limits for buffering and automatic work.

Three limits, each named for the one thing it bounds and each defaulting to
the literal the code already carried:

- ``writerQueueMB`` -- how much backlog the recording writer may hold before
  the acquisition loop blocks (``WRITER_QUEUE_MAX_BYTES``);
- ``perDetectorQueueMB`` -- how much backlog any one ``(detector, consumer)``
  chunk queue may hold before that consumer's stream is declared incomplete
  (``MAX_QUEUED_CONSUMER_BYTES``);
- ``processingWorkingSetMB`` -- the working set ImProcess may spend on
  *automatic* work: contrast sampling and the mean preview.

They live on ``Options.memory`` in ``imcontrol_options.json`` -- a property
of the computer, not of the microscope, so never in a setup file -- and are
adopted once at startup by :func:`configure`. Until then, and for any value
that cannot be honoured, each consumer keeps its own literal: it asks
:func:`effectiveBytes` with that literal as the default, so a test that
patches the constant still tests the constant.

None of these is a process limit, and this module claims none. Camera
drivers allocate their own ring buffers (the Hamamatsu interface about
4 GiB), datasets are as large as the data, and chunk consumers register as
they please. What the limits bound is what ImSwitch *chooses* to hold in its
queues and spend on work nobody asked for; and every message that quotes
one names the setting that moves it. Design: ``docs/design/plans/memory-budgets.md``.
"""
from __future__ import annotations

import logging
import math
import threading
from typing import Any, Dict, Optional

MIB = 1024 * 1024

#: The settings file the limits live in, for messages.
OPTIONS_FILE = 'imcontrol_options.json'

#: For each limit (in bytes, as consumers use it), the option field in MiB.
SETTING_FIELDS: Dict[str, str] = {
    'writerQueueBytes': 'writerQueueMB',
    'perDetectorQueueBytes': 'perDetectorQueueMB',
    'processingWorkingSetBytes': 'processingWorkingSetMB',
}

_lock = threading.Lock()
_configured: Dict[str, int] = {}


def settingRef(limit: str) -> str:
    """How a message names the setting that moves ``limit``."""
    return f'memory.{SETTING_FIELDS[limit]} in {OPTIONS_FILE}'


def configuredBytes(limit: str) -> Optional[int]:
    """The configured value in bytes, or ``None`` when the literal applies."""
    if limit not in SETTING_FIELDS:
        raise KeyError(f'unknown memory limit {limit!r}')
    with _lock:
        return _configured.get(limit)


def effectiveBytes(limit: str, default: int) -> int:
    """The configured value, or the caller's own literal."""
    value = configuredBytes(limit)
    return int(default) if value is None else value


def configure(memoryOptions: Any, *, logger: Optional[logging.Logger] = None) -> Dict[str, int]:
    """Adopt ``Options.memory``; returns what was adopted, in bytes.

    A value that is not a positive whole number of MiB is not honoured: it is
    reported by name and the consumer's literal stands, because a typo in a
    preferences file must not take a rig down. ``None`` (no ``memory`` group
    at all) clears any earlier configuration.
    """
    log = logger or logging.getLogger(__name__)
    adopted: Dict[str, int] = {}
    for limit, field in SETTING_FIELDS.items():
        raw = getattr(memoryOptions, field, None) if memoryOptions is not None else None
        if raw is None:
            continue
        mib = _wholeMib(raw)
        if mib is None:
            log.warning(
                f'Ignoring {settingRef(limit)}: {raw!r} is not a positive whole '
                f'number of MiB; the built-in default stands.'
            )
            continue
        adopted[limit] = mib * MIB
    with _lock:
        _configured.clear()
        _configured.update(adopted)
    if adopted:
        log.info(
            'Memory limits: '
            + ', '.join(f'{SETTING_FIELDS[k]} = {v // MIB} MiB' for k, v in adopted.items())
            + f' ({OPTIONS_FILE})'
        )
    return dict(adopted)


def reset() -> None:
    """Forget any configuration; every consumer is back on its literal."""
    with _lock:
        _configured.clear()


def wholeMib(raw: Any) -> Optional[int]:
    """``raw`` as a positive, finite whole number of MiB, or None."""
    return _wholeMib(raw)


def _wholeMib(raw: Any) -> Optional[int]:
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    # "NaN", "Infinity" and "1e309" all parse as floats and then make int()
    # raise -- at hardware-control startup. Not finite is not a size.
    if not math.isfinite(value) or value <= 0 or value != int(value):
        return None
    return int(value)


def describeBytes(nbytes: int) -> str:
    """A short human figure for a byte count: ``260 MiB``, ``8.0 MiB``, ``12 kB``."""
    nbytes = int(nbytes)
    if nbytes >= 100 * MIB:
        return f'{nbytes / MIB:.0f} MiB'
    if nbytes >= MIB:
        return f'{nbytes / MIB:.1f} MiB'
    if nbytes >= 1024:
        return f'{nbytes / 1024:.0f} kB'
    return f'{nbytes} B'


def frameBudgetNote(frameBytes: int, budgetBytes: int) -> str:
    """What one queue budget means for frames of ``frameBytes``.

    Either how many fit, or -- for a frame that exceeds the budget on its own,
    a scan-driven detector's volume typically -- that it is admitted alone and
    nothing fits behind it until it is read.
    """
    frameBytes = int(frameBytes)
    budgetBytes = int(budgetBytes)
    if frameBytes <= 0:
        return 'frame size unknown'
    if frameBytes > budgetBytes:
        return (
            f'exceeds the {describeBytes(budgetBytes)} queue budget on its own: '
            f'admitted alone, no further backlog fits behind it until it is read'
        )
    return f'about {budgetBytes // frameBytes} fit in the {describeBytes(budgetBytes)} queue budget'


__all__ = [
    'MIB', 'OPTIONS_FILE', 'SETTING_FIELDS',
    'configure', 'configuredBytes', 'describeBytes', 'effectiveBytes',
    'frameBudgetNote', 'reset', 'settingRef', 'wholeMib',
]
