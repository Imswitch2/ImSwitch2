"""
Region-wise pooling and summary statistics.
"""
import numpy as np
import pandas as pd

from imswitch.imcommon.algorithms.fast_regionprops import (
    region_shape_descriptors,
    regionprops_table_fast,
)

from .anisotropy import (
    anisotropy_from_x,
    gaussian_fit_anisotropy,
    standard_anisotropy_intensity_maps,
)
from .polarization import safe_inverse_variance


# =============================================================================
# Pooling helpers
# =============================================================================

def weighted_pool(values, variances, region_mask, eps=1e-12):
    """
    Inverse-variance weighted pooled mean and SE over all valid superpixels in
    a region.

    Returns
    -------
    mean, se, n_valid : float, float, int
    """
    vals  = values[region_mask]
    vars_ = variances[region_mask]

    valid = np.isfinite(vals) & np.isfinite(vars_) & (vars_ > 0)
    vals  = vals[valid]
    vars_ = vars_[valid]

    if len(vals) == 0:
        return np.nan, np.nan, 0

    w    = safe_inverse_variance(vars_, eps=eps)
    mean = np.sum(w * vals) / np.sum(w)
    se   = np.sqrt(1.0 / np.sum(w))
    return mean, se, len(vals)


def weighted_spatial_stats(values, variances, region_mask, eps=1e-12):
    """
    Inverse-variance weighted mean and weighted spatial SD across superpixels
    in one region.

    The SD captures spatial heterogeneity within the region, not uncertainty
    of the mean.

    Returns
    -------
    mean, sd_spatial, n_valid : float, float, int
    """
    vals  = values[region_mask]
    vars_ = variances[region_mask]

    valid = np.isfinite(vals) & np.isfinite(vars_) & (vars_ > 0)
    vals  = vals[valid]
    vars_ = vars_[valid]

    if len(vals) == 0:
        return np.nan, np.nan, 0

    w          = safe_inverse_variance(vars_, eps=eps)
    mean       = np.sum(w * vals) / np.sum(w)
    var_spatial = np.sum(w * (vals - mean) ** 2) / np.sum(w)
    return mean, np.sqrt(var_spatial), len(vals)


def pool_region_polarization(stats, region_mask):
    """
    Pool all relevant quantities from one PolarizationStats object over one
    region.  Returns a dict.
    """
    IH, IH_se, n_IH = weighted_pool(stats.IH_mean, stats.IH_var_mean, region_mask)
    IV, IV_se, n_IV = weighted_pool(stats.IV_mean, stats.IV_var_mean, region_mask)

    S0, S0_se, _ = weighted_pool(stats.S0_mean, stats.S0_var_mean, region_mask)
    S1, S1_se, _ = weighted_pool(stats.S1_mean, stats.S1_var_mean, region_mask)
    S2, S2_se, _ = weighted_pool(stats.S2_mean, stats.S2_var_mean, region_mask)

    return {
        "IH": IH, "IH_se": IH_se,
        "IV": IV, "IV_se": IV_se,
        "S0": S0, "S0_se": S0_se,
        "S1": S1, "S1_se": S1_se,
        "S2": S2, "S2_se": S2_se,
        "n_superpixels": n_IH,
    }


def mean_region_value(values, region_mask):
    vals = values[region_mask]
    valid = np.isfinite(vals)
    if not np.any(valid):
        return np.nan
    return float(np.mean(vals[valid]))


def _empty_geometry():
    return {
        "area_pixels": 0,
        "centroid_y": np.nan,
        "centroid_x": np.nan,
        "bbox_min_y": np.nan,
        "bbox_min_x": np.nan,
        "bbox_max_y": np.nan,
        "bbox_max_x": np.nan,
        "width_pixels": np.nan,
        "height_pixels": np.nan,
        "ellipticity": np.nan,
    }


