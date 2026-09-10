"""The provenance graph: how a result was made, as a replayable DAG.

The linear footprint (:mod:`.footprint`) answers "what was done to this
result" well enough for a tooltip, and badly for replay. It is copied from
the first input only, so a channel merge forgets its second branch; every
output of a split carries the same list, so "the previous result" is
ambiguous; and its parameters are summarised, so a step that was run cannot
be run again from what was written down.

This module records a graph instead, under ``metadata["provenance"]``:

* a **node** per operation (``source``, ``reconstruct``, ``consolidate``,
  ``process``, ``napari-import``; ``opaque`` for a result whose origin is
  unknown), with a minted step id, the plugin that ran, its parameters,
  **ordered** input references and **named** output ports;
* every result carries the **transitive** union of its inputs' nodes plus its
  own, keyed by step id so shared ancestors deduplicate, and an ``output``
  reference saying which node and port *this* result is;
* parameters are encoded **strictly**: what cannot be written down losslessly
  marks the node ``replayable: false`` with a reason, instead of a summary
  quietly standing in for the value.

Saves are not nodes. A written file describes itself with an ``artifact``
record added by the writer; the computational identity of a result is never
changed by saving it.

The readable linear history is *derived* from the graph (the chain of primary
inputs), so everything that reads ``processing_history`` keeps working.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any

import numpy as np

from imswitch.imcommon.algorithms.spatial_frame import mint_uid

#: Where the graph lives on a result.
PROVENANCE_KEY = "provenance"
SCHEMA_VERSION = 1

OPS = ("source", "reconstruct", "consolidate", "process", "napari-import", "opaque")
DEFAULT_PORT = "out"
SOURCE_PORT = "data"

#: Limits applied to graphs read from files we did not write ourselves.
MAX_NODES = 10_000
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
#: Largest array written verbatim into a node's parameters. A kernel is worth
#: keeping; a mask the size of the image is not, and would make the
#: provenance larger than the pixels.
MAX_ARRAY_ELEMENTS = 4096

#: Params never worth recording, mirroring the footprint.
_SKIP_PARAMS = frozenset({"results", "additional_results", "roi_restriction"})


class ProvenanceError(ValueError):
    """The graph is malformed, or cannot be built from what was given."""


class NotEncodable(ProvenanceError):
    """A parameter value has no lossless JSON form."""


class ProvenanceConflict(ProvenanceError):
    """Two graphs disagree about the content of one step id."""


# --------------------------------------------------------------------------
# strict parameter encoding
# --------------------------------------------------------------------------

def _is_marker_key(key: str) -> bool:
    """Whether a dict key could be mistaken for one of the encoder's markers."""
    return len(key) > 4 and key.startswith("__") and key.endswith("__")


