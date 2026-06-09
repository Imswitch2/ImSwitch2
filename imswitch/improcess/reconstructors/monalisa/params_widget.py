"""MoNaLISA reconstruction parameter widget."""

from pyqtgraph.parametertree import Parameter, ParameterTree
from qtpy import QtWidgets


class MonalisaParamsWidget(QtWidgets.QWidget):
    """
    Parameter tree for MoNaLISA SIM reconstruction.
    
    Manages:
    - Pixel size
    - CPU/GPU device selection
    - Pattern parameters (row/col offset and period)
    - Reconstruction options (PSF FWHM, background modelling)
    - Bleaching correction flag
    """
    
    def __init__(self, parent=None):
        super().__init__(parent)
        
        # Create parameter tree
        params = [
            {'name': 'Pixel size', 'type': 'float', 'value': 77, 'suffix': 'nm'},
            {'name': 'CPU/GPU', 'type': 'list', 'values': ['GPU', 'CPU']},
            {'name': 'Pattern', 'type': 'group', 'children': [
                {'name': 'Row-offset', 'type': 'float', 'value': 9.89, 'limits': (0, 9999)},
                {'name': 'Col-offset', 'type': 'float', 'value': 10.4, 'limits': (0, 9999)},
                {'name': 'Row-period', 'type': 'float', 'value': 11.05, 'limits': (0, 9999)},
                {'name': 'Col-period', 'type': 'float', 'value': 11.05, 'limits': (0, 9999)}]},
            {'name': 'Reconstruction options', 'type': 'group', 'children': [
                {'name': 'PSF FWHM', 'type': 'float', 'value': 220, 'limits': (0, 9999),
                 'suffix': 'nm'},
                {'name': 'BG modelling', 'type': 'list',
                 'values': ['Constant', 'Gaussian', 'No background'], 'children': [
                    {'name': 'BG Gaussian size', 'type': 'float', 'value': 500, 'suffix': 'nm'}]}]},
            {'name': 'Bleaching correction', 'type': 'bool', 'value': False}
        ]
        
        self.p = Parameter.create(name='params', type='group', children=params)
        self.tree = ParameterTree(showHeader=False)
        self.tree.setParameters(self.p, showTop=False)

        # Read-only status row showing the reconstructed pixel size in nm.
        # Populated after each reconstruction via setOutputPixelSize so the
        # user can see at a glance what they'll get without having to inspect
        # the napari scale bar. Display-only — does NOT control reconstruction.
        self._outputPixelSizeLabel = QtWidgets.QLabel(
            'Output pixel size: — (set after reconstruction)'
        )
        self._outputPixelSizeLabel.setStyleSheet(
            'color: palette(mid); font-size: 9pt; padding: 2px 4px;'
        )
        self._outputPixelSizeLabel.setToolTip(
            'Reconstructed pixel pitch in nm. Derived from the sample step '
            'size and the pattern grid; not directly tunable.'
        )

        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(self.tree)
        layout.addWidget(self._outputPixelSizeLabel)
        layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(layout)
    
    def get_values(self) -> dict:
        """
        Get all parameter values as a flat dict for use in reconstruction.
        
        Returns:
            Dict with keys:
                - pixel_size_nm: float
                - device: str ('CPU' or 'GPU')
                - row_offset: float
                - col_offset: float
                - row_period: float
                - col_period: float
                - psf_fwhm_nm: float
                - bg_modelling: str ('Constant', 'Gaussian', 'No background')
                - bg_gaussian_size_nm: float (only relevant if bg_modelling == 'Gaussian')
                - bleaching_correction: bool
        """
        pattern_pars = self.p.param('Pattern')
        recon_opts = self.p.param('Reconstruction options')
        bg_modelling = recon_opts.param('BG modelling')
        
        return {
            'pixel_size_nm': self.p.param('Pixel size').value(),
            'device': self.p.param('CPU/GPU').value(),
            'row_offset': pattern_pars.param('Row-offset').value(),
            'col_offset': pattern_pars.param('Col-offset').value(),
            'row_period': pattern_pars.param('Row-period').value(),
            'col_period': pattern_pars.param('Col-period').value(),
            'psf_fwhm_nm': recon_opts.param('PSF FWHM').value(),
            'bg_modelling': bg_modelling.value(),
            'bg_gaussian_size_nm': bg_modelling.param('BG Gaussian size').value(),
            'bleaching_correction': self.p.param('Bleaching correction').value()
        }
    
    def set_pattern_params(self, row_offset: float, col_offset: float, row_period: float, col_period: float):
        """Update pattern parameters (e.g., after automatic pattern finding)."""
        pattern_pars = self.p.param('Pattern')
        pattern_pars.param('Row-offset').setValue(row_offset)
        pattern_pars.param('Col-offset').setValue(col_offset)
        pattern_pars.param('Row-period').setValue(row_period)
        pattern_pars.param('Col-period').setValue(col_period)

    def setOutputPixelSize(self, output_pixel_size_nm: tuple[float, float] | None) -> None:
        """Update the read-only output-pixel-size label.

        Called by the reconstructor (via the main view controller) after a
        successful reconstruction with the ``(y_nm, x_nm)`` pair attached to
        :class:`MonalisaProcessingResult.output_pixel_size_nm`.
        """
        label = getattr(self, '_outputPixelSizeLabel', None)
        if label is None:
            return
        if not output_pixel_size_nm:
            label.setText('Output pixel size: — (set after reconstruction)')
            return
        y_nm, x_nm = output_pixel_size_nm
        if abs(y_nm - x_nm) < 0.01:
            label.setText(f'Output pixel size: {y_nm:.3g} nm/px')
        else:
            label.setText(
                f'Output pixel size: Y {y_nm:.3g}  /  X {x_nm:.3g} nm/px'
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
