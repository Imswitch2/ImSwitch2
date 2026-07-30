"""Sidecar files that make a saved set of tiles a reconstructable mosaic.

Each tile is written as an ordinary OME image carrying its own stage position
in ``Plane/@PositionX|Y`` — that is OME's standard answer to "where on the
sample was this taken", and any OME-aware reader can use it.

Two sidecars are written alongside them:

``TileConfiguration.txt``
    The layout format used by Fiji's Grid/Collection Stitching plugin and by
    BigStitcher. Not an OME standard, but the de-facto one for handing a tile
    set to a stitcher, and the fastest route from a saved run to a stitched
    result in software far better at it than a live preview.

``tiles.json``
    Everything the other two cannot express: the grid index of each tile, the
    registration correction that was applied, and the acquisition settings the
    mosaic geometry depends on.

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
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TILE_CONFIG_NAME = 'TileConfiguration.txt'
MANIFEST_NAME = 'tiles.json'


@dataclass
class TileRecord:
    """One saved tile: where it came from and where it ended up."""

    filename: str
    #: Spiral grid index, in stage axes.
    grid: Tuple[int, int]
    #: Commanded stage position, in µm.
    stage_um: Tuple[float, float]
    #: Top-left corner in the mosaic, in pixels (post-registration).
    pixel_xy: Tuple[float, float]
    #: Registration correction applied to this tile, in pixels ``(dy, dx)``.
    correction_px: Tuple[float, float] = (0.0, 0.0)


@dataclass
class TileDataset:
    """Accumulates tile records and writes the sidecars."""

    pixel_size_um: Tuple[float, float] = (1.0, 1.0)   # (y, x)
    step_um: float = 0.0
    tile_shape_px: Tuple[int, int] = (0, 0)           # (h, w)
    orientation: Tuple[bool, bool, bool] = (False, False, False)
    tiles: List[TileRecord] = field(default_factory=list)
    extra: Dict = field(default_factory=dict)

    def add(self, record: TileRecord) -> None:
        self.tiles.append(record)

    def write(self, folder: Path) -> List[Path]:
        """Write both sidecars into ``folder``; returns the paths written."""
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        written = []
        for writer in (self._write_tile_configuration, self._write_manifest):
            try:
                written.append(writer(folder))
            except Exception as exc:
                logger.error('Failed to write tiling sidecar: %s', exc)
        return [path for path in written if path is not None]

    def _write_tile_configuration(self, folder: Path) -> Path:
        """Write Fiji/BigStitcher ``TileConfiguration.txt``.

        Positions are in pixels, which is what the Fiji stitcher expects, and
        are the mosaic positions actually used — so if registration moved a
        tile, the stitcher starts from the corrected layout rather than the
        commanded one.
        """
        path = folder / TILE_CONFIG_NAME
        lines = [
            '# Define the number of dimensions we are working on',
            'dim = 2',
            '',
            '# Define the image coordinates',
        ]
        for tile in self.tiles:
            x, y = tile.pixel_xy
            lines.append(f'{tile.filename}; ; ({x:.3f}, {y:.3f})')
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        return path

    def _write_manifest(self, folder: Path) -> Path:
        path = folder / MANIFEST_NAME
        flip_x, flip_y, swap_axes = self.orientation
        payload = {
            'format': 'imswitch-tiling/1',
            'pixel_size_um': {
                'y': float(self.pixel_size_um[0]),
                'x': float(self.pixel_size_um[1]),
            },
            'tile_step_um': float(self.step_um),
            'tile_shape_px': {
                'height': int(self.tile_shape_px[0]),
                'width': int(self.tile_shape_px[1]),
            },
            'orientation': {
                'flip_x': bool(flip_x),
                'flip_y': bool(flip_y),
                'swap_axes': bool(swap_axes),
            },
            'tiles': [asdict(tile) for tile in self.tiles],
        }
        payload.update(self.extra)
        path.write_text(json.dumps(payload, indent=2), encoding='utf-8')
        return path


def default_tiling_folder(root: Optional[Path] = None) -> Path:
    """``<root>/<YYYY_MM_DD>/tiling_<HHMMSS>`` — the house tiling convention."""
    from datetime import datetime

    from imswitch.imcontrol.model.workflows.paths import resolve_measurements_root

    base = resolve_measurements_root(root)
    now = datetime.now()
    return Path(base) / now.strftime('%Y_%m_%d') / f'tiling_{now.strftime("%H%M%S")}'
