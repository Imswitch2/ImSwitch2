"""Pure table operations on localization recarrays (SMLM Phase 6).

Every function here is a pure numpy transformation on the canonical
localization schema (:mod:`imswitch.improcess.model.localization_schema`),
with no Qt or result-object dependencies, so the table processors stay thin
wrappers and the algorithms are unit-testable in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from imswitch.improcess.analysis.smlm_render import render_xy
from imswitch.improcess.model.localization_schema import as_localizations


# --- filtering -------------------------------------------------------------


def filter_localizations(
    locs,
    *,
    min_photons: float | None = None,
    max_photons: float | None = None,
    min_sigma_nm: float | None = None,
    max_sigma_nm: float | None = None,
    min_frame: int | None = None,
    max_frame: int | None = None,
) -> tuple[np.recarray, np.ndarray]:
    """Filter a localization table by photon count, lateral sigma and frame.

    ``None`` disables a bound. The sigma bounds apply to both lateral sigmas
    (``sigma_x_nm`` and ``sigma_y_nm`` must each be inside the range).

    Returns ``(filtered, mask)`` where ``mask`` is the boolean keep-mask over
    the input rows.
    """
    table = as_localizations(locs)
    mask = np.ones(len(table), dtype=bool)
    if min_photons is not None:
        mask &= table.photons >= float(min_photons)
    if max_photons is not None:
        mask &= table.photons <= float(max_photons)
    if min_sigma_nm is not None:
        mask &= (table.sigma_x_nm >= float(min_sigma_nm)) & (
            table.sigma_y_nm >= float(min_sigma_nm)
        )
    if max_sigma_nm is not None:
        mask &= (table.sigma_x_nm <= float(max_sigma_nm)) & (
            table.sigma_y_nm <= float(max_sigma_nm)
        )
    if min_frame is not None:
        mask &= table.frame >= int(min_frame)
    if max_frame is not None:
        mask &= table.frame <= int(max_frame)
    return table[mask].copy().view(np.recarray), mask


# --- drift estimation ------------------------------------------------------


@dataclass(frozen=True)
class DriftEstimate:
    """Per-frame lateral drift estimated from temporal segments.

    ``frames`` spans every frame between the first and last localization;
    ``drift_x_nm``/``drift_y_nm`` are the estimated drift at each of those
    frames, relative to the first (reference) segment. ``segment_centers``
    and ``segment_dx_nm``/``segment_dy_nm`` are the raw per-segment anchors
    the per-frame curves were interpolated from.
    """

    frames: np.ndarray
    drift_x_nm: np.ndarray
    drift_y_nm: np.ndarray
    segment_centers: np.ndarray
    segment_dx_nm: np.ndarray
    segment_dy_nm: np.ndarray
    render_pixel_size_nm: float


def _subpixel_peak(corr: np.ndarray) -> tuple[float, float]:
    """Peak position of a circular cross-correlation with parabolic
    refinement per axis, unwrapped to signed shifts."""
    peak = np.unravel_index(int(np.argmax(corr)), corr.shape)
    shifts = []
    for axis, index in enumerate(peak):
        n = corr.shape[axis]
        take = [peak[0], peak[1]]
        take[axis] = (index - 1) % n
        c_prev = corr[tuple(take)]
        take[axis] = (index + 1) % n
        c_next = corr[tuple(take)]
        c_mid = corr[peak]
        denom = c_prev - 2.0 * c_mid + c_next
        offset = 0.5 * (c_prev - c_next) / denom if denom != 0 else 0.0
        # Parabolic offset is only trustworthy inside one bin.
        offset = float(np.clip(offset, -0.5, 0.5))
        shift = index + offset
        if shift > n / 2:
            shift -= n
        shifts.append(float(shift))
    return shifts[0], shifts[1]


def cross_correlation_shift(reference: np.ndarray, image: np.ndarray) -> tuple[float, float]:
    """Estimate the (dy, dx) shift of ``image`` relative to ``reference``.

    Positive values mean the content of ``image`` sits at larger row/column
    coordinates than in ``reference`` (i.e. ``image ≈ reference`` shifted by
    ``(+dy, +dx)``). Uses FFT circular cross-correlation with parabolic
    subpixel refinement.
    """
    ref = np.asarray(reference, dtype=np.float64)
    img = np.asarray(image, dtype=np.float64)
    if ref.shape != img.shape:
        raise ValueError("reference and image must have the same shape")
    ref = ref - ref.mean()
    img = img - img.mean()
    corr = np.fft.irfft2(
        np.fft.rfft2(img) * np.conj(np.fft.rfft2(ref)), s=ref.shape
    )
    return _subpixel_peak(corr)


def estimate_drift(
    locs,
    *,
    segments: int = 10,
    render_pixel_size_nm: float = 30.0,
) -> DriftEstimate:
    """Estimate lateral drift by cross-correlating temporal segment renders.

    The frame range is split into ``segments`` equal bins; each bin is
    rendered as a 2D histogram on a shared grid and cross-correlated against
    the first non-empty bin. Per-frame drift is interpolated linearly between
    segment centers (clamped at the ends).
    """
    table = as_localizations(locs)
    if len(table) == 0:
        raise ValueError("Cannot estimate drift from an empty localization table")
    if segments < 2:
        raise ValueError("Drift estimation needs at least 2 temporal segments")
    if render_pixel_size_nm <= 0:
        raise ValueError("render_pixel_size_nm must be positive")

    frame_min = int(table.frame.min())
    frame_max = int(table.frame.max())
    if frame_max - frame_min + 1 < segments:
        raise ValueError(
            f"Frame span {frame_max - frame_min + 1} is too short for "
            f"{segments} segments"
        )

    edges = np.linspace(frame_min, frame_max + 1, segments + 1)
    bounds = (
        float(table.x_nm.min()),
        float(table.x_nm.max()),
        float(table.y_nm.min()),
        float(table.y_nm.max()),
    )

    centers = []
    renders = []
    for index in range(segments):
        in_bin = (table.frame >= edges[index]) & (table.frame < edges[index + 1])
        if not np.any(in_bin):
            continue
        image, _grid = render_xy(
            table.x_nm[in_bin],
            table.y_nm[in_bin],
            pixel_size_nm=render_pixel_size_nm,
            render_type="histogram",
            bounds=bounds,
        )
        centers.append(0.5 * (edges[index] + edges[index + 1]))
        renders.append(image)

    if len(renders) < 2:
        raise ValueError(
            "Drift estimation needs at least 2 non-empty temporal segments"
        )

    reference = renders[0]
    dy_nm = np.zeros(len(renders))
    dx_nm = np.zeros(len(renders))
    for index in range(1, len(renders)):
        dy_px, dx_px = cross_correlation_shift(reference, renders[index])
        dy_nm[index] = dy_px * render_pixel_size_nm
        dx_nm[index] = dx_px * render_pixel_size_nm

    frames = np.arange(frame_min, frame_max + 1)
    centers_arr = np.asarray(centers, dtype=np.float64)
    return DriftEstimate(
        frames=frames,
        drift_x_nm=np.interp(frames, centers_arr, dx_nm),
        drift_y_nm=np.interp(frames, centers_arr, dy_nm),
        segment_centers=centers_arr,
        segment_dx_nm=dx_nm,
        segment_dy_nm=dy_nm,
        render_pixel_size_nm=float(render_pixel_size_nm),
    )


def apply_drift(locs, estimate: DriftEstimate) -> np.recarray:
    """Return a drift-corrected copy of ``locs`` (positions minus drift)."""
    table = as_localizations(locs).copy().view(np.recarray)
    frames = table.frame.astype(np.float64)
    table.x_nm = table.x_nm - np.interp(
        frames, estimate.frames, estimate.drift_x_nm
    ).astype(np.float32)
    table.y_nm = table.y_nm - np.interp(
        frames, estimate.frames, estimate.drift_y_nm
    ).astype(np.float32)
    return table


# --- grouping / linking ----------------------------------------------------


def link_localizations(
    locs,
    *,
    radius_nm: float,
    max_dark_frames: int = 0,
) -> np.recarray:
    """Merge localizations of the same emitter across consecutive frames.

    A localization joins an existing chain when it lies within ``radius_nm``
    of the chain's last position and the chain was last seen at most
    ``max_dark_frames`` frames before the previous frame (0 = strictly
    consecutive). Each chain merges into one localization: photon-weighted
    mean position and sigmas, summed photons, first frame.
    """
    from scipy.spatial import cKDTree

    if radius_nm <= 0:
        raise ValueError("radius_nm must be positive")
    if max_dark_frames < 0:
        raise ValueError("max_dark_frames must be non-negative")

    table = as_localizations(locs)
    if len(table) == 0:
        return table.copy().view(np.recarray)

    order = np.argsort(table.frame, kind="stable")
    chains: list[list[int]] = []
    last_frame: list[int] = []
    last_xy: list[tuple[float, float]] = []
    open_chains: list[int] = []

    unique_frames = np.unique(table.frame)
    row_by_frame = {
        int(frame): order[table.frame[order] == frame] for frame in unique_frames
    }

    for frame in (int(value) for value in unique_frames):
        open_chains = [
            chain_id
            for chain_id in open_chains
            if frame - last_frame[chain_id] <= max_dark_frames + 1
        ]
        rows = row_by_frame[frame]
        matched_this_frame: set[int] = set()
        if open_chains:
            tree = cKDTree([last_xy[chain_id] for chain_id in open_chains])
            points = np.column_stack(
                [table.x_nm[rows], table.y_nm[rows]]
            ).astype(np.float64)
            distances, nearest = tree.query(points, k=1)
            # Assign closest points first so one chain never absorbs a
            # farther localization while a closer one starts a new chain.
            for point_index in np.argsort(distances):
                row = int(rows[point_index])
                chain_id = open_chains[int(nearest[point_index])]
                if (
                    distances[point_index] <= radius_nm
                    and chain_id not in matched_this_frame
                ):
                    chains[chain_id].append(row)
                    last_frame[chain_id] = frame
                    last_xy[chain_id] = (
                        float(table.x_nm[row]),
                        float(table.y_nm[row]),
                    )
                    matched_this_frame.add(chain_id)
                else:
                    _start_chain(
                        chains, last_frame, last_xy, open_chains, table, row, frame
                    )
        else:
            for row in rows:
                _start_chain(
                    chains, last_frame, last_xy, open_chains, table, int(row), frame
                )

    merged = table[: len(chains)].copy().view(np.recarray)
    for chain_index, chain_rows in enumerate(chains):
        rows = np.asarray(chain_rows)
        photons = table.photons[rows].astype(np.float64)
        weights = np.where(photons > 0, photons, 1.0)
        total = weights.sum()
        for column in ("x_nm", "y_nm", "z_nm", "sigma_x_nm", "sigma_y_nm", "sigma_z_nm"):
            merged[column][chain_index] = (
                np.asarray(table[column][rows], dtype=np.float64) * weights
            ).sum() / total
        merged.photons[chain_index] = photons.sum()
        merged.frame[chain_index] = table.frame[rows].min()
    return merged


def _start_chain(chains, last_frame, last_xy, open_chains, table, row, frame):
    chain_id = len(chains)
    chains.append([row])
    last_frame.append(frame)
    last_xy.append((float(table.x_nm[row]), float(table.y_nm[row])))
    open_chains.append(chain_id)


__all__ = [
    "DriftEstimate",
    "apply_drift",
    "cross_correlation_shift",
    "estimate_drift",
    "filter_localizations",
    "link_localizations",
]
