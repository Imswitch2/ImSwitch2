"""Line-step sum: one image per distinct line-step condition of an Advanced Scan.

The Advanced Scan can repeat every line several times -- "line steps" -- and
switch a different set of TTL devices on for each repeat. Two-colour STED is
the motivating case: ten line steps alternating "488 + STED" and "561 + STED"
interleave the two colours line by line, so both see the same sample motion.
A scan-driven point detector (APD, PMT) stores one plane per line step, as
``(frame, condition, [Z,] Y, X)``.

This reconstructor reads which devices each line step switched on from the
recording's ``ScanTTL`` metadata, groups the line steps that are the same
condition, and sums each group into one image. The ten steps above come out
as two images, each the sum of five.

What makes two line steps "the same" is a choice (``match``):

- ``On/off, power and timing`` (the default) also keeps apart steps that
  switch on the same devices at a different per-step power, intra-pixel pulse
  timing, sequence program or positioner move -- summing those would mix two
  different measurements;
- ``On/off only`` looks at which devices are on and nothing else.

If the analysed detector itself has a line-step gate, the steps it was gated
off on hold no signal and are left out rather than summed into an image.

Copyright (C) 2020-2026 ImSwitch developers
This file is part of ImSwitch.

ImSwitch is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

ImSwitch is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping, Sequence

import numpy as np
from qtpy import QtWidgets

from imswitch.imcommon.algorithms.spatial_frame import content_digest_uid
from imswitch.improcess.model.acquisition_layout_resolver import _normalized_attrs
from imswitch.improcess.model.contrast import finite_range
from imswitch.improcess.model.param_spec import ParamField
from imswitch.improcess.model.result import (
    DisplayLayerSpec,
    ProcessingResult,
    SerializationView,
    ViewMode,
)
from imswitch.improcess.model.result_io import save_image_result
from imswitch.improcess.reconstructors.base import (
    ReconstructionContext,
    Reconstructor,
    SourceInspection,
)

if TYPE_CHECKING:
    from imswitch.improcess.model import DataObj


MATCH_SETTINGS = "On/off, power and timing"
MATCH_ONOFF = "On/off only"
MATCH_OPTIONS = (MATCH_SETTINGS, MATCH_ONOFF)

#: Storage roles to the axis names shown on the viewer's sliders.
_AXIS_NAMES = {
    "frame": "T",
    "time": "T",
    "condition": "C",
    "channel": "C",
    "scan_x": "X",
    "scan_y": "Y",
    "scan_z": "Z",
    "detector_x": "X",
    "detector_y": "Y",
}

#: One colour per summed image, overlaid additively. Green/magenta first:
#: the usual two-colour pair, and readable with red-green colour blindness.
_COLORMAPS = ("green", "magenta", "cyan", "yellow", "red", "blue")

_ATTR = "ScanTTL:"


class LineStepSumError(ValueError):
    """The source cannot be summed by line-step condition."""


@dataclass(frozen=True)
class LineStepGroup:
    """Line steps that share one condition, and what that condition is."""

    label: str
    steps: tuple[int, ...]
    devices: tuple[str, ...]
    settings: str = ""

    @property
    def step_numbers(self) -> str:
        """The steps as the scan widget numbers them (from 1)."""
        return ", ".join(str(step + 1) for step in self.steps)


@dataclass(frozen=True)
class LineStepPlan:
    """How one recording's line steps group, decided from metadata alone."""

    groups: tuple[LineStepGroup, ...]
    n_steps: int
    skipped_steps: tuple[int, ...] = ()
    gate: str | None = None

    def describe(self) -> str:
        parts = [
            f"{group.label}: step{'s' if len(group.steps) > 1 else ''} {group.step_numbers}"
            for group in self.groups
        ]
        if self.skipped_steps:
            numbers = ", ".join(str(step + 1) for step in self.skipped_steps)
            parts.append(f"left out ({self.gate} gated off): {numbers}")
        return "; ".join(parts)


# --------------------------------------------------------------------- grouping
def _per_step(value: Any, n_steps: int, what: str, device: str) -> list:
    if not isinstance(value, (list, tuple)):
        raise LineStepSumError(
            f"The {what} of {device!r} is not a per-line-step list ({value!r})."
        )
    if len(value) != n_steps:
        raise LineStepSumError(
            f"The {what} of {device!r} has {len(value)} entries, but the scan has "
            f"{n_steps} line steps, so it is unclear which entry belongs to which step."
        )
    return list(value)


