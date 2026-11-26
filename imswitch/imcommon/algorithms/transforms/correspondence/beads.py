"""Sparse beads: localise them in each image, pair them without a prior.

The bead strategy for two images of the same sparse, random bead field taken
by two different detectors -- a widefield camera and a point scanner, say --
whose relation is unknown: any rotation, either handedness (one may be the
mirror image of the other), any scale, any shift, and fields of view that
only partly overlap (a scan usually covers a sub-region of the camera field).

* **Localisation** (:func:`detect_beads`, one image at a time): background
  removed with a wide Gaussian, a difference-of-Gaussians band-pass matched to
  the PSF, local maxima above ``threshold`` robust noise levels (MAD), then a
  symmetric 2-D Gaussian fit per bead for the sub-pixel centre and its width.
  The PSF sigma is measured from the brightest peaks when not given, so the
  two images may have different PSFs, backgrounds and SNR. Rejected and
  counted: saturated beads, beads too near the edge for a fit, beads with a
  neighbour closer than ``min_separation_sigma`` sigmas (merged pairs), and
  fits whose width is far from the median (aggregates, debris).
* **Pairing** (:func:`match_point_sets`): every bead and pairs of its nearest
  neighbours form triangles; their side-length ratios do not change under
  rotation, scale or mirroring, so triangles are matched across the two sets
  by those ratios. Each match proposes a similarity (both handednesses are
  tried), every proposal is scored by how many target beads it lands within
  ``tolerance`` of a source bead, the best are refined by iterating
  mutual-nearest-neighbour pairing and a least-squares similarity, and the
  winner is checked against the best *different* refined proposal: a second
  placement nearly as good means the pattern is regular or symmetric (a grid,
  rows of beads), and the pairing is refused as ambiguous.

Points are ``(row, col)``, pixel-centred, as everywhere in this module. The
pairing works on physical coordinates: each image's points are multiplied by
its ``pixel_size_row`` / ``pixel_size_col`` parameters first (1 = pixels), so
an image with non-square pixels (a scan with different steps per axis) is
matched in its true geometry; the pairs come back in pixels.

Qt-free; numpy and scipy only.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np
from scipy import ndimage, optimize, spatial

from ..parameters import ParameterSpec
from .registry import (
    CorrespondenceResult,
    CorrespondenceStrategySpec,
    ImageLocalizationResult,
    LocalizationDiagnostics,
    LocalizationFailure,
)

__all__ = [
    "BeadDetections",
    "BeadMatch",
    "BeadMatchError",
    "bead_strategy_spec",
    "detect_beads",
    "match_point_sets",
    "similarity_from_points",
]


class BeadMatchError(ValueError):
    """The two bead sets cannot be paired trustworthily."""


# --------------------------------------------------------------------------
# localisation
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class BeadDetections:
    """Beads in one image: ``positions`` ``(N, 2)`` (row, col) px, brightest
    first; ``amplitudes`` and ``sigmas`` (px) from the fit; ``sigma_px`` the
    PSF sigma used; ``noise`` the band-passed noise; ``rejected`` reason ->
    count; ``candidates`` every peak above threshold (for inspection)."""

    positions: np.ndarray
    amplitudes: np.ndarray
    sigmas: np.ndarray
    sigma_px: float
    noise: float
    rejected: Mapping[str, int] = field(default_factory=dict)
    candidates: np.ndarray | None = None

    def __len__(self) -> int:
        return int(self.positions.shape[0])


def _robust_noise(values: np.ndarray) -> float:
    values = np.asarray(values, dtype=np.float64).ravel()
    noise = 1.4826 * float(np.median(np.abs(values - np.median(values))))
    return noise if noise > 0 else (float(np.std(values)) or 1.0)


def _bandpass(image: np.ndarray, sigma: float) -> np.ndarray:
    smooth = ndimage.gaussian_filter(image, max(sigma, 0.5), mode="nearest")
    background = ndimage.gaussian_filter(image, max(4.0 * sigma, 3.0), mode="nearest")
    return smooth - background


def _gauss2d(params, rows, cols):
    amp, r0, c0, sigma, offset = params
    return offset + amp * np.exp(-((rows - r0) ** 2 + (cols - c0) ** 2) / (2.0 * sigma * sigma))


def _fit_spot(image: np.ndarray, r: int, c: int, sigma: float):
    """Symmetric 2-D Gaussian + offset around the integer peak ``(r, c)``:
    ``(row, col, amplitude, sigma)`` or ``None``."""
    half = max(2, int(math.ceil(2.5 * sigma)))
    r_lo, r_hi, c_lo, c_hi = r - half, r + half + 1, c - half, c + half + 1
    if r_lo < 0 or c_lo < 0 or r_hi > image.shape[0] or c_hi > image.shape[1]:
        return None
    window = image[r_lo:r_hi, c_lo:c_hi]
    rows, cols = np.mgrid[r_lo:r_hi, c_lo:c_hi].astype(np.float64)
    offset0 = float(np.percentile(window, 20))
    amp0 = float(image[r, c] - offset0)
    if amp0 <= 0:
        return None

    def residual(p):
        return (_gauss2d(p, rows, cols) - window).ravel()

    try:
        result = optimize.least_squares(
            residual, (amp0, float(r), float(c), float(sigma), offset0),
            bounds=((0.0, r_lo - 0.5, c_lo - 0.5, 0.25, -np.inf),
                    (np.inf, r_hi - 0.5, c_hi - 0.5, 4.0 * half, np.inf)),
            method="trf", max_nfev=200)
    except (ValueError, np.linalg.LinAlgError):
        return None
    amp, r0, c0, s, _ = result.x
    reach = 1.5 * max(1.0, sigma)
    if not result.success or abs(r0 - r) > reach or abs(c0 - c) > reach:
        return None
    return float(r0), float(c0), float(amp), float(abs(s))


def _candidates(image: np.ndarray, sigma: float, threshold: float):
    band = _bandpass(image, sigma)
    noise = _robust_noise(band)
    size = max(3, 2 * int(math.ceil(1.5 * sigma)) + 1)
    peaks = (band == ndimage.maximum_filter(band, size=size, mode="nearest")) & (band > threshold * noise)
    rows, cols = np.nonzero(peaks)
    order = np.argsort(-band[rows, cols])
    return rows[order], cols[order], noise


def _measure_sigma(image: np.ndarray, guess: float, threshold: float) -> float:
    sigma = guess
    for _ in range(2):
        rows, cols, _ = _candidates(image, sigma, threshold)
        widths = [fit[3] for r, c in zip(rows[:40], cols[:40])
                  if (fit := _fit_spot(image, int(r), int(c), sigma)) is not None]
        if len(widths) < 3:
            break
        sigma = float(np.median(widths))
    return max(0.5, sigma)


def detect_beads(image: Any, *, sigma_px: float | None = None, threshold: float = 6.0,
                 saturation: float | None = None, min_separation_sigma: float = 4.0,
                 edge_sigma: float = 2.5, width_tolerance: float = 0.5,
                 max_beads: int = 2000) -> BeadDetections:
    """Sub-pixel bead centres in a 2-D image (see the module docstring).

    ``sigma_px`` the PSF sigma (``None``/0: measured); ``threshold`` in robust
    noise units of the band-passed image; ``saturation`` the value at or
    above which a pixel counts as saturated (``None``: the integer dtype's
    maximum; float images are not checked)."""
    raw = np.asarray(image)
    if raw.ndim != 2:
        raise ValueError(f"bead detection needs a 2-D image, got shape {raw.shape}")
    if saturation is None and np.issubdtype(raw.dtype, np.integer):
        saturation = float(np.iinfo(raw.dtype).max)
    data = raw.astype(np.float64)
    sigma = float(sigma_px) if sigma_px else _measure_sigma(data, 1.5, threshold)
    rows, cols, noise = _candidates(data, sigma, threshold)
    rejected = {"saturated": 0, "edge": 0, "fit": 0, "width": 0, "merged": 0}
    found = []
    edge = edge_sigma * sigma
    half = max(1, int(round(sigma)))
    for r, c in zip(rows, cols):
        r, c = int(r), int(c)
        if r < edge or c < edge or r > data.shape[0] - 1 - edge or c > data.shape[1] - 1 - edge:
            rejected["edge"] += 1
            continue
        if saturation is not None and np.any(
                data[max(0, r - half):r + half + 1, max(0, c - half):c + half + 1] >= saturation):
            rejected["saturated"] += 1
            continue
        fit = _fit_spot(data, r, c, sigma)
        if fit is None:
            rejected["fit"] += 1
            continue
        found.append(fit)
    candidates = np.column_stack([rows, cols]).astype(np.float64) if len(rows) else np.zeros((0, 2))
    if not found:
        return BeadDetections(np.zeros((0, 2)), np.zeros(0), np.zeros(0), sigma, noise, rejected, candidates)
    arr = np.asarray(found, dtype=np.float64)
    # Widths against the PSF sigma (measured from the brightest beads, or
    # given): an aggregate or an unresolved pair is wider than one bead.
    width_ok = np.abs(arr[:, 3] / sigma - 1.0) <= width_tolerance
    rejected["width"] += int((~width_ok).sum())
    arr = arr[width_ok]
    if len(arr) >= 2:
        dist, _ = spatial.cKDTree(arr[:, :2]).query(arr[:, :2], k=2)
        isolated = dist[:, 1] >= min_separation_sigma * sigma
        rejected["merged"] += int((~isolated).sum())
        arr = arr[isolated]
    arr = arr[np.argsort(-arr[:, 2])][:max_beads]
    return BeadDetections(arr[:, :2].copy(), arr[:, 2].copy(), arr[:, 3].copy(), sigma, noise, rejected,
                          candidates)


# --------------------------------------------------------------------------
# pairing
# --------------------------------------------------------------------------

def _apply(matrix: np.ndarray, points: np.ndarray) -> np.ndarray:
    return points @ matrix[:2, :2].T + matrix[:2, 2]


def similarity_from_points(source, target, *, mirror: bool) -> np.ndarray:
    """Least-squares similarity (Umeyama) of the given handedness, as a 3x3
    homogeneous matrix ``target ~ M @ (row, col, 1)``."""
    src = np.asarray(source, dtype=np.float64)
    dst = np.asarray(target, dtype=np.float64)
    sign = np.array([1.0, -1.0]) if mirror else np.array([1.0, 1.0])
    s = src * sign
    mu_s, mu_d = s.mean(axis=0), dst.mean(axis=0)
    s_c, d_c = s - mu_s, dst - mu_d
    u, sv, vt = np.linalg.svd(d_c.T @ s_c / len(s))
    d = np.eye(2)
    if np.linalg.det(u) * np.linalg.det(vt) < 0:
        d[1, 1] = -1.0
    var = float((s_c ** 2).sum() / len(s))
    if var <= 1e-300:
        raise BeadMatchError("cannot fit a scale to coincident points")
    linear = float(np.trace(np.diag(sv) @ d) / var) * (u @ d @ vt)
    matrix = np.eye(3)
    matrix[:2, :2] = linear @ np.diag(sign)
    matrix[:2, 2] = mu_d - linear @ mu_s
    return matrix


def _mutual_pairs(mapped: np.ndarray, target: np.ndarray, tolerance: float):
    if len(mapped) == 0 or len(target) == 0:
        return np.zeros(0, int), np.zeros(0, int)
    d_ij, j = spatial.cKDTree(target).query(mapped, k=1)
    _, i_back = spatial.cKDTree(mapped).query(target, k=1)
    i = np.arange(len(mapped))
    keep = (d_ij < tolerance) & (i_back[j] == i)
    return i[keep], j[keep]


def _triangles(points: np.ndarray, neighbours: int):
    """Triangles of each point with pairs of its nearest neighbours: vertex
    indices ordered by their opposite side (shortest first) and the
    invariants ``(a/c, b/c)`` for sides ``a <= b <= c``. Near-isosceles and
    near-flat triangles have no stable vertex order and are skipped."""
    n = len(points)
    if n < 3:
        return np.zeros((0, 3), int), np.zeros((0, 2))
    _, idx = spatial.cKDTree(points).query(points, k=min(neighbours + 1, n))
    seen, tris, inv = set(), [], []
    for p in range(n):
        for a, b in itertools.combinations(idx[p, 1:], 2):
            key = tuple(sorted((p, int(a), int(b))))
            if key in seen:
                continue
            seen.add(key)
            v = points[list(key)]
            sides = np.array([np.linalg.norm(v[1] - v[2]), np.linalg.norm(v[0] - v[2]),
                              np.linalg.norm(v[0] - v[1])])
            order = np.argsort(sides)
            s = sides[order]
            if s[2] <= 0 or (s[1] - s[0]) / s[2] < 0.05 or (s[2] - s[1]) / s[2] < 0.05 \
                    or (s[0] + s[1]) / s[2] - 1.0 < 0.05:
                continue
            tris.append([key[m] for m in order])
            inv.append((s[0] / s[2], s[1] / s[2]))
    return np.asarray(tris, dtype=int).reshape(-1, 3), np.asarray(inv, dtype=np.float64).reshape(-1, 2)


def _refine(matrix: np.ndarray, src: np.ndarray, dst: np.ndarray, tolerance: float):
    mirror = bool(np.linalg.det(matrix[:2, :2]) < 0)
    pairs = np.zeros((0, 2), int)
    for _ in range(10):
        i, j = _mutual_pairs(_apply(matrix, src), dst, tolerance)
        if len(i) < 3:
            break
        new_pairs = np.column_stack([i, j])
        matrix = similarity_from_points(src[i], dst[j], mirror=mirror)
        if np.array_equal(new_pairs, pairs):
            break
        pairs = new_pairs
    i, j = _mutual_pairs(_apply(matrix, src), dst, tolerance)
    return matrix, np.column_stack([i, j]).astype(int)


@dataclass(frozen=True)
class BeadMatch:
    """The pairing: ``pairs`` ``(i_source, j_target)``; ``matrix`` the
    refined similarity (3x3, source -> target, physical units);
    ``runner_up`` the pair count of the best *different* placement;
    ``proposals`` how many triangle matches were scored."""

    pairs: np.ndarray
    matrix: np.ndarray
    runner_up: int
    runner_up_matrix: np.ndarray | None
    proposals: int

    @property
    def count(self) -> int:
        return int(len(self.pairs))

    @property
    def mirror(self) -> bool:
        return bool(np.linalg.det(self.matrix[:2, :2]) < 0)

    @property
    def scale(self) -> float:
        return float(math.sqrt(abs(np.linalg.det(self.matrix[:2, :2]))))

    @property
    def rotation_deg(self) -> float:
        linear = self.matrix[:2, :2] @ (np.diag([1.0, -1.0]) if self.mirror else np.eye(2))
        return float(math.degrees(math.atan2(linear[1, 0], linear[0, 0])))


def match_point_sets(source, target, *, tolerance: float, neighbours: int = 6, allow_mirror: bool = True,
                     scale_range: tuple[float, float] | None = None, invariant_tolerance: float = 0.01,
                     max_proposals: int = 20000, refine: int = 60, ambiguity_ratio: float = 0.6,
                     min_pairs: int = 6, seed: int = 0) -> BeadMatch:
    """Pair two point sets related by an unknown similarity (any rotation,
    mirrored or not when ``allow_mirror``, any scale and shift), with points
    missing from either. ``tolerance`` is in target units. Raises
    :class:`BeadMatchError` when fewer than ``min_pairs`` pair up or when a
    different placement pairs at least ``ambiguity_ratio`` as many."""
    src = np.asarray(source, dtype=np.float64).reshape(-1, 2)
    dst = np.asarray(target, dtype=np.float64).reshape(-1, 2)
    if len(src) < 3 or len(dst) < 3:
        raise BeadMatchError(f"too few beads to pair: {len(src)} and {len(dst)}")
    tri_s, inv_s = _triangles(src, neighbours)
    tri_d, inv_d = _triangles(dst, neighbours)
    if len(tri_s) == 0 or len(tri_d) == 0:
        raise BeadMatchError("too few beads to form triangles in one of the images")
    lists = spatial.cKDTree(inv_s).query_ball_point(inv_d, r=invariant_tolerance)
    proposals = [(int(si), int(di)) for di, lst in enumerate(lists) for si in lst]
    if len(proposals) > max_proposals:
        rng = np.random.default_rng(seed)
        proposals = [proposals[k] for k in rng.choice(len(proposals), max_proposals, replace=False)]
    src_tree = spatial.cKDTree(src)
    scored = []
    for si, di in proposals:
        a, b = src[tri_s[si]], dst[tri_d[di]]
        for mirror in ((False, True) if allow_mirror else (False,)):
            m = similarity_from_points(a, b, mirror=mirror)
            if np.max(np.linalg.norm(_apply(m, a) - b, axis=1)) > tolerance:
                continue
            scale = math.sqrt(abs(np.linalg.det(m[:2, :2])))
            if scale_range and not scale_range[0] <= scale <= scale_range[1]:
                continue
            # Score in the source frame: the target points mapped back.
            d, _ = src_tree.query(_apply(np.linalg.inv(m), dst), k=1)
            scored.append((int((d * scale < tolerance).sum()), m))
    if not scored:
        raise BeadMatchError(
            "no bead pattern is common to both images (no triangle of beads matched): do both "
            "images show the same beads? Check the detections.")
    scored.sort(key=lambda item: -item[0])
    refined = sorted((_refine(m, src, dst, tolerance) for _, m in scored[:refine]),
                     key=lambda item: -len(item[1]))
    matrix, pairs = refined[0]
    probe = src[pairs[:, 0]] if len(pairs) else src
    # Judge every placement at the precision the best one reached (three
    # times its RMS, at least a tenth of the tolerance): a wrong placement
    # that collects pairs only at the loose search tolerance is not a rival.
    if len(pairs) >= 3:
        rms = float(np.sqrt(np.mean(np.sum((_apply(matrix, src[pairs[:, 0]]) - dst[pairs[:, 1]]) ** 2, axis=1))))
    else:
        rms = tolerance
    tight = min(tolerance, max(3.0 * rms, 0.1 * tolerance))
    # Rivals are placements at about the same scale (a lattice shift, a
    # symmetry of the pattern); one at a very different scale that collects
    # a few pairs is a chance fit to a dense field, not an ambiguity.
    best_scale = math.sqrt(abs(np.linalg.det(matrix[:2, :2])))
    runner, runner_matrix = 0, None
    for m, _ in refined[1:]:
        if np.max(np.linalg.norm(_apply(m, probe) - _apply(matrix, probe), axis=1)) <= 2 * tolerance:
            continue   # the same placement, less refined
        if abs(math.log(math.sqrt(abs(np.linalg.det(m[:2, :2]))) / best_scale)) > math.log(1.25):
            continue
        count = len(_mutual_pairs(_apply(m, src), dst, tight)[0])
        if count > runner:
            runner, runner_matrix = count, m
    match = BeadMatch(pairs=pairs, matrix=matrix, runner_up=runner, runner_up_matrix=runner_matrix,
                      proposals=len(proposals))
    if match.count < min_pairs:
        raise BeadMatchError(
            f"only {match.count} beads pair up between the two images (at least {min_pairs} needed): "
            "do both images show the same bead field (same stage position, same focus)?")
    if runner >= ambiguity_ratio * match.count:
        raise BeadMatchError(
            f"ambiguous pairing: a different placement pairs {runner} beads against {match.count} for the "
            "best -- the bead pattern is regular or symmetric (a grid, rows of beads). Use a sparse, "
            "random bead field.")
    return match


# --------------------------------------------------------------------------
# the strategy
# --------------------------------------------------------------------------

_LOCALIZATION_SPECS = {
    "psf_sigma_px": ParameterSpec(
        0.0, float, "PSF sigma (px, 0 = measure)",
        "Gaussian sigma of a bead's image in this image's pixels. 0 measures it from the "
        "brightest beads, so the two images may have different PSFs.",
        minimum=0.0, step=0.1, decimals=2, group="Bead detection"),
    "threshold": ParameterSpec(
        6.0, float, "Detection threshold (noise units)",
        "Band-passed peak height in robust noise levels. Lower it for dim beads, raise it "
        "against false detections.", minimum=1.0, step=0.5, decimals=1, group="Bead detection"),
    "saturation": ParameterSpec(
        0.0, float, "Saturation level (0 = dtype maximum)",
        "Beads with a pixel at or above this are rejected (their centre is biased).",
        minimum=0.0, step=100.0, decimals=0, advanced=True, group="Bead detection"),
    "min_separation_sigma": ParameterSpec(
        4.0, float, "Minimum bead separation (sigmas)",
        "Beads closer than this to another are both rejected as merged.",
        minimum=1.0, step=0.5, decimals=1, advanced=True, group="Bead detection"),
    "pixel_size_row": ParameterSpec(
        1.0, float, "Pixel size, rows (physical units)",
        "Physical size of one pixel along the rows, used only to pair the beads in their "
        "true geometry (non-square pixels, a scan with different steps per axis). The "
        "pairs are returned in pixels.", minimum=1e-9, step=0.01, decimals=4, group="Geometry"),
    "pixel_size_col": ParameterSpec(
        1.0, float, "Pixel size, columns (physical units)",
        "As for rows, along the columns.", minimum=1e-9, step=0.01, decimals=4, group="Geometry"),
}

_PAIRING_SPECS = {
    "tolerance": ParameterSpec(
        0.0, float, "Pairing tolerance (Reference units, 0 = auto)",
        "Largest distance between a mapped Moving bead and its Reference bead. 0 takes a "
        "quarter of the median Reference bead spacing, at least two Reference pixels.",
        minimum=0.0, step=0.1, decimals=3, group="Pairing"),
    "allow_mirror": ParameterSpec(
        True, bool, "Allow a mirror image",
        "Also try the mirrored placement (an odd number of reflections between the two "
        "detectors).", group="Pairing"),
    "ambiguity_ratio": ParameterSpec(
        0.6, float, "Ambiguity ratio",
        "Refuse when a different placement pairs at least this fraction of the best one's beads.",
        minimum=0.05, maximum=1.0, step=0.05, decimals=2, advanced=True, group="Pairing"),
    "min_pairs": ParameterSpec(
        6, int, "Minimum paired beads", "Refuse with fewer pairs.", minimum=3, step=1,
        group="Pairing"),
    "neighbours": ParameterSpec(
        6, int, "Triangle neighbours", "Nearest neighbours each bead forms triangles with.",
        minimum=2, maximum=12, step=1, advanced=True, group="Pairing"),
}


def _localize_beads(image: np.ndarray, parameters: Mapping[str, Any]) -> ImageLocalizationResult:
    found = detect_beads(
        image, sigma_px=float(parameters["psf_sigma_px"]) or None, threshold=float(parameters["threshold"]),
        saturation=float(parameters["saturation"]) or None,
        min_separation_sigma=float(parameters["min_separation_sigma"]))
    diagnostics = LocalizationDiagnostics(detected_points=found.candidates, refined_points=found.positions)
    if len(found) < 3:
        raise LocalizationFailure(
            f"only {len(found)} isolated, unsaturated beads found (rejected: "
            f"{dict(found.rejected)}); lower the threshold or use a denser bead sample", diagnostics)
    return ImageLocalizationResult(
        points=found.positions,
        metadata={"bead_count": len(found), "psf_sigma_px": found.sigma_px, "noise": found.noise,
                  "rejected": dict(found.rejected),
                  "pixel_size": (float(parameters["pixel_size_row"]), float(parameters["pixel_size_col"]))},
        diagnostics=diagnostics, payload=found)


def auto_tolerance(target_points: np.ndarray, pixel: float) -> float:
    """A quarter of the median nearest-neighbour spacing, at most
    ``50 * pixel``, at least two pixels."""
    target_points = np.asarray(target_points, dtype=np.float64)
    if len(target_points) < 2:
        return 2.0 * pixel
    nn, _ = spatial.cKDTree(target_points).query(target_points, k=2)
    return max(2.0 * pixel, min(float(np.median(nn[:, 1])) / 4.0, 50.0 * pixel))


def _match_beads(source: ImageLocalizationResult, target: ImageLocalizationResult,
                 source_parameters: Mapping[str, Any], target_parameters: Mapping[str, Any],
                 pairing: Mapping[str, Any]) -> CorrespondenceResult:
    s_scale = np.array([source_parameters["pixel_size_row"], source_parameters["pixel_size_col"]], float)
    t_scale = np.array([target_parameters["pixel_size_row"], target_parameters["pixel_size_col"]], float)
    src, dst = source.points * s_scale, target.points * t_scale
    tolerance = float(pairing["tolerance"]) or auto_tolerance(dst, float(t_scale.max()))
    try:
        match = match_point_sets(src, dst, tolerance=tolerance, neighbours=int(pairing["neighbours"]),
                                 allow_mirror=bool(pairing["allow_mirror"]),
                                 ambiguity_ratio=float(pairing["ambiguity_ratio"]),
                                 min_pairs=int(pairing["min_pairs"]))
    except BeadMatchError as error:
        raise ValueError(str(error)) from None
    i, j = match.pairs[:, 0], match.pairs[:, 1]
    return CorrespondenceResult(
        source.points[i], target.points[j],
        metadata={"strategy": "beads", "paired": match.count, "moving_beads": source.point_count,
                  "reference_beads": target.point_count, "runner_up": match.runner_up,
                  "tolerance": tolerance, "mirror": match.mirror, "rotation_deg": match.rotation_deg,
                  "scale": match.scale, "proposals": match.proposals},
        source_localization=source, target_localization=target)


def bead_strategy_spec() -> CorrespondenceStrategySpec:
    """The 'Beads (sparse, any orientation)' strategy."""
    return CorrespondenceStrategySpec(
        name="beads",
        label="Beads (sparse, any orientation)",
        description=(
            "Localise isolated beads independently in Moving and Reference (sub-pixel Gaussian fits; "
            "saturated, merged and edge beads rejected) and pair them by the shapes of the triangles "
            "they form -- no approximate transform needed: any rotation, a mirror image, any scale, "
            "and a Reference field covering only part of the Moving one. Refuses a regular pattern."),
        parameter_specs=_LOCALIZATION_SPECS,
        localize_handler=_localize_beads,
        pairing_match_handler=_match_beads,
        pairing_parameter_specs=_PAIRING_SPECS,
    )
