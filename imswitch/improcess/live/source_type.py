"""What one raw recording *is* -- separately from how it gets read.

A job resolves to exactly one :class:`RawSourceType`: the container format, the
on-disk layout, and the two numbers describing the data's shape. That answers
two questions that are otherwise tangled together:

  * **which reader?** -- layout and format pick one of the six ``LiveSource``
    classes (:mod:`~imswitch.improcess.live.source_factory`);
  * **should we even start?** -- the selected reconstructor is asked whether it
    can work on this shape, *before* a reader is built. MoNaLISA needs stacks
    of frames per timepoint; a pass-through viewer does not.

Keeping the two apart is the point of this module. Classification embedded in
source construction can only be consulted by building the source, which is
exactly what a compatibility gate must avoid doing.

Layout is decided from evidence, strongest first:

  1. ``scan0``/``scan1``/... groups inside the store -- structural, and true of
     legacy files that carry no recorder metadata at all;
  2. the recorder's own ``recording:num_timepoints`` and
     ``recording:single_lapse_file``;
  3. otherwise a single dataset, which claims the least.

The pure half (:func:`raw_source_type_from_evidence`) reads dictionaries and is
testable without creating a store. :func:`probe_source_type` is the only
function here that touches disk.
"""

import os
from dataclasses import dataclass
from typing import Any

import numpy as np

from imswitch.improcess.model.dataset_sources import HDF5_SPEC, ZARR_SPEC

# Reuse the recorder-metadata readers rather than keeping a second set that can
# drift: _meta_lookup handles both the flat ImSwitch2 layout and legacy nested
# ``ImswitchData``, _coerce_positive_int rejects the literal "null" older files
# write for a missing value, and _derive_scan_frames_per_stack already encodes
# the fallback chain down to ScanTTL / ScanStage geometry.
from .sources import (
    Hdf5LiveSource,
    ZarrLiveSource,
    _highest_lapse_index_span,
    _lapse_index_template,
    _coerce_positive_int,
    _derive_scan_frames_per_stack,
    _meta_lookup,
    _zarr_store_streamable,
)


FORMAT_ZARR = ZARR_SPEC.id
FORMAT_HDF5 = HDF5_SPEC.id

#: One dataset: a snap, or any non-lapse recording mode (SpecFrames, SpecTime,
#: ScanOnce, UntilStop). Read by ``ZarrLiveSource`` / ``Hdf5LiveSource``.
LAYOUT_SINGLE = "single"
#: One file per timepoint -- ``<name>_scanNN`` siblings in a folder. Read by
#: the ``*MultiFileLapseSource`` classes.
LAYOUT_MULTIFILE_LAPSE = "multifile_lapse"
#: One file holding every timepoint as a ``scanN`` group. Read by the
#: ``*LapseSource`` classes.
LAYOUT_SINGLE_LAPSE_FILE = "single_lapse_file"

LAYOUTS = (
    LAYOUT_SINGLE,
    LAYOUT_MULTIFILE_LAPSE,
    LAYOUT_SINGLE_LAPSE_FILE,
)


def _coerce_bool(value: Any, default: bool = False) -> bool:
    """Read a boolean recorder attribute stored as a bool, a number, or text.

    Zarr round-trips a real bool through JSON, but HDF5 and older writers store
    ``0``/``1`` or the strings ``"True"``/``"False"`` -- and ``bool("False")``
    is ``True``, which is the trap this exists to avoid.
    """
    if value is None:
        return default
    if isinstance(value, (bytes, np.bytes_)):
        value = value.decode(errors="ignore")
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "yes", "on", "1"}:
            return True
        if text in {"false", "no", "off", "0"}:
            return False
        return default
    if isinstance(value, (list, tuple, np.ndarray)):
        array = np.asarray(value).flatten()
        if array.size != 1:
            return default
        value = array[0]
    try:
        return bool(value)
    except (TypeError, ValueError):
        return default


def is_scan_group_name(name: Any) -> bool:
    """Whether ``name`` is a ``scan0`` / ``scan17`` timepoint group."""
    text = str(name)
    return text.startswith("scan") and text[4:].isdigit()


@dataclass(frozen=True)
class RawSourceType:
    """The shape of one raw recording, as the live path sees it.

    Attributes:
        format_id: :data:`FORMAT_ZARR` or :data:`FORMAT_HDF5`.
        layout: one of :data:`LAYOUTS`.
        frames_per_stack: raw frames making up one timepoint. ``1`` for a snap
            or a plain camera recording; ``nx * ny`` for a scan.
        num_timepoints: timepoints the recording spans; ``1`` when not a lapse.
    """

    format_id: str
    layout: str
    frames_per_stack: int
    num_timepoints: int

    @property
    def is_timelapse(self) -> bool:
        """Whether this recording spans more than one timepoint."""
        return self.num_timepoints > 1

    @property
    def has_frame_stacks(self) -> bool:
        """Whether a timepoint is a *stack* of frames rather than one image.

        The discriminator a scanning reconstructor needs, and independent of
        :attr:`is_timelapse`: a camera lapse is fifty timepoints of one frame
        each, with nothing for a pattern reassignment to work on.
        """
        return self.frames_per_stack > 1

    @property
    def total_frames(self) -> int:
        """Frames across the whole recording, gaps included."""
        return self.frames_per_stack * self.num_timepoints


