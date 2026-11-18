"""SNOUTY lightsheet deskew reconstructor plugin."""

from typing import TYPE_CHECKING

import numpy as np
from qtpy import QtWidgets

from imswitch.imcommon.model import initLogger
from imswitch.improcess.model.param_spec import ParamField
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
    output_kind = "image"
    file_extensions = ["hdf5", "h5", "tiff"]
    description = "Lightsheet deskew for SNOUTY/OPM/MS-RESOLFT data"

    #: One deskew consumes one skewed stack. A ``(T, Z, Y, X)`` input (a
    #: time-lapse a Python step has reshaped) is auto-indexed over T by the
    #: runner; the 3D layout with ``n_timepoints`` along the plane axis stays
    #: as it was. The plane axis goes by several names.
    core_dims_in = ("Z|Frame|Plane", "Y", "X")
    core_dims_out = ("Z", "Y", "X")
    
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

    @classmethod
    def param_spec(cls) -> tuple:
        return (
            ParamField('device', 'select', 'CPU', label='Device', options=('CPU', 'GPU')),
            ParamField('n_timepoints', 'int', 1, label='Timepoints', min=1, max=9999),
            ParamField(
                'c_px', 'float', 100.0, label='Camera pixel size', min=1, max=10000, suffix='nm',
                group='Geometry',
            ),
            ParamField(
                'alpha_deg', 'float', 30.0, label='Tilt angle', min=0, max=90, suffix='°',
                group='Geometry',
            ),
            ParamField(
                'dy', 'float', 210.0, label='Scan step', min=1, max=10000, suffix='nm',
                group='Geometry',
            ),
            ParamField(
                'sample_vx_size', 'float', 200.0, label='Output voxel size', min=1, max=10000,
                suffix='nm', group='Geometry',
            ),
            ParamField(
                'camera_offset', 'float', 100.0, label='Camera offset', min=0, max=65535,
                suffix='ADU', group='Acquisition',
            ),
            ParamField('flip_data', 'bool', False, label='Flip data', group='Acquisition'),
            ParamField('cycles', 'int', 1, label='Cycles', min=1, max=9999, group='Acquisition'),
            ParamField(
                'planes_in_cycle', 'int', 1, label='Planes per cycle', min=1, max=9999,
                group='Acquisition',
            ),
            ParamField('restack', 'bool', True, label='Restack', group='Acquisition'),
        )

    def __init__(self):
        self._logger = initLogger('SnoutyReconstructor')
    
    def check_input(self, data_obj, params: dict | None) -> list[str]:
        """The core-dims check, and no double loop over time.

        With a time axis in front of the stack the runner already calls the
        deskew once per timepoint; splitting each stack again into
        ``n_timepoints`` would cut one timepoint's planes into pieces.
        """
        from imswitch.improcess.model.core_dims import CoreDimsError, match_core_dims, wants_iteration

        problems = super().check_input(data_obj, params)
        params = dict(params or {})
        if data_obj is None or not wants_iteration(params):
            return problems
        handle = getattr(data_obj, "data_handle", None)
        shape = tuple(getattr(handle, "shape", ()) or ())
        try:
            match = match_core_dims(getattr(data_obj, "axis_labels", None) or [], shape, self.core_dims_in)
        except CoreDimsError:
            return problems
        if match.iterates and int(params.get("n_timepoints", 1) or 1) != 1:
            problems.append(
                f"{self.name}: the input already has {', '.join(match.loop_labels)} in front of the "
                f"stack, so each deskew gets one timepoint; set n_timepoints to 1 "
                f"(it is {params.get('n_timepoints')})"
            )
        return problems

    def stack_results(self, results: list, match, *, loop_scales=None):
        """A time loop stacks back into a 4D :class:`SnoutyResult` (its
        orthogonal views and HDF5 layout); any other loop is generic."""
        if tuple(match.loop_labels) == ("T",) and all(isinstance(r, SnoutyResult) and r.data.ndim == 3 for r in results):
            data = np.stack([np.asarray(r.data) for r in results], axis=0)
            first = results[0]
            stacked = SnoutyResult(
                name=first.name, data=data, params=dict(first.params),
                display_levels=_display_levels(data),
            )
            stacked.attrs = dict(first.attrs)
            return stacked
        return super().stack_results(results, match, loop_scales=loop_scales)

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

        result = SnoutyResult(
            name=data_obj.name,
            data=result_data,
            params=params,
            display_levels=_display_levels(result_data),
        )
        result.attrs = deskewed_attrs(data_obj, params)
        return result


#: Attribute keys that describe the *camera* grid of a skewed recording; after
#: a deskew they no longer describe the data, which is on the sample grid.
_CAMERA_GRID_KEYS = ("camera pixel size", "pixel size")


def deskewed_attrs(data_obj, params: dict) -> dict:
    """What a deskewed volume tells the next step: the input's attributes
    without the raw layout and camera-grid keys, and ``geometry: deskewed``
    with the isotropic voxel size it was resampled to (nm)."""
    from imswitch.improcess.model.result_source import slice_attrs

    try:
        attrs = slice_attrs(dict(getattr(data_obj, "attrs", None) or {}))
    except Exception:
        attrs = {}
    attrs = {
        key: value for key, value in attrs.items()
        if not (str(key).lower().startswith("detector:")
                and str(key).split(":")[-1].strip().lower() in _CAMERA_GRID_KEYS)
    }
    voxel = float(params.get("sample_vx_size", 0.0) or 0.0)
    attrs.update({
        "geometry": "deskewed",
        "voxel_size_nm": [voxel, voxel, voxel],
        "pixel_size_nm": voxel,
    })
    return attrs


def _display_levels(data) -> tuple[float, float]:
    return float(np.percentile(data, 1)), float(np.percentile(data, 99.9))


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
