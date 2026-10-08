"""Richards & Wolf vectorial point-spread function on an isotropic voxel grid.

Port of the ``RichardsWolfPSF`` generator in Deconvolution_GUI
(``model/psfGeneration.py``), itself after the EPFL PSFGenerator. The three
Kirchhoff integrals are the same; what changed is how they are evaluated. The
original walks every radius of every plane through an adaptive Simpson loop
in Python, which takes minutes for a 101-plane PSF. Here the Bessel factors,
which depend only on radius and aperture angle, are computed once as a matrix
and every defocus plane is a single complex matrix product against them. A
101-plane PSF takes well under a second.
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np
from scipy.special import jv


def _simpson_grid(theta_max: float, k: float, max_extent_m: float) -> np.ndarray:
    """Aperture-angle samples dense enough for the fastest phase in the integrand.

    The phase ``k (r sin θ + z cos θ)`` turns at most ``k · max(r, |z|)`` radians
    per radian of θ. Thirty-two samples per phase cycle keeps composite Simpson
    far below the 10 % tolerance the original adaptive loop accepted.
    """
    cycles = k * max_extent_m * theta_max / (2.0 * np.pi)
    n = int(min(32768, max(256, 32 * np.ceil(cycles + 2))))
    if n % 2:
        n += 1
    return np.linspace(0.0, theta_max, n + 1)


def richards_wolf_radial(
    r_m: np.ndarray,
    z_m: np.ndarray,
    na: float,
    wavelength_nm: float,
    ri: float,
) -> np.ndarray:
    """Intensity ``h(z, r)`` of the Richards & Wolf PSF, shape ``(len(z_m), len(r_m))``.

    ``r_m`` are lateral radii and ``z_m`` defocus values, both in metres.
    """
    if not 0 < na < ri:
        raise ValueError(
            f"Detection NA must be positive and below the immersion index, "
            f"got NA={na!r} with n={ri!r}"
        )
    r_m = np.asarray(r_m, dtype=float).ravel()
    z_m = np.asarray(z_m, dtype=float).ravel()
    k = 2.0 * np.pi * ri / (wavelength_nm * 1e-9)
    theta_max = float(np.arcsin(na / ri))
    extent = max(float(np.max(r_m, initial=0.0)), float(np.max(np.abs(z_m), initial=0.0)))
    theta = _simpson_grid(theta_max, k, extent)

    sin_t, cos_t = np.sin(theta), np.cos(theta)
    prefactor = np.sqrt(cos_t) * sin_t
    x = k * np.outer(r_m, sin_t)                       # (n_r, n_theta)
    b0 = prefactor * (1.0 + cos_t) * jv(0, x)
    b1 = prefactor * sin_t * jv(1, x)
    b2 = prefactor * (1.0 - cos_t) * jv(2, x)

    weights = np.ones_like(theta)
    weights[1:-1:2] = 4.0
    weights[2:-1:2] = 2.0
    weights *= (theta[1] - theta[0]) / 3.0
    phase = np.exp(1j * k * np.outer(z_m, cos_t)) * weights   # (n_z, n_theta)

    i0 = phase @ b0.T
    i1 = phase @ b1.T
    i2 = phase @ b2.T
    return (np.abs(i0) ** 2 + 2.0 * np.abs(i1) ** 2 + np.abs(i2) ** 2).astype(np.float64)


@lru_cache(maxsize=8)
def richards_wolf_psf(
    size_px: int,
    voxel_nm: float,
    na: float,
    wavelength_nm: float,
    ri: float = 1.5,
    radial_oversampling: int = 4,
) -> np.ndarray:
    """A cubic ``(z, y, x)`` Richards & Wolf PSF, peak-normalised to 1, float32.

    The radial profile of every plane is sampled every ``1 / radial_oversampling``
    voxel and linearly interpolated onto the grid, where the original sampled at
    whole voxels. Results are cached per parameter set for the process lifetime,
    so re-running a reconstruction with the same optics does not recompute it.
    """
    size_px = int(size_px)
    if size_px < 3:
        raise ValueError(f"PSF size must be at least 3 voxels, got {size_px}")
    if voxel_nm <= 0 or wavelength_nm <= 0:
        raise ValueError("PSF voxel size and wavelength must be positive")
    centre = (size_px - 1) / 2.0
    offsets = np.arange(size_px) - centre
    yy, xx = np.meshgrid(offsets, offsets, indexing="ij")
    r_px = np.hypot(yy, xx)
    step = 1.0 / int(radial_oversampling)
    r_samples = np.arange(0.0, float(r_px.max()) + 2 * step, step)
    z_m = offsets * voxel_nm * 1e-9
    profile = richards_wolf_radial(r_samples * voxel_nm * 1e-9, z_m, na, wavelength_nm, ri)
    psf = np.stack([np.interp(r_px, r_samples, profile[i]) for i in range(size_px)])
    psf /= psf.max()
    return psf.astype(np.float32)


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
