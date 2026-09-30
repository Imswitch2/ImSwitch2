"""Reconstruct recordings ImControl hands over in memory, when asked to.

A recording saved with *Save in memory for reconstruction* reaches ImProcess
over the module channel the moment it is finished. With the Multidata
panel's policy set to *Open and reconstruct*, this controller runs the
active reconstructor's batch ``process()`` on every dataset it holds and
publishes the results; with any other policy it does nothing. The recording
is read through ``DataObj`` exactly as the same container on disk would be,
so grouped metadata (``ScanStage:*`` and the like) and ``scanN`` lapse
groups are seen as they are from a file.
"""

from imswitch.imcommon.model.logging import initLogger
from imswitch.improcess.model import DataObj
from imswitch.improcess.model.image_sources import dataset_names, open_memory_container
from imswitch.improcess.model.memory_recording_preferences import (
    POLICY_RECONSTRUCT,
    load_memory_recording_policy,
)
from .basecontrollers import ImProcessWidgetController


class MemoryLiveController(ImProcessWidgetController):
    """Routes completed RAM recordings through the active reconstructor."""

    RESULT_LABEL = "Live (RAM)"

    def __init__(self, *args, mainController=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._mainController = mainController
        self._logger = initLogger(self, tryInheritParent=False)

        self._enabled = load_memory_recording_policy() == POLICY_RECONSTRUCT

        self._moduleCommChannel.memoryRecordings.sigDataSet.connect(self._onMemoryDataSet)
        self._commChannel.sigMemoryRecordingPolicyChanged.connect(self._onPolicyChanged)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def setEnabled(self, enabled: bool) -> None:
        """Enable or disable automatic RAM reconstruction."""
        enabled = bool(enabled)
        if enabled == self._enabled:
            return
        self._enabled = enabled
        self._logger.info(f"Memory live reconstruction {'enabled' if enabled else 'disabled'}")

    def _onPolicyChanged(self, policy: str) -> None:
        self.setEnabled(str(policy) == POLICY_RECONSTRUCT)

    def _onMemoryDataSet(self, name: str, vFileItem) -> None:
        """A recording arrived in memory: reconstruct each dataset it holds."""
        if not self._enabled:
            return

        try:
            container = open_memory_container(vFileItem.data)
        except Exception as error:
            self._logger.debug(
                f"Skipping memory recording '{name}': it is not a readable "
                f"container ({error})"
            )
            return

        reconstructor = self._getActiveReconstructor()
        if reconstructor is None:
            self._logger.warning(
                f"No active reconstructor available for memory recording '{name}'"
            )
            return
        params = self._getReconstructorParams()
        filePath = vFileItem.filePath if getattr(vFileItem, 'savedToDisk', False) else None

        try:
            names = dataset_names(container)
        except Exception as error:
            self._logger.error(
                f"Could not list the datasets of memory recording '{name}': {error}"
            )
            return
        for datasetName in names:
            self._processDataset(name, datasetName, container, filePath, reconstructor, params)

    def _processDataset(self, name, datasetName, container, filePath, reconstructor,
                        params: dict) -> None:
        try:
            dataObj = DataObj(name, datasetName, path=filePath, file=container)

            from imswitch.improcess.reconstructors.run import run_reconstruction

            result = run_reconstruction(reconstructor, dataObj, params).result
            self._commChannel.sigResultProduced.emit(result, self.RESULT_LABEL)
            self._logger.info(
                f"Completed RAM reconstruction: {reconstructor.name} on {name}/{datasetName}"
            )
        except Exception as error:
            self._logger.error(
                f"Failed to reconstruct dataset '{datasetName}' of memory recording "
                f"'{name}': {error}",
                exc_info=True,
            )

    def _getActiveReconstructor(self):
        """Get the active reconstructor from the main view controller."""
        if self._mainController is None:
            return None
        return getattr(self._mainController, '_activeReconstructor', None)

    def _getReconstructorParams(self) -> dict:
        """Get the current reconstructor parameters from the view."""
        widget = getattr(self._mainController, '_widget', None)
        if widget is None:
            return {}

        getter = getattr(widget, 'getReconstructionParams', None)
        if callable(getter):
            try:
                return getter()
            except Exception as exc:
                self._logger.warning(f"Could not read reconstruction params from view: {exc}")

        par_tree = getattr(widget, 'parTree', None)
        for legacy_getter_name in ('get_values', 'get_param_dict'):
            legacy_getter = getattr(par_tree, legacy_getter_name, None)
            if callable(legacy_getter):
                try:
                    return legacy_getter()
                except Exception as exc:
                    self._logger.warning(
                        f"Could not read reconstruction params via {legacy_getter_name}: {exc}"
                    )

        return {}


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
