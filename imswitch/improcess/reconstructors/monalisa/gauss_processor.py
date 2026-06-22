"""Fast Gaussian-weighted reassignment processor for MoNaLISA live reconstruction."""

from typing import TYPE_CHECKING, Any

import numpy as np

from imswitch.imcommon.model import initLogger

from .scan_geometry import get_1d_indices, get_interp_coords, get_rectangles_coords

try:
    import cupy as cp

    CUPY_AVAILABLE = True
except ImportError:
    CUPY_AVAILABLE = False
    cp = None

if TYPE_CHECKING:
    if CUPY_AVAILABLE:
        import cupy as cp


class GaussProcessorCPU:
    """
    CPU-based image reconstruction using Gaussian least-squares weighting and bilinear interpolation.

    Args:
        xp: X-axis period.
        xo: X-axis offset.
        yp: Y-axis period.
        yo: Y-axis offset.
        nx_c: Number of foci along x.
        ny_c: Number of foci along y.
        nx_s: Scanner steps along x.
        ny_s: Scanner steps along y.
        num_cols: Total number of columns in the frame.
        num_rows: Total number of rows in the frame.
        num_rects: Number of rectangles used to model the foci.
        scan_ori: Scan orientation string (e.g. "+x+y").
    """

    def __init__(
        self,
        xp: float,
        xo: float,
        yp: float,
        yo: float,
        nx_c: int,
        ny_c: int,
        nx_s: int,
        ny_s: int,
        num_rows: int,
        num_cols: int,
        num_rects: int = 3,
        scan_ori: str = "+x+y",
    ):
        self.num_rows = num_rows
        self.num_cols = num_cols
        self.num_foci = nx_c * ny_c
        self.num_rects = num_rects
        self.scan_ori = scan_ori
        self.x_interp, self.y_interp = get_interp_coords(
            xp, xo, yp, yo, nx_c, ny_c, num_rows, num_cols, num_rects
        )
        self.lsq_weights, self.pts_per_focus = self._calculate_weights(num_rects)
        self.frame_inds = get_1d_indices(nx_c, ny_c, nx_s, ny_s, scan_ori)
        self.num_frames_in_stack = nx_s * ny_s

    def _calculate_weights(self, num_rects: int) -> tuple[np.ndarray, int]:
        """
        Calculate the least-squares weights based on a Gaussian profile.

        Args:
            num_rects: Number of rectangles used to model the foci.

        Returns:
            Tuple containing the 1D weight array and the number of points per focus.
        """
        Xr, Yr = get_rectangles_coords(num_rects)
        sigma = 2.0
        gauss_vec = np.exp(-((Xr**2 + Yr**2) / (2 * sigma**2)))
        bg_vec = np.ones(len(gauss_vec))
        A_mat = np.stack((gauss_vec, bg_vec))
        lsq_weights = np.linalg.pinv(A_mat)[:, 0]
        return np.array(lsq_weights), len(gauss_vec)

    def process_frame(self, frame: np.ndarray) -> np.ndarray:
        """
        Perform bilinear interpolation on the input frame and apply weights.

        Args:
            frame: The 2D raw image frame.

        Returns:
            1D array of reconstructed intensity values.
        """
        x0 = np.clip(np.floor(self.x_interp).astype(np.int32), 0, self.num_cols - 1)
        x1 = np.clip(x0 + 1, 0, self.num_cols - 1)

        y0 = np.clip(np.floor(self.y_interp).astype(np.int32), 0, self.num_rows - 1)
        y1 = np.clip(y0 + 1, 0, self.num_rows - 1)

        dx = self.x_interp - x0
        dy = self.y_interp - y0

        interp_vals = (
            frame[y0, x0] * (1 - dx) * (1 - dy)
            + frame[y1, x0] * (1 - dx) * dy
            + frame[y0, x1] * dx * (1 - dy)
            + frame[y1, x1] * dx * dy
        ).reshape((self.num_foci, self.pts_per_focus))

        return np.dot(interp_vals, self.lsq_weights)

    def process_chunk(self, chunk: np.ndarray) -> np.ndarray:
        """
        Process an entire 3D chunk (num_frames, Y, X) at once.

        Args:
            chunk: A sub-stack (chunk) of raw frames.

        Returns:
            Processed pixels for each raw frame in the chunk.
        """
        x0 = np.clip(np.floor(self.x_interp).astype(np.int32), 0, self.num_cols - 1)
        x1 = np.clip(x0 + 1, 0, self.num_cols - 1)
        y0 = np.clip(np.floor(self.y_interp).astype(np.int32), 0, self.num_rows - 1)
        y1 = np.clip(y0 + 1, 0, self.num_rows - 1)

        dx = self.x_interp - x0
        dy = self.y_interp - y0

        interp_vals = (
            chunk[:, y0, x0] * (1 - dx) * (1 - dy)
            + chunk[:, y1, x0] * (1 - dx) * dy
            + chunk[:, y0, x1] * dx * (1 - dy)
            + chunk[:, y1, x1] * dx * dy
        ).reshape((-1, self.num_foci, self.pts_per_focus))

        return np.matmul(interp_vals, self.lsq_weights)

    def update_frame_inds(
        self,
        nx_c: int,
        ny_c: int,
        nx_s: int,
        ny_s: int,
        scan_ori: str,
    ):
        """
        Update the frame indices for the processor.

        Args:
            nx_c: Number of foci along x.
            ny_c: Number of foci along y.
            nx_s: Scanner steps along x.
            ny_s: Scanner steps along y.
            scan_ori: Scan orientation string.
        """
        self.frame_inds = get_1d_indices(nx_c, ny_c, nx_s, ny_s, scan_ori)


