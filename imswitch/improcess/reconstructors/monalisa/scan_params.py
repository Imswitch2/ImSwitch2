"""MoNaLISA scan parameters from a recording's acquisition metadata.

This was a controller method that read the widget's axis-label strings and
mutated the controller's scan-parameter dict in place. A headless run has
no widget and no controller, but needs exactly the same answer from the
same attributes, so the logic lives here as a pure function over ``(scan
parameters, attrs, labels, frame count)``; the controller calls it.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class AxisLabels:
    """The display strings the scan-parameter dict is written in."""

    # The GUI's strings (ImProcessMainView), so a headless dict and a GUI dict
    # describe the same scan with the same words.
    r_l: str = "Right/Left"
    u_d: str = "Up/Down"
    b_f: str = "Back/Forth"
    timepoints: str = "Timepoints"
    p: str = "pos"
    n: str = "neg"


DEFAULT_LABELS = AxisLabels()


def default_scan_params(labels: AxisLabels = DEFAULT_LABELS) -> dict:
    return {
        "dimensions": [labels.u_d, labels.r_l, labels.b_f, labels.timepoints],
        "directions": [labels.p, labels.p, labels.p],
        "steps": ["35", "35", "1", "1"],
        "step_sizes": ["35", "35", "35", "1"],
        "n_linesteps": 1,
        "unidirectional": True,
    }


def positive_int_attr(attrs, key):
    """A positive scalar integer metadata value, else ``None``."""
    try:
        value = attrs[key]
        if isinstance(value, (list, tuple, np.ndarray)):
            value = np.asarray(value).flatten()
            if value.size != 1:
                return None
            value = value[0]
        if isinstance(value, (bytes, np.bytes_)):
            value = value.decode(errors="ignore")
        number = int(float(value))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return number if number > 0 else None


def apply_scan_attrs(scan_params: dict, attrs, labels: AxisLabels = DEFAULT_LABELS,
                     num_frames: int | None = None) -> dict:
    """A copy of ``scan_params`` updated from ImControl acquisition ``attrs``.

    Only recordings written by ImControl (HDF5/Zarr) carry these attributes;
    TIFF stacks and external data have none, and the input is returned
    unchanged (as a copy) in that case.
    """
    result = copy.deepcopy(scan_params)
    if not attrs:
        return result

    dimension_map = {"X": labels.r_l, "Y": labels.u_d, "Z": labels.b_f}
    try:
        targets = attrs["ScanStage:target_device"]
        for index in range(0, min(3, len(targets))):
            target = targets[index]
            if isinstance(target, (bytes, np.bytes_)):
                target = target.decode(errors="ignore")
            result["dimensions"][index] = dimension_map[str(target).upper()]
    except (KeyError, TypeError, AttributeError):
        pass

    try:
        positive = attrs["ScanStage:positive_direction"]
        for index in range(0, min(3, len(positive))):
            result["directions"][index] = labels.p if positive[index] else labels.n
    except (KeyError, TypeError):
        pass

    num_linesteps = positive_int_attr(attrs, "ScanTTL:n_linesteps") or 1
    result["n_linesteps"] = num_linesteps

    # Prefer the physical X/Y counts recorded by advanced scans. Unlike
    # sqrt(numFrames), these remain correct when every physical line is
    # repeated for multiple line-step conditions.
    spatial_steps = {
        labels.r_l: positive_int_attr(attrs, "ScanTTL:Nx"),
        labels.u_d: positive_int_attr(attrs, "ScanTTL:Ny"),
    }
    for index, dimension in enumerate(result["dimensions"][:3]):
        if spatial_steps.get(dimension) is not None:
            result["steps"][index] = str(spatial_steps[dimension])

    # Older recordings may lack ScanTTL:Nx/Ny but still contain physical axis
    # lengths and pitches. Their convention is positions=length/step.
    try:
        axis_lengths = np.asarray(attrs["ScanStage:axis_length"], dtype=float).flatten()
        axis_steps = np.asarray(attrs["ScanStage:axis_step_size"], dtype=float).flatten()
    except (KeyError, TypeError, ValueError):
        axis_lengths = axis_steps = np.array([])
    for index in range(min(3, axis_lengths.size, axis_steps.size)):
        if spatial_steps.get(result["dimensions"][index]) is not None:
            continue
        if axis_steps[index] != 0:
            count = max(1, int(round(abs(axis_lengths[index] / axis_steps[index]))))
            result["steps"][index] = str(count)

    try:
        frames = int(num_frames) if num_frames is not None else None
    except Exception:
        frames = None
    if frames:
        spatial_product = int(np.prod(np.asarray(result["steps"][:3], dtype=int)))
        if spatial_product <= 0 or frames % spatial_product != 0:
            # Last-resort compatibility for metadata-light square scans.
            frames_per_condition = (
                frames // num_linesteps if frames % num_linesteps == 0 else frames
            )
            side = int(np.sqrt(frames_per_condition))
            if side * side == frames_per_condition:
                result["steps"][0] = str(side)
                result["steps"][1] = str(side)
                spatial_product = side * side * int(result["steps"][2])

        if spatial_product > 0 and frames % spatial_product == 0:
            output_time_steps = frames // spatial_product
            result["steps"][3] = str(output_time_steps)
            if output_time_steps % num_linesteps != 0:
                # The metadata cannot describe this detector's frame stream;
                # retain legacy ordering rather than mis-group it.
                result["n_linesteps"] = 1

    try:
        step_sizes = attrs["ScanStage:axis_step_size"]
    except (KeyError, TypeError):
        pass
    else:
        for index in range(0, min(4, len(step_sizes))):
            result["step_sizes"][index] = str(step_sizes[index] * 1000)  # um -> nm

    return result


def scan_params_for_source(scan_params: dict, data_obj,
                           labels: AxisLabels = DEFAULT_LABELS) -> dict:
    """A copy of ``scan_params`` filled for ``data_obj``: layout first, attributes second.

    The resolved acquisition layout already knows the real counts, pitches and
    directions (``scan_params_from_layout``); the ``ScanStage:*``/``ScanTTL:*``
    attributes are re-parsed only when it cannot answer. One function for the
    GUI dialog and the headless runs, so the two cannot disagree about a file.
    """
    from .scan_geometry import scan_params_from_layout

    try:
        resolved = getattr(data_obj, "acquisition_layout", None)
        values = scan_params_from_layout(resolved, {
            "r_l_text": labels.r_l, "u_d_text": labels.u_d, "b_f_text": labels.b_f,
            "timepoints_text": labels.timepoints, "p_text": labels.p, "n_text": labels.n,
        })
    except Exception:
        # Pre-filling scan parameters must never stop a file from opening.
        values = None
    if values is not None:
        result = copy.deepcopy(scan_params)
        result.update(values)
        return result
    try:
        frames = int(data_obj.numFrames)
    except Exception:
        frames = None
    return apply_scan_attrs(scan_params, getattr(data_obj, "attrs", None) or {}, labels, frames)


__all__ = ["DEFAULT_LABELS", "AxisLabels", "apply_scan_attrs", "default_scan_params", "scan_params_for_source", "positive_int_attr"]


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
