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


def normalize_processor_output(output) -> tuple[ProcessingResult, ...]:
    """Normalize a processor return value to a tuple of results."""
    if isinstance(output, ProcessorOutput):
        return output.results
    if isinstance(output, ProcessingResult):
        return (output,)
    if isinstance(output, (list, tuple)):
        results = tuple(output)
        if all(isinstance(result, ProcessingResult) for result in results):
            return results
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

    def accepts(self, result: ProcessingResult) -> bool:
        """Full compatibility gate: semantic kind, then shape/axis contract.

        UI code deciding whether to offer this processor for a result must
        call this, not ``applies_to`` directly — ``applies_to`` only encodes
        the shape/axis contract and cannot tell a metrics table from an
        image.
        """
        return result_kind(result) in self.kinds and bool(self.applies_to(result))

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
