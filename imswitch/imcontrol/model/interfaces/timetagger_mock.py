"""In-process stand-in for the Swabian ``TimeTagger`` Python module.

``MockTimeTaggerApi`` quacks like the vendor module (``createTimeTagger``,
``freeTimeTagger``, measurement classes) and ``MockTimeTagger`` like the
card object it returns, for the subset ImSwitch uses. This first cut models
*counting only*: every channel has a configurable rate, the built-in test
signal, a trigger level and a dead time, and the measurements report those
rates. ``EventGenerator`` and ``Flim`` exist so a scan on a simulated rig
completes with an empty histogram instead of failing; a photon and
scan-edge signal model (the second phase of the Lifetime 2.0 plan) replaces
them with real synthetic data.

Nothing here runs a thread: rates are analytic, so results are exact and
tests are deterministic.
"""

from __future__ import annotations

import threading
from typing import Dict, Iterable, List, Optional

import numpy as np

#: The card's own test signal, roughly what a Time Tagger 20 produces.
TEST_SIGNAL_RATE_HZ = 850_000.0

CHANNEL_UNUSED = -134217728  # TimeTagger.CHANNEL_UNUSED


class MockTimeTagger:
    """The card object. ``rates_hz`` seeds what each physical input sees."""

    def __init__(self, serial: Optional[str] = None,
                 rates_hz: Optional[Dict[int, float]] = None,
                 model: str = "Time Tagger 20 (mock)"):
        self._serial = serial or "MOCK-000001"
        self._model = model
        self._rates: Dict[int, float] = {int(k): float(v)
                                         for k, v in (rates_hz or {}).items()}
        self._triggerLevels: Dict[int, float] = {}
        self._inputDelays: Dict[int, int] = {}
        self._deadtimes: Dict[int, int] = {}
        self._testSignal: Dict[int, bool] = {}
        self._conditionalFilter = ([], [])
        self._overflows = 0
        self._freed = False
        self._lock = threading.Lock()
        #: Every measurement object created against this card, so a test can
        #: check that nothing is left behind.
        self.measurements: List[object] = []

    # -- identity -------------------------------------------------------- #

    def getSerial(self):
        return self._serial

    def getModel(self):
        return self._model

    def getConfiguration(self):
        return {"serial": self._serial, "model": self._model, "mock": True}

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

    def getConditionalFilterTrigger(self):
        return list(self._conditionalFilter[0])

    def getConditionalFilterFiltered(self):
        return list(self._conditionalFilter[1])

    def getOverflowsAndClear(self):
        with self._lock:
            n, self._overflows = self._overflows, 0
        return n

    def getOverflows(self):
        return self._overflows

    # -- the signal seen on a channel ------------------------------------ #

    def setChannelRate(self, channel: int, rate_hz: float) -> None:
        """Test hook: what the input is receiving, in counts per second."""
        self._rates[int(channel)] = float(rate_hz)

    def channelRate(self, channel: int) -> float:
        channel = int(channel)
        if self._testSignal.get(abs(channel), False):
            return TEST_SIGNAL_RATE_HZ
        return self._rates.get(channel, 0.0)

    def addOverflows(self, n: int) -> None:
        """Test hook: pretend the USB link dropped ``n`` blocks."""
        with self._lock:
            self._overflows += int(n)

    # -- lifecycle ------------------------------------------------------- #

    @property
    def freed(self) -> bool:
        return self._freed

    def _free(self):
        self._freed = True


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
        tagger.measurements.append(self)

    def start(self):
        self._running = True

    def startFor(self, duration_ps, clear=True):
        self._capture_ps = int(duration_ps)
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


class Countrate(_Measurement):
    def __init__(self, tagger, channels: Iterable[int]):
        super().__init__(tagger)
        self._channels = [int(c) for c in channels]

    def getData(self):
        return np.array([self._tagger.channelRate(c) for c in self._channels],
                        dtype=np.float64)


class Counter(_Measurement):
    def __init__(self, tagger, channels: Iterable[int], binwidth=1_000_000_000,
                 n_values=1000):
        super().__init__(tagger)
        self._channels = [int(c) for c in channels]
        self._binwidth = int(binwidth)
        self._n_values = int(n_values)

    def getData(self, rolling=True):
        per_bin = [self._tagger.channelRate(c) * self._binwidth * 1e-12
                   for c in self._channels]
        return np.tile(np.array(per_bin, dtype=np.int64)[:, None],
                       (1, self._n_values))

    def getDataNormalized(self, rolling=True):
        return np.tile(
            np.array([self._tagger.channelRate(c) for c in self._channels],
                     dtype=np.float64)[:, None],
            (1, self._n_values),
        )

    def getIndex(self):
        return np.arange(self._n_values, dtype=np.int64) * self._binwidth


class EventGenerator(_Measurement):
    """Records the marker pattern a detector asks for; emits nothing yet."""

    _next_virtual = 1000

    def __init__(self, tagger, trigger_channel, pattern, trigger_divider=1,
                 divider_offset=0, stop_channel=CHANNEL_UNUSED):
        super().__init__(tagger)
        self.trigger_channel = int(trigger_channel)
        self.pattern = np.asarray(pattern, dtype=np.int64).copy()
        EventGenerator._next_virtual += 1
        self._channel = EventGenerator._next_virtual

    def getChannel(self):
        return self._channel


class Flim(_Measurement):
    """A FLIM frame with no photons in it. Shapes match the vendor class."""

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
        self._frames_acquired = 0

    def getCurrentFrame(self):
        return np.zeros((self.n_pixels, self.n_bins), dtype=np.uint32)

    def getReadyFrame(self):
        return self.getCurrentFrame()

    def getCurrentFrameIntensity(self):
        return np.zeros(self.n_pixels, dtype=np.uint32)

    def getFramesAcquired(self):
        return self._frames_acquired

    def getIndex(self):
        return np.arange(self.n_bins, dtype=np.int64) * self.binwidth


class MockTimeTaggerApi:
    """What ``import TimeTagger`` would give you, for the parts we use.

    ``TimeTaggerManager`` holds one of these as its ``api`` when simulating,
    so the FLIM detector writes ``api.Flim(api_tagger, ...)`` the same way
    whether the card is real or not.
    """

    CHANNEL_UNUSED = CHANNEL_UNUSED
    Countrate = Countrate
    Counter = Counter
    EventGenerator = EventGenerator
    Flim = Flim

    def __init__(self, rates_hz: Optional[Dict[int, float]] = None,
                 model: str = "Time Tagger 20 (mock)"):
        self._rates = dict(rates_hz or {})
        self._model = model
        self.created: List[MockTimeTagger] = []

    def createTimeTagger(self, serial: Optional[str] = None):
        tagger = MockTimeTagger(serial=serial, rates_hz=self._rates,
                                model=self._model)
        self.created.append(tagger)
        return tagger

    def freeTimeTagger(self, tagger):
        tagger._free()

    @staticmethod
    def scanTimeTagger():
        return ["MOCK-000001"]


__all__ = [
    "CHANNEL_UNUSED",
    "TEST_SIGNAL_RATE_HZ",
    "Counter",
    "Countrate",
    "EventGenerator",
    "Flim",
    "MockTimeTagger",
    "MockTimeTaggerApi",
]
