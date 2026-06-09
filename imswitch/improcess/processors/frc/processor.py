"""Fourier ring correlation processor."""

from typing import Callable

import numpy as np
from qtpy import QtWidgets

from imswitch.imcommon.model import initLogger
from imswitch.improcess.analysis.frc import frc_two_image, single_image_frc
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors.base import Processor

from .result import FRCResult


class FRCProcessor(Processor):
    """Compute two-image or single-image Fourier ring correlation."""

    name = "FRC Resolution"
    id = "frc"

    def __init__(self):
        self._logger = initLogger(self, tryInheritParent=False)

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: result.data.ndim >= 2

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        mode_combo = QtWidgets.QComboBox()
        mode_combo.addItems(["single-image", "two-image"])
        mode_combo.setCurrentText("single-image")
        layout.addRow("Mode:", mode_combo)

        axis_combo = QtWidgets.QComboBox()
        axis_combo.addItems(["Auto", "T", "C", "Z", "D0", "D1", "D2"])
        axis_combo.setCurrentText("Auto")
        layout.addRow("Compare axis:", axis_combo)

        index_a_spin = QtWidgets.QSpinBox()
        index_a_spin.setRange(0, 999999)
        index_a_spin.setValue(0)
        layout.addRow("Index A:", index_a_spin)

        index_b_spin = QtWidgets.QSpinBox()
        index_b_spin.setRange(0, 999999)
        index_b_spin.setValue(1)
        layout.addRow("Index B:", index_b_spin)

        split_combo = QtWidgets.QComboBox()
        split_combo.addItems(["checkerboard", "odd-even"])
        split_combo.setCurrentText("checkerboard")
        layout.addRow("Single-image split:", split_combo)

        window_combo = QtWidgets.QComboBox()
        window_combo.addItems(["hann", "none"])
        window_combo.setCurrentText("hann")
        layout.addRow("Window:", window_combo)

        pixel_size_spin = QtWidgets.QDoubleSpinBox()
        pixel_size_spin.setDecimals(6)
        pixel_size_spin.setRange(1e-9, 1e12)
        pixel_size_spin.setValue(1.0)
        layout.addRow("Pixel size:", pixel_size_spin)

        unit_combo = QtWidgets.QComboBox()
        unit_combo.addItems(["px", "nm", "um"])
        unit_combo.setCurrentText("px")
        layout.addRow("Resolution unit:", unit_combo)

        def get_values():
            return {
                "mode": mode_combo.currentText(),
                "compare_axis": axis_combo.currentText(),
                "index_a": index_a_spin.value(),
                "index_b": index_b_spin.value(),
                "single_image_split": split_combo.currentText(),
                "window": window_combo.currentText(),
                "pixel_size": pixel_size_spin.value(),
                "resolution_unit": unit_combo.currentText(),
            }

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        mode = params.get("mode", "single-image")
        pixel_size = float(params.get("pixel_size", 1.0))
        resolution_unit = params.get("resolution_unit", "px")
        frequency_unit = f"cycles/{resolution_unit}"
        window = params.get("window", "hann")

        if mode == "two-image":
            axis_label = self._resolve_compare_axis(result, params.get("compare_axis", "Auto"))
            image_a = self._extract_2d(result, axis_label, int(params.get("index_a", 0)))
            image_b = self._extract_2d(result, axis_label, int(params.get("index_b", 1)))
            analysis = frc_two_image(
                image_a,
                image_b,
                pixel_size=pixel_size,
                frequency_unit=frequency_unit,
                resolution_unit=resolution_unit,
                window=window,
            )
            name = f"{result.name} (FRC {axis_label}{params.get('index_a', 0)}-{params.get('index_b', 1)})"
        elif mode == "single-image":
            image = self._extract_2d(result, None, int(params.get("index_a", 0)))
            analysis = single_image_frc(
                image,
                split=params.get("single_image_split", "checkerboard"),
                pixel_size=pixel_size,
                frequency_unit=frequency_unit,
                resolution_unit=resolution_unit,
                window=window,
            )
            name = f"{result.name} (single-image FRC)"
        else:
            raise ValueError(f"Unsupported FRC mode: {mode!r}")

        self._logger.info(
            "FRC result: cutoff=%s %s, resolution=%s %s",
            analysis.cutoff_frequency,
            analysis.frequency_unit,
            analysis.resolution,
            analysis.resolution_unit,
        )
        return FRCResult(name=name, analysis=analysis, params=dict(params))

    def _resolve_compare_axis(self, result: ProcessingResult, requested: str) -> str:
        if requested != "Auto":
            if requested not in result.axis_labels:
                raise ValueError(f"Requested compare axis {requested!r} not in {result.axis_labels}")
            if result.data.shape[result.axis_labels.index(requested)] < 2:
                raise ValueError(f"Compare axis {requested!r} needs at least two planes")
            return requested

        for candidate in ("T", "C", "Z"):
            if candidate in result.axis_labels:
                axis = result.axis_labels.index(candidate)
                if result.data.shape[axis] >= 2:
                    return candidate
        for axis, size in enumerate(result.data.shape[:-2]):
            if size >= 2:
                return result.axis_labels[axis]
        raise ValueError("Two-image FRC needs an axis with at least two planes")

    def _extract_2d(
        self,
        result: ProcessingResult,
        compare_axis_label: str | None,
        compare_index: int,
    ) -> np.ndarray:
        data = np.asarray(result.data)
        if data.ndim == 2:
            return data
        if data.ndim < 2:
            raise ValueError(f"FRC needs at least 2D data, got shape {data.shape}")

        indexer = []
        compare_axis = (
            result.axis_labels.index(compare_axis_label)
            if compare_axis_label is not None
            else None
        )
        for axis, size in enumerate(data.shape):
            is_spatial = axis >= data.ndim - 2
            if is_spatial:
                indexer.append(slice(None))
            elif axis == compare_axis:
                if compare_index < 0 or compare_index >= size:
                    raise ValueError(
                        f"FRC compare index {compare_index} out of range for axis "
                        f"{compare_axis_label!r} with size {size}"
                    )
                indexer.append(compare_index)
            else:
                indexer.append(0)

        image = np.asarray(data[tuple(indexer)])
        if image.ndim != 2:
            raise ValueError(f"Could not extract a 2D FRC image from shape {data.shape}")
        return image
