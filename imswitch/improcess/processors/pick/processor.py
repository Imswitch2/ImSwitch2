"""Points -> points: the one row a rule chooses.

The deciding step of a smart pipeline: of every region ``regionprops``
measured, which one does the stage move to? ``largest`` (by ``area``),
``brightest`` (by ``mean_intensity``, ties by ``max_intensity``), ``first``
(row order) or ``nearest`` (to a typed ``(x, y)``). The chosen row keeps its
frame, its source frame index and every property column, so what reaches a
transform or a move step is the same claim the measurement made. No points
in gives no points out -- an empty result in the same frame -- rather than
an error, because "nothing found on this frame" is a normal outcome a
pipeline has to carry, not a failure.
"""

from __future__ import annotations

from typing import Callable

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.model.param_spec import ParamField
from imswitch.improcess.model.points_result import UNKNOWN_FRAME, PointsResult
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors.base import Processor

PICK_RULES = ("largest", "brightest", "first", "nearest")


class PickProcessor(Processor):
    """Keep the one point a rule selects (or none, when there are none)."""

    name = "Pick point"
    id = "pick"
    category = "Measurement"
    kinds = ("points",)
    output_kind = "points"

    @classmethod
    def default_params(cls) -> dict:
        return {"rule": "largest", "x": 0.0, "y": 0.0}

    @classmethod
    def param_spec(cls) -> tuple:
        return (
            ParamField(
                "rule", "select", "largest", label="Rule", options=PICK_RULES,
                help=(
                    "largest by area; brightest by mean_intensity (then max_intensity); "
                    "first in row order; nearest to (x, y)"
                ),
            ),
            ParamField(
                "x", "float", 0.0, label="X", decimals=3,
                help="Column coordinate the nearest rule measures distance to",
            ),
            ParamField(
                "y", "float", 0.0, label="Y", decimals=3,
                help="Row coordinate the nearest rule measures distance to",
            ),
        )

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        """Any points result: the table is (N, D) by construction."""
        return lambda result: True

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        rule_combo = QtWidgets.QComboBox()
        rule_combo.addItems(list(PICK_RULES))
        rule_combo.setToolTip(
            "largest by area; brightest by mean_intensity (then max_intensity); "
            "first in row order; nearest to (x, y)"
        )
        layout.addRow("Rule:", rule_combo)

        x_spin = QtWidgets.QDoubleSpinBox()
        x_spin.setDecimals(3)
        x_spin.setRange(-1e12, 1e12)
        x_spin.setValue(0.0)
        x_spin.setToolTip("Column coordinate the nearest rule measures distance to")
        layout.addRow("X:", x_spin)

        y_spin = QtWidgets.QDoubleSpinBox()
        y_spin.setDecimals(3)
        y_spin.setRange(-1e12, 1e12)
        y_spin.setValue(0.0)
        y_spin.setToolTip("Row coordinate the nearest rule measures distance to")
        layout.addRow("Y:", y_spin)

        def get_values():
            return {
                "rule": rule_combo.currentText(),
                "x": float(x_spin.value()),
                "y": float(y_spin.value()),
            }

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        rule = str(params.get("rule", "largest"))
        if rule not in PICK_RULES:
            raise ValueError(f"Unknown pick rule {rule!r}; expected one of {list(PICK_RULES)}")
        coordinates = np.asarray(getattr(result, "coordinates", result.data), dtype=np.float64)
        if coordinates.ndim != 2:
            raise ValueError(f"Pick needs (N, D) points, got shape {coordinates.shape}")
        properties = {
            str(key): np.asarray(column)
            for key, column in (getattr(result, "properties", None) or {}).items()
        }
        frame = str(getattr(result, "frame", None) or UNKNOWN_FRAME)
        frame_index = getattr(result, "frame_index", None)
        axis_names = list(getattr(result, "axis_names", None) or []) or None
        coordinate_scale = list(getattr(result, "coordinate_scale", None) or []) or None
        source_meta = getattr(result, "metadata", None) or {}
        metadata = {
            k: v for k, v in source_meta.items() if k not in ("frame", "frame_index")
        }
        metadata["pick_rule"] = rule

        if coordinates.shape[0] == 0:
            chosen = np.zeros((0, coordinates.shape[1]), dtype=np.float64)
            kept = {key: column[:0] for key, column in properties.items()}
        else:
            index = pick_index(
                coordinates, properties, rule,
                x=float(params.get("x", 0.0)), y=float(params.get("y", 0.0)),
            )
            chosen = coordinates[[index]]
            kept = {key: column[[index]] for key, column in properties.items()}
            metadata["picked_row"] = int(index)

        return PointsResult(
            f"{result.name} (pick {rule})",
            chosen,
            frame=frame,
            frame_index=frame_index,
            axis_names=axis_names,
            properties=kept,
            coordinate_scale=coordinate_scale,
            scale_unit=getattr(result, "scale_unit", "px"),
            metadata=metadata,
        )


def pick_index(
    coordinates: np.ndarray,
    properties: dict[str, np.ndarray],
    rule: str,
    *,
    x: float = 0.0,
    y: float = 0.0,
) -> int:
    """Row index ``rule`` selects among ``coordinates`` (at least one row).

    Raises ``ValueError`` naming the property column a rule needs and the
    input lacks, so a pipeline wired without an intensity image fails on
    the word ``mean_intensity`` rather than on a KeyError.
    """
    coordinates = np.asarray(coordinates, dtype=np.float64)
    if coordinates.shape[0] == 0:
        raise ValueError("Cannot pick from zero points")

    def column(name: str) -> np.ndarray:
        if name not in properties:
            raise ValueError(
                f"Pick rule {rule!r} needs the {name!r} property, which the points lack "
                f"(available: {sorted(properties) or 'none'})"
            )
        return np.asarray(properties[name], dtype=np.float64)

    if rule == "first":
        return 0
    if rule == "largest":
        return int(np.argmax(column("area")))
    if rule == "brightest":
        mean = column("mean_intensity")
        if "max_intensity" in properties:
            peak = np.asarray(properties["max_intensity"], dtype=np.float64)
            # lexsort orders by the last key first: mean, then max as the tie-break;
            # descending, so the brightest row sorts first.
            order = np.lexsort((-peak, -mean))
            return int(order[0])
        return int(np.argmax(mean))
    if rule == "nearest":
        # The last two coordinate columns are (y, x), as in napari, whatever
        # leads them.
        delta = coordinates[:, -2:] - np.array([float(y), float(x)])
        return int(np.argmin(np.einsum("ij,ij->i", delta, delta)))
    raise ValueError(f"Unknown pick rule {rule!r}; expected one of {list(PICK_RULES)}")


__all__ = ["PICK_RULES", "PickProcessor", "pick_index"]


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
