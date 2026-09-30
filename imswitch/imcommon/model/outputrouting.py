"""Per-thread routing of ``sys.stdout`` / ``sys.stderr``.

``contextlib.redirect_stdout`` swaps the process-global stream, so while it is
in force every thread's ``print`` lands in it -- the GUI thread's and the log
handlers' included. A run on a worker thread (a script, a processor) must
capture its own output and nobody else's; this routes by thread instead.
Qt-free.
"""

import sys
import threading


class ThreadRoutingStream:
    """A ``sys.stdout``/``sys.stderr`` replacement that routes each thread's
    writes to a registered sink and everything else to the stream it
    wrapped. Installed once per process, lazily, and never removed: it
    delegates to the original stream, so leaving it in place is harmless,
    while a foreign replacement of ``sys.stdout`` made later (the pyqtgraph
    console swaps it around its own commands) is simply wrapped anew by the
    next run."""

    def __init__(self, fallback):
        self._fallback = fallback
        self._sinks = {}
        self._lock = threading.Lock()

    def register(self, threadIdent, sink):
        """Route ``threadIdent``'s writes to ``sink``; returns the sink it replaced
        (``None`` if it had none), so a nested route can put it back."""
        with self._lock:
            previous = self._sinks.get(threadIdent)
            self._sinks[threadIdent] = sink
            return previous

    def unregister(self, threadIdent, restore=None):
        """Stop routing ``threadIdent``, or go back to ``restore`` if one is given."""
        with self._lock:
            if restore is None:
                self._sinks.pop(threadIdent, None)
            else:
                self._sinks[threadIdent] = restore

    def sinkOf(self, threadIdent):
        with self._lock:
            return self._sinks.get(threadIdent)

    def _target(self):
        return self._sinks.get(threading.get_ident(), self._fallback)

    def write(self, text):
        return self._target().write(text)

    def writelines(self, lines):
        target = self._target()
        for line in lines:
            target.write(line)

    def flush(self):
        target = self._target()
        flush = getattr(target, 'flush', None)
        if callable(flush):
            flush()

    def __getattr__(self, name):
        return getattr(self._fallback, name)


class routeThisThreadsOutputTo:
    """Context manager: route this thread's stdout/stderr writes to ``sink``
    (anything with ``write``).

    Re-entrant: a route opened inside another one (a script run inside a
    processor run, both capturing) puts the outer sink back when it ends, and
    :func:`currentRoute` lets the inner one pass what it captures on to it."""

    def __init__(self, sink):
        self._sink = sink
        self._routers = ()
        self._previous = ()

    def __enter__(self):
        ident = threading.get_ident()
        routers, previous = [], []
        for streamName in ('stdout', 'stderr'):
            stream = getattr(sys, streamName)
            if not isinstance(stream, ThreadRoutingStream):
                stream = ThreadRoutingStream(stream)
                setattr(sys, streamName, stream)
            previous.append(stream.register(ident, self._sink))
            routers.append(stream)
        self._routers = tuple(routers)
        self._previous = tuple(previous)
        return self

    def __exit__(self, *_exc):
        ident = threading.get_ident()
        for router, previous in zip(self._routers, self._previous):
            router.unregister(ident, restore=previous)
        return False


def currentRoute():
    """The sink this thread's ``stdout`` is routed to now, or ``None``."""
    stream = sys.stdout
    if isinstance(stream, ThreadRoutingStream):
        return stream.sinkOf(threading.get_ident())
    return None


# Copyright (C) 2020-2026 ImSwitch developers
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
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
