"""SNOUTY deskew reconstruction result."""

import h5py
import numpy as np
import tifffile as tiff
from pathlib import Path

from imswitch.improcess.model.result import ProcessingResult, ViewMode


class SnoutyResult(ProcessingResult):
    """
    SNOUTY lightsheet deskew result.
    
    Data shape:
        - 3D: (Z, Y, X) — single timepoint
        - 4D: (T, Z, Y, X) — multi-timepoint
    
    Axis labels: ["Z", "Y", "X"] for 3D, ["T", "Z", "Y", "X"] for 4D.
    
    View modes (the viewer displays the last two transposed axes and puts
    sliders on the rest):
        - XY: identity — slider over Z, displays (Y, X)
        - XZ: slider over Y, displays (Z, X)
        - YZ: slider over X, displays (Z, Y)
    """
    
    def __init__(
        self,
        name: str,
        data: np.ndarray,
        params: dict,
        display_levels: tuple[float, float] | None = None
    ):
        """
        Args:
            name: Human-readable name (e.g., "snouty_deskewed")
            data: Deskewed volume (3D or 4D)
            params: Parameter dict used for deskewing (saved for metadata)
            display_levels: Optional (min, max) display range
        """
        ndim = data.ndim
        sample_vx_um = float(params.get("sample_vx_size", 1.0)) / 1000.0
        if ndim == 3:
            axis_labels = ["Z", "Y", "X"]
            axis_scales = [sample_vx_um, sample_vx_um, sample_vx_um]
            view_modes = [
                ViewMode("XY", (0, 1, 2)),  # Z, Y, X — slider Z, displays (Y, X)
                ViewMode("XZ", (1, 0, 2)),  # Y, Z, X — slider Y, displays (Z, X)
                ViewMode("YZ", (2, 0, 1)),  # X, Z, Y — slider X, displays (Z, Y)
            ]
        elif ndim == 4:
            axis_labels = ["T", "Z", "Y", "X"]
            axis_scales = [1.0, sample_vx_um, sample_vx_um, sample_vx_um]
            view_modes = [
                ViewMode("XY", (0, 1, 2, 3)),  # T, Z, Y, X — sliders T/Z, displays (Y, X)
                ViewMode("XZ", (0, 2, 1, 3)),  # T, Y, Z, X — sliders T/Y, displays (Z, X)
                ViewMode("YZ", (0, 3, 1, 2)),  # T, X, Z, Y — sliders T/X, displays (Z, Y)
            ]
        else:
            raise ValueError(f"SNOUTY result must be 3D or 4D, got shape {data.shape}")
        
        super().__init__(
            name=name,
            data=data,
            axis_labels=axis_labels,
            view_modes=view_modes,
            display_levels=display_levels,
            axis_scales=axis_scales,
            scale_unit="um",
        )
        
        self.params = params
    
    def save(self, path: Path, fmt: str = "tiff") -> None:
        """
        Save SNOUTY deskew result.
        
        Args:
            path: Output file path
            fmt: Format string ("tiff" or "hdf5")
        
        TIFF format:
            - ImageJ-compatible with axes "ZYX" (3D) or "TZYX" (4D)
        
        HDF5 format:
            - 3D: Single dataset named "volume"
            - 4D: One dataset per timepoint named "t000", "t001", ...
        """
        if fmt == "tiff":
            self._save_tiff(path)
        elif fmt == "hdf5":
            self._save_hdf5(path)
        else:
            raise ValueError(f"SNOUTY result supports 'tiff' or 'hdf5', got '{fmt}'")
    
    def _save_tiff(self, path: Path) -> None:
        """Save as ImageJ-compatible TIFF."""
        if self.data.ndim == 3:
            axes = "ZYX"
        elif self.data.ndim == 4:
            axes = "TZYX"
        else:
            raise ValueError(f"Cannot save {self.data.ndim}D data as TIFF")
        
        # Save with ImageJ metadata
        tiff.imwrite(
            str(path),
            self.data,
            imagej=True,
            metadata={'axes': axes},
            photometric='minisblack'
        )
    
    def _save_hdf5(self, path: Path) -> None:
        """Save as HDF5 with per-timepoint datasets (4D) or single volume (3D)."""
        with h5py.File(str(path), 'w') as f:
            if self.data.ndim == 3:
                # Single 3D volume
                f.create_dataset('volume', data=self.data, compression='gzip')
            elif self.data.ndim == 4:
                # One dataset per timepoint: t000, t001, ...
                n_timepoints = self.data.shape[0]
                for t in range(n_timepoints):
                    dataset_name = f"t{t:03d}"
                    f.create_dataset(dataset_name, data=self.data[t], compression='gzip')
            else:
                raise ValueError(f"Cannot save {self.data.ndim}D data as HDF5")
            
            # Save parameters as attributes
            for key, value in self.params.items():
                f.attrs[key] = value


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
