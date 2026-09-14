"""Processing result abstractions for ImProcess reconstructors and processors."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np

from imswitch.imcommon.algorithms.spatial_frame import mint_uid

from .plotting import PlotPayload

#: Semantic result kinds. ``data`` alone cannot distinguish a microscope
#: image from a metrics table with a 2D array — the kind says what the
#: values MEAN, so processors and UI can filter on semantics instead of
#: shape alone.
#:
#: - ``"image"`` — calibrated intensity image/stack (the default);
#: - ``"labels"`` — integer segmentation label mask;
#: - ``"table"`` — tabular metrics (rows x named metric columns);
#: - ``"curve"`` — sampled function values (e.g. FRC over frequency);
#: - ``"localization"`` — point-emitter coordinate table with render;
#: - ``"rgb"`` — autoscaled uint8 visualization/export product, not
#:   quantitative intensities;
#: - ``"composite"`` — multi-channel intensity stack with independently
#:   scaled display channels.
RESULT_KINDS = (
    "image",
    "labels",
    "table",
    "curve",
    "localization",
    "rgb",
    "composite",
)


def result_kind(result) -> str:
    """Semantic kind of ``result``; duck-typed/legacy results count as images."""
    return str(getattr(result, "kind", "image") or "image")


@dataclass
class ViewMode:
    """Named axis permutation for ReconstructionView."""
    name: str
    transpose: tuple[int, ...]


@dataclass
class DisplayLayerSpec:
    """One Napari layer derived from a processing result.

    Results can expose these when their canonical data array groups
    semantically different images that should not share contrast limits, or
    when the result renders as a non-image napari layer (labels/points/shapes).

    ``kind`` selects the napari layer type the renderer adds:

    - ``"image"`` (default) — an intensity image (``add_image``);
    - ``"labels"`` — an integer label mask (``add_labels``);
    - ``"points"`` — an ``(N, D)`` coordinate array (``add_points``);
    - ``"shapes"`` — a list of shape vertex arrays (``add_shapes``).

    ``role`` tells the processing UI how to interpret the layer:

    - ``"primary"`` — the result's canonical output;
    - ``"context"`` — a background/source layer shown only to interpret the
      output (e.g. the source image behind a segmentation mask); never offered
      as a processor input;
    - ``"overlay"`` — an auxiliary annotation layer.

    ``component`` is the stable id used for display-setting persistence and
    explicit component inputs (falls back to ``metadata['component']`` then
    ``name``). ``layer_kwargs`` carries kind-specific napari options
    (e.g. ``size``/``face_color`` for points, ``shape_type`` for shapes).
    """

    name: str
    data: np.ndarray | Any
    axis_labels: list[str]
    display_levels: tuple[float, float] | None = None
    axis_scales: list[float] | None = None
    scale_unit: str = "px"
    colormap: str = "grayclip"
    rgb: bool = False
    visible: bool = True
    metadata: dict[str, Any] | None = None
    kind: str = "image"
    role: str = "primary"
    component: str | None = None
    layer_kwargs: dict[str, Any] | None = None
    #: Identity of the pixel grid this layer is on. Display layers of one
    #: result can sit on *different* grids (a mask and a differently-sized
    #: overlay), so this is per-layer rather than inherited wholesale from the
    #: parent result. ``None`` means "the parent result's grid".
    coordinate_space_uid: str | None = None


@dataclass
class ProcessorInputChoice:
    """One explicit input option for result-based processors."""

    id: str
    label: str
    result: "ProcessingResult"


class ProcessingResult(ABC):
    """
    Abstract base for all reconstructor/processor outputs.
    
    Replaces the direct ReconObj exposure. Each reconstructor returns a
    subclass tailored to its output format (MoNaLISA coefficients vs.
    raw frames vs. STED deconvolved stacks, etc.).
    """

    #: Semantic kind of this result (one of RESULT_KINDS). Subclasses whose
    #: ``data`` is not a calibrated intensity image MUST override this, or
    #: shape-only gates will offer image processors on non-image values.
    kind: str = "image"
    #: Publish :meth:`table_records` to the shared Results dock on production,
    #: for a result whose ``kind`` is not ``"table"``.
    #:
    #: An analysis that fits something yields two things — the curve and the
    #: parameters of the fit — and the parameters are the answer. A curve
    #: result that leaves this False renders a picture of numbers with no way
    #: to read the numbers out. Set it True when the rows are a handful of
    #: summary values; leave it False when they are bulk data (a localization
    #: table can run to six figures of rows).
    publishes_table_rows: bool = False

    def __init__(
        self,
        name: str,
        data: np.ndarray | Any,  # Allow zarr.Array in future
        axis_labels: list[str],
        view_modes: list[ViewMode] | None = None,
        display_levels: tuple[float, float] | None = None,
        axis_scales: list[float] | None = None,
        scale_unit: str = "px",
        *,
        result_uid: str | None = None,
        dataset_uid: str | None = None,
        coordinate_space_uid: str | None = None,
        lineage: tuple[str, ...] = (),
        identity_kind: str = "minted",
    ):
        """
        Args:
            name: Human-readable result name (e.g., "MoNaLISA reconstruction",
                  "Drift-corrected frames")
            data: N-dimensional array, no fixed shape requirement
            axis_labels: Dimension labels in data order, e.g. ["T", "Z", "Y", "X"]
                         or ["Dataset", "Base", "T", "Z", "Y", "X"]
            view_modes: Optional list of axis permutations for viewer. If None,
                        defaults to a single "Standard" mode with no transposition.
            display_levels: Optional (min, max) display range for initial viewer LUT
        """
        self.name = name
        self.data = data
        self.axis_labels = axis_labels
        self.display_levels = display_levels
        self.display_colormap = "grayclip"
        # Four identities, kept apart on purpose (see
        # imcommon.algorithms.spatial_frame): the dataset a result came from,
        # the result itself, the pixel grid it lives on, and what it derives
        # from. `name` is a mutable display label and cannot serve as any of
        # them — two unrelated results can share a name, and renaming one must
        # not make it a different result.
        self.result_uid = result_uid or mint_uid("result")
        self.dataset_uid = dataset_uid or mint_uid("data")
        self.coordinate_space_uid = coordinate_space_uid or mint_uid("space")
        #: What ROI restricted this result, when one did (P-R). Empty for a
        #: whole-frame run, so its absence means "the whole image" rather than
        #: "unknown".
        self.roi_provenance: dict[str, Any] = {}
        self.lineage = tuple(lineage)
        self.identity_kind = identity_kind
        self._display_layer_settings: dict[str, dict[str, Any]] = {}
        self.axis_scales = (
            axis_scales
            if axis_scales is not None
            else [1.0 for _ in range(data.ndim)]
        )
        self.scale_unit = scale_unit
        
        if view_modes is None:
            # Default: single standard view with identity permutation
            self.view_modes = [ViewMode("Standard", tuple(range(data.ndim)))]
        else:
            self.view_modes = view_modes

    def adopt_identity_from(self, source: "ProcessingResult", *, same_grid: bool):
        """Record that this result was derived from ``source``; returns self.

        Applied after construction rather than threaded through every
        subclass's ``__init__``: there are twenty-odd result types, and adding
        five keyword arguments to each of them would be a large change that
        every future result type would have to remember to repeat.

        See :meth:`derived_identity` for what ``same_grid`` means — it is the
        one judgement the caller has to make, and it is not cosmetic.

        A source that cannot describe its identity (a plugin result that does
        not derive from this class, say) leaves this result with the fresh
        identity it was born with. Losing provenance is a real cost, but it is
        a smaller one than a processor refusing to run.
        """
        describe = getattr(source, "derived_identity", None)
        if not callable(describe):
            return self
        identity = describe(same_grid=same_grid)
        self.dataset_uid = identity["dataset_uid"]
        self.lineage = identity["lineage"]
        self.identity_kind = identity["identity_kind"]
        if identity["coordinate_space_uid"] is not None:
            self.coordinate_space_uid = identity["coordinate_space_uid"]
        return self

    def mint_coordinate_space(self) -> None:
        """Give this result a pixel grid of its own (P-R).

        Called when an output cannot be on its source's grid however the
        processor is declared — a crop, most obviously, which moves every pixel
        index. Claiming a shared grid there makes every ROI measured on the
        output read the wrong pixels, which is exactly what the identity is
        for.
        """
        self.coordinate_space_uid = mint_uid("space")

    def derived_identity(self, *, same_grid: bool) -> dict[str, Any]:
        """Identity kwargs for a result derived from this one.

        Pass ``same_grid=True`` when the output is pixel-aligned with this
        result by construction — a projection, a filter, a threshold — so ROIs
        drawn on one measure correctly on the other.  Pass ``False`` when the
        pixel grid changes: a crop with an offset, a resample, a rescale.
        Getting this wrong is not cosmetic; ``coordinate_space_uid`` is what
        decides whether two results are considered the same grid at all.

        ``dataset_uid`` is always inherited (the data still came from the same
        acquisition) and ``lineage`` records which result this came from.
        """
        return {
            "dataset_uid": self.dataset_uid,
            "coordinate_space_uid": self.coordinate_space_uid if same_grid else None,
            "lineage": (*self.lineage, self.result_uid),
            "identity_kind": self.identity_kind,
        }

    def setDispLevels(self, levels) -> None:
        """Compatibility hook for the legacy ReconstructionViewController."""
        self.display_levels = levels

    def getDispLevels(self):
        """Compatibility hook for the legacy ReconstructionViewController."""
        return self.display_levels

    def setDisplayColormap(self, colormap: str) -> None:
        """Persist the preferred colormap for the primary image layer."""
        self.display_colormap = str(colormap)

    def getDisplayColormap(self) -> str:
        """Return the preferred colormap for the primary image layer."""
        return self.display_colormap

    def setDisplayLayerLevels(self, layer_id: str, levels) -> None:
        settings = self._display_layer_settings.setdefault(str(layer_id), {})
        settings["display_levels"] = tuple(float(value) for value in levels)

    def getDisplayLayerLevels(self, layer_id: str):
        settings = self._display_layer_settings.get(str(layer_id), {})
        return settings.get("display_levels")

    def setDisplayLayerColormap(self, layer_id: str, colormap: str) -> None:
        settings = self._display_layer_settings.setdefault(str(layer_id), {})
        settings["colormap"] = str(colormap)

    def getDisplayLayerColormap(self, layer_id: str, default: str = "grayclip") -> str:
        settings = self._display_layer_settings.get(str(layer_id), {})
        return str(settings.get("colormap", default))

    def setDisplayLayerVisible(self, layer_id: str, visible: bool) -> None:
        settings = self._display_layer_settings.setdefault(str(layer_id), {})
        settings["visible"] = bool(visible)

    def getDisplayLayerVisible(self, layer_id: str, default: bool = True) -> bool:
        settings = self._display_layer_settings.get(str(layer_id), {})
        return bool(settings.get("visible", default))

    def displayLayerSettings(self) -> dict[str, dict[str, Any]]:
        """Return a copy of persisted display-layer overrides."""
        return {
            layer_id: dict(settings)
            for layer_id, settings in self._display_layer_settings.items()
        }
    
    @abstractmethod
    def save(self, path: Path, fmt: str) -> None:
        """
        Save the result to disk.
        
        Args:
            path: Output file path
            fmt: Format string (e.g., "tiff", "hdf5", "zarr")
        
        The implementation is modality-specific: MoNaLISA saves 6D ImageJ TIFFs
        with specific axis order, STED might save multi-channel TIFFs, etc.
        """
        ...

    def plot_payloads(self) -> list[PlotPayload]:
        """Return optional graph payloads for the ImProcess graph widget."""
        return []

    def table_columns(self) -> list[str]:
        """Return the column order for the shared results table.

        Third rendering channel alongside :meth:`display_layers` (napari) and
        :meth:`plot_payloads` (graph panel): results whose values are rows of
        named metrics expose them here instead of leaving the numbers locked
        inside ``data``. Empty (the default) means the result has no tabular
        view.
        """
        return []

    def table_records(self) -> list[dict[str, Any]]:
        """Return one dict per row, keyed by :meth:`table_columns`.

        Shaped for ``ResultsTableWidget.append_records(columns, records)``.
        Results with ``kind == "table"`` are appended to the shared Results
        dock automatically when produced; other kinds (e.g. ``"localization"``,
        whose row count can reach six figures) expose records here for
        explicit export, and opt into automatic display by setting
        :attr:`publishes_table_rows`.
        """
        return []

    def display_layers(self) -> list[DisplayLayerSpec]:
        """Return optional independently-scaled viewer layers.

        The default empty list keeps the existing single-layer display path.
        Subclasses should use this only when their canonical ``data`` array
        contains heterogeneous components that should be inspected separately.
        """
        return []

    def applyDisplayLayerSettings(
        self,
        layers: list[DisplayLayerSpec],
    ) -> list[DisplayLayerSpec]:
        """Apply persisted per-layer display overrides to layer specs."""
        adjusted = []
        for layer in layers:
            layer_id = _display_layer_component_id(layer)
            settings = self._display_layer_settings.get(layer_id, {})
            adjusted.append(
                replace(
                    layer,
                    display_levels=settings.get("display_levels", layer.display_levels),
                    colormap=settings.get("colormap", layer.colormap),
                    visible=settings.get("visible", layer.visible),
                )
            )
        return adjusted

    def processor_input_choices(self) -> list[ProcessorInputChoice]:
        """Return explicit input choices for result-based processors.

        The whole result is always offered first. If the result exposes
        display layers, each layer is also wrapped as a lightweight
        ProcessingResult so processors can operate on a named component
        without implicitly depending on Napari's active layer.
        """
        choices = [ProcessorInputChoice("result", "Whole result", self)]
        for layer in self.display_layers():
            # Context layers are display-only (e.g. the source image behind a
            # segmentation mask) — never offer them as a processor input.
            if getattr(layer, "role", "primary") == "context":
                continue
            component = _display_layer_component_id(layer)
            choices.append(
                ProcessorInputChoice(
                    id=f"component:{component}",
                    label=str(component),
                    result=DisplayLayerProcessingResult.from_spec(self, layer),
                )
            )
        return choices


class DisplayLayerProcessingResult(ProcessingResult):
    """ProcessingResult wrapper for one DisplayLayerSpec component."""

    def __init__(
        self,
        name: str,
        data: np.ndarray | Any,
        axis_labels: list[str],
        *,
        source_result: ProcessingResult,
        component: str,
        display_levels: tuple[float, float] | None = None,
        axis_scales: list[float] | None = None,
        scale_unit: str = "px",
        metadata: dict[str, Any] | None = None,
        kind: str = "image",
    ):
        super().__init__(
            name=name,
            data=data,
            axis_labels=axis_labels,
            display_levels=display_levels,
            axis_scales=axis_scales,
            scale_unit=scale_unit,
        )
        self.source_result = source_result
        self.component = component
        self.metadata = dict(metadata or {})
        self.kind = str(kind or "image")

    @classmethod
    def from_spec(
        cls,
        source_result: ProcessingResult,
        layer: DisplayLayerSpec,
    ) -> "DisplayLayerProcessingResult":
        component = _display_layer_component_id(layer)
        wrapped = cls(
            name=f"{source_result.name}_{component}",
            data=layer.data,
            axis_labels=list(layer.axis_labels),
            source_result=source_result,
            component=component,
            display_levels=layer.display_levels,
            axis_scales=layer.axis_scales,
            scale_unit=layer.scale_unit,
            metadata=layer.metadata,
            # A component input inherits the layer's semantics: an image
            # channel stays "image", a labels layer stays "labels", and
            # points/shapes pass through (matching no image processor).
            kind=layer.kind,
        )
        # A component is a *view* of its parent, not new data: it must inherit
        # the parent's identity, or an ROI drawn on the displayed result would
        # be judged unrelated to the very component it was drawn over. The
        # spec may override the coordinate space when a display layer sits on
        # its own grid (a differently-sized overlay).
        wrapped.adopt_identity_from(source_result, same_grid=True)
        if getattr(layer, "coordinate_space_uid", None):
            wrapped.coordinate_space_uid = layer.coordinate_space_uid
        return wrapped

    def save(self, path: Path, fmt: str) -> None:
        raise ValueError(
            "Display-layer processor inputs are derived components; save the "
            "processor output or the original source result instead."
        )


def _display_layer_component_id(layer: DisplayLayerSpec) -> str:
    if getattr(layer, "component", None):
        return str(layer.component)
    metadata = layer.metadata or {}
    component = metadata.get("component")
    if component:
        return str(component)
    return str(layer.name)


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
