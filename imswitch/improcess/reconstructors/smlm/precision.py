"""Localization precision — pure-function core.

How well an emitter's *position* is known, as opposed to how broad its spot
was. The distinction matters: precision is what an SMLM reconstruction should
be rendered with, and what the field filters on. See
:mod:`~imswitch.improcess.model.localization_schema` for the two column
families that carry them.

The estimator is the Thompson/Larson/Webb closed form with Mortensen's
correction — the same one ThunderSTORM reports as ``uncertainty`` and Picasso
as ``lp``:

.. math::

    \\sigma^2 = \\frac{s^2 + a^2/12}{N}
              + \\frac{8 \\pi s^4 b^2}{a^2 N^2}

for PSF width :math:`s`, pixel size :math:`a`, total photons :math:`N` and
background standard deviation :math:`b` (photons per pixel). The first term is
photon shot noise plus pixelation; the second is background.

.. warning::

   **This is an estimate, not a CRLB.** It inherits every limitation of the
   fitter that feeds it. In particular ImProcess does not yet apply camera
   calibration, so ``photons`` and ``background`` are in detector counts rather
   than true photons unless the camera happens to have unit gain. The estimate
   is then correct in *shape* but scaled — good for ranking and filtering
   localizations against each other, not for quoting an absolute nanometre
   precision in a publication. Fixing that means gain/offset/QE conversion at
   the fitter, which is tracked separately.
"""

from __future__ import annotations

import numpy as np

__all__ = ["localization_precision_nm"]


def localization_precision_nm(
    sigma_nm: np.ndarray | float,
    photons: np.ndarray | float,
    *,
    pixel_size_nm: float,
    background: np.ndarray | float = 0.0,
) -> np.ndarray:
    """Estimated lateral localization precision, in nanometres.

    Args:
        sigma_nm: Fitted PSF standard deviation, in nm.
        photons: Total integrated photons (counts) in the spot.
        pixel_size_nm: Camera pixel size in nm, projected into sample space.
        background: Mean background level per pixel, in the same units as
            ``photons``. Assumed Poisson, so its variance is taken as the
            level itself; pass 0 to drop the background term.

    Returns:
        Float32 array of precisions in nm, broadcast over the inputs. Rows
        with no usable photon count yield ``0.0``, which the schema reads as
        "not measured" rather than "infinitely precise".
    """
    if pixel_size_nm <= 0:
        raise ValueError("pixel_size_nm must be positive")

    sigma = np.asarray(sigma_nm, dtype=np.float64)
    count = np.asarray(photons, dtype=np.float64)
    back = np.asarray(background, dtype=np.float64)
    area = float(pixel_size_nm)

    sigma, count, back = np.broadcast_arrays(sigma, count, back)
    usable = np.isfinite(count) & (count > 0) & np.isfinite(sigma) & (sigma > 0)

    # Guard the division rather than the inputs, so a bad row costs one slot
    # instead of forcing the caller to pre-filter.
    safe_count = np.where(usable, count, 1.0)
    safe_sigma = np.where(usable, sigma, 1.0)
    # Poisson background: variance equals the mean level.
    variance = np.clip(np.where(np.isfinite(back), back, 0.0), 0.0, None)

    shot = (safe_sigma ** 2 + area ** 2 / 12.0) / safe_count
    background_term = (
        8.0 * np.pi * safe_sigma ** 4 * variance / (area ** 2 * safe_count ** 2)
    )
    precision = np.sqrt(shot + background_term)
    return np.where(usable, precision, 0.0).astype(np.float32)
