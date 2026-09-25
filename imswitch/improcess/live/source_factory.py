"""Pick the ``LiveSource`` that reads a given recording.

Pure dispatch: a :class:`~imswitch.improcess.live.source_type.RawSourceType`
maps onto exactly one of the six reader classes, and nothing here opens a store
to decide. Classification happens once, in
:mod:`~imswitch.improcess.live.source_type`, so the same answer serves both the
compatibility gate and this factory -- rather than the store being opened once
to ask "can this plugin use it?" and again to ask "what reads it?".

============  =======================  ==============================
format        layout                   reader
============  =======================  ==============================
``zarr``      ``single``               ``ZarrLiveSource``
``zarr``      ``multifile_lapse``      ``ZarrMultiFileLapseSource``
``zarr``      ``single_lapse_file``    ``ZarrLapseSource``
``hdf5``      ``single``               ``Hdf5LiveSource``
``hdf5``      ``multifile_lapse``      ``Hdf5MultiFileLapseSource``
``hdf5``      ``single_lapse_file``    ``Hdf5LapseSource``
============  =======================  ==============================
"""

from pathlib import Path

from .source_type import (
    FORMAT_HDF5,
    FORMAT_ZARR,
    format_id_for_path,
    LAYOUT_MULTIFILE_LAPSE,
    LAYOUT_SINGLE,
    LAYOUT_SINGLE_LAPSE_FILE,
    RawSourceType,
    probe_source_type,
)
from .sources import (
    Hdf5LapseSource,
    Hdf5LiveSource,
    Hdf5MultiFileLapseSource,
    LiveSource,
    ZarrLapseSource,
    ZarrLiveSource,
    ZarrMultiFileLapseSource,
)


#: ``(format, layout)`` -> reader class. Complete by construction: every pair
#: the classifier can produce has an entry, so a missing one is a bug rather
#: than a silently wrong reader.
_READERS: dict[tuple[str, str], type[LiveSource]] = {
    (FORMAT_ZARR, LAYOUT_SINGLE): ZarrLiveSource,
    (FORMAT_ZARR, LAYOUT_MULTIFILE_LAPSE): ZarrMultiFileLapseSource,
    (FORMAT_ZARR, LAYOUT_SINGLE_LAPSE_FILE): ZarrLapseSource,
    (FORMAT_HDF5, LAYOUT_SINGLE): Hdf5LiveSource,
    (FORMAT_HDF5, LAYOUT_MULTIFILE_LAPSE): Hdf5MultiFileLapseSource,
    (FORMAT_HDF5, LAYOUT_SINGLE_LAPSE_FILE): Hdf5LapseSource,
}


def reader_class_for(source_type: RawSourceType) -> type[LiveSource]:
    """The reader class for a classified recording.

    Raises:
        ValueError: no reader covers this ``(format, layout)`` pair.
    """
    key = (source_type.format_id, source_type.layout)
    try:
        return _READERS[key]
    except KeyError:
        raise ValueError(
            f"No live source reads a {source_type.format_id} "
            f"{source_type.layout} recording"
        ) from None


def make_live_source(
    path: str | Path,
    source_type: RawSourceType | None = None,
    *,
    fmt: str | None = None,
    detector_name: str | None = None,
    chunk_size: int | None = None,
) -> LiveSource:
    """Create the reader for the recording at ``path``.

    Args:
        path: The dataset to read. For a multi-file lapse this is the
            timepoint-0 store; the reader finds its siblings from there.
        source_type: The recording's classification. Pass the one the
            compatibility gate already obtained -- omitting it re-probes, which
            means opening the store a second time for an answer already known.
        fmt: Force the container format (``'zarr'`` / ``'hdf5'`` / ``'h5'``)
            instead of reading it off the suffix.
        detector_name: Detector to read; ``None`` auto-detects (the first).
        chunk_size: Override the poll chunk size.

    Without a ``source_type`` the store is probed, and a store that cannot be
    read -- it does not exist yet, or is still being written -- falls back to
    the **single-dataset** reader for its format. That is the layout-agnostic
    answer, and it keeps the factory usable as pure suffix dispatch for a path
    nothing has written yet.

    Raises:
        NotImplementedError: ``path`` is a TIFF, which no live source reads.
        ValueError: the format cannot be determined, or no reader covers it.
    """
    if source_type is None:
        source_type = probe_source_type(path)

    if source_type is None:
        format_id = _format_id(path, fmt)
        # Layout is unknowable without reading, and "one dataset" is the
        # assumption that claims the least.
        source_type = RawSourceType(format_id, LAYOUT_SINGLE, 1, 1)

    reader = reader_class_for(source_type)

    if source_type.layout == LAYOUT_MULTIFILE_LAPSE:
        # Seed up front: these derive the sibling-name template from it.
        #
        # num_timepoints is deliberately NOT passed. A value given here wins
        # over the recorder's own, and bounds the run -- while the
        # classification's count is only what existed at probe time. Handing
        # it over would cap a still-growing acquisition at however many files
        # happened to be on disk when the watcher first looked.
        return reader(path, detector_name=detector_name, chunk_size=chunk_size)
    return reader(detector_name=detector_name, chunk_size=chunk_size)


def _format_id(path: str | Path, fmt: str | None) -> str:
    """Container format from an explicit override, else from the suffix."""
    if fmt is not None:
        normalized = str(fmt).strip().lower().lstrip(".")
        if normalized in {"hdf5", "h5", "hdf"}:
            return FORMAT_HDF5
        if normalized == "zarr":
            return FORMAT_ZARR
        raise ValueError(
            f"Unsupported format {fmt!r}. Supported formats: 'zarr', 'hdf5'."
        )

    suffix = Path(path).suffix.lower()
    if suffix in {".tif", ".tiff"}:
        raise NotImplementedError(
            "TIFF live sources are not supported; no LiveSource reads a TIFF."
        )

    format_id = format_id_for_path(path)
    if format_id is None:
        raise ValueError(
            f"Cannot determine format from path suffix {suffix!r}. "
            f"Provide an explicit fmt parameter ('zarr' or 'hdf5')."
        )
    return format_id


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
