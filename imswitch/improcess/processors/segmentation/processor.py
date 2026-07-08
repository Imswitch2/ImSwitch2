"""Threshold + connected-component segmentation processor."""

from typing import Callable

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.analysis.segmentation import segment_image
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors._extraction import axis_labels_for_data
from imswitch.improcess.processors.base import Processor

from .result import SegmentationResult


class SegmentationProcessor(Processor):
    """Segment a 2D plane from an ImProcess result."""

    name = "Segmentation"
    id = "segmentation"

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        """Require at least a 2-D image; one (Y, X) plane is segmented, with any
        extra non-spatial axes collapsed to caller-selected indices (default
        0)."""
        return lambda result: result.data.ndim >= 2

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        method_combo = QtWidgets.QComboBox()
        method_combo.addItems(["otsu", "manual", "triangle", "yen", "local", "watershed"])
        layout.addRow("Method:", method_combo)

        threshold_spin = QtWidgets.QDoubleSpinBox()
        threshold_spin.setDecimals(6)
        threshold_spin.setRange(-1e12, 1e12)
        threshold_spin.setValue(0.0)
        layout.addRow("Manual value:", threshold_spin)

        min_area_spin = QtWidgets.QSpinBox()
        min_area_spin.setRange(1, 999999999)
        min_area_spin.setValue(10)
        layout.addRow("Min area:", min_area_spin)

        smooth_spin = QtWidgets.QDoubleSpinBox()
        smooth_spin.setDecimals(3)
        smooth_spin.setRange(0.0, 1000.0)
        smooth_spin.setValue(0.0)
        layout.addRow("Smooth sigma:", smooth_spin)

        background_spin = QtWidgets.QDoubleSpinBox()
        background_spin.setDecimals(1)
        background_spin.setRange(0.0, 10000.0)
        background_spin.setValue(0.0)
        layout.addRow("Top-hat radius:", background_spin)

        morphology_spin = QtWidgets.QSpinBox()
        morphology_spin.setRange(0, 9999)
        morphology_spin.setValue(0)
        layout.addRow("Morph radius:", morphology_spin)

        fill_holes_check = QtWidgets.QCheckBox("Fill holes")
        layout.addRow("", fill_holes_check)

        clear_border_check = QtWidgets.QCheckBox("Clear border")
        layout.addRow("", clear_border_check)

        local_block_spin = QtWidgets.QSpinBox()
        local_block_spin.setRange(3, 9999)
        local_block_spin.setSingleStep(2)
        local_block_spin.setValue(51)
        layout.addRow("Local block:", local_block_spin)

        local_offset_spin = QtWidgets.QDoubleSpinBox()
        local_offset_spin.setDecimals(6)
        local_offset_spin.setRange(-1e12, 1e12)
        local_offset_spin.setValue(0.0)
        layout.addRow("Local offset:", local_offset_spin)

        watershed_distance_spin = QtWidgets.QSpinBox()
        watershed_distance_spin.setRange(1, 9999)
        watershed_distance_spin.setValue(5)
        layout.addRow("Watershed distance:", watershed_distance_spin)

        t_index_spin = QtWidgets.QSpinBox()
        t_index_spin.setRange(0, 999999)
        t_index_spin.setValue(0)
        layout.addRow("T index:", t_index_spin)

        z_index_spin = QtWidgets.QSpinBox()
        z_index_spin.setRange(0, 999999)
        z_index_spin.setValue(0)
        layout.addRow("Z index:", z_index_spin)

        c_index_spin = QtWidgets.QSpinBox()
        c_index_spin.setRange(0, 999999)
        c_index_spin.setValue(0)
        layout.addRow("C index:", c_index_spin)

        axis_indices_edit = QtWidgets.QLineEdit()
        axis_indices_edit.setPlaceholderText("e.g. Dataset=0, Base=1")
        layout.addRow("Other axes:", axis_indices_edit)

        def get_values():
            return {
                "threshold_method": method_combo.currentText(),
                "threshold_value": threshold_spin.value(),
                "min_area": min_area_spin.value(),
                "smooth_sigma": smooth_spin.value(),
                "background_radius": background_spin.value(),
                "morphology_radius": morphology_spin.value(),
                "fill_holes": fill_holes_check.isChecked(),
                "clear_border": clear_border_check.isChecked(),
                "local_block_size": local_block_spin.value(),
                "local_offset": local_offset_spin.value(),
                "watershed_min_distance": watershed_distance_spin.value(),
                "t_index": t_index_spin.value(),
                "z_index": z_index_spin.value(),
                "c_index": c_index_spin.value(),
                "axis_indices": axis_indices_edit.text().strip(),
            }

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        image, plane_indices = self._extract_2d(result, params)
        axis_scales = self._spatial_axis_scales(result)
        threshold_method = params.get("threshold_method", "otsu")
        analysis = segment_image(
            image,
            threshold_method=threshold_method,
            threshold_value=(
                float(params.get("threshold_value", 0.0))
                if threshold_method == "manual"
                else None
            ),
            min_area=int(params.get("min_area", 10)),
            smooth_sigma=float(params.get("smooth_sigma", 0.0)),
            background_radius=float(params.get("background_radius", 0.0)),
            morphology_radius=int(params.get("morphology_radius", 0)),
            fill_holes=bool(params.get("fill_holes", False)),
            clear_border=bool(params.get("clear_border", False)),
            local_block_size=int(params.get("local_block_size", 51)),
            local_offset=float(params.get("local_offset", 0.0)),
            watershed_min_distance=int(params.get("watershed_min_distance", 5)),
        )
        analysis.metadata["source_plane_indices"] = dict(plane_indices)
        return SegmentationResult(
            name=f"{result.name} (segmentation)",
            analysis=analysis,
            params=dict(params),
            axis_scales=axis_scales,
            scale_unit=result.scale_unit,
            source_image=np.asarray(image),
        )

    @staticmethod
    def _extract_2d(result: ProcessingResult, params: dict | None = None) -> tuple[np.ndarray, dict]:
        data = np.asarray(result.data)
        if data.ndim == 2:
            return data, {}
        if data.ndim < 2:
            raise ValueError(f"Segmentation needs at least 2D data, got shape {data.shape}")
        params = params or {}
        labels = axis_labels_for_data(result, data)
        explicit_indices = _parse_axis_indices(params.get("axis_indices", ""))
        non_spatial_labels = set(labels[: data.ndim - 2])
        unknown_labels = sorted(set(explicit_indices) - non_spatial_labels)
        if unknown_labels:
            raise ValueError(
                "Segmentation axis_indices contains unknown axis label(s): "
                f"{unknown_labels}. Available non-spatial axes: {sorted(non_spatial_labels)}"
            )
        requested_indices = _requested_plane_indices(params, explicit_indices)
        indexer = []
        plane_indices = {}
        for axis in range(data.ndim):
            if axis >= data.ndim - 2:
                indexer.append(slice(None))
            else:
                label = labels[axis]
                index = int(requested_indices.get(label, 0))
                size = data.shape[axis]
                if index < 0 or index >= size:
                    raise ValueError(
                        f"Segmentation index {index} out of range for axis "
                        f"{label!r} with size {size}"
                    )
                indexer.append(index)
                plane_indices[label] = index
        image = np.asarray(data[tuple(indexer)])
        if image.ndim != 2:
            raise ValueError(f"Could not extract a 2D segmentation image from shape {data.shape}")
        return image, plane_indices

    @staticmethod
    def _spatial_axis_scales(result: ProcessingResult) -> list[float] | None:
        data = np.asarray(result.data)
        axis_scales = list(getattr(result, "axis_scales", []) or [])
        if len(axis_scales) != data.ndim or data.ndim < 2:
            return None
        return [float(axis_scales[-2]), float(axis_scales[-1])]


def _requested_plane_indices(params: dict, explicit_indices: dict[str, int]) -> dict[str, int]:
    indices = {
        "T": int(params.get("t_index", 0)),
        "Z": int(params.get("z_index", 0)),
        "C": int(params.get("c_index", 0)),
    }
    indices.update(explicit_indices)
    return indices


def _parse_axis_indices(text: object) -> dict[str, int]:
    if text is None:
        return {}
    raw = str(text).strip()
    if not raw:
        return {}
    parsed = {}
    for item in raw.split(","):
        part = item.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(
                "Segmentation axis_indices must use comma-separated Label=index entries"
            )
        label, value = part.split("=", 1)
        label = label.strip()
        if not label:
            raise ValueError("Segmentation axis_indices contains an empty axis label")
        try:
            parsed[label] = int(value.strip())
        except ValueError as exc:
            raise ValueError(f"Invalid segmentation index for axis {label!r}: {value!r}") from exc
    return parsed
