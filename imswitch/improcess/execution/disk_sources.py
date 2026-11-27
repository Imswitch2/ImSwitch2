"""Safe physical-source selection for the future Session incremental runner.

The six ImControl layout families use the existing, Session-tested
``LiveSource`` readers.  This adapter only resolves the detector explicitly,
without choosing the first sibling in a multi-detector recording.  The
Session's ``TimeLapseIndex`` still owns logical acquisition identity and
scope; this is *not* a second directory catalog or Auto readiness policy.

Inspection opens metadata/groups only. Pixel reads happen in the reader's
``poll()`` on the executor's I/O thread.
"""

from __future__ import annotations

from pathlib import Path

from .contracts import Incompatible
from imswitch.improcess.live.lapse_group_indices import scan_group_indices


def _detectors(group, *, hdf5: bool) -> tuple[str, ...]:
    """Find data-bearing detector groups; do not select one by sorting."""
    names = []
    for name in group.keys():
        child = group[name]
        if hdf5:
            import h5py
            if isinstance(child, h5py.Group) and "data" in child:
                names.append(str(name))
            elif isinstance(child, h5py.Dataset) and child.ndim == 3:
                names.append(str(name))
        else:
            from imswitch.improcess.model.image_sources import is_zarr_group
            if is_zarr_group(child) and "data" in child:
                names.append(str(name))
    return tuple(sorted(names))


def inspect_detector_names(
    path: str | Path, *, timepoint_start: int = 0,
) -> tuple[str, ...]:
    """Recognize detectors in single-store or ``scanN``-group Zarr/HDF5.

    Multi-file lapse stores are inspected at the selected seed item; siblings
    are never read to infer a detector default. If the source is not readable
    yet, the caller may retry without ever queueing an accidental detector.
    """
    from imswitch.improcess.live.source_type import format_id_for_path
    fmt = format_id_for_path(path)
    if fmt == "hdf5":
        from imswitch.improcess.model.hdf5_access import (
            assert_raw_hdf5_new_reader_available, open_raw_hdf5,
        )
        assert_raw_hdf5_new_reader_available(str(path))
        with open_raw_hdf5(str(path)) as root:
            indices = scan_group_indices(root.keys())
            group = root[f"scan{int(timepoint_start) if int(timepoint_start) in indices else indices[0]}"] if indices else root
            return _detectors(group, hdf5=True)
    if fmt == "zarr":
        import zarr
        from imswitch.improcess.model.image_sources import is_zarr_array
        root = zarr.open(str(path), mode="r")
        if is_zarr_array(root):
            name = root.attrs.get("recording:detector_name")
            return (str(name),) if name else ("raw",)
        indices = scan_group_indices(root.keys())
        group = root[f"scan{int(timepoint_start) if int(timepoint_start) in indices else indices[0]}"] if indices else root
        return _detectors(group, hdf5=False)
    raise Incompatible(f"No incremental Zarr/HDF5 reader for {path}")


def make_incremental_disk_source(
    path: str | Path, *, detector_name: str | None = None,
    source_type=None, chunk_size: int | None = None,
    timepoint_start: int = 0, timepoint_stop: int | None = None,
):
    """Select a physical reader, refusing ambiguous multi-detector input.

    Returns ``(source, resolved_detector)``. Passing an explicit detector
    preserves existing single-detector workflows and prevents silently
    reconstructing a different sibling. No new detector-selection UX is added.
    This is intentionally NOT the Follow Auto readiness gate (Milestone 2).
    """
    available = inspect_detector_names(path, timepoint_start=timepoint_start)
    if detector_name is not None:
        if available and detector_name not in available:
            raise Incompatible(
                f"Detector {detector_name!r} not found; available: {available}"
            )
        selected = detector_name
    elif len(available) == 1:
        selected = available[0]
    elif len(available) > 1:
        raise Incompatible(
            f"Ambiguous multi-detector recording ({', '.join(available)}); "
            "select a detector explicitly"
        )
    else:
        raise Incompatible("No data-bearing detector found in recording")

    from imswitch.improcess.live.source_factory import make_live_source
    source = make_live_source(
        path, source_type=source_type, detector_name=selected,
        chunk_size=chunk_size, timepoint_start=timepoint_start,
        timepoint_stop=timepoint_stop,
    )
    return source, selected
