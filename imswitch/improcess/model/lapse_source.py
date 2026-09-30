"""A time lapse opened as one source, stacked lazily along T.

ImControl records a lapse one item per timepoint: a file per point
(``<name>_time07_Camera.h5``, ``<name>_scan0007_APD.zarr``) or a group per point
inside one file (``scan0/Camera``, ``scan1/Camera``). Every item says which lapse
it belongs to -- ``recording:lapse_index``, ``recording:num_timepoints``,
``recording:single_lapse_file`` -- and, for a scan lapse, whether its points
differ by time at all or by where the stage was (the lapse partition of its
acquisition layout).

Opened as separate items, a 2000-point lapse is 2000 full loads, and a
single-file lapse re-lists the whole file for each of them. Opened here it is
one source, in three steps that each touch as little as they can:

1. :func:`discover_time_lapse` opens the item the user picked, decides from its
   recorded attributes whether it is a time lapse at all, and lists its
   siblings once -- a directory listing, or the root groups of one file. No
   sibling is opened. Cheap enough for the GUI thread.
2. :func:`read_lapse_headers` opens every item for its shape, times and
   completion outcome, never its pixels. Meant for a worker thread: it reports
   progress and can be cancelled.
3. :func:`plan_time_lapse` places each item at its recorded index -- so a
   missing point is a marked gap rather than a shift of everything after it --
   and refuses items whose shapes cannot share one stack.
   :class:`LazyTimeLapseArray` then reads a timepoint only when something
   indexes it.

This is the data layer. Which T scale to show, whether an incomplete point is
marked or left out, and how the stack is exported belong to whatever uses it --
the Time lapse reconstructor today, a per-timepoint MoNaLISA or BeadRec run
later.

Copyright (C) 2020-2026 ImSwitch developers
This file is part of ImSwitch.

ImSwitch is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

ImSwitch is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

from __future__ import annotations

import os
import re
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import h5py
import numpy as np
import tifffile as tiff
import zarr

from imswitch.imcommon.model.acquisition_metadata import (
    WRITER_STATE_WRITING,
    RecordingLifecycle,
)
from imswitch.improcess.model.dataset_sources import (
    SOURCE_SPECS,
    resolve_dataset_source,
    tiling_manifest_for_path,
)
from imswitch.improcess.model.image_sources import (
    dataset_names,
    decode_layout_attrs,
    default_axis_labels,
    is_structured_detector_group,
    is_zarr_group,
    resolve_image,
)
from imswitch.improcess.model.virtual_image import virtual_array_from_native


#: ``DataObj.sourceKind`` of a whole lapse, and the id a reconstructor lists in
#: ``accepted_source_kinds`` to be handed one.
TIME_LAPSE_SOURCE_KIND = "time-lapse"

STORAGE_ONE_FILE_PER_ITEM = "one-file-per-item"
STORAGE_ONE_GROUP_PER_ITEM = "one-group-per-item"

#: Lapse partition kinds that make a lapse's points something other than
#: timepoints. ImControl writes them since the tile-partition fix; the reader
#: honours them rather than stacking a mosaic's tiles as time.
NON_TIME_PARTITION_KINDS = frozenset({"tile", "position"})

#: The per-timepoint group a single-file lapse writes. The number is the next
#: free one in the file, not the lapse index: recording a second lapse into the
#: same file carries on from where the first stopped.
_LAPSE_GROUP = re.compile(r"scan(\d+)")

#: File-name tokens a multi-file lapse inserts before the detector name.
_MULTI_FILE_TOKENS = ("time", "scan")

TIMEPOINT_COMPLETE = "complete"
#: Recorded, but short or unfinished -- stopped early, or never finalized.
TIMEPOINT_INCOMPLETE = "incomplete"
#: No usable item at this index: never written, holds no frames, or unreadable.
TIMEPOINT_MISSING = "missing"

INCOMPLETE_MARK = "mark"
INCOMPLETE_SKIP = "skip"


class NotATimeLapse(ValueError):
    """The source is readable but is not a time lapse; the message says why."""


class TimeLapseShapeError(ValueError):
    """The lapse's items cannot share one stack; the message names them."""


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LapseItemRef:
    """Where one lapse item lives: a container, and a dataset inside it.

    ``ordinal`` orders candidates before their attributes are read: the index
    in a multi-file name, or the group number in a single-file lapse. Only the
    recorded ``recording:lapse_index`` decides where an item goes in the stack.
    ``dataset`` is ``None`` when the container holds a single dataset that is
    only named once it is opened.
    """

    ordinal: int
    path: Path
    dataset: str | None


@dataclass(frozen=True)
class LapseChannel:
    """One detector's run of lapse items."""

    detector: str
    label: str
    candidates: tuple[LapseItemRef, ...]


@dataclass(frozen=True)
class TimeLapseIndex:
    """What discovery found, from the anchor's attributes and one listing.

    Nothing here comes from a sibling's contents; :func:`read_lapse_headers`
    is what opens them.
    """

    anchor: LapseItemRef
    anchor_detector: str
    storage: str
    root: Path
    planned_count: int
    anchor_index: int
    interval_s: float | None
    channels: tuple[LapseChannel, ...]
    anchor_attrs: Mapping[str, Any] = field(default_factory=dict, repr=False)
    anchor_shape: tuple[int, ...] = ()

    @property
    def name(self) -> str:
        if self.storage == STORAGE_ONE_GROUP_PER_ITEM:
            # ``<savename>_<detector>.hdf5``: the detector is named separately.
            name = _without_extension(self.root.name)
            suffix = f"_{self.anchor_detector}"
            return name[: -len(suffix)] if name.endswith(suffix) else name
        prefix = _multi_file_prefix(self.anchor.path.name, self.anchor_index,
                                    self.planned_count)
        return prefix or self.root.name

    @property
    def detectors(self) -> tuple[str, ...]:
        return tuple(channel.detector for channel in self.channels)

    def channel(self, detector: str | None = None) -> LapseChannel:
        wanted = detector or self.anchor_detector
        for channel in self.channels:
            if channel.detector == wanted:
                return channel
        raise KeyError(
            f"This lapse recorded no detector {wanted!r}; it has "
            f"{', '.join(self.detectors) or 'none'}"
        )

    def describe(self) -> str:
        channel = self.channel()
        storage = (
            "one file" if self.storage == STORAGE_ONE_GROUP_PER_ITEM
            else "one file per timepoint"
        )
        interval = (
            f", every {self.interval_s:g} s" if self.interval_s else ""
        )
        return (
            f"{self.name}: {len(channel.candidates)} of "
            f"{self.planned_count} planned timepoints found ({storage}"
            f"{interval})."
        )


