"""Application shutdown state shared between modules.

``launchApp`` runs a *prepare* phase before any module's ``closeEvent``:
modules that own threads able to reach hardware (scripting, above all)
drain them there and record the outcome here. ``imcontrol`` reads it before
finalizing hardware managers and fails closed - exactly as it already does
when its own controller workers or the server thread do not drain.
"""

import threading


class ShutdownState:
    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self.begun = False
            #: None = no scripting module took part; True/False = drain outcome.
            self.scriptingDrained = None
            self.reasons = []

    def begin(self):
        with self._lock:
            self.begun = True

    def recordScriptingDrain(self, drained, reason=''):
        with self._lock:
            self.scriptingDrained = bool(drained)
            if reason:
                self.reasons.append(reason)

    def hardwareFinalizationAllowed(self):
        """False only when a participating module reported an undrained
        worker; no participation means no objection."""
        with self._lock:
            return self.scriptingDrained is not False


shutdownState = ShutdownState()


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
