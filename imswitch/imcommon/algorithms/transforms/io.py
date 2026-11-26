"""Persistence for transform records.

JSON is canonical: human-readable, diffable and reviewable in a pull request,
which matters for a file that encodes a claim about a rig. HDF5 is supported as
well, but as a *group* serializer -- transforms need to be embeddable next to
the data they describe inside an OME-HDF5/Zarr recording, not only to live as
standalone sidecars.

The HDF5 form stores the same JSON document in an attribute and *additionally*
writes the matrix as a plain dataset so other tools can read it without knowing
this schema. To keep those two from drifting, the dataset is regenerated on
every write and never consulted on read: the JSON is the single source of
truth.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from .base import model_from_params
from .model import AcquisitionContext, ResidualStats, SpatialTransform

__all__ = [
    "SCHEMA",
    "to_dict",
    "from_dict",
    "save_json",
    "load_json",
    "save_h5",
    "load_h5",
    "save",
    "load",
]

SCHEMA = "imswitch.transform/1"

_DEFAULT_H5_GROUP = "transform"


def to_dict(transform: SpatialTransform) -> dict[str, Any]:
    """Return the JSON-safe document for one transform record."""
    document: dict[str, Any] = {
        "schema": SCHEMA,
        "created_at": transform.created_at,
        "source_frame": transform.source_frame,
        "target_frame": transform.target_frame,
        "units": transform.units,
        "ndim": transform.ndim,
        "kind": transform.kind,
        "params": transform.model.to_params(),
    }
    source_context = transform.source_context.to_dict()
    if source_context:
        document["source_context"] = source_context
    target_context = transform.target_context.to_dict()
    if target_context:
        document["target_context"] = target_context
    if transform.residuals is not None:
        document["residuals"] = transform.residuals.to_dict()
    if transform.provenance:
        document["provenance"] = _jsonable(dict(transform.provenance))
    return document


def from_dict(data: Mapping[str, Any]) -> SpatialTransform:
    """Rebuild a transform record from :func:`to_dict` output."""
    schema = str(data.get("schema", ""))
    if schema != SCHEMA:
        raise ValueError(
            f"unsupported transform schema {schema!r}; this build reads {SCHEMA!r}"
        )
    try:
        kind = data["kind"]
    except KeyError:
        raise ValueError("transform document is missing its 'kind'") from None

    residuals = data.get("residuals")
    return SpatialTransform(
        model=model_from_params(kind, data.get("params", {})),
        source_frame=str(data.get("source_frame", "source")),
        target_frame=str(data.get("target_frame", "target")),
        units=str(data.get("units", "px")),
        source_context=AcquisitionContext.from_dict(data.get("source_context")),
        target_context=AcquisitionContext.from_dict(data.get("target_context")),
        residuals=None if residuals is None else ResidualStats.from_dict(residuals),
        provenance=dict(data.get("provenance", {})),
        created_at=str(data.get("created_at", "")),
    )


def save_json(path: str | Path, transform: SpatialTransform) -> Path:
    """Write one transform record as JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_dict(transform), indent=2, sort_keys=True) + "\n")
    return path


def load_json(path: str | Path) -> SpatialTransform:
    """Read a transform record written by :func:`save_json`."""
    path = Path(path)
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    return from_dict(data)


def save_h5(
    path: str | Path,
    transform: SpatialTransform,
    *,
    group: str = _DEFAULT_H5_GROUP,
    mode: str = "a",
) -> Path:
    """Write a transform record into an HDF5 group.

    ``mode='a'`` by default so a transform can be attached to an existing
    recording rather than replacing it.
    """
    import h5py

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = to_dict(transform)

    with h5py.File(str(path), mode) as handle:
        if group in handle:
            del handle[group]
        target = handle.create_group(group)
        target.attrs["schema"] = SCHEMA
        target.attrs["transform_json"] = json.dumps(document, sort_keys=True)
        # Convenience only -- see the module docstring. Never read back.
        matrix = transform.as_matrix()
        if matrix is not None:
            target.create_dataset("matrix", data=np.asarray(matrix, dtype=np.float64))
    return path


def load_h5(
    path: str | Path, *, group: str = _DEFAULT_H5_GROUP
) -> SpatialTransform:
    """Read a transform record written by :func:`save_h5`."""
    import h5py

    with h5py.File(str(path), "r") as handle:
        if group not in handle:
            raise ValueError(f"{path} has no transform group {group!r}")
        node = handle[group]
        try:
            raw = node.attrs["transform_json"]
        except KeyError:
            raise ValueError(
                f"{path}:{group} has no 'transform_json' attribute; it was not "
                "written by this module"
            ) from None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return from_dict(json.loads(str(raw)))


def save(path: str | Path, transform: SpatialTransform, **kwargs: Any) -> Path:
    """Write a transform, choosing the format from the file suffix."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in (".h5", ".hdf5"):
        return save_h5(path, transform, **kwargs)
    if suffix == ".json":
        return save_json(path, transform)
    raise ValueError(
        f"unsupported transform file suffix {path.suffix!r}; use .json (canonical) "
        "or .h5/.hdf5"
    )


def load(path: str | Path, **kwargs: Any) -> SpatialTransform:
    """Read a transform, choosing the format from the file suffix."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in (".h5", ".hdf5"):
        return load_h5(path, **kwargs)
    if suffix == ".json":
        return load_json(path)
    raise ValueError(
        f"unsupported transform file suffix {path.suffix!r}; use .json (canonical) "
        "or .h5/.hdf5"
    )


def _jsonable(value: Any) -> Any:
    """Coerce numpy scalars/arrays in provenance into JSON-safe values."""
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    return value
