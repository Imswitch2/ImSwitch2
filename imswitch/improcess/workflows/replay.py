"""From a saved file back to a workflow that makes it again.

Every file ImProcess writes carries its provenance graph: the source, the
reconstruction with its parameters, every processing step with its
parameters, input ports and ROI restriction, and which node the file is.
That is a workflow in all but shape. This module reshapes it: ancestors of
the file's node, in dependency order, become ``Source`` / ``Reconstruct``
/ ``Consolidate`` / ``Process`` steps, and a ``Save`` step writes the
output again -- somewhere the caller chooses, never over the original.

Like the Fiji macro recorder, except nothing had to be recording: the
record is in the file. Where the record is incomplete (a node marked
non-replayable, a plugin that is not installed, a file from before the
graph existed) this refuses with the reasons, and the LLM route described
in the docs is for filling those gaps by hand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from imswitch.improcess.model.provenance import (
    DEFAULT_PORT,
    decode_strict,
    output_of,
    validate_graph,
)
from imswitch.improcess.model.save_protocol import ProvenanceDocument
from imswitch.improcess.workflows.sources import SourceSpec
from imswitch.improcess.workflows.steps import (
    Consolidate,
    Process,
    Reconstruct,
    Ref,
    Save,
    Source,
    Workflow,
)


class ReplayError(ValueError):
    """The recorded provenance cannot be turned into a runnable workflow.

    ``boundaries`` are the provenance node ids of interactive steps (an
    external import, an opaque origin) that stopped the replay; ``blocking``
    are the reasons that are NOT such a step (a missing plugin, a ROI
    restriction by reference, parameters that cannot be replayed). A caller
    may retry with ``allow_inputs=True`` exactly when :attr:`only_boundaries`:
    structured on purpose, so nobody has to match message text.
    """

    def __init__(self, message: str, reasons: list[str] | None = None, *,
                 boundaries: list[str] | None = None, blocking: list[str] | None = None):
        super().__init__(message)
        self.reasons = list(reasons or [])
        self.boundaries = list(boundaries or [])
        self.blocking = list(self.reasons if blocking is None and not self.boundaries else (blocking or []))

    @property
    def only_boundaries(self) -> bool:
        """Every reason is an interactive step: the rest of the recipe is sound."""
        return bool(self.boundaries) and not self.blocking


@dataclass
class ReplayResult:
    workflow: Workflow
    warnings: list[str] = field(default_factory=list)
    node_ids: dict[str, str] = field(default_factory=dict)   # step id -> provenance node id
    #: ``(source step id, what to bind)`` for each input the user must bind to
    #: run the workflow: the stand-ins for interactive steps (``allow_inputs``).
    inputs: list[tuple[str, str]] = field(default_factory=list)
    #: The warnings that are about interactive steps left out (a subset of ``warnings``).
    interactive: list[str] = field(default_factory=list)


#: Non-replayable ops that are an *interactive step* rather than a broken
#: recipe: work done by hand in another program (``external-import``, and
#: ``napari-import`` as older files call it) or whose origin was never
#: recorded (``opaque``). With ``allow_inputs`` each becomes an input.
BOUNDARY_OPS = ("external-import", "napari-import", "opaque")


_PREFIX = {"source": "src", "reconstruct": "rec", "consolidate": "cons", "process": "proc"}


class _Skip(Exception):
    """This node depends on one that could not be replayed."""


def _ancestors(graph: dict, node_id: str) -> list[str]:
    """Node ids reachable backwards from ``node_id``, in dependency order."""
    nodes = graph["nodes"]
    order: list[str] = []
    done: set[str] = set()
    # Iterative post-order: a validated graph may hold thousands of nodes in
    # one chain, and Python's recursion limit is not part of the contract.
    stack: list[tuple[str, bool]] = [(node_id, False)]
    while stack:
        current, expanded = stack.pop()
        if current in done:
            continue
        if expanded:
            done.add(current)
            order.append(current)
            continue
        stack.append((current, True))
        for ref in reversed(nodes[current].get("inputs") or []):
            child = str(ref["node"])
            if child not in done:
                stack.append((child, False))
    return order


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]+", "-", str(text)).strip("-") or "step"


def _decode(node: dict, plugin, warnings: list[str], label: str) -> dict:
    """The node's parameters as the plugin takes them, migrated if need be."""
    encoded = dict(node.get("params") or {})
    recorded_version = int(node.get("params_version", 1) or 1)
    if plugin is None:
        return decode_strict(encoded)
    current = int(getattr(type(plugin), "params_version", 1) or 1)
    if recorded_version > current:
        raise ReplayError(
            f"{label}: parameters were recorded with {node.get('plugin_id')!r} "
            f"params_version {recorded_version}, newer than the installed {current}"
        )
    if recorded_version < current:
        encoded = plugin.migrate_params(encoded, recorded_version)
        warnings.append(
            f"{label}: parameters migrated from params_version {recorded_version} to {current}"
        )
    recorded_plugin_version = str(node.get("plugin_version") or "")
    installed = str(getattr(plugin, "version", "") or "")
    if recorded_plugin_version and installed and recorded_plugin_version != installed:
        warnings.append(
            f"{label}: {node.get('plugin_id')!r} was version {recorded_plugin_version}, "
            f"installed is {installed}"
        )
    return plugin.decode_params(encoded)


class _Builder:
    """Turns the ancestors of one node into steps, skipping what cannot run."""

    def __init__(self, graph: dict, registry, *, allow_inputs: bool = False, fed_fmt: str = "tiff"):
        self.graph = graph
        self.nodes = graph["nodes"]
        self.registry = registry
        self.allow_inputs = allow_inputs
        self.fed_fmt = fed_fmt
        self.warnings: list[str] = []
        self.reasons: list[str] = []
        #: Boundary nodes met in strict mode, and the reasons that are not boundaries.
        self.boundary_nodes: list[str] = []
        self.boundary_reasons: list[str] = []
        #: Nodes replaced by an input ``Source`` (allow_inputs), and the inputs made.
        self.boundary_sources: set[str] = set()
        self.inputs: list[tuple[str, str]] = []
        self.interactive: list[str] = []
        self.saved_for_inputs = 0
        self.terminal_boundary = False
        self.used_ids: set[str] = set()
        self.step_ids: dict[str, str] = {}
        self.skipped: set[str] = set()
        self.counters: dict[str, int] = {}
        self.steps: list = []

    def step_id_for(self, node_id: str, op: str) -> str:
        self.counters[op] = self.counters.get(op, 0) + 1
        sid = f"{_PREFIX.get(op, op)}{self.counters[op]}"
        self.step_ids[node_id] = sid
        return sid

    def ref_for(self, ref: dict) -> Ref:
        target = str(ref["node"])
        port = str(ref.get("port") or DEFAULT_PORT)
        if target in self.skipped:
            raise _Skip(target)
        if target not in self.step_ids:
            raise ReplayError(f"node {target!r} is referenced before it is defined")
        if self.nodes[target].get("op") == "source" or target in self.boundary_sources:
            return Ref(self.step_ids[target], None)
        return Ref(self.step_ids[target], port)

    def build(self, out_id: str) -> None:
        for node_id in _ancestors(self.graph, out_id):
            node = self.nodes[node_id]
            op = str(node.get("op"))
            label = f"{op} {node_id[-8:]}"
            if not node.get("replayable", True) and op != "source":
                if op in BOUNDARY_OPS and self.allow_inputs:
                    self.boundary_to_input(node_id, node, is_output=node_id == out_id)
                    continue
                for reason in node.get("reasons") or ["marked non-replayable"]:
                    text = f"{label} ({node.get('plugin_id') or op}): {reason}"
                    self.reasons.append(text)
                    if op in BOUNDARY_OPS:
                        self.boundary_reasons.append(text)
                if op in BOUNDARY_OPS:
                    self.boundary_nodes.append(node_id)
                self.skipped.add(node_id)
                continue
            try:
                self.emit(node_id, node, op, label)
            except _Skip:
                # Its own record is fine; an ancestor's is not, and that
                # ancestor's reason is already listed.
                self.skipped.add(node_id)

    # -- interactive steps become inputs ---------------------------------------

    def _unique(self, base: str) -> str:
        taken = self.used_ids | {step.id for step in self.steps}
        candidate, number = base, 1
        while candidate in taken:
            number += 1
            candidate = f"{base}{number}"
        self.used_ids.add(candidate)
        return candidate

    @staticmethod
    def _tool_of(node: dict) -> str:
        if node.get("op") == "opaque":
            return ""
        return str(node.get("tool") or ("napari" if node.get("op") == "napari-import" else "external"))

    def _describe_boundary(self, node: dict, tool: str) -> tuple[str, str]:
        """``(what the step was, what to bind)`` for the warning and the input."""
        if not tool:
            what = str(node.get("label") or "a step with no recorded origin")
            return what, f"{what}; bind a file or a result"
        details = node.get("details") if isinstance(node.get("details"), dict) else {}
        layer = node.get("layer_name")
        if tool == "fiji":
            steps = len(((details.get("fiji_history") or {}).get("steps")) or [])
            known = [f"Fiji image {details['image_id']}"] if "image_id" in details else []
            if steps:
                known.append(f"{steps} recorded Fiji step{'' if steps == 1 else 's'}")
            what = "The Fiji step" + (f" ({', '.join(known)})" if known else "")
            made = f"the image produced in Fiji (was {layer!r}" if layer else "the image produced in Fiji (was unnamed"
            if "image_id" in details:
                made += f", Fiji image {details['image_id']}"
            return what, made + "); bind a file or a result"
        plugin = node.get("plugin_name") or tool
        widget = node.get("widget_name")
        known = [f"plugin {plugin!r}"] + ([f"widget {widget!r}"] if widget else []) + ([f"layer {layer!r}"] if layer else [])
        what = f"The {tool} step ({', '.join(known)})"
        return what, f"the result produced in {tool} by {plugin!r}; bind a file or a result"

    def _interactive(self, text: str) -> None:
        self.warnings.append(text)
        self.interactive.append(text)

    def boundary_to_input(self, node_id: str, node: dict, *, is_output: bool) -> None:
        """Replace an interactive step by an input the user binds at run time.

        What FED the step (its inputs' ancestry, e.g. the image sent to Fiji)
        was replayable and is already emitted; its last result gets its own
        ``Save`` so a run reproduces "the image you sent". The step itself
        becomes a path-less ``Source`` that everything downstream reads from,
        unless it was the final result, in which case nothing follows it.
        """
        tool = self._tool_of(node)
        if tool:
            key = f"{re.sub(r'[^a-z0-9]+', '_', tool.lower()).strip('_') or 'external'}"
            input_id = self._unique(f"from_{key}")
            suffix = input_id[len("from_"):]
        else:
            input_id = self._unique("input")
            suffix = input_id
        what, how = self._describe_boundary(node, tool)
        saves = []
        for ref in node.get("inputs") or []:
            target = str(ref["node"])
            if target not in self.step_ids or target in self.boundary_sources:
                continue            # nothing replayable fed it, or it is itself an input
            if self.nodes[target].get("op") == "source":
                continue            # the raw recording is already the user's file
            port = str(ref.get("port") or DEFAULT_PORT)
            sid = self._unique(f"save_for_{suffix}")
            saves.append(Save(sid, Ref(self.step_ids[target], port), fmt=self.fed_fmt,
                              path_template="{out_dir}/{source_stem}_for_" + suffix + "{ext}"))
        self.steps.extend(saves)
        self.saved_for_inputs += len(saves)
        shown = "Fiji" if tool == "fiji" else tool
        sent = "saves what was sent to " + (shown or "it") + " and " if saves else ""
        if is_output:
            self.terminal_boundary = True
            where = shown or "a step with no recorded origin"
            detail = what[what.index("("):] if "(" in what else ""
            if saves:
                self._interactive(
                    f"The last step happened in {where}{' ' + detail if detail else ''}; a workflow cannot "
                    f"contain it, so the workflow stops at what was sent to {shown or 'it'} (saved as "
                    f"{saves[0].path_template.split('/')[-1]})."
                )
            else:
                self._interactive(
                    f"The last step happened in {where}{' ' + detail if detail else ''}; a workflow cannot "
                    "contain it and nothing replayable was sent to it."
                )
            return
        self.steps.append(Source(input_id, SourceSpec(metadata={"input": how})))
        self.step_ids[node_id] = input_id
        self.boundary_sources.add(node_id)
        self.inputs.append((input_id, how))
        self._interactive(
            f"{what} cannot be part of a workflow; the workflow {sent}starts again from an input "
            f"{input_id!r} you bind when running it."
        )

    def emit(self, node_id: str, node: dict, op: str, label: str) -> None:
        if op == "source":
            description = node.get("source") or {}
            sid = self.step_id_for(node_id, op)
            fingerprint = dict(description.get("fingerprint") or {})
            self.steps.append(Source(sid, SourceSpec(
                path=description.get("path"),
                dataset=description.get("dataset"),
                source_kind="tiling-manifest" if fingerprint.get("manifest") else "auto",
                metadata={"name": description.get("name") or "",
                          "recorded_kind": str(description.get("kind") or "file")},
                fingerprint=fingerprint,
            )))
            return
        if op in ("reconstruct", "consolidate"):
            plugin_id = str(node.get("plugin_id") or "")
            plugin = self.registry.get_reconstructor(plugin_id, raise_on_missing=False) if self.registry else None
            if self.registry is not None and plugin is None:
                self.reasons.append(f"{label}: reconstructor {plugin_id!r} is not installed")
                self.skipped.add(node_id)
                return
            refs = [self.ref_for(r) for r in node.get("inputs") or []]
            params = _decode(node, plugin, self.warnings, label)
            sid = self.step_id_for(node_id, op)
            if op == "reconstruct":
                if node.get("mode") == "streaming":
                    self.warnings.append(f"{label}: recorded from a live stream; replayed as a batch reconstruction")
                self.steps.append(Reconstruct(sid, plugin_id, params, inputs=refs))
            else:
                self.steps.append(Consolidate(sid, plugin_id, inputs=refs, params=params))
            return
        if op == "process":
            plugin_id = str(node.get("plugin_id") or "")
            plugin = self.registry.get_processor(plugin_id, raise_on_missing=False) if self.registry else None
            if self.registry is not None and plugin is None:
                self.reasons.append(f"{label}: processor {plugin_id!r} is not installed")
                self.skipped.add(node_id)
                return
            refs = [self.ref_for(r) for r in node.get("inputs") or []]
            params = _decode(node, plugin, self.warnings, label)
            restriction = node.get("restriction")
            if isinstance(restriction, dict):
                restriction = {k: v for k, v in restriction.items() if k != "summary"}
                if restriction.get("by_reference"):
                    self.reasons.append(f"{label}: the ROI restriction was recorded by reference only")
                    self.skipped.add(node_id)
                    return
            else:
                restriction = None
            sid = self.step_id_for(node_id, op)
            self.steps.append(Process(sid, plugin_id, params, inputs=refs, restriction=restriction))
            return
        self.reasons.append(f"{label}: {op} steps cannot be replayed")
        self.skipped.add(node_id)


