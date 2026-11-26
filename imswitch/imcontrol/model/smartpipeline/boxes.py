"""Boxes that travel with points through a transform step.

A points table may carry a bounding box per row in four property columns,
``bbox_y0, bbox_x0, bbox_y1, bbox_x1`` -- in the units of the points' frame,
``y`` meaning coordinate column 0 and ``x`` column 1, as for the coordinates
themselves (the ``roi-dff`` processor writes them in camera pixels, with
the centroid's convention: pixel centres at integers, the box reaching half
a pixel beyond its outer pixels).

A ``transform`` step maps the centroids; it maps the boxes too, properly:
the **four corners** go through the same transform and the box in the new
frame is their axis-aligned bounding box. For a scale-flip (scale, swap,
mirror, offset) that is the exact image of the rectangle; for a rotation or a
shear it is the smallest axis-aligned rectangle containing the rotated one,
larger than the ROI, never smaller. The camera-pixel box stays on in
``src_bbox_y0 ...``; ``bbox_*`` is now in the target frame.
"""

from __future__ import annotations

import numpy as np

BOX_COLUMNS = ("bbox_y0", "bbox_x0", "bbox_y1", "bbox_x1")
SOURCE_BOX_COLUMNS = ("src_bbox_y0", "src_bbox_x0", "src_bbox_y1", "src_bbox_x1")

#: Keys of a transform record that may hold how well the calibration fits.
RESIDUAL_KEYS = ("residual_um", "residual_rms_um", "rms_residual_um", "rms_um", "residual", "rms")


def has_boxes(obj) -> bool:
    properties = getattr(obj, "properties", None) or {}
    return all(name in properties for name in BOX_COLUMNS)


def transform_record(transform) -> dict:
    """What a log needs to know of a transform: its id, type and, when its
    record states one, the calibration residual."""
    try:
        payload = transform.to_dict()
    except Exception:  # noqa: BLE001 - a transform that cannot describe itself
        payload = {}
    record = {"id": str(getattr(transform, "id", "")), "type": str(payload.get("type", ""))}
    for scope in (payload, *(payload.get(key) for key in ("fit", "quality", "calibration") if isinstance(payload.get(key), dict))):
        for key in RESIDUAL_KEYS:
            if key in scope and scope[key] is not None:
                record["residual"] = scope[key]
                record["residual_key"] = key
                return record
    return record


def map_boxes(source, moved, transform, context) -> None:
    """Replace ``moved``'s ``bbox_*`` columns (in place) by the box of the
    four transformed corners of ``source``'s boxes; keep the originals in
    ``src_bbox_*``. Does nothing when ``source`` has no box columns."""
    if not has_boxes(source):
        return
    properties = source.properties
    y0, x0, y1, x1 = (np.asarray(properties[name], dtype=np.float64) for name in BOX_COLUMNS)
    count = y0.shape[0]
    if count == 0:
        for name in SOURCE_BOX_COLUMNS:
            moved.properties[name] = np.zeros(0)
        return
    corners = np.stack([np.column_stack([y0, x0]), np.column_stack([y0, x1]),
                        np.column_stack([y1, x0]), np.column_stack([y1, x1])])   # (4, N, 2)
    columns = np.asarray(source.coordinates).shape[1]
    flat = corners.reshape(-1, 2)
    if columns > 2:   # a transform that wants a third column gets zeros there
        flat = np.column_stack([flat, np.zeros((flat.shape[0], columns - 2))])
    mapped = np.asarray(transform.apply(flat, dict(context or {})), dtype=np.float64)[:, :2].reshape(4, count, 2)
    low, high = mapped.min(axis=0), mapped.max(axis=0)
    for name, value in zip(SOURCE_BOX_COLUMNS, (y0, x0, y1, x1)):
        moved.properties[name] = value
    for name, value in zip(BOX_COLUMNS, (low[:, 0], low[:, 1], high[:, 0], high[:, 1])):
        moved.properties[name] = value


def box_of_row(properties: dict, row: int, axes: list[str]) -> dict[str, tuple[float, float]] | None:
    """``{axis: (low, high)}`` of one row's box: column 0 -> ``axes[0]``, column 1 -> ``axes[1]``;
    None when the points carry no box columns."""
    if not all(name in properties for name in BOX_COLUMNS) or len(axes) < 2:
        return None
    y0, x0, y1, x1 = (float(properties[name][row]) for name in BOX_COLUMNS)
    return {axes[0]: (min(y0, y1), max(y0, y1)), axes[1]: (min(x0, x1), max(x0, x1))}


