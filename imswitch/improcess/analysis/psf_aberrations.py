"""Zernike aberration estimation from a 3-D bead stack.

Model-based phase retrieval: a forward PSF model with a Zernike pupil phase
is least-squares fitted to the averaged, high-SNR bead from
:func:`imswitch.improcess.analysis.bead_psf.average_psf`::

    data(z, y, x) ~ A * PSF(z - z0, y - y0, x - x0 | c_5 .. c_11) (*) bead + b

* A through-focus stack is required: an in-focus image alone cannot tell the
  sign of the even modes (astigmatism, spherical).
* Piston (Z1) and tilt (Z2, Z3) are not fitted; tilt is the lateral position.
  Defocus (Z4) is replaced by the exact high-NA propagation defocus via z0.
* Default modes are Noll 5-11: astigmatism (5, 6), coma (7, 8), trefoil
  (9, 10) and primary spherical (11). Coefficients are nm RMS of a standard,
  Noll-normalised basis, so the total RMS wavefront error is
  ``sqrt(sum c_j^2)``.

Conventions. z grows with the array index (``z_flip`` reverses it); angles
are in image coordinates (x = column, y = row). Signs and angles depend on
both, magnitudes do not. Note that the SLM pattern designer in imcontrol
(``aberrationPatterns.ZernikeGenerator``) defines modes 2/3, 7/8 and 11
differently from the standard basis used here, so coefficients measured
here cannot be applied there one to one for those modes.

The forward model is pluggable: anything implementing :class:`PSFBackend`
works, e.g. a vectorial model from an external PSF simulation package. The
built-in :class:`ScalarPSF` is a scalar Fourier-optics model, which is an
approximation at high NA.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Protocol, Sequence

import numpy as np
from scipy import ndimage, optimize

from .bead_psf import BeadAnalysis, average_psf, bead_sigma_nm, expected_fwhm_nm

DEFAULT_MODES = (5, 6, 7, 8, 9, 10, 11)
MODE_NAMES = {
    4: "defocus",
    5: "astigmatism (oblique)",
    6: "astigmatism (vertical)",
    7: "coma (vertical)",
    8: "coma (horizontal)",
    9: "trefoil (vertical)",
    10: "trefoil (oblique)",
    11: "spherical (primary)",
    12: "secondary astigmatism (vertical)",
    13: "secondary astigmatism (oblique)",
    22: "spherical (secondary)",
}


# --------------------------------------------------------------------------- #
# Zernike polynomials (Noll)
# --------------------------------------------------------------------------- #
def noll_to_nm(j: int) -> tuple[int, int]:
    """Noll index to ``(n, m)``; ``m > 0`` is the cosine, ``m < 0`` the sine term."""
    if j < 1:
        raise ValueError("Noll indices start at 1")
    n, j1 = 0, j - 1
    while j1 > n:
        n += 1
        j1 -= n
    m = (-1) ** j * ((n % 2) + 2 * int((j1 + ((n + 1) % 2)) / 2))
    return n, m


def zernike(j: int, rho: np.ndarray, theta: np.ndarray) -> np.ndarray:
    """Noll-normalised Zernike polynomial (unit RMS over the unit disk)."""
    n, m = noll_to_nm(j)
    am = abs(m)
    radial = np.zeros_like(rho, dtype=np.float64)
    for k in range((n - am) // 2 + 1):
        coefficient = (-1) ** k * math.factorial(n - k) / (
            math.factorial(k)
            * math.factorial((n + am) // 2 - k)
            * math.factorial((n - am) // 2 - k)
        )
        radial += coefficient * rho ** (n - 2 * k)
    if m == 0:
        return math.sqrt(n + 1) * radial
    angular = np.cos(am * theta) if m > 0 else np.sin(am * theta)
    return math.sqrt(2 * (n + 1)) * radial * angular


# --------------------------------------------------------------------------- #
# Forward models
# --------------------------------------------------------------------------- #
class PSFBackend(Protocol):
    """A forward PSF model.

    Called with the output shape ``(z, y, x)``, the pixel size ``(z, y, x)`` in
    nm, ``{noll_index: nm RMS}`` and the emitter position relative to the
    grid centre in nm; returns the intensity PSF on that grid.
    """

    def __call__(
        self,
        shape_zyx: tuple[int, int, int],
        pixel_size_nm: tuple[float, float, float],
        coeffs_nm: dict[int, float],
        shift_zyx_nm: Sequence[float],
    ) -> np.ndarray: ...


@dataclass
class ScalarPSF:
    """Scalar Fourier-optics PSF with exact angular-spectrum defocus.

    ``apodization`` is ``"aplanatic"`` (1/sqrt(cos theta), emission side) or
    ``"none"``. Use ``oversample > 1`` when the camera pixel approaches the
    Nyquist limit lambda/(4 NA); ``pad`` avoids wrap-around of defocused planes.
    """

    na: float
    wavelength_nm: float
    n_immersion: float = 1.515
    apodization: str = "aplanatic"
    oversample: int = 1
    pad: int = 2
    _grid_key: tuple | None = field(default=None, init=False, repr=False)
    _grid: dict | None = field(default=None, init=False, repr=False)

    def _pupil_grid(self, ny: int, nx: int, py: float, px: float) -> dict:
        os_ = self.oversample
        size_y, size_x = ny * self.pad * os_, nx * self.pad * os_
        ky = np.fft.fftfreq(size_y, d=py / os_)
        kx = np.fft.fftfreq(size_x, d=px / os_)
        KY, KX = np.meshgrid(ky, kx, indexing="ij")
        kr = np.hypot(KX, KY)
        kmax = self.na / self.wavelength_nm
        inside = kr <= kmax
        k_medium = self.n_immersion / self.wavelength_nm
        kz = np.sqrt(np.clip(k_medium**2 - kr**2, 0, None))
        amplitude = inside.astype(np.float64)
        if self.apodization == "aplanatic":
            cos_theta = np.where(inside, kz / k_medium, 1.0)
            amplitude = amplitude / np.sqrt(np.clip(cos_theta, 1e-3, None))
        elif self.apodization != "none":
            raise ValueError(f"unknown apodization {self.apodization!r}")
        return {
            "KY": KY, "KX": KX, "rho": np.where(inside, kr / kmax, 0.0),
            "theta": np.arctan2(KY, KX), "kz": kz, "amplitude": amplitude, "inside": inside,
            "size": (size_y, size_x), "zernike": {},
        }

    def __call__(self, shape_zyx, pixel_size_nm, coeffs_nm, shift_zyx_nm) -> np.ndarray:
        nz, ny, nx = shape_zyx
        pz, py, px = pixel_size_nm
        key = (ny, nx, py, px)
        if self._grid_key != key:
            self._grid, self._grid_key = self._pupil_grid(ny, nx, py, px), key
        grid = self._grid
        phase = np.zeros_like(grid["rho"])
        for j, c in coeffs_nm.items():
            if c == 0:
                continue
            if j not in grid["zernike"]:
                grid["zernike"][j] = np.where(grid["inside"], zernike(j, grid["rho"], grid["theta"]), 0.0)
            phase += (2 * np.pi * c / self.wavelength_nm) * grid["zernike"][j]
        z0, y0, x0 = shift_zyx_nm
        phase -= 2 * np.pi * (grid["KY"] * y0 + grid["KX"] * x0)
        pupil = grid["amplitude"] * np.exp(1j * phase)

        z = (np.arange(nz) - (nz - 1) / 2) * pz - z0
        propagator = np.exp(2j * np.pi * grid["kz"][None] * z[:, None, None])
        field_ = np.fft.ifft2(pupil[None] * propagator)
        intensity = np.abs(np.fft.fftshift(field_, axes=(1, 2))) ** 2

        os_ = self.oversample
        cy, cx = grid["size"][0] // 2, grid["size"][1] // 2
        hy, hx = ny * os_ // 2, nx * os_ // 2
        intensity = intensity[:, cy - hy:cy - hy + ny * os_, cx - hx:cx - hx + nx * os_]
        if os_ > 1:
            intensity = intensity.reshape(nz, ny, os_, nx, os_).mean(axis=(2, 4))
        return intensity


# --------------------------------------------------------------------------- #
# Fit
# --------------------------------------------------------------------------- #
@dataclass
class AberrationFit:
    """Fitted Zernike coefficients (nm RMS) and fit diagnostics."""

    coeffs_nm: dict[int, float]
    coeffs_err_nm: dict[int, float]
    rms_nm: float
    strehl: float
    pairs: dict[str, dict[str, float]]
    shift_zyx_nm: tuple[float, float, float]
    r2: float
    model: np.ndarray
    data: np.ndarray
    wavelength_nm: float
    n_beads: int = 1
    warnings: list[str] = field(default_factory=list)

    def rows(self) -> list[dict[str, object]]:
        """One row per fitted mode."""
        return [
            {
                "noll": j,
                "mode": MODE_NAMES.get(j, f"Z{j}"),
                "coefficient_nm_rms": c,
                "error_nm_rms": self.coeffs_err_nm[j],
                "milliwaves": 1000.0 * c / self.wavelength_nm,
            }
            for j, c in self.coeffs_nm.items()
        ]


def _pairs(c: dict[int, float]) -> dict[str, dict[str, float]]:
    out = {}
    for name, (j_sin, j_cos, fold) in {
        "astigmatism": (5, 6, 2), "coma": (7, 8, 1), "trefoil": (9, 10, 3),
    }.items():
        if j_sin in c and j_cos in c:
            out[name] = {
                "magnitude_nm_rms": math.hypot(c[j_sin], c[j_cos]),
                "angle_deg": math.degrees(math.atan2(c[j_sin], c[j_cos]) / fold),
            }
    return out


def fit_aberrations(
    data: np.ndarray,
    pixel_size_nm: Sequence[float],
    na: float,
    wavelength_nm: float,
    n_immersion: float = 1.515,
    modes: Sequence[int] = DEFAULT_MODES,
    bead_diameter_nm: float | None = None,
    bead_labeling: str = "volume",
    backend: PSFBackend | None = None,
    z_flip: bool = False,
    n_starts: int = 3,
) -> AberrationFit:
    """Fit Zernike coefficients to a roughly centred 3-D bead stack ``(z, y, x)``.

    The fit runs in two stages (position, amplitude, astigmatism and spherical
    first, then every mode) from ``n_starts`` spherical starting values, and
    keeps the best: phase retrieval has local minima.
    """
    data = np.asarray(data, dtype=np.float64)
    if data.ndim != 3:
        raise ValueError("aberration fitting needs a 3-D (Z, Y, X) stack")
    if z_flip:
        data = data[::-1]
    px = tuple(float(v) for v in pixel_size_nm)
    backend = backend or ScalarPSF(na, wavelength_nm, n_immersion)
    modes = tuple(int(j) for j in modes)
    if any(j <= 4 for j in modes):
        raise ValueError("piston, tilt and defocus (Noll 1-4) are position parameters, not fitted modes")

    bead_sigma_px = None
    if bead_diameter_nm:
        s = bead_sigma_nm(bead_diameter_nm, bead_labeling)
        bead_sigma_px = tuple(s / p for p in px)

    reference_max = float(backend(data.shape, px, {}, (0.0, 0.0, 0.0)).max())
    baseline = float(np.median(data))
    scale = float(data.max() - baseline) or 1.0
    normalized = (data - baseline) / scale

    def model(p: np.ndarray) -> np.ndarray:
        amplitude, background, z0, y0, x0 = p[:5]
        psf = backend(data.shape, px, dict(zip(modes, p[5:])), (z0, y0, x0)) / reference_max
        if bead_sigma_px:
            psf = ndimage.gaussian_filter(psf, bead_sigma_px, mode="constant")
        return amplitude * psf + background

    def residual(p: np.ndarray) -> np.ndarray:
        return (model(p) - normalized).ravel()

    weights = np.clip(normalized - 0.2, 0, None)
    com = ndimage.center_of_mass(weights) if weights.sum() > 0 else np.array(data.shape) / 2
    start_position = [(com[k] - (data.shape[k] - 1) / 2) * px[k] for k in range(3)]

    lam = wavelength_nm
    extent = [data.shape[k] * px[k] for k in range(3)]
    lower = np.array([0.0, -1.0, *(-e for e in extent)] + [-lam] * len(modes))
    upper = np.array([np.inf, 1.0, *extent] + [lam] * len(modes))
    x_scale = np.array([1.0, 0.1, *px] + [lam / 20] * len(modes))

    first_stage = [0, 1, 2, 3, 4] + [5 + i for i, j in enumerate(modes) if j in (5, 6, 11)]
    spherical = modes.index(11) if 11 in modes else None
    starts = [0.0] if spherical is None or n_starts <= 1 else list(np.linspace(-lam / 8, lam / 8, n_starts))
    best = None
    for start in starts:
        p0 = np.array([1.0, 0.0, *start_position] + [0.0] * len(modes))
        if spherical is not None:
            p0[5 + spherical] = start

        def partial(q, full=p0.copy()):
            full[first_stage] = q
            return residual(full)

        stage1 = optimize.least_squares(
            partial, p0[first_stage], bounds=(lower[first_stage], upper[first_stage]),
            x_scale=x_scale[first_stage], max_nfev=200,
        )
        p0[first_stage] = stage1.x
        stage2 = optimize.least_squares(residual, p0, bounds=(lower, upper), x_scale=x_scale, max_nfev=400)
        if best is None or stage2.cost < best.cost:
            best = stage2

    p = best.x
    dof = max(1, best.jac.shape[0] - best.jac.shape[1])
    try:
        covariance = np.linalg.pinv(best.jac.T @ best.jac) * (2 * best.cost / dof)
        errors = np.sqrt(np.clip(np.diag(covariance), 0, None))
    except np.linalg.LinAlgError:
        errors = np.full(len(p), np.nan)

    fitted = model(p)
    ss_tot = float(((normalized - normalized.mean()) ** 2).sum())
    r2 = 1.0 - float(((fitted - normalized) ** 2).sum()) / ss_tot if ss_tot > 0 else float("nan")
    coeffs = {j: float(v) for j, v in zip(modes, p[5:])}
    rms = math.sqrt(sum(v * v for v in coeffs.values()))
    model_image = fitted * scale + baseline
    data_image = data
    if z_flip:
        model_image, data_image = model_image[::-1], data_image[::-1]
    return AberrationFit(
        coeffs_nm=coeffs,
        coeffs_err_nm={j: float(v) for j, v in zip(modes, errors[5:])},
        rms_nm=rms,
        strehl=math.exp(-((2 * math.pi * rms / lam) ** 2)),
        pairs=_pairs(coeffs),
        shift_zyx_nm=tuple(float(v) for v in p[2:5]),
        r2=r2,
        model=model_image,
        data=data_image,
        wavelength_nm=lam,
    )


def fit_aberrations_from_analysis(
    data: np.ndarray,
    analysis: BeadAnalysis,
    mask: np.ndarray,
    lateral_half_nm: float = 1500.0,
    axial_half_nm: float | None = None,
    modes: Sequence[int] = DEFAULT_MODES,
    backend: PSFBackend | None = None,
    z_flip: bool = False,
) -> AberrationFit:
    """Average the selected beads on a crop wide enough for phase retrieval
    and fit Zernike modes. Needs calibrated 3-D data and NA + wavelength."""
    p = analysis.params
    if analysis.ndim != 3:
        raise ValueError("aberration fitting needs a 3-D stack")
    if not p.physical:
        raise ValueError("aberration fitting needs a physical pixel size (nm), not px")
    if not (p.na and p.wavelength_nm):
        raise ValueError("aberration fitting needs NA and emission wavelength")
    px = np.asarray(p.pixel_size, dtype=np.float64)
    if axial_half_nm is None:
        axial = expected_fwhm_nm(p)[1]
        axial_half_nm = 2.5 * (axial if axial and np.isfinite(axial) else 3 * 0.51 * p.wavelength_nm / p.na)
    half = np.ceil(np.array([axial_half_nm, lateral_half_nm, lateral_half_nm]) / px).astype(int)
    half = np.minimum(half, (np.array(data.shape) - 5) // 2)
    averaged = average_psf(data, analysis, mask, half_px=half, fit=False)
    if averaged is None:
        raise ValueError(
            "no selected, isolated bead fits the aberration crop; "
            "reduce the crop size or relax the selection"
        )
    result = fit_aberrations(
        averaged.image, tuple(px), p.na, p.wavelength_nm, p.refractive_index, modes=modes,
        bead_diameter_nm=p.bead_diameter_nm, bead_labeling=p.bead_labeling,
        backend=backend, z_flip=z_flip,
    )
    result.n_beads = averaged.n
    if averaged.n < 5:
        result.warnings.append(f"Aberrations estimated from only {averaged.n} bead(s).")
    if result.r2 < 0.9:
        result.warnings.append(
            f"Model fit R^2 = {result.r2:.2f}: the model may not describe this PSF "
            "(check NA, wavelength and pixel size, or use a vectorial backend)."
        )
    return result
