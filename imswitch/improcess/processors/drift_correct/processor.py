"""Cross-correlation drift correction processor."""

from typing import Callable

import numpy as np
from qtpy import QtWidgets
from scipy import ndimage
from skimage.registration import phase_cross_correlation

from imswitch.improcess.processors.base import Processor
from imswitch.improcess.model.result import ProcessingResult
from imswitch.imcommon.model import initLogger
from .result import DriftCorrectedResult


class DriftCorrectProcessor(Processor):
    """
    Drift correction using phase cross-correlation.
    
    Computes sub-pixel shifts between consecutive frames (or against a reference frame)
    and applies correction via Fourier-domain shifts.
    
    Works on any ProcessingResult with a time axis ("T").
    """
    
    name = "Drift Correction"
    id = "drift-correct"
    
    def __init__(self):
        self._logger = initLogger(self, tryInheritParent=False)
    
    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        """Gate: requires time axis."""
        return lambda r: "T" in r.axis_labels
    
    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        """
        Create parameter widget for drift correction settings.
        
        Parameters:
        - Reference frame index (0 = first frame)
        - Correction mode: "sequential" (frame-to-frame) or "reference" (all to ref)
        """
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)
        
        # Reference frame selector
        ref_frame_spin = QtWidgets.QSpinBox()
        ref_frame_spin.setMinimum(0)
        ref_frame_spin.setMaximum(9999)
        ref_frame_spin.setValue(0)
        ref_frame_spin.setToolTip("Frame index to use as reference (0-indexed)")
        layout.addRow("Reference frame:", ref_frame_spin)
        
        # Correction mode selector
        mode_combo = QtWidgets.QComboBox()
        mode_combo.addItems(["reference", "sequential"])
        mode_combo.setCurrentText("reference")
        mode_combo.setToolTip(
            "reference: align all frames to reference frame\n"
            "sequential: align each frame to previous frame"
        )
        layout.addRow("Correction mode:", mode_combo)
        
        # Upsample factor for sub-pixel precision
        upsample_spin = QtWidgets.QSpinBox()
        upsample_spin.setMinimum(1)
        upsample_spin.setMaximum(100)
        upsample_spin.setValue(10)
        upsample_spin.setToolTip("Sub-pixel precision factor (higher = more accurate, slower)")
        layout.addRow("Upsample factor:", upsample_spin)
        
        # Store references for get_values()
        widget._ref_frame_spin = ref_frame_spin
        widget._mode_combo = mode_combo
        widget._upsample_spin = upsample_spin
        
        # Add get_values() method
        def get_values():
            return {
                'reference_frame': ref_frame_spin.value(),
                'mode': mode_combo.currentText(),
                'upsample_factor': upsample_spin.value()
            }
        widget.get_values = get_values
        
        return widget
    
    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        """
        Apply drift correction to the input result.
        
        Args:
            result: Input ProcessingResult with time axis
            params: Parameter dict with 'reference_frame', 'mode', 'upsample_factor'
        
        Returns:
            DriftCorrectedResult with corrected data and drift_xy array
        """
        # Extract parameters
        reference_frame = params.get('reference_frame', 0)
        mode = params.get('mode', 'reference')
        upsample_factor = params.get('upsample_factor', 10)
        
        self._logger.info(
            f"Applying drift correction: mode={mode}, ref_frame={reference_frame}, "
            f"upsample={upsample_factor}"
        )
        
        # Find time axis
        try:
            t_axis = result.axis_labels.index("T")
        except ValueError:
            raise ValueError("Drift correction requires a time axis ('T')")
        
        # Get data shape info
        data = result.data
        n_frames = data.shape[t_axis]
        
        # Validate reference frame
        if reference_frame < 0 or reference_frame >= n_frames:
            self._logger.warning(
                f"Reference frame {reference_frame} out of range [0, {n_frames-1}], "
                f"using frame 0"
            )
            reference_frame = 0
        
        # Move time axis to first position for easier iteration
        data_t_first = np.moveaxis(data, t_axis, 0)
        
        # Compute shifts
        shifts = self._compute_shifts(
            data_t_first, reference_frame, mode, upsample_factor
        )
        
        # Apply shifts
        corrected_data = self._apply_shifts(data_t_first, shifts)
        
        # Move time axis back to original position
        corrected_data = np.moveaxis(corrected_data, 0, t_axis)
        
        # Create result
        return DriftCorrectedResult(
            name=f"{result.name} (drift-corrected)",
            data=corrected_data,
            axis_labels=result.axis_labels,
            drift_xy=shifts,
            view_modes=result.view_modes,
            display_levels=result.display_levels,
            axis_scales=list(result.axis_scales),
            scale_unit=result.scale_unit,
        )
    
    def _compute_shifts(
        self,
        data: np.ndarray,
        reference_frame: int,
        mode: str,
        upsample_factor: int
    ) -> np.ndarray:
        """
        Compute drift shifts for all frames.
        
        Args:
            data: Data with time axis in first position (T, ...)
            reference_frame: Index of reference frame
            mode: "reference" or "sequential"
            upsample_factor: Sub-pixel precision factor
        
        Returns:
            shifts: Array of shape (n_frames, 2) with (Y, X) shifts
        """
        n_frames = data.shape[0]
        shifts = np.zeros((n_frames, 2), dtype=np.float32)
        
        # Get reference frame
        ref_frame = self._extract_2d_frame(data, reference_frame)
        
        if mode == "reference":
            # Align all frames to reference
            for i in range(n_frames):
                if i == reference_frame:
                    continue  # No shift for reference frame
                
                current_frame = self._extract_2d_frame(data, i)
                shift, _, _ = phase_cross_correlation(
                    ref_frame,
                    current_frame,
                    upsample_factor=upsample_factor
                )
                shifts[i] = shift
        
        elif mode == "sequential":
            # Align each frame to previous frame
            cumulative_shift = np.zeros(2, dtype=np.float32)
            prev_frame = ref_frame
            
            for i in range(n_frames):
                if i == reference_frame:
                    prev_frame = ref_frame
                    continue
                
                current_frame = self._extract_2d_frame(data, i)
                shift, _, _ = phase_cross_correlation(
                    prev_frame,
                    current_frame,
                    upsample_factor=upsample_factor
                )
                
                cumulative_shift += shift
                shifts[i] = cumulative_shift
                prev_frame = current_frame
        
        else:
            raise ValueError(f"Unknown mode: {mode}")
        
        self._logger.debug(f"Computed shifts (Y, X):\n{shifts}")
        return shifts
    
    def _extract_2d_frame(self, data: np.ndarray, frame_idx: int) -> np.ndarray:
        """
        Extract a 2D frame from potentially higher-dimensional data.
        
        Assumes time axis is first, and last 2 dimensions are Y/X.
        For intermediate dimensions (Z, C, etc.), takes the first slice.
        """
        frame = data[frame_idx]
        
        # Reduce to 2D by taking first slice of any extra dimensions
        while frame.ndim > 2:
            frame = frame[0]
        
        return frame
    
    def _apply_shifts(self, data: np.ndarray, shifts: np.ndarray) -> np.ndarray:
        """
        Apply computed shifts to data using Fourier-domain shifting.
        
        Args:
            data: Data with time axis in first position (T, ...)
            shifts: Array of shape (n_frames, 2) with (Y, X) shifts
        
        Returns:
            corrected_data: Drift-corrected data with same shape as input
        """
        n_frames = data.shape[0]
        corrected_data = np.zeros_like(data)
        
        for i in range(n_frames):
            frame = data[i]
            shift = shifts[i]
            
            # Apply shift to 2D slices
            corrected_frame = self._shift_nd(frame, shift)
            corrected_data[i] = corrected_frame
        
        return corrected_data
    
    def _shift_nd(self, data: np.ndarray, shift_yx: np.ndarray) -> np.ndarray:
        """
        Apply Y/X shift to potentially higher-dimensional data.
        
        Applies shift to last 2 dimensions, preserving other dimensions.
        """
        if data.ndim == 2:
            # Simple 2D case. fourier_shift returns a frequency-domain array;
            # an explicit inverse FFT is required to get the shifted image.
            return np.fft.ifftn(
                ndimage.fourier_shift(np.fft.fftn(data), shift_yx)
            ).real
        
        # Higher-dimensional case: apply shift to each 2D slice
        result = np.zeros_like(data)
        
        # Iterate over all slices in extra dimensions
        if data.ndim == 3:
            for z in range(data.shape[0]):
                result[z] = np.fft.ifftn(
                    ndimage.fourier_shift(np.fft.fftn(data[z]), shift_yx)
                ).real
        elif data.ndim == 4:
            for z in range(data.shape[0]):
                for c in range(data.shape[1]):
                    result[z, c] = np.fft.ifftn(
                        ndimage.fourier_shift(np.fft.fftn(data[z, c]), shift_yx)
                    ).real
        elif data.ndim == 5:
            for z in range(data.shape[0]):
                for c in range(data.shape[1]):
                    for b in range(data.shape[2]):
                        result[z, c, b] = np.fft.ifftn(
                            ndimage.fourier_shift(np.fft.fftn(data[z, c, b]), shift_yx)
                        ).real
        else:
            # Fallback: flatten extra dims, process, reshape
            extra_shape = data.shape[:-2]
            n_extra = np.prod(extra_shape)
            flat_data = data.reshape(n_extra, *data.shape[-2:])
            flat_result = np.zeros_like(flat_data)
            
            for i in range(n_extra):
                flat_result[i] = np.fft.ifftn(
                    ndimage.fourier_shift(np.fft.fftn(flat_data[i]), shift_yx)
                ).real
            
            result = flat_result.reshape(data.shape)
        
        return result


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
