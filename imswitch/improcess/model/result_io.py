"""One way to write an image result, in every container we support.

Before this, saving a result as TIFF was ``tifffile.imwrite(path, data)`` --
pixels and nothing else. A cropped reconstruction reopened as an anonymous
array: no pixel size, no axis labels, and no record of the crop. The
calibration was known at every point up to the moment of writing and then
thrown away, which is the one place it cannot be recovered from.

Three containers, one description. :class:`OmeImageMeta` is the shared
vocabulary (the same one ImControl records with, so a reconstruction and its
raw data describe their axes identically), and the processing footprint rides
along as JSON in whatever each format offers for text:

- OME-TIFF   OME-XML in the ImageDescription tag; footprint as an annotation
- HDF5       ``element_size_um`` for Fiji, OME-XML, footprint as an attribute
- OME-NGFF   ``multiscales`` under ``ome``; footprint in the group attributes

Formats are chosen by the file suffix, so "save as .h5" means HDF5 without a
second control to keep in step with the filename.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from imswitch.imcommon.model.ome_metadata import (
    _SPACE_UNIT,
    OmeAxis,
    OmeImageMeta,
    build_ome_xml,
)
from imswitch.improcess.model.footprint import HISTORY_KEY, history_of
from imswitch.improcess.model.provenance import PROVENANCE_KEY

#: Suffix -> canonical format name. The canonical names are what `save(fmt=)`
#: takes, and what the file dialog's filters resolve to.
SUFFIX_FORMATS = {
    "tif": "tiff", "tiff": "tiff", "ome.tif": "tiff", "ome.tiff": "tiff",
    "h5": "hdf5", "hdf5": "hdf5", "hdf": "hdf5",
    "zarr": "zarr", "ome.zarr": "zarr",
}

#: What the save dialog offers, in order. OME-TIFF first: it is the one every
#: other program reads.
SAVE_FILTERS = (
    ("OME-TIFF", "tiff", ".ome.tif"),
    ("HDF5", "hdf5", ".h5"),
    ("OME-Zarr", "zarr", ".ome.zarr"),
)

#: Axis label -> OME axis. Anything else is carried as a plain space axis so an
#: unusual label cannot make a result unsaveable.
_AXIS_TYPES = {"x": "space", "y": "space", "z": "space", "t": "time", "c": "channel"}


class UnsupportedResultFormat(ValueError):
    """The requested container cannot hold this result."""


def file_dialog_filter() -> str:
    """Qt name filter covering every writable format."""
    return ";;".join(
        f"{label} (*{suffix})" for label, _fmt, suffix in SAVE_FILTERS
    )


def format_for_path(path, default: str = "tiff") -> str:
    """The format a filename asks for.

    Two-part suffixes are checked first so ``.ome.zarr`` is Zarr rather than
    being read as a bare ``.zarr`` that happens to follow ``.ome``.
    """
    name = Path(path).name.lower()
    for suffix, fmt in sorted(SUFFIX_FORMATS.items(), key=lambda kv: -len(kv[0])):
        if name.endswith("." + suffix):
            return fmt
    return default


def ome_meta_for_result(result, *, name: str = "") -> OmeImageMeta:
    """Describe ``result``'s axes and calibration in OME terms."""
    data = np.asarray(getattr(result, "data", None))
    labels = [str(label) for label in getattr(result, "axis_labels", []) or []]
    scales = [float(value) for value in getattr(result, "axis_scales", []) or []]
    unit = str(getattr(result, "scale_unit", "px") or "px")

    # An axis with no label still needs one, or the axis count disagrees with
    # the data and OmeImageMeta refuses the pair outright.
    while len(labels) < data.ndim:
        labels.insert(0, "Q")
    while len(scales) < len(labels):
        scales.insert(0, 1.0)

    # OME has no "pixels" unit: an uncalibrated result says so by carrying a
    # scale of 1 with no unit, rather than claiming 1 µm per pixel.
    ome_unit = None if unit in ("px", "pixel", "pixels", "") else unit
    if ome_unit == "um":
        ome_unit = _SPACE_UNIT

    # OME names each axis exactly once, so an unrecognised label cannot simply
    # become "z": a result with two of them would claim two Z axes, and
    # tifffile refuses that outright ("multiple 'Z' dimensions"). Each axis
    # takes the first name still free instead.
    axes = []
    used: set[str] = set()
    for label in (labels[: data.ndim] if data.ndim else labels):
        key = label.lower()[:1]
        if key not in _AXIS_TYPES or key in used:
            key = next((name for name in "ztcyx" if name not in used), "")
        if not key:
            # More axes than OME has names for. Described as far as it goes;
            # the writer falls back to a plain container for the rest.
            break
        used.add(key)
        kind = _AXIS_TYPES.get(key, "space")
        axes.append(
            OmeAxis(
                key,
                kind,
                ome_unit if kind == "space" else (None if kind == "channel" else "s"),
            )
        )
    return OmeImageMeta(
        name=name or str(getattr(result, "name", "") or "result"),
        axes=axes,
        scale=scales[: len(axes)],
        dtype=data.dtype if data.size else None,
    )


