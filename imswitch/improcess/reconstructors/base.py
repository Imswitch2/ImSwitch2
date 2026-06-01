"""Base contract for ImProcess reconstructor plugins."""

from abc import ABC, abstractmethod
from typing import Any

from qtpy import QtWidgets

from imswitch.improcess.model import DataObj
from imswitch.improcess.model.result import ProcessingResult


class Reconstructor(ABC):
    """
    Turns raw DataObj into a ProcessingResult.
    
    Each dataset is processed by exactly one reconstructor. Examples:
    - MoNaLISA SIM pattern extraction + reassignment
    - SNOUTY lightsheet deskew
    - View-only (wraps raw data with no processing)
    - STED deconvolution
    """
    
    # Class attributes (override in subclasses)
    name: str = "Unnamed Reconstructor"  # Human-readable, shown in plugin picker
    id: str = "unnamed"  # Stable identifier for config + registry lookups
    file_extensions: list[str] = ["hdf5", "tiff", "zarr"]  # Watcher dispatch + filtering
    
    @abstractmethod
    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        """
        Return the parameter editor widget embedded in ImProcessMainView left panel.
        
        Replaces the hard-coded ReconParTree. The widget should expose a
        `get_values() -> dict` method that the controller calls before processing.
        
        MoNaLISA example: pattern offsets/periods, PSF FWHM, BG model, denoiser name.
        View-only example: just a "Pixel size (nm)" input field.
        """
        ...
    
    @abstractmethod
    def make_metadata_dialog(self, parent: QtWidgets.QWidget) -> QtWidgets.QDialog | None:
        """
        Return the acquisition metadata dialog (replaces ScanParamsDialog).
        
        Return None if the reconstructor needs no acquisition metadata.
        
        MoNaLISA: 4D scan geometry (dimensions, directions, steps, unidirectional flag).
        View-only: None (no scan parameters needed).
        """
        ...
    
    @abstractmethod
    def process(self, data_obj: DataObj, params: dict) -> ProcessingResult:
        """
        Run the reconstruction pipeline.
        
        Pure function over (data, params). No side effects, no GUI updates.
        All MoNaLISA-specific logic (PatternFinder, SignalExtractor, coeffsToImage)
        lives inside the MoNaLISA plugin's implementation of this method.
        
        Args:
            data_obj: Raw input data (HDF5/TIFF/Zarr)
            params: Parameter dict from `make_param_widget().get_values()`
        
        Returns:
            ProcessingResult with data + axis_labels + view_modes
        """
        ...
    
    def make_overlay(self, data_obj: DataObj, params: dict) -> Any | None:
        """
        Optional: provide a viewer overlay for the raw data view (DataFrame).
        
        Examples:
        - MoNaLISA: red scatter points showing detected SIM pattern grid
        - STED: depletion beam ROI outline
        - View-only: None (no overlay)
        
        Returns:
            A pyqtgraph GraphicsItem (e.g., ScatterPlotItem) to be added to
            DataFrame.imageItem.getViewBox(), or None for no overlay.
        """
        return None


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
