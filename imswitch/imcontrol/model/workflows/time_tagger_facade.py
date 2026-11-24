"""``facade.time_tagger``: the Swabian card from a script.

Thin, synchronous, cancel-aware: every blocking call sleeps in slices that
honour the script's cancel token, and frees its measurement object in a
``finally`` (``OperationCancelled`` is a ``BaseException``). Results are
small dataclasses with a ``summary()`` the tutorials print and a
``to_dict()`` they save. Everything speaks *roles* (``photons``,
``laser_sync``, ``line_clock``, ``frame_clock``, ``sted_pulse``); the card's
channels are the ``timeTagger`` block's business.

Nothing here is exported through ``api.imcontrol`` directly: a script gets
it from ``api.imcontrol.buildWorkflowFacade().time_tagger``.
"""

from __future__ import annotations

import contextlib
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np

from imswitch.imcommon.model.cancellation import cancellableSleep, checkpoint
from imswitch.imcontrol.model.managers.TimeTaggerManager import (
    ROLE_FIELDS,
    ROLES,
    TimeTaggerBusyError,
    TimeTaggerError,
    TimeTaggerManager,
)
from imswitch.imcontrol.model.timeresolved import orient_cube

#: The card's own two-tag timing jitter, RMS picoseconds, by model family.
#: A measured period jitter at or below this is the card, not the laser.
JITTER_FLOOR_PS = {"Time Tagger 20": 48.0, "Time Tagger Ultra": 30.0,
                   "Time Tagger X": 8.0}


def _jitter_floor_ps(model: str) -> float:
    for family, floor in JITTER_FLOOR_PS.items():
        if family in (model or ""):
            return floor
    return 48.0


def _fwhm_ns(t_ns: np.ndarray, counts: np.ndarray) -> float:
    counts = np.asarray(counts, dtype=np.float64)
    if counts.size == 0 or counts.max() <= 0:
        return 0.0
    half = counts.max() / 2.0
    above = np.flatnonzero(counts >= half)
    if above.size < 2:
        return float(t_ns[1] - t_ns[0]) if t_ns.size > 1 else 0.0
    return float(t_ns[above[-1]] - t_ns[above[0]])


# --------------------------------------------------------------------------- #
# Results                                                                      #
# --------------------------------------------------------------------------- #


@dataclass
class RatesResult:
    rates_hz: Dict[str, float]
    duration_s: float

    def summary(self) -> str:
        parts = [f"{role}: {rate / 1e6:.4f} MHz" if rate >= 1e6 else f"{role}: {rate:,.0f} Hz"
                 for role, rate in self.rates_hz.items()]
        return f"count rates over {self.duration_s:g} s -- " + ", ".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        return {"rates_hz": dict(self.rates_hz), "duration_s": self.duration_s}


@dataclass
class RepRateResult:
    role: str
    rate_hz: float
    period_ps: float
    jitter_ps: float
    """ RMS spread of the period histogram, over ``divider`` periods. """
    divider: int
    n_periods: int
    card_floor_ps: float
    card_limited: bool
    """ The measured jitter is at the card's own floor: the laser is at
    least this good, and the number says nothing more about it. """
    filter_was_on: bool

    def summary(self) -> str:
        jitter = (f"jitter {self.jitter_ps:.0f} ps RMS over {self.divider} periods"
                  + (" (card-limited)" if self.card_limited else ""))
        return (f"{self.role}: {self.rate_hz / 1e6:.5f} MHz, period "
                f"{self.period_ps / 1000:.4f} ns, {jitter}; set laser_rep_rate_mhz = "
                f"{self.rate_hz / 1e6:.4f}")

    def to_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class PeriodResult:
    """A scan clock's rate and period, measured as it runs (no divider, no
    transaction: the clocks are slow and the card is not touched)."""

    role: str
    rate_hz: float
    period_ps: float
    """ The mean edge-to-edge time; for the line clock dwell x Nx plus the
    flyback. ``0`` without edges. """
    jitter_ps: float
    n_periods: int
    duration_s: float

    def summary(self) -> str:
        if self.n_periods == 0:
            return f"{self.role}: no edges in {self.duration_s:g} s"
        return (f"{self.role}: {self.rate_hz:,.2f} Hz, period "
                f"{self.period_ps / 1e6:.4f} us ({self.n_periods} periods, "
                f"jitter {self.jitter_ps:.0f} ps RMS)")

    def to_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class SkewResult:
    """The signed time from an edge of ``from_role`` to the next edge of
    ``to_role``, measured through a known added delay (see ``skew``)."""

    from_role: str
    to_role: str
    skew_ps: float
    """ Positive: ``to_role`` fires after ``from_role``. """
    added_delay_ps: int
    n_pairs: int
    pattern_offset_ps: int
    """ Where the detector's first pixel marker sits after a line edge. """

    @property
    def frame_leads_pixel_0(self) -> bool:
        """Whether a frame edge lands before the first pixel marker, which
        the card needs to re-sync its pixel index on it (frame -> line
        skew below the pattern offset)."""
        return self.skew_ps + self.pattern_offset_ps > 0

    def summary(self) -> str:
        if self.n_pairs == 0:
            return f"{self.from_role} -> {self.to_role}: no edge pairs"
        return (f"{self.from_role} -> {self.to_role}: {self.skew_ps:+.0f} ps "
                f"({self.n_pairs} pairs, through a {self.added_delay_ps} ps added delay)")

    def to_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class HistogramResult:
    click_role: str
    start_role: str
    direction: str
    """ The card's TCSPC direction the histogram was taken in. """
    t_axis_ns: np.ndarray
    """ Forward time, whatever the direction. """
    counts: np.ndarray
    binwidth_ps: int
    duration_s: float
    peak_ns: float
    fwhm_ns: float
    total: int
    photon_delay_ps: int = 0
    """ The delay the photon input carried during the measurement (the
    card's configured one), so ``peak_ns`` is an absolute ``t0_ps``. """

    def summary(self) -> str:
        return (f"{self.click_role} vs {self.start_role} ({self.direction}): "
                f"{self.total:,} counts in {self.duration_s:g} s, peak at "
                f"{self.peak_ns:.3f} ns, FWHM {self.fwhm_ns * 1000:.0f} ps")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "click_role": self.click_role, "start_role": self.start_role,
            "direction": self.direction, "t_axis_ns": self.t_axis_ns.tolist(),
            "counts": self.counts.tolist(), "binwidth_ps": self.binwidth_ps,
            "duration_s": self.duration_s, "peak_ns": self.peak_ns,
            "fwhm_ns": self.fwhm_ns, "total": self.total,
            "photon_delay_ps": self.photon_delay_ps,
        }


