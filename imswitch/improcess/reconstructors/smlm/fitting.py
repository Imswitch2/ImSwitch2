"""Single-spot sub-pixel fitting — pure-function core.

Provenance
----------
Carried over from the maintainer's own earlier SMLM work; no third-party code
is copied here. The *approach* follows Picasso (Jungmann Lab) — see
``ACKNOWLEDGMENTS.md`` and cite Schnitzbauer et al., Nat Protoc 12,
1198-1228 (2017) if you use this in published work.

Two methods, both numpy/scipy only:

* ``"gausslq"`` — background-subtracted centroid + second-moment sigmas. Fast,
  no optimiser, robust starting point.
* ``"mle"`` — Poisson maximum-likelihood Gaussian fit (scipy ``L-BFGS-B``).

Returns fitted quantities in *frame pixel* coordinates; the localizer converts
to nm using the acquisition pixel size.
"""

from __future__ import annotations

import numpy as np
from scipy.optimize import minimize


def _gaussian_negloglik(params, roi_img: np.ndarray) -> float:
    y0, x0, sigma, amplitude, background = params
    rows, cols = np.indices(roi_img.shape)
    model = amplitude * np.exp(
        -((cols - x0) ** 2 + (rows - y0) ** 2) / (2 * sigma ** 2)
    ) + background
    model = np.clip(model, 1e-6, None)
    # Poisson negative log-likelihood (dropping the data-only constant term).
    return float(np.sum(model - roi_img * np.log(model)))


def fit_spot(
    frame: np.ndarray,
    y0: int,
    x0: int,
    roi: int = 7,
    method: str = "gausslq",
) -> dict:
    """Fit one emitter at candidate integer centre ``(y0, x0)``.

    Returns a dict with keys ``x``, ``y`` (frame-pixel sub-pixel centre),
    ``intensity`` (photons proxy), ``sigma_x``, ``sigma_y`` (px). Raises
    ``ValueError``/``RuntimeError`` when the ROI is out of bounds, has no
    positive signal, or the MLE optimiser fails.
    """
    frame = np.asarray(frame)
    half = roi // 2
    if (
        y0 - half < 0
        or y0 + half >= frame.shape[0]
        or x0 - half < 0
        or x0 + half >= frame.shape[1]
    ):
        raise ValueError("ROI outside image bounds")

    roi_img = frame[y0 - half:y0 + half + 1, x0 - half:x0 + half + 1].astype(np.float32)
    edge = np.hstack([roi_img[0, :], roi_img[-1, :], roi_img[:, 0], roi_img[:, -1]])
    background = float(np.median(edge))
    roi_img = np.clip(roi_img - background, 0, None)

    rows, cols = np.indices(roi_img.shape)
    total = float(roi_img.sum())
    if total <= 0:
        raise ValueError("Non-positive total intensity in ROI")

    y_centroid = float((rows * roi_img).sum() / total)
    x_centroid = float((cols * roi_img).sum() / total)

    if method == "gausslq":
        x2 = float(((cols - x_centroid) ** 2 * roi_img).sum() / total)
        y2 = float(((rows - y_centroid) ** 2 * roi_img).sum() / total)
        return {
            "x": x0 - half + x_centroid,
            "y": y0 - half + y_centroid,
            "intensity": total,
            "sigma_x": float(np.sqrt(max(x2, 1e-3))),
            "sigma_y": float(np.sqrt(max(y2, 1e-3))),
        }

    if method == "mle":
        p0 = [roi / 2, roi / 2, 1.5, float(roi_img.max()), background]
        bounds = [(0, roi - 1), (0, roi - 1), (0.5, 5.0), (0, None), (0, None)]
        result = minimize(
            _gaussian_negloglik, p0, args=(roi_img,), bounds=bounds, method="L-BFGS-B"
        )
        if not result.success:
            raise RuntimeError("MLE fit failed")
        y_fit, x_fit, sigma, amplitude, _bg = result.x
        return {
            "x": x0 - half + float(x_fit),
            "y": y0 - half + float(y_fit),
            "intensity": float(amplitude),
            "sigma_x": float(sigma),
            "sigma_y": float(sigma),
        }

    raise ValueError(f"Unsupported fit method: {method!r}")


def fit_spots(
    frame: np.ndarray,
    coords: np.ndarray,
    roi: int = 7,
    method: str = "gausslq",
) -> list[dict]:
    """Fit every candidate in ``coords`` (``(N, 2)`` ``(row, col)``).

    Spots whose fit raises (out-of-bounds, dark ROI, optimiser failure) are
    skipped rather than aborting the frame.
    """
    fits: list[dict] = []
    for y0, x0 in np.asarray(coords).reshape(-1, 2):
        try:
            fits.append(fit_spot(frame, int(y0), int(x0), roi=roi, method=method))
        except (ValueError, RuntimeError):
            continue
    return fits


__all__ = ["fit_spot", "fit_spots"]
