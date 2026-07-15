"""Runtime analysis tool descriptors for ImProcess."""

from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeAnalysisToolSpec:
    """UI/runtime descriptor for a loadable analysis tool."""

    id: str
    title: str
    attribute: str
    widget_kind: str
    processor_id: str | None = None
    category: str = "Other"


@dataclass(frozen=True)
class RuntimeAnalysisPanelShortcut:
    """Always-visible shortcut for opening an interactive analysis panel."""

    id: str
    title: str
    tooltip: str


_PROCESSOR_WIDGET_SPECS = {
    "drift-correct": (
        "Drift correction",
        "driftCorrectProcessorWidget",
        "result-processor",
    ),
    "denoise": ("Denoise", "denoiseProcessorWidget", "result-processor"),
    "projection": ("Projection", "projectionWidget", "result-processor"),
    "segmentation": ("Segmentation", "segmentationWidget", "segmentation"),
    "psf-resolution": ("PSF resolution", "psfResolutionWidget", "psf-resolution"),
    "colocalization": ("Colocalization", "colocalizationWidget", "colocalization"),
    "frc": ("FRC", "frcWidget", "result-processor"),
    "multicolor-registration": ("Multicolor", "multicolorWidget", "multicolor"),
    "multicolor-apply": ("Multicolor", "multicolorWidget", "multicolor"),
}

_NON_PROCESSOR_TOOL_SPECS = {
    "graph": RuntimeAnalysisToolSpec(
        id="graph",
        title="Graph",
        attribute="graphWidget",
        widget_kind="graph",
        processor_id=None,
        category="Exploration",
    ),
    "profile": RuntimeAnalysisToolSpec(
        id="profile",
        title="Profile",
        attribute="profileWidget",
        widget_kind="profile",
        processor_id=None,
        category="Exploration",
    ),
    "roi-manager": RuntimeAnalysisToolSpec(
        id="roi-manager",
        title="ROI manager",
        attribute="roiManagerWidget",
        widget_kind="roi-manager",
        processor_id=None,
        category="ROI",
    ),
    "roi-stats": RuntimeAnalysisToolSpec(
        id="roi-stats",
        title="ROI stats",
        attribute="roiStatsWidget",
        widget_kind="roi-stats",
        processor_id=None,
        category="ROI",
    ),
}

_PANEL_SHORTCUTS = (
    RuntimeAnalysisPanelShortcut(
        id="graph",
        title="Graph",
        tooltip="Open the graph panel",
    ),
    RuntimeAnalysisPanelShortcut(
        id="profile",
        title="Profile",
        tooltip="Open the profile panel",
    ),
    RuntimeAnalysisPanelShortcut(
        id="roi-manager",
        title="ROI manager",
        tooltip="Open the ROI manager panel",
    ),
    RuntimeAnalysisPanelShortcut(
        id="roi-stats",
        title="ROI stats",
        tooltip="Open the ROI statistics panel",
    ),
    RuntimeAnalysisPanelShortcut(
        id="projection",
        title="Projection",
        tooltip="Open the projection panel",
    ),
    RuntimeAnalysisPanelShortcut(
        id="segmentation",
        title="Segmentation",
        tooltip="Open the segmentation panel",
    ),
)


def runtime_analysis_tool_specs() -> dict[str, RuntimeAnalysisToolSpec]:
    """Return all built-in runtime-loadable analysis tool descriptors."""
    from imswitch.improcess.processors import available_processor_specs

    specs: dict[str, RuntimeAnalysisToolSpec] = {}
    for processor_id, processor_name, processor_category in available_processor_specs():
        title, attribute, widget_kind = _PROCESSOR_WIDGET_SPECS.get(
            processor_id,
            (
                str(processor_name or processor_id),
                _generic_processor_attribute(processor_id),
                "result-processor",
            ),
        )
        specs[processor_id] = RuntimeAnalysisToolSpec(
            id=processor_id,
            title=title,
            attribute=attribute,
            widget_kind=widget_kind,
            processor_id=processor_id,
            category=str(processor_category or "Other"),
        )
    specs.update(_NON_PROCESSOR_TOOL_SPECS)
    return specs


def runtime_analysis_tool_choices() -> list[tuple[str, str]]:
    """Return ``(tool_id, title)`` choices for the runtime-loader combo.

    Tools that share one widget (e.g. multicolor registration + apply both open
    the Multicolor panel) collapse to a single entry; the first (sorted) id wins.
    """
    choices: list[tuple[str, str]] = []
    seen_widgets: set[str] = set()
    for spec in sorted(
        runtime_analysis_tool_specs().values(),
        key=lambda item: (item.category, item.title, item.id),
    ):
        if spec.attribute in seen_widgets:
            continue
        seen_widgets.add(spec.attribute)
        choices.append((spec.id, spec.title))
    return choices


def runtime_analysis_panel_shortcuts() -> list[RuntimeAnalysisPanelShortcut]:
    """Return the Fiji-like panel shortcuts available in this build."""
    specs = runtime_analysis_tool_specs()
    return [shortcut for shortcut in _PANEL_SHORTCUTS if shortcut.id in specs]


def runtime_result_processor_ids() -> list[str]:
    """Return runtime tool ids backed by the generic ResultProcessorWidget."""
    return [
        spec.id
        for spec in sorted(
            runtime_analysis_tool_specs().values(),
            key=lambda item: (item.category, item.title, item.id),
        )
        if spec.widget_kind == "result-processor" and spec.processor_id is not None
    ]


def _generic_processor_attribute(processor_id: str) -> str:
    normalized = "".join(
        char if char.isalnum() else "_"
        for char in processor_id
    )
    return f"runtimeProcessorWidget_{normalized}"


__all__ = [
    "RuntimeAnalysisPanelShortcut",
    "RuntimeAnalysisToolSpec",
    "runtime_analysis_panel_shortcuts",
    "runtime_analysis_tool_choices",
    "runtime_analysis_tool_specs",
    "runtime_result_processor_ids",
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
