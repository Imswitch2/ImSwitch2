"""The fast-Gauss extractor of MoNaLISA: the joint Gaussian fit, scattered by scan index.

What the live session and the offline *Fast Gauss* method run on every frame.
The geometry is fixed for a recording and typed in or found once: an
axis-aligned grid of foci from the pattern finder, a spot width from the PSF
FWHM and the pixel size, the steps of the scan. The extraction operator is
built once from them, and every frame is then one sparse product and one
pre-factorized solve (:class:`~.extraction.ExtractionOperator`), which fits
the amplitudes of *all* foci of the frame together on a constant background.
On a GPU the same product and solve run on the device (CuPy), so the frames
of a live acquisition never have to come back to the host per frame.

This is the lattice pipeline's extraction without what the lattice
reconstructor reads from the frames (the lattice, the spot width, the shift
factor) and without its corrections (frame gain, cell offsets, reassignment):
the fast alternative, for live reconstruction. It replaced the isolated
per-focus fit on a small footprint, which read a share of every neighbour's
light as its own -- a ghost of the image one period away -- and the
footprint and pinhole options that existed to keep that in check.
"""

from __future__ import annotations

import numpy as np

from imswitch.imcommon.model import initLogger

from .extraction import DEFAULT_REACH_SIGMA, ExtractionOperator
from .scan_geometry import get_1d_indices, get_center_coords

try:
    import cupy as cp
    import cupyx.scipy.sparse as cusparse
    import cupyx.scipy.sparse.linalg as cusparse_linalg

    CUPY_AVAILABLE = True
except ImportError:
    CUPY_AVAILABLE = False
    cp = None
    cusparse = None
    cusparse_linalg = None

#: The spot width used when neither the PSF FWHM nor the pixel size is known.
DEFAULT_GAUSSIAN_SIGMA_PX = 2.0


class _DeviceSolve:
    """The operator's per-frame product and solve, on the device.

    Mirrors ``ExtractionOperator._solve``: the projector (the weighted
    design, transposed) and the factorized normal matrix are copied to the
    device once; a chunk of frames is then one sparse product and one solve
    with the factors, for all frames of the chunk at once.
    """

    def __init__(self, operator: ExtractionOperator):
        import scipy.sparse as sparse

        self.row_pixel = cp.asarray(operator._row_pixel)
        # Float64 throughout, as on the CPU: the factors are, and a sparse
        # product is not where a GPU's float32 speed would be felt.
        self.projector = cusparse.csr_matrix(operator._projector.astype(np.float64))
        normal = (operator._projector @ operator._design).tocsc()
        diagonal = normal.diagonal()
        normal = normal + sparse.diags(1e-10 * diagonal + 1e-300, format="csc")
        self.solver = cusparse_linalg.splu(cusparse.csc_matrix(normal.astype(np.float64)))
        columns = operator._amplitude_column
        have = operator.valid & (columns >= 0)
        self.amplitude_column = cp.asarray(np.where(have, columns, 0))
        self.have = cp.asarray(have)
        self.num_coefficients = int(operator._design.shape[1])
        self.frame_shape = operator.frame_shape

    def amplitudes(self, chunk) -> "cp.ndarray":
        chunk = cp.asarray(chunk)
        if chunk.ndim == 2:
            chunk = chunk[None]
        flat = chunk.reshape(chunk.shape[0], -1)
        data = flat[:, self.row_pixel].astype(cp.float64).T
        projected = self.projector @ data
        solution = self.solver.solve(projected)                 # (coefficients, frames)
        amplitude = solution[self.amplitude_column].T            # (frames, num_foci)
        return cp.where(self.have[None, :], amplitude, 0.0).astype(cp.float32)


