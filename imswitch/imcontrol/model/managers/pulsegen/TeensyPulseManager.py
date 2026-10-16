"""PulseGeneratorManager backend for a Teensy / Arduino running the
ImSwitch v4 pulse-generator firmware (with v3 fallback)."""

from __future__ import annotations

import dataclasses
import threading
from typing import List, Optional

from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.SetupInfo import TeensyPulseInfo  # re-export

from .PulseGeneratorManager import (
    PulseGeneratorError,
    PulseGeneratorManager,
    PulseStep,
)

# ``TeensyPulseInfo`` is defined in SetupInfo.py so it can sit alongside
# the other setup-file dataclasses.  Re-exported here for callers who
# only know about the manager.
__all__ = ['TeensyPulseManager', 'TeensyPulseInfo', 'teensy_info']


def teensy_info(name, props) -> TeensyPulseInfo:
    """A ``pulseGenerators`` entry's ``managerProperties`` as the
    ``teensyPulse`` section they mirror: the same keys and defaults, and a
    key it does not have refused by name."""
    known = [f.name for f in dataclasses.fields(TeensyPulseInfo)]
    unknown = sorted(set(props) - set(known))
    if unknown:
        raise ValueError(f'{name}: TeensyPulseManager does not take managerProperties '
                         f'{", ".join(unknown)}; it takes {", ".join(known)}.')
    # Each key read by name, so that the config editor lists them.
    default = TeensyPulseInfo()
    return TeensyPulseInfo(
        port=props.get('port', default.port),
        baud=props.get('baud', default.baud),
        pinMap=props.get('pinMap', default.pinMap),
        useMockOnFailure=props.get('useMockOnFailure', default.useMockOnFailure),
        mockNChannels=props.get('mockNChannels', default.mockNChannels),
        mockMinPulseUs=props.get('mockMinPulseUs', default.mockMinPulseUs),
        mockMaxSteps=props.get('mockMaxSteps', default.mockMaxSteps),
    )


