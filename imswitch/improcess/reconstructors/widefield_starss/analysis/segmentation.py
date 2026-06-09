"""
Segmentation and masking utilities.

Supports three modes:
  - Standard Otsu-based segmentation on the superpixel intensity image.
  - Line-PSF segmentation via Otsu thresholding (``segment_line_psf``):
    appropriate for line-scanning PSF data where the signal is a bright
    continuous stripe, not isolated spots.
  - PSF / point-source detection via local peak finding (``segment_psf_peaks``):
    for bead/spot data on a mosaic camera (rarely needed).
  - Optional pre-masking with a layer mask (TIFF stack of binary masks,
    as produced by Program/single_line_mask.py).
"""
import numpy as np
import skimage as ski
import tifffile as tf
from scipy.ndimage import gaussian_filter

from imswitch.improcess.analysis.segmentation import segment_image

from .containers import PolarizationStats


# =============================================================================
# Layer mask (from single_line_mask.py workflow)
# =============================================================================

def load_layer_mask(path):
    """
    Load a layer mask TIFF.

    The file is either:
      - A single 2D binary/label image, or
      - A stack of binary frames that are summed and thresholded to a single mask.

    Returns a 2D boolean array on the full-camera pixel grid (before superpixel
    downsampling).  Pass it to build_mask() via the `layer_mask` parameter.
    """
    arr = tf.imread(path).astype(np.float32)

    if arr.ndim == 3:
        arr = arr.sum(axis=0)

    return arr > 0


def _apply_layer_mask_to_superpixel_grid(layer_mask_full, superpixel_shape):
    """
    Downsample a full-pixel binary layer mask to the superpixel grid by majority
    vote within each 2×2 superpixel block.

    Parameters
    ----------
    layer_mask_full : 2D bool array, shape (H, W)
        Full-pixel mask.
    superpixel_shape : (h, w)
        Target shape after 2x2 downsampling (= shape of the intensity maps).

    Returns
    -------
    2D bool array of shape superpixel_shape.
    """
    h, w = superpixel_shape
    # The superpixel grid is obtained by even-row/even-col sampling of the mosaic,
    # so the full pixel grid is 2× in each dimension.
    full_h, full_w = h * 2, w * 2

    mh, mw = layer_mask_full.shape

    # Pad if the mask is smaller than expected (e.g. saved with a different ROI).
    # Extra pixels are treated as background (False).
    if mh < full_h or mw < full_w:
        padded = np.zeros((full_h, full_w), dtype=layer_mask_full.dtype)
        padded[:mh, :mw] = layer_mask_full[:mh, :mw]
        mask_work = padded
    else:
        # Crop if larger than expected
        mask_work = layer_mask_full[:full_h, :full_w]

    # Reshape into (h, 2, w, 2) and take majority over the 2×2 block
    blocks = mask_work.reshape(h, 2, w, 2)
    return blocks.mean(axis=(1, 3)) > 0.5


# =============================================================================
# Segmentation
# =============================================================================

def make_simple_mask(image, sigma=2.0, min_size=200, hole_size=200, threshold_scale=1.0):
    """
    Otsu-based segmentation on the superpixel intensity image.

    Parameters
    ----------
    image : 2D ndarray
        Intensity image on the superpixel grid.
    sigma : float
        Gaussian pre-blur radius (superpixels).
    min_size : int
        Minimum object area in superpixels.  Use a small value (~5–20) for PSF
        point-source data; larger values (~200) for extended cell data.
    hole_size : int
        Maximum hole area to fill (superpixels).
    threshold_scale : float
        Multiplier on the Otsu threshold (>1 → stricter, <1 → more inclusive).

    Returns
    -------
    mask : 2D int32 label image (0 = background, 1,2,... = regions).
    """
    blurred = gaussian_filter(image, sigma=sigma)

    try:
        thresh = ski.filters.threshold_otsu(blurred)
    except ValueError:
        # Image is uniform (no foreground/background separation possible).
        # Return an empty mask rather than crashing.
        return np.zeros_like(image, dtype=np.int32)

    binary  = blurred > (threshold_scale * thresh)
    binary  = ski.morphology.remove_small_objects(binary, min_size=min_size)
    binary  = ski.morphology.remove_small_holes(binary, area_threshold=hole_size)
    return ski.measure.label(binary)