def _margin_points(
    xp: float, xo: float, yp: float, yo: float, num_rows: int, num_cols: int,
    margin: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Grid points outside the frame but within ``margin`` of it.

    Their tails reach into the frame, and the joint fit accounts for them so
    that the foci at the border are not read brighter than they are.
    """
    xs = np.arange(np.floor(-(xo + margin) / xp), np.ceil((num_cols + margin - xo) / xp) + 1)
    ys = np.arange(np.floor(-(yo + margin) / yp), np.ceil((num_rows + margin - yo) / yp) + 1)
    gx, gy = np.meshgrid(xo + xs * xp, yo + ys * yp)
    gx, gy = gx.ravel(), gy.ravel()
    near = (gx >= -margin) & (gx < num_cols + margin) & (gy >= -margin) & (gy < num_rows + margin)
    outside = (gx < 0) | (gx >= num_cols) | (gy < 0) | (gy >= num_rows)
    keep = near & outside
    return gx[keep], gy[keep]


class FastGaussExtractor:
    """Joint Gaussian extraction on a fixed grid, with the scan's index map.

    Args:
        xp, xo, yp, yo: Period and offset of the grid along x and y, px.
        nx_c, ny_c: Number of foci along x and y (inside the frame).
        nx_s, ny_s: Scanner steps along x and y.
        num_rows, num_cols: Frame size.
        gaussian_sigma_px: Spot width, px.
        fit_background: Fit a constant background per focus; off is the pure
            matched filter.
        scan_ori: Scan orientation string (e.g. ``"+x+y"``), for the index
            map; :meth:`update_frame_inds` changes it once it is detected.
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
        gaussian_sigma_px: float = DEFAULT_GAUSSIAN_SIGMA_PX,
        fit_background: bool = True,
        scan_ori: str = "+x+y",
        use_gpu: bool = False,
    ):
        gaussian_sigma_px = float(gaussian_sigma_px)
        if not gaussian_sigma_px > 0:
            raise ValueError("gaussian_sigma_px must be > 0")
        self.num_rows = int(num_rows)
        self.num_cols = int(num_cols)
        self.num_foci = int(nx_c) * int(ny_c)
        self.gaussian_sigma_px = gaussian_sigma_px
        self.fit_background = bool(fit_background)
        self.scan_ori = scan_ori
        self.use_gpu = bool(use_gpu) and CUPY_AVAILABLE
        self._device = None

        # The foci in the frame in the order the index map expects (row-major
        # over the grid), then the ones just outside it.
        x_in, y_in = get_center_coords(xp, xo, yp, yo, nx_c, ny_c)
        margin = 3.0 * gaussian_sigma_px
        x_out, y_out = _margin_points(xp, xo, yp, yo, num_rows, num_cols, margin)
        self.operator = ExtractionOperator.build(
            np.concatenate([x_in, x_out]),
            np.concatenate([y_in, y_out]),
            gaussian_sigma_px,
            (self.num_rows, self.num_cols),
            reach_sigma=DEFAULT_REACH_SIGMA,
            background="constant" if self.fit_background else "none",
            joint=True,
        )
        self.frame_inds = get_1d_indices(nx_c, ny_c, nx_s, ny_s, scan_ori)
        self.num_frames_in_stack = int(nx_s) * int(ny_s)
        if self.use_gpu:
            self._device = _DeviceSolve(self.operator)

    def process_chunk(self, chunk) -> np.ndarray:
        """Amplitudes of the foci of every frame of ``chunk``: ``(frames, num_foci)``.

        A focus the fit cannot place (too few pixels in the frame) is 0.
        A chunk on the device is solved there and the result copied back.
        """
        if self._device is not None:
            return cp.asnumpy(self.process_chunk_gpu(chunk))
        chunk = np.asarray(chunk)
        amplitude = self.operator.apply(chunk).amplitude[:, : self.num_foci]
        return np.nan_to_num(amplitude, nan=0.0).astype(np.float32, copy=False)

    def process_chunk_gpu(self, chunk_gpu):
        """The same on the device; the result stays there (a CuPy array)."""
        if self._device is None:
            raise RuntimeError("This extractor was built without a GPU")
        return self._device.amplitudes(chunk_gpu)[:, : self.num_foci]

    def process_frame(self, frame: np.ndarray) -> np.ndarray:
        """Amplitudes of the foci of one frame: ``(num_foci,)``."""
        return self.process_chunk(np.asarray(frame)[np.newaxis])[0]

    def update_frame_inds(
        self, nx_c: int, ny_c: int, nx_s: int, ny_s: int, scan_ori: str
    ) -> None:
        """Rebuild the index map for a detected orientation."""
        self.scan_ori = scan_ori
        self.frame_inds = get_1d_indices(nx_c, ny_c, nx_s, ny_s, scan_ori)


def make_fast_gauss_extractor(
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
    gaussian_sigma_px: float = DEFAULT_GAUSSIAN_SIGMA_PX,
    fit_background: bool = True,
    scan_ori: str = "+x+y",
    use_gpu: bool = False,
) -> FastGaussExtractor:
    """Build the extractor and log what it was built with.

    ``use_gpu`` asks for the device solve; without CuPy, or when the device
    factorization fails, the extractor runs on the CPU and says so.
    """
    logger = initLogger("make_fast_gauss_extractor")
    if use_gpu and not CUPY_AVAILABLE:
        logger.info("GPU requested but CuPy not available; falling back to CPU")
    try:
        extractor = FastGaussExtractor(
            xp, xo, yp, yo, nx_c, ny_c, nx_s, ny_s, num_rows, num_cols,
            gaussian_sigma_px=gaussian_sigma_px, fit_background=fit_background,
            scan_ori=scan_ori, use_gpu=use_gpu,
        )
    except Exception as error:
        if not (use_gpu and CUPY_AVAILABLE):
            raise
        logger.warning(f"GPU extraction could not be set up ({error}); using the CPU")
        extractor = FastGaussExtractor(
            xp, xo, yp, yo, nx_c, ny_c, nx_s, ny_s, num_rows, num_cols,
            gaussian_sigma_px=gaussian_sigma_px, fit_background=fit_background,
            scan_ori=scan_ori, use_gpu=False,
        )
    logger.info(
        f"Joint Gaussian extraction of {extractor.num_foci} foci "
        f"(sigma {extractor.gaussian_sigma_px:.2f} px, "
        f"{'constant background' if extractor.fit_background else 'no background'}, "
        f"{'GPU' if extractor.use_gpu else 'CPU'})"
    )
    return extractor


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
