"""Controller for the ImProcess Metadata panel.

Keeps the panel pointed at whatever file the user is working on: it follows
the current DataObj, and lets the user peek into any other supported file
without loading its pixels.
"""

from pathlib import Path

from qtpy import QtWidgets

import imswitch.imcommon.view.guitools as guitools
from imswitch.improcess.model.dataset_sources import (
    LOCATOR_DIRECTORY,
    SOURCE_SPECS,
    file_dialog_filter,
)
from imswitch.improcess.model.metadata_tree import (
    metadata_tree_from_container,
    metadata_tree_from_result,
    read_metadata_tree,
)
from .basecontrollers import ImProcessWidgetController


class MetadataController(ImProcessWidgetController):
    """Feeds the Metadata panel with metadata trees read from data files."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._sourcePath = None
        # The in-session result the panel follows, when the selection in the
        # reconstruction list is one; None while the panel shows a file.
        self._currentResult = None

        self._commChannel.sigCurrentDataChanged.connect(self.currentDataChanged)
        # Results made in the session (a duplicate, a crop, a processor's
        # output) have no file; the panel follows them through the list's
        # selection so it stays current with what is being done live.
        resultChanged = getattr(self._commChannel, "sigCurrentResultChanged", None)
        if resultChanged is not None:
            resultChanged.connect(self.currentResultChanged)
        self._widget.sigOpenFileRequested.connect(self.openFile)
        self._widget.sigReloadRequested.connect(self.reload)

    # -- entry points --------------------------------------------------------

    def currentResultChanged(self, result) -> None:
        """Show the selected result's own metadata and provenance.

        A file loaded as data keeps its file view (``currentDataChanged``);
        selecting a result in the list switches the panel to that result,
        including everything done to it in this session. Deselecting keeps
        whatever was last shown rather than blanking the panel.
        """
        if result is None or not hasattr(result, "axis_labels"):
            return
        self._currentResult = result
        self._showResult()

    def currentDataChanged(self, dataObj) -> None:
        """Show the metadata of the DataObj that just became current."""
        self._currentResult = None
        if dataObj is None:
            self._sourcePath = None
            self._widget.setMetadataTree(None, sourceLabel="No file loaded")
            self._widget.setReloadEnabled(False)
            return

        path = getattr(dataObj, "dataPath", None)
        container = getattr(dataObj, "_file", None)
        self._sourcePath = str(path) if path else None

        # Prefer the handle the DataObj already holds: reopening an HDF5 file
        # that is open elsewhere in this process can fail on file locking, and
        # a live recording may still be being written to.
        if container is not None:
            try:
                tree = metadata_tree_from_container(
                    container,
                    name=Path(path).name if path else getattr(dataObj, "name", None),
                    source_path=path,
                )
            except Exception as exc:
                self._logger.warning(f"Could not read metadata from open file: {exc}")
                tree = None
            if tree is not None:
                self._show(tree, path)
                return

        if self._sourcePath:
            self.reload()
        else:
            self._widget.setMetadataTree(None, sourceLabel="Current data has no file on disk")
            self._widget.setReloadEnabled(False)

    def openFile(self) -> None:
        """Ask for any supported file and show its metadata (no data loaded)."""
        path = self._requestPath()
        if not path:
            return
        self._sourcePath = str(path)
        self.reload()

    def reload(self) -> None:
        """Re-read the metadata of the current source path, or rebuild the
        current result's tree (its provenance grows as it is processed)."""
        if self._currentResult is not None:
            self._showResult()
            return
        if not self._sourcePath:
            return
        try:
            tree = read_metadata_tree(self._sourcePath)
        except Exception as exc:
            self._logger.warning(f"Could not read metadata from {self._sourcePath}: {exc}")
            self._widget.setMetadataTree(None, sourceLabel=str(self._sourcePath))
            self._widget.setStatus(f"Could not read metadata: {exc}")
            self._widget.setReloadEnabled(True)
            return
        self._show(tree, self._sourcePath)

    # -- internals -----------------------------------------------------------

    def _showResult(self) -> None:
        result = self._currentResult
        try:
            tree = metadata_tree_from_result(result)
        except Exception as exc:
            self._logger.warning(f"Could not describe result {getattr(result, 'name', result)!r}: {exc}")
            self._widget.setStatus(f"Could not describe result: {exc}")
            return
        label = f"{getattr(result, 'name', 'result')} (result in session)"
        self._widget.setMetadataTree(tree, sourceLabel=label)
        self._widget.setReloadEnabled(True)

    def _show(self, tree, path) -> None:
        self._widget.setMetadataTree(tree, sourceLabel=str(path) if path else tree.name)
        self._widget.setReloadEnabled(bool(self._sourcePath))

    def _requestPath(self):
        """File-or-folder chooser: Zarr stores are directories, the rest files."""
        fileSpecs = [spec for spec in SOURCE_SPECS if spec.locator != LOCATOR_DIRECTORY]
        directorySpecs = [spec for spec in SOURCE_SPECS if spec.locator == LOCATOR_DIRECTORY]

        choices = []
        if fileSpecs:
            labels = " / ".join(spec.label for spec in fileSpecs)
            choices.append((f"{labels} file", None))
        for spec in directorySpecs:
            choices.append((f"{spec.label} folder", spec))

        if len(choices) > 1:
            selected, accepted = QtWidgets.QInputDialog.getItem(
                self._widget,
                "Read metadata from",
                "Source type",
                [label for label, _spec in choices],
                0,
                False,
            )
            if not accepted:
                return None
            spec = next((spec for label, spec in choices if label == selected), None)
        else:
            spec = choices[0][1] if choices else None

        if spec is not None and spec.locator == LOCATOR_DIRECTORY:
            return guitools.askForFolderPath(
                self._widget, caption=f"Read metadata from {spec.label}"
            )
        return guitools.askForFilePath(
            self._widget,
            caption="Read metadata from file",
            nameFilter=file_dialog_filter(fileSpecs),
        )


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
