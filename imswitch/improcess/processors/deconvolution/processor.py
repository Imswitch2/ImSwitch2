"""2D Richardson-Lucy deconvolution with a theoretical PSF.

Each Y/X plane is deconvolved on its own; leading Z/T/C axes are looped over,
not blurred across. The PSF is never measured: it is the in-focus widefield
PSF of an aberration-free circular pupil, written down in Fourier space as the
incoherent OTF and computed for the data's own pixel grid.

The forward model is a convolution with that PSF under reflective boundary
conditions. Mirroring the plane once along each axis makes the periodic
extension the FFT assumes continuous, so the circular convolution of the
mirrored plane *is* the reflective convolution of the original: no padding
width has to be chosen, and the frame edges do not ring.
"""

from __future__ import annotations

from typing import Callable

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.contrast import finite_range
from imswitch.improcess.model.param_spec import ParamField
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors._axis_split import (
    axis_labels_for_result,
    axis_scales_for_result,
    shape_for_result,
)
from imswitch.improcess.processors.base import Processor

PSF_MODELS = ("airy", "gaussian")

#: Paraxial widefield PSF approximated by a Gaussian, sigma = 0.21 lambda / NA:
#: the least-squares fit of a Gaussian to the in-focus Airy pattern in
#: B. Zhang, J. Zerubia, J.-C. Olivo-Marin, "Gaussian approximations of
#: fluorescence microscope point-spread function models", Applied Optics 46,
#: 1819-1829 (2007), Table 1 (2D widefield, paraxial).
ZHANG_2007_WIDEFIELD_SIGMA_PER_LAMBDA_OVER_NA = 0.21

#: FWHM = 2 sqrt(2 ln 2) sigma for a Gaussian.
_FWHM_PER_SIGMA = 2.0 * np.sqrt(2.0 * np.log(2.0))

_NM_PER_UNIT = {
    "nm": 1.0,
    "nanometer": 1.0,
    "nanometers": 1.0,
    "um": 1e3,
    "micrometer": 1e3,
    "micrometers": 1e3,
    "micron": 1e3,
    "microns": 1e3,
    "mm": 1e6,
    "millimeter": 1e6,
    "millimeters": 1e6,
}


