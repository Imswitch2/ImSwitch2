"""Default deconvolution parameters, shared by the widget and ``default_params``."""

DECONVOLUTION_DEFAULTS = {
    "iterations": 10,
    "detection_na": 1.1,
    "wavelength_nm": 510.0,
    "immersion_ri": 1.5,
    "psf_size_px": 61,
    "psf_path": "",
    "confined_sheet_fwhm_nm": 1200.0,
    "readout_sheet_fwhm_nm": 1200.0,
    "background_sheet_ratio": 0.1,
    "kernel_clip_factor": 0.01,
    "normalisation_clip": 0.3,
    "gradient_consent": False,
}


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
