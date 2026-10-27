"""What does not work yet with separate image windows, in one place.

Window mode keeps the napari viewer object alive (napari plugins and the storm
display need it) but hides its canvas, so anything that draws on or reads
napari layers has nothing to work on. Until each of those tools is given a
window-side implementation they are disabled -- visibly, with a tooltip that
says why -- instead of being left to do nothing silently.

This module is the registry. To bring a tool back in window mode, implement it
against :class:`ImageWindow` and add its id to :data:`SUPPORTED_IN_WINDOW_MODE`;
nothing else needs to change.
"""

from __future__ import annotations

from dataclasses import dataclass

UNSUPPORTED_TOOLTIP = "Not yet supported with separate windows (Preferences → Image display)"


@dataclass(frozen=True)
class NapariOnlyTool:
    """A tool that draws on or reads the embedded napari viewer's layers."""

    id: str
    label: str
    #: ``"panel"`` (a dock widget, keyed by its runtime ``widget_kind``),
    #: ``"image-action"`` (a toolbar/menu action id) or ``"menu"``.
    kind: str
    #: The view attribute holding the panel/menu (panels and menus only).
    attribute: str = ""


NAPARI_ONLY_TOOLS: tuple[NapariOnlyTool, ...] = (
    NapariOnlyTool("profile", "Profile", "panel", "profileWidget"),
    NapariOnlyTool("roi-manager", "ROI manager", "panel", "roiManagerWidget"),
    NapariOnlyTool("roi-stats", "ROI stats", "panel", "roiStatsWidget"),
    NapariOnlyTool("segmentation", "Segmentation (preview)", "panel", "segmentationWidget"),
    NapariOnlyTool("psf-resolution", "PSF resolution", "panel", "psfResolutionWidget"),
    NapariOnlyTool("colocalization", "Colocalization", "panel", "colocalizationWidget"),
    NapariOnlyTool("multicolor", "Multicolor (previews)", "panel", "multicolorWidget"),
    NapariOnlyTool("crop-substack", "Crop/Substack (Stack subset)", "image-action"),
    NapariOnlyTool("napari-plugins", "napari plugins", "menu", "_napariPluginsMenu"),
)

#: Ids re-enabled in window mode. Empty until the ROI tools (W5) land; adding
#: an id here is all it takes to switch that tool back on.
SUPPORTED_IN_WINDOW_MODE: frozenset[str] = frozenset()


def unsupported_tools() -> tuple[NapariOnlyTool, ...]:
    """The tools that stay disabled in window mode."""
    return tuple(tool for tool in NAPARI_ONLY_TOOLS if tool.id not in SUPPORTED_IN_WINDOW_MODE)


def panel_tool(widget_kind: str) -> NapariOnlyTool | None:
    """The restricted panel tool for a runtime ``widget_kind``, or None."""
    for tool in unsupported_tools():
        if tool.kind == "panel" and tool.id == widget_kind:
            return tool
    return None


def restrict_panel(widget) -> None:
    """Disable a napari-only panel and say why."""
    if widget is None:
        return
    widget.setEnabled(False)
    widget.setToolTip(UNSUPPORTED_TOOLTIP)
