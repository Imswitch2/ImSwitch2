"""Scope transient reconstruction settings to the source that resolves them.

This module deliberately does not read the Session registry or widget state:
Manual/Locked calibration is an explicit reusable user choice, whereas an
Auto result for the currently displayed image is not a global fallback.
"""

from __future__ import annotations


def fresh_acquisition_params(reconstructor, params: dict) -> dict:
    """Remove ephemeral Auto calibration when submitting an independent job.

    Called once per queued logical acquisition (not per timelapse timepoint).
    Manual and Locked values are left untouched. Mutates neither the input
    dictionary nor the UI widget's currently displayed localization.
    """
    prepared = dict(params or {})
    if (getattr(reconstructor, "id", None) == "monalisa"
            and str(prepared.get("localization_mode", "Auto")) == "Auto"):
        prepared["auto_localization"] = {}
    return prepared
