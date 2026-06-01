"""Processing result abstractions for ImProcess reconstructors and processors."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .plotting import PlotPayload


@dataclass
class ViewMode:
    """Named axis permutation for ReconstructionView."""
    name: str
    transpose: tuple[int, ...]


class ProcessingResult(ABC):
    """
    Abstract base for all reconstructor/processor outputs.
    
    Replaces the direct ReconObj exposure. Each reconstructor returns a
    subclass tailored to its output format (MoNaLISA coefficients vs.
    raw frames vs. STED deconvolved stacks, etc.).
    """
    
    def __init__(
        self,
        name: str,
        data: np.ndarray | Any,  # Allow zarr.Array in future
        axis_labels: list[str],
        view_modes: list[ViewMode] | None = None,
        display_levels: tuple[float, float] | None = None
    ):
        """
        Args:
            name: Human-readable result name (e.g., "MoNaLISA reconstruction",
                  "Drift-corrected frames")
            data: N-dimensional array, no fixed shape requirement
            axis_labels: Dimension labels in data order, e.g. ["T", "Z", "Y", "X"]
                         or ["Dataset", "Base", "T", "Z", "Y", "X"]
            view_modes: Optional list of axis permutations for viewer. If None,
                        defaults to a single "Standard" mode with no transposition.
            display_levels: Optional (min, max) display range for initial viewer LUT
        """
        self.name = name
        self.data = data
        self.axis_labels = axis_labels
        self.display_levels = display_levels
        
        if view_modes is None:
            # Default: single standard view with identity permutation
            self.view_modes = [ViewMode("Standard", tuple(range(data.ndim)))]
        else:
            self.view_modes = view_modes
    
    @abstractmethod
    def save(self, path: Path, fmt: str) -> None:
        """
        Save the result to disk.
        
        Args:
            path: Output file path
            fmt: Format string (e.g., "tiff", "hdf5", "zarr")
        
        The implementation is modality-specific: MoNaLISA saves 6D ImageJ TIFFs
        with specific axis order, STED might save multi-channel TIFFs, etc.
        """
        ...

    def plot_payloads(self) -> list[PlotPayload]:
        """Return optional graph payloads for the ImProcess graph widget."""
        return []


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