def region_geometry_table(mask, pixel_scale=1):
    """
    Basic per-label cell geometry for every region in ``mask``, in one pass.

    Vectorized replacement for calling :func:`region_geometry` once per label:
    a single ``regionprops_table_fast`` scatter pass over ``mask`` instead of a
    full-image ``regionprops`` per region (the old O(#regions x image) path).
    Returns ``{label: geometry-dict}`` with exactly the keys and units that
    :func:`region_geometry` produces (``ellipticity`` from the inertia-tensor
    major/minor axes, matching skimage).

    ``pixel_scale`` converts mask-grid coordinates to source-image pixels.  In
    polarization-mosaic mode each mask pixel is one 2x2 source-image superpixel.
    """
    mask = np.asarray(mask)
    if mask.dtype == bool:  # fast core rejects bool as ambiguous
        mask = mask.astype(np.uint8)

    table = regionprops_table_fast(
        mask, properties=("label", "area", "centroid", "bbox", "inertia_tensor")
    )
    descriptors = region_shape_descriptors(table)
    major = descriptors["axis_major_length"]
    minor = descriptors["axis_minor_length"]

    geometry = {}
    for i, label in enumerate(table["label"]):
        maj = float(major[i])
        ellipticity = np.nan if maj <= 0 else 1.0 - (float(minor[i]) / maj)
        min_y, min_x = table["bbox-0"][i], table["bbox-1"][i]
        max_y, max_x = table["bbox-2"][i], table["bbox-3"][i]
        geometry[int(label)] = {
            "area_pixels": int(table["area"][i] * pixel_scale * pixel_scale),
            "centroid_y": float(table["centroid-0"][i] * pixel_scale),
            "centroid_x": float(table["centroid-1"][i] * pixel_scale),
            "bbox_min_y": int(min_y * pixel_scale),
            "bbox_min_x": int(min_x * pixel_scale),
            "bbox_max_y": int(max_y * pixel_scale),
            "bbox_max_x": int(max_x * pixel_scale),
            "width_pixels": int((max_x - min_x) * pixel_scale),
            "height_pixels": int((max_y - min_y) * pixel_scale),
            "ellipticity": ellipticity,
        }
    return geometry


def region_geometry(mask, label, pixel_scale=1):
    """
    Basic cell geometry for a single ``label`` in the mask.

    ``pixel_scale`` converts mask-grid coordinates to source-image pixels.  In
    polarization-mosaic mode each mask pixel is one 2x2 source-image superpixel.

    When measuring every region, prefer :func:`region_geometry_table`, which
    computes all labels in a single vectorized pass.
    """
    return region_geometry_table(mask, pixel_scale=pixel_scale).get(
        int(label), _empty_geometry()
    )


def region_channel_stats(prefix, stats, region_mask):
    channel_specs = {
        "0": ("I0_mean", "I0_var_signal", "I0_var_mean"),
        "45": ("I45_mean", "I45_var_signal", "I45_var_mean"),
        "90": ("I90_mean", "I90_var_signal", "I90_var_mean"),
        "135": ("I135_mean", "I135_var_signal", "I135_var_mean"),
        "total": ("S0_mean", "S0_var_signal", "S0_var_mean"),
    }
    out = {}
    for channel, (mean_attr, var_attr, fallback_var_attr) in channel_specs.items():
        var_map = getattr(stats, var_attr, getattr(stats, fallback_var_attr))
        out[f"{prefix}_{channel}_mean_signal"] = mean_region_value(
            getattr(stats, mean_attr), region_mask
        )
        out[f"{prefix}_{channel}_signal_variance"] = mean_region_value(
            var_map, region_mask
        )
    return out


def split_region_channel_stats(region_mask, ihh_map, ihv_map, ivh_map, ivv_map,
                               ihh_var, ihv_var, ivh_var, ivv_var):
    channel_specs = {
        "H_total": (ihh_map, ihh_var),
        "V_total": (ivv_map, ivv_var),
        "HH": (ihh_map, ihh_var),
        "HV": (ihv_map, ihv_var),
        "VH": (ivh_map, ivh_var),
        "VV": (ivv_map, ivv_var),
    }
    out = {}
    for channel, (mean_map, var_map) in channel_specs.items():
        out[f"{channel}_mean_signal"] = mean_region_value(mean_map, region_mask)
        out[f"{channel}_signal_variance"] = mean_region_value(var_map, region_mask)
    return out


# =============================================================================
# Region analysis
# =============================================================================

