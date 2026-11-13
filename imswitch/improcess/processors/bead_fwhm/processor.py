"""Bead FWHM processor: find beads by threshold, fit 2D Gaussians, FWHM and SNR per plane."""

from __future__ import annotations

import math
from typing import Callable

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.analysis.bead_fwhm import THRESHOLD_MODES, BeadParams, analyse_planes
from imswitch.improcess.model.param_spec import ParamField
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors.base import Processor

from .result import BeadFwhmResult

_THRESHOLD_HELP = (
    "relative: in multiples of each plane's robust noise above its median "
    "(median + k x 1.4826 x MAD of the smoothed plane). absolute: in the "
    "image's own units, compared with the smoothed plane."
)
_WINDOW_HELP = (
    "Square fitted around each bead. Also the isolation distance: a bead with "
    "another one inside its window is not used. Make it about three times "
    "the expected FWHM."
)


class _BeadFwhmParamsWidget(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        defaults = BeadFwhmProcessor.default_params()
        form = QtWidgets.QFormLayout(self)

        self.threshold = QtWidgets.QDoubleSpinBox(self)
        self.threshold.setRange(0.0, 1e9)
        self.threshold.setDecimals(2)
        self.threshold.setValue(defaults["threshold"])
        self.threshold.setToolTip(_THRESHOLD_HELP)
        form.addRow("Threshold", self.threshold)

        self.threshold_mode = QtWidgets.QComboBox(self)
        self.threshold_mode.addItems(list(THRESHOLD_MODES))
        self.threshold_mode.setCurrentText(defaults["threshold_mode"])
        self.threshold_mode.setToolTip(_THRESHOLD_HELP)
        form.addRow("Threshold mode", self.threshold_mode)

        self.window = QtWidgets.QSpinBox(self)
        self.window.setRange(5, 101)
        self.window.setSingleStep(2)
        self.window.setSuffix(" px")
        self.window.setValue(defaults["window"])
        self.window.setToolTip(_WINDOW_HELP)
        form.addRow("Fit window", self.window)

        self.sigma = QtWidgets.QDoubleSpinBox(self)
        self.sigma.setRange(0.0, 10.0)
        self.sigma.setDecimals(2)
        self.sigma.setSingleStep(0.25)
        self.sigma.setSuffix(" px")
        self.sigma.setValue(defaults["sigma"])
        self.sigma.setToolTip("Gaussian smoothing before the threshold; the fit uses raw pixels.")
        form.addRow("Detection smoothing", self.sigma)

        self.min_r2 = QtWidgets.QDoubleSpinBox(self)
        self.min_r2.setRange(0.0, 1.0)
        self.min_r2.setDecimals(2)
        self.min_r2.setSingleStep(0.05)
        self.min_r2.setValue(defaults["min_r2"])
        self.min_r2.setToolTip("Fits that explain less of their window than this are not used.")
        form.addRow("Minimum fit R²", self.min_r2)

    def get_values(self) -> dict:
        return {
            "threshold": float(self.threshold.value()),
            "threshold_mode": self.threshold_mode.currentText(),
            "window": int(self.window.value()),
            "sigma": float(self.sigma.value()),
            "min_r2": float(self.min_r2.value()),
        }


class BeadFwhmProcessor(Processor):
    """Find beads by threshold, fit each with a 2D Gaussian, report FWHM and SNR.

    A stack is analysed plane by plane (every leading axis); the Graph panel
    then shows the mean FWHM and the mean SNR of each plane's beads.
    """

    name = "Bead FWHM"
    id = "bead-fwhm"
    category = "Measurement"
    output_kind = "table"

    @classmethod
    def default_params(cls) -> dict:
        defaults = BeadParams()
        return {
            "threshold": defaults.threshold,
            "threshold_mode": defaults.threshold_mode,
            "window": defaults.window,
            "sigma": defaults.sigma,
            "min_r2": defaults.min_r2,
        }

    @classmethod
    def param_spec(cls) -> tuple:
        return (
            ParamField("threshold", "float", 10.0, label="Threshold", min=0.0,
                       decimals=2, help=_THRESHOLD_HELP),
            ParamField("threshold_mode", "select", "relative", label="Threshold mode",
                       options=THRESHOLD_MODES, help=_THRESHOLD_HELP),
            ParamField("window", "int", 11, label="Fit window", min=5, max=101, step=2,
                       suffix="px", help=_WINDOW_HELP),
            ParamField("sigma", "float", 1.0, label="Detection smoothing", min=0.0, max=10.0,
                       decimals=2, step=0.25, suffix="px", advanced=True,
                       help="Gaussian smoothing before the threshold; the fit uses raw pixels."),
            ParamField("min_r2", "float", 0.8, label="Minimum fit R²", min=0.0, max=1.0,
                       decimals=2, step=0.05, advanced=True,
                       help="Fits that explain less of their window than this are not used."),
        )

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        """Any image of rank 2 or more; every leading axis is a plane."""
        return lambda result: len(np.shape(result.data)) >= 2

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        return _BeadFwhmParamsWidget(parent)

    def apply(self, result: ProcessingResult, params: dict) -> BeadFwhmResult:
        settings = BeadParams.from_params({**self.default_params(), **dict(params or {})})
        data = result.data
        shape = tuple(int(n) for n in np.shape(data))
        if len(shape) < 2:
            raise ValueError(f"Bead FWHM needs an image of at least 2 dimensions, got {shape}")
        leading = shape[:-2]
        n_planes = math.prod(leading) if leading else 1

        def planes():
            if not leading:
                yield np.asarray(data)
                return
            for index in np.ndindex(*leading):
                yield np.asarray(data[index])

        analysis = analyse_planes(planes(), n_planes, settings)
        labels = list(getattr(result, "axis_labels", []) or [])
        if len(labels) != len(shape):
            labels = [f"Axis{i}" for i in range(len(shape) - 2)] + ["Y", "X"]
        scales = list(getattr(result, "axis_scales", []) or [])
        if len(scales) != len(shape):
            scales = [1.0] * len(shape)
        return BeadFwhmResult(
            f"{result.name} (bead FWHM)",
            analysis,
            image=data,
            image_axis_labels=labels,
            image_axis_scales=scales,
            image_scale_unit=str(getattr(result, "scale_unit", "px") or "px"),
            params={**self.default_params(), **dict(params or {})},
        )


__all__ = ["BeadFwhmProcessor"]
