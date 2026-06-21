"""Target list model for tiled target workflows.

Provides Target and TargetList classes for managing stage positions from
segmentation, manual selection, or imported sources. Supports JSON serialization
for persistence and conversion from segmentation results via StitchedImage
pixel-to-stage mapping.

Copyright (C) 2020-2026 ImSwitch developers
This file is part of ImSwitch.

ImSwitch is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

ImSwitch is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

import numpy as np


def _json_value(value: Any) -> Any:
    """Return a JSON-compatible representation of values from numpy/skimage."""
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return [_json_value(item) for item in value.tolist()]
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


@dataclass
class Target:
    """Single target position for tiled target workflows.

    Attributes:
        id: Unique stable identifier (int).
        stage_xy: Stage position in um as ``(x, y)``.
        source: Origin of this target: ``"manual"``, ``"segmentation"``, or ``"imported"``.
        enabled: Whether this target is active for acquisition.
        row_col: Optional overview pixel coordinates as ``(row, col)``.
        z_um: Optional Z position in um.
        props: Optional dict of segmentation or manual metadata.
    """

    id: int
    stage_xy: Tuple[float, float]
    source: Literal["manual", "segmentation", "imported"]
    enabled: bool = True
    row_col: Optional[Tuple[int, int]] = None
    z_um: Optional[float] = None
    props: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "id": int(self.id),
            "stage_xy": [float(self.stage_xy[0]), float(self.stage_xy[1])],
            "source": self.source,
            "enabled": bool(self.enabled),
            "row_col": [int(self.row_col[0]), int(self.row_col[1])] if self.row_col is not None else None,
            "z_um": float(self.z_um) if self.z_um is not None else None,
            "props": _json_value(self.props),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Target:
        """Construct from JSON-deserialized dict."""
        stage_xy = data["stage_xy"]
        row_col = data.get("row_col")
        return cls(
            id=int(data["id"]),
            stage_xy=(float(stage_xy[0]), float(stage_xy[1])),
            source=data["source"],
            enabled=bool(data.get("enabled", True)),
            row_col=(int(row_col[0]), int(row_col[1])) if row_col is not None else None,
            z_um=float(data["z_um"]) if data.get("z_um") is not None else None,
            props=dict(data.get("props", {})),
        )


class TargetList:
    """Container for target positions with enable/disable and JSON persistence.

    Maintains stable ordering and IDs for targets across acquisition sessions.

    Attributes:
        targets: List of Target instances in acquisition order.
    """

    def __init__(self, targets: Optional[List[Target]] = None) -> None:
        """Initialize with optional target list."""
        self.targets: List[Target] = targets if targets is not None else []

    def add(
        self,
        stage_xy: Tuple[float, float],
        source: Literal["manual", "segmentation", "imported"],
        enabled: bool = True,
        row_col: Optional[Tuple[int, int]] = None,
        z_um: Optional[float] = None,
        props: Optional[Dict[str, Any]] = None,
    ) -> Target:
        """Add a new target and return it.

        Args:
            stage_xy: Stage position in um as ``(x, y)``.
            source: Origin: ``"manual"``, ``"segmentation"``, or ``"imported"``.
            enabled: Whether this target is active.
            row_col: Optional overview pixel coordinates as ``(row, col)``.
            z_um: Optional Z position in um.
            props: Optional dict of metadata.

        Returns:
            The created Target instance.
        """
        new_id = self._next_id()
        target = Target(
            id=new_id,
            stage_xy=stage_xy,
            source=source,
            enabled=enabled,
            row_col=row_col,
            z_um=z_um,
            props=props if props is not None else {},
        )
        self.targets.append(target)
        return target

    def enabled_targets(self) -> List[Target]:
        """Return list of enabled targets in order."""
        return [t for t in self.targets if t.enabled]

    def _next_id(self) -> int:
        """Generate next stable ID."""
        if not self.targets:
            return 1
        return max(t.id for t in self.targets) + 1

    def to_dict(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "version": 1,
            "targets": [t.to_dict() for t in self.targets],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TargetList:
        """Construct from JSON-deserialized dict."""
        version = data.get("version", 1)
        if version != 1:
            raise ValueError(f"Unsupported TargetList version: {version}")

        targets = [Target.from_dict(t) for t in data.get("targets", [])]
        return cls(targets=targets)

    def save_json(self, path: Path | str) -> None:
        """Save to JSON file.

        Args:
            path: Output JSON file path.
        """
        path = Path(path)
        with path.open("w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def load_json(cls, path: Path | str) -> TargetList:
        """Load from JSON file.

        Args:
            path: Input JSON file path.

        Returns:
            TargetList instance.
        """
        path = Path(path)
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)

    @classmethod
    def from_segmentation(
        cls,
        segmentation_result,
        stitched_image,
        canvas_origin_stage: Tuple[float, float],
    ) -> TargetList:
        """Convert segmentation output to TargetList.

        Args:
            segmentation_result: CellTargetResult from ``detect_cell_targets(...)``.
            stitched_image: StitchedImage instance for pixel-to-stage conversion.
            canvas_origin_stage: Stage position ``(x, y)`` of overview top-left corner.

        Returns:
            TargetList with targets from valid (filtered) segmentation results.
        """
        target_list = cls()

        if segmentation_result.n_valid == 0:
            return target_list

        filtered = segmentation_result.filtered_props

        for i in range(segmentation_result.n_valid):
            row = int(filtered["centroid_row"][i])
            col = int(filtered["centroid_col"][i])

            stage_x, stage_y = stitched_image.pixel_to_stage(row, col, canvas_origin_stage)

            props = {}
            for key, values in filtered.items():
                try:
                    value = values[i]
                except (IndexError, TypeError):
                    value = values
                props[key] = _json_value(value)

            target_list.add(
                stage_xy=(stage_x, stage_y),
                source="segmentation",
                enabled=True,
                row_col=(row, col),
                z_um=None,
                props=props,
            )

        return target_list

    def __len__(self) -> int:
        """Return total number of targets."""
        return len(self.targets)

    def __getitem__(self, idx: int) -> Target:
        """Get target by index."""
        return self.targets[idx]

    def __iter__(self):
        """Iterate over all targets."""
        return iter(self.targets)
