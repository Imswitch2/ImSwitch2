"""PSF / bead resolution processor."""

from typing import Callable

from qtpy import QtWidgets

from imswitch.improcess.analysis.psf_resolution import fit_psf_batch
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors._extraction import extract_2d_plane
from imswitch.improcess.processors.base import Processor

from .result import PSFResolutionResult


class PSFResolutionProcessor(Processor):
    """Fit a 2D Gaussian PSF to a 2D plane from an ImProcess result."""

    name = "PSF / Bead Resolution"
    id = "psf-resolution"
    category = "Measurement"

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        """Require at least a 2-D image; a single (Y, X) plane is fitted, with
        any extra non-spatial axes collapsed to index 0."""
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
        image = extract_2d_plane(result)
        analysis = fit_psf_batch(
            image,
            params.get("rois") or None,
            pixel_size=float(params.get("pixel_size", 1.0)),
            unit=str(params.get("unit", "px")),
        )
        return PSFResolutionResult(
            name=f"{result.name} (PSF resolution)",
            analysis=analysis,
            params=dict(params),
        )