def plan_box_scan(box: dict, *, steps: dict, margin_um: float = 0.0, margin_frac: float = 0.0,
                  min_size_um: float = 0.0, max_size_um: float = 0.0, ranges: dict | None = None,
                  min_overlap: float = 0.5) -> dict:
    """The scan rectangle for a box (``{axis: (low, high)}`` in scan micrometres).

    Per axis: pad the box by ``margin_um`` on each side plus ``margin_frac`` of its length on each
    side; raise it to ``min_size_um`` / lower it to ``max_size_um`` around its centre (0 = no limit);
    then fit it to the scanner's ``ranges`` (``{axis: (low, high)}``, an axis without one is not limited).
    A box whose centre is outside the range, or of which less than ``min_overlap`` (area fraction of the
    padded box) lies inside, is **skipped**; a box partly outside is clipped (and the clip logged in
    ``notes``). The size is a whole number of ``steps[axis]`` (what the panel scans: ``round(size/step)``
    pixels), rounded up to cover the box, down only where the range does not allow more.

    Returns ``{"skip": reason or None, "centre": {axis: um}, "size": {axis: um}, "pixels": {axis: n},
    "padded": {axis: (lo, hi)}, "notes": [...]}``.
    """
    import math

    ranges = ranges or {}
    notes: list[str] = []
    padded, centre, size, pixels = {}, {}, {}, {}
    wanted_area = fitted_area = 1.0
    for axis, (low, high) in box.items():
        length = max(0.0, float(high) - float(low))
        pad = float(margin_um) + float(margin_frac) * length
        mid = 0.5 * (float(low) + float(high))
        want = length + 2.0 * pad
        if min_size_um and want < float(min_size_um):
            want = float(min_size_um)
            notes.append(f"{axis}: raised to the minimum size {min_size_um:g} um")
        if max_size_um and want > float(max_size_um):
            want = float(max_size_um)
            notes.append(f"{axis}: capped at the maximum size {max_size_um:g} um")
        step = float(steps[axis])
        if step <= 0:
            return {"skip": f"{axis}: the step size is {step:g} um", "centre": {}, "size": {}, "pixels": {},
                    "padded": {}, "notes": notes}
        lo, hi = mid - want / 2.0, mid + want / 2.0
        padded[axis] = (lo, hi)
        wanted_area *= max(want, step)
        limit = ranges.get(axis)
        if limit is not None:
            rlo, rhi = float(limit[0]), float(limit[1])
            if not rlo <= mid <= rhi:
                return {"skip": f"{axis}: the box centre {mid:.2f} um is outside the scanner range "
                                f"[{rlo:.2f}, {rhi:.2f}] um", "centre": {}, "size": {}, "pixels": {},
                        "padded": padded, "notes": notes}
            clipped_lo, clipped_hi = max(lo, rlo), min(hi, rhi)
            if clipped_lo > lo or clipped_hi < hi:
                notes.append(f"{axis}: clipped from [{lo:.2f}, {hi:.2f}] to [{clipped_lo:.2f}, {clipped_hi:.2f}] um "
                             f"by the scanner range")
            lo, hi = clipped_lo, clipped_hi
        count = max(1, math.ceil((hi - lo) / step - 1e-9))
        if limit is not None and count * step > float(limit[1]) - float(limit[0]) + 1e-9:
            count = max(1, math.floor((float(limit[1]) - float(limit[0])) / step + 1e-9))
        extent = count * step
        centre_um = 0.5 * (lo + hi)
        if limit is not None:
            centre_um = min(max(centre_um, float(limit[0]) + extent / 2.0), float(limit[1]) - extent / 2.0)
        centre[axis], size[axis], pixels[axis] = centre_um, extent, count
        fitted_area *= max(min(hi - lo, want), step)
    overlap = fitted_area / wanted_area if wanted_area > 0 else 1.0
    if overlap < float(min_overlap):
        return {"skip": f"only {overlap:.0%} of the padded box lies inside the scanner range "
                        f"(minimum {float(min_overlap):.0%})", "centre": {}, "size": {}, "pixels": {},
                "padded": padded, "notes": notes}
    return {"skip": None, "centre": centre, "size": size, "pixels": pixels, "padded": padded, "notes": notes}


__all__ = ["box_of_row", "plan_box_scan", "BOX_COLUMNS", "SOURCE_BOX_COLUMNS", "has_boxes", "map_boxes", "transform_record"]