def _mapping(attrs: Mapping[str, Any], key: str) -> dict:
    value = attrs.get(_ATTR + key)
    return dict(value) if isinstance(value, Mapping) else {}


def _is_gate_of(device: str, detector: str | None) -> bool:
    """Whether a TTL line is the analysed detector's own gate.

    Matched as the acquisition-layout resolver matches detector masks, so the
    two never disagree about which line gates the detector.
    """
    if not detector:
        return False
    device_lower, detector_lower = str(device).lower(), str(detector).lower()
    return detector_lower == device_lower or detector_lower in device_lower


def _canonical(value: Any) -> str:
    """A stable, comparable text for a nested settings value."""

    def rounded(item):
        if isinstance(item, float):
            return round(item, 12)
        if isinstance(item, Mapping):
            return {str(key): rounded(val) for key, val in item.items()}
        if isinstance(item, (list, tuple)):
            return [rounded(val) for val in item]
        return item

    return json.dumps(rounded(value), sort_keys=True, default=str)


class _Settings:
    """The per-step settings beyond on/off, read once from the attributes."""

    def __init__(self, attrs: Mapping[str, Any], n_steps: int):
        self.n_steps = n_steps
        self.advanced = bool(attrs.get(_ATTR + "advanced_mode", False))
        self.power = {
            device: _per_step(vector, n_steps, "line-step power", device)
            for device, vector in _mapping(attrs, "linestep_power_percent").items()
        }
        self.power_enabled = _mapping(attrs, "linestep_power_enabled")
        self.starts = self._steps(attrs, "pulse_starts_s", "pulse start times")
        self.ends = self._steps(attrs, "pulse_ends_s", "pulse end times")
        sequence_mode = (
            self.advanced
            and str(attrs.get(_ATTR + "advanced_program_mode", "")) == "sequence"
        )
        rows = attrs.get(_ATTR + "advanced_sequence_rows")
        self.sequence = (
            _per_step(rows, n_steps, "sequence program", "the scan")
            if sequence_mode and isinstance(rows, (list, tuple)) and any(rows)
            else None
        )
        self.positioners: dict[str, list] = {}
        if self.advanced and bool(attrs.get(_ATTR + "intra_pixel_positioner_movement", False)):
            enables = _mapping(attrs, "positioner_linestep_enable")
            starts = _mapping(attrs, "positioner_movement_starts_s")
            ends = _mapping(attrs, "positioner_movement_ends_s")
            sizes = _mapping(attrs, "positioner_step_size_um")
            for positioner, enable in enables.items():
                enable = _per_step(enable, n_steps, "line-step movement", positioner)
                program = []
                for step in range(n_steps):
                    program.append([
                        bool(enable[step]),
                        self._at(starts, positioner, step),
                        self._at(ends, positioner, step),
                        self._at(sizes, positioner, step),
                    ])
                self.positioners[positioner] = program

    def _steps(self, attrs, key, what):
        if not self.advanced:
            # Outside advanced mode the designer switches a device on for the
            # whole pixel; the stored pulse lists are empty and do not apply.
            return {}
        return {
            device: _per_step(vector, self.n_steps, what, device)
            for device, vector in _mapping(attrs, key).items()
        }

    def _at(self, table: Mapping[str, Any], positioner: str, step: int):
        value = table.get(positioner)
        if value is None:
            return None
        return _per_step(value, self.n_steps, "movement program", positioner)[step]

    def power_of(self, device: str, step: int) -> float | None:
        if not bool(self.power_enabled.get(device, True)):
            return None
        vector = self.power.get(device)
        return None if vector is None else round(float(vector[step]), 3)

    def pulses_of(self, device: str, step: int):
        starts = self.starts.get(device)
        ends = self.ends.get(device)
        if not starts and not ends:
            return None
        pulses = (
            list(starts[step]) if starts else [],
            list(ends[step]) if ends else [],
        )
        return pulses if any(pulses) else None

    def key(self, on: Sequence[str], step: int) -> str:
        return _canonical({
            "power": {device: self.power_of(device, step) for device in on},
            "pulses": {device: self.pulses_of(device, step) for device in on},
            "sequence": None if self.sequence is None else self.sequence[step],
            "positioners": {name: program[step] for name, program in self.positioners.items()},
        })

    def summary(self, on: Sequence[str], step: int) -> str:
        parts = []
        for device in on:
            power = self.power_of(device, step)
            parts.append(device if power is None else f"{device} {power:g}%")
        notes = []
        if any(self.pulses_of(device, step) for device in on):
            notes.append("custom pulse timing")
        if self.sequence is not None and self.sequence[step]:
            notes.append("sequence program")
        if any(program[step][0] for program in self.positioners.values()):
            notes.append("positioner move")
        text = ", ".join(parts) if parts else "no TTL device on"
        return f"{text}; {', '.join(notes)}" if notes else text


