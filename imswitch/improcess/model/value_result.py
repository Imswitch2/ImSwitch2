"""One number, and one yes/no: the smallest things a workflow can produce.

Until now a step's output was an array of some shape; a Python step that
computed a single number was refused, and a one-element array became a
curve. A smart pipeline is made of such small things -- a focus score, a
"was something detected" -- so they get result kinds of their own
(``"value"`` and ``"event"``, see :data:`~.result.RESULT_KINDS`). Both are
one-row tables to the Results dock and save as CSV with a companion JSON.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import numpy as np

from imswitch.improcess.model.result import ProcessingResult, ViewMode


class _RowResult(ProcessingResult):
    """A result that is one record. ``data`` holds its numeric face."""

    publishes_table_rows = True
    supported_formats = ("csv",)

    def __init__(self, name: str, data: np.ndarray, metadata: dict[str, Any] | None = None):
        super().__init__(
            name=name,
            data=data,
            axis_labels=["Column"],
            view_modes=[ViewMode("Table", (0,))],
            display_levels=None,
        )
        self.metadata = dict(metadata or {})

    def plan_save(self, path: Path, fmt: str):
        from imswitch.improcess.model.save_protocol import SavePlan, companion_json_path

        path = Path(path)
        if fmt != "csv":
            raise ValueError(f"{type(self).__name__} saves as CSV, got {fmt!r}")
        return SavePlan(path, fmt, (companion_json_path(path),))

    def write_files(self, plan, document) -> None:
        from imswitch.improcess.model.save_protocol import write_companion_json

        columns = self.table_columns()
        with open(plan.primary, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for record in self.table_records():
                writer.writerow({k: _cell(v) for k, v in record.items()})
        write_companion_json(plan.primary, document)


class ValueResult(_RowResult):
    """A single number with an optional unit."""

    kind = "value"

    def __init__(self, name: str, value: float, *, unit: str = "",
                 metadata: dict[str, Any] | None = None):
        number = float(np.asarray(value).reshape(-1)[0]) if np.asarray(value).size == 1 else None
        if number is None:
            raise ValueError(f"a value is a single number, got shape {np.asarray(value).shape}")
        super().__init__(name, np.asarray([number], dtype=np.float64), metadata)
        self.unit = str(unit or "")

    @property
    def value(self) -> float:
        return float(np.asarray(self.data)[0])

    def table_columns(self) -> list[str]:
        return ["value", "unit"]

    def table_records(self) -> list[dict[str, Any]]:
        return [{"value": self.value, "unit": self.unit}]

    def __repr__(self) -> str:
        unit = f" {self.unit}" if self.unit else ""
        return f"ValueResult({self.name!r}, {self.value:g}{unit})"


class EventResult(_RowResult):
    """Whether something happened, and what: ``fired`` plus a payload."""

    kind = "event"

    def __init__(self, name: str, fired: bool, *, payload: dict[str, Any] | None = None,
                 metadata: dict[str, Any] | None = None):
        super().__init__(name, np.asarray([1 if fired else 0], dtype=np.int64), metadata)
        self.fired = bool(fired)
        self.payload = dict(payload or {})

    def __bool__(self) -> bool:
        return self.fired

    def table_columns(self) -> list[str]:
        return ["fired", *self.payload.keys()]

    def table_records(self) -> list[dict[str, Any]]:
        return [{"fired": self.fired, **self.payload}]

    def __repr__(self) -> str:
        return f"EventResult({self.name!r}, fired={self.fired}, payload={self.payload})"


def _cell(value):
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value)
    if hasattr(value, "item"):
        return value.item()
    return value


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
