"""The one place an ROI's shape is turned into pixels.

Every measurement, set operation, overlay outline and hit test goes through
here.  Before this existed, three different modules each had their own idea of
what an ROI's pixels were — the ROI manager read a pixel list, while the PSF
and colocalization kernels quietly fell back to the bounding rectangle for
anything they did not recognise, so a polygon or a segmentation mask was
measured as the box around it and reported a plausible, wrong number.

Two rules keep this honest:

* **rasterisation is local.**  :func:`roi_mask_local` returns a mask covering
  only the ROI's bounding box plus the slices that place it, so measuring 200
  ROIs on a 2048² image costs the area of the ROIs, not 200 full images.
  :func:`roi_mask` exists for the few operations that are image-sized by
  definition, and says so.
* **nothing else branches on ``roi_type``.**  Callers that legitimately need to
  know what a type supports ask :func:`roi_capabilities` instead of
  reimplementing the dispatch.

``scipy`` and ``skimage`` are imported inside the functions that need them, so
importing this module stays cheap for callers that only want a bounding box.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .roi import ROIRecord, replaced
from .roi_payload import MaskPayload, decode_mask, encode_mask

#: ROI types whose geometry is a closed area that can be filled and measured.
AREA_TYPES = frozenset(
    {"rectangle", "ellipse", "oval", "polygon", "freehand", "composite", "mask"}
)
#: ROI types that describe a path rather than an area.
LINE_TYPES = frozenset({"line", "polyline", "path"})
#: ROI types that are a set of positions.
POINT_TYPES = frozenset({"point", "multipoint"})


class UnsupportedROIGeometry(ValueError):
    """This ROI's shape cannot be turned into an area of pixels.

    Raised rather than approximated. A line has no interior, and an unknown
    type has no defined one; answering with the bounding box would produce a
    number that looks right and is not, which is precisely the failure this
    module exists to remove.
    """


@dataclass(frozen=True)
class ROICapabilities:
    """What a given ROI type supports.

    The sanctioned way to ask a type-dependent question. I/O, measurement
    applicability and overlay rendering all legitimately need this; what they
    must not do is rasterise or extract pixels themselves.
    """

    is_area: bool
    is_line: bool
    is_point: bool

    @property
    def has_interior(self) -> bool:
        return self.is_area


def roi_capabilities(roi_type: str) -> ROICapabilities:
    kind = str(roi_type or "").lower()
    return ROICapabilities(
        is_area=kind in AREA_TYPES,
        is_line=kind in LINE_TYPES,
        is_point=kind in POINT_TYPES,
    )


def _clip_bounds(bounds, shape) -> tuple[int, int, int, int]:
    r0, r1, c0, c1 = (int(round(float(v))) for v in bounds)
    rlo, rhi = sorted((r0, r1))
    clo, chi = sorted((c0, c1))
    return (
        max(0, min(rlo, shape[0])),
        max(0, min(rhi, shape[0])),
        max(0, min(clo, shape[1])),
        max(0, min(chi, shape[1])),
    )


def roi_bounds(roi: ROIRecord) -> tuple[int, int, int, int]:
    """The ROI's tight, half-open bounding box, derived from its geometry."""
    if roi.vertices:
        arr = np.asarray(roi.vertices, dtype=np.float64)
        rows, cols = arr[:, 0], arr[:, 1]
        return (
            int(np.floor(rows.min())),
            int(np.ceil(rows.max())),
            int(np.floor(cols.min())),
            int(np.ceil(cols.max())),
        )
    return tuple(int(v) for v in roi.bounds)  # type: ignore[return-value]


def _polygon_mask(vertices: np.ndarray, shape: tuple[int, int], offset) -> np.ndarray:
    """Rasterise a polygon, a pixel belonging to it when its centre is inside."""
    from skimage.draw import polygon as sk_polygon

    rows = vertices[:, 0] - offset[0]
    cols = vertices[:, 1] - offset[1]
    mask = np.zeros(shape, dtype=bool)
    if shape[0] <= 0 or shape[1] <= 0:
        return mask
    rr, cc = sk_polygon(rows, cols, shape=shape)
    mask[rr, cc] = True
    return mask


def _ellipse_mask(bounds, shape: tuple[int, int]) -> np.ndarray:
    r0, r1, c0, c1 = bounds
    if shape[0] <= 0 or shape[1] <= 0:
        return np.zeros(shape, dtype=bool)
    centre_r = (r1 - r0 - 1) / 2.0
    centre_c = (c1 - c0 - 1) / 2.0
    radius_r = max((r1 - r0) / 2.0, 1e-9)
    radius_c = max((c1 - c0) / 2.0, 1e-9)
    rows = np.arange(shape[0])[:, None] - centre_r
    cols = np.arange(shape[1])[None, :] - centre_c
    return (rows / radius_r) ** 2 + (cols / radius_c) ** 2 <= 1.0


