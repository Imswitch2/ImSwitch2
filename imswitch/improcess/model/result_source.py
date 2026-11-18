"""A result as a reconstructor's input: the bridge from ``ProcessingResult`` to ``DataObj``.

A reconstructor reads a :class:`~imswitch.improcess.model.DataObj` -- the
shape every file, live stack and RAM recording is handed over in -- and a
workflow used to insist that the ``DataObj`` be a raw ``Source``. That made
any chain with two reconstructions impossible, and so was any reconstruction
of data a Python step had first reshaped (a SNOUTY time-lapse stored side by
side along a camera axis). Whether a reconstructor can take something is a
question about the *data* -- its axes, kind and attributes -- not about where
it came from; the provenance records the history, it does not gate it.

:func:`data_obj_from_result` therefore wraps any image result as a
``DataObj``. It reads the result's array without copying it (a lazy array
stays lazy), carries the result's axes, scales and attributes (so
``recorded_snouty_geometry`` and ``acquisition_layout`` work where the
attributes are present), and remembers the result it came from
(:attr:`ResultDataObj.provenance_parent`), so the reconstruction's
provenance graph continues the upstream chain instead of starting at a fresh,
unreplayable "memory" source.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from imswitch.improcess.model.DataObj import DataObj
from imswitch.improcess.model.virtual_image import VirtualImageSource, _VirtualArrayBase

#: Result kinds a reconstructor may be fed: pixel data on a grid.
IMAGE_RESULT_KINDS = ("image", "labels")

#: Attribute keys that describe how a *raw recording* is laid out. A slice of a
#: result (auto-indexing) is no longer that recording, and a layout read from
#: them would describe frames the slice does not have; :func:`slice_attrs`
#: drops them.
LAYOUT_ATTR_PREFIXES = ("AcquisitionLayout:",)


def result_attrs(result) -> dict[str, Any]:
    """The source-style attributes ``result`` carries (a copy), or ``{}``.

    ``attrs`` is the in-memory metadata a result passes on to whatever reads
    it next (a reshape keeps the SNOUTY geometry, a deskew says
    ``geometry: deskewed``). It is not the ``metadata`` written beside the
    pixels; see :attr:`~imswitch.improcess.model.result.ProcessingResult.attrs`.
    """
    attrs = getattr(result, "attrs", None)
    return dict(attrs) if isinstance(attrs, dict) else {}


def slice_attrs(attrs: dict[str, Any] | None) -> dict[str, Any]:
    """``attrs`` without the raw-recording layout keys (see :data:`LAYOUT_ATTR_PREFIXES`)."""
    return {
        key: value for key, value in dict(attrs or {}).items()
        if not any(str(key).startswith(prefix) for prefix in LAYOUT_ATTR_PREFIXES)
    }


def is_image_result(value) -> bool:
    """Whether ``value`` is a result a reconstructor could read pixels from."""
    from imswitch.improcess.model.result import ProcessingResult, result_kind

    if not isinstance(value, ProcessingResult):
        return False
    if result_kind(value) not in IMAGE_RESULT_KINDS:
        return False
    data = getattr(value, "data", None)
    return data is not None and len(getattr(data, "shape", ()) or ()) >= 2


class _ResultArray(_VirtualArrayBase):
    """A result's array as the lazy-array contract ``DataObj`` reads.

    Indexing reads only what is asked for when the array is lazy; an
    in-memory array is handed out as itself (no copy).
    """

    backend = "result"

    def __init__(self, array: Any):
        self._array = array

    @property
    def shape(self) -> tuple[int, ...]:
        return tuple(int(n) for n in self._array.shape)

    @property
    def dtype(self) -> np.dtype:
        return np.dtype(self._array.dtype)

    @property
    def chunks(self) -> tuple[int, ...] | None:
        chunks = getattr(self._array, "chunks", None)
        try:
            return tuple(int(n) for n in chunks) if chunks is not None else None
        except TypeError:
            return None

    def __getitem__(self, key: Any) -> np.ndarray:
        return np.asarray(self._array[key])

    def asarray(self) -> np.ndarray:
        if isinstance(self._array, np.ndarray):
            return self._array
        return np.asarray(self._array[...] if hasattr(self._array, "__getitem__") else self._array)


class ResultDataObj(DataObj):
    """A ``DataObj`` backed by a result's array instead of a file.

    Everything a reconstructor reads from a ``DataObj`` is here -- ``data``,
    ``data_handle``, ``attrs``, ``axis_labels``, ``axis_scales``,
    ``scale_unit``, ``acquisition_layout`` -- and nothing assumes a path:
    ``dataPath`` is ``None``, opening is a no-op and unloading never closes
    the result's array (it belongs to the result, which lives on).
    """

    DATASET_NAME = "result"

    def __init__(
        self,
        result,
        *,
        name: str | None = None,
        data=None,
        axis_labels=None,
        axis_scales=None,
        scale_unit: str | None = None,
        attrs: dict | None = None,
        parent=None,
    ):
        display = str(name or getattr(result, "name", "") or "result")
        super().__init__(display, self.DATASET_NAME)
        # DataObj.__init__ treats a name that happens to be an existing file
        # as that file's path: a result named after its recording must not
        # be mistaken for the recording.
        self.dataPath = None
        array = getattr(result, "data", None) if data is None else data
        if array is None or len(getattr(array, "shape", ()) or ()) < 1:
            raise ValueError(f"result {display!r} has no array a reconstructor could read")
        ndim = len(array.shape)
        labels = list(axis_labels if axis_labels is not None else (getattr(result, "axis_labels", None) or []))
        if len(labels) != ndim:
            from imswitch.improcess.model.image_sources import default_axis_labels

            labels = list(default_axis_labels(ndim))
        scales = list(axis_scales if axis_scales is not None else (getattr(result, "axis_scales", None) or []))
        if len(scales) != ndim:
            scales = [1.0] * ndim
        self._attrs = dict(result_attrs(result) if attrs is None else attrs)
        self._axis_labels = [str(label) for label in labels]
        self._axis_labels_declared = True
        self._axis_scales = [float(scale) for scale in scales]
        self._scale_unit = str(scale_unit or getattr(result, "scale_unit", None) or "px")
        self._source_info = {
            "dataset_name": display,
            "dataset_path": None,
            "source_format": "result",
        }
        self._dataSource = VirtualImageSource(
            name=display,
            array=_ResultArray(array),
            attrs=self._attrs,
            array_path=None,
            source_format="result",
            axis_labels=list(self._axis_labels),
            axis_scales=list(self._axis_scales),
            scale_unit=self._scale_unit,
        )
        # An in-memory array is already "loaded": handing it out costs
        # nothing, and a reconstructor that checks ``dataLoaded`` before
        # unloading leaves it alone.
        self._data = array if isinstance(array, np.ndarray) else None
        #: The result this data was taken from (the provenance graph's link
        #: upstream; see :func:`~imswitch.improcess.model.provenance.record_reconstruction`).
        self.provenance_parent = result if parent is None else parent

    # -- no file behind it ---------------------------------------------------

    def checkAndOpenData(self):
        return

    def checkAndUnloadData(self):
        # Only what was decoded from a lazy array is dropped; the array
        # itself, and an in-memory one, belong to the result.
        if not isinstance(getattr(self._dataSource.array, "_array", None), np.ndarray):
            self._data = None
        self._meanData = None
        self._acquisitionLayoutResolution = None

    @property
    def recording_lifecycle(self):
        return None

    @property
    def acquisition_layout(self):
        """The layout the attributes declare for *this* array, resolved the
        way a file's is (an override, then the declared layout, then
        inference from the shape)."""
        if self._acquisitionLayoutResolution is not None:
            return self._acquisitionLayoutResolution
        from imswitch.improcess.model.acquisition_layout_resolver import resolve_acquisition_layout

        attrs = self._attrs or {}
        detector = attrs.get("recording:detector_name") or attrs.get("detector_name") or self.name
        self._acquisitionLayoutDetector = str(detector)
        self._acquisitionLayoutResolution = resolve_acquisition_layout(
            attrs,
            shape=tuple(self._dataSource.array.shape),
            detector=str(detector),
            axis_labels=self._axis_labels,
            axis_metadata_explicit=True,
            user_override=self._acquisitionLayoutOverride,
        )
        return self._acquisitionLayoutResolution

    def describesSameAs(self, other):
        return isinstance(other, ResultDataObj) and other.provenance_parent is self.provenance_parent

    def checkModifTime(self, minDiffTime):
        return


def data_obj_from_result(result, **overrides) -> ResultDataObj:
    """``result`` as a ``DataObj`` a reconstructor reads (see :class:`ResultDataObj`).

    ``overrides`` (``data``, ``axis_labels``, ``axis_scales``, ``attrs``,
    ``name``, ``parent``) describe a *view* of the result instead of the
    whole of it: auto-indexing hands each slice through here with its core
    axes and the result it was sliced from as the parent.
    """
    if isinstance(result, DataObj):
        return result
    if not is_image_result(result) and "data" not in overrides:
        from imswitch.improcess.model.result import result_kind

        raise ValueError(
            f"{getattr(result, 'name', result)!r} is a {result_kind(result)} result; "
            "a reconstruction reads image data"
        )
    return ResultDataObj(result, **overrides)


def as_reconstruction_input(value):
    """``value`` as a reconstructor takes it: a ``DataObj`` (or anything that
    is not a result, a test stub say) as it is, a result bridged."""
    from imswitch.improcess.model.result import ProcessingResult

    if isinstance(value, ProcessingResult):
        return data_obj_from_result(value)
    return value


__all__ = [
    "IMAGE_RESULT_KINDS",
    "LAYOUT_ATTR_PREFIXES",
    "ResultDataObj",
    "as_reconstruction_input",
    "data_obj_from_result",
    "is_image_result",
    "result_attrs",
    "slice_attrs",
]


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
