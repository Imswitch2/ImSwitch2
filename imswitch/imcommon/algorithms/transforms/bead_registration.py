"""Camera pixels -> point-scan coordinates from one bead image per detector.

The calibration a camera-triggered point scan needs: where, in the
scanner's own coordinates, does a camera pixel lie? One widefield image and
one point-scan (APD) image of the same sparse bead field answer it.

:func:`register_bead_images`:

1. localises the beads in both images (the ``beads`` correspondence
   strategy, :mod:`.correspondence.beads`);
2. converts the scan image's bead centres into scan coordinates with the
   scan's own geometry (:func:`scan_geometry_affine`): rows are the slow
   (second) scan axis, columns the fast (first) one, pixel ``j`` centred at
   ``first + j * step`` -- the geometry the scan controller publishes with
   every point-scan frame, so no calibration number is involved;
3. pairs camera beads with scan beads without a prior (any rotation, a
   mirror image, any scale, a scan field covering part of the camera field;
   a regular pattern is refused as ambiguous);
4. fits through :func:`~.estimate.fit_transform` in the scan's units:
   ``similarity_reflect`` (rotation, one scale, mirror or not) by default,
   ``affine`` when asked or, with ``model="auto"``, when it lowers the
   residual significantly (F-test p < 0.01 *and* at least 10 % lower RMS)
   and at least ``min_beads_affine`` pairs exist;
5. refuses (:class:`BeadRegistrationError`) too few beads, an ambiguous
   pairing, or residuals above ``max_residual``.

The result is a :class:`~.model.SpatialTransform` from camera ``(row, col)``
pixels (pixel-centred; ``(0, 0)`` the centre of the first pixel of the frame
as delivered, i.e. of its ROI, binned) to the scan coordinates of the axes
``target_context.extra["axes"]`` names, in that column order, in
``target_context.extra["units"]`` (µm). Its contexts carry the camera grid
(shape, ROI, binning, pixel size) and the scan configuration it was measured
with; a different camera grid or scan configuration needs a new calibration.

Not modelled: stage position, field distortion, chromatic shift between the
two channels, z. A shift of the scan image along its fast axis (the phase
delay of a galvo scan) is a translation here and is absorbed: the transform
holds for the phase delay it was measured with.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

import numpy as np
from scipy import stats

from .correspondence.beads import (
    BeadDetections,
    BeadMatchError,
    auto_tolerance,
    detect_beads,
    match_point_sets,
)
from .estimate import ModelFit, fit_transform
from .model import AcquisitionContext, ResidualStats, SpatialTransform
from .models.affine import AffineTransform

__all__ = [
    "BeadRegistration",
    "BeadRegistrationError",
    "BeadRegistrationOptions",
    "decompose_affine",
    "register_bead_images",
    "scan_axes",
    "scan_geometry_affine",
]


class BeadRegistrationError(ValueError):
    """The two bead images do not give a transform that can be trusted."""


# --------------------------------------------------------------------------
# scan geometry
# --------------------------------------------------------------------------

def scan_axes(geometry: Any) -> list[dict]:
    """The scanned axes of a scan geometry, fast axis first, as dicts with
    ``device``, ``step_um``, ``first_um`` (and ``count``). ``geometry`` is a
    mapping ``{"axes": [...]}`` or an object with ``.axes``."""
    axes = geometry.get("axes") if isinstance(geometry, Mapping) else getattr(geometry, "axes", None)
    out = []
    for axis in list(axes or ()):
        if not isinstance(axis, Mapping):
            axis = {"device": axis.device, "step_um": axis.step_um, "first_um": axis.first_um,
                    "count": getattr(axis, "count", None)}
        out.append(dict(axis))
    if len(out) < 2:
        raise BeadRegistrationError("the scan image needs a two-axis scan geometry (fast axis first)")
    return out


def scan_geometry_affine(geometry: Any, target_axes: Sequence[str] | None = None
                         ) -> tuple[AffineTransform, tuple[str, str]]:
    """Scan-image ``(row, col)`` pixels -> scan µm with columns in
    ``target_axes`` order (default: slow axis, then fast axis -- ``("Y",
    "X")`` for an X-fast scan). Rows are the second axis, columns the first."""
    axes = scan_axes(geometry)
    fast, slow = axes[0], axes[1]
    names = (str(slow["device"]), str(fast["device"]))
    order = tuple(str(a) for a in target_axes) if target_axes else names
    if sorted(order) != sorted(names) or len(order) != 2:
        raise BeadRegistrationError(f"target axes {list(order)} are not the scanned axes {list(names)}")
    # Per scanned axis: which pixel column drives it, its step and first pixel.
    per = {names[0]: (0, float(slow["step_um"]), float(slow["first_um"])),
           names[1]: (1, float(fast["step_um"]), float(fast["first_um"]))}
    linear = np.zeros((2, 2))
    offset = np.zeros(2)
    for out, name in enumerate(order):
        column, step, first = per[name]
        linear[out, column] = step
        offset[out] = first
    return AffineTransform.from_linear_offset(linear, offset), order


# --------------------------------------------------------------------------
# decomposition
# --------------------------------------------------------------------------

def decompose_affine(matrix: Any) -> dict:
    """Rotation (deg), scale (sqrt|det|), per-axis scales, shear and mirror
    of a 2-D affine, as ``A = R(theta) @ [[sx, k*sx], [0, sy]] @ M`` with
    ``M = diag(1, -1)`` when mirrored. Translation as given."""
    matrix = np.asarray(matrix, dtype=np.float64)
    linear = matrix[:2, :2]
    mirror = bool(np.linalg.det(linear) < 0)
    m = linear @ np.diag([1.0, -1.0]) if mirror else linear
    q, r = np.linalg.qr(m)
    signs = np.sign(np.diag(r))
    signs[signs == 0] = 1.0
    q, r = q * signs, (r.T * signs).T
    return {"rotation_deg": math.degrees(math.atan2(q[1, 0], q[0, 0])),
            "scale": float(math.sqrt(abs(np.linalg.det(linear)))),
            "scale_axes": (float(r[0, 0]), float(r[1, 1])),
            "shear": float(r[0, 1] / r[0, 0]) if r[0, 0] else 0.0,
            "mirror": mirror,
            "translation": (float(matrix[0, 2]), float(matrix[1, 2]))}


# --------------------------------------------------------------------------
# the registration
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class BeadRegistrationOptions:
    """``camera`` / ``scan`` detection keywords for :func:`detect_beads`
    (``sigma_px``, ``threshold``, ``saturation``, ``min_separation_sigma``)
    in each image's own pixels; ``model`` ``"auto"``,
    ``"similarity_reflect"`` or ``"affine"``; ``tolerance`` the pairing
    distance in scan units (``None``: a quarter of the median scan-bead
    spacing, at least two scan steps); ``max_residual`` the largest accepted
    residual (scan units); ``min_beads`` the fewest pairs accepted;
    ``ambiguity_ratio`` see :func:`match_point_sets`; ``camera_pixel_um``
    the camera's configured pixel size (warns when the fitted scale differs
    by more than ``scale_warning``); ``target_axes`` the output column order."""

    camera: Mapping[str, Any] = field(default_factory=dict)
    scan: Mapping[str, Any] = field(default_factory=dict)
    model: str = "auto"
    tolerance: float | None = None
    max_residual: float = 0.25
    min_beads: int = 6
    min_beads_affine: int = 10
    ambiguity_ratio: float = 0.6
    allow_mirror: bool = True
    camera_pixel_um: float | None = None
    scale_warning: float = 0.2
    target_axes: tuple[str, str] | None = None


@dataclass
class BeadRegistration:
    """What :func:`register_bead_images` found."""

    transform: SpatialTransform
    fit: ModelFit
    target_axes: tuple[str, str]
    camera_points: np.ndarray        # (N, 2) paired camera px (row, col)
    scan_points_px: np.ndarray       # (N, 2) paired scan px (row, col)
    scan_points: np.ndarray          # (N, 2) paired scan µm, target_axes order
    camera_detections: BeadDetections
    scan_detections: BeadDetections
    scan_all: np.ndarray             # every scan bead, µm
    runner_up: int
    similarity_rms: float
    affine_rms: float
    model_rule: str
    tolerance: float
    unpaired_scan: int
    unpaired_camera_in_field: int
    warnings: list = field(default_factory=list)

    @property
    def matrix(self) -> np.ndarray:
        return self.transform.as_matrix()

    @property
    def residual_vectors(self) -> np.ndarray:
        return self.fit.mapped_points - self.scan_points

    @property
    def residual_norms(self) -> np.ndarray:
        return np.linalg.norm(self.residual_vectors, axis=1)

    @property
    def inliers(self) -> np.ndarray:
        return np.asarray(self.fit.inliers, dtype=bool)

    @property
    def rms(self) -> float:
        return float(np.sqrt(np.mean(self.residual_norms[self.inliers] ** 2)))

    @property
    def max(self) -> float:
        return float(self.residual_norms[self.inliers].max())

    @property
    def scale(self) -> float:
        return float(math.sqrt(abs(np.linalg.det(self.matrix[:2, :2]))))

    def geometry(self) -> dict:
        return decompose_affine(self.matrix)

    def report(self) -> str:
        g = self.geometry()
        a0, a1 = self.target_axes
        n_in = int(self.inliers.sum())
        lines = [
            f"model: {self.transform.provenance.get('estimator')} ({self.model_rule})",
            f"beads: camera {len(self.camera_detections)}, scan {len(self.scan_detections)}; paired "
            f"{len(self.camera_points)}, inliers {n_in}, outliers {len(self.camera_points) - n_in}; "
            f"unpaired scan beads {self.unpaired_scan}, unpaired camera beads inside the scan field "
            f"{self.unpaired_camera_in_field}",
            f"residuals (inliers): RMS {self.rms * 1000:.1f} nm = {self.rms / self.scale:.3f} camera px, "
            f"max {self.max * 1000:.1f} nm = {self.max / self.scale:.3f} camera px "
            f"(similarity RMS {self.similarity_rms * 1000:.1f} nm, affine RMS {self.affine_rms * 1000:.1f} nm)",
            f"rotation {g['rotation_deg']:.3f} deg; scale {g['scale']:.5f} um per camera px "
            f"(axes {g['scale_axes'][0]:.5f}, {g['scale_axes'][1]:.5f}, shear {g['shear']:.4f}); "
            f"mirror {'YES' if g['mirror'] else 'no'}",
            f"camera pixel (0, 0) -> {a0} = {g['translation'][0]:.3f} um, {a1} = {g['translation'][1]:.3f} um "
            "(the translation; it includes any fast-axis offset of the scan image, e.g. from the phase delay)",
            f"matrix: ({a0}, {a1}) [um] = A @ (row, col) + t:\n"
            + np.array2string(self.matrix[:2], precision=6, suppress_small=False),
            f"ambiguity check: best different placement pairs {self.runner_up} beads",
        ]
        lines += [f"WARNING: {w}" for w in self.warnings]
        return "\n".join(lines)


def _f_test(rss_small: float, rss_large: float, n_pairs: int, extra: int = 2, dof_large: int = 6) -> float:
    dof = 2 * n_pairs - dof_large
    if dof <= 0:
        return 1.0
    if rss_large <= 0:
        return 0.0 if rss_small > 0 else 1.0
    return float(stats.f.sf(((rss_small - rss_large) / extra) / (rss_large / dof), extra, dof))


def register_bead_images(camera_image: Any, scan_image: Any, scan_geometry: Any,
                         options: BeadRegistrationOptions | None = None, *,
                         source_frame: str = "camera", target_frame: str = "scan",
                         camera_context: AcquisitionContext | None = None,
                         scan_context: AcquisitionContext | None = None,
                         provenance: Mapping[str, Any] | None = None) -> BeadRegistration:
    """The camera -> scan map from one bead image per detector (see the
    module docstring). ``scan_geometry`` is the scan image's geometry
    (``{"axes": [{"device", "step_um", "first_um"}, ...]}``, fast first)."""
    options = options or BeadRegistrationOptions()
    if options.model not in ("auto", "similarity_reflect", "similarity", "affine"):
        raise BeadRegistrationError(f"model must be auto, similarity_reflect or affine, not {options.model!r}")
    pixel_to_um, target_axes = scan_geometry_affine(scan_geometry, options.target_axes)
    camera = detect_beads(camera_image, **dict(options.camera))
    scan = detect_beads(scan_image, **dict(options.scan))
    need = max(3, int(options.min_beads))
    if len(camera) < need or len(scan) < need:
        raise BeadRegistrationError(
            f"too few beads: {len(camera)} in the camera image and {len(scan)} in the scan image; at least "
            f"{need} isolated, unsaturated beads are needed in each (camera rejected {dict(camera.rejected)}, "
            f"scan rejected {dict(scan.rejected)}). Lower the threshold or use a denser bead sample.")
    scan_um = pixel_to_um.map_points(scan.positions)
    steps = [abs(float(a["step_um"])) for a in scan_axes(scan_geometry)[:2]]
    tolerance = float(options.tolerance) if options.tolerance else auto_tolerance(scan_um, max(steps))
    try:
        match = match_point_sets(camera.positions, scan_um, tolerance=tolerance,
                                 allow_mirror=options.allow_mirror, ambiguity_ratio=options.ambiguity_ratio,
                                 min_pairs=need)
    except BeadMatchError as error:
        raise BeadRegistrationError(str(error)) from None
    i, j = match.pairs[:, 0], match.pairs[:, 1]
    src, dst = camera.positions[i], scan_um[j]
    similarity_kind = "similarity_reflect" if options.allow_mirror else "similarity"
    sim = fit_transform(similarity_kind, src, dst, residual_threshold=tolerance)
    aff = fit_transform("affine", src, dst, residual_threshold=tolerance)
    both = sim.inliers & aff.inliers
    if both.sum() < need:
        both = sim.inliers
    rss_sim = float(np.sum(np.linalg.norm(sim.mapped_points - dst, axis=1)[both] ** 2))
    rss_aff = float(np.sum(np.linalg.norm(aff.mapped_points - dst, axis=1)[both] ** 2))
    n_both = int(both.sum())
    rms_sim, rms_aff = math.sqrt(rss_sim / n_both), math.sqrt(rss_aff / n_both)
    if options.model in ("similarity_reflect", "similarity"):
        chosen, rule = sim, "asked for"
    elif options.model == "affine":
        chosen, rule = aff, "asked for"
    elif n_both < options.min_beads_affine:
        chosen, rule = sim, f"auto: {n_both} pairs, fewer than {options.min_beads_affine} for an affine"
    else:
        p = _f_test(rss_sim, rss_aff, n_both)
        if p < 0.01 and rms_aff < 0.9 * rms_sim:
            chosen, rule = aff, f"auto: the affine lowers the RMS by {100 * (1 - rms_aff / rms_sim):.0f} % (F-test p={p:.1e})"
        else:
            chosen, rule = sim, f"auto: the affine is not significantly better (F-test p={p:.2g})"
    warnings = []
    scale = math.sqrt(abs(np.linalg.det(chosen.transform.as_matrix()[:2, :2])))
    if options.camera_pixel_um:
        ratio = scale / float(options.camera_pixel_um)
        if abs(ratio - 1.0) > options.scale_warning:
            warnings.append(
                f"the fitted scale {scale:.4f} um per camera px differs from the camera's configured pixel size "
                f"{float(options.camera_pixel_um):.4f} um by {100 * (ratio - 1):+.0f} % -- binning, a wrong pixel "
                "size, or a wrong galvo conversionFactor?")
    if int(chosen.inliers.sum()) < len(src):
        warnings.append(f"{len(src) - int(chosen.inliers.sum())} paired beads are fit outliers (left out)")
    mapped_all = chosen.transform.map_points(camera.positions)
    lo, hi = scan_um.min(axis=0), scan_um.max(axis=0)
    inside = np.all((mapped_all >= lo) & (mapped_all <= hi), axis=1)
    paired_camera = np.zeros(len(camera), bool)
    paired_camera[i] = True
    inliers = np.asarray(chosen.inliers, bool)
    residuals = ResidualStats.from_residuals(
        np.linalg.norm(chosen.mapped_points - dst, axis=1), inliers=inliers, threshold=tolerance, unit="um")
    inlier_norms = np.linalg.norm(chosen.mapped_points - dst, axis=1)[inliers]
    g = decompose_affine(chosen.transform.as_matrix())
    record_provenance = {
        "method": "bead registration (imswitch.imcommon.algorithms.transforms.bead_registration)",
        "estimator": chosen.estimator, "model_rule": rule, "strategy": "beads",
        "beads_camera": len(camera), "beads_scan": len(scan), "beads_paired": int(len(src)),
        "beads_used": int(inliers.sum()), "runner_up": match.runner_up, "tolerance_um": tolerance,
        "residual_rms_um": float(np.sqrt(np.mean(inlier_norms ** 2))),
        "residual_max_um": float(inlier_norms.max()),
        "similarity_rms_um": rms_sim, "affine_rms_um": rms_aff,
        "rotation_deg": g["rotation_deg"], "scale_um_per_px": g["scale"], "mirror": g["mirror"],
        "camera_psf_sigma_px": camera.sigma_px, "scan_psf_sigma_px": scan.sigma_px,
        "source_points": "camera (row, col) pixel centres; (0, 0) = centre of the first pixel of the frame (ROI, binned)",
        **dict(provenance or {}),
    }
    if warnings:
        record_provenance["warnings"] = list(warnings)
    scan_ctx = scan_context or AcquisitionContext()
    scan_extra = {**dict(scan_ctx.extra), "axes": list(target_axes), "units": "um",
                  "scan_geometry": {"axes": scan_axes(scan_geometry)}}
    transform = SpatialTransform(
        model=chosen.transform, source_frame=source_frame, target_frame=target_frame, units="um",
        source_context=camera_context or AcquisitionContext(), target_context=replace(scan_ctx, extra=scan_extra),
        residuals=residuals, provenance=record_provenance)
    result = BeadRegistration(
        transform=transform, fit=chosen, target_axes=target_axes, camera_points=src,
        scan_points_px=scan.positions[j], scan_points=dst, camera_detections=camera, scan_detections=scan,
        scan_all=scan_um, runner_up=match.runner_up, similarity_rms=rms_sim, affine_rms=rms_aff, model_rule=rule,
        tolerance=tolerance, unpaired_scan=len(scan) - len(set(j.tolist())),
        unpaired_camera_in_field=int((inside & ~paired_camera).sum()), warnings=warnings)
    if result.max > options.max_residual:
        raise BeadRegistrationError(
            f"residuals too large: max {result.max * 1000:.0f} nm, RMS {result.rms * 1000:.0f} nm, above the "
            f"{options.max_residual * 1000:.0f} nm limit (distortion, drift between the two images, or wrong "
            f"pairs)\n{result.report()}")
    return result
