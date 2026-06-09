"""
Data containers for polarization anisotropy analysis.
"""
import numpy as np
from dataclasses import dataclass


@dataclass
class PolarizationStats:
    """
    Per-superpixel statistics for one acquisition (e.g. H-excitation or V-excitation).
    All arrays are on the super-pixel grid.
    """
    I0_mean: np.ndarray
    I45_mean: np.ndarray
    I90_mean: np.ndarray
    I135_mean: np.ndarray

    I0_var_mean: np.ndarray
    I45_var_mean: np.ndarray
    I90_var_mean: np.ndarray
    I135_var_mean: np.ndarray
    I0_var_signal: np.ndarray
    I45_var_signal: np.ndarray
    I90_var_signal: np.ndarray
    I135_var_signal: np.ndarray

    S0_mean: np.ndarray
    S0_var_mean: np.ndarray
    S0_var_signal: np.ndarray
    S1_mean: np.ndarray
    S1_var_mean: np.ndarray
    S2_mean: np.ndarray
    S2_var_mean: np.ndarray

    IH_mean: np.ndarray
    IH_var_mean: np.ndarray
    IV_mean: np.ndarray
    IV_var_mean: np.ndarray

    intensity_for_segmentation: np.ndarray


@dataclass
class AnisotropyMaps:
    """
    Raw and smoothed anisotropy maps for one pair of acquisitions.
    """
    ihh: np.ndarray
    ihv: np.ndarray
    ivh: np.ndarray
    ivv: np.ndarray

    ihh_var: np.ndarray
    ihv_var: np.ndarray
    ivh_var: np.ndarray
    ivv_var: np.ndarray

    x_raw: np.ndarray
    r_raw: np.ndarray
    r_raw_se: np.ndarray

    x_smooth: np.ndarray
    r_smooth: np.ndarray
    r_smooth_se: np.ndarray

    valid_mask: np.ndarray
    smooth_sigma: float
    anisotropy_mode: str = "stokes"