def group_line_steps(
    attrs: Mapping[str, Any] | None,
    n_steps: int,
    *,
    detector: str | None = None,
    match: str = MATCH_SETTINGS,
) -> LineStepPlan:
    """Group a scan's line steps by condition, from its ``ScanTTL`` metadata.

    Args:
        attrs: The recording's attributes, flat (``"ScanTTL:linestep_enable"``)
            or as stored, JSON-encoded values included.
        n_steps: Number of line steps in the stored data.
        detector: The analysed detector; a TTL line matching its name is its
            gate, and the steps it was gated off on are left out.
        match: ``MATCH_SETTINGS`` or ``MATCH_ONOFF``.

    Returns:
        The groups in order of their first line step.
    """
    if match not in MATCH_OPTIONS:
        raise LineStepSumError(f"Unknown grouping {match!r}; expected one of {MATCH_OPTIONS}.")
    n_steps = int(n_steps)
    if n_steps < 2:
        raise LineStepSumError(
            "This scan has a single line step, so there is nothing to combine. "
            "Open it with 'View only'."
        )
    attrs = _normalized_attrs(attrs or {})
    recorded = attrs.get(_ATTR + "n_linesteps")
    if recorded is not None and int(recorded) != n_steps:
        raise LineStepSumError(
            f"The scan metadata says {int(recorded)} line steps, but the stored data "
            f"holds {n_steps}."
        )
    masks = _mapping(attrs, "linestep_enable")
    if not masks:
        raise LineStepSumError(
            "This recording does not say which devices were on in each line step "
            "(no ScanTTL linestep_enable metadata), so its line steps cannot be grouped."
        )

    gate = None
    gate_mask = None
    devices: list[str] = []
    enabled: dict[str, list[bool]] = {}
    for device, mask in masks.items():
        mask = [bool(value) for value in _per_step(mask, n_steps, "line-step mask", device)]
        if _is_gate_of(device, detector):
            gate, gate_mask = device, mask
            continue
        devices.append(device)
        enabled[device] = mask

    settings = _Settings(attrs, n_steps) if match == MATCH_SETTINGS else None

    order: list[tuple] = []
    members: dict[tuple, list[int]] = {}
    skipped: list[int] = []
    for step in range(n_steps):
        if gate_mask is not None and not gate_mask[step]:
            skipped.append(step)
            continue
        on = tuple(device for device in devices if enabled[device][step])
        key = (on, settings.key(on, step) if settings is not None else "")
        if key not in members:
            members[key] = []
            order.append(key)
        members[key].append(step)
    if not order:
        raise LineStepSumError(
            f"The detector's gate {gate!r} is off in every line step; nothing was recorded."
        )

    base = {key: " + ".join(key[0]) if key[0] else "No TTL device on" for key in order}
    labels = dict(base)
    if settings is not None:
        # Same devices, different settings: name them by power first, which is
        # what usually differs; anything else is told apart by its steps.
        repeated = {key for key in order if list(base.values()).count(base[key]) > 1}
        for key in repeated:
            first = members[key][0]
            parts = []
            for device in key[0]:
                power = settings.power_of(device, first)
                parts.append(device if power is None else f"{device} {power:g}%")
            labels[key] = " + ".join(parts) if parts else base[key]
        still_repeated = [
            key for key in order if list(labels.values()).count(labels[key]) > 1
        ]
        for key in still_repeated:
            numbers = ", ".join(str(step + 1) for step in members[key])
            labels[key] = f"{labels[key]} (steps {numbers})"

    groups = []
    for key in order:
        steps = tuple(members[key])
        summary = (
            settings.summary(key[0], steps[0]) if settings is not None
            else (", ".join(key[0]) or "no TTL device on")
        )
        groups.append(LineStepGroup(labels[key], steps, tuple(key[0]), summary))
    return LineStepPlan(tuple(groups), n_steps, tuple(skipped), gate)


