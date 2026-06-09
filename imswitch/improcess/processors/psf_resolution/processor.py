"""PSF / bead resolution processor."""

from typing import Callable

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.analysis.psf_resolution import fit_psf_batch
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors.base import Processor

from .result import PSFResolutionResult


class PSFResolutionProcessor(Processor):
    """Fit a 2D Gaussian PSF to a 2D plane from an ImProcess result."""

    name = "PSF / Bead Resolution"
    id = "psf-resolution"

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: result.data.ndim >= 2

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        pixel_size_spin = QtWidgets.QDoubleSpinBox()
        pixel_size_spin.setDecimals(6)
        pixel_size_spin.setRange(1e-9, 1e12)
        pixel_size_spin.setValue(1.0)
        layout.addRow("Pixel size:", pixel_size_spin)

        unit_combo = QtWidgets.QComboBox()
        unit_combo.addItems(["px", "nm", "um"])
        unit_combo.setCurrentText("px")
        layout.addRow("Resolution unit:", unit_combo)

        def get_values():
            return {
                "pixel_size": pixel_size_spin.value(),
                "unit": unit_combo.currentText(),
            }

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        image = self._extract_2d(result)
        analysis = fit_psf_batch(
            image,
            pixel_size=float(params.get("pixel_size", 1.0)),
            unit=str(params.get("unit", "px")),
        )
        return PSFResolutionResult(
            name=f"{result.name} (PSF resolution)",
            analysis=analysis,
            params=dict(params),
        )

    @staticmethod
    def _extract_2d(result: ProcessingResult) -> np.ndarray:
        data = np.asarray(result.data)
        if data.ndim == 2:
            return data
        if data.ndim < 2:
            raise ValueError(f"PSF fitting needs at least 2D data, got shape {data.shape}")
        indexer = []
        for axis in range(data.ndim):
            indexer.append(slice(None) if axis >= data.ndim - 2 else 0)
        image = np.asarray(data[tuple(indexer)])
        if image.ndim != 2:
            raise ValueError(f"Could not extract a 2D PSF image from shape {data.shape}")
        return image