class RichardsonLucyProcessor(Processor):
    """Richardson-Lucy deconvolution of each Y/X plane with a theoretical PSF."""

    name = "Deconvolve (Richardson-Lucy)"
    id = "deconvolve-rl"
    category = "Restoration"
    # Output is pixel-for-pixel aligned with the input, so an ROI drawn
    # on one measures the same features on the other.
    preserves_grid = True
    # Cropping to a region deconvolves that region alone (with reflective
    # edges), which answers the same question about it as the full frame;
    # the cost scales with the frame, so crop is offered first.
    accepts_roi = True
    roi_modes = ('crop', 'mask')

    @classmethod
    def default_params(cls) -> dict:
        return {
            'iterations': 10,
            'psf_model': 'airy',
            'wavelength_nm': 520.0,
            'na': 1.4,
            'fwhm_nm': 0.0,
            'pixel_size_nm': 0.0,
        }

    @classmethod
    def param_spec(cls) -> tuple:
        return (
            ParamField(
                'iterations', 'int', 10, label='Iterations',
                help='Richardson-Lucy steps. More steps sharpen further and amplify noise.',
                min=1, max=10000,
            ),
            ParamField(
                'psf_model', 'select', 'airy', label='PSF model',
                help=(
                    'airy: exact in-focus widefield PSF of a circular pupil. '
                    'gaussian: Gaussian of the FWHM below, or the Zhang 2007 '
                    'fit to the Airy pattern (sigma = 0.21 lambda/NA) when the FWHM is 0.'
                ),
                options=PSF_MODELS,
            ),
            ParamField(
                'wavelength_nm', 'float', 520.0, label='Emission wavelength (nm)',
                help='Emission (detected) wavelength of the dye.',
                min=100.0, max=5000.0, decimals=1,
            ),
            ParamField(
                'na', 'float', 1.4, label='Numerical aperture',
                help='Detection objective NA.', min=0.01, max=2.0, decimals=3,
            ),
            ParamField(
                'fwhm_nm', 'float', 0.0, label='Gaussian FWHM (nm)',
                help=(
                    'Gaussian model only: a measured PSF FWHM (e.g. from PSF '
                    'resolution). 0 derives it from wavelength and NA.'
                ),
                min=0.0, max=1e6, decimals=1,
            ),
            ParamField(
                'pixel_size_nm', 'float', 0.0, label='Pixel size (nm)',
                help="0 uses the result's calibrated Y/X pixel size.",
                min=0.0, max=1e6, decimals=2,
            ),
        )

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: len(shape_for_result(result)) >= 2

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)
        fields = {field.key: field for field in self.param_spec()}

        def double_spin(key: str) -> QtWidgets.QDoubleSpinBox:
            field = fields[key]
            spin = QtWidgets.QDoubleSpinBox()
            spin.setDecimals(field.decimals)
            spin.setRange(field.min, field.max)
            spin.setValue(field.default)
            spin.setToolTip(field.help)
            layout.addRow(f"{field.label}:", spin)
            return spin

        iterations_spin = QtWidgets.QSpinBox()
        iterations_spin.setRange(fields['iterations'].min, fields['iterations'].max)
        iterations_spin.setValue(fields['iterations'].default)
        iterations_spin.setToolTip(fields['iterations'].help)
        layout.addRow(f"{fields['iterations'].label}:", iterations_spin)

        model_combo = QtWidgets.QComboBox()
        model_combo.addItems(list(PSF_MODELS))
        model_combo.setCurrentText(fields['psf_model'].default)
        model_combo.setToolTip(fields['psf_model'].help)
        layout.addRow(f"{fields['psf_model'].label}:", model_combo)

        wavelength_spin = double_spin('wavelength_nm')
        na_spin = double_spin('na')
        fwhm_spin = double_spin('fwhm_nm')
        fwhm_spin.setSpecialValueText("from wavelength / NA")
        pixel_spin = double_spin('pixel_size_nm')
        pixel_spin.setSpecialValueText("from data")

        def sync_enabled():
            gaussian = model_combo.currentText() == "gaussian"
            fwhm_spin.setEnabled(gaussian)
            measured = gaussian and fwhm_spin.value() > 0
            wavelength_spin.setEnabled(not measured)
            na_spin.setEnabled(not measured)

        model_combo.currentIndexChanged.connect(lambda _index: sync_enabled())
        fwhm_spin.valueChanged.connect(lambda _value: sync_enabled())
        sync_enabled()

        def get_values():
            return {
                'iterations': int(iterations_spin.value()),
                'psf_model': model_combo.currentText(),
                'wavelength_nm': float(wavelength_spin.value()),
                'na': float(na_spin.value()),
                'fwhm_nm': float(fwhm_spin.value()),
                'pixel_size_nm': float(pixel_spin.value()),
            }

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        iterations = int(params.get('iterations', 10))
        model = str(params.get('psf_model', 'airy'))
        wavelength_nm = float(params.get('wavelength_nm', 520.0))
        na = float(params.get('na', 1.4))
        fwhm_nm = float(params.get('fwhm_nm', 0.0))
        pixel_size_nm = float(params.get('pixel_size_nm', 0.0))
        if iterations < 1:
            raise ValueError(f"Richardson-Lucy needs at least 1 iteration, got {iterations}")

        labels = axis_labels_for_result(result)
        y_axis, x_axis = _spatial_axes(labels)
        pixel_nm = _pixel_size_nm(result, labels, (y_axis, x_axis), pixel_size_nm)

        data = np.asarray(result.data, dtype=np.float32)
        if data.ndim < 2:
            raise ValueError("Deconvolution needs at least a 2D image")
        negative = int(np.count_nonzero(data < 0))
        planes = np.moveaxis(data, (y_axis, x_axis), (-2, -1))
        stack = planes.reshape((-1,) + planes.shape[-2:])

        otf = theoretical_otf(
            _mirrored_shape(stack.shape[-2:]), pixel_nm, model=model,
            wavelength_nm=wavelength_nm, na=na, fwhm_nm=fwhm_nm,
        )
        out = np.empty_like(stack)
        for index, plane in enumerate(stack):
            out[index] = richardson_lucy_2d(plane, otf, iterations)
        out = np.moveaxis(out.reshape(planes.shape), (-2, -1), (y_axis, x_axis))

        return ArrayProcessingResult(
            name=f"{result.name} (RL {iterations})",
            data=out,
            axis_labels=list(labels),
            display_levels=finite_range(out),
            axis_scales=list(axis_scales_for_result(result)),
            scale_unit=getattr(result, "scale_unit", "px"),
            metadata={
                "operation": self.id,
                "iterations": iterations,
                "psf_model": model,
                "wavelength_nm": wavelength_nm,
                "na": na,
                "fwhm_nm": fwhm_nm if model == "gaussian" and fwhm_nm > 0 else None,
                "pixel_size_nm": list(pixel_nm),
                "boundary": "reflective",
                "negative_pixels_clipped": negative,
            },
        )


