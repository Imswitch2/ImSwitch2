"""Reading a provenance document back out of a file.

Every container ImProcess writes carries the document somewhere:

* OME-TIFF / plain TIFF: JSON in the ``ImageDescription`` (OME puts it in the
  image's ``Description`` element);
* HDF5: a root attribute;
* OME-NGFF / Zarr: a group attribute;
* CSV and anything else without a metadata slot: a listed
  ``<name>.provenance.json`` companion.

Files written before the graph existed carry only the linear
``processing_history`` (schema 0); they come back as a document with no
graph and that history, so a caller can still say "made with these steps"
even if it cannot replay them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from imswitch.improcess.model.footprint import HISTORY_KEY
from imswitch.improcess.model.provenance import PROVENANCE_KEY, validate_graph
from imswitch.improcess.model.save_protocol import (
    ProvenanceDocument,
    companion_json_path,
)


class ProvenanceReadError(ValueError):
    """The file exists but its provenance could not be read."""


def read_provenance(path, *, validate: bool = True) -> ProvenanceDocument:
    """The document carried by ``path``; empty when the file carries none.

    ``validate`` runs :func:`~.provenance.validate_graph` on any graph found,
    because a file is not trusted just because we write files like it.
    """
    path = Path(path)
    if not path.exists():
        raise ProvenanceReadError(f"{path} does not exist")
    name = path.name.lower()
    if name.endswith((".zarr", ".ome.zarr")) or path.is_dir():
        document = _read_zarr(path)
    elif name.endswith((".tif", ".tiff")):
        document = _read_tiff(path)
    elif name.endswith((".h5", ".hdf5", ".hdf")):
        document = _read_hdf5(path)
    elif name.endswith(".provenance.json"):
        document = _read_declared_json(path)
    else:
        document = _read_companion(path)
    if validate and document.graph is not None:
        validate_graph(document.graph)
    return document


# --------------------------------------------------------------------------

def _document_from_payload(payload: Any) -> ProvenanceDocument:
    """A document from whatever JSON object a container stored.

    Accepts the modern form (``{"provenance": {...}, "processing_history": [...]}``),
    a document written at the top level, and a bare schema-0 history.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            # A description that is not JSON is not provenance (a plain
            # ImageJ or user description); nothing was declared, nothing is lost.
            return ProvenanceDocument()
    if not isinstance(payload, dict):
        return ProvenanceDocument()
    inner = payload.get(PROVENANCE_KEY)
    if isinstance(inner, str):
        try:
            inner = json.loads(inner)
        except (TypeError, ValueError) as exc:
            # Present but unreadable is a different thing from absent: this
            # file *declared* provenance and it is corrupt. Say so rather
            # than falling back to an older history that may not match.
            raise ProvenanceReadError(f"the declared provenance is not valid JSON: {exc}") from exc
    if PROVENANCE_KEY in payload and not isinstance(inner, dict):
        raise ProvenanceReadError("the declared provenance is not a JSON object")
    if isinstance(inner, dict):
        try:
            document = ProvenanceDocument.from_dict(inner, strict=True)
        except ValueError as exc:
            raise ProvenanceReadError(str(exc)) from exc
        if not document.history:
            document.history = _history(payload)
        return document
    if "graph" in payload or "artifact" in payload:
        return ProvenanceDocument.from_dict(payload)
    return ProvenanceDocument(history=_history(payload))


def _history(payload: dict) -> list:
    history = payload.get(HISTORY_KEY)
    if isinstance(history, str):
        try:
            history = json.loads(history)
        except (TypeError, ValueError):
            history = []
    return list(history) if isinstance(history, list) else []


def _read_tiff(path: Path) -> ProvenanceDocument:
    import tifffile

    with tifffile.TiffFile(str(path)) as handle:
        ome = handle.ome_metadata
        if ome:
            document = _document_from_ome_xml(ome)
            if document is not None:
                return document
        description = None
        try:
            description = handle.pages[0].description
        except Exception:
            description = None
        if handle.imagej_metadata:
            # The ImageJ description is key=value lines; our JSON rides in
            # a dedicated key.
            declared = handle.imagej_metadata.get(PROVENANCE_KEY)
            if declared:
                return _document_from_payload({PROVENANCE_KEY: declared})   # strict: it was declared
            text = handle.imagej_metadata.get("Description")
            if text:
                document = _document_from_payload(text)
                if document.graph is not None or document.history:
                    return document
        if description:
            return _document_from_payload(description)
    return ProvenanceDocument()


def _document_from_ome_xml(xml: str) -> ProvenanceDocument | None:
    from xml.etree import ElementTree

    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return None
    namespace = root.tag.split("}")[0].strip("{") if "}" in root.tag else ""
    tag = f"{{{namespace}}}Description" if namespace else "Description"
    for node in root.iter(tag):
        if node.text:
            document = _document_from_payload(node.text)
            if document.graph is not None or document.history:
                return document
    return None


def _read_hdf5(path: Path) -> ProvenanceDocument:
    import h5py

    with h5py.File(str(path), "r") as handle:
        attrs = {key: handle.attrs[key] for key in handle.attrs.keys()}
    return _document_from_payload(_decode_attrs(attrs))


def _read_zarr(path: Path) -> ProvenanceDocument:
    import zarr

    root = zarr.open_group(str(path), mode="r")
    return _document_from_payload(dict(root.attrs))


def _read_companion(path: Path) -> ProvenanceDocument:
    companion = companion_json_path(path)
    if companion.exists():
        return _read_declared_json(companion)
    return ProvenanceDocument()


def _read_declared_json(path: Path) -> ProvenanceDocument:
    """A ``.provenance.json`` file: its existence declares provenance, so a
    file that is not a JSON document is an error, not an empty document."""
    try:
        return ProvenanceDocument.from_json(path.read_text(encoding="utf-8"), strict=True)
    except ValueError as exc:
        raise ProvenanceReadError(f"{path.name}: {exc}") from exc


def _decode_attrs(attrs: dict) -> dict:
    decoded = {}
    for key, value in attrs.items():
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        elif hasattr(value, "item") and not isinstance(value, (str, bytes)):
            try:
                value = value.item()
            except Exception:
                pass
        decoded[str(key)] = value
    return decoded


__all__ = ["ProvenanceReadError", "read_provenance"]


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