def encode_strict(value: Any, *, _path: str = "") -> Any:
    """``value`` as JSON-compatible data, losslessly, or :class:`NotEncodable`.

    The footprint's ``json_safe`` truncates and summarises so that *something*
    is always written. That is the right choice for a tooltip and the wrong
    one for replay, where a parameter that reads back as ``"<array>"`` is a
    run that fails later with less information than we had here.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not np.isfinite(value):
            return {"__float__": str(value)}
        return value
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return encode_strict(float(value), _path=_path)
    if isinstance(value, dict):
        # A plain dict is written as-is only when it cannot be mistaken for
        # one of the encoder's own markers: string keys, none of them of the
        # ``__name__`` form. Anything else goes through the pairs form, which
        # the decoder always reads back as a dict, so a user value that
        # happens to be {"__tuple__": [1, 2]} stays exactly that.
        if all(isinstance(key, str) and not _is_marker_key(key) for key in value):
            return {
                key: encode_strict(item, _path=f"{_path}.{key}" if _path else key)
                for key, item in value.items()
            }
        pairs = []
        for key, item in value.items():
            if not isinstance(key, (str, int, float, bool, tuple)) and key is not None:
                raise NotEncodable(f"{_path or 'params'}: dict key {key!r} cannot be written down")
            pairs.append([
                encode_strict(key, _path=f"{_path}<key>"),
                encode_strict(item, _path=f"{_path}.{key}" if _path else str(key)),
            ])
        return {"__dict__": pairs}
    if isinstance(value, tuple):
        return {"__tuple__": [
            encode_strict(item, _path=f"{_path}[{index}]") for index, item in enumerate(value)
        ]}
    if isinstance(value, list):
        return [
            encode_strict(item, _path=f"{_path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, np.ndarray):
        if value.size > MAX_ARRAY_ELEMENTS:
            raise NotEncodable(
                f"{_path or 'params'}: array of {value.size} elements exceeds "
                f"the {MAX_ARRAY_ELEMENTS}-element limit"
            )
        if value.dtype.kind not in "biuf":
            raise NotEncodable(f"{_path or 'params'}: array dtype {value.dtype} is not numeric")
        return {"__ndarray__": value.tolist(), "dtype": str(value.dtype), "shape": list(value.shape)}
    encode = getattr(value, "encode_provenance", None)
    if callable(encode):
        return encode_strict(encode(), _path=_path)
    raise NotEncodable(f"{_path or 'params'}: {type(value).__name__} has no lossless JSON form")


def decode_strict(value: Any) -> Any:
    """Inverse of :func:`encode_strict` for the markers it introduces."""
    if isinstance(value, dict):
        if set(value) == {"__float__"}:
            return float(value["__float__"])
        if set(value) == {"__ndarray__", "dtype", "shape"}:
            return np.asarray(value["__ndarray__"], dtype=value["dtype"]).reshape(value["shape"])
        if set(value) == {"__tuple__"}:
            return tuple(decode_strict(item) for item in value["__tuple__"])
        if set(value) == {"__dict__"}:
            return {decode_strict(k): decode_strict(v) for k, v in value["__dict__"]}
        return {key: decode_strict(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode_strict(item) for item in value]
    return value


def _not_json_lossless(encoded: dict) -> list:
    """Keys of ``encoded`` that do not survive ``json.dumps``/``json.loads``.

    The check is on the *whole mapping* as it would be written, not on the
    values one by one: JSON turns a non-string key into a string, keeps a
    ``True`` key as ``"true"``, and merges ``1`` and ``"1"`` keys into one.
    A key that is not a string is reported as itself so the caller can
    remove it.
    """
    bad: list = [key for key in encoded if not isinstance(key, str)]
    for key, value in encoded.items():
        if not isinstance(key, str):
            continue
        try:
            text = json.dumps({key: value}, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            bad.append(key)
            continue
        if json.loads(text) != {key: value}:
            bad.append(key)
    return bad


def json_safe_value(value: Any) -> Any:
    from imswitch.improcess.model.footprint import json_safe

    return json_safe(value)


def encode_params(params: dict | None) -> tuple[dict, list[str]]:
    """Encode every recordable parameter; the second item lists the failures.

    One parameter that cannot be encoded does not lose the others: the
    replayable ones are written strictly, the failed one is written as the
    footprint's readable summary, and the node is marked non-replayable with
    the reason. A replay can then say *which* setting it cannot reproduce.
    """
    from imswitch.improcess.model.footprint import json_safe

    encoded: dict[str, Any] = {}
    reasons: list[str] = []
    for key, value in (params or {}).items():
        name = str(key)
        if name in _SKIP_PARAMS or name.startswith("_"):
            continue
        try:
            encoded[name] = encode_strict(value, _path=name)
        except NotEncodable as exc:
            encoded[name] = json_safe(value)
            reasons.append(str(exc))
    return encoded, reasons


# --------------------------------------------------------------------------
# graph access
# --------------------------------------------------------------------------

def graph_of(result) -> dict | None:
    """The graph recorded on ``result``, or ``None``. Not a copy: never mutate."""
    metadata = getattr(result, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    graph = metadata.get(PROVENANCE_KEY)
    return graph if isinstance(graph, dict) else None


def set_graph(result, graph: dict) -> bool:
    """Record ``graph`` on ``result``, giving it a metadata dict if it has none."""
    metadata = getattr(result, "metadata", None)
    if not isinstance(metadata, dict):
        metadata = {}
        try:
            result.metadata = metadata
        except Exception:
            return False
    metadata[PROVENANCE_KEY] = graph
    return True


def output_of(graph: dict) -> tuple[str, str]:
    output = graph.get("output") or {}
    return str(output.get("node", "")), str(output.get("port", DEFAULT_PORT))


def output_node(result) -> dict | None:
    """The node that produced ``result``, if it has a graph."""
    graph = graph_of(result)
    if graph is None:
        return None
    node_id, _port = output_of(graph)
    return graph.get("nodes", {}).get(node_id)


def graph_json(result) -> str:
    graph = graph_of(result)
    return json.dumps(graph if graph is not None else {}, ensure_ascii=False)


def _describe(result) -> str:
    name = getattr(result, "name", "") or type(result).__name__
    uid = getattr(result, "result_uid", "")
    return f"{name} [{uid}]" if uid else str(name)


def output_ref(result) -> dict:
    """``{"node", "port"}`` for ``result``, minting an opaque node if it has none.

    A result that arrived without provenance -- loaded from a file written
    before this existed, produced by a plugin that built it by hand -- is still
    somebody's input. It gets an ``opaque`` node naming what is known (its
    display name and uid) so the graph downstream stays connected, and the
    node is attached to the result so a second use references the same id.
    """
    graph = graph_of(result)
    if graph is not None:
        node_id, port = output_of(graph)
        return {"node": node_id, "port": port}
    node_id, node = make_node(
        "opaque",
        outputs=(DEFAULT_PORT,),
        extra={"label": _describe(result)},
    )
    set_graph(result, new_graph({node_id: node}, node_id, DEFAULT_PORT))
    return {"node": node_id, "port": DEFAULT_PORT}


# --------------------------------------------------------------------------
# building
# --------------------------------------------------------------------------

def _imswitch_version() -> str:
    try:
        from imswitch import __version__

        return str(__version__)
    except Exception:
        return ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_node(
    op: str,
    *,
    plugin=None,
    params: dict | None = None,
    inputs=(),
    input_labels=(),
    outputs=(DEFAULT_PORT,),
    source: dict | None = None,
    extra: dict | None = None,
) -> tuple[str, dict]:
    """One node and its minted id.

    ``inputs`` are ``{"node", "port"}`` references in the order the operation
    consumed them; ``input_labels`` are the matching human-readable names,
    kept so the derived history can say "from MoNaLISA recon [uid]" without
    walking the graph.
    """
    if op not in OPS:
        raise ProvenanceError(f"unknown provenance op {op!r}")
    codec = getattr(plugin, "encode_params", None) if plugin is not None else None
    if callable(codec):
        # The plugin's own codec knows how to write down objects the strict
        # encoder would refuse; a codec that misbehaves must not lose the run,
        # and a codec that returns something JSON would quietly mangle must
        # not be trusted either: its output has to survive a JSON round trip
        # unchanged, or the node is not replayable.
        try:
            encoded, reasons = codec(params)
            encoded, reasons = dict(encoded or {}), list(reasons or [])
            problems = _not_json_lossless(encoded)
            if problems:
                fallback, _ = encode_params(params)
                for key in problems:
                    value = encoded.pop(key)
                    name = key if isinstance(key, str) else str(key)
                    encoded[name] = fallback.get(name, json_safe_value(value))
                reasons.extend(f"plugin codec output for {key!r} is not lossless JSON" for key in problems)
        except Exception as exc:  # noqa: BLE001
            encoded, reasons = encode_params(params)
            reasons.append(f"plugin codec failed: {exc}")
    else:
        encoded, reasons = encode_params(params)
    node: dict[str, Any] = {
        "op": op,
        "time": _now(),
        "inputs": [dict(ref) for ref in inputs],
        "outputs": [str(port) for port in outputs],
        "replayable": not reasons,
    }
    if input_labels:
        node["input_labels"] = [str(label) for label in input_labels]
    if plugin is not None:
        node["plugin_id"] = str(getattr(plugin, "id", "") or type(plugin).__name__)
        node["label"] = str(getattr(plugin, "name", "") or node["plugin_id"])
        node["plugin_version"] = str(getattr(plugin, "version", "") or "")
        node["params_version"] = int(getattr(plugin, "params_version", 1) or 1)
    if params is not None or plugin is not None:
        node["params"] = encoded
    if reasons:
        node["reasons"] = reasons
    if source is not None:
        node["source"] = dict(source)
    if extra:
        node.update({str(key): value for key, value in extra.items()})
    if op == "opaque":
        node["replayable"] = False
        node.setdefault("reasons", ["origin not recorded"])
    return mint_uid("step"), node


def new_graph(nodes: dict, node_id: str, port: str = DEFAULT_PORT) -> dict:
    return {
        "schema": SCHEMA_VERSION,
        "imswitch_version": _imswitch_version(),
        "output": {"node": node_id, "port": port},
        "nodes": dict(nodes),
    }


def _same_content(a: dict, b: dict) -> bool:
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def merge_nodes(*node_maps: dict) -> dict:
    """Union of node maps keyed by step id.

    Two graphs that both descend from one source carry the same node under
    the same id, and that is the normal case. The same id with *different*
    content is not something this code ever produces; when it turns up (two
    files edited by hand, a collision) it is reported, not resolved by
    whichever came last.
    """
    merged: dict[str, dict] = {}
    for nodes in node_maps:
        for node_id, node in (nodes or {}).items():
            existing = merged.get(node_id)
            if existing is None:
                merged[node_id] = node
            elif existing is not node and not _same_content(existing, node):
                raise ProvenanceConflict(
                    f"step {node_id!r} has two different definitions"
                )
    return merged


def _graphs_of(results) -> list[dict]:
    graphs = []
    for item in results:
        graph = graph_of(item)
        if graph is not None:
            graphs.append(graph)
    return graphs


def _attach(result, nodes: dict, node_id: str, port: str) -> None:
    graph = new_graph(nodes, node_id, port)
    if set_graph(result, graph):
        from imswitch.improcess.model.footprint import set_history

        set_history(result, derive_history(graph))


# --------------------------------------------------------------------------
# recording
# --------------------------------------------------------------------------

#: Largest ROI geometry written into a node verbatim. Beyond this the ROIs are
#: recorded by reference (set uid/revision) and the node is non-replayable.
MAX_RESTRICTION_BYTES = 256 * 1024


def encode_restriction(restriction) -> tuple[dict, list[str]]:
    """The ROI restriction a run was narrowed to, as node data.

    Runner-level, not a parameter: the run path strips the restriction from
    the processor's params before ``apply`` sees it, so no processor codec
    could ever record it. The geometry travels when it is small enough to be
    worth carrying (rectangles, polygons, modest masks); a mask the size of
    the image is kept by reference and the node says it cannot be replayed.
    """
    reasons: list[str] = []
    summary = {}
    describe = getattr(restriction, "provenance", None)
    if callable(describe):
        try:
            summary = dict(describe())
        except Exception:
            summary = {}
    encode = getattr(restriction, "encode_provenance", None)
    if not callable(encode):
        reasons.append("restriction: cannot be encoded")
        return {"summary": summary, "by_reference": True}, reasons
    try:
        geometry = encode_strict(encode(), _path="restriction")
    except NotEncodable as exc:
        reasons.append(str(exc))
        return {"summary": summary, "by_reference": True}, reasons
    size = len(json.dumps(geometry, ensure_ascii=False).encode("utf-8"))
    if size > MAX_RESTRICTION_BYTES:
        reasons.append(
            f"restriction: ROI geometry is {size} bytes, above the "
            f"{MAX_RESTRICTION_BYTES}-byte limit; recorded by reference"
        )
        geometry = {key: value for key, value in geometry.items() if key != "rois"}
        return {"summary": summary, "by_reference": True, **geometry}, reasons
    return {"summary": summary, "by_reference": False, **geometry}, reasons


def record_process(
    results, source, processor, params=None, inputs=(), ports=None, restriction=None
):
    """Record one processor run on each of its ``results``; returns ``results``.

    ``inputs`` is the ordered list a multi-input processor consumed
    (``params["results"]``); when empty, ``source`` is the one input. ``ports``
    names the outputs (one per result); a single output is ``"out"``, several
    unnamed ones are ``out0, out1, …`` so they are at least distinct.
    ``restriction`` is the ROI restriction the run was narrowed to, if any.
    """
    results = tuple(results)
    if processor is None:
        return results
    consumed = [item for item in (list(inputs) or [source]) if item is not None]
    refs = [output_ref(item) for item in consumed]
    labels = [_describe(item) for item in consumed]
    port_names = _ports_for(results, ports)
    node_id, node = make_node(
        "process",
        plugin=processor,
        params=params,
        inputs=refs,
        input_labels=labels,
        outputs=port_names,
    )
    if restriction is not None:
        encoded, reasons = encode_restriction(restriction)
        node["restriction"] = encoded
        if reasons:
            # Parameter failures and restriction failures both count; keep both.
            node["replayable"] = False
            node["reasons"] = [*(node.get("reasons") or []), *reasons]
    ancestors = merge_nodes(*(graph.get("nodes", {}) for graph in _graphs_of(consumed)))
    nodes = merge_nodes(ancestors, {node_id: node})
    for result, port in zip(results, port_names):
        if result is source:
            # A processor that returns its input unchanged must not append a
            # step to the very history it inherited.
            continue
        _attach(result, nodes, node_id, port)
    return results


def _ports_for(results, ports) -> list[str]:
    if ports is not None:
        names = [str(port) for port in ports]
        if len(names) != len(results):
            raise ProvenanceError(
                f"{len(names)} output ports named for {len(results)} results"
            )
        if len(set(names)) != len(names):
            raise ProvenanceError(f"output ports are not unique: {names}")
        return names
    if len(results) == 1:
        return [DEFAULT_PORT]
    return [f"{DEFAULT_PORT}{index}" for index in range(len(results))]


SOURCE_KINDS = ("file", "memory", "live")


def source_kind_of(data_obj) -> str:
    """``file`` for data on disk, ``live`` for a stream, ``memory`` otherwise.

    A ``DataObj`` with a path is a file. A ``StreamInit`` (it carries
    ``stack_info``) is live. An in-memory wrapper (a buffered live stack, a
    RAM recording) has no path of its own; it may still know where the
    recording was written (``recording:dataset_path``), which is recorded
    as ``path`` because that is what a replay could open.
    """
    if getattr(data_obj, "stack_info", None) is not None:
        return "live"
    if getattr(data_obj, "dataPath", None) or getattr(data_obj, "path", None):
        return "file"
    return "memory"


def _source_path_of(data_obj, kind: str):
    path = getattr(data_obj, "dataPath", None) or getattr(data_obj, "path", None)
    if path:
        return str(path)
    if kind == "live":
        path = getattr(data_obj, "source_path", None)
        if not path:
            info = getattr(data_obj, "stack_info", None)
            path = getattr(info, "dataset_path", None)
        return str(path) if path else None
    info = getattr(data_obj, "source_info", None) or getattr(data_obj, "_source_info", None) or {}
    path = info.get("dataset_path") if isinstance(info, dict) else None
    if not path:
        attrs = getattr(data_obj, "attrs", None) or getattr(data_obj, "_attrs", None) or {}
        try:
            path = attrs.get("recording:dataset_path")
        except Exception:
            path = None
    return str(path) if path else None


def describe_source(data_obj) -> dict:
    """What a replay needs to find and verify the raw data.

    The fingerprint is cheap on purpose (no pixel hashing): dataset shape and
    dtype, the file's size and modification time, and a digest of the
    attributes. It detects a file that was re-recorded or re-exported, which
    is what goes wrong in practice; a full hash is a replay-time option.

    ``kind`` says whether there is anything on disk to replay from at all
    (see :func:`source_kind_of`); a memory or live source without a path is
    described as far as it goes and the reconstruction node is marked
    non-replayable by the caller.
    """
    kind = source_kind_of(data_obj)
    path = _source_path_of(data_obj, kind)
    dataset = getattr(data_obj, "datasetName", None)
    if callable(dataset):
        dataset = None
    if dataset is None:
        dataset = getattr(data_obj, "dataset_name", None) or getattr(data_obj, "_datasetName", None)
    description: dict[str, Any] = {
        "kind": kind,
        "path": path,
        "dataset": str(dataset) if dataset else None,
        "name": str(getattr(data_obj, "name", "") or ""),
    }
    fingerprint: dict[str, Any] = {}
    if path:
        try:
            stat = os.stat(path)
            fingerprint["size"] = int(stat.st_size) if os.path.isfile(path) else None
            fingerprint["mtime"] = float(stat.st_mtime)
        except OSError:
            pass
    try:
        # Never materialise pixels here: a DataObj exposes its open handle,
        # an in-memory wrapper already holds its array, a stream init carries
        # its first frames.
        if hasattr(data_obj, "data_handle"):
            handle = data_obj.data_handle
        elif getattr(data_obj, "_data", None) is not None:
            handle = data_obj._data
        elif kind == "live":
            handle = getattr(data_obj, "data", None)
        else:
            handle = None
        if handle is not None and hasattr(handle, "shape"):
            fingerprint["shape"] = [int(n) for n in handle.shape]
            fingerprint["dtype"] = str(handle.dtype)
    except Exception:
        pass
    try:
        attrs = getattr(data_obj, "attrs", None)
        if attrs:
            fingerprint["attrs_digest"] = attrs_digest(attrs)
    except Exception:
        pass
    manifest = getattr(data_obj, "sourceFingerprint", None)
    if manifest:
        fingerprint["manifest"] = manifest if isinstance(manifest, (str, int, float)) else str(manifest)
    # A full content hash is opt-in (the runner computes it when asked to);
    # it is the only field that proves the pixels are the same.
    digest = getattr(data_obj, "_provenance_sha256", None)
    if digest:
        fingerprint["sha256"] = str(digest)
    description["fingerprint"] = fingerprint
    return description


def attrs_digest(attrs) -> str:
    """A sha256 over *every* attribute value, in full.

    The footprint's ``json_safe`` truncates long lists and strings, which is
    right for a readable summary and wrong for a fingerprint: two files
    whose attributes differ past the cut would hash the same. This uses a
    complete, order-independent rendering instead (arrays as their full
    element lists, bytes decoded, sets sorted).
    """
    payload = json.dumps(_full_json(dict(attrs)), sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _full_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _full_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_full_json(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_full_json(item) for item in value)
    if isinstance(value, np.ndarray):
        return {"__ndarray__": value.tolist(), "dtype": str(value.dtype), "shape": list(value.shape)}
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, float) and not np.isfinite(value):
        return str(value)
    return value


_SOURCE_NODE_ATTR = "_provenance_source_node"


def source_node_for(data_obj) -> tuple[str, dict]:
    """The source node for ``data_obj``, minted once and remembered on it.

    Two reconstructions of one file must reference the same source node, or a
    consolidation of them would claim two different inputs.
    """
    cached = getattr(data_obj, _SOURCE_NODE_ATTR, None)
    if isinstance(cached, tuple) and len(cached) == 2:
        return cached
    node_id, node = make_node("source", source=describe_source(data_obj), outputs=(SOURCE_PORT,))
    try:
        setattr(data_obj, _SOURCE_NODE_ATTR, (node_id, node))
    except Exception:
        pass
    return node_id, node


def _unreplayable_source_reason(source_node: dict) -> str | None:
    """A source with no path on disk cannot be opened again, whatever its kind.

    A memory or live source that knows the recording it was also written to
    is fine: that file is what a replay opens.
    """
    description = source_node.get("source") or {}
    if not description.get("path"):
        return (
            f"source is {description.get('kind', 'unknown')} data that was not "
            "persisted to a file"
        )
    return None


def record_reconstruction(result, reconstructor, params, data_obj, *, extra: dict | None = None):
    """Record that ``result`` came from ``reconstructor`` over ``data_obj``.

    A source that is not a file (a buffered live stack, a RAM recording)
    leaves the node non-replayable with the reason, unless the wrapper knew
    the path the recording was also written to.
    """
    source_id, source = source_node_for(data_obj)
    node_id, node = make_node(
        "reconstruct",
        plugin=reconstructor,
        params=params,
        inputs=[{"node": source_id, "port": SOURCE_PORT}],
        input_labels=[source.get("source", {}).get("name") or source_id],
        outputs=(DEFAULT_PORT,),
        extra=extra,
    )
    reason = _unreplayable_source_reason(source)
    if reason:
        node["replayable"] = False
        node["reasons"] = [*(node.get("reasons") or []), reason]
    _attach(result, merge_nodes({source_id: source}, {node_id: node}), node_id, DEFAULT_PORT)
    return result


COMPLETION_STATES = ("partial", "complete", "stalled", "failed")
_STREAM_NODE_ATTR = "_provenance_stream_node"


def record_streaming(result, reconstructor, params, source, *, session, completion: dict):
    """Record a streaming reconstruction snapshot on ``result``.

    Every ``result()`` snapshot and the final ``finish()`` go through here.
    The step id is minted once per ``session`` and reused, so a stream that
    yields a hundred snapshots does not leave a hundred nodes; the node is
    rewritten with the latest ``completion`` (``status`` in
    :data:`COMPLETION_STATES`, ``frames_committed``, ``expected_frames``).
    Only a ``complete`` run over a file source is replayable.
    """
    status = str(completion.get("status", "partial"))
    if status not in COMPLETION_STATES:
        raise ProvenanceError(f"unknown completion status {status!r}")
    source_id, source_node = source_node_for(source)
    cached = getattr(session, _STREAM_NODE_ATTR, None)
    node_id = cached if isinstance(cached, str) and cached else None
    new_id, node = make_node(
        "reconstruct",
        plugin=reconstructor,
        params=params,
        inputs=[{"node": source_id, "port": SOURCE_PORT}],
        input_labels=[source_node.get("source", {}).get("name") or source_id],
        outputs=(DEFAULT_PORT,),
        extra={
            "mode": "streaming",
            "completion": {
                "status": status,
                "frames_committed": completion.get("frames_committed"),
                "expected_frames": completion.get("expected_frames"),
            },
        },
    )
    if node_id is None:
        node_id = new_id
        try:
            setattr(session, _STREAM_NODE_ATTR, node_id)
        except Exception:
            pass
    reasons = list(node.get("reasons") or [])
    source_reason = _unreplayable_source_reason(source_node)
    if source_reason:
        reasons.append(source_reason)
    if status != "complete":
        reasons.append(f"stream finished as {status}")
    if reasons:
        node["replayable"] = False
        node["reasons"] = reasons
    _attach(result, merge_nodes({source_id: source_node}, {node_id: node}), node_id, DEFAULT_PORT)
    return result


def record_consolidation(merged, results, reconstructor, params=None):
    """Record ``merged`` as the consolidation of every result in ``results``."""
    results = list(results)
    refs = [output_ref(item) for item in results]
    node_id, node = make_node(
        "consolidate",
        plugin=reconstructor,
        params=params,
        inputs=refs,
        input_labels=[_describe(item) for item in results],
        outputs=(DEFAULT_PORT,),
    )
    ancestors = merge_nodes(*(graph.get("nodes", {}) for graph in _graphs_of(results)))
    _attach(merged, merge_nodes(ancestors, {node_id: node}), node_id, DEFAULT_PORT)
    return merged


def record_import(
    result,
    *,
    plugin_name: str,
    widget_name: str | None,
    layer_name: str,
    layer_type: str,
    grid: str,
    source_result=None,
):
    """Record a layer taken back from a napari plugin as ``result``.

    Non-replayable by definition: what the plugin did between receiving our
    layers and producing this one is not something we can run again.
    """
    inputs = [output_ref(source_result)] if source_result is not None else []
    labels = [_describe(source_result)] if source_result is not None else []
    node_id, node = make_node(
        "napari-import",
        inputs=inputs,
        input_labels=labels,
        outputs=(DEFAULT_PORT,),
        extra={
            "plugin_name": str(plugin_name),
            "widget_name": str(widget_name or ""),
            "layer_name": str(layer_name),
            "layer_type": str(layer_type),
            "grid": str(grid),
            "label": f"Imported from {plugin_name}",
            "replayable": False,
            "reasons": ["produced interactively inside a napari plugin"],
        },
    )
    ancestors = merge_nodes(
        *(graph.get("nodes", {}) for graph in _graphs_of([source_result] if source_result is not None else []))
    )
    _attach(result, merge_nodes(ancestors, {node_id: node}), node_id, DEFAULT_PORT)
    return result


# --------------------------------------------------------------------------
# derived linear history
# --------------------------------------------------------------------------

def primary_chain(graph: dict) -> list[tuple[str, dict]]:
    """The nodes from the output back along first inputs, oldest first."""
    nodes = graph.get("nodes", {})
    node_id, _port = output_of(graph)
    chain: list[tuple[str, dict]] = []
    seen: set[str] = set()
    while node_id and node_id in nodes and node_id not in seen:
        seen.add(node_id)
        node = nodes[node_id]
        chain.append((node_id, node))
        inputs = node.get("inputs") or []
        node_id = str(inputs[0].get("node", "")) if inputs else ""
    chain.reverse()
    return chain


def derive_history(graph: dict) -> list[dict]:
    """The footprint's linear list, computed from the graph.

    Source and opaque nodes are not steps -- nothing was *done* at them -- so
    a plain reconstruction has one step and a cropped one has two, exactly as
    the linear footprint recorded before the graph existed.
    """
    from imswitch.improcess.model.footprint import json_safe

    history = []
    for node_id, node in primary_chain(graph):
        op = node.get("op")
        if op in ("source", "opaque"):
            continue
        params = node.get("params") or {}
        if node.get("replayable", True):
            params = json_safe(decode_strict(params))
        restriction = node.get("restriction")
        if isinstance(restriction, dict) and restriction.get("summary"):
            # The readable chain says "filtered inside ROI cell-3", as the
            # linear footprint always did; the geometry stays on the node.
            params = {**params, "region": restriction["summary"]}
        step: dict[str, Any] = {
            "operation": node.get("plugin_id") or op,
            "label": node.get("label") or node.get("plugin_id") or op,
            "time": node.get("time", ""),
            "params": params,
            "step_id": node_id,
        }
        labels = node.get("input_labels") or []
        if labels:
            step["inputs"] = list(labels)
        history.append(step)
    return history


# --------------------------------------------------------------------------
# validation of graphs we did not build in this process
# --------------------------------------------------------------------------

def validate_graph(graph: Any) -> dict:
    """Check a graph read from a file; returns it, or raises :class:`ProvenanceError`.

    A file is not trusted just because we wrote files like it. The checks are
    the ones that would otherwise turn into an unbounded walk, a silent
    overwrite, or a replay of a step that never existed.
    """
    if not isinstance(graph, dict):
        raise ProvenanceError("provenance is not a JSON object")
    schema = graph.get("schema")
    if not isinstance(schema, int) or schema < 1 or schema > SCHEMA_VERSION:
        raise ProvenanceError(f"unsupported provenance schema {schema!r}")
    nodes = graph.get("nodes")
    if not isinstance(nodes, dict):
        raise ProvenanceError("provenance has no node table")
    if len(nodes) > MAX_NODES:
        raise ProvenanceError(f"provenance has {len(nodes)} nodes; the limit is {MAX_NODES}")
    payload = len(json.dumps(graph, ensure_ascii=False, default=str).encode("utf-8"))
    if payload > MAX_PAYLOAD_BYTES:
        raise ProvenanceError(f"provenance is {payload} bytes; the limit is {MAX_PAYLOAD_BYTES}")

    for node_id, node in nodes.items():
        if not isinstance(node_id, str) or not isinstance(node, dict):
            raise ProvenanceError("malformed node table")
        if node.get("op") not in OPS:
            raise ProvenanceError(f"node {node_id!r} has unknown op {node.get('op')!r}")
        outputs = node.get("outputs")
        if not isinstance(outputs, list) or not outputs or len(set(outputs)) != len(outputs):
            raise ProvenanceError(f"node {node_id!r} has no distinct output ports")
        for ref in node.get("inputs") or []:
            target = nodes.get(str(ref.get("node", ""))) if isinstance(ref, dict) else None
            if target is None:
                raise ProvenanceError(f"node {node_id!r} references a missing input node")
            if str(ref.get("port", "")) not in (target.get("outputs") or []):
                raise ProvenanceError(
                    f"node {node_id!r} references port {ref.get('port')!r} that "
                    f"{ref.get('node')!r} does not have"
                )

    out_id, out_port = output_of(graph)
    if out_id not in nodes:
        raise ProvenanceError("provenance output points at a missing node")
    if out_port not in nodes[out_id].get("outputs", []):
        raise ProvenanceError(f"provenance output names an unknown port {out_port!r}")

    # Acyclic: iterative DFS with colours, so a hostile file cannot recurse us out.
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {node_id: WHITE for node_id in nodes}
    for start in nodes:
        if colour[start] != WHITE:
            continue
        stack = [(start, iter(ref["node"] for ref in nodes[start].get("inputs") or []))]
        colour[start] = GREY
        while stack:
            node_id, children = stack[-1]
            advanced = False
            for child in children:
                if colour[child] == GREY:
                    raise ProvenanceError(f"provenance has a cycle through {child!r}")
                if colour[child] == WHITE:
                    colour[child] = GREY
                    stack.append((child, iter(ref["node"] for ref in nodes[child].get("inputs") or [])))
                    advanced = True
                    break
            if not advanced:
                colour[node_id] = BLACK
                stack.pop()
    return graph


__all__ = [
    "COMPLETION_STATES",
    "DEFAULT_PORT",
    "SOURCE_KINDS",
    "record_streaming",
    "source_kind_of",
    "MAX_ARRAY_ELEMENTS",
    "MAX_NODES",
    "MAX_PAYLOAD_BYTES",
    "NotEncodable",
    "OPS",
    "PROVENANCE_KEY",
    "ProvenanceConflict",
    "ProvenanceError",
    "SCHEMA_VERSION",
    "SOURCE_PORT",
    "decode_strict",
    "derive_history",
    "attrs_digest",
    "describe_source",
    "encode_params",
    "encode_strict",
    "graph_json",
    "graph_of",
    "make_node",
    "merge_nodes",
    "new_graph",
    "output_node",
    "output_ref",
    "primary_chain",
    "record_consolidation",
    "record_import",
    "record_process",
    "record_reconstruction",
    "set_graph",
    "source_node_for",
    "validate_graph",
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
