"""SNOUTY lightsheet deskew reconstructor plugin."""

from typing import TYPE_CHECKING

import numpy as np
from qtpy import QtWidgets

from imswitch.imcommon.model import initLogger
from imswitch.improcess.reconstructors.base import Reconstructor
from .deskew_cpu import DeskewProcessorCPU
from .params_widget import SnoutyParamsWidget
from .restack import restack_interleaved
from .result import SnoutyResult

if TYPE_CHECKING:
    from imswitch.improcess.model import DataObj


class SnoutyReconstructor(Reconstructor):
    """
    SNOUTY lightsheet deskew reconstructor.
    
    Geometric transformation of obliquely-illuminated lightsheet data
    (SNOUTY / OPM / MS-RESOLFT) from camera space (planes, cam_y, cam_x)
    to sample space (sample_z, sample_y, sample_x).
    
    Supports:
    - CPU-only deskew (Phase D.1)
    - Single- and multi-timepoint data
    - MS-RESOLFT cycle de-interlacing
    
    GPU path arrives in Phase D.2.
    """
    
    name = "SNOUTY deskew"
    id = "snouty"
    file_extensions = ["hdf5", "h5", "tiff"]
    description = "Lightsheet deskew for SNOUTY/OPM/MS-RESOLFT data"
    
    def __init__(self):
        self._logger = initLogger('SnoutyReconstructor')
    
    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        """Create and return the SNOUTY parameter widget."""
        return SnoutyParamsWidget(parent)
    
    def make_metadata_dialog(self, parent: QtWidgets.QWidget) -> QtWidgets.QDialog | None:
        """
        No separate metadata dialog needed.
        
        Geometry parameters are in the param widget and auto-detected from
        HDF5 attributes when available.
        """
        return None
    
    def process(self, data_obj: 'DataObj', params: dict) -> SnoutyResult:
        """
        Run SNOUTY deskew reconstruction.
        
        Args:
            data_obj: DataObj containing raw scan data (3D: planes, cam_y, cam_x)
            params: Parameter dict from SnoutyParamsWidget.get_values() with keys:
                - device: str ('CPU' or 'GPU')
                - n_timepoints: int (≥1)
                - c_px, alpha_deg, dy, sample_vx_size: float (geometry)
                - camera_offset: float (ADU)
                - flip_data: bool
                - cycles, planes_in_cycle: int (MS-RESOLFT)
                - restack: bool
        
        Returns:
            SnoutyResult containing deskewed 3D or 4D volume
        """
        # Load data
        preloaded = data_obj.dataLoaded
        try:
            data_obj.checkAndLoadData()
            stack = data_obj.data
        finally:
            if not preloaded:
                data_obj.checkAndUnloadData()
        
        # Validate data shape
        if stack.ndim != 3:
            raise ValueError(
                f'Expected 3D data (planes, cam_y, cam_x), got shape {stack.shape}'
            )
        
        # De-interlace if requested (MS-RESOLFT)
        if params.get('restack', True):
            cycles = params.get('cycles', 1)
            planes_in_cycle = params.get('planes_in_cycle', 1)
            if cycles > 1 or planes_in_cycle > 1:
                stack = restack_interleaved(stack, cycles, planes_in_cycle)
                self._logger.info(
                    f'Restacked {cycles} cycles × {planes_in_cycle} planes/cycle'
                )
        
        # Pick processor (CPU or GPU)
        device = params.get('device', 'CPU').upper()
        if device == 'GPU':
            try:
                from .deskew_gpu import DeskewProcessorGPU
                processor_class = DeskewProcessorGPU
            except (ImportError, RuntimeError) as e:
                raise RuntimeError(
                    f'GPU deskew unavailable: {e}. Use device="CPU" or install CuPy.'
                ) from e
        else:
            processor_class = DeskewProcessorCPU
        
        # Build processor config (subset of params that constructor expects)
        processor_params = {
            'c_px': params['c_px'],
            'alpha_deg': params['alpha_deg'],
            'dy': params['dy'],
            'sample_vx_size': params['sample_vx_size'],
            'camera_offset': params.get('camera_offset', 0.0),
            'flip_data': params.get('flip_data', False),
        }
        processor = processor_class(processor_params)
        
        # Timelapse handling
        n_timepoints = params.get('n_timepoints', 1)
        if n_timepoints > 1:
            self._logger.info(
                f'Processing {n_timepoints} timepoints with {device} deskew...'
            )
            # Split stack into timepoints along axis 0
            timepoint_stacks = np.array_split(stack, n_timepoints, axis=0)
            deskewed_timepoints = []
            for t, tp_stack in enumerate(timepoint_stacks):
                self._logger.info(f'  Timepoint {t+1}/{n_timepoints}...')
                deskewed = processor.process_stack(tp_stack)
                deskewed_timepoints.append(deskewed)
            # Stack into 4D: (T, Z, Y, X)
            result_data = np.stack(deskewed_timepoints, axis=0)
            self._logger.info(
                f'Reconstruction complete: {result_data.shape} (T, Z, Y, X)'
            )
        else:
            self._logger.info(f'Processing single timepoint with {device} deskew...')
            result_data = processor.process_stack(stack)
            self._logger.info(
                f'Reconstruction complete: {result_data.shape} (Z, Y, X)'
            )
        
        # Compute display levels
        data_min = float(np.percentile(result_data, 1))
        data_max = float(np.percentile(result_data, 99.9))
        
        # Create result
        return SnoutyResult(
            name=data_obj.name,
            data=result_data,
            params=params,
            display_levels=(data_min, data_max)
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
