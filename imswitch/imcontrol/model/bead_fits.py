from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Protocol

import numpy as np


@dataclass(frozen=True)
class FitResult:
    """Result of fitting a model to a bead image."""

    model: str
    params: dict[str, float]
    param_std: dict[str, float]
    r_squared: float
    center_px: tuple[float, float]
    summary: dict[str, object]
    # ROI the fit was evaluated in, as (x0, y0, x1, y1) in full-image
    # coordinates. Positional params (x0/y0, phases) are ROI-local; shift
    # evaluation coordinates by (x0, y0) to render on the full image.
    roi: tuple[int, int, int, int] | None = None


class FitModel(ABC):
    """Interface for parametric bead-image models."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the model identifier for the registry."""
        pass

    @property
    @abstractmethod
    def param_names(self) -> tuple[str, ...]:
        """Return parameter names in the order expected by model()."""
        pass

    @abstractmethod
    def initial_guess(self, image: np.ndarray, mask: np.ndarray | None) -> np.ndarray:
        """Return initial parameter estimates for curve_fit."""
        pass

    @abstractmethod
    def model(self, xy: tuple[np.ndarray, np.ndarray], *params) -> np.ndarray:
        """Evaluate the model at (x, y) mesh coordinates."""
        pass

    @abstractmethod
    def bounds(
        self, image: np.ndarray, mask: np.ndarray | None
    ) -> tuple[np.ndarray, np.ndarray]:
        """Return (lower_bounds, upper_bounds) for curve_fit."""
        pass

    @abstractmethod
    def summary(self, params: dict[str, float]) -> dict[str, object]:
        """Return human-readable summary metrics derived from fit parameters."""
        pass

    def center_px(
        self, params: dict[str, float], shape: tuple[int, int]
    ) -> tuple[float, float]:
        """Return the (y, x) feature center in local fit coordinates.

        Default expects 'x0'/'y0' parameters; periodic models without an
        explicit center override this.
        """
        return (params["y0"], params["x0"])

    # Periodic models set this so auto-ROI does not crop the image down to a
    # single blob, which would destroy the wavelength information.
    wants_full_image: bool = False


class Gaussian2D(FitModel):
    """Rotated 2D Gaussian model: I(x,y) = A * exp(-a*u^2 - 2*b*u*v - c*v^2) + offset.

    Parameters: amplitude, x0, y0, sigma_x, sigma_y, theta (radians), offset.
    """

    @property
    def name(self) -> str:
        return "gaussian2d"

    @property
    def param_names(self) -> tuple[str, ...]:
        return ("amplitude", "x0", "y0", "sigma_x", "sigma_y", "theta", "offset")

    def initial_guess(self, image: np.ndarray, mask: np.ndarray | None) -> np.ndarray:
        """Estimate initial Gaussian parameters from image moments."""
        if mask is not None:
            data = np.where(mask, image, 0.0)
        else:
            data = image

        height, width = image.shape
        y_coords, x_coords = np.indices(image.shape)

        total = float(np.sum(data))
        if total == 0:
            x0 = width / 2.0
            y0 = height / 2.0
        else:
            x0 = float(np.sum(x_coords * data) / total)
            y0 = float(np.sum(y_coords * data) / total)

        amplitude = float(np.max(image) - np.min(image))
        offset = float(np.min(image))
        sigma_x = width / 4.0
        sigma_y = height / 4.0
        theta = 0.0

        return np.array([amplitude, x0, y0, sigma_x, sigma_y, theta, offset])

    def model(
        self, xy: tuple[np.ndarray, np.ndarray], *params
    ) -> np.ndarray:
        """Evaluate rotated Gaussian at mesh coordinates."""
        amplitude, x0, y0, sigma_x, sigma_y, theta, offset = params
        x, y = xy
        cos_theta = np.cos(theta)
        sin_theta = np.sin(theta)

        u = (x - x0) * cos_theta + (y - y0) * sin_theta
        v = -(x - x0) * sin_theta + (y - y0) * cos_theta

        a = 1 / (2 * sigma_x**2)
        c = 1 / (2 * sigma_y**2)

        return amplitude * np.exp(-(a * u**2 + c * v**2)) + offset

    def bounds(
        self, image: np.ndarray, mask: np.ndarray | None
    ) -> tuple[np.ndarray, np.ndarray]:
        """Return reasonable bounds for Gaussian parameters."""
        height, width = image.shape
        value_range = float(np.max(image) - np.min(image))

        lower = np.array([0, 0, 0, 0.1, 0.1, -np.pi, -np.inf])
        upper = np.array(
            [
                2 * value_range,
                width,
                height,
                max(width, height),
                max(width, height),
                np.pi,
                np.inf,
            ]
        )
        return lower, upper

    def summary(self, params: dict[str, float]) -> dict[str, object]:
        """Return summary including FWHM and ellipticity."""
        sigma_x = params["sigma_x"]
        sigma_y = params["sigma_y"]
        fwhm_x = 2 * np.sqrt(2 * np.log(2)) * sigma_x
        fwhm_y = 2 * np.sqrt(2 * np.log(2)) * sigma_y
        ellipticity = abs(sigma_x - sigma_y) / max(sigma_x, sigma_y)

        return {
            "fwhm_x": fwhm_x,
            "fwhm_y": fwhm_y,
            "ellipticity": ellipticity,
            "theta_degrees": np.degrees(params["theta"]),
        }