class TeensyPulseManager(PulseGeneratorManager):
    """Drives a Teensy / Arduino pulse generator.

    On construction, tries to open the configured port via
    :class:`TeensyPulseDriver`.  On failure, falls back to
    :class:`MockTeensyPulseDriver` if ``info.useMockOnFailure`` is True.
    Either way :attr:`connected` reflects what actually happened so the
    UI can surface a degraded state.

    All capability properties (:attr:`min_pulse_width_ns`,
    :attr:`n_digital_channels`, etc.) reflect the *connected* backend's
    capabilities — so a v3 firmware connection will report
    ``n_digital_channels == 3``, a v4 firmware whatever the firmware
    declares via ``*IDN?``, and the mock whatever was configured.

    :meth:`snap` is overridden to use the driver's native one-shot path
    (lower latency than the ABC's default which programs a 2-step
    sequence).
    """

    # Jitter is a property of the firmware platform (Teensy 4.1 + USB
    # serial framing + ``delayMicroseconds`` scheduler) rather than of
    # the protocol version, so we report it constant across v3 and v4.
    _JITTER_NS = 1_000

    #: The v4 wire protocol sends step durations as whole microseconds
    #: (``<dur_us>,<bitmask_hex>``, docs/design/ARCHITECTURE.md).
    _TIMING_RESOLUTION_NS = 1_000

    #: v4 firmware keeps ``n_reps`` in a ``uint32_t`` (``seqNReps`` in
    #: WidefieldStarss teensy/arduino_code_teensy4p1_v4.txt); ``*IDN?`` does
    #: not report it.
    _V4_MAX_REPEATS = 2 ** 32 - 1

    def __init__(self, info, name=None, **lowLevelManagers):
        """``info``: the setup's ``teensyPulse`` section, or a
        ``pulseGenerators`` entry whose ``managerProperties`` are that
        section's keys."""
        super().__init__()
        self.__logger = initLogger(self, instanceName=name)
        if not isinstance(info, TeensyPulseInfo):
            info = teensy_info(name, getattr(info, 'managerProperties', None) or {})
        self._info = info

        self._driver = self._open_driver(info)

        # Non-blocking run() spawns a thread that calls driver.wait_done.
        # Track it so finalize() / stop() can join it.
        self._run_thread: Optional[threading.Thread] = None
        # State guard — driver.start() rejects double-starts at the wire
        # level, but the wait_done thread completes asynchronously so we
        # need our own flag for the manager's running-vs-idle contract.
        self._running = False
        self._running_lock = threading.RLock()

    def _open_driver(self, info: TeensyPulseInfo):
        # Lazy import so test environments without pyserial can still
        # import this module — only the mock path is exercised there.
        from imswitch.imcontrol.model.interfaces.teensypulse import (
            MockTeensyPulseDriver,
            TeensyPulseDriver,
        )

        if not info.port:
            if not info.useMockOnFailure:
                raise RuntimeError(
                    'TeensyPulseInfo.port is empty and useMockOnFailure is '
                    'False — refusing to construct manager'
                )
            self.__logger.warning(
                'No Teensy port configured; using MockTeensyPulseDriver'
            )
            return MockTeensyPulseDriver(
                n_channels=info.mockNChannels,
                min_pulse_us=info.mockMinPulseUs,
                max_steps=info.mockMaxSteps,
            )

        try:
            driver = TeensyPulseDriver(port=info.port, baud=info.baud)
            self.__logger.info(
                f'Connected to Teensy on {info.port} '
                f'({driver.capabilities.protocol_version}, '
                f'n_channels={driver.capabilities.n_channels})'
            )
            return driver
        except Exception as e:
            if not info.useMockOnFailure:
                raise
            self.__logger.warning(
                f'Failed to open Teensy on {info.port}: {e}; '
                f'falling back to MockTeensyPulseDriver'
            )
            # Distinguishes this fallback from a configured (empty-port) mock.
            self._mock_fallback = True
            return MockTeensyPulseDriver(
                n_channels=info.mockNChannels,
                min_pulse_us=info.mockMinPulseUs,
                max_steps=info.mockMaxSteps,
            )

    # ------------------------------------------------------------------
    # Capability properties — derived from the connected driver.
    # ------------------------------------------------------------------

    @property
    def jitter_ns(self) -> int:
        return self._JITTER_NS

    @property
    def min_pulse_width_ns(self) -> int:
        return self._driver.capabilities.min_pulse_us * 1_000

    @property
    def n_digital_channels(self) -> int:
        return self._driver.capabilities.n_channels

    @property
    def supports_hw_trigger_in(self) -> bool:
        # v4 spec reserves this for v5; v3 has no input trigger either.
        return False

    @property
    def supports_analog(self) -> bool:
        return False

    @property
    def max_sequence_steps(self) -> int:
        # v3 firmware plays no uploaded sequences (upload_sequence refuses).
        caps = self._driver.capabilities
        return caps.max_steps if caps.is_v4 else 0

    @property
    def timing_resolution_ns(self) -> int:
        return self._TIMING_RESOLUTION_NS

    @property
    def max_repeats(self) -> Optional[int]:
        return self._V4_MAX_REPEATS if self._driver.capabilities.is_v4 else None

    @property
    def stop_drives_low(self) -> bool:
        # The firmware's STOP handler calls allChannelsLow(); so does the mock.
        return True

    @property
    def stop_granularity(self) -> str:
        # STOP is checked between steps (and every 16 ms inside long ones).
        return 'step-boundary'

    @property
    def connected(self) -> bool:
        # Mock driver has the same is_open semantics as the real one,
        # so this single check covers both cases.  The semantic
        # "connected to real hardware" distinction is also surfacable
        # via the protocol_version of the driver's capabilities —
        # consumers that care about that should inspect the driver
        # directly via :attr:`driver`.
        return getattr(self._driver, 'is_open', True)

    @property
    def driver(self):
        """The underlying driver instance.

        Exposed for advanced consumers (and tests) that need to assert
        on the mock's timeline or query the protocol version directly.
        Most callers should go through the manager's API instead.
        """
        return self._driver

    # ------------------------------------------------------------------
    # Pin control.
    # ------------------------------------------------------------------

    def setDigital(self, channel: int, enable: bool) -> None:
        with self._running_lock:
            if self._running:
                raise PulseGeneratorError(
                    'cannot setDigital while a sequence is running'
                )
            self._driver.pin(int(channel), bool(enable))

    # ------------------------------------------------------------------
    # Sequence programming and execution.
    # ------------------------------------------------------------------

    def program_sequence(self, steps: List[PulseStep]) -> None:
        with self._running_lock:
            if self._running:
                raise PulseGeneratorError(
                    'cannot program a new sequence while one is running'
                )
        if not steps:
            raise ValueError('program_sequence requires at least one step')

        # ABC validation contract: surface ValueError for short
        # durations or out-of-range channels.  Same checks the driver
        # does, but doing them here too gives a cleaner error message
        # (the driver's would mention "us" while the ABC speaks "ns").
        min_ns = self.min_pulse_width_ns
        n_ch = self.n_digital_channels
        wire_steps: List = []
        for idx, step in enumerate(steps):
            if step.duration_ns < min_ns:
                raise ValueError(
                    f'step {idx} duration_ns={step.duration_ns} below '
                    f'backend minimum {min_ns}'
                )
            for ch in step.channel_states:
                if not (0 <= ch < n_ch):
                    raise ValueError(
                        f'step {idx} channel {ch} outside [0, {n_ch})'
                    )
            duration_us = max(1, step.duration_ns // 1_000)
            bitmask = 0
            for ch, level in step.channel_states.items():
                if level:
                    bitmask |= (1 << ch)
            wire_steps.append((duration_us, bitmask))

        # Driver handles max-steps validation against the firmware cap.
        # We pass n_reps=1 here; n_reps in run() is applied via the
        # driver's per-call semantics.  But the v4 protocol bakes n_reps
        # into SEQ — so we re-upload on each run() with the requested
        # rep count.  Stash the steps and apply on run().
        self._programmed_steps = wire_steps

    def run(self, n_reps: int = 1, blocking: bool = False, trigger: str = 'none') -> None:
        if trigger not in self.run_triggers:
            raise ValueError(f'The Teensy takes no {trigger!r} trigger; it runs on START only.')
        if not hasattr(self, '_programmed_steps') or self._programmed_steps is None:
            raise PulseGeneratorError(
                'no sequence programmed; call program_sequence first'
            )
        with self._running_lock:
            if self._running:
                raise PulseGeneratorError('a sequence is already running')
            if n_reps < 1:
                raise ValueError(f'n_reps={n_reps} must be >= 1')
            self._running = True

        # Upload + start synchronously.  upload_sequence happens here
        # (not in program_sequence) because v4 SEQ bakes n_reps into the
        # header — re-uploading per-run lets callers vary reps without
        # rebuilding the sequence.
        try:
            self._driver.upload_sequence(self._programmed_steps, n_reps=n_reps)
            self._driver.start()
        except Exception:
            with self._running_lock:
                self._running = False
            raise

        self.sigSequenceStarted.emit()

        def _wait_and_signal():
            try:
                # Conservative completion timeout: sum of step durations
                # × n_reps + 1 s headroom.  Sequences longer than this
                # are programming bugs; the driver itself has a
                # per-call timeout too.
                total_us = sum(s[0] for s in self._programmed_steps) * n_reps
                total_s = total_us * 1e-6 + 1.0
                done = self._driver.wait_done(timeout=total_s)
            except Exception as e:
                self.__logger.exception('Teensy sequence failed mid-run')
                with self._running_lock:
                    self._running = False
                self.sigSequenceFailed.emit(str(e))
                return
            with self._running_lock:
                self._running = False
            # sigSequenceDone fires on both DONE (done=True) and STOPPED
            # (done=False) — the ABC contract doesn't distinguish.
            self.sigSequenceDone.emit()

        if blocking:
            _wait_and_signal()
        else:
            self._run_thread = threading.Thread(
                target=_wait_and_signal, daemon=True,
            )
            self._run_thread.start()

    def stop(self) -> None:
        with self._running_lock:
            if not self._running:
                # ABC says stop() is a no-op when idle.
                return
        # Send STOP to the device; the running-flag will be cleared by
        # the wait-and-signal thread once it observes STOPPED.
        try:
            self._driver.stop()
        except Exception as e:
            self.__logger.warning(f'driver.stop() raised: {e}')
        if self._run_thread is not None and self._run_thread.is_alive():
            self._run_thread.join(timeout=2.0)
            if self._run_thread.is_alive():
                raise TimeoutError(
                    'Teensy pulse worker did not stop within 2 s; the sequence '
                    'may still be running'
                )

    # ------------------------------------------------------------------
    # Snap override — go straight to the driver's native one-shot.
    # ------------------------------------------------------------------

    def snap(self, channels: List[int], width_ns: int) -> None:
        if width_ns < self.min_pulse_width_ns:
            raise ValueError(
                f'width_ns={width_ns} below backend minimum '
                f'{self.min_pulse_width_ns}'
            )
        width_us = max(1, width_ns // 1_000)
        with self._running_lock:
            if self._running:
                raise PulseGeneratorError(
                    'cannot snap while a sequence is running'
                )
            self._running = True
        try:
            self._driver.snap(channels, width_us)
        finally:
            with self._running_lock:
                self._running = False

    # ------------------------------------------------------------------

    def finalize(self) -> None:
        try:
            self.stop()
        finally:
            try:
                self._driver.close()
            except Exception:
                pass


# Copyright (C) 2026 ImSwitch developers
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