def _spatial_axes(labels) -> tuple[int, int]:
    """Return the (Y, X) axis indices; fall back to the last two axes."""
    labels = list(labels)
    try:
        return labels.index("Y"), labels.index("X")
    except ValueError:
        return len(labels) - 2, len(labels) - 1


def _pixel_size_nm(result, labels, spatial_axes, override_nm: float) -> tuple[float, float]:
    """``(dy, dx)`` in nm: the override when set, else the calibrated scales.

    An uncalibrated (``px``) result has no physical pixel size, and the PSF
    cannot be drawn without one, so that is an error naming the fix rather
    than a guess.
    """
    if override_nm > 0:
        return float(override_nm), float(override_nm)
    unit = str(getattr(result, "scale_unit", "px") or "px").strip().lower()
    unit = unit.replace("µ", "u").replace("μ", "u")
    nm_per_unit = _NM_PER_UNIT.get(unit)
    if nm_per_unit is None:
        raise ValueError(
            f"'{result.name}' has no physical pixel size (unit {unit!r}); "
            f"set Pixel size (nm) to deconvolve it"
        )
    scales = axis_scales_for_result(result)
    dy, dx = (float(scales[axis]) * nm_per_unit for axis in spatial_axes)
    if not (dy > 0 and dx > 0):
        raise ValueError(
            f"'{result.name}' has a non-positive Y/X pixel size ({dy}, {dx} nm); "
            f"set Pixel size (nm) to deconvolve it"
        )
    return dy, dx


def _mirrored_shape(shape) -> tuple[int, int]:
    return 2 * int(shape[0]), 2 * int(shape[1])


