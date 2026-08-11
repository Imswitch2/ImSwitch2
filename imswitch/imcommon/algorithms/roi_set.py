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


__all__ = ["MeasurementConfig", "ROISet"]
