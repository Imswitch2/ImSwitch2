"""MoNaLISA reconstruction parameter widget."""

from pyqtgraph.parametertree import Parameter, ParameterTree
from qtpy import QtWidgets

from .gauss_processor import (
    DEFAULT_FOOTPRINT_NUM_RECTS,
    DEFAULT_GAUSSIAN_SIGMA_PX,
    DEFAULT_PINHOLE_RADIUS_SIGMA,
)


class MonalisaParamsWidget(QtWidgets.QWidget):
    """
    Parameter tree for MoNaLISA SIM reconstruction.
    
    Manages:
    - Pixel size
    - Reconstruction method
    - CPU/GPU device selection
    - Pattern parameters (row/col offset and period)
    - Reconstruction options (PSF FWHM, background modelling)
    - Fast Gauss options (footprint shell count, Gaussian sigma)
    - Bleaching correction flag
    """
    
    def __init__(self, parent=None):
        super().__init__(parent)
        
        # Create parameter tree
        params = [
            {'name': 'Pixel size', 'type': 'float', 'value': 77, 'suffix': 'nm'},
            {'name': 'Reconstruction method', 'type': 'list',
             'value': 'Fast Gauss MoNaLISA',
             'values': ['Fast Gauss MoNaLISA', 'MoNaLISA'],
             'tip': (
                 'Fast Gauss MoNaLISA (default) uses the low-latency Gaussian '
                 'reassignment path that live reconstruction always uses. '
                 'MoNaLISA runs the full post-acquisition SignalExtractor path.'
             )},
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
            {'name': 'Fast Gauss options', 'type': 'group', 'children': [
                {'name': 'Footprint mode', 'type': 'list',
                 'value': 'Rectangular shells',
                 'values': ['Rectangular shells', 'Circular pinhole'],
                 'tip': ('Rectangular shells keeps the Mini_Recon footprint. '
                         'Circular pinhole uses the Pinhole radius value.')},
                {'name': 'Footprint rectangles', 'type': 'int',
                 'value': DEFAULT_FOOTPRINT_NUM_RECTS, 'limits': (1, 99),
                 'tip': ('Concentric rectangular shells sampled around each focus '
                         '(used in Rectangular shells mode).')},
                {'name': 'Gaussian sigma', 'type': 'float',
                 'value': DEFAULT_GAUSSIAN_SIGMA_PX, 'limits': (0.01, 9999),
                 'suffix': 'px',
                 'tip': 'Gaussian sigma for the footprint fit, in pixels.'},
                {'name': 'Pinhole radius', 'type': 'float',
                 'value': DEFAULT_PINHOLE_RADIUS_SIGMA, 'limits': (0.01, 99),
                 'suffix': '×σ',
                 'tip': ('Circular detection pinhole radius as a multiple of the '
                         'Gaussian sigma (image-scanning-microscopy style). '
                         'Used only in Circular pinhole mode; smaller trades '
                         'signal for resolution, larger trades resolution for SNR.')},
                {'name': 'Parameter sweep', 'type': 'group', 'children': [
                    {'name': 'Enable sweep', 'type': 'bool', 'value': False,
                     'tip': ('Advanced: reconstruct once per sweep value and '
                             'stack the results along a leading Sweep axis — '
                             'slide through it in the viewer to find the best '
                             'setting. Offline Fast Gauss only.')},
                    {'name': 'Sweep parameter', 'type': 'list',
                     'value': 'Pinhole radius (×σ)',
                     'values': ['Pinhole radius (×σ)', 'Gaussian sigma (px)'],
                     'tip': ('Which fast-Gauss parameter to sweep. Sweeping '
                             'the pinhole radius forces Circular pinhole '
                             'footprint mode.')},
                    {'name': 'Sweep values', 'type': 'str',
                     'value': '0.75, 1.0, 1.25, 1.5, 2.0, 2.5',
                     'tip': ('Comma-separated values, or an inclusive range '
                             'start:step:stop (e.g. 0.5:0.25:2.5).')}]}]},
            {'name': 'Bleaching correction', 'type': 'bool', 'value': False},
            {'name': 'Auto-detect scan orientation', 'type': 'bool', 'value': True,
             'tip': (
                 'Override the scan-params dialog by picking the fast/slow axis '
                 'and pos/neg direction that minimize total variation of the '
                 'reconstructed image — ported from Mini_Recon.'
             )},
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
                - reconstruction_method: str ('MoNaLISA' or 'Fast Gauss MoNaLISA')
                - device: str ('CPU' or 'GPU')
                - row_offset: float
                - col_offset: float
                - row_period: float
                - col_period: float
                - psf_fwhm_nm: float
                - bg_modelling: str ('Constant', 'Gaussian', 'No background')
                - bg_gaussian_size_nm: float (only relevant if bg_modelling == 'Gaussian')
                - fast_gauss_footprint_mode: str
                - fast_gauss_footprint_num_rects: int
                - fast_gauss_gaussian_sigma_px: float
                - fast_gauss_pinhole_radius_sigma: float
                - sweep_enabled: bool
                - sweep_parameter: str (UI label of the swept parameter)
                - sweep_values_text: str (unparsed sweep-values field)
                - bleaching_correction: bool
        """
        pattern_pars = self.p.param('Pattern')
        recon_opts = self.p.param('Reconstruction options')
        fast_gauss_opts = self.p.param('Fast Gauss options')
        bg_modelling = recon_opts.param('BG modelling')
        
        return {
            'pixel_size_nm': self.p.param('Pixel size').value(),
            'reconstruction_method': self.p.param('Reconstruction method').value(),
            'device': self.p.param('CPU/GPU').value(),
            'row_offset': pattern_pars.param('Row-offset').value(),
            'col_offset': pattern_pars.param('Col-offset').value(),
            'row_period': pattern_pars.param('Row-period').value(),
            'col_period': pattern_pars.param('Col-period').value(),
            'psf_fwhm_nm': recon_opts.param('PSF FWHM').value(),
            'bg_modelling': bg_modelling.value(),
            'bg_gaussian_size_nm': bg_modelling.param('BG Gaussian size').value(),
            'fast_gauss_footprint_mode': fast_gauss_opts.param(
                'Footprint mode').value(),
            'fast_gauss_footprint_num_rects': fast_gauss_opts.param(
                'Footprint rectangles').value(),
            'fast_gauss_gaussian_sigma_px': fast_gauss_opts.param(
                'Gaussian sigma').value(),
            'fast_gauss_pinhole_radius_sigma': fast_gauss_opts.param(
                'Pinhole radius').value(),
            'sweep_enabled': fast_gauss_opts.param('Parameter sweep').param(
                'Enable sweep').value(),
            'sweep_parameter': fast_gauss_opts.param('Parameter sweep').param(
                'Sweep parameter').value(),
            'sweep_values_text': fast_gauss_opts.param('Parameter sweep').param(
                'Sweep values').value(),
            'bleaching_correction': self.p.param('Bleaching correction').value(),
            'auto_scan_orientation': self.p.param('Auto-detect scan orientation').value(),
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
