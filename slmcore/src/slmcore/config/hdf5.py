"""Recursive HDF5 codec for slmcore configuration dictionaries.

Encoding rules:
- mappings are stored as HDF5 groups;
- NumPy arrays are stored as datasets;
- all other supported values are JSON-encoded into HDF5 attributes.

Tuples are normalized to lists and NumPy scalars to Python scalars when read.
"""

from __future__ import annotations

import json
from typing import Any,Mapping

import h5py
import numpy as np


HDF5Parent = h5py.File | h5py.Group

_ARRAY_DTYPE_ATTR = "__slmcore_numpy_dtype__"
_SUPPORTED_ARRAY_KINDS = frozenset("biufcSU")


class HDF5CodecError(ValueError):
    """Raised when HDF5 configuration data is malformed or unsupported."""


def write_value(parent: HDF5Parent,name: str,value: Any) -> None:
    """Write one configuration value below ``parent``.

    Mappings are written recursively as groups, arrays as datasets, and all
    remaining JSON-compatible values as attributes.
    """
    _validate_name(name)

    if name in parent or name in parent.attrs:
        raise HDF5CodecError(
            f"Cannot write '{name}': that name already exists below "
            f"'{parent.name}'"
        )

    if isinstance(value,Mapping):
        group = parent.create_group(name)

        for key,item in value.items():
            if not isinstance(key,str):
                raise TypeError(
                    f"Configuration dictionary keys must be strings, got "
                    f"{type(key).__name__} below '{group.name}'"
                )

            write_value(group,key,item)

        return

    if isinstance(value,np.ndarray):
        _write_array(parent,name,value)
        return

    parent.attrs[name] = _json_dumps(value,_child_path(parent,name))


def read_value(parent: HDF5Parent,name: str) -> Any:
    """Read one configuration value stored below ``parent``."""
    _validate_name(name)

    has_child = name in parent
    has_attribute = name in parent.attrs

    if has_child and has_attribute:
        raise HDF5CodecError(
            f"Malformed HDF5 data below '{parent.name}': '{name}' exists "
            f"both as an object and an attribute"
        )

    if has_attribute:
        return _json_loads(
            parent.attrs[name],_child_path(parent,name)
        )

    if not has_child:
        raise KeyError(
            f"HDF5 configuration value '{_child_path(parent,name)}' "
            f"does not exist"
        )

    node = parent[name]

    if isinstance(node,h5py.Group):
        return _read_mapping(node)

    if isinstance(node,h5py.Dataset):
        return _read_array(node)

    raise HDF5CodecError(
        f"Unsupported HDF5 object at '{node.name}': "
        f"{type(node).__name__}"
    )


def write_mapping(group: HDF5Parent,data: Mapping[str,Any]) -> None:
    """Write all entries of a mapping directly into an existing group."""
    if not isinstance(data,Mapping):
        raise TypeError(
            f"Expected a mapping, got {type(data).__name__}"
        )

    for key,value in data.items():
        if not isinstance(key,str):
            raise TypeError(
                f"Configuration dictionary keys must be strings, got "
                f"{type(key).__name__} below '{group.name}'"
            )

        write_value(group,key,value)


def read_mapping(group: HDF5Parent) -> dict[str, Any]:
    """Read all configuration values directly contained in a group."""
    return _read_mapping(group)


# ---------------------------------------------------------------------
# Arrays
# ---------------------------------------------------------------------

def _write_array(
    parent: HDF5Parent,
    name: str,
    value: np.ndarray,
) -> None:
    array = np.asarray(value)

    if array.dtype.kind == "O":
        raise TypeError(
            f"Object-dtype arrays are not supported at "
            f"'{_child_path(parent,name)}'"
        )

    if array.dtype.kind not in _SUPPORTED_ARRAY_KINDS:
        raise TypeError(
            f"Unsupported NumPy dtype at '{_child_path(parent,name)}': "
            f"{array.dtype}"
        )

    kwargs = _array_dataset_kwargs(array)

    try:
        if array.dtype.kind == "U":
            dataset = parent.create_dataset(
                name,
                data=array.astype(object),
                dtype=h5py.string_dtype(encoding="utf-8"),
                **kwargs,
            )
            dataset.attrs[_ARRAY_DTYPE_ATTR] = array.dtype.str

        else:
            parent.create_dataset(name,data=array,**kwargs)

    except (TypeError,ValueError,RuntimeError) as error:
        raise HDF5CodecError(
            f"Could not write NumPy array at "
            f"'{_child_path(parent,name)}'"
        ) from error


