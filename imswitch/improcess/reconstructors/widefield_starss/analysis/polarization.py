"""
Polarization-channel splitting and per-superpixel statistics.
"""
import numpy as np

from .containers import PolarizationStats


# =============================================================================
# Mosaic splitting
# =============================================================================

def split_frame_into4(img):
    """
    Split one polarization mosaic frame into the four analyzer channels.
    Pixel convention (row, col):
        I0   -> odd row,  odd col
        I45  -> odd row,  even col
        I90  -> even row, even col
        I135 -> even row, odd col
    """
    img_0   = img[1::2, 1::2]
    img_45  = img[1::2, ::2]
    img_90  = img[::2,  ::2]
    img_135 = img[::2,  1::2]
    return img_0, img_45, img_90, img_135


def split_stack_into4(stack):
    """
    Split a stack of polarization mosaic frames into four analyzer stacks.
    stack shape: (N, H, W)
    """
    if stack.ndim <3:
        stack = np.expand_dims(stack, axis=0)
    img_0   = stack[:, 1::2, 1::2]
    img_45  = stack[:, 1::2, ::2]
    img_90  = stack[:, ::2,  ::2]
    img_135 = stack[:, ::2,  1::2]
    return img_0, img_45, img_90, img_135


# =============================================================================
# Basic math helpers
# =============================================================================

def safe_inverse_variance(var, eps=1e-12):
    return 1.0 / np.maximum(var, eps)


def weighted_mean_two_estimates(m1, v1, m2, v2, eps=1e-12):
    """Inverse-variance weighted mean of two independent estimates."""
    w1 = safe_inverse_variance(v1, eps=eps)
    w2 = safe_inverse_variance(v2, eps=eps)
    mean = (w1 * m1 + w2 * m2) / (w1 + w2)
    var  = 1.0 / (w1 + w2)
    return mean, var


# =============================================================================
# Polarization statistics
# =============================================================================