# ---------------------------------------------------------------------- summing
def sum_dtype(dtype, n_terms: int) -> np.dtype:
    """The narrowest type that holds a sum of ``n_terms`` values of ``dtype``."""
    dtype = np.dtype(dtype)
    if dtype.kind == "b":
        dtype = np.dtype(np.uint8)
    if dtype.kind in "ui":
        info = np.iinfo(dtype)
        candidates = (np.uint32, np.uint64) if dtype.kind == "u" else (np.int32, np.int64)
        for candidate in candidates:
            limits = np.iinfo(candidate)
            if (limits.bits >= info.bits and int(info.max) * n_terms <= int(limits.max)
                    and int(info.min) * n_terms >= int(limits.min)):
                return np.dtype(candidate)
        return np.dtype(np.float64)
    if dtype.kind == "f":
        return np.dtype(np.float32) if dtype.itemsize <= 4 else dtype
    if dtype.kind == "c":
        return dtype
    raise LineStepSumError(f"Cannot sum line steps of type {dtype}.")


def sum_line_steps(
    data,
    condition_axis: int,
    groups: Sequence[LineStepGroup],
    context: ReconstructionContext | None = None,
) -> np.ndarray:
    """Sum each group's planes along ``condition_axis``.

    Reads one line-step plane at a time, so an HDF5 or Zarr dataset is never
    loaded whole. The group axis replaces the condition axis in the output.
    """
    shape = list(data.shape)
    del shape[condition_axis]
    out_dtype = sum_dtype(data.dtype, max(len(group.steps) for group in groups))
    out = np.zeros((len(groups), *shape), dtype=out_dtype)
    total = sum(len(group.steps) for group in groups)
    done = 0
    for index, group in enumerate(groups):
        for step in group.steps:
            if context is not None:
                context.check_cancelled()
            selection = [slice(None)] * len(data.shape)
            selection[condition_axis] = step
            plane = np.asarray(data[tuple(selection)])
            out[index] += plane.astype(out_dtype, copy=False)
            done += 1
            if context is not None:
                context.report("assemble", done, total, f"line step {step + 1}")
    return np.ascontiguousarray(np.moveaxis(out, 0, condition_axis))