class DonutR2Gaussian(FitModel):
    """Isotropic donut model: I(r) = A * r'^2 * exp(-r'^2 / (2*sigma^2)) + B.

    Parameters: amplitude, x0, y0, sigma, offset.
    Note: For elliptical/rotated extension, add sigma_y and theta parameters
    and compute r'^2 from rotated coordinates (see Gaussian2D).
    """

    @property
    def name(self) -> str:
        return "donut_r2_gaussian"

    @property
    def param_names(self) -> tuple[str, ...]:
        return ("amplitude", "x0", "y0", "sigma", "offset")

    def initial_guess(self, image: np.ndarray, mask: np.ndarray | None) -> np.ndarray:
        """Estimate initial donut parameters from image statistics."""
        if mask is not None:
            data = np.where(mask, image, 0.0)
        else:
            data = image

        height, width = image.shape
        y_coords, x_coords = np.indices(image.shape)

        total = float(np.sum(data))
        if total == 0:
            x0 = width / 2.0
            y0 = height / 2.0
        else:
            x0 = float(np.sum(x_coords * data) / total)
            y0 = float(np.sum(y_coords * data) / total)

        amplitude = float(np.max(image) - np.min(image))
        offset = float(np.min(image))
        sigma = min(width, height) / 4.0

        return np.array([amplitude, x0, y0, sigma, offset])

    def model(
        self, xy: tuple[np.ndarray, np.ndarray], *params
    ) -> np.ndarray:
        """Evaluate isotropic r^2*Gaussian donut at mesh coordinates."""
        amplitude, x0, y0, sigma, offset = params
        x, y = xy
        r_squared = (x - x0) ** 2 + (y - y0) ** 2
        return amplitude * r_squared * np.exp(-r_squared / (2 * sigma**2)) + offset

    def bounds(
        self, image: np.ndarray, mask: np.ndarray | None
    ) -> tuple[np.ndarray, np.ndarray]:
        """Return reasonable bounds for donut parameters."""
        height, width = image.shape
        value_range = float(np.max(image) - np.min(image))

        lower = np.array([0, 0, 0, 0.1, -np.inf])
        upper = np.array(
            [
                2 * value_range,
                width,
                height,
                max(width, height),
                np.inf,
            ]
        )
        return lower, upper

    def summary(self, params: dict[str, float]) -> dict[str, object]:
        """Return summary including peak radius."""
        sigma = params["sigma"]
        peak_radius = sigma * np.sqrt(2)
        return {
            "peak_radius": peak_radius,
            "sigma": sigma,
        }


