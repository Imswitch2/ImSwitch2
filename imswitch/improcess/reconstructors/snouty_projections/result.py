"""SNOUTY projections reconstruction result."""

import numpy as np
import tifffile as tiff
from pathlib import Path

from imswitch.improcess.model.result import ProcessingResult, ViewMode


class SnoutyProjectionsResult(ProcessingResult):
    """
    SNOUTY lightsheet projections result (fast preview mode).
    
    Data shape:
        - 3D: (3, H, W) — three projections (xy, xz, yz) padded to common canvas
        - 4D: (T, 3, H, W) — multi-timepoint projections
    
    Axis labels: ["projection", "Y", "X"] for 3D, ["T", "projection", "Y", "X"] for 4D.
    
    The "projection" axis contains:
        - Index 0: XY projection (max along Z)
        - Index 1: XZ projection (max along Y)
        - Index 2: YZ projection (max along X)
    
    Each projection is padded to the common canvas size (H, W) with zeros.
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
            name: Human-readable name (e.g., "snouty_projections")
            data: Padded projection stack (3D or 4D)
            params: Parameter dict used for deskewing (saved for metadata)
            display_levels: Optional (min, max) display range
        """
        ndim = data.ndim
        if ndim == 3:
            axis_labels = ["projection", "Y", "X"]
            view_modes = [
                ViewMode("Standard", (0, 1, 2)),  # Scroll through projections
            ]
        elif ndim == 4:
            axis_labels = ["T", "projection", "Y", "X"]
            view_modes = [
                ViewMode("Standard", (0, 1, 2, 3)),  # Scroll through time and projections
            ]
        else:
            raise ValueError(
                f"SNOUTY projections result must be 3D or 4D, got shape {data.shape}"
            )
        
        super().__init__(
            name=name,
            data=data,
            axis_labels=axis_labels,
            view_modes=view_modes,
            display_levels=display_levels
        )
        
        self.params = params
    
    def save(self, path: Path, fmt: str = "tiff") -> None:
        """
        Save SNOUTY projections result.
        
        Args:
            path: Output file path
            fmt: Format string ("tiff" only for now)
        
        TIFF format:
            - ImageJ-compatible with axes "CYX" (3D) or "TCYX" (4D)
            - The three projections map to the C (channel) axis
        """
        if fmt == "tiff":
            self._save_tiff(path)
        else:
            raise ValueError(
                f"SNOUTY projections result supports 'tiff' only, got '{fmt}'"
            )
    
    def _save_tiff(self, path: Path) -> None:
        """Save as ImageJ-compatible TIFF with projections as channels."""
        if self.data.ndim == 3:
            axes = "CYX"
        elif self.data.ndim == 4:
            axes = "TCYX"
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
