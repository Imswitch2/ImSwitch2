"""Set and shape operations on ROIs (P-5).

Every operation here works over the **union of the inputs' bounding boxes**,
never over the image. A boolean AND of two 20x20 ROIs on a 4096x4096 mosaic
costs a 20x20 array; doing it on image-sized masks — which is the obvious
implementation — costs 16 million booleans per operand for an answer that
cannot be larger than the smaller input.

The one documented exception is :func:`make_inverse`, whose result *is*
image-sized by definition (A-17). It carries a memory guard and refuses rather
than trying, because the alternative to a clear refusal is the session dying.

Pure: numpy, and `scipy`/`skimage` imported lazily inside the functions that
need them. No Qt, no napari, no ImProcess.
"""

from __future__ import annotations

import numpy as np

from .roi import ROIRecord, new_uid
from .roi_geometry import roi_bounds, roi_from_mask, roi_mask_local

#: Operations combining several ROIs into one.
COMBINE_OPS = ("and", "or", "xor", "subtract")

#: Refusal threshold for the A-17 full-frame operations, in pixels. A boolean
#: mask is one byte per pixel, so this is ~256 MiB — large enough for any
#: realistic camera frame or mosaic tile, small enough that hitting it means
#: something has gone wrong rather than something is merely big.
MAX_FULL_FRAME_PIXELS = 256 * 1024 * 1024


class ROIOperationError(ValueError):
    """An operation was refused, with a reason worth showing the user."""


