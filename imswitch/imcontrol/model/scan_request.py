"""Script/remote-facing scan request handles (plan A-05).

``runScan()`` returns a :class:`ScanRunHandle` bound to the exact completion
the accepting controller reports, so a caller can wait for *this* scan
regardless of when it created any signal waiter. The handle is cooperative
(it honours the caller's cancel token), refuses to block the GUI thread, and
serializes to a plain dict for REST/Pyro clients, which poll
``getScanRequestStatus(requestId)`` instead of holding the live object.
"""

import threading
import time
import uuid
from collections import OrderedDict

from qtpy import QtCore

from imswitch.imcommon.model.cancellation import checkpoint


class ScanRequestRejectedError(RuntimeError):
    """The scan controller refused to start the requested scan. No lifecycle
    signal was published for it."""


class ScanFailedError(RuntimeError):
    """The scan started but did not complete successfully."""


def _onGuiThread():
    app = QtCore.QCoreApplication.instance()
    return app is not None and QtCore.QThread.currentThread() is app.thread()


class ScanRunHandle:
    """Completion handle for one accepted scan request.

    ``exact`` is True when the handle is bound to the accepting controller's
    identity-scoped completion; False when it falls back to the global
    ``scanEnded`` signal observed from before dispatch (a scan source that
    does not report on requests)."""

    def __init__(self, source, completion, *, exact=True, requestId=None):
        self.requestId = requestId or uuid.uuid4().hex[:12]
        self.source = source
        self.exact = bool(exact)
        self._completion = completion

    @property
    def done(self) -> bool:
        return bool(self._completion.wait(0))

    @property
    def successful(self):
        """True/False once done, None while pending."""
        return self._completion.successful if self.done else None

    @property
    def message(self) -> str:
        return self._completion.message

    @property
    def state(self) -> str:
        if not self.done:
            return 'pending'
        return 'succeeded' if self._completion.successful else 'failed'

    def add_done_callback(self, callback):
        """``callback(handle)`` once the scan has ended (immediately if it
        already has). Safe from the GUI thread."""
        self._completion.add_done_callback(lambda _completion: callback(self))

    def wait(self, timeout=None, pollIntervalSeconds=0.05) -> bool:
        """Block until the scan has ended. Returns False on timeout.

        Cooperative: raises ``OperationCancelled`` when the calling script is
        stopped. Refuses to run on the GUI thread, where blocking would stop
        the very callbacks that resolve it (use ``done``, ``wait(timeout=0)``
        or ``add_done_callback`` there)."""
        if timeout is not None and float(timeout) <= 0:
            return self.done
        if _onGuiThread():
            raise RuntimeError(
                'ScanRunHandle.wait() cannot block the GUI thread; use '
                'handle.done, handle.wait(timeout=0) or add_done_callback().'
            )
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        while True:
            checkpoint()
            if self._completion.wait(0):
                return True
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._completion.wait(min(pollIntervalSeconds, remaining))
            else:
                self._completion.wait(pollIntervalSeconds)

    def to_dict(self) -> dict:
        return {
            'requestId': self.requestId,
            'source': self.source,
            'state': self.state,
            'message': self.message,
            'exact': self.exact,
        }

    def __repr__(self):
        return (
            f'ScanRunHandle(requestId={self.requestId!r}, source={self.source!r}, '
            f'state={self.state!r})'
        )


class ScanRequestRegistry:
    """Bounded registry of recent handles, keyed by request id, so that
    remote clients can poll their status after the call returned."""

    def __init__(self, capacity=64):
        self._capacity = int(capacity)
        self._handles = OrderedDict()
        self._lock = threading.Lock()

    def register(self, handle):
        with self._lock:
            self._handles[handle.requestId] = handle
            while len(self._handles) > self._capacity:
                self._handles.popitem(last=False)
        return handle

    def get(self, requestId):
        with self._lock:
            return self._handles.get(requestId)


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
