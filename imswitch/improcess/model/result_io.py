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

A result whose data is lazy -- a time lapse, a virtually opened file -- is
written one index of its leading axis at a time, never read whole: a lapse
larger than memory saves as readily as it opens.
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
from imswitch.improcess.model.lazy_array import plane_chunks
from imswitch.improcess.model.provenance import PROVENANCE_KEY
from imswitch.improcess.model.save_protocol import (
    SavePlan,
    description_payload,
    document_for,
    embed_hdf5,
    embed_zarr,
)

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


#: Above this, a streamed TIFF is written as BigTIFF. Classic TIFF addresses
#: 4 GiB; the margin covers the tags and the OME-XML.
_BIGTIFF_BYTES = 2 ** 32 - 2 ** 27


class UnsupportedResultFormat(ValueError):
    """The requested container cannot hold this result."""


def _is_lazy(data: Any) -> bool:
    """An array-like that reads when indexed rather than holding its values."""
    return (
        not isinstance(data, np.ndarray)
        and hasattr(data, "shape")
        and hasattr(data, "dtype")
        and hasattr(data, "__getitem__")
    )


def _shape_and_dtype(data: Any) -> tuple[tuple[int, ...], np.dtype]:
    """Shape and dtype without reading a lazy array's values."""
    if _is_lazy(data):
        return tuple(int(size) for size in data.shape), np.dtype(data.dtype)
    array = np.asarray(data)
    return tuple(array.shape), array.dtype


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
    """Describe ``result``'s axes and calibration in OME terms (via its view)."""
    view = result.serialization_view() if hasattr(result, "serialization_view") else None
    if view is None:
        from imswitch.improcess.model.result import SerializationView

        data = getattr(result, "data", None)
        view = SerializationView(
            data=data,
            axis_labels=[str(l) for l in getattr(result, "axis_labels", []) or []],
            axis_scales=[float(v) for v in getattr(result, "axis_scales", []) or []],
            scale_unit=str(getattr(result, "scale_unit", "px") or "px"),
        )
    return ome_meta_for_view(view, name=name or str(getattr(result, "name", "") or "result"))


