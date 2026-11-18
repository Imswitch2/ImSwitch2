"""A simulated step board's camera pulses, handed to the mock cameras.

A simulated NI-DAQ triggers the mock cameras on its lines
(``mockscan.ScanSimulationCoordinator``). A simulated step board plays its
program in a moment and triggers nothing, so a mock camera on its external
trigger waited for frames that never came: a recording on a step-board mock
setup stalled. After a simulated run, the rising edges the run played on each
camera are counted from its log and handed to the cameras that are acquiring
(they hold an acquisition lease), spread over the run's own duration -- at
least 0.2 s, at most 5 s, as the NI-DAQ simulation spreads them -- and the run
reports its end after the last one. A real board triggers its cameras itself;
nothing here touches it.

Qt-free.
"""

import threading
from collections import defaultdict
from typing import Callable, Dict, Iterable

from imswitch.imcommon.model import initLogger

#: How long the frames of one run take at least and at most, and how often
#: they are handed over (seconds): the NI-DAQ simulation's numbers.
MIN_DURATION_S = 0.2
MAX_DURATION_S = 5.0
TICK_S = 0.03


def rising_edges(entries: Iterable[tuple]) -> Dict[str, int]:
    """The pulses each device's line played in a run log: the times it went
    from off to on. Writes at one tick count as one change -- a window to the
    end of one step and one from the start of the next are one pulse, as the
    line itself shows it."""
    writes = defaultdict(list)
    for order, entry in enumerate(entries):
        if entry[0] in ('on', 'off'):
            _, segment, tick, device = entry[:4]
            writes[device].append((segment, tick, order, entry[0] == 'on'))
    counts = {}
    for device, items in writes.items():
        items.sort()
        on, pulses, i = False, 0, 0
        while i < len(items):
            when, now = items[i][:2], on
            while i < len(items) and items[i][:2] == when:
                now = items[i][3]
                i += 1
            pulses += now and not on
            on = now
        if pulses:
            counts[device] = pulses
    return counts


def deliver(counts: Dict[str, int], duration_s: float, trigger: Callable[[str, int], None],
            stop: threading.Event) -> bool:
    """Hand ``counts`` frames to ``trigger(detector, n)``, spread evenly over
    ``duration_s`` (clamped to 0.2-5 s; ``0`` hands them over at once).
    False when ``stop`` was set first: the frames not yet handed over are not."""
    if duration_s <= 0:
        for name, total in counts.items():
            trigger(name, total)
        return True
    duration_s = min(MAX_DURATION_S, max(MIN_DURATION_S, float(duration_s)))
    ticks = max(1, int(round(duration_s / TICK_S)))
    handed = dict.fromkeys(counts, 0)
    for tick in range(1, ticks + 1):
        if stop.wait(duration_s / ticks):
            return False
        for name, total in counts.items():
            n = total * tick // ticks - handed[name]
            if n > 0:
                handed[name] += n
                trigger(name, n)
    return True


class FrameDelivery:
    """Which detectors take a simulated run's pulses, and how: a detector of
    the setup that is acquiring and whose manager has ``mockTrigger`` (a mock
    camera; a real camera's manager has none)."""

    def __init__(self, detectorsManager):
        self._detectors = detectorsManager
        self.__logger = initLogger(self, tryInheritParent=False)

    def armed(self, counts: Dict[str, int]) -> Dict[str, int]:
        """The part of ``counts`` (device: pulses) that goes to a detector."""
        names = set(self._detectors.getAllDeviceNames())
        out = {}
        for name, n in counts.items():
            if name not in names:
                continue
            manager = self._detectors[name]
            if not callable(getattr(manager, 'mockTrigger', None)):
                continue
            if not getattr(manager, 'forAcquisition', True):
                continue
            try:
                leased = self._detectors.isDetectorLeased(name)
            except Exception:
                # A detector whose state cannot be told is not shown frames a
                # camera would not have taken.
                continue
            if leased:
                out[name] = n
        return out

    def trigger(self, name: str, n: int):
        try:
            self._detectors[name].mockTrigger(int(n))
        except Exception as error:
            self.__logger.warning(f'Simulated frame trigger for {name} failed: {error}')


def deliver_run_frames(engine, entries, wall_ticks, stop: threading.Event, *,
                       paced=True) -> bool:
    """What a step engine does after a simulated run: ``True`` when its
    frames were handed over (or there were none to hand), ``False`` when a
    stop came first. ``wall_ticks`` is the run's length on the engine's grid;
    ``paced`` false hands the frames over at once (a run that was stopped)."""
    delivery = getattr(engine, 'frameDelivery', None)
    if delivery is None:
        return True
    counts = delivery.armed(rising_edges(entries))
    if not counts:
        return True
    grid = getattr(engine, '_grid', None) or 0.0
    duration = float(wall_ticks or 0) * grid if paced else 0.0
    return deliver(counts, duration, delivery.trigger, stop)


__all__ = ['FrameDelivery', 'MAX_DURATION_S', 'MIN_DURATION_S', 'deliver',
           'deliver_run_frames', 'rising_edges']
