"""Live mode: watch a root folder and reconstruct the raw data that appears in it."""

import os

from imswitch.improcess.live.dataset_resolver import DatasetResolver, EntryInfo
from imswitch.improcess.live.directory_observer import DirectoryObserver
from .basecontrollers import ImProcessWidgetController


class DirectoryWatcherController(ImProcessWidgetController):
    """Built up from the requirements, one at a time.

    So far: the DO reports entries, and the DR resolves them into datasets.
    """

    def __init__(self, *args, main_controller=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._main_controller = main_controller
        self._observer: DirectoryObserver | None = None
        self._resolver: DatasetResolver | None = None

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

        # Same root: keep the DO and the DR, and with them what has already
        # been seen and what is still pending. A new root starts both fresh.
        if self._observer is None or not _same_path(self._observer.root, root):
            for unit in (self._observer, self._resolver):
                if unit is not None:
                    unit.deleteLater()
            self._observer = DirectoryObserver(root, parent=self)
            self._resolver = DatasetResolver(parent=self)
            self._observer.sigEntryFound.connect(self._on_entry_found)
            self._resolver.sigResolved.connect(self._on_dataset_resolved)

        self._logger.info(f"Monitoring {self._observer.root}")
        self._resolver.start()
        self._observer.start()

    def _stop_monitoring(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._resolver.stop()
            self._logger.info("Monitoring stopped")

    def _on_entry_found(self, path: str) -> None:
        self._logger.info(f"Entry found: {os.path.basename(path)}")
        self._resolver.add(path)

    def _on_dataset_resolved(self, dataset: EntryInfo) -> None:
        S = "".join(f", {name}={size}" for name, size in dataset.S)
        T = "?" if dataset.T is None else dataset.T
        self._logger.info(
            f"Dataset: {os.path.basename(dataset.entry)} -- {dataset.packaging} "
            f"{dataset.format}, (T={T}{S}, Y={dataset.frame_shape[0]}, "
            f"X={dataset.frame_shape[1]}) {dataset.dtype}"
        )


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