def ome_meta_for_view(view, *, name: str = "result") -> OmeImageMeta:
    """Describe a :class:`~.result.SerializationView` in OME terms."""
    shape, dtype = _shape_and_dtype(view.data)
    ndim = len(shape)
    labels = [str(label) for label in view.axis_labels or []]
    scales = [float(value) for value in view.axis_scales or []]
    unit = str(view.scale_unit or "px")

    # An axis with no label still needs one, or the axis count disagrees with
    # the data and OmeImageMeta refuses the pair outright.
    while len(labels) < ndim:
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
    for label in (labels[:ndim] if ndim else labels):
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
    channels = [{"name": str(n)} for n in (view.channel_names or [])]
    return OmeImageMeta(
        name=name,
        axes=axes,
        scale=scales[: len(axes)],
        dtype=dtype if int(np.prod(shape)) else None,
        channels=channels,
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
            # A type-specific key (a document tag in ``extra``, say) wins over
            # the generic identity attribute of the same name.
            annotations.setdefault(attribute, str(value))
    lineage = getattr(result, "lineage", ()) or ()
    if lineage:
        annotations["lineage"] = [str(item) for item in lineage]
    roi_provenance = getattr(result, "roi_provenance", None)
    if roi_provenance:
        annotations["roi_provenance"] = dict(roi_provenance)
    return annotations


def _json_attributes(result, extra: dict | None = None, document=None) -> dict[str, str]:
    """Annotations flattened to strings, for containers that only take scalars.

    Everything is JSON so a reader gets structure back rather than having to
    parse a repr; a value that will not serialise is written as its text form
    rather than taking the whole save down with it.
    """
    from imswitch.improcess.model.footprint import json_safe

    out = {HISTORY_KEY: json.dumps(history_of(result), ensure_ascii=False)}
    if document is not None:
        out[PROVENANCE_KEY] = document.to_json()
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

def _aligned_labels(result, ndim: int) -> list[str]:
    labels = [str(label) for label in getattr(result, "axis_labels", []) or []]
    while len(labels) < ndim:
        labels.insert(0, "Q")
    return labels[-ndim:] if ndim else labels


def _plane_delta_t(result, shape, times_s) -> list[float] | None:
    """Per-plane ``DeltaT`` for OME, from one time per index of the T axis.

    OME numbers planes over every axis but the last two; each plane takes the
    time of the T index it belongs to. None when the result has no T among
    those axes or the times do not cover it -- a partial list would put times
    on the wrong planes.
    """
    if times_s is None or len(shape) < 3:
        return None
    labels = _aligned_labels(result, len(shape))
    if "T" not in labels[:-2]:
        return None
    axis = labels.index("T")
    times = list(times_s)
    if len(times) != shape[axis] or any(value is None for value in times):
        return None
    leading = tuple(shape[:-2])
    positions = np.unravel_index(np.arange(int(np.prod(leading))), leading)[axis]
    return [float(times[int(position)]) for position in positions]


def _leading_blocks(data, shape):
    """``(index, block)`` along the first axis, each read on its own."""
    if len(shape) <= 2:
        yield (), np.asarray(data[...])
        return
    for position in range(shape[0]):
        yield (position,), np.asarray(data[position])


def _tiff_pages(data, shape):
    for _index, block in _leading_blocks(data, shape):
        yield from block.reshape(-1, *shape[-2:])


def _save_tiff(result, path: Path, data, meta: OmeImageMeta, extra=None,
               document=None, times_s=None) -> None:
    import tifffile

    shape, dtype = _shape_and_dtype(data)
    metadata = meta.tiff_metadata(shape)
    # The provenance travels in the OME Description, which is where OME puts
    # "how was this made" and what a compliant reader will show.
    from imswitch.improcess.model.footprint import json_safe

    description = {
        key: json_safe(value)
        for key, value in result_annotations(result, extra).items()
    }
    if document is not None:
        description = description_payload(document, description)
    else:
        history = history_of(result)
        if history:
            description[HISTORY_KEY] = history
    if description:
        metadata["Description"] = json.dumps(description, ensure_ascii=False, default=str)

    # Say what the samples are instead of letting tifffile infer it. Left to
    # guess, a trailing axis of size 3 or 4 is read as RGB(A) -- so a crop four
    # pixels wide would be written as a colour image, and with `ome=True` the
    # stored shape then disagrees with the declared axes and the write fails
    # outright. Only a result that really is RGB says so.
    photometric = "rgb" if _is_rgb(result) else "minisblack"
    if len(shape) < 2:
        raise ValueError(
            f"cannot write {getattr(result, 'name', 'result')!r} as TIFF: a TIFF page needs "
            f"two axes, this result has {len(shape)} (shape {shape})"
        )
    delta_t = _plane_delta_t(result, shape, times_s)
    if delta_t is not None:
        plane = dict(metadata.get("Plane") or {})
        plane["DeltaT"] = delta_t
        plane["DeltaTUnit"] = ["s"] * len(delta_t)
        metadata["Plane"] = plane

    streamed = _is_lazy(data) and photometric != "rgb"
    if _is_lazy(data) and not streamed:
        data = np.asarray(data)
    options = {}
    if streamed:
        # Pages are handed over one leading index at a time; tifffile needs the
        # whole shape up front to lay out the file and the OME-XML.
        nbytes = int(np.prod(shape)) * dtype.itemsize
        options = {"shape": shape, "dtype": dtype, "bigtiff": nbytes > _BIGTIFF_BYTES}

    def pixels():
        return _tiff_pages(data, shape) if streamed else data

    try:
        tifffile.imwrite(
            str(path), pixels(), ome=True, metadata=metadata,
            photometric=photometric, **options,
        )
    except Exception:
        # OME cannot describe every array -- more axes than it has names for,
        # or a shape it will not accept. Refusing to write would be worse than
        # writing a plainer file: the previous behaviour lost the metadata on
        # every save and still always produced one. The description, which
        # carries the calibration and the footprint, goes in either way.
        metadata.pop("Description", None)
        tifffile.imwrite(
            str(path), pixels(),
            description=json.dumps(description, ensure_ascii=False, default=str) or None,
            metadata=metadata, photometric=photometric, **options,
        )


def _is_rgb(result) -> bool:
    from imswitch.improcess.model.result import result_kind

    try:
        return result_kind(result) == "rgb"
    except Exception:
        return False


def _save_hdf5(result, path: Path, data, meta: OmeImageMeta, extra=None,
               document=None, times_s=None) -> None:
    import h5py

    shape, dtype = _shape_and_dtype(data)
    with h5py.File(str(path), "w") as handle:
        if _is_lazy(data):
            dataset = handle.create_dataset(
                "data", shape=shape, dtype=dtype,
                chunks=plane_chunks(shape) if 0 not in shape and len(shape) > 2 else None,
            )
            for index, block in _leading_blocks(data, shape):
                dataset[index if index else ...] = block
        else:
            dataset = handle.create_dataset("data", data=data)
        # Fiji reads element_size_um; everything else reads the OME-XML.
        dataset.attrs["element_size_um"] = meta.element_size_um()
        dataset.attrs["axis_labels"] = ",".join(
            str(label) for label in getattr(result, "axis_labels", []) or []
        )
        dataset.attrs["scale_unit"] = str(getattr(result, "scale_unit", "px"))
        delta_t = _plane_delta_t(result, shape, times_s)
        try:
            handle.attrs["ome_xml"] = build_ome_xml(
                meta, shape,
                plane=(
                    {"DeltaT": delta_t, "DeltaTUnit": ["s"] * len(delta_t)}
                    if delta_t is not None else None
                ),
            )
        except Exception:
            # A container that cannot express this array's axes is still worth
            # writing: the pixels and the footprint are the point.
            pass
        if times_s is not None and all(value is not None for value in times_s):
            times = handle.create_dataset(
                "t_seconds", data=np.asarray(list(times_s), dtype=float)
            )
            times.attrs["unit"] = "s"
            times.attrs["axis"] = "T"
        for key, value in _json_attributes(result, extra).items():
            handle.attrs[key] = value
        if document is not None:
            embed_hdf5(handle, document)
        if meta.channels:
            handle.attrs["channel_names"] = json.dumps([c.get("name") for c in meta.channels])


def _save_zarr(result, path: Path, data, meta: OmeImageMeta, extra=None,
               document=None, times_s=None) -> None:
    import zarr

    shape, dtype = _shape_and_dtype(data)
    root = zarr.open_group(str(path), mode="w")
    lazy = _is_lazy(data)
    chunks = plane_chunks(shape) if lazy and 0 not in shape and len(shape) > 2 else None
    if hasattr(root, "create_array"):
        options = {"chunks": chunks} if chunks else {}
        array = root.create_array("0", shape=shape, dtype=dtype, **options)
    else:  # zarr 2
        array = root.create_dataset("0", shape=shape, dtype=dtype, chunks=chunks)
    if lazy:
        for index, block in _leading_blocks(data, shape):
            array[index if index else ...] = block
    else:
        array[...] = data
    attributes = dict(root.attrs)
    try:
        attributes["ome"] = meta.ngff_ome_metadata(path="0", ndim=len(shape))
    except Exception:
        # An array OME cannot describe still gets written, with its axis
        # labels and footprint in the plain attributes.
        pass
    attributes.update(_json_attributes(result, extra))
    if times_s is not None and all(value is not None for value in times_s):
        # OME-NGFF has a time scale but no per-timepoint times.
        attributes["t_seconds"] = [float(value) for value in times_s]
    root.attrs.update(attributes)
    if document is not None:
        embed_zarr(root, document)


_WRITERS = {"tiff": _save_tiff, "hdf5": _save_hdf5, "zarr": _save_zarr}


def save_image_result(result, path, fmt: str | None = None, extra: dict | None = None,
                      document=None, *, times_s=None) -> str:
    """Write ``result`` to ``path``; returns the format actually used.

    The single writer for anything with a ``data`` array. A result type with
    genuinely different content -- a localization table, a curve, a per-
    timepoint HDF5 layout -- keeps its own writer, but an image is an image.
    ``extra`` is whatever that result type knows and the generic path does not
    (a projection's axis, a denoiser's model), recorded alongside the rest.

    What is written is the result's :meth:`~.result.ProcessingResult.serialization_view`,
    so a type whose on-disk layout differs from its in-memory one keeps it.
    ``document`` is the provenance document the save protocol built for this
    file; called directly (outside the protocol) one is built for a
    single-file plan, so the file still carries its graph.

    ``times_s`` is one time in seconds per index of the result's T axis, for a
    T axis that is not evenly spaced or whose spacing is worth keeping exactly:
    OME-TIFF and HDF5's OME-XML get it as each plane's ``DeltaT``, and HDF5 and
    Zarr as a ``t_seconds`` list. The T scale still goes in as the increment.
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
    view = result.serialization_view() if hasattr(result, "serialization_view") else None
    data = view.data if view is not None else getattr(result, "data", None)
    if not _is_lazy(data):
        data = np.asarray(data)
    shape, _dtype = _shape_and_dtype(data)
    if len(shape) == 0:
        raise UnsupportedResultFormat("Result has no image data to write")
    if view is not None and view.extra:
        extra = {**(view.extra or {}), **(extra or {})}
    if document is None:
        document = document_for(result, SavePlan(path, fmt))
    path.parent.mkdir(parents=True, exist_ok=True)
    if view is not None:
        meta = ome_meta_for_view(view, name=str(getattr(result, "name", "") or "result"))
    else:
        meta = ome_meta_for_result(result)
    writer(result, path, data, meta, extra, document, times_s=times_s)
    return fmt


__all__ = [
    "SAVE_FILTERS",
    "SUFFIX_FORMATS",
    "UnsupportedResultFormat",
    "file_dialog_filter",
    "format_for_path",
    "ome_meta_for_result",
    "ome_meta_for_view",
    "result_annotations",
    "save_image_result",
]