def compute_polarization_stats(signal_stack, background_stack):
    """
    Compute per-superpixel means and frame-based variance-of-the-mean
    for all analyzer channels, then construct robust S0, S1, S2 and virtual H/V.

    Parameters
    ----------
    signal_stack, background_stack : ndarray, shape (N, H, W)
        Paired signal and background frames (already split by the loader).
        Each frame is a full polarization mosaic.

    Returns
    -------
    PolarizationStats
    """
    s0, s45, s90, s135 = split_stack_into4(signal_stack)
    b0, b45, b90, b135 = split_stack_into4(background_stack)

    c0   = s0   - b0
    c45  = s45  - b45
    c90  = s90  - b90
    c135 = s135 - b135

    n = c0.shape[0]

    # Use float64 only for the accumulation step so that mean/variance are
    # numerically accurate; the final outputs are cast back to float32.
    I0_mean   = np.mean(c0,   axis=0, dtype=np.float64).astype(np.float32)
    I45_mean  = np.mean(c45,  axis=0, dtype=np.float64).astype(np.float32)
    I90_mean  = np.mean(c90,  axis=0, dtype=np.float64).astype(np.float32)
    I135_mean = np.mean(c135, axis=0, dtype=np.float64).astype(np.float32)

    # Frame-based variance of the mean (SEM^2)
    if n < 2:
        I0_var_mean = np.zeros_like(I0_mean, dtype=np.float32)
        I45_var_mean = np.zeros_like(I45_mean, dtype=np.float32)
        I90_var_mean = np.zeros_like(I90_mean, dtype=np.float32)
        I135_var_mean = np.zeros_like(I135_mean, dtype=np.float32)
        I0_var_signal = np.zeros_like(I0_mean, dtype=np.float32)
        I45_var_signal = np.zeros_like(I45_mean, dtype=np.float32)
        I90_var_signal = np.zeros_like(I90_mean, dtype=np.float32)
        I135_var_signal = np.zeros_like(I135_mean, dtype=np.float32)
    else:
        I0_var_signal = np.var(c0, axis=0, ddof=1, dtype=np.float64).astype(np.float32)
        I45_var_signal = np.var(c45, axis=0, ddof=1, dtype=np.float64).astype(np.float32)
        I90_var_signal = np.var(c90, axis=0, ddof=1, dtype=np.float64).astype(np.float32)
        I135_var_signal = np.var(c135, axis=0, ddof=1, dtype=np.float64).astype(np.float32)
        I0_var_mean = (I0_var_signal / n).astype(np.float32)
        I45_var_mean = (I45_var_signal / n).astype(np.float32)
        I90_var_mean = (I90_var_signal / n).astype(np.float32)
        I135_var_mean = (I135_var_signal / n).astype(np.float32)

    # Robust S0: inverse-variance weighted combination of two independent estimates
    A_mean     = I0_mean  + I90_mean
    B_mean     = I45_mean + I135_mean
    A_var_mean = I0_var_mean  + I90_var_mean
    B_var_mean = I45_var_mean + I135_var_mean
    A_var_signal = I0_var_signal + I90_var_signal
    B_var_signal = I45_var_signal + I135_var_signal

    S0_mean, S0_var_mean = weighted_mean_two_estimates(A_mean, A_var_mean, B_mean, B_var_mean)
    _, S0_var_signal = weighted_mean_two_estimates(A_mean, A_var_signal, B_mean, B_var_signal)

    S1_mean     = I0_mean  - I90_mean
    S1_var_mean = I0_var_mean + I90_var_mean

    S2_mean     = I45_mean  - I135_mean
    S2_var_mean = I45_var_mean + I135_var_mean

    # Virtual H/V from Stokes (ignores cross-terms between S0 and S1, first-order approx)
    IH_mean = 0.5 * (S0_mean + S1_mean)
    IV_mean = 0.5 * (S0_mean - S1_mean)

    IH_var_mean = 0.25 * (S0_var_mean + S1_var_mean)
    IV_var_mean = 0.25 * (S0_var_mean + S1_var_mean)

    return PolarizationStats(
        I0_mean=I0_mean,
        I45_mean=I45_mean,
        I90_mean=I90_mean,
        I135_mean=I135_mean,
        I0_var_mean=I0_var_mean,
        I45_var_mean=I45_var_mean,
        I90_var_mean=I90_var_mean,
        I135_var_mean=I135_var_mean,
        I0_var_signal=I0_var_signal,
        I45_var_signal=I45_var_signal,
        I90_var_signal=I90_var_signal,
        I135_var_signal=I135_var_signal,
        S0_mean=S0_mean,
        S0_var_mean=S0_var_mean,
        S0_var_signal=S0_var_signal,
        S1_mean=S1_mean,
        S1_var_mean=S1_var_mean,
        S2_mean=S2_mean,
        S2_var_mean=S2_var_mean,
        IH_mean=IH_mean,
        IH_var_mean=IH_var_mean,
        IV_mean=IV_mean,
        IV_var_mean=IV_var_mean,
        intensity_for_segmentation=S0_mean.copy(),
    )


def compute_simple_intensity_stats(signal_stack, background_stack):
    """
    Compute per-pixel mean and variance-of-the-mean for plain intensity data
    (no polarization mosaic).

    Used for split-detection PSF measurements where the polarization separation
    is done optically before the camera, so every pixel is a direct intensity
    reading in a single detection channel.

    Parameters
    ----------
    signal_stack, background_stack : ndarray, shape (N, H, W)
        Paired signal and background frames (already selected by the loader).

    Returns
    -------
    mean : float32 ndarray, shape (H, W)
    var_mean : float32 ndarray, shape (H, W)
        Variance of the mean (SEM²) across the N paired frames.
    """
    n = signal_stack.shape[0]
    if n < 2:
        raise ValueError("Need at least 2 paired frames to estimate variance.")

    corrected = signal_stack - background_stack
    mean = np.mean(corrected, axis=0, dtype=np.float64).astype(np.float32)
    var_mean = (np.var(corrected, axis=0, ddof=1) / n).astype(np.float32)
    return mean, var_mean
