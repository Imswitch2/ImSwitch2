"""Layout-agnostic metadata reading for ImProcess measurement files.

Nothing in this module knows about ImSwitch's recording layout. The walkers
descend whatever hierarchy the container actually has — HDF5 groups/datasets,
Zarr groups/arrays, TIFF series/pages — and surface every attribute found on
the way down. A file written by a future storer, or by a completely different
program, therefore shows up without any change here.

The result is a plain :class:`MetadataNode` tree that is fully detached from
the file (attribute values are materialized during the walk), so callers may
close the container immediately and keep displaying the tree.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping

import numpy as np


KIND_ROOT = "root"
KIND_GROUP = "group"
KIND_ARRAY = "array"
KIND_ATTRIBUTE = "attribute"
KIND_INFO = "info"
KIND_ERROR = "error"


@dataclass(frozen=True)
class MetadataLimits:
    """Bounds that keep a pathological file from freezing the GUI.

    Metadata is normally tiny, but nothing stops a container from holding a
    hundred thousand members or an attribute with a megabyte of embedded XML.
    Every limit that trips inserts a visible ``KIND_INFO`` marker node rather
    than silently dropping content.
    """

    max_depth: int = 24
    max_children: int = 512
    max_nodes: int = 20000
    max_value_chars: int = 400


DEFAULT_LIMITS = MetadataLimits()


@dataclass(frozen=True)
class MetadataNode:
    """One entry in the metadata tree.

    ``value`` keeps the original Python/numpy object so exporters can format
    it their own way; ``detail`` is a short human-readable type/shape summary
    for the tree's third column.
    """

    name: str
    kind: str = KIND_GROUP
    path: str = ""
    value: Any = None
    detail: str = ""
    children: tuple["MetadataNode", ...] = field(default_factory=tuple)

    def display_value(self, limits: MetadataLimits = DEFAULT_LIMITS) -> str:
        if self.value is None and self.kind in (KIND_ROOT, KIND_GROUP, KIND_ARRAY):
            return ""
        return format_metadata_value(self.value, limits.max_value_chars)

    def iter_nodes(self) -> Iterator["MetadataNode"]:
        """Yield this node and every descendant, depth first."""
        yield self
        for child in self.children:
            yield from child.iter_nodes()

    def find(self, path: str) -> "MetadataNode | None":
        for node in self.iter_nodes():
            if node.path == path:
                return node
        return None

    def count(self) -> int:
        return sum(1 for _ in self.iter_nodes())


# --- public entry points -----------------------------------------------------


def read_metadata_tree(
    path: str | os.PathLike,
    *,
    limits: MetadataLimits = DEFAULT_LIMITS,
) -> MetadataNode:
    """Read the full metadata hierarchy of the measurement file at ``path``.

    Opens the container read-only, walks it, and closes it again — the returned
    tree holds no live file handles.
    """
    from imswitch.improcess.model.dataset_sources import resolve_dataset_source

    source_path = Path(path)
    format_id = None
    try:
        source = resolve_dataset_source(source_path)
        source_path = Path(source.path)
        format_id = source.format_id
    except Exception:
        # Unknown extension: still show what the filesystem knows about it.
        pass

    children = [_file_info_node(source_path, format_id)]
    if format_id is None:
        children.append(
            MetadataNode(
                name="Unsupported file type",
                kind=KIND_ERROR,
                path="!error",
                value=f'No metadata reader for "{source_path.name}"',
            )
        )
        return _root_node(source_path, children)

    container = None
    try:
        container = _open_container(str(source_path), format_id)
        children.extend(_container_children(container, limits=limits))
    except Exception as exc:
        children.append(_error_node("Could not read metadata", exc, "!error"))
    finally:
        _close_container(container)

    return _root_node(source_path, children)


def metadata_tree_from_container(
    container: Any,
    *,
    name: str | None = None,
    source_path: str | os.PathLike | None = None,
    limits: MetadataLimits = DEFAULT_LIMITS,
) -> MetadataNode:
    """Build a metadata tree from an already-open container.

    Used when the caller already holds the file open (an ImProcess ``DataObj``
    does), so the file is not opened a second time. Ownership stays with the
    caller: this never closes ``container``.
    """
    children: list[MetadataNode] = []
    resolved_path = Path(source_path) if source_path is not None else None
    if resolved_path is not None:
        children.append(_file_info_node(resolved_path, None))
    try:
        children.extend(_container_children(container, limits=limits))
    except Exception as exc:
        children.append(_error_node("Could not read metadata", exc, "!error"))

    display_name = name or (resolved_path.name if resolved_path else type(container).__name__)
    return MetadataNode(
        name=display_name,
        kind=KIND_ROOT,
        path="",
        children=tuple(children),
    )


def metadata_tree_to_dict(
    node: MetadataNode,
    limits: MetadataLimits = DEFAULT_LIMITS,
) -> dict:
    """Serialize a tree to JSON-safe nested dicts (for export/round-tripping)."""
    payload: dict[str, Any] = {"name": node.name, "kind": node.kind, "path": node.path}
    if node.detail:
        payload["detail"] = node.detail
    if node.value is not None:
        payload["value"] = _json_safe(node.value, limits)
    if node.children:
        payload["children"] = [metadata_tree_to_dict(child, limits) for child in node.children]
    return payload


def metadata_tree_rows(
    node: MetadataNode,
    limits: MetadataLimits = DEFAULT_LIMITS,
) -> list[dict[str, str]]:
    """Flatten every attribute in the tree to ``path``/``value``/``type`` rows.

    Only leaf-ish entries carry information worth tabulating, so groups and the
    structural array nodes are skipped unless they hold a value.
    """
    rows = []
    for entry in node.iter_nodes():
        if entry.kind in (KIND_ROOT, KIND_GROUP):
            continue
        if entry.value is None and not entry.detail:
            continue
        rows.append(
            {
                "path": entry.path,
                "value": entry.display_value(limits),
                "type": entry.detail,
            }
        )
    return rows


def format_metadata_value(value: Any, max_chars: int = DEFAULT_LIMITS.max_value_chars) -> str:
    """Render an attribute value as a single short line of text."""
    text = _format_value_full(value)
    if len(text) > max_chars:
        return f"{text[:max_chars]}… ({len(text)} chars)"
    return text


def value_detail(value: Any) -> str:
    """Short type/shape summary shown next to a value."""
    if isinstance(value, np.ndarray):
        return f"{value.dtype}{list(value.shape)}"
    if isinstance(value, np.generic):
        return str(value.dtype)
    if isinstance(value, (bytes, bytearray)):
        return f"bytes[{len(value)}]"
    if isinstance(value, str):
        return f"str[{len(value)}]"
    if isinstance(value, Mapping):
        return f"dict[{len(value)}]"
    if isinstance(value, (list, tuple)):
        return f"{type(value).__name__}[{len(value)}]"
    if value is None:
        return ""
    return type(value).__name__


# --- container dispatch ------------------------------------------------------


def _open_container(path: str, format_id: str) -> Any:
    if format_id == "hdf5":
        import h5py

        return h5py.File(path, "r")
    if format_id == "tiff":
        import tifffile as tiff

        return tiff.TiffFile(path)
    if format_id == "zarr":
        import zarr

        return zarr.open(path, mode="r")
    raise ValueError(f'No metadata reader for format "{format_id}"')


def _close_container(container: Any) -> None:
    if container is None:
        return
    close = getattr(container, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _container_children(container: Any, *, limits: MetadataLimits) -> list[MetadataNode]:
    """Walk any supported container and return the root's child nodes."""
    from imswitch.improcess.model.image_sources import is_zarr_array, is_zarr_group

    context = _WalkContext(limits)

    import h5py

    if isinstance(container, (h5py.Group, h5py.Dataset)):
        return _hdf5_children(container, "", context, depth=0)
    if is_zarr_group(container) or is_zarr_array(container):
        return _zarr_children(container, "", context, depth=0)

    try:
        import tifffile as tiff
    except Exception:  # pragma: no cover - tifffile is a hard dependency
        tiff = None
    if tiff is not None and isinstance(container, tiff.TiffFile):
        return _tiff_children(container, context)

    raise ValueError(f'Unsupported metadata container "{type(container).__name__}"')


