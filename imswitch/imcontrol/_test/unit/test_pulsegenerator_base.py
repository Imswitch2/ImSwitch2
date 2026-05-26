"""Contract tests for ``PulseGeneratorManager``.

Exercises the abstract base via a tiny in-process ``NullPulseGenerator``
that records calls — no hardware, no vendor libraries needed.

The Phase 0 spec is the source of truth; if a contract assertion here
contradicts the spec, fix the spec, not the tests.
"""

from __future__ import annotations

import threading
import time

import pytest
from qtpy import QtCore

from imswitch.imcontrol.model.managers.pulsegen import (
    PulseGeneratorError,
    PulseGeneratorManager,
    PulseStep,
)


# ---------------------------------------------------------------------------
# Test double: a minimal, hardware-free implementation of the ABC.
# ---------------------------------------------------------------------------


class NullPulseGenerator(PulseGeneratorManager):
    """In-memory backend that records every call.

    Channel state is held in ``self.pin_states``.  Programmed sequences
    are stored on ``self.last_program``.  ``run`` simulates a sequence
    by walking the steps and updating ``pin_states``; the (synthetic)
    duration in real wall-clock is zero — tests get deterministic
    behavior without sleeping.
    """

    def __init__(
        self,
        n_channels: int = 8,
        min_pulse_ns: int = 100,
        analog: bool = False,
        sim_duration_s: float = 0.0,
    ):
        super().__init__()
        self._n = n_channels
        self._min = min_pulse_ns
        self._analog = analog
        self._sim_duration_s = sim_duration_s

        self.pin_states: dict[int, bool] = {ch: False for ch in range(n_channels)}
        self.analog_states: dict[int, float] = {}
        self.last_program: list[PulseStep] | None = None
        self.last_n_reps: int | None = None
        self.timeline: list[tuple[int, dict[int, bool]]] = []  # (rep_idx, states_snapshot)

        self._running = False
        self._stop_requested = False
        self._worker: threading.Thread | None = None

        self.started_count = 0
        self.done_count = 0
        self.failed_count = 0
        # DirectConnection: signal handlers run synchronously in the emitting
        # thread.  Without it, queued signals from the worker thread would
        # need a pumping event loop to be delivered — which no test has.
        self.sigSequenceStarted.connect(
            lambda: self._inc('started'), QtCore.Qt.DirectConnection
        )
        self.sigSequenceDone.connect(
            lambda: self._inc('done'), QtCore.Qt.DirectConnection
        )
        self.sigSequenceFailed.connect(
            lambda _: self._inc('failed'), QtCore.Qt.DirectConnection
        )

    def _inc(self, which: str):
        if which == 'started':
            self.started_count += 1
        elif which == 'done':
            self.done_count += 1
        elif which == 'failed':
            self.failed_count += 1

    # --- capability properties --------------------------------------------

    @property
    def jitter_ns(self) -> int:
        return 0

    @property
    def min_pulse_width_ns(self) -> int:
        return self._min

    @property
    def n_digital_channels(self) -> int:
        return self._n

    @property
    def supports_hw_trigger_in(self) -> bool:
        return False

    @property
    def supports_analog(self) -> bool:
        return self._analog

    # --- pin control ------------------------------------------------------

    def setDigital(self, channel: int, enable: bool) -> None:
        if self._running:
            raise PulseGeneratorError('cannot setDigital while running')
        if not (0 <= channel < self._n):
            raise ValueError(f'channel {channel} out of range')
        self.pin_states[channel] = bool(enable)

    def setAnalog(self, channel: int, voltage: float) -> None:
        if not self._analog:
            return super().setAnalog(channel, voltage)  # raises
        self.analog_states[channel] = voltage

    # --- sequence ---------------------------------------------------------

    def program_sequence(self, steps):
        if self._running:
            raise PulseGeneratorError('cannot program while running')
        if not steps:
            raise ValueError('empty sequence')
        for idx, step in enumerate(steps):
            if step.duration_ns < self.min_pulse_width_ns:
                raise ValueError(f'step {idx} below min_pulse_width_ns')
            for ch in step.channel_states:
                if not (0 <= ch < self._n):
                    raise ValueError(f'step {idx} channel {ch} out of range')
        self.last_program = list(steps)

    def run(self, n_reps: int = 1, blocking: bool = False) -> None:
        if self.last_program is None:
            raise PulseGeneratorError('no sequence programmed')
        if self._running:
            raise PulseGeneratorError('already running')
        if n_reps < 1:
            raise ValueError('n_reps must be >= 1')

        self._running = True
        self._stop_requested = False
        self.last_n_reps = n_reps
        self.sigSequenceStarted.emit()

        def _execute():
            try:
                for rep in range(n_reps):
                    if self._stop_requested:
                        break
                    for step in self.last_program:
                        if self._stop_requested:
                            break
                        # Apply: channels not listed default to LOW.
                        for ch in range(self._n):
                            self.pin_states[ch] = bool(step.channel_states.get(ch, False))
                        self.timeline.append((rep, dict(self.pin_states)))
                if self._sim_duration_s > 0:
                    time.sleep(self._sim_duration_s)
                self.sigSequenceDone.emit()
            finally:
                self._running = False

        if blocking:
            _execute()
        else:
            self._worker = threading.Thread(target=_execute, daemon=True)
            self._worker.start()

    def stop(self) -> None:
        if not self._running:
            return
        self._stop_requested = True
        if self._worker is not None:
            self._worker.join(timeout=1.0)


# ---------------------------------------------------------------------------
# Capability-property contract.
# ---------------------------------------------------------------------------


