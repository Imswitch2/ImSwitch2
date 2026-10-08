"""In-process stand-in for the Swabian ``TimeTagger`` Python module.

``MockTimeTaggerApi`` quacks like the vendor module (``createTimeTagger``,
``freeTimeTagger``, the measurement classes) and ``MockTimeTagger`` like the
card it returns, for the subset ImSwitch uses. Behind them sits a
``SignalModel`` that says what every input *sees*, in two regimes:

* **parked beam** -- no scan running: a photon rate with one lifetime, an
  IRF, dark counts and afterpulsing, the laser sync at its rep rate. This is
  what the device-level tutorials (trigger levels, dark counts, rep rate,
  bandwidth, IRF) measure.
* **scanning** -- line and frame edges loaded from the scan designer's own
  TTL arrays (``load_scan_edges``), and a synthetic sample (``MockSample``:
  a photon-rate map and a lifetime map) from which ``Flim`` draws a Poisson
  TCSPC cube, pixel by pixel.

Everything is analytic or sampled from a seeded generator when a measurement
is read: no thread, no wall clock, so results are exact and tests are
deterministic. Trigger levels, edge signs, dead times, the card's tag budget
(overflows) and the conditional filter (which reverses the TCSPC direction)
are modelled because the tutorials teach exactly those; ``faults`` lets a
tutorial break the signal on purpose.
"""

from __future__ import annotations

import math
import threading
import time
import weakref
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional

import numpy as np
from scipy.special import erfc

#: The card's own test signal, roughly what a Time Tagger 20 produces.
TEST_SIGNAL_RATE_HZ = 850_000.0

CHANNEL_UNUSED = -134217728  # TimeTagger.CHANNEL_UNUSED

#: USB tag budgets per card model, tags per second (round numbers from the
#: vendor's specifications; the Ultra depends on the host).
TAG_BUDGET_PER_MODEL = {
    "Time Tagger 20": 8.5e6,
    "Time Tagger Ultra": 65e6,
    "Time Tagger X": 1.0e9,
}


# --------------------------------------------------------------------------- #
# The physics                                                                  #
# --------------------------------------------------------------------------- #


def decay_shape(t_ns: np.ndarray, lifetime_ns, t0_ns: float, irf_fwhm_ns: float,
                period_ns: float, n_wraps: int = 3) -> np.ndarray:
    """A mono-exponential decay through a Gaussian IRF, wrapped on the period.

    The exponentially modified Gaussian, evaluated at the bin centres ``t_ns``
    (forward time) for a lifetime (scalar or array broadcastable against the
    leading axes), with the tails of the previous ``n_wraps`` periods folded
    in: at 80 MHz a 4 ns decay is still 4 % of its peak when the next pulse
    arrives. Normalised to unit sum over the bins it is given.
    """

    tau = np.asarray(lifetime_ns, dtype=np.float64)[..., None]
    sigma = max(1e-6, float(irf_fwhm_ns) / 2.354820045)
    t = np.asarray(t_ns, dtype=np.float64)
    total = np.zeros(np.broadcast_shapes(tau.shape, t.shape), dtype=np.float64)
    for wrap in range(n_wraps + 1):
        x = t + wrap * float(period_ns) - float(t0_ns)
        arg = (sigma / tau - x / sigma) / math.sqrt(2.0)
        with np.errstate(over="ignore", invalid="ignore"):
            emg = (0.5 / tau) * np.exp(0.5 * (sigma / tau) ** 2 - x / tau) * erfc(arg)
        total += np.where(np.isfinite(emg), emg, 0.0)
    norm = total.sum(axis=-1, keepdims=True)
    return np.where(norm > 0, total / np.maximum(norm, 1e-300), 0.0)


@dataclass
class MockSample:
    """What the parked beam, or each scan pixel, is looking at."""

    name: str
    rate_map: Callable[[int, int], np.ndarray]
    """ ``(ny, nx) -> detected photon rate per pixel in Hz``. """
    lifetime_map: Callable[[int, int], np.ndarray]
    """ ``(ny, nx) -> lifetime per pixel in ns``. """
    parked_rate_hz: float = 500e3
    parked_lifetime_ns: float = 2.5


def _uniform_sample() -> MockSample:
    return MockSample(
        "uniform",
        lambda ny, nx: np.full((ny, nx), 1.0e6),
        lambda ny, nx: np.full((ny, nx), 2.5),
    )


def _gradient_sample() -> MockSample:
    return MockSample(
        "gradient",
        lambda ny, nx: np.full((ny, nx), 1.0e6),
        lambda ny, nx: np.tile(np.linspace(1.0, 5.0, max(1, nx)), (ny, 1)),
    )


