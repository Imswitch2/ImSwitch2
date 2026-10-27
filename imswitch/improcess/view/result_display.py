"""The surface ImProcess's controllers and image toolbar use to show a result.

Until now the only display was :class:`ReconstructionView` (an embedded napari
viewer), so the controllers called it directly and nothing said which of its
methods were the contract and which were napari housekeeping. A second display
(one pyqtgraph window per result, ``image_window.ImageWindow``) has to serve
the same calls, so the contract is written down here, derived from the call
sites in ``ReconstructionViewController`` and ``ImageToolbarController`` and
nothing else. A method that is not called from there is not in this file.

Two protocols, because the embedded view does two jobs and a window does one:

- :class:`ResultDisplay` -- render one result and expose its contrast. The
  napari view and each separate window implement it.
- :class:`ResultListPane` -- the list of loaded results. Only the embedded
  view owns it; in window mode it stays there and the windows hang off it.

Conventions shared by every method below:

- *Levels* are ``(minimum, maximum)`` in the data's own units.
- A *layer id* is the layer's ``component`` when it has one, else its name,
  which is the key the result files its per-layer settings under.
- A *layer state* is a dict with ``id``, ``name``, ``visible``, ``colormap``,
  ``display_levels`` (``(min, max)`` or None) and ``metadata``.
"""

from __future__ import annotations

from typing import Any, Iterable, Protocol, runtime_checkable


@runtime_checkable
class ResultDisplay(Protocol):
    """Render a result and report/accept its contrast."""

    #: Emitted with the new dims position (a tuple of indices, one per axis in
    #: displayed order) whenever a non-displayed axis moves.
    sigAxisStepChanged: Any
    #: Emitted when the user picks another view mode (axis permutation).
    sigViewChanged: Any
    #: Emitted as ``(layer metadata dict, (min, max))`` whenever a layer's
    #: contrast changes, from whichever source.
    sigImageLevelsChanged: Any

    # --- what is shown -----------------------------------------------------

    def setImage(
        self, im, axisLabels, axisScales=None, scaleUnit="px", colormap="grayclip",
        name=None, identity=None,
    ) -> None:
        """Show one array as the (only) image; ``im`` is already in displayed axis order.

        ``axisScales`` None means 1.0 per axis; ``name`` None means unnamed;
        ``identity`` is the spatial-provenance dict, or None to clear it.
        """

    def setDisplayLayers(self, layerSpecs, identity=None) -> None:
        """Show a result's ``DisplayLayerSpec`` list; an empty/unusable list clears the display."""

    def clearImage(self) -> None:
        """Show nothing."""

    def tryFastLiveUpdate(self, result, transposeOrder) -> bool:
        """Swap pixels in place for a repeat live update; False (nothing touched) if the layers no longer match."""

    def moveDimStep(self, axis: int, step: int) -> None:
        """Move the slider of displayed axis ``axis`` to ``step`` (clamped); an unknown axis is ignored."""

    def resetView(self) -> None:
        """Fit the whole image into the view."""

    # --- view modes ----------------------------------------------------------

    def setViewModes(self, viewModes) -> None:
        """Offer these ``ViewMode`` objects; None or empty restores the defaults."""

    def getViewName(self):
        """Name of the selected view mode."""

    # --- the image(s) and their contrast ------------------------------------

    def getImage(self):
        """The array of the primary image layer (may be lazy)."""

    def getImageDisplayLevels(self):
        """Levels of the primary image layer."""

    def setImageDisplayLevels(self, minimum, maximum) -> None:
        """Set the primary image layer's levels."""

    def setImageDisplayLevelsRange(self, minimum, maximum) -> None:
        """Set the range the primary image layer's contrast slider spans."""

    def getActiveImage(self):
        """The array of the active image layer (may be lazy)."""

    def getActiveImageLayerMetadata(self) -> dict:
        """Metadata of the active layer (``component``, ``source_result``, ...); a copy."""

    def getActiveImageDisplayLevels(self):
        """Levels of the active image layer."""

    def setActiveImageDisplayLevels(self, minimum, maximum) -> None:
        """Set the active layer's levels."""

    def setActiveImageDisplayLevelsRange(self, minimum, maximum) -> None:
        """Set the range the active layer's contrast slider spans."""

    def getActiveImageColormap(self) -> str:
        """Name of the active layer's colormap (LUT)."""

    def setActiveImageColormap(self, colormap: str) -> None:
        """Set the active layer's colormap by name."""

    def getImageLayerStates(self) -> list[dict]:
        """One state dict per image layer, in drawing order."""

    def setImageLayerVisible(self, layer_id: str, visible: bool) -> None:
        """Show/hide one layer; an unknown id is ignored."""

    def setImageLayerColormap(self, layer_id: str, colormap: str) -> None:
        """Set one layer's colormap; an unknown id is ignored."""


@runtime_checkable
class ResultListPane(Protocol):
    """The list of loaded results, as the controllers use it."""

    #: The Qt list widget itself (the live path asks it for row/current item).
    reconList: Any
    #: Current list item changed.
    sigItemSelected: Any
    #: Selection (not only the current item) changed.
    sigSelectionChanged: Any
    #: One or more results were removed from the list.
    sigResultsRemoved: Any

    def addNewData(self, reconObj, name):
        """Append an entry, de-duplicating ``name`` with a ``.N`` suffix; selects it."""

    def addNamedData(self, reconObj, name):
        """Append an entry labelled exactly ``name``; selects it; returns the list item."""

    def replaceData(self, oldObj, newObj, name) -> bool:
        """Put ``newObj`` where ``oldObj`` is listed; False when ``oldObj`` is not listed."""

    def getCurrentItemIndex(self):
        """Row of the current entry, or None."""

    def getCurrentItemData(self):
        """The current entry's result, or None."""

    def getAllItemDatas(self) -> Iterable:
        """Yield ``(name, result)`` for every entry, in list order."""

    def getSelectedItemDatas(self) -> Iterable:
        """Yield ``(name, result)`` for every selected entry."""


__all__ = ["ResultDisplay", "ResultListPane"]