def is_time_lapse_item(attrs: Mapping[str, Any]) -> bool:
    """Whether ``attrs`` are those of one item of a multi-point lapse.

    Every recording carries ``recording:num_timepoints`` -- a plain recording
    says 1 -- so presence alone proves nothing; the count has to exceed one.
    """
    count = _as_int(attrs.get("recording:num_timepoints"))
    return count is not None and count > 1 and _as_int(
        attrs.get("recording:lapse_index")
    ) is not None


def discover_time_lapse(
    path: str | Path,
    dataset: str | None = None,
) -> TimeLapseIndex:
    """Resolve any one item of a time lapse to the whole lapse.

    Raises :class:`NotATimeLapse` with the reason when ``path`` is readable
    but is a single recording, a tiling run, or a lapse whose points were
    positioned rather than timed.
    """
    source = resolve_dataset_source(path, allowed_specs=SOURCE_SPECS)
    root = Path(source.path)
    try:
        manifest = tiling_manifest_for_path(root)
    except ValueError:
        manifest = root
    if manifest is not None:
        raise NotATimeLapse(
            f"{root.name} belongs to the tiling run at {Path(manifest).parent}; "
            "its points are tiles, not timepoints. The Tiling mosaic "
            "reconstructor reassembles it."
        )

    container = _open_container(root)
    try:
        groups = _lapse_groups(container)
        if dataset is None:
            dataset = _default_dataset(container, groups)
        else:
            dataset = _item_dataset(container, groups, dataset)
        image = resolve_image(container, dataset, validate_layout_metadata=False)
        attrs = _item_attrs(container, image.attrs)
        anchor_shape = tuple(int(size) for size in image.array.shape)
        _require_time_lapse(attrs, root.name)
        planned = int(_as_int(attrs["recording:num_timepoints"]))
        anchor_index = int(_as_int(attrs["recording:lapse_index"]))
        anchor_detector = _detector_name(attrs, dataset)
        single_file = _as_bool(attrs.get("recording:single_lapse_file"))
        group_match = _LAPSE_GROUP.fullmatch(str(dataset).split("/", 1)[0])
        if single_file or (groups and group_match is not None):
            if not groups or group_match is None:
                raise NotATimeLapse(
                    f"{root.name} says it is a single-file lapse, but "
                    f"{dataset!r} is not one of its per-timepoint groups."
                )
            channels = _single_file_channels(container, groups, root)
            storage = STORAGE_ONE_GROUP_PER_ITEM
            anchor = LapseItemRef(int(group_match.group(1)), root, dataset)
            anchor_detector = dataset.split("/", 1)[1]
        else:
            names_in_file = dataset_names(container)
            channels = _multi_file_channels(
                root, anchor_index, planned, dataset, names_in_file,
            )
            storage = STORAGE_ONE_FILE_PER_ITEM
            anchor = LapseItemRef(anchor_index, root, dataset)
            anchor_detector = _channel_for(channels, anchor).detector
    finally:
        _close_container(container)

    return TimeLapseIndex(
        anchor=anchor,
        anchor_detector=anchor_detector,
        storage=storage,
        root=root if storage == STORAGE_ONE_GROUP_PER_ITEM else root.parent,
        planned_count=planned,
        anchor_index=anchor_index,
        interval_s=_as_float(attrs.get("recording:lapse_interval_s")),
        channels=channels,
        anchor_attrs=attrs,
        anchor_shape=anchor_shape,
    )


def _require_time_lapse(attrs: Mapping[str, Any], name: str) -> None:
    count = _as_int(attrs.get("recording:num_timepoints"))
    index = _as_int(attrs.get("recording:lapse_index"))
    if count is None or index is None:
        raise NotATimeLapse(
            f"{name} records no lapse position (recording:lapse_index, "
            "recording:num_timepoints); it was not recorded as a time lapse."
        )
    if count <= 1:
        raise NotATimeLapse(
            f"{name} is a single recording, not a time lapse "
            "(recording:num_timepoints is 1)."
        )
    kind = _positioned_kind(attrs)
    if kind == "tile":
        raise NotATimeLapse(
            f"{name} is one tile of a tiling run: its points differ by stage "
            "position, not time. The Tiling mosaic reconstructor reassembles it."
        )
    if kind is not None:
        raise NotATimeLapse(
            f"{name} is one point of a positioned lapse: a workflow moved the "
            f"stage before each point, so its points are {kind}s, not "
            "timepoints."
        )


def _positioned_kind(attrs: Mapping[str, Any]) -> str | None:
    """``tile``/``position`` when the lapse's points were not plain time."""
    try:
        layout = decode_layout_attrs(dict(attrs))
    except Exception:
        layout = None
    if layout is not None:
        for partition in layout.partitions:
            if partition.kind in NON_TIME_PARTITION_KINDS:
                return partition.kind
    # A tiling point names its grid cell even where no layout was recorded --
    # a camera frame stream carries no scan layout to scope.
    if any(str(key).startswith("Tiling:") for key in attrs):
        return "tile"
    return None


def _open_container(path: Path):
    source = resolve_dataset_source(path, allowed_specs=SOURCE_SPECS)
    if source.format_id == "hdf5":
        return h5py.File(str(source.path), "r")
    if source.format_id == "tiff":
        return tiff.TiffFile(str(source.path))
    if source.format_id == "zarr":
        return zarr.open(str(source.path), mode="r")
    raise ValueError(f'Unsupported container "{source.path}"')


def _close_container(container) -> None:
    closer = getattr(container, "close", None)
    if callable(closer) and not is_zarr_group(container):
        try:
            closer()
        except Exception:
            pass


