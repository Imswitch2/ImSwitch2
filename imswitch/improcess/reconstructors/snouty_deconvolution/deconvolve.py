"""Richardson–Lucy deconvolution through the tilted light-sheet sampling geometry.

Port of ``Deconvolver.Deconvolve`` and the ``convTransform`` /
``invConvTransform`` CUDA kernels of Deconvolution_GUI, written once against an
array module so the same code runs on NumPy and CuPy.

The image formation model is: the sample volume ``x`` lives on the deskew
output grid; blurring it with the effective kernel ``K`` and reading the
blurred volume at the (rounded) sample coordinate of every camera voxel gives
the expected camera stack. In the original this is a per-voxel loop over the
kernel neighbourhood on the GPU. Here it is the same operator written as a
whole-volume FFT convolution followed by a gather; the adjoint is a scatter of
the camera voxels onto the grid followed by a convolution with the flipped
kernel, which is what the original's ``invConvTransform`` computes by atomic
adds. With an odd-sized kernel the two share the centre ``n // 2`` and are
exact adjoints of each other.

The original never re-zeroed its adjoint canvas between iterations, so every
correction factor carried the previous one divided by ``H^T 1``. This port
builds the canvas afresh each iteration.
"""

from __future__ import annotations

from typing import Callable

import numpy as np
from scipy.fft import next_fast_len

from imswitch.improcess.reconstructors.snouty.deskew_cpu import build_deskew_transform

ProgressFn = Callable[[int, int], None]
CancelFn = Callable[[], None]


def _is_cupy(xp) -> bool:
    return xp.__name__ == "cupy"


def _to_host(array) -> np.ndarray:
    """A NumPy copy of ``array`` whichever backend owns it."""
    get = getattr(array, "get", None)
    return np.asarray(get() if callable(get) else array)


