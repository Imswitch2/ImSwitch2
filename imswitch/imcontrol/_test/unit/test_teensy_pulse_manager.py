"""Tests for TeensyPulseManager.

The manager wraps a TeensyPulseDriver and bridges it to the
PulseGeneratorManager ABC.  These tests drive the manager with its
mock-fallback path (no port → MockTeensyPulseDriver) so they're
hardware-free and deterministic.

The driver layer is covered by ``test_teensypulse_driver.py``; here we
focus on the manager-specific responsibilities:

* capability mapping (driver µs → manager ns)
* PulseStep → (duration_us, bitmask) translation
* sigSequenceStarted/Done emission across blocking and non-blocking run
* state guards (setDigital/snap/program while running)
* mock fallback when the configured port can't be opened
"""

from __future__ import annotations

import time

import pytest
from qtpy import QtCore

from imswitch.imcontrol.model.managers.pulsegen import (
    PulseGeneratorError,
    PulseStep,
    TeensyPulseInfo,
    TeensyPulseManager,
)
from imswitch.imcontrol.model.interfaces.teensypulse import (
    MockTeensyPulseDriver,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_manager(**info_kwargs) -> TeensyPulseManager:
    """Construct a TeensyPulseManager that uses the mock backend.

    Any port-less TeensyPulseInfo with useMockOnFailure=True (the
    default) drops straight into MockTeensyPulseDriver, so we can build
    headless managers without monkeypatching pyserial.
    """
    info = TeensyPulseInfo(port=None, useMockOnFailure=True, **info_kwargs)
    return TeensyPulseManager(info)


class _SignalCounter:
    """Helper: count signal emissions via DirectConnection so tests
    don't need a running Qt event loop."""

    def __init__(self, manager: TeensyPulseManager):
        self.started = 0
        self.done = 0
        self.failed = 0
        manager.sigSequenceStarted.connect(
            self._inc_started, QtCore.Qt.DirectConnection
        )
        manager.sigSequenceDone.connect(
            self._inc_done, QtCore.Qt.DirectConnection
        )
        manager.sigSequenceFailed.connect(
            self._inc_failed, QtCore.Qt.DirectConnection
        )

    def _inc_started(self):
        self.started += 1

    def _inc_done(self):
        self.done += 1

    def _inc_failed(self, _msg: str):
        self.failed += 1

    def wait_done(self, timeout_s: float = 1.0):
        deadline = time.monotonic() + timeout_s
        while self.done == 0 and time.monotonic() < deadline:
            time.sleep(0.005)


# ---------------------------------------------------------------------------
# Construction and capability mapping
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_no_port_with_mock_fallback_uses_mock_driver(self):
        m = _make_manager()
        assert isinstance(m.driver, MockTeensyPulseDriver)
        assert m.connected is True

    def test_no_port_without_mock_fallback_raises(self):
        info = TeensyPulseInfo(port=None, useMockOnFailure=False)
        with pytest.raises(RuntimeError, match='useMockOnFailure'):
            TeensyPulseManager(info)

    def test_fake_port_falls_back_to_mock(self):
        # On any platform, opening a port that doesn't exist must fail
        # — the manager's mock fallback catches and substitutes.
        m = TeensyPulseManager(TeensyPulseInfo(
            port='/dev/definitely-not-a-real-port',
            useMockOnFailure=True,
        ))
        assert isinstance(m.driver, MockTeensyPulseDriver)


class TestCapabilityMapping:
    def test_capabilities_reflect_driver(self):
        m = _make_manager(mockNChannels=8, mockMinPulseUs=2, mockMaxSteps=64)
        assert m.n_digital_channels == 8
        assert m.min_pulse_width_ns == 2_000
        assert m.supports_analog is False
        assert m.supports_hw_trigger_in is False
        assert m.jitter_ns == 1_000

    def test_min_pulse_width_uses_driver_floor(self):
        # min_pulse_us=5 → 5000ns
        m = _make_manager(mockMinPulseUs=5)
        assert m.min_pulse_width_ns == 5_000


# ---------------------------------------------------------------------------
# setDigital / snap
# ---------------------------------------------------------------------------


class TestSetDigital:
    def test_sets_pin_state_on_driver(self):
        m = _make_manager()
        m.setDigital(3, True)
        assert m.driver.get_pin_state(3) is True
        m.setDigital(3, False)
        assert m.driver.get_pin_state(3) is False

    def test_rejects_out_of_range_channel(self):
        m = _make_manager(mockNChannels=4)
        with pytest.raises(ValueError):
            m.setDigital(99, True)


class TestSnap:
    def test_snap_uses_driver_native_path(self):
        m = _make_manager()
        m.snap(channels=[1, 3], width_ns=50_000)
        # Driver mock records the timeline; both channels should have
        # a rise→fall pair of width 50 µs (= 50_000 ns).
        m.driver.assert_pulse(channel=1, start_ns=0, duration_ns=50_000)
        m.driver.assert_pulse(channel=3, start_ns=0, duration_ns=50_000)

    def test_snap_rejects_short_pulse(self):
        m = _make_manager(mockMinPulseUs=10)  # 10 µs = 10_000 ns floor
        with pytest.raises(ValueError):
            m.snap(channels=[0], width_ns=5_000)


# ---------------------------------------------------------------------------
# program_sequence + run
# ---------------------------------------------------------------------------


def _two_step_seq():
    """A simple 2-step sequence: ch0+ch2 HIGH for 100 µs, then LOW."""
    return [
        PulseStep(duration_ns=100_000, channel_states={0: True, 2: True}),
        PulseStep(duration_ns=100_000, channel_states={}),
    ]


class TestProgramAndRun:
    def test_program_then_blocking_run_emits_started_and_done(self):
        m = _make_manager()
        counter = _SignalCounter(m)
        m.program_sequence(_two_step_seq())
        m.run(n_reps=3, blocking=True)
        assert counter.started == 1
        assert counter.done == 1
        assert counter.failed == 0

    def test_program_then_nonblocking_run_returns_immediately(self):
        m = _make_manager()
        counter = _SignalCounter(m)
        # Tiny per-step real delay so the sequence doesn't finish
        # synchronously between start() and our timing assertion.
        m.driver.simulated_step_delay_s = 0.02
        m.program_sequence(_two_step_seq())

        t0 = time.monotonic()
        m.run(n_reps=1, blocking=False)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.05, f'run(blocking=False) took {elapsed:.3f}s'

        counter.wait_done(timeout_s=2.0)
        assert counter.started == 1
        assert counter.done == 1

    def test_run_rejects_when_no_program(self):
        m = _make_manager()
        with pytest.raises(PulseGeneratorError, match='no sequence'):
            m.run()

    def test_run_rejects_double_start(self):
        m = _make_manager()
        m.driver.simulated_step_delay_s = 0.05
        m.program_sequence(_two_step_seq())
        m.run(n_reps=1, blocking=False)
        with pytest.raises(PulseGeneratorError, match='already running'):
            m.run()
        m.stop()

    def test_run_rejects_invalid_n_reps(self):
        m = _make_manager()
        m.program_sequence(_two_step_seq())
        with pytest.raises(ValueError):
            m.run(n_reps=0)

    def test_program_rejects_empty_sequence(self):
        m = _make_manager()
        with pytest.raises(ValueError):
            m.program_sequence([])

    def test_program_rejects_short_step(self):
        m = _make_manager(mockMinPulseUs=10)  # → 10_000 ns floor
        with pytest.raises(ValueError):
            m.program_sequence([
                PulseStep(duration_ns=5_000, channel_states={0: True})
            ])

    def test_program_rejects_out_of_range_channel(self):
        m = _make_manager(mockNChannels=4)
        with pytest.raises(ValueError):
            m.program_sequence([
                PulseStep(duration_ns=100_000, channel_states={5: True})
            ])

    def test_sequence_produces_correct_bitmask_on_driver(self):
        """PulseStep → bitmask translation: full state per step, channels
        not listed default to LOW.  We assert by inspecting the mock's
        timeline after blocking run."""
        m = _make_manager(mockNChannels=4)
        m.program_sequence([
            PulseStep(duration_ns=10_000, channel_states={0: True, 2: True}),
            PulseStep(duration_ns=10_000, channel_states={1: True}),
            PulseStep(duration_ns=10_000, channel_states={}),
        ])
        m.run(n_reps=1, blocking=True)

        # Channel 0: HIGH at step 0, LOW at step 1 (not listed → LOW).
        ch0_events = [e for e in m.driver.timeline if e.channel == 0]
        assert [e.level for e in ch0_events] == [True, False]
        # Channel 2: same as ch0.
        ch2_events = [e for e in m.driver.timeline if e.channel == 2]
        assert [e.level for e in ch2_events] == [True, False]
        # Channel 1: HIGH at step 1, LOW at step 2.
        ch1_events = [e for e in m.driver.timeline if e.channel == 1]
        assert [e.level for e in ch1_events] == [True, False]


# ---------------------------------------------------------------------------
# State guards
# ---------------------------------------------------------------------------


class TestStateGuards:
    def test_setdigital_rejected_while_running(self):
        m = _make_manager()
        m.driver.simulated_step_delay_s = 0.05
        m.program_sequence(_two_step_seq())
        m.run(n_reps=1, blocking=False)
        with pytest.raises(PulseGeneratorError):
            m.setDigital(1, True)
        m.stop()

    def test_program_sequence_rejected_while_running(self):
        m = _make_manager()
        m.driver.simulated_step_delay_s = 0.05
        m.program_sequence(_two_step_seq())
        m.run(n_reps=1, blocking=False)
        with pytest.raises(PulseGeneratorError):
            m.program_sequence(_two_step_seq())
        m.stop()


class TestStop:
    def test_stop_when_idle_is_noop(self):
        m = _make_manager()
        m.stop()  # must not raise

    def test_stop_emits_sigsequencedone(self):
        m = _make_manager()
        counter = _SignalCounter(m)
        m.driver.simulated_step_delay_s = 0.05
        # Long sequence so we can stop mid-flight.
        m.program_sequence([
            PulseStep(duration_ns=1_000_000, channel_states={0: True}),
            PulseStep(duration_ns=1_000_000, channel_states={}),
        ])
        m.run(n_reps=100, blocking=False)
        time.sleep(0.05)
        m.stop()
        # sigSequenceDone fires after STOPPED arrives.
        counter.wait_done(timeout_s=2.0)
        assert counter.done == 1


# ---------------------------------------------------------------------------
# Finalize
# ---------------------------------------------------------------------------


class TestFinalize:
    def test_finalize_closes_driver(self):
        m = _make_manager()
        assert m.connected is True
        m.finalize()
        assert m.connected is False
