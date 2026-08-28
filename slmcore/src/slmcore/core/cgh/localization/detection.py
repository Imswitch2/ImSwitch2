"""Target-independent subpixel spot detection."""

from __future__ import annotations



import numpy as np
from scipy.ndimage import gaussian_filter,maximum_filter

from .model import DetectedSpots,SpotDetectionOptions


def crop_image(
    image: np.ndarray,
    options: SpotDetectionOptions,
    crop_coord: tuple[int, int, int, int] | None=None,
):
    """Crop a 2D image automatically or reuse an explicit crop.

    Automatic cropping finds the thresholded bounding box directly and expands
    that box by the same amount that the former binary-dilation step contributed
    to its extent.  Only the final crop is converted/copied to float64.
    """
    source = np.asarray(image)
    if source.ndim != 2:
        raise ValueError("Localization image must be two-dimensional")
    if not np.all(np.isfinite(source)):
        raise ValueError("Localization image contains non-finite values")

    height,width = source.shape

    if crop_coord is None:
        maximum = float(np.max(source)) if source.size else 0.0
        if maximum <= 0:
            coord = (0,height,0,width)
        else:
            mask = source > maximum * float(options.crop_threshold)
            yy,xx = np.nonzero(mask)
            if yy.size == 0:
                coord = (0,height,0,width)
            else:
                y1 = int(yy.min())
                y2 = int(yy.max()) + 1
                x1 = int(xx.min())
                x2 = int(xx.max()) + 1

                # Equivalent bounding-box expansion to binary_dilation with a
                # square ``size x size`` structuring element and default origin.
                size = int(options.dilation_kernel_size)
                lower_margin = size // 2
                upper_margin = (size - 1) // 2

                coord = (
                    max(0,y1 - lower_margin),
                    min(height,y2 + upper_margin),
                    max(0,x1 - lower_margin),
                    min(width,x2 + upper_margin),
                )
    else:
        coord = tuple(int(v) for v in crop_coord)
        if len(coord) != 4:
            raise ValueError("crop_coord must be (y1, y2, x1, x2)")

    y1,y2,x1,x2 = coord
    if not (0 <= y1 < y2 <= height and 0 <= x1 < x2 <= width):
        raise ValueError("crop_coord lies outside the localization image")

    cropped = np.array(
        source[y1:y2,x1:x2],
        dtype=np.float64,
        copy=True,
    )
    return cropped,coord


def detect_spots(
    image: np.ndarray,
    options: SpotDetectionOptions=SpotDetectionOptions(),
    *,
    expected_period_px: float | tuple[float, float] | None=None,
    crop_coord: tuple[int, int, int, int] | None=None,
) -> DetectedSpots:
    """Detect unordered spot centers with local-maxima + subpixel refinement."""
    cropped,crop_coord = crop_image(image,options,crop_coord=crop_coord)
    processed = _preprocess(cropped,float(options.blur_sigma))

    min_distance = options.min_distance_px
    if min_distance is None:
        # Keep the generic detector safe for registration fallback.  A supplied
        # period may relax suppression for very dense lattices, but it must not
        # suppress spots more aggressively than the target-independent default.
        default_min_distance = max(
            1.0,2.0 * int(options.refinement_window_px) + 1.0
        )
        min_distance = default_min_distance
        if expected_period_px is not None:
            if np.isscalar(expected_period_px):
                expected_min = float(expected_period_px)
            else:
                expected_min = min(
                    float(expected_period_px[0]),
                    float(expected_period_px[1]),
                )
            guided_distance = (
                expected_min * float(options.min_distance_fraction)
            )
            min_distance = min(default_min_distance,guided_distance)
    min_distance = max(1.0,float(min_distance))

    positions,scores = _local_maxima(
        processed,
        threshold_rel=float(options.threshold_rel),
        min_distance=min_distance,
        max_spots=options.max_spots,
    )
    if positions.shape[1] == 0:
        raise RuntimeError("No localization spots were detected")

    positions = _refine_positions(
        cropped,positions,
        method=str(options.refinement_method),
        window=int(options.refinement_window_px),
    )
    intensities = _sample_bilinear(cropped,positions)
    return DetectedSpots(
        positions_px=positions,
        intensities=intensities,
        scores=scores,
        cropped_image=cropped,
        processed_image=processed,
        crop_coord=crop_coord,
        diagnostics={
            "min_distance_px":min_distance,
            "threshold_rel":float(options.threshold_rel),
        },
    )