class GaussProcessorGPU:
    """
    GPU-accelerated image reconstruction using Gaussian least-squares weighting and bilinear interpolation.

    Requires CuPy. Falls back to CPU if unavailable.

    Args:
        xp: X-axis period.
        xo: X-axis offset.
        yp: Y-axis period.
        yo: Y-axis offset.
        nx_c: Number of foci along x.
        ny_c: Number of foci along y.
        nx_s: Scanner steps along x.
        ny_s: Scanner steps along y.
        num_cols: Total number of columns in the frame.
        num_rows: Total number of rows in the frame.
        num_rects: Number of rectangles used to model the foci.
        scan_ori: Scan orientation string (e.g. "+x+y").
    """

    def __init__(
        self,
        xp: float,
        xo: float,
        yp: float,
        yo: float,
        nx_c: int,
        ny_c: int,
        nx_s: int,
        ny_s: int,
        num_rows: int,
        num_cols: int,
        num_rects: int = 3,
        scan_ori: str = "+x+y",
    ):
        self.num_rows = num_rows
        self.num_cols = num_cols
        self.num_foci = nx_c * ny_c
        self.num_rects = num_rects
        self.scan_ori = scan_ori
        x_interp, y_interp = get_interp_coords(
            xp, xo, yp, yo, nx_c, ny_c, num_rows, num_cols, num_rects
        )
        self.x_interp = cp.array(x_interp)
        self.y_interp = cp.array(y_interp)
        self.lsq_weights, self.pts_per_focus = self._calculate_weights(num_rects)
        self.frame_inds = get_1d_indices(nx_c, ny_c, nx_s, ny_s, scan_ori)
        self.num_frames_in_stack = nx_s * ny_s

    def _calculate_weights(self, num_rects: int) -> tuple:
        """
        Calculate the least-squares weights based on a Gaussian profile.

        Args:
            num_rects: Number of rectangles used to model the foci.

        Returns:
            Tuple containing the 1D weight array (CuPy) and the number of points per focus.
        """
        Xr, Yr = get_rectangles_coords(num_rects)
        sigma = 2.0
        gauss_vec = np.exp(-((Xr**2 + Yr**2) / (2 * sigma**2)))
        bg_vec = np.ones(len(gauss_vec))
        A_mat = np.stack((gauss_vec, bg_vec))
        lsq_weights = np.linalg.pinv(A_mat)[:, 0]
        return cp.array(lsq_weights), len(gauss_vec)

    def process_frame(self, frame_gpu: Any) -> np.ndarray:
        """
        Perform bilinear interpolation on the input frame and apply weights.

        Args:
            frame_gpu: The 2D raw image frame (already on GPU).

        Returns:
            1D array of reconstructed intensity values on CPU.
        """
        x0 = cp.clip(cp.floor(self.x_interp).astype(cp.int32), 0, self.num_cols - 1)
        x1 = cp.clip(x0 + 1, 0, self.num_cols - 1)

        y0 = cp.clip(cp.floor(self.y_interp).astype(cp.int32), 0, self.num_rows - 1)
        y1 = cp.clip(y0 + 1, 0, self.num_rows - 1)

        dx = self.x_interp - x0
        dy = self.y_interp - y0

        interp_vals = (
            frame_gpu[y0, x0] * (1 - dx) * (1 - dy)
            + frame_gpu[y1, x0] * (1 - dx) * dy
            + frame_gpu[y0, x1] * dx * (1 - dy)
            + frame_gpu[y1, x1] * dx * dy
        ).reshape((self.num_foci, self.pts_per_focus))

        return cp.asnumpy(cp.dot(interp_vals, self.lsq_weights))

    def process_chunk(self, chunk_gpu: Any) -> np.ndarray:
        """
        Process an entire 3D chunk (num_frames, Y, X) at once on the GPU.

        Args:
            chunk_gpu: A sub-stack (chunk) of raw frames on GPU.

        Returns:
            Processed pixels for each raw frame in the chunk (on CPU).
        """
        x0 = cp.clip(cp.floor(self.x_interp).astype(cp.int32), 0, self.num_cols - 1)
        x1 = cp.clip(x0 + 1, 0, self.num_cols - 1)
        y0 = cp.clip(cp.floor(self.y_interp).astype(cp.int32), 0, self.num_rows - 1)
        y1 = cp.clip(y0 + 1, 0, self.num_rows - 1)

        dx = self.x_interp - x0
        dy = self.y_interp - y0

        interp_vals = (
            chunk_gpu[:, y0, x0] * (1 - dx) * (1 - dy)
            + chunk_gpu[:, y1, x0] * (1 - dx) * dy
            + chunk_gpu[:, y0, x1] * dx * (1 - dy)
            + chunk_gpu[:, y1, x1] * dx * dy
        ).reshape((-1, self.num_foci, self.pts_per_focus))

        return cp.asnumpy(cp.matmul(interp_vals, self.lsq_weights))

    def update_frame_inds(
        self,
        nx_c: int,
        ny_c: int,
        nx_s: int,
        ny_s: int,
        scan_ori: str,
    ):
        """
        Update the frame indices for the processor.

        Args:
            nx_c: Number of foci along x.
            ny_c: Number of foci along y.
            nx_s: Scanner steps along x.
            ny_s: Scanner steps along y.
            scan_ori: Scan orientation string.
        """
        self.frame_inds = get_1d_indices(nx_c, ny_c, nx_s, ny_s, scan_ori)


