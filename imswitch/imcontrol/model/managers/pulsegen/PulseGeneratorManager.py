"""Abstract base class for digital pulse-generator backends.

A pulse generator is anything that can drive a small number of digital
output channels with deterministic timing — Swabian PulseStreamer, a
Teensy/Arduino microcontroller, an NI digital-out card, etc.  Backends
expose explicit capability properties (``jitter_ns``,
``min_pulse_width_ns``, ``n_digital_channels``, ...) so consumers can
introspect what they're getting rather than assuming all backends are
equivalent — see [WSIntegration.md](../../../../../../WSIntegration.md)
for the rationale.

Subclasses live alongside this file under ``pulsegen/``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List

from imswitch.imcommon.framework import Signal, SignalInterface


@dataclass(frozen=True)
class PulseStep:
    """One row in a pulse sequence.

    ``channel_states`` is the *full* state of every channel during the
    step (not a delta against the previous step).  Channels not listed
    default to LOW.  This matches the wire bitmask used by the Teensy
    firmware: ``bitmask = OR of (1 << ch) for ch in channel_states where
    channel_states[ch] is True``.

    Full-state-per-step keeps the data unambiguous about idle channels
    and makes step-N executable without replaying steps 0..N-1.
    """
    duration_ns: int
    channel_states: Dict[int, bool] = field(default_factory=dict)


class PulseGeneratorError(Exception):
    """Backend or contract violation in a PulseGeneratorManager."""


class PulseGeneratorManager(SignalInterface, ABC):
    """Abstract base for digital pulse generators.

    Lifecycle
    ---------
    ``__init__`` connects to the backend.  On connection failure the
    backend may either raise or enter a degraded mock mode and set
    ``connected`` to False — this is backend-specific.

    ``finalize()`` should stop any running sequence and release the
    hardware.  Default no-op; override as needed.

    Concurrency
    -----------
    Methods are NOT required to be thread-safe by the contract.
    Consumers serialize.  A backend's internal worker thread (for
    non-blocking ``run``) is private — it must not call ``setDigital``
    etc. on itself.  Signals may be emitted from any thread.
    """

    sigSequenceStarted = Signal()
    sigSequenceDone = Signal()
    sigSequenceFailed = Signal(str)  # payload: error description

    # ------------------------------------------------------------------
    # Capability introspection — set at __init__, read by consumers.
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def jitter_ns(self) -> int:
        """Worst-case timing jitter on a step transition, in ns.

        Honest value, not best-case.  Used by consumers to decide whether
        the backend is suitable for time-critical work.
        """

    @property
    @abstractmethod
    def min_pulse_width_ns(self) -> int:
        """Shortest step duration the backend can resolve, in ns."""

    @property
    @abstractmethod
    def n_digital_channels(self) -> int:
        """Number of usable digital output channels.  Logical, 0..n-1."""

    @property
    @abstractmethod
    def supports_hw_trigger_in(self) -> bool:
        """True if ``run`` can wait for an external trigger before starting."""

    @property
    @abstractmethod
    def supports_analog(self) -> bool:
        """True if ``setAnalog`` works."""

    @property
    def connected(self) -> bool:
        """True if the backend is talking to real hardware.

        Default returns True.  Override in backends that may fall back
        to an in-process mock so consumers (and the UI) can surface the
        degraded state.
        """
        return True

    # ------------------------------------------------------------------
    # Instant pin control.
    # ------------------------------------------------------------------

    @abstractmethod
    def setDigital(self, channel: int, enable: bool) -> None:
        """Set a single channel to a constant HIGH or LOW.

        Must raise :class:`PulseGeneratorError` if a sequence is
        currently running — call :meth:`stop` first.
        """

    def setAnalog(self, channel: int, voltage: float) -> None:
        """Set an analog channel to a constant voltage.

        Default impl raises :class:`NotImplementedError`.  Backends that
        return True from :attr:`supports_analog` must override.
        """
        raise NotImplementedError(
            f'{type(self).__name__} does not support analog output'
        )

    # ------------------------------------------------------------------
    # Sequence programming and execution.
    # ------------------------------------------------------------------

    @abstractmethod
    def program_sequence(self, steps: List[PulseStep]) -> None:
        """Upload a sequence to the backend.  Does not start execution.

        Raises
        ------
        ValueError
            If ``steps`` is empty, any step's ``duration_ns`` is below
            :attr:`min_pulse_width_ns`, or any channel index is outside
            ``[0, n_digital_channels)``.
        PulseGeneratorError
            If a sequence is currently running.
        """

    @abstractmethod
    def run(self, n_reps: int = 1, blocking: bool = False) -> None:
        """Execute the most recently programmed sequence ``n_reps`` times.

        Parameters
        ----------
        n_reps : int
            Number of repetitions.  Must be >= 1.
        blocking : bool
            If True, return only after the sequence finishes (or raise
            on backend error).  If False, return immediately and emit
            :attr:`sigSequenceDone` from a worker thread when done.

        Backends whose underlying API is synchronous MUST spawn a worker
        thread to honour ``blocking=False`` — the caller must never
        block on a non-blocking run.

        Raises
        ------
        PulseGeneratorError
            If no sequence has been programmed, or one is already
            running.
        ValueError
            If ``n_reps`` < 1.
        """

    @abstractmethod
    def stop(self) -> None:
        """Abort any running sequence.

        Safe to call when nothing is running (no-op).  Blocks until the
        backend confirms the stop.  Emits :attr:`sigSequenceDone`.
        """

    # ------------------------------------------------------------------
    # Default helpers built on the abstract API.
    # ------------------------------------------------------------------

    def snap(self, channels: List[int], width_ns: int) -> None:
        """Pulse the given channels HIGH for ``width_ns``, then LOW.

        Default impl uses :meth:`program_sequence` + :meth:`run` with a
        2-step sequence (HIGH-hold, LOW-tail).  The trailing LOW step
        guarantees the channels physically drop before this call
        returns — without it the backend would leave the last asserted
        state on the wire indefinitely.

        Backends with a native one-shot path (e.g. Teensy ``Snap,...``)
        may override for lower latency.
        """
        if width_ns < self.min_pulse_width_ns:
            raise ValueError(
                f'width_ns={width_ns} below backend minimum '
                f'{self.min_pulse_width_ns}'
            )
        high_state = {ch: True for ch in channels}
        low_state = {ch: False for ch in channels}
        tail_ns = max(self.min_pulse_width_ns, 1000)
        self.program_sequence([
            PulseStep(duration_ns=width_ns, channel_states=high_state),
            PulseStep(duration_ns=tail_ns, channel_states=low_state),
        ])
        self.run(n_reps=1, blocking=True)

    # ------------------------------------------------------------------

    def finalize(self) -> None:
        """Release the backend.  Default no-op.

        Override to stop any running sequence and close the connection.
        """


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