def _preprocess(image: np.ndarray,blur_sigma: float) -> np.ndarray:
    signal = np.asarray(image,dtype=np.float64)
    if signal.size == 0:
        return np.array(signal,copy=True)
    if blur_sigma > 0:
        smoothed = gaussian_filter(signal,blur_sigma)
        background = gaussian_filter(signal,max(3.0,4.0 * blur_sigma))
        signal = smoothed - background
    else:
        signal = signal - float(np.median(signal))
    signal = np.maximum(signal,0.0)
    maximum = float(np.max(signal))
    if maximum > 0:
        signal = signal / maximum
    return signal


def _local_maxima(processed,threshold_rel,min_distance,max_spots):
    """Find separated local maxima using exact greedy semantics in O(N)-like time.

    ``maximum_filter`` performs the expensive image-space neighborhood test.
    The remaining greedy pass is retained to resolve plateaus/ties exactly, but
    accepted maxima are indexed in spatial cells so each candidate compares only
    with nearby accepted maxima instead of all previously selected points.
    """
    radius = max(1,int(round(min_distance)))
    size = 2 * radius + 1
    maximum = maximum_filter(processed,size=size,mode="nearest")
    mask = (processed == maximum) & (processed >= float(threshold_rel))
    yy,xx = np.nonzero(mask)
    if yy.size == 0:
        return np.empty((2,0),dtype=np.float64),np.empty((0,),dtype=np.float64)

    values = processed[yy,xx]
    order = np.argsort(values)[::-1]

    min_distance = float(min_distance)
    min_distance_sq = min_distance ** 2
    cell_size = min_distance

    selected_x = []
    selected_y = []
    selected_scores = []
    cells = {}

    for raw_index in order:
        x = float(xx[raw_index])
        y = float(yy[raw_index])
        cell_x = int(np.floor(x / cell_size))
        cell_y = int(np.floor(y / cell_size))

        keep = True
        for neighbor_y in range(cell_y - 1,cell_y + 2):
            if not keep:
                break
            for neighbor_x in range(cell_x - 1,cell_x + 2):
                for selected_index in cells.get((neighbor_x,neighbor_y),()):
                    dx = x - selected_x[selected_index]
                    dy = y - selected_y[selected_index]
                    if dx*dx + dy*dy < min_distance_sq:
                        keep = False
                        break
                if not keep:
                    break

        if not keep:
            continue

        selected_index = len(selected_x)
        selected_x.append(x)
        selected_y.append(y)
        selected_scores.append(float(values[raw_index]))
        cells.setdefault((cell_x,cell_y),[]).append(selected_index)

        if max_spots is not None and selected_index + 1 >= int(max_spots):
            break

    positions = np.array(
        [selected_x,selected_y],
        dtype=np.float64,
    )
    scores = np.asarray(selected_scores,dtype=np.float64)
    return positions,scores


