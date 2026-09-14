"""How an ROI is drawn.

Kept separate from the ROI itself so appearance can be shared: a set carries a
default style and a record only overrides it when it actually differs, which
means the common case costs nothing to store or serialise.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ROIStyle:
    """Stroke, fill and label appearance for an ROI.

    Every field is optional; ``None`` means "inherit", so a record with no
    overrides serialises to nothing at all.
    """

    stroke_color: str | None = None
    #: Screen pixels. None follows the overlay's zoom-compensated default,
    #: which keeps outlines readable at any zoom.
    stroke_width: float | None = None
    fill_color: str | None = None
    fill_opacity: float = 0.0
    label_color: str | None = None
    label_visible: bool = True

    def merged_with(self, base: "ROIStyle | None") -> "ROIStyle":
        """This style over ``base``, field by field."""
        if base is None:
            return self
        return ROIStyle(
            stroke_color=self.stroke_color or base.stroke_color,
            stroke_width=self.stroke_width if self.stroke_width is not None else base.stroke_width,
            fill_color=self.fill_color or base.fill_color,
            fill_opacity=self.fill_opacity or base.fill_opacity,
            label_color=self.label_color or base.label_color,
            label_visible=self.label_visible and base.label_visible,
        )

    def to_json(self) -> dict:
        return {
            key: value
            for key, value in {
                "stroke_color": self.stroke_color,
                "stroke_width": self.stroke_width,
                "fill_color": self.fill_color,
                "fill_opacity": self.fill_opacity or None,
                "label_color": self.label_color,
                "label_visible": None if self.label_visible else False,
            }.items()
            if value is not None
        }

    @classmethod
    def from_json(cls, payload: dict | None) -> "ROIStyle | None":
        if not payload:
            return None
        return cls(
            stroke_color=payload.get("stroke_color"),
            stroke_width=payload.get("stroke_width"),
            fill_color=payload.get("fill_color"),
            fill_opacity=float(payload.get("fill_opacity", 0.0) or 0.0),
            label_color=payload.get("label_color"),
            label_visible=bool(payload.get("label_visible", True)),
        )


__all__ = ["ROIStyle"]
