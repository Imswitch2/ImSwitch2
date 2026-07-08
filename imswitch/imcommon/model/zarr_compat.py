"""Small compatibility shims for the zarr 2.x/3.x API split."""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import unquote, urlparse

import zarr


_V3_ONLY_CREATE_ARRAY_KWARGS = {
    "chunk_key_encoding",
    "config",
    "serializer",
    "shards",
    "storage_options",
    "write_data",
}


def install_zarr_create_array_compat() -> None:
    """Provide ``Group.create_array`` on zarr 2.x.

    The project still allows zarr>=2.10, while newer tests and code use the
    zarr 3 spelling. Keep the shim narrow: only group classes without a native
    create_array method are patched, and calls are forwarded to create_dataset.
    """
    for group_cls in _zarr_group_classes():
        if hasattr(group_cls, "create_array") or not hasattr(group_cls, "create_dataset"):
            continue

        def create_array(self, name: str, **kwargs: Any) -> Any:
            dimension_names = kwargs.pop("dimension_names", None)
            attributes = kwargs.pop("attributes", None)
            chunks = kwargs.pop("chunks", "auto")
            kwargs = {
                key: value
                for key, value in kwargs.items()
                if key not in _V3_ONLY_CREATE_ARRAY_KWARGS
            }
            if chunks != "auto":
                kwargs["chunks"] = chunks

            array = self.create_dataset(name, **kwargs)
            if attributes:
                for key, value in attributes.items():
                    array.attrs[key] = value
            write_zarr_json_sidecar(array, dimension_names)
            return array

        setattr(group_cls, "create_array", create_array)


def write_zarr_json_sidecar(array: Any, dimension_names: Any | None) -> None:
    """Write the dimension_names field expected from zarr 3 metadata.

    On zarr 3 this is normally handled by the library. On zarr 2 the store uses
    .zarray/.zattrs, but a tiny zarr.json sidecar preserves the metadata
    contract used by the NGFF tests without changing the readable zarr 2 array.
    """
    if dimension_names is None:
        return
    array_path = _node_filesystem_path(array)
    if not array_path:
        return

    try:
        os.makedirs(array_path, exist_ok=True)
        json_path = os.path.join(array_path, "zarr.json")
        metadata = {}
        if os.path.exists(json_path):
            try:
                with open(json_path, encoding="utf-8") as handle:
                    metadata = json.load(handle)
            except (OSError, ValueError, TypeError):
                metadata = {}
        metadata["dimension_names"] = list(dimension_names)
        tmp_path = f"{json_path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(metadata, handle, separators=(",", ":"))
        os.replace(tmp_path, json_path)
    except OSError:
        return


def _zarr_group_classes() -> list[type]:
    classes = []
    for candidate in (
        getattr(zarr, "Group", None),
        getattr(getattr(zarr, "hierarchy", None), "Group", None),
    ):
        if isinstance(candidate, type) and candidate not in classes:
            classes.append(candidate)
    return classes


def _node_filesystem_path(node: Any) -> str | None:
    store_path = getattr(node, "store_path", None)
    if isinstance(store_path, str):
        parsed = urlparse(store_path)
        if parsed.scheme == "file":
            return unquote(parsed.path)
        if parsed.scheme == "":
            return os.fspath(store_path)

    store = getattr(node, "store", None)
    root = None
    for attr in ("path", "root", "dir_path"):
        value = getattr(store, attr, None)
        if isinstance(value, (str, os.PathLike)):
            root = os.fspath(value)
            break
    if root is None:
        return None

    relative = getattr(node, "path", "") or getattr(node, "name", "")
    relative = str(relative).strip("/")
    if not relative:
        return root
    return os.path.join(root, *relative.split("/"))