def make_generic_segmentation_mask(image, sigma=2.0, min_size=200):
    """
    Segment with the generic ImProcess segmentation analysis kernel.

    This mirrors the ``segmentation`` processor path: Otsu thresholding,
    optional Gaussian smoothing and connected-component filtering. It does not
    apply WFS-specific hole filling or threshold scaling.
    """
    analysis = segment_image(
        image,
        threshold_method="otsu",
        min_area=max(1, int(min_size)),
        smooth_sigma=float(sigma),
    )
    return analysis.labels.astype(np.int32, copy=False)


def build_mask(
    stats_h,
    stats_v=None,
    segment=True,
    sigma=2.0,
    min_size=200,
    hole_size=200,
    threshold_scale=1.0,
    layer_mask=None,
):
    """
    Build a label mask on the superpixel grid.

    Parameters
    ----------
    stats_h : PolarizationStats
    stats_v : PolarizationStats or None
        If provided, the average of both intensity images is used for segmentation.
    segment : bool
        If False, a single all-ones region is returned (no segmentation).
    sigma, min_size, hole_size, threshold_scale : see make_simple_mask().
    layer_mask : 2D bool array or None
        Optional pre-mask on the full pixel grid (output of load_layer_mask()).
        Pixels outside this mask are excluded before Otsu segmentation.

    Returns
    -------
    mask : 2D int32 label image
    base_image : 2D float ndarray used for segmentation
    """
    if stats_v is None:
        base_image = stats_h.intensity_for_segmentation
    else:
        base_image = 0.5 * (
            stats_h.intensity_for_segmentation + stats_v.intensity_for_segmentation
        )

    if not segment:
        mask = np.ones_like(base_image, dtype=np.int32)
        if layer_mask is not None:
            sp_mask = _apply_layer_mask_to_superpixel_grid(layer_mask, base_image.shape)
            mask = np.where(sp_mask, mask, 0)
        return mask, base_image

    seg_image = base_image.copy()

    if layer_mask is not None:
        sp_mask = _apply_layer_mask_to_superpixel_grid(layer_mask, base_image.shape)
        # Zero-out pixels outside the layer mask before Otsu so bright regions
        # outside the mask don't distort the threshold.
        seg_image = np.where(sp_mask, seg_image, 0.0)

    mask = make_simple_mask(
        seg_image,
        sigma=sigma,
        min_size=min_size,
        hole_size=hole_size,
        threshold_scale=threshold_scale,
    )

    return mask, base_image


# =============================================================================
# Line-PSF segmentation (split-detection / beam-splitter mode)
# =============================================================================

def segment_line_psf(
    image,
    sigma=2.0,
    threshold_scale=1.0,
    min_size=5,
    layer_mask_sp=None,
    line_roi=None,
):
    """
    Find the bright line in a line-PSF image via Gaussian smoothing + Otsu thresholding.

    In line-scanning PSF data the excitation beam produces a single bright
    stripe across the image.  Peak-finding (``segment_psf_peaks``) is not
    appropriate here; Otsu thresholding cleanly separates the bright line
    pixels from the dark background.

    When the line is thin relative to the image height, Otsu's threshold
    can be dominated by background.  Use ``line_roi`` to restrict Otsu
    to a row range around the expected line position, giving a much better
    foreground / background ratio.

    Parameters
    ----------
    image : 2D ndarray
        Intensity image (e.g. average of all four anisotropy channel maps).
    sigma : float
        Gaussian pre-blur radius (pixels) applied before thresholding.
    threshold_scale : float
        Multiplier on the Otsu threshold.  Values > 1 select only the
        brightest pixels; values < 1 include more of the line flanks.
    min_size : int
        Minimum connected-region area (pixels).  Removes isolated noise
        pixels that survive thresholding.
    layer_mask_sp : 2D bool array or None
        Optional binary pre-mask on the same pixel grid as ``image``.
        Pixels outside the mask are excluded from thresholding and zeroed
        in the output.
    line_roi : (int, int) or None
        Row range ``(row_start, row_end)`` within the image where the line
        is expected.  Otsu is computed only on this strip, then the resulting
        threshold is applied back to the strip.  Pixels outside the ROI are
        always background.  If None, the full image is used.

    Returns
    -------
    mask : 2D int32 label image  (0 = background, 1, 2, ... = line regions).
    """
    H, W = image.shape

    # Determine the row slice to work on
    if line_roi is not None:
        r0, r1 = int(line_roi[0]), int(line_roi[1])
        r0 = max(0, r0)
        r1 = min(H, r1)
    else:
        r0, r1 = 0, H

    crop = image[r0:r1, :].astype(np.float64)
    blurred_crop = gaussian_filter(crop, sigma=sigma)

    if layer_mask_sp is not None:
        mask_crop = layer_mask_sp[r0:r1, :]
        detect_crop = np.where(mask_crop, blurred_crop, 0.0)
    else:
        detect_crop = blurred_crop

    try:
        thresh = ski.filters.threshold_otsu(detect_crop)
    except ValueError:
        return np.zeros((H, W), dtype=np.int32)

    binary_crop = detect_crop > (threshold_scale * thresh)

    if layer_mask_sp is not None:
        binary_crop = binary_crop & mask_crop

    if min_size > 0:
        binary_crop = ski.morphology.remove_small_objects(binary_crop, min_size=min_size)

    # Embed the cropped binary mask back into the full-size image
    binary_full = np.zeros((H, W), dtype=bool)
    binary_full[r0:r1, :] = binary_crop

    return ski.measure.label(binary_full).astype(np.int32)


