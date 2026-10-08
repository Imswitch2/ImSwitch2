"""Target-independent subpixel spot detection."""

from __future__ import annotations



import numpy as np
from scipy.ndimage import gaussian_filter,maximum_filter
from scipy.optimize import least_squares

from .model import DetectedSpots,SpotDetectionOptions


def crop_image(
    image: np.ndarray,
    options: SpotDetectionOptions,
    crop_coord: tuple[int, int, int, int] | None=None,
):
    """Crop a 2D image automatically or reuse an explicit crop.

    Automatic cropping finds the thresholded bounding box directly and expands
    it by ``crop_margin_px`` on every side. The deprecated
    ``dilation_kernel_size`` input is still accepted for direct API callers and
    converted to the equivalent margin. Only the final crop is copied to float64.
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

                if options.dilation_kernel_size is not None:
                    # Backward compatibility for direct users of the former
                    # kernel-size API. Normal UI/workflow callers now provide
                    # the actual per-side crop margin directly.
                    margin = int(options.dilation_kernel_size) // 2
                else:
                    margin = int(options.crop_margin_px)

                coord = (
                    max(0,y1 - margin),
                    min(height,y2 + margin),
                    max(0,x1 - margin),
                    min(width,x2 + margin),
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
    """Refine local-maxima positions inside a small image window.

    ``max`` keeps pixel precision, ``cog`` computes a background-subtracted
    center of gravity, and ``gaussian`` fits an axis-aligned 2D Gaussian plus a
    constant background. Full-size Gaussian windows are fitted together with a
    small batched Gauss-Newton solver; truncated border windows retain the
    scalar bounded SciPy fit. Both paths fall back to CoG on unreliable fits.
    """
    if window <= 0 or positions.shape[1] == 0:
        return np.array(positions,dtype=np.float64,copy=True)

    image = np.asarray(image,dtype=np.float64)
    h,w = image.shape
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
            # CoG is both the final result for ``cog`` and the robust initial /
            # fallback center for Gaussian fitting.
            minima = np.min(patches,axis=(1,2),keepdims=True)
            weights = patches - minima
            totals = np.sum(weights,axis=(1,2))
            cog_valid = np.isfinite(totals) & (totals > 0)

            if np.any(cog_valid):
                weights_valid = weights[cog_valid]
                totals_valid = totals[cog_valid]
                dx = np.sum(
                    weights_valid * offsets[None,None,:],
                    axis=(1,2),
                ) / totals_valid
                dy = np.sum(
                    weights_valid * offsets[None,:,None],
                    axis=(1,2),
                ) / totals_valid

                valid_indices = interior_indices[cog_valid]
                refined[0,valid_indices] = centers_x[valid_indices] + dx
                refined[1,valid_indices] = centers_y[valid_indices] + dy

            if method == "gaussian":
                patch_x1 = cx - window
                patch_y1 = cy - window
                initial_local = np.column_stack((
                    refined[0,interior_indices] - patch_x1,
                    refined[1,interior_indices] - patch_y1,
                ))
                fitted_local,fit_valid = _refine_patches_gaussian(
                    patches,initial_local,
                )
                if np.any(fit_valid):
                    fitted_indices = interior_indices[fit_valid]
                    refined[0,fitted_indices] = (
                        patch_x1[fit_valid] + fitted_local[fit_valid,0]
                    )
                    refined[1,fitted_indices] = (
                        patch_y1[fit_valid] + fitted_local[fit_valid,1]
                    )

    # Border patches have variable shapes. They are uncommon for ordinary
    # localization crops, so the scalar path keeps exact edge semantics without
    # penalizing the large regular arrays that benefit from batching.
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

        fallback = _refine_patch_cog(
            patch,x1=x1,y1=y1,default=(x0,y0),
        )
        if method == "gaussian":
            gaussian = _refine_patch_gaussian(
                patch,x1=x1,y1=y1,initial=fallback,
            )
            refined[:,index] = fallback if gaussian is None else gaussian
        else:
            refined[:,index] = fallback

    return refined


def _refine_patches_gaussian(patches,initial,max_iterations=5):
    """Fit equal-sized Gaussian patches together with batched Gauss-Newton.

    Parameters are ``background, log(amplitude), x, y, log(sigma_x),
    log(sigma_y)``. Log-parameterizing positive quantities avoids the overhead
    of a generic bounded optimizer while keeping amplitude and widths physical.
    Invalid or non-improving fits are flagged for the caller to keep its CoG
    fallback.
    """
    patches = np.asarray(patches,dtype=np.float64)
    initial = np.asarray(initial,dtype=np.float64)
    if patches.ndim != 3:
        raise ValueError("Gaussian patch batch must have shape (n, y, x)")

    count,height,width = patches.shape
    centers = np.array(initial,dtype=np.float64,copy=True)
    valid = np.zeros(count,dtype=bool)
    if count == 0 or min(height,width) < 3 or height * width < 9:
        return centers,valid
    if initial.shape != (count,2):
        raise ValueError("Gaussian initial centers must have shape (n, 2)")

    data = patches.reshape(count,-1)
    minimum = np.min(patches,axis=(1,2))
    maximum = np.max(patches,axis=(1,2))
    amplitude = maximum - minimum
    valid = (
        np.isfinite(amplitude)
        & (amplitude > 0)
        & np.all(np.isfinite(initial),axis=1)
    )
    if not np.any(valid):
        return centers,valid

    yy,xx = np.meshgrid(
        np.arange(height,dtype=np.float64),
        np.arange(width,dtype=np.float64),
        indexing="ij",
    )
    xx = xx.reshape(1,-1)
    yy = yy.reshape(1,-1)

    sigma0 = max(0.75,0.20 * float(max(width,height)))
    sigma_max = max(1.0,2.0 * float(max(width,height)))
    signal_scale = np.maximum.reduce((
        amplitude,
        np.abs(maximum),
        np.abs(minimum),
        np.ones(count,dtype=np.float64),
    ))

    parameters = np.empty((count,6),dtype=np.float64)
    parameters[:,0] = minimum
    parameters[:,1] = np.log(np.maximum(amplitude,1e-12))
    parameters[:,2] = np.clip(initial[:,0],0.0,width - 1.0)
    parameters[:,3] = np.clip(initial[:,1],0.0,height - 1.0)
    parameters[:,4] = np.log(sigma0)
    parameters[:,5] = np.log(sigma0)

    min_log_sigma = np.log(0.30)
    max_log_sigma = np.log(sigma_max)
    max_log_amplitude = np.log(np.maximum(4.0 * signal_scale,amplitude))
    background_min = minimum - 2.0 * signal_scale
    background_max = maximum + 2.0 * signal_scale
    initial_sse = None

    for _ in range(max(1,int(max_iterations))):
        background = parameters[:,0:1]
        fitted_amplitude = np.exp(parameters[:,1:2])
        center_x = parameters[:,2:3]
        center_y = parameters[:,3:4]
        sigma_x = np.exp(parameters[:,4:5])
        sigma_y = np.exp(parameters[:,5:6])

        delta_x = xx - center_x
        delta_y = yy - center_y
        norm_x = delta_x / sigma_x
        norm_y = delta_y / sigma_y
        gaussian = fitted_amplitude * np.exp(
            -0.5 * (norm_x * norm_x + norm_y * norm_y)
        )
        residual = background + gaussian - data
        if initial_sse is None:
            initial_sse = np.sum(residual * residual,axis=1)

        jacobian = np.stack((
            np.ones_like(gaussian),
            gaussian,
            gaussian * delta_x / (sigma_x * sigma_x),
            gaussian * delta_y / (sigma_y * sigma_y),
            gaussian * norm_x * norm_x,
            gaussian * norm_y * norm_y,
        ),axis=2)
        jacobian_t = np.transpose(jacobian,(0,2,1))
        normal = jacobian_t @ jacobian
        gradient = np.einsum("npi,np->ni",jacobian,residual)

        # A small scale-aware LM term keeps the tiny 6x6 systems invertible on
        # noisy or nearly flat patches without materially changing good fits.
        diagonal = np.diagonal(normal,axis1=1,axis2=2)
        damping = 1e-4 * (np.mean(diagonal,axis=1) + 1e-12)
        diag_indices = np.arange(6)
        normal[:,diag_indices,diag_indices] += damping[:,None]

        try:
            step = np.linalg.solve(normal,-gradient[...,None])[...,0]
        except np.linalg.LinAlgError:
            step = np.einsum(
                "nij,nj->ni",np.linalg.pinv(normal),-gradient,
            )
        step = np.where(np.isfinite(step),step,0.0)

        # Limit a single iteration from jumping across the small local window.
        # Width/amplitude steps are in log-space.
        step[:,1] = np.clip(step[:,1],-1.0,1.0)
        step[:,2:4] = np.clip(step[:,2:4],-1.0,1.0)
        step[:,4:6] = np.clip(step[:,4:6],-0.5,0.5)
        parameters += step

        parameters[:,0] = np.clip(
            parameters[:,0],background_min,background_max,
        )
        parameters[:,1] = np.clip(
            parameters[:,1],np.log(1e-12),max_log_amplitude,
        )
        parameters[:,2] = np.clip(parameters[:,2],-0.5,width - 0.5)
        parameters[:,3] = np.clip(parameters[:,3],-0.5,height - 0.5)
        parameters[:,4] = np.clip(
            parameters[:,4],min_log_sigma,max_log_sigma,
        )
        parameters[:,5] = np.clip(
            parameters[:,5],min_log_sigma,max_log_sigma,
        )

    background = parameters[:,0:1]
    fitted_amplitude = np.exp(parameters[:,1:2])
    center_x = parameters[:,2:3]
    center_y = parameters[:,3:4]
    sigma_x = np.exp(parameters[:,4:5])
    sigma_y = np.exp(parameters[:,5:6])
    gaussian = fitted_amplitude * np.exp(
        -0.5 * (
            ((xx - center_x) / sigma_x) ** 2
            + ((yy - center_y) / sigma_y) ** 2
        )
    )
    final_sse = np.sum((background + gaussian - data) ** 2,axis=1)

    valid &= (
        np.all(np.isfinite(parameters),axis=1)
        & np.isfinite(final_sse)
        & (final_sse <= initial_sse * (1.0 + 1e-9))
    )
    centers[:,0] = parameters[:,2]
    centers[:,1] = parameters[:,3]
    return centers,valid

def _refine_patch_cog(patch,*,x1,y1,default):
    """Return a background-subtracted CoG center for one local patch."""
    patch = np.asarray(patch,dtype=np.float64)
    if patch.size == 0:
        return np.asarray(default,dtype=np.float64)
    weights = patch - float(np.min(patch))
    total = float(np.sum(weights))
    if not np.isfinite(total) or total <= 0:
        return np.asarray(default,dtype=np.float64)
    yy,xx = np.meshgrid(
        np.arange(y1,y1 + patch.shape[0],dtype=np.float64),
        np.arange(x1,x1 + patch.shape[1],dtype=np.float64),
        indexing="ij",
    )
    return np.asarray((
        float(np.sum(xx * weights) / total),
        float(np.sum(yy * weights) / total),
    ),dtype=np.float64)


def _refine_patch_gaussian(patch,*,x1,y1,initial):
    """Fit an axis-aligned 2D Gaussian center, returning ``None`` on failure."""
    patch = np.asarray(patch,dtype=np.float64)
    if patch.ndim != 2 or min(patch.shape) < 3 or patch.size < 9:
        return None

    minimum = float(np.min(patch))
    maximum = float(np.max(patch))
    amplitude = maximum - minimum
    if not np.isfinite(amplitude) or amplitude <= 0:
        return None

    height,width = patch.shape
    yy,xx = np.meshgrid(
        np.arange(height,dtype=np.float64),
        np.arange(width,dtype=np.float64),
        indexing="ij",
    )
    initial = np.asarray(initial,dtype=np.float64)
    x0 = float(np.clip(initial[0] - x1,0.0,width - 1.0))
    y0 = float(np.clip(initial[1] - y1,0.0,height - 1.0))

    # A one-pixel sigma is a stable generic starting point for the small local
    # windows used here. Bounds intentionally allow broader spots while keeping
    # the center inside the measured patch.
    sigma0 = max(0.75,0.20 * float(max(width,height)))
    sigma_max = max(1.0,2.0 * float(max(width,height)))
    signal_scale = max(amplitude,abs(maximum),abs(minimum),1.0)

    def residual(parameters):
        background,amp,cx,cy,sigma_x,sigma_y = parameters
        model = background + amp * np.exp(
            -0.5 * (
                ((xx - cx) / sigma_x) ** 2
                + ((yy - cy) / sigma_y) ** 2
            )
        )
        return (model - patch).ravel()

    lower = np.asarray((
        minimum - 2.0 * signal_scale,
        0.0,
        -0.5,
        -0.5,
        0.30,
        0.30,
    ),dtype=np.float64)
    upper = np.asarray((
        maximum + 2.0 * signal_scale,
        max(4.0 * signal_scale,amplitude),
        width - 0.5,
        height - 0.5,
        sigma_max,
        sigma_max,
    ),dtype=np.float64)
    guess = np.asarray((
        minimum,amplitude,x0,y0,sigma0,sigma0,
    ),dtype=np.float64)

    try:
        fit = least_squares(
            residual,
            guess,
            bounds=(lower,upper),
            method="trf",
            max_nfev=100,
        )
    except (ValueError,FloatingPointError,RuntimeError):
        return None

    if not fit.success or not np.all(np.isfinite(fit.x)):
        return None
    _,fitted_amplitude,cx,cy,sigma_x,sigma_y = fit.x
    if fitted_amplitude <= 0 or sigma_x <= 0 or sigma_y <= 0:
        return None

    return np.asarray((x1 + cx,y1 + cy),dtype=np.float64)


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
