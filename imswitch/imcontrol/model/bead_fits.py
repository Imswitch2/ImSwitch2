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


FIT_MODELS: dict[str, FitModel] = {
    "gaussian2d": Gaussian2D(),
    "donut_r2_gaussian": DonutR2Gaussian(),
}