def _read_array(dataset: h5py.Dataset) -> np.ndarray:
    try:
        if _ARRAY_DTYPE_ATTR in dataset.attrs:
            dtype_text = _attribute_text(
                dataset.attrs[_ARRAY_DTYPE_ATTR],
                dataset.name,
            )
            dtype = np.dtype(dtype_text)
            raw = dataset.asstr()[()]
            array = np.array(raw,dtype=dtype,copy=True)

        else:
            array = np.array(dataset[()],copy=True)

        return array.reshape(dataset.shape)

    except (TypeError,ValueError,UnicodeDecodeError) as error:
        raise HDF5CodecError(
            f"Could not read NumPy array from '{dataset.name}'"
        ) from error


def _array_dataset_kwargs(array: np.ndarray) -> dict[str, Any]:
    """Compress non-empty, non-scalar arrays."""
    if array.ndim == 0 or array.size == 0:
        return {}

    return {
        "compression":"gzip",
        "compression_opts":4,
    }


# ---------------------------------------------------------------------
# JSON leaves
# ---------------------------------------------------------------------

def _json_dumps(value: Any,path: str) -> str:
    normalized = _normalize_json_value(value,path)

    try:
        return json.dumps(
            normalized,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",",":"),
        )

    except (TypeError,ValueError) as error:
        raise TypeError(
            f"Value at '{path}' is not JSON serializable"
        ) from error


def _json_loads(value: Any,path: str) -> Any:
    text = _attribute_text(value,path)

    try:
        return json.loads(text)

    except (TypeError,json.JSONDecodeError) as error:
        raise HDF5CodecError(
            f"Attribute at '{path}' does not contain valid JSON"
        ) from error


def _normalize_json_value(value: Any,path: str) -> Any:
    """Normalize supported values before JSON encoding."""
    if isinstance(value,np.generic):
        return _normalize_json_value(value.item(),path)

    if value is None or isinstance(value,(str,bool,int,float)):
        return value

    if isinstance(value,(list,tuple)):
        return [
            _normalize_json_value(item,f"{path}[{index}]")
            for index,item in enumerate(value)
        ]

    if isinstance(value,Mapping):
        normalized = {}

        for key,item in value.items():
            if not isinstance(key,str):
                raise TypeError(
                    f"JSON dictionary keys must be strings at '{path}', got "
                    f"{type(key).__name__}"
                )

            normalized[key] = _normalize_json_value(
                item,f"{path}.{key}"
            )

        return normalized

    if isinstance(value,np.ndarray):
        raise TypeError(
            f"NumPy arrays nested inside JSON values are not supported at "
            f"'{path}'. Store the array directly as a dictionary value."
        )

    raise TypeError(
        f"Unsupported configuration value at '{path}': "
        f"{type(value).__name__}"
    )


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _read_mapping(group: HDF5Parent) -> dict[str, Any]:
    duplicate_names = set(group.keys()) & set(group.attrs.keys())

    if duplicate_names:
        raise HDF5CodecError(
            f"Malformed group '{group.name}': names exist both as objects "
            f"and attributes: {sorted(duplicate_names)}"
        )

    result = {}

    for key in group.attrs:
        result[key] = _json_loads(
            group.attrs[key],_child_path(group,key)
        )

    for key in group.keys():
        result[key] = read_value(group,key)

    return result


def _attribute_text(value: Any,path: str) -> str:
    if isinstance(value,str):
        return value

    if isinstance(value,bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as error:
            raise HDF5CodecError(
                f"Attribute at '{path}' is not valid UTF-8"
            ) from error

    if isinstance(value,np.str_):
        return str(value)

    if isinstance(value,np.bytes_):
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError as error:
            raise HDF5CodecError(
                f"Attribute at '{path}' is not valid UTF-8"
            ) from error

    raise HDF5CodecError(
        f"Attribute at '{path}' must contain JSON text, got "
        f"{type(value).__name__}"
    )


def _validate_name(name: str) -> None:
    if not isinstance(name,str):
        raise TypeError(
            f"HDF5 names must be strings, got {type(name).__name__}"
        )

    if not name:
        raise ValueError("HDF5 names cannot be empty")

    if "/" in name:
        raise ValueError(
            f"HDF5 names cannot contain '/': {name!r}"
        )


def _child_path(parent: HDF5Parent,name: str) -> str:
    if parent.name == "/":
        return f"/{name}"

    return f"{parent.name}/{name}"


__all__ = [
    "HDF5CodecError",
    "read_mapping",
    "read_value",
    "write_mapping",
    "write_value",
]