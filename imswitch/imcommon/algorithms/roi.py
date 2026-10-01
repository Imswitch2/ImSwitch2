"""Shared ROI record data contract.

A plain, dependency-light value type describing a named region of interest in
image row/column coordinates. Shared by ``improcess`` ROI/segmentation tools
and any ``imcontrol`` workflow that needs to describe regions, so it lives in
``imcommon`` rather than either app module.

Fields are **append-only**: the type is constructed positionally in several
places, and every mutation goes through a helper that copies what it is not
changing, so a record gaining a field never silently loses it somewhere else.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field, fields
from typing import Literal

from .roi_payload import MaskPayload
from .roi_style import ROIStyle

#: Changes to these fields alter what an ROI measures, so they bump
#: ``revision`` and invalidate cached measurements. Renaming or restyling an
#: ROI deliberately does not — otherwise renaming one ROI would throw away
#: every cached measurement in the set.
MEASUREMENT_AFFECTING_FIELDS = frozenset(
    {"bounds", "pixels", "vertices", "mask", "position", "frame_uid", "visible"}
)


@dataclass(frozen=True)
class ROIRecord:
    """A named ROI in image row/column coordinates."""

    # --- 1.0 fields, unchanged in name, order and meaning -----------------
    name: str
    roi_type: str
    bounds: tuple[int, int, int, int]
    visible: bool = True
    source: str = "manual"
    #: Deprecated in favour of :attr:`mask`; still read so 1.0 records and
    #: exports keep measuring exactly as they did.
    pixels: tuple[tuple[int, int], ...] | None = None

    # --- 2.0 fields, appended together --------------------------------------
    #: Stable identity. Empty on a freshly constructed record; the model
    #: assigns one on ingest. Not a default_factory, so two records built the
    #: same way still compare equal and existing tests are unaffected.
    uid: str = ""
    #: Bumped only by measurement-affecting changes (see the constant above).
    revision: int = 0
    #: Row/col vertices in image pixel coordinates, for vector ROI types.
    vertices: tuple[tuple[float, float], ...] | None = None
    #: Encoded pixel mask over :attr:`bounds`, for composite ROIs.
    mask: MaskPayload | None = None
    #: Axis-labelled slice position, e.g. ``(("Z", 12), ("T", 3))``. An axis
    #: that is absent means "every index on that axis", which is ImageJ's
    #: default. Labelled rather than a fixed (C, Z, T) tuple because axis
    #: labels here are arbitrary and their order is view-dependent.
    position: tuple[tuple[str, int], ...] = ()
    #: The plane this ROI's coordinates belong to (see spatial_frame). The
    #: frame itself lives once per ROI set, not copied onto every record.
    frame_uid: str = ""
    #: Appearance override; None inherits the set's default style.
    style: ROIStyle | None = None
    group: int = 0
    properties: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, object]:
        """JSON-ready form, omitting anything left at its default.

        Keeping defaults out means a plain rectangle serialises to exactly what
        it did in 1.0, so old and new exports stay readable by both.
        """
        data: dict[str, object] = {
            "name": self.name,
            "roi_type": self.roi_type,
            "bounds": list(self.bounds),
            "visible": self.visible,
            "source": self.source,
        }
        if self.pixels is not None:
            data["pixels"] = [list(pixel) for pixel in self.pixels]
        if self.uid:
            data["uid"] = self.uid
        if self.revision:
            data["revision"] = self.revision
        if self.vertices is not None:
            data["vertices"] = [list(vertex) for vertex in self.vertices]
        if self.mask is not None:
            data["mask"] = self.mask.to_json()
        if self.position:
            data["position"] = [list(item) for item in self.position]
        if self.frame_uid:
            data["frame_uid"] = self.frame_uid
        if self.style is not None:
            data["style"] = self.style.to_json()
        if self.group:
            data["group"] = self.group
        if self.properties:
            data["properties"] = [list(item) for item in self.properties]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "ROIRecord":
        """Read a record from any subset of the fields.

        Tolerating omissions is the migration path: a 1.0 export has none of
        the 2.0 fields and must load unchanged.
        """
        bounds = data.get("bounds", (0, 0, 0, 0))
        pixels = data.get("pixels")
        vertices = data.get("vertices")
        mask = data.get("mask")
        position = data.get("position") or ()
        properties = data.get("properties") or ()
        return cls(
            name=str(data.get("name", "ROI")),
            roi_type=str(data.get("roi_type", "rectangle")),
            bounds=tuple(int(v) for v in bounds),  # type: ignore[arg-type]
            visible=bool(data.get("visible", True)),
            source=str(data.get("source", "manual")),
            pixels=(
                tuple((int(row), int(col)) for row, col in pixels)  # type: ignore[union-attr]
                if pixels is not None
                else None
            ),
            uid=str(data.get("uid", "")),
            revision=int(data.get("revision", 0)),
            vertices=(
                tuple((float(row), float(col)) for row, col in vertices)  # type: ignore[union-attr]
                if vertices is not None
                else None
            ),
            mask=MaskPayload.from_json(mask) if mask else None,  # type: ignore[arg-type]
            position=tuple(
                (str(label), int(index)) for label, index in position  # type: ignore[misc]
            ),
            frame_uid=str(data.get("frame_uid", "")),
            style=ROIStyle.from_json(data.get("style")),  # type: ignore[arg-type]
            group=int(data.get("group", 0)),
            properties=tuple(
                (str(key), str(value)) for key, value in properties  # type: ignore[misc]
            ),
        )


def new_uid() -> str:
    """A fresh ROI identity.

    A full uuid: an ROI set can be merged with another from a different
    session or machine, and shortening the identity to save a few characters
    would trade collision resistance for nothing anyone can see.
    """
    return str(uuid.uuid4())


def replaced(roi: ROIRecord, **changes) -> ROIRecord:
    """Return ``roi`` with ``changes`` applied, preserving every other field.

    The single sanctioned way to modify a record. Rebuilding one field by
    field looks equivalent and silently drops anything the call site forgot,
    which is how geometry, style or provenance would go missing on something
    as innocuous as a rename.

    ``uid`` is preserved (only an explicit change replaces it, which is what
    duplication does) and ``revision`` advances exactly when the change can
    alter what the ROI measures.
    """
    import dataclasses

    if "revision" not in changes:
        touched = set(changes) & MEASUREMENT_AFFECTING_FIELDS
        if touched:
            changes["revision"] = roi.revision + 1
    return dataclasses.replace(roi, **changes)


def duplicated(roi: ROIRecord, *, name: str) -> ROIRecord:
    """A copy of ``roi`` under ``name`` — the one case that mints a new uid."""
    return replaced(roi, name=name, uid=new_uid())


__all__ = [
    "MEASUREMENT_AFFECTING_FIELDS",
    "ROIRecord",
    "ROIStyle",
    "MaskPayload",
    "duplicated",
    "new_uid",
    "replaced",
]