def result_annotations(result, extra: dict | None = None) -> dict[str, Any]:
    """Everything about ``result`` worth writing beside the pixels.

    ``extra`` is what a specific result type knows and the generic path does
    not. It is passed through rather than merged onto ``result.metadata``: not
    every result even has that attribute, so merging silently dropped the
    extras for the ones that do not -- and a save has no business modifying
    the thing it is saving.
    """
    annotations: dict[str, Any] = {}
    metadata = getattr(result, "metadata", None)
    if isinstance(metadata, dict):
        # The provenance graph is written by its own path (Phase 2 of the
        # workflows plan), not flattened through json_safe, which would
        # truncate its nested nodes into summaries.
        annotations.update(
            {k: v for k, v in metadata.items() if k not in (HISTORY_KEY, PROVENANCE_KEY)}
        )
    if extra:
        annotations.update({str(k): v for k, v in extra.items()})
    for attribute in ("result_uid", "dataset_uid", "coordinate_space_uid", "kind"):
        value = getattr(result, attribute, None)
        if value:
            annotations[attribute] = str(value)
    lineage = getattr(result, "lineage", ()) or ()
    if lineage:
        annotations["lineage"] = [str(item) for item in lineage]
    roi_provenance = getattr(result, "roi_provenance", None)
    if roi_provenance:
        annotations["roi_provenance"] = dict(roi_provenance)
    return annotations


def _json_attributes(result, extra: dict | None = None) -> dict[str, str]:
    """Annotations flattened to strings, for containers that only take scalars.

    Everything is JSON so a reader gets structure back rather than having to
    parse a repr; a value that will not serialise is written as its text form
    rather than taking the whole save down with it.
    """
    from imswitch.improcess.model.footprint import json_safe

    out = {HISTORY_KEY: json.dumps(history_of(result), ensure_ascii=False)}
    for key, value in result_annotations(result, extra).items():
        if isinstance(value, str):
            out[str(key)] = value
            continue
        try:
            out[str(key)] = json.dumps(json_safe(value), ensure_ascii=False)
        except (TypeError, ValueError):
            out[str(key)] = str(value)
    return out


# --------------------------------------------------------------------------
# writers
# --------------------------------------------------------------------------

def _save_tiff(result, path: Path, data: np.ndarray, meta: OmeImageMeta, extra=None) -> None:
    import tifffile

    metadata = meta.tiff_metadata(data.shape)
    # The footprint travels as an OME structured annotation, which is where
    # OME puts "how was this made" and what a compliant reader will show.
    from imswitch.improcess.model.footprint import json_safe

    description = {
        key: json_safe(value)
        for key, value in result_annotations(result, extra).items()
    }
    history = history_of(result)
    if history:
        description[HISTORY_KEY] = history
    if description:
        metadata["Description"] = json.dumps(description, ensure_ascii=False)

    # Say what the samples are instead of letting tifffile infer it. Left to
    # guess, a trailing axis of size 3 or 4 is read as RGB(A) -- so a crop four
    # pixels wide would be written as a colour image, and with `ome=True` the
    # stored shape then disagrees with the declared axes and the write fails
    # outright. Only a result that really is RGB says so.
    photometric = "rgb" if _is_rgb(result) else "minisblack"
    try:
        tifffile.imwrite(
            str(path), data, ome=True, metadata=metadata, photometric=photometric
        )
    except Exception:
        # OME cannot describe every array -- more axes than it has names for,
        # or a shape it will not accept. Refusing to write would be worse than
        # writing a plainer file: the previous behaviour lost the metadata on
        # every save and still always produced one. The description, which
        # carries the calibration and the footprint, goes in either way.
        metadata.pop("Description", None)
        tifffile.imwrite(
            str(path), data,
            description=json.dumps(description, ensure_ascii=False) or None,
            metadata=metadata, photometric=photometric,
        )


