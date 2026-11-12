"""Tile spots: count diffraction-limited spots per tile of a tiling overview.

Runs on a stitched mosaic from the ``tiling-mosaic`` reconstructor, but
counts on the **tiles** it was assembled from: it reads the run's
``tiles.json`` again, so every count belongs to one field of view and one
commanded stage position, which is what the stage has to go back to. The
detection is the one ``spot-count`` uses (same settings, same spots), on
each tile's maximum projection when tiles carry Z planes or channels.

Two outputs, both points:

* ``tiles`` -- one row per tile, most spots first, at the stage position to
  revisit (``(y, x)`` in µm, frame ``stage:<positioner>``): the tile centre,
  or the mean position of its spots (``target``). ``selected`` marks the
  tiles with at least ``min_spots`` spots, ``rank`` orders them.
* ``spots`` -- every spot in mosaic pixels (scaled to µm, so it overlays the
  mosaic), with its tile, its stage position and its detection scores. The
  placement is the commanded layout: a mosaic refined by registration can sit
  a few pixels from it.

Spots in the overlap of two tiles are counted in both: the count is per field
of view, not per sample feature.
"""

from __future__ import annotations

from typing import Callable

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.analysis.spot_count import (
    TARGET_MODES,
    SpotParams,
    count_spots_in_tiling,
)
from imswitch.improcess.model.param_spec import ParamField
from imswitch.improcess.model.points_result import PointsResult
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors.base import OutputSpec, Processor, ProcessorOutput
from imswitch.improcess.processors.spot_count import (
    add_spot_param_rows,
    spot_default_params,
    spot_param_fields,
)

#: Columns of the tile table carried into the ``tiles`` points.
_TILE_PROPERTIES = (
    "tile", "spots", "rank", "selected", "grid_row", "grid_col",
    "tile_x_um", "tile_y_um", "target_x_um", "target_y_um",
)
#: Spot columns carried into the ``spots`` points.
_SPOT_PROPERTIES = (
    "tile_id", "y", "x", "stage_x_um", "stage_y_um",
    "net_gradient", "intensity", "contrast",
)


def tiling_manifest_of(result) -> str | None:
    """The ``tiles.json`` behind ``result``, when it is a tiling mosaic."""
    manifest = getattr(result, "manifest", None)
    if manifest:
        return str(manifest)
    provenance = getattr(result, "provenance", None)
    manifest = getattr(provenance, "manifest", None)
    return str(manifest) if manifest else None


def _mosaic_detector(result) -> str | None:
    """The detector a payload mosaic shows; ``None`` for the alignment one."""
    detector = getattr(getattr(result, "provenance", None), "detector", None)
    return detector if detector and not str(detector).startswith("__") else None


class TileSpotsProcessor(Processor):
    """Count spots per tile of a tiling run and rank the tiles to revisit."""

    name = "Tile spots"
    id = "tile-spots"
    category = "Measurement"
    run_label = "Find spots per tile"
    kinds = ("image",)
    output_kind = "points"
    #: ``detector``: one detector of a multi-detector run (default: the
    #: mosaic's own, else the one the run aligned on). For scripts.
    extra_param_keys = ("detector",)

    @classmethod
    def default_params(cls) -> dict:
        return {**spot_default_params(), "min_spots": 1, "target": TARGET_MODES[0]}

    @classmethod
    def param_spec(cls) -> tuple:
        return spot_param_fields() + (
            ParamField(
                "min_spots", "int", 1, label="Min spots", min=0, max=1_000_000,
                help="Tiles with at least this many spots are selected for revisiting",
                group="Targets",
            ),
            ParamField(
                "target", "select", TARGET_MODES[0], label="Target", options=TARGET_MODES,
                help="Where to send the stage: the mean position of the tile's "
                     "spots, or the tile's own centre",
                group="Targets",
            ),
        )

    def output_spec(self, params: dict | None = None, input_specs=None) -> OutputSpec:
        return OutputSpec(ports=("tiles", "spots"), kind=self.output_kind)

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        """A mosaic that knows the tiling run it came from."""
        return lambda result: tiling_manifest_of(result) is not None

    def check_inputs(self, results) -> tuple[bool, str]:
        results = list(results or [])
        if len(results) == 1 and tiling_manifest_of(results[0]) is None:
            return False, (
                f"{self.name} needs a tiling mosaic: open the run's tiles.json "
                f"with the tiling-mosaic reconstructor"
            )
        return super().check_inputs(results)

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)
        spot_values = add_spot_param_rows(layout)

        min_spin = QtWidgets.QSpinBox()
        min_spin.setRange(0, 1_000_000)
        min_spin.setValue(1)
        min_spin.setToolTip("Tiles with at least this many spots are selected for revisiting")
        layout.addRow("Min spots:", min_spin)

        target_combo = QtWidgets.QComboBox()
        target_combo.addItems(list(TARGET_MODES))
        target_combo.setToolTip(
            "Where to send the stage: the mean position of the tile's spots, or its centre"
        )
        layout.addRow("Target:", target_combo)

        def get_values():
            return {
                **spot_values(),
                "min_spots": int(min_spin.value()),
                "target": target_combo.currentText(),
            }

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessorOutput:
        manifest = tiling_manifest_of(result)
        if manifest is None:
            raise ValueError(
                "Tile spots needs a tiling mosaic: open the run's tiles.json with "
                "the tiling-mosaic reconstructor"
            )
        found = count_spots_in_tiling(
            manifest,
            SpotParams.from_params(params),
            detector=params.get("detector") or _mosaic_detector(result),
            min_spots=int(params.get("min_spots", 1)),
            target=str(params.get("target", TARGET_MODES[0])),
        )
        stage_frame = f"stage:{found.positioner}" if found.positioner else "stage"
        settings = {
            "operation": self.id,
            "manifest": found.manifest,
            "positioner": found.positioner,
            "axes": list(found.axes) if found.axes else None,
            "min_spots": int(params.get("min_spots", 1)),
            "target": str(params.get("target", TARGET_MODES[0])),
            "selected_tiles": int(np.count_nonzero(found.tiles["selected"])),
            "total_spots": int(len(found.spots["y"])),
        }
        tiles = PointsResult(
            f"{result.name} tiles by spot count",
            np.column_stack([found.tiles["target_y_um"], found.tiles["target_x_um"]]).reshape(-1, 2),
            frame=stage_frame,
            axis_names=["y", "x"],
            properties={key: np.asarray(found.tiles[key]) for key in _TILE_PROPERTIES},
            scale_unit="µm",
            metadata=settings,
        )
        pixel_y, pixel_x = found.pixel_size_um
        spots = PointsResult(
            f"{result.name} spots",
            np.column_stack([found.spots["mosaic_y"], found.spots["mosaic_x"]]).reshape(-1, 2),
            frame="mosaic",
            axis_names=["y", "x"],
            properties={key: np.asarray(found.spots[key]) for key in _SPOT_PROPERTIES},
            coordinate_scale=[pixel_y, pixel_x],
            scale_unit="µm",
            metadata=settings,
        )
        return ProcessorOutput((tiles, spots), keys=("tiles", "spots"))


__all__ = ["TileSpotsProcessor", "tiling_manifest_of"]


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
