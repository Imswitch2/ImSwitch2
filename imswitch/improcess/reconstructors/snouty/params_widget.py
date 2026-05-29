"""SNOUTY deskew parameter widget."""

from pyqtgraph.parametertree import Parameter, ParameterTree
from qtpy import QtWidgets

from .metadata import snouty_params_from_attrs, DEFAULT_PARAMS


class SnoutyParamsWidget(QtWidgets.QWidget):
    """
    Parameter tree for SNOUTY lightsheet deskew.
    
    Manages:
    - Geometry: camera pixel size, tilt angle, scan step, output voxel size
    - Acquisition: camera offset, flip data, cycles, planes per cycle, restack
    - Device: CPU/GPU selection
    - Timelapse: number of timepoints
    """
    
    def __init__(self, parent=None):
        super().__init__(parent)
        
        # Create parameter tree with Mini_Recon defaults
        params = [
            {'name': 'Device', 'type': 'list', 'values': ['CPU', 'GPU'], 'value': 'CPU'},
            {'name': 'Timepoints', 'type': 'int', 'value': 1, 'limits': (1, 9999)},
            {'name': 'Geometry', 'type': 'group', 'children': [
                {'name': 'Camera pixel size', 'type': 'float', 'value': DEFAULT_PARAMS['c_px'], 
                 'suffix': 'nm', 'limits': (1, 10000)},
                {'name': 'Tilt angle', 'type': 'float', 'value': DEFAULT_PARAMS['alpha_deg'],
                 'suffix': '°', 'limits': (0, 90)},
                {'name': 'Scan step', 'type': 'float', 'value': DEFAULT_PARAMS['dy'],
                 'suffix': 'nm', 'limits': (1, 10000)},
                {'name': 'Output voxel size', 'type': 'float', 'value': DEFAULT_PARAMS['sample_vx_size'],
                 'suffix': 'nm', 'limits': (1, 10000)}]},
            {'name': 'Acquisition', 'type': 'group', 'children': [
                {'name': 'Camera offset', 'type': 'float', 'value': DEFAULT_PARAMS['camera_offset'],
                 'suffix': 'ADU', 'limits': (0, 65535)},
                {'name': 'Flip data', 'type': 'bool', 'value': DEFAULT_PARAMS['flip_data']},
                {'name': 'Cycles', 'type': 'int', 'value': DEFAULT_PARAMS['cycles'], 'limits': (1, 9999)},
                {'name': 'Planes per cycle', 'type': 'int', 'value': DEFAULT_PARAMS['planes_in_cycle'],
                 'limits': (1, 9999)},
                {'name': 'Restack', 'type': 'bool', 'value': DEFAULT_PARAMS['restack']}]}
        ]
        
        self.p = Parameter.create(name='params', type='group', children=params)
        self.tree = ParameterTree(showHeader=False)
        self.tree.setParameters(self.p, showTop=False)
        
        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(self.tree)
        layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(layout)
    
    def get_values(self) -> dict:
        """
        Get all parameter values as a flat dict for use in reconstruction.
        
        Returns:
            Dict with keys:
                - device: str ('CPU' or 'GPU')
                - n_timepoints: int
                - c_px: float (nm)
                - alpha_deg: float (degrees)
                - dy: float (nm)
                - sample_vx_size: float (nm)
                - camera_offset: float (ADU)
                - flip_data: bool
                - cycles: int
                - planes_in_cycle: int
                - restack: bool
        """
        geom = self.p.param('Geometry')
        acq = self.p.param('Acquisition')
        
        return {
            'device': self.p.param('Device').value(),
            'n_timepoints': self.p.param('Timepoints').value(),
            'c_px': geom.param('Camera pixel size').value(),
            'alpha_deg': geom.param('Tilt angle').value(),
            'dy': geom.param('Scan step').value(),
            'sample_vx_size': geom.param('Output voxel size').value(),
            'camera_offset': acq.param('Camera offset').value(),
            'flip_data': acq.param('Flip data').value(),
            'cycles': acq.param('Cycles').value(),
            'planes_in_cycle': acq.param('Planes per cycle').value(),
            'restack': acq.param('Restack').value()
        }
    
    def set_from_attrs(self, attrs: dict) -> None:
        """
        Load parameters from HDF5 attributes (metadata auto-detection).
        
        Args:
            attrs: DataObj.attrs dict (HDF5 root attributes)
        """
        params = snouty_params_from_attrs(attrs)
        
        # Update geometry
        geom = self.p.param('Geometry')
        geom.param('Camera pixel size').setValue(params['c_px'])
        geom.param('Tilt angle').setValue(params['alpha_deg'])
        geom.param('Scan step').setValue(params['dy'])
        geom.param('Output voxel size').setValue(params['sample_vx_size'])
        
        # Update acquisition
        acq = self.p.param('Acquisition')
        acq.param('Camera offset').setValue(params['camera_offset'])
        acq.param('Flip data').setValue(params['flip_data'])
        acq.param('Cycles').setValue(params['cycles'])
        acq.param('Planes per cycle').setValue(params['planes_in_cycle'])
        acq.param('Restack').setValue(params['restack'])


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
