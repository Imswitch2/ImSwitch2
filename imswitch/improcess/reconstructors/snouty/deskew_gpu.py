"""
GPU-based deskew processor for SNOUTY lightsheet data.

Vendored from Mini_Recon/core/DeskewProcessorGPU.py.
Requires CuPy; imports are guarded at module level.
"""

from __future__ import annotations
from typing import TYPE_CHECKING

import numpy as np

# Guard CuPy imports — module must load on non-CUDA systems
try:
    import cupy as cp
    import cupyx
    _CUPY_AVAILABLE = True
except ImportError:
    _CUPY_AVAILABLE = False
    if not TYPE_CHECKING:
        cp = None  # type: ignore
        cupyx = None  # type: ignore

# Optional: cupyx.scipy.ndimage.gaussian_filter for separable Gaussian smoothing
try:
    from cupyx.scipy.ndimage import gaussian_filter as cp_gaussian_filter
    _CP_GAUSSIAN = True
except ImportError:
    _CP_GAUSSIAN = False


def cupy_available() -> bool:
    """Return True if CuPy is installed and importable."""
    return _CUPY_AVAILABLE


class DeskewProcessorGPU:
    """
    GPU deskew for tilted light-sheet (Snouty / OPM) stacks.

    Key optimisations:
    - Scatter indices (meshgrid + rint + mask + flat_idx) computed ONCE per call,
      reused for both the data scatter and the normalisation scatter.
    - cupyx.scatter_add used for both passes.
    - Separable gaussian_filter (O(N) per axis) replaces fftconvolve;
      skipped entirely when all sigmas < 0.5 vx.
    """

    def __init__(self, args: dict) -> None:
        if not _CUPY_AVAILABLE:
            raise RuntimeError(
                "SNOUTY GPU deskew requires cupy + a CUDA-capable GPU. "
                "Install with: pip install cupy-cuda12x (or cupy-cuda11x for older CUDA)"
            )
        
        self.c_px = float(args["c_px"])
        self.alpha = float(np.deg2rad(args["alpha_deg"]))
        self.dy = float(args["dy"])
        self.vx = float(args["sample_vx_size"])
        self.camera_offset = float(args.get("camera_offset", 0.0))
        self.flip_data = bool(args.get("flip_data", False))

        self.M = cp.asarray(self._build_transform(), dtype=cp.float32)

        # Cache for scatter indices — reused when geometry (stack shape) is unchanged
        self._scatter_cache_key: tuple | None = None
        self._scatter_cache_idx: 'cp.ndarray | None' = None
        self._scatter_cache_mask: 'cp.ndarray | None' = None

        x_dist = self.c_px / self.vx
        y_dist = self.dy / self.vx
        z_dist = self.dy * np.tan(self.alpha) / self.vx
        self._sigma = [max(0.0, z_dist / 2.355),
                       max(0.0, y_dist / 2.355),
                       max(0.0, x_dist / 2.355)]

    def _build_transform(self) -> np.ndarray:
        T = np.array([
            [self.c_px * np.sin(self.alpha), 0.0,     0.0],
            [self.c_px * np.cos(self.alpha), self.dy, 0.0],
            [0.0,                            0.0,     self.c_px],
        ])
        return T / self.vx

    def _flat_scatter_indices_and_weights(self, data_shape: tuple, out_shape: tuple):
        cache_key = (data_shape, out_shape)
        if self._scatter_cache_key == cache_key:
            return self._scatter_cache_idx, self._scatter_cache_mask

        nz, ny, nx = data_shape
        Sz, Sy, Sx = out_shape

        iz, iy, ix = cp.meshgrid(
            cp.arange(nz, dtype=cp.float32),
            cp.arange(ny, dtype=cp.float32),
            cp.arange(nx, dtype=cp.float32),
            indexing="ij",
        )

        sz = cp.rint(self.M[0, 0] * iz + self.M[0, 1] * iy).astype(cp.int64)
        sy = cp.rint(self.M[1, 0] * iz + self.M[1, 1] * iy).astype(cp.int64)
        sx = cp.rint(self.M[2, 2] * ix).astype(cp.int64)

        mask = (sz >= 0) & (sz < Sz) & (sy >= 0) & (sy < Sy) & (sx >= 0) & (sx < Sx)

        # make invalid entries harmless by sending them to index 0 with weight 0
        flat_idx = sz * (Sy * Sx) + sy * Sx + sx
        flat_idx = cp.where(mask, flat_idx, 0).ravel()
        weights = mask.astype(cp.float32).ravel()

        self._scatter_cache_key = cache_key
        self._scatter_cache_idx = flat_idx
        self._scatter_cache_mask = weights

        return flat_idx, weights

    def _smooth_2d(self, img: 'cp.ndarray', sigma: list) -> 'cp.ndarray':
        if max(sigma) < 0.5:
            return img
        if _CP_GAUSSIAN:
            return cp_gaussian_filter(img, sigma=sigma, mode='nearest')
        from scipy.ndimage import gaussian_filter as cpu_gf
        return cp.asarray(cpu_gf(cp.asnumpy(img), sigma=sigma, mode='nearest'))

    def _smooth(self, canvas: 'cp.ndarray') -> 'cp.ndarray':
        if max(self._sigma) < 0.5:
            return canvas
        if _CP_GAUSSIAN:
            return cp_gaussian_filter(canvas, sigma=self._sigma, mode='nearest')
        from scipy.ndimage import gaussian_filter as cpu_gf
        return cp.asarray(cpu_gf(cp.asnumpy(canvas), sigma=self._sigma, mode='nearest'))

    def process_stack(self, stack: 'cp.ndarray') -> 'cp.ndarray':
        """(planes, cam_y, cam_x) → (sample_z, sample_y, sample_x)"""
        if self.flip_data:
            stack = stack[::-1]
        data = cp.transpose(stack, (1, 0, 2)).astype(cp.float32)
        adjusted = (data - self.camera_offset).clip(0)

        size_data = cp.asarray(adjusted.shape, dtype=cp.float32)
        out_shape = tuple(int(s) for s in cp.asnumpy(cp.ceil(self.M @ size_data)).astype(int))
        flat_idx, valid_weights = self._flat_scatter_indices_and_weights(
            adjusted.shape, out_shape
        )

        flat_data = adjusted.ravel()

        n_out = int(np.prod(out_shape))

        recon_flat = cp.zeros(n_out, dtype=cp.float32)
        cupyx.scatter_add(recon_flat, flat_idx, flat_data * valid_weights)

        ones_flat = cp.zeros(n_out, dtype=cp.float32)
        cupyx.scatter_add(ones_flat, flat_idx, valid_weights)

        recon_canvas = recon_flat.reshape(out_shape)
        ones_canvas = ones_flat.reshape(out_shape)

        interp_data = self._smooth(recon_canvas)
        interp_ones = self._smooth(ones_canvas).clip(0.1)

        return cp.divide(interp_data, interp_ones)

    def process_projections(self, stack: 'cp.ndarray') -> dict:
        """Scatter to 3-D canvas then max-project; no FFT convolution."""
        if self.flip_data:
            stack = stack[::-1]
        data = cp.transpose(stack, (1, 0, 2)).astype(cp.float32)
        adjusted = (data - self.camera_offset).clip(0)

        size_data = cp.asarray(adjusted.shape, dtype=cp.float32)
        out_shape = tuple(int(s) for s in cp.asnumpy(cp.ceil(self.M @ size_data)).astype(int))

        flat_idx, valid_weights = self._flat_scatter_indices_and_weights(
            adjusted.shape, out_shape
        )

        flat_data = adjusted.ravel()
        n_out = int(np.prod(out_shape))

        canvas_flat = cp.zeros(n_out, dtype=cp.float32)
        cupyx.scatter_add(canvas_flat, flat_idx, flat_data * valid_weights)

        canvas = canvas_flat.reshape(out_shape)

        sz, sy, sx = self._sigma
        xy = self._smooth_2d(canvas.max(axis=0), [sy, sx])
        xz = self._smooth_2d(canvas.max(axis=1), [sz, sx])
        yz = self._smooth_2d(canvas.max(axis=2), [sz, sy])
        return {
            "xy": cp.asnumpy(xy),
            "xz": cp.asnumpy(xz),
            "yz": cp.asnumpy(yz),
        }

    def process_frame(self, frame: 'cp.ndarray') -> 'cp.ndarray':
        return self.process_stack(frame[cp.newaxis, ...]).squeeze(0)


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