# =============================================================================
# PSF / point-source segmentation (mosaic-camera mode)
# =============================================================================

def segment_psf_peaks(
    image,
    sigma=2.0,
    min_distance=5,
    threshold_rel=0.1,
    psf_radius=3,
    layer_mask_sp=None,
):
    """
    Detect PSF spots as local intensity maxima and assign each a circular ROI.

    Unlike Otsu segmentation, this works reliably for sparse point sources
    whose intensity distribution has no clear bimodal structure.

    Parameters
    ----------
    image : 2D ndarray
        Intensity image on the superpixel grid.
    sigma : float
        Gaussian pre-blur before peak detection (superpixels).
        Should roughly match the PSF width on the superpixel grid.
    min_distance : int
        Minimum centre-to-centre distance between detected peaks (superpixels).
        Peaks closer than this are deduplicated, keeping the brighter one.
    threshold_rel : float
        Peaks must exceed ``threshold_rel * image.max()`` to be accepted.
        Increase to reject faint spots.
    psf_radius : int
        Radius of the circular region assigned to each PSF (superpixels).
        If disks overlap, earlier-detected (brighter) spots take priority.
    layer_mask_sp : 2D bool array or None
        Optional binary mask on the superpixel grid.  Pixels outside the mask
        are zeroed before peak detection so that out-of-mask bright spots are
        ignored.

    Returns
    -------
    mask : 2D int32 label image (0 = background, 1, 2, ... = PSF regions).
    """
    from skimage.feature import peak_local_max
    from skimage.draw import disk as sk_disk

    detect_image = gaussian_filter(image.astype(np.float64), sigma=sigma)

    if layer_mask_sp is not None:
        detect_image = np.where(layer_mask_sp, detect_image, 0.0)

    coords = peak_local_max(
        detect_image,
        min_distance=max(1, int(min_distance)),
        threshold_rel=threshold_rel,
    )

    mask = np.zeros_like(image, dtype=np.int32)
    for i, (r, c) in enumerate(coords, start=1):
        rr, cc = sk_disk((r, c), psf_radius, shape=image.shape)
        # Brighter (earlier-ranked) spots take priority on overlap
        free = mask[rr, cc] == 0
        mask[rr[free], cc[free]] = i

    return mask


def build_psf_mask(
    stats_h,
    stats_v=None,
    sigma=2.0,
    min_distance=5,
    threshold_rel=0.1,
    psf_radius=3,
    layer_mask=None,
):
    """
    Build a per-PSF label mask using local peak detection.

    Parameters
    ----------
    stats_h : PolarizationStats
    stats_v : PolarizationStats or None
        If provided, the average of both intensity images is used for detection.
    sigma, min_distance, threshold_rel, psf_radius : see segment_psf_peaks().
    layer_mask : 2D bool array or None
        Full-pixel layer mask (output of load_layer_mask()).

    Returns
    -------
    mask : 2D int32 label image
    base_image : 2D float ndarray used for detection
    """
    if stats_v is None:
        base_image = stats_h.intensity_for_segmentation
    else:
        base_image = 0.5 * (
            stats_h.intensity_for_segmentation + stats_v.intensity_for_segmentation
        )

    layer_mask_sp = None
    if layer_mask is not None:
        layer_mask_sp = _apply_layer_mask_to_superpixel_grid(layer_mask, base_image.shape)

    mask = segment_psf_peaks(
        base_image,
        sigma=sigma,
        min_distance=min_distance,
        threshold_rel=threshold_rel,
        psf_radius=psf_radius,
        layer_mask_sp=layer_mask_sp,
    )

    return mask, base_image
