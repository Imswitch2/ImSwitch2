"""Helpers for reading ImProcess-specific setup configuration."""

from typing import Any


def load_processing_config(logger: Any = None) -> dict[str, Any]:
    """Return the optional ``processing`` block from the selected setup JSON."""
    try:
        from imswitch.imcontrol.model import configfiletools
        from imswitch.imcontrol.model.SetupInfo import SetupInfo

        options, _ = configfiletools.loadOptions()
        setup_info = configfiletools.loadSetupInfo(options, SetupInfo)
        catch_all = getattr(setup_info, "_catchAll", None) or {}
        processing_config = catch_all.get("processing", {}) or {}
        if isinstance(processing_config, dict):
            return processing_config
    except Exception as exc:
        if logger is not None:
            logger.info(
                f"No setup configuration available ({exc!r}); using standalone defaults"
            )
    return {}


def is_graph_panel_enabled(processing_config: dict[str, Any]) -> bool:
    """Return whether the optional ImProcess graph panel should be shown."""
    return bool(processing_config.get("graphPanel", True))


def plugin_ids_from_config(
    processing_config: dict[str, Any],
) -> tuple[list[str], list[str], bool]:
    """Return reconstructor IDs, processor IDs and whether plugin config was explicit."""
    has_plugin_config = (
        "reconstructors" in processing_config or "processors" in processing_config
    )
    if has_plugin_config:
        return (
            processing_config.get("reconstructors", ["monalisa"]),
            processing_config.get("processors", ["drift-correct"]),
            True,
        )
    return ["view-only"], ["drift-correct"], False
