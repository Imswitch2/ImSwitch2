"""The pipeline step kinds as the workflow editor sees them.

:func:`pipeline_descriptors` turns the five kinds of :mod:`.steps` into
:class:`~imswitch.imcommon.model.workfloweditor.catalog.StepDescriptor`
entries for a :class:`~imswitch.imcommon.model.workfloweditor.catalog.Catalog`:
what each is called, its own fields (a live source's detector, a gate's
rule), the parameters it takes (a gate's depend on its rule, an action's on
the catalogue entry), its arity and ports, and how a fresh one is made. The
host fills in what only it knows -- the detector names, the transform ids,
the action catalogue -- so the forms offer choices rather than free text.
"""

from __future__ import annotations

from imswitch.imcommon.model.param_spec import ParamField, spec_from_defaults
from imswitch.imcommon.model.smartpipeline.actions import ActionCatalog
from imswitch.imcommon.model.smartpipeline.steps import (
    DONE_PORT,
    FIRED_PORT,
    GATE_RULES,
    LIVE_MODES,
    SINK_KINDS,
    Action,
    Gate,
    Live,
    Sink,
    Transform,
)
from imswitch.imcommon.model.workflow.steps import DEFAULT_PORT, SOURCE_PORT
from imswitch.imcommon.model.workfloweditor.catalog import StepDescriptor

PIPELINE_CATEGORY = "Pipeline"


def _select_or_text(key: str, default: str, options, *, label: str, help: str = "") -> ParamField:
    """A choice when the host knows the options, free text otherwise."""
    options = tuple(str(o) for o in options)
    if options:
        return ParamField(key, "select", default if default in options else options[0], label=label,
                          help=help, options=options)
    return ParamField(key, "text", default, label=label, help=help)


_GATE_FIELDS = {
    "condition": (
        ParamField("min_count", "int", 1, label="At least", help="Points needed for the gate to open", min=0),
        ParamField("min", "float", None, label="Minimum", help="A value below this closes the gate", nullable=True),
        ParamField("max", "float", None, label="Maximum", help="A value above this closes the gate", nullable=True),
        ParamField("invert", "bool", False, label="Invert", help="Open when the condition does NOT hold"),
    ),
    "min-interval": (
        ParamField("seconds", "float", 1.0, label="At most every", suffix="s", min=1e-3, decimals=3),
    ),
    "count-limit": (
        ParamField("count", "int", 1, label="Times", help="Opens this many times, then stays closed", min=1),
    ),
    "once": (),
    "busy": (),
}


def gate_fields(step) -> tuple[ParamField, ...]:
    """The parameters of a gate, for its rule."""
    return _GATE_FIELDS.get(str(getattr(step, "rule", "condition")), ())


def pipeline_descriptors(*, detectors=(), transforms=(), actions: ActionCatalog | None = None) -> tuple[StepDescriptor, ...]:
    """Descriptors for ``live``, ``transform``, ``gate``, ``action`` and ``sink``.

    ``detectors`` are the detector names the live source may pick from,
    ``transforms`` the transform ids (a registry's ``ids()`` or any names),
    ``actions`` the action catalogue; without them the forms take free text.
    """
    detectors = tuple(str(d) for d in detectors)
    transform_ids = tuple(str(t) for t in transforms)
    action_ids = tuple(actions.ids()) if actions is not None else ()

    def make_live(step_id, refs):
        return Live(step_id, detector=detectors[0] if detectors else "")

    def make_transform(step_id, refs):
        return Transform(step_id, transform=transform_ids[0] if transform_ids else "identity", inputs=refs)

    def make_gate(step_id, refs):
        return Gate(step_id, "condition", inputs=refs)

    def make_action(step_id, refs):
        return Action(step_id, action=action_ids[0] if action_ids else "log", inputs=refs)

    def make_sink(step_id, refs):
        return Sink(step_id, "event-log", inputs=refs)

    def action_fields(step) -> tuple[ParamField, ...]:
        spec = actions.get(getattr(step, "action", "")) if actions is not None else None
        if spec is None:
            return spec_from_defaults(dict(getattr(step, "params", {}) or {}))
        return spec_from_defaults(spec.params)

    action_help = "; ".join(f"{spec.id}: {spec.description}" for spec in (actions.specs() if actions else ())
                            if spec.description)
    return (
        StepDescriptor(
            "live", "Live detector", PIPELINE_CATEGORY,
            "Frames from a detector, as they come or on a clock. The pipeline's source.",
            attributes=(
                _select_or_text("detector", "", detectors, label="Detector"),
                ParamField("mode", "select", "live", label="Mode", options=LIVE_MODES,
                           help="live: every frame the detector delivers; clock: one every interval"),
                ParamField("interval_s", "float", 0.0, label="Interval", suffix="s", min=0.0, decimals=3,
                           help="For the clock mode; 0 in live mode"),
            ),
            min_inputs=0, max_inputs=0, ports=(SOURCE_PORT,), make=make_live,
        ),
        StepDescriptor(
            "transform", "Transform", PIPELINE_CATEGORY,
            "Points from one frame of reference into another, by a calibrated transform (identity when they already match).",
            attributes=(_select_or_text("transform", "identity", transform_ids, label="Transform",
                                        help="A calibration file's id"),),
            min_inputs=1, max_inputs=1, accepted_kinds=("points",), ports=(DEFAULT_PORT,), make=make_transform,
        ),
        StepDescriptor(
            "gate", "Gate", PIPELINE_CATEGORY,
            "Lets the tick through or not: a condition on the input, a minimum interval, once, a count, "
            "or not while an action runs. 'out' is the input unchanged; 'fired' says whether it opened.",
            attributes=(ParamField("rule", "select", "condition", label="Rule", options=GATE_RULES),),
            fields_for=gate_fields,
            min_inputs=1, max_inputs=1, ports=(DEFAULT_PORT, FIRED_PORT), make=make_gate,
        ),
        StepDescriptor(
            "action", "Action", PIPELINE_CATEGORY,
            "Hardware, from the action catalogue. The actions run one after the other, each after the one "
            "before it completed; 'done' is the outcome.",
            attributes=(
                _select_or_text("action", "", action_ids, label="Action", help=action_help),
                ParamField("timeout_s", "float", None, label="Timeout", suffix="s", min=0.0, nullable=True,
                           help="The action fails after this long without completing"),
                ParamField("on_arm", "bool", False, label="Run on arm",
                           help="Start once right after arming, before any frame (a first overview scan)"),
            ),
            fields_for=action_fields,
            min_inputs=1, max_inputs=None, ports=(DONE_PORT,), make=make_action,
        ),
        StepDescriptor(
            "sink", "Sink", PIPELINE_CATEGORY,
            "Where a value goes to be seen: the event log, or an overlay on the live view.",
            attributes=(ParamField("sink", "select", "event-log", label="Sink", options=SINK_KINDS),),
            min_inputs=1, max_inputs=1, ports=(), produces=False, make=make_sink,
        ),
    )


__all__ = ["PIPELINE_CATEGORY", "gate_fields", "pipeline_descriptors"]


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