def test_capability_properties_present():
    gen = NullPulseGenerator()
    assert isinstance(gen.jitter_ns, int)
    assert isinstance(gen.min_pulse_width_ns, int)
    assert isinstance(gen.n_digital_channels, int)
    assert isinstance(gen.supports_hw_trigger_in, bool)
    assert isinstance(gen.supports_analog, bool)
    assert gen.connected is True


def test_set_analog_default_raises_when_unsupported():
    gen = NullPulseGenerator(analog=False)
    with pytest.raises(NotImplementedError):
        gen.setAnalog(0, 1.0)


def test_set_analog_works_when_supported():
    gen = NullPulseGenerator(analog=True)
    gen.setAnalog(0, 0.7)
    assert gen.analog_states[0] == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# program_sequence validation.
# ---------------------------------------------------------------------------


def test_program_sequence_rejects_empty():
    gen = NullPulseGenerator()
    with pytest.raises(ValueError):
        gen.program_sequence([])


def test_program_sequence_rejects_short_duration():
    gen = NullPulseGenerator(min_pulse_ns=1000)
    with pytest.raises(ValueError):
        gen.program_sequence([PulseStep(duration_ns=500, channel_states={0: True})])


def test_program_sequence_rejects_out_of_range_channel():
    gen = NullPulseGenerator(n_channels=4)
    with pytest.raises(ValueError):
        gen.program_sequence([PulseStep(duration_ns=10_000, channel_states={5: True})])


def test_program_sequence_accepts_valid():
    gen = NullPulseGenerator()
    steps = [
        PulseStep(duration_ns=10_000, channel_states={0: True, 2: True}),
        PulseStep(duration_ns=10_000, channel_states={}),
    ]
    gen.program_sequence(steps)
    assert gen.last_program == steps


# ---------------------------------------------------------------------------
# run() — blocking and non-blocking semantics.
# ---------------------------------------------------------------------------


def test_run_blocking_completes_before_return():
    gen = NullPulseGenerator()
    gen.program_sequence([
        PulseStep(duration_ns=1_000, channel_states={0: True}),
        PulseStep(duration_ns=1_000, channel_states={}),
    ])
    gen.run(n_reps=3, blocking=True)
    assert gen.started_count == 1
    assert gen.done_count == 1
    assert gen.last_n_reps == 3
    # Timeline contains rep*step entries
    assert len(gen.timeline) == 3 * 2


def test_run_non_blocking_returns_immediately_and_emits_done():
    gen = NullPulseGenerator(sim_duration_s=0.05)
    gen.program_sequence([PulseStep(duration_ns=1_000, channel_states={0: True})])

    t0 = time.monotonic()
    gen.run(n_reps=1, blocking=False)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.04, f'non-blocking run took {elapsed}s — should return immediately'

    # Wait for sigSequenceDone.
    deadline = time.monotonic() + 1.0
    while gen.done_count == 0 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert gen.done_count == 1


def test_run_rejects_when_no_program():
    gen = NullPulseGenerator()
    with pytest.raises(PulseGeneratorError):
        gen.run()


def test_run_rejects_double_start():
    gen = NullPulseGenerator(sim_duration_s=0.1)
    gen.program_sequence([PulseStep(duration_ns=1_000, channel_states={0: True})])
    gen.run(blocking=False)
    with pytest.raises(PulseGeneratorError):
        gen.run()
    gen.stop()


def test_run_rejects_invalid_n_reps():
    gen = NullPulseGenerator()
    gen.program_sequence([PulseStep(duration_ns=1_000, channel_states={0: True})])
    with pytest.raises(ValueError):
        gen.run(n_reps=0)


# ---------------------------------------------------------------------------
# stop() — no-op when idle, aborts when running.
# ---------------------------------------------------------------------------


def test_stop_is_safe_when_idle():
    gen = NullPulseGenerator()
    gen.stop()  # should not raise


def test_stop_aborts_running_sequence():
    gen = NullPulseGenerator(sim_duration_s=0.2)
    gen.program_sequence([PulseStep(duration_ns=1_000, channel_states={0: True})])
    gen.run(n_reps=100, blocking=False)
    gen.stop()
    # After stop, running flag is clear and sigSequenceDone fired.
    assert gen.done_count == 1


# ---------------------------------------------------------------------------
# setDigital state-coupling rules.
# ---------------------------------------------------------------------------


def test_setdigital_updates_state():
    gen = NullPulseGenerator()
    gen.setDigital(2, True)
    assert gen.pin_states[2] is True
    gen.setDigital(2, False)
    assert gen.pin_states[2] is False


def test_setdigital_rejected_while_running():
    gen = NullPulseGenerator(sim_duration_s=0.1)
    gen.program_sequence([PulseStep(duration_ns=1_000, channel_states={0: True})])
    gen.run(blocking=False)
    with pytest.raises(PulseGeneratorError):
        gen.setDigital(1, True)
    gen.stop()


# ---------------------------------------------------------------------------
# snap() default-impl contract.
# ---------------------------------------------------------------------------


def test_snap_produces_high_then_low_timeline():
    gen = NullPulseGenerator()
    gen.snap(channels=[1, 3], width_ns=10_000)
    # snap programs a 2-step sequence: HIGH on ch 1+3, then all-LOW tail.
    assert gen.last_program is not None
    assert len(gen.last_program) == 2
    high, low = gen.last_program
    assert high.channel_states == {1: True, 3: True}
    assert all(v is False for v in low.channel_states.values())
    # Final pin state is LOW on all channels (the tail step).
    assert all(s is False for s in gen.pin_states.values())


def test_snap_rejects_too_short_pulse():
    gen = NullPulseGenerator(min_pulse_ns=1000)
    with pytest.raises(ValueError):
        gen.snap(channels=[0], width_ns=500)
