"""SNOUTY deconvolution parameter widget: the deskew tree plus a deconvolution group."""

from imswitch.improcess.reconstructors.snouty.params_widget import SnoutyParamsWidget

from .defaults import DECONVOLUTION_DEFAULTS


class SnoutyDeconvolutionParamsWidget(SnoutyParamsWidget):
    """The SNOUTY deskew parameters with a *Deconvolution* group appended.

    Geometry, acquisition and device settings are inherited unchanged, so the
    metadata prefill keeps working. ``get_values()`` returns the deskew keys
    plus those of :data:`DECONVOLUTION_DEFAULTS`.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        d = DECONVOLUTION_DEFAULTS
        self.p.addChild({'name': 'Deconvolution', 'type': 'group', 'children': [
            {'name': 'Iterations', 'type': 'int', 'value': d['iterations'], 'limits': (1, 10000)},
            {'name': 'Detection NA', 'type': 'float', 'value': d['detection_na'],
             'limits': (0.01, 2.0), 'step': 0.01},
            {'name': 'Wavelength', 'type': 'float', 'value': d['wavelength_nm'],
             'suffix': 'nm', 'limits': (200, 2000)},
            {'name': 'Immersion index', 'type': 'float', 'value': d['immersion_ri'],
             'limits': (1.0, 2.0), 'step': 0.01},
            {'name': 'PSF size', 'type': 'int', 'value': d['psf_size_px'],
             'suffix': 'vx', 'limits': (3, 1001)},
            {'name': 'PSF file', 'type': 'file', 'value': d['psf_path'],
             'winTitle': 'Optical PSF (3D TIFF at the output voxel size)',
             'nameFilter': 'TIFF (*.tif *.tiff);;All files (*)',
             'tip': 'Optional 3D TIFF at the output voxel size; empty generates a Richards & Wolf PSF'},
            {'name': 'Confined sheet FWHM', 'type': 'float', 'value': d['confined_sheet_fwhm_nm'],
             'suffix': 'nm', 'limits': (1, 100000)},
            {'name': 'Read-out sheet FWHM', 'type': 'float', 'value': d['readout_sheet_fwhm_nm'],
             'suffix': 'nm', 'limits': (1, 100000)},
            {'name': 'Background sheet ratio', 'type': 'float', 'value': d['background_sheet_ratio'],
             'limits': (0.0, 1.0), 'step': 0.05},
            {'name': 'Kernel clip factor', 'type': 'float', 'value': d['kernel_clip_factor'],
             'limits': (1e-6, 0.5), 'step': 0.005},
            {'name': 'Normalisation clip', 'type': 'float', 'value': d['normalisation_clip'],
             'limits': (0.0, 1.0), 'step': 0.05,
             'tip': 'Lower clip of the normalisation volume as a fraction of its maximum'},
            {'name': 'Gradient consent', 'type': 'bool', 'value': d['gradient_consent'],
             'tip': 'Accept an update only where two binomial halves of the data agree on it'},
            {'name': 'Sweep iterations', 'type': 'bool', 'value': d['sweep_iterations'],
             'tip': 'Debugging: output one volume per iteration up to Iterations, as a stack '
                    '(first timepoint only)'},
            {'name': 'Alignment file', 'type': 'file', 'value': d['alignment_path'],
             'winTitle': 'Multicolor alignment file',
             'nameFilter': 'HDF5 (*.h5 *.hdf5);;All files (*)',
             'tip': 'Optional multicolor alignment .h5 from Multicolor Registration: each colour '
                    'strip is deconvolved with its own wavelength'},
            {'name': 'Wavelengths', 'type': 'str', 'value': d['wavelengths_nm'],
             'tip': 'Comma-separated emission wavelengths in nm, one per strip of the alignment '
                    'file; empty uses Wavelength for every strip'},
        ]})

    def get_values(self) -> dict:
        values = super().get_values()
        dec = self.p.param('Deconvolution')
        values.update({
            'iterations': dec.param('Iterations').value(),
            'detection_na': dec.param('Detection NA').value(),
            'wavelength_nm': dec.param('Wavelength').value(),
            'immersion_ri': dec.param('Immersion index').value(),
            'psf_size_px': dec.param('PSF size').value(),
            'psf_path': dec.param('PSF file').value(),
            'confined_sheet_fwhm_nm': dec.param('Confined sheet FWHM').value(),
            'readout_sheet_fwhm_nm': dec.param('Read-out sheet FWHM').value(),
            'background_sheet_ratio': dec.param('Background sheet ratio').value(),
            'kernel_clip_factor': dec.param('Kernel clip factor').value(),
            'normalisation_clip': dec.param('Normalisation clip').value(),
            'gradient_consent': dec.param('Gradient consent').value(),
            'sweep_iterations': dec.param('Sweep iterations').value(),
            'alignment_path': dec.param('Alignment file').value(),
            'wavelengths_nm': dec.param('Wavelengths').value(),
        })
        return values


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
