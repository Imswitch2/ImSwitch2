"""Resolve optional scan-step overrides, preserving the old shared-step key.

Zero means use the recording.  Historical ``scan_step_nm`` values apply to
both lateral axes; new X/Y/Z fields override only their respective axes.
"""

from __future__ import annotations


def scan_step_overrides(params: dict) -> tuple[float, float, float]:
    shared = float(params.get("scan_step_nm", 0.0) or 0.0)
    return (
        float(params.get("scan_step_x_nm", 0.0) or 0.0) or shared,
        float(params.get("scan_step_y_nm", 0.0) or 0.0) or shared,
        float(params.get("scan_step_z_nm", 0.0) or 0.0),
    )