def _union_bounds(rois) -> tuple[int, int, int, int]:
    boxes = [roi_bounds(roi) for roi in rois]
    if not boxes:
        raise ROIOperationError("no ROIs to combine")
    return (
        min(box[0] for box in boxes),
        max(box[1] for box in boxes),
        min(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _mask_in_window(roi: ROIRecord, window: tuple[int, int, int, int]) -> np.ndarray:
    """The ROI rasterised into an arbitrary window, in window coordinates.

    ``roi_mask_local`` clips to an image, which is right for measuring and
    wrong here: a set operation's window is the union of the operands' boxes,
    which may start above or left of the image origin.
    """
    r0, r1, c0, c1 = (int(v) for v in window)
    height, width = max(0, r1 - r0), max(0, c1 - c0)
    out = np.zeros((height, width), dtype=bool)
    if height == 0 or width == 0:
        return out

    # Rasterise over the ROI's own box, then place it. Asking for a mask on a
    # notional image of the window's size would re-clip against the wrong
    # origin and silently move the ROI.
    br0, br1, bc0, bc1 = (int(v) for v in roi_bounds(roi))
    shifted = ROIRecord(
        name=roi.name,
        roi_type=roi.roi_type,
        bounds=(br0 - r0, br1 - r0, bc0 - c0, bc1 - c0),
        source=roi.source,
        visible=roi.visible,
        pixels=(
            tuple((int(r) - r0, int(c) - c0) for r, c in roi.pixels)
            if roi.pixels is not None
            else None
        ),
        vertices=(
            tuple((float(r) - r0, float(c) - c0) for r, c in roi.vertices)
            if roi.vertices is not None
            else None
        ),
        mask=roi.mask,
    )
    local, (rows, cols) = roi_mask_local(shifted, (height, width))
    if local.size:
        out[rows, cols] = local
    return out


def _frame_of(rois) -> str:
    """The frame the inputs share, or a refusal.

    Combining ROIs from two different planes is not a set operation on
    anything: their pixel indices do not refer to the same grid, so the answer
    would be arithmetic on unrelated coordinates.
    """
    frames = {str(getattr(roi, "frame_uid", "") or "") for roi in rois}
    if len(frames) > 1:
        raise ROIOperationError(
            "these ROIs were captured on different planes; combining them "
            "would treat two coordinate grids as one"
        )
    return frames.pop() if frames else ""


def _position_of(rois) -> tuple:
    positions = {tuple(getattr(roi, "position", ()) or ()) for roi in rois}
    return positions.pop() if len(positions) == 1 else ()


def combine(rois, op: str, *, name: str = "") -> ROIRecord:
    """AND / OR / XOR / SUBTRACT over several ROIs, in the union of their boxes."""
    rois = list(rois)
    op = str(op).lower()
    if op not in COMBINE_OPS:
        raise ROIOperationError(f"unknown operation {op!r}")
    if len(rois) < 2:
        raise ROIOperationError("select at least two ROIs")

    frame_uid = _frame_of(rois)
    window = _union_bounds(rois)
    masks = [_mask_in_window(roi, window) for roi in rois]

    result = masks[0]
    for mask in masks[1:]:
        if op == "and":
            result = result & mask
        elif op == "or":
            result = result | mask
        elif op == "xor":
            result = result ^ mask
        else:  # subtract: the first ROI minus every other
            result = result & ~mask

    if not result.any():
        raise ROIOperationError(f"the {op.upper()} of these ROIs is empty")
    return roi_from_mask(
        result,
        name=name or f"{rois[0].name}_{op}",
        source="operation",
        offset=(window[0], window[2]),
        position=_position_of(rois),
        frame_uid=frame_uid,
    )


def split(roi: ROIRecord, *, connectivity: int = 2) -> list[ROIRecord]:
    """One ROI per connected component, in the original's own box.

    The union of the parts is the original by construction — they are its
    pixels, partitioned — which is what makes Split safe to use on a
    segmentation result that arrived as one composite.
    """
    from scipy.ndimage import label

    box = roi_bounds(roi)
    mask = _mask_in_window(roi, box)
    if not mask.any():
        return []
    structure = np.ones((3, 3), dtype=bool) if connectivity == 2 else None
    labels, count = label(mask, structure=structure)
    parts = []
    for index in range(1, count + 1):
        parts.append(
            roi_from_mask(
                labels == index,
                name=f"{roi.name}_{index}",
                source="operation",
                offset=(box[0], box[2]),
                position=tuple(getattr(roi, "position", ()) or ()),
                frame_uid=str(getattr(roi, "frame_uid", "") or ""),
            )
        )
    return parts


def _grown_window(box, margin: int) -> tuple[int, int, int, int]:
    return (box[0] - margin, box[1] + margin, box[2] - margin, box[3] + margin)


def enlarge(roi: ROIRecord, pixels: int, *, name: str = "") -> ROIRecord:
    """Grow (or, for a negative count, shrink) an ROI by a pixel margin.

    "Within ``n`` pixels of the ROI", by a Euclidean distance transform — which
    is what the words mean, and what ImageJ does. Iterated binary dilation,
    the obvious alternative, grows in a diamond: enlarging by 2 would reach 2
    pixels sideways but only 1 diagonally, so a circle would come out a
    lozenge. Corners therefore come out rounded, which is correct: the pixel
    diagonally 2 away from a corner is 2.83 pixels from the ROI.

    Computed over the box plus the margin, so this never needs an image-sized
    array however far it grows.
    """
    from scipy.ndimage import distance_transform_edt

    pixels = int(pixels)
    if pixels == 0:
        return roi
    margin = abs(pixels)
    window = _grown_window(roi_bounds(roi), margin)
    mask = _mask_in_window(roi, window)
    if not mask.any():
        raise ROIOperationError("this ROI has no pixels to grow")
    if pixels > 0:
        grown = distance_transform_edt(~mask) <= pixels
    else:
        grown = distance_transform_edt(mask) > margin
    if not grown.any():
        raise ROIOperationError("shrinking removed the whole ROI")
    return roi_from_mask(
        grown,
        name=name or f"{roi.name}_{'grown' if pixels > 0 else 'shrunk'}",
        source="operation",
        offset=(window[0], window[2]),
        position=tuple(getattr(roi, "position", ()) or ()),
        frame_uid=str(getattr(roi, "frame_uid", "") or ""),
    )


def make_band(roi: ROIRecord, width: int, *, name: str = "") -> ROIRecord:
    """The band of given width just outside the ROI — ImageJ's *Make Band*.

    The enlarged ROI minus the original, which is the local-background ring a
    ratiometric measurement needs.
    """
    width = int(width)
    if width <= 0:
        raise ROIOperationError("band width must be at least 1 pixel")
    grown = enlarge(roi, width)
    return combine([grown, roi], "subtract", name=name or f"{roi.name}_band")


def to_bounding_box(roi: ROIRecord, *, name: str = "") -> ROIRecord:
    """The ROI's axis-aligned bounding box, as a rectangle ROI."""
    r0, r1, c0, c1 = roi_bounds(roi)
    if r1 <= r0 or c1 <= c0:
        raise ROIOperationError("this ROI has no extent")
    return ROIRecord(
        name=name or f"{roi.name}_bbox",
        roi_type="rectangle",
        bounds=(int(r0), int(r1), int(c0), int(c1)),
        source="operation",
        position=tuple(getattr(roi, "position", ()) or ()),
        frame_uid=str(getattr(roi, "frame_uid", "") or ""),
        uid=new_uid(),
    )


def convex_hull(roi: ROIRecord, *, name: str = "") -> ROIRecord:
    """The ROI's convex hull, as a polygon in image coordinates."""
    from scipy.spatial import ConvexHull

    box = roi_bounds(roi)
    mask = _mask_in_window(roi, box)
    rows, cols = np.nonzero(mask)
    if rows.size < 3:
        raise ROIOperationError("a convex hull needs at least three pixels")
    points = np.column_stack([rows + box[0], cols + box[2]]).astype(float)
    try:
        hull = points[ConvexHull(points).vertices]
    except Exception as exc:  # degenerate: all points collinear
        raise ROIOperationError(f"no convex hull for this ROI ({exc})") from exc
    return ROIRecord(
        name=name or f"{roi.name}_hull",
        roi_type="polygon",
        bounds=(
            int(np.floor(hull[:, 0].min())),
            int(np.ceil(hull[:, 0].max())),
            int(np.floor(hull[:, 1].min())),
            int(np.ceil(hull[:, 1].max())),
        ),
        source="operation",
        vertices=tuple((float(r), float(c)) for r, c in hull),
        position=tuple(getattr(roi, "position", ()) or ()),
        frame_uid=str(getattr(roi, "frame_uid", "") or ""),
        uid=new_uid(),
    )


def translate(roi: ROIRecord, drow: int, dcol: int, *, name: str = "") -> ROIRecord:
    """Move an ROI by a pixel offset, keeping everything else about it.

    Geometry only, so identity survives: the moved ROI is the same ROI
    somewhere else, and `replaced()` bumps its revision because a move changes
    what it measures.
    """
    from .roi import replaced

    drow, dcol = int(drow), int(dcol)
    r0, r1, c0, c1 = roi_bounds(roi)
    changes = {"bounds": (r0 + drow, r1 + drow, c0 + dcol, c1 + dcol)}
    if roi.vertices is not None:
        changes["vertices"] = tuple(
            (float(r) + drow, float(c) + dcol) for r, c in roi.vertices
        )
    if roi.pixels is not None:
        changes["pixels"] = tuple(
            (int(r) + drow, int(c) + dcol) for r, c in roi.pixels
        )
    if name:
        changes["name"] = name
    return replaced(roi, **changes)


def make_inverse(roi: ROIRecord, shape, *, name: str = "") -> ROIRecord:
    """Everything in the image that this ROI is not (A-17).

    **The one image-sized operation here**, because the complement of a small
    ROI spans the frame and there is no local representation of it. The size is
    checked before anything is allocated: refusing with a number is a better
    outcome than an unexplained MemoryError, and a lazy complement was rejected
    because it would infect every consumer of a mask payload with laziness for
    this one case.
    """
    height, width = int(shape[0]), int(shape[1])
    if height <= 0 or width <= 0:
        raise ROIOperationError("the image has no extent")
    if height * width > MAX_FULL_FRAME_PIXELS:
        raise ROIOperationError(
            f"the inverse of an ROI on a {height}x{width} image needs "
            f"{height * width / 2**20:.0f} MiB; that is above the "
            f"{MAX_FULL_FRAME_PIXELS / 2**20:.0f} MiB limit for whole-frame "
            "operations"
        )
    local, (rows, cols) = roi_mask_local(roi, (height, width))
    full = np.ones((height, width), dtype=bool)
    if local.size:
        full[rows, cols] &= ~local
    if not full.any():
        raise ROIOperationError("this ROI covers the whole image")
    return roi_from_mask(
        full,
        name=name or f"{roi.name}_inverse",
        source="operation",
        position=tuple(getattr(roi, "position", ()) or ()),
        frame_uid=str(getattr(roi, "frame_uid", "") or ""),
    )


# --------------------------------------------------------------------------
# P-5.5 — rescaling to another frame, the explicit counterpart to A-13
# --------------------------------------------------------------------------

def frame_mapping(source, target, *, transforms=None) -> np.ndarray:
    """The 3x3 source-pixel → target-pixel matrix, or a refusal.

    A-13 refuses to *measure* through a transform, because that is
    reprojection and reprojection is a decision. This is where the user makes
    that decision explicitly: the ROI is rewritten into the target's
    coordinates once, visibly, and from then on it simply belongs there.

    Composed as ``inv(target.affine) @ edge @ source.affine`` — through world
    coordinates, which is the only place two pixel grids are comparable.
    """
    if tuple(source.plane_axes) != tuple(target.plane_axes):
        raise ROIOperationError(
            f"{'/'.join(source.plane_axes)} and {'/'.join(target.plane_axes)} "
            "are different planes; there is no mapping between them"
        )

    edge_affine = np.eye(3)
    if source.coordinate_space_uid != target.coordinate_space_uid:
        edge = None
        if transforms is not None:
            edge = transforms.edge(
                source.coordinate_space_uid, target.coordinate_space_uid
            )
        if edge is None:
            raise ROIOperationError(
                "these are unrelated pixel grids and no registration between "
                "them is known"
            )
        edge_affine = np.asarray(edge.affine, dtype=float).reshape(3, 3)

    source_affine = np.asarray(source.affine, dtype=float).reshape(3, 3)
    target_affine = np.asarray(target.affine, dtype=float).reshape(3, 3)
    try:
        inverse = np.linalg.inv(target_affine)
    except np.linalg.LinAlgError as exc:
        raise ROIOperationError(
            "the target frame's transform cannot be inverted"
        ) from exc
    return inverse @ edge_affine @ source_affine


def _apply(matrix: np.ndarray, points: np.ndarray) -> np.ndarray:
    homogeneous = np.column_stack([points, np.ones(len(points))])
    mapped = homogeneous @ matrix.T
    return mapped[:, :2] / mapped[:, 2:3]


def rescale_to_frame(
    roi: ROIRecord, source, target, *, transforms=None, name: str = ""
) -> ROIRecord:
    """Rewrite an ROI's geometry into ``target``'s pixel coordinates.

    Vector ROIs are mapped exactly. A rasterised one is resampled, which is
    lossy — a mask has no sub-pixel truth to recover — so it becomes a
    composite carrying the resampled pixels rather than pretending to be the
    shape it once was.
    """
    from .roi import replaced

    matrix = frame_mapping(source, target, transforms=transforms)
    changes: dict = {
        "frame_uid": str(getattr(target, "frame_uid", "") or ""),
    }
    if name:
        changes["name"] = name

    if roi.vertices is not None:
        mapped = _apply(matrix, np.asarray(roi.vertices, dtype=float))
        changes["vertices"] = tuple((float(r), float(c)) for r, c in mapped)
        changes["bounds"] = (
            int(np.floor(mapped[:, 0].min())),
            int(np.ceil(mapped[:, 0].max())),
            int(np.floor(mapped[:, 1].min())),
            int(np.ceil(mapped[:, 1].max())),
        )
        return replaced(roi, **changes)

    if roi.mask is None and roi.pixels is None:
        # A rectangle or ellipse stored as bounds only. Its four corners map
        # exactly; if the mapping rotates or shears them it is no longer an
        # axis-aligned box, so it becomes the polygon it actually is rather
        # than a bounding box quietly larger than the shape.
        r0, r1, c0, c1 = roi_bounds(roi)
        corners = np.array(
            [(r0, c0), (r0, c1), (r1, c1), (r1, c0)], dtype=float
        )
        mapped = _apply(matrix, corners)
        axis_aligned = (
            abs(matrix[0, 1]) < 1e-9 and abs(matrix[1, 0]) < 1e-9
        )
        bounds = (
            int(np.floor(mapped[:, 0].min())),
            int(np.ceil(mapped[:, 0].max())),
            int(np.floor(mapped[:, 1].min())),
            int(np.ceil(mapped[:, 1].max())),
        )
        changes["bounds"] = bounds
        if not axis_aligned:
            changes["roi_type"] = "polygon"
            changes["vertices"] = tuple((float(r), float(c)) for r, c in mapped)
        return replaced(roi, **changes)

    from scipy.ndimage import affine_transform

    box = roi_bounds(roi)
    mask = _mask_in_window(roi, box)
    corners = np.array(
        [(box[0], box[2]), (box[0], box[3]), (box[1], box[3]), (box[1], box[2])],
        dtype=float,
    )
    mapped = _apply(matrix, corners)
    out_r0, out_r1 = int(np.floor(mapped[:, 0].min())), int(np.ceil(mapped[:, 0].max()))
    out_c0, out_c1 = int(np.floor(mapped[:, 1].min())), int(np.ceil(mapped[:, 1].max()))
    out_shape = (max(1, out_r1 - out_r0), max(1, out_c1 - out_c0))
    if out_shape[0] * out_shape[1] > MAX_FULL_FRAME_PIXELS:
        raise ROIOperationError("the rescaled ROI is too large to resample")

    inverse = np.linalg.inv(matrix)
    # affine_transform maps *output* coordinates back to input ones, so the
    # inverse mapping is the one it wants — and the offsets place the two
    # local windows in the same world.
    origin = _apply(inverse, np.array([[out_r0, out_c0]], dtype=float))[0]
    resampled = affine_transform(
        mask.astype(np.float32),
        inverse[:2, :2],
        offset=(origin[0] - box[0], origin[1] - box[2]),
        output_shape=out_shape,
        order=0,
        mode="constant",
        cval=0.0,
    )
    grown = resampled > 0.5
    if not grown.any():
        raise ROIOperationError("the rescaled ROI is empty in the target frame")
    return roi_from_mask(
        grown,
        name=name or roi.name,
        source=roi.source,
        offset=(out_r0, out_c0),
        position=tuple(getattr(roi, "position", ()) or ()),
        frame_uid=str(getattr(target, "frame_uid", "") or ""),
    )


# --------------------------------------------------------------------------
# P-6.4 — between an ROI set and an image
# --------------------------------------------------------------------------

def rois_from_labels(
    labels,
    *,
    name_prefix: str = "ROI",
    source: str = "labels",
    position=(),
    frame_uid: str = "",
    background: int = 0,
) -> list[ROIRecord]:
    """One ROI per label value — ImageJ's *Create Selection* over a label image.

    Each ROI is built from its own bounding box, so a five-hundred-label
    segmentation costs the sum of the objects rather than five hundred
    image-sized masks.
    """
    from scipy.ndimage import find_objects

    array = np.asarray(labels)
    if array.ndim != 2:
        raise ROIOperationError(f"expected a 2D label image, got shape {array.shape}")
    if array.dtype == bool:
        # A plain mask is a one-label image; treating it as one keeps a single
        # code path rather than a near-copy for the boolean case.
        array = array.astype(np.int32)

    rois = []
    for index, window in enumerate(find_objects(array), start=1):
        if window is None or index == background:
            continue
        rows, cols = window
        local = array[rows, cols] == index
        if not local.any():
            continue
        rois.append(
            roi_from_mask(
                local,
                name=f"{name_prefix}_{index}",
                source=source,
                offset=(int(rows.start), int(cols.start)),
                position=tuple(position),
                frame_uid=str(frame_uid or ""),
            )
        )
    return rois


def labels_from_rois(rois, shape) -> np.ndarray:
    """An ROI set as a label image — the inverse of *Create Selection* (A-17).

    Image-sized by definition, so it carries the same guard as
    :func:`make_inverse` and refuses with a size rather than dying in the
    allocator. Later ROIs overwrite earlier ones where they overlap, and the
    labels follow the order given, so ``labels == 3`` is the third ROI.
    """
    height, width = int(shape[0]), int(shape[1])
    if height <= 0 or width <= 0:
        raise ROIOperationError("the image has no extent")
    if height * width > MAX_FULL_FRAME_PIXELS:
        raise ROIOperationError(
            f"a {height}x{width} label image needs "
            f"{height * width * 4 / 2**20:.0f} MiB; that is above the limit "
            "for whole-frame operations"
        )
    out = np.zeros((height, width), dtype=np.int32)
    for index, roi in enumerate(rois, start=1):
        local, (rowslice, colslice) = roi_mask_local(roi, (height, width))
        if local.size:
            window = out[rowslice, colslice]
            window[local] = index
    return out


__all__ = [
    "COMBINE_OPS",
    "MAX_FULL_FRAME_PIXELS",
    "ROIOperationError",
    "labels_from_rois",
    "rois_from_labels",
    "combine",
    "convex_hull",
    "enlarge",
    "frame_mapping",
    "make_band",
    "make_inverse",
    "rescale_to_frame",
    "split",
    "to_bounding_box",
    "translate",
]
