"""ImageJ `.roi` and `RoiSet.zip` interop (P-6.1, P-6.6).

Fiji's ROI format is the lingua franca of this corner of microscopy, so the
value here is not in reading our own files back — it is in a set drawn in Fiji
measuring here, and a set drawn here opening there.

`roifile` is imported **lazily**, inside the functions that need it, so it is a
genuinely optional dependency: without it the panel's buttons are disabled with
a tooltip saying why, and nothing else in ImSwitch notices.

**What crosses, and what does not.** ImageJ's format cannot express everything
an `ROIRecord` holds — arbitrary axis labels, a spatial frame, our style model.
Rather than dropping those quietly, every conversion returns an
:class:`InteropReport` listing what was lost and why. A silent lossy export is
how a user discovers six months later that their groups never made it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .roi import ROIRecord, new_uid
from .roi_geometry import roi_bounds, roi_mask_local, roi_outline

#: ImageJ position axes, in the order its API exposes them. Anything else in an
#: ROI's position cannot be written to a `.roi` file at all.
IMAGEJ_AXES = ("C", "Z", "T")

#: `roifile.ROI_TYPE` name (lowercased) -> our roi_type. Taken from the enum
#: rather than guessed: ImageJ calls a rectangle "RECT", and a map keyed on
#: "rectangle" silently falls through to the polygon default for every
#: rectangle ever exported.
_IMPORT_TYPES = {
    "rect": "rectangle",
    "oval": "ellipse",
    "polygon": "polygon",
    "freehand": "freehand",
    "traced": "freehand",
    "freeline": "line",
    "polyline": "line",
    "line": "line",
    "point": "point",
}


class ImageJInteropError(RuntimeError):
    """Interop was asked for and could not be done, with a reason."""


@dataclass
class InteropReport:
    """What an import or export could not carry across.

    Structured rather than logged: the panel shows it, and a test can assert
    that a specific loss was *reported* rather than merely not crashing.
    """

    losses: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    count: int = 0

    def loss(self, message: str) -> None:
        if message not in self.losses:
            self.losses.append(message)

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    @property
    def lossless(self) -> bool:
        return not self.losses

    @property
    def summary(self) -> str:
        parts = [f"{self.count} ROI(s)"]
        if self.losses:
            parts.append(f"{len(self.losses)} thing(s) could not be carried across")
        if self.warnings:
            parts.append(f"{len(self.warnings)} warning(s)")
        return "; ".join(parts)


def available() -> bool:
    """Whether ImageJ interop can run at all."""
    try:
        import roifile  # noqa: F401
    except Exception:
        return False
    return True


def _require_roifile():
    try:
        import roifile
    except Exception as exc:  # pragma: no cover - exercised by `available()`
        raise ImageJInteropError(
            "ImageJ ROI interop needs the optional 'roifile' package "
            "(pip install roifile)"
        ) from exc
    return roifile


# --------------------------------------------------------------------------
# import
# --------------------------------------------------------------------------

def _position_from(imagej_roi) -> tuple[tuple[str, int], ...]:
    """ImageJ's C/Z/T position as our axis-labelled form.

    ImageJ counts slices from 1 and uses 0 for "not set"; we count from 0, so
    an unset axis must be dropped rather than turned into slice -1.
    """
    position = []
    for label, attribute in zip(IMAGEJ_AXES, ("c_position", "z_position", "t_position")):
        value = int(getattr(imagej_roi, attribute, 0) or 0)
        if value > 0:
            position.append((label, value - 1))
    return tuple(position)


def roi_from_imagej(imagej_roi, report: InteropReport) -> ROIRecord:
    """One ImageJ ROI as an :class:`ROIRecord`."""
    kind = str(getattr(imagej_roi.roitype, "name", imagej_roi.roitype)).lower()
    roi_type = _IMPORT_TYPES.get(kind, "polygon")
    name = str(getattr(imagej_roi, "name", "") or "ROI")

    coordinates = None
    try:
        coordinates = imagej_roi.coordinates()
    except Exception:
        coordinates = None

    vertices = None
    if coordinates is not None and len(np.shape(coordinates)) == 2:
        # roifile yields (x, y); our records are (row, col) throughout, and
        # mixing the two is the classic way to transpose an entire ROI set.
        points = np.asarray(coordinates, dtype=float)
        vertices = tuple((float(y), float(x)) for x, y in points)

    if roi_type == "rectangle" and vertices is not None:
        rows = [v[0] for v in vertices]
        cols = [v[1] for v in vertices]
        bounds = (
            int(np.floor(min(rows))), int(np.ceil(max(rows))),
            int(np.floor(min(cols))), int(np.ceil(max(cols))),
        )
        vertices = None
    elif vertices is not None:
        rows = [v[0] for v in vertices]
        cols = [v[1] for v in vertices]
        bounds = (
            int(np.floor(min(rows))), int(np.ceil(max(rows))),
            int(np.floor(min(cols))), int(np.ceil(max(cols))),
        )
    else:
        top = int(getattr(imagej_roi, "top", 0) or 0)
        left = int(getattr(imagej_roi, "left", 0) or 0)
        bottom = int(getattr(imagej_roi, "bottom", top) or top)
        right = int(getattr(imagej_roi, "right", left) or left)
        bounds = (top, bottom, left, right)
        report.warn(f"{name}: no outline in the file; using its bounding box")

    if kind not in _IMPORT_TYPES:
        report.warn(f"{name}: ImageJ type {kind!r} imported as a polygon")

    group = int(getattr(imagej_roi, "group", 0) or 0)
    return ROIRecord(
        name=name,
        roi_type=roi_type,
        bounds=bounds,
        source="imagej",
        vertices=vertices,
        position=_position_from(imagej_roi),
        group=group,
        uid=new_uid(),
    )


def read_imagej(path) -> tuple[list[ROIRecord], InteropReport]:
    """Every ROI in a `.roi` or `RoiSet.zip`, with a report of what was lost."""
    roifile = _require_roifile()
    report = InteropReport()
    try:
        found = roifile.roiread(str(path))
    except Exception as exc:
        raise ImageJInteropError(f"could not read {path}: {exc}") from exc

    if not isinstance(found, (list, tuple)):
        found = [found]

    rois = []
    for index, item in enumerate(found):
        try:
            rois.append(roi_from_imagej(item, report))
        except Exception as exc:
            # One unreadable ROI in a zip of two hundred must not cost the
            # other one hundred and ninety-nine.
            report.warn(f"ROI {index} could not be read: {exc}")
    report.count = len(rois)
    if rois:
        report.loss(
            "ImageJ files carry no spatial frame, so imported ROIs are not "
            "tied to a plane until they are measured"
        )
    return rois, report


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------

def _imagej_position(roi: ROIRecord, report: InteropReport) -> dict:
    """Our position as ImageJ's C/Z/T, reporting axes it cannot hold."""
    out = {}
    for label, index in roi.position:
        if label in IMAGEJ_AXES:
            out[f"{label.lower()}_position"] = int(index) + 1
        else:
            report.loss(
                f"ImageJ positions are C/Z/T only, so the {label} index of "
                f"{roi.name!r} is not written"
            )
    return out


