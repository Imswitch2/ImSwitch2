"""Drift-corrected processing result."""

from pathlib import Path
from typing import Any

import numpy as np

from imswitch.improcess.model.plotting import PlotPayload, PlotSeries
from imswitch.improcess.model.result import ProcessingResult


class DriftCorrectedResult(ProcessingResult):
    """
    Result from drift correction processor.
    
    Stores drift-corrected data plus the computed shift vectors.
    """
    
    def __init__(
        self,
        name: str,
        data: np.ndarray | Any,
        axis_labels: list[str],
        drift_xy: np.ndarray,  # Shape: (n_frames, 2) - X/Y shifts for each frame
        **kwargs
    ):
        super().__init__(name, data, axis_labels, **kwargs)
        self.drift_xy = drift_xy
    

    supported_formats = ("tiff", "hdf5", "zarr")

    def plan_save(self, path: Path, fmt: str):
        from imswitch.improcess.model.save_protocol import SavePlan

        path = Path(path)
        if fmt == 'tiff':
            # The drift vectors ride in a companion beside the TIFF.
            return SavePlan(path, fmt, (path.with_suffix('.drift.npy'),))
        return SavePlan(path, fmt)

    def write_files(self, plan, document) -> None:
        """
        Write drift-corrected data to disk.

        TIFF: OME-TIFF through the shared writer plus a companion ``.drift.npy``
        with the drift vectors. HDF5/Zarr: data and drift vectors in one
        container.
        """
        if plan.fmt == 'tiff':
            from imswitch.improcess.model.result_io import save_image_result

            save_image_result(self, plan.primary, 'tiff', document=document)
            np.save(plan.companions[0], self.drift_xy)
        elif plan.fmt == 'hdf5':
            import h5py

            from imswitch.improcess.model.save_protocol import embed_hdf5

            with h5py.File(plan.primary, 'w') as f:
                f.create_dataset('data', data=self.data)
                f.create_dataset('drift_xy', data=self.drift_xy)
                f.attrs['axis_labels'] = ','.join(self.axis_labels)
                f.attrs['axis_scales'] = np.asarray(self.axis_scales, dtype=float)
                f.attrs['scale_unit'] = self.scale_unit
                f.attrs['name'] = self.name
                embed_hdf5(f, document)
        elif plan.fmt == 'zarr':
            import zarr

            from imswitch.improcess.model.save_protocol import embed_zarr

            root = zarr.open_group(str(plan.primary), mode='w')
            for key, value in (('data', np.asarray(self.data)), ('drift_xy', np.asarray(self.drift_xy))):
                if hasattr(root, 'create_array'):      # zarr 3
                    array = root.create_array(key, shape=value.shape, dtype=value.dtype)
                    array[...] = value
                else:                                  # zarr 2
                    root.create_dataset(key, data=value)
            root.attrs['axis_labels'] = ','.join(self.axis_labels)
            root.attrs['axis_scales'] = np.asarray(self.axis_scales, dtype=float).tolist()
            root.attrs['scale_unit'] = self.scale_unit
            root.attrs['name'] = self.name
            embed_zarr(root, document)
        else:
            raise ValueError(f"Unsupported format: {plan.fmt}")

    def plot_payloads(self) -> list[PlotPayload]:
        """Return drift trace plots for the generic ImProcess graph widget."""
        frames = np.arange(self.drift_xy.shape[0])
        return [
            PlotPayload(
                title="Drift correction",
                x_label="Frame",
                y_label="Shift (px)",
                series=[
                    PlotSeries(name="Y shift", x=frames, y=self.drift_xy[:, 0], kind="line"),
                    PlotSeries(name="X shift", x=frames, y=self.drift_xy[:, 1], kind="line"),
                ],
            )
        ]


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
