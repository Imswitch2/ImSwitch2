"""Isolated GPU ISM reassignment plugin for ImProcess.

Pattern localization deliberately reuses the existing, well-tested ImProcess
MoNaLISA ``PatternFinder``.  Reconstruction uses the ISM path extracted from
monalisa-xrecon and ported to CuPy; it does not call or depend on the separate
experimental ISM mode currently present in the MoNaLISA reconstructor.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.reconstructors.base import Reconstructor
from imswitch.improcess.reconstructors.monalisa.pattern_finder import PatternFinder

from .kernel import patternfinder_to_xrecon, reconstruct_ism_gpu
from .params_widget import IsmReassignParamsWidget

if TYPE_CHECKING:
    from imswitch.improcess.model import DataObj


class IsmReassignReconstructor(Reconstructor):
    """GPU implementation of the monalisa-xrecon ISM reassignment path."""

    name = "ISM reassignment (GPU)"
    id = "ism-reassign"
    file_extensions = ["hdf5", "tiff", "tif", "zarr"]
    description = "PatternFinder localization + GPU Fourier ISM pixel reassignment"
    default_save_subdir = "ism-reassign"
    execution_policy = "worker"

    def __init__(self):
        self._pattern_finder = PatternFinder()

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        return IsmReassignParamsWidget(parent)

    def make_metadata_dialog(self, parent: QtWidgets.QWidget) -> QtWidgets.QDialog | None:
        return None

    def find_pattern(self, data_obj: "DataObj", param_widget: QtWidgets.QWidget):
        """Run ImProcess PatternFinder on the complete raw acquisition stack."""
        if not isinstance(param_widget, IsmReassignParamsWidget):
            raise TypeError(
                f"Expected IsmReassignParamsWidget, got {type(param_widget).__name__}"
            )

        preloaded = data_obj.dataLoaded
        try:
            data_obj.checkAndLoadData()
            if data_obj.data is None:
                raise ValueError("Could not load source data for pattern localization")
            raw = np.asarray(data_obj.data)
            if raw.ndim not in (2, 3):
                raise ValueError(
                    "PatternFinder expects a 2-D image or 3-D frame stack; "
                    f"got {raw.shape}"
                )

            current = param_widget.get_values()
            pattern, lattice = self._pattern_finder.findPatternOrLattice(
                raw,
                xp_guess=current.get("col_period"),
                yp_guess=current.get("row_period"),
            )
        finally:
            if not preloaded:
                data_obj.checkAndUnloadData()
        if pattern is None:
            raise ValueError(
                "ISM reassignment currently requires an axis-aligned rectangular "
                f"pattern; PatternFinder detected {lattice.describe()}"
            )

        param_widget.set_pattern_params(*pattern)
        param_widget.set_show_pattern(True)
        return pattern

    def process(
        self,
        data_obj: "DataObj",
        params: dict,
        context=None,
    ) -> ArrayProcessingResult:
        if context is not None:
            context.report("inspect", 1, 1, "Loading ISM frame stack")
            context.check_cancelled()

        preloaded = data_obj.dataLoaded
        try:
            data_obj.checkAndLoadData()
            if data_obj.data is None:
                raise ValueError("Could not load ISM source data")
            raw = np.asarray(data_obj.data)
        finally:
            if not preloaded:
                data_obj.checkAndUnloadData()

        if raw.ndim != 3:
            raise ValueError(
                "ISM reassignment currently supports one 3-D raw acquisition "
                f"(M*M, Y, X); got {raw.shape}"
            )

        row_period = float(params["row_period"])
        col_period = float(params["col_period"])
        pattern = (
            np.mod(float(params["row_offset"]), row_period),
            np.mod(float(params["col_offset"]), col_period),
            row_period,
            col_period,
        )
        period, phase = patternfinder_to_xrecon(pattern)

        if context is not None:
            context.report("align", 1, 1, "Preparing GPU ISM geometry")
            context.check_cancelled()
            context.report("allocate", 1, 1, "Uploading and allocating CUDA buffers")

        started = time.perf_counter()
        image, geometry = reconstruct_ism_gpu(
            raw,
            pattern_period=period,
            pattern_phase=phase,
            scanning_orientation=params.get("scanning_orientation", "X+Y-"),
            psf_fwhm_nm=float(params.get("psf_fwhm_nm", 200.0)),
            pixel_size_nm=float(params.get("pixel_size_nm", 100.0)),
            oversampling=float(params.get("oversampling", 2.0)),
            ism_shift=float(params.get("ism_shift", 0.5)),
            remove_mean_of_patch=bool(params.get("remove_mean_of_patch", True)),
            frame_batch_size=params.get("frame_batch_size"),
            check_cancelled=(context.check_cancelled if context is not None else None),
        )
        elapsed = time.perf_counter() - started

        if context is not None:
            context.report("assemble", 1, 1, "GPU ISM reconstruction complete")
            context.check_cancelled()

        name = getattr(data_obj, "name", None) or "ISM"
        result = ArrayProcessingResult(
            name=f"{name} (ISM GPU)",
            data=np.asarray(image, dtype=np.float32),
            axis_labels=["Y", "X"],
            axis_scales=[
                float(geometry.output_pixel_size_nm),
                float(geometry.output_pixel_size_nm),
            ],
            scale_unit="nm",
            metadata={
                "reconstructor": self.id,
                "patternfinder_pattern": tuple(float(v) for v in pattern),
                "xrecon_period_xy": tuple(float(v) for v in period),
                "xrecon_phase_xy": tuple(float(v) for v in phase),
                "scanning_orientation": params.get("scanning_orientation", "X+Y-"),
                "psf_fwhm_nm": float(params.get("psf_fwhm_nm", 200.0)),
                "camera_pixel_size_nm": float(params.get("pixel_size_nm", 100.0)),
                "oversampling": float(params.get("oversampling", 2.0)),
                "ism_shift": float(params.get("ism_shift", 0.5)),
                "output_pixel_size_nm": float(geometry.output_pixel_size_nm),
                "gpu_reconstruction_seconds": float(elapsed),
            },
        )
        if context is not None:
            context.report("finalize", 1, 1, f"ISM GPU done in {elapsed:.3f} s")
        return result


__all__ = ["IsmReassignReconstructor"]
