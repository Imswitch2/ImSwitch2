"""Promote a table of points to a localization result, explicitly.

A Points layer taken back from a napari plugin lands as a table
(:class:`~imswitch.improcess.model.points_table_result.PointsTableResult`)
because nothing says its points are emitters. This is the one place that
promotion happens, and it asks for what the localization schema needs: which
columns are ``x`` and ``y`` (and optionally ``z``, ``frame``, ``photons``,
``sigma``), what unit they are in, and the pixel size that relates them to
the camera grid. Anything not mapped is left out rather than guessed.

Units are per axis, not one number for everything:

* ``px`` -- lateral columns are camera pixels (``pixel_size_nm`` each), a
  ``z`` column is in Z steps (``z_step_nm`` each, and required);
* ``nm`` / ``um`` -- every mapped column is already physical;
* ``table`` -- the table's own per-axis ``coordinate_scale`` and
  ``scale_unit`` (what an imported napari Points layer carries) convert each
  axis column; the rule for ``px`` applies when that unit is pixels.
"""

from __future__ import annotations

from typing import Callable

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors.base import Processor

UNITS = ("px", "nm", "um", "table")

_TO_NM = {"nm": 1.0, "um": 1000.0, "µm": 1000.0, "micrometer": 1000.0, "micron": 1000.0,
          "mm": 1e6, "millimeter": 1e6, "m": 1e9, "meter": 1e9}


class TableToLocalizationsProcessor(Processor):
    """Table (points + properties) -> LocalizationResult with a column mapping."""

    name = "Table to localizations"
    id = "table-to-localizations"
    category = "SMLM"
    kinds = ("table",)
    preserves_grid = False

    @classmethod
    def default_params(cls) -> dict:
        return {
            "x_column": "x",
            "y_column": "y",
            "z_column": "",
            "frame_column": "",
            "photons_column": "",
            "sigma_column": "",
            "unit": "px",
            "pixel_size_nm": 100.0,
            "z_step_nm": 0.0,
        }

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: callable(getattr(result, "table_columns", None)) and bool(result.table_columns())

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)
        edits = {}
        for key, label, default in (
            ("x_column", "X column:", "x"), ("y_column", "Y column:", "y"),
            ("z_column", "Z column (optional):", ""), ("frame_column", "Frame column (optional):", ""),
            ("photons_column", "Photons column (optional):", ""), ("sigma_column", "Sigma column (optional):", ""),
        ):
            edit = QtWidgets.QLineEdit(default)
            layout.addRow(label, edit)
            edits[key] = edit
        unit_combo = QtWidgets.QComboBox()
        unit_combo.addItems(list(UNITS))
        layout.addRow("Coordinate unit:", unit_combo)
        pixel_spin = QtWidgets.QDoubleSpinBox()
        pixel_spin.setRange(0.001, 100000.0)
        pixel_spin.setDecimals(3)
        pixel_spin.setValue(100.0)
        layout.addRow("Pixel size (nm):", pixel_spin)
        z_spin = QtWidgets.QDoubleSpinBox()
        z_spin.setRange(0.0, 100000.0)
        z_spin.setDecimals(3)
        z_spin.setValue(0.0)
        z_spin.setSpecialValueText("unknown")
        layout.addRow("Z step (nm):", z_spin)

        def get_values():
            values = {key: edit.text().strip() for key, edit in edits.items()}
            values.update({
                "unit": unit_combo.currentText(),
                "pixel_size_nm": float(pixel_spin.value()),
                "z_step_nm": float(z_spin.value()),
            })
            return values

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        from imswitch.improcess.model.localization_result import LocalizationResult
        from imswitch.improcess.model.localization_schema import localizations_from_columns

        unit = str(params.get("unit", "px"))
        if unit not in UNITS:
            raise ValueError(f"unit must be one of {UNITS}, got {unit!r}")
        pixel_size_nm = float(params.get("pixel_size_nm", 100.0))
        if pixel_size_nm <= 0:
            raise ValueError("pixel_size_nm must be positive")
        z_step = float(params.get("z_step_nm", 0.0) or 0.0)

        records = result.table_records()
        columns = result.table_columns()
        if not records:
            raise ValueError("the table has no rows")
        axis_names = list(getattr(result, "axis_names", []) or [])
        axis_scale = list(getattr(result, "coordinate_scale", []) or [])
        table_unit = str(getattr(result, "scale_unit", "px") or "px")
        if unit == "table" and (not axis_names or len(axis_scale) != len(axis_names)):
            raise ValueError("unit 'table' needs a table with per-axis coordinate_scale (an imported Points layer)")

        def column(name: str, required: bool = False):
            name = str(name or "").strip()
            if not name:
                if required:
                    raise ValueError("x_column and y_column are required")
                return None
            if name not in columns:
                raise ValueError(f"column {name!r} not in the table (columns: {columns})")
            return name, np.asarray([row[name] for row in records], dtype=np.float64)

        def to_nm(name: str, values, *, axial: bool):
            """``values`` of column ``name`` in nanometres, per that axis' unit."""
            if unit == "table":
                if name in axis_names:
                    scale, base_unit = float(axis_scale[axis_names.index(name)]), table_unit
                else:
                    # A property column (not a coordinate axis) has no scale
                    # of its own; it is read in the table's unit.
                    scale, base_unit = 1.0, table_unit
            else:
                scale, base_unit = 1.0, unit
            if base_unit in _TO_NM:
                return values * scale * _TO_NM[base_unit]
            if base_unit in ("px", "pixel", "pixels"):
                if axial:
                    if z_step <= 0:
                        raise ValueError(
                            f"column {name!r} is in Z steps; z_step_nm must be positive to convert it"
                        )
                    return values * scale * z_step
                return values * scale * pixel_size_nm
            raise ValueError(f"cannot convert unit {base_unit!r} of column {name!r} to nanometres")

        x_name, x_raw = column(params.get("x_column"), required=True)
        y_name, y_raw = column(params.get("y_column"), required=True)
        payload = {
            "x_nm": to_nm(x_name, x_raw, axial=False),
            "y_nm": to_nm(y_name, y_raw, axial=False),
        }
        frame = column(params.get("frame_column"))
        payload["frame"] = (frame[1] if frame is not None else np.zeros(len(x_raw))).astype(np.int64)
        z = column(params.get("z_column"))
        if z is not None:
            payload["z_nm"] = to_nm(z[0], z[1], axial=True)
        photons = column(params.get("photons_column"))
        if photons is not None:
            payload["photons"] = photons[1]
        sigma = column(params.get("sigma_column"))
        if sigma is not None:
            sigma_nm = to_nm(sigma[0], sigma[1], axial=False)
            payload["sigma_x_nm"] = sigma_nm
            payload["sigma_y_nm"] = sigma_nm

        locs = localizations_from_columns(payload)
        return LocalizationResult(
            f"{result.name} (localizations)", locs,
            pixel_size_nm=pixel_size_nm,
            z_step_nm=z_step if z_step > 0 else None,
            dims="3D" if z is not None else "2D",
            source_name=getattr(result, "name", None),
            metadata={"promoted_from": "table", "column_mapping": {
                k: params.get(k) for k in ("x_column", "y_column", "z_column", "frame_column",
                                            "photons_column", "sigma_column")
            }, "coordinate_unit": unit,
               "table_coordinate_scale": axis_scale if unit == "table" else None,
               "table_scale_unit": table_unit if unit == "table" else None},
        )


__all__ = ["TableToLocalizationsProcessor", "UNITS"]


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
