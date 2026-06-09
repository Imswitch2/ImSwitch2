"""
Anisotropy formulas and map construction.
"""
import numpy as np
from scipy.ndimage import gaussian_filter
from scipy.optimize import least_squares

from .containers import AnisotropyMaps
from .polarization import safe_inverse_variance


# =============================================================================
# Anisotropy formulas
# =============================================================================

def anisotropy_from_x(ihh, ihv, ivh, ivv, sigma_hh, sigma_hv, sigma_vh, sigma_vv, eps=1e-12):
    """
    Direct x-based anisotropy (calibration-free):

        x = sqrt( (ihv * ivh) / (ihh * ivv) )
        r = (1 - x) / (1 + 2x)

    with first-order propagated SE.

    G and K2 calibration factors cancel algebraically in this formulation.
    """
    ihh = np.maximum(ihh, eps)
    ihv = np.maximum(ihv, eps)
    ivh = np.maximum(ivh, eps)
    ivv = np.maximum(ivv, eps)

    # Guard sigmas: replace NaN/zero with eps so a missing uncertainty estimate
    # does not propagate silently as NaN or produce a division by zero.
    sigma_hh = np.where(np.isfinite(sigma_hh), np.maximum(sigma_hh, 0.0), eps)
    sigma_hv = np.where(np.isfinite(sigma_hv), np.maximum(sigma_hv, 0.0), eps)
    sigma_vh = np.where(np.isfinite(sigma_vh), np.maximum(sigma_vh, 0.0), eps)
    sigma_vv = np.where(np.isfinite(sigma_vv), np.maximum(sigma_vv, 0.0), eps)

    x = np.sqrt((ihv * ivh) / (ihh * ivv))
    r = (1.0 - x) / (1.0 + 2.0 * x)

    # Variance of log(x^2) = sum of relative variances of the four intensities
    var_log_x = 0.25 * (
        (sigma_hv / ihv) ** 2 +
        (sigma_vh / ivh) ** 2 +
        (sigma_hh / ihh) ** 2 +
        (sigma_vv / ivv) ** 2
    )
    sigma_x  = x * np.sqrt(var_log_x)
    dr_dx    = -3.0 / (1.0 + 2.0 * x) ** 2
    sigma_r  = np.abs(dr_dx) * sigma_x

    return x, r, sigma_r


def gaussian_fit_anisotropy(ihh, ihv, ivh, ivv, sigma_hh, sigma_hv, sigma_vh, sigma_vv, eps=1e-12):
    """
    Gaussian-weighted fit to the four-intensity model:

        mu_hh = P
        mu_hv = P * x * r2
        mu_vh = P * x * r1
        mu_vv = P * r1 * r2

    Returns r = (1-x)/(1+2x) with propagated SE.

    Uses pseudo-inverse for covariance estimation to handle near-singular
    Jacobians robustly.
    """
    I     = np.array([ihh, ihv, ivh, ivv], dtype=np.float64)
    sigma = np.maximum(
        np.array([sigma_hh, sigma_hv, sigma_vh, sigma_vv], dtype=np.float64),
        eps,
    )

    def model(params):
        P, x, r1, r2 = params
        return np.array([P, P * x * r2, P * x * r1, P * r1 * r2], dtype=np.float64)

    def residuals(params):
        return (model(params) - I) / sigma

    P0  = max(ihh, eps)
    x0  = np.sqrt(max((ihv * ivh) / max(ivv * ihh, eps), eps))
    r10 = max(ivh / max(ihh * x0, eps), eps)
    r20 = max(ihv / max(ihh * x0, eps), eps)

    initial = np.array([P0, x0, r10, r20], dtype=np.float64)
    bounds  = ([eps, eps, eps, eps], [np.inf, np.inf, np.inf, np.inf])

    result  = least_squares(residuals, initial, bounds=bounds)
    P, x, r1, r2 = result.x
    r = (1.0 - x) / (1.0 + 2.0 * x)

    try:
        J   = result.jac
        # Use pseudo-inverse for robustness on near-singular Jacobians
        cov = np.linalg.pinv(J.T @ J)
        sigma_params = np.sqrt(np.maximum(np.diag(cov), 0.0))
        sigma_x = sigma_params[1]
        dr_dx   = -3.0 / (1.0 + 2.0 * x) ** 2
        sigma_r = np.abs(dr_dx) * sigma_x
    except (np.linalg.LinAlgError, ValueError):
        sigma_x = np.nan
        sigma_r = np.nan

    return {
        "P":              P,
        "x":              x,
        "r1":             r1,
        "r2":             r2,
        "anisotropy":     r,
        "anisotropy_se":  sigma_r,
        "sigma_x":        sigma_x,
        "success":        result.success,
        "cost":           result.cost,
    }


# =============================================================================
# Spatial smoothing of mean and variance
# =============================================================================

def _effective_n_gaussian(sigma_pix):
    """
    Approximate effective sample size of a normalized 2D Gaussian kernel.
    Continuous approximation:  N_eff ~ 4 * pi * sigma^2
    """
    return 4.0 * np.pi * sigma_pix ** 2


def smooth_mean_and_variance(mean_map, var_map, sigma):
    """
    Smooth a mean map and its variance map with a Gaussian kernel.

    Mean:     mu_s  = G(mu)
    Variance: var_s ~ G(var) / N_eff   with N_eff ~ 4*pi*sigma^2

    This is an approximation (assumes locally i.i.d. pixels within the kernel
    support), but gives reasonable scale-dependent uncertainty maps.
    """
    mean_s   = gaussian_filter(mean_map, sigma=sigma)
    neff     = max(_effective_n_gaussian(sigma), 1.0)
    var_s    = gaussian_filter(var_map, sigma=sigma) / neff
    return mean_s, var_s


