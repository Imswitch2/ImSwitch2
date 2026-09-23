"""SNOUTY lightsheet deskew reconstructor plugin."""

from typing import TYPE_CHECKING

import numpy as np
from qtpy import QtWidgets

from imswitch.imcommon.model import initLogger
from imswitch.improcess.reconstructors.base import Reconstructor
from ._pipeline import load_restack_deskew_timelapse
from .params_widget import SnoutyParamsWidget
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
    - CPU and GPU deskew (GPU requires CuPy)
    - Single- and multi-timepoint data
    - MS-RESOLFT cycle de-interlacing
    """
    
    name = "SNOUTY deskew"
    id = "snouty"
    file_extensions = ["hdf5", "h5", "tiff"]
    description = "Lightsheet deskew for SNOUTY/OPM/MS-RESOLFT data"
    
    @classmethod
    def default_params(cls) -> dict:
        return {   'device': 'CPU',
        'n_timepoints': 1,
        'c_px': 100.0,
        'alpha_deg': 30.0,
        'dy': 210.0,
        'sample_vx_size': 200.0,
        'camera_offset': 100.0,
        'flip_data': False,
        'cycles': 1,
        'planes_in_cycle': 1,
        'restack': True}

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
    
    def process(
        self, data_obj: 'DataObj', params: dict, context=None
    ) -> SnoutyResult:
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
        def per_timepoint(processor, tp_stack, use_gpu, cp):
            if use_gpu:
                # Convert to CuPy for GPU processing, back to NumPy for storage
                return cp.asnumpy(processor.process_stack(cp.asarray(tp_stack)))
            return processor.process_stack(tp_stack)

        deskewed_timepoints = load_restack_deskew_timelapse(
            data_obj, params,
            per_timepoint_fn=per_timepoint,
            logger=self._logger,
        )

        # The pipeline may take the timepoint count from the recording rather
        # than the widget, so what was actually reconstructed decides the rank.
        if len(deskewed_timepoints) > 1:
            # Stack into 4D: (T, Z, Y, X)
            result_data = np.stack(deskewed_timepoints, axis=0)
        else:
            result_data = deskewed_timepoints[0]
        self._logger.info(f'Reconstruction complete: {result_data.shape}')

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