def _lapse_groups(container) -> list[tuple[int, str]]:
    """Numbered per-timepoint groups at the root, by name alone."""
    if not (isinstance(container, h5py.Group) or is_zarr_group(container)):
        return []
    groups = []
    for name in container.keys():
        match = _LAPSE_GROUP.fullmatch(str(name))
        if match is not None:
            groups.append((int(match.group(1)), str(name)))
    return sorted(groups)


def _default_dataset(container, groups) -> str:
    if groups:
        _ordinal, group = groups[0]
        detectors = _group_detectors(container[group])
        if detectors:
            return f"{group}/{detectors[0]}"
    names = dataset_names(container)
    if not names:
        raise NotATimeLapse("The file holds no image.")
    return names[0]


def _item_dataset(container, groups, dataset: str) -> str:
    """The ``scanN/<detector>`` item a dataset path of a single-file lapse lies in.

    A path picked inside the container can name the group alone (``scan4``)
    or reach below the detector (``scan4/Camera/data``); either way the item
    is that group's detector. Any other path is returned as given.
    """
    parts = [part for part in str(dataset).split("/") if part]
    if not parts or parts[0] not in {name for _ordinal, name in groups}:
        return dataset
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    detectors = _group_detectors(container[parts[0]])
    return f"{parts[0]}/{detectors[0]}" if detectors else dataset


def _group_detectors(group) -> list[str]:
    names = []
    for name in sorted(group.keys()):
        try:
            if is_structured_detector_group(group[name]):
                names.append(str(name))
        except Exception:
            continue
    return names


def _item_attrs(container, image_attrs: Mapping[str, Any]) -> dict[str, Any]:
    """An item's attributes over its container's, as DataObj reads them."""
    attrs: dict[str, Any] = {}
    if isinstance(container, h5py.File) or is_zarr_group(container):
        try:
            attrs.update(dict(container.attrs))
        except Exception:
            pass
    attrs.update(image_attrs)
    return attrs


def _detector_name(attrs: Mapping[str, Any], dataset: str | None) -> str:
    recorded = _as_text(attrs.get("recording:detector_name"))
    if recorded:
        return recorded
    return str(dataset or "detector").rsplit("/", 1)[-1]


def _single_file_channels(container, groups, root: Path) -> tuple[LapseChannel, ...]:
    detectors: dict[str, list[LapseItemRef]] = {}
    for ordinal, group in groups:
        try:
            names = _group_detectors(container[group])
        except Exception:
            continue
        for detector in names:
            detectors.setdefault(detector, []).append(
                LapseItemRef(ordinal, root, f"{group}/{detector}")
            )
    return tuple(
        LapseChannel(detector, detector, tuple(refs))
        for detector, refs in sorted(detectors.items())
    )


def _index_token(index: int, planned: int) -> str:
    """How ImControl pads the lapse index in a file name: to the planned count."""
    return str(int(index)).zfill(len(str(int(planned))))


def _split_multi_file_name(name: str, index: int, planned: int):
    """``(prefix, token, suffix)`` around the anchor's own index, or None."""
    padded = _index_token(index, planned)
    best = None
    for token in _MULTI_FILE_TOKENS:
        marker = f"_{token}{padded}"
        position = name.rfind(marker)
        if position < 0:
            continue
        rest = name[position + len(marker):]
        # The index is followed by the detector suffix or the extension, never
        # by another digit -- ``_time07`` must not match inside ``_time070``.
        if rest[:1].isdigit():
            continue
        if best is None or position > best[0]:
            best = (position, token, rest)
    if best is None:
        return None
    position, token, rest = best
    return name[:position], token, rest


def _multi_file_prefix(name: str, index: int, planned: int) -> str | None:
    split = _split_multi_file_name(name, index, planned)
    return split[0] if split else None


def _without_extension(name: str) -> str:
    lowered = name.lower()
    for spec in SOURCE_SPECS:
        for extension in sorted(spec.suffixes, key=len, reverse=True):
            if lowered.endswith(extension):
                return name[: -len(extension)]
    return name


def _suffix_label(suffix: str) -> str:
    """``_Camera.ome.tiff`` -> ``Camera``; a bare extension -> ``''``."""
    return _without_extension(suffix).lstrip("_")


def _multi_file_channels(
    anchor_path: Path,
    anchor_index: int,
    planned: int,
    anchor_dataset: str,
    names_in_anchor: Sequence[str],
) -> tuple[LapseChannel, ...]:
    split = _split_multi_file_name(anchor_path.name, anchor_index, planned)
    if split is None:
        raise NotATimeLapse(
            f"{anchor_path.name} records lapse point {anchor_index} of "
            f"{planned}, but its name does not carry that index "
            f"(_time{_index_token(anchor_index, planned)} or "
            f"_scan{_index_token(anchor_index, planned)}), so its siblings "
            "cannot be found. Was it renamed?"
        )
    prefix, token, anchor_suffix = split
    width = len(_index_token(anchor_index, planned))
    pattern = re.compile(
        re.escape(prefix) + "_" + token + r"(\d{" + str(width) + r"})(\D.*)$"
    )
    by_suffix: dict[str, list[LapseItemRef]] = {}
    folder = anchor_path.parent
    with os.scandir(folder) as entries:
        for entry in entries:
            match = pattern.match(entry.name)
            if match is None:
                continue
            suffix = match.group(2)
            try:
                resolve_dataset_source(folder / entry.name, allowed_specs=SOURCE_SPECS)
            except ValueError:
                continue
            by_suffix.setdefault(suffix, []).append(
                LapseItemRef(int(match.group(1)), folder / entry.name, None)
            )

    channels = []
    for suffix, refs in sorted(by_suffix.items()):
        refs.sort(key=lambda ref: ref.ordinal)
        label = _suffix_label(suffix)
        if suffix == anchor_suffix and len(names_in_anchor) > 1:
            # Several detectors in one file per timepoint: one channel each,
            # all reading the same sibling files.
            for dataset in names_in_anchor:
                channels.append(LapseChannel(
                    f"{label}/{dataset}" if label else str(dataset),
                    f"{label} {dataset}".strip(),
                    tuple(
                        LapseItemRef(ref.ordinal, ref.path, dataset)
                        for ref in refs
                    ),
                ))
            continue
        if suffix == anchor_suffix:
            refs = [
                LapseItemRef(ref.ordinal, ref.path, anchor_dataset)
                if ref.path == anchor_path else ref
                for ref in refs
            ]
        channels.append(LapseChannel(label or "detector", label or "detector",
                                     tuple(refs)))
    return tuple(channels)