def make_gauss_processor(
    xp: float,
    xo: float,
    yp: float,
    yo: float,
    nx_c: int,
    ny_c: int,
    nx_s: int,
    ny_s: int,
    num_rows: int,
    num_cols: int,
    num_rects: int = 3,
    scan_ori: str = "+x+y",
    use_gpu: bool = False,
) -> GaussProcessorCPU | GaussProcessorGPU:
    """
    Factory function to create a Gauss processor (CPU or GPU).

    Args:
        xp: X-axis period.
        xo: X-axis offset.
        yp: Y-axis period.
        yo: Y-axis offset.
        nx_c: Number of foci along x.
        ny_c: Number of foci along y.
        nx_s: Scanner steps along x.
        ny_s: Scanner steps along y.
        num_rows: Total number of rows in the frame.
        num_cols: Total number of columns in the frame.
        num_rects: Number of rectangles used to model the foci.
        scan_ori: Scan orientation string.
        use_gpu: If True, attempt to use GPU; falls back to CPU if unavailable.

    Returns:
        A GaussProcessor instance (GPU if requested and available, otherwise CPU).
    """
    logger = initLogger("make_gauss_processor")
    args = (xp, xo, yp, yo, nx_c, ny_c, nx_s, ny_s, num_rows, num_cols, num_rects, scan_ori)

    if use_gpu and CUPY_AVAILABLE:
        logger.info("Creating GPU Gauss processor")
        return GaussProcessorGPU(*args)
    elif use_gpu and not CUPY_AVAILABLE:
        logger.info("GPU requested but CuPy not available; falling back to CPU")
        return GaussProcessorCPU(*args)
    else:
        return GaussProcessorCPU(*args)


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