class Sine2D(FitModel):
    """Crossed standing-wave pattern: I(x,y) = A * sin(2πx/λx + φx) * sin(2πy/λy + φy) + B.

    Parameters: amplitude, lambda_x, phi_x, lambda_y, phi_y, offset.
    Suited to periodic illumination patterns (e.g. RESOLFT/MoNaLISA lattices).
    Phases are defined in the local fit coordinate frame; the pair
    (φx + π, φy + π) yields the same pattern, so individual phases are only
    determined up to that joint shift.
    """

    wants_full_image = True

    @property
    def name(self) -> str:
        return "sine2d"

    @property
    def param_names(self) -> tuple[str, ...]:
        return ("amplitude", "lambda_x", "phi_x", "lambda_y", "phi_y", "offset")

    def initial_guess(self, image: np.ndarray, mask: np.ndarray | None) -> np.ndarray:
        """Estimate wavelengths and phases from the dominant 2D FFT component.

        For A·sin(αx+φx)·sin(αy+φy), the FFT has peaks at (±fx, ±fy) with
        coefficient angles θ(+fx,+fy) = φx + φy + π and θ(+fx,-fy) = φx - φy,
        from which both phases follow.
        """
        height, width = image.shape
        data = image - float(np.mean(image))

        spectrum = np.fft.fft2(data)
        magnitude = np.abs(spectrum)
        freq_y = np.fft.fftfreq(height)
        freq_x = np.fft.fftfreq(width)

        # Search the half-plane fx >= 0, excluding DC row/column, so the
        # dominant lattice component (fx, fy) is found once.
        search = magnitude.copy()
        search[:, freq_x < 0] = 0
        search[0, :] = 0
        search[:, 0] = 0
        ky, kx = np.unravel_index(int(np.argmax(search)), search.shape)

        fx = abs(freq_x[kx])
        fy = abs(freq_y[ky])
        lambda_x = 1.0 / fx if fx > 0 else float(width)
        lambda_y = 1.0 / fy if fy > 0 else float(height)

        # Indices of the (+fx, +fy) and (+fx, -fy) coefficients
        ky_pos = ky if freq_y[ky] >= 0 else (-ky) % height
        ky_neg = (-ky_pos) % height
        theta_pp = float(np.angle(spectrum[ky_pos, kx]))
        theta_pm = float(np.angle(spectrum[ky_neg, kx]))

        phi_x = 0.5 * (theta_pp - np.pi + theta_pm)
        phi_y = 0.5 * (theta_pp - np.pi - theta_pm)

        amplitude = 2.0 * float(np.std(data))
        offset = float(np.mean(image))

        return np.array([amplitude, lambda_x, phi_x, lambda_y, phi_y, offset])

    def model(
        self, xy: tuple[np.ndarray, np.ndarray], *params
    ) -> np.ndarray:
        """Evaluate the crossed sine pattern at mesh coordinates."""
        amplitude, lambda_x, phi_x, lambda_y, phi_y, offset = params
        x, y = xy
        return (
            amplitude
            * np.sin(2 * np.pi * x / lambda_x + phi_x)
            * np.sin(2 * np.pi * y / lambda_y + phi_y)
            + offset
        )

    def bounds(
        self, image: np.ndarray, mask: np.ndarray | None
    ) -> tuple[np.ndarray, np.ndarray]:
        """Return reasonable bounds for sine-pattern parameters."""
        height, width = image.shape
        value_range = float(np.max(image) - np.min(image))
        max_lambda = 4.0 * max(width, height)

        lower = np.array([0, 2.0, -2 * np.pi, 2.0, -2 * np.pi, -np.inf])
        upper = np.array(
            [2 * value_range, max_lambda, 2 * np.pi, max_lambda, 2 * np.pi, np.inf]
        )
        return lower, upper

    def summary(self, params: dict[str, float]) -> dict[str, object]:
        """Return summary including periods and phases in degrees."""
        return {
            "lambda_x": params["lambda_x"],
            "lambda_y": params["lambda_y"],
            "phi_x_degrees": np.degrees(params["phi_x"]),
            "phi_y_degrees": np.degrees(params["phi_y"]),
        }

    def center_px(
        self, params: dict[str, float], shape: tuple[int, int]
    ) -> tuple[float, float]:
        """Return the pattern maximum nearest to the image center.

        Maxima sit where both sines equal 1 (amplitude is bounded >= 0):
        x* = λx·(π/2 - φx)/2π + k·λx, analogously for y.
        """
        height, width = shape

        def nearest(lam: float, phi: float, target: float) -> float:
            base = lam * (np.pi / 2 - phi) / (2 * np.pi)
            k = round((target - base) / lam)
            return base + k * lam

        cx = nearest(params["lambda_x"], params["phi_x"], (width - 1) / 2.0)
        cy = nearest(params["lambda_y"], params["phi_y"], (height - 1) / 2.0)
        return (cy, cx)


FIT_MODELS: dict[str, FitModel] = {
    "gaussian2d": Gaussian2D(),
    "donut_r2_gaussian": DonutR2Gaussian(),
    "sine2d": Sine2D(),
}