@dataclass
class SweepResult:
    role: str
    levels_v: List[float]
    rates_hz: List[float]
    plateau_v: Optional[float]
    """ The middle of the levels that count at least 90 % of the maximum;
    ``None`` when nothing counted. """
    restored_v: float

    @property
    def only_near_zero(self) -> bool:
        """True when the input counts only at the levels nearest 0 V.

        That is not a plateau: either the comparator sits in the noise and
        the pulses never reach any swept level (no signal: the cable, the
        channel number, a laser that is not pulsing), or the pulses are
        smaller than the sweep's first step (a sync photodiode into 50 Ω).
        ``rep_rate()`` at such a level tells the two apart: a real sync has
        a stable period, noise has none.
        """
        if self.plateau_v is None:
            return False
        rates = np.asarray(self.rates_hz, dtype=float)
        magnitudes = np.abs(np.asarray(self.levels_v, dtype=float))
        counting = magnitudes[rates >= 0.1 * rates.max()]
        return bool(counting.size and counting.max() <= 1.5 * magnitudes.min() + 1e-9)

    def summary(self) -> str:
        rows = ", ".join(f"{v:+.2f} V: {r:,.0f}" for v, r in zip(self.levels_v, self.rates_hz))
        if self.plateau_v is None:
            plateau = "no plateau: nothing counted (wrong polarity, or no signal)"
        elif self.only_near_zero:
            plateau = (f"counts only next to 0 V ({self.plateau_v:+.2f} V): noise, or pulses "
                       f"smaller than the first level; rep_rate() there tells which")
        else:
            plateau = f"plateau around {self.plateau_v:+.2f} V"
        return f"{self.role} trigger sweep -- {rows}; {plateau}"

    def to_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


# --------------------------------------------------------------------------- #
# Trigger levels: what an input carries, and how a level is judged             #
# --------------------------------------------------------------------------- #

#: The inputs' range (50 ohm, both polarities).
INPUT_RANGE_V = 2.5
#: Below this a comparator counts electrical noise, whatever is cabled.
NOISE_FLOOR_V = 0.05
#: A level wants at least this much room to the noise floor.
SAFE_MARGIN_V = 0.05
#: A plateau narrower than this leaves no room for drifts and reflections.
MIN_PLATEAU_WIDTH_V = 0.1
#: What an NI-DAQ digital line reaches into the card's 50 ohm input, at
#: least (about 1 to 1.5 V): a clock threshold above this may never fire.
DAQ_LINE_REACH_V = 1.0
#: A level counts as "counting" with at least this many counts.
MIN_LEVEL_COUNTS = 5
#: The coarse sweep, as magnitudes in volts (the sign is the input's).
COARSE_LEVELS_V = (0.02, 0.04, 0.07, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.65,
                   0.8, 1.0, 1.25, 1.5, 2.0, 2.5)
#: Levels added between two coarse levels to place a plateau edge.
FINE_STEPS = 5

#: Hints per kind of signal, by the role an input is configured as. A role
#: is only a label: an input without one gets the generic hints, and every
#: check works on any input.
SIGNAL_KINDS: Dict[str, Dict[str, Any]] = {
    "photons": dict(kind="detector pulses", scan_only=False,
                    typical="a SPAD or hybrid detector's NIM pulse, about -0.4 to -0.8 V "
                            "into 50 ohm: falling edge (negative channel), negative level"),
    "laser_sync": dict(kind="laser sync", scan_only=False,
                       typical="a laser's sync output, often NIM (negative) or a 0.5-1 V "
                               "positive pulse; a photodiode sync can be under 0.1 V"),
    "line_clock": dict(kind="DAQ clock", scan_only=True,
                       typical="an NI-DAQ line, 1-1.5 V into 50 ohm, rising edge: "
                               "about +0.5 V, never above 1 V", default_v=0.5),
    "frame_clock": dict(kind="DAQ clock", scan_only=True,
                        typical="an NI-DAQ line, 1-1.5 V into 50 ohm, rising edge: "
                                "about +0.5 V, never above 1 V", default_v=0.5),
    "sted_pulse": dict(kind="photodiode", scan_only=False,
                       typical="a fast photodiode on a pulsed beam, often small "
                               "(tens to hundreds of mV)"),
}
GENERIC_KIND = dict(kind="TTL or pulse source", scan_only=False,
                    typical="the sign of the channel number says the polarity: "
                            "negative channel, negative pulses")

#: Verdicts of ``find_trigger_level``, with what to do about each.
LEVEL_VERDICTS: Dict[str, str] = {
    "good": "a wide plateau: the level in its middle is safe; apply it",
    "too_small": ("the pulses barely clear the noise: no level is safe -- attenuate "
                  "less, amplify, or shorten the cable; the middle of the narrow "
                  "plateau is the best there is"),
    "wrong_polarity": ("the pulses have the other sign: flip the sign of the channel "
                       "number in the timeTagger block (and the level with it)"),
    "noise_only": ("counts only next to 0 V: that is noise (nothing cabled, a source "
                   "that is off) or pulses smaller than ~0.1 V"),
    "no_signal": ("no counts at any level of either sign: check the cable, the "
                  "input number and that the source is on"),
    "needs_scan": ("this input carries a scan clock, which fires only while a scan "
                   "runs: run a scan with the time-resolved detector disabled "
                   "(enabled = False) and Repeat on, and find again; until then "
                   "keep the default level"),
}


