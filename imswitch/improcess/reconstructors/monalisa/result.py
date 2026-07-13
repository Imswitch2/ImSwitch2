"""MoNaLISA-specific processing result."""

import os
from pathlib import Path

import numpy as np
import tifffile as tiff

from imswitch.improcess.model.result import DisplayLayerSpec, ProcessingResult, ViewMode
from .coeffs_to_image import output_pixel_size_nm, reconstruct_images_from_coeffs

# Canonical semantic-name → scan-dimension-name map. Reconstructions produced
# by MonalisaReconstructor use these names; the legacy controller path passes
# its own widget-text map (e.g. "Right/Left") instead.
DEFAULT_AXIS_LABEL_MAP = {
    'r_l_text': 'Right-Left',
    'u_d_text': 'Up-Down',
    'b_f_text': 'Back-Front',
    'timepoints_text': 'Timepoints',
    'p_text': 'pos',
    'n_text': 'neg',
}


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
        coeffs: np.ndarray | None = None,
        axis_label_map: dict[str, str] | None = None,
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
            coeffs: Optional 5D ``(Dataset, Base, frames, gridRows, gridCols)``
                signal-extraction coefficients. Retained so the viewer can
                re-reconstruct cheaply when scan params change (the "Update
                reconstruction" action) and so coefficients can be exported.
            axis_label_map: Semantic-name → scan-dimension-name map describing
                how ``scan_params['dimensions']`` is named. Defaults to the
                canonical reconstructor names; the legacy controller path
                passes its widget-text names instead.
        """
        if axis_labels is None:
            axis_labels = ["Dataset", "Base", "T", "Z", "Y", "X"]

        if axis_label_map is None:
            axis_label_map = dict(DEFAULT_AXIS_LABEL_MAP)

        # Define view modes for MoNaLISA 6D data. The viewer displays the last
        # two transposed axes and puts sliders on the rest, so the orthogonal
        # views must move Z into a displayed position.
        view_modes = [
            ViewMode("Standard", (0, 1, 2, 3, 4, 5)),  # Dataset, Base, T, Z, Y, X — displays (Y, X)
            ViewMode("Bottom", (0, 1, 2, 4, 3, 5)),    # Dataset, Base, T, Y, Z, X — displays (Z, X)
            ViewMode("Left", (0, 1, 2, 5, 3, 4)),      # Dataset, Base, T, X, Z, Y — displays (Z, Y)
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
                    bf_index = scan_params['dimensions'].index(axis_label_map['b_f_text'])
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
        self.coeffs = coeffs
        self.axis_label_map = axis_label_map

    @classmethod
    def from_coeffs(
        cls,
        name: str,
        coeffs: np.ndarray,
        scan_params: dict,
        axis_label_map: dict[str, str],
        display_levels: tuple[float, float] | None = None,
    ) -> "MonalisaProcessingResult":
        """Build a result by reassembling images from per-base coefficients.

        Args:
            name: Result name.
            coeffs: 5D ``(Dataset, Base, frames, gridRows, gridCols)`` array.
            scan_params: Scan metadata dict.
            axis_label_map: Semantic-name → dimension-name map matching
                ``scan_params['dimensions']``.
            display_levels: Optional (min, max); auto-computed from the 1st /
                99.9th percentile when omitted.
        """
        data = reconstruct_images_from_coeffs(coeffs, scan_params, axis_label_map)
        out_px = output_pixel_size_nm(scan_params, axis_label_map)
        if display_levels is None:
            display_levels = (
                float(np.percentile(data, 1)),
                float(np.percentile(data, 99.9)),
            )
        return cls(
            name=name,
            data=data,
            scan_params=scan_params,
            display_levels=display_levels,
            output_pixel_size_nm=out_px,
            coeffs=coeffs,
            axis_label_map=axis_label_map,
        )

    def getCoeffs(self) -> np.ndarray | None:
        """Return the retained signal-extraction coefficients (or None)."""
        return self.coeffs

    def getScanParams(self) -> dict:
        return self.scan_params

    def updateScanParams(self, scan_params: dict) -> None:
        """Replace scan params (used before :meth:`updateImages`)."""
        self.scan_params = scan_params

    def updateImages(self) -> None:
        """Re-reassemble ``data`` from the retained coefficients.

        Lets the viewer re-render after the user edits scan geometry without
        re-running the expensive signal-extraction step. No-op when coefficients
        were not retained (e.g. results loaded from disk).
        """
        if self.coeffs is None:
            return
        self.data = reconstruct_images_from_coeffs(
            self.coeffs, self.scan_params, self.axis_label_map
        )
        self.output_pixel_size_nm = output_pixel_size_nm(
            self.scan_params, self.axis_label_map
        )
        if (
            self.output_pixel_size_nm is not None
            and "Y" in self.axis_labels
            and "X" in self.axis_labels
        ):
            y_nm, x_nm = self.output_pixel_size_nm
            self.axis_scales[self.axis_labels.index("Y")] = float(y_nm)
            self.axis_scales[self.axis_labels.index("X")] = float(x_nm)
            try:
                bf_index = self.scan_params['dimensions'].index(
                    self.axis_label_map['b_f_text']
                )
                z_nm = float(self.scan_params['step_sizes'][bf_index])
                if "Z" in self.axis_labels:
                    self.axis_scales[self.axis_labels.index("Z")] = z_nm
            except (KeyError, ValueError, TypeError):
                pass

    def _base_component_name(self, base_index: int) -> str:
        """Return the semantic name for a base component."""
        if base_index == 0:
            return "signal"
        elif base_index == 1:
            return "background"
        else:
            return f"base_{base_index}"
    
    def display_layers(self) -> list[DisplayLayerSpec]:
        """Split the Base axis into named viewer layers.
        
        Returns one layer per base component (signal, background, base_2, ...),
        each with the Base axis sliced out and independent contrast limits.
        """
        if "Base" not in self.axis_labels:
            return []
        
        base_axis = self.axis_labels.index("Base")
        num_bases = self.data.shape[base_axis]
        
        # Build axis labels and scales for display layers (all axes except Base)
        layer_axis_labels = [lbl for lbl in self.axis_labels if lbl != "Base"]
        layer_axis_scales = [
            scale for i, scale in enumerate(self.axis_scales) if i != base_axis
        ]
        
        layers = []
        for base_idx in range(num_bases):
            # Slice out this base component
            layer_data = np.take(self.data, base_idx, axis=base_axis)
            
            # Compute per-layer contrast limits from finite values
            finite_data = layer_data[np.isfinite(layer_data)]
            if finite_data.size > 0:
                vmin, vmax = np.percentile(finite_data, [1, 99])
                if vmin == vmax:
                    value = float(finite_data.min())
                    pad = max(abs(value) * 1e-6, 1e-6)
                    vmin, vmax = value - pad, value + pad
            else:
                vmin, vmax = 0.0, 1.0
            
            component_name = self._base_component_name(base_idx)
            layer = DisplayLayerSpec(
                name=f"{self.name}_{component_name}",
                data=layer_data,
                axis_labels=layer_axis_labels,
                display_levels=(float(vmin), float(vmax)),
                axis_scales=layer_axis_scales,
                scale_unit=self.scale_unit,
                # Show the reconstruction (signal, base 0) by default; hide the
                # background/other bases so the first thing seen after a recon is
                # the reconstruction, not the background layer sitting on top.
                visible=(base_idx == 0),
                metadata={
                    "source_result": self.name,
                    "component": component_name,
                    "base_index": base_idx,
                },
            )
            layers.append(layer)
        
        return layers
    
    def save(self, path: Path, fmt: str = "tiff") -> None:
        """
        Save MoNaLISA reconstruction as ImageJ-compatible 6D TIFF.
        
        Args:
            path: Output file path
            fmt: Format string ("tiff" only for now)
        """
        if fmt != "tiff":
            raise ValueError(f"MoNaLISA result only supports 'tiff' format, got '{fmt}'")
        
        # Compute ImageJ metadata. Dimension names are resolved through the
        # result's axis-label map so both reconstructor-produced ("Right-Left")
        # and legacy controller-produced ("Right/Left") scan params save.
        dims = self.scan_params['dimensions']
        step_sizes = self.scan_params['step_sizes']
        vxsizec = int(float(step_sizes[dims.index(self.axis_label_map['r_l_text'])]))
        vxsizer = int(float(step_sizes[dims.index(self.axis_label_map['u_d_text'])]))
        vxsizez = int(float(step_sizes[dims.index(self.axis_label_map['b_f_text'])]))
        
        # ImageJ hyperstack dimensions. The reconstruction is
        # (Dataset, Base, T, Z, Y, X); ImageJ wants the canonical (T, Z, C, Y, X)
        # ordering, so the Dataset and Base axes are folded into the channel
        # axis (C = Dataset*Base, e.g. ds0-signal, ds0-background, ds1-signal …).
        numDatasets = self.data.shape[0]
        numBases = self.data.shape[1]
        numTimepoints = self.data.shape[2]
        numSlices = self.data.shape[3]
        numRows = self.data.shape[4]
        numCols = self.data.shape[5]

        ijmetadata = {'axes': 'TZCYX'}

        # Resolution metadata
        resolution = (10000.0 / vxsizec, 10000.0 / vxsizer)

        # (Dataset, Base, T, Z, Y, X) -> (T, Z, Dataset, Base, Y, X) -> (T, Z, C, Y, X)
        data_to_save = np.moveaxis(self.data, [0, 1, 2, 3, 4, 5], [2, 3, 0, 1, 4, 5])
        data_to_save = np.ascontiguousarray(data_to_save).reshape(
            numTimepoints, numSlices, numDatasets * numBases, numRows, numCols
        )

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