def workflow_from_provenance(
    document: ProvenanceDocument | dict,
    *,
    registry=None,
    name: str | None = None,
    save_fmt: str | None = None,
    save_template: str | None = None,
    allow_inputs: bool = False,
) -> ReplayResult:
    """A :class:`Workflow` that reproduces the file ``document`` describes.

    ``registry`` (optional) lets parameters be decoded by the plugins' own
    codecs, migrated across ``params_version`` changes, and lets missing
    plugins be reported here instead of at run time.

    By default any non-replayable ancestor refuses the whole replay. With
    ``allow_inputs`` an *interactive step* (an import from Fiji or a napari
    plugin, a result with no recorded origin) is instead replaced by an
    **input** the user binds when running: everything except that step
    becomes the workflow, what fed it is saved as ``..._for_<tool>``, and
    each such step is a line in :attr:`ReplayResult.warnings`. A broken
    recipe (a missing plugin, a ROI recorded by reference) still refuses.
    """
    if isinstance(document, dict):
        document = ProvenanceDocument.from_dict(document)
    if document.graph is None:
        return _from_history(document, registry=registry, name=name, save_fmt=save_fmt)
    graph = validate_graph(document.graph)
    out_id, out_port = output_of(graph)

    fmt = save_fmt or str((document.artifact or {}).get("fmt") or "tiff")
    builder = _Builder(graph, registry, allow_inputs=allow_inputs, fed_fmt=fmt)
    builder.build(out_id)
    if builder.reasons:
        raise ReplayError(
            "the recorded run cannot be replayed:\n  " + "\n  ".join(builder.reasons)
            + "\n(see the LLM guide in docs/improcess-workflows.rst for filling the gaps)",
            builder.reasons,
            boundaries=builder.boundary_nodes,
            blocking=[r for r in builder.reasons if r not in builder.boundary_reasons],
        )
    if builder.terminal_boundary:
        if not builder.saved_for_inputs:
            raise ReplayError(
                "the result was produced interactively from a recording directly, so there is "
                "nothing between the recording and that step to put in a workflow",
                ["nothing replayable precedes the interactive step"],
            )
    else:
        if out_id not in builder.step_ids:
            raise ReplayError("the file's own node was not replayable")
        output_is_source = graph["nodes"][out_id].get("op") == "source"
        output_ref = Ref(builder.step_ids[out_id], None if output_is_source else out_port)
        builder.steps.append(Save("save1", input=output_ref, fmt=fmt,
                                  path_template=save_template or "{out_dir}/{source_stem}_replay{ext}"))
    if allow_inputs:
        used = {ref.step for step in builder.steps for ref in (
            [step.input] if isinstance(step, Save) else list(getattr(step, "inputs", []) or []))}
        # A raw recording that only fed an interactive step is not an input of the workflow.
        builder.steps = [s for s in builder.steps if not isinstance(s, Source) or s.id in used]

    warnings = list(builder.warnings)
    recorded_version = str(graph.get("imswitch_version") or "")
    from imswitch.improcess.model.plugin_versions import imswitch_version

    if recorded_version and recorded_version != imswitch_version():
        warnings.append(f"recorded with ImSwitch {recorded_version}, this is {imswitch_version()}")

    artifact = dict(document.artifact or {})
    kept = {step.id for step in builder.steps}
    node_ids = {sid: nid for nid, sid in builder.step_ids.items() if sid in kept}
    workflow = Workflow(
        name or f"replay-{_slug(artifact.get('primary') or out_id[-8:])}",
        builder.steps,
        description="replayed from the provenance of " + str(artifact.get("primary") or "a result"),
        metadata={
            "replayed_from": {"artifact": artifact, "imswitch_version": recorded_version},
            "provenance_nodes": dict(node_ids),
        },
    )
    return ReplayResult(workflow=workflow, warnings=warnings, node_ids=node_ids, inputs=list(builder.inputs),
                        interactive=list(builder.interactive))


