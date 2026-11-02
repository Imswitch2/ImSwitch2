"""Points in the napari viewer <-> scanner positions (plan §5.5, D1).

A live point-scan layer shows its pixels at ``scale`` with no ``translate``:
a world coordinate is micrometres from the centre of the displayed image's
first pixel. The frame's :class:`~imswitch.imcontrol.model.scan_frame.
DisplayedScanGeometry` (filed on the layer with the pixels) says which scan
those pixels came from and which display transform turned them. With it, a
point drawn on the layer maps to the scanner positions under it, and back.

Pure functions over the last two image axes: rows are the second scanned
axis, columns the first (fast) axis.
"""

from __future__ import annotations

from typing import Dict, Mapping, Optional, Sequence, Tuple

from imswitch.imcontrol.model.scan_frame import AxisGeometry, DisplayedScanGeometry
from .display_transform import display_to_raw_point, raw_to_display_point


def _axes(shown: DisplayedScanGeometry) -> Tuple[AxisGeometry, Optional[AxisGeometry]]:
    axes = shown.geometry.axes
    return axes[0], (axes[1] if len(axes) > 1 else None)


def _displayScale(shown: DisplayedScanGeometry) -> Tuple[float, float]:
    """World units per displayed pixel, (row, column)."""
    column, row = _axes(shown)
    rawRow = row.step_um if row is not None else column.step_um
    rawColumn = column.step_um
    if (shown.display_transform.rotation // 90) % 2:
        return rawColumn, rawRow
    return rawRow, rawColumn


def world_to_scanner(point: Sequence[float], shown: DisplayedScanGeometry) -> Dict[str, float]:
    """Scanner positions (µm per device) under a world point [..., row, col]."""
    column, row = _axes(shown)
    rowScale, columnScale = _displayScale(shown)
    rawRow, rawColumn = display_to_raw_point(
        float(point[-2]) / rowScale, float(point[-1]) / columnScale,
        shown.raw_shape, shown.display_transform,
    )
    positions = {column.device: column.position_um(rawColumn)}
    if row is not None:
        positions[row.device] = row.position_um(rawRow)
    return positions


def scanner_to_world(positions: Mapping[str, float],
                     shown: DisplayedScanGeometry) -> Tuple[float, float]:
    """The world point [row, col] over scanner positions (µm per device)."""
    column, row = _axes(shown)
    rawColumn = (positions[column.device] - column.first_um) / column.step_um
    rawRow = 0.0
    if row is not None:
        rawRow = (positions[row.device] - row.first_um) / row.step_um
    displayRow, displayColumn = raw_to_display_point(
        rawRow, rawColumn, shown.raw_shape, shown.display_transform
    )
    rowScale, columnScale = _displayScale(shown)
    return displayRow * rowScale, displayColumn * columnScale


def rectangle_to_extents(vertices: Sequence[Sequence[float]],
                         shown: DisplayedScanGeometry) -> Dict[str, Tuple[float, float]]:
    """Per scanned device, the (low, high) positions a drawn shape spans."""
    extents: Dict[str, Tuple[float, float]] = {}
    for vertex in vertices:
        for device, position in world_to_scanner(vertex, shown).items():
            low, high = extents.get(device, (position, position))
            extents[device] = (min(low, position), max(high, position))
    return extents


def extents_to_rectangle(extents: Mapping[str, Tuple[float, float]],
                         shown: DisplayedScanGeometry) -> list:
    """The four world corners [row, col] of a region given per device.

    A shown axis missing from ``extents`` is not part of the region (an XZ
    scan drawn on an XY overview parks Y); pass it as a zero-width extent.
    """
    column, row = _axes(shown)
    columnLow, columnHigh = extents[column.device]
    if row is None:
        corners = [{column.device: columnLow}, {column.device: columnHigh}]
        points = [scanner_to_world(c, shown) for c in corners]
        (r0, c0), (r1, c1) = points
        return [[r0, c0], [r0, c1], [r1, c1], [r1, c0]]
    rowLow, rowHigh = extents[row.device]
    corners = [
        {column.device: columnLow, row.device: rowLow},
        {column.device: columnHigh, row.device: rowLow},
        {column.device: columnHigh, row.device: rowHigh},
        {column.device: columnLow, row.device: rowHigh},
    ]
    return [list(scanner_to_world(corner, shown)) for corner in corners]


__all__ = [
    'extents_to_rectangle',
    'rectangle_to_extents',
    'scanner_to_world',
    'world_to_scanner',
]
