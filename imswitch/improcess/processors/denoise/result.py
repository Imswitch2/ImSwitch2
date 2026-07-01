"""Denoised processing result."""

from pathlib import Path
from typing import Any

import h5py
import numpy as np
import tifffile as tiff

from imswitch.improcess.model.result import ProcessingResult


class DenoisedResult(ProcessingResult):
    """Result wrapper for a neural-network-denoised image stack."""

    def __init__(
        self,
        name: str,
        data: np.ndarray | Any,
        axis_labels: list[str],
        model_name: str,
        model_type: str,
        crop_size: int,
        pad: bool,
        **kwargs,
    ):
        super().__init__(name, data, axis_labels, **kwargs)
        self.model_name = model_name
        self.model_type = model_type
        self.crop_size = crop_size
        self.pad = pad

    def save(self, path: Path, fmt: str = 'tiff') -> None:
        """Save the denoised array as TIFF (default) or HDF5."""
        if fmt.lower() in ('tiff', 'tif'):
            tiff.imwrite(
                str(path),
                np.asarray(self.data, dtype=np.float32),
                imagej=True,
                resolution=(1, 1),
                metadata={
                    'spacing': 1,
                    'unit': self.scale_unit,
                    'axes': ''.join(self.axis_labels),
                    'denoise_model_name': self.model_name,
                    'denoise_model_type': self.model_type,
                    'denoise_crop_size': self.crop_size,
                    'denoise_pad': self.pad,
                },
            )
        elif fmt.lower() in ('hdf5', 'h5', 'hdf'):
            with h5py.File(str(path), 'w') as f:
                f.create_dataset(
                    'denoised',
                    data=np.asarray(self.data, dtype=np.float32),
                    compression='gzip',
                )
                f.attrs['axis_labels'] = ''.join(self.axis_labels)
                f.attrs['axis_scales'] = np.asarray(self.axis_scales, dtype=float)
                f.attrs['scale_unit'] = self.scale_unit
                f.attrs['denoise_model_name'] = self.model_name
                f.attrs['denoise_model_type'] = self.model_type
                f.attrs['denoise_crop_size'] = self.crop_size
                f.attrs['denoise_pad'] = self.pad
        else:
            raise ValueError(
                f"DenoisedResult supports TIFF or HDF5, got {fmt!r}"
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
