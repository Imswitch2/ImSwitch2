"""Measuring what Stop costs the GUI thread, without hardware.

Shared by the Stop-responsiveness tests and by the one-off measurements behind
them.  Three pieces:

* :class:`GapProbe` -- a heartbeat timer on the GUI thread and, beside it, a
  sampler thread that records which function the GUI thread was sitting in
  whenever the heartbeat was late.  The longest gap is the longest time the
  window could not repaint or take a click.
* :class:`FakeDaqTask` and :func:`install_daq` -- NI-DAQ tasks with the timing
  the driver has (stop/close blocking, ``wait_until_done`` lasting the length
  of the waveform) registered into the real :class:`NidaqManager` of a rig that
  otherwise runs on its simulated board, so the real ``stopScan``, task
  teardown, waiter joins and one-shot rest writes execute against them.
* :func:`slow` -- wraps a method with a sleep, to make one device realistically
  slow at one seam.
"""
import collections
import contextlib
import sys
import threading
import time

from qtpy import QtCore, QtTest


class GapProbe:
    """Longest GUI-thread gap between two heartbeats, and where it went."""

    def __init__(self, intervalMs=5, sampleS=0.01, lateS=0.03, depth=3):
        self._interval = intervalMs / 1000.0
        self._timer = QtCore.QTimer()
        self._timer.setInterval(intervalMs)
        self._timer.timeout.connect(self._beat)
        self._mainId = threading.get_ident()
        self._sampleS = sampleS
        self._lateS = lateS
        self._depth = depth
        self._last = None
        self._stop = threading.Event()
        self._sampler = None
        self.gaps = []
        self.where = collections.Counter()

    def _beat(self):
        now = time.monotonic()
        if self._last is not None:
            self.gaps.append(now - self._last)
        self._last = now

    def start(self):
        self._last = time.monotonic()
        self._stop.clear()
        self._timer.start()
        self._sampler = threading.Thread(target=self._sample, daemon=True,
                                         name='GapProbeSampler')
        self._sampler.start()
        return self

    def stop(self):
        self._timer.stop()
        self._stop.set()
        if self._sampler is not None:
            self._sampler.join(1.0)
        self._beat()
        return self.maxGap

    @property
    def maxGap(self):
        return max(self.gaps) if self.gaps else 0.0

    def _sample(self):
        while not self._stop.wait(self._sampleS):
            last = self._last
            if last is None or time.monotonic() - last < self._lateS:
                continue
            frame = sys._current_frames().get(self._mainId)
            labels = []
            while frame is not None:
                code = frame.f_code
                if 'imswitch' in code.co_filename and '_test' not in code.co_filename:
                    labels.append(f'{code.co_filename.split("imswitch/")[-1]}:'
                                  f'{code.co_name}')
                frame = frame.f_back
            # innermost imswitch frames name what is being waited for
            self.where[' < '.join(labels[:self._depth])] += 1

    def top(self, n=4):
        return [(name, count * self._sampleS) for name, count in self.where.most_common(n)]


def slow(obj, name, seconds):
    """Make ``obj.name`` sleep ``seconds`` before it runs; returns the undo."""
    original = getattr(obj, name)

    def delayed(*args, **kwargs):
        time.sleep(seconds)
        return original(*args, **kwargs)
    setattr(obj, name, delayed)
    return lambda: setattr(obj, name, original)


class FakeDaqTask:
    """A driver task with the timing of the real one.

    ``stopS`` / ``closeS``: how long ``stop`` / ``close`` block (a stop that
    waits for the buffer in flight is ``stopS`` of the rest of the scan).
    ``playS``: how long ``wait_until_done`` blocks before the waveform ends on
    its own (it returns at once when the task is stopped or closed -- the
    driver aborts a wait on stop).
    """

    def __init__(self, name='task', *, stopS=0.0, closeS=0.0, playS=3600.0,
                 generated=1):
        self.name = name
        self.stopS, self.closeS, self.playS = stopS, closeS, playS
        self.calls = []
        self._ended = threading.Event()
        self.timing = type('T', (), {'samp_clk_max_rate': 2.0e6,
                                     'samp_clk_rate': 100000.0})()
        self.out_stream = type('O', (), {'total_samp_per_chan_generated': generated})()

    def write(self, values, auto_start=False):
        self.calls.append(('write', auto_start))

    def start(self):
        self.calls.append(('start',))

    def wait_until_done(self, timeout=None):
        self.calls.append(('wait_until_done', timeout))
        self._ended.wait(self.playS)

    def stop(self):
        self.calls.append(('stop',))
        time.sleep(self.stopS)
        self._ended.set()

    def close(self):
        self.calls.append(('close',))
        time.sleep(self.closeS)
        self._ended.set()


def install_daq(nidaq, *, ao=None, do=None, timer=None, rest=None, log=None):
    """Put driver-like tasks into the simulated ``nidaq`` when a scan starts.

    ``ao`` / ``do`` / ``timer``: kwargs of :class:`FakeDaqTask` for the run's
    output tasks (``None``: no such task).  ``rest``: kwargs for the one-shot
    tasks ``stopScan`` writes the rest levels with; ``rest['playS']`` is the
    time the write takes to play out (the ramp).  ``log``: a list that gets
    ``(task name, call)`` in the order the driver sees them.
    Returns the dict of created tasks.
    """
    from imswitch.imcontrol.model.managers.NidaqManager import WaitThread

    made = {}

    def logged(task):
        if log is None:
            return task
        for call in ('write', 'start', 'wait_until_done', 'stop', 'close'):
            original = getattr(task, call)

            def wrapper(*a, _o=original, _c=call, _t=task, **k):
                log.append((_t.name, _c))
                return _o(*a, **k)
            setattr(task, call, wrapper)
        return task

    def oneShot(name, *args, **kwargs):
        task = logged(FakeDaqTask(name, **(rest or {'playS': 0.001})))
        made[name] = task
        return task
    nidaq._NidaqManager__createChanAOTask = oneShot
    nidaq._NidaqManager__createLineDOTask = oneShot

    def onStarted():
        generation = nidaq._scanGeneration
        for key, spec in (('timer', timer), ('do', do), ('ao', ao)):
            if spec is None:
                continue
            task = logged(FakeDaqTask(key, **spec))
            made[key] = task
            waiter = WaitThread()
            waiter.connect(task)
            with nidaq._scanStateLock:
                nidaq.tasks[key] = task
                nidaq._taskGenerations[key] = generation
                nidaq._taskWaiters[key] = waiter
            waiter.start()
    nidaq.sigScanStarted.connect(onStarted)
    return made


@contextlib.contextmanager
def measured(probe=None):
    probe = probe or GapProbe()
    probe.start()
    try:
        yield probe
    finally:
        probe.stop()


def wait_until(predicate, timeoutS):
    deadline = time.monotonic() + timeoutS
    while not predicate() and time.monotonic() < deadline:
        QtTest.QTest.qWait(10)
    return predicate()
