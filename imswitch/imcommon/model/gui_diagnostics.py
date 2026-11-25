"""Diagnostics for a GUI that stops responding.

Two independent tools, both cheap and both safe to leave in a release:

* :class:`GuiStallWatchdog` -- off by default.  While armed, a heartbeat timer
  on the GUI thread says the event loop is alive; a plain Python thread notices
  when it has not for ``thresholdS`` seconds and writes the main thread's stack
  (and every other thread's) to a file in the log folder, again every
  ``thresholdS`` while the stall lasts, then logs how long it was.  It is the
  tool for "the window went 'not responding' and I do not know where": the
  first dump names the call the GUI thread is sitting in.

* :func:`timedStep` -- a context manager that logs one INFO line per named step
  with its duration and the thread it ran on, so one log from a rig shows where
  the time of a multi-step sequence (a scan stop) went.

Enable the watchdog with ``IMSWITCH_GUI_STALL_SECONDS=2`` in the environment or
``imswitch --gui-stall-seconds 2``; dumps land in
``<ImSwitch user folder>/logs/gui_stall.log`` and each stall also logs a
WARNING line to the normal log.
"""
import contextlib
import logging
import os
import sys
import threading
import time
import traceback
from datetime import datetime

from qtpy import QtCore

_logger = logging.getLogger('imswitch.gui_diagnostics')

ENV_STALL_SECONDS = 'IMSWITCH_GUI_STALL_SECONDS'
STALL_LOG_NAME = 'gui_stall.log'

#: Steps shorter than this are logged at DEBUG, not INFO: the INFO log of a
#: sequence that went well stays short, and the one that did not shows every
#: step that cost real time.
STEP_INFO_THRESHOLD_S = 0.005


@contextlib.contextmanager
def timedStep(sequence, step, logger=None):
    """Log ``sequence`` / ``step`` with its duration and thread when it ends.

    Never raises on its own account and never swallows an exception of the
    wrapped step.  A step that raises is logged as such, with its duration.
    """
    log = logger or _logger
    started = time.monotonic()
    failed = False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        duration = time.monotonic() - started
        level = (logging.INFO if duration >= STEP_INFO_THRESHOLD_S or failed
                 else logging.DEBUG)
        try:
            log.log(level, '[%s] step "%s" took %.1f ms on thread %s%s',
                    sequence, step, duration * 1000.0,
                    threading.current_thread().name,
                    ' (raised)' if failed else '')
        except Exception:  # a logging failure must never break a stop
            pass


def stallThresholdFromEnvironment(environ=None):
    """Seconds from ``IMSWITCH_GUI_STALL_SECONDS``, or ``None`` when unset or
    not a positive number."""
    raw = (environ if environ is not None else os.environ).get(ENV_STALL_SECONDS, '')
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _formatStack(frame):
    return ''.join(traceback.format_stack(frame))


class GuiStallWatchdog:
    """Dump the GUI thread's stack when it stops pumping events.

    Construct on the GUI thread (the heartbeat timer lives there), then
    :meth:`start`.  ``dumpPath`` is appended to, never truncated.
    """

    def __init__(self, thresholdS, dumpPath, *, heartbeatMs=None, pollS=None):
        self.thresholdS = float(thresholdS)
        if self.thresholdS <= 0:
            raise ValueError('The stall threshold must be positive.')
        self.dumpPath = dumpPath
        self._heartbeatMs = int(heartbeatMs or max(20, min(250, self.thresholdS * 1000 / 5)))
        self._pollS = float(pollS or max(0.01, min(0.25, self.thresholdS / 5)))
        self._lastBeat = time.monotonic()
        self._mainId = threading.get_ident()
        self._stop = threading.Event()
        self._thread = None
        self._timer = QtCore.QTimer()
        self._timer.setInterval(self._heartbeatMs)
        self._timer.timeout.connect(self._beat)
        #: Stall durations seen so far (seconds), for tests and diagnostics.
        self.stalls = []
        self.dumpCount = 0

    def _beat(self):
        self._lastBeat = time.monotonic()

    def start(self):
        if self._thread is not None:
            return self
        self._lastBeat = time.monotonic()
        self._timer.start()
        self._thread = threading.Thread(
            target=self._watch, name='GuiStallWatchdog', daemon=True)
        self._thread.start()
        _logger.info('GUI stall watchdog armed: threshold %.1f s, dumps to %s',
                     self.thresholdS, self.dumpPath)
        return self

    def stop(self):
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(2.0)
        try:
            self._timer.stop()
        except RuntimeError:
            pass

    def _watch(self):
        stallStart = None
        nextDump = None
        while not self._stop.wait(self._pollS):
            now = time.monotonic()
            idle = now - self._lastBeat
            if idle >= self.thresholdS:
                if stallStart is None:
                    stallStart = self._lastBeat
                    nextDump = now
                if now >= nextDump:
                    self._dump(now - stallStart)
                    nextDump = now + self.thresholdS
            elif stallStart is not None:
                duration = now - stallStart
                self.stalls.append(duration)
                _logger.warning('GUI thread was unresponsive for %.1f s '
                                '(stacks in %s)', duration, self.dumpPath)
                self._write(f'--- GUI thread responsive again after {duration:.1f} s ---\n\n')
                stallStart = None

    def _dump(self, stalledS):
        frames = sys._current_frames()
        names = {t.ident: t.name for t in threading.enumerate()}
        parts = [f'=== {datetime.now().isoformat(timespec="seconds")} GUI thread '
                 f'not pumping events for {stalledS:.1f} s ===\n']
        mainFrame = frames.get(self._mainId)
        if mainFrame is not None:
            parts.append(f'--- main thread ({names.get(self._mainId, "?")}) ---\n')
            parts.append(_formatStack(mainFrame))
        for ident, frame in frames.items():
            if ident in (self._mainId, threading.get_ident()):
                continue
            parts.append(f'--- thread {names.get(ident, ident)} ---\n')
            parts.append(_formatStack(frame))
        parts.append('\n')
        self.dumpCount += 1
        _logger.warning('GUI thread has not returned to the event loop for %.1f s; '
                        'stack dump %d written to %s', stalledS, self.dumpCount,
                        self.dumpPath)
        self._write(''.join(parts))

    def _write(self, text):
        try:
            os.makedirs(os.path.dirname(self.dumpPath), exist_ok=True)
            with open(self.dumpPath, 'a', encoding='utf-8') as handle:
                handle.write(text)
        except OSError:
            _logger.error('Could not write the GUI stall dump to %s', self.dumpPath,
                          exc_info=True)


def startGuiStallWatchdog(thresholdS, logFolder):
    """Arm a watchdog writing ``<logFolder>/gui_stall.log``; ``None`` when
    ``thresholdS`` is not a positive number."""
    if thresholdS is None or thresholdS <= 0:
        return None
    return GuiStallWatchdog(thresholdS, os.path.join(logFolder, STALL_LOG_NAME)).start()
