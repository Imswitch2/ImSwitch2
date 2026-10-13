"""Live mode: watch a root folder and reconstruct the raw data that appears in it."""

import os

from imswitch.improcess.live.directory_observer import DirectoryObserver
from .basecontrollers import ImProcessWidgetController


class DirectoryWatcherController(ImProcessWidgetController):
    """Built up from the requirements, one at a time. So far: monitoring."""

    def __init__(self, *args, main_controller=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._main_controller = main_controller
        self._observer: DirectoryObserver | None = None

        self._widget.sigMonitorChanged.connect(self._on_monitor_changed)

    def _on_monitor_changed(self, enabled: bool) -> None:
        if enabled:
            self._start_monitoring()
        else:
            self._stop_monitoring()

    def _start_monitoring(self) -> None:
        root = self._widget.path
        if not root or not os.path.isdir(root):
            self._logger.error(f"Not a folder: {root!r}")
            self._widget.monitorFolderCheck.setChecked(False)
            return

        # Same root: keep the DO, and with it what has already been seen.
        if self._observer is None or not _same_path(self._observer.root, root):
            if self._observer is not None:
                self._observer.deleteLater()
            self._observer = DirectoryObserver(root, parent=self)
            self._observer.sigEntryFound.connect(self._on_entry_found)

        self._logger.info(f"Monitoring {self._observer.root}")
        self._observer.start()

    def _stop_monitoring(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._logger.info("Monitoring stopped")

    def _on_entry_found(self, path: str) -> None:
        self._logger.info(f"Entry found: {path}")


def _same_path(a: str, b: str) -> bool:
    """Whether two paths name the same folder (case-insensitive on Windows)."""
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


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
