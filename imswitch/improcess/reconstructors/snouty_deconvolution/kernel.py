"""Effective detection kernel of a tilted light-sheet (SNOUTY / OPM / PLSR) scan.

Port of ``KernelHandler.makePLSRKernel`` from Deconvolution_GUI. The kernel is
the optical PSF of the detection objective, multiplied by the illumination
sheet lying in the camera's focal plane (a mixture of a confined and a wider
read-out sheet), optionally convolved with the camera pixel footprint when a
pixel is much larger than an output voxel, and cropped to its support.

Two deliberate departures from the original:

* every mesh axis is centred on its own length, where the original centred all
  three on the z length (correct only for cubic PSFs);
* the result is normalised to unit sum rather than unit peak. Richardson–Lucy
  is invariant to the kernel scale up to a global factor on the estimate, and
  a unit-sum kernel makes that estimate come out in camera intensity units.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from scipy.signal import fftconvolve

from .psf import richards_wolf_psf

FWHM_TO_SIGMA = 1.0 / (2.0 * np.sqrt(2.0 * np.log(2.0)))   # 1 / 2.3548

#: Thickness of the camera pixel footprint along the sheet normal (nm), as in the
#: original generator.
PIXEL_FOOTPRINT_THICKNESS_NM = 80.0


def _centred_axes(shape: tuple[int, ...]) -> tuple[np.ndarray, ...]:
    """``(z, y, x)`` meshes in voxels, each axis centred on its own extent."""
    axes = [np.arange(n, dtype=float) - (n - 1) / 2.0 for n in shape]
    return tuple(np.meshgrid(*axes, indexing="ij"))


def light_sheet_profile(
    shape: tuple[int, int, int],
    alpha_deg: float,
    voxel_nm: float,
    confined_fwhm_nm: float,
    readout_fwhm_nm: float,
    background_ratio: float,
) -> np.ndarray:
    """Illumination profile on the ``(z, y, x)`` voxel grid.

    The sheet lies in the camera focal plane, which the deskew geometry maps to
    the direction ``(sin α, cos α)`` in sample ``(z, y)``. Its normal is
    ``(cos α, −sin α)``, so the profile is Gaussian in ``z cos α − y sin α``.
    """
    alpha = np.deg2rad(alpha_deg)
    zz, yy, _xx = _centred_axes(shape)
    normal = zz * np.cos(alpha) - yy * np.sin(alpha)
    sigma_confined = confined_fwhm_nm * FWHM_TO_SIGMA / voxel_nm
    sigma_readout = readout_fwhm_nm * FWHM_TO_SIGMA / voxel_nm
    confined = np.exp(-(normal ** 2) / (2.0 * sigma_confined ** 2))
    readout = np.exp(-(normal ** 2) / (2.0 * sigma_readout ** 2))
    ratio = float(background_ratio)
    return ratio * readout + (1.0 - ratio) * confined


def pixel_footprint_kernel(
    voxel_nm: float,
    c_px: float,
    alpha_deg: float,
    thickness_nm: float = PIXEL_FOOTPRINT_THICKNESS_NM,
) -> np.ndarray:
    """A camera pixel's footprint in sample voxels: a thin tilted slab.

    Odd voxel counts per axis cover the projected pixel extent
    ``(c sin α, c cos α, c)``; across the slab the profile is a Gaussian of
    FWHM ``thickness_nm`` along the sheet normal.
    """
    alpha = np.deg2rad(alpha_deg)
    extent_nm = np.array([c_px * np.sin(alpha), c_px * np.cos(alpha), c_px])
    counts = (np.floor((extent_nm / voxel_nm) / 2.0) * 2 + 1).astype(int)
    zz, yy, _xx = _centred_axes(tuple(int(n) for n in counts))
    normal = zz * np.cos(alpha) - yy * np.sin(alpha)
    sigma = thickness_nm * FWHM_TO_SIGMA / voxel_nm
    return np.exp(-(normal ** 2) / (2.0 * sigma ** 2))


def crop_to_support(volume: np.ndarray, clip_factor: float) -> np.ndarray:
    """Crop to the bounding box where any axis-wise maximum exceeds ``clip_factor · max``."""
    cutoff = float(volume.max()) * float(clip_factor)
    slices = []
    for axis in range(volume.ndim):
        others = tuple(a for a in range(volume.ndim) if a != axis)
        trace = volume.max(axis=others)
        keep = np.flatnonzero(trace > cutoff)
        if keep.size == 0:
            raise ValueError("Kernel is empty after clipping; lower the clip factor")
        slices.append(slice(int(keep[0]), int(keep[-1]) + 1))
    return volume[tuple(slices)]


def pad_to_odd(volume: np.ndarray) -> np.ndarray:
    """Append one zero plane on every even-length axis.

    The sheared convolution operators centre the kernel on index ``n // 2``;
    odd lengths make that the true centre, so forward and adjoint share it.
    """
    pad = [(0, 1 - n % 2) for n in volume.shape]
    if any(p[1] for p in pad):
        volume = np.pad(volume, pad)
    return volume


def load_psf(path: str | Path) -> np.ndarray:
    """A 3D PSF from a TIFF file, as float32."""
    import tifffile

    psf = np.asarray(tifffile.imread(str(path)), dtype=np.float32)
    if psf.ndim != 3:
        raise ValueError(f"PSF file {path} must hold a 3D volume, got shape {psf.shape}")
    return psf


def effective_kernel(params: dict) -> np.ndarray:
    """The unit-sum, odd-sized effective kernel for one parameter set.

    ``params`` carries the deskew geometry (``c_px``, ``alpha_deg``,
    ``sample_vx_size``) and the deconvolution keys of
    :data:`~.reconstructor.DECONVOLUTION_DEFAULTS`. A non-empty ``psf_path``
    loads the optical PSF from disk, otherwise a Richards & Wolf PSF is
    generated at the output voxel size.
    """
    voxel_nm = float(params["sample_vx_size"])
    alpha_deg = float(params["alpha_deg"])
    c_px = float(params["c_px"])

    psf_path = str(params.get("psf_path", "") or "").strip()
    if psf_path:
        psf = load_psf(psf_path)
    else:
        psf = richards_wolf_psf(
            int(params["psf_size_px"]),
            voxel_nm,
            float(params["detection_na"]),
            float(params["wavelength_nm"]),
            float(params["immersion_ri"]),
        )

    sheet = light_sheet_profile(
        psf.shape,
        alpha_deg,
        voxel_nm,
        float(params["confined_sheet_fwhm_nm"]),
        float(params["readout_sheet_fwhm_nm"]),
        float(params["background_sheet_ratio"]),
    )
    kernel = sheet * psf.astype(np.float64)

    if c_px > 2.0 * voxel_nm:
        kernel = fftconvolve(kernel, pixel_footprint_kernel(voxel_nm, c_px, alpha_deg))

    kernel = crop_to_support(kernel, float(params["kernel_clip_factor"]))
    kernel = pad_to_odd(np.clip(kernel, 0.0, None))
    total = float(kernel.sum())
    if total <= 0:
        raise ValueError("Effective kernel has no positive support")
    return (kernel / total).astype(np.float32)


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
