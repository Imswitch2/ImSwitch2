"""
ImSwitch HDF5 metadata → SNOUTY deskew parameters.

Extracts geometry and scan parameters from DataObj.attrs (the merged dict of
HDF5 root attributes + dataset attributes) and returns a dict consumable by
DeskewProcessor constructors.
"""

import logging
from typing import Any

import numpy as np

_logger = logging.getLogger(__name__)

# ImProcess defaults, originally based on Mini_Recon.
DEFAULT_PARAMS = {
    "c_px": 100.0,              # nm
    "alpha_deg": 30.0,          # degrees
    "dy": 210.0,                # nm
    "sample_vx_size": 200.0,    # nm
    "camera_offset": 100.0,     # ADU
    "flip_data": False,
    "cycles": 1,
    "planes_in_cycle": 1,
    "restack": True,
}


def recorded_snouty_geometry(data_obj: Any) -> tuple[int, int, int | None] | None:
    """``(cycles, planes_in_cycle, timepoints)`` from the resolved layout.

    One code path decides the loop structure for both the combined-array case,
    where the recording carries a ``time`` loop, and the split case, where each
    file or group holds one timepoint and time lives in the partition instead.
    The split case returns 1 timepoint for the array in hand, so the two
    storage modes reconstruct to the same coordinates.

    ``None`` when the source cannot describe itself, which leaves the widget
    and attribute values in charge.
    """
    resolved = getattr(data_obj, "acquisition_layout", None)
    layout = getattr(resolved, "layout", None)
    if layout is None or getattr(resolved, "confidence", None) == "low":
        return None
    loops = {loop.kind: loop for loop in layout.event_loops}
    cycle, plane = loops.get("cycle"), loops.get("plane")
    if cycle is None or plane is None:
        return None
    time = loops.get("time")
    return cycle.count, plane.count, time.count if time is not None else 1


def snouty_param_overrides_from_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    """Return only SNOUTY parameters explicitly represented in ``attrs``.

    The sparse result is used by the parameter widget when the selected
    dataset changes.  Keeping it sparse prevents absent metadata from
    resetting user-entered values to application defaults.

    ``ScanStage:positive_direction`` is deliberately not interpreted here:
    stage direction and the deskew processor's plane-order flip are separate
    concepts.
    """
    params: dict[str, Any] = {}
    _found: dict[str, Any] = {}   # collects what was actually read, for debug log

    _logger.debug(
        "snouty_param_overrides_from_attrs: inspecting %d attribute keys",
        len(attrs),
    )

    # ------------------------------------------------------------------
    # Camera pixel size  (c_px)
    #
    # Search priority:
    #   1. Detector:*:Camera pixel size           — scalar µm (any depth)
    #      e.g. "Detector:Orca:Camera pixel size"
    #      e.g. "Detector:Orca:Param:Camera pixel size"   ← actual key in files
    #   2. Detector:*:Pixel size                  — array µm [z, y, x]
    #      e.g. "Detector:Orca:Pixel size": array([1., 0.1, 0.1])
    #      lateral pixel = last element (x-axis)
    # ------------------------------------------------------------------
    c_px_um = None

    # Pass 1 — prefer keys whose last segment is "Camera pixel size"
    for key, value in attrs.items():
        parts = key.split(":")
        if (
            len(parts) >= 3
            and parts[0].lower() == "detector"
            and parts[-1].lower() == "camera pixel size"
        ):
            try:
                arr = np.asarray(value, dtype=float)
                c_px_um = float(arr.flat[-1])   # scalar or last element of array
                _found["c_px_key"] = key
                _found["c_px_raw_um"] = c_px_um
                break
            except (TypeError, ValueError) as exc:
                _logger.debug("  c_px: could not convert %r=%r: %s", key, value, exc)

    # Pass 2 — fallback to "Pixel size" array keys
    if c_px_um is None:
        for key, value in attrs.items():
            parts = key.split(":")
            if (
                len(parts) >= 3
                and parts[0].lower() == "detector"
                and parts[-1].lower() == "pixel size"
            ):
                try:
                    arr = np.asarray(value, dtype=float)
                    # Array is (z, y, x) pixel sizes in µm; lateral = last element
                    c_px_um = float(arr.flat[-1])
                    _found["c_px_key"] = key
                    _found["c_px_raw_um"] = c_px_um
                    _found["c_px_note"] = "array fallback — took last element"
                    break
                except (TypeError, ValueError) as exc:
                    _logger.debug("  c_px fallback: could not convert %r=%r: %s", key, value, exc)

    if c_px_um is not None:
        params["c_px"] = c_px_um * 1000.0   # µm → nm
        _found["c_px_nm"] = params["c_px"]

    # Scan step (dy): MS-RESOLFT_Scan:cycleStepSizeUm [µm → nm]
    dy_val = attrs.get("MS-RESOLFT_Scan:cycleStepSizeUm")
    if dy_val is not None:
        try:
            params["dy"] = float(dy_val) * 1000.0
            _found["dy_raw_um"] = float(dy_val)
            _found["dy_nm"] = params["dy"]
        except (TypeError, ValueError) as exc:
            _logger.debug("  dy: could not convert %r: %s", dy_val, exc)

    cycles_val = attrs.get("MS-RESOLFT_Scan:cycleSteps")
    if cycles_val is not None:
        try:
            params["cycles"] = int(cycles_val)
            _found["cycles"] = params["cycles"]
        except (TypeError, ValueError) as exc:
            _logger.debug("  cycles: could not convert %r: %s", cycles_val, exc)

    planes_val = attrs.get("MS-RESOLFT_Scan:roSteps")
    if planes_val is not None:
        try:
            params["planes_in_cycle"] = int(planes_val)
            _found["planes_in_cycle"] = params["planes_in_cycle"]
        except (TypeError, ValueError) as exc:
            _logger.debug("  planes_in_cycle: could not convert %r: %s", planes_val, exc)

    _logger.debug(
        "snouty_param_overrides_from_attrs: resolved from file: %s",
        {k: v for k, v in _found.items()},
    )
    return params


def snouty_params_from_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    """
    Extract SNOUTY deskew parameters from ImSwitch HDF5 attributes.

    Args:
        attrs: DataObj.attrs dict (merged root + dataset HDF5 attributes)

    Returns:
        Parameter dict with keys: c_px, alpha_deg, dy, sample_vx_size,
        camera_offset, flip_data, cycles, planes_in_cycle, restack.
        Missing keys are filled with the ImProcess defaults.  Dataset-change
        handling uses :func:`snouty_param_overrides_from_attrs` directly so
        missing metadata does not reset values in the widget.

    Examples:
        >>> attrs = {"Detector:Cam:Param:Camera pixel size": 0.108}
        >>> snouty_params_from_attrs(attrs)
        {'c_px': 108.0, 'alpha_deg': 30.0, ...}
    """
    params = DEFAULT_PARAMS.copy()
    params.update(snouty_param_overrides_from_attrs(attrs))
    _logger.debug(
        "snouty_params_from_attrs: final params: "
        "c_px=%.1f nm  alpha_deg=%.1f°  dy=%.1f nm  sample_vx=%.1f nm  "
        "camera_offset=%.1f ADU  flip=%s  cycles=%d  planes/cycle=%d",
        params["c_px"], params["alpha_deg"], params["dy"], params["sample_vx_size"],
        params["camera_offset"], params["flip_data"],
        params["cycles"], params["planes_in_cycle"],
    )

    return params


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