def format_id_for_path(path: Any) -> str | None:
    """``"zarr"`` / ``"hdf5"`` for ``path``, or ``None`` if it is neither.

    Name-only, so it costs no I/O and answers correctly for a store that is
    still being written.
    """
    name = os.path.basename(str(path).rstrip("/\\")).lower()
    for spec in (ZARR_SPEC, HDF5_SPEC):
        if name.endswith(spec.suffixes):
            return spec.id
    return None


def raw_source_type_from_evidence(
    attrs: dict[str, Any] | None,
    format_id: str,
    *,
    scan_groups: int = 0,
    sibling_span: int = 1,
    stack_info: Any = None,
    frames_in_seed: int | None = None,
) -> RawSourceType:
    """Classify a recording from its metadata and structure.

    Args:
        attrs: The seed dataset's attributes, flat or nested.
        format_id: :data:`FORMAT_ZARR` or :data:`FORMAT_HDF5`.
        scan_groups: How many ``scanN`` groups the store holds. Non-zero is
            decisive on its own: a file laid out that way *is* a single-file
            lapse. The count also floors the timepoint total, which is how a
            legacy store carrying no metadata still reports its real length.
        sibling_span: How many timepoint slots the ``_scanNN`` files beside
            this one span, gaps included; ``1`` when it stands alone. Like
            the group count this is structural, and it is the only evidence
            a multi-file lapse leaves on a seed whose recorder wrote no
            lapse metadata.
        stack_info: Optional ``StackInfo`` from a reader that already opened
            the store. Better informed than the attributes alone -- for a
            completed store carrying nothing, the reader falls back to the
            array's own length, which no attribute records.

    Anything not written by an ImSwitch lapse has none of this, and absent
    evidence resolves to a single dataset of single frames: the safe default,
    and the right answer for a snap, a non-lapse recording, or a foreign file.
    """
    attrs = attrs or {}

    num_timepoints = max(
        _coerce_positive_int(_meta_lookup(attrs, "recording:num_timepoints")) or 1,
        int(scan_groups),
        int(sibling_span),
    )
    frames_per_stack = (
        _derive_scan_frames_per_stack(attrs)
        or _coerce_positive_int(getattr(stack_info, "frames_per_stack", None))
        or _coerce_positive_int(getattr(stack_info, "expected_frames", None))
        or _coerce_positive_int(_meta_lookup(attrs, "recording:expected_frames"))
        or _coerce_positive_int(frames_in_seed)
        or 1
    )
    single_lapse_file = _coerce_bool(
        _meta_lookup(attrs, "recording:single_lapse_file")
    )

    if scan_groups or (single_lapse_file and num_timepoints > 1):
        layout = LAYOUT_SINGLE_LAPSE_FILE
    elif sibling_span > 1 or num_timepoints > 1:
        layout = LAYOUT_MULTIFILE_LAPSE
    else:
        layout = LAYOUT_SINGLE

    return RawSourceType(
        format_id=format_id,
        layout=layout,
        frames_per_stack=frames_per_stack,
        num_timepoints=num_timepoints,
    )


def probe_source_type(seed_path: Any) -> RawSourceType | None:
    """Open ``seed_path`` far enough to classify it, or ``None`` if not yet.

    ``None`` is not an error: it means "ask again later". A store still being
    written, a file the recorder still holds, an entry that vanished between
    discovery and here -- all are ordinary states on a watched folder, and all
    resolve on a later poll. Callers should leave such a job queued rather than
    report a failure.

    The single-store reader for the format does the opening, so the per-format
    knowledge of *where* attributes live is not duplicated here: Zarr keeps
    them on the array (or the first detector's array under a group), HDF5
    splits them between the file root and the dataset and merges the two. Both
    readers resolve that already and return a merged ``StackInfo``.

    Read-only and metadata-only -- no frame is fetched -- and the reader is
    closed before returning, so nothing is held open on the caller's thread.
    """
    format_id = format_id_for_path(seed_path)
    if format_id is None:
        return None

    # Zarr publishes its own readiness: a store is safe to follow once it is
    # write-complete or carries the committed-frames barrier. HDF5 has no
    # equivalent, because its reader opens in SWMR mode -- which is what makes
    # reading a file the recorder still holds work at all -- so there the
    # attempt is the test.
    # Structure first: a single-file lapse has no root-level array for the
    # readiness check to inspect, so gating on it would reject the layout
    # outright rather than report it.
    scan_groups = _scan_group_count(seed_path, format_id)
    sibling_span = _sibling_lapse_span(seed_path)
    if (not scan_groups and format_id == FORMAT_ZARR
            and not _zarr_store_streamable(seed_path)):
        return None

    source = ZarrLiveSource() if format_id == FORMAT_ZARR else Hdf5LiveSource()
    stack_info = None
    try:
        stack_info = source.open(seed_path)
    except Exception:
        # A single-file lapse does not open as a single store -- its frames
        # live under scanN groups -- so a failure here is still a classified
        # recording when the structure already said what it is.
        if not scan_groups:
            return None
    finally:
        # A reader that raised part-way may not have every attribute set, and a
        # probe must never be the thing that raises.
        try:
            source.close()
        except Exception:
            pass

    attrs = getattr(stack_info, "attrs", None)
    frames_in_seed = None
    if scan_groups:
        # A lapse file keeps its frames inside the scanN groups, so the
        # root says nothing about stack size. Read the first group, or a
        # scanning recording stored this way reports one frame per
        # timepoint and fails the has_frame_stacks gate.
        group_attrs, frames_in_seed = _scan0_evidence(seed_path, format_id)
        attrs = {**(group_attrs or {}), **(attrs or {})}
    if not attrs:
        attrs = _root_attrs(seed_path, format_id)
    return raw_source_type_from_evidence(
        attrs,
        format_id,
        scan_groups=scan_groups,
        sibling_span=sibling_span,
        stack_info=stack_info,
        frames_in_seed=frames_in_seed,
    )