class ShearedRichardsonLucy:
    """Multiplicative Richardson–Lucy through the SNOUTY sampling operator.

    Args:
        geometry: ``c_px``, ``alpha_deg``, ``dy``, ``sample_vx_size``,
            ``camera_offset`` and ``flip_data`` as the deskew processors take them.
        kernel: Odd-sized, non-negative effective kernel on the output grid.
        xp: ``numpy`` or ``cupy``.
        normalisation_clip: Lower clip of ``H^T 1`` as a fraction of its maximum,
            which stops voxels the camera barely sees from exploding.
        gradient_consent: Split the data into two binomial halves and accept an
            update only where both halves agree on its direction.
        seed: Seed of the binomial split.
    """

    def __init__(
        self,
        geometry: dict,
        kernel: np.ndarray,
        xp=np,
        *,
        normalisation_clip: float = 0.3,
        gradient_consent: bool = False,
        seed: int = 0,
    ) -> None:
        self.xp = xp
        self.c_px = float(geometry["c_px"])
        self.alpha = float(np.deg2rad(geometry["alpha_deg"]))
        self.dy = float(geometry["dy"])
        self.vx = float(geometry["sample_vx_size"])
        self.camera_offset = float(geometry.get("camera_offset", 0.0))
        self.flip_data = bool(geometry.get("flip_data", False))
        self.normalisation_clip = float(normalisation_clip)
        self.gradient_consent = bool(gradient_consent)
        self.seed = int(seed)

        kernel = np.asarray(kernel, dtype=np.float32)
        if kernel.ndim != 3 or any(n % 2 == 0 for n in kernel.shape):
            raise ValueError(f"Kernel must be 3D with odd sides, got shape {kernel.shape}")
        if np.any(kernel < 0):
            raise ValueError("Kernel must be non-negative")
        self.kernel = kernel
        self.M = build_deskew_transform(self.c_px, self.alpha, self.dy, self.vx).astype(np.float32)

        self._prepared_shape: tuple[int, ...] | None = None
        self.out_shape: tuple[int, int, int] | None = None
        self._flat_idx = None
        self._mask = None
        self._fft_shape: tuple[int, ...] | None = None
        self._kernel_f = None
        self._kernel_flip_f = None
        self._ht1 = None

    # ------------------------------------------------------------------ setup
    def _rfftn(self, a, shape):
        if _is_cupy(self.xp):
            return self.xp.fft.rfftn(a, s=shape)
        from scipy import fft as sfft
        return sfft.rfftn(a, s=shape, workers=-1)

    def _irfftn(self, a, shape):
        if _is_cupy(self.xp):
            return self.xp.fft.irfftn(a, s=shape)
        from scipy import fft as sfft
        return sfft.irfftn(a, s=shape, workers=-1)

    def output_shape(self, stack_shape: tuple[int, int, int]) -> tuple[int, int, int]:
        """Deskew canvas shape for a ``(planes, cam_y, cam_x)`` stack."""
        planes, cam_y, cam_x = stack_shape
        size = np.asarray((cam_y, planes, cam_x), dtype=np.float32)
        return tuple(int(s) for s in np.ceil(self.M @ size).astype(int))

    def prepare(self, data_shape: tuple[int, int, int]) -> None:
        """Index tables, kernel spectra and ``H^T 1`` for ``(cam_y, planes, cam_x)`` data."""
        if self._prepared_shape == tuple(data_shape):
            return
        xp = self.xp
        nz, ny, nx = data_shape
        size = np.asarray(data_shape, dtype=np.float32)
        out_shape = tuple(int(s) for s in np.ceil(self.M @ size).astype(int))
        sz_n, sy_n, sx_n = out_shape

        iz, iy, ix = xp.meshgrid(
            xp.arange(nz, dtype=xp.float32),
            xp.arange(ny, dtype=xp.float32),
            xp.arange(nx, dtype=xp.float32),
            indexing="ij",
        )
        M = self.M
        sz = xp.rint(float(M[0, 0]) * iz + float(M[0, 1]) * iy).astype(xp.int64)
        sy = xp.rint(float(M[1, 0]) * iz + float(M[1, 1]) * iy).astype(xp.int64)
        sx = xp.rint(float(M[2, 2]) * ix).astype(xp.int64)
        mask = (sz >= 0) & (sz < sz_n) & (sy >= 0) & (sy < sy_n) & (sx >= 0) & (sx < sx_n)
        flat_idx = ((sz * sy_n + sy) * sx_n + sx)[mask]

        fft_shape = tuple(
            next_fast_len(n + k - 1) for n, k in zip(out_shape, self.kernel.shape)
        )
        kernel = xp.asarray(self.kernel)
        kernel_flip = kernel[::-1, ::-1, ::-1]

        self.out_shape = out_shape
        self._mask = mask
        self._flat_idx = flat_idx
        self._fft_shape = fft_shape
        self._kernel_f = self._rfftn(kernel, fft_shape)
        self._kernel_flip_f = self._rfftn(kernel_flip, fft_shape)
        self._prepared_shape = tuple(data_shape)

        ones = xp.ones(int(flat_idx.shape[0]), dtype=xp.float32)
        ht1 = self.adjoint(ones)
        floor = self.normalisation_clip * float(ht1.max())
        self._ht1 = xp.maximum(ht1, xp.float32(floor))

    # -------------------------------------------------------------- operators
    def _convolve_same(self, volume, spectrum):
        """Centred convolution of ``volume`` with the kernel behind ``spectrum``."""
        xp = self.xp
        full = self._irfftn(self._rfftn(volume, self._fft_shape) * spectrum, self._fft_shape)
        start = [k // 2 for k in self.kernel.shape]
        stop = [s + n for s, n in zip(start, self.out_shape)]
        return xp.ascontiguousarray(
            full[start[0]:stop[0], start[1]:stop[1], start[2]:stop[2]]
        ).astype(xp.float32)

    def forward(self, volume):
        """Expected camera values at the in-canvas camera voxels (1D, in index order)."""
        blurred = self._convolve_same(volume, self._kernel_f)
        return blurred.ravel()[self._flat_idx]

    def adjoint(self, values):
        """Transpose of :meth:`forward`: scatter onto the grid, blur with the flipped kernel."""
        xp = self.xp
        n_out = int(np.prod(self.out_shape))
        canvas = xp.bincount(self._flat_idx, weights=values.astype(xp.float32), minlength=n_out)
        canvas = canvas.reshape(self.out_shape).astype(xp.float32)
        return self._convolve_same(canvas, self._kernel_flip_f)

    # ------------------------------------------------------------------- data
    def camera_values(self, stack):
        """Offset-corrected, clipped camera voxels in the index order of :meth:`forward`."""
        xp = self.xp
        stack = xp.asarray(stack)
        if self.flip_data:
            stack = stack[::-1]
        data = xp.transpose(stack, (1, 0, 2)).astype(xp.float32)
        adjusted = xp.maximum(data - xp.float32(self.camera_offset), xp.float32(0.0))
        self.prepare(adjusted.shape)
        return adjusted[self._mask]

    # ------------------------------------------------------------------- loop
    def _update_factor(self, data, expected):
        xp = self.xp
        ratio = data / xp.maximum(expected, xp.float32(1e-12))
        return self.adjoint(ratio) / self._ht1

    def run(
        self,
        stack,
        iterations: int,
        *,
        progress: ProgressFn | None = None,
        cancel: CancelFn | None = None,
        initial=None,
    ):
        """Deconvolve one ``(planes, cam_y, cam_x)`` stack; returns the sample volume."""
        xp = self.xp
        iterations = int(iterations)
        if iterations < 1:
            raise ValueError(f"Iterations must be at least 1, got {iterations}")
        data = self.camera_values(stack)
        if initial is None:
            estimate = xp.ones(self.out_shape, dtype=xp.float32)
        else:
            estimate = xp.asarray(initial, dtype=xp.float32).copy()

        if self.gradient_consent:
            # The split happens once per stack, so it is done on the host with
            # NumPy's generator for both backends: one RNG, one result,
            # whichever device runs the iterations.
            counts = np.rint(_to_host(data)).astype(np.int64)
            half_a_host = np.random.default_rng(self.seed).binomial(counts, 0.5)
            half_a = xp.asarray(half_a_host, dtype=xp.float32)
            half_b = xp.asarray(counts - half_a_host, dtype=xp.float32)

        for iteration in range(iterations):
            if cancel is not None:
                cancel()
            expected = self.forward(estimate)
            if self.gradient_consent:
                half_expected = expected * xp.float32(0.5)
                factor_a = self._update_factor(half_a, half_expected)
                factor_b = self._update_factor(half_b, half_expected)
                agree = xp.sign(factor_a - 1.0) == xp.sign(factor_b - 1.0)
                factor = xp.where(agree, 0.5 * (factor_a + factor_b), xp.float32(1.0))
            else:
                factor = self._update_factor(data, expected)
            estimate *= factor.astype(xp.float32)
            if progress is not None:
                progress(iteration + 1, iterations)
        return estimate


class DeconvolutionProcessorCPU(ShearedRichardsonLucy):
    """NumPy backend of the sheared Richardson–Lucy processor."""

    def __init__(self, geometry: dict, kernel: np.ndarray, **options) -> None:
        super().__init__(geometry, kernel, np, **options)


class DeconvolutionProcessorGPU(ShearedRichardsonLucy):
    """CuPy backend; raises a clear error when CuPy is not importable."""

    def __init__(self, geometry: dict, kernel: np.ndarray, **options) -> None:
        try:
            import cupy as cp
        except ImportError as exc:
            raise RuntimeError(
                "SNOUTY GPU deconvolution requires cupy + a CUDA-capable GPU. "
                "Install with: pip install cupy-cuda12x (or cupy-cuda11x for older CUDA)"
            ) from exc
        super().__init__(geometry, kernel, cp, **options)


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
