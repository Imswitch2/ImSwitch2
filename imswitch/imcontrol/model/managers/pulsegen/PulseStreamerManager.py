"""PulseGeneratorManager backend for the Swabian Instruments Pulse Streamer 8/2."""

from __future__ import annotations

import io
import sys
import threading
from typing import List, Optional

from imswitch.imcommon.model import initLogger

from .PulseGeneratorManager import (
    PulseGeneratorError,
    PulseGeneratorManager,
    PulseStep,
)

# Lazy-import the vendor library.  Keeping it optional means setups
# without a PulseStreamer (or without the library installed) can still
# import this module — the manager just refuses to connect.
try:
    from pulsestreamer import PulseStreamer, OutputState, Sequence
except ImportError:
    PulseStreamer = None
    OutputState = None
    Sequence = None


DIG_CH_MAX_NUMBER = 8
ANG_CH_MAX_NUMBER = 2

# Pulse Streamer 8/2 spec: 8 ns minimum pulse width, 1 ns resolution
# on transitions.  Jitter is sub-ns on the hardware, but we report 1 ns
# as an honest worst-case rounding floor.
_PS_JITTER_NS = 1
_PS_MIN_PULSE_NS = 8


class PulseStreamerManager(PulseGeneratorManager):
    """Drives a Swabian Pulse Streamer 8/2.

    Setup file ``pulseStreamer`` block:

    - ``ipAddress`` — IP address of the streamer on the network.
    """

    def __init__(self, setupInfo):
        super().__init__()

        # Redirect stdout while constructing the vendor object; its
        # constructor prints connection info we want to capture in the
        # logger instead of the terminal.
        self.__stdOut = io.StringIO()
        sys.stdout = self.__stdOut

        self.__logger = initLogger(self)
        self.__ipAddress = setupInfo.pulseStreamer.ipAddress

        self.__digitalChannels: List[int] = []
        self.__analogChannels: List[float] = [0.0, 0.0]

        self.__pulseStreamer = None
        self.__sequence = None  # last programmed pulsestreamer.Sequence
        self.__run_thread: Optional[threading.Thread] = None
        self.__running = False
        self.__stop_requested = False

        if PulseStreamer is None:
            self.__logger.error(
                'pulsestreamer library not installed — PulseStreamerManager '
                'will refuse all operations.  Install with `pip install '
                'pulsestreamer`.'
            )
            sys.stdout = sys.__stdout__
            return

        try:
            self.__pulseStreamer = PulseStreamer(self.__ipAddress)
            captured = self.__stdOut.getvalue().splitlines()
            for line in captured:
                if line.strip():
                    self.__logger.info(line)
        except Exception as e:
            self.__logger.error(
                f'Failed to connect to PulseStreamer at {self.__ipAddress}: {e}'
            )
            self.__pulseStreamer = None
        finally:
            sys.stdout = sys.__stdout__

    # ------------------------------------------------------------------
    # Capability properties — see PulseGeneratorManager docstrings.
    # ------------------------------------------------------------------

    @property
    def jitter_ns(self) -> int:
        return _PS_JITTER_NS

    @property
    def min_pulse_width_ns(self) -> int:
        return _PS_MIN_PULSE_NS

    @property
    def n_digital_channels(self) -> int:
        return DIG_CH_MAX_NUMBER

    @property
    def supports_hw_trigger_in(self) -> bool:
        return True

    @property
    def supports_analog(self) -> bool:
        return True

    @property
    def connected(self) -> bool:
        return self.__pulseStreamer is not None

    # ------------------------------------------------------------------
    # Instant pin control — preserves the original API verbatim.
    # ------------------------------------------------------------------

    def setDigital(self, channel, enable):
        """Set a digital channel's constant output level.

        ``channel`` may be a single int or an iterable accepted by
        ``OutputState`` — kept polymorphic for backward compatibility
        with the original API.  ``enable`` is truthy for HIGH, falsy for
        LOW.

        Note: this REPLACES the asserted-channel list with
        ``[channel]`` when enabling and with ``[]`` when disabling —
        matching the original behavior.  Callers that need bitwise
        composition should use :meth:`program_sequence` instead.
        """
        if self.__pulseStreamer is None:
            self.__logger.warning('PulseStreamer not connected, setDigital ignored')
            return
        if self.__running:
            raise PulseGeneratorError(
                'Cannot setDigital while a sequence is running; call stop() first'
            )
        if channel is None:
            raise PulseStreamerManagerError(
                'Target has no digital channel assigned to it'
            )
        if not self._areChannelsOk(channel, DIG_CH_MAX_NUMBER):
            raise PulseStreamerManagerError(
                f'Target digital channels are out of bounds '
                f'(min. is 0, max. is {DIG_CH_MAX_NUMBER - 1})'
            )

        if bool(enable):
            self.__digitalChannels = [channel]
        else:
            self.__digitalChannels = []

        self.__pulseStreamer.constant(OutputState(
            self.__digitalChannels,
            self.__analogChannels[0],
            self.__analogChannels[1],
        ))

    def setAnalog(self, channel, voltage, min_val=0.0, max_val=1.0):
        """Set an analog channel's constant output voltage."""
        if self.__pulseStreamer is None:
            self.__logger.warning('PulseStreamer not connected, setAnalog ignored')
            return
        if self.__running:
            raise PulseGeneratorError(
                'Cannot setAnalog while a sequence is running; call stop() first'
            )
        if channel is None:
            raise PulseStreamerManagerError(
                'Target has no analog channel assigned to it'
            )
        if not self._areChannelsOk(channel, ANG_CH_MAX_NUMBER):
            raise PulseStreamerManagerError(
                f'Target analog channels are out of bounds '
                f'(min. is 0, max. is {ANG_CH_MAX_NUMBER - 1})'
            )

        if voltage > 0.0:
            voltage = min(voltage, max_val)
        else:
            voltage = max(voltage, min_val)

        if channel == 0:
            self.__analogChannels[0] = voltage
        else:
            self.__analogChannels[1] = voltage
        self.__pulseStreamer.constant(OutputState(
            self.__digitalChannels,
            self.__analogChannels[0],
            self.__analogChannels[1],
        ))

    def _areChannelsOk(self, channel, upper_bound) -> bool:
        """Validate that ``channel`` (int or iterable of ints) is < ``upper_bound``."""
        if hasattr(channel, '__iter__'):
            return all(int(c) < upper_bound for c in channel)
        return int(channel) < upper_bound

    # ------------------------------------------------------------------
    # Sequence programming and execution.
    # ------------------------------------------------------------------

    def program_sequence(self, steps: List[PulseStep]) -> None:
        if self.__pulseStreamer is None:
            raise PulseGeneratorError('PulseStreamer not connected')
        if self.__running:
            raise PulseGeneratorError(
                'Cannot program a new sequence while one is running'
            )
        if not steps:
            raise ValueError('program_sequence requires at least one step')

        # Validate per-step constraints before touching hardware.
        for idx, step in enumerate(steps):
            if step.duration_ns < self.min_pulse_width_ns:
                raise ValueError(
                    f'step {idx} duration_ns={step.duration_ns} below backend '
                    f'minimum {self.min_pulse_width_ns}'
                )
            for ch in step.channel_states:
                if not (0 <= ch < self.n_digital_channels):
                    raise ValueError(
                        f'step {idx} channel {ch} out of range '
                        f'[0, {self.n_digital_channels})'
                    )

        # Per-channel timeline: list of (duration_ns, level) for each ch.
        seq = Sequence()
        for ch in range(self.n_digital_channels):
            pattern = [
                (step.duration_ns, 1 if step.channel_states.get(ch, False) else 0)
                for step in steps
            ]
            seq.setDigital(ch, pattern)

        self.__sequence = seq

    def run(self, n_reps: int = 1, blocking: bool = False) -> None:
        if self.__pulseStreamer is None:
            raise PulseGeneratorError('PulseStreamer not connected')
        if self.__sequence is None:
            raise PulseGeneratorError('No sequence programmed; call program_sequence first')
        if self.__running:
            raise PulseGeneratorError('A sequence is already running')
        if n_reps < 1:
            raise ValueError(f'n_reps={n_reps} must be >= 1')

        self.__running = True
        self.__stop_requested = False
        self.sigSequenceStarted.emit()

        def _execute():
            try:
                self.__pulseStreamer.stream(self.__sequence, n_runs=n_reps)
                # PulseStreamer.stream is async on the device; poll until
                # finished or stop requested.
                while not self.__stop_requested:
                    if not self.__pulseStreamer.isStreaming():
                        break
                    # Tight-ish poll; pulsestreamer doesn't expose a blocking wait.
                    threading.Event().wait(0.01)
                self.sigSequenceDone.emit()
            except Exception as e:
                self.__logger.exception('PulseStreamer sequence failed')
                self.sigSequenceFailed.emit(str(e))
            finally:
                self.__running = False

        if blocking:
            _execute()
        else:
            self.__run_thread = threading.Thread(target=_execute, daemon=True)
            self.__run_thread.start()

    def stop(self) -> None:
        if self.__pulseStreamer is None:
            return
        if not self.__running:
            return
        self.__stop_requested = True
        try:
            # forceFinal() drops the streamer back to its idle output state.
            # The poll loop in run()'s worker thread will then exit.
            self.__pulseStreamer.forceFinal()
        except Exception as e:
            self.__logger.warning(f'PulseStreamer stop() raised: {e}')
        # Wait for the worker thread to acknowledge.
        if self.__run_thread is not None and self.__run_thread.is_alive():
            self.__run_thread.join(timeout=1.0)

    def finalize(self) -> None:
        self.stop()


class PulseStreamerManagerError(Exception):
    """ Exception raised when error occurs in PulseStreamerManager """

    def __init__(self, message):
        super().__init__(message)
        self.message = message


# Copyright (C) 2021-2026 ImSwitch developers
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