def roi_mask_local(
    roi: ROIRecord, shape: tuple[int, int]
) -> tuple[np.ndarray, tuple[slice, slice]]:
    """``(local_mask, (row_slice, col_slice))`` for ``roi`` on an image.

    The canonical rasterisation. The mask covers only the ROI's clipped
    bounding box, so ``image[rs, cs][local_mask]`` reads exactly the ROI's
    pixels without ever building an image-sized array.

    Never raises for an ROI that falls outside the image: it returns a
    zero-sized mask, which the measurement layer reports as an empty ROI. An
    ROI drawn on a larger result must not be able to abort a whole batch.
    """
    shape = (int(shape[0]), int(shape[1]))
    r0, r1, c0, c1 = _clip_bounds(roi_bounds(roi), shape)
    slices = (slice(r0, r1), slice(c0, c1))
    local_shape = (max(0, r1 - r0), max(0, c1 - c0))
    if local_shape[0] == 0 or local_shape[1] == 0:
        return np.zeros(local_shape, dtype=bool), slices

    kind = str(roi.roi_type or "").lower()

    if roi.mask is not None:
        full = decode_mask(roi.mask)
        # The payload is stored over the ROI's own (unclipped) bounds, so trim
        # it to the part that survived clipping to the image.
        br0, br1, bc0, bc1 = (int(v) for v in roi_bounds(roi))
        top, left = r0 - br0, c0 - bc0
        window = full[top:top + local_shape[0], left:left + local_shape[1]]
        out = np.zeros(local_shape, dtype=bool)
        out[:window.shape[0], :window.shape[1]] = window
        return out, slices

    if roi.pixels is not None:
        # Legacy records: an explicit pixel list, still read exactly.
        coords = np.asarray(roi.pixels, dtype=np.int64).reshape((-1, 2))
        mask = np.zeros(local_shape, dtype=bool)
        if coords.size:
            rows = coords[:, 0] - r0
            cols = coords[:, 1] - c0
            inside = (
                (rows >= 0) & (rows < local_shape[0])
                & (cols >= 0) & (cols < local_shape[1])
            )
            mask[rows[inside], cols[inside]] = True
        return mask, slices

    caps = roi_capabilities(kind)
    if not caps.is_area:
        # Lines, points and unknown types have no interior to fill. Falling
        # back to the bounding box would return a plausible number for a
        # question that was never asked — measuring "the area of a line" as the
        # rectangle it spans. Refuse instead; line sampling is its own feature.
        raise UnsupportedROIGeometry(
            f"ROI {roi.name!r} of type {roi.roi_type!r} has no measurable area"
        )

    if roi.vertices is not None:
        # Any area ROI carrying explicit vertices is rasterised as the polygon
        # they describe — which is also what makes a *rotated* rectangle
        # measure as the rotated shape rather than its axis-aligned box.
        arr = np.asarray(roi.vertices, dtype=np.float64)
        return _polygon_mask(arr, local_shape, (r0, c0)), slices

    if kind in ("ellipse", "oval"):
        # Built over the ROI's own bounds and then cropped to the visible part.
        # Rebuilding it from the clipped bounds would re-centre and shrink the
        # ellipse, so a partially off-screen ellipse would silently become a
        # different, smaller one that happens to fit.
        br0, br1, bc0, bc1 = roi_bounds(roi)
        full = _ellipse_mask((br0, br1, bc0, bc1), (br1 - br0, bc1 - bc0))
        top, left = r0 - br0, c0 - bc0
        window = full[top:top + local_shape[0], left:left + local_shape[1]]
        out = np.zeros(local_shape, dtype=bool)
        out[:window.shape[0], :window.shape[1]] = window
        return out, slices

    if kind in ("rectangle", "composite", "mask"):
        # An axis-aligned rectangle really is its whole box; composite/mask
        # types without a payload have no pixels recorded at all.
        return np.ones(local_shape, dtype=bool), slices

    raise UnsupportedROIGeometry(
        f"ROI {roi.name!r} of type {roi.roi_type!r} cannot be rasterised"
    )


def roi_mask(roi: ROIRecord, shape: tuple[int, int]) -> np.ndarray:
    """Image-sized mask.

    Only for operations that are image-sized by definition — Make Inverse,
    Create Mask, exporting a label image. Never use this in a measurement
    loop; :func:`roi_mask_local` is the one for that.
    """
    local, (rs, cs) = roi_mask_local(roi, shape)
    full = np.zeros((int(shape[0]), int(shape[1])), dtype=bool)
    if local.size:
        full[rs, cs] = local
    return full