def _channel_for(channels: Sequence[LapseChannel], anchor: LapseItemRef) -> LapseChannel:
    for channel in channels:
        for ref in channel.candidates:
            if ref.path == anchor.path and ref.dataset in (anchor.dataset, None):
                return channel
    raise NotATimeLapse(f"{anchor.path.name} was not found among its own siblings.")


# --------------------------------------------------------------------------
# headers
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LapseItemHeader:
    """One item's description, read without its pixels."""

    ref: LapseItemRef
    dataset: str
    lapse_index: int | None
    planned_count: int | None
    shape: tuple[int, ...]
    dtype: np.dtype
    axis_labels: tuple[str, ...]
    axis_scales: tuple[float, ...]
    scale_unit: str
    started_at: datetime | None
    planned_start: datetime | None
    lifecycle: RecordingLifecycle | None


@dataclass(frozen=True)
class LapseItemFailure:
    """A candidate that could not be described, and why."""

    ref: LapseItemRef
    reason: str


@dataclass(frozen=True)
class LapseHeaders:
    detector: str
    headers: tuple[LapseItemHeader, ...]
    failures: tuple[LapseItemFailure, ...]


def read_lapse_headers(
    index: TimeLapseIndex,
    detector: str | None = None,
    *,
    progress: Callable[[int, int], None] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> LapseHeaders:
    """Open every candidate of one detector for its header, never its pixels.

    A single-file lapse is opened once for all of its groups; a multi-file lapse
    opens and closes each file in turn, so a long lapse never holds thousands
    of handles.
    """
    channel = index.channel(detector)
    candidates = channel.candidates
    total = len(candidates)
    headers: list[LapseItemHeader] = []
    failures: list[LapseItemFailure] = []

    shared = None
    if index.storage == STORAGE_ONE_GROUP_PER_ITEM:
        shared = _open_container(index.root)
    try:
        for position, ref in enumerate(candidates):
            if check_cancelled is not None:
                check_cancelled()
            try:
                if shared is not None:
                    headers.append(_read_header(shared, ref))
                else:
                    container = _open_container(ref.path)
                    try:
                        headers.append(_read_header(container, ref))
                    finally:
                        _close_container(container)
            except Exception as exc:
                failures.append(LapseItemFailure(ref, _failure_reason(exc)))
            if progress is not None:
                progress(position + 1, total)
    finally:
        if shared is not None:
            _close_container(shared)
    return LapseHeaders(channel.detector, tuple(headers), tuple(failures))


def _failure_reason(exc: Exception) -> str:
    text = str(exc) or type(exc).__name__
    return text if len(text) <= 200 else text[:197] + "..."


def _read_header(container, ref: LapseItemRef) -> LapseItemHeader:
    dataset = ref.dataset
    if dataset is None:
        names = dataset_names(container)
        if not names:
            raise ValueError("holds no image (stopped before its first frame?)")
        dataset = names[0]
    image = resolve_image(container, dataset, validate_layout_metadata=False)
    attrs = _item_attrs(container, image.attrs)
    shape = tuple(int(size) for size in image.array.shape)
    labels = tuple(image.axis_labels or default_axis_labels(len(shape)))
    scales = tuple(float(value) for value in (image.axis_scales or ()))
    if len(scales) != len(shape):
        from imswitch.improcess.model.image_sources import axis_scales_from_element_size

        fallback, unit = axis_scales_from_element_size(attrs, len(shape))
        scales = tuple(fallback or (1.0,) * len(shape))
        scale_unit = image.scale_unit or unit or "px"
    else:
        scale_unit = image.scale_unit or "px"
    return LapseItemHeader(
        ref=ref,
        dataset=str(dataset),
        lapse_index=_as_int(attrs.get("recording:lapse_index")),
        planned_count=_as_int(attrs.get("recording:num_timepoints")),
        shape=shape,
        dtype=np.dtype(image.array.dtype),
        axis_labels=labels,
        axis_scales=scales,
        scale_unit=str(scale_unit),
        started_at=_as_datetime(attrs.get("acquisition:start_time")),
        planned_start=_as_datetime(attrs.get("recording:planned_start_time")),
        lifecycle=image.recording_lifecycle,
    )


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TimepointSlot:
    """One plane of the stack along T, at the lapse index it was recorded at."""

    lapse_index: int
    state: str
    reason: str = ""
    ref: LapseItemRef | None = None
    dataset: str | None = None
    stored_shape: tuple[int, ...] | None = None
    dtype: np.dtype | None = None
    started_at: datetime | None = None
    planned_start: datetime | None = None

    @property
    def readable(self) -> bool:
        return self.ref is not None and self.stored_shape is not None


@dataclass(frozen=True)
class TimeLapsePlan:
    """Everything needed to read the stack, and to say what is in it."""

    index: TimeLapseIndex
    detector: str
    slots: tuple[TimepointSlot, ...]
    item_shape: tuple[int, ...]
    dtype: np.dtype
    item_axis_labels: tuple[str, ...]
    item_axis_scales: tuple[float, ...]
    scale_unit: str
    #: Items discovery listed that do not belong here, with the reason.
    excluded: tuple[tuple[LapseItemRef, str], ...] = ()
    #: Incomplete points left out of the stack; they were at the end of it.
    skipped: tuple[TimepointSlot, ...] = ()
    #: What was asked for incomplete points: ``mark`` or ``skip``.
    incomplete: str = INCOMPLETE_MARK
    #: Every item stores one frame ahead of its plane -- a camera lapse point
    #: is a one-frame recording -- and that axis is left out of the stack, so
    #: it is ``(T, Y, X)`` rather than ``(T, 1, Y, X)``.
    squeezed_leading: bool = False

    @property
    def shape(self) -> tuple[int, ...]:
        return (len(self.slots), *self.item_shape)

    @property
    def stored_item_shape(self) -> tuple[int, ...]:
        """The shape each complete item has on disk."""
        if self.squeezed_leading:
            return (1, *self.item_shape)
        return tuple(self.item_shape)

    @property
    def lapse_indices(self) -> tuple[int, ...]:
        return tuple(slot.lapse_index for slot in self.slots)

    def slots_in(self, state: str) -> tuple[TimepointSlot, ...]:
        return tuple(slot for slot in self.slots if slot.state == state)

    def planned_times_s(self) -> tuple[float | None, ...]:
        """When each point was scheduled, from the first point's schedule.

        The recorded per-point schedule when every point carries one, otherwise
        the uniform interval; ``None`` throughout when neither was recorded.
        """
        starts = [slot.planned_start for slot in self.slots]
        origin = next((start for start in starts if start is not None), None)
        if origin is not None and all(start is not None for start in starts):
            return tuple((start - origin).total_seconds() for start in starts)
        interval = self.index.interval_s
        if interval is None:
            return tuple(None for _slot in self.slots)
        first = self.slots[0].lapse_index if self.slots else 0
        return tuple(
            (slot.lapse_index - first) * float(interval) for slot in self.slots
        )

    def actual_times_s(self) -> tuple[float | None, ...]:
        """When each point's recording actually started, from the first one's."""
        origin = next(
            (slot.started_at for slot in self.slots if slot.started_at is not None),
            None,
        )
        if origin is None:
            return tuple(None for _slot in self.slots)
        return tuple(
            (slot.started_at - origin).total_seconds()
            if slot.started_at is not None else None
            for slot in self.slots
        )


def plan_time_lapse(
    index: TimeLapseIndex,
    headers: LapseHeaders,
    *,
    incomplete: str = INCOMPLETE_MARK,
) -> TimeLapsePlan:
    """Place every item at its recorded index and check they share a shape.

    Points are placed by ``recording:lapse_index``, never by the order they
    were found in, so a missing point stays a gap at its own time. The stack
    runs to the last point recorded: points planned but never started (a
    lapse stopped at 42 of 100) are not in it. With ``incomplete="skip"``,
    incomplete and missing points at the *end* of the stack are left out; one
    in the middle stays in, marked, because removing it would move every later
    point onto the wrong time.

    Raises :class:`TimeLapseShapeError` when complete points disagree on shape
    or dtype, or an incomplete point is not a prefix of the others.
    """
    if incomplete not in (INCOMPLETE_MARK, INCOMPLETE_SKIP):
        raise ValueError(f"incomplete must be 'mark' or 'skip', not {incomplete!r}")

    members, excluded = _members_of_this_lapse(index, headers)
    if not members and not headers.failures:
        raise TimeLapseShapeError(
            f"No readable item of {index.name} belongs to this lapse."
        )

    slots_by_index: dict[int, TimepointSlot] = {}
    for header in members:
        slots_by_index[header.lapse_index] = _slot_for(header)
    for failure in headers.failures:
        # Unreadable items can only be placed where their name puts them, and
        # only in a multi-file lapse, whose names carry the index.
        if index.storage != STORAGE_ONE_FILE_PER_ITEM:
            excluded.append((failure.ref, failure.reason))
            continue
        position = failure.ref.ordinal
        if position in slots_by_index or position >= index.planned_count:
            excluded.append((failure.ref, failure.reason))
            continue
        slots_by_index[position] = TimepointSlot(
            lapse_index=position,
            state=TIMEPOINT_MISSING,
            reason=failure.reason,
            ref=None,
        )
    if not slots_by_index:
        raise TimeLapseShapeError(f"No item of {index.name} could be read.")

    last = max(slots_by_index)
    slots = [
        slots_by_index.get(
            position,
            TimepointSlot(position, TIMEPOINT_MISSING, "not recorded"),
        )
        for position in range(0, last + 1)
    ]

    reference = _reference_header(members)
    if reference is None:
        raise TimeLapseShapeError(
            f"No point of {index.name} holds an image; nothing to stack."
        )
    slots = [_checked_slot(slot, reference, index.name) for slot in slots]

    skipped: list[TimepointSlot] = []
    if incomplete == INCOMPLETE_SKIP:
        while slots and slots[-1].state != TIMEPOINT_COMPLETE:
            skipped.insert(0, slots.pop())
        if not slots:
            raise TimeLapseShapeError(
                f"Every point of {index.name} is incomplete; skipping them "
                "leaves nothing to stack."
            )

    shape = tuple(reference.shape)
    labels = list(reference.axis_labels)
    scales = list(reference.axis_scales)
    squeezed = len(shape) >= 3 and shape[0] == 1
    if squeezed:
        shape, labels, scales = shape[1:], labels[1:], scales[1:]
    # The stack's own T is the lapse; an item's leading axis is its frames.
    # Two axes both called T would make every reader of the labels, OME
    # included, take one for the other.
    labels = ["Frame" if label == "T" else label for label in labels]

    return TimeLapsePlan(
        index=index,
        detector=headers.detector,
        slots=tuple(slots),
        item_shape=shape,
        dtype=reference.dtype,
        item_axis_labels=tuple(labels),
        item_axis_scales=tuple(scales),
        scale_unit=reference.scale_unit,
        excluded=tuple(excluded),
        skipped=tuple(skipped),
        incomplete=incomplete,
        squeezed_leading=squeezed,
    )


def _members_of_this_lapse(index: TimeLapseIndex, headers: LapseHeaders):
    """The headers that are points of the anchor's lapse, and the rest with why."""
    excluded: list[tuple[LapseItemRef, str]] = []
    candidates = []
    for header in headers.headers:
        if header.lapse_index is None:
            excluded.append((header.ref, "records no lapse index"))
        elif header.planned_count != index.planned_count:
            excluded.append((
                header.ref,
                f"belongs to a {header.planned_count}-point lapse, not this "
                f"{index.planned_count}-point one",
            ))
        elif not 0 <= header.lapse_index < index.planned_count:
            excluded.append((
                header.ref,
                f"lapse index {header.lapse_index} is outside 0-"
                f"{index.planned_count - 1}",
            ))
        elif (
            index.storage == STORAGE_ONE_FILE_PER_ITEM
            and header.lapse_index != header.ref.ordinal
        ):
            excluded.append((
                header.ref,
                f"is named point {header.ref.ordinal} but records lapse index "
                f"{header.lapse_index}",
            ))
        else:
            candidates.append(header)

    if index.storage == STORAGE_ONE_GROUP_PER_ITEM:
        candidates, others = _anchor_run(candidates, index)
        excluded.extend(
            (header.ref, "belongs to another lapse recorded into the same file")
            for header in others
        )

    members: list[LapseItemHeader] = []
    seen: dict[int, LapseItemHeader] = {}
    for header in candidates:
        earlier = seen.get(header.lapse_index)
        if earlier is not None:
            excluded.append((
                header.ref,
                f"repeats lapse index {header.lapse_index}, already held by "
                f"{_ref_label(earlier.ref)}",
            ))
            continue
        seen[header.lapse_index] = header
        members.append(header)
    return members, excluded


def _lapse_runs(headers: Sequence[LapseItemHeader]) -> list[list[LapseItemHeader]]:
    """One file's groups, split into the lapses recorded into it.

    Groups are numbered by the next free slot in the file, so a second lapse
    recorded into the same file continues the numbering from where the first
    stopped. Within one lapse the recorded index only ever increases; where it
    does not, a new lapse began. Every header must carry a lapse index.
    """
    ordered = sorted(headers, key=lambda header: header.ref.ordinal)
    runs: list[list[LapseItemHeader]] = []
    for header in ordered:
        if runs and header.lapse_index > runs[-1][-1].lapse_index:
            runs[-1].append(header)
        else:
            runs.append([header])
    return runs


def lapses_in_file(index: TimeLapseIndex, detector: str | None = None) -> int:
    """How many lapses the file of a single-file lapse holds.

    Reads every group's header, never its pixels, and splits them by the rule
    planning uses (:func:`_lapse_runs`), so the count agrees with what
    :func:`plan_time_lapse` would stack. A multi-file lapse is one lapse.
    """
    if index.storage != STORAGE_ONE_GROUP_PER_ITEM:
        return 1
    headers = read_lapse_headers(index, detector)
    indexed = [header for header in headers.headers if header.lapse_index is not None]
    return max(1, len(_lapse_runs(indexed)))


def _anchor_run(headers: Sequence[LapseItemHeader], index: TimeLapseIndex):
    """Split one file's groups into lapses and keep the anchor's.

    See :func:`_lapse_runs` for where one lapse ends and the next begins.
    """
    runs = _lapse_runs(headers)
    for run in runs:
        if any(header.ref.ordinal == index.anchor.ordinal for header in run):
            others = [header for other in runs if other is not run for header in other]
            return run, others
    return (runs[0] if runs else []), [
        header for run in runs[1:] for header in run
    ]


def _slot_for(header: LapseItemHeader) -> TimepointSlot:
    state, reason = _completion(header)
    if not header.shape or 0 in header.shape:
        state, reason = TIMEPOINT_MISSING, "holds no frames"
    return TimepointSlot(
        lapse_index=int(header.lapse_index),
        state=state,
        reason=reason,
        ref=header.ref if state != TIMEPOINT_MISSING else None,
        dataset=header.dataset,
        stored_shape=header.shape if state != TIMEPOINT_MISSING else None,
        dtype=header.dtype,
        started_at=header.started_at,
        planned_start=header.planned_start,
    )


def _completion(header: LapseItemHeader) -> tuple[str, str]:
    lifecycle = header.lifecycle
    if lifecycle is None:
        return TIMEPOINT_COMPLETE, ""
    if lifecycle.completion_outcome == "stopped_early":
        actual, planned = lifecycle.actual_frames, lifecycle.planned_frames
        if actual is not None and planned is not None:
            return TIMEPOINT_INCOMPLETE, f"stopped early: {actual} of {planned} frames"
        return TIMEPOINT_INCOMPLETE, "stopped early"
    if lifecycle.writer_state == WRITER_STATE_WRITING or _stale_writing(lifecycle):
        return TIMEPOINT_INCOMPLETE, "never finalized: still writing, or the writer was interrupted"
    return TIMEPOINT_COMPLETE, ""


def _stale_writing(lifecycle: RecordingLifecycle) -> bool:
    return any(
        issue.code in ("STALE_WRITING_MARKER", "INCOMPLETE_STREAM_MARKER")
        for issue in lifecycle.issues
    )


def _reference_header(members: Sequence[LapseItemHeader]) -> LapseItemHeader | None:
    """The shape every complete point has to share: the first complete one's."""
    usable = [header for header in members if header.shape and 0 not in header.shape]
    complete = [
        header for header in usable
        if _completion(header)[0] == TIMEPOINT_COMPLETE
    ]
    if complete:
        return min(complete, key=lambda header: header.lapse_index)
    if usable:
        # Only incomplete points: the largest is the best available frame.
        return max(usable, key=lambda header: (header.shape, -header.lapse_index))
    return None


def _checked_slot(slot: TimepointSlot, reference: LapseItemHeader, name: str) -> TimepointSlot:
    if not slot.readable:
        return slot
    label = f"point {slot.lapse_index}"
    if slot.dtype is not None and np.dtype(slot.dtype) != np.dtype(reference.dtype):
        raise TimeLapseShapeError(
            f"{name} cannot be stacked: {label} holds {np.dtype(slot.dtype)} "
            f"but point {reference.lapse_index} holds "
            f"{np.dtype(reference.dtype)}."
        )
    shape = tuple(slot.stored_shape)
    if shape == reference.shape:
        return slot
    expected = "x".join(str(size) for size in reference.shape)
    found = "x".join(str(size) for size in shape)
    if slot.state == TIMEPOINT_COMPLETE:
        raise TimeLapseShapeError(
            f"{name} cannot be stacked: {label} is {found} but point "
            f"{reference.lapse_index} is {expected}. The recording changed "
            "shape part-way (ROI, binning or scan size); open each part "
            "separately."
        )
    if (
        len(shape) == len(reference.shape)
        and shape[1:] == reference.shape[1:]
        and shape[0] < reference.shape[0]
    ):
        return slot
    raise TimeLapseShapeError(
        f"{name} cannot be stacked: incomplete {label} is {found}, which is not "
        f"a shorter run of the other points' {expected}."
    )


def _ref_label(ref: LapseItemRef) -> str:
    return f"{ref.path.name}::{ref.dataset}" if ref.dataset else ref.path.name


# --------------------------------------------------------------------------
# the lazy stack
# --------------------------------------------------------------------------


class _ItemReader:
    """Open lapse items on demand and keep a few of them open.

    Bounded, so browsing a thousand-file lapse never holds a thousand handles;
    locked, because napari and a worker may read at the same time.
    """

    def __init__(self, max_containers: int = 8, max_arrays: int = 64):
        self._lock = threading.RLock()
        self._containers: OrderedDict[str, Any] = OrderedDict()
        self._arrays: OrderedDict[tuple[str, str], Any] = OrderedDict()
        self._max_containers = max(1, int(max_containers))
        self._max_arrays = max(1, int(max_arrays))

    def read(self, ref: LapseItemRef, dataset: str | None, key) -> np.ndarray:
        with self._lock:
            array = self._array(ref, dataset)
            if _plain_key(key):
                return np.asarray(array[key])
            # Arrays and reversed slices are not something every backend
            # indexes (h5py refuses both); one item is small, so read it and
            # let NumPy apply the key.
            return np.asarray(np.asarray(array[...])[key])

    def _array(self, ref: LapseItemRef, dataset: str | None):
        path = str(ref.path)
        name = dataset or ref.dataset
        key = (path, str(name))
        array = self._arrays.get(key)
        if array is not None:
            self._arrays.move_to_end(key)
            return array
        container = self._container(path)
        if name is None:
            names = dataset_names(container)
            if not names:
                raise ValueError(f"{ref.path.name} holds no image")
            name = names[0]
        image = resolve_image(container, name, validate_layout_metadata=False)
        array = virtual_array_from_native(image.array)
        self._arrays[key] = array
        while len(self._arrays) > self._max_arrays:
            _old_key, old = self._arrays.popitem(last=False)
            _close_quietly(old)
        return array

    def _container(self, path: str):
        container = self._containers.get(path)
        if container is not None:
            self._containers.move_to_end(path)
            return container
        container = _open_container(Path(path))
        self._containers[path] = container
        while len(self._containers) > self._max_containers:
            old_path, old = self._containers.popitem(last=False)
            for key in [key for key in self._arrays if key[0] == old_path]:
                _close_quietly(self._arrays.pop(key))
            _close_container(old)
        return container

    def close(self) -> None:
        with self._lock:
            for array in self._arrays.values():
                _close_quietly(array)
            self._arrays.clear()
            for container in self._containers.values():
                _close_container(container)
            self._containers.clear()


def _close_quietly(array) -> None:
    try:
        array.close()
    except Exception:
        pass


def _plain_key(key) -> bool:
    for part in key:
        if isinstance(part, (int, np.integer)):
            continue
        if isinstance(part, slice) and (part.step is None or part.step > 0):
            continue
        return False
    return True


class LazyTimeLapseArray:
    """A lapse as one ``(T, ...)`` array that reads a timepoint when indexed.

    Satisfies the same contract as the lazy DataObj arrays (``shape``,
    ``dtype``, ``__getitem__``, ``asarray``, ``close``), so anything that takes
    those takes a lapse. A missing point reads as zeros and an incomplete
    point's missing frames do too; which points those are is in the plan the
    array was built from, not in the pixels.
    """

    backend = "time-lapse"
    supports_lazy_indexing = True
    #: It opens and closes its own files, so the viewer may read it later --
    #: plane by plane as the slider moves -- rather than whole up front.
    display_lazily = True

    def __init__(self, plan: TimeLapsePlan, *, reader: _ItemReader | None = None):
        self._plan = plan
        self._slots = tuple(plan.slots)
        self._item_shape = tuple(int(size) for size in plan.item_shape)
        self._stored_shape = tuple(int(size) for size in plan.stored_item_shape)
        self._prefix = (0,) if plan.squeezed_leading else ()
        self._dtype = np.dtype(plan.dtype)
        self._reader = reader or _ItemReader()

    @property
    def plan(self) -> TimeLapsePlan:
        return self._plan

    @property
    def shape(self) -> tuple[int, ...]:
        return (len(self._slots), *self._item_shape)

    @property
    def ndim(self) -> int:
        return len(self.shape)

    @property
    def dtype(self) -> np.dtype:
        return self._dtype

    @property
    def size(self) -> int:
        return int(np.prod(self.shape))

    @property
    def nbytes(self) -> int:
        return self.size * self._dtype.itemsize

    @property
    def chunks(self) -> tuple[int, ...]:
        return (1, *self._item_shape)

    def __len__(self) -> int:
        return len(self._slots)

    def timepoint(self, position: int) -> np.ndarray:
        """One whole timepoint, as recorded (zeros where it is missing)."""
        return self[int(position)]

    def __getitem__(self, key) -> np.ndarray:
        key = _normalize_key(key, self.ndim)
        first, rest = key[0], key[1:]
        if isinstance(first, (int, np.integer)):
            position = _wrap_index(int(first), len(self._slots))
            return self._read_slot(self._slots[position], rest)
        positions = np.arange(len(self._slots))[first]
        out = np.empty(
            (len(positions), *_result_shape(self._item_shape, rest)),
            dtype=self._dtype,
        )
        for offset, position in enumerate(positions):
            out[offset] = self._read_slot(self._slots[int(position)], rest)
        return out

    def asarray(self) -> np.ndarray:
        return self[...]

    def __array__(self, dtype=None, copy=None) -> np.ndarray:
        array = self.asarray()
        if dtype is not None:
            return np.asarray(array, dtype=dtype)
        return array

    def to_dask(self, chunks: str | tuple[int, ...] = "native"):
        """A dask view; by default one chunk per timepoint.

        Named here rather than tokenized: dask would otherwise try to hash
        this object's open file handles to name the graph. The name is unique
        per call, never reused, so a chunk cached for one lapse can never be
        served for another.
        """
        import dask.array as da

        return da.from_array(
            self,
            chunks=self.chunks if chunks == "native" else chunks,
            asarray=False,
            fancy=False,
            name=f"time-lapse-{uuid.uuid4().hex}",
        )

    def close(self) -> None:
        self._reader.close()

    def _read_slot(self, slot: TimepointSlot, rest) -> np.ndarray:
        if not slot.readable:
            return np.zeros(_result_shape(self._item_shape, rest), self._dtype)
        stored = tuple(slot.stored_shape)
        if stored == self._stored_shape:
            data = self._reader.read(
                slot.ref, slot.dataset, (*self._prefix, *rest)
            )
        else:
            data = self._read_padded(slot, stored, rest)
        return np.asarray(data, dtype=self._dtype)

    def _read_padded(self, slot: TimepointSlot, stored, rest) -> np.ndarray:
        """Read a short point, filling the frames it never recorded with zeros.

        Planning allows only the leading axis to be short, so that is the only
        axis padded here.
        """
        available = int(stored[0])
        wanted = np.arange(self._item_shape[0])[rest[0]]
        tail = tuple(rest[1:])
        tail_shape = _result_shape(self._item_shape[1:], tail)
        if np.ndim(wanted) == 0:
            position = int(wanted)
            if position < available:
                return self._reader.read(slot.ref, slot.dataset, (position, *tail))
            return np.zeros(tail_shape, self._dtype)
        out = np.zeros((len(wanted), *tail_shape), self._dtype)
        present = wanted < available
        if present.any():
            positions = wanted[present]
            low, high = int(positions.min()), int(positions.max()) + 1
            block = self._reader.read(
                slot.ref, slot.dataset, (slice(low, high), *tail)
            )
            out[present] = block[positions - low]
        return out


def _normalize_key(key, ndim: int) -> tuple:
    if not isinstance(key, tuple):
        key = (key,)
    if any(part is None for part in key):
        raise IndexError("A time-lapse stack cannot insert new axes while indexing")
    if any(part is Ellipsis for part in key):
        position = next(i for i, part in enumerate(key) if part is Ellipsis)
        fill = (slice(None),) * (ndim - (len(key) - 1))
        key = key[:position] + fill + key[position + 1:]
    if len(key) > ndim:
        raise IndexError(f"too many indices for a {ndim}-dimensional stack")
    key = key + (slice(None),) * (ndim - len(key))
    normalized = []
    for part in key:
        if isinstance(part, (list, np.ndarray)):
            part = np.asarray(part)
            if part.dtype == bool:
                part = np.nonzero(part)[0]
        normalized.append(part)
    return tuple(normalized)


def _wrap_index(position: int, size: int) -> int:
    wrapped = position + size if position < 0 else position
    if not 0 <= wrapped < size:
        raise IndexError(f"index {position} is out of range for {size} timepoints")
    return wrapped


def _result_shape(shape: Sequence[int], key: Sequence[Any]) -> tuple[int, ...]:
    out = []
    for size, part in zip(shape, key):
        if isinstance(part, (int, np.integer)):
            continue
        if isinstance(part, slice):
            out.append(len(range(*part.indices(int(size)))))
        else:
            out.append(len(np.arange(int(size))[part]))
    return tuple(out)


# --------------------------------------------------------------------------
# one call for consumers
# --------------------------------------------------------------------------


def open_time_lapse(
    path: str | Path | TimeLapseIndex,
    *,
    dataset: str | None = None,
    detector: str | None = None,
    incomplete: str = INCOMPLETE_MARK,
    progress: Callable[[int, int], None] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> LazyTimeLapseArray:
    """Discover, describe and plan a lapse, and return it as a lazy stack.

    The per-timepoint entry point for a reconstructor that wants to process a
    lapse one point at a time: ``stack.plan.slots`` says what each point is,
    ``stack.timepoint(t)`` reads it.
    """
    index = (
        path if isinstance(path, TimeLapseIndex)
        else discover_time_lapse(path, dataset)
    )
    headers = read_lapse_headers(
        index, detector, progress=progress, check_cancelled=check_cancelled
    )
    return LazyTimeLapseArray(plan_time_lapse(index, headers, incomplete=incomplete))


# --------------------------------------------------------------------------
# attribute coercion
# --------------------------------------------------------------------------


def _native(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray) and value.size == 1:
        return value.reshape(()).item()
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _as_int(value: Any) -> int | None:
    value = _native(value)
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(number) or number != int(number):
        return None
    return int(number)


def _as_float(value: Any) -> float | None:
    value = _native(value)
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _as_bool(value: Any) -> bool:
    value = _native(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value) if value is not None else False


def _as_text(value: Any) -> str | None:
    value = _native(value)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_datetime(value: Any) -> datetime | None:
    text = _as_text(value)
    if text is None:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        # A naive time is the recording host's local time; comparing it with
        # an aware one would raise, so every time is made aware the same way.
        moment = moment.astimezone()
    return moment.astimezone(timezone.utc)


__all__ = [
    "INCOMPLETE_MARK",
    "INCOMPLETE_SKIP",
    "LapseChannel",
    "LapseHeaders",
    "LapseItemFailure",
    "LapseItemHeader",
    "LapseItemRef",
    "LazyTimeLapseArray",
    "NON_TIME_PARTITION_KINDS",
    "NotATimeLapse",
    "STORAGE_ONE_FILE_PER_ITEM",
    "STORAGE_ONE_GROUP_PER_ITEM",
    "TIMEPOINT_COMPLETE",
    "TIMEPOINT_INCOMPLETE",
    "TIMEPOINT_MISSING",
    "TIME_LAPSE_SOURCE_KIND",
    "TimeLapseIndex",
    "TimeLapsePlan",
    "TimeLapseShapeError",
    "TimepointSlot",
    "discover_time_lapse",
    "is_time_lapse_item",
    "lapses_in_file",
    "open_time_lapse",
    "plan_time_lapse",
    "read_lapse_headers",
]