class _WalkContext:
    """Mutable node budget shared by one walk."""

    def __init__(self, limits: MetadataLimits) -> None:
        self.limits = limits
        self.remaining = limits.max_nodes

    def take(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0


# --- HDF5 --------------------------------------------------------------------


def _hdf5_children(node: Any, path: str, context: _WalkContext, depth: int) -> list[MetadataNode]:
    import h5py

    children = _attribute_nodes(getattr(node, "attrs", {}), path, context, depth)
    if not isinstance(node, h5py.Group):
        return children

    if depth >= context.limits.max_depth:
        children.append(_depth_limit_node(path, context.limits))
        return children

    try:
        keys = sorted(node.keys())
    except Exception as exc:
        children.append(_error_node("Could not list members", exc, _join(path, "!error")))
        return children

    shown, hidden = _limit(keys, context.limits.max_children)
    for key in shown:
        if not context.take():
            children.append(_budget_limit_node(path, context.limits))
            break
        children.append(_hdf5_member_node(node, str(key), path, context, depth))
    if hidden:
        children.append(_more_node(path, hidden))
    return children


def _hdf5_member_node(
    group: Any,
    key: str,
    path: str,
    context: _WalkContext,
    depth: int,
) -> MetadataNode:
    import h5py

    child_path = _join(path, key)
    try:
        member = group[key]
    except Exception as exc:
        return _error_node(key, exc, child_path)

    if isinstance(member, h5py.Dataset):
        return MetadataNode(
            name=key,
            kind=KIND_ARRAY,
            path=child_path,
            detail=_array_detail(member),
            children=tuple(_attribute_nodes(member.attrs, child_path, context, depth + 1)),
        )
    if isinstance(member, h5py.Group):
        return MetadataNode(
            name=key,
            kind=KIND_GROUP,
            path=child_path,
            children=tuple(_hdf5_children(member, child_path, context, depth + 1)),
        )
    # Named datatypes and anything else h5py hands back.
    return MetadataNode(
        name=key,
        kind=KIND_INFO,
        path=child_path,
        detail=type(member).__name__,
    )


# --- Zarr --------------------------------------------------------------------


def _zarr_children(node: Any, path: str, context: _WalkContext, depth: int) -> list[MetadataNode]:
    from imswitch.improcess.model.image_sources import is_zarr_group

    children = _attribute_nodes(getattr(node, "attrs", {}), path, context, depth)
    if not is_zarr_group(node):
        return children

    if depth >= context.limits.max_depth:
        children.append(_depth_limit_node(path, context.limits))
        return children

    try:
        keys = sorted(node.keys())
    except Exception as exc:
        children.append(_error_node("Could not list members", exc, _join(path, "!error")))
        return children

    shown, hidden = _limit(keys, context.limits.max_children)
    for key in shown:
        if not context.take():
            children.append(_budget_limit_node(path, context.limits))
            break
        children.append(_zarr_member_node(node, str(key), path, context, depth))
    if hidden:
        children.append(_more_node(path, hidden))
    return children


def _zarr_member_node(
    group: Any,
    key: str,
    path: str,
    context: _WalkContext,
    depth: int,
) -> MetadataNode:
    from imswitch.improcess.model.image_sources import is_zarr_array, is_zarr_group

    child_path = _join(path, key)
    try:
        member = group[key]
    except Exception as exc:
        return _error_node(key, exc, child_path)

    if is_zarr_array(member):
        return MetadataNode(
            name=key,
            kind=KIND_ARRAY,
            path=child_path,
            detail=_array_detail(member),
            children=tuple(
                _attribute_nodes(getattr(member, "attrs", {}), child_path, context, depth + 1)
            ),
        )
    if is_zarr_group(member):
        return MetadataNode(
            name=key,
            kind=KIND_GROUP,
            path=child_path,
            children=tuple(_zarr_children(member, child_path, context, depth + 1)),
        )
    return MetadataNode(
        name=key,
        kind=KIND_INFO,
        path=child_path,
        detail=type(member).__name__,
    )


# --- TIFF --------------------------------------------------------------------


def _tiff_children(file: Any, context: _WalkContext) -> list[MetadataNode]:
    children: list[MetadataNode] = []
    children.append(_tiff_flags_node(file, context))
    children.extend(_tiff_metadata_blocks(file, context))
    children.append(_tiff_series_node(file, context))
    return [child for child in children if child is not None]


def _tiff_flags_node(file: Any, context: _WalkContext) -> MetadataNode:
    """Expose the ``is_*`` format flags tifffile detected, without naming them.

    Flag names come from tifffile's own ``FILE_FLAGS`` registry plus whatever
    ``is_*`` attributes the class declares, so a newer tifffile that recognises
    more vendor flavours shows them without a change here.
    """
    path = "TIFF flags"
    entries: list[MetadataNode] = []
    for name in _tiff_flag_names(file):
        try:
            value = getattr(file, name)
        except Exception:
            continue
        if value is None or value is False:
            continue
        if not context.take():
            entries.append(_budget_limit_node(path, context.limits))
            break
        entries.append(
            MetadataNode(
                name=name,
                kind=KIND_ATTRIBUTE,
                path=_join(path, name),
                value=value,
                detail=value_detail(value),
            )
        )
    return MetadataNode(name=path, kind=KIND_GROUP, path=path, children=tuple(entries))


def _tiff_flag_names(file: Any) -> list[str]:
    names = {name for name in dir(type(file)) if name.startswith("is_")}
    try:
        import tifffile as tiff

        names.update(f"is_{flag}" for flag in tiff.TIFF.FILE_FLAGS)
    except Exception:
        pass
    return sorted(names)


def _tiff_metadata_blocks(file: Any, context: _WalkContext) -> list[MetadataNode]:
    """Every ``*_metadata`` block tifffile exposes (OME, ImageJ, vendor, ...).

    Probed by name so vendor blocks this code has never heard of still appear.
    """
    nodes: list[MetadataNode] = []
    for name in sorted(dir(file)):
        if not name.endswith("_metadata"):
            continue
        try:
            value = getattr(file, name)
        except Exception:
            continue
        if value is None:
            continue
        if isinstance(value, (str, bytes, Mapping, list, tuple)) and len(value) == 0:
            continue
        if not context.take():
            nodes.append(_budget_limit_node("", context.limits))
            break
        nodes.append(_attribute_node(name, value, "", context, depth=1))
    return nodes


def _tiff_series_node(file: Any, context: _WalkContext) -> MetadataNode:
    path = "Series"
    entries: list[MetadataNode] = []
    try:
        series_list = list(file.series)
    except Exception as exc:
        return MetadataNode(
            name=path,
            kind=KIND_GROUP,
            path=path,
            children=(_error_node("Could not read series", exc, _join(path, "!error")),),
        )

    shown, hidden = _limit(series_list, context.limits.max_children)
    for index, series in enumerate(shown):
        if not context.take():
            entries.append(_budget_limit_node(path, context.limits))
            break
        entries.append(_tiff_series_entry(series, index, path, context))
    if hidden:
        entries.append(_more_node(path, hidden))
    return MetadataNode(name=path, kind=KIND_GROUP, path=path, children=tuple(entries))


def _tiff_series_entry(series: Any, index: int, path: str, context: _WalkContext) -> MetadataNode:
    name = str(getattr(series, "name", None) or f"series_{index}")
    series_path = _join(path, name)
    children: list[MetadataNode] = []
    for key in ("axes", "shape", "dtype", "kind", "is_multifile"):
        try:
            value = getattr(series, key)
        except Exception:
            continue
        if value is None:
            continue
        if isinstance(value, np.dtype):
            value = str(value)
        children.append(
            MetadataNode(
                name=key,
                kind=KIND_ATTRIBUTE,
                path=_join(series_path, key),
                value=value,
                detail=value_detail(value),
            )
        )
    tags = _tiff_tag_node(series, series_path, context)
    if tags is not None:
        children.append(tags)
    return MetadataNode(
        name=name,
        kind=KIND_GROUP,
        path=series_path,
        detail=_array_detail(series),
        children=tuple(children),
    )


def _tiff_tag_node(series: Any, path: str, context: _WalkContext) -> MetadataNode | None:
    """TIFF tags of the series' first page — the per-file baseline metadata."""
    try:
        page = series.pages[0]
        tags = list(page.tags)
    except Exception:
        return None
    if not tags:
        return None

    tag_path = _join(path, "TIFF tags")
    entries: list[MetadataNode] = []
    shown, hidden = _limit(tags, context.limits.max_children)
    for tag in shown:
        if not context.take():
            entries.append(_budget_limit_node(tag_path, context.limits))
            break
        try:
            tag_name = str(getattr(tag, "name", None) or getattr(tag, "code", "tag"))
            value = tag.value
        except Exception:
            continue
        entries.append(_attribute_node(tag_name, value, tag_path, context, depth=2))
    if hidden:
        entries.append(_more_node(tag_path, hidden))
    return MetadataNode(
        name="TIFF tags", kind=KIND_GROUP, path=tag_path, children=tuple(entries)
    )


# --- attributes and value expansion ------------------------------------------


def _attribute_nodes(
    attrs: Any,
    path: str,
    context: _WalkContext,
    depth: int,
) -> list[MetadataNode]:
    try:
        items = sorted(dict(attrs).items(), key=lambda item: str(item[0]))
    except Exception as exc:
        return [_error_node("Could not read attributes", exc, _join(path, "!attrs"))]

    nodes: list[MetadataNode] = []
    shown, hidden = _limit(items, context.limits.max_children)
    for key, value in shown:
        if not context.take():
            nodes.append(_budget_limit_node(path, context.limits))
            break
        nodes.append(_attribute_node(str(key), value, path, context, depth + 1))
    if hidden:
        nodes.append(_more_node(path, hidden))
    return nodes


def _attribute_node(
    name: str,
    value: Any,
    path: str,
    context: _WalkContext,
    depth: int,
) -> MetadataNode:
    node_path = _join(path, name)
    return MetadataNode(
        name=name,
        kind=KIND_ATTRIBUTE,
        path=node_path,
        value=value,
        detail=value_detail(value),
        children=tuple(_value_children(value, node_path, context, depth)),
    )


def _value_children(
    value: Any,
    path: str,
    context: _WalkContext,
    depth: int,
) -> list[MetadataNode]:
    """Expand a structured attribute value into child nodes.

    Nested dicts, lists of dicts, and strings holding JSON or XML are opened up
    so an ``ome`` blob or an NGFF ``multiscales`` entry reads as a hierarchy
    instead of one unreadable line.
    """
    if depth >= context.limits.max_depth:
        return [_depth_limit_node(path, context.limits)]

    expanded = _expandable(value)
    if expanded is None:
        return []

    nodes: list[MetadataNode] = []
    if isinstance(expanded, Mapping):
        items = list(expanded.items())
        shown, hidden = _limit(items, context.limits.max_children)
        for key, child_value in shown:
            if not context.take():
                nodes.append(_budget_limit_node(path, context.limits))
                break
            nodes.append(_attribute_node(str(key), child_value, path, context, depth + 1))
        if hidden:
            nodes.append(_more_node(path, hidden))
        return nodes

    items = list(expanded)
    shown, hidden = _limit(items, context.limits.max_children)
    for index, child_value in enumerate(shown):
        if not context.take():
            nodes.append(_budget_limit_node(path, context.limits))
            break
        nodes.append(_attribute_node(f"[{index}]", child_value, path, context, depth + 1))
    if hidden:
        nodes.append(_more_node(path, hidden))
    return nodes


def _expandable(value: Any) -> Any:
    """Return the structure to expand for ``value``, or None to keep it a leaf.

    Flat sequences of scalars stay leaves — they already read fine on one line.
    """
    if isinstance(value, (bytes, bytearray)):
        value = _decode_bytes(value)
    if isinstance(value, str):
        return _parse_embedded_document(value)
    if isinstance(value, Mapping):
        return value if value else None
    if isinstance(value, np.ndarray):
        if value.dtype != object and not value.dtype.names:
            return None
        listed = value.tolist()
        return _expandable_sequence(listed) if isinstance(listed, list) else None
    if isinstance(value, (list, tuple)):
        return _expandable_sequence(value)
    return None


def _expandable_sequence(value: Any) -> Any:
    """A sequence is only worth expanding when it holds structure, not scalars."""
    if not value:
        return None
    if any(isinstance(item, (Mapping, list, tuple, np.ndarray)) for item in value):
        return value
    return None


def _parse_embedded_document(text: str) -> Any:
    """Parse a string attribute that actually carries JSON or XML."""
    stripped = text.strip()
    if len(stripped) < 2:
        return None
    if stripped[0] in "{[":
        try:
            parsed = json.loads(stripped)
        except Exception:
            return None
        return parsed if isinstance(parsed, (Mapping, list)) and parsed else None
    if stripped.startswith("<"):
        try:
            import tifffile as tiff

            parsed = tiff.xml2dict(stripped)
        except Exception:
            return None
        return parsed if isinstance(parsed, Mapping) and parsed else None
    return None


# --- node helpers ------------------------------------------------------------


def _root_node(path: Path, children: list[MetadataNode]) -> MetadataNode:
    # The full path lives in the File group (and in the panel header); keeping
    # it out of the root's detail column stops it from dictating column widths.
    return MetadataNode(
        name=path.name or str(path),
        kind=KIND_ROOT,
        path="",
        children=tuple(children),
    )


def _file_info_node(path: Path, format_id: str | None) -> MetadataNode:
    entries: list[MetadataNode] = [
        MetadataNode(
            name="path",
            kind=KIND_ATTRIBUTE,
            path="File/path",
            value=str(path),
            detail="str",
        )
    ]
    if format_id:
        entries.append(
            MetadataNode(
                name="format",
                kind=KIND_ATTRIBUTE,
                path="File/format",
                value=format_id,
                detail="str",
            )
        )
    try:
        stat = path.stat()
    except OSError:
        stat = None
    if stat is not None:
        if path.is_dir():
            size, complete = _directory_size(path)
        else:
            size, complete = stat.st_size, True
        entries.append(
            MetadataNode(
                name="size",
                kind=KIND_ATTRIBUTE,
                path="File/size",
                value=size,
                detail="bytes" if complete else "bytes (partial)",
            )
        )
        entries.append(
            MetadataNode(
                name="modified",
                kind=KIND_ATTRIBUTE,
                path="File/modified",
                value=datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                detail="str",
            )
        )
    return MetadataNode(name="File", kind=KIND_GROUP, path="File", children=tuple(entries))


#: A chunked Zarr timelapse can hold hundreds of thousands of chunk files;
#: stat-ing all of them just to print a size would stall the panel, so the
#: walk stops here and the value is reported as partial.
_MAX_SIZE_WALK_FILES = 5000


def _directory_size(path: Path) -> tuple[int, bool]:
    """``(bytes, complete)`` for a directory-backed store (Zarr), best effort."""
    total = 0
    counted = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                if counted >= _MAX_SIZE_WALK_FILES:
                    return total, False
                counted += 1
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    continue
    except OSError:
        return total, False
    return total, True


def _error_node(name: str, exc: Exception, path: str) -> MetadataNode:
    return MetadataNode(
        name=name,
        kind=KIND_ERROR,
        path=path,
        value=f"{type(exc).__name__}: {exc}",
        detail="error",
    )


def _more_node(path: str, hidden: int) -> MetadataNode:
    return MetadataNode(
        name=f"… {hidden} more entries not shown",
        kind=KIND_INFO,
        path=_join(path, "!more"),
        detail="truncated",
    )


def _depth_limit_node(path: str, limits: MetadataLimits) -> MetadataNode:
    return MetadataNode(
        name=f"… nesting deeper than {limits.max_depth} levels not shown",
        kind=KIND_INFO,
        path=_join(path, "!depth"),
        detail="truncated",
    )


def _budget_limit_node(path: str, limits: MetadataLimits) -> MetadataNode:
    return MetadataNode(
        name=f"… stopped after {limits.max_nodes} entries",
        kind=KIND_INFO,
        path=_join(path, "!budget"),
        detail="truncated",
    )


def _limit(items, max_items: int):
    items = list(items)
    if len(items) <= max_items:
        return items, 0
    return items[:max_items], len(items) - max_items


def _join(path: str, name: str) -> str:
    return f"{path}/{name}" if path else str(name)


def _array_detail(array: Any) -> str:
    shape = getattr(array, "shape", None)
    dtype = getattr(array, "dtype", None)
    parts = []
    if shape is not None:
        parts.append(str(tuple(shape)))
    if dtype is not None:
        parts.append(str(dtype))
    return " ".join(parts)


# --- value formatting --------------------------------------------------------


def _format_value_full(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return _decode_bytes(value)
    if isinstance(value, np.generic):
        return _format_scalar(value.item())
    if isinstance(value, np.ndarray):
        return _format_array(value)
    if isinstance(value, Mapping):
        return _format_json_like(value)
    if isinstance(value, (list, tuple, set)):
        return _format_json_like(list(value))
    return _format_scalar(value)


def _format_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, (bytes, bytearray)):
        return _decode_bytes(value)
    return str(value)


def _format_array(value: np.ndarray) -> str:
    if value.size == 0:
        return f"[] ({value.dtype})"
    if value.size <= 16:
        flat = [_format_scalar(item) for item in value.reshape(-1).tolist()]
        return "[" + ", ".join(flat) + "]"
    head = [_format_scalar(item) for item in value.reshape(-1)[:8].tolist()]
    return "[" + ", ".join(head) + f", …] (shape={tuple(value.shape)}, {value.dtype})"


def _format_json_like(value: Any) -> str:
    try:
        return json.dumps(_json_safe(value, DEFAULT_LIMITS), default=str)
    except Exception:
        return str(value)


def _decode_bytes(value: bytes | bytearray) -> str:
    return bytes(value).decode("utf-8", "replace")


def _json_safe(value: Any, limits: MetadataLimits) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return _decode_bytes(value)
    if isinstance(value, np.generic):
        return _json_safe(value.item(), limits)
    if isinstance(value, np.ndarray):
        return _json_safe(value.tolist(), limits)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item, limits) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item, limits) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


__all__ = [
    "DEFAULT_LIMITS",
    "KIND_ARRAY",
    "KIND_ATTRIBUTE",
    "KIND_ERROR",
    "KIND_GROUP",
    "KIND_INFO",
    "KIND_ROOT",
    "MetadataLimits",
    "MetadataNode",
    "format_metadata_value",
    "metadata_tree_from_container",
    "metadata_tree_rows",
    "metadata_tree_to_dict",
    "read_metadata_tree",
    "value_detail",
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
