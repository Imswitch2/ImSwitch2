"""MoNaLISA-specific processing result."""

import copy

import numpy as np
import tifffile as tiff

from imswitch.imcommon.model import initLogger

from imswitch.improcess.model.result import DisplayLayerSpec, ProcessingResult, ViewMode
from .coeffs_to_image import (
    LayoutPlacement,
    output_pixel_size_nm,
    reconstruct_images_from_coeffs,
)


def _projection_metadata(placement: LayoutPlacement | None) -> dict | None:
    """Describe how acquisition loops were projected onto the 6D result axes.

    The result keeps six dimensions so viewers, exporters and saved projects
    stay compatible, which means line-step conditions have to share the T axis
    with real elapsed time. Recording the projection is what makes that
    acceptable: nothing downstream may read a condition index as a timepoint
    just because it arrived on the axis historically called T.
    """
    if placement is None:
        return None
    folded = placement.folds_condition_into_time
    if not folded:
        display_name = "Time"
    elif placement.n_time == 1:
        display_name = "Condition"
    else:
        display_name = "Time x Condition"
    return {
        "axis": "T",
        "display_name": display_name,
        "components": ("time", "condition") if folded else ("time",),
        "order": "time-major",
        "n_time": placement.n_time,
        "n_conditions": placement.n_conditions,
        "condition_labels": placement.condition_labels,
    }

# ImageJ's hyperstack layout addresses data with 32-bit offsets, so it caps
# out below 4 GB. Past that only BigTIFF can hold the file -- and BigTIFF
# cannot carry ImageJ metadata (see write_files()).
IMAGEJ_MAX_BYTES = 3_900_000_000

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
        placement: "LayoutPlacement | None" = None,
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
        self.placement = placement
        self.acquisition_projection = _projection_metadata(placement)

    @classmethod
    def from_coeffs(
        cls,
        name: str,
        coeffs: np.ndarray,
        scan_params: dict,
        axis_label_map: dict[str, str],
        display_levels: tuple[float, float] | None = None,
        placement: LayoutPlacement | None = None,
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
            placement: Optional recorded per-frame output coordinates. When
                given it is authoritative and the scan-parameter arithmetic is
                never consulted.
        """
        data = reconstruct_images_from_coeffs(
            coeffs, scan_params, axis_label_map, placement=placement
        )
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
            placement=placement,
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

    def display_layer_data(self) -> list[np.ndarray]:
        """Per-base data slices (views), in the same order as ``display_layers``.

        No contrast/percentile recompute -- this is the hot path called on
        every streaming update. The heavy ``np.percentile`` over the whole
        growing volume in ``display_layers`` runs only on the first full render.
        """
        if "Base" not in self.axis_labels:
            return []
        base_axis = self.axis_labels.index("Base")
        idx = [slice(None)] * self.data.ndim
        out = []
        for base_idx in range(self.data.shape[base_axis]):
            idx[base_axis] = base_idx
            out.append(self.data[tuple(idx)])
        return out
    

    supported_formats = ("tiff", "imagej")

    def _fold_for_disk(self):
        """(Dataset, Base, T, Z, Y, X) -> (T, Z, C, Y, X), C = Dataset x Base,
        the hyperstack layout every MoNaLISA file has had, with the step sizes
        and channel names that describe it."""
        dims = self.scan_params['dimensions']
        step_sizes = self.scan_params['step_sizes']
        vxsizec = float(step_sizes[dims.index(self.axis_label_map['r_l_text'])])
        vxsizer = float(step_sizes[dims.index(self.axis_label_map['u_d_text'])])
        vxsizez = float(step_sizes[dims.index(self.axis_label_map['b_f_text'])])
        data = np.asarray(self.data)
        numDatasets, numBases, numTimepoints, numSlices, numRows, numCols = data.shape
        folded = np.moveaxis(data, [0, 1, 2, 3, 4, 5], [2, 3, 0, 1, 4, 5])
        folded = np.ascontiguousarray(folded).reshape(
            numTimepoints, numSlices, numDatasets * numBases, numRows, numCols
        )
        channel_names = [
            f"ds{dataset}-{self._base_component_name(base)}"
            for dataset in range(numDatasets) for base in range(numBases)
        ]
        return folded, (vxsizec, vxsizer, vxsizez), channel_names

    def serialization_view(self):
        """The on-disk layout (see :meth:`_fold_for_disk`), in nanometres."""
        from imswitch.improcess.model.result import SerializationView

        folded, (vxsizec, vxsizer, vxsizez), channel_names = self._fold_for_disk()
        return SerializationView(
            data=folded,
            axis_labels=["T", "Z", "C", "Y", "X"],
            axis_scales=[1.0, vxsizez, 1.0, vxsizer, vxsizec],
            scale_unit="nm",
            channel_names=channel_names,
            extra={"scan_params": copy.deepcopy(self.scan_params)},
        )

    def write_files(self, plan, document) -> None:
        """OME-TIFF through the shared writer (the default), or the ImageJ
        hyperstack writer of old under ``fmt="imagej"`` for one release."""
        if plan.fmt == "tiff":
            from imswitch.improcess.model.result_io import save_image_result

            save_image_result(self, plan.primary, "tiff", document=document)
            return
        if plan.fmt != "imagej":
            raise ValueError(f"MoNaLISA result supports 'tiff' or 'imagej', got {plan.fmt!r}")
        folded, (vxsizec, vxsizer, _vxsizez), _names = self._fold_for_disk()
        ijmetadata = {'axes': 'TZCYX', 'provenance': document.to_json()}
        resolution = (10000.0 / vxsizec, 10000.0 / vxsizer)

        # `bigtiff` and `imagej` are mutually exclusive: asking for both makes
        # tifffile emit "writing nonconformant BigTIFF ImageJ" and produces a
        # file ImageJ may not open as a hyperstack. Write the ImageJ layout
        # while the data fits it, and only fall back to BigTIFF beyond that --
        # where the ImageJ metadata cannot be carried anyway.
        if folded.nbytes < IMAGEJ_MAX_BYTES:
            with tiff.TiffWriter(str(plan.primary), imagej=True) as tif:
                tif.write(
                    folded,
                    resolution=resolution,
                    metadata=ijmetadata,
                    photometric='minisblack',
                )
            return

        initLogger('MonalisaProcessingResult').warning(
            f'Reconstruction is {folded.nbytes / 1e9:.1f} GB, past the '
            f'ImageJ hyperstack limit; writing BigTIFF without ImageJ '
            f'metadata (axis order is still {ijmetadata["axes"]}).'
        )
        # Not ImageJ's description, but tifffile's own JSON one, which still
        # carries the axes and the provenance record.
        with tiff.TiffWriter(str(plan.primary), bigtiff=True) as tif:
            tif.write(
                folded,
                resolution=resolution,
                metadata=ijmetadata,
                photometric='minisblack',
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
