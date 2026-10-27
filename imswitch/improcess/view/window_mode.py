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

from qtpy import QtWidgets

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

#: Ids re-enabled in window mode: the core of the ROI tools, which draw and
#: measure through :mod:`~imswitch.improcess.view.window_tools`, and
#: Crop/Substack, whose range table and ROI chooser read the ROI manager and
#: need no canvas (only its live crop outline is drawn on the napari viewer,
#: which window mode leaves out). Inside them a few controls that need napari
#: stay disabled, each with its own tooltip (the ROI manager's Freehand shape,
#: Profile's layer chooser). Adding an id here is all it takes to switch a
#: tool back on.
SUPPORTED_IN_WINDOW_MODE: frozenset[str] = frozenset(
    {"profile", "roi-manager", "roi-stats", "crop-substack"}
)


def unsupported_tools() -> tuple[NapariOnlyTool, ...]:
    """The tools that stay disabled in window mode."""
    return tuple(tool for tool in NAPARI_ONLY_TOOLS if tool.id not in SUPPORTED_IN_WINDOW_MODE)


def panel_tool(widget_kind: str, processor_id: str | None = None) -> NapariOnlyTool | None:
    """The restricted panel tool for a runtime panel, or None.

    Matched on the ``widget_kind`` *or* the ``processor_id``: a tool built on
    the generic result-processor panel has the kind ``"result-processor"``
    whatever it is, so its own id is the only thing that names it.
    """
    for tool in unsupported_tools():
        if tool.kind == "panel" and tool.id in (widget_kind, processor_id):
            return tool
    return None


def restrict_panel(widget, dock_actions=None) -> None:
    """Disable a napari-only panel and say why, wherever the pointer rests on it.

    A tooltip on the panel alone is not enough: a disabled panel's buttons and
    labels each show their own tooltip (or none), so the reason would appear
    only on the panel's bare background. It is set on every child as well, on
    the title of the dock holding the panel, and (``dock_actions``: title ->
    action, the View menu's entries) on its menu entry.
    """
    if widget is None:
        return
    widget.setEnabled(False)
    widget.setToolTip(UNSUPPORTED_TOOLTIP)
    for child in widget.findChildren(QtWidgets.QWidget):
        child.setToolTip(UNSUPPORTED_TOOLTIP)
    dock = widget.parentWidget()
    while dock is not None and not _is_dock(dock):
        dock = dock.parentWidget()
    if dock is not None:
        dock.label.setToolTip(UNSUPPORTED_TOOLTIP)
        action = (dock_actions or {}).get(dock.name())
        if action is not None:
            action.setToolTip(UNSUPPORTED_TOOLTIP)


def _is_dock(widget) -> bool:
    from pyqtgraph.dockarea import Dock

    return isinstance(widget, Dock)
