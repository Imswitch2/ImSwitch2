"""Python reproduction of the measured 2019 MoNaLISA DLL CPU extraction.

Independent unit-cell Gaussian least-squares fits use original camera pixels,
DLL-style integrated Gaussian coefficients and the broad 9999-nm background.
Unlike the DLL, this module does not depend on Windows or CUDA.  The sparse extraction matrices are ported without mathematical changes from
the WIP DLL-equivalent reference. Full reconstruction also depends on common
placement, calibration and correction settings.
"""

from __future__ import annotations

import numpy as np
from scipy import sparse

# Avoid importing the Windows-DLL reconstructor (and Qt) from the pure math path.
_AXIS_LABEL_MAP = {
    'r_l_text': 'Right-Left', 'u_d_text': 'Up-Down',
    'b_f_text': 'Back-Front', 'timepoints_text': 'Timepoints',
    'p_text': 'pos', 'n_text': 'neg',
}

DLL_PYTHON_PLACEMENT_LEGACY = 'Legacy arithmetic'
DLL_PYTHON_PLACEMENT_COMMON = 'Common recorded scan'
DLL_PYTHON_PLACEMENTS = (
    DLL_PYTHON_PLACEMENT_LEGACY, DLL_PYTHON_PLACEMENT_COMMON,
)


def coefficient_grid_shape(camera_shape, pattern):
    """Historic DLL half-period last-center rule (positive canonical offsets)."""
    height, width = map(int, camera_shape)
    off_y, off_x, per_y, per_x = map(float, pattern)
    if min(height, width) <= 0 or min(per_y, per_x) <= 0:
        raise ValueError('Invalid camera size or DLL focus period')
    if not np.isfinite((off_y, off_x, per_y, per_x)).all():
        raise ValueError('Invalid DLL grid parameters')
    if off_y < 0 or off_x < 0 or off_y >= per_y or off_x >= per_x:
        raise ValueError('DLL grid offsets must be normalized into [0, period)')
    n_rows = 1 + int((height - 1 - off_y) / per_y + .5)
    n_cols = 1 + int((width - 1 - off_x) / per_x + .5)
    if n_rows < 1 or n_cols < 1:
        raise ValueError('DLL grid does not intersect the camera')
    return n_rows, n_cols


