"""Render a LocalizationResult into a super-resolved image/volume."""

from __future__ import annotations

from typing import Callable

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.analysis.smlm_render import render_xy, render_xyz
from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.localization_result import LocalizationResult
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors.base import Processor


class SmlmRenderProcessor(Processor):
    """Turn a localization table into an image the napari viewer can show."""

    name = "SMLM render"
    id = "smlm-render"
    category = "Localization"
    kinds = ("localization",)

    @classmethod
    def default_params(cls) -> dict:
        return {   'render_type': 'histogram',
        'pixel_size_nm': 10.0,
        'fwhm_nm': 20.0,
        'render_3d': False,
        'z_pixel_size_nm': 20.0}

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: isinstance(result, LocalizationResult)

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        render_combo = QtWidgets.QComboBox()
        render_combo.addItems(["histogram", "fixed_gaussian"])
        layout.addRow("Render:", render_combo)

        pixel_spin = QtWidgets.QDoubleSpinBox()
        pixel_spin.setRange(0.1, 1000.0)
        pixel_spin.setValue(10.0)
        pixel_spin.setSuffix(" nm/px")
        layout.addRow("Output pixel:", pixel_spin)

        fwhm_spin = QtWidgets.QDoubleSpinBox()
        fwhm_spin.setRange(0.1, 10000.0)
        fwhm_spin.setValue(20.0)
        fwhm_spin.setSuffix(" nm")
        layout.addRow("Gaussian FWHM:", fwhm_spin)

        three_d_check = QtWidgets.QCheckBox("Render 3D volume")
        layout.addRow(three_d_check)

        z_pixel_spin = QtWidgets.QDoubleSpinBox()
        z_pixel_spin.setRange(0.1, 5000.0)
        z_pixel_spin.setValue(20.0)
        z_pixel_spin.setSuffix(" nm/px")
        layout.addRow("Z pixel:", z_pixel_spin)

        def get_values():
            return {
                "render_type": render_combo.currentText(),
                "pixel_size_nm": float(pixel_spin.value()),
                "fwhm_nm": float(fwhm_spin.value()),
                "render_3d": bool(three_d_check.isChecked()),
                "z_pixel_size_nm": float(z_pixel_spin.value()),
            }

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        if not isinstance(result, LocalizationResult):
            raise TypeError("SMLM render requires a LocalizationResult input")

        locs = result.locs
        render_type = str(params.get("render_type", "histogram"))
        pixel_size_nm = float(params.get("pixel_size_nm", 10.0))
        fwhm_nm = float(params.get("fwhm_nm", 20.0))
        render_3d = bool(params.get("render_3d", False)) and result.dims == "3D"

        if render_3d:
            z_pixel_size_nm = float(params.get("z_pixel_size_nm", pixel_size_nm))
            image, grid = render_xyz(
                locs.x_nm,
                locs.y_nm,
                locs.z_nm,
                pixel_size_nm=pixel_size_nm,
                z_pixel_size_nm=z_pixel_size_nm,
                render_type=render_type,
                fwhm_nm=fwhm_nm,
                fwhm_z_nm=params.get("fwhm_z_nm"),
            )
            axis_labels = ["Z", "Y", "X"]
        else:
            image, grid = render_xy(
                locs.x_nm,
                locs.y_nm,
                pixel_size_nm=pixel_size_nm,
                render_type=render_type,
                fwhm_nm=fwhm_nm,
            )
            axis_labels = ["Y", "X"]

        display_levels = None
        if image.size and np.any(image > 0):
            display_levels = (0.0, float(np.percentile(image[image > 0], 99.5)))

        name = f"{result.name} ({render_type} {pixel_size_nm:g}nm)"
        return ArrayProcessingResult(
            name=name,
            data=image,
            axis_labels=axis_labels,
            axis_scales=list(grid.pixel_size_nm),
            scale_unit="nm",
            display_levels=display_levels,
            metadata={
                "source_result": result.name,
                "render_type": render_type,
                "pixel_size_nm": pixel_size_nm,
                "fwhm_nm": fwhm_nm,
                "origin_nm": list(grid.origin_nm),
                "localizations": result.count,
            },
        )


__all__ = ["SmlmRenderProcessor"]
