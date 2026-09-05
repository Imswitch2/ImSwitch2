"""A generic label image result.

:class:`~.roi_mask_result.ROIMaskResult` is a label image *with ROI names*,
one label per ROI. A label image that came from somewhere else -- a napari
plugin's connected components, a threshold -- has no ROI names to carry, and
wrapping it in an image result would offer it every intensity filter in the
toolbar. This is the plain typed home for such data: ``kind = "labels"``,
integer values, written through the shared image writer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.model.result_io import save_image_result


class LabelsResult(ProcessingResult):
    """An integer label image; value ``n`` is object ``n``, 0 is background."""

    kind = "labels"

    def __init__(
        self,
        name: str,
        data: np.ndarray | Any,
        axis_labels: list[str] | None = None,
        *,
        axis_scales: list[float] | None = None,
        scale_unit: str = "px",
        metadata: dict[str, Any] | None = None,
    ):
        array = np.asarray(data)
        if array.dtype.kind not in "iub":
            raise TypeError(f"labels must be integer-valued, got dtype {array.dtype}")
        if array.dtype.kind == "b":
            array = array.astype(np.uint8)
        top = int(array.max()) if array.size else 0
        super().__init__(
            name=name,
            data=array,
            axis_labels=list(axis_labels or _default_labels(array.ndim)),
            axis_scales=list(axis_scales or [1.0] * array.ndim),
            scale_unit=scale_unit,
            display_levels=(0.0, float(max(1, top))),
        )
        self.metadata = dict(metadata or {})

    @property
    def label_count(self) -> int:
        data = np.asarray(self.data)
        return int(len(np.unique(data[data != 0]))) if data.size else 0

    def save(self, path: Path, fmt: str = "tiff") -> None:
        save_image_result(self, path, fmt, extra={"labels": True})


def _default_labels(ndim: int) -> list[str]:
    return ["Z", "Y", "X"][-ndim:] if ndim <= 3 else [f"D{i}" for i in range(ndim - 2)] + ["Y", "X"]


__all__ = ["LabelsResult"]


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
