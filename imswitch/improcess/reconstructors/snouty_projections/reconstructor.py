"""SNOUTY lightsheet projections reconstructor plugin (fast preview path)."""

from typing import TYPE_CHECKING

import numpy as np
from qtpy import QtWidgets

from imswitch.imcommon.model import initLogger
from imswitch.improcess.reconstructors.base import Reconstructor
from imswitch.improcess.reconstructors.snouty.deskew_cpu import DeskewProcessorCPU
from imswitch.improcess.reconstructors.snouty.params_widget import SnoutyParamsWidget
from imswitch.improcess.reconstructors.snouty.restack import restack_interleaved
from .result import SnoutyProjectionsResult

if TYPE_CHECKING:
    from imswitch.improcess.model import DataObj


class SnoutyProjectionsReconstructor(Reconstructor):
    """
    SNOUTY lightsheet projections-only reconstructor (fast preview).
    
    Computes max-projections (xy, xz, yz) of the deskewed canvas without
    smoothing. Much faster than full volume reconstruction, ideal for
    previewing large or multi-timepoint datasets.
    
    Shares the same parameter widget, metadata adapter, and deskew processors
    as the full-volume SNOUTY plugin. Only the process() body differs.
    
    Output:
        - 3D: (3, H, W) — three projections padded to common canvas
        - 4D: (T, 3, H, W) — multi-timepoint projections
    """
    
    name = "SNOUTY projections"
    id = "snouty-projections"
    file_extensions = ["hdf5", "h5", "tiff"]
    description = "Fast projection-only preview for SNOUTY/OPM/MS-RESOLFT data"
    
    def __init__(self):
        self._logger = initLogger('SnoutyProjectionsReconstructor')
    
    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        """Create and return the SNOUTY parameter widget (shared with full-volume plugin)."""
        return SnoutyParamsWidget(parent)
    
    def make_metadata_dialog(self, parent: QtWidgets.QWidget) -> QtWidgets.QDialog | None:
        """
        No separate metadata dialog needed.
        
        Geometry parameters are in the param widget and auto-detected from
        HDF5 attributes when available.
        """
        return None
    
    def process(self, data_obj: 'DataObj', params: dict) -> SnoutyProjectionsResult:
        """
        Run SNOUTY projections reconstruction.
        
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
            SnoutyProjectionsResult containing three padded projections (3D or 4D)
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
        use_gpu = False
        if device == 'GPU':
            try:
                from imswitch.improcess.reconstructors.snouty.deskew_gpu import DeskewProcessorGPU
                import cupy as cp
                processor_class = DeskewProcessorGPU
                use_gpu = True
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
                f'Processing {n_timepoints} timepoint projections with {device} deskew...'
            )
            # Split stack into timepoints along axis 0
            timepoint_stacks = np.array_split(stack, n_timepoints, axis=0)
            projection_timepoints = []
            for t, tp_stack in enumerate(timepoint_stacks):
                self._logger.info(f'  Timepoint {t+1}/{n_timepoints}...')
                if use_gpu:
                    projections = processor.process_projections(cp.asarray(tp_stack))
                    try:
                        cp.get_default_memory_pool().free_all_blocks()
                    except Exception:
                        pass
                else:
                    projections = processor.process_projections(tp_stack)
                # Pad and stack projections
                padded = self._pad_and_stack_projections(projections)
                projection_timepoints.append(padded)
            # Stack into 4D: (T, 3, H, W)
            result_data = np.stack(projection_timepoints, axis=0)
            self._logger.info(
                f'Projections complete: {result_data.shape} (T, projection, Y, X)'
            )
        else:
            self._logger.info(f'Processing single timepoint projections with {device} deskew...')
            if use_gpu:
                projections = processor.process_projections(cp.asarray(stack))
            else:
                projections = processor.process_projections(stack)
            # Pad and stack projections
            result_data = self._pad_and_stack_projections(projections)
            self._logger.info(
                f'Projections complete: {result_data.shape} (projection, Y, X)'
            )
        
        # Compute display levels
        data_min = float(np.percentile(result_data, 1))
        data_max = float(np.percentile(result_data, 99.9))
        
        # Create result
        return SnoutyProjectionsResult(
            name=f"{data_obj.name}_projections",
            data=result_data,
            params=params,
            display_levels=(data_min, data_max)
        )
    
    def _pad_and_stack_projections(self, projections: dict) -> np.ndarray:
        """
        Pad the three projections to a common canvas and stack.
        
        Args:
            projections: Dict with keys "xy", "xz", "yz", each a 2D float32 array
                - xy: (Sy, Sx)
                - xz: (Sz, Sx)
                - yz: (Sz, Sy)
        
        Returns:
            Padded stack (3, H, W) where H = max(Sy, Sz), W = max(Sx, Sy)
        """
        xy = projections["xy"]  # (Sy, Sx)
        xz = projections["xz"]  # (Sz, Sx)
        yz = projections["yz"]  # (Sz, Sy)
        
        # Determine common canvas size
        Sy, Sx = xy.shape
        Sz_xz, Sx_xz = xz.shape  # Sz, Sx
        Sz_yz, Sy_yz = yz.shape  # Sz, Sy
        
        Sz = Sz_xz  # or Sz_yz, should be the same
        H = max(Sy, Sz)
        W = max(Sx, Sy)
        
        # Pad each projection to (H, W)
        xy_padded = self._pad_to_shape(xy, (H, W))
        xz_padded = self._pad_to_shape(xz, (H, W))
        yz_padded = self._pad_to_shape(yz, (H, W))
        
        # Stack along new axis: (3, H, W)
        return np.stack([xy_padded, xz_padded, yz_padded], axis=0)
    
    def _pad_to_shape(self, array: np.ndarray, target_shape: tuple) -> np.ndarray:
        """
        Pad a 2D array to target shape with zeros.
        
        Args:
            array: 2D array to pad
            target_shape: (H, W) target shape
        
        Returns:
            Padded array of shape target_shape
        """
        H, W = target_shape
        h, w = array.shape
        
        if h > H or w > W:
            raise ValueError(
                f"Array shape {array.shape} exceeds target shape {target_shape}"
            )
        
        # Pad to target shape (pad on right/bottom only)
        pad_h = H - h
        pad_w = W - w
        return np.pad(array, ((0, pad_h), (0, pad_w)), mode='constant', constant_values=0)
    
    def _to_numpy(self, array):
        """Convert CuPy array to NumPy array if needed."""
        if hasattr(array, 'get'):
            return array.get()
        return array


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
