"""DirectoryObserver (DO): report each new entry of a root folder, once."""

import os

from qtpy import QtCore

from imswitch.imcommon.model.logging import initLogger


class DirectoryObserver(QtCore.QObject):
    """Checks a root folder at a fixed interval and emits each new entry once.

    An entry is a direct child of the root, file or folder. The DO never looks
    inside an entry and knows nothing about data formats: what an entry *is*
    is decided downstream.

    Entries already in the root are emitted on the first check, so data that
    finished before monitoring started is still reported. What has been
    emitted is remembered across ``stop()`` / ``start()``; a different root
    takes a new DO.
    """

    sigEntryFound = QtCore.Signal(str)  # absolute path of a new entry

    def __init__(self, root: str, interval_ms: int = 1000, parent=None) -> None:
        super().__init__(parent)
        self._root = os.path.abspath(root)
        self._seen: set[str] = set()
        self._root_unreadable = False
        self._logger = initLogger(self, tryInheritParent=False)
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.check)

    @property
    def root(self) -> str:
        return self._root

    @property
    def running(self) -> bool:
        return self._timer.isActive()

    def start(self) -> None:
        """Check now, then once every interval."""
        self.check()
        self._timer.start()

    def stop(self) -> None:
        """Stop checking. What has been seen is kept."""
        self._timer.stop()

    @QtCore.Slot()
    def check(self) -> None:
        """Emit every entry not emitted before, in name order."""
        try:
            names = os.listdir(self._root)
        except OSError as e:
            # Warn once per outage, not once per interval.
            if not self._root_unreadable:
                self._logger.warning(f"Cannot read {self._root}: {e}")
                self._root_unreadable = True
            return

        if self._root_unreadable:
            self._logger.info(f"{self._root} is readable again")
            self._root_unreadable = False

        for n in names:
            path = os.path.join(self._root, n)
            if path not in self._seen:
                self._seen.add(path)
                self.sigEntryFound.emit(path)


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