def roi_to_imagej(roi: ROIRecord, report: InteropReport):
    """One :class:`ROIRecord` as an ImageJ ROI object."""
    roifile = _require_roifile()

    kind = str(roi.roi_type or "").lower()
    if kind == "rectangle" and roi.vertices is None:
        r0, r1, c0, c1 = roi_bounds(roi)
        imagej = roifile.ImagejRoi.frompoints(
            [[c0, r0], [c1, r0], [c1, r1], [c0, r1]]
        )
        imagej.roitype = roifile.ROI_TYPE.RECT
        imagej.left, imagej.right = int(c0), int(c1)
        imagej.top, imagej.bottom = int(r0), int(r1)
    else:
        parts = roi_outline(roi)
        if not parts:
            raise ImageJInteropError(f"{roi.name!r} has no outline to write")
        if len(parts) > 1:
            report.loss(
                f"{roi.name!r} has {len(parts)} disconnected parts; ImageJ "
                "stores one outline, so only the largest is written"
            )
            parts = [max(parts, key=len)]
        # (row, col) -> (x, y), the one conversion that silently transposes an
        # entire set when it is forgotten.
        points = [[float(c), float(r)] for r, c in np.asarray(parts[0], dtype=float)]
        imagej = roifile.ImagejRoi.frompoints(points)

    imagej.name = str(roi.name)
    if roi.group:
        try:
            imagej.group = int(roi.group)
        except Exception:
            report.loss(f"the group of {roi.name!r} could not be written")
    for key, value in _imagej_position(roi, report).items():
        setattr(imagej, key, value)

    if roi.mask is not None and kind not in ("rectangle", "ellipse", "polygon"):
        report.loss(
            f"{roi.name!r} is a pixel mask; ImageJ stores it as its traced "
            "outline, so holes and single-pixel detail may not survive"
        )
    if roi.style is not None:
        report.loss("ImSwitch ROI styles have no ImageJ equivalent")
    if roi.properties:
        report.loss(
            "ImageJ has no place for arbitrary ROI properties, so they are "
            "not written"
        )
    return imagej


def write_imagej(path, rois) -> InteropReport:
    """Write ROIs to a `.roi` (one) or `RoiSet.zip` (many)."""
    roifile = _require_roifile()
    report = InteropReport()
    converted = []
    for roi in rois:
        try:
            converted.append(roi_to_imagej(roi, report))
        except Exception as exc:
            report.warn(f"{roi.name!r} could not be written: {exc}")
    if not converted:
        raise ImageJInteropError("none of these ROIs could be written")
    report.count = len(converted)
    # A single ROI to a `.roi` path must be written as one ROI, not as a
    # one-entry zip: `roiwrite` decides by what it is *given*, not by the
    # extension, so passing a list writes a zip whatever the file is called —
    # and Fiji then refuses to open the `.roi` it produced.
    payload = converted[0] if len(converted) == 1 and str(path).endswith(".roi") else converted
    try:
        roifile.roiwrite(str(path), payload, mode="w")
    except Exception as exc:
        raise ImageJInteropError(f"could not write {path}: {exc}") from exc
    return report


def imagej_mask(roi: ROIRecord, shape) -> np.ndarray:
    """The ROI's local mask, for callers comparing an import with an export."""
    local, _slices = roi_mask_local(roi, shape)
    return local


__all__ = [
    "IMAGEJ_AXES",
    "ImageJInteropError",
    "InteropReport",
    "available",
    "imagej_mask",
    "read_imagej",
    "roi_from_imagej",
    "roi_to_imagej",
    "write_imagej",
]
