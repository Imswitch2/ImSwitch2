"""Where a set of pixel coordinates lives, and whether two such places match.

An ROI (or any other pixel-coordinate annotation) is meaningless without saying
*of what*: "rows 10-40, columns 12-60" is only comparable against another image
if that image is the same plane of the same data on the same grid.  A
:class:`SpatialFrame` records that context, and :func:`compatibility` decides
what may be measured where.

Four identities are kept apart deliberately, because collapsing them makes
unrelated images look interchangeable:

``dataset_uid``
    the acquisition/source the data ultimately came from.
``result_uid``
    one logical processing result — stable across renames, and across saving
    and reloading the same result.
``coordinate_space_uid``
    the shared pixel grid.  Two results that are pixel-aligned by construction
    (a filter and its input, a projection and its stack) share it; a resample
    does not.  **This is what "exact" compares**, not the dataset.
``lineage``
    provenance only — which results this was derived from.  It deliberately
    carries no spatial transform; relationships between different coordinate
    spaces are explicit :class:`TransformEdge` records instead.

Pure data plus numpy-free arithmetic: this module must stay importable without
napari, Qt or either app package.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Literal, Protocol

#: Verdicts, ordered from most to least trustworthy.
Compatibility = Literal[
    "exact",
    "registered",
    "pixel-compatible",
    "clippable",
    "incompatible",
]

_RANK: dict[str, int] = {
    "exact": 0,
    "registered": 1,
    "pixel-compatible": 2,
    "clippable": 3,
    "incompatible": 4,
}

#: 3x3 row-major pixel -> world transform that changes nothing.
IDENTITY_AFFINE: tuple[float, ...] = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)

IdentityKind = Literal["minted", "derived"]


@dataclass(frozen=True)
class AxisDescriptor:
    """One axis of the data, including the ones not currently displayed.

    ``size`` is what lets a stored position such as ``Z=12`` be validated
    against a different result rather than silently clamped into range.
    """

    label: str
    size: int
    scale: float = 1.0
    unit: str = "px"


@dataclass(frozen=True)
class TransformEdge:
    """A known spatial relationship between two coordinate spaces."""

    src_space: str
    dst_space: str
    affine: tuple[float, ...]  # 3x3 row-major, src pixel -> dst pixel
    source: str = "registration"
    confidence: float | None = None


class TransformRegistry(Protocol):
    """Lookup for :class:`TransformEdge` records."""

    def edge(self, src_space: str, dst_space: str) -> TransformEdge | None:
        ...


class InMemoryTransformRegistry:
    """Trivial registry; nothing in the committed scope populates one."""

    def __init__(self, edges: list[TransformEdge] | None = None):
        self._edges: dict[tuple[str, str], TransformEdge] = {
            (edge.src_space, edge.dst_space): edge for edge in (edges or [])
        }

    def add(self, edge: TransformEdge) -> None:
        self._edges[(edge.src_space, edge.dst_space)] = edge

    def edge(self, src_space: str, dst_space: str) -> TransformEdge | None:
        return self._edges.get((src_space, dst_space))


@dataclass(frozen=True)
class SpatialFrame:
    """The plane a set of pixel coordinates belongs to."""

    coordinate_space_uid: str
    result_uid: str
    dataset_uid: str
    plane_axes: tuple[str, str]
    axes: tuple[AxisDescriptor, ...]
    shape: tuple[int, int]
    affine: tuple[float, ...] = IDENTITY_AFFINE
    unit: str = "px"
    component: str | None = None
    view_mode: str | None = None
    lineage: tuple[str, ...] = ()
    identity_kind: IdentityKind = "minted"
    frame_uid: str = field(default="", compare=False)

    def __post_init__(self):
        # frame_uid is derived, never minted: two views of the same plane must
        # produce the same uid, including across a save/reload cycle where no
        # uid was stored anywhere.
        if not self.frame_uid:
            object.__setattr__(self, "frame_uid", derive_frame_uid(self))

    def axis(self, label: str) -> AxisDescriptor | None:
        for descriptor in self.axes:
            if descriptor.label == label:
                return descriptor
        return None


def derive_frame_uid(frame: SpatialFrame) -> str:
    """Stable hash of everything that makes a displayed plane what it is.

    Deterministic by design (A-27): identical planes always hash alike, so a
    frame recorded in a saved ROI set still matches the same plane in a later
    session without anything having been written into the image file.
    """
    parts = [
        frame.coordinate_space_uid,
        "|".join(frame.plane_axes),
        "x".join(str(int(n)) for n in frame.shape),
        ",".join(f"{float(v):.12g}" for v in frame.affine),
        frame.unit,
        frame.component or "",
        frame.view_mode or "",
    ]
    digest = hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"frame-{digest[:32]}"


def mint_uid(prefix: str) -> str:
    """A fresh identity for something created in this session."""
    return f"{prefix}-{uuid.uuid4()}"


def content_digest_uid(prefix: str, *parts: object) -> str:
    """A deterministic identity for data that arrived without one.

    Used when loading a file that predates identity tracking: the same content
    yields the same uid, and moving the file does not change it (callers pass
    content, not paths). Frames built on these must be marked ``"derived"`` —
    an inferred identity may never claim ``exact``.
    """
    payload = "\x1f".join(str(part) for part in parts)
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:32]}"


def _affine_matches(a: tuple[float, ...], b: tuple[float, ...], *, rtol: float = 1e-6) -> bool:
    if len(a) != len(b):
        return False
    for lhs, rhs in zip(a, b):
        lhs, rhs = float(lhs), float(rhs)
        scale = max(abs(lhs), abs(rhs), 1.0)
        if abs(lhs - rhs) > rtol * scale:
            return False
    return True


def _cap(verdict: Compatibility, ceiling: Compatibility) -> Compatibility:
    """Return the weaker of two verdicts."""
    return verdict if _RANK[verdict] >= _RANK[ceiling] else ceiling


def compatibility(
    source: SpatialFrame,
    target: SpatialFrame,
    *,
    positions: tuple[tuple[str, int], ...] = (),
    transforms: TransformRegistry | None = None,
) -> Compatibility:
    """Can coordinates recorded in ``source`` be measured against ``target``?

    Ordered as a decision tree, not a table of conditions: the order is the
    whole point.  Unrelated data is rejected on the coordinate-space check
    **before** any shape or scale comparison is reached, so two unrelated
    images that happen to share a shape can never come back as comparable.

    ``positions`` are the axis-labelled slice indices the coordinates were
    recorded at, e.g. ``(("Z", 12), ("T", 3))``.  They are passed in rather
    than read off a record so this module stays independent of any particular
    annotation type.

    ``transforms`` is consulted only to *name* the ``registered`` case; nothing
    in the committed scope measures through a transform, because doing so is
    reprojection and reprojection stays an explicit user action.
    """
    # 1. A different plane is a different physical quantity: an XY region says
    #    nothing about an XZ slice.
    if tuple(source.plane_axes) != tuple(target.plane_axes):
        return "incompatible"

    # 2. A stored slice position must exist in the target and be in range.
    for label, index in positions:
        descriptor = target.axis(label)
        if descriptor is None:
            return "incompatible"
        if not 0 <= int(index) < int(descriptor.size):
            return "incompatible"

    # 3. Different grids are only relatable through an explicit transform.
    #    This is where unrelated data is rejected -- before shapes are compared.
    if source.coordinate_space_uid != target.coordinate_space_uid:
        if transforms is not None:
            edge = transforms.edge(
                source.coordinate_space_uid, target.coordinate_space_uid
            )
            if edge is not None:
                return "registered"
        return "incompatible"

    # -- from here on both frames are on the same pixel grid ------------------

    # 4. An identity that was inferred rather than recorded may not claim
    #    certainty, however well everything else lines up.
    ceiling: Compatibility = "exact"
    if "derived" in (source.identity_kind, target.identity_kind):
        ceiling = "pixel-compatible"

    # 5. Same plane, same grid, same geometry.
    if source.frame_uid == target.frame_uid:
        return _cap("exact", ceiling)
    if (
        tuple(source.shape) == tuple(target.shape)
        and _affine_matches(source.affine, target.affine)
        and source.unit == target.unit
    ):
        return _cap("exact", ceiling)

    # 6. Same pixels, different calibration.
    if tuple(source.shape) == tuple(target.shape):
        return _cap("pixel-compatible", ceiling)

    # 7. Same grid, different extent: usable only over the overlap, and only
    #    if the user opts in.
    return _cap("clippable", ceiling)


#: Verdicts that may be measured without the user explicitly opting in.
AUTO_MEASURABLE: frozenset[str] = frozenset({"exact", "pixel-compatible"})

#: Verdicts a user may opt into through the preflight dialog.
OPT_IN_MEASURABLE: frozenset[str] = frozenset({"clippable"})


def is_auto_measurable(verdict: Compatibility) -> bool:
    """True when measuring needs no confirmation.

    ``registered`` is deliberately excluded: measuring through a transform is
    reprojection, which stays an explicit action.
    """
    return verdict in AUTO_MEASURABLE


__all__ = [
    "AUTO_MEASURABLE",
    "OPT_IN_MEASURABLE",
    "AxisDescriptor",
    "Compatibility",
    "IDENTITY_AFFINE",
    "IdentityKind",
    "InMemoryTransformRegistry",
    "SpatialFrame",
    "TransformEdge",
    "TransformRegistry",
    "compatibility",
    "content_digest_uid",
    "derive_frame_uid",
    "is_auto_measurable",
    "mint_uid",
]
