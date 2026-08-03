"""ImProcess keyboard-shortcut catalog, Fiji-parity defaults and persistence.

Reuses the shared ShortcutManager/editor from imcommon (the same machinery
imcontrol uses). Defaults follow Fiji/ImageJ where an equivalent exists:

===========================  ============  ==============================
Action                       Default       Fiji/ImageJ equivalent
===========================  ============  ==============================
Quick load data              Ctrl+O        File > Open
Virtual load data            Ctrl+Shift+O  (Open as virtual stack)
Save reconstruction          Ctrl+S        File > Save
Save all reconstructions     Ctrl+Shift+S  File > Save As
Brightness/Contrast          Ctrl+Shift+C  Image > Adjust > B&C
Channels dialog              Ctrl+Shift+Z  Image > Color > Channels Tool
Duplicate                    Ctrl+Shift+D  Image > Duplicate
Crop/Substack                Ctrl+Shift+X  Image > Crop
Graph panel                  Ctrl+H        Analyze > Histogram
Profile panel                Ctrl+K        Analyze > Plot Profile
ROI manager panel            Ctrl+T        Analyze > Tools > ROI Manager
ROI stats panel              Ctrl+M        Analyze > Measure
===========================  ============  ==============================

Everything else registers unbound (rebindable in the editor). Overrides are
persisted per-user in ``improcess_shortcuts.json`` under the ImSwitch config
root — ImProcess has no per-setup JSON like imcontrol's setupInfo.
"""

from __future__ import annotations

import json
import os

from imswitch.imcommon.model import ShortcutScope, dirtools, initLogger

SHORTCUTS_FILENAME = "improcess_shortcuts.json"

#: (actionId, display name, default key or None, view signal name)
_SIGNAL_SPECS = (
    ("file.quick-load", "Quick load data", "Ctrl+O", "sigQuickLoadData"),
    ("file.quick-load-virtual", "Virtual load data", "Ctrl+Shift+O",
     "sigQuickLoadVirtualData"),
    ("file.save-reconstruction", "Save reconstruction", "Ctrl+S",
     "sigSaveReconstruction"),
    ("file.save-reconstruction-all", "Save all reconstructions", "Ctrl+Shift+S",
     "sigSaveReconstructionAll"),
    ("file.save-coeffs", "Save coefficients", None, "sigSaveCoeffs"),
    ("file.save-coeffs-all", "Save all coefficients", None, "sigSaveCoeffsAll"),
    ("image.auto-contrast", "Auto contrast", None, "sigImageAutoContrastRequested"),
    ("image.brightness-contrast", "Brightness/Contrast", "Ctrl+Shift+C",
     "sigImageContrastDialogRequested"),
    ("image.reset-contrast", "Reset contrast", None, "sigImageResetContrastRequested"),
    ("image.channels", "Channels dialog", "Ctrl+Shift+Z",
     "sigImageChannelControlsRequested"),
    ("image.reset-view", "Reset view", None, "sigImageResetViewRequested"),
    ("image.duplicate", "Duplicate", "Ctrl+Shift+D", "sigImageDuplicateRequested"),
    ("image.crop-substack", "Crop/Substack", "Ctrl+Shift+X",
     "sigImageCropSubstackRequested"),
    ("image.max-projection", "Max projection", None, "sigImageMaxProjectionRequested"),
    ("image.split-stack", "Split stack", None, "sigImageSplitStackRequested"),
    ("image.split-channels", "Split channels", None, "sigImageSplitChannelsRequested"),
    ("image.merge-channels", "Merge channels", None, "sigImageMergeChannelsRequested"),
    ("image.stack-combine", "Stack/Combine", None, "sigImageStackCombineRequested"),
    ("image.image-calculator", "Image calculator", None, "sigImageCalculatorRequested"),
    ("image.make-composite", "Make composite", None, "sigImageMakeCompositeRequested"),
    ("image.make-rgb", "Make RGB", None, "sigImageMakeRgbRequested"),
)

#: (actionId, display name, default key or None, runtime tool id)
_PANEL_SPECS = (
    ("panel.graph", "Graph panel", "Ctrl+H", "graph"),
    ("panel.profile", "Profile panel", "Ctrl+K", "profile"),
    ("panel.roi-manager", "ROI manager panel", "Ctrl+T", "roi-manager"),
    ("panel.roi-stats", "ROI stats panel", "Ctrl+M", "roi-stats"),
    ("panel.projection", "Projection panel", None, "projection"),
    ("panel.segmentation", "Segmentation panel", None, "segmentation"),
    ("panel.metadata", "Metadata panel", None, "metadata"),
)


def improcess_shortcut_defaults() -> dict[str, str | None]:
    """Return actionId -> default key sequence for every catalogued action."""
    defaults = {aid: key for aid, _name, key, _sig in _SIGNAL_SPECS}
    defaults.update({aid: key for aid, _name, key, _tool in _PANEL_SPECS})
    defaults["panel.results-table"] = None
    return defaults


def register_improcess_shortcuts(manager, mainView) -> None:
    """Register the ImProcess action catalog on a ShortcutManager."""
    for action_id, display_name, default_key, signal_name in _SIGNAL_SPECS:
        signal = getattr(mainView, signal_name)
        manager.registerAction(
            actionId=action_id,
            displayName=display_name,
            callback=lambda sig=signal: sig.emit(),
            defaultKeySequence=default_key,
            scope=ShortcutScope.Window,
            owner=mainView,
        )
    for action_id, display_name, default_key, tool_id in _PANEL_SPECS:
        manager.registerAction(
            actionId=action_id,
            displayName=display_name,
            callback=lambda tid=tool_id, view=mainView: (
                view.sigLoadProcessorRequested.emit(tid)
            ),
            defaultKeySequence=default_key,
            scope=ShortcutScope.Window,
            owner=mainView,
        )
    manager.registerAction(
        actionId="panel.results-table",
        displayName="Results table panel",
        callback=lambda view=mainView: view.raiseDockByTitle("Results"),
        defaultKeySequence=None,
        scope=ShortcutScope.Window,
        owner=mainView,
    )


def _shortcuts_file_path() -> str:
    return os.path.join(dirtools.UserFileDirs.Root, SHORTCUTS_FILENAME)


def load_shortcut_overrides() -> dict:
    """Load persisted per-user shortcut overrides; empty dict when absent."""
    path = _shortcuts_file_path()
    try:
        with open(path, encoding="utf-8") as file:
            overrides = json.load(file)
        return overrides if isinstance(overrides, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        initLogger("ImProcessShortcuts").warning(
            f"Could not read shortcut overrides from {path}; using defaults",
        )
        return {}


def save_shortcut_overrides(overrides: dict) -> None:
    """Persist shortcut overrides (only diffs from defaults) per-user."""
    path = _shortcuts_file_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(overrides, file, indent=2, sort_keys=True)


__all__ = [
    "SHORTCUTS_FILENAME",
    "improcess_shortcut_defaults",
    "load_shortcut_overrides",
    "register_improcess_shortcuts",
    "save_shortcut_overrides",
]
