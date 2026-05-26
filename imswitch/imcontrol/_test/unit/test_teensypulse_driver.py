"""Tests for TeensyPulseDriver + MockTeensyPulseDriver + v4 wire protocol.

Three layers of coverage:

1. **Mock-direct** — drive the in-process simulator via its public API
   and assert pin states / timeline.  Cheap, deterministic.
2. **Edge-case contract** — error paths, channel ranges, max-steps,
   v3 handshake fallback (mock configured as v3).
3. **Wire-protocol cross-check** — a fake serial transport whose other
   end is a tiny line-by-line interpreter of the v4 protocol.  The real
   ``TeensyPulseDriver`` sends bytes; the interpreter answers; assertions
   confirm the round-trip works.  This is the only way to catch wire-
   format bugs without a real Teensy.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

import pytest

from imswitch.imcontrol.model.interfaces.teensypulse import (
    MockTeensyPulseDriver,
    TeensyCapabilities,
    TeensyProtocolError,
    TeensyPulseDriver,
    TimelineEvent,
)


# =========================================================================
# Layer 1 — mock-direct API tests.
# =========================================================================


class TestMockDriverBasics:
    def test_capabilities_default_to_v4(self):
        d = MockTeensyPulseDriver()
        assert d.capabilities.protocol_version == 'v4.0'
        assert d.capabilities.n_channels == 16
        assert d.capabilities.min_pulse_us == 1
        assert d.capabilities.max_steps == 256
        assert d.capabilities.is_v4

    def test_idn_string_round_trip(self):
        d = MockTeensyPulseDriver(n_channels=8, min_pulse_us=2, max_steps=64)
        assert d.idn() == 'IMSWITCH_TEENSY,4.0,8,2,64'

    def test_v3_mock_returns_empty_idn(self):
        d = MockTeensyPulseDriver(protocol_version='v3', n_channels=3,
                                  min_pulse_us=1, max_steps=16)
        assert d.idn() == ''
        assert not d.capabilities.is_v4

    def test_pin_changes_state_and_records_event(self):
        d = MockTeensyPulseDriver()
        d.pin(5, True)
        assert d.get_pin_state(5) is True
        events = [e for e in d.timeline if e.channel == 5]
        assert len(events) == 1
        assert events[0].level is True
        assert events[0].source == 'pin'
        d.pin(5, False)
        assert d.get_pin_state(5) is False

    def test_pin_idempotent_no_redundant_events(self):
        d = MockTeensyPulseDriver()
        d.pin(3, True)
        d.pin(3, True)
        d.pin(3, True)
        assert len([e for e in d.timeline if e.channel == 3]) == 1

    def test_pin_rejects_out_of_range(self):
        d = MockTeensyPulseDriver(n_channels=4)
        with pytest.raises(ValueError):
            d.pin(5, True)


class TestMockSnap:
    def test_snap_creates_high_low_pair(self):
        d = MockTeensyPulseDriver()
        d.snap(channels=[2, 4], width_us=50)
        # Both channels should have a HIGH then LOW transition.
        d.assert_pulse(channel=2, start_ns=0, duration_ns=50_000)
        d.assert_pulse(channel=4, start_ns=0, duration_ns=50_000)
        # Final state: both LOW.
        assert d.get_pin_state(2) is False
        assert d.get_pin_state(4) is False

    def test_snap_rejects_short_width(self):
        d = MockTeensyPulseDriver(min_pulse_us=1000)
        with pytest.raises(ValueError):
            d.snap(channels=[0], width_us=500)


class TestMockSequence:
    def _checkerboard_steps(self, n_steps=4, dur_us=100):
        # Alternating bitmask 0x05 / 0x00 (channels 0 and 2 toggle).
        return [(dur_us, 0x05 if i % 2 == 0 else 0) for i in range(n_steps)]

    def test_program_then_run_blocking_walks_steps(self):
        d = MockTeensyPulseDriver()
        d.upload_sequence(self._checkerboard_steps(4, 100), n_reps=2)
        d.start()
        d.wait_done(timeout=1.0)
        # 4-step checkerboard × 2 reps → 4 transitions per rep × 2 = 8.
        ch0 = [e for e in d.timeline if e.channel == 0]
        ch2 = [e for e in d.timeline if e.channel == 2]
        assert [e.level for e in ch0] == [True, False] * 4
        assert [e.level for e in ch2] == [True, False] * 4

    def test_sequence_advances_virtual_clock(self):
        d = MockTeensyPulseDriver()
        d.upload_sequence([(100, 0x01), (200, 0x00)], n_reps=1)
        d.start()
        d.wait_done(timeout=1.0)
        # Channel 0 rises at t=0, falls at t=100µs = 100_000ns.
        d.assert_pulse(channel=0, start_ns=0, duration_ns=100_000)

    def test_upload_rejects_empty_sequence(self):
        d = MockTeensyPulseDriver()
        with pytest.raises(ValueError):
            d.upload_sequence([], n_reps=1)

    def test_upload_rejects_too_many_steps(self):
        d = MockTeensyPulseDriver(max_steps=4)
        with pytest.raises(ValueError):
            d.upload_sequence([(100, 0x01)] * 5, n_reps=1)

    def test_upload_rejects_short_duration(self):
        d = MockTeensyPulseDriver(min_pulse_us=10)
        with pytest.raises(ValueError):
            d.upload_sequence([(5, 0x01)], n_reps=1)

    def test_upload_rejects_bitmask_above_n_channels(self):
        d = MockTeensyPulseDriver(n_channels=4)
        # Bitmask 0x10 = channel 4, out of range for n_channels=4.
        with pytest.raises(ValueError):
            d.upload_sequence([(100, 0x10)], n_reps=1)

    def test_start_rejects_when_no_sequence(self):
        d = MockTeensyPulseDriver()
        with pytest.raises(TeensyProtocolError):
            d.start()

    def test_start_rejects_double(self):
        d = MockTeensyPulseDriver(simulated_step_delay_s=0.05)
        d.upload_sequence([(100, 0x01)] * 100, n_reps=1)
        d.start()
        with pytest.raises(TeensyProtocolError):
            d.start()
        d.stop()

    def test_stop_during_run_sets_pins_low_and_returns_stopped(self):
        d = MockTeensyPulseDriver(simulated_step_delay_s=0.02)
        d.upload_sequence([(100, 0x07)] * 50, n_reps=1)
        d.start()
        time.sleep(0.03)  # let a couple of steps run
        d.stop()
        # wait_done returns False on STOPPED.
        result = d.wait_done(timeout=2.0)
        assert result is False
        # All channels LOW after stop.
        assert all(not d.get_pin_state(ch) for ch in range(3))

    def test_stop_when_idle_is_noop(self):
        d = MockTeensyPulseDriver()
        d.stop()  # should not raise


class TestV3Fallback:
    def test_v3_mock_rejects_sequence_operations(self):
        d = MockTeensyPulseDriver(protocol_version='v3', n_channels=3,
                                  min_pulse_us=1, max_steps=16)
        with pytest.raises(TeensyProtocolError):
            d.upload_sequence([(100, 0x01)], n_reps=1)
        with pytest.raises(TeensyProtocolError):
            d.start()

    def test_v3_pin_and_snap_still_work(self):
        d = MockTeensyPulseDriver(protocol_version='v3', n_channels=3,
                                  min_pulse_us=1, max_steps=16)
        d.pin(1, True)
        assert d.get_pin_state(1) is True
        d.snap([0, 2], width_us=10)
        d.assert_pulse(channel=0, start_ns=0, duration_ns=10_000)
        d.assert_pulse(channel=2, start_ns=0, duration_ns=10_000)


# =========================================================================
# Layer 3 — wire-protocol cross-check.
# =========================================================================
#
# A fake duplex byte stream replaces pyserial.Serial.  The "firmware"
# side parses incoming lines, executes them against an inner
# MockTeensyPulseDriver (so we trust one source of behaviour) and emits
# the v4 wire responses.  The real TeensyPulseDriver then talks to this
# fake stream and we assert that the round trip works.
#
# This catches wire-format bugs in TeensyPulseDriver — IDN regex,
# SEQ framing, terminal-token detection, two-line START response — that
# the mock-direct tests can't.


class _FakeSerial:
    """Minimal pyserial-API-compatible duplex pipe with a firmware simulator
    listening on the other side.
    """

    def __init__(self, firmware: '_V4FirmwareSim'):
        self._firmware = firmware
        # Bytes from firmware → host
        self._read_buf = bytearray()
        self._read_lock = threading.Lock()
        self._closed = False
        self._line_buf = bytearray()
        # Hand the firmware a write callback.
        firmware.attach_host(self._emit_to_host)

    # pyserial-compatible API surface used by TeensyPulseDriver
    @property
    def is_open(self) -> bool:
        return not self._closed

    def close(self) -> None:
        self._closed = True

    def reset_input_buffer(self) -> None:
        with self._read_lock:
            self._read_buf.clear()

    def write(self, data: bytes) -> int:
        # Stream bytes into the firmware line by line.
        for b in data:
            if b == ord('\n'):
                line = self._line_buf.decode('ascii', errors='replace').strip('\r ')
                self._line_buf.clear()
                self._firmware.handle_line(line)
            elif b != ord('\r'):
                self._line_buf.append(b)
        return len(data)

    def read(self, size: int = 1) -> bytes:
        # TeensyPulseDriver uses a per-call timeout of 0.05s on
        # self._ser.timeout.  We don't model that here — return whatever
        # bytes are queued immediately, empty if none.  The driver's
        # outer deadline loop handles waiting.
        time.sleep(0.001)  # tiny yield so threaded firmware can produce bytes
        with self._read_lock:
            if not self._read_buf:
                return b''
            out = bytes(self._read_buf[:size])
            del self._read_buf[:size]
            return out

    def _emit_to_host(self, line: str) -> None:
        with self._read_lock:
            self._read_buf.extend((line + '\n').encode('ascii'))


class _V4FirmwareSim:
    """Tiny v4 protocol interpreter.

    Owns an inner MockTeensyPulseDriver (so we don't reimplement
    semantics) and translates between wire lines and that driver's
    method calls.
    """

    def __init__(self, n_channels=16, min_pulse_us=1, max_steps=256):
        self._mock = MockTeensyPulseDriver(
            n_channels=n_channels,
            min_pulse_us=min_pulse_us,
            max_steps=max_steps,
        )
        # Send IDN_BANNER on boot so the driver's drain sees something.
        # (Real firmware does the same.)
        self._send = None  # set in attach_host
        # SEQ upload state
        self._upload_remaining = 0
        self._upload_buf: list = []
        self._upload_nreps = 1

    @property
    def mock(self):
        return self._mock

    def attach_host(self, send_callback):
        self._send = send_callback
        # Boot banner.
        self._send(self._mock.idn())

    def handle_line(self, line: str) -> None:
        if self._upload_remaining > 0:
            self._handle_seq_step(line)
            return

        if line == '*IDN?':
            self._send(self._mock.idn())
        elif line.startswith('PIN,'):
            self._handle_pin(line)
        elif line.startswith('PinHigh,') or line.startswith('PinLow,'):
            self._handle_legacy_pin(line)
        elif line.startswith('SEQ,'):
            self._handle_seq_header(line)
        elif line == 'START':
            self._handle_start()
        elif line == 'STOP':
            self._handle_stop()
        # Unknown commands silently dropped, like real firmware.

    # --- handlers ---

    def _handle_pin(self, line):
        try:
            _, n, lvl = line.split(',')
            n = int(n)
            lvl = bool(int(lvl))
        except ValueError:
            self._send('ERR,bad_pin')
            return
        try:
            self._mock.pin(n, lvl)
        except ValueError:
            self._send('ERR,bad_pin')
            return
        self._send('OK')

    def _handle_legacy_pin(self, line):
        try:
            cmd, n = line.split(',')
            n = int(n)
            lvl = cmd == 'PinHigh'
            self._mock.pin(n, lvl)
        except ValueError:
            self._send('ERR,bad_pin')
            return
        self._send('OK')

    def _handle_seq_header(self, line):
        try:
            _, n_steps, n_reps = line.split(',')
            n_steps = int(n_steps)
            n_reps = int(n_reps)
        except ValueError:
            self._send('ERR,bad_step,0,bitmask_format')
            return
        if n_steps > self._mock.capabilities.max_steps:
            self._send('ERR,too_many_steps')
            return
        self._upload_remaining = n_steps
        self._upload_buf = []
        self._upload_nreps = n_reps

    def _handle_seq_step(self, line):
        try:
            dur_str, mask_str = line.split(',')
            dur = int(dur_str)
            mask = int(mask_str, 16) if mask_str.lower().startswith('0x') else int(mask_str)
        except ValueError:
            idx = len(self._upload_buf)
            self._upload_buf = []
            self._upload_remaining = 0
            self._send(f'ERR,bad_step,{idx},bitmask_format')
            return
        self._upload_buf.append((dur, mask))
        self._upload_remaining -= 1
        if self._upload_remaining == 0:
            try:
                self._mock.upload_sequence(self._upload_buf,
                                           n_reps=self._upload_nreps)
            except (ValueError, TeensyProtocolError):
                self._send('ERR,bad_step,0,duration_too_small')
                return
            self._send(f'READY,{len(self._upload_buf)}')

    def _handle_start(self):
        try:
            self._mock.start()
        except TeensyProtocolError as e:
            if 'no sequence' in str(e):
                self._send('ERR,no_sequence')
            elif 'already' in str(e):
                self._send('ERR,already_running')
            else:
                self._send('ERR,no_sequence')
            return
        self._send('STARTED')

        def watcher():
            try:
                done = self._mock.wait_done(timeout=10.0)
            except TeensyProtocolError:
                self._send('ERR,timeout')
                return
            self._send('DONE' if done else 'STOPPED')

        threading.Thread(target=watcher, daemon=True).start()

    def _handle_stop(self):
        # Mirror real firmware:
        #   * If a sequence is running, handleStop only sets the abort
        #     flag (silently).  The running-sequence watcher then emits
        #     STOPPED on its own as the run unwinds.
        #   * If idle, handleStop emits STOPPED here.
        # The driver's stop() is fire-and-forget under either branch.
        if self._mock._running:
            self._mock.stop()  # triggers watcher → STOPPED
        else:
            self._send('STOPPED')


@pytest.fixture
def fake_firmware(monkeypatch):
    """Replace serial.Serial with a fake whose other end is _V4FirmwareSim."""
    fw = _V4FirmwareSim()
    fake = _FakeSerial(fw)

    # Patch the module's serial reference so TeensyPulseDriver picks up our fake.
    import imswitch.imcontrol.model.interfaces.teensypulse as tp_mod

    class _FakeSerialModule:
        Serial = staticmethod(lambda *a, **kw: fake)

    monkeypatch.setattr(tp_mod, 'serial', _FakeSerialModule)
    return fw, fake


class TestWireProtocolV4:
    def test_handshake_populates_capabilities(self, fake_firmware):
        drv = TeensyPulseDriver(port='/fake')
        assert drv.capabilities.is_v4
        assert drv.capabilities.n_channels == 16
        assert drv.capabilities.min_pulse_us == 1
        assert drv.capabilities.max_steps == 256
        drv.close()

    def test_pin_round_trip(self, fake_firmware):
        fw, _ = fake_firmware
        drv = TeensyPulseDriver(port='/fake')
        drv.pin(3, True)
        assert fw.mock.get_pin_state(3) is True
        drv.pin(3, False)
        assert fw.mock.get_pin_state(3) is False
        drv.close()

    def test_pin_out_of_range_returns_err(self, fake_firmware):
        drv = TeensyPulseDriver(port='/fake')
        # Driver validates before sending so this raises ValueError, not
        # the firmware ERR — that's the documented behaviour.
        with pytest.raises(ValueError):
            drv.pin(99, True)
        drv.close()

    def test_sequence_round_trip(self, fake_firmware):
        fw, _ = fake_firmware
        drv = TeensyPulseDriver(port='/fake')
        steps = [(100, 0x05), (100, 0x00), (100, 0x05), (100, 0x00)]
        drv.upload_sequence(steps, n_reps=2)
        drv.start()
        done = drv.wait_done(timeout=2.0)
        assert done is True
        # 4-step checkerboard × 2 reps → 4 transitions per rep × 2 = 8.
        events_ch0 = [e for e in fw.mock.timeline if e.channel == 0]
        assert [e.level for e in events_ch0] == [True, False] * 4
        drv.close()

    def test_stop_round_trip(self, fake_firmware):
        fw, _ = fake_firmware
        fw.mock.simulated_step_delay_s = 0.02
        drv = TeensyPulseDriver(port='/fake')
        drv.upload_sequence([(100, 0x01)] * 100, n_reps=1)
        drv.start()
        time.sleep(0.05)
        drv.stop()
        # wait_done returns False after STOPPED.
        result = drv.wait_done(timeout=2.0)
        assert result is False
        drv.close()

    def test_concurrent_stop_during_wait_done(self, fake_firmware):
        """Regression: the manager calls wait_done() from a worker thread
        while the main thread calls stop().  Both touch the driver's
        serial state; an earlier design had stop() read STOPPED itself,
        which raced with wait_done's read and either stole the STOPPED
        line (wait_done hangs) or wiped its rx buffer.

        Real firmware emits STOPPED exactly once.  The driver must
        deliver it to whoever is currently reading."""
        fw, _ = fake_firmware
        fw.mock.simulated_step_delay_s = 0.02
        drv = TeensyPulseDriver(port='/fake')
        drv.upload_sequence([(100, 0x01)] * 100, n_reps=1)
        drv.start()

        wait_result = {}

        def waiter():
            try:
                wait_result['done'] = drv.wait_done(timeout=2.0)
            except Exception as e:
                wait_result['error'] = e

        t = threading.Thread(target=waiter, daemon=True)
        t.start()
        time.sleep(0.05)        # let the sequence enter a step
        drv.stop()              # send STOP — fire and forget
        t.join(timeout=2.0)

        assert 'error' not in wait_result, f'wait_done raised: {wait_result.get("error")}'
        assert wait_result.get('done') is False, (
            f'expected wait_done to observe STOPPED; got {wait_result}'
        )
        drv.close()

    def test_sequence_upload_rejects_oversize(self, fake_firmware):
        drv = TeensyPulseDriver(port='/fake')
        # Driver-side validation catches this before the wire.
        with pytest.raises(ValueError):
            drv.upload_sequence([(100, 0x01)] * 1000, n_reps=1)
        drv.close()


# Verify import-graph health.
def test_module_imports_without_pyserial(monkeypatch):
    """MockTeensyPulseDriver must work even when pyserial is absent."""
    import imswitch.imcontrol.model.interfaces.teensypulse as tp_mod
    monkeypatch.setattr(tp_mod, 'serial', None)
    # Mock construction & basic ops still succeed.
    d = MockTeensyPulseDriver()
    d.pin(0, True)
    assert d.get_pin_state(0) is True
    # Real driver construction without pyserial raises ImportError.
    with pytest.raises(ImportError):
        TeensyPulseDriver(port='/no-such')
