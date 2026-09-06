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
    """The recorded provenance cannot be turned into a runnable workflow."""

    def __init__(self, message: str, reasons: list[str] | None = None):
        super().__init__(message)
        self.reasons = list(reasons or [])


@dataclass
class ReplayResult:
    workflow: Workflow
    warnings: list[str] = field(default_factory=list)
    node_ids: dict[str, str] = field(default_factory=dict)   # step id -> provenance node id


_PREFIX = {"source": "src", "reconstruct": "rec", "consolidate": "cons", "process": "proc"}


class _Skip(Exception):
    """This node depends on one that could not be replayed."""


def _ancestors(graph: dict, node_id: str) -> list[str]:
    """Node ids reachable backwards from ``node_id``, in dependency order."""
    nodes = graph["nodes"]
    order: list[str] = []
    seen: set[str] = set()

    def visit(current: str) -> None:
        if current in seen:
            return
        seen.add(current)
        for ref in nodes[current].get("inputs") or []:
            visit(str(ref["node"]))
        order.append(current)

    visit(node_id)
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

    def __init__(self, graph: dict, registry):
        self.graph = graph
        self.nodes = graph["nodes"]
        self.registry = registry
        self.warnings: list[str] = []
        self.reasons: list[str] = []
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
        if self.nodes[target].get("op") == "source":
            return Ref(self.step_ids[target], None)
        return Ref(self.step_ids[target], port)

    def build(self, out_id: str) -> None:
        for node_id in _ancestors(self.graph, out_id):
            node = self.nodes[node_id]
            op = str(node.get("op"))
            label = f"{op} {node_id[-8:]}"
            if not node.get("replayable", True) and op != "source":
                for reason in node.get("reasons") or ["marked non-replayable"]:
                    self.reasons.append(f"{label} ({node.get('plugin_id') or op}): {reason}")
                self.skipped.add(node_id)
                continue
            try:
                self.emit(node_id, node, op, label)
            except _Skip:
                # Its own record is fine; an ancestor's is not, and that
                # ancestor's reason is already listed.
                self.skipped.add(node_id)

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
) -> ReplayResult:
    """A :class:`Workflow` that reproduces the file ``document`` describes.

    ``registry`` (optional) lets parameters be decoded by the plugins' own
    codecs, migrated across ``params_version`` changes, and lets missing
    plugins be reported here instead of at run time.
    """
    if isinstance(document, dict):
        document = ProvenanceDocument.from_dict(document)
    if document.graph is None:
        return _from_history(document, registry=registry, name=name, save_fmt=save_fmt)
    graph = validate_graph(document.graph)
    out_id, out_port = output_of(graph)

    builder = _Builder(graph, registry)
    builder.build(out_id)
    if builder.reasons:
        raise ReplayError(
            "the recorded run cannot be replayed:\n  " + "\n  ".join(builder.reasons)
            + "\n(see the LLM guide in docs/improcess-workflows.rst for filling the gaps)",
            builder.reasons,
        )
    if out_id not in builder.step_ids:
        raise ReplayError("the file's own node was not replayable")

    fmt = save_fmt or str((document.artifact or {}).get("fmt") or "tiff")
    output_is_source = graph["nodes"][out_id].get("op") == "source"
    output_ref = Ref(builder.step_ids[out_id], None if output_is_source else out_port)
    builder.steps.append(Save("save1", input=output_ref, fmt=fmt,
                              path_template=save_template or "{out_dir}/{source_stem}_replay{ext}"))

    warnings = list(builder.warnings)
    recorded_version = str(graph.get("imswitch_version") or "")
    from imswitch.improcess.model.plugin_versions import imswitch_version

    if recorded_version and recorded_version != imswitch_version():
        warnings.append(f"recorded with ImSwitch {recorded_version}, this is {imswitch_version()}")

    artifact = dict(document.artifact or {})
    node_ids = {sid: nid for nid, sid in builder.step_ids.items()}
    workflow = Workflow(
        name or f"replay-{_slug(artifact.get('primary') or out_id[-8:])}",
        builder.steps,
        description="replayed from the provenance of " + str(artifact.get("primary") or "a result"),
        metadata={
            "replayed_from": {"artifact": artifact, "imswitch_version": recorded_version},
            "provenance_nodes": dict(node_ids),
        },
    )
    return ReplayResult(workflow=workflow, warnings=warnings, node_ids=node_ids)


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


__all__ = ["ReplayError", "ReplayResult", "workflow_from_file", "workflow_from_provenance"]


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
