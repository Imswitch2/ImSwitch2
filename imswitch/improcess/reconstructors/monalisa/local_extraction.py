"""Independent per-focus Gaussian extraction, ported from wip/monalisa-recon2.

The first integration uses native camera pixels and a uniform square/circular
footprint. The existing alternative interpolation and numerical choices are
retained as backend options for reproducible investigation.
"""

from __future__ import annotations

import numpy as np

from .extraction import CUPY_AVAILABLE, ExtractionResult, cp


class LocalExtractionOperator:
    """Historical Fast-Gauss local estimator on arbitrary focus centres.

    Each focus is fitted independently from a configurable local footprint.
    The historical default is a filled 5 x 5 rectangle; a filled circular
    pinhole is also available for comparison. By default, camera data are
    sampled at subpixel focus coordinates using bilinear interpolation.
    The diagnostic ``camera_pixels`` mode instead reads the
    original sensor pixels and evaluates each focus' Gaussian at their true
    displacement from its subpixel centre (as in the historical MATLAB code).
    It retains the selected local footprint, not MATLAB's lattice-unit-cell
    partition; this is a sampling comparison, not a full MATLAB port.

    The local estimator deliberately implements only the coefficient planes
    needed by Fast reconstruction.  It has no joint residual state for
    reassignment or variance diagnostics.
    """

    def __init__(
        self,
        x: np.ndarray,
        y: np.ndarray,
        sigma: float,
        frame_shape: tuple[int, int],
        *,
        background: str = "constant",
        footprint_shape: str = "rectangular",
        footprint_size: int = 5,
        pinhole_radius_px: float = 2.0,
        sampling: str = "bilinear",
        background_basis: str = "constant",
        integrated: bool = False,
        pixel_size_nm: float | None = None,
        lattice=None,
        focus_coordinates: str = "localized",
    ):
        rows, cols = (int(n) for n in frame_shape)
        if background not in ("constant", "none"):
            raise ValueError(
                "Local Fast estimator supports only constant or no background"
            )
        x = np.asarray(x, dtype=float).ravel()
        y = np.asarray(y, dtype=float).ravel()
        if x.size == 0 or x.shape != y.shape:
            raise ValueError("x and y must be non-empty and have the same length")
        sigma = float(sigma)
        if not np.isfinite(sigma) or sigma <= 0:
            raise ValueError("Local Fast estimator needs a finite positive sigma")

        self.frame_shape = (rows, cols)
        self.num_foci = int(x.size)
        self.background = str(background)
        self.focus_coordinates = str(focus_coordinates).strip().lower()
        if self.focus_coordinates not in ("localized", "dll_grid"):
            raise ValueError("Focus coordinates must be localized or dll_grid")
        footprint_shape = str(footprint_shape).strip().lower()
        if footprint_shape not in ("rectangular", "circular", "lattice"):
            raise ValueError("Local footprint must be rectangular, circular, or lattice")
        self.footprint_shape = footprint_shape
        # The DLL's rectangular camera grid is used only as a *coordinate*
        # reference, and (for lattice support) to assign pixel ownership.
        # Extraction weights are always calculated independently below.
        grid = None
        if footprint_shape == "lattice" or self.focus_coordinates == "dll_grid":
            if lattice is None:
                raise ValueError("DLL-grid centres and lattice cells require a lattice")
            try:
                per_x, off_x, per_y, off_y = lattice.to_grid_params(tol=0.02)
            except ValueError as exc:
                raise ValueError("DLL grid requires an axis-aligned rectangular lattice") from exc
            if min(per_x, per_y) <= 0:
                raise ValueError("Invalid DLL grid period")
            nr = 1 + int((rows - 1 - off_y) / per_y + .5)
            nc = 1 + int((cols - 1 - off_x) / per_x + .5)
            if nr < 1 or nc < 1:
                raise ValueError("DLL grid does not intersect the camera")
            ir = np.rint((y - off_y) / per_y).astype(np.int64)
            ic = np.rint((x - off_x) / per_x).astype(np.int64)
            canonical_x, canonical_y = off_x + ic * per_x, off_y + ir * per_y
            inside_grid = (ir >= 0) & (ir < nr) & (ic >= 0) & (ic < nc)
            aligned = ((np.abs(x - canonical_x) < .45 * per_x)
                       & (np.abs(y - canonical_y) < .45 * per_y))
            grid = (per_x, off_x, per_y, off_y, nr, nc, ir, ic, inside_grid & aligned)
            if footprint_shape == "lattice":
                selected = ir[inside_grid & aligned] * nc + ic[inside_grid & aligned]
                if len(np.unique(selected)) != len(selected):
                    raise ValueError("Local lattice support has duplicate focus cells")
            if self.focus_coordinates == "dll_grid":
                x, y = canonical_x, canonical_y
        if background_basis not in ("constant", "broad_gaussian"):
            raise ValueError("Local background basis must be constant or broad_gaussian")
        if background_basis == "broad_gaussian" and background == "constant":
            if pixel_size_nm is None or float(pixel_size_nm) <= 0:
                raise ValueError("Broad Gaussian background needs camera pixel size")
            bg_sigma = 9999.0 / (2.355 * float(pixel_size_nm))
        else:
            bg_sigma = None
        self.background_basis = background_basis
        self.integrated = bool(integrated)
        amp_scale = (2 * np.pi * sigma**2) if integrated else 1.0
        bg_scale = ((2 * np.pi * bg_sigma**2) if integrated and bg_sigma is not None else 1.0)
        self.sampling = str(sampling).strip().lower()
        if self.sampling not in ("bilinear", "camera_pixels"):
            raise ValueError("Local sampling must be 'bilinear' or 'camera_pixels'")
        if footprint_shape == "lattice":
            # Unit cells are defined in the original camera grid. The selected
            # sampling mode must not change their pixel ownership.
            self.sampling = "camera_pixels"
        self.joint = False
        self.reach_sigma = np.nan
        self.valid = (
            (x >= -0.5) & (x < cols - 0.5)
            & (y >= -0.5) & (y < rows - 0.5)
        ) if grid is None or footprint_shape != "lattice" else grid[-1].copy()
        if not self.valid.any():
            raise ValueError("No local focus intersects the camera grid")
        footprint_size = int(footprint_size)
        pinhole_radius_px = float(pinhole_radius_px)
        if footprint_shape == "rectangular":
            if footprint_size < 1 or footprint_size % 2 == 0:
                raise ValueError(
                    "Local rectangular footprint size must be a positive odd integer"
                )
        elif not np.isfinite(pinhole_radius_px) or pinhole_radius_px <= 0:
            raise ValueError("Local circular footprint radius must be finite and > 0")

        self.footprint_shape = footprint_shape
        self.footprint_size = footprint_size
        self.pinhole_radius_px = pinhole_radius_px
        if footprint_shape == "lattice":
            # Independent Local Gaussian+background normal-equation solve, not
            # DllFastExtractionOperator or python_dll_coefficients. The only
            # DLL convention is geometric: every sensor pixel belongs to its
            # nearest canonical focus cell (including the rectangular edges).
            per_x, off_x, per_y, off_y, nr, nc, ir, ic, assigned = grid
            yy = np.floor((np.arange(rows) - off_y) / per_y + .5).astype(np.int64)
            xx = np.floor((np.arange(cols) - off_x) / per_x + .5).astype(np.int64)
            eligible = ((yy[:, None] >= 0) & (yy[:, None] < nr)
                        & (xx[None, :] >= 0) & (xx[None, :] < nc))
            owners = yy[:, None] * nc + xx[None, :]
            pixel_ids = np.flatnonzero(eligible)
            owner_ids = owners.ravel()[pixel_ids]
            order = np.argsort(owner_ids, kind="stable")
            pixel_ids, owner_ids = pixel_ids[order], owner_ids[order]
            edges = np.searchsorted(owner_ids, np.arange(nr * nc + 1))
            indices = np.where(assigned, ir * nc + ic, 0).astype(np.int64)
            lengths = np.where(assigned, edges[indices + 1] - edges[indices], 0)
            width = int(np.max(lengths))
            if width < 1:
                raise ValueError("No local lattice cell contains camera pixels")
            pixel_index = np.zeros((self.num_foci, width), dtype=np.int64)
            keep = np.zeros((self.num_foci, width), dtype=bool)
            for focus in np.flatnonzero(assigned & (lengths > 0)):
                pids = pixel_ids[edges[indices[focus]]:edges[indices[focus] + 1]]
                pixel_index[focus, :len(pids)] = pids
                keep[focus, :len(pids)] = True
            py, px = np.divmod(pixel_index, cols)
            dist2 = (px - x[:, None]) ** 2 + (py - y[:, None]) ** 2
            gaussian = np.exp(-dist2 / (2 * sigma**2)) * keep
            energy = np.sum(gaussian**2, axis=1)
            if background == "constant":
                basis = (np.exp(-dist2 / (2 * bg_sigma**2)) * keep
                         if bg_sigma is not None else keep.astype(float))
                mixed = np.sum(gaussian * basis, axis=1)
                bg_energy = np.sum(basis**2, axis=1)
                determinant = bg_energy * energy - mixed**2
                usable = (lengths >= 2) & (determinant > 1e-12)
                denom = np.where(usable, determinant, 1.0)[:, None]
                self._native_amplitude_weights = np.where(
                    usable[:, None],
                    (bg_energy[:, None] * gaussian - mixed[:, None] * basis)
                    / denom * amp_scale, 0.0,
                )
                self._native_background_weights = np.where(
                    usable[:, None],
                    (energy[:, None] * basis - mixed[:, None] * gaussian)
                    / denom * bg_scale, 0.0,
                )
            else:
                usable = (lengths >= 1) & (energy > 1e-12)
                self._native_amplitude_weights = np.where(
                    usable[:, None], gaussian / np.where(usable, energy, 1.)[:, None]
                    * amp_scale, 0.0,
                )
                self._native_background_weights = None
            self._native_pixel_index = pixel_index
            self.valid &= usable
            self.pixels_per_focus = np.where(self.valid, lengths, 0).astype(int)
            self._points_per_focus = width
            self._device_arrays = None
            return
        if footprint_shape == "rectangular":
            half_width = footprint_size // 2
            coords = np.arange(-half_width, half_width + 1, dtype=float)
            ox, oy = np.meshgrid(coords, coords, indexing="xy")
            offset_x = ox.ravel()
            offset_y = oy.ravel()
        else:
            half_width = int(np.ceil(pinhole_radius_px))
            coords = np.arange(-half_width, half_width + 1, dtype=float)
            ox, oy = np.meshgrid(coords, coords, indexing="xy")
            if self.sampling == "bilinear":
                keep = (ox**2 + oy**2) <= pinhole_radius_px**2
                offset_x = ox[keep].ravel()
                offset_y = oy[keep].ravel()
            else:
                # Candidate square must include sensor pixels whose true
                # subpixel distance lies inside the disc, even if their
                # integer offset is outside a disc centred at the origin.
                offset_x = ox.ravel()
                offset_y = oy.ravel()

        self._offset_x = offset_x
        self._offset_y = offset_y
        self._points_per_focus = int(self._offset_x.size)
        self.pixels_per_focus = np.where(
            self.valid, self._points_per_focus, 0
        ).astype(int)

        if self.sampling == "camera_pixels":
            # Sensor-grid samples are not translated/interpolated.  The nearest
            # integer pixel anchors the footprint, but Gaussian weights use the
            # *actual* pixel-to-subpixel-centre distances, per focus.
            px = np.rint(x).astype(np.int64)[:, None] + self._offset_x.astype(np.int64)
            py = np.rint(y).astype(np.int64)[:, None] + self._offset_y.astype(np.int64)
            keep = (px >= 0) & (px < cols) & (py >= 0) & (py < rows)
            dist2 = (px - x[:, None]) ** 2 + (py - y[:, None]) ** 2
            if footprint_shape == "circular":
                keep &= dist2 <= pinhole_radius_px**2
            self._native_pixel_index = (
                np.clip(py, 0, rows - 1) * cols + np.clip(px, 0, cols - 1)
            ).astype(np.int64)
            gaussian = np.exp(-dist2 / (2.0 * sigma**2)) * keep
            count = keep.sum(axis=1)
            energy = np.sum(gaussian**2, axis=1)
            if background == "constant":
                bg_basis = (np.exp(-dist2 / (2.0 * bg_sigma**2)) * keep
                            if bg_sigma is not None else keep.astype(float))
                summed = np.sum(gaussian * bg_basis, axis=1)
                bg_energy = np.sum(bg_basis**2, axis=1)
                determinant = bg_energy * energy - summed**2
                fit_valid = (count >= 2) & (determinant > 1e-12)
                denom = np.where(fit_valid, determinant, 1.0)[:, None]
                self._native_amplitude_weights = np.where(
                    fit_valid[:, None],
                    (bg_energy[:, None] * gaussian - summed[:, None] * bg_basis) / denom
                    * amp_scale,
                    0.0,
                )
                self._native_background_weights = np.where(
                    fit_valid[:, None],
                    (energy[:, None] * bg_basis - summed[:, None] * gaussian) / denom
                    * bg_scale,
                    0.0,
                )
            else:
                fit_valid = (count >= 1) & (energy > 1e-12)
                self._native_amplitude_weights = np.where(
                    fit_valid[:, None],
                    gaussian / np.where(fit_valid, energy, 1.0)[:, None] * amp_scale,
                    0.0,
                )
                self._native_background_weights = None
            self.valid &= fit_valid
            self.pixels_per_focus = np.where(self.valid, count, 0).astype(int)
        else:
            # Historical get_interp_coords() clips the requested sample
            # coordinate before bilinear interpolation. Leave this path intact.
            xi = np.clip(x[:, None] + self._offset_x[None, :], 0.0, cols - 1.0)
            yi = np.clip(y[:, None] + self._offset_y[None, :], 0.0, rows - 1.0)
            self._x0 = np.floor(xi).astype(np.int32)
            self._y0 = np.floor(yi).astype(np.int32)
            self._x1 = np.clip(self._x0 + 1, 0, cols - 1)
            self._y1 = np.clip(self._y0 + 1, 0, rows - 1)
            self._dx = xi - self._x0
            self._dy = yi - self._y0
            gaussian = np.exp(
                -(self._offset_x**2 + self._offset_y**2) / (2.0 * sigma**2)
            )
            if background == "constant":
                # One common set of weights for each bilinearly shifted footprint.
                bg_basis = (np.exp(-(self._offset_x**2 + self._offset_y**2) /
                                   (2.0 * bg_sigma**2)) if bg_sigma is not None
                            else np.ones_like(gaussian))
                pinv = np.linalg.pinv(np.stack([gaussian, bg_basis]))
                self._amplitude_weights = np.asarray(pinv[:, 0] * amp_scale, dtype=float)
                self._background_weights = np.asarray(pinv[:, 1] * bg_scale, dtype=float)
            else:
                denom = float(np.dot(gaussian, gaussian))
                self._amplitude_weights = np.asarray(gaussian / denom * amp_scale, dtype=float)
                self._background_weights = None
        self._device_arrays = None

    @classmethod
    def build(
        cls,
        x: np.ndarray,
        y: np.ndarray,
        sigma: float | np.ndarray,
        frame_shape: tuple[int, int],
        *,
        background: str = "constant",
        footprint_shape: str = "rectangular",
        footprint_size: int = 5,
        pinhole_radius_px: float = 2.0,
        sampling: str = "bilinear",
        background_basis: str = "constant",
        integrated: bool = False,
        pixel_size_nm: float | None = None,
        lattice=None,
        focus_coordinates: str = "localized",
        **_ignored,
    ) -> "LocalExtractionOperator":
        sigma_values = np.broadcast_to(np.asarray(sigma, dtype=float), np.asarray(x).shape)
        if not np.allclose(sigma_values, sigma_values.flat[0], rtol=1e-10, atol=1e-12):
            raise ValueError(
                "Local Fast estimator requires one shared Gaussian spot width"
            )
        return cls(
            x, y, float(sigma_values.flat[0]), frame_shape,
            background=background, footprint_shape=footprint_shape,
            footprint_size=footprint_size, pinhole_radius_px=pinhole_radius_px,
            sampling=sampling, background_basis=background_basis,
            integrated=integrated, pixel_size_nm=pixel_size_nm,
            lattice=lattice, focus_coordinates=focus_coordinates,
        )

    def _interpolate_numpy(self, frames: np.ndarray) -> np.ndarray:
        frames = np.asarray(frames)
        if frames.ndim == 2:
            frames = frames[None]
        if frames.ndim != 3 or tuple(frames.shape[1:]) != self.frame_shape:
            raise ValueError(
                f"Expected frames of shape (K, {self.frame_shape[0]}, "
                f"{self.frame_shape[1]}), got {frames.shape}"
            )
        if self.sampling == "camera_pixels":
            return frames.reshape(frames.shape[0], -1)[:, self._native_pixel_index]
        values = (
            frames[:, self._y0, self._x0] * (1.0 - self._dx) * (1.0 - self._dy)
            + frames[:, self._y1, self._x0] * (1.0 - self._dx) * self._dy
            + frames[:, self._y0, self._x1] * self._dx * (1.0 - self._dy)
            + frames[:, self._y1, self._x1] * self._dx * self._dy
        )
        return values

    def _result_numpy(self, frames: np.ndarray) -> ExtractionResult:
        samples = self._interpolate_numpy(frames)
        if self.sampling == "camera_pixels":
            amplitude = np.einsum(
                "kfp,fp->kf", samples, self._native_amplitude_weights, optimize=True
            )
            background = (
                None if self._native_background_weights is None else
                np.einsum(
                    "kfp,fp->kf", samples, self._native_background_weights, optimize=True
                )
            )
        else:
            amplitude = samples @ self._amplitude_weights
            background = (
                None
                if self._background_weights is None
                else samples @ self._background_weights
            )
        amplitude = np.asarray(amplitude, dtype=float)
        amplitude[:, ~self.valid] = np.nan
        if background is not None:
            background = np.asarray(background, dtype=float)
            background[:, ~self.valid] = np.nan
        count = int(amplitude.shape[0])
        return ExtractionResult(
            amplitude=amplitude,
            background=background,
            haze=None,
            noise_sigma=np.full(count, np.nan),
            coefficients=np.empty((count, 0), dtype=float),
        )

    def _ensure_device_arrays(self):
        if not CUPY_AVAILABLE:
            return None
        if self._device_arrays is None:
            if self.sampling == "camera_pixels":
                self._device_arrays = (
                    cp.asarray(self._native_pixel_index),
                    cp.asarray(self._native_amplitude_weights),
                    cp.asarray(self.valid),
                )
                if self._native_background_weights is not None:
                    self._device_arrays += (cp.asarray(self._native_background_weights),)
                return self._device_arrays
            self._device_arrays = tuple(
                cp.asarray(value)
                for value in (
                    self._x0, self._x1, self._y0, self._y1,
                    self._dx, self._dy, self._amplitude_weights,
                    self.valid,
                )
            )
            if self._background_weights is not None:
                self._device_arrays += (cp.asarray(self._background_weights),)
        return self._device_arrays

    def _native_planes_device(self, chunk, arrays):
        pixel_index, amp_weights, valid, *rest = arrays
        samples = chunk.reshape(chunk.shape[0], -1)[:, pixel_index]
        amplitude = cp.where(
            valid[None, :], cp.sum(samples * amp_weights[None, :, :], axis=-1), cp.nan
        )
        background = None
        if rest:
            background = cp.where(
                valid[None, :], cp.sum(samples * rest[0][None, :, :], axis=-1), cp.nan
            )
        return amplitude, background

    def _result_gpu(self, frames) -> ExtractionResult:
        arrays = self._ensure_device_arrays()
        chunk = cp.asarray(frames)
        if chunk.ndim == 2:
            chunk = chunk[None]
        if chunk.ndim != 3 or tuple(chunk.shape[1:]) != self.frame_shape:
            raise ValueError(
                f"Expected frames of shape (K, {self.frame_shape[0]}, "
                f"{self.frame_shape[1]}), got {tuple(chunk.shape)}"
            )
        if self.sampling == "camera_pixels":
            amplitude, background = self._native_planes_device(chunk, arrays)
        else:
            x0, x1, y0, y1, dx, dy, amp_w, valid, *rest = arrays
            values = (
                chunk[:, y0, x0] * (1.0 - dx) * (1.0 - dy)
                + chunk[:, y1, x0] * (1.0 - dx) * dy
                + chunk[:, y0, x1] * dx * (1.0 - dy)
                + chunk[:, y1, x1] * dx * dy
            )
            amplitude = values @ amp_w
            amplitude = cp.where(valid[None, :], amplitude, cp.nan)
            background = None
            if rest:
                background = values @ rest[0]
                background = cp.where(valid[None, :], background, cp.nan)
        count = int(amplitude.shape[0])
        return ExtractionResult(
            amplitude=cp.asnumpy(amplitude),
            background=None if background is None else cp.asnumpy(background),
            haze=None,
            noise_sigma=np.full(count, np.nan),
            coefficients=np.empty((count, 0), dtype=float),
        )

    def fast_planes_device(self, frames, device: str = "GPU", *, preselect_host=False):
        """Historical Local estimator planes retained on the GPU."""
        choice = str(device or "GPU").strip().lower()
        if choice != "gpu" or not CUPY_AVAILABLE:
            raise RuntimeError("GPU-resident extraction requires CuPy/GPU")
        arrays = self._ensure_device_arrays()
        chunk = cp.asarray(frames)
        if chunk.ndim == 2:
            chunk = chunk[None]
        if chunk.ndim != 3 or tuple(chunk.shape[1:]) != self.frame_shape:
            raise ValueError(
                f"Expected frames of shape (K, {self.frame_shape[0]}, "
                f"{self.frame_shape[1]}), got {tuple(chunk.shape)}"
            )
        if self.sampling == "camera_pixels":
            amplitude, background = self._native_planes_device(chunk, arrays)
            return amplitude, background, None
        x0, x1, y0, y1, dx, dy, amp_w, valid, *rest = arrays
        values = (
            chunk[:, y0, x0] * (1.0 - dx) * (1.0 - dy)
            + chunk[:, y1, x0] * (1.0 - dx) * dy
            + chunk[:, y0, x1] * dx * (1.0 - dy)
            + chunk[:, y1, x1] * dx * dy
        )
        amplitude = cp.where(valid[None, :], values @ amp_w, cp.nan)
        background = None
        if rest:
            background = cp.where(valid[None, :], values @ rest[0], cp.nan)
        return amplitude, background, None

    def apply_fast_device(
        self, frames, device: str = "CPU", chunk: int = 64, *,
        preselect_host: bool = False,
    ) -> ExtractionResult:
        choice = str(device or "CPU").strip().lower()
        if choice not in ("cpu", "gpu"):
            raise ValueError(f"Unknown extraction device {device!r}")
        if choice == "gpu" and CUPY_AVAILABLE:
            return self._result_gpu(frames)
        return self._result_numpy(frames)

    def apply_device(
        self, frames, device: str = "CPU", chunk: int = 64, *,
        preselect_host: bool = False,
    ) -> ExtractionResult:
        return self.apply_fast_device(
            frames, device=device, chunk=chunk, preselect_host=preselect_host
        )

    @property
    def variance_gain(self) -> np.ndarray:
        raise ValueError(
            "Variance weighting is unavailable with the Local Fast estimator"
        )
