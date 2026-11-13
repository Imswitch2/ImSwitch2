"""Canonical scientific MoNaLISA result (M2D-B).

New Fast, Full/lattice and Legacy reconstructions all create a ``MonalisaResult``.
The original fixed-6D and flexible-image result implementations remain as
private *format adapters* until old saved projects and downstream consumers
have completed their migration.  They continue to own their established
rendering and writing contracts, not the Session/queue lifecycle.

The result's source representation is explicit: a historical 6D ``T`` axis
may fold acquisition conditions into elapsed time, whereas a flexible result
has named T/C/Z axes.  No axis relationship is inferred from the NumPy shape.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from imswitch.improcess.model.result import ProcessingResult


CLASSIC = "coefficients_6d"
FLEXIBLE = "flexible_image"


def _classic_adapter():
    # Lazy imports avoid the compatibility modules importing this class while
    # defining their original (pickle-compatible) result implementations.
    from .result import _ClassicMonalisaResultAdapter
    return _ClassicMonalisaResultAdapter


def _flexible_adapter():
    from imswitch.improcess.reconstructors.monalisa_lattice.result import (
        _FlexibleMonalisaResultAdapter,
    )
    return _FlexibleMonalisaResultAdapter


class MonalisaResult(ProcessingResult):
    """One new result class for all MoNaLISA reconstruction engines.

    The classic ``data=..., scan_params=...`` and flexible
    ``image=..., params=..., diagnostics=...`` constructor signatures are
    accepted temporarily, so existing producer code and saved projects can
    migrate independently.  All freshly constructed objects are this class.

    Result representation describes *how an image is stored and displayed*;
    it never selects a reconstruction algorithm or timepoint assembly axis.
    """

    # Preserve the historical class-level format contract for importers that
    # query MonalisaProcessingResult.supported_formats rather than an instance.
    supported_formats = ("tiff", "hdf5", "zarr", "imagej")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if "image" in kwargs:
            if "data" in kwargs or "scan_params" in kwargs:
                raise TypeError("Choose either image or classic data/scan_params")
            original = _flexible_adapter()(*args, **kwargs)
            variant = FLEXIBLE
        else:
            original = _classic_adapter()(*args, **kwargs)
            variant = CLASSIC
        self._inherit(original, variant)

    def _inherit(self, original: ProcessingResult, variant: str) -> None:
        # The adapter constructor validates every image, layer and axis before
        # publication.  Transfer its initialized state without copying large
        # data arrays or generating new identity/provenance UUIDs.
        self.__dict__.update(original.__dict__)
        self._monalisa_representation = variant
        self.supported_formats = original.supported_formats
        self.publishes_table_rows = bool(getattr(original, "publishes_table_rows", False))
        # This is scientific output description, not Session job state. The
        # ordinary result serializer includes it in provenance on any format.
        metadata = getattr(self, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = {}
            self.metadata = metadata
        metadata.setdefault("monalisa_result", {
            "schema": 1,
            **self.scientific_axis_metadata,
        })

    def __setstate__(self, state: dict) -> None:
        """Restore results pickled under either historical public class name."""
        self.__dict__.update(state)
        # Earlier classes supplied these as class attributes; after module
        # aliases resolve to MonalisaResult, recover the correct instance policy.
        adapter = _flexible_adapter() if self.is_flexible else _classic_adapter()
        self.supported_formats = adapter.supported_formats
        self.publishes_table_rows = bool(getattr(adapter, "publishes_table_rows", False))

    @classmethod
    def from_coeffs(
        cls,
        name: str,
        coeffs: np.ndarray,
        scan_params: dict,
        axis_label_map: dict[str, str],
        display_levels: tuple[float, float] | None = None,
        placement=None,
    ) -> "MonalisaResult":
        """Legacy-compatible coefficient assembly with no extra image copy."""
        original = _classic_adapter().from_coeffs(
            name=name,
            coeffs=coeffs,
            scan_params=scan_params,
            axis_label_map=axis_label_map,
            display_levels=display_levels,
            placement=placement,
        )
        result = cls.__new__(cls)
        result._inherit(original, CLASSIC)
        return result

    @property
    def monalisa_representation(self) -> str:
        """Reconstruct a historical object's variant from its saved fields."""
        explicit = getattr(self, "_monalisa_representation", None)
        if explicit in (CLASSIC, FLEXIBLE):
            return explicit
        # Old serialized result dictionaries did not include the new marker.
        # This is a format signature, NOT an inference of timepoint semantics.
        if "scan_params" in self.__dict__:
            return CLASSIC
        if "diagnostics" in self.__dict__ and "params" in self.__dict__:
            return FLEXIBLE
        raise ValueError("Unknown MoNaLISA result representation")

    @property
    def is_flexible(self) -> bool:
        return self.monalisa_representation == FLEXIBLE

    @property
    def scientific_axis_metadata(self) -> dict[str, Any]:
        """Expose axes without treating folded historical T as elapsed time."""
        labels = tuple(self.axis_labels)
        result = {"axes": labels, "representation": self.monalisa_representation}
        if not self.is_flexible:
            projection = getattr(self, "acquisition_projection", None)
            if isinstance(projection, dict):
                result["acquisition_projection"] = dict(projection)
        else:
            result["condition_labels"] = tuple(getattr(self, "condition_labels", ()))
        return result

    # ---- Format-specific scientific / display behavior, kept as adapters ---
    def initial_view_mode_name(self) -> str:
        implementation = _flexible_adapter() if self.is_flexible else _classic_adapter()
        return implementation.initial_view_mode_name(self)

    def _base_component_name(self, base_index: int) -> str:
        return _classic_adapter()._base_component_name(self, base_index)

    def _planes(self):
        return _flexible_adapter()._planes(self)

    def _like_image(self, plane):
        return _flexible_adapter()._like_image(self, plane)

    def display_layers(self):
        implementation = _flexible_adapter() if self.is_flexible else _classic_adapter()
        return implementation.display_layers(self)

    def display_layer_data(self):
        implementation = _flexible_adapter() if self.is_flexible else _classic_adapter()
        return implementation.display_layer_data(self)

    def table_columns(self) -> list[str]:
        if self.is_flexible:
            return _flexible_adapter().table_columns(self)
        return ProcessingResult.table_columns(self)

    def table_records(self) -> list[dict[str, Any]]:
        if self.is_flexible:
            return _flexible_adapter().table_records(self)
        return ProcessingResult.table_records(self)

    def getCoeffs(self):
        return getattr(self, "coeffs", None)

    def getScanParams(self) -> dict:
        return getattr(self, "scan_params", {})

    def updateScanParams(self, scan_params: dict) -> None:
        # The old flexible result did not support this legacy controller
        # action. Its canonical compatibility method is deliberately a no-op
        # rather than changing the Gaussian result or raising from a Qt slot.
        if not self.is_flexible:
            _classic_adapter().updateScanParams(self, scan_params)

    def updateImages(self) -> None:
        """Keep the classic coefficient re-rendering action unchanged."""
        if not self.is_flexible:
            _classic_adapter().updateImages(self)

    def _fold_for_disk(self):
        if self.is_flexible:
            raise ValueError("Flexible MoNaLISA results use native named axes")
        return _classic_adapter()._fold_for_disk(self)

    def serialization_view(self):
        if self.is_flexible:
            return ProcessingResult.serialization_view(self)
        return _classic_adapter().serialization_view(self)

    def write_files(self, plan, document) -> None:
        implementation = _flexible_adapter() if self.is_flexible else _classic_adapter()
        implementation.write_files(self, plan, document)


def is_flexible_monalisa_result(result: object) -> bool:
    """Recognize the flexible representation, including historical results.

    Class-name checks cannot distinguish the variants after unification;
    explicit representation or legacy scientific fields can.
    """
    if isinstance(result, MonalisaResult):
        return result.is_flexible
    return ("diagnostics" in getattr(result, "__dict__", {}) and
            "params" in getattr(result, "__dict__", {}) and
            "scan_params" not in getattr(result, "__dict__", {}))
