"""Denoised processing result."""

from typing import Any

import h5py
import numpy as np

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


    supported_formats = ("tiff", "hdf5")

    def _extra(self) -> dict:
        return {
            'denoise_model_name': self.model_name,
            'denoise_model_type': self.model_type,
            'denoise_crop_size': self.crop_size,
            'denoise_pad': self.pad,
        }

    def write_files(self, plan, document) -> None:
        """OME-TIFF through the shared writer, or the HDF5 layout of old."""
        if plan.fmt == 'tiff':
            from imswitch.improcess.model.result_io import save_image_result

            save_image_result(self, plan.primary, 'tiff', extra=self._extra(), document=document)
        elif plan.fmt == 'hdf5':
            from imswitch.improcess.model.save_protocol import embed_hdf5

            with h5py.File(str(plan.primary), 'w') as f:
                f.create_dataset(
                    'denoised',
                    data=np.asarray(self.data, dtype=np.float32),
                    compression='gzip',
                )
                f.attrs['axis_labels'] = ''.join(self.axis_labels)
                f.attrs['axis_scales'] = np.asarray(self.axis_scales, dtype=float)
                f.attrs['scale_unit'] = self.scale_unit
                for key, value in self._extra().items():
                    f.attrs[key] = value
                embed_hdf5(f, document)
        else:
            raise ValueError(
                f"DenoisedResult supports TIFF or HDF5, got {plan.fmt!r}"
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
