"""Result of the MoNaLISA lattice reconstructor."""

from __future__ import annotations

from typing import Any

import numpy as np

from imswitch.improcess.model.result import DisplayLayerSpec, ProcessingResult


def _levels(data: np.ndarray) -> tuple[float, float]:
    """Display range from the 1st to the 99.9th percentile of a sample."""
    flat = np.asarray(data).ravel()
    if flat.size > 2_000_000:
        flat = flat[:: flat.size // 2_000_000 + 1]
    flat = flat[np.isfinite(flat)]
    if flat.size == 0:
        return 0.0, 1.0
    low, high = np.percentile(flat, [1.0, 99.9])
    if high <= low:
        high = low + max(abs(float(low)) * 1e-6, 1e-6)
    return float(low), float(high)


class MonalisaLatticeResult(ProcessingResult):
    """The reconstruction, and what was asked for with it.

    ``data`` is the reconstruction: ``(Y, X)`` for a recording of one scan,
    ``(T, Y, X)`` for one of several. Pixels no focus measured are 0.

    The other images of the run are display layers of their own, each with
    its own contrast, and inputs a processor can pick:

    - ``sharpened``: the reconstruction after the sharpening filter;
    - ``background``: the constant the fit took for background, per pixel;
    - ``pinholes``: the pinhole stack, one image per pixel of the footprint.
      With it every layer has a ``Pinhole`` axis: the stack runs along it,
      and the reconstruction is the same image at every position, so that
      the slider compares every pinhole with it.

    ``diagnostics`` holds what the pipeline read from the frames; its rows go
    to the results table.
    """

    publishes_table_rows = True

    def __init__(
        self,
        name: str,
        image: np.ndarray,
        *,
        output_pixel_nm: float | None,
        params: dict,
        diagnostics: dict[str, Any],
        sharpened: np.ndarray | None = None,
        background: np.ndarray | None = None,
        pinholes: np.ndarray | None = None,
        pinhole_offsets: tuple[np.ndarray, np.ndarray] | None = None,
    ):
        image = np.asarray(image, dtype=np.float32)
        if image.ndim == 3 and image.shape[0] == 1:
            image = image[0]
        if image.ndim not in (2, 3):
            raise ValueError(f"Expected a (Y, X) or (T, Y, X) image, got {image.shape}")
        # Without the size of a scan step the image has pixels and no scale.
        calibrated = bool(output_pixel_nm)
        pixel_um = float(output_pixel_nm) / 1000.0 if calibrated else 1.0
        labels = ["Y", "X"] if image.ndim == 2 else ["T", "Y", "X"]
        scales = [pixel_um, pixel_um] if image.ndim == 2 else [1.0, pixel_um, pixel_um]
        super().__init__(
            name=name,
            data=image,
            axis_labels=labels,
            display_levels=_levels(image),
            axis_scales=scales,
            scale_unit="um" if calibrated else "px",
        )
        self.params = dict(params)
        self.diagnostics = dict(diagnostics)
        self.output_pixel_size_nm = float(output_pixel_nm) if calibrated else None
        self.sharpened = self._like_image(sharpened)
        self.background = self._like_image(background)
        self.pinholes = None if pinholes is None else np.asarray(pinholes, np.float32)
        self.pinhole_offsets = pinhole_offsets
        if self.pinholes is not None:
            expected = (*image.shape[:-2], self.pinholes.shape[-3], *image.shape[-2:])
            if self.pinholes.shape != expected:
                raise ValueError(
                    f"The pinhole stack has shape {self.pinholes.shape}, "
                    f"expected {expected}"
                )

    def _like_image(self, plane):
        if plane is None:
            return None
        plane = np.asarray(plane, dtype=np.float32)
        if plane.ndim == 3 and plane.shape[0] == 1 and self.data.ndim == 2:
            plane = plane[0]
        if plane.shape != self.data.shape:
            raise ValueError(
                f"A plane of shape {plane.shape} does not fit the image {self.data.shape}"
            )
        return plane

    # ------------------------------------------------------------- display
    def _planes(self) -> list[tuple[str, np.ndarray, bool]]:
        planes = [("reconstruction", self.data, True)]
        if self.sharpened is not None:
            planes.append(("sharpened", self.sharpened, False))
        if self.background is not None:
            planes.append(("background", self.background, False))
        return planes

    def display_layers(self) -> list[DisplayLayerSpec]:
        planes = self._planes()
        if len(planes) == 1 and self.pinholes is None:
            return []
        labels = list(self.axis_labels)
        scales = list(self.axis_scales)
        count = None
        if self.pinholes is not None:
            count = self.pinholes.shape[-3]
            labels = [*labels[:-2], "Pinhole", *labels[-2:]]
            scales = [*scales[:-2], 1.0, *scales[-2:]]

        layers = []
        for component, plane, visible in planes:
            data = plane
            if count is not None:
                # The same image at every pinhole: a view, not a copy.
                expanded = np.expand_dims(plane, axis=-3)
                shape = (*plane.shape[:-2], count, *plane.shape[-2:])
                data = np.broadcast_to(expanded, shape)
            layers.append(DisplayLayerSpec(
                name=f"{self.name}_{component}",
                data=data,
                axis_labels=list(labels),
                display_levels=_levels(plane),
                axis_scales=list(scales),
                scale_unit=self.scale_unit,
                visible=visible,
                component=component,
                metadata={"source_result": self.name, "component": component},
            ))
        if self.pinholes is not None:
            layers.append(DisplayLayerSpec(
                name=f"{self.name}_pinholes",
                data=self.pinholes,
                axis_labels=list(labels),
                display_levels=_levels(self.pinholes),
                axis_scales=list(scales),
                scale_unit=self.scale_unit,
                visible=False,
                component="pinholes",
                metadata={
                    "source_result": self.name,
                    "component": "pinholes",
                    "pinhole_dx": [int(v) for v in self.pinhole_offsets[0]],
                    "pinhole_dy": [int(v) for v in self.pinhole_offsets[1]],
                },
            ))
        return layers

    def display_layer_data(self) -> list[np.ndarray]:
        return [layer.data for layer in self.display_layers()]

    # --------------------------------------------------------------- table
    def table_columns(self) -> list[str]:
        return ["result", "quantity", "value"]

    def table_records(self) -> list[dict[str, Any]]:
        return [
            {"result": self.name, "quantity": key, "value": value}
            for key, value in self.diagnostics.items()
        ]

    # ---------------------------------------------------------------- save
    def write_files(self, plan, document) -> None:
        from imswitch.improcess.model.footprint import json_safe
        from imswitch.improcess.model.result_io import save_image_result

        save_image_result(
            self, plan.primary, plan.fmt,
            extra={
                "monalisa_lattice_params": json_safe(dict(self.params)),
                "monalisa_lattice_diagnostics": json_safe(dict(self.diagnostics)),
            },
            document=document,
        )


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
