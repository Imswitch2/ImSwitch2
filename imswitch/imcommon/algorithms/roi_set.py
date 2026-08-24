"""A named collection of ROIs, and the settings measured alongside them.

A single global ROI list stops making sense as soon as two results are open:
regions drawn on one reconstruction are not meaningful on another unless the
frames say so.  Grouping ROIs with the frames they belong to — and with the
measurement configuration they were measured under — is what makes an exported
set self-describing, and what lets persistence be a single object rather than
several loosely-related blobs.

Frames are stored **once per set** and referenced by uid from each record, so a
two-hundred-ROI set carries one copy of its frame rather than two hundred.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace

from .roi import ROIRecord
from .roi_style import ROIStyle
from .spatial_frame import SpatialFrame


@dataclass(frozen=True)
class MeasurementConfig:
    """Which measurements are taken, and how they are reported."""

    #: Chosen measurement ids. ``None`` means "not configured" and takes the
    #: registry's defaults; an empty tuple means the user explicitly wants no
    #: measurements, which is a different thing and must not silently become
    #: the defaults again.
    selected: tuple[str, ...] | None = None
    decimals: int = 3
    scientific: bool = False
    #: Inclusive intensity window for "limit to threshold"; None counts every
    #: finite pixel. Explicit rather than implied by whatever the segmentation
    #: panel last used, so a measurement can be reproduced from the set alone.
    threshold: tuple[float, float] | None = None
    #: Perpendicular samples averaged for a line ROI, as in ImageJ. Part of
    #: the configuration rather than a per-measurement argument because it
    #: changes the numbers, so it has to travel with them.
    line_width: int = 1
    #: ImageJ's *Display label*: add a human-readable ``label`` column
    #: ("image:roi") to published rows. The machine-readable identity columns
    #: are not affected — §4.8 requires those on every row regardless.
    display_label: bool = False
    revision: int = 0

    def with_changes(self, **changes) -> "MeasurementConfig":
        changes.setdefault("revision", self.revision + 1)
        return replace(self, **changes)

    def to_json(self) -> dict:
        return {
            "selected": None if self.selected is None else list(self.selected),
            "decimals": self.decimals,
            "scientific": self.scientific,
            "threshold": list(self.threshold) if self.threshold else None,
            "line_width": self.line_width,
            "display_label": self.display_label,
            "revision": self.revision,
        }

    @classmethod
    def from_json(cls, payload: dict | None) -> "MeasurementConfig":
        if not payload:
            return cls()
        threshold = payload.get("threshold")
        selected = payload.get("selected")
        return cls(
            selected=None if selected is None else tuple(str(v) for v in selected),
            decimals=int(payload.get("decimals", 3)),
            scientific=bool(payload.get("scientific", False)),
            threshold=tuple(float(v) for v in threshold) if threshold else None,  # type: ignore[arg-type]
            line_width=int(payload.get("line_width", 1)),
            display_label=bool(payload.get("display_label", False)),
            revision=int(payload.get("revision", 0)),
        )


@dataclass(frozen=True)
class ROISet:
    """An ordered set of ROIs plus everything needed to interpret them."""

    uid: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = "ROIs"
    rois: tuple[ROIRecord, ...] = ()
    frames: tuple[SpatialFrame, ...] = ()
    default_style: ROIStyle | None = None
    measurement_config: MeasurementConfig = field(default_factory=MeasurementConfig)
    #: Advances on *any* change, including renames — the panel and overlay
    #: redraw off this, while per-ROI ``revision`` (which ignores renames)
    #: drives measurement caching.
    revision: int = 0
    dataset_uid: str | None = None

    def frame(self, frame_uid: str) -> SpatialFrame | None:
        for frame in self.frames:
            if frame.frame_uid == frame_uid:
                return frame
        return None

    def with_changes(self, **changes) -> "ROISet":
        """Any other field, with the set's revision advanced.

        The revision is what the panel and overlay redraw off, so a change
        applied without it is a change nothing notices.
        """
        changes.setdefault("revision", self.revision + 1)
        return replace(self, **changes)

    def with_rois(self, rois) -> "ROISet":
        return replace(self, rois=tuple(rois), revision=self.revision + 1)

    def with_frame(self, frame: SpatialFrame) -> "ROISet":
        """Register ``frame`` if the set does not already know it."""
        if self.frame(frame.frame_uid) is not None:
            return self
        return replace(self, frames=(*self.frames, frame), revision=self.revision + 1)

    def to_json(self) -> dict:
        from dataclasses import asdict

        return {
            "uid": self.uid,
            "name": self.name,
            "rois": [roi.to_dict() for roi in self.rois],
            "frames": [asdict(frame) for frame in self.frames],
            "default_style": self.default_style.to_json() if self.default_style else None,
            "measurement_config": self.measurement_config.to_json(),
            "revision": self.revision,
            "dataset_uid": self.dataset_uid,
        }


# --------------------------------------------------------------------------
# P-S — more than one set: merging and comparing
# --------------------------------------------------------------------------

#: What may happen to an incoming ROI when two sets are merged.
CONFLICT_POLICIES = ("skip", "replace", "keep-both")


@dataclass(frozen=True)
class MergeReport:
    """What a merge did, per incoming ROI.

    A merge that silently overwrote would be indistinguishable from one that
    found nothing to overwrite, so every category is reported — including the
    boring ones, because "12 identical, 0 conflicts" is the answer that lets
    someone trust the result.
    """

    added: tuple[str, ...] = ()
    identical: tuple[str, ...] = ()
    renamed: tuple[tuple[str, str], ...] = ()
    conflicts: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    replaced: tuple[str, ...] = ()

    @property
    def summary(self) -> str:
        parts = []
        for label, values in (
            ("added", self.added),
            ("identical", self.identical),
            ("renamed", self.renamed),
            ("replaced", self.replaced),
            ("skipped", self.skipped),
        ):
            if values:
                parts.append(f"{len(values)} {label}")
        return ", ".join(parts) if parts else "nothing to merge"


def _same_roi(left: ROIRecord, right: ROIRecord) -> bool:
    """Whether two records describe the same region, ignoring display-only fields.

    Compared on the measurement-affecting fields plus the type: a set merged
    from a copy where only the colour changed is not a conflict, and calling it
    one would put a decision in front of the user for nothing.
    """
    from .roi import MEASUREMENT_AFFECTING_FIELDS

    if left.roi_type != right.roi_type:
        return False
    return all(
        getattr(left, field_name) == getattr(right, field_name)
        for field_name in MEASUREMENT_AFFECTING_FIELDS
    )


def _unique_name(base: str, taken) -> str:
    if base not in taken:
        return base
    index = 1
    while f"{base}_{index}" in taken:
        index += 1
    return f"{base}_{index}"


def merge_sets(
    target: "ROISet", source: "ROISet", *, on_conflict: str = "skip"
) -> tuple["ROISet", MergeReport]:
    """Fold ``source`` into ``target``, reporting every decision.

    Identity does the work. An ROI present in both under the same uid **is**
    the same ROI: identical if its geometry matches, a conflict if it does
    not — the same region edited two different ways. A different uid that
    happens to share a name is only a name collision, resolved by renaming,
    because a name is display text and was never the identity.

    ``on_conflict`` decides what a genuine conflict costs: ``skip`` keeps the
    target's version, ``replace`` takes the source's, ``keep-both`` admits
    the source's under a fresh uid and name. Defaulting to ``skip`` because it
    is the only one of the three that cannot lose work.
    """
    from .roi import new_uid, replaced

    if on_conflict not in CONFLICT_POLICIES:
        raise ValueError(f"unknown conflict policy {on_conflict!r}")

    rois = list(target.rois)
    by_uid = {roi.uid: index for index, roi in enumerate(rois) if roi.uid}
    names = {roi.name for roi in rois}

    added, identical, renamed, conflicts, skipped, replaced_uids = [], [], [], [], [], []

    for incoming in source.rois:
        existing_index = by_uid.get(incoming.uid) if incoming.uid else None
        if existing_index is not None:
            existing = rois[existing_index]
            if _same_roi(existing, incoming):
                identical.append(incoming.uid)
                continue
            conflicts.append(incoming.uid)
            if on_conflict == "skip":
                skipped.append(incoming.uid)
                continue
            if on_conflict == "replace":
                rois[existing_index] = incoming
                replaced_uids.append(incoming.uid)
                continue
            # keep-both: a fresh identity, because two different regions
            # cannot share one uid without one of them becoming unreachable.
            incoming = replaced(incoming, uid=new_uid())

        name = _unique_name(incoming.name, names)
        if name != incoming.name:
            renamed.append((incoming.name, name))
            incoming = replaced(incoming, name=name)
        names.add(name)
        if incoming.uid:
            by_uid[incoming.uid] = len(rois)
        rois.append(incoming)
        added.append(incoming.uid)

    merged = target.with_rois(rois)
    # Frames come along, still stored once per set: an ROI whose frame_uid
    # pointed into the source set would otherwise resolve to nothing here,
    # and an unresolvable frame reads as "no provenance" rather than as the
    # missing-data bug it is.
    for frame in source.frames:
        merged = merged.with_frame(frame)

    return merged, MergeReport(
        added=tuple(added),
        identical=tuple(identical),
        renamed=tuple(renamed),
        conflicts=tuple(conflicts),
        skipped=tuple(skipped),
        replaced=tuple(replaced_uids),
    )


@dataclass(frozen=True)
class ComparisonReport:
    """How two sets differ, by identity."""

    only_in_left: tuple[str, ...] = ()
    only_in_right: tuple[str, ...] = ()
    identical: tuple[str, ...] = ()
    different: tuple[str, ...] = ()

    @property
    def summary(self) -> str:
        return (
            f"{len(self.identical)} identical, {len(self.different)} differing, "
            f"{len(self.only_in_left)} only in the first, "
            f"{len(self.only_in_right)} only in the second"
        )


def compare_sets(left: "ROISet", right: "ROISet") -> ComparisonReport:
    """What two sets have in common and where they diverge.

    Keyed on uid, like the merge: comparing by name would report a rename as
    two unrelated ROIs, which is the opposite of what a comparison is for.
    """
    left_by_uid = {roi.uid: roi for roi in left.rois if roi.uid}
    right_by_uid = {roi.uid: roi for roi in right.rois if roi.uid}
    shared = left_by_uid.keys() & right_by_uid.keys()
    return ComparisonReport(
        only_in_left=tuple(sorted(left_by_uid.keys() - right_by_uid.keys())),
        only_in_right=tuple(sorted(right_by_uid.keys() - left_by_uid.keys())),
        identical=tuple(
            sorted(uid for uid in shared if _same_roi(left_by_uid[uid], right_by_uid[uid]))
        ),
        different=tuple(
            sorted(
                uid for uid in shared
                if not _same_roi(left_by_uid[uid], right_by_uid[uid])
            )
        ),
    )


__all__ = [
    "CONFLICT_POLICIES",
    "ComparisonReport",
    "MeasurementConfig",
    "MergeReport",
    "ROISet",
    "compare_sets",
    "merge_sets",
]