def workflow_from_provenance_around_interactive_steps(document, **kwargs) -> ReplayResult:
    """Strict replay; if only interactive steps stood in the way, replay around them.

    The two-step policy every caller wants (export, edit): a recipe that is
    broken for any other reason still refuses with its reasons, while one
    whose only gaps are steps done by hand in Fiji or a napari plugin becomes
    a workflow with those steps as inputs, and the warnings say so.
    """
    try:
        return workflow_from_provenance(document, **kwargs)
    except ReplayError as exc:
        if not exc.only_boundaries:
            raise
    return workflow_from_provenance(document, allow_inputs=True, **kwargs)


def _from_history(document: ProvenanceDocument, *, registry, name, save_fmt) -> ReplayResult:
    """Best effort for a file that carries only the linear history (schema 0).

    The history names the processor steps and their (possibly summarised)
    parameters but not the source or the reconstruction. A source stub and,
    when the first step is not a reconstruction, a view-only reconstruction
    are emitted, and every gap is a warning; a step whose plugin cannot be
    found is a refusal.
    """
    history = list(document.history or [])
    if not history:
        raise ReplayError("the file carries no provenance at all", ["no provenance"])
    warnings = ["schema-0 file: the source and the reconstruction were not recorded; "
                "the source step is unbound and must be provided"]
    steps: list = [Source("src1")]
    previous = Ref("src1", None)
    counters = {"rec": 0, "proc": 0}
    reasons: list[str] = []
    for index, entry in enumerate(history):
        op = str(entry.get("operation") or "")
        params = dict(entry.get("params") or {})
        had_region = "region" in params
        params.pop("region", None)
        is_recon = registry is not None and registry.get_reconstructor(op, raise_on_missing=False) is not None
        is_proc = registry is None or registry.get_processor(op, raise_on_missing=False) is not None
        if is_recon:
            counters["rec"] += 1
            sid = f"rec{counters['rec']}"
            steps.append(Reconstruct(sid, op, params, inputs=[previous]))
            previous = Ref(sid, DEFAULT_PORT)
            continue
        if not is_proc:
            reasons.append(f"history step {index + 1}: {op!r} is neither an installed reconstructor nor processor")
            continue
        if counters["rec"] == 0:
            steps.append(Reconstruct("rec1", "view-only", {}, inputs=[previous]))
            counters["rec"] = 1
            previous = Ref("rec1", DEFAULT_PORT)
            warnings.append("no reconstruction was recorded; a view-only reconstruction was assumed")
        counters["proc"] += 1
        sid = f"proc{counters['proc']}"
        if had_region:
            warnings.append(f"{sid}: the recorded run was restricted to an ROI whose geometry was not kept")
        steps.append(Process(sid, op, params, inputs=[previous]))
        previous = Ref(sid, DEFAULT_PORT)
    if reasons:
        raise ReplayError("the recorded history cannot be replayed:\n  " + "\n  ".join(reasons), reasons)
    steps.append(Save("save1", input=previous, fmt=save_fmt or "tiff",
                      path_template="{out_dir}/{source_stem}_replay{ext}"))
    workflow = Workflow(name or "replay-history", steps,
                        description="reconstructed from a schema-0 processing history; check the source",
                        metadata={"replayed_from": {"schema": 0}})
    return ReplayResult(workflow=workflow, warnings=warnings)


def workflow_from_file(path, *, registry=None, name: str | None = None, save_fmt: str | None = None) -> ReplayResult:
    """Read the provenance of ``path`` and turn it into a workflow."""
    from imswitch.improcess.model.provenance_io import read_provenance

    document = read_provenance(path)
    result = workflow_from_provenance(
        document, registry=registry,
        name=name or f"replay-{_slug(Path(path).name.split('.')[0])}", save_fmt=save_fmt,
    )
    result.workflow.metadata.setdefault("replayed_from", {})["file"] = str(Path(path).resolve())
    return result


__all__ = [
    "BOUNDARY_OPS", "ReplayError", "ReplayResult", "workflow_from_file", "workflow_from_provenance",
    "workflow_from_provenance_around_interactive_steps",
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