def signal_kind(role: Optional[str]) -> Dict[str, Any]:
    """The hints for the input a role is configured on (generic without)."""
    return SIGNAL_KINDS.get(role or "", GENERIC_KIND)


def _input_label(channel: int, role: Optional[str]) -> str:
    return f"{role} (input {channel:+d})" if role else f"input {channel:+d}"


@dataclass
class LevelResult:
    """What ``find_trigger_level`` found on one input."""

    input: str
    """ The role and input, or ``"input N"`` for an input without a role. """
    channel: int
    role: Optional[str]
    verdict: str
    """ A key of ``LEVEL_VERDICTS``. """
    level_v: Optional[float]
    """ The level to use (the middle of the plateau), signed; ``None`` when
    there is none to recommend. """
    plateau_v: Optional[tuple]
    """ ``(low, high)`` magnitudes of the plateau, volts. """
    width_v: float
    margin_v: float
    """ From the level down to the plateau's low edge, where the counts
    start to depart towards the noise. """
    rate_hz: float
    """ The count rate on the plateau. """
    original_v: float
    applied: bool
    levels_v: List[float] = field(default_factory=list)
    rates_hz: List[float] = field(default_factory=list)
    reason: str = ""
    """ Why this verdict, in words; ``action`` says what to do. """

    @property
    def action(self) -> str:
        return LEVEL_VERDICTS.get(self.verdict, "")

    @property
    def field_name(self) -> Optional[str]:
        """The ``timeTagger`` block field that keeps this level, if any."""
        fields = ROLE_FIELDS.get(self.role or "")
        return fields[1] if fields else None

    def block_line(self) -> Optional[str]:
        """The exact line for the setup file's ``timeTagger`` block, e.g.
        ``"photonsTriggerV": -0.27`` (``None`` for an input without a role
        or without a level)."""
        name = self.field_name
        if name is None or self.level_v is None:
            return None
        return f'"{name}": {self.level_v:.3f}'

    def summary(self) -> str:
        head = f"{self.input}: {self.verdict}"
        if self.level_v is not None:
            head += f", level {self.level_v:+.3f} V"
        if self.plateau_v is not None:
            head += (f" (plateau {self.plateau_v[0]:.3f}-{self.plateau_v[1]:.3f} V, "
                     f"width {self.width_v:.3f} V, {self.margin_v:.3f} V above its low "
                     f"edge, {self.rate_hz:,.0f} Hz)")
        text = head + (f". {self.reason}" if self.reason else "") + f" -- {self.action}"
        if self.applied:
            text += f" [applied; was {self.original_v:+.3f} V]"
        else:
            text += f" [level left at {self.original_v:+.3f} V]"
        return text

    def to_dict(self) -> Dict[str, Any]:
        out = {k: getattr(self, k) for k in self.__dataclass_fields__}
        out["action"] = self.action
        out["block_line"] = self.block_line()
        return out


def _plateau(magnitudes: Sequence[float], rates: Sequence[float],
             duration_s: float) -> Optional[tuple]:
    """The widest run of consecutive levels (by volts) that count the same
    rate, above the noise floor: ``(first index, last index)`` or None.

    "The same" is within 10 % or three Poisson sigmas of the run's first
    level, whichever is wider; a level needs ``MIN_LEVEL_COUNTS`` counts.
    """
    best = None
    n = len(magnitudes)
    for i in range(n):
        ref = rates[i]
        if magnitudes[i] < NOISE_FLOOR_V - 1e-9 or ref * duration_s < MIN_LEVEL_COUNTS:
            continue
        # Poisson noise widens the band at low counts, never past half the rate.
        tol = min(0.5 * ref, max(0.1 * ref, 3.0 * math.sqrt(ref * duration_s) / duration_s))
        j = i
        while (j + 1 < n and abs(rates[j + 1] - ref) <= tol
               and rates[j + 1] * duration_s >= MIN_LEVEL_COUNTS):
            j += 1
        if j > i:
            span = magnitudes[j] - magnitudes[i]
            if best is None or span > magnitudes[best[1]] - magnitudes[best[0]]:
                best = (i, j)
    return best


def _bump(magnitudes: Sequence[float], rates: Sequence[float], duration_s: float) -> bool:
    """Whether the counts peak *above* the lowest level: a pulse that clears
    the comparator's floor. Noise falls monotonically from 0 V instead."""
    if not len(rates):
        return False
    peak = int(np.argmax(rates))
    return peak > 0 and rates[peak] * duration_s >= MIN_LEVEL_COUNTS \
        and rates[peak] > 1.5 * rates[0]


def level_warnings(manager, input, level_v: float, *, rate_hz: Optional[float] = None,
                   scan_running: Optional[bool] = None) -> List[str]:
    """What is wrong with ``level_v`` on an input, without touching the card.

    For any input; a role adds its hints (a scan clock's reach, that it is
    silent between scans). ``rate_hz`` is what the input counts now, if
    known; a scan clock's 0 Hz is flagged only when ``scan_running``.
    """
    channel, role = manager.resolveInput(input)
    kind = signal_kind(role)
    level = float(level_v)
    out: List[str] = []
    if abs(level) > INPUT_RANGE_V:
        out.append(f"{level:+.3f} V is outside the input range (+/-{INPUT_RANGE_V:g} V)")
    if level != 0.0 and (level < 0) != (channel < 0):
        out.append(
            f"the level is {'negative' if level < 0 else 'positive'} but the channel "
            f"({channel:+d}) triggers on the {'falling' if channel < 0 else 'rising'} edge: "
            "the sign of the channel says the pulses' polarity")
    if abs(level) < NOISE_FLOOR_V + SAFE_MARGIN_V:
        out.append(f"{level:+.3f} V is inside the comparator's noise "
                   f"(below {NOISE_FLOOR_V + SAFE_MARGIN_V:g} V): noise will count")
    if kind.get("scan_only") and abs(level) > DAQ_LINE_REACH_V:
        out.append(f"{level:+.3f} V is above what a DAQ line reliably reaches into 50 ohm "
                   f"(~1-1.5 V): the clock may never fire")
    if rate_hz is not None and rate_hz <= 0:
        if kind.get("scan_only"):
            if scan_running:
                out.append("0 Hz during a scan: the clock does not reach the card at this level")
        else:
            out.append("counts nothing: no signal, the wrong polarity, or a level above the pulses")
    return out