# ----------------------------------------------------------------------- result
class LineStepSumResult(ProcessingResult):
    """The summed images on a ``C`` axis, one viewer layer each."""

    kind = "image"
    #: One row per summed image, so which steps went where is readable at once.
    publishes_table_rows = True

    def __init__(self, *args, plan: LineStepPlan, channel_axis: int,
                 source_name: str, match: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.plan = plan
        self.channel_axis = int(channel_axis)
        self.source_name = source_name
        self.match = match

    @property
    def groups(self) -> tuple[LineStepGroup, ...]:
        return self.plan.groups

    def channel_names(self) -> list[str]:
        return [group.label for group in self.groups]

    def display_layers(self) -> list[DisplayLayerSpec]:
        labels = [
            label for axis, label in enumerate(self.axis_labels) if axis != self.channel_axis
        ]
        scales = [
            scale for axis, scale in enumerate(self.axis_scales) if axis != self.channel_axis
        ]
        single = len(self.groups) == 1
        layers = []
        for index, group in enumerate(self.groups):
            data = np.take(self.data, index, axis=self.channel_axis)
            layers.append(DisplayLayerSpec(
                name=group.label,
                data=data,
                axis_labels=labels,
                display_levels=finite_range(data),
                axis_scales=scales,
                scale_unit=self.scale_unit,
                colormap="grayclip" if single else _COLORMAPS[index % len(_COLORMAPS)],
                # The condition's name doubles as the component id, so a
                # processor's input list offers "488 (EXC) + 775 (STED)".
                component=group.label,
                metadata={
                    "source_result": self.name,
                    "component": group.label,
                    "channel_axis": self.channel_axis,
                    "channel_index": index,
                    "line_steps": [step + 1 for step in group.steps],
                    "settings": group.settings,
                },
                layer_kwargs=None if single else {"blending": "additive"},
            ))
        return layers

    def serialization_view(self) -> SerializationView:
        view = super().serialization_view()
        view.channel_names = self.channel_names()
        return view

    def write_files(self, plan, document) -> None:
        save_image_result(self, plan.primary, plan.fmt, document=document)

    def table_columns(self) -> list[str]:
        return ["recording", "image", "line steps", "steps summed", "settings"]

    def table_records(self) -> list[dict[str, Any]]:
        rows = [
            {
                "recording": self.source_name,
                "image": group.label,
                "line steps": group.step_numbers,
                "steps summed": len(group.steps),
                "settings": group.settings,
            }
            for group in self.groups
        ]
        if self.plan.skipped_steps:
            rows.append({
                "recording": self.source_name,
                "image": "(left out)",
                "line steps": ", ".join(str(step + 1) for step in self.plan.skipped_steps),
                "steps summed": 0,
                "settings": f"detector gate {self.plan.gate!r} off",
            })
        return rows


# ------------------------------------------------------------------- the plugin
class _LineStepSumParamsWidget(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        form = QtWidgets.QFormLayout(self)
        self.match = QtWidgets.QComboBox(self)
        self.match.addItems(list(MATCH_OPTIONS))
        self.match.setCurrentText(LineStepSumReconstructor.default_params()["match"])
        self.match.setToolTip(
            "Line steps are summed when they agree on this. 'On/off, power and "
            "timing' also keeps apart steps that switch on the same devices at a "
            "different power, pulse timing, sequence program or positioner move.")
        form.addRow("Group line steps by", self.match)
        note = QtWidgets.QLabel(
            "Sums the line steps of an Advanced Scan that share a condition: "
            "one image per combination of devices switched on.", self)
        note.setWordWrap(True)
        form.addRow(note)

    def get_values(self) -> dict:
        return {"match": self.match.currentText()}


def _source_identity(data_obj) -> str | None:
    """The file's identity, as View only derives it, so ROIs line up."""
    path = getattr(data_obj, "dataPath", None)
    if not path:
        return None
    try:
        stat = os.stat(path)
        return content_digest_uid("data", path, stat.st_size, int(stat.st_mtime))
    except OSError:
        return content_digest_uid("data", path)


class LineStepSumReconstructor(Reconstructor):
    """Sum an Advanced Scan's line steps by condition."""

    name = "Line-step sum"
    id = "linestep-sum"
    output_kind = "image"
    # View only's list: registered after it, this never wins its auto-select.
    file_extensions = ["hdf5", "tiff", "tif", "zarr"]
    description = (
        "Advanced Scan with line steps: sum the steps that share a laser "
        "combination, one image per combination (e.g. two-colour STED)"
    )
    default_save_subdir = "linestep_sum"

    @classmethod
    def default_params(cls) -> dict:
        return {"match": MATCH_SETTINGS}

    @classmethod
    def param_spec(cls) -> tuple:
        return (
            ParamField(
                "match", "select", MATCH_SETTINGS, label="Group line steps by",
                options=MATCH_OPTIONS,
                help="Line steps are summed when they agree on this. 'On/off, power "
                     "and timing' also keeps apart steps that switch on the same "
                     "devices at a different power, pulse timing, sequence program "
                     "or positioner move.",
            ),
        )

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        return _LineStepSumParamsWidget(parent)

    def make_metadata_dialog(self, parent: QtWidgets.QWidget):
        return None

    # ---------------------------------------------------------- inspection
    def _locate(self, data_obj: "DataObj") -> tuple[list[str], int, str | None]:
        """Storage axes, the condition axis, and the detector, from the layout."""
        try:
            resolved = data_obj.acquisition_layout
        except Exception as error:
            raise LineStepSumError(
                f"The acquisition of this file could not be resolved: {error}"
            ) from error
        layout = getattr(resolved, "layout", None)
        if layout is None:
            raise LineStepSumError("This file does not describe its acquisition.")
        axes = list(layout.storage_axes)
        if "condition" not in axes:
            if any(loop.kind == "condition" for loop in layout.event_loops):
                raise LineStepSumError(
                    "This detector recorded its line steps as separate frames, not "
                    "as line-step planes. Line-step sum combines the planes of a "
                    "scan-driven point detector (APD, PMT)."
                )
            raise LineStepSumError(
                "This recording has no line-step axis: the scan used a single line "
                "step, or it was not recorded with the Advanced Scan."
            )
        return axes, axes.index("condition"), layout.detector

    def plan(self, data_obj: "DataObj", match: str = MATCH_SETTINGS) -> LineStepPlan:
        """Group ``data_obj``'s line steps without reading any pixels."""
        axes, condition_axis, detector = self._locate(data_obj)
        shape = tuple(np.shape(data_obj.data_handle))
        if len(shape) != len(axes):
            raise LineStepSumError(
                f"The stored data has {len(shape)} axes but its layout names "
                f"{len(axes)} ({', '.join(axes)})."
            )
        return group_line_steps(
            data_obj.attrs, shape[condition_axis], detector=detector, match=match,
        )

    def inspect_source(self, data_obj: "DataObj"):
        try:
            plan = self.plan(data_obj)
        except Exception as error:
            return SourceInspection(
                source_kind=getattr(data_obj, "sourceKind", "image"), warning=str(error),
            )
        return SourceInspection(
            source_kind=getattr(data_obj, "sourceKind", "image"),
            metadata={
                "line_steps": plan.n_steps,
                "line_step_images": len(plan.groups),
                "line_step_groups": plan.describe(),
            },
        )

    # ------------------------------------------------------------- process
    def process(self, data_obj: "DataObj", params: dict,
                context: ReconstructionContext | None = None) -> LineStepSumResult:
        p = dict(self.default_params())
        p.update(params or {})
        match = str(p["match"])
        if context is not None:
            context.report("inspect", 0, 1, "reading line-step metadata")
        axes, condition_axis, detector = self._locate(data_obj)

        virtual = (
            getattr(data_obj, "sourceLoaded", False)
            and not getattr(data_obj, "dataLoaded", False)
            and getattr(data_obj, "data_handle", None) is not None
        )
        preloaded = getattr(data_obj, "dataLoaded", False)
        try:
            if virtual:
                data = data_obj.data_handle
            else:
                data_obj.checkAndLoadData()
                data = data_obj.data
            if len(data.shape) != len(axes):
                raise LineStepSumError(
                    f"The stored data has {len(data.shape)} axes but its layout names "
                    f"{len(axes)} ({', '.join(axes)})."
                )
            plan = group_line_steps(
                data_obj.attrs, data.shape[condition_axis], detector=detector, match=match,
            )
            summed = sum_line_steps(data, condition_axis, plan.groups, context)
            raw_shape = tuple(data.shape)
            scales = list(data_obj.axis_scales or [])
            unit = data_obj.scale_unit or "px"
        finally:
            if not virtual and not preloaded:
                data_obj.checkAndUnloadData()

        if len(scales) != len(axes):
            scales, unit = [1.0] * len(axes), "px"
        labels = [_AXIS_NAMES.get(axis, axis.capitalize()) for axis in axes]
        scales[condition_axis] = 1.0
        # A single scan frame is not a time axis worth a slider.
        drop = [axis for axis, role in enumerate(axes)
                if role == "frame" and summed.shape[axis] == 1]
        if drop:
            summed = np.squeeze(summed, axis=tuple(drop))
            labels = [label for axis, label in enumerate(labels) if axis not in drop]
            scales = [scale for axis, scale in enumerate(scales) if axis not in drop]
            condition_axis -= sum(1 for axis in drop if axis < condition_axis)

        if context is not None:
            context.report("finalize", 1, 1, plan.describe())
        dataset_uid = _source_identity(data_obj)
        identity: dict[str, Any] = {"identity_kind": "derived"}
        if dataset_uid is not None:
            identity.update(
                dataset_uid=dataset_uid,
                result_uid=content_digest_uid("result", dataset_uid, "linestep-sum", match),
                # The summed images sit on the raw scan's pixel grid, so an ROI
                # drawn on the View only image measures the same pixels here.
                coordinate_space_uid=content_digest_uid("space", dataset_uid, raw_shape[-2:]),
            )
        return LineStepSumResult(
            f"{data_obj.name} line-step sum",
            summed,
            labels,
            view_modes=[ViewMode("Standard", tuple(range(summed.ndim)))],
            axis_scales=scales,
            scale_unit=unit,
            plan=plan,
            channel_axis=condition_axis,
            source_name=str(data_obj.name),
            match=match,
            **identity,
        )


__all__ = [
    "LineStepGroup",
    "LineStepPlan",
    "LineStepSumError",
    "LineStepSumReconstructor",
    "LineStepSumResult",
    "MATCH_ONOFF",
    "MATCH_OPTIONS",
    "MATCH_SETTINGS",
    "group_line_steps",
    "sum_dtype",
    "sum_line_steps",
]