def _beads_sample() -> MockSample:
    """Two bead populations (1.5 ns and 4.0 ns) on a dim background."""

    def _maps(ny, nx):
        rng = np.random.default_rng(12345)
        yy, xx = np.mgrid[0:ny, 0:nx]
        rate = np.full((ny, nx), 50e3)
        tau = np.full((ny, nx), 3.0)
        n_beads = max(2, (ny * nx) // 200)
        sigma = max(1.0, min(ny, nx) / 24)
        for k in range(n_beads):
            cy, cx = rng.uniform(0, ny), rng.uniform(0, nx)
            blob = np.exp(-((yy - cy) ** 2 + (xx - cx) ** 2) / (2 * sigma ** 2))
            rate += 3.0e6 * blob
            tau = np.where(blob > 0.3, 1.5 if k % 2 == 0 else 4.0, tau)
        return rate, tau

    cache: Dict[tuple, tuple] = {}

    def _rate(ny, nx):
        if (ny, nx) not in cache:
            cache[(ny, nx)] = _maps(ny, nx)
        return cache[(ny, nx)][0]

    def _tau(ny, nx):
        if (ny, nx) not in cache:
            cache[(ny, nx)] = _maps(ny, nx)
        return cache[(ny, nx)][1]

    return MockSample("beads_two_lifetimes", _rate, _tau)


SAMPLE_PRESETS: Dict[str, Callable[[], MockSample]] = {
    "uniform": _uniform_sample,
    "gradient": _gradient_sample,
    "beads_two_lifetimes": _beads_sample,
}


@dataclass
class SignalModel:
    """What the card's inputs see. Channels are physical input numbers."""

    photon_channel: int = 1
    sync_channel: int = 2
    line_channel: int = 3
    frame_channel: Optional[int] = None
    sample: MockSample = field(default_factory=_beads_sample)
    rep_rate_hz: float = 80e6
    sync_jitter_ps: float = 20.0
    irf_fwhm_ps: float = 350.0
    t0_ps: float = 1000.0
    """ Where the IRF peak sits after the sync, forward time. """
    dark_rate_hz: float = 2000.0
    afterpulse_fraction: float = 0.02
    """ Afterpulses as a fraction of detected photons, flat over the period. """
    pulse_amplitude_v: Dict[int, float] = field(default_factory=dict)
    """ Per input: a SPAD's NIM pulse is about -0.5 V, a DAQ line about
    +1.2 V into 50 ohm. Defaults are filled in per role. """
    laser_on: bool = True
    """ The excitation laser: off, the parked photon rate is dark counts only
    (what tutorial 03 measures). The mock cannot see the rig's lasers, so a
    tutorial sets this through the facade. """
    model: str = "Time Tagger X"
    """ Sets the USB tag budget (``TAG_BUDGET_PER_MODEL``). The default is
    the card that takes an unfiltered 80 MHz sync without overflowing, so
    the shipped mock setup images cleanly in forward mode; the bandwidth
    tutorial switches to a Time Tagger 20 through the ``model`` fault. """
    seed: int = 0
    faults: Dict[str, Any] = field(default_factory=dict)
    """ ``missing_line_clock``, ``line_delay_ps`` (how *late* the line clock
    reaches the card against the beam's position, in ps; negative = early,
    as with a galvo lagging its command -- ``lineClockDelayPs`` cancels it
    with the opposite sign), ``wrong_sync_polarity``, ``dead_photons``,
    ``laser_rep_rate_mhz`` (an override), ``model`` (a card model from
    ``TAG_BUDGET_PER_MODEL``, for its tag budget). The first four are read
    live (``set_fault``); the last two at construction. """

    def __post_init__(self):
        amps = {
            abs(self.photon_channel): -0.5,
            abs(self.sync_channel): 0.8,
            abs(self.line_channel): 1.2,
        }
        if self.frame_channel is not None:
            amps[abs(self.frame_channel)] = 1.2
        for ch, amp in amps.items():
            self.pulse_amplitude_v.setdefault(ch, amp)
        if self.faults.get("wrong_sync_polarity"):
            self.pulse_amplitude_v[abs(self.sync_channel)] = -0.8
        override = self.faults.get("laser_rep_rate_mhz")
        if override:
            self.rep_rate_hz = float(override) * 1e6
        model = self.faults.get("model")
        if model:
            self.model = str(model)

    @property
    def period_ps(self) -> float:
        return 1e12 / self.rep_rate_hz

    @property
    def tag_budget(self) -> float:
        return TAG_BUDGET_PER_MODEL.get(self.model, 8.5e6)

    @classmethod
    def from_info(cls, info) -> "SignalModel":
        """Build from a ``TimeTaggerInfo`` (roles, sample, faults)."""
        preset = SAMPLE_PRESETS.get(getattr(info, "mockSample", "") or "", _beads_sample)
        return cls(
            photon_channel=int(info.photonsChannel),
            sync_channel=int(info.laserSyncChannel),
            line_channel=int(info.lineClockChannel),
            frame_channel=(int(info.frameClockChannel)
                           if info.frameClockChannel is not None else None),
            sample=preset(),
            faults=dict(getattr(info, "mockFaults", {}) or {}),
        )


# --------------------------------------------------------------------------- #
# The card                                                                     #
# --------------------------------------------------------------------------- #


class MockTimeTagger:
    """The card object."""

    def __init__(self, serial: Optional[str] = None,
                 model: Optional[SignalModel] = None,
                 rates_hz: Optional[Dict[int, float]] = None):
        self._serial = serial or "MOCK-000001"
        self._model = model or SignalModel()
        #: Explicit per-input rates override the signal model (test hook).
        self._rates: Dict[int, float] = {int(k): float(v)
                                         for k, v in (rates_hz or {}).items()}
        self._triggerLevels: Dict[int, float] = {}
        self._inputDelays: Dict[int, int] = {}
        self._deadtimes: Dict[int, int] = {}
        self._testSignal: Dict[int, bool] = {}
        self._conditionalFilter: tuple = ([], [])
        self._eventDividers: Dict[int, int] = {}
        self._overflows = 0
        self._freed = False
        self._lock = threading.Lock()
        self._rng = np.random.default_rng(self._model.seed)
        #: Scan edges by physical input, picoseconds, and the scan's duration.
        self._edges: Dict[int, np.ndarray] = {}
        self._scan_duration_s = 0.0
        self._scan_active = False
        #: Wall-clock moment the scan's clocks started (``start_scan``), so a
        #: measurement sees the edges that fall into *its* window, as the
        #: card would; ``None`` = unknown, every scan input then counts the
        #: scan's average rate while it is active.
        self._scan_started_at: Optional[float] = None
        #: Virtual channel id -> EventGenerator, for Flim to find its markers.
        self._generators: Dict[int, "EventGenerator"] = {}
        #: Every measurement object created against this card.
        # Weak, like the vendor library: a measurement lives as long as its
        # Python object, so a dropped Flim (and its frame) is freed, not
        # kept with every previous scan's cube.
        self.measurements: "weakref.WeakSet[object]" = weakref.WeakSet()

    # -- identity -------------------------------------------------------- #

    def getSerial(self):
        return self._serial

    def getModel(self):
        return f"{self._model.model} (mock)"

    def getConfiguration(self):
        return {"serial": self._serial, "model": self.getModel(), "mock": True}

    @property
    def signal_model(self) -> SignalModel:
        return self._model

    # -- conditioning ---------------------------------------------------- #

    def setTriggerLevel(self, channel, voltage):
        self._triggerLevels[abs(int(channel))] = float(voltage)

    def getTriggerLevel(self, channel):
        return self._triggerLevels.get(abs(int(channel)), 0.5)

    def setInputDelay(self, channel, delay_ps):
        self._inputDelays[int(channel)] = int(delay_ps)

    def getInputDelay(self, channel):
        return self._inputDelays.get(int(channel), 0)

    def setDeadtime(self, channel, deadtime_ps):
        self._deadtimes[int(channel)] = int(deadtime_ps)

    def getDeadtime(self, channel):
        return self._deadtimes.get(int(channel), 0)

    def setTestSignal(self, channels, enabled):
        if isinstance(channels, int):
            channels = [channels]
        for channel in channels:
            self._testSignal[abs(int(channel))] = bool(enabled)

    def getTestSignal(self, channel):
        return self._testSignal.get(abs(int(channel)), False)

    def setConditionalFilter(self, trigger, filtered):
        self._conditionalFilter = (list(trigger), list(filtered))

    def setEventDivider(self, channel, divider):
        self._eventDividers[abs(int(channel))] = max(1, int(divider))

    def getEventDivider(self, channel):
        return self._eventDividers.get(abs(int(channel)), 1)

    def getConditionalFilterTrigger(self):
        return list(self._conditionalFilter[0])

    def getConditionalFilterFiltered(self):
        return list(self._conditionalFilter[1])

    @property
    def filterOn(self) -> bool:
        trig, filt = self._conditionalFilter
        m = self._model
        return (abs(m.photon_channel) in {abs(c) for c in trig}
                and abs(m.sync_channel) in {abs(c) for c in filt})

    def getOverflowsAndClear(self):
        with self._lock:
            n, self._overflows = self._overflows, 0
        return n

    def getOverflows(self):
        return self._overflows

    # -- the signal seen on an input ------------------------------------- #

    def setChannelRate(self, channel: int, rate_hz: float) -> None:
        """Test hook: pin what an input receives, bypassing the model."""
        self._rates[int(channel)] = float(rate_hz)

    def _trigger_factor(self, channel: int) -> float:
        """How much of an input's pulses the comparator sees: a sigmoid
        plateau between a small threshold and the pulse amplitude, zero for
        the wrong polarity."""
        ch = abs(int(channel))
        amp = self._model.pulse_amplitude_v.get(ch)
        if amp is None:
            return 1.0
        trig = self._triggerLevels.get(ch, 0.5)
        if trig == 0.0 or (trig > 0) != (amp > 0):
            return 0.0
        # A comparator counts every pulse whose amplitude clears the
        # threshold: a flat plateau, with 20 mV wide edges at the noise
        # floor (50 mV) and at the pulse amplitude.
        a, v = abs(amp), abs(trig)
        return float(1 / (1 + math.exp(-(a - v) / 0.02)) * 1 / (1 + math.exp(-(v - 0.05) / 0.02)))

    def _deadtime_factor(self, channel: int, rate_hz: float) -> float:
        dead_s = self._deadtimes.get(int(channel), 0) * 1e-12
        return 1.0 / (1.0 + rate_hz * dead_s)

    def _raw_rate(self, channel: int) -> float:
        """The pulses arriving at an input, before the comparator."""
        ch = int(channel)
        m = self._model
        if ch in self._rates:
            return self._rates[ch]
        if abs(ch) == abs(m.photon_channel):
            if m.faults.get("dead_photons"):
                return 0.0
            if not m.laser_on:
                return m.dark_rate_hz
            return m.sample.parked_rate_hz * (1 + m.afterpulse_fraction) + m.dark_rate_hz
        if abs(ch) == abs(m.sync_channel):
            return m.rep_rate_hz
        edges = self._edges.get(abs(ch))
        if edges is not None and self._scan_active and self._scan_duration_s > 0:
            return len(edges) / self._scan_duration_s
        return 0.0

    def _scan_edges_in_window(self, channel: int, duration_s: float,
                              started_at: Optional[float] = None) -> Optional[int]:
        """Edges of a scan input inside a measurement's window of
        ``duration_s`` that began at wall-clock ``started_at`` (now minus
        the duration when unknown), in scan time; ``None`` when the input
        is not a scan clock or the scan's start is unknown."""
        edges = self._edges.get(abs(int(channel)))
        if edges is None or self._scan_started_at is None:
            return None
        if started_at is None:
            started_at = time.monotonic() - float(duration_s)
        lo = (started_at - self._scan_started_at) * 1e12
        hi = lo + float(duration_s) * 1e12
        return int(np.count_nonzero((edges >= lo) & (edges < hi)))

    def channelRate(self, channel: int, duration_s: Optional[float] = None,
                    started_at: Optional[float] = None) -> float:
        """What the card counts on an input, after trigger level, dead time,
        the test signal and the conditional filter. For a scan clock with a
        known scan start, ``duration_s`` (from wall-clock ``started_at``) is
        the measurement's window and the rate is the edges that fell into
        it (a Counter spanning the scan sees every line; one started late
        misses the first)."""
        ch = int(channel)
        if self._testSignal.get(abs(ch), False):
            return TEST_SIGNAL_RATE_HZ
        if ch in self._rates:
            return self._rates[ch]  # pinned by a test: no comparator model
        m = self._model
        raw = self._raw_rate(ch)
        if duration_s:
            n = self._scan_edges_in_window(ch, duration_s, started_at)
            if n is not None:
                raw = n / float(duration_s)
        rate = raw * self._trigger_factor(ch)
        rate *= self._deadtime_factor(ch, rate)
        if abs(ch) == abs(m.sync_channel) and self.filterOn:
            # Only the first sync after each photon is transmitted.
            rate = min(rate, self.channelRate(m.photon_channel))
        rate /= self._eventDividers.get(abs(ch), 1)
        return rate

    def set_laser_on(self, on: bool) -> None:
        """Test / tutorial hook: block or unblock the excitation."""
        self._model.laser_on = bool(on)

    def set_fault(self, name: str, value) -> None:
        """Test / tutorial hook: inject or clear a fault at run time (the
        live ones: ``line_delay_ps``, ``missing_line_clock``,
        ``dead_photons``, ``wrong_sync_polarity``). ``None`` clears it."""
        if value is None:
            self._model.faults.pop(str(name), None)
        else:
            self._model.faults[str(name)] = value

    def sample_truth(self, ny: int, nx: int):
        """What the sample really looks like on an ``ny`` x ``nx`` grid:
        ``(rate_map_hz, lifetime_map_ns)`` -- the truth a FLIM image is
        compared against (tutorial 09)."""
        sample = self._model.sample
        return (np.asarray(sample.rate_map(int(ny), int(nx)), dtype=np.float64),
                np.asarray(sample.lifetime_map(int(ny), int(nx)), dtype=np.float64))

    def transmittedRate(self) -> float:
        """All tags the USB link must carry per second."""
        m = self._model
        channels = {abs(m.photon_channel), abs(m.sync_channel), abs(m.line_channel)}
        if m.frame_channel is not None:
            channels.add(abs(m.frame_channel))
        return sum(self.channelRate(c) for c in channels)

    def _budget_factor(self, duration_s: float) -> float:
        """Fraction of tags that survive the link; counts overflows as it goes."""
        total = self.transmittedRate()
        budget = self._model.tag_budget
        if total <= budget:
            return 1.0
        lost_blocks = int((total - budget) * max(duration_s, 1e-3) / 1e5) + 1
        with self._lock:
            self._overflows += lost_blocks
        return budget / total

    def addOverflows(self, n: int) -> None:
        """Test hook: pretend the USB link dropped ``n`` blocks."""
        with self._lock:
            self._overflows += int(n)

    # -- scan edges ------------------------------------------------------ #

    def load_scan_edges(self, edges_ps: Dict[int, Iterable[float]],
                        duration_s: float) -> None:
        """Edge timestamps per physical input for the scan about to run."""
        self._edges = {}
        for channel, edges in edges_ps.items():
            arr = np.asarray(list(edges), dtype=np.int64)
            if abs(int(channel)) == abs(self._model.line_channel) \
                    and self._model.faults.get("missing_line_clock"):
                arr = arr[:0]
            self._edges[abs(int(channel))] = arr
        self._scan_duration_s = float(duration_s)
        self._scan_active = True
        self._scan_started_at = None

    def clear_scan_edges(self) -> None:
        self._edges = {}
        self._scan_duration_s = 0.0
        self._scan_active = False
        self._scan_started_at = None

    def start_scan(self) -> None:
        """The scan's clocks start now (``sigScanStarted``): from here the
        scan inputs count the edges that fall into a measurement's window."""
        self._scan_started_at = time.monotonic()
        self._scan_active = True

    def end_scan(self) -> None:
        """The scan is over: its clocks stop (count rates drop to 0), while
        the edges stay for a ``Flim`` frame that is read afterwards."""
        self._scan_active = False

    @property
    def scan_active(self) -> bool:
        return self._scan_active

    def edges(self, channel: int) -> np.ndarray:
        return self._edges.get(abs(int(channel)), np.zeros(0, dtype=np.int64))

    def _delay_ps(self, channel: int) -> int:
        """The input delay stamped onto a channel, whichever sign it was
        set with."""
        ch = int(channel)
        return int(self._inputDelays.get(ch, 0) + self._inputDelays.get(-ch, 0))

    def stamped_edges(self, channel: int) -> np.ndarray:
        """The edges of a scan clock as the card timestamps them: the loaded
        edges plus the input delay. Empty for any other input."""
        edges = self.edges(channel)
        if edges.size == 0:
            return edges
        return edges + self._delay_ps(channel)

    def has_scan_edges(self, channel: int) -> bool:
        return abs(int(channel)) in self._edges

    def marker_times(self, virtual_channel: int) -> np.ndarray:
        """The markers an ``EventGenerator`` emits on its virtual channel for
        the loaded scan: one pattern per stamped trigger edge."""
        gen = self._generators.get(int(virtual_channel))
        if gen is None:
            return np.zeros(0, dtype=np.int64)
        trigger = self.stamped_edges(gen.trigger_channel)
        if trigger.size == 0 or gen.pattern.size == 0:
            return np.zeros(0, dtype=np.int64)
        return (trigger[:, None] + gen.pattern[None, :]).reshape(-1)

    # -- histograms ------------------------------------------------------ #

    def _direction_for(self, start_channel: int, click_channel: int) -> Optional[str]:
        """``'forward'``, ``'reverse'``, ``'garbage'`` (forward slots with the
        filter on: each click is measured against the previous photon's
        sync), or ``None`` (not a photon/sync pair)."""
        m = self._model
        p, s = abs(m.photon_channel), abs(m.sync_channel)
        start, click = abs(int(start_channel)), abs(int(click_channel))
        if (start, click) == (s, p):
            return "garbage" if self.filterOn else "forward"
        if (start, click) == (p, s):
            return "reverse"
        return None

    def expected_histogram(self, start_channel: int, click_channel: int,
                           binwidth_ps: int, n_bins: int,
                           photons: np.ndarray, lifetime_ns: np.ndarray,
                           background: np.ndarray) -> np.ndarray:
        """Expected counts per bin for ``photons`` with ``lifetime_ns`` on the
        histogram's own axis (what the card returns), for any leading shape."""
        direction = self._direction_for(start_channel, click_channel)
        t_raw_ns = (np.arange(n_bins) + 0.5) * binwidth_ps * 1e-3
        photons = np.asarray(photons, dtype=np.float64)[..., None]
        background = np.asarray(background, dtype=np.float64)[..., None]
        flat = np.full(t_raw_ns.shape, 1.0 / n_bins)
        if direction is None:
            return np.zeros(photons.shape[:-1] + (n_bins,))
        if direction == "garbage":
            return (photons + background) * flat
        m = self._model
        period_ns = m.period_ps * 1e-3
        delay_ns = self._inputDelays.get(m.photon_channel, 0) * 1e-3
        if direction == "forward":
            # A photon delay moves the peak: setInputDelay(photons, -t0).
            t_forward = t_raw_ns - delay_ns
            shape = decay_shape(t_forward, lifetime_ns, m.t0_ps * 1e-3,
                                m.irf_fwhm_ps * 1e-3, period_ns)
            # The window may be shorter than the period: a truncated decay.
            inside = (t_raw_ns < period_ns).astype(float)
            shape = shape * inside
            total = shape.sum(axis=-1, keepdims=True)
            return photons * shape + background * flat * (total > 0)
        # Reverse: t' = T_rep - t, early photons sit near the end. A window
        # longer than the period sees the next period's copy; shorter, it
        # loses the peak, which is the point the tutorial makes.
        t_forward = (period_ns - t_raw_ns) % period_ns
        shape = decay_shape(t_forward, lifetime_ns, m.t0_ps * 1e-3,
                            m.irf_fwhm_ps * 1e-3, period_ns)
        return photons * shape + background * flat

    def sample_counts(self, expected: np.ndarray) -> np.ndarray:
        return self._rng.poisson(np.clip(expected, 0, None)).astype(np.uint32)

    # -- lifecycle ------------------------------------------------------- #

    @property
    def freed(self) -> bool:
        return self._freed

    def _free(self):
        self._freed = True


# --------------------------------------------------------------------------- #
# Measurements                                                                 #
# --------------------------------------------------------------------------- #


class _Measurement:
    """Shared start/stop bookkeeping of the vendor measurement classes."""

    def __init__(self, tagger: MockTimeTagger):
        if not isinstance(tagger, MockTimeTagger):
            raise TypeError("mock measurements need a MockTimeTagger")
        if tagger.freed:
            raise RuntimeError("TimeTagger has been freed")
        self._tagger = tagger
        self._running = True
        self._capture_ps = 0
        self._started_at: Optional[float] = None
        tagger.measurements.add(self)

    def start(self):
        self._running = True
        self._started_at = time.monotonic()

    def startFor(self, duration_ps, clear=True):
        self._capture_ps = int(duration_ps)
        self._started_at = time.monotonic()
        self._running = False  # the capture is complete the moment it is read

    def stop(self):
        self._running = False

    def clear(self):
        pass

    def isRunning(self):
        return self._running

    def waitUntilFinished(self, timeout=-1):
        return True

    def getCaptureDuration(self):
        return self._capture_ps

    @property
    def _duration_s(self) -> float:
        return self._capture_ps * 1e-12 if self._capture_ps else 1.0


class Countrate(_Measurement):
    def __init__(self, tagger, channels: Iterable[int]):
        super().__init__(tagger)
        self._channels = [int(c) for c in channels]

    def getData(self):
        self._tagger._budget_factor(self._duration_s)
        return np.array([self._tagger.channelRate(c, self._duration_s, self._started_at)
                         for c in self._channels], dtype=np.float64)


class Counter(_Measurement):
    def __init__(self, tagger, channels: Iterable[int], binwidth=1_000_000_000,
                 n_values=1000):
        super().__init__(tagger)
        self._channels = [int(c) for c in channels]
        self._binwidth = int(binwidth)
        self._n_values = int(n_values)

    def getData(self, rolling=True):
        window_s = self._binwidth * 1e-12
        per_bin = [self._tagger.channelRate(c, window_s, self._started_at) * window_s
                   for c in self._channels]
        return np.tile(np.rint(np.array(per_bin, dtype=np.float64)).astype(np.int64)[:, None],
                       (1, self._n_values))

    def getDataNormalized(self, rolling=True):
        return np.tile(
            np.array([self._tagger.channelRate(c) for c in self._channels],
                     dtype=np.float64)[:, None],
            (1, self._n_values),
        )

    def getIndex(self):
        return np.arange(self._n_values, dtype=np.int64) * self._binwidth


class Histogram(_Measurement):
    """Parked-beam TCSPC histogram between two inputs."""

    def __init__(self, tagger, click_channel, start_channel, binwidth=1000,
                 n_bins=1000):
        super().__init__(tagger)
        self.click_channel = int(click_channel)
        self.start_channel = int(start_channel)
        self.binwidth = int(binwidth)
        self.n_bins = int(n_bins)

    def getIndex(self):
        return np.arange(self.n_bins, dtype=np.int64) * self.binwidth

    def getData(self):
        tagger = self._tagger
        m = tagger._model
        if tagger.has_scan_edges(self.click_channel) and tagger.has_scan_edges(self.start_channel):
            return self._from_edges()
        duration = self._duration_s
        factor = tagger._budget_factor(duration)
        photons = tagger.channelRate(m.photon_channel) * duration * factor
        if m.faults.get("dead_photons") or not m.laser_on:
            photons = min(photons, m.dark_rate_hz * duration * factor)
        dark = tagger._raw_rate(m.photon_channel) - m.sample.parked_rate_hz * (1 + m.afterpulse_fraction)
        background = (max(0.0, dark) + m.sample.parked_rate_hz * m.afterpulse_fraction) \
            * tagger._trigger_factor(m.photon_channel) * duration * factor
        signal = max(0.0, photons - background)
        expected = tagger.expected_histogram(
            self.start_channel, self.click_channel, self.binwidth, self.n_bins,
            np.array(signal), np.array(m.sample.parked_lifetime_ns),
            np.array(background),
        )
        return tagger.sample_counts(expected).astype(np.int64)

    def _from_edges(self) -> np.ndarray:
        """Two scan clocks: the time from each start edge to the click edges
        that fall inside the window, as the card stamps them (input delays
        included). The frame-to-line skew measurement (tutorial 08)."""
        tagger = self._tagger
        starts = np.sort(tagger.stamped_edges(self.start_channel))
        clicks = np.sort(tagger.stamped_edges(self.click_channel))
        counts = np.zeros(self.n_bins, dtype=np.int64)
        window = self.n_bins * self.binwidth
        if starts.size == 0 or clicks.size == 0:
            return counts
        lo = np.searchsorted(clicks, starts, side="left")
        hi = np.searchsorted(clicks, starts + window, side="left")
        for start, a, b in zip(starts, lo, hi):
            if b > a:
                bins = (clicks[a:b] - start) // self.binwidth
                np.add.at(counts, bins.astype(np.int64), 1)
        return counts


class TimeDifferences(_Measurement):
    """Time from each start to the next click. With ``click == start`` it
    is the period histogram of a periodic signal -- what the rep-rate
    measurement reads for the laser's period jitter, floored by the card's
    own two-tag jitter."""

    #: The card's two-tag timing jitter floor, RMS picoseconds, per model.
    JITTER_FLOOR_PS = {"Time Tagger 20": 48.0, "Time Tagger Ultra": 30.0,
                       "Time Tagger X": 8.0}

    def __init__(self, tagger, click_channel, start_channel=CHANNEL_UNUSED,
                 next_channel=CHANNEL_UNUSED, sync_channel=CHANNEL_UNUSED,
                 binwidth=1000, n_bins=1000, n_histograms=1):
        super().__init__(tagger)
        self.click_channel = int(click_channel)
        self.start_channel = int(start_channel)
        self.binwidth = int(binwidth)
        self.n_bins = int(n_bins)

    def getIndex(self):
        return np.arange(self.n_bins, dtype=np.int64) * self.binwidth

    def getData(self):
        tagger = self._tagger
        m = tagger._model
        counts = np.zeros((1, self.n_bins), dtype=np.int64)
        if abs(self.click_channel) != abs(self.start_channel):
            return counts
        if tagger.has_scan_edges(self.click_channel):
            # A scan clock: the real edge-to-edge differences of the loaded
            # scan (line = dwell x Nx + flyback), jittered by the card.
            edges = np.sort(tagger.stamped_edges(self.click_channel))
            if edges.size < 2 or not tagger.scan_active:
                return counts
            floor = self.JITTER_FLOOR_PS.get(m.model, 30.0)
            diffs = np.diff(edges).astype(np.float64)
            diffs += tagger._rng.normal(0.0, floor, size=diffs.size)
            bins = np.floor(diffs / self.binwidth).astype(np.int64)
            inside = (bins >= 0) & (bins < self.n_bins)
            np.add.at(counts[0], bins[inside], 1)
            return counts
        rate = tagger.channelRate(self.click_channel)
        if rate <= 0:
            return counts
        period_ps = 1e12 / rate
        floor = self.JITTER_FLOOR_PS.get(m.model, 30.0)
        sigma = math.hypot(m.sync_jitter_ps, floor)
        centres = (np.arange(self.n_bins) + 0.5) * self.binwidth
        shape = np.exp(-0.5 * ((centres - period_ps) / sigma) ** 2)
        total = shape.sum()
        if total <= 0:
            return counts
        n = rate * self._duration_s
        counts[0] = tagger.sample_counts(n * shape / total)
        return counts


class EventGenerator(_Measurement):
    """Markers from a pattern on every edge of a trigger input."""

    _next_virtual = 1000

    def __init__(self, tagger, trigger_channel, pattern, trigger_divider=1,
                 divider_offset=0, stop_channel=CHANNEL_UNUSED):
        super().__init__(tagger)
        self.trigger_channel = int(trigger_channel)
        self.pattern = np.asarray(pattern, dtype=np.int64).copy()
        EventGenerator._next_virtual += 1
        self._channel = EventGenerator._next_virtual
        tagger._generators[self._channel] = self

    def getChannel(self):
        return self._channel


class _ScopeEvent:
    """One edge in a ``Scope`` trace: ``state`` (True = rising) at ``time``
    picoseconds after the trigger edge."""

    __slots__ = ("state", "time")

    def __init__(self, state: bool, time: int):
        self.state = bool(state)
        self.time = int(time)

    def __repr__(self):
        return f"Event({'rising' if self.state else 'falling'} @ {self.time} ps)"


class Scope(_Measurement):
    """An oscilloscope-like trace: every edge on ``event_channels`` within
    ``window_size`` ps after the first edge on ``trigger_channel`` (the
    vendor's ``Scope`` with ``n_traces=1``). Scan clocks and the pixel
    markers come from the loaded scan (input delays included), the laser
    sync is periodic, the photons Poisson at the channel's rate. Clock
    pulses are 1 us wide, the sync half a period."""

    CLOCK_WIDTH_PS = 1_000_000

    def __init__(self, tagger, event_channels: Iterable[int], trigger_channel,
                 window_size, n_traces=1, n_max_events=1000):
        super().__init__(tagger)
        self.event_channels = [int(c) for c in event_channels]
        self.trigger_channel = int(trigger_channel)
        self.window_size = int(window_size)
        self.n_max_events = int(n_max_events)

    def _edges_of(self, channel: int, t0: int, t1: int) -> np.ndarray:
        tagger = self._tagger
        m = tagger._model
        if channel in tagger._generators:
            times = tagger.marker_times(channel)
        elif tagger.has_scan_edges(channel):
            times = tagger.stamped_edges(channel)
        elif abs(channel) == abs(m.sync_channel):
            period = m.period_ps
            first = math.ceil(t0 / period) * period
            times = np.arange(first, t1, period, dtype=np.float64)
        elif abs(channel) == abs(m.photon_channel):
            n = tagger._rng.poisson(tagger.channelRate(channel) * (t1 - t0) * 1e-12)
            times = np.sort(tagger._rng.uniform(t0, t1, size=int(n)))
        else:
            times = np.zeros(0)
        times = np.asarray(times, dtype=np.float64)
        return times[(times >= t0) & (times < t1)].astype(np.int64)

    def getData(self):
        tagger = self._tagger
        m = tagger._model
        trigger = self._edges_of(self.trigger_channel, 0, 1 << 62)
        if trigger.size == 0:
            return [[] for _ in self.event_channels]
        t0 = int(trigger[0])
        t1 = t0 + self.window_size
        traces = []
        for channel in self.event_channels:
            events = []
            width = (m.period_ps / 2 if abs(channel) == abs(m.sync_channel)
                     else self.CLOCK_WIDTH_PS)
            for t in self._edges_of(channel, t0, t1)[: self.n_max_events]:
                events.append(_ScopeEvent(True, t - t0))
                if t + width < t1:
                    events.append(_ScopeEvent(False, int(t + width - t0)))
            traces.append(sorted(events, key=lambda e: e.time))
        return traces


class _FlimFrameInfo:
    """What ``Flim.getReadyFrameEx`` returns."""

    def __init__(self, histograms: np.ndarray, frame_number: int, valid: bool = True):
        self._histograms = histograms
        self._frame_number = frame_number
        self._valid = valid

    def isValid(self):
        return self._valid

    def getFrameNumber(self):
        return self._frame_number

    def getHistograms(self):
        return self._histograms.copy()

    def getIntensities(self):
        return self._histograms.sum(axis=1).astype(np.uint32)

    def getPixelPosition(self):
        return int(self._histograms.shape[0])


class Flim(_Measurement):
    """A FLIM frame drawn from the sample, pixel by pixel.

    The image geometry is what the markers say: one row per edge on the
    pixel-begin generator's trigger input, one column per pattern entry.
    No edges (the line clock not cabled, or the ``missing_line_clock``
    fault) means an empty frame -- the "image is all zeros" symptom.

    The card closes a frame once ``n_pixels`` pixel ends have passed, which
    the mock reports through ``getFramesAcquired``: ``1`` as soon as the
    loaded edges carry enough markers, independent of any read. A test can
    delay that with ``complete_after_polls`` to exercise the worker's
    count-based final-frame detection in both orders against scan-done.
    """

    def __init__(self, tagger, start_channel, click_channel,
                 pixel_begin_channel, n_pixels, n_bins, binwidth,
                 pixel_end_channel=CHANNEL_UNUSED,
                 frame_begin_channel=CHANNEL_UNUSED,
                 finish_after_outputframe=0, n_frame_average=1,
                 pre_initialize=True):
        super().__init__(tagger)
        self.start_channel = int(start_channel)
        self.click_channel = int(click_channel)
        self.pixel_begin_channel = int(pixel_begin_channel)
        self.pixel_end_channel = int(pixel_end_channel)
        self.frame_begin_channel = int(frame_begin_channel)
        self.n_pixels = int(n_pixels)
        self.n_bins = int(n_bins)
        self.binwidth = int(binwidth)
        self._frame: Optional[np.ndarray] = None
        self._polls = 0
        #: Test knob: the frame counts as closed only after this many
        #: ``getFramesAcquired`` calls. The default, 1, lets the detector's
        #: baseline read at arming see an open frame and the worker's first
        #: look see it closed -- a scan that completes before scan-done.
        self.complete_after_polls = 1

    def getIndex(self):
        return np.arange(self.n_bins, dtype=np.int64) * self.binwidth

    def _geometry(self):
        gen = self._tagger._generators.get(self.pixel_begin_channel)
        if gen is None:
            return 0, 0, 0.0, 0
        edges = self._tagger.edges(gen.trigger_channel)
        ny = len(edges)
        nx = len(gen.pattern)
        dwell_ps = float(gen.pattern[1] - gen.pattern[0]) if nx > 1 else 0.0
        offset_ps = int(gen.pattern[0]) if nx else 0
        return ny, nx, dwell_ps, offset_ps

    def _render(self) -> np.ndarray:
        tagger = self._tagger
        m = tagger._model
        frame = np.zeros((self.n_pixels, self.n_bins), dtype=np.uint32)
        ny, nx, dwell_ps, offset_ps = self._geometry()
        if ny == 0 or nx == 0 or dwell_ps <= 0:
            return frame
        n_used = min(self.n_pixels, ny * nx)
        ny_used = n_used // nx
        if ny_used == 0:
            return frame
        dwell_s = dwell_ps * 1e-12

        rate = np.asarray(m.sample.rate_map(ny_used, nx), dtype=np.float64)
        tau = np.asarray(m.sample.lifetime_map(ny_used, nx), dtype=np.float64)
        # The line delay. A line clock reaching the card *late* against the
        # beam (the ``line_delay_ps`` fault, positive) starts the markers
        # late, so each pixel samples a later position and the image shifts
        # left; an early clock (negative fault: a galvo lagging its command)
        # the other way. A card input delay or a pattern offset moves the
        # markers by its own sign, so ``lineClockDelayPs`` cancels the fault
        # with the opposite sign: positive (the pattern) for an early clock,
        # negative (the card) for a late one.
        lateness_ps = (float(m.faults.get("line_delay_ps", 0))
                       + tagger._delay_ps(m.line_channel)
                       + offset_ps)
        shift = int(round(lateness_ps / dwell_ps)) if dwell_ps else 0
        if shift:
            rate = np.roll(rate, -shift, axis=1)
            tau = np.roll(tau, -shift, axis=1)

        factor = tagger._trigger_factor(m.photon_channel)
        factor *= tagger._budget_factor(self._tagger._scan_duration_s or dwell_s * n_used)
        if m.faults.get("dead_photons"):
            factor = 0.0
        photons = rate * dwell_s * factor
        background = (m.dark_rate_hz + rate * m.afterpulse_fraction) * dwell_s * factor
        expected = tagger.expected_histogram(
            self.start_channel, self.click_channel, self.binwidth, self.n_bins,
            photons, tau, background,
        )
        counts = tagger.sample_counts(expected).reshape(ny_used * nx, self.n_bins)
        frame[:ny_used * nx] = counts
        return frame

    def _markers_suffice(self) -> bool:
        ny, nx, dwell_ps, _ = self._geometry()
        return ny * nx >= self.n_pixels and dwell_ps > 0

    def getCurrentFrame(self):
        if self._frame is None:
            self._frame = self._render()
        return self._frame.copy()

    def getReadyFrame(self):
        return self.getCurrentFrame()

    def getReadyFrameEx(self):
        if self.getFramesAcquired() < 1:
            return _FlimFrameInfo(np.zeros((self.n_pixels, self.n_bins), np.uint32), 0, valid=False)
        return _FlimFrameInfo(self.getCurrentFrame(), 1)

    def getCurrentFrameIntensity(self):
        return self.getCurrentFrame().sum(axis=1).astype(np.uint32)

    def getFramesAcquired(self):
        self._polls += 1
        if not self._markers_suffice():
            return 0
        return 1 if self._polls > self.complete_after_polls else 0


class MockTimeTaggerApi:
    """What ``import TimeTagger`` would give you, for the parts we use.

    ``TimeTaggerManager`` holds one of these as its ``api`` when simulating,
    so the FLIM detector writes ``api.Flim(api_tagger, ...)`` the same way
    whether the card is real or not.
    """

    CHANNEL_UNUSED = CHANNEL_UNUSED
    Countrate = Countrate
    Counter = Counter
    Histogram = Histogram
    TimeDifferences = TimeDifferences
    EventGenerator = EventGenerator
    Scope = Scope
    Flim = Flim

    def __init__(self, rates_hz: Optional[Dict[int, float]] = None,
                 model: Optional[SignalModel] = None):
        self._rates = dict(rates_hz or {})
        self._model = model
        self.created: List[MockTimeTagger] = []

    @classmethod
    def from_info(cls, info) -> "MockTimeTaggerApi":
        return cls(model=SignalModel.from_info(info))

    def createTimeTagger(self, serial: Optional[str] = None):
        tagger = MockTimeTagger(serial=serial, model=self._model,
                                rates_hz=self._rates)
        self.created.append(tagger)
        return tagger

    def freeTimeTagger(self, tagger):
        tagger._free()

    @staticmethod
    def scanTimeTagger():
        return ["MOCK-000001"]


__all__ = [
    "CHANNEL_UNUSED",
    "SAMPLE_PRESETS",
    "TAG_BUDGET_PER_MODEL",
    "TEST_SIGNAL_RATE_HZ",
    "Counter",
    "Countrate",
    "EventGenerator",
    "Flim",
    "Histogram",
    "MockSample",
    "MockTimeTagger",
    "MockTimeTaggerApi",
    "SignalModel",
    "TimeDifferences",
    "decay_shape",
]
