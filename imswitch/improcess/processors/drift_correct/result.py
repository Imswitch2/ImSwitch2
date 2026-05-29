"""Drift-corrected processing result."""

from pathlib import Path
from typing import Any

import numpy as np
import tifffile as tiff

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
    
    def save(self, path: Path, fmt: str = 'tiff') -> None:
        """
        Save drift-corrected data to disk.
        
        For TIFF format, saves the array with ImageJ metadata.
        Also saves drift vectors to a companion .npy file.
        """
        if fmt.lower() in ['tiff', 'tif']:
            # Determine resolution and spacing from axis labels
            # For now, use unit resolution
            tiff.imwrite(
                str(path),
                self.data.astype(np.float32),
                imagej=True,
                resolution=(1, 1),
                metadata={'spacing': 1, 'unit': 'px', 'axes': ''.join(self.axis_labels)}
            )
            
            # Save drift vectors to companion file
            drift_path = path.with_suffix('.drift.npy')
            np.save(drift_path, self.drift_xy)
            
        elif fmt.lower() in ['hdf5', 'hdf']:
            import h5py
            with h5py.File(path, 'w') as f:
                f.create_dataset('data', data=self.data)
                f.create_dataset('drift_xy', data=self.drift_xy)
                f.attrs['axis_labels'] = ','.join(self.axis_labels)
                f.attrs['name'] = self.name
        
        elif fmt.lower() == 'zarr':
            import zarr
            root = zarr.open(str(path), mode='w')
            root.create_dataset('data', data=self.data)
            root.create_dataset('drift_xy', data=self.drift_xy)
            root.attrs['axis_labels'] = ','.join(self.axis_labels)
            root.attrs['name'] = self.name
        
        else:
            raise ValueError(f"Unsupported format: {fmt}")

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