def analyze_regions(stats_h, stats_v, anis_maps, mask, anisotropy_mode="stokes"):
    """
    Region-wise analysis.

    For each labelled region in ``mask`` computes:
    - pooled direct anisotropy (x-based formula on pooled intensities from
      ``anisotropy_mode``)
    - pooled Gaussian-fit anisotropy
    - weighted spatial SD of raw and smoothed per-pixel anisotropy
    - Stokes DoLP / AoLP for H and V excitation

    Parameters
    ----------
    stats_h, stats_v : PolarizationStats
    anis_maps : AnisotropyMaps
    mask : 2D int label array  (0 = background)

    Returns
    -------
    pandas.DataFrame, one row per region.
    """
    labels = np.unique(mask)
    labels = labels[labels > 0]

    geometry_table = region_geometry_table(mask, pixel_scale=2)

    results = []

    for label in labels:
        region_mask = (mask == label)
        geometry = geometry_table.get(int(label), _empty_geometry())

        pooled_h = pool_region_polarization(stats_h, region_mask)
        pooled_v = pool_region_polarization(stats_v, region_mask)
        ihh_map, ihv_map, ivh_map, ivv_map, ihh_var, ihv_var, ivh_var, ivv_var = (
            standard_anisotropy_intensity_maps(stats_h, stats_v, mode=anisotropy_mode)
        )
        ihh, sigma_hh, _ = weighted_pool(ihh_map, ihh_var, region_mask)
        ihv, sigma_hv, _ = weighted_pool(ihv_map, ihv_var, region_mask)
        ivh, sigma_vh, _ = weighted_pool(ivh_map, ivh_var, region_mask)
        ivv, sigma_vv, _ = weighted_pool(ivv_map, ivv_var, region_mask)

        x_direct, r_direct, r_direct_se = anisotropy_from_x(
            ihh, ihv, ivh, ivv,
            sigma_hh, sigma_hv, sigma_vh, sigma_vv,
        )

        fit = gaussian_fit_anisotropy(
            ihh, ihv, ivh, ivv,
            sigma_hh, sigma_hv, sigma_vh, sigma_vv,
        )

        _, r_raw_spatial_sd, _ = weighted_spatial_stats(
            anis_maps.r_raw,
            np.maximum(anis_maps.r_raw_se ** 2, 1e-12),
            region_mask,
        )

        _, r_smooth_spatial_sd, _ = weighted_spatial_stats(
            anis_maps.r_smooth,
            np.maximum(anis_maps.r_smooth_se ** 2, 1e-12),
            region_mask,
        )

        r_raw_mean_map, r_raw_mean_map_se, _ = weighted_pool(
            anis_maps.r_raw,
            np.maximum(anis_maps.r_raw_se ** 2, 1e-12),
            region_mask,
        )

        r_smooth_mean_map, r_smooth_mean_map_se, _ = weighted_pool(
            anis_maps.r_smooth,
            np.maximum(anis_maps.r_smooth_se ** 2, 1e-12),
            region_mask,
        )

        dolp_h = (
            np.sqrt(pooled_h["S1"] ** 2 + pooled_h["S2"] ** 2)
            / max(pooled_h["S0"], 1e-12)
        )
        aolp_h = 0.5 * np.arctan2(pooled_h["S2"], pooled_h["S1"])

        dolp_v = (
            np.sqrt(pooled_v["S1"] ** 2 + pooled_v["S2"] ** 2)
            / max(pooled_v["S0"], 1e-12)
        )
        aolp_v = 0.5 * np.arctan2(pooled_v["S2"], pooled_v["S1"])

        results.append({
            "label":         int(label),
            "area_superpixels": int(np.sum(region_mask)),
            **geometry,

            "ihh": ihh, "ihh_frame_se": sigma_hh,
            "ihv": ihv, "ihv_frame_se": sigma_hv,
            "ivh": ivh, "ivh_frame_se": sigma_vh,
            "ivv": ivv, "ivv_frame_se": sigma_vv,
            "anisotropy_mode": anisotropy_mode,

            "x_direct":                     x_direct,
            "anisotropy_direct":            r_direct,
            "anisotropy_direct_frame_se":   r_direct_se,

            "x_fit":                        fit["x"],
            "anisotropy_fit":               fit["anisotropy"],
            "anisotropy_fit_frame_se":      fit["anisotropy_se"],
            "fit_success":                  fit["success"],
            "fit_cost":                     fit["cost"],

            "anisotropy_raw_map_mean":      r_raw_mean_map,
            "anisotropy_raw_map_mean_se":   r_raw_mean_map_se,
            "anisotropy_raw_map_spatial_sd": r_raw_spatial_sd,

            "anisotropy_smooth_map_mean":      r_smooth_mean_map,
            "anisotropy_smooth_map_mean_se":   r_smooth_mean_map_se,
            "anisotropy_smooth_map_spatial_sd": r_smooth_spatial_sd,

            "Hexc_DoLP":    dolp_h,
            "Hexc_AoLP_rad": aolp_h,
            "Vexc_DoLP":    dolp_v,
            "Vexc_AoLP_rad": aolp_v,

            "Hexc_S0": pooled_h["S0"],
            "Hexc_S1": pooled_h["S1"],
            "Hexc_S2": pooled_h["S2"],
            "Vexc_S0": pooled_v["S0"],
            "Vexc_S1": pooled_v["S1"],
            "Vexc_S2": pooled_v["S2"],
            **region_channel_stats("H", stats_h, region_mask),
            **region_channel_stats("V", stats_v, region_mask),
        })

    return pd.DataFrame(results)