def theoretical_otf(
    shape,
    pixel_nm,
    *,
    model: str = "airy",
    wavelength_nm: float = 520.0,
    na: float = 1.4,
    fwhm_nm: float = 0.0,
) -> np.ndarray:
    """Half-plane (``rfft2``) OTF of a theoretical 2D PSF on a ``shape`` grid.

    ``airy`` is the incoherent OTF of an aberration-free circular pupil,
    ``2/pi (acos s - s sqrt(1 - s^2))`` with ``s = f / (2 NA / lambda)``.
    ``gaussian`` is a Gaussian of ``fwhm_nm``, or of the Zhang 2007 sigma
    when ``fwhm_nm`` is 0. Both theoretical models are integrated over the
    pixel area (a sinc per axis), since a camera pixel collects light over its
    whole face; a measured FWHM already includes that and is used as is.

    The PSF is then made non-negative and unit-sum in real space: a pupil
    cut-off above the sampling Nyquist frequency is truncated by the grid, and
    the truncated kernel may dip below zero, which Richardson-Lucy, a
    multiplicative update, cannot represent.
    """
    from scipy import fft

    ny, nx = int(shape[0]), int(shape[1])
    dy, dx = float(pixel_nm[0]), float(pixel_nm[1])
    fy = fft.fftfreq(ny, d=dy)[:, None]
    fx = fft.rfftfreq(nx, d=dx)[None, :]
    f = np.sqrt(fy * fy + fx * fx)
    pixel_integration = np.sinc(fy * dy) * np.sinc(fx * dx)

    if model == "airy":
        if not (wavelength_nm > 0 and na > 0):
            raise ValueError("The Airy PSF needs a positive wavelength and NA")
        s = np.clip(f / (2.0 * na / wavelength_nm), 0.0, 1.0)
        otf = (2.0 / np.pi) * (np.arccos(s) - s * np.sqrt(1.0 - s * s))
        otf = otf * pixel_integration
    elif model == "gaussian":
        if fwhm_nm > 0:
            sigma = fwhm_nm / _FWHM_PER_SIGMA
            integrate = 1.0
        else:
            if not (wavelength_nm > 0 and na > 0):
                raise ValueError("The Gaussian PSF needs an FWHM, or a wavelength and NA")
            sigma = ZHANG_2007_WIDEFIELD_SIGMA_PER_LAMBDA_OVER_NA * wavelength_nm / na
            integrate = pixel_integration
        otf = np.exp(-2.0 * (np.pi * sigma * f) ** 2) * integrate
    else:
        raise ValueError(f"Unknown PSF model {model!r}; expected one of {list(PSF_MODELS)}")

    psf = fft.irfft2(otf, s=(ny, nx))
    psf = np.clip(psf, 0.0, None)
    total = psf.sum()
    if not total > 0:
        raise ValueError("The theoretical PSF vanished on this pixel grid")
    return fft.rfft2(psf / total).astype(np.complex64)


def richardson_lucy_2d(plane: np.ndarray, otf: np.ndarray, iterations: int) -> np.ndarray:
    """Deconvolve one Y/X plane by ``iterations`` Richardson-Lucy steps.

    ``otf`` is a :func:`theoretical_otf` for twice the plane's shape: the
    plane is mirrored into that grid (reflective boundaries) and cut back out
    at the end. The estimate starts flat at the data mean, so no pixel is
    pinned to zero by a zero in the data, and it keeps the data's total
    intensity because the OTF is 1 at zero frequency. Negative data (an
    unsubtracted offset going below zero) has no Poisson meaning and is
    clipped to zero.
    """
    from scipy import fft

    from imswitch.imcommon.model import checkpoint

    h, w = plane.shape
    observed = np.clip(np.asarray(plane, dtype=np.float32), 0.0, None)
    observed = np.concatenate([observed, observed[::-1]], axis=0)
    observed = np.concatenate([observed, observed[:, ::-1]], axis=1)
    shape = observed.shape
    if otf.shape != (shape[0], shape[1] // 2 + 1):
        raise ValueError(f"OTF shape {otf.shape} does not fit a mirrored plane {shape}")
    otf_adjoint = np.conj(otf)

    estimate = np.full(shape, observed.mean(), dtype=np.float32)
    if not estimate.flat[0] > 0:
        return np.zeros((h, w), dtype=np.float32)
    ratio = np.empty_like(observed)
    for _ in range(int(iterations)):
        checkpoint()
        blurred = fft.irfft2(fft.rfft2(estimate, workers=-1) * otf, s=shape, workers=-1)
        ratio.fill(0.0)
        np.divide(observed, blurred, out=ratio, where=blurred > 0)
        correction = fft.irfft2(fft.rfft2(ratio, workers=-1) * otf_adjoint, s=shape, workers=-1)
        estimate *= np.clip(correction, 0.0, None).astype(np.float32, copy=False)
    return np.ascontiguousarray(estimate[:h, :w])


__all__ = [
    "PSF_MODELS",
    "RichardsonLucyProcessor",
    "ZHANG_2007_WIDEFIELD_SIGMA_PER_LAMBDA_OVER_NA",
    "richardson_lucy_2d",
    "theoretical_otf",
]
