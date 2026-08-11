"""Running a processor over a region rather than a whole frame (P-R).

A processor written against "an image" should not have to learn about ROIs to
be usable on one. So the restriction is applied *around* it, centrally: the
input is narrowed, the processor runs on an ordinary array, and the output is
told what it was narrowed to.

Two modes, because they answer different questions and neither is a superset:

* **crop** — the output is the region, on its own smaller grid. What you want
  for "denoise this cell and look at it", and for anything whose cost scales
  with the frame.
* **mask** — the output is the whole frame with everything outside the region
  set aside. What you want when the result has to stay pixel-aligned with the
  original, or when a filter's behaviour at the region's edge matters.

The distinction is not cosmetic: a crop moves every pixel index, so an ROI
drawn on the input measures different pixels on the output. That is why a
cropped result is never allowed to claim its source's coordinate space,
whatever the processor declares about preserving the grid.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

from imswitch.imcommon.algorithms.roi_geometry import roi_bounds, roi_mask_local

#: How an ROI narrows a processor's input.
ROI_MODES = ("crop", "mask")

#: The parameter key the UI fills in and the run path reads. One name, so a
#: processor never has to invent its own and a caller never has to guess.
ROI_PARAM = "roi_restriction"


class ROIRestrictionError(ValueError):
    """A restriction could not be applied, with a reason worth showing."""


@dataclass(frozen=True)
class ROIRestriction:
    """Which regions to run over, and how."""

    rois: tuple = ()
    mode: str = "crop"
    #: Where the regions came from, recorded on the output so a result can say
    #: which ROI set — at which revision — produced it.
    set_uid: str = ""
    set_name: str = ""
    set_revision: int = 0
    #: What masked-out pixels become. NaN says "not measured" to anything
    #: numerically literate; 0 says "measured, and dark", which is a different
    #: and usually false claim.
    fill: float = float("nan")
    #: Filled in by :func:`restrict_result`; the pixel offset of a crop.
    offset: tuple[int, int] = (0, 0)

    def __post_init__(self):
        if self.mode not in ROI_MODES:
            raise ROIRestrictionError(
                f"unknown ROI mode {self.mode!r}; expected one of {ROI_MODES}"
            )

    @property
    def active(self) -> bool:
        return bool(self.rois)

    def provenance(self) -> dict:
        """What the output records about where its region came from."""
        return {
            "roi_set_uid": self.set_uid,
            "roi_set_name": self.set_name,
            "roi_set_revision": int(self.set_revision),
            "roi_mode": self.mode,
            "roi_names": [str(getattr(roi, "name", "")) for roi in self.rois],
            "roi_uids": [str(getattr(roi, "uid", "")) for roi in self.rois],
            "roi_offset": [int(self.offset[0]), int(self.offset[1])],
        }


def union_bounds(rois, shape) -> tuple[int, int, int, int]:
    """The clipped box covering every ROI, or a refusal.

    Clipped to the image because a region that is partly outside it is still a
    region; refused only when *nothing* of it is inside, which is a different
    and genuinely unanswerable case.
    """
    boxes = [roi_bounds(roi) for roi in rois]
    if not boxes:
        raise ROIRestrictionError("no ROIs to restrict to")
    height, width = int(shape[0]), int(shape[1])
    r0 = max(0, min(box[0] for box in boxes))
    r1 = min(height, max(box[1] for box in boxes))
    c0 = max(0, min(box[2] for box in boxes))
    c1 = min(width, max(box[3] for box in boxes))
    if r1 <= r0 or c1 <= c0:
        raise ROIRestrictionError(
            "the selected ROIs fall entirely outside this image"
        )
    return (r0, r1, c0, c1)


def union_mask(rois, shape) -> np.ndarray:
    """An image-sized boolean union of the ROIs.

    Image-sized by necessity — masking is defined over the frame — but built
    from each ROI's own local mask, so the cost is the sum of the regions
    plus one frame, not one frame per ROI.
    """
    height, width = int(shape[0]), int(shape[1])
    out = np.zeros((height, width), dtype=bool)
    for roi in rois:
        try:
            local, slices = roi_mask_local(roi, (height, width))
        except Exception:
            # A line or a point has no interior to mask with. Skipped rather
            # than refused: a mixed set should still restrict by its regions.
            continue
        if local.size:
            out[slices] |= local
    if not out.any():
        raise ROIRestrictionError(
            "the selected ROIs cover no pixels of this image"
        )
    return out


def restrict_array(
    data, axis_labels, restriction: ROIRestriction
) -> tuple[np.ndarray, tuple[int, int]]:
    """``(restricted, offset)`` for an nD array whose last two axes are the plane.

    Every non-plane axis is preserved: restricting a Z-stack to a region gives
    the region on every slice, not one slice of it.
    """
    array = np.asarray(data)
    if array.ndim < 2:
        raise ROIRestrictionError("this result has no image plane to restrict")
    shape = array.shape[-2:]

    if restriction.mode == "crop":
        r0, r1, c0, c1 = union_bounds(restriction.rois, shape)
        return array[..., r0:r1, c0:c1], (r0, c0)

    mask = union_mask(restriction.rois, shape)
    # float, because the fill is NaN by default and an integer array cannot
    # hold it — silently casting NaN to 0 there would be exactly the false
    # "measured, and dark" claim the fill exists to avoid.
    out = array.astype(np.float64, copy=True)
    out[..., ~mask] = restriction.fill
    return out, (0, 0)


def restrict_result(result, restriction: ROIRestriction):
    """A shallow copy of ``result`` holding only the restricted pixels.

    A copy rather than a mutation: the source stays exactly as it was, so a
    failed or cancelled run leaves nothing behind, and the same source can be
    restricted two different ways in one session.
    """
    import copy

    if not restriction.active:
        return result, restriction

    restricted, offset = restrict_array(
        getattr(result, "data", None), getattr(result, "axis_labels", ()), restriction
    )
    narrowed = copy.copy(result)
    narrowed.data = restricted
    name = getattr(result, "name", "result")
    narrowed.name = f"{name} [{restriction.mode}]"
    # A crop moves every pixel index, so the narrowed input is *not* on its
    # source's grid. Recorded here so the run path can override whatever the
    # processor declares about preserving it.
    return narrowed, replace(restriction, offset=offset)


def apply_provenance(results, restriction: ROIRestriction) -> None:
    """Record the region on each output, and refuse it the source's grid.

    Both halves matter. Without the provenance a cropped result cannot say
    which ROI produced it; without the coordinate-space reset it would claim
    to share a grid with its source, and every ROI measured on it afterwards
    would read the wrong pixels.
    """
    if not restriction.active:
        return
    provenance = restriction.provenance()
    for result in results:
        try:
            existing = dict(getattr(result, "roi_provenance", {}) or {})
        except Exception:
            existing = {}
        existing.update(provenance)
        try:
            result.roi_provenance = existing
        except Exception:
            continue
        if restriction.mode == "crop":
            mint = getattr(result, "mint_coordinate_space", None)
            if callable(mint):
                mint()


__all__ = [
    "ROI_MODES",
    "ROI_PARAM",
    "ROIRestriction",
    "ROIRestrictionError",
    "apply_provenance",
    "restrict_array",
    "restrict_result",
    "union_bounds",
    "union_mask",
]
