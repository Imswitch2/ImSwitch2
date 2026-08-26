"""Small workflow policies for choosing localization-source defaults.

The numerical localizer accepts explicit ``target``, ``manual`` or ``auto``
source modes.  This module does not change that algorithm.  It only decides
which modes are sensible defaults when a new image measurement enters a host
workflow.
"""

from __future__ import annotations

from typing import Any,Mapping

from ...measurement import ImageMeasurement


_SOURCE_KEYS = (
    "period_prior_mode",
    "stagger_prior_mode",
    "lattice_size_prior_mode",
)


def suggest_localization_sources(
    measurement: ImageMeasurement,
    context: Mapping[str, Any] | None=None,
    *,
    allow_target_hints: bool=False,
) -> dict[str, str]:
    """Return source-mode defaults for one newly supplied measurement.

    Loaded files intentionally default to fully automatic localization because
    the image cannot be assumed to correspond to the currently displayed CGH.
    Detector acquisitions may prefer target-derived hints, but only when the
    host explicitly confirms that the relevant CGH result is current.

    Target mode is selected independently for period, stagger and lattice size
    so a partially available context degrades cleanly to ``auto`` per field.
    The returned mapping is suitable for the ``localization`` feedback
    parameter namespace.
    """
    if not isinstance(measurement,ImageMeasurement):
        raise TypeError("measurement must be an ImageMeasurement")

    result = {key:"auto" for key in _SOURCE_KEYS}
    if measurement.source.strip().lower() == "file" or not allow_target_hints:
        return result

    values = dict(context or {})
    if values.get("target_expected_period_px") is not None:
        result["period_prior_mode"] = "target"
    if values.get("target_stagger") is not None:
        result["stagger_prior_mode"] = "target"
    if values.get("target_lattice_count") is not None:
        result["lattice_size_prior_mode"] = "target"
    return result


__all__ = ["suggest_localization_sources"]
