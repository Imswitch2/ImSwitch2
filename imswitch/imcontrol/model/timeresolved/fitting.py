"""Per-pixel lifetime fitters — pure NumPy, no Qt, no vendor library.

These are the three estimators ``SwabianTimeTaggerManager``'s worker runs on
a ``(Ny, Nx, n_bins)`` TCSPC cube. They were moved here unchanged from the
manager so a second backend, the processing tests and later the Lifetime
widget can share them. Every fitter compensates the IRF peak the same way:
the caller locates the peak bin on the aggregated decay and passes it in, and
the reported lifetime is referenced to that peak, not to the histogram's
``t = 0``.

Units: ``cube`` holds counts; ``t_axis`` is in seconds; lifetimes come back
in seconds as ``float32`` arrays of the cube's spatial shape.
"""

from __future__ import annotations

import numpy as np


def fit_moment(cube, t_axis, peak_bin: int = 0):
    """Mean photon arrival time (first moment of the histogram).

    For an exponential decay starting at ``t_peak`` the measured mean equals
    ``t_peak + tau``, so ``t_peak`` is subtracted. The fastest method; still
    biased by background and by the IRF width, but it needs no model.
    Returns ``(intensity, lifetime)``, both of the cube's spatial shape.
    """
    intensity = cube.sum(axis=2)
    numer = (cube * t_axis[None, None, :]).sum(axis=2)
    lifetime = np.zeros_like(intensity, dtype=np.float32)
    good = intensity > 0
    t_peak = float(t_axis[int(peak_bin)]) if 0 <= peak_bin < len(t_axis) else 0.0
    lifetime[good] = (numer[good] / intensity[good]) - t_peak
    return intensity.astype(np.float32), lifetime


def fit_phasor(cube, intensity, omega, cos_table, sin_table, t_peak: float):
    """Phasor (first-harmonic Fourier) lifetime with IRF offset compensation.

    Projects each normalised histogram onto ``cos_table`` / ``sin_table``
    (``cos(omega t)`` / ``sin(omega t)`` over the bin centres, precomputed by
    the caller once per scan), then rotates the ``(g, s)`` phasor by
    ``-omega * t_peak``. With the ``e^{+i omega t}`` convention used here
    (``P = g + i s``) a shifted decay ``h_meas(t) = h_true(t - t_peak)`` gives
    ``P_meas = e^{+i omega t_peak} P_true``, so::

        g_true = g cos(phi) + s sin(phi)
        s_true = s cos(phi) - g sin(phi),  phi = omega * t_peak
        tau = s_true / (omega * g_true)

    ``omega`` is ``2 pi f_rep``: the laser period, not the histogram window.
    Returns the lifetime image only; the caller already has ``intensity``.
    """
    h = cube.astype(np.float64) / intensity.clip(1).astype(np.float64)[:, :, None]
    g = (h * cos_table[None, None, :]).sum(axis=2)
    s = (h * sin_table[None, None, :]).sum(axis=2)

    phi = omega * t_peak
    cphi, sphi = np.cos(phi), np.sin(phi)
    g_true = g * cphi + s * sphi
    s_true = s * cphi - g * sphi

    denom = omega * g_true
    with np.errstate(invalid='ignore', divide='ignore'):
        lifetime = np.where(np.abs(denom) > 1e-30, s_true / denom, 0.0)
    return lifetime.astype(np.float32)


def fit_exp1(cube, t_axis_f64, peak_bin: int):
    """Weighted log-linear single-exponential fit on the bins past the peak.

    Minimises ``sum_k w_k (log h_k - a - b t_k)^2`` with ``w_k = sqrt(h_k)``
    (Poisson weighting) over bins ``>= peak_bin``, with the time axis shifted
    so the peak sits at ``t = 0``: the IRF rising edge, which would otherwise
    tilt the slope, is left out of the fit. ``tau = -1 / b``.

    ``t_axis_f64`` has shape ``(1, 1, n_bins)``. Returns the lifetime image.
    """
    n_bins = t_axis_f64.shape[-1]
    k0 = max(0, min(int(peak_bin), n_bins - 2))
    sub_cube = cube[..., k0:]
    t = (t_axis_f64[..., k0:] - t_axis_f64[..., k0:k0 + 1])
    h = sub_cube.astype(np.float64)

    w = np.sqrt(np.where(h > 0, h, 0.0))
    log_h = np.where(h > 0, np.log(h), 0.0)

    sw = w.sum(axis=2)
    swt = (w * t).sum(axis=2)
    swt2 = (w * t ** 2).sum(axis=2)
    swlh = (w * log_h).sum(axis=2)
    swtlh = (w * t * log_h).sum(axis=2)

    det = sw * swt2 - swt ** 2
    with np.errstate(invalid='ignore', divide='ignore'):
        slope = np.where(np.abs(det) > 1e-30,
                         (sw * swtlh - swt * swlh) / det,
                         0.0)

    lifetime = np.where(slope < 0, (-1.0 / slope).astype(np.float32), 0.0)
    return lifetime.astype(np.float32)


__all__ = ["fit_exp1", "fit_moment", "fit_phasor"]
