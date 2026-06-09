"""MoNaLISA-specific processing result."""

import os
from pathlib import Path

import numpy as np
import tifffile as tiff

from imswitch.improcess.model.result import ProcessingResult, ViewMode


class MonalisaProcessingResult(ProcessingResult):
    """
    MoNaLISA reconstruction result.
    
    data shape: (Dataset, Base, T, Z, Y, X)
    axis_labels: ["Dataset", "Base", "T", "Z", "Y", "X"]
    
    Stores scan parameters for metadata and saves as ImageJ-compatible 6D TIFFs.
    """
    
    def __init__(
        self,
        name: str,
        data: np.ndarray,
        scan_params: dict,
        axis_labels: list[str] | None = None,
        display_levels: tuple[float, float] | None = None,
        axis_scales: list[float] | None = None,
        scale_unit: str = "nm",
        output_pixel_size_nm: tuple[float, float] | None = None,
    ):
        """
        Args:
            name: Human-readable name (e.g., "20240101_sample1")
            data: 6D array (Dataset, Base, T, Z, Y, X)
            scan_params: Scan metadata dict with 'dimensions', 'directions', 'steps', 'step_sizes'
            axis_labels: Optional axis labels (defaults to 6D MoNaLISA standard)
            display_levels: Optional (min, max) display range
            axis_scales: Optional per-axis scales (length 6). Overrides the
                Y/X scales derived from ``output_pixel_size_nm``.
            scale_unit: Display unit for the napari scale bar — defaults to
                ``"nm"`` because MoNaLISA's reconstructed pixel grid is
                sub-nanometer-step.
            output_pixel_size_nm: Optional ``(y_nm, x_nm)`` describing the
                reconstructed pixel pitch. Stored on the result for the
                parameter widget to display, and used to populate the Y/X
                entries of ``axis_scales`` when ``axis_scales`` is None.
        """
        if axis_labels is None:
            axis_labels = ["Dataset", "Base", "T", "Z", "Y", "X"]

        # Define view modes for MoNaLISA 6D data
        view_modes = [
            ViewMode("Standard", (0, 1, 2, 3, 4, 5)),  # Dataset, Base, T, Z, Y, X
            ViewMode("Bottom", (1, 0, 2, 3, 5, 4)),    # Base, Dataset, T, Z, X, Y (XZ plane)
            ViewMode("Left", (1, 0, 2, 3, 4, 5)),      # Base, Dataset, T, Z, Y, X (YZ plane)
        ]

        # Derive axis_scales from output_pixel_size_nm so the napari scale bar
        # and downstream profile / PSF analyses get the right physical units
        # without the caller having to assemble a 6-element scale list.
        if axis_scales is None:
            axis_scales = [1.0] * data.ndim
            if (
                output_pixel_size_nm is not None
                and data.ndim >= 2
                and "Y" in axis_labels
                and "X" in axis_labels
            ):
                y_nm, x_nm = output_pixel_size_nm
                axis_scales[axis_labels.index("Y")] = float(y_nm)
                axis_scales[axis_labels.index("X")] = float(x_nm)
                # Z scale from scan_params if available — keeps 3D stacks
                # visually proportional when the user flips through slices.
                try:
                    bf_index = scan_params['dimensions'].index('Back-Front')
                    z_nm = float(scan_params['step_sizes'][bf_index])
                    if "Z" in axis_labels:
                        axis_scales[axis_labels.index("Z")] = z_nm
                except (KeyError, ValueError, TypeError):
                    pass

        super().__init__(
            name=name,
            data=data,
            axis_labels=axis_labels,
            view_modes=view_modes,
            display_levels=display_levels,
            axis_scales=axis_scales,
            scale_unit=scale_unit,
        )

        self.scan_params = scan_params
        self.output_pixel_size_nm = output_pixel_size_nm
    
    def save(self, path: Path, fmt: str = "tiff") -> None:
        """
        Save MoNaLISA reconstruction as ImageJ-compatible 6D TIFF.
        
        Args:
            path: Output file path
            fmt: Format string ("tiff" only for now)
        """
        if fmt != "tiff":
            raise ValueError(f"MoNaLISA result only supports 'tiff' format, got '{fmt}'")
        
        # Compute ImageJ metadata
        vxsizec = int(float(
            self.scan_params['step_sizes'][self.scan_params['dimensions'].index('Right-Left')]
        ))
        vxsizer = int(float(
            self.scan_params['step_sizes'][self.scan_params['dimensions'].index('Up-Down')]
        ))
        vxsizez = int(float(
            self.scan_params['step_sizes'][self.scan_params['dimensions'].index('Back-Front')]
        ))
        
        # ImageJ axes attribute
        numDatasets = self.data.shape[0]
        numBases = self.data.shape[1]
        numTimepoints = self.data.shape[2]
        numSlices = self.data.shape[3]
        
        ijmetadata = {
            'axes': 'TZCYXS' if numDatasets > 1 else 'TZCYX'
        }
        
        # Resolution metadata
        resolution = (10000.0 / vxsizec, 10000.0 / vxsizer)
        
        # Reshape for ImageJ: collapse Dataset + Base into S (series/channels)
        # ImageJ TIFF stacks expect (T, Z, C, Y, X) or (T, Z, C, Y, X, S)
        data_to_save = self.data
        if numDatasets > 1:
            # (Dataset, Base, T, Z, Y, X) -> (T, Z, Base*Dataset, Y, X)
            data_to_save = np.moveaxis(data_to_save, [0, 1, 2, 3, 4, 5], [2, 4, 0, 1, 5, 3])
            data_to_save = data_to_save.reshape(
                numTimepoints, numSlices, numBases * numDatasets, -1, data_to_save.shape[-1]
            )
        else:
            # (1, Base, T, Z, Y, X) -> (T, Z, Base, Y, X)
            data_to_save = data_to_save[0]  # drop Dataset axis
            data_to_save = np.moveaxis(data_to_save, [0, 1, 2, 3], [0, 1, 3, 2])
        
        # Save with ImageJ compatibility
        with tiff.TiffWriter(str(path), bigtiff=True, imagej=True) as tif:
            tif.write(
                data_to_save,
                resolution=resolution,
                metadata=ijmetadata,
                photometric='minisblack'
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
