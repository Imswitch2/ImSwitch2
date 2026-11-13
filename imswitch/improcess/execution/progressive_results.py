"""Bounded, semantic assembly of independent finite reconstruction results.

The first result becomes the job-owned public object; its array storage is
allocated once to the requested output extent. Later results fill their exact
logical slots, rather than repeatedly concatenating or replacing the object.

This is an opt-in facility for compatible scientific result types. Geometry,
axes and optional output layers must match; a historical MoNaLISA condition-
folded T is *not* an elapsed-time axis. Unsupported results fail explicitly
instead of being silently reshaped or having output layers dropped.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .result_assembly import AssemblyError, AssemblyRelation, ResultAssemblyPlan


_OPTIONAL_ARRAYS = ("sharpened", "background", "pinholes")


@dataclass
class _AllocatedLayer:
    name: str
    destination: np.ndarray
    axis: int
    item_shape: tuple[int, ...]


class ProgressiveResultAssembler:
    """Build one stable result for finite T or Dataset steps.

    No Qt, Session ownership, worker or reconstructor dependencies. ``append``
    must be called on the publishing thread with a completed result that the
    processing worker no longer mutates.
    """

    def __init__(self, plan: ResultAssemblyPlan):
        if plan.relation not in (AssemblyRelation.TIMEPOINT, AssemblyRelation.DATASET):
            raise AssemblyError("Progressive assembly requires explicit T or Dataset relation")
        if plan.expected_count is None or plan.expected_count < 1:
            raise AssemblyError("Progressive finite assembly requires known selected extent")
        self.plan = plan
        self.result = None
        self._layers: list[_AllocatedLayer] = []
        self._received: set[int] = set()
        self._reference = None

    @staticmethod
    def _appendable_axis(result, name: str):
        labels = list(getattr(result, "axis_labels", ()) or ())
        if len(labels) != len(set(labels)):
            raise AssemblyError("Result axis labels are not unique")
        if name in labels:
            return labels.index(name), False
        # All elapsed-time/Dataset semantics come from the *plan*. Introducing
        # an axis does not mean guessing which axis an input array represents.
        if not labels or len(labels) != np.ndim(result.data):
            raise AssemblyError("Result has no usable named axes")
        return 0, True

    @staticmethod
    def _reject_time_conditions(result):
        projection = getattr(result, "acquisition_projection", None) or {}
        components = projection.get("components", ()) if isinstance(projection, dict) else ()
        if "condition" in components:
            raise AssemblyError("MoNaLISA T folds acquisition conditions, not elapsed time")
        # A flexible MoNaLISA result with multiple conditions may use a
        # condition-leading T. Do not mislabel those conditions as timepoints.
        if (getattr(result, "condition_labels", ()) and "T" in result.axis_labels
                and "C" not in result.axis_labels):
            raise AssemblyError("MoNaLISA condition axis cannot be reused for timelapse T")

    @staticmethod
    def _semantic_signature(result):
        """Scientific meaning outside the chosen assembly coordinate."""
        offset = getattr(result, "pinhole_offsets", None)
        if offset is None:
            offsets = None
        else:
            offsets = tuple(tuple(np.asarray(values).ravel().tolist()) for values in offset)
        projection = getattr(result, "acquisition_projection", None) or {}
        components = tuple(projection.get("components", ())) if isinstance(projection, dict) else ()
        pixel = getattr(result, "output_pixel_size_nm", None)
        if pixel is not None:
            pixel = tuple(np.asarray(pixel).ravel().tolist())
        return (
            tuple(result.axis_labels), tuple(result.axis_scales),
            getattr(result, "scale_unit", None), getattr(result, "kind", "image"),
            pixel,
            tuple(getattr(result, "condition_labels", ()) or ()),
            tuple(getattr(result, "scanned_axes", ()) or ()),
            components, offsets,
        )

    def _normalize(self, result):
        axis_name = self.plan.axis
        if axis_name == "T":
            self._reject_time_conditions(result)
        axis, inserted = self._appendable_axis(result, axis_name)
        if inserted:
            labels = list(result.axis_labels)
            result.axis_labels = [axis_name, *labels]
            result.axis_scales = [1., *list(result.axis_scales)]
            modes = list(getattr(result, "view_modes", ()) or ())
            if modes:
                result.view_modes = [
                    type(mode)(mode.name, (0, *(int(i) + 1 for i in mode.transpose)))
                    for mode in modes
                ]
            for name in ("data", *_OPTIONAL_ARRAYS):
                value = getattr(result, name, None)
                if value is not None:
                    setattr(result, name, np.expand_dims(np.asarray(value), axis=0))
        if result.data.shape[axis] != 1:
            raise AssemblyError(
                f"Each independent {axis_name} result must occupy exactly one slot, "
                f"got {result.data.shape[axis]}"
            )
        if axis_name == "T" and getattr(result, "coeffs", None) is not None:
            # Old coefficients have (Dataset, Base, frames, row, col) and do
            # not carry the T coordinate; accumulating their first slot only
            # would make the Update reconstruction action scientifically wrong.
            raise AssemblyError("Classic coefficient results need a timepoint-aware coefficient adapter")
        return axis

    @staticmethod
    def _storage(example, axis, count):
        source = np.asarray(example)
        if source.dtype.kind not in "biufc":
            raise AssemblyError("Only numeric image arrays support progressive assembly")
        dims = list(source.shape)
        dims[axis] = count
        if source.dtype.kind in "fc":
            return np.full(tuple(dims), np.nan, dtype=source.dtype)
        return np.zeros(tuple(dims), dtype=source.dtype)

    @staticmethod
    def _slice(array, axis, ordinal):
        index = [slice(None)] * array.ndim
        index[axis] = slice(ordinal, ordinal + 1)
        return tuple(index)

    def _install(self, result, axis):
        self._layers = []
        for name in ("data", *_OPTIONAL_ARRAYS):
            part = getattr(result, name, None)
            if part is None:
                continue
            arr = np.asarray(part)
            if arr.ndim <= axis or arr.shape[axis] != 1:
                raise AssemblyError(f"{name} does not align with {self.plan.axis} axis")
            self._layers.append(_AllocatedLayer(
                name, self._storage(arr, axis, self.plan.expected_count),
                axis, tuple(arr.shape),
            ))
        # Classic Dataset coefficients carry Dataset at axis 0. If present,
        # keep every coefficient stack for Update reconstruction.
        coeffs = getattr(result, "coeffs", None)
        if coeffs is not None:
            if self.plan.relation != AssemblyRelation.DATASET or np.asarray(coeffs).shape[0] != 1:
                raise AssemblyError("Coefficients cannot be assembled on this semantic axis")
            arr = np.asarray(coeffs)
            self._layers.append(_AllocatedLayer(
                "coeffs", self._storage(arr, 0, self.plan.expected_count), 0,
                tuple(arr.shape),
            ))
        for layer in self._layers:
            value = np.asarray(getattr(result, layer.name))
            layer.destination[self._slice(layer.destination, layer.axis, 0)] = value
            setattr(result, layer.name, layer.destination)
        self.result = result

    def append(self, part, ordinal: int):
        ordinal = int(ordinal)
        self.plan.coordinate_for_ordinal(ordinal)
        if ordinal in self._received:
            raise AssemblyError(f"Duplicate {self.plan.axis} step {ordinal}")
        axis = self._normalize(part)
        if self.result is None:
            # On first step the result is repurposed as the accumulating output.
            # Store the unchanged first-step descriptors for subsequent
            # validation; do not run validation against the expanded shape.
            self._reference = self._semantic_signature(part)
            self._install(part, axis)
            if ordinal != 0:
                for layer in self._layers:
                    # _install copied the first item to slot 0; place it in
                    # the true slot, then restore the unfilled marker.
                    first = layer.destination[self._slice(layer.destination, layer.axis, 0)].copy()
                    layer.destination[self._slice(layer.destination, layer.axis, 0)] = (
                        np.nan if layer.destination.dtype.kind in "fc" else 0
                    )
                    layer.destination[self._slice(layer.destination, layer.axis, ordinal)] = first
        else:
            if self._semantic_signature(part) != self._reference:
                raise AssemblyError("Result axes, units, condition labels or pixel calibration differ")
            expected_layers = {layer.name for layer in self._layers}
            actual_layers = {name for name in ("data", *_OPTIONAL_ARRAYS, "coeffs")
                             if getattr(part, name, None) is not None}
            if expected_layers != actual_layers:
                raise AssemblyError("Optional output layers or coefficients differ")
            # Validate *all* layers before modifying any published storage.
            for layer in self._layers:
                arr = np.asarray(getattr(part, layer.name))
                if arr.shape != layer.item_shape or arr.dtype != layer.destination.dtype:
                    raise AssemblyError(f"{layer.name} shape or dtype differs between results")
            for layer in self._layers:
                arr = np.asarray(getattr(part, layer.name))
                layer.destination[self._slice(layer.destination, layer.axis, ordinal)] = arr
        self._received.add(ordinal)
        return self.result

    def trim_completed_prefix(self) -> None:
        """Drop never-published trailing T from a cancelled finite acquisition.

        The public result object retains identity, but its scientific arrays
        now contain only the proven complete prefix. Coverage/provenance still
        record the originally requested/planned extent. A non-contiguous
        result is left alone: shifting source T coordinates would be unsafe.
        """
        if (self.result is None or self.plan.relation != AssemblyRelation.TIMEPOINT
                or not self._received):
            return
        count = len(self._received)
        if self._received != set(range(count)) or count == self.plan.expected_count:
            return
        for layer in self._layers:
            index = [slice(None)] * layer.destination.ndim
            index[layer.axis] = slice(0, count)
            setattr(self.result, layer.name,
                    np.array(layer.destination[tuple(index)], copy=True))

    @property
    def completed_count(self):
        return len(self._received)

    @property
    def received_ordinals(self):
        return frozenset(self._received)


__all__ = ["ProgressiveResultAssembler"]