def roi_outline(roi: ROIRecord, *, max_vertices: int = 256) -> list[np.ndarray]:
    """Closed polygon(s) tracing the ROI, in image pixel coordinates.

    A list, not one polygon: a composite ROI can be several disconnected
    pieces, and a region with a hole has an inner boundary as well as an outer
    one. Anything drawing an ROI has to cope with more than one part.
    """
    kind = str(roi.roi_type or "").lower()
    r0, r1, c0, c1 = roi_bounds(roi)

    if roi.vertices and kind in ("polygon", "freehand", "line", "polyline", "path"):
        return [np.asarray(roi.vertices, dtype=np.float64)]

    if roi.mask is not None or roi.pixels is not None:
        from skimage import measure as sk_measure

        local, (rs, cs) = roi_mask_local(roi, (r1 + 1, c1 + 1))
        if not local.any():
            return []
        # Pad so a region touching the edge still yields a closed contour.
        padded = np.pad(local, 1, mode="constant", constant_values=False)
        contours = sk_measure.find_contours(padded.astype(float), 0.5)
        parts = []
        for contour in contours:
            contour = contour - 1.0  # undo the pad
            contour[:, 0] += rs.start
            contour[:, 1] += cs.start
            if len(contour) > max_vertices:
                step = int(np.ceil(len(contour) / max_vertices))
                contour = contour[::step]
            parts.append(contour)
        return parts

    if kind in ("ellipse", "oval"):
        angles = np.linspace(0.0, 2.0 * np.pi, min(max_vertices, 64), endpoint=False)
        centre_r, centre_c = (r0 + r1) / 2.0, (c0 + c1) / 2.0
        radius_r, radius_c = (r1 - r0) / 2.0, (c1 - c0) / 2.0
        return [
            np.stack(
                [centre_r + radius_r * np.sin(angles), centre_c + radius_c * np.cos(angles)],
                axis=1,
            )
        ]

    return [np.array([[r0, c0], [r0, c1], [r1, c1], [r1, c0]], dtype=np.float64)]


def _point_in_polygon(point, polygon) -> bool:
    """Even-odd ray crossing test."""
    row, col = float(point[0]), float(point[1])
    poly = np.asarray(polygon, dtype=np.float64)
    inside = False
    count = len(poly)
    for index in range(count):
        r_i, c_i = poly[index]
        r_j, c_j = poly[(index - 1) % count]
        if (c_i > col) != (c_j > col):
            crossing_row = (r_j - r_i) * (col - c_i) / (c_j - c_i) + r_i
            if row < crossing_row:
                inside = not inside
    return inside


def _distance_to_polygon(point, polygon) -> float:
    poly = np.asarray(polygon, dtype=np.float64)
    pt = np.asarray(point, dtype=np.float64)
    starts = poly
    ends = np.roll(poly, -1, axis=0)
    seg = ends - starts
    length_sq = np.einsum("ij,ij->i", seg, seg)
    length_sq[length_sq == 0] = 1e-12
    t = np.clip(np.einsum("ij,ij->i", pt - starts, seg) / length_sq, 0.0, 1.0)
    closest = starts + seg * t[:, None]
    return float(np.min(np.linalg.norm(closest - pt, axis=1)))


def roi_hit_test(roi: ROIRecord, position, *, tolerance: float = 0.0) -> bool:
    """Is ``position`` (row, col, in ROI pixel coordinates) inside ``roi``?

    Holes count as **outside**: a click in the hole of an annulus is not on the
    annulus, so it falls through to whatever else is under the cursor. That is
    what "the ROI does not contain that pixel" means, and it is why selection
    cannot be delegated to napari's ``Shapes.get_value`` — that returns only
    the topmost shape and cannot express it.
    """
    row, col = float(position[0]), float(position[1])
    r0, r1, c0, c1 = roi_bounds(roi)
    if not (
        r0 - tolerance <= row <= r1 + tolerance
        and c0 - tolerance <= col <= c1 + tolerance
    ):
        return False

    caps = roi_capabilities(roi.roi_type)

    if roi.mask is not None or roi.pixels is not None:
        # Ask the mask itself, so holes and disconnected parts are exact.
        pixel = (int(np.floor(row)), int(np.floor(col)))
        local, (rs, cs) = roi_mask_local(roi, (max(r1, pixel[0]) + 1, max(c1, pixel[1]) + 1))
        rr, cc = pixel[0] - rs.start, pixel[1] - cs.start
        if 0 <= rr < local.shape[0] and 0 <= cc < local.shape[1] and local[rr, cc]:
            return True
        if tolerance <= 0:
            return False
        return any(
            _distance_to_polygon((row, col), part) <= tolerance
            for part in roi_outline(roi)
        )

    if caps.is_line:
        parts = roi_outline(roi)
        return any(
            _distance_to_polygon((row, col), part) <= max(tolerance, 1.0)
            for part in parts
        )

    for part in roi_outline(roi):
        if _point_in_polygon((row, col), part):
            return True
        if tolerance > 0 and _distance_to_polygon((row, col), part) <= tolerance:
            return True
    return False


