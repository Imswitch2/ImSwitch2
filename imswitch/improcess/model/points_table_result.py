"""A table of points with per-point properties.

A Points layer taken back from a napari plugin is a set of coordinates and
whatever columns the plugin attached. It is **not** a localization result:
nothing says the points are emitters, that there is a frame column, photon
counts, or a fit uncertainty, and a ``LocalizationResult`` that lacked them
would still be offered every SMLM processor. So the points land as a plain
table -- ``kind = "table"`` -- and become localizations only through an
explicit conversion that asks for the column mapping.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from imswitch.improcess.model.result import ProcessingResult, ViewMode


class PointsTableResult(ProcessingResult):
    """``(N, D)`` coordinates plus named property columns, one row per point."""

    kind = "table"

    def __init__(
        self,
        name: str,
        coordinates: np.ndarray | Any,
        *,
        axis_names: list[str] | None = None,
        properties: dict[str, Any] | None = None,
        coordinate_scale: list[float] | None = None,
        scale_unit: str = "px",
        metadata: dict[str, Any] | None = None,
    ):
        coords = np.asarray(coordinates, dtype=np.float64)
        if coords.ndim != 2:
            raise ValueError(f"coordinates must be (N, D), got shape {coords.shape}")
        count, ndim = coords.shape
        self.axis_names = list(axis_names or _default_axis_names(ndim))
        if len(self.axis_names) != ndim:
            raise ValueError("axis_names must name every coordinate column")
        self.properties: dict[str, np.ndarray] = {}
        for key, values in (properties or {}).items():
            column = np.asarray(values)
            if column.ndim != 1 or len(column) != count:
                raise ValueError(f"property {key!r} must have one value per point")
            self.properties[str(key)] = column
        self.coordinate_scale = list(coordinate_scale or [1.0] * ndim)
        numeric = [
            np.asarray(column, dtype=np.float64)
            for column in self.properties.values()
            if np.asarray(column).dtype.kind in "biuf"
        ]
        data = np.column_stack([coords, *numeric]) if numeric else coords
        super().__init__(
            name=name,
            data=data,
            axis_labels=["Point", "Column"],
            view_modes=[ViewMode("Table", (0, 1))],
            display_levels=None,
            scale_unit=scale_unit,
        )
        self.metadata = dict(metadata or {})

    @property
    def count(self) -> int:
        return int(np.asarray(self.data).shape[0])

    @property
    def coordinates(self) -> np.ndarray:
        return np.asarray(self.data)[:, : len(self.axis_names)]

    def table_columns(self) -> list[str]:
        return [*self.axis_names, *self.properties.keys()]

    def table_records(self) -> list[dict[str, Any]]:
        coords = self.coordinates
        records = []
        for index in range(self.count):
            row = {name: float(coords[index, axis]) for axis, name in enumerate(self.axis_names)}
            for key, column in self.properties.items():
                value = column[index]
                row[key] = value.item() if hasattr(value, "item") else value
            records.append(row)
        return records

    def save(self, path: Path, fmt: str = "csv") -> None:
        path = Path(path)
        if fmt in ("csv", "txt"):
            self._save_csv(path)
        elif fmt in ("hdf5", "h5", "hdf"):
            self._save_hdf5(path)
        else:
            raise ValueError(f"points table supports CSV or HDF5, got {fmt!r}")

    def _save_csv(self, path: Path) -> None:
        import csv

        columns = self.table_columns()
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for record in self.table_records():
                writer.writerow(record)

    def _save_hdf5(self, path: Path) -> None:
        import h5py

        from imswitch.improcess.model.footprint import json_safe

        with h5py.File(str(path), "w") as handle:
            handle.create_dataset("coordinates", data=self.coordinates)
            group = handle.create_group("properties")
            for key, column in self.properties.items():
                array = np.asarray(column)
                if array.dtype.kind in "OU":
                    array = array.astype("S")
                group.create_dataset(key, data=array)
            handle.attrs["axis_names"] = json.dumps(self.axis_names)
            handle.attrs["coordinate_scale"] = json.dumps(self.coordinate_scale)
            handle.attrs["scale_unit"] = str(self.scale_unit)
            handle.attrs["metadata"] = json.dumps(json_safe(self.metadata), ensure_ascii=False)


def _default_axis_names(ndim: int) -> list[str]:
    names = ["z", "y", "x"]
    return names[-ndim:] if ndim <= 3 else [f"d{i}" for i in range(ndim)]


__all__ = ["PointsTableResult"]


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