# =============================================================================
# Anisotropy maps
# =============================================================================

def _build_anisotropy_maps_core(
    ihh, ihv, ivh, ivv,
    ihh_var, ihv_var, ivh_var, ivv_var,
    smooth_sigma=2.0, intensity_threshold=None, eps=1e-12,
):
    """
    Core computation shared by build_anisotropy_maps and
    build_anisotropy_maps_split_detection.
    """
    x_raw, r_raw, r_raw_se = anisotropy_from_x(
        ihh, ihv, ivh, ivv,
        np.sqrt(ihh_var), np.sqrt(ihv_var), np.sqrt(ivh_var), np.sqrt(ivv_var),
        eps=eps,
    )

    ihh_s, ihh_var_s = smooth_mean_and_variance(ihh, ihh_var, smooth_sigma)
    ihv_s, ihv_var_s = smooth_mean_and_variance(ihv, ihv_var, smooth_sigma)
    ivh_s, ivh_var_s = smooth_mean_and_variance(ivh, ivh_var, smooth_sigma)
    ivv_s, ivv_var_s = smooth_mean_and_variance(ivv, ivv_var, smooth_sigma)

    x_smooth, r_smooth, r_smooth_se = anisotropy_from_x(
        ihh_s, ihv_s, ivh_s, ivv_s,
        np.sqrt(ihh_var_s), np.sqrt(ihv_var_s), np.sqrt(ivh_var_s), np.sqrt(ivv_var_s),
        eps=eps,
    )

    total_intensity = ihh + ihv + ivh + ivv
    if intensity_threshold is None:
        valid_mask = np.isfinite(total_intensity)
    else:
        valid_mask = np.isfinite(total_intensity) & (total_intensity > intensity_threshold)

    r_raw    = np.where(valid_mask, r_raw,    np.nan)
    r_raw_se = np.where(valid_mask, r_raw_se, np.nan)
    x_raw    = np.where(valid_mask, x_raw,    np.nan)

    r_smooth    = np.where(valid_mask, r_smooth,    np.nan)
    r_smooth_se = np.where(valid_mask, r_smooth_se, np.nan)
    x_smooth    = np.where(valid_mask, x_smooth,    np.nan)

    return AnisotropyMaps(
        ihh=ihh, ihv=ihv, ivh=ivh, ivv=ivv,
        ihh_var=ihh_var, ihv_var=ihv_var, ivh_var=ivh_var, ivv_var=ivv_var,
        x_raw=x_raw, r_raw=r_raw, r_raw_se=r_raw_se,
        x_smooth=x_smooth, r_smooth=r_smooth, r_smooth_se=r_smooth_se,
        valid_mask=valid_mask,
        smooth_sigma=smooth_sigma,
    )


def build_anisotropy_maps(stats_h, stats_v, smooth_sigma=2.0, intensity_threshold=None, eps=1e-12):
    """
    Create raw and smoothed anisotropy maps — standard (single-camera) mode.

    H and V detection channels are the virtual IH / IV derived from the
    polarization mosaic Stokes parameters (IH ≈ I0°, IV ≈ I90°).

    Parameters
    ----------
    stats_h, stats_v : PolarizationStats
        H- and V-excitation statistics (full-frame polarization mosaic).
    smooth_sigma : float
        Gaussian smoothing sigma in superpixels for the smoothed map.
    intensity_threshold : float or None
        Pixels below this total intensity are marked invalid (NaN).

    Returns
    -------
    AnisotropyMaps
    """
    return _build_anisotropy_maps_core(
        stats_h.IH_mean,     stats_h.IV_mean,     stats_v.IH_mean,     stats_v.IV_mean,
        stats_h.IH_var_mean, stats_h.IV_var_mean, stats_v.IH_var_mean, stats_v.IV_var_mean,
        smooth_sigma=smooth_sigma, intensity_threshold=intensity_threshold, eps=eps,
    )


def build_anisotropy_maps_split_detection(
    ihh, ihv, ivh, ivv,
    ihh_var, ihv_var, ivh_var, ivv_var,
    smooth_sigma=2.0, intensity_threshold=None, eps=1e-12,
):
    """
    Create raw and smoothed anisotropy maps — split-detection (beam-splitter) mode.

    The polarization separation is done optically before the camera, so there
    is no 2×2 mosaic.  Each pixel in each half is a direct intensity reading.
    The four intensity arrays are computed by ``compute_simple_intensity_stats``
    on the corresponding spatial halves of the H and V acquisition stacks:

        I_HH = upper half of _h.tif mean  (H-excitation, H-detection)
        I_HV = lower half of _h.tif mean  (H-excitation, V-detection)
        I_VH = upper half of _v.tif mean  (V-excitation, H-detection)
        I_VV = lower half of _v.tif mean  (V-excitation, V-detection)

    Parameters
    ----------
    ihh, ihv, ivh, ivv : float32 ndarray, shape (H, W)
        Per-pixel mean intensity maps for the four quadrants.
    ihh_var, ihv_var, ivh_var, ivv_var : float32 ndarray, shape (H, W)
        Corresponding variance-of-the-mean (SEM²) maps.
    smooth_sigma : float
        Gaussian smoothing sigma in pixels for the smoothed anisotropy map.
    intensity_threshold : float or None

    Returns
    -------
    AnisotropyMaps
    """
    return _build_anisotropy_maps_core(
        ihh, ihv, ivh, ivv,
        ihh_var, ihv_var, ivh_var, ivv_var,
        smooth_sigma=smooth_sigma, intensity_threshold=intensity_threshold, eps=eps,
    )
