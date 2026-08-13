"""What was done to a result, recorded on the result.

A reconstruction that has been cropped, filtered and thresholded is four
decisions away from the raw data, and none of those decisions were written
down anywhere the file could carry. This records each one as it happens, so a
saved result answers "how was this made" without the answer depending on
someone's notes.

The chain is a plain list of dicts under ``metadata['processing_history']``:
the result already carries metadata everywhere it goes, and every container we
write (OME-TIFF, HDF5, OME-NGFF) has somewhere to put a JSON string. No
sidecar files -- a footprint that lives beside the data is a footprint that is
lost the first time the file is copied.

The list is append-only and inherited: a step is added to a *copy* of the
source's chain, so a result never mutates the history of the thing it came
from, and two results derived from one source do not grow each other's.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import numpy as np

#: Where the chain lives on a result.
HISTORY_KEY = "processing_history"

#: Params never worth recording: pure UI plumbing, or the multi-input list
#: whose members are recorded as `inputs` in readable form anyway.
_SKIP_PARAMS = frozenset({"results", "additional_results", "roi_restriction"})

#: Longest string kept verbatim in a param value. A pasted expression or a
#: path is worth having; a base64 mask payload is not, and would make the
#: metadata larger than the image.
_MAX_STRING = 512


def json_safe(value: Any, *, _depth: int = 0) -> Any:
    """``value`` reduced to something ``json.dumps`` accepts.

    Anything that cannot be represented is *summarised* rather than dropped:
    knowing a parameter was a 512x512 array is worth more than the key
    silently going missing, which reads as "this step had no such setting".
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, str) and len(value) > _MAX_STRING:
            return value[:_MAX_STRING] + f"... ({len(value)} chars)"
        if isinstance(value, float) and not np.isfinite(value):
            # JSON has no NaN or Infinity; a reader that rejects them would
            # reject the whole footprint over one parameter.
            return str(value)
        return value
    if isinstance(value, (np.integer, np.floating, np.bool_)):
        return json_safe(value.item(), _depth=_depth)
    if _depth >= 6:
        return f"<nested {type(value).__name__}>"
    if isinstance(value, dict):
        return {
            str(key): json_safe(item, _depth=_depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        if len(items) > 64:
            return [json_safe(item, _depth=_depth + 1) for item in items[:64]] + [
                f"... ({len(items)} items)"
            ]
        return [json_safe(item, _depth=_depth + 1) for item in items]
    if isinstance(value, np.ndarray):
        return f"<array shape={tuple(value.shape)} dtype={value.dtype}>"
    for attribute in ("name", "id"):
        label = getattr(value, attribute, None)
        if isinstance(label, str) and label:
            return label
    return f"<{type(value).__name__}>"


def _clean_params(params: dict | None) -> dict:
    if not params:
        return {}
    return {
        str(key): json_safe(value)
        for key, value in params.items()
        if key not in _SKIP_PARAMS and not str(key).startswith("_")
    }


def _describe(result) -> str:
    """How an input is named in a step. The name is what the user sees in the
    reconstruction list; the uid is what survives a rename."""
    name = getattr(result, "name", "") or type(result).__name__
    uid = getattr(result, "result_uid", "")
    return f"{name} [{uid}]" if uid else str(name)


def make_step(
    operation: str,
    *,
    label: str = "",
    params: dict | None = None,
    inputs=(),
    extra: dict | None = None,
) -> dict:
    """One entry in the chain."""
    step: dict[str, Any] = {
        "operation": str(operation),
        "label": str(label or operation),
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "params": _clean_params(params),
    }
    described = [_describe(item) for item in inputs if item is not None]
    if described:
        step["inputs"] = described
    if extra:
        step.update(_clean_params(extra))
    return step


def history_of(result) -> list[dict]:
    """The chain recorded on ``result``, oldest step first. Never ``None``."""
    metadata = getattr(result, "metadata", None)
    if not isinstance(metadata, dict):
        return []
    history = metadata.get(HISTORY_KEY)
    return list(history) if isinstance(history, list) else []


def set_history(result, history) -> None:
    metadata = getattr(result, "metadata", None)
    if isinstance(metadata, dict):
        metadata[HISTORY_KEY] = list(history)


def record_step(results, source, processor, params: dict | None = None, inputs=()):
    """Append one step to each result's inherited chain; returns ``results``.

    Called from the one place every processor output passes through, so a
    processor records its footprint without knowing this module exists -- the
    same reasoning as the spatial provenance attached alongside it. Twenty-odd
    processors each remembering to add a line is a footprint that is mostly
    missing.
    """
    if processor is None:
        return results
    operation = getattr(processor, "id", "") or type(processor).__name__
    label = getattr(processor, "name", "") or operation
    sources = list(inputs) or ([source] if source is not None else [])
    inherited = history_of(source) if source is not None else []
    step = make_step(operation, label=label, params=params, inputs=sources)
    for result in results:
        if result is source:
            # A processor that returns its input unchanged (a no-op crop, a
            # view) must not append a step to the very history it inherited.
            continue
        set_history(result, [*inherited, step])
    return results


def history_json(result) -> str:
    """The chain as a JSON string, for containers that store text attributes."""
    return json.dumps(history_of(result), ensure_ascii=False)


def format_history(result) -> str:
    """The chain as readable lines, for a tooltip or a log."""
    lines = []
    for index, step in enumerate(history_of(result), start=1):
        params = step.get("params") or {}
        rendered = ", ".join(f"{key}={value}" for key, value in sorted(params.items()))
        lines.append(f"{index}. {step.get('label', '?')}" + (f" ({rendered})" if rendered else ""))
    return "\n".join(lines)


__all__ = [
    "HISTORY_KEY",
    "format_history",
    "history_json",
    "history_of",
    "json_safe",
    "make_step",
    "record_step",
    "set_history",
]