def _refine_positions(image,positions,method,window):
    """Refine detected maxima, vectorizing all full-sized interior patches."""
    if window <= 0 or positions.shape[1] == 0:
        return np.array(positions,dtype=np.float64,copy=True)

    image = np.asarray(image,dtype=np.float64)
    h,w = image.shape
    count = positions.shape[1]
    refined = np.array(positions,dtype=np.float64,copy=True)

    centers_x = np.floor(positions[0]).astype(np.int64)
    centers_y = np.floor(positions[1]).astype(np.int64)

    interior = (
        (centers_x - window >= 0)
        & (centers_x + window < w)
        & (centers_y - window >= 0)
        & (centers_y + window < h)
    )
    interior_indices = np.flatnonzero(interior)

    if interior_indices.size:
        cx = centers_x[interior_indices]
        cy = centers_y[interior_indices]
        offsets = np.arange(-window,window + 1,dtype=np.int64)

        patches = image[
            cy[:,None,None] + offsets[None,:,None],
            cx[:,None,None] + offsets[None,None,:],
        ]

        if method == "max":
            flat_indices = np.argmax(
                patches.reshape(patches.shape[0],-1),
                axis=1,
            )
            patch_size = 2 * window + 1
            iy = flat_indices // patch_size
            ix = flat_indices % patch_size
            refined[0,interior_indices] = cx + offsets[ix]
            refined[1,interior_indices] = cy + offsets[iy]
        else:
            minima = np.min(patches,axis=(1,2),keepdims=True)
            weights = patches - minima
            totals = np.sum(weights,axis=(1,2))
            valid = totals > 0

            if np.any(valid):
                weights_valid = weights[valid]
                totals_valid = totals[valid]
                dx = np.sum(
                    weights_valid * offsets[None,None,:],
                    axis=(1,2),
                ) / totals_valid
                dy = np.sum(
                    weights_valid * offsets[None,:,None],
                    axis=(1,2),
                ) / totals_valid

                valid_indices = interior_indices[valid]
                refined[0,valid_indices] = centers_x[valid_indices] + dx
                refined[1,valid_indices] = centers_y[valid_indices] + dy

    # Border patches have variable shapes. They are uncommon for ordinary
    # localization crops, so retaining the scalar reference implementation for
    # only those points keeps exact edge semantics without slowing the common
    # case.
    for index in np.flatnonzero(~interior):
        x0,y0 = positions[:,index]
        x1 = max(0,int(np.floor(x0)) - window)
        x2 = min(w,int(np.floor(x0)) + window + 1)
        y1 = max(0,int(np.floor(y0)) - window)
        y2 = min(h,int(np.floor(y0)) + window + 1)
        patch = np.asarray(image[y1:y2,x1:x2],dtype=np.float64)
        if patch.size == 0:
            refined[:,index] = (x0,y0)
            continue
        if method == "max":
            iy,ix = np.unravel_index(int(np.argmax(patch)),patch.shape)
            refined[:,index] = (x1 + ix,y1 + iy)
            continue
        weights = patch - float(np.min(patch))
        total = float(np.sum(weights))
        if total <= 0:
            refined[:,index] = (x0,y0)
            continue
        yy,xx = np.meshgrid(
            np.arange(y1,y2,dtype=np.float64),
            np.arange(x1,x2,dtype=np.float64),
            indexing="ij",
        )
        refined[0,index] = float(np.sum(xx * weights) / total)
        refined[1,index] = float(np.sum(yy * weights) / total)

    return refined


def _sample_bilinear(image,positions):
    """Sample all spot intensities with vectorized bilinear interpolation."""
    image = np.asarray(image,dtype=np.float64)
    h,w = image.shape
    if positions.shape[1] == 0:
        return np.empty((0,),dtype=np.float64)

    x = np.clip(
        np.asarray(positions[0],dtype=np.float64),
        0.0,float(w - 1),
    )
    y = np.clip(
        np.asarray(positions[1],dtype=np.float64),
        0.0,float(h - 1),
    )

    x0 = np.floor(x).astype(np.int64)
    y0 = np.floor(y).astype(np.int64)
    x1 = np.minimum(w - 1,x0 + 1)
    y1 = np.minimum(h - 1,y0 + 1)

    dx = x - x0
    dy = y - y0

    return (
        image[y0,x0] * (1.0 - dx) * (1.0 - dy)
        + image[y0,x1] * dx * (1.0 - dy)
        + image[y1,x0] * (1.0 - dx) * dy
        + image[y1,x1] * dx * dy
    )