def analyze_split_detection_line_psf(
    ihh_map, ihv_map, ivh_map, ivv_map,
    ihh_var, ihv_var, ivh_var, ivv_var,
    upper_mask,
    lower_mask,
):
    """
    Pooled anisotropy for a line-PSF acquired in split-detection mode.

    H-detection channels (IHH, IVH) are pooled over ``upper_mask`` and
    V-detection channels (IHV, IVV) over ``lower_mask``.  The two halves
    are optically conjugate but the line typically appears at different row
    positions within each half (both near the beam-splitter boundary).
    Using per-half masks avoids diluting each channel with dark background
    pixels from the opposite half.

    Parameters
    ----------
    ihh_map, ihv_map, ivh_map, ivv_map : float32 ndarray, shape (H, W)
    ihh_var, ihv_var, ivh_var, ivv_var : float32 ndarray, shape (H, W)
    upper_mask : 2D int or bool array
        Label/binary mask identifying the line pixels in the upper (H-detection)
        half.  Non-zero pixels are pooled.
    lower_mask : 2D int or bool array
        Label/binary mask identifying the line pixels in the lower (V-detection)
        half.  Non-zero pixels are pooled.

    Returns
    -------
    pandas.DataFrame — one row (the single line region).
    """
    upper_bool = upper_mask > 0
    lower_bool = lower_mask > 0
    combined_mask = np.where(upper_bool | lower_bool, 1, 0).astype(np.uint8)
    geometry = region_geometry(combined_mask, 1, pixel_scale=1)

    ihh, sigma_hh, n_upper = weighted_pool(ihh_map, ihh_var, upper_bool)
    ivh, sigma_vh, _       = weighted_pool(ivh_map, ivh_var, upper_bool)
    ihv, sigma_hv, n_lower = weighted_pool(ihv_map, ihv_var, lower_bool)
    ivv, sigma_vv, _       = weighted_pool(ivv_map, ivv_var, lower_bool)

    x_direct, r_direct, r_direct_se = anisotropy_from_x(
        ihh, ihv, ivh, ivv,
        sigma_hh, sigma_hv, sigma_vh, sigma_vv,
    )

    fit = gaussian_fit_anisotropy(
        ihh, ihv, ivh, ivv,
        sigma_hh, sigma_hv, sigma_vh, sigma_vv,
    )

    # Per-pixel anisotropy maps are NOT physically meaningful in this mode
    # because the upper and lower halves are not spatially registered (the
    # line sits at different row positions in each half).  Map-based stats
    # are set to NaN to avoid reporting misleading numbers.

    return pd.DataFrame([{
        "label":                            1,
        "area_superpixels":                 int(n_upper + n_lower),
        **geometry,
        "area_superpixels_upper":           int(n_upper),
        "area_superpixels_lower":           int(n_lower),

        "ihh": ihh, "ihh_frame_se": sigma_hh,
        "ihv": ihv, "ihv_frame_se": sigma_hv,
        "ivh": ivh, "ivh_frame_se": sigma_vh,
        "ivv": ivv, "ivv_frame_se": sigma_vv,

        "x_direct":                         x_direct,
        "anisotropy_direct":                r_direct,
        "anisotropy_direct_frame_se":       r_direct_se,

        "x_fit":                            fit["x"],
        "anisotropy_fit":                   fit["anisotropy"],
        "anisotropy_fit_frame_se":          fit["anisotropy_se"],
        "fit_success":                      fit["success"],
        "fit_cost":                         fit["cost"],

        "anisotropy_raw_map_mean":          np.nan,
        "anisotropy_raw_map_mean_se":       np.nan,
        "anisotropy_raw_map_spatial_sd":    np.nan,

        "anisotropy_smooth_map_mean":       np.nan,
        "anisotropy_smooth_map_mean_se":    np.nan,
        "anisotropy_smooth_map_spatial_sd": np.nan,
        **split_region_channel_stats(
            upper_bool | lower_bool,
            ihh_map, ihv_map, ivh_map, ivv_map,
            ihh_var, ihv_var, ivh_var, ivv_var,
        ),
    }])


