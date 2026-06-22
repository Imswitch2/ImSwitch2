"""Base contract for ImProcess reconstructor plugins."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.model import DataObj
from imswitch.improcess.model.result import ProcessingResult, ViewMode


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
    is_pass_through: bool = False
    """Set ``True`` for reconstructors whose ``process()`` is a no-op wrap.

    When the active reconstructor is pass-through, ImProcess auto-routes the
    current ``DataObj`` to the napari viewer the moment it changes, so the
    user does not have to click 'Reconstruct current' for a plugin whose only
    job is to display the data. Pass-through plugins also hide the
    'Reconstruct current' and 'Update reconstruction' actions, since those
    are ceremonial in their case.
    """

    default_save_subdir: str = "rec"
    """Subdirectory name the WatcherFrame uses for reconstructed outputs.

    The file watcher writes one output per watched input under
    ``{watched_dir}/{default_save_subdir}/``. Override per modality to pick a
    plugin-appropriate folder name (e.g. ``"deskew"`` for SNOUTY) or to keep
    multiple watchers coexisting without overwriting each other. ``"rec"`` is
    the historical MoNaLISA default and is kept here so behavior is
    unchanged for plugins that don't override.
    """


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


@dataclass(frozen=True)
class StackInfo:
    """Metadata for one logical live stack."""

    frame_shape: tuple[int, ...]
    dtype: np.dtype
    attrs: dict[str, Any] = field(default_factory=dict)
    frames_per_stack: int | None = None
    expected_frames: int | None = None
    detector_name: str | None = None
    dataset_path: str | None = None
    source_format: str | None = None


@dataclass(frozen=True)
class Chunk:
    """Contiguous frame range yielded by a live source."""

    data: np.ndarray
    start: int
    end: int


@dataclass(frozen=True)
class StreamPlan:
    """Output shape and display metadata for a streaming session."""

    out_shape: tuple[int, ...]
    axis_labels: list[str]
    view_modes: list[ViewMode]
    dtype: np.dtype = np.dtype(np.float32)
    scale_unit: str = "px"
    axis_scales: list[float] | None = None


@dataclass(frozen=True)
class StreamInit:
    """First frames plus metadata, without forcing chunks through DataObj."""

    name: str
    dataset_name: str
    data: np.ndarray
    attrs: dict[str, Any] = field(default_factory=dict)
    source_path: str | None = None
    stack_info: StackInfo | None = None


class StreamingSession(ABC):
    """Stateful live reconstruction for one stack or recording."""

    @abstractmethod
    def begin(self, init_obj: StreamInit, params: dict) -> StreamPlan:
        """Inspect the first frames, allocate state, and return the output plan."""
        ...

    @abstractmethod
    def push(self, chunk: np.ndarray, start: int, end: int) -> None:
        """Process raw frames in the half-open range ``[start:end]``."""
        ...

    @abstractmethod
    def result(self) -> ProcessingResult:
        """Return a snapshot of the current reconstruction."""
        ...

    def finish(self) -> ProcessingResult:
        """Finalize processing and return the final result."""
        return self.result()

    def close(self) -> None:
        """Free optional resources such as GPU buffers."""
        return None


class StreamingReconstructor(Reconstructor):
    """Reconstructor that supports sub-stack live updates."""

    supports_streaming: bool = True

    @abstractmethod
    def make_session(self) -> StreamingSession:
        """Create a fresh session for one live stack or recording."""
        ...


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
