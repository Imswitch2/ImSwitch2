"""Base contract for ImProcess processor plugins."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable

from qtpy import QtWidgets

from imswitch.improcess.model.result import ProcessingResult, result_kind


@dataclass(frozen=True)
class ProcessorOutput:
    """One or more ProcessingResults returned by a processor."""

    results: tuple[ProcessingResult, ...]

    def __init__(self, results):
        normalized = tuple(results)
        if not all(isinstance(result, ProcessingResult) for result in normalized):
            raise TypeError("ProcessorOutput results must be ProcessingResult objects")
        object.__setattr__(self, "results", normalized)


def attach_provenance(
    results, source, processor, params=None, inputs=()
) -> tuple[ProcessingResult, ...]:
    """Record on each result what it was derived from.

    Two kinds of provenance, attached together because they answer two halves
    of the same question. The spatial identity says which pixels the result
    shares with its source; the processing footprint says what was done to get
    there, and travels into the saved file's metadata.

    Applied centrally rather than in every processor: there are twenty-odd of
    them, and provenance that depends on each author remembering to add a line
    is provenance that is mostly missing. A processor only has to declare
    ``preserves_grid``; if it declares nothing, the output gets its own
    coordinate space, which is the answer that cannot mislead.
    """
    from imswitch.improcess.model.footprint import record_step

    results = tuple(results)
    # The footprint is recorded even for a source-less run: "cropped with these
    # ranges" is worth keeping whether or not the input is still identifiable.
    record_step(results, source, processor, params, inputs)
    if source is None:
        return results
    same_grid = bool(getattr(processor, "preserves_grid", None))
    for result in results:
        adopt = getattr(result, "adopt_identity_from", None)
        if callable(adopt):
            adopt(source, same_grid=same_grid)
    return results


def normalize_processor_output(
    output, source=None, processor=None, params=None, inputs=()
) -> tuple[ProcessingResult, ...]:
    """Normalize a processor return value to a tuple of results.

    When ``source`` and ``processor`` are given, provenance is attached here so
    every processor inherits it without having to opt in. ``params`` are what
    the run was asked for, and become the footprint step's settings.
    """
    if isinstance(output, ProcessorOutput):
        return attach_provenance(output.results, source, processor, params, inputs)
    if isinstance(output, ProcessingResult):
        return attach_provenance((output,), source, processor, params, inputs)
    if isinstance(output, (list, tuple)):
        results = tuple(output)
        if all(isinstance(result, ProcessingResult) for result in results):
            return attach_provenance(results, source, processor, params, inputs)
    raise TypeError(
        "Processor output must be a ProcessingResult, ProcessorOutput, "
        "or a sequence of ProcessingResult objects"
    )


class Processor(ABC):
    """
    Operates on a ProcessingResult, returns a new one. Stackable.
    
    Processors are modality-agnostic by design — drift correction works
    on anything with a time axis regardless of which Reconstructor produced it.
    
    Examples:
    - Drift correction (requires time axis)
    - Denoising (works on any array)
    - Max-projection along an axis
    - FLIM lifetime overlay (requires FLIM metadata)
    """
    
    # Class attributes (override in subclasses)
    name: str = "Unnamed Processor"  # Human-readable
    id: str = "unnamed"  # Stable identifier for config + registry
    category: str = "Other"  # Human-readable grouping for runtime tools/docs
    #: Semantic result kinds this processor accepts (ProcessingResult.kind).
    #: Checked by accepts() BEFORE the shape/axis gate, so a table result with
    #: a 2D data array is never offered to an image processor.
    kinds: tuple[str, ...] = ("image",)
    #: How many results this processor consumes in one run. The default 1..1
    #: is the ordinary "one result in, one result out" processor.
    #:
    #: A processor with ``max_inputs != 1`` is a MULTI-INPUT processor: the UI
    #: passes the ordered inputs as ``params["results"]`` and calls
    #: ``apply(results[0], params)``, so ``apply`` must read its inputs from
    #: the params rather than from the first argument alone. ``max_inputs =
    #: None`` means unbounded (merge as many channels as you like).
    #:
    #: Single-input processors are run over several results by looping — the
    #: UI's "Apply to: selected/all results" scope — which needs no opt-in
    #: because ``apply`` is a pure function of one result.
    min_inputs: int = 1
    max_inputs: int | None = 1
    #: Whether this processor's output sits on the *same pixel grid* as its
    #: input, so an ROI drawn on one measures the same features on the other.
    #: True for filters, thresholds, projections along a non-spatial axis;
    #: False for anything that crops, resamples, rescales or reprojects.
    #: ``None`` means "not declared", and the output then gets a fresh
    #: coordinate space — the safe answer, because wrongly claiming a shared
    #: grid makes ROIs measure the wrong pixels.
    preserves_grid: bool | None = None
    #: Whether this processor can be run over an ROI rather than a whole frame
    #: (P-R). Opt-in, because the answer is not universal: a drift correction
    #: over a cropped region is a different measurement, not a cheaper one, and
    #: offering it would invite a result nobody can interpret.
    #:
    #: The restriction is applied **around** the processor by the run path —
    #: `apply` still receives an ordinary result — so declaring this is the
    #: whole of what a processor has to do.
    accepts_roi: bool = False
    #: Which restriction modes make sense here, in the order offered. A filter
    #: usually wants `mask` first (its output stays pixel-aligned); anything
    #: whose cost scales with the frame usually wants `crop`.
    roi_modes: tuple[str, ...] = ("crop", "mask")

    def accepts(self, result: ProcessingResult) -> bool:
        """Full compatibility gate: semantic kind, then shape/axis contract.

        UI code deciding whether to offer this processor for a result must
        call this, not ``applies_to`` directly — ``applies_to`` only encodes
        the shape/axis contract and cannot tell a metrics table from an
        image.
        """
        return result_kind(result) in self.kinds and bool(self.applies_to(result))

    def check_inputs(self, results) -> tuple[bool, str]:
        """Return ``(ok, reason)`` for running this processor on ``results``.

        The reason is user-facing: UI offering a multi-input processor shows
        it instead of presenting a silently disabled button, which is the
        whole point of routing compatibility through one hook. Subclasses that
        need their inputs to agree on more than count (shape, axis labels,
        pixel scales) override this and return a reason naming the mismatch.

        Must stay metadata-only — no pixel data is materialized here, so a
        lazily-backed result can be offered without reading it from disk.
        """
        results = list(results or [])
        count = len(results)
        if count < self.min_inputs:
            noun = "result" if self.min_inputs == 1 else "results"
            return False, f"{self.name} needs at least {self.min_inputs} {noun}"
        if self.max_inputs is not None and count > self.max_inputs:
            noun = "result" if self.max_inputs == 1 else "results"
            return False, (
                f"{self.name} takes at most {self.max_inputs} {noun}, "
                f"but {count} are selected"
            )
        for index, result in enumerate(results, start=1):
            if not self.accepts(result):
                name = getattr(result, "name", f"input {index}")
                return False, f"{self.name} does not apply to '{name}'"
        return True, ""

    @property
    @abstractmethod
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        """
        Shape/axis gate: return True if this processor can handle the given
        result's array layout. Semantic-kind filtering is handled by
        ``accepts()``; keep this gate about shapes and axis labels.

        Examples:
        - Drift correction: lambda r: "T" in r.axis_labels
        - Z-projection: lambda r: "Z" in r.axis_labels
        - Denoising: lambda r: True  # works on anything
        """
        ...
    
    @abstractmethod
    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        """
        Return the parameter editor widget shown in the processing chain panel.
        
        Should expose a `get_values() -> dict` method.
        """
        ...
    
    @abstractmethod
    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult | ProcessorOutput:
        """
        Apply the processing step.
        
        Pure function: no side effects, no GUI updates.
        Returns one or more ProcessingResults (may reuse the input data array or copy).
        
        Args:
            result: Input result from a reconstructor or previous processor
            params: Parameter dict from `make_param_widget().get_values()`
        
        Returns:
            New ProcessingResult with updated data/axis_labels/view_modes
        """
        ...


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