def analyze_regions_split_detection(
    ihh_map, ihv_map, ivh_map, ivv_map,
    ihh_var, ihv_var, ivh_var, ivv_var,
    anis_maps, mask,
):
    """
    Region-wise analysis for split-detection (beam-splitter) PSF data.

    The four intensity maps come directly from ``compute_simple_intensity_stats``
    on each spatial half of the H / V stacks.  There is no polarization mosaic,
    so Stokes-based DoLP / AoLP columns are omitted.

    Parameters
    ----------
    ihh_map, ihv_map, ivh_map, ivv_map : float32 ndarray, shape (H, W)
        Per-pixel mean intensity maps (HH, HV, VH, VV quadrants).
    ihh_var, ihv_var, ivh_var, ivv_var : float32 ndarray, shape (H, W)
        Corresponding variance-of-the-mean (SEM²) maps.
    anis_maps : AnisotropyMaps
    mask : 2D int label array  (0 = background)

    Returns
    -------
    pandas.DataFrame — same core columns as analyze_regions so that reporting
    and plotting functions work without modification.
    """
    labels = np.unique(mask)
    labels = labels[labels > 0]

    geometry_table = region_geometry_table(mask, pixel_scale=1)

    results = []

    for label in labels:
        region_mask = (mask == label)
        geometry = geometry_table.get(int(label), _empty_geometry())

        ihh, sigma_hh, _ = weighted_pool(ihh_map, ihh_var, region_mask)
        ihv, sigma_hv, _ = weighted_pool(ihv_map, ihv_var, region_mask)
        ivh, sigma_vh, _ = weighted_pool(ivh_map, ivh_var, region_mask)
        ivv, sigma_vv, _ = weighted_pool(ivv_map, ivv_var, region_mask)

        x_direct, r_direct, r_direct_se = anisotropy_from_x(
            ihh, ihv, ivh, ivv,
            sigma_hh, sigma_hv, sigma_vh, sigma_vv,
        )

        fit = gaussian_fit_anisotropy(
            ihh, ihv, ivh, ivv,
            sigma_hh, sigma_hv, sigma_vh, sigma_vv,
        )

        _, r_raw_spatial_sd, _ = weighted_spatial_stats(
            anis_maps.r_raw,
            np.maximum(anis_maps.r_raw_se ** 2, 1e-12),
            region_mask,
        )

        _, r_smooth_spatial_sd, _ = weighted_spatial_stats(
            anis_maps.r_smooth,
            np.maximum(anis_maps.r_smooth_se ** 2, 1e-12),
            region_mask,
        )

        r_raw_mean_map, r_raw_mean_map_se, _ = weighted_pool(
            anis_maps.r_raw,
            np.maximum(anis_maps.r_raw_se ** 2, 1e-12),
            region_mask,
        )

        r_smooth_mean_map, r_smooth_mean_map_se, _ = weighted_pool(
            anis_maps.r_smooth,
            np.maximum(anis_maps.r_smooth_se ** 2, 1e-12),
            region_mask,
        )

        results.append({
            "label":            int(label),
            "area_superpixels": int(np.sum(region_mask)),
            **geometry,

            "ihh": ihh, "ihh_frame_se": sigma_hh,
            "ihv": ihv, "ihv_frame_se": sigma_hv,
            "ivh": ivh, "ivh_frame_se": sigma_vh,
            "ivv": ivv, "ivv_frame_se": sigma_vv,

            "x_direct":                     x_direct,
            "anisotropy_direct":            r_direct,
            "anisotropy_direct_frame_se":   r_direct_se,

            "x_fit":                        fit["x"],
            "anisotropy_fit":               fit["anisotropy"],
            "anisotropy_fit_frame_se":      fit["anisotropy_se"],
            "fit_success":                  fit["success"],
            "fit_cost":                     fit["cost"],

            "anisotropy_raw_map_mean":      r_raw_mean_map,
            "anisotropy_raw_map_mean_se":   r_raw_mean_map_se,
            "anisotropy_raw_map_spatial_sd": r_raw_spatial_sd,

            "anisotropy_smooth_map_mean":      r_smooth_mean_map,
            "anisotropy_smooth_map_mean_se":   r_smooth_mean_map_se,
            "anisotropy_smooth_map_spatial_sd": r_smooth_spatial_sd,
            **split_region_channel_stats(
                region_mask,
                ihh_map, ihv_map, ivh_map, ivv_map,
                ihh_var, ihv_var, ivh_var, ivv_var,
            ),
        })

    return pd.DataFrame(results)
