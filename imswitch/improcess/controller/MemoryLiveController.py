"""Controller for in-RAM live reconstruction hand-off from imcontrol (Phase P5)."""

import h5py

from imswitch.imcommon.model.logging import initLogger
from imswitch.improcess.live import InMemoryStackWrapper
from .basecontrollers import ImProcessWidgetController


class MemoryLiveController(ImProcessWidgetController):
    """Routes completed RAM recordings through the active reconstructor.
    
    When enabled, subscribes to memory recordings from imcontrol and auto-routes
    newly-arrived HDF5-backed in-RAM datasets through the active reconstructor's
    batch process() path. Emits results via sigResultProduced for display in
    ImProcess ReconstructionView.
    
    Scoped to completed HDF5 RAM recordings for v1 (Phase P5).
    """

    def __init__(self, *args, mainController=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._mainController = mainController
        self._logger = initLogger(self, tryInheritParent=False)
        
        self._enabled = False
        
        self._moduleCommChannel.memoryRecordings.sigDataSet.connect(self._onMemoryDataSet)

    def setEnabled(self, enabled: bool) -> None:
        """Enable or disable automatic RAM reconstruction."""
        self._enabled = enabled
        self._logger.info(f"Memory live reconstruction {'enabled' if enabled else 'disabled'}")

    def _onMemoryDataSet(self, name: str, vFileItem) -> None:
        """Handle a new memory recording arrival from imcontrol.
        
        Args:
            name: Recording name (from RecordingManager).
            vFileItem: VFile item containing the recording data.
        """
        if not self._enabled:
            return
        
        try:
            data = vFileItem.data
            if not isinstance(data, h5py.File):
                try:
                    data = h5py.File(data)
                except Exception as e:
                    self._logger.debug(
                        f"Skipping non-HDF5 memory recording '{name}': {e} "
                        "(only HDF5 RAM recordings supported in v1)"
                    )
                    return
            
            reconstructor = self._getActiveReconstructor()
            if reconstructor is None:
                self._logger.warning(
                    f"No active reconstructor available for memory recording '{name}'"
                )
                return
            
            params = self._getReconstructorParams()
            
            for datasetName in data.keys():
                self._processDataset(name, datasetName, data, reconstructor, params)
        
        except Exception as e:
            self._logger.error(f"Failed to process memory recording '{name}': {e}", exc_info=True)

    def _processDataset(self, name: str, datasetName: str, data: h5py.File,
                       reconstructor, params: dict) -> None:
        """Process a single dataset from an HDF5 memory recording.
        
        Args:
            name: Recording name.
            datasetName: Dataset name within the HDF5 file.
            data: h5py.File handle.
            reconstructor: Active reconstructor instance.
            params: Reconstruction parameters.
        """
        try:
            dataset = data[datasetName]
            
            if not isinstance(dataset, h5py.Dataset):
                if isinstance(dataset, h5py.Group) and 'data' in dataset:
                    dataset = dataset['data']
                else:
                    self._logger.debug(f"Skipping non-dataset key '{datasetName}' in '{name}'")
                    return
            
            array = dataset[:]
            attrs = dict(dataset.attrs)
            
            root_attrs = dict(data.attrs)
            attrs.update(root_attrs)
            
            wrapper = InMemoryStackWrapper(
                name=name,
                dataset_name=datasetName,
                data=array,
                attrs=attrs,
            )
            
            result = reconstructor.process(wrapper, params)
            
            self._commChannel.sigResultProduced.emit(result, "Live (RAM)")
            self._logger.info(
                f"Completed RAM reconstruction: {reconstructor.name} on {name}/{datasetName}"
            )
        
        except Exception as e:
            self._logger.error(
                f"Failed to process dataset '{datasetName}' from memory recording '{name}': {e}",
                exc_info=True
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
