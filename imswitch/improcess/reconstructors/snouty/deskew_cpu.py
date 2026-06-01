"""
CPU-based deskew processor for lightsheet data (SNOUTY/OPM/MS-RESOLFT).

Vendored from Mini_Recon (https://github.com/khoj00/Mini_Recon).
"""

import numpy as np
from scipy.ndimage import gaussian_filter


class DeskewProcessorCPU:
    """
    CPU fallback of DeskewProcessorGPU.

    Key optimisations vs. first version:
    - Scatter indices computed ONCE per call (meshgrid + rint + mask), not twice.
    - np.bincount used for both passes (C-level, vectorised); the ones pass uses
      the integer overload (no float weights → faster still).
    - gaussian_filter (separable, O(N) per axis) replaces fftconvolve; skipped
      entirely when all sigmas < 0.5 vx (dense scan regime).
    - Input dtype kept as-is until the camera-offset subtraction forces float32.
    """

    def __init__(self, args: dict) -> None:
        self.c_px = float(args["c_px"])
        self.alpha = float(np.deg2rad(args["alpha_deg"]))
        self.dy = float(args["dy"])
        self.vx = float(args["sample_vx_size"])
        self.camera_offset = float(args.get("camera_offset", 0.0))
        self.flip_data = bool(args.get("flip_data", False))

        self.M = self._build_transform().astype(np.float32)

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

    def _scatter_indices(self, data_shape: tuple, out_shape: tuple):
        """Return (flat_idx, mask) for scatter into *out_shape* canvas.

        Computed once and reused for both the data pass and the ones pass.
        """
        nz, ny, nx = data_shape
        Sz, Sy, Sx = out_shape
        iz, iy, ix = np.meshgrid(
            np.arange(nz, dtype=np.float32),
            np.arange(ny, dtype=np.float32),
            np.arange(nx, dtype=np.float32),
            indexing="ij",
        )
        sz = np.rint(self.M[0, 0] * iz + self.M[0, 1] * iy).astype(np.intp)
        sy = np.rint(self.M[1, 0] * iz + self.M[1, 1] * iy).astype(np.intp)
        sx = np.rint(self.M[2, 2] * ix).astype(np.intp)
        mask = (sz >= 0) & (sz < Sz) & (sy >= 0) & (sy < Sy) & (sx >= 0) & (sx < Sx)
        flat_idx = (sz[mask] * Sy + sy[mask]) * Sx + sx[mask]
        return flat_idx, mask

    def process_stack(self, stack: np.ndarray) -> np.ndarray:
        """(planes, cam_y, cam_x) → (sample_z, sample_y, sample_x)"""
        if self.flip_data:
            stack = stack[::-1]
        # Transpose to (cam_y, planes, cam_x); cast to float32 only now (offset needs it)
        data = np.transpose(stack, (1, 0, 2))
        adjusted = (data.astype(np.float32) - self.camera_offset).clip(0)

        size_data = np.asarray(adjusted.shape, dtype=np.float32)
        out_shape = tuple(int(s) for s in np.ceil(self.M @ size_data).astype(int))
        Sz, Sy, Sx = out_shape
        n_vox = Sz * Sy * Sx

        flat_idx, mask = self._scatter_indices(adjusted.shape, out_shape)

        recon_flat = np.bincount(flat_idx,
                                 weights=adjusted[mask].astype(np.float64),
                                 minlength=n_vox)
        ones_flat = np.bincount(flat_idx, minlength=n_vox)   # int counts, no weights

        recon_canvas = recon_flat.reshape(out_shape).astype(np.float32)
        ones_canvas  = ones_flat .reshape(out_shape).astype(np.float32)

        if max(self._sigma) >= 0.5:
            interp_data = gaussian_filter(recon_canvas, sigma=self._sigma, mode='nearest')
            interp_ones = gaussian_filter(ones_canvas,  sigma=self._sigma, mode='nearest').clip(0.1)
        else:
            interp_data = recon_canvas
            interp_ones = ones_canvas.clip(0.1)

        return np.divide(interp_data, interp_ones)

    def process_projections(self, stack: np.ndarray) -> dict:
        """Deskew-accurate projections: scatter to 3D canvas then max-project.
        No FFT convolution. Returns {"xy","xz","yz"} as float32 2-D arrays.
        """
        if self.flip_data:
            stack = stack[::-1]
        data = np.transpose(stack, (1, 0, 2))
        adjusted = (data.astype(np.float32) - self.camera_offset).clip(0)

        size_data = np.asarray(adjusted.shape, dtype=np.float32)
        out_shape = tuple(int(s) for s in np.ceil(self.M @ size_data).astype(int))
        Sz, Sy, Sx = out_shape

        flat_idx, mask = self._scatter_indices(adjusted.shape, out_shape)
        canvas = (np.bincount(flat_idx,
                              weights=adjusted[mask].astype(np.float64),
                              minlength=Sz * Sy * Sx)
                  .reshape(out_shape).astype(np.float32))
        xy = canvas.max(axis=0)
        xz = canvas.max(axis=1)
        yz = canvas.max(axis=2)
        if max(self._sigma) >= 0.5:
            sz, sy, sx = self._sigma
            xy = gaussian_filter(xy, sigma=[sy, sx], mode='nearest')
            xz = gaussian_filter(xz, sigma=[sz, sx], mode='nearest')
            yz = gaussian_filter(yz, sigma=[sz, sy], mode='nearest')
        return {"xy": xy, "xz": xz, "yz": yz}

    def process_frame(self, frame: np.ndarray) -> np.ndarray:
        return self.process_stack(frame[np.newaxis, ...]).squeeze(0)


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
