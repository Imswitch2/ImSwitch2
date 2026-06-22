"""MoNaLISA SIM reconstructor plugin."""

from typing import TYPE_CHECKING

import numpy as np
from qtpy import QtWidgets

from imswitch.imcommon.model import initLogger
from imswitch.improcess.reconstructors.base import StreamingReconstructor
from .live_session import MonalisaLiveSession
from .orientation import auto_detect_scan_orientation
from .params_widget import MonalisaParamsWidget
from .pattern_finder import PatternFinder
from .result import MonalisaProcessingResult
from .signal_extractor import SignalExtractor

if TYPE_CHECKING:
    from imswitch.improcess.model import DataObj


class MonalisaReconstructor(StreamingReconstructor):
    """
    MoNaLISA structured illumination microscopy (SIM) reconstructor.
    
    Extracts spatial frequency components from point-scanning data acquired
    with a patterned illumination grating, then reassigns them to reconstruct
    super-resolved images.
    
    Input data format:
        3D array (frames, rows, cols) where frames are ordered according to scan dimensions
    
    Output format:
        6D array (datasets, bases, timepoints, slices, rows, cols)
        - datasets: typically 1 (or multiple if consolidating multi-data)
        - bases: number of spatial frequency components
        - timepoints, slices, rows, cols: scan dimensions
    """
    
    name = "MoNaLISA"
    id = "monalisa"
    file_extensions = ["hdf5", "zarr"]
    description = "Point-scanning SIM reconstruction with pattern-based signal extraction"
    supports_streaming = True
    
    def __init__(self):
        self._logger = initLogger('MonalisaReconstructor')
        self._pattern_finder = PatternFinder()
        self._signal_extractor = None  # Lazy-loaded on first use (Windows-only)
        self._axis_labels = {
            'r_l_text': 'Right-Left',
            'u_d_text': 'Up-Down',
            'b_f_text': 'Back-Front',
            'timepoints_text': 'Timepoints',
            'p_text': 'pos',
            'n_text': 'neg'
        }
    
    def _ensure_signal_extractor(self):
        """Lazy-load SignalExtractor (Windows-only, requires CUDA DLLs)."""
        if self._signal_extractor is None:
            try:
                self._signal_extractor = SignalExtractor()
            except RuntimeError as e:
                raise RuntimeError(
                    f'SignalExtractor initialization failed: {e}. '
                    'MoNaLISA reconstruction requires Windows + CUDA libraries.'
                ) from e
    
    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        """Create and return the MoNaLISA parameter widget."""
        return MonalisaParamsWidget(parent)
    
    def make_metadata_dialog(self, parent: QtWidgets.QWidget) -> QtWidgets.QDialog | None:
        """
        Create scan parameters dialog for MoNaLISA acquisition metadata.
        
        This manages the 4D scan geometry: dimensions, directions, steps, step_sizes,
        and unidirectional flag. For now, we return None and handle this separately
        in the controller transition phase.
        
        TODO: Wrap ScanParamsDialog in a proper MonalisaScanParamsDialog subclass.
        """
        # For now, return None - the controller will manage ScanParamsDialog directly
        # during the transition phase
        return None
    
    def find_pattern(self, data: np.ndarray, param_widget: QtWidgets.QWidget) -> None:
        """
        Automatically detect the illumination pattern in the data and update widget params.
        
        Args:
            data: Raw scan data (3D: frames, rows, cols)
            param_widget: MonalisaParamsWidget instance to update
        """
        if not isinstance(param_widget, MonalisaParamsWidget):
            raise TypeError(f'Expected MonalisaParamsWidget, got {type(param_widget).__name__}')
        
        # Use first frame for pattern detection
        test_frame = data[0] if data.ndim == 3 else data
        
        # Find pattern
        row_offset, col_offset, row_period, col_period = self._pattern_finder.find(test_frame)
        
        # Update widget
        param_widget.set_pattern_params(row_offset, col_offset, row_period, col_period)
        
        self._logger.info(f'Pattern found: row_offset={row_offset:.2f}, col_offset={col_offset:.2f}, '
                         f'row_period={row_period:.2f}, col_period={col_period:.2f}')
    
    def process(self, data_obj: 'DataObj', params: dict) -> MonalisaProcessingResult:
        """
        Reconstruct MoNaLISA SIM data.
        
        Args:
            data_obj: DataObj containing raw scan data + metadata
            params: Parameter dict with keys (from MonalisaParamsWidget.get_values()):
                - pixel_size_nm: float
                - device: str ('CPU' or 'GPU')
                - row_offset, col_offset, row_period, col_period: float
                - psf_fwhm_nm: float
                - bg_modelling: str
                - bg_gaussian_size_nm: float
                - bleaching_correction: bool
                - scan_params: dict with 'dimensions', 'directions', 'steps', 'step_sizes', 'unidirectional'
        
        Returns:
            MonalisaProcessingResult containing reconstructed 6D image
        """
        # Extract scan parameters (required for MoNaLISA)
        scan_params = params.get('scan_params')
        if scan_params is None:
            raise ValueError('MoNaLISA requires scan_params in params dict')
        
        # Load data
        preloaded = data_obj.dataLoaded
        try:
            data_obj.checkAndLoadData()
            data = data_obj.data
        finally:
            if not preloaded:
                data_obj.checkAndUnloadData()
        
        # Validate data shape
        if data.ndim != 3:
            raise ValueError(f'Expected 3D data (frames, rows, cols), got shape {data.shape}')
        
        # Bleaching correction
        if params.get('bleaching_correction', False):
            data = self._apply_bleaching_correction(data)
        
        # Build pattern
        row_offset = np.mod(params['row_offset'], params['row_period'])
        col_offset = np.mod(params['col_offset'], params['col_period'])
        pattern = (row_offset, col_offset, params['row_period'], params['col_period'])
        
        # Build sigmas for signal extraction
        fwhm_nm = np.array([params['psf_fwhm_nm']])
        if params['bg_modelling'] == 'Constant':
            fwhm_nm = np.append(fwhm_nm, 9999)  # Code for constant bg
        elif params['bg_modelling'] == 'No background':
            fwhm_nm = np.append(fwhm_nm, 0)  # Code for zero bg
        elif params['bg_modelling'] == 'Gaussian':
            fwhm_nm = np.append(fwhm_nm, params['bg_gaussian_size_nm'])
        else:
            raise ValueError(f'Invalid BG modelling "{params["bg_modelling"]}"')
        
        sigmas = fwhm_nm / (2.355 * params['pixel_size_nm'])
        
        # Extract coefficients
        device = params['device'].lower()
        self._logger.info(f'Extracting signal with {params["device"]} on {data.shape[0]} frames...')
        self._ensure_signal_extractor()
        coeffs = self._signal_extractor.extractSignal(data, sigmas, pattern, device)
        # SignalExtractor returns shape (numBases, numFrames, gridRows, gridCols).
        # coeffs_to_image() takes a 3D (frames, gridRows, gridCols) slice, so we
        # iterate per base and stack along a new leading Base axis — matching
        # the legacy ReconObj.updateImages contract. Without this loop the
        # output collapsed to a confusing 2-frame stack because the bases axis
        # was treated as if it were the scan-frame axis.
        if coeffs.ndim != 4:
            raise ValueError(
                f'SignalExtractor returned shape {coeffs.shape}; '
                f'expected (numBases, numFrames, gridRows, gridCols)'
            )
        num_bases = coeffs.shape[0]

        # Auto-detect the scan fast/slow axes and pos/neg directions by
        # minimizing the total variation of the signal-base reconstruction.
        # Mirrors Mini_Recon's get_orientation; user can disable via the
        # 'Auto-detect scan orientation' checkbox to keep the dialog values.
        if params.get('auto_scan_orientation', True):
            try:
                best_params, best_label, best_score = auto_detect_scan_orientation(
                    coeffs[0], scan_params, self._axis_labels,
                )
                self._logger.info(
                    f'Auto scan orientation: {best_label}  (TV score {best_score:.3g})'
                )
                scan_params = best_params
            except Exception as exc:
                # The detector is a quality-of-life add-on; falling back to
                # the dialog values must never block a reconstruction.
                self._logger.warning(
                    f'Scan-orientation auto-detect failed, using dialog values: {exc}'
                )

        self._logger.info(
            f'Converting coefficients to images ({num_bases} bases x '
            f'{coeffs.shape[1]} frames -> per-base reconstruction)...'
        )
        # Add the leading Dataset axis (single dataset per process() call) so
        # the retained coefficients match the (Dataset, Base, frames, gridRows,
        # gridCols) contract shared with the legacy controller path.  Retaining
        # them lets the viewer re-reconstruct on scan-param edits and export
        # coefficients.  from_coeffs() reassembles the 6D image, derives the
        # output pixel pitch and auto display levels.
        coeffs_5d = coeffs[np.newaxis, ...]
        result = MonalisaProcessingResult.from_coeffs(
            name=data_obj.name,
            coeffs=coeffs_5d,
            scan_params=scan_params,
            axis_label_map=self._axis_labels,
        )

        self._logger.info(f'Reconstruction complete: shape {result.data.shape}')
        return result
    
    def make_session(self) -> MonalisaLiveSession:
        """
        Create a fresh streaming session for live reconstruction.
        
        Returns:
            MonalisaLiveSession instance.
        """
        return MonalisaLiveSession()
    
    def _apply_bleaching_correction(self, data: np.ndarray) -> np.ndarray:
        """
        Apply photobleaching correction to raw data.
        
        Uses 4th-power energy normalization: c = (E_0 / E_i)^4
        
        Args:
            data: 3D array (frames, rows, cols)
        
        Returns:
            Corrected 3D array
        """
        corrected_data = data.copy()
        energy = np.sum(data, axis=(1, 2))
        for i in range(data.shape[0]):
            c = (energy[0] / energy[i]) ** 4
            corrected_data[i, :, :] = data[i, :, :] * c
        return corrected_data


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
