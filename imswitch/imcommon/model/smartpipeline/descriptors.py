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
    "delay": (
        ParamField("seconds", "float", 1.0, label="After", suffix="s", min=1e-3, decimals=3,
                   help="Opens on the first tick this long after a trigger (a fired event, points, any input)"),
    ),
    "once": (),
    "busy": (),
}


def gate_fields(step) -> tuple[ParamField, ...]:
    """The parameters of a gate, for its rule."""
    return _GATE_FIELDS.get(str(getattr(step, "rule", "condition")), ())


def action_fields_of(spec, params=None) -> tuple[ParamField, ...]:
    """The parameters of an action: its catalogue entry's declared fields
    (``spec.fields``), the others derived from their defaults; with no entry
    (an unknown action) the fields the step's own ``params`` imply."""
    if spec is None:
        return spec_from_defaults(dict(params or {}))
    declared = {field.key: field for field in (getattr(spec, "fields", None) or ())}
    return tuple(declared.get(key) or field for key, field in
                 ((key, spec_from_defaults({key: value})[0]) for key, value in spec.params.items()))


def param_field(step, key: str, actions: ActionCatalog | None = None) -> ParamField | None:
    """The field of ``step.params[key]`` for a gate or an action (what a
    workflow control exposing it shows and checks), else ``None``. The hook
    :func:`~imswitch.imcommon.model.workflow.prepare.control_states` takes as
    ``field_for`` for a pipeline."""
    if isinstance(step, Gate):
        fields = gate_fields(step)
    elif isinstance(step, Action):
        spec = actions.get(step.action) if actions is not None else None
        fields = action_fields_of(spec, step.params)
    else:
        return None
    return next((field for field in fields if field.key == key), None)


def pipeline_descriptors(*, detectors=(), transforms=(), actions: ActionCatalog | None = None) -> tuple[StepDescriptor, ...]:
    """Descriptors for ``live``, ``transform``, ``gate``, ``action`` and ``sink``.

    ``detectors`` are the detector names the live source may pick from,
    ``transforms`` the transform ids (a registry's ``ids()`` or any names),
    ``actions`` the action catalogue; without them the forms take free text.
    """
    detectors = tuple(str(d) for d in detectors)
    transform_ids = tuple(str(t) for t in transforms)
    # The actions grouped by category (Scan, Motion, Light, Devices, Recording,
    # Other), each shown as "Category › Label (id)": one list, sorted.
    grouped = [(category, spec) for category, specs in (actions.by_category() if actions is not None else ())
               for spec in specs]
    action_ids = tuple(spec.id for _category, spec in grouped)
    action_labels = tuple(f"{category} › {spec.label or spec.id} ({spec.id})" for category, spec in grouped)

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
        return action_fields_of(spec, getattr(step, "params", {}) or {})

    action_help = "\n".join(f"{category} › {spec.id}: {spec.description}" for category, spec in grouped
                             if spec.description)
    if action_ids:
        action_field = ParamField("action", "select", action_ids[0], label="Action", help=action_help,
                                  options=action_ids, option_labels=action_labels)
    else:
        action_field = ParamField("action", "text", "", label="Action", help=action_help)
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
                action_field,
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


__all__ = ["PIPELINE_CATEGORY", "action_fields_of", "gate_fields", "param_field", "pipeline_descriptors"]


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
