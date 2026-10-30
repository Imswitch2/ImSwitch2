"""Restore an ImProcess-saved image result without routing it through raw data."""

from __future__ import annotations

from pathlib import Path
import json


from imswitch.improcess.model.DataObj import DataObj
from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.provenance import PROVENANCE_KEY
from imswitch.improcess.model.provenance_io import read_provenance


def _decode_annotation(value):
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
    return value


def _tiff_description_payload(path: Path) -> dict:
    """Return the JSON Description payload written by ImProcess TIFF saves."""
    import tifffile
    from xml.etree import ElementTree

    with tifffile.TiffFile(str(path)) as handle:
        ome = getattr(handle, "ome_metadata", None)
        if ome:
            try:
                root = ElementTree.fromstring(ome)
                for node in root.iter():
                    if node.tag.rsplit("}", 1)[-1] != "Description" or not node.text:
                        continue
                    value = _decode_annotation(node.text)
                    if isinstance(value, dict):
                        return value
            except ElementTree.ParseError:
                pass
        try:
            value = _decode_annotation(handle.pages[0].description)
        except Exception:
            value = None
        return value if isinstance(value, dict) else {}


def read_saved_result_metadata(path: str | Path, backing=None) -> dict:
    """Read persisted result annotations needed after restoring a saved result.

    HDF5/Zarr expose ImProcess annotations as attributes; TIFF carries them in
    the JSON Image/OME Description.  Values written as JSON text are decoded
    back to their structured form.
    """
    path = Path(path).expanduser().resolve()
    metadata = {}
    owned_backing = None
    if backing is None and not path.name.lower().endswith((".tif", ".tiff")):
        datasets = list(DataObj.getDatasetNames(str(path)))
        dataset_name = (
            "data" if "data" in datasets else
            "0" if "0" in datasets else
            datasets[0] if len(datasets) == 1 else None
        )
        if dataset_name is not None:
            owned_backing = DataObj(path.name, dataset_name, path=str(path))
            owned_backing.checkAndOpenData()
            backing = owned_backing
    try:
        attrs = getattr(backing, "attrs", None) if backing is not None else None
        if isinstance(attrs, dict):
            for key in ("monalisa_calibration",):
                if key in attrs:
                    metadata[key] = _decode_annotation(attrs[key])
        if "monalisa_calibration" not in metadata and path.name.lower().endswith((".tif", ".tiff")):
            payload = _tiff_description_payload(path)
            if "monalisa_calibration" in payload:
                metadata["monalisa_calibration"] = _decode_annotation(payload["monalisa_calibration"])
        return metadata
    finally:
        if owned_backing is not None:
            owned_backing.checkAndUnloadData()


def _display_name(path: Path, document) -> str:
    """Name used when a saved result is restored into Reconstruction objects.

    The artifact record is authoritative for files written by current ImProcess.
    Older Session files did not store the original result name; for those, the
    saved filename is a much better reconstruction-object label than the
    provenance plugin label (``MoNaLISA``), which describes how it was made but
    not which result object this is.
    """
    artifact = document.artifact or {}
    original = artifact.get("result_name")
    if original:
        return str(original)

    name = path.name
    for suffix in (".ome.zarr", ".ome.tiff", ".ome.tif", ".hdf5", ".tiff", ".zarr", ".h5", ".tif"):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def load_saved_result(
    path: str | Path, *, session_origin: str | None = None
) -> ArrayProcessingResult:
    """Restore a saved image result as a lazy ``ProcessingResult``.

    This is intentionally separate from FileIO's raw-data loader.  A backing
    ``DataObj`` is used only as the existing container/metadata reader; it is
    retained on the returned result so an HDF5 handle stays alive while the
    lazy array is in the reconstruction list.
    """
    path = Path(path).expanduser().resolve()
    document = read_provenance(path)
    datasets = list(DataObj.getDatasetNames(str(path)))
    if not datasets:
        raise ValueError(f"{path.name} contains no image dataset")
    # ImProcess generic result writers use ``data`` in HDF5 and ``0`` in
    # OME-Zarr. HDF5 time-lapse results may additionally carry ``t_seconds``;
    # that coordinate dataset is metadata, not a second reconstruction image.
    if "data" in datasets:
        dataset_name = "data"
    elif "0" in datasets:
        dataset_name = "0"
    elif len(datasets) == 1:
        dataset_name = datasets[0]
    else:
        raise ValueError(
            f"{path.name} contains {len(datasets)} datasets and no canonical saved-result image"
        )

    backing = DataObj(path.name, dataset_name, path=str(path))
    backing.checkAndOpenData()
    data = backing.data_handle
    if data is None:
        raise ValueError(f"Could not open image data in {path.name}")

    metadata = read_saved_result_metadata(path, backing)
    if document.graph is not None:
        metadata[PROVENANCE_KEY] = document.graph
    result = ArrayProcessingResult(
        _display_name(path, document),
        data,
        list(backing.axis_labels or []),
        axis_scales=(list(backing.axis_scales) if backing.axis_scales else None),
        scale_unit=str(backing.scale_unit or "px"),
        metadata=metadata,
    )

    # Generic identity fields are written beside every image result. Restore
    # them when available so reopening does not pretend this is a brand-new
    # reconstruction object.
    try:
        attrs = backing.attrs or {}
        for attr in ("result_uid", "dataset_uid", "coordinate_space_uid"):
            value = attrs.get(attr)
            if value:
                setattr(result, attr, str(value))
        lineage = attrs.get("lineage")
        if isinstance(lineage, str):
            import json
            try:
                lineage = json.loads(lineage)
            except ValueError:
                lineage = None
        if isinstance(lineage, (list, tuple)):
            result.lineage = tuple(str(item) for item in lineage)
    except Exception:
        pass

    result._session_saved_path = str(path)
    result._session_restored = True
    result._session_backing_data_obj = backing
    if session_origin:
        # Lifecycle/debug metadata only. Active-Session association is always
        # resolved from provenance, never from this origin marker.
        result._session_origin = str(session_origin)
    return result


__all__ = ["load_saved_result", "read_saved_result_metadata"]