def _is_rgb(result) -> bool:
    from imswitch.improcess.model.result import result_kind

    try:
        return result_kind(result) == "rgb"
    except Exception:
        return False


def _save_hdf5(result, path: Path, data: np.ndarray, meta: OmeImageMeta, extra=None) -> None:
    import h5py

    with h5py.File(str(path), "w") as handle:
        dataset = handle.create_dataset("data", data=data)
        # Fiji reads element_size_um; everything else reads the OME-XML.
        dataset.attrs["element_size_um"] = meta.element_size_um()
        dataset.attrs["axis_labels"] = ",".join(
            str(label) for label in getattr(result, "axis_labels", []) or []
        )
        dataset.attrs["scale_unit"] = str(getattr(result, "scale_unit", "px"))
        try:
            handle.attrs["ome_xml"] = build_ome_xml(meta, data.shape)
        except Exception:
            # A container that cannot express this array's axes is still worth
            # writing: the pixels and the footprint are the point.
            pass
        for key, value in _json_attributes(result, extra).items():
            handle.attrs[key] = value


def _save_zarr(result, path: Path, data: np.ndarray, meta: OmeImageMeta, extra=None) -> None:
    import zarr

    root = zarr.open_group(str(path), mode="w")
    if hasattr(root, "create_array"):
        array = root.create_array("0", shape=data.shape, dtype=data.dtype)
        array[...] = data
    else:  # zarr 2
        array = root.create_dataset("0", data=data)
    attributes = dict(root.attrs)
    try:
        attributes["ome"] = meta.ngff_ome_metadata(path="0", ndim=data.ndim)
    except Exception:
        # An array OME cannot describe still gets written, with its axis
        # labels and footprint in the plain attributes.
        pass
    attributes.update(_json_attributes(result, extra))
    root.attrs.update(attributes)


_WRITERS = {"tiff": _save_tiff, "hdf5": _save_hdf5, "zarr": _save_zarr}


def save_image_result(result, path, fmt: str | None = None, extra: dict | None = None) -> str:
    """Write ``result`` to ``path``; returns the format actually used.

    The single writer for anything with a ``data`` array. A result type with
    genuinely different content -- a localization table, a curve, a per-
    timepoint HDF5 layout -- keeps its own ``save``, but an image is an image.
    ``extra`` is whatever that result type knows and the generic path does not
    (a projection's axis, a denoiser's model), recorded alongside the rest.
    """
    path = Path(path)
    fmt = str(fmt or format_for_path(path)).lower()
    fmt = SUFFIX_FORMATS.get(fmt, fmt)
    writer = _WRITERS.get(fmt)
    if writer is None:
        raise UnsupportedResultFormat(
            f"Cannot write {fmt!r}; supported formats are "
            f"{', '.join(sorted(_WRITERS))}"
        )
    data = np.asarray(getattr(result, "data", None))
    if data.ndim == 0:
        raise UnsupportedResultFormat("Result has no image data to write")
    path.parent.mkdir(parents=True, exist_ok=True)
    writer(result, path, data, ome_meta_for_result(result), extra)
    return fmt


__all__ = [
    "SAVE_FILTERS",
    "SUFFIX_FORMATS",
    "UnsupportedResultFormat",
    "file_dialog_filter",
    "format_for_path",
    "ome_meta_for_result",
    "result_annotations",
    "save_image_result",
]