def roi_from_vertices(
    vertices,
    *,
    roi_type: str,
    name: str,
    source: str = "manual",
    position: tuple[tuple[str, int], ...] = (),
    frame_uid: str = "",
) -> ROIRecord:
    """Build a vector ROI, deriving its bounds from the geometry."""
    arr = np.asarray(vertices, dtype=np.float64)
    if arr.ndim != 2 or arr.shape[1] < 2:
        raise ValueError(f"vertices must have shape (N, >=2), got {arr.shape}")
    arr = arr[:, :2]
    rows, cols = arr[:, 0], arr[:, 1]
    bounds = (
        int(np.floor(np.nanmin(rows))),
        int(np.ceil(np.nanmax(rows))),
        int(np.floor(np.nanmin(cols))),
        int(np.ceil(np.nanmax(cols))),
    )
    caps = roi_capabilities(roi_type)
    if caps.is_area and (bounds[0] == bounds[1] or bounds[2] == bounds[3]):
        raise ValueError(f"{roi_type} ROI has zero area")
    return ROIRecord(
        name=name,
        roi_type=roi_type,
        bounds=bounds,
        source=source,
        vertices=tuple((float(r), float(c)) for r, c in arr),
        position=position,
        frame_uid=frame_uid,
    )


def roi_from_mask(
    mask: np.ndarray,
    *,
    name: str,
    source: str = "segmentation",
    offset: tuple[int, int] = (0, 0),
    position: tuple[tuple[str, int], ...] = (),
    frame_uid: str = "",
) -> ROIRecord:
    """Build a composite ROI from a boolean mask, trimmed to its content."""
    arr = np.asarray(mask, dtype=bool)
    if arr.ndim != 2:
        raise ValueError(f"mask must be 2D, got shape {arr.shape}")
    rows = np.flatnonzero(arr.any(axis=1))
    cols = np.flatnonzero(arr.any(axis=0))
    if rows.size == 0 or cols.size == 0:
        bounds = (int(offset[0]), int(offset[0]), int(offset[1]), int(offset[1]))
        return ROIRecord(
            name=name,
            roi_type="composite",
            bounds=bounds,
            source=source,
            mask=encode_mask(np.zeros((0, 0), dtype=bool)),
            position=position,
            frame_uid=frame_uid,
        )
    r0, r1 = int(rows[0]), int(rows[-1]) + 1
    c0, c1 = int(cols[0]), int(cols[-1]) + 1
    return ROIRecord(
        name=name,
        roi_type="composite",
        bounds=(r0 + offset[0], r1 + offset[0], c0 + offset[1], c1 + offset[1]),
        source=source,
        mask=encode_mask(arr[r0:r1, c0:c1]),
        position=position,
        frame_uid=frame_uid,
    )


def roi_from_payload(
    payload: MaskPayload,
    bounds: tuple[int, int, int, int],
    *,
    name: str,
    source: str = "segmentation",
    **kwargs,
) -> ROIRecord:
    """Build a composite ROI from an already-encoded payload."""
    return ROIRecord(
        name=name,
        roi_type="composite",
        bounds=tuple(int(v) for v in bounds),  # type: ignore[arg-type]
        source=source,
        mask=payload,
        **kwargs,
    )


def with_geometry_from_mask(roi: ROIRecord, mask: np.ndarray, offset=(0, 0)) -> ROIRecord:
    """``roi`` with its geometry replaced by ``mask`` (bounds re-derived)."""
    rebuilt = roi_from_mask(mask, name=roi.name, source=roi.source, offset=offset)
    return replaced(
        roi,
        roi_type="composite",
        bounds=rebuilt.bounds,
        mask=rebuilt.mask,
        vertices=None,
        pixels=None,
    )


__all__ = [
    "AREA_TYPES",
    "UnsupportedROIGeometry",
    "LINE_TYPES",
    "POINT_TYPES",
    "ROICapabilities",
    "roi_bounds",
    "roi_capabilities",
    "roi_from_mask",
    "roi_from_payload",
    "roi_from_vertices",
    "roi_hit_test",
    "roi_mask",
    "roi_mask_local",
    "roi_outline",
    "with_geometry_from_mask",
]
