"""Shared ROI record data contract.

A plain, dependency-light value type describing a named region of interest in
image row/column coordinates. Shared by ``improcess`` ROI/segmentation tools
and any ``imcontrol`` workflow that needs to describe regions, so it lives in
``imcommon`` rather than either app module.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ROIRecord:
    """A named ROI in image row/column coordinates."""

    name: str
    roi_type: str
    bounds: tuple[int, int, int, int]
    visible: bool = True
    source: str = "manual"
    pixels: tuple[tuple[int, int], ...] | None = None

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["bounds"] = list(self.bounds)
        if self.pixels is not None:
            data["pixels"] = [list(pixel) for pixel in self.pixels]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "ROIRecord":
        bounds = data.get("bounds", (0, 0, 0, 0))
        pixels = data.get("pixels")
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
        )


__all__ = ["ROIRecord"]