def _scan_group_count(path: Any, format_id: str) -> int:
    """How many ``scanN`` timepoint groups the store holds; ``0`` if none.

    Structural, so it answers for legacy files written before the recorder
    stamped ``recording:single_lapse_file`` -- and the count is the store's
    real timepoint total when no attribute records it.
    """
    try:
        if format_id == FORMAT_ZARR:
            import zarr

            root = zarr.open(str(path), mode="r")
            if not hasattr(root, "keys"):
                return 0
            return sum(1 for key in root.keys() if is_scan_group_name(key))

        import h5py

        with h5py.File(str(path), "r", libver="latest", swmr=True) as handle:
            return sum(1 for key in handle.keys() if is_scan_group_name(key))
    except Exception:
        return 0


def _root_attrs(path: Any, format_id: str) -> dict[str, Any]:
    """Root-level attributes, for a store no single-dataset reader could open."""
    try:
        if format_id == FORMAT_ZARR:
            import zarr

            return dict(zarr.open(str(path), mode="r").attrs)

        import h5py

        with h5py.File(str(path), "r", libver="latest", swmr=True) as handle:
            return dict(handle.attrs)
    except Exception:
        return {}


def _scan0_evidence(path: Any, format_id: str) -> "tuple[dict, int | None]":
    """Attributes and frame count of the first ``scanN`` group.

    A single-file lapse stores every timepoint as a group, so the recorder
    metadata and the stack size live one level down. Returns ``({}, None)``
    when nothing can be read.
    """
    try:
        if format_id == FORMAT_ZARR:
            import zarr

            root = zarr.open(str(path), mode="r")
            names = sorted(
                (k for k in root.keys() if is_scan_group_name(k)),
                key=lambda k: int(str(k)[4:]),
            )
            if not names:
                return {}, None
            return _first_array_evidence(root[names[0]])

        import h5py

        with h5py.File(str(path), "r", libver="latest", swmr=True) as handle:
            names = sorted(
                (k for k in handle.keys() if is_scan_group_name(k)),
                key=lambda k: int(str(k)[4:]),
            )
            if not names:
                return {}, None
            return _first_array_evidence(handle[names[0]])
    except Exception:
        return {}, None


def _first_array_evidence(group: Any) -> "tuple[dict, int | None]":
    """Merge a scan group's own attrs with its first array's."""
    attrs = dict(getattr(group, "attrs", {}) or {})
    frames = None
    for key in list(getattr(group, "keys", lambda: [])()):
        node = group[key]
        shape = getattr(node, "shape", None)
        if shape is None:
            continue
        attrs = {**attrs, **dict(getattr(node, "attrs", {}) or {})}
        frames = int(shape[0]) if len(shape) >= 3 else None
        break
    return attrs, frames

def _sibling_lapse_span(path: Any) -> int:
    """Timepoint slots the ``_scanNN`` files beside ``path`` span, gaps included.

    ``1`` when the name carries no scan index, or nothing matches beside it.

    This is the same derivation ``ZarrMultiFileLapseSource`` performs to
    follow its siblings, done here so the classification agrees with what
    the reader would do -- and because a seed written by a recorder that
    stamped no lapse metadata leaves this as its only evidence. Without it a
    real timelapse classifies as a single dataset and exactly one timepoint
    is reconstructed.
    """
    try:
        template = _lapse_index_template(str(path))
        if template is None:
            return 1
        return max(1, int(_highest_lapse_index_span(template, template[4])))
    except Exception:
        return 1

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
