"""MoNaLISA reconstruction parameter widget."""

from pyqtgraph.parametertree import Parameter, ParameterTree
from qtpy import QtWidgets

from .gauss_processor import (
    DEFAULT_FOOTPRINT_NUM_RECTS,
    DEFAULT_GAUSSIAN_SIGMA_PX,
    DEFAULT_PINHOLE_RADIUS_SIGMA,
)
from .sweep import sweepable_parameters


def apply_method_dependent_options(root_param, method) -> None:
    """Show only the parameter groups relevant to the selected method.

    Shared by the plugin widget and the legacy ``ReconParTree`` (which mirror
    each other): method-specific groups are hidden rather than disabled so
    the tree stays short, and the sweep-parameter choices follow the
    method's sweepable-parameter registry. Group names that a particular
    tree does not contain are skipped.
    """
    method = str(method or '')
    sweepable = sweepable_parameters(method)
    visibility = {
        'Fast Gauss options': method == 'Fast Gauss MoNaLISA',
        'ISM reassignment options': method == 'ISM reassignment',
        'Parameter sweep': bool(sweepable),
        # The checkbox only gates the classic path; the other methods always
        # auto-detect the orientation.
        'Auto-detect scan orientation': method == 'MoNaLISA',
    }
    for name, visible in visibility.items():
        try:
            root_param.param(name).setOpts(visible=visible)
        except KeyError:
            continue

    try:
        bg_modelling = root_param.param('Reconstruction options').param(
            'BG modelling'
        )
        bg_modelling.setOpts(visible=method != 'ISM reassignment')
    except KeyError:
        pass

    if sweepable:
        try:
            sweep_parameter = root_param.param('Parameter sweep').param(
                'Sweep parameter'
            )
        except KeyError:
            return
        labels = list(sweepable)
        sweep_parameter.setLimits(labels)
        if sweep_parameter.value() not in labels:
            sweep_parameter.setValue(labels[0])


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
             'values': ['Fast Gauss MoNaLISA', 'MoNaLISA', 'ISM reassignment'],
             'tip': (
                 'Fast Gauss MoNaLISA (default) uses the low-latency Gaussian '
                 'reassignment path that live reconstruction always uses. '
                 'MoNaLISA runs the full post-acquisition SignalExtractor '
                 'path. ISM reassignment is the validated xrecon '
                 'image-scanning implementation with CPU and GPU backends; '
                 'see ISM reassignment options.'
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
                {'name': 'Pattern geometry', 'type': 'list',
                 'value': 'Auto',
                 'values': ['Auto', 'Rectangular grid', 'General lattice'],
                 'tip': ('Auto detects the illumination pattern and keeps '
                         'axis-aligned grids on the exact legacy pipeline, '
                         'rerouting rotated-square (diamond) or hexagonal '
                         'patterns to the scatter-and-grid reassignment. '
                         'The general path needs a correct Pixel size and '
                         'also stores the pre-gridding spot positions and '
                         'intensities on the result.')},
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
                {'name': 'Sampling', 'type': 'list',
                 'value': 'Bilinear (legacy)',
                 'values': ['Bilinear (legacy)', 'Exact pixel'],
                 'tip': ('How footprint samples are read. Bilinear (legacy) '
                         'interpolates at fixed offsets from the fractional '
                         'focus center, which low-passes the peak and biases '
                         'amplitudes by a few percent depending on each '
                         "focus' subpixel position. Exact pixel fits the "
                         'true integer pixels with per-focus weights: '
                         'unbiased and slightly faster.')},
                ]},
            {'name': 'ISM reassignment options', 'type': 'group', 'children': [
                {'name': 'Oversampling', 'type': 'float', 'value': 2.0,
                 'limits': (1.0, 8.0)},
                {'name': 'ISM shift', 'type': 'float', 'value': 0.5,
                 'limits': (-4.0, 4.0),
                 'tip': ('Fourier pixel-reassignment fraction; 0.5 is the '
                         'standard matched-PSF value.')},
                {'name': 'Subtract patch mean', 'type': 'bool', 'value': True},
                {'name': 'Frame batch', 'type': 'int', 'value': 0,
                 'limits': (0, 100000),
                 'tip': ('0 processes all frames of one scan together. Lower '
                         'values reduce temporary CPU/GPU memory use.')} ]},
            {'name': 'Parameter sweep', 'type': 'group', 'children': [
                {'name': 'Enable sweep', 'type': 'bool', 'value': False,
                 'tip': ('Advanced: reconstruct once per sweep value and '
                         'stack the results along a leading Sweep axis — '
                         'slide through it in the viewer to find the best '
                         'setting.')},
                {'name': 'Sweep parameter', 'type': 'list',
                 'value': 'Pinhole radius (×σ)',
                 'values': ['Pinhole radius (×σ)'],
                 'tip': ('Which parameter to sweep; the choices follow the '
                         'selected reconstruction method. Sweeping the '
                         'pinhole radius forces Circular pinhole footprint '
                         'mode.')},
                {'name': 'Sweep values', 'type': 'str',
                 'value': '0.75, 1.0, 1.25, 1.5, 2.0, 2.5',
                 'tip': ('Comma-separated values, or an inclusive range '
                         'start:step:stop (e.g. 0.5:0.25:2.5).')}]},
            {'name': 'Bleaching correction', 'type': 'bool', 'value': False},
            {'name': 'Auto-detect scan orientation', 'type': 'bool', 'value': True,
             'tip': (
                 'Classic MoNaLISA: override the scan-params dialog by picking '
                 'the fast/slow axis and pos/neg direction that minimize total '
                 'variation. ISM reassignment always auto-detects.'
             )},
        ]
        
        self.p = Parameter.create(name='params', type='group', children=params)
        self.tree = ParameterTree(showHeader=False)
        self.tree.setParameters(self.p, showTop=False)

        # Show only the groups the selected method actually reads.
        method_param = self.p.param('Reconstruction method')
        method_param.sigValueChanged.connect(
            lambda _param, value: apply_method_dependent_options(self.p, value)
        )
        apply_method_dependent_options(self.p, method_param.value())

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
                - reconstruction_method: str
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
                - fast_gauss_sampling_mode: str (UI label; normalized by the
                  processor to 'bilinear' or 'exact')
                - sweep_enabled: bool
                - sweep_parameter: str (UI label of the swept parameter)
                - sweep_values_text: str (unparsed sweep-values field)
                - ism_reassign_oversampling: float
                - ism_reassign_shift: float
                - ism_reassign_remove_mean_of_patch: bool
                - ism_reassign_frame_batch_size: int | None
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
            'fast_gauss_pattern_geometry': fast_gauss_opts.param(
                'Pattern geometry').value(),
            'fast_gauss_footprint_mode': fast_gauss_opts.param(
                'Footprint mode').value(),
            'fast_gauss_footprint_num_rects': fast_gauss_opts.param(
                'Footprint rectangles').value(),
            'fast_gauss_gaussian_sigma_px': fast_gauss_opts.param(
                'Gaussian sigma').value(),
            'fast_gauss_pinhole_radius_sigma': fast_gauss_opts.param(
                'Pinhole radius').value(),
            'fast_gauss_sampling_mode': fast_gauss_opts.param(
                'Sampling').value(),
            'sweep_enabled': self.p.param('Parameter sweep').param(
                'Enable sweep').value(),
            'sweep_parameter': self.p.param('Parameter sweep').param(
                'Sweep parameter').value(),
            'sweep_values_text': self.p.param('Parameter sweep').param(
                'Sweep values').value(),
            'ism_reassign_oversampling': self.p.param('ISM reassignment options').param(
                'Oversampling').value(),
            'ism_reassign_shift': self.p.param('ISM reassignment options').param(
                'ISM shift').value(),
            'ism_reassign_remove_mean_of_patch': self.p.param('ISM reassignment options').param(
                'Subtract patch mean').value(),
            'ism_reassign_frame_batch_size': (
                int(self.p.param('ISM reassignment options').param('Frame batch').value())
                or None
            ),
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
