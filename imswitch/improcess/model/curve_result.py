"""A one-dimensional result: a function sampled along one axis.

A row of numbers is not an image. Pushed into the image viewer it has nothing to
draw, and wrapped in an image result it would be offered every image filter and
refuse to save as TIFF. Its natural home is the Graph panel, which is what
``kind = "curve"`` selects (see :data:`~.result.RESULT_KINDS`).

``x`` is the sample index times the axis scale -- evenly spaced samples, which
is what a per-frame or per-plane measurement is. A curve against an irregular
axis is a table, not this.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.plotting import PlotPayload, PlotSeries
from imswitch.improcess.model.result_io import _AXIS_TYPES, save_image_result


class CurveResult(ArrayProcessingResult):
    """Values along one named axis, drawn as a line in the Graph panel."""

    kind = "curve"
    supported_formats = ("csv", "hdf5", "zarr")

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
        if array.ndim != 1:
            raise ValueError(f"a curve is one-dimensional, got {array.ndim} dimensions")
        super().__init__(
            name=name,
            data=array,
            axis_labels=list(axis_labels or ["Index"]),
            axis_scales=list(axis_scales or [1.0]),
            scale_unit=scale_unit,
            metadata=metadata,
        )

    @property
    def x_values(self) -> np.ndarray:
        """Where each sample sits along the axis: index times the axis scale."""
        return np.arange(len(np.asarray(self.data)), dtype=np.float64) * float(self.axis_scales[0])

    def x_unit(self) -> str:
        """The unit of the axis, or ``""`` when the axis has none to claim.

        A result carries one ``scale_unit`` -- the pixel size's -- for all its
        axes, but only a spatial axis is measured in it. A time axis is in
        seconds, as the OME writer assumes, and a frame, channel or plain index
        axis has no unit. A scale of 1 says the axis is not calibrated at all.
        """
        if float(self.axis_scales[0]) == 1.0:
            return ""
        axis_type = _AXIS_TYPES.get(str(self.axis_labels[0]).lower()[:1])
        if axis_type == "time":
            return "s"
        if axis_type == "space" and self.scale_unit not in ("", "px", "pixel", "pixels"):
            return str(self.scale_unit)
        return ""

    def x_label(self) -> str:
        """The axis name, with its unit when it has one."""
        label = str(self.axis_labels[0])
        unit = self.x_unit()
        return f"{label} ({unit})" if unit else label

    def plot_payloads(self) -> list[PlotPayload]:
        y = np.asarray(self.data, dtype=np.float64)
        return [
            PlotPayload(
                title=self.name,
                x_label=self.x_label(),
                y_label="",
                series=[PlotSeries(name=self.name, x=self.x_values, y=y, kind="line")],
                metadata={"samples": int(y.size)},
            )
        ]

    def plan_save(self, path: Path, fmt: str):
        from imswitch.improcess.model.save_protocol import SavePlan, companion_json_path

        path = Path(path)
        if fmt == "csv":
            return SavePlan(path, fmt, (companion_json_path(path),))
        return SavePlan(path, fmt)

    def write_files(self, plan, document) -> None:
        if plan.fmt == "csv":
            from imswitch.improcess.model.save_protocol import write_companion_json

            self._save_csv(plan.primary)
            write_companion_json(plan.primary, document)
        else:
            save_image_result(self, plan.primary, plan.fmt, document=document)

    def _save_csv(self, path: Path) -> None:
        """Two columns, the axis then the value; the header names both."""
        unit = self.x_unit()
        x_name = str(self.axis_labels[0]) + (f" [{unit}]" if unit else "")
        values = np.asarray(self.data)
        if values.dtype.kind == "b":
            values = values.astype(np.uint8)
        # Each value is written as the shortest text that reads back as the same
        # number *of its own type*: a float32 curve says 1.0358881, not the
        # eighteen digits of exponent notation ``savetxt`` gives every number.
        # The axis is a calibration, so twelve digits is more than it can mean.
        lines = [f"{_csv_field(x_name)},{_csv_field(self.name)}"]
        # ``!s``: str() keeps a float32's own digits where format() widens it to a double.
        lines += [f"{x:.12g},{value!s}" for x, value in zip(self.x_values, values)]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _csv_field(text: str) -> str:
    """``text`` as one CSV header field: quoted when a comma or quote would split it."""
    if any(char in text for char in ',"\n'):
        return '"' + text.replace('"', '""') + '"'
    return text


__all__ = ["CurveResult"]
