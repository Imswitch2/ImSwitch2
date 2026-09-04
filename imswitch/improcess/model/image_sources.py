"""Image-array resolution helpers for improcess input files.

This layer resolves logical dataset names to concrete array handles and source
metadata. It deliberately sits below reconstructors and above file/container
opening so HDF5, legacy Zarr, and OME-NGFF Zarr can share one dataset contract.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from typing import Any

import h5py
import numpy as np
import tifffile as tiff
import zarr

from imswitch.imcommon.model.zarr_compat import install_zarr_create_array_compat
from imswitch.imcommon.model.acquisition_layout import (
    AcquisitionLayout,
    decode_acquisition_layout,
)
from imswitch.imcommon.model.acquisition_metadata import (
    RecordingLifecycle,
    RecordingLifecycleMarkers,
    flatten_acquisition_metadata,
    normalize_recording_lifecycle,
)


install_zarr_create_array_compat()


_ZarrGroup = getattr(zarr, "Group", None) or getattr(getattr(zarr, "hierarchy", None), "Group", None)
_ZarrArray = getattr(zarr, "Array", None) or getattr(getattr(zarr, "core", None), "Array", None)

_DEFAULT_AXIS_LABELS = ["T", "Z", "C", "Y", "X"]
_AXIS_LABEL_ALIASES = {
    "t": "T",
    "time": "T",
    "z": "Z",
    "c": "C",
    "channel": "C",
    "y": "Y",
    "x": "X",
}
_UNIT_ALIASES = {
    "micrometer": "um",
    "micrometre": "um",
    "um": "um",
    "\u00b5m": "um",
    "nanometer": "nm",
    "nanometre": "nm",
    "nm": "nm",
    "millimeter": "mm",
    "millimetre": "mm",
    "mm": "mm",
    "meter": "m",
    "metre": "m",
    "m": "m",
    "second": "s",
    "seconds": "s",
    "s": "s",
}


@dataclass(frozen=True)
class ResolvedImage:
    """One logical image resolved to an array-like object."""

    name: str
    array: Any
    attrs: dict[str, Any] = field(default_factory=dict)
    array_path: str | None = None
    source_format: str | None = None
    axis_labels: list[str] | None = None
    axis_scales: list[float] | None = None
    scale_unit: str | None = None
    acquisition_layout: AcquisitionLayout | None = None
    recording_lifecycle: RecordingLifecycle | None = None


def decode_layout_attrs(attrs: dict[str, Any]) -> AcquisitionLayout | None:
    encoded = attrs.get("AcquisitionLayout:json")
    declared_schema = attrs.get("AcquisitionLayout:schema")
    if encoded is None:
        if declared_schema is not None:
            raise ValueError("AcquisitionLayout:schema is present without AcquisitionLayout:json")
        return None
    layout = decode_acquisition_layout(encoded)
    if declared_schema is not None:
        if isinstance(declared_schema, bytes):
            declared_schema = declared_schema.decode("utf-8", "replace")
        if str(declared_schema) != layout.schema:
            raise ValueError("AcquisitionLayout:schema disagrees with the encoded layout")
    return layout


def _marker_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return None
    return bool(value)


def _marker_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, np.generic):
        value = value.item()
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _side_dataset_scalar(parent: Any, name: str) -> Any:
    if parent is None or name not in parent:
        return None
    node = parent[name]
    if not isinstance(node, h5py.Dataset) or node.size != 1:
        return None
    try:
        return node[0]
    except (IndexError, TypeError, ValueError):
        return None


def _hdf5_lifecycle(array: h5py.Dataset, attrs: dict[str, Any]) -> RecordingLifecycle:
    parent = array.parent if isinstance(array.parent, h5py.Group) else None
    markers = RecordingLifecycleMarkers(
        writing=_marker_bool(attrs.get("writing")),
        frames_committed=_marker_int(_side_dataset_scalar(parent, "frames_committed")),
        stream_complete=_marker_bool(_side_dataset_scalar(parent, "stream_complete")),
    )
    return normalize_recording_lifecycle(attrs, markers)


def _zarr_lifecycle(attrs: dict[str, Any]) -> RecordingLifecycle:
    return normalize_recording_lifecycle(
        attrs,
        RecordingLifecycleMarkers(
            writing=_marker_bool(attrs.get("writing")),
            frames_committed=_marker_int(attrs.get("recording:frames_committed")),
        ),
    )


def is_zarr_group(obj: Any) -> bool:
    return _ZarrGroup is not None and isinstance(obj, _ZarrGroup)


def is_zarr_array(obj: Any) -> bool:
    return _ZarrArray is not None and isinstance(obj, _ZarrArray)


def is_array_node(node: Any) -> bool:
    return isinstance(node, h5py.Dataset) or is_zarr_array(node)


def is_structured_detector_group(node: Any) -> bool:
    if isinstance(node, h5py.Group):
        return "data" in node and isinstance(node["data"], h5py.Dataset)
    if is_zarr_group(node):
        return "data" in node and is_zarr_array(node["data"])
    return False


def default_axis_labels(ndim: int) -> list[str]:
    if ndim <= len(_DEFAULT_AXIS_LABELS):
        return _DEFAULT_AXIS_LABELS[-ndim:]
    extra = [f"D{i}" for i in range(ndim - len(_DEFAULT_AXIS_LABELS))]
    return [*extra, *_DEFAULT_AXIS_LABELS]


def dataset_names(container: Any) -> list[str]:
    if is_zarr_group(container):
        return _zarr_dataset_names(container)
    if isinstance(container, h5py.Group):
        return _hdf5_dataset_names(container)
    if isinstance(container, tiff.TiffFile):
        return tiff_dataset_names(container)
    raise ValueError(f'Unsupported image container "{type(container).__name__}"')


def resolve_image(
    container: Any,
    dataset_name: str | None,
    *,
    validate_layout_metadata: bool = True,
) -> ResolvedImage:
    if is_zarr_group(container):
        return _resolve_zarr_image(
            container,
            dataset_name,
            validate_layout_metadata=validate_layout_metadata,
        )
    if isinstance(container, h5py.Group):
        return _resolve_hdf5_image(
            container,
            dataset_name,
            validate_layout_metadata=validate_layout_metadata,
        )
    if isinstance(container, tiff.TiffFile):
        return _resolve_tiff_image(
            container,
            dataset_name,
            validate_layout_metadata=validate_layout_metadata,
        )
    raise ValueError(f'Unsupported image container "{type(container).__name__}"')


def tiff_dataset_names(file: tiff.TiffFile) -> list[str]:
    names = []
    series_count = len(file.series)
    for index, series in enumerate(file.series):
        name = _tiff_series_name(series, index, series_count)
        names.append(name)
    return names or ["default"]


#: Prefix of the per-timepoint groups a single-file lapse recording writes.
_LAPSE_ITEM_PREFIX = "scan"


def _lapse_item_order(name: str) -> tuple[int, int, str]:
    """Sort key putting ``scan2`` before ``scan10``.

    The groups are numbered, so ordering them as text reads a twelve-point
    lapse back as 0, 1, 10, 11, 2, ... The picker shows that order and the
    multi-data list is built in it, while the live readers sort the same groups
    numerically -- two layers disagreeing about the order of one file.
    """
    ordinal = name[len(_LAPSE_ITEM_PREFIX):]
    if name.startswith(_LAPSE_ITEM_PREFIX) and ordinal.isdigit():
        return (0, int(ordinal), "")
    return (1, 0, name)


def _lapse_item_dataset_names(node: Any) -> list[str]:
    """Detector names one level inside a single-file lapse item group.

    A lapse recorded as one file writes each timepoint into its own group --
    ``scan0/Camera/data``, ``scan1/Camera/data`` -- so the detector groups the
    readers look for sit one level deeper than in every other recording. A
    reader that only looks at the root finds no datasets at all and reports the
    file as empty, which is what made single-file lapse recordings unopenable.

    Exactly one extra level is descended, and only through a plain group that
    holds detector groups, so this recognizes the lapse layout without turning
    the discovery into a general tree walk that would surface arrays the
    storers never meant as datasets.

    This is the first code in the open path that dereferences nodes it did not
    write, so a child that cannot be resolved -- a dangling link, a group whose
    backing file is gone -- is skipped rather than allowed to fail the whole
    container. Discovery answers "what can be opened here"; one unreadable
    neighbour is not an answer of "nothing".
    """
    if not (isinstance(node, h5py.Group) or is_zarr_group(node)):
        return []
    if is_structured_detector_group(node):
        return []
    names = []
    for name in sorted(node.keys()):
        try:
            child = node[name]
        except Exception:
            continue
        if is_structured_detector_group(child):
            names.append(name)
    return names


def _hdf5_dataset_names(group: h5py.Group) -> list[str]:
    names = []
    nested = []
    for name in group.keys():
        try:
            node = group[name]
        except Exception:
            continue
        if is_array_node(node) or is_structured_detector_group(node):
            names.append(name)
            continue
        nested.extend(
            (name, child) for child in _lapse_item_dataset_names(node)
        )
    nested.sort(key=lambda item: (_lapse_item_order(item[0]), item[1]))
    return names + [f"{item}/{child}" for item, child in nested]


def _resolve_hdf5_image(
    group: h5py.Group,
    dataset_name: str | None,
    *,
    validate_layout_metadata: bool,
) -> ResolvedImage:
    if dataset_name is None:
        raise ValueError("datasetName is required")

    node = group[dataset_name]
    if is_structured_detector_group(node):
        array = node["data"]
        attrs = dict(array.attrs)
        metadata = node.get("metadata")
        if metadata is not None:
            attrs.update(flatten_metadata_attrs(metadata))
        return ResolvedImage(
            name=dataset_name,
            array=array,
            attrs=attrs,
            array_path=f"{dataset_name}/data",
            axis_labels=_axis_labels_from_hdf5_attrs(attrs, array.ndim),
            axis_scales=None,
            scale_unit=None,
            source_format="hdf5",
            acquisition_layout=(
                decode_layout_attrs(attrs) if validate_layout_metadata else None
            ),
            recording_lifecycle=_hdf5_lifecycle(array, attrs),
        )

    if is_array_node(node):
        attrs = dict(node.attrs)
        return ResolvedImage(
            name=dataset_name,
            array=node,
            attrs=attrs,
            array_path=dataset_name,
            axis_labels=_axis_labels_from_hdf5_attrs(attrs, node.ndim),
            source_format="hdf5",
            acquisition_layout=(
                decode_layout_attrs(attrs) if validate_layout_metadata else None
            ),
            recording_lifecycle=_hdf5_lifecycle(node, attrs),
        )

    raise ValueError(f'Dataset "{dataset_name}" is not an array or structured detector group')


def _resolve_tiff_image(
    file: tiff.TiffFile,
    dataset_name: str | None,
    *,
    validate_layout_metadata: bool,
) -> ResolvedImage:
    series_names = tiff_dataset_names(file)
    if dataset_name is None:
        if len(series_names) != 1:
            raise RuntimeError("File contains multiple datasets")
        dataset_name = series_names[0]

    for index, series in enumerate(file.series):
        name = _tiff_series_name(series, index, len(file.series))
        if dataset_name != name:
            continue

        attrs = _tiff_series_attrs(file, index, series)
        axis_labels = _axis_labels_from_tiff_axes(getattr(series, "axes", None))
        if axis_labels is not None and len(axis_labels) != len(series.shape):
            axis_labels = None
        axis_scales, scale_unit = _axis_scales_from_tiff_attrs(attrs, axis_labels, len(series.shape))
        observed_frames = 1 if len(series.shape) <= 2 else int(series.shape[0])
        lifecycle = normalize_recording_lifecycle(
            attrs,
            RecordingLifecycleMarkers(
                writing=False,
                frames_committed=observed_frames,
            ),
        )
        return ResolvedImage(
            name=name,
            array=series,
            attrs=attrs,
            array_path=str(index),
            source_format="ome-tiff" if getattr(series, "kind", None) == "ome" else "tiff",
            axis_labels=axis_labels,
            axis_scales=axis_scales,
            scale_unit=scale_unit,
            acquisition_layout=(
                decode_layout_attrs(attrs) if validate_layout_metadata else None
            ),
            recording_lifecycle=lifecycle,
        )

    raise ValueError(f'Dataset "{dataset_name}" was not found in TIFF series {series_names}')


def _tiff_series_name(series: Any, index: int, series_count: int) -> str:
    name = getattr(series, "name", None)
    if isinstance(name, str) and name:
        return name
    if series_count == 1:
        return "default"
    return f"series_{index}"


def _tiff_series_attrs(file: tiff.TiffFile, series_index: int, series: Any) -> dict[str, Any]:
    attrs = {
        "tiff:series_index": series_index,
        "tiff:axes": getattr(series, "axes", None),
    }
    attrs.update(_ome_pixels_attrs(file, series_index))
    attrs.update(_ome_map_annotation_attrs(file, series_index))
    return attrs


def _ome_pixels_attrs(file: tiff.TiffFile, image_index: int) -> dict[str, Any]:
    metadata = getattr(file, "ome_metadata", None)
    if not metadata:
        return {}
    try:
        parsed = tiff.xml2dict(metadata)
    except Exception:
        return {}

    ome = parsed.get("OME") if isinstance(parsed, dict) else None
    if not isinstance(ome, dict):
        return {}
    images = ome.get("Image")
    if isinstance(images, dict):
        images = [images]
    if not isinstance(images, list) or image_index >= len(images):
        return {}

    image = images[image_index]
    if not isinstance(image, dict):
        return {}
    pixels = image.get("Pixels")
    if not isinstance(pixels, dict):
        return {}

    attrs = {}
    for axis in ("X", "Y", "Z"):
        size_key = f"PhysicalSize{axis}"
        unit_key = f"PhysicalSize{axis}Unit"
        if size_key in pixels:
            attrs[f"ome:{size_key}"] = pixels[size_key]
        if unit_key in pixels:
            attrs[f"ome:{unit_key}"] = pixels[unit_key]
    if "DimensionOrder" in pixels:
        attrs["ome:DimensionOrder"] = pixels["DimensionOrder"]
    if "Name" in image:
        attrs["ome:ImageName"] = image["Name"]
    return attrs


def _ome_map_annotation_attrs(file: tiff.TiffFile, image_index: int) -> dict[str, Any]:
    metadata = getattr(file, "ome_metadata", None)
    if not metadata:
        return {}
    try:
        root = ET.fromstring(metadata)
    except ET.ParseError:
        return {}

    images = root.findall("{*}Image")
    if image_index >= len(images):
        return {}
    annotation_ids = {
        reference.get("ID")
        for reference in images[image_index].findall("{*}AnnotationRef")
        if reference.get("ID")
    }
    if not annotation_ids:
        return {}
    annotations: dict[str, Any] = {}
    for annotation in root.findall(".//{*}MapAnnotation"):
        if annotation.get("ID") not in annotation_ids:
            continue
        for item in annotation.findall("./{*}Value/{*}M"):
            key = item.get("K")
            if not key:
                continue
            value = item.text or ""
            if key in {
                "recording:planned_frames",
                "recording:actual_frames",
                "recording:planned_partitions",
                "recording:actual_partitions",
            }:
                try:
                    annotations[key] = int(value)
                    continue
                except ValueError:
                    pass
            annotations[key] = value
    return annotations


def _zarr_dataset_names(group: Any) -> list[str]:
    root_image = _ngff_image(group, validate_layout_metadata=False)
    if root_image is not None:
        return [root_image.name]

    names = []
    nested = []
    for name in sorted(group.keys()):
        try:
            node = group[name]
        except Exception:
            continue
        if (
            is_array_node(node)
            or is_structured_detector_group(node)
            or _ngff_image(node, name, validate_layout_metadata=False) is not None
        ):
            names.append(name)
            continue
        nested.extend(
            (name, child) for child in _lapse_item_dataset_names(node)
        )
    nested.sort(key=lambda item: (_lapse_item_order(item[0]), item[1]))
    return names + [f"{item}/{child}" for item, child in nested]


def _resolve_zarr_image(
    group: Any,
    dataset_name: str | None,
    *,
    validate_layout_metadata: bool,
) -> ResolvedImage:
    if dataset_name is None:
        raise ValueError("datasetName is required")

    root_image = _ngff_image(
        group,
        validate_layout_metadata=validate_layout_metadata,
    )
    if root_image is not None and dataset_name == root_image.name:
        return root_image

    node = group[dataset_name]
    ngff_image = _ngff_image(
        node,
        dataset_name,
        validate_layout_metadata=validate_layout_metadata,
    )
    if ngff_image is not None:
        # _ngff_image resolves array_path relative to ``node``; rebase it onto
        # ``group`` (the store root) so callers that re-traverse from the root —
        # e.g. ZarrLiveSource during polling — actually reach the array.
        if ngff_image.array_path:
            ngff_image = replace(
                ngff_image, array_path=f"{dataset_name}/{ngff_image.array_path}"
            )
        return ngff_image

    if is_structured_detector_group(node):
        array = node["data"]
        attrs = dict(array.attrs)
        metadata = node.get("metadata")
        if metadata is not None:
            attrs.update(flatten_metadata_attrs(metadata))
        return ResolvedImage(
            name=dataset_name,
            array=array,
            attrs=attrs,
            array_path=f"{dataset_name}/data",
            source_format="zarr",
            acquisition_layout=(
                decode_layout_attrs(attrs) if validate_layout_metadata else None
            ),
            recording_lifecycle=_zarr_lifecycle(attrs),
        )

    if is_array_node(node):
        attrs = dict(node.attrs)
        return ResolvedImage(
            name=dataset_name,
            array=node,
            attrs=attrs,
            array_path=dataset_name,
            source_format="zarr",
            acquisition_layout=(
                decode_layout_attrs(attrs) if validate_layout_metadata else None
            ),
            recording_lifecycle=_zarr_lifecycle(attrs),
        )

    raise ValueError(f'Dataset "{dataset_name}" is not an array or structured detector group')


def _ngff_image(
    group: Any,
    fallback_name: str | None = None,
    *,
    validate_layout_metadata: bool = True,
) -> ResolvedImage | None:
    multiscale = _ngff_multiscale(group)
    dataset = _ngff_dataset_entry(multiscale)
    if multiscale is None or dataset is None:
        return None

    array_path = _ngff_array_path(dataset)
    if array_path is None:
        return None
    try:
        array = group[array_path]
    except Exception:
        return None
    if not is_zarr_array(array):
        return None

    name = _ngff_image_name(multiscale, array_path, fallback_name)
    axis_labels, axis_scales, scale_unit = _ngff_axis_metadata(multiscale, dataset, array)
    attrs = dict(group.attrs)
    attrs.update(dict(array.attrs))
    metadata = group.get("metadata")
    if metadata is not None:
        attrs.update(flatten_metadata_attrs(metadata))
    axes = multiscale.get("axes")
    if axes is not None:
        attrs["ngff:axes"] = axes
    transforms = dataset.get("coordinateTransformations")
    if transforms is not None:
        attrs["ngff:coordinateTransformations"] = transforms

    return ResolvedImage(
        name=name,
        array=array,
        attrs=attrs,
        array_path=array_path,
        source_format="ome-zarr",
        axis_labels=axis_labels,
        axis_scales=axis_scales,
        scale_unit=scale_unit,
        acquisition_layout=(
            decode_layout_attrs(attrs) if validate_layout_metadata else None
        ),
        recording_lifecycle=_zarr_lifecycle(attrs),
    )


def _ngff_multiscale(group: Any) -> dict[str, Any] | None:
    if not is_zarr_group(group):
        return None

    attrs = dict(group.attrs)
    multiscales = attrs.get("multiscales")
    if not multiscales:
        ome = attrs.get("ome")
        if isinstance(ome, dict):
            multiscales = ome.get("multiscales")

    if not multiscales:
        return None
    first = multiscales[0] if isinstance(multiscales, (list, tuple)) else multiscales
    return first if isinstance(first, dict) else None


def _ngff_dataset_entry(multiscale: dict[str, Any] | None) -> dict[str, Any] | None:
    if multiscale is None:
        return None
    datasets = multiscale.get("datasets")
    if not datasets:
        return None
    first = datasets[0]
    return first if isinstance(first, dict) else None


def _ngff_array_path(dataset: dict[str, Any]) -> str | None:
    path = dataset.get("path")
    return str(path).strip("/") if path is not None else None


def _ngff_image_name(
    multiscale: dict[str, Any],
    array_path: str,
    fallback_name: str | None,
) -> str:
    name = multiscale.get("name")
    if isinstance(name, str) and name:
        return name
    if fallback_name:
        return fallback_name
    return array_path


def _ngff_axis_metadata(
    multiscale: dict[str, Any],
    dataset: dict[str, Any],
    array: Any,
) -> tuple[list[str] | None, list[float] | None, str | None]:
    axes = multiscale.get("axes")
    axis_labels = _axis_labels_from_ngff_axes(axes)
    if axis_labels is not None and len(axis_labels) != len(array.shape):
        axis_labels = None

    axis_scales = _axis_scales_from_transformations(
        dataset.get("coordinateTransformations")
    )
    if axis_scales is not None and len(axis_scales) != len(array.shape):
        axis_scales = None

    scale_unit = _scale_unit_from_ngff_axes(axes)
    return axis_labels, axis_scales, scale_unit


def _axis_labels_from_ngff_axes(axes: Any) -> list[str] | None:
    if not isinstance(axes, list):
        return None

    labels = []
    for index, axis in enumerate(axes):
        if isinstance(axis, dict):
            name = axis.get("name") or axis.get("type")
        else:
            name = axis
        if name is None:
            labels.append(f"D{index}")
        else:
            labels.append(_canonical_axis_label(str(name)))
    return labels


def _axis_labels_from_tiff_axes(axes: str | None) -> list[str] | None:
    if not axes:
        return None
    return [_canonical_axis_label(axis) for axis in str(axes)]


def _axis_labels_from_hdf5_attrs(attrs: dict[str, Any], ndim: int) -> list[str] | None:
    """Read axis labels from an ImSwitch/OME HDF5 ``axes`` attr.

    The OME recording storer writes ``axes`` (e.g. ``"TYX"`` or ``["T","Y","X"]``)
    alongside ``element_size_um``. Legacy pre-OME recordings have no such attr, so
    this returns ``None`` and the reader falls back to ``default_axis_labels`` —
    keeping old files loading exactly as before.
    """
    axes = attrs.get("axes")
    if axes is None:
        return None
    if isinstance(axes, (bytes, bytearray)):
        axes = axes.decode("utf-8", "ignore")
    if isinstance(axes, str):
        items = list(axes)
    elif isinstance(axes, (list, tuple, np.ndarray)):
        items = [
            item.decode("utf-8", "ignore") if isinstance(item, (bytes, bytearray)) else str(item)
            for item in axes
        ]
    else:
        return None
    labels = [_canonical_axis_label(str(item)) for item in items if str(item).strip()]
    if len(labels) != ndim:
        return None
    return labels


def _axis_scales_from_tiff_attrs(
    attrs: dict[str, Any],
    axis_labels: list[str] | None,
    ndim: int,
) -> tuple[list[float] | None, str | None]:
    if not axis_labels or len(axis_labels) != ndim:
        return None, None

    scales = [1.0] * ndim
    units = []
    any_scale = False
    for axis_name in ("X", "Y", "Z"):
        size_key = f"ome:PhysicalSize{axis_name}"
        if size_key not in attrs or axis_name not in axis_labels:
            continue
        try:
            scale = float(attrs[size_key])
        except (TypeError, ValueError):
            continue
        scales[axis_labels.index(axis_name)] = scale
        any_scale = True
        unit = attrs.get(f"ome:PhysicalSize{axis_name}Unit")
        if unit:
            units.append(_normalize_unit(str(unit)))

    if not any_scale:
        return None, None
    unique_units = [unit for unit in units if unit]
    if not unique_units:
        return scales, "px"
    if len(set(unique_units)) == 1:
        return scales, unique_units[0]
    return scales, unique_units[-1]


def _axis_scales_from_transformations(transformations: Any) -> list[float] | None:
    if not isinstance(transformations, list):
        return None
    for transform in transformations:
        if not isinstance(transform, dict) or transform.get("type") != "scale":
            continue
        scale = transform.get("scale")
        if not isinstance(scale, (list, tuple)):
            return None
        try:
            return [float(value) for value in scale]
        except (TypeError, ValueError):
            return None
    return None


def _scale_unit_from_ngff_axes(axes: Any) -> str | None:
    if not isinstance(axes, list):
        return None

    units = []
    for axis in axes:
        if not isinstance(axis, dict):
            continue
        axis_type = str(axis.get("type", "")).lower()
        if axis_type and axis_type != "space":
            continue
        unit = axis.get("unit")
        if unit:
            units.append(_normalize_unit(str(unit)))

    unique_units = [unit for unit in units if unit]
    if not unique_units:
        return None
    if len(set(unique_units)) == 1:
        return unique_units[0]
    return unique_units[-1]


def axis_scales_from_element_size(
    attrs: dict[str, Any] | None,
    ndim: int,
) -> tuple[list[float] | None, str | None]:
    """Derive ``(axis_scales, unit)`` from a Fiji/ImSwitch ``element_size_um`` attr.

    ``element_size_um`` is the historical ImSwitch/Fiji calibration carried on
    HDF5/Zarr arrays. Returns ``(None, None)`` when absent or unparsable so the
    caller can fall back to unit pixels.
    """
    if attrs is None:
        return None, None

    value = attrs.get("element_size_um")
    if value is None:
        return None, None

    try:
        array = np.asarray(value, dtype=float).flatten()
    except (TypeError, ValueError):
        return None, None

    if array.size == 0:
        return None, None
    if array.size == 1:
        if ndim < 2:
            return [float(array[0])] * ndim, "um"
        return [1.0] * max(ndim - 2, 0) + [float(array[0]), float(array[0])], "um"
    if array.size == ndim:
        return [float(scale) for scale in array], "um"
    if array.size < ndim:
        return [1.0] * (ndim - array.size) + [float(scale) for scale in array], "um"
    return [float(scale) for scale in array[-ndim:]], "um"


def _canonical_axis_label(name: str) -> str:
    normalized = name.strip()
    return _AXIS_LABEL_ALIASES.get(normalized.lower(), normalized)


def _normalize_unit(unit: str) -> str:
    normalized = unit.strip()
    return _UNIT_ALIASES.get(normalized.lower(), normalized)


def flatten_metadata_attrs(metadata_group: Any, prefix: list[str] | None = None) -> dict[str, Any]:
    """Extract a native metadata tree, then use the shared mapping flattener."""

    def nested_mapping(group: Any) -> dict[str, Any]:
        nested: dict[str, Any] = dict(group.attrs)
        for name in group.keys():
            child = group[name]
            if isinstance(child, h5py.Group) or is_zarr_group(child):
                nested[name] = nested_mapping(child)
        return nested

    nested: dict[str, Any] = nested_mapping(metadata_group)
    for component in reversed(prefix or []):
        nested = {component: nested}
    return flatten_acquisition_metadata(nested)