@dataclass
class ChannelReport:
    role: str
    channel: int
    edge: str
    trigger_v: float
    deadtime_ps: int
    delay_ps: int


# --------------------------------------------------------------------------- #
# The facade                                                                   #
# --------------------------------------------------------------------------- #


class TimeTaggerFacade:
    """See the module docstring. ``detectorsManager`` lets ``preflight``
    read the FLIM detector's own settings."""

    def __init__(self, manager: TimeTaggerManager, detectorsManager=None) -> None:
        if not isinstance(manager, TimeTaggerManager):
            raise TypeError("TimeTaggerFacade needs a TimeTaggerManager")
        self._manager = manager
        self._detectorsManager = detectorsManager

    # -- identity -------------------------------------------------------- #

    @property
    def manager(self) -> TimeTaggerManager:
        return self._manager

    @property
    def is_mock(self) -> bool:
        return self._manager.isMock

    @property
    def connected(self) -> bool:
        return self._manager.connected

    @property
    def model(self) -> str:
        return self._manager.model

    @property
    def serial(self) -> str:
        return self._manager.serial

    @property
    def tcspc_direction(self) -> str:
        return self._manager.tcspcDirection

    def roles(self) -> List[str]:
        return [role for role in ROLES if self._manager.hasRole(role)]

    def channels(self) -> Dict[str, ChannelReport]:
        return {
            role: ChannelReport(role, info.channel, info.edge, info.trigger_v,
                                info.deadtime_ps, info.delay_ps)
            for role, info in self._manager.channels().items()
        }

    def metadata(self) -> Dict[str, Any]:
        return self._manager.metadata()

    def health(self, integration_s: float = 0.2):
        return self._manager.health(integration_s)

    def overflows(self) -> int:
        return self._manager.overflows()

    # -- conditioning (refused while a scan holds the card) --------------- #

    def set_trigger_level(self, role: str, voltage: float,
                          owner: Optional[str] = None) -> None:
        """``owner`` is the calibration transaction's name when the write
        happens inside one (``with tt.calibration('x'): ...``)."""
        self._manager.setTriggerLevel(role, voltage, owner=owner)

    def set_delay(self, role: str, delay_ps: int, owner: Optional[str] = None) -> None:
        self._manager.setDelay(role, delay_ps, owner=owner)

    def set_deadtime(self, role: str, deadtime_ps: int,
                     owner: Optional[str] = None) -> None:
        self._manager.setDeadtime(role, deadtime_ps, owner=owner)

    def calibration(self, owner: str):
        """Own the card's conditioning for a calibration; see the manager."""
        return self._manager.calibrationTransaction(owner)

    def test_signal(self, roles: Iterable[str], enabled: bool,
                    owner: Optional[str] = None) -> None:
        """The card's built-in test signal on these roles' inputs.

        Refused while a scan holds the card or another calibration owns
        it (a test signal on the line clock would inject pixel markers
        into the scan). Prefer :meth:`test_signal_on`, which owns the
        card for the whole enable-measure-restore sequence.
        """
        self._manager.setTestSignal(list(roles), bool(enabled), owner=owner)

    @contextlib.contextmanager
    def test_signal_on(self, roles: Iterable[str]):
        """``with tt.test_signal_on(['line_clock']): ...``: the test signal
        on for the body, off again afterwards (also on an error or a
        cancellation), inside a calibration transaction so no scan can
        start on it and no other calibration can change the card."""
        roles = list(roles)
        owner = "test_signal"
        with self._manager.calibrationTransaction(owner):
            self._manager.setTestSignal(roles, True, owner=owner)
            try:
                yield self
            finally:
                self._manager.setTestSignal(roles, False, owner=owner)

    def set_mock_laser(self, on: bool) -> None:
        """Block or unblock the *mock* card's excitation. The mock cannot see
        the rig's lasers; on a real card this does nothing, and the laser is
        switched through ``api.imcontrol.setLaserActive``."""
        hook = getattr(self._manager._tagger, "set_laser_on", None) if self.is_mock else None
        if hook is not None:
            hook(bool(on))

    # -- measurements ---------------------------------------------------- #

    #: How long past its capture a measurement may take to finish before
    #: the facade gives up on it (the vendor's own default is unlimited).
    FINISH_MARGIN_S = 5.0
    #: One bounded wait on the card between cancellation checks.
    FINISH_POLL_MS = 100

    def _run(self, measurement, duration_s: float, after_start=None):
        """startFor + a cancellable wait; the object is stopped afterwards.
        ``after_start`` runs once the capture is running (to start the scan
        a measurement must span from its first edge).

        The wait is bounded at every step: the capture itself sleeps in
        cancellable slices, and the tail is polled with ``waitUntilFinished``
        calls of ``FINISH_POLL_MS`` so a Stop reaches the script while the
        card is still finishing, and ``FINISH_MARGIN_S`` past the capture
        is a ``TimeTaggerError`` rather than a worker blocked for good.
        """
        duration = max(0.001, float(duration_s))
        try:
            measurement.startFor(int(duration * 1e12))
            if after_start is not None:
                after_start()
            cancellableSleep(duration)
            deadline = time.monotonic() + self.FINISH_MARGIN_S
            while not measurement.waitUntilFinished(self.FINISH_POLL_MS):
                checkpoint()
                if time.monotonic() > deadline:
                    raise TimeTaggerError(
                        f"{type(measurement).__name__} did not finish within "
                        f"{self.FINISH_MARGIN_S:g} s of its {duration:g} s "
                        "capture; the card may have stalled."
                    )
            return measurement.getData()
        finally:
            try:
                measurement.stop()
            except Exception:
                pass

    def count_rates(self, roles: Optional[Sequence[str]] = None,
                    duration_s: float = 1.0) -> RatesResult:
        """Counts per second per role -- or per input: an entry may also be a
        channel number (any input, configured as a role or not)."""
        roles = list(roles) if roles is not None else self.roles()
        manager = self._manager
        channels = [manager.resolveInput(r)[0] for r in roles]
        rate = manager.api.Countrate(manager.tagger, channels)
        data = np.asarray(self._run(rate, duration_s), dtype=np.float64)
        return RatesResult({r: float(v) for r, v in zip(roles, data)}, float(duration_s))

    def dark_rates(self, roles: Sequence[str] = ("photons",),
                   duration_s: float = 2.0) -> RatesResult:
        """Count rates with the excitation blocked: dark counts plus the
        afterpulses of whatever still fires. Blocking the laser is the
        caller's job (``api.imcontrol.setLaserActive``; ``set_mock_laser``
        on the mock)."""
        return self.count_rates(roles, duration_s)

    def rep_rate(self, role: str = "laser_sync", duration_s: float = 2.0,
                 divider: int = 16, binwidth_ps: int = 10) -> RepRateResult:
        """The laser's repetition rate and period jitter, safely on any card.

        Inside a calibration transaction: the conditional filter is taken
        off (its output is not periodic) and the sync is divided by
        ``divider`` (so an 80 MHz sync is 5 M tags/s on a Time Tagger 20),
        the rate is counted and the period histogram taken, and both are
        restored in ``finally``. The jitter is over ``divider`` periods and
        is floored by the card's own two-tag jitter.
        """
        manager = self._manager
        divider = max(1, int(divider))
        with manager.calibrationTransaction("rep_rate"):
            tagger, api = manager.tagger, manager.api
            channel = manager.channel(role)
            # Snapshot what the card actually carries, and put exactly that
            # back: a divider or a filter someone set before is theirs.
            divider_before = int(tagger.getEventDivider(channel))
            filter_before = (list(tagger.getConditionalFilterTrigger()),
                             list(tagger.getConditionalFilterFiltered()))
            filter_on = bool(filter_before[0])
            try:
                if filter_on:
                    tagger.setConditionalFilter(trigger=[], filtered=[])
                tagger.setEventDivider(channel, divider)
                rate = api.Countrate(tagger, [channel])
                divided_hz = float(np.asarray(self._run(rate, duration_s))[0])
                rate_hz = divided_hz * divider
                if rate_hz <= 0:
                    raise TimeTaggerError(
                        f"No edges on {role}: check the cable and the trigger "
                        f"level ({manager.channelInfo(role).trigger_v:+.2f} V)."
                    )
                period_ps = 1e12 / rate_hz
                # The period histogram must hold the divided period with
                # room on both sides; coarsen the bins rather than exceed
                # 20 000 of them.
                span_ps = divider * period_ps * 1.5
                binwidth_ps = int(max(binwidth_ps, math.ceil(span_ps / 20000)))
                n_bins = int(max(64, math.ceil(span_ps / binwidth_ps)))
                diff = api.TimeDifferences(tagger, channel, channel,
                                           binwidth=binwidth_ps, n_bins=n_bins)
                hist = np.asarray(self._run(diff, duration_s), dtype=np.float64).reshape(-1)
            finally:
                try:
                    tagger.setEventDivider(channel, divider_before)
                finally:
                    if filter_on:
                        tagger.setConditionalFilter(
                            trigger=filter_before[0], filtered=filter_before[1],
                        )
        centres = (np.arange(hist.size) + 0.5) * binwidth_ps
        total = hist.sum()
        if total > 0:
            mean = (hist * centres).sum() / total
            jitter = math.sqrt(max(0.0, ((hist * (centres - mean) ** 2).sum() / total)))
        else:
            jitter = 0.0
        floor = _jitter_floor_ps(self.model)
        return RepRateResult(
            role=role, rate_hz=rate_hz, period_ps=period_ps, jitter_ps=jitter,
            divider=divider, n_periods=int(total), card_floor_ps=floor,
            card_limited=jitter <= 1.5 * floor, filter_was_on=filter_on,
        )

    def histogram(self, click_role: str = "photons", start_role: str = "laser_sync",
                  binwidth_ps: int = 32, n_bins: Optional[int] = None,
                  duration_s: float = 1.0, laser_rep_rate_mhz: float = 80.0,
                  owner: Optional[str] = None) -> HistogramResult:
        """A TCSPC histogram between two roles, returned in forward time.

        For the photon/sync pair the card's direction decides the slots: in
        reverse mode (conditional filter on) the photon starts and the sync
        stops, and the axis is mirrored on the laser period so the decay
        reads forward. ``n_bins`` defaults to one laser period.

        Measured at a *known* delay state: inside a calibration
        transaction (so it is refused while a scan holds the card) the
        click input is put to the card's configured delay for the
        measurement and restored afterwards. A forward-mode scan leaves
        ``-t0_ps`` on the photon input and reverse mode applies t0 in
        software, so without this the peak would depend on what ran last;
        with it the peak *is* the absolute ``t0_ps`` (tutorial 06). Pass
        ``owner`` when the caller already holds a calibration transaction.
        """
        manager = self._manager
        direction = "forward"
        pair = {click_role, start_role}
        if pair == {"photons", "laser_sync"}:
            direction = manager.tcspcDirection
            if direction == "reverse":
                click_role, start_role = "laser_sync", "photons"
        period_ps = 1e6 / max(1e-9, float(laser_rep_rate_mhz))
        if n_bins is None:
            n_bins = max(1, int(math.ceil(period_ps / binwidth_ps)))
        tagger, api = manager.tagger, manager.api
        photon_role = "photons" if "photons" in pair else click_role
        transaction = (contextlib.nullcontext() if owner is not None
                       else manager.calibrationTransaction("histogram"))
        with transaction:
            photon_ch = manager.channel(photon_role)
            delay_before = int(tagger.getInputDelay(photon_ch))
            delay_ps = int(manager.hardwareDelayPs(photon_role))
            try:
                if delay_before != delay_ps:
                    tagger.setInputDelay(photon_ch, delay_ps)
                hist = api.Histogram(tagger, manager.channel(click_role),
                                     manager.channel(start_role), binwidth_ps,
                                     int(n_bins))
                counts = np.asarray(self._run(hist, duration_s),
                                    dtype=np.float64).reshape(-1)
            finally:
                if delay_before != delay_ps:
                    tagger.setInputDelay(photon_ch, delay_before)
        raw_axis_ns = (np.arange(counts.size) + 0.5) * binwidth_ps * 1e-3
        cube, axis = orient_cube(counts[None, None, :], raw_axis_ns, direction,
                                 period_ns=period_ps * 1e-3)
        counts = cube[0, 0]
        peak = float(axis[int(np.argmax(counts))]) if counts.sum() > 0 else 0.0
        return HistogramResult(
            click_role="photons" if pair == {"photons", "laser_sync"} else click_role,
            start_role="laser_sync" if pair == {"photons", "laser_sync"} else start_role,
            direction=direction, t_axis_ns=np.asarray(axis), counts=counts,
            binwidth_ps=int(binwidth_ps), duration_s=float(duration_s),
            peak_ns=peak, fwhm_ns=_fwhm_ns(np.asarray(axis), counts),
            total=int(counts.sum()), photon_delay_ps=delay_ps,
        )

    # -- scan clocks (measured while a scan runs; no transaction) ---------- #

    def count_edges(self, role: str, duration_s: float = 1.0, start=None) -> int:
        """Edges on a role's input over ``duration_s``: for the line clock
        during a scan, the number of lines the card saw. ``start`` is called
        once the counter runs -- pass the scan's start, so the count spans
        the scan from its first edge (a counter started after the scan
        misses the first lines)."""
        manager = self._manager
        channel = manager.channel(role)
        window_ps = int(max(0.001, float(duration_s)) * 1e12)
        counter = manager.api.Counter(manager.tagger, [channel], binwidth=window_ps, n_values=1)
        data = np.asarray(self._run(counter, duration_s, after_start=start))
        return int(round(float(np.asarray(data).reshape(-1)[0]))) if data.size else 0

    def period(self, role: str, duration_s: float = 1.0,
               expected_period_ps: Optional[float] = None,
               binwidth_ps: Optional[int] = None) -> PeriodResult:
        """A scan clock's rate and period, from its edge-to-edge histogram.

        ``expected_period_ps`` sizes the histogram (1.5 x it); without it
        the rate is counted first and the period taken from that. Not for
        the laser sync: ``rep_rate`` divides and unfilters it.
        """
        manager = self._manager
        tagger, api = manager.tagger, manager.api
        channel = manager.channel(role)
        if expected_period_ps is None:
            rate = api.Countrate(tagger, [channel])
            rate_hz = float(np.asarray(self._run(rate, min(duration_s, 0.5)))[0])
            if rate_hz <= 0:
                return PeriodResult(role, 0.0, 0.0, 0.0, 0, float(duration_s))
            expected_period_ps = 1e12 / rate_hz
        span_ps = float(expected_period_ps) * 1.5
        binwidth_ps = int(max(binwidth_ps or 1, math.ceil(span_ps / 20000)))
        n_bins = int(max(64, math.ceil(span_ps / binwidth_ps)))
        diff = api.TimeDifferences(tagger, channel, channel, binwidth=binwidth_ps, n_bins=n_bins)
        hist = np.asarray(self._run(diff, duration_s), dtype=np.float64).reshape(-1)
        total = hist.sum()
        if total <= 0:
            return PeriodResult(role, 0.0, 0.0, 0.0, 0, float(duration_s))
        centres = (np.arange(hist.size) + 0.5) * binwidth_ps
        mean = float((hist * centres).sum() / total)
        jitter = math.sqrt(max(0.0, float((hist * (centres - mean) ** 2).sum() / total)))
        return PeriodResult(role, 1e12 / mean, mean, jitter, int(total), float(duration_s))

    def skew(self, from_role: str = "frame_clock", to_role: str = "line_clock",
             duration_s: float = 1.0, added_delay_ps: int = 50_000,
             binwidth_ps: int = 100, start=None) -> SkewResult:
        """The signed time from each ``from_role`` edge to the next
        ``to_role`` edge, with both orders visible.

        A histogram only sees a click *after* its start, so ``to_role`` is
        delayed by a known ``added_delay_ps`` on the card for the
        measurement and that delay subtracted from the peak: a ``to_role``
        edge up to ``added_delay_ps`` *before* the ``from_role`` edge then
        reads as a negative skew instead of vanishing. Runs inside a
        calibration transaction (the delay is a conditioning write), so a
        scan must not hold the card: run the scan with the FLIM detector
        disabled (tutorial 08). The configured delay is restored. ``start``
        runs once the histogram is collecting (pass the scan's start: the
        frame edge comes once, at the scan's beginning).
        """
        manager = self._manager
        tagger, api = manager.tagger, manager.api
        added = int(max(1, added_delay_ps))
        window_ps = 2 * added
        n_bins = int(math.ceil(window_ps / binwidth_ps))
        with manager.calibrationTransaction("skew"):
            to_ch, from_ch = manager.channel(to_role), manager.channel(from_role)
            base = int(manager.hardwareDelayPs(to_role))
            before = int(tagger.getInputDelay(to_ch))
            try:
                tagger.setInputDelay(to_ch, base + added)
                hist = api.Histogram(tagger, to_ch, from_ch, binwidth_ps, n_bins)
                counts = np.asarray(self._run(hist, duration_s, after_start=start),
                                    dtype=np.float64).reshape(-1)
            finally:
                tagger.setInputDelay(to_ch, before)
        total = counts.sum()
        if total <= 0:
            skew_ps = 0.0
        else:
            centres = (np.arange(counts.size) + 0.5) * binwidth_ps
            skew_ps = float(centres[int(np.argmax(counts))]) - added
        offset = int(manager.patternOffsetPs("line_clock")) if manager.hasRole("line_clock") else 0
        return SkewResult(from_role, to_role, skew_ps, added, int(total), offset)

    def scope(self, roles: Sequence[str], trigger_role: str, window_ps: int,
              duration_s: float = 1.0, extra_channels: Optional[Dict[str, int]] = None,
              detector_name: Optional[str] = None, start=None,
              prepare=None) -> Dict[str, List[tuple]]:
        """An oscilloscope-like trace: the edges on each role within
        ``window_ps`` after the first edge on ``trigger_role``, as
        ``(time_ps, 'rising' | 'falling')`` per role. ``extra_channels``
        adds virtual channels under a name; ``detector_name`` adds the FLIM
        detector's pixel markers of the scan it has prepared (none when it
        is disabled). A plain measurement: allowed during a scan hold.
        The markers exist only once the scan is *prepared*, so ``prepare``
        (a callable that requests the scan and returns when it is built, not
        yet running) is called before the capture is set up, and ``start``
        (returning when the clocks run) after it is armed; tutorial 08 passes
        ``run.prepare`` and ``run.start``."""
        manager = self._manager
        if prepare is not None:
            prepare()
        names = list(roles)
        channels = [manager.channel(r) for r in names]
        extra = dict(extra_channels or {})
        if detector_name is not None and self._detectorsManager is not None:
            detector = self._detectorsManager[detector_name]
            markers = getattr(detector, "pixel_marker_channels", None)
            if markers is not None:
                extra.update(markers())
        for name, channel in extra.items():
            names.append(str(name))
            channels.append(int(channel))
        trace = manager.api.Scope(manager.tagger, channels, manager.channel(trigger_role),
                                  int(window_ps))
        data = self._run(trace, duration_s, after_start=start)
        # The vendor's event state is an enum whose LOW is not zero, so it
        # is compared with HIGH, not tested as a boolean (every edge would
        # read "rising"); the mock's state is a plain bool.
        high = getattr(getattr(manager.api, "State", None), "HIGH", None)

        def is_high(state) -> bool:
            return bool(state == high) if high is not None else bool(state)

        out: Dict[str, List[tuple]] = {}
        for name, events in zip(names, data):
            out[name] = [(int(e.time), "rising" if is_high(e.state) else "falling") for e in events]
        return out

    def pattern_offset_ps(self, role: str = "line_clock") -> int:
        """Where the FLIM detector starts its pixel markers after an edge of
        ``role`` (``pixelPatternOffsetPs`` plus a positive line delay)."""
        return int(self._manager.patternOffsetPs(role))

    # -- mock-only hooks (no-ops on a real card) ---------------------------- #

    def mock_truth(self, ny: int, nx: int):
        """The *mock* sample's ``(rate_map_hz, lifetime_map_ns)`` on an
        ``ny`` x ``nx`` grid, or ``None`` on a real card (whose truth is the
        APD image)."""
        hook = getattr(self._manager._tagger, "sample_truth", None) if self.is_mock else None
        return hook(ny, nx) if hook is not None else None

    def set_mock_fault(self, name: str, value) -> None:
        """Inject (or clear, with ``None``) a *mock* fault at run time, for
        example ``line_delay_ps``; nothing on a real card."""
        hook = getattr(self._manager._tagger, "set_fault", None) if self.is_mock else None
        if hook is not None:
            hook(name, value)

    def sted_pulse_delay(self, duration_s: float = 1.0, binwidth_ps: int = 16,
                         laser_rep_rate_mhz: float = 80.0,
                         owner: Optional[str] = None) -> HistogramResult:
        """Where the STED pulse sits after the laser sync: the histogram of
        the ``sted_pulse`` photodiode against the sync, so its ``peak_ns``
        is on the same absolute axis as :meth:`histogram`'s IRF peak (the
        STED-to-excitation delay is the difference). Needs the optional
        ``sted_pulse`` role."""
        if not self._manager.hasRole("sted_pulse"):
            raise TimeTaggerError("No sted_pulse role: cable a photodiode on the STED beam "
                                  "and set stedPulseChannel in the timeTagger block.")
        return self.histogram("sted_pulse", "laser_sync", binwidth_ps=binwidth_ps,
                              duration_s=duration_s, laser_rep_rate_mhz=laser_rep_rate_mhz,
                              owner=owner)

    def trigger_sweep(self, role: str, levels_v: Sequence[float],
                      duration_s: float = 0.2) -> SweepResult:
        """Count rate at each trigger level; the original level is restored.

        Inside a calibration transaction, so a scan cannot start on a
        sweep's temporary level. Pass levels of the input's polarity: a
        SPAD's NIM pulse is negative, so negative levels.
        """
        manager = self._manager
        original = manager.channelInfo(role).trigger_v
        levels = [float(v) for v in levels_v]
        rates: List[float] = []
        with manager.calibrationTransaction("trigger_sweep"):
            try:
                for level in levels:
                    manager.setTriggerLevel(role, level, owner="trigger_sweep")
                    rates.append(self.count_rates([role], duration_s).rates_hz[role])
            finally:
                manager.setTriggerLevel(role, original, owner="trigger_sweep")
        plateau = None
        if rates and max(rates) > 0:
            top = [v for v, r in zip(levels, rates) if r >= 0.9 * max(rates)]
            plateau = float(np.median(top))
        return SweepResult(role, levels, rates, plateau, original)

    def find_trigger_level(self, input, duration_s: float = 0.1, *,
                           apply: bool = False) -> LevelResult:
        """Find a good trigger level for one input, without choosing a range.

        ``input`` is a role (``"photons"``) or a channel number -- any input,
        ``-5`` for a second detector on input 5's falling edge. The sign of
        the channel says the pulses' polarity; the levels of that sign are
        swept coarsely from 20 mV to the input's 2.5 V, and when nothing
        but noise counts there, the other sign is probed. Around the widest
        plateau -- the levels that count the same rate -- the edges are
        swept finely, and the level is the plateau's middle: as far from
        the noise below as from the pulses' top above.

        Returns a ``LevelResult`` with the verdict (``LEVEL_VERDICTS``), the
        plateau, its width and the margin. Runs inside a calibration
        transaction (refused while a scan holds the card), and puts the
        input's level back afterwards -- also on an error or a Stop --
        unless ``apply`` is set and the verdict is ``good``. ``duration_s``
        is per level, for 20 to 40 levels. A scan clock counts only while
        a scan runs; between scans it reads ``needs_scan``.
        """
        manager = self._manager
        channel, role = manager.resolveInput(input)
        kind = signal_kind(role)
        sign = -1.0 if channel < 0 else 1.0
        duration = max(0.001, float(duration_s))
        owner = "find_trigger_level"
        measured: Dict[float, float] = {}

        def measure(levels):
            for level in levels:
                level = round(float(level), 4)
                if level in measured:
                    continue
                checkpoint()
                manager.setInputTriggerLevel(channel, level, owner=owner)
                counter = manager.api.Countrate(manager.tagger, [channel])
                measured[level] = float(np.asarray(self._run(counter, duration))[0])

        def side(polarity):
            pairs = sorted((abs(v), r) for v, r in measured.items()
                           if v != 0 and (v < 0) == (polarity < 0))
            return [m for m, _ in pairs], [r for _, r in pairs]

        def signal(mags, rates):
            return _plateau(mags, rates, duration) is not None or _bump(mags, rates, duration)

        original = float(manager.triggerLevelOf(channel))
        applied = False
        with manager.calibrationTransaction(owner):
            try:
                measure([sign * m for m in COARSE_LEVELS_V])
                polarity = sign
                mags, rates = side(sign)
                if not signal(mags, rates):
                    measure([-sign * m for m in COARSE_LEVELS_V])
                    other = side(-sign)
                    if signal(*other):
                        polarity = -sign
                        mags, rates = other
                run = _plateau(mags, rates, duration)
                if run is not None:
                    # Place both edges between the coarse levels around them.
                    i, j = run
                    fine = []
                    if i > 0:
                        fine += list(np.linspace(mags[i - 1], mags[i], FINE_STEPS + 2)[1:-1])
                    if j + 1 < len(mags):
                        fine += list(np.linspace(mags[j], mags[j + 1], FINE_STEPS + 2)[1:-1])
                    measure([polarity * m for m in fine])
                    mags, rates = side(polarity)
                    run = _plateau(mags, rates, duration)
                result = self._judge_levels(channel, role, kind, sign, polarity, mags, rates,
                                            run, duration, original)
                if apply and result.verdict == "good" and result.level_v is not None:
                    manager.setInputTriggerLevel(channel, result.level_v, owner=owner)
                    applied = True
            finally:
                if not applied:
                    manager.setInputTriggerLevel(channel, original, owner=owner)
        result.applied = applied
        result.levels_v = sorted(measured)
        result.rates_hz = [measured[v] for v in result.levels_v]
        return result

    @staticmethod
    def _judge_levels(channel, role, kind, sign, polarity, mags, rates, run,
                      duration, original) -> LevelResult:
        label = _input_label(channel, role)
        counting = [m for m, r in zip(mags, rates) if r * duration >= MIN_LEVEL_COUNTS]

        def result(verdict, level=None, plateau=None, width=0.0, margin=0.0, rate=0.0,
                   reason=""):
            return LevelResult(label, channel, role, verdict, level, plateau, width, margin,
                               rate, original, False, reason=reason)

        if run is None:
            if not counting:
                if kind.get("scan_only"):
                    default = float(kind.get("default_v", 0.5)) * sign
                    return result("needs_scan", level=default,
                                  reason=f"no edges at any level now; {kind['typical']}")
                return result("no_signal", reason="nothing counted at any level, either sign")
            if not _bump(mags, rates, duration):
                return result("noise_only", reason=(
                    f"counts fall from {rates[0]:,.0f} Hz at {mags[0]:.2f} V towards "
                    f"larger levels, without a plateau (nothing counts above "
                    f"{max(counting):.2f} V)"))
            # A signal, but no two levels agree: a pulse barely above noise.
            peak = int(np.argmax(rates))
            verdict = "wrong_polarity" if polarity != sign else "too_small"
            return result(verdict, level=polarity * mags[peak], rate=rates[peak], reason=(
                f"counts peak at {polarity * mags[peak]:+.2f} V without a plateau"))
        i, j = run
        lo, hi = float(mags[i]), float(mags[j])
        middle = 0.5 * (lo + hi)
        width = hi - lo
        rate = float(np.median(rates[i:j + 1]))
        common = dict(level=round(polarity * middle, 3), plateau=(lo, hi), width=width,
                      margin=middle - lo, rate=rate)
        if polarity != sign:
            return result("wrong_polarity", reason=(
                f"the {'negative' if polarity < 0 else 'positive'} levels count "
                f"{rate:,.0f} Hz, the {'positive' if polarity < 0 else 'negative'} ones "
                f"nothing; use channel {-channel:+d}"), **common)
        if width < MIN_PLATEAU_WIDTH_V or middle < NOISE_FLOOR_V + SAFE_MARGIN_V:
            return result("too_small", reason=(
                f"the plateau is {width * 1000:.0f} mV wide (a safe one is at least "
                f"{MIN_PLATEAU_WIDTH_V * 1000:.0f} mV)"), **common)
        return result("good", reason=f"pulses reach about {hi:.2f} V", **common)

    # -- preflight ------------------------------------------------------- #

    def preflight(self, detector_name: Optional[str] = None, duration_s: float = 1.0):
        """The checklist the Signals panel shows; see ``timeresolved.preflight``."""
        from imswitch.imcontrol.model.timeresolved.preflight import run_device_preflight

        detector = None
        if detector_name is not None and self._detectorsManager is not None:
            detector = self._detectorsManager[detector_name]
        return run_device_preflight(self, detector, duration_s=duration_s)


__all__ = [
    "LEVEL_VERDICTS",
    "LevelResult",
    "SIGNAL_KINDS",
    "level_warnings",
    "signal_kind",
    "ChannelReport",
    "HistogramResult",
    "PeriodResult",
    "SkewResult",
    "JITTER_FLOOR_PS",
    "RatesResult",
    "RepRateResult",
    "SweepResult",
    "TimeTaggerBusyError",
    "TimeTaggerError",
    "TimeTaggerFacade",
]
