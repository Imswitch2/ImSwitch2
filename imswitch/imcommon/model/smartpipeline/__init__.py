"""The smart-pipeline document: what a smart-microscopy pipeline file says.

A smart pipeline is a workflow (:mod:`imswitch.imcommon.model.workflow`) with
five more kinds of step: a ``live`` source (a detector), a ``transform``
(points from one frame of reference into another), a ``gate`` (pure logic
that lets a tick through or not), an ``action`` (hardware, through a
catalogue) and a ``sink`` (an event log, an overlay). The processing steps
in between are ImProcess's, unchanged. This package holds the vocabulary --
step classes, frame tags, the coordinate contract, the transform slot and the
action-spec types -- and the static checks on a pipeline; it imports neither
Qt nor ImProcess nor ImControl, so the editor can open a pipeline wherever
it runs. Running one is the engine's business
(``imswitch.imcontrol.model.smartpipeline``).

Design: ``docs/design/plans/smart-microscopy-pipeline-designer.md``.
"""

from imswitch.imcommon.model.smartpipeline.actions import ActionCatalog, ActionSpec
from imswitch.imcommon.model.smartpipeline.document import (
    PIPELINE_METADATA_KEY,
    PipelineSettings,
    SmartPipelineDocument,
    action_chain,
    pipeline_kinds,
    pipeline_settings,
    source_step_of,
    validate_pipeline,
)
from imswitch.imcommon.model.smartpipeline.frames import (
    FRAME_KINDS,
    UNKNOWN_FRAME,
    CoordinateError,
    FrameIdentity,
    FrameTag,
    check_points,
    fired_of,
    frame_of,
    kind_of,
    points_of,
    value_of,
)
from imswitch.imcommon.model.smartpipeline.steps import (
    GATE_RULES,
    LIVE_MODES,
    SINK_KINDS,
    Action,
    Gate,
    Live,
    Sink,
    Transform,
)
from imswitch.imcommon.model.smartpipeline.transforms import (
    CoordinateTransform,
    Identity,
    Polynomial3,
    ScaleFlip,
    ScanGeometry,
    TransformRegistry,
    apply_transform,
    transform_from_dict,
)
from imswitch.imcommon.model.smartpipeline.values import Event, Frame, Points, Value

__all__ = [
    "Action",
    "ActionCatalog",
    "ActionSpec",
    "CoordinateError",
    "CoordinateTransform",
    "Event",
    "FRAME_KINDS",
    "Frame",
    "FrameIdentity",
    "FrameTag",
    "GATE_RULES",
    "Gate",
    "Identity",
    "LIVE_MODES",
    "Live",
    "PIPELINE_METADATA_KEY",
    "PipelineSettings",
    "Points",
    "Polynomial3",
    "SINK_KINDS",
    "ScaleFlip",
    "ScanGeometry",
    "Sink",
    "SmartPipelineDocument",
    "Transform",
    "TransformRegistry",
    "UNKNOWN_FRAME",
    "Value",
    "action_chain",
    "apply_transform",
    "check_points",
    "fired_of",
    "frame_of",
    "kind_of",
    "pipeline_kinds",
    "pipeline_settings",
    "points_of",
    "source_step_of",
    "transform_from_dict",
    "validate_pipeline",
    "value_of",
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
