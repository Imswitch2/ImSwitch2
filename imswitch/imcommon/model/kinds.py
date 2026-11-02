"""The kind vocabulary: what a result, or a workflow port, carries.

Shared by every module that talks about results without touching them:
the workflow schema (:mod:`imswitch.imcommon.model.workflow`), the editor
(:mod:`imswitch.imcommon.model.workfloweditor`) and ImProcess's result
classes (:mod:`imswitch.improcess.model.result`, which re-exports these).
Pure: no numpy, no Qt.
"""

#: Semantic result kinds. ``data`` alone cannot distinguish a microscope
#: image from a metrics table with a 2D array — the kind says what the
#: values MEAN, so processors and UI can filter on semantics instead of
#: shape alone.
#:
#: - ``"image"`` — calibrated intensity image/stack (the default);
#: - ``"labels"`` — integer segmentation label mask;
#: - ``"table"`` — tabular metrics (rows x named metric columns);
#: - ``"curve"`` — sampled function values (e.g. FRC over frequency);
#: - ``"localization"`` — point-emitter coordinate table with render;
#: - ``"rgb"`` — autoscaled uint8 visualization/export product, not
#:   quantitative intensities;
#: - ``"composite"`` — multi-channel intensity stack with independently
#:   scaled display channels;
#: - ``"points"`` — ``(N, D)`` coordinates tagged with the frame of reference
#:   they are measured in (``PointsResult``): what a detection step hands to a
#:   transform or a hardware action in a smart pipeline;
#: - ``"value"`` — one number with an optional unit (``ValueResult``);
#: - ``"event"`` — a yes/no with a payload (``EventResult``): whether something
#:   was detected, and what.
RESULT_KINDS = (
    "image",
    "labels",
    "table",
    "curve",
    "localization",
    "rgb",
    "composite",
    "points",
    "value",
    "event",
)
POINTS_KIND = "points"
VALUE_KIND = "value"
EVENT_KIND = "event"


#: What a workflow *port* may carry, beyond the result kinds: a Source's
#: recording (before any reconstruction), ``any`` for a step that declares its
#: output depends on the run (the Python step), and ``unknown`` for a plugin
#: that declares nothing. Only a declared result kind is ever checked.
RECORDING_KIND = "recording"
ANY_KIND = "any"
UNKNOWN_KIND = "unknown"
PORT_KINDS = RESULT_KINDS + (RECORDING_KIND, ANY_KIND, UNKNOWN_KIND)


def result_kind(result) -> str:
    """Semantic kind of ``result``; duck-typed/legacy results count as images."""
    return str(getattr(result, "kind", "image") or "image")


__all__ = [
    "ANY_KIND",
    "EVENT_KIND",
    "POINTS_KIND",
    "PORT_KINDS",
    "RECORDING_KIND",
    "RESULT_KINDS",
    "UNKNOWN_KIND",
    "VALUE_KIND",
    "result_kind",
]


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
