"""Judging a fitted calibration: its numbers in words, a QC figure, a check
on new data, and whether the grid it is applied to is the one it was made on.

Shared by every calibration method of the Transform Calibration panel (beads,
foci grids, clicked pairs) and Qt-free: matplotlib renders the figure
off-screen.

* :func:`describe_transform` -- model, pairs, residuals, rotation, scale,
  mirror, translation and matrix, in the record's units (with nm beside µm).
* :func:`qc_figure` -- both images with the pairs numbered, the residual
  vectors (magnified) on the reference image, and a residual histogram.
* :func:`verify_on_points` -- an independent check of a saved transform on
  points localised in a (preferably new) pair of images: no fitting, each
  reference point compared with the nearest mapped moving point.
* :func:`context_problems` -- how an acquisition context differs from the
  one a transform was measured with (image shape, ROI, binning).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from .model import AcquisitionContext, SpatialTransform

__all__ = [
    "context_problems",
    "decompose_matrix",
    "describe_transform",
    "qc_figure",
    "verify_on_points",
]


def decompose_matrix(matrix: Any) -> dict:
    """Rotation (deg), scale, per-axis scales, shear, mirror and translation
    of a 2-D affine (see :func:`.bead_registration.decompose_affine`)."""
    from .bead_registration import decompose_affine

    return decompose_affine(np.asarray(matrix, dtype=np.float64))


def _length(value: float, unit: str) -> str:
    if unit in ("um", "µm"):
        return f"{value * 1000:.1f} nm"
    return f"{value:.3f} {unit}"


def describe_transform(transform: SpatialTransform, fit: Any = None, *, axes: Sequence[str] | None = None) -> list[str]:
    """The lines a person judging a calibration reads first."""
    provenance = dict(transform.provenance or {})
    unit = "µm" if str(transform.units) in ("um", "µm") else str(transform.units)
    estimator = getattr(fit, "estimator", None) or provenance.get("estimator") or transform.kind
    selection = getattr(fit, "selection", None) or provenance.get("model_selection") or {}
    rule = selection.get("rule") if isinstance(selection, dict) else None
    rule = rule or provenance.get("model_rule")
    lines = [f"Model: {estimator}" + (f" — {rule}" if rule else "")]
    residuals = transform.residuals
    if residuals is not None:
        lines.append(f"Pairs: {residuals.n_points}, inliers {residuals.n_inliers}")
        lines.append(f"Residual: RMS {_length(residuals.rms, residuals.unit)}, max {_length(residuals.max, residuals.unit)}"
                     f", median {_length(residuals.median, residuals.unit)}")
    matrix = transform.as_matrix()
    names = list(axes or transform.target_context.extra.get("axes") or ("row", "col"))
    if matrix is not None:
        g = decompose_matrix(matrix)
        per = f"{unit} per moving px" if unit != "px" else "(reference px per moving px)"
        lines.append(f"Rotation {g['rotation_deg']:.3f}°, {'MIRRORED' if g['mirror'] else 'not mirrored'}")
        lines.append(f"Scale {g['scale']:.5f} {per} (axes {g['scale_axes'][0]:.5f}, {g['scale_axes'][1]:.5f}; "
                     f"shear {g['shear']:.4f})")
        lines.append(f"Moving pixel (0, 0) lands at {names[0]} = {g['translation'][0]:.3f}, "
                     f"{names[1]} = {g['translation'][1]:.3f} {unit}")
        lines.append(f"Matrix ({names[0]}, {names[1]}) = A @ (row, col) + t:")
        lines.extend("  " + "  ".join(f"{v:+12.6f}" for v in row) for row in np.asarray(matrix)[:2])
    else:
        degree = getattr(transform.model, "degree", None)
        if degree is not None:
            lines.append(f"Polynomial transform · degree {int(degree)}")
            lines.append("Inspect / tune… → Transform field shows the fitted nonlinear mapping.")
        else:
            lines.append(f"Non-affine transform ({transform.kind}) — no homogeneous matrix.")
    return lines


def verify_on_points(transform: SpatialTransform, source_points: Any, target_points: Any, *,
                     tolerance: float) -> dict:
    """Map ``source_points`` (moving px) with ``transform`` and compare each
    of ``target_points`` (in the transform's target units) with the nearest
    mapped point: how many lie within ``tolerance``, their distances (RMS,
    median, max) and the mean offset (a shift since the calibration)."""
    from scipy import spatial

    source = np.asarray(source_points, dtype=np.float64).reshape(-1, 2)
    target = np.asarray(target_points, dtype=np.float64).reshape(-1, 2)
    if len(source) == 0 or len(target) == 0:
        raise ValueError(f"nothing to compare: {len(source)} moving and {len(target)} reference points")
    mapped = transform.map_points(source)
    distance, index = spatial.cKDTree(mapped).query(target, k=1)
    close = distance < tolerance
    offsets = target[close] - mapped[index[close]]
    nan = math.nan
    return {"moving_points": int(len(source)), "reference_points": int(len(target)),
            "matched": int(close.sum()), "unmatched_reference_points": int((~close).sum()),
            "tolerance": float(tolerance), "units": str(transform.units),
            "rms": float(np.sqrt(np.mean(distance[close] ** 2))) if close.any() else nan,
            "median": float(np.median(distance[close])) if close.any() else nan,
            "max": float(distance[close].max()) if close.any() else nan,
            "mean_offset": offsets.mean(axis=0).tolist() if close.any() else [nan, nan]}


def context_problems(expected: AcquisitionContext, actual: AcquisitionContext, *, label: str = "moving") -> list[str]:
    """How ``actual`` differs from ``expected`` in what a pixel calibration
    depends on (image shape, ROI, binning); empty when it does not or when
    ``expected`` does not say."""
    problems = []
    if expected.image_shape is not None and actual.image_shape is not None \
            and tuple(expected.image_shape)[-2:] != tuple(actual.image_shape)[-2:]:
        problems.append(f"the {label} image is {tuple(actual.image_shape)[-2:]}, calibrated on "
                        f"{tuple(expected.image_shape)[-2:]}")
    if expected.roi is not None and actual.roi is not None and tuple(expected.roi) != tuple(actual.roi):
        problems.append(f"the {label} ROI is {list(actual.roi)}, calibrated on {list(expected.roi)}")
    if expected.binning is not None and actual.binning is not None and int(expected.binning) != int(actual.binning):
        problems.append(f"the {label} binning is {actual.binning}, calibrated on {expected.binning}")
    return problems


def qc_figure(source_image: Any, target_image: Any, source_px: Any, target_px: Any, mapped_target_px: Any, *,
              inliers: Any = None, residuals: Any = None, unit: str = "px", title: str = "",
              source_detected: Any = None, path: str | Path | None = None, magnify: float | None = None):
    """Moving and reference images with the pairs numbered (inliers green,
    outliers red), the residual vectors magnified on the reference image
    (``mapped_target_px`` -- where the moving points land, in reference
    pixels), and a histogram of ``residuals`` (the fit's, in ``unit``).
    Saved as PNG when ``path`` is given (returns the path), else returns the
    figure."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    source_px = np.asarray(source_px, dtype=np.float64).reshape(-1, 2)
    target_px = np.asarray(target_px, dtype=np.float64).reshape(-1, 2)
    mapped = np.asarray(mapped_target_px, dtype=np.float64).reshape(-1, 2)
    inl = np.ones(len(source_px), bool) if inliers is None else np.asarray(inliers, bool)
    res = np.linalg.norm(mapped - target_px, axis=1) if residuals is None else np.asarray(residuals, float)
    fig = Figure(figsize=(15, 5.2), dpi=110)
    FigureCanvasAgg(fig)
    ax_src, ax_dst, ax_hist = fig.subplots(1, 3)
    for ax, image in ((ax_src, source_image), (ax_dst, target_image)):
        image = np.asarray(image, dtype=np.float64)
        lo, hi = np.percentile(image, (1, 99.7))
        ax.imshow(image, cmap="gray", vmin=lo, vmax=hi if hi > lo else lo + 1)
    if source_detected is not None and len(source_detected):
        det = np.asarray(source_detected, float)
        ax_src.scatter(det[:, 1], det[:, 0], s=14, facecolors="none", edgecolors="tab:blue", linewidths=0.6)
    for ax, pts in ((ax_src, source_px), (ax_dst, target_px)):
        ax.scatter(pts[inl, 1], pts[inl, 0], s=40, facecolors="none", edgecolors="lime", linewidths=1.0)
        ax.scatter(pts[~inl, 1], pts[~inl, 0], s=40, marker="x", color="red")
        for k, (r, c) in enumerate(pts):
            ax.annotate(str(k + 1), (c, r), color="yellow", fontsize=6, xytext=(3, 3), textcoords="offset points")
    vec = mapped - target_px
    norm = np.linalg.norm(vec, axis=1)
    if magnify is None:
        typical = float(np.median(norm)) if len(norm) and np.median(norm) > 0 else 1.0
        magnify = max(1.0, round(0.05 * max(np.shape(target_image)) / typical))
    ax_dst.quiver(target_px[:, 1], target_px[:, 0], vec[:, 1] * magnify, vec[:, 0] * magnify, angles="xy",
                  scale_units="xy", scale=1, color="red", width=0.004)
    ax_src.set_title(f"moving: {len(source_px)} pairs (green: inliers)")
    rms = float(np.sqrt(np.mean(res[inl] ** 2))) if inl.any() else float("nan")
    ax_dst.set_title(f"reference: residuals x{magnify:g} (RMS {_length(rms, unit)})")
    shown = res * 1000 if unit in ("um", "µm") else res
    ax_hist.hist(shown, bins=max(5, len(res) // 2), color="tab:gray")
    ax_hist.set_xlabel("residual (nm)" if unit in ("um", "µm") else f"residual ({unit})")
    ax_hist.set_ylabel("pairs")
    ax_hist.set_title(title, fontsize=9)
    fig.tight_layout()
    if path is None:
        return fig
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(path))
    return path
