"""Driver for the ImSwitch Teensy pulse generator (firmware v3 + v4).

Two classes, one interface:

* :class:`TeensyPulseDriver` — talks the ASCII line protocol over pyserial.
* :class:`MockTeensyPulseDriver` — in-process simulator, same public API,
  exposes a virtual-time ``timeline`` of pin transitions so tests can
  assert sequence behaviour without hardware or real sleeps.

Both populate a :class:`TeensyCapabilities` tuple at construction so
consumers (notably :class:`PulseGeneratorManager` subclasses) can query
``min_pulse_us`` / ``n_channels`` / ``max_steps`` without knowing which
firmware version is on the wire.

See the v4 wire protocol section of ``WSIntegration.md`` for the
authoritative spec.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

# pyserial is optional — the mock driver doesn't need it.  Import lazily
# so headless test environments without pyserial can still use the mock.
try:
    import serial  # type: ignore
except ImportError:  # pragma: no cover - test envs may not have pyserial
    serial = None  # type: ignore


# --- Hardcoded v3 capability constants ------------------------------------
# The v3 firmware doesn't answer ``*IDN?``, so we baseline these from the
# code in arduino_code_teensy4p1_v3.txt: 3 channels, ~1 µs floor (the
# ``delayMicroseconds`` resolution), 16 steps per sequence.
_V3_N_CHANNELS = 3
_V3_MIN_PULSE_US = 1
_V3_MAX_STEPS = 16

# Default v4 capabilities used when IDN parsing fails partway.
_V4_FALLBACK_N_CHANNELS = 16
_V4_FALLBACK_MIN_PULSE_US = 1
_V4_FALLBACK_MAX_STEPS = 256

_IDN_REGEX = re.compile(
    r'^IMSWITCH_TEENSY,(?P<ver>[^,]+),(?P<nch>\d+),(?P<min_us>\d+),(?P<max_steps>\d+)$'
)


class TeensyProtocolError(RuntimeError):
    """Raised when the device returns an ``ERR,*`` line or times out."""


@dataclass
class TeensyCapabilities:
    """Per-connection capability snapshot.

    Populated at construction (real driver: via ``*IDN?``; mock: set
    directly).  Consumed by the manager layer to fill the
    :class:`PulseGeneratorManager` capability properties.
    """
    protocol_version: str          # "v3" or e.g. "v4.0"
    n_channels: int
    min_pulse_us: int
    max_steps: int

    @property
    def is_v4(self) -> bool:
        return self.protocol_version.startswith('v4')


@dataclass
class TimelineEvent:
    """A single pin transition recorded by the mock driver.

    ``virtual_time_ns`` is monotonically increasing across the whole
    session (not reset per sequence), in nanoseconds.  Tests assert
    against this rather than wall-clock time so they're deterministic
    and fast.
    """
    virtual_time_ns: int
    channel: int
    level: bool
    source: str = 'unknown'        # 'pin', 'snap', 'seq', 'stop'


# =========================================================================
# Real driver — speaks the protocol over pyserial.
# =========================================================================


class TeensyPulseDriver:
    """Real pyserial-backed driver for v3 and v4 Teensy firmware.

    Selects protocol mode at construction via ``*IDN?``:

    * v4 firmware answers ``IMSWITCH_TEENSY,<ver>,<nch>,<min_us>,<max_steps>``
      → ``capabilities`` reflects what the firmware reports.
    * Anything else (silence, junk, v3 firmware that silently drops the
      unknown command) → assumed v3, capabilities baked in.

    v3 mode supports :meth:`pin` and :meth:`snap` (which use the legacy
    ``PinHigh,``/``PinLow,``/``Snap,`` commands).
    :meth:`upload_sequence` / :meth:`start` raise on v3 — sequence
    semantics differ enough between v3 ``Parameters,...`` and v4 ``SEQ``
    that we expose v4-only sequences here.  Use a v4 firmware for
    sequences, or call the manager layer's snap-loop fallback.
    """

    # Time budget for *IDN? — anything slower is treated as v3 silence.
    _IDN_TIMEOUT_S = 0.2
    # Time budget for short ack commands (PIN, STOP, PinHigh/Low).
    _SHORT_TIMEOUT_S = 0.2
    # Budget for SEQ upload completion (READY,n).
    _SEQ_UPLOAD_TIMEOUT_S = 1.0
    # Budget for STARTED ack on START — must be quick.
    _START_ACK_TIMEOUT_S = 0.2

    def __init__(self, port: str, baud: int = 115200):
        if serial is None:
            raise ImportError(
                'pyserial is required for TeensyPulseDriver; '
                'use MockTeensyPulseDriver in tests'
            )
        # Tight per-read timeout — we frame reads explicitly via
        # _read_line_until and aggregate timeouts at the call site.
        self._ser = serial.Serial(
            port=port, baudrate=baud,
            timeout=0.05,
            write_timeout=1.0,
        )
        self._lock = threading.RLock()
        # Persistent rx scratch — bytes past the first '\n' in a chunk
        # would otherwise be discarded if we re-entered _read_line_until.
        # Carrying them across calls is how we don't lose the second
        # line of a "STARTED\nDONE\n" reply.
        self._rx_buf = bytearray()

        # Drain any boot banner left over from a reset.
        self._drain(quiet_window_s=0.05, max_wait_s=0.5)

        self.capabilities = self._handshake()

    # -- public API -------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    def idn(self) -> str:
        """Raw IDN string.  Empty on v3."""
        with self._lock:
            self._ser.reset_input_buffer()
            self._rx_buf.clear()
            self._ser.write(b'*IDN?\n')
            line = self._read_line_until(self._IDN_TIMEOUT_S)
            return line or ''

    def pin(self, n: int, level: bool) -> None:
        """Set channel ``n`` to a constant HIGH or LOW level."""
        self._require_channel(n)
        if self.capabilities.is_v4:
            cmd = f'PIN,{int(n)},{1 if level else 0}\n'
            ok = self._send_recv(cmd, terminal=('OK', 'ERR'),
                                 timeout=self._SHORT_TIMEOUT_S)
        else:
            cmd = f'{"PinHigh" if level else "PinLow"},{int(n)}\n'
            # v3 replies DONE; v4 may also accept the legacy commands and
            # reply OK.  Accept both.
            ok = self._send_recv(cmd, terminal=('OK', 'DONE', 'ERR'),
                                 timeout=self._SHORT_TIMEOUT_S)
        _raise_on_err(ok)

    def snap(self, channels: Sequence[int], width_us: int) -> None:
        """Pulse all ``channels`` HIGH for ``width_us``, then LOW.

        On v3 we use the legacy ``Snap,laser,camera,width`` command, which
        maps to channels[0]+channels[1].  Extra channels raise — v3
        firmware doesn't support more than two in a single Snap.

        On v4 we'd implement this via SEQ for arbitrary channel counts
        — but that path is in the manager, not the driver, so v4 callers
        usually go through ``upload_sequence`` + ``start`` instead.  The
        v4 path here exists for symmetry / parity tests.
        """
        for ch in channels:
            self._require_channel(ch)
        if width_us < self.capabilities.min_pulse_us:
            raise ValueError(
                f'width_us={width_us} below firmware min '
                f'{self.capabilities.min_pulse_us}'
            )

        if self.capabilities.is_v4:
            # 2-step SEQ: all-HIGH then all-LOW.
            bitmask = 0
            for ch in channels:
                bitmask |= (1 << ch)
            self.upload_sequence([(width_us, bitmask), (width_us, 0)], n_reps=1)
            self.start()
            self.wait_done(timeout=width_us * 2e-6 + 1.0)
        else:
            if len(channels) != 2:
                raise ValueError(
                    f'v3 firmware Snap requires exactly 2 channels '
                    f'(laser_pin, camera_pin); got {len(channels)}'
                )
            cmd = f'Snap,{int(channels[0])},{int(channels[1])},{int(width_us)}\n'
            # v3 Snap takes the full pulse duration to return DONE.
            self._send_recv(cmd, terminal=('DONE', 'ERR'),
                            timeout=width_us * 1e-6 + 1.0)

    def upload_sequence(
        self,
        steps: Sequence[Tuple[int, int]],
        n_reps: int = 1,
    ) -> None:
        """Upload an arbitrary sequence (v4 only).

        ``steps`` is a list of ``(duration_us, bitmask)`` tuples.
        Bitmask is the *full* channel state for the step.
        """
        if not self.capabilities.is_v4:
            raise TeensyProtocolError(
                'upload_sequence requires v4 firmware; '
                'use snap() or pin() on v3'
            )
        if n_reps < 1:
            raise ValueError(f'n_reps={n_reps} must be >= 1')
        if not steps:
            raise ValueError('empty sequence')
        if len(steps) > self.capabilities.max_steps:
            raise ValueError(
                f'sequence has {len(steps)} steps; '
                f'firmware max is {self.capabilities.max_steps}'
            )
        for idx, (dur, mask) in enumerate(steps):
            if dur < self.capabilities.min_pulse_us:
                raise ValueError(
                    f'step {idx} duration_us={dur} below min '
                    f'{self.capabilities.min_pulse_us}'
                )
            if not (0 <= mask < (1 << self.capabilities.n_channels)):
                raise ValueError(
                    f'step {idx} bitmask 0x{mask:X} outside firmware range '
                    f'(n_channels={self.capabilities.n_channels})'
                )

        # Build the upload payload in one buffer to minimise serial round
        # trips — the firmware reads line-by-line, but Python doesn't
        # need to flush between them.
        lines = [f'SEQ,{len(steps)},{int(n_reps)}\n']
        for dur, mask in steps:
            lines.append(f'{int(dur)},0x{int(mask):X}\n')
        payload = ''.join(lines).encode('ascii')

        with self._lock:
            self._ser.reset_input_buffer()
            self._ser.write(payload)
            ack = self._read_line_until_match(
                lambda s: s.startswith('READY,') or s.startswith('ERR,'),
                timeout=self._SEQ_UPLOAD_TIMEOUT_S,
            )
        _raise_on_err(ack)
        # Could parse READY,<n> here for an extra integrity check; the
        # firmware already validated n_steps so we skip.

    def start(self) -> None:
        """Begin executing the most recently uploaded sequence.

        Returns when the firmware acknowledges with ``STARTED``.  The
        sequence is still running; call :meth:`wait_done` to block until
        ``DONE`` / ``STOPPED``.
        """
        if not self.capabilities.is_v4:
            raise TeensyProtocolError('start() requires v4 firmware')
        ack = self._send_recv(b'START\n', terminal=('STARTED', 'ERR'),
                              timeout=self._START_ACK_TIMEOUT_S)
        _raise_on_err(ack)

    def wait_done(self, timeout: Optional[float] = None) -> bool:
        """Block until ``DONE`` or ``STOPPED`` arrives.

        Returns True on ``DONE``, False on ``STOPPED``.  Raises
        :class:`TeensyProtocolError` on ``ERR,*`` or timeout.

        Acquires :attr:`_lock` around every serial read but releases it
        between iterations, so a concurrent :meth:`stop` from another
        thread can interleave its STOP write without us discarding the
        firmware's eventual STOPPED line or stealing each other's
        in-flight bytes.
        """
        if not self.capabilities.is_v4:
            raise TeensyProtocolError('wait_done() requires v4 firmware')
        deadline_s = None if timeout is None else (time.monotonic() + timeout)
        while True:
            if deadline_s is not None and time.monotonic() >= deadline_s:
                raise TeensyProtocolError('wait_done timed out')
            # Try to pop a line first — fast path when bytes already
            # arrived during a previous iteration's lock-hold window.
            line = self._pop_line_from_rx_buf()
            if line is None:
                # Acquire the lock to do a single physical read; release
                # between iterations so stop() can write STOP.
                with self._lock:
                    chunk = self._ser.read(64)
                    if chunk:
                        self._rx_buf.extend(chunk)
                        line = self._pop_line_from_rx_buf()
                if not chunk and line is None:
                    # No data; brief yield and retry.
                    time.sleep(0.001)
                    continue
            if line is None:
                continue
            if line == 'DONE':
                return True
            if line == 'STOPPED':
                return False
            if line.startswith('ERR,'):
                raise TeensyProtocolError(line)
            # Anything else is debug chatter from the firmware; ignore.

    def stop(self) -> None:
        """Abort the running sequence.  Safe to call when idle.

        Fire-and-forget on v4: takes the lock, writes STOP, releases.
        Does NOT read a response — the firmware emits STOPPED exactly
        once (either from its handleStop when idle, or from the running
        sequence's unwind when busy), and that line will be picked up
        by whoever is in :meth:`wait_done`, or flushed by the next
        :meth:`_send_recv`'s reset-input-buffer when nobody is.

        This is why :meth:`wait_done` takes the lock per-iteration too —
        we need it to play nicely with our STOP write.
        """
        if not self.capabilities.is_v4:
            # v3 has no abort path; document and no-op.
            return
        with self._lock:
            # NOTE: deliberately not calling reset_input_buffer or
            # clearing _rx_buf — that would discard bytes wait_done is
            # in the middle of consuming.
            self._ser.write(b'STOP\n')

    # -- internals --------------------------------------------------------

    def _require_channel(self, n: int) -> None:
        if not (0 <= int(n) < self.capabilities.n_channels):
            raise ValueError(
                f'channel {n} out of range [0, {self.capabilities.n_channels})'
            )

    def _send_recv(
        self,
        cmd,
        terminal: Iterable[str],
        timeout: float,
    ) -> str:
        """Send one command, return the first terminal-token line."""
        if isinstance(cmd, str):
            cmd = cmd.encode('ascii')
        terminal = tuple(terminal)

        def is_terminal(s: str) -> bool:
            return any(s == t or s.startswith(t + ',') for t in terminal)

        with self._lock:
            self._ser.reset_input_buffer()
            self._rx_buf.clear()
            self._ser.write(cmd)
            return self._read_line_until_match(is_terminal, timeout=timeout)

    def _pop_line_from_rx_buf(self) -> Optional[str]:
        """Pop the first '\\n'-terminated line from the persistent rx
        buffer, or return None if no full line is available yet."""
        nl = self._rx_buf.find(b'\n')
        if nl < 0:
            return None
        line_bytes = bytes(self._rx_buf[:nl])
        del self._rx_buf[:nl + 1]
        return line_bytes.decode('ascii', errors='replace').strip('\r ')

    def _read_line_until(self, timeout: float) -> str:
        """Read one ``\\n``-terminated line within ``timeout`` seconds.

        Returns the line minus terminator (and any leading/trailing CR).
        Empty string on timeout.

        Uses :attr:`_rx_buf` as a persistent scratch so bytes that follow
        the first '\\n' in a chunk are kept for the next call rather
        than dropped — required for the v4 two-line ``STARTED\\nDONE\\n``
        response.
        """
        cached = self._pop_line_from_rx_buf()
        if cached is not None:
            return cached

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = self._ser.read(64)
            if not chunk:
                continue
            self._rx_buf.extend(chunk)
            cached = self._pop_line_from_rx_buf()
            if cached is not None:
                return cached
        return ''

    def _read_line_until_match(self, predicate, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            line = self._read_line_until(remaining)
            if not line:
                continue
            if predicate(line):
                return line
            # Otherwise it's debug chatter or a non-terminal response —
            # keep reading.
        raise TeensyProtocolError('timeout waiting for terminal response')

    def _drain(self, quiet_window_s: float, max_wait_s: float) -> None:
        """Read & discard until the port has been quiet for quiet_window_s."""
        last_byte_at = time.monotonic()
        end = last_byte_at + max_wait_s
        while time.monotonic() < end:
            chunk = self._ser.read(256)
            if chunk:
                last_byte_at = time.monotonic()
            elif time.monotonic() - last_byte_at >= quiet_window_s:
                return

    def _handshake(self) -> TeensyCapabilities:
        idn = self.idn()
        m = _IDN_REGEX.match(idn) if idn else None
        if not m:
            return TeensyCapabilities(
                protocol_version='v3',
                n_channels=_V3_N_CHANNELS,
                min_pulse_us=_V3_MIN_PULSE_US,
                max_steps=_V3_MAX_STEPS,
            )
        return TeensyCapabilities(
            protocol_version='v' + m.group('ver'),
            n_channels=int(m.group('nch')),
            min_pulse_us=int(m.group('min_us')),
            max_steps=int(m.group('max_steps')),
        )


# =========================================================================
# Mock driver — in-process, deterministic, no hardware.
# =========================================================================


class MockTeensyPulseDriver:
    """In-process simulator of the v4 firmware.

    Public API mirrors :class:`TeensyPulseDriver` so the manager layer can
    treat them interchangeably.  In addition:

    * ``timeline`` — every pin transition recorded with a virtual
      timestamp (ns).  Tests assert against this.
    * ``simulated_step_delay_s`` — wall-clock delay inserted between
      sequence steps when running.  Default ``0.0`` makes sequences
      complete almost instantly; set to ~10 ms in tests that need to
      exercise :meth:`stop` mid-flight.

    The mock validates the *semantic* contract — channel ranges, step
    durations, max-steps, no-sequence-no-start — exactly the same way
    the firmware would.  It does NOT emulate the wire format, since
    tests at this layer assert effects, not bytes.  (The wire format is
    cross-checked separately by tests against a fake-serial transport.)
    """

    def __init__(
        self,
        n_channels: int = _V4_FALLBACK_N_CHANNELS,
        min_pulse_us: int = _V4_FALLBACK_MIN_PULSE_US,
        max_steps: int = _V4_FALLBACK_MAX_STEPS,
        protocol_version: str = 'v4.0',
        simulated_step_delay_s: float = 0.0,
    ):
        self.capabilities = TeensyCapabilities(
            protocol_version=protocol_version,
            n_channels=n_channels,
            min_pulse_us=min_pulse_us,
            max_steps=max_steps,
        )
        self.simulated_step_delay_s = simulated_step_delay_s

        self.pin_states: dict[int, bool] = {ch: False for ch in range(n_channels)}
        self.timeline: List[TimelineEvent] = []
        self._virtual_clock_ns: int = 0

        self._buffered_steps: Optional[List[Tuple[int, int]]] = None
        self._buffered_n_reps: int = 1

        self._running = False
        self._stop_requested = False
        self._done_event = threading.Event()
        self._done_event.set()  # idle == "done"
        self._stopped_flag = False
        self._worker: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._closed = False

    # -- public API mirrors TeensyPulseDriver ----------------------------

    @property
    def is_open(self) -> bool:
        return not self._closed

    def close(self) -> None:
        self.stop()
        self._closed = True

    def idn(self) -> str:
        cap = self.capabilities
        if cap.is_v4:
            ver_short = cap.protocol_version.lstrip('v')
            return (
                f'IMSWITCH_TEENSY,{ver_short},'
                f'{cap.n_channels},{cap.min_pulse_us},{cap.max_steps}'
            )
        return ''

    def pin(self, n: int, level: bool) -> None:
        self._require_channel(n)
        with self._lock:
            self._set_pin(n, bool(level), source='pin')

    def snap(self, channels: Sequence[int], width_us: int) -> None:
        for ch in channels:
            self._require_channel(ch)
        if width_us < self.capabilities.min_pulse_us:
            raise ValueError(
                f'width_us={width_us} below firmware min '
                f'{self.capabilities.min_pulse_us}'
            )
        with self._lock:
            for ch in channels:
                self._set_pin(ch, True, source='snap')
            self._advance_clock(width_us * 1_000)
            for ch in channels:
                self._set_pin(ch, False, source='snap')

    def upload_sequence(
        self,
        steps: Sequence[Tuple[int, int]],
        n_reps: int = 1,
    ) -> None:
        if not self.capabilities.is_v4:
            raise TeensyProtocolError('upload_sequence requires v4 firmware')
        if n_reps < 1:
            raise ValueError(f'n_reps={n_reps} must be >= 1')
        if not steps:
            raise ValueError('empty sequence')
        if len(steps) > self.capabilities.max_steps:
            raise ValueError(
                f'sequence has {len(steps)} steps; max {self.capabilities.max_steps}'
            )
        for idx, (dur, mask) in enumerate(steps):
            if dur < self.capabilities.min_pulse_us:
                raise ValueError(f'step {idx} duration_us={dur} below min')
            if not (0 <= mask < (1 << self.capabilities.n_channels)):
                raise ValueError(
                    f'step {idx} bitmask 0x{mask:X} outside channel range'
                )
        with self._lock:
            if self._running:
                raise TeensyProtocolError(
                    'cannot upload sequence while one is running'
                )
            self._buffered_steps = [(int(d), int(m)) for d, m in steps]
            self._buffered_n_reps = int(n_reps)

    def start(self) -> None:
        if not self.capabilities.is_v4:
            raise TeensyProtocolError('start() requires v4 firmware')
        with self._lock:
            if self._buffered_steps is None:
                raise TeensyProtocolError('no sequence uploaded')
            if self._running:
                raise TeensyProtocolError('already running')
            self._running = True
            self._stop_requested = False
            self._stopped_flag = False
            self._done_event.clear()
            self._worker = threading.Thread(
                target=self._run_loop, daemon=True,
            )
            self._worker.start()

    def wait_done(self, timeout: Optional[float] = None) -> bool:
        if not self._done_event.wait(timeout=timeout):
            raise TeensyProtocolError('wait_done timed out')
        return not self._stopped_flag

    def stop(self) -> None:
        with self._lock:
            self._stop_requested = True
        if self._worker is not None:
            self._worker.join(timeout=2.0)

    # -- test helpers ----------------------------------------------------

    def get_pin_state(self, n: int) -> bool:
        return self.pin_states[n]

    def get_timeline(self) -> List[TimelineEvent]:
        return list(self.timeline)

    def assert_pulse(
        self,
        channel: int,
        start_ns: int,
        duration_ns: int,
        tol_ns: int = 0,
    ) -> None:
        """Assert that ``channel`` had a HIGH pulse starting near
        ``start_ns`` of length near ``duration_ns``.  Raises
        ``AssertionError`` if not found.
        """
        # Find the first LOW→HIGH on the channel at-or-after start_ns - tol_ns
        events = [e for e in self.timeline if e.channel == channel]
        rises = [
            e for e in events
            if e.level and abs(e.virtual_time_ns - start_ns) <= tol_ns
        ]
        assert rises, (
            f'no rising edge on channel {channel} within {tol_ns}ns '
            f'of t={start_ns}; events on this channel: {events}'
        )
        rise = rises[0]
        # Following falling edge
        falls = [
            e for e in events
            if not e.level and e.virtual_time_ns > rise.virtual_time_ns
        ]
        assert falls, f'channel {channel} rose at t={rise.virtual_time_ns} but never fell'
        fall = falls[0]
        actual_duration = fall.virtual_time_ns - rise.virtual_time_ns
        assert abs(actual_duration - duration_ns) <= tol_ns, (
            f'channel {channel} pulse duration {actual_duration}ns '
            f'!= {duration_ns}ns (±{tol_ns})'
        )

    # -- internals -------------------------------------------------------

    def _require_channel(self, n: int) -> None:
        if not (0 <= int(n) < self.capabilities.n_channels):
            raise ValueError(
                f'channel {n} out of range [0, {self.capabilities.n_channels})'
            )

    def _set_pin(self, ch: int, level: bool, source: str) -> None:
        if self.pin_states[ch] == level:
            return  # No transition — don't pollute timeline.
        self.pin_states[ch] = level
        self.timeline.append(TimelineEvent(
            virtual_time_ns=self._virtual_clock_ns,
            channel=ch,
            level=level,
            source=source,
        ))

    def _advance_clock(self, ns: int) -> None:
        self._virtual_clock_ns += ns

    def _run_loop(self) -> None:
        try:
            assert self._buffered_steps is not None
            for _ in range(self._buffered_n_reps):
                if self._stop_requested:
                    break
                for dur_us, mask in self._buffered_steps:
                    if self._stop_requested:
                        break
                    # Apply: every channel's state for the step.
                    for ch in range(self.capabilities.n_channels):
                        wanted = bool(mask & (1 << ch))
                        self._set_pin(ch, wanted, source='seq')
                    if self.simulated_step_delay_s > 0:
                        time.sleep(self.simulated_step_delay_s)
                    self._advance_clock(dur_us * 1_000)
            if self._stop_requested:
                # Firmware behaviour: set all channels LOW on STOP.
                for ch in range(self.capabilities.n_channels):
                    self._set_pin(ch, False, source='stop')
                self._stopped_flag = True
        finally:
            self._running = False
            self._done_event.set()


# -- module-private helpers ------------------------------------------------


def _raise_on_err(line: str) -> None:
    if line.startswith('ERR,'):
        raise TeensyProtocolError(line)


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