def python_dll_coefficients(
    frames: np.ndarray,
    pattern: tuple[float, float, float, float],
    sigmas_px: np.ndarray,
    grid_shape: tuple[int, int],
    *,
    chunk_frames: int = 64,
    _return_matrices: bool = False,
) -> np.ndarray:
    """Fit independent Gaussian bases in non-overlapping DLL-style unit cells.

    Matches the *interior* unit-cell response measured with the DLL impulse
    probe at FWHM 220 and 320 nm.  Border-cell normalization in the compiled
    DLL is still under investigation; discrepancies there are intentionally
    preserved for diagnosis rather than corrected by arbitrary special cases.

    The returned coefficients use integrated Gaussian normalization 2*pi*sigma²,
    including the extremely broad ``9999 nm`` Gaussian used for 'Constant' BG.
    """
    raw = np.asarray(frames)
    if raw.ndim != 3:
        raise ValueError('Expected camera frames shaped (frames, Y, X)')
    if not np.issubdtype(raw.dtype, np.number):
        raise ValueError('Camera frames must be numeric')
    height, width = map(int, raw.shape[1:])
    nr, nc = map(int, grid_shape)
    if min(height, width, nr, nc) < 1:
        raise ValueError('Camera and coefficient dimensions must be positive')
    sy = np.asarray(sigmas_px, dtype=float).ravel()
    if not len(sy) or not np.isfinite(sy).all() or sy[0] <= 0 or np.any(sy < 0):
        raise ValueError('Invalid Gaussian sigmas')
    if len(sy) > 2:
        raise ValueError('Diagnostic supports one signal plus one background basis')
    off_y, off_x, per_y, per_x = map(float, pattern)
    if min(per_y, per_x) <= 0:
        raise ValueError('Pattern periods must be positive')

    # Own each *original camera pixel* using the closest lattice center.
    # Never bilinearly interpolate data or tie the cell size to spot FWHM.
    yi = np.floor((np.arange(height) - off_y) / per_y + 0.5).astype(np.int64)
    xi = np.floor((np.arange(width) - off_x) / per_x + 0.5).astype(np.int64)
    owners = yi[:, None] * nc + xi[None, :]
    valid = ((yi[:, None] >= 0) & (yi[:, None] < nr)
             & (xi[None, :] >= 0) & (xi[None, :] < nc))
    pixel_ids = np.flatnonzero(valid)
    cell_ids = owners.ravel()[pixel_ids]
    sort = np.argsort(cell_ids, kind='stable')
    pixel_ids, cell_ids = pixel_ids[sort], cell_ids[sort]
    edges = np.searchsorted(cell_ids, np.arange(nr*nc+1))

    row_ids = [[] for _ in sy]
    col_ids = [[] for _ in sy]
    weights = [[] for _ in sy]
    for focus in range(nr*nc):
        ix = pixel_ids[edges[focus]:edges[focus+1]]
        if ix.size < len(sy):
            continue
        y, x = np.divmod(ix, width)
        cy = off_y + (focus // nc) * per_y
        cx = off_x + (focus % nc) * per_x
        r2 = (y-cy)**2 + (x-cx)**2
        positive = sy > 0
        basis = np.exp(-r2[:, None] / (2.0 * sy[positive][None, :]**2))
        if basis.shape[1] == 1:
            dual = basis[:, 0] / np.dot(basis[:, 0], basis[:, 0])
            fitted = (dual * (2 * np.pi * sy[0]**2))[None, :]
        else:
            gram = basis.T @ basis
            if np.linalg.cond(gram) > 1e12:
                # Extremely small/cropped cells cannot resolve two bases.
                continue
            fitted = (np.linalg.solve(gram, basis.T)
                      * (2 * np.pi * sy[positive]**2)[:, None])
        for basis_index in range(fitted.shape[0]):
            row_ids[basis_index].extend([focus] * len(ix))
            col_ids[basis_index].extend(ix.tolist())
            weights[basis_index].extend(fitted[basis_index].tolist())

    matrices = [
        sparse.csr_matrix((vals, (rows, cols)),
                          shape=(nr*nc, height*width), dtype=np.float64)
        for rows, cols, vals in zip(row_ids, col_ids, weights)
    ]
    if _return_matrices:
        return matrices
    result = np.zeros((len(sy), raw.shape[0], nr, nc), dtype=np.float32)
    # Avoid unbounded float64 temporary buffers for large camera frames.
    chunk_frames = max(1, min(int(chunk_frames), int(64*1024**2 // max(8*height*width, 1))))
    for start in range(0, raw.shape[0], chunk_frames):
        block = np.asarray(raw[start:start+chunk_frames], dtype=np.float64).reshape(-1, height*width)
        for k, matrix in enumerate(matrices):
            result[k, start:start+len(block)] = np.asarray(block @ matrix.T).reshape(-1, nr, nc)
    return result



class DllFastExtractionOperator:
    """Measured DLL CPU extractor, in the normal pipeline placement pipeline.

    Evaluates the exact same unit-cell weight matrices as
    :func:`python_dll_coefficients`, then maps the DLL grid coefficients back
    onto the current pipeline focus-coordinate order.  The Pipeline retains
    its own orientation and placement; this class never places pixels.

    Gaussian amplitudes are *integrated* (2*pi*sigma**2), unlike Local/Joint.
    ``background='constant'`` is represented by the DLL's 9999-nm wide
    Gaussian basis, which is the implementation of the *Constant* menu choice.
    """

    def __init__(self, x, y, sigma, frame_shape, *, lattice, background="constant",
                 pixel_size_nm=None):
        from .spot_settings import GAUSSIAN_FWHM_TO_SIGMA
        self.frame_shape = tuple(map(int, frame_shape))
        height, width = self.frame_shape
        x, y = np.asarray(x, dtype=float).ravel(), np.asarray(y, dtype=float).ravel()
        if len(x) != len(y) or len(x) == 0:
            raise ValueError("DLL-equivalent needs a nonempty focus grid")
        sigma = np.broadcast_to(np.asarray(sigma, dtype=float), x.shape)
        if not np.all(np.isfinite(sigma)) or np.any(sigma <= 0):
            raise ValueError("DLL-equivalent needs a positive shared sigma")
        if not np.allclose(sigma, sigma[0], rtol=1e-5, atol=1e-8):
            raise ValueError("DLL-equivalent requires one shared Gaussian width")
        if background not in ("none", "constant"):
            raise ValueError("DLL-equivalent supports constant or no background")
        if lattice is None:
            raise ValueError("DLL-equivalent requires a rectangular lattice")
        try:
            period_x, off_x, period_y, off_y = lattice.to_grid_params(tol=0.02)
        except ValueError as exc:
            raise ValueError("DLL-equivalent requires an axis-aligned rectangular lattice") from exc
        self.pattern = (float(off_y), float(off_x), float(period_y), float(period_x))
        self.grid_shape = coefficient_grid_shape(self.frame_shape, self.pattern)
        nr, nc = self.grid_shape
        self.num_foci = len(x)
        self.background = background
        self.joint = False
        self.reach_sigma = np.nan
        self._gpu_weights = None
        # Pipeline's FWHM conversion uses the exact Gaussian constant, but the
        # 2019 DLL uses 2.355. Match the DLL's sigma at a given user FWHM.
        signal_sigma = float(sigma[0]) * GAUSSIAN_FWHM_TO_SIGMA / 2.355
        if background == "constant":
            if pixel_size_nm is None or float(pixel_size_nm) <= 0:
                raise ValueError("DLL-equivalent Constant background needs the camera pixel size")
            bg_sigma = 9999.0 / (2.355 * float(pixel_size_nm))
            sigmas = np.array((signal_sigma, bg_sigma), dtype=float)
        else:
            sigmas = np.array((signal_sigma,), dtype=float)
        self.sigmas_px = sigmas

        # Native DLL grid coefficients are [row, col].  Pipeline foci may include
        # additional margin points, or use a different ordering; map explicitly
        # rather than assuming the two arrays are interchangeable.
        row = np.rint((y - off_y) / period_y).astype(np.int64)
        col = np.rint((x - off_x) / period_x).astype(np.int64)
        inside = (row >= 0) & (row < nr) & (col >= 0) & (col < nc)
        # A half-period tolerance prevents off-grid or staggered localization
        # from being silently assigned to a neighboring canonical DLL focus.
        aligned = ((np.abs(y - (off_y + row * period_y)) < 0.45 * period_y)
                   & (np.abs(x - (off_x + col * period_x)) < 0.45 * period_x))
        self.valid = inside & aligned
        indices = (row * nc + col).astype(np.int64)
        selected = indices[self.valid]
        if not selected.size:
            raise ValueError("DLL-equivalent focus grid does not intersect the camera")
        if len(np.unique(selected)) != len(selected):
            raise ValueError("DLL-equivalent focus grid has duplicate DLL cells")
        self._focus_to_grid = np.where(self.valid, indices, 0).astype(np.int64)
        self.pixels_per_focus = np.zeros(self.num_foci, dtype=np.int64)
        # Build matrices just once, sharing *exactly* the same numerical kernel
        # as the standalone DLL-equivalent reconstructor. No DLL is loaded.
        self._weights = python_dll_coefficients(
            np.empty((0, height, width), dtype=np.uint16),
            self.pattern, sigmas, self.grid_shape, _return_matrices=True,
        )
        for matrix in self._weights:
            self.pixels_per_focus[self.valid] = np.diff(matrix.indptr)[selected]
        if not np.any(self.pixels_per_focus[self.valid] > 0):
            raise ValueError("No DLL-equivalent unit cells contain valid camera pixels")
        self.valid &= self.pixels_per_focus > 0

    @classmethod
    def build(cls, x, y, sigma, frame_shape, *, lattice, background="constant",
              pixel_size_nm=None):
        return cls(x, y, sigma, frame_shape, lattice=lattice,
                   background=background, pixel_size_nm=pixel_size_nm)

    def _extract(self, frames):
        data = np.asarray(frames)
        if data.ndim == 2:
            data = data[None]
        if data.ndim != 3 or tuple(data.shape[-2:]) != self.frame_shape:
            raise ValueError(f"Expected camera frames (N, {self.frame_shape}), got {data.shape}")
        flat = data.reshape(len(data), -1).astype(np.float64, copy=False)
        k = len(data)
        vals = np.full((len(self._weights), k, self.num_foci), np.nan, dtype=np.float32)
        for j, weight in enumerate(self._weights):
            # The sparse model returns [frame, DLL grid cell]; reorder for Fast.
            for start in range(0, k, 32):
                chunk = np.asarray(flat[start:start+32] @ weight.T, dtype=np.float32)
                mapped = np.full((len(chunk), self.num_foci), np.nan, dtype=np.float32)
                mapped[:, self.valid] = chunk[:, self._focus_to_grid[self.valid]]
                vals[j, start:start+len(chunk), :] = mapped
        return vals

    def fast_planes_device(self, frames, device="CPU", *, preselect_host=False):
        # Deliberately CPU numerical reference, even with Fast's GPU selection.
        # GPU placement can still consume these arrays if requested.
        values = self._extract(frames)
        amp, bg = values[0], (values[1] if len(values) > 1 else None)
        if str(device).lower() == "gpu":
            from .extraction import CUPY_AVAILABLE
            if CUPY_AVAILABLE:
                import cupy as cp
                return cp.asarray(amp), (None if bg is None else cp.asarray(bg)), None
        return amp, bg, None

    def apply_fast_device(self, frames, device="CPU", chunk=64, *, preselect_host=False):
        from .extraction import ExtractionResult
        # Keep the host arrays for pipeline placement, even if its GPU device is
        # selected. This diagnostic path prioritizes model equality over speed.
        values = self._extract(frames)
        return ExtractionResult(
            amplitude=values[0], background=values[1] if len(values)>1 else None,
            haze=None, noise_sigma=np.full(values.shape[1], np.nan),
            coefficients=np.empty((values.shape[1], 0), dtype=float),
        )

    def apply_device(self, frames, device="CPU", chunk=64, *, preselect_host=False):
        return self.apply_fast_device(frames, device, chunk, preselect_host=preselect_host)

    @property
    def variance_gain(self):
        raise ValueError("Variance weighting unavailable for DLL-equivalent")
