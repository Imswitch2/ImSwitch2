"""Three-color strip alignment helpers for deskewed ImProcess volumes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import h5py
import numpy as np
from scipy.ndimage import affine_transform as _ndimage_affine


REGISTRATION_MODES = ("maxproj", "volume", "descriptor_3d")
SPLIT_AXES = ("Z", "Y", "X")
ALIGNMENT_VERSION = 1

_BEAD_SIGMA = 1.5
_BEAD_MIN_DIST = 6
_BEAD_THR_REL = 0.50
_MATCH_MAX_DIST = 25.0
_RANSAC_N_ITER = 2000
_RANSAC_INLIER_PX = 3.0


def split_axis_index(axis: str) -> int:
    """Return the ``(Z, Y, X)`` volume axis index for a split-axis label."""
    label = str(axis).strip().upper()
    if label not in SPLIT_AXES:
        raise ValueError(f"split axis must be one of {SPLIT_AXES}, got {axis!r}")
    return SPLIT_AXES.index(label)


def split_rois(volume: np.ndarray, bounds: list[int], axis: str = "X") -> list[np.ndarray]:
    """Split a ``(Z, Y, X)`` volume into equal-width slice ROIs along one axis.

    ``bounds`` has one more entry than the number of slices; each ROI starts at
    ``bounds[i]`` and all ROIs share the smallest bounded width so they can be
    stacked into one channel axis. Splitting along more than one axis in a
    single pass is not supported yet; the per-axis signature is the extension
    point for that.
    """
    if volume.ndim != 3:
        raise ValueError(f"split_rois expects a 3D volume, got shape {volume.shape}")
    if len(bounds) < 3:
        raise ValueError(f"bounds must define at least two slices, got {bounds!r}")
    axis_index = split_axis_index(axis)
    bounds = [int(bound) for bound in bounds]
    if bounds != sorted(bounds):
        raise ValueError(f"bounds must be sorted, got {bounds!r}")
    size = volume.shape[axis_index]
    if bounds[0] < 0 or bounds[-1] > size:
        raise ValueError(f"bounds {bounds!r} exceed input {axis} range 0..{size}")
    n_slices = len(bounds) - 1
    widths = [bounds[i + 1] - bounds[i] for i in range(n_slices)]
    if min(widths) <= 0:
        raise ValueError(f"bounds must define non-empty ROIs, got {bounds!r}")
    roi_width = min(widths)
    rois = []
    for i in range(n_slices):
        index: list[Any] = [slice(None)] * 3
        index[axis_index] = slice(bounds[i], bounds[i] + roi_width)
        rois.append(volume[tuple(index)])
    return rois


def split_x_rois(volume: np.ndarray, x_bounds: list[int]) -> list[np.ndarray]:
    """Split a ``(Z, Y, X)`` volume into three equal-width X-strip ROIs."""
    if len(x_bounds) != 4:
        raise ValueError(f"x_bounds must be [x0, x1, x2, x3], got {x_bounds!r}")
    return split_rois(volume, x_bounds, axis="X")


def default_bounds(size: int, n_slices: int) -> list[int]:
    """Return equal-width slice boundaries along one axis."""
    if n_slices < 2:
        raise ValueError(f"Need at least 2 slices, got {n_slices}")
    if size < n_slices:
        raise ValueError(f"Need at least {n_slices} pixels for {n_slices} slices, got {size}")
    return [(i * size) // n_slices for i in range(n_slices + 1)]


def default_x_bounds(x_size: int) -> list[int]:
    """Return thirds-based X boundaries for a three-strip acquisition."""
    return default_bounds(x_size, 3)


def parse_bounds(value: Any, size: int, n_slices: int) -> list[int]:
    """Parse user-supplied slice boundaries, falling back to equal slices."""
    if value is None:
        return default_bounds(size, n_slices)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default_bounds(size, n_slices)
        parts = [part.strip() for part in text.replace(";", ",").split(",")]
        bounds = [int(part) for part in parts if part]
    else:
        bounds = [int(part) for part in value]
    if len(bounds) != n_slices + 1:
        raise ValueError(
            f"bounds must have {n_slices + 1} values for {n_slices} slices, got {bounds!r}"
        )
    if bounds[-1] == 0:
        bounds[-1] = size
    return bounds


def parse_x_bounds(value: Any, x_size: int) -> list[int]:
    """Parse user-supplied X boundaries, falling back to equal thirds."""
    return parse_bounds(value, x_size, 3)


def detect_beads(
    volume: np.ndarray,
    bounds: list[int],
    axis: str = "X",
    sigma: float = _BEAD_SIGMA,
    min_distance: int = _BEAD_MIN_DIST,
    threshold_rel: float | list[float] = _BEAD_THR_REL,
) -> list[np.ndarray]:
    """Detect bead candidates in each slice ROI of a calibration volume.

    Returns one ``(N, 3)`` ROI-local ``(z, y, x)`` coordinate array per
    channel, brightest first. This is the standalone first step of the
    ``descriptor_3d`` workflow; feed the result back into
    :func:`extract_alignment` via ``bead_coords`` to register without
    re-detecting.
    """
    rois = split_rois(np.asarray(volume), bounds, axis=axis)
    thresholds = _per_channel_thresholds(threshold_rel, len(rois))
    return [
        _detect_beads_3d(
            roi,
            sigma=sigma,
            min_distance=min_distance,
            threshold_rel=thresholds[channel],
        )[0]
        for channel, roi in enumerate(rois)
    ]


def beads_to_global(
    bead_coords: list[np.ndarray], bounds: list[int], axis: str = "X"
) -> list[np.ndarray]:
    """Offset ROI-local bead coordinates back into full-volume coordinates."""
    axis_index = split_axis_index(axis)
    out = []
    for channel, coords in enumerate(bead_coords):
        coords = np.asarray(coords, dtype=np.float64).reshape(-1, 3).copy()
        coords[:, axis_index] += int(bounds[channel])
        out.append(coords)
    return out


def _per_channel_thresholds(threshold_rel: Any, n_channels: int) -> list[float]:
    thresholds = (
        [float(value) for value in threshold_rel]
        if isinstance(threshold_rel, (list, tuple))
        else [float(threshold_rel)] * n_channels
    )
    if len(thresholds) != n_channels:
        raise ValueError(
            f"threshold_rel must be a scalar or {n_channels} values, got {threshold_rel!r}"
        )
    return thresholds


def extract_alignment(
    volume: np.ndarray,
    bounds: list[int],
    mode: str = "maxproj",
    reference_channel: int = 0,
    split_axis: str = "X",
    bead_sigma: float = _BEAD_SIGMA,
    bead_min_dist: int = _BEAD_MIN_DIST,
    bead_thr_rel: float | list[float] = _BEAD_THR_REL,
    match_max_dist: float = _MATCH_MAX_DIST,
    ransac_n_iter: int = _RANSAC_N_ITER,
    ransac_inlier_px: float = _RANSAC_INLIER_PX,
    bead_coords: list[np.ndarray] | None = None,
) -> dict:
    """Compute alignment transforms from a multi-strip bead volume.

    ``bead_coords`` (``descriptor_3d`` only) takes per-channel ROI-local
    coordinates from :func:`detect_beads`, so bead finding and registration
    can run as two separate steps.
    """
    if mode not in REGISTRATION_MODES:
        raise ValueError(f"mode must be one of {REGISTRATION_MODES}, got {mode!r}")
    if volume.ndim != 3:
        raise ValueError(f"volume must be 3D (Z,Y,X), got shape {volume.shape}")

    rois = split_rois(np.asarray(volume), bounds, axis=split_axis)
    n_channels = len(rois)
    if not 0 <= int(reference_channel) < n_channels:
        raise ValueError(
            f"reference_channel must be in 0..{n_channels - 1}, got {reference_channel}"
        )
    roi_width = rois[0].shape[split_axis_index(split_axis)]

    diagnostics = None
    if mode == "descriptor_3d":
        transforms, diagnostics = _extract_descriptor_3d(
            rois,
            reference_channel,
            bead_sigma=bead_sigma,
            bead_min_dist=bead_min_dist,
            bead_thr_rel=bead_thr_rel,
            match_max_dist=match_max_dist,
            ransac_n_iter=ransac_n_iter,
            ransac_inlier_px=ransac_inlier_px,
            bead_coords=bead_coords,
        )
    else:
        transforms = _extract_projection_alignment(rois, reference_channel, mode)

    alignment = {
        # Key name kept for alignment-file compatibility; the values are the
        # split boundaries along ``split_axis``.
        "x_bounds": [int(bound) for bound in bounds],
        "split_axis": str(split_axis).strip().upper(),
        "reference_channel": int(reference_channel),
        "mode": str(mode),
        "roi_width": int(roi_width),
        "transforms": transforms,
        "source_shape": tuple(int(size) for size in volume.shape),
    }
    if diagnostics is not None:
        alignment["diagnostics"] = diagnostics
    return alignment


def save_alignment(alignment: dict, path: str | Path) -> None:
    """Write an alignment dictionary to a self-contained HDF5 file."""
    path = Path(path)
    with h5py.File(str(path), "w") as h5:
        h5.attrs["alignment_version"] = ALIGNMENT_VERSION
        h5.attrs["mode"] = alignment["mode"]
        h5.attrs["reference_channel"] = alignment["reference_channel"]
        h5.attrs["split_axis"] = alignment.get("split_axis", "X")
        h5.attrs["n_channels"] = len(alignment["transforms"])
        h5.attrs["roi_width"] = alignment["roi_width"]
        h5.attrs["source_shape"] = np.asarray(alignment["source_shape"], dtype=np.int64)
        h5.create_dataset("x_bounds", data=np.asarray(alignment["x_bounds"], dtype=np.int64))
        diagnostics = alignment.get("diagnostics") or {}
        for key in ("bead_counts", "matched_counts", "inlier_counts"):
            values = diagnostics.get(key)
            if values is not None:
                h5.attrs[f"diag_{key}"] = np.asarray(
                    [-1 if value is None else int(value) for value in values],
                    dtype=np.int64,
                )
        for channel, transform in enumerate(alignment["transforms"]):
            group = h5.create_group(f"channel_{channel}")
            group.create_dataset(
                "affine_2d",
                data=np.asarray(transform["affine_2d"], dtype=np.float64),
            )
            group.attrs["z_shift"] = int(transform["z_shift"])
            affine_3d = transform.get("affine_3d")
            if affine_3d is not None:
                group.create_dataset("affine_3d", data=np.asarray(affine_3d, dtype=np.float64))


def load_alignment(path: str | Path) -> dict:
    """Read an alignment HDF5 file produced by :func:`save_alignment`."""
    with h5py.File(str(path), "r") as h5:
        version = int(h5.attrs.get("alignment_version", ALIGNMENT_VERSION))
        if version != ALIGNMENT_VERSION:
            raise ValueError(f"Unsupported alignment file version {version}")
        alignment = {
            "mode": str(h5.attrs["mode"]),
            "reference_channel": int(h5.attrs["reference_channel"]),
            "split_axis": str(h5.attrs.get("split_axis", "X")),
            "roi_width": int(h5.attrs["roi_width"]),
            "source_shape": tuple(int(value) for value in h5.attrs["source_shape"]),
            "x_bounds": [int(value) for value in h5["x_bounds"][:]],
            "transforms": [],
        }
        diagnostics = {}
        for key in ("bead_counts", "matched_counts", "inlier_counts"):
            values = h5.attrs.get(f"diag_{key}")
            if values is not None:
                diagnostics[key] = [
                    None if int(value) < 0 else int(value) for value in values
                ]
        if diagnostics:
            alignment["diagnostics"] = diagnostics
        for channel in range(int(h5.attrs.get("n_channels", 3))):
            group = h5[f"channel_{channel}"]
            alignment["transforms"].append(
                {
                    "affine_2d": np.asarray(group["affine_2d"][:], dtype=np.float64),
                    "z_shift": int(group.attrs["z_shift"]),
                    "affine_3d": (
                        np.asarray(group["affine_3d"][:], dtype=np.float64)
                        if "affine_3d" in group
                        else None
                    ),
                }
            )
    return alignment


def apply_alignment(volume: np.ndarray, alignment: dict) -> np.ndarray:
    """Apply a saved alignment to one ``(Z, Y, X)`` deskewed volume."""
    if volume.ndim != 3:
        raise ValueError(f"apply_alignment expects a 3D volume, got shape {volume.shape}")
    split_axis = str(alignment.get("split_axis", "X"))
    axis_index = split_axis_index(split_axis)
    limit = int(alignment["x_bounds"][-1])
    if volume.shape[axis_index] < limit:
        raise ValueError(
            f"Alignment bounds require {split_axis} >= {limit}, "
            f"but input has {split_axis}={volume.shape[axis_index]}"
        )

    rois = split_rois(np.asarray(volume), alignment["x_bounds"], axis=split_axis)
    if len(alignment["transforms"]) != len(rois):
        raise ValueError(
            f"Alignment has {len(alignment['transforms'])} transform(s) but the "
            f"bounds define {len(rois)} slice(s)"
        )
    z_size, y_size, x_size = rois[0].shape
    output = np.zeros((len(rois), z_size, y_size, x_size), dtype=np.float32)
    reference_channel = int(alignment["reference_channel"])

    for channel, roi in enumerate(rois):
        roi = roi.astype(np.float32, copy=False)
        if channel == reference_channel:
            output[channel] = roi
            continue

        transform = alignment["transforms"][channel]
        affine_3d = transform.get("affine_3d")
        if affine_3d is not None:
            affine_3d = np.asarray(affine_3d, dtype=np.float64)
            linear_inv = np.linalg.inv(affine_3d[:, :3])
            offset_inv = -linear_inv @ affine_3d[:, 3]
            output[channel] = _ndimage_affine(
                roi,
                linear_inv,
                offset=offset_inv,
                order=1,
                mode="constant",
                cval=0.0,
                output_shape=(z_size, y_size, x_size),
            )
            continue

        z_shift = int(transform["z_shift"])
        if z_shift:
            shifted = np.zeros_like(roi)
            if z_shift > 0:
                shifted[z_shift:] = roi[: z_size - z_shift]
            else:
                shifted[: z_size + z_shift] = roi[-z_shift:]
            roi = shifted

        matrix_2d = np.asarray(transform["affine_2d"], dtype=np.float64)
        for z_index in range(z_size):
            output[channel, z_index] = _warp_slice_2d(roi[z_index], matrix_2d)

    return output


def apply_alignment_to_result_data(data: np.ndarray, axis_labels: list[str], alignment: dict) -> np.ndarray:
    """Apply alignment to SNOUTY-style ``ZYX`` or ``TZYX`` result data."""
    if axis_labels == ["Z", "Y", "X"] and data.ndim == 3:
        return apply_alignment(np.asarray(data), alignment)
    if axis_labels == ["T", "Z", "Y", "X"] and data.ndim == 4:
        return np.stack([apply_alignment(timepoint, alignment) for timepoint in data], axis=0)
    raise ValueError(
        "multicolor alignment currently supports axis labels ['Z','Y','X'] or "
        f"['T','Z','Y','X'], got {axis_labels!r} with shape {data.shape}"
    )


def extract_calibration_volume(data: np.ndarray, axis_labels: list[str], time_index: int = 0) -> np.ndarray:
    """Return one ``(Z,Y,X)`` volume from SNOUTY-style result data."""
    if axis_labels == ["Z", "Y", "X"] and data.ndim == 3:
        return np.asarray(data)
    if axis_labels == ["T", "Z", "Y", "X"] and data.ndim == 4:
        if time_index < 0 or time_index >= data.shape[0]:
            raise ValueError(f"time_index {time_index} out of range for T={data.shape[0]}")
        return np.asarray(data[time_index])
    raise ValueError(
        "multicolor registration currently supports axis labels ['Z','Y','X'] or "
        f"['T','Z','Y','X'], got {axis_labels!r} with shape {data.shape}"
    )


def output_axis_labels(axis_labels: list[str]) -> list[str]:
    """Return axis labels after applying three-color alignment."""
    if axis_labels == ["Z", "Y", "X"]:
        return ["C", "Z", "Y", "X"]
    if axis_labels == ["T", "Z", "Y", "X"]:
        return ["T", "C", "Z", "Y", "X"]
    raise ValueError(f"Unsupported axis labels for multicolor output: {axis_labels!r}")


def output_axis_scales(axis_labels: list[str], axis_scales: list[float]) -> list[float]:
    """Return axis scales after inserting a color axis."""
    if axis_labels == ["Z", "Y", "X"]:
        return [1.0, *axis_scales]
    if axis_labels == ["T", "Z", "Y", "X"]:
        return [axis_scales[0], 1.0, *axis_scales[1:]]
    raise ValueError(f"Unsupported axis labels for multicolor output: {axis_labels!r}")


def alignment_summary(alignment: dict) -> str:
    """Return a compact human-readable alignment summary."""
    n_channels = len(alignment["transforms"])
    text = (
        f"{n_channels}-color alignment (mode={alignment['mode']}, "
        f"ref={alignment['reference_channel']}, "
        f"axis={alignment.get('split_axis', 'X')}, "
        f"bounds={alignment['x_bounds']}, width={alignment['roi_width']})"
    )
    if alignment["mode"] == "descriptor_3d":
        parts = []
        for channel, transform in enumerate(alignment["transforms"]):
            if channel == alignment["reference_channel"]:
                continue
            affine_3d = transform.get("affine_3d")
            if affine_3d is not None:
                parts.append(f"ch{channel}:|t|={np.linalg.norm(affine_3d[:, 3]):.1f}px")
        if parts:
            text = f"{text}  {', '.join(parts)}"
    bead_counts = (alignment.get("diagnostics") or {}).get("bead_counts")
    if bead_counts:
        text = f"{text}  beads={bead_counts}"
    return text


def transform_details(alignment: dict) -> str:
    """Return a multi-line readable report of bead counts and final transforms."""
    lines = []
    reference_channel = int(alignment["reference_channel"])
    diagnostics = alignment.get("diagnostics") or {}
    bead_counts = diagnostics.get("bead_counts")
    matched_counts = diagnostics.get("matched_counts")
    inlier_counts = diagnostics.get("inlier_counts")
    if bead_counts:
        lines.append(
            "Beads found: "
            + ", ".join(f"ch{channel}={count}" for channel, count in enumerate(bead_counts))
        )
    for channel, transform in enumerate(alignment["transforms"]):
        if channel == reference_channel:
            lines.append(f"ch{channel}: reference (identity)")
            continue
        stats = ""
        if matched_counts and matched_counts[channel] is not None:
            inliers = inlier_counts[channel] if inlier_counts else None
            stats = f" [{matched_counts[channel]} matched, {inliers} inlier(s)]"
        affine_3d = transform.get("affine_3d")
        if affine_3d is not None:
            affine_3d = np.asarray(affine_3d, dtype=np.float64)
            shift = affine_3d[:, 3]
            lines.append(
                f"ch{channel}: 3D affine{stats}, shift (z,y,x)=("
                f"{shift[0]:+.2f}, {shift[1]:+.2f}, {shift[2]:+.2f}) px"
            )
            for row in affine_3d:
                lines.append("    [" + "  ".join(f"{value:+.4f}" for value in row) + "]")
            continue
        matrix = np.asarray(transform["affine_2d"], dtype=np.float64)
        lines.append(
            f"ch{channel}: z_shift={int(transform['z_shift'])}, "
            f"2D shift (y,x)=({matrix[1, 2]:+.2f}, {matrix[0, 2]:+.2f}) px{stats}"
        )
        for row in matrix[:2]:
            lines.append("    [" + "  ".join(f"{value:+.4f}" for value in row) + "]")
    return "\n".join(lines)


def _extract_projection_alignment(
    rois: list[np.ndarray],
    reference_channel: int,
    mode: str,
) -> list[dict]:
    ref = rois[reference_channel]
    ref_xy = ref.max(axis=0).astype(np.float32)
    ref_xz = ref.max(axis=1).astype(np.float32) if mode == "volume" else None
    transforms = []

    for channel, target in enumerate(rois):
        if channel == reference_channel:
            transforms.append(_identity_transform())
            continue
        affine_2d = _register_2d_affine(target.max(axis=0).astype(np.float32), ref_xy)
        z_shift = 0
        if mode == "volume":
            z_shift = _z_shift_xcorr(target.max(axis=1).astype(np.float32), ref_xz)
        transforms.append(
            {
                "affine_2d": affine_2d,
                "affine_3d": None,
                "z_shift": int(z_shift),
            }
        )
    return transforms


def _extract_descriptor_3d(
    rois: list[np.ndarray],
    reference_channel: int,
    bead_sigma: float,
    bead_min_dist: int,
    bead_thr_rel: float | list[float],
    match_max_dist: float,
    ransac_n_iter: int,
    ransac_inlier_px: float,
    bead_coords: list[np.ndarray] | None = None,
) -> tuple[list[dict], dict]:
    n_channels = len(rois)
    if bead_coords is not None:
        if len(bead_coords) != n_channels:
            raise ValueError(
                f"bead_coords must have {n_channels} channel entries, got {len(bead_coords)}"
            )
        coords = [np.asarray(channel_coords).reshape(-1, 3) for channel_coords in bead_coords]
    else:
        thresholds = _per_channel_thresholds(bead_thr_rel, n_channels)
        coords = [
            _detect_beads_3d(
                roi,
                sigma=bead_sigma,
                min_distance=bead_min_dist,
                threshold_rel=thresholds[channel],
            )[0]
            for channel, roi in enumerate(rois)
        ]
    ref_coords = coords[reference_channel].astype(np.float64)
    if len(ref_coords) < 4:
        raise RuntimeError(
            f"Reference channel {reference_channel} has only {len(ref_coords)} bead(s); "
            "descriptor_3d needs at least 4."
        )

    bead_counts = [int(len(channel_coords)) for channel_coords in coords]
    matched_counts: list[int | None] = [None] * n_channels
    inlier_counts: list[int | None] = [None] * n_channels

    transforms = []
    for channel in range(n_channels):
        if channel == reference_channel:
            transforms.append(_identity_transform(include_3d=True))
            continue

        src_coords = coords[channel].astype(np.float64)
        coarse_shift = _coarse_shift_3d(rois[channel], rois[reference_channel])
        shifted_src = src_coords + coarse_shift
        matched_src_shifted, matched_ref, _distances = _match_beads(
            shifted_src,
            ref_coords,
            max_distance=match_max_dist,
        )
        matched_counts[channel] = int(len(matched_src_shifted))
        if len(matched_src_shifted) < 4:
            raise RuntimeError(
                f"Channel {channel}: only {len(matched_src_shifted)} bead pair(s) found; "
                "descriptor_3d needs at least 4."
            )

        affine_fine, inliers = _fit_affine_3d_ransac(
            matched_src_shifted,
            matched_ref,
            n_iter=ransac_n_iter,
            inlier_thr=ransac_inlier_px,
        )
        if affine_fine is None:
            raise RuntimeError(f"Channel {channel}: RANSAC failed to fit a 3D affine.")
        inlier_counts[channel] = int(inliers.sum())

        affine_3d = affine_fine.copy()
        affine_3d[:, 3] = affine_fine[:, :3] @ coarse_shift + affine_fine[:, 3]
        if inliers.sum() < 4:
            affine_3d = _fit_affine_3d_lstsq(
                matched_src_shifted - coarse_shift,
                matched_ref,
            )
        transforms.append(
            {
                "affine_2d": np.eye(3, dtype=np.float64),
                "affine_3d": affine_3d,
                "z_shift": 0,
            }
        )
    diagnostics = {
        "bead_counts": bead_counts,
        "matched_counts": matched_counts,
        "inlier_counts": inlier_counts,
    }
    return transforms, diagnostics


def _identity_transform(include_3d: bool = False) -> dict:
    affine_3d = np.column_stack([np.eye(3), np.zeros(3)]) if include_3d else None
    return {
        "affine_2d": np.eye(3, dtype=np.float64),
        "affine_3d": affine_3d,
        "z_shift": 0,
    }


def _require_pystackreg():
    try:
        from pystackreg import StackReg
    except ImportError as exc:
        raise RuntimeError(
            "pystackreg is required for multicolor 'maxproj' and 'volume' modes. "
            "Install it with: pip install pystackreg"
        ) from exc
    return StackReg


def _register_2d_affine(target: np.ndarray, reference: np.ndarray) -> np.ndarray:
    StackReg = _require_pystackreg()
    stackreg = StackReg(StackReg.AFFINE)
    return np.asarray(
        stackreg.register(reference.astype(np.float32), target.astype(np.float32)),
        dtype=np.float64,
    )


def _z_shift_xcorr(target_xz: np.ndarray, ref_xz: np.ndarray) -> int:
    ref_profile = ref_xz.sum(axis=1).astype(np.float64)
    target_profile = target_xz.sum(axis=1).astype(np.float64)
    ref_profile -= ref_profile.mean()
    target_profile -= target_profile.mean()
    corr = np.correlate(ref_profile, target_profile, mode="full")
    return int(np.argmax(corr) - (len(ref_profile) - 1))


def _pystackreg_to_ndimage(matrix: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    array = np.asarray(matrix, dtype=np.float64)
    affine = np.array([[array[1, 1], array[1, 0]], [array[0, 1], array[0, 0]]])
    offset = np.array([array[1, 2], array[0, 2]], dtype=np.float64)
    return affine, offset


def _warp_slice_2d(slice_: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    affine, offset = _pystackreg_to_ndimage(matrix)
    return _ndimage_affine(
        slice_,
        affine,
        offset=offset,
        order=1,
        mode="constant",
        cval=0.0,
        output_shape=slice_.shape,
    )


def _coarse_shift_3d(src_vol: np.ndarray, ref_vol: np.ndarray) -> np.ndarray:
    def phase_corr_2d(ref: np.ndarray, src: np.ndarray) -> tuple[float, float]:
        ref = ref.astype(np.float64)
        src = src.astype(np.float64)
        ref -= ref.mean()
        src -= src.mean()
        f_ref = np.fft.rfft2(ref)
        f_src = np.fft.rfft2(src)
        cross = f_ref * np.conj(f_src)
        denom = np.abs(cross)
        denom[denom < 1e-10] = 1e-10
        corr = np.fft.irfft2(cross / denom, s=ref.shape)
        peak = np.unravel_index(np.argmax(corr), ref.shape)
        row = peak[0] if peak[0] < ref.shape[0] // 2 else peak[0] - ref.shape[0]
        col = peak[1] if peak[1] < ref.shape[1] // 2 else peak[1] - ref.shape[1]
        return float(row), float(col)

    dy_xy, dx_xy = phase_corr_2d(ref_vol.max(axis=0), src_vol.max(axis=0))
    dz_xz, dx_xz = phase_corr_2d(ref_vol.max(axis=1), src_vol.max(axis=1))
    dz_yz, dy_yz = phase_corr_2d(ref_vol.max(axis=2), src_vol.max(axis=2))
    return np.array(
        [(dz_xz + dz_yz) / 2.0, (dy_xy + dy_yz) / 2.0, (dx_xy + dx_xz) / 2.0],
        dtype=np.float64,
    )


def _detect_beads_3d(
    volume: np.ndarray,
    sigma: float = _BEAD_SIGMA,
    min_distance: int = _BEAD_MIN_DIST,
    threshold_rel: float = _BEAD_THR_REL,
) -> tuple[np.ndarray, np.ndarray]:
    from scipy.ndimage import gaussian_filter, maximum_filter

    smoothed = gaussian_filter(volume.astype(np.float32), sigma=sigma)
    footprint = 2 * int(min_distance) + 1
    is_local_max = maximum_filter(smoothed, size=footprint) == smoothed
    threshold = float(threshold_rel) * float(smoothed.max())
    mask = is_local_max & (smoothed > threshold)
    coords = np.argwhere(mask)
    if coords.size == 0:
        return np.empty((0, 3), dtype=np.intp), np.empty(0, dtype=np.float32)
    values = smoothed[mask]
    order = np.argsort(values)[::-1]
    return coords[order].astype(np.intp), values[order]


def _match_beads(
    src_pts: np.ndarray,
    ref_pts: np.ndarray,
    max_distance: float = _MATCH_MAX_DIST,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    from scipy.spatial import cKDTree

    empty = np.empty((0, 3), dtype=np.float64)
    if len(src_pts) == 0 or len(ref_pts) == 0:
        return empty, empty, np.empty(0, dtype=np.float64)
    tree = cKDTree(ref_pts.astype(np.float64))
    distances, indices = tree.query(src_pts.astype(np.float64), k=1)
    mask = distances < float(max_distance)
    return (
        src_pts[mask].astype(np.float64),
        ref_pts[indices[mask]].astype(np.float64),
        distances[mask].astype(np.float64),
    )


def _fit_affine_3d_lstsq(src_pts: np.ndarray, ref_pts: np.ndarray) -> np.ndarray | None:
    if len(src_pts) < 4:
        return None
    hom = np.column_stack([src_pts.astype(np.float64), np.ones(len(src_pts))])
    rows = [
        np.linalg.lstsq(hom, ref_pts[:, axis].astype(np.float64), rcond=None)[0]
        for axis in range(3)
    ]
    return np.asarray(rows, dtype=np.float64)


def _fit_affine_3d_ransac(
    src_pts: np.ndarray,
    ref_pts: np.ndarray,
    n_iter: int = _RANSAC_N_ITER,
    inlier_thr: float = _RANSAC_INLIER_PX,
    rng_seed: int = 42,
) -> tuple[np.ndarray | None, np.ndarray]:
    if len(src_pts) < 4:
        return None, np.zeros(len(src_pts), dtype=bool)
    rng = np.random.default_rng(rng_seed)
    hom = np.column_stack([src_pts.astype(np.float64), np.ones(len(src_pts))])
    best_inliers = np.zeros(len(src_pts), dtype=bool)

    for _ in range(int(n_iter)):
        sample = rng.choice(len(src_pts), 4, replace=False)
        candidate = _fit_affine_3d_lstsq(src_pts[sample], ref_pts[sample])
        if candidate is None:
            continue
        prediction = (candidate @ hom.T).T
        residual = np.linalg.norm(prediction - ref_pts.astype(np.float64), axis=1)
        inliers = residual < float(inlier_thr)
        if inliers.sum() > best_inliers.sum():
            best_inliers = inliers

    if best_inliers.sum() >= 4:
        return _fit_affine_3d_lstsq(src_pts[best_inliers], ref_pts[best_inliers]), best_inliers
    return _fit_affine_3d_lstsq(src_pts, ref_pts), best_inliers
