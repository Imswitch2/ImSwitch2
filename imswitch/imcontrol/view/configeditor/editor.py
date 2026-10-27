#!/usr/bin/env python3
"""
ImSwitch Config Studio
Visual editor for ImSwitch JSON configuration files.

Two ways in:

* standalone --- ``python utility_scripts/imswitch_config_editor.py
  [/path/to/config/dir]``, which owns its own QApplication and dark theme;
* embedded --- ``Hardware > Edit hardware configuration...`` in imcontrol, which
  opens :class:`MainWindow` as a window of the running application and leaves
  the application-wide palette alone.

The module therefore never touches ``QApplication`` state outside :func:`main`.
"""

import ast
import colorsys
import copy
import json
import math
import os
import re
import shutil
import sys
from dataclasses import dataclass, field as dc_field
from pathlib import Path
import glob
from PyQt5.QtCore import Qt, pyqtSignal, QRect, QSize, QPoint
from PyQt5.QtGui import QFont, QPalette, QColor
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialogButtonBox,
    QFileDialog, QFormLayout, QFrame, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton, QScrollArea,
    QSizePolicy, QSplitter, QStatusBar, QTabWidget, QToolBar, QHeaderView,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QAction,
    QDialog, QTextEdit, QLayout, QLayoutItem, QStyle, QPlainTextEdit, QStackedWidget,
)

# Try to import the manager catalog (Phase 1: registry-backed discovery)
# and shared model helpers (Phase 5: consolidation). Falls back gracefully
# if the model package is unavailable
_MANAGER_CATALOG = None
_coercion_module = None
_io_module = None
_schema_module = None
_resources_module = None
_roles_module = None
_disabled_devices_module = None
_comparison_module = None
_setup_kind_metadata = ()
try:
    from imswitch.imcontrol.model.configeditor.catalog import build_catalog
    _MANAGER_CATALOG = build_catalog(include_generated_schemas=True)
    from imswitch.imcontrol.model.configeditor import coercion as _coercion_module
    from imswitch.imcontrol.model.configeditor import io as _io_module
    from imswitch.imcontrol.model.configeditor import schemas as _schema_module
    from imswitch.imcontrol.model.configeditor import resources as _resources_module
    from imswitch.imcontrol.model.configeditor import roles as _roles_module
    from imswitch.imcontrol.model.configeditor import disabled_devices as _disabled_devices_module
    from imswitch.imcontrol.model.configeditor import comparison as _comparison_module
    from imswitch.imcontrol.model.plugins.setup_metadata import setup_kinds
    _setup_kind_metadata = setup_kinds()
except ImportError as e:
    # Model package unavailable; fallback implementations will be used
    pass
except Exception as e:
    # Log but don't crash if imports fail
    import logging
    logging.getLogger(__name__).warning(f"Failed to import model helpers: {e}")
    pass

# =============================================================================
# Authoritative category registry
# =============================================================================
# Category labels/colours belong to the UI.  Category names and legacy manager
# directories are derived from plugins.setup_metadata whenever it is available.
_CATEGORY_LABEL_OVERRIDES = {
    "rs232devices": "RS232 Devices",
    "slms": "SLMs",
}
_FALLBACK_CATEGORY_REGISTRY = {
    "detectors": (None, "detectors"),
    "lasers": (None, "lasers"),
    "positioners": (None, "positioners"),
    "rotators": (None, "rotators"),
    "rs232devices": ("RS232 Devices", "rs232"),
    "slms": ("SLMs", "slms"),
    "flipMirrors": (None, "flipMirrors"),
    "pulsegen": (None, "pulsegen"),
    "stands": (None, "stands"),
}
_CATEGORY_REGISTRY = (
    {
        metadata.editor_category: (
            _CATEGORY_LABEL_OVERRIDES.get(metadata.editor_category),
            metadata.legacy_manager_directory,
        )
        for metadata in _setup_kind_metadata
    }
    if _setup_kind_metadata
    else _FALLBACK_CATEGORY_REGISTRY
)


def _imcontrol_dir() -> Path:
    """Return the ``imswitch/imcontrol`` directory this editor is part of.

    Two callers still read ImSwitch's own source tree: the legacy manager scan
    used when the catalog is unavailable, and the ``availableWidgets``
    docstring parser. Both used to walk up from a script in
    ``utility_scripts/``; anchoring on this module keeps them correct now that
    it lives inside the package.
    """
    return Path(__file__).resolve().parents[2]


def _user_template_dir() -> Path:
    """Return the directory holding the operator's saved device templates.

    Deliberately under the user config root rather than next to this module:
    an installed ImSwitch lives in ``site-packages``, which is shared between
    users and may be read-only, so templates saved there would be lost on the
    next upgrade -- if they could be written at all.
    """
    return _imswitch_user_root() / "config_editor_templates"


def _imswitch_user_root() -> Path:
    """Return ImSwitch's user config root using the app's shared convention."""
    try:
        from imswitch.imcommon.model import dirtools

        return Path(dirtools.UserFileDirs.Root)
    except Exception:
        if os.name == "nt":
            return Path.home() / "Documents" / "ImSwitchConfig"
        return Path.home() / "ImSwitchConfig"


def _default_setup_dir() -> Path:
    return _imswitch_user_root() / "imcontrol_setups"


def _default_config_dir() -> Path:
    return _imswitch_user_root() / "config"


# =============================================================================
# Schema loading – reads builtin_templates/{category}/*.json at startup
# =============================================================================
def _load_schemas() -> dict:
    """Load all built-in manager schemas from builtin_templates/ subdirectories.

    Singleton-section schemas (those with ``"section": true``) live in
    ``builtin_templates/sections/`` and are handled by ``_load_section_schemas``;
    they are skipped here so they don't appear in the per-device manager picker.

    Blank schemas (``"blank": true``) are also skipped here and loaded separately
    by ``_load_blank_schemas()``.
    """
    schemas: dict = {}
    base = Path(__file__).resolve().parent / "builtin_templates"
    if base.is_dir():
        for cat_dir in sorted(base.iterdir()):
            if not cat_dir.is_dir() or cat_dir.name in {"sections", "categories"}:
                continue
            for f in sorted(cat_dir.glob("*.json")):
                try:
                    with open(f, encoding="utf-8") as fh:
                        data = json.load(fh)
                        # Skip blank templates
                        if data.get("blank"):
                            continue
                        schemas[f.stem] = data
                except Exception:
                    pass
    return schemas


def _load_blank_schemas() -> dict:
    """Load per-category blank schemas from builtin_templates/<category>/_blank.json.

    Returns a dict keyed by category name.
    """
    blanks: dict = {}
    base = Path(__file__).resolve().parent / "builtin_templates"
    if base.is_dir():
        for cat_dir in sorted(base.iterdir()):
            if not cat_dir.is_dir() or cat_dir.name in {"sections", "categories"}:
                continue
            blank_file = cat_dir / "_blank.json"
            if blank_file.exists():
                try:
                    with open(blank_file, encoding="utf-8") as fh:
                        blanks[cat_dir.name] = json.load(fh)
                except Exception:
                    pass
    return blanks


def _discover_managers() -> dict:
    """Auto-discover manager class names from the imswitch source tree.

    Returns a dict: category → [manager_name, ...]
    Degrades gracefully if the managers tree is not found.
    
    Phase 1: Uses the manager catalog when available; falls back to legacy scan.
    """
    # Use catalog if available
    if _MANAGER_CATALOG is not None:
        by_cat = _MANAGER_CATALOG.by_category()
        result: dict = {}
        for cat, infos in by_cat.items():
            result[cat] = [info.manager_name for info in infos]
            result[cat].sort()
        return result
    
    # Legacy filesystem scan (fallback when catalog unavailable)
    discovered: dict = {}
    managers_root = _imcontrol_dir() / "model" / "managers"

    if not managers_root.is_dir():
        return discovered

    # Base manager class names to skip (one per category)
    base_managers = {
        "DetectorManager", "LaserManager", "PositionerManager", "RotatorManager",
        "FlipMirrorManager", "PulseGeneratorManager", "StandManager",
        "RS232Manager", "SLMManager"
    }

    for cat, (_, dir_name) in _CATEGORY_REGISTRY.items():
        cat_dir = managers_root / dir_name
        if not cat_dir.is_dir():
            continue
        for f in cat_dir.glob("*Manager.py"):
            stem = f.stem
            if stem in base_managers or stem.startswith("_"):
                continue
            discovered.setdefault(cat, []).append(stem)

    # Sort each category's list
    for lst in discovered.values():
        lst.sort()

    return discovered


def _load_category_schemas() -> dict:
    """Load explicit per-device-category settings schemas.

    Category settings are stored separately from device dictionaries under
    ``deviceCategorySettings``. Explicit schemas define actual fields; after
    loading, the registry is completed with empty schemas so every device
    category exposes the same Settings affordance.
    """
    schemas: dict = {}
    base = Path(__file__).resolve().parent / "builtin_templates" / "categories"
    if base.is_dir():
        for f in sorted(base.glob("*.json")):
            try:
                with open(f, encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception:
                continue
            key = data.get("category") or f.stem
            schemas[key] = data
    return schemas


def _load_section_schemas() -> dict:
    """Load singleton-section schemas from builtin_templates/sections/.

    Returns a dict keyed by the top-level JSON key (e.g. ``"focusLock"``,
    ``"scan"``). Each value has the same shape as a device schema but with a
    flat ``fields`` list (no ``top``/``props``/``nested``), plus ``section``,
    ``group`` (``"system"`` or ``"extras"``), and optional ``legacy`` flags.
    """
    sections: dict = {}
    base = Path(__file__).resolve().parent / "builtin_templates" / "sections"
    if base.is_dir():
        for f in sorted(base.glob("*.json")):
            try:
                with open(f, encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception:
                continue
            key = data.get("key") or f.stem
            _resolve_section_dynamic_options(data)
            sections[key] = data
    return sections


#: Plugin lists used when ImProcess cannot be imported — the Config Studio is
#: a standalone PyQt5 script and may run without the rest of ImSwitch
#: installed. These MUST mirror the real registries; a stale entry here shows
#: the operator a plugin list that silently omits whatever was added since.
#: ``test_config_editor_plugin_fallback.py`` fails when they drift apart.
IMPROCESS_RECONSTRUCTOR_FALLBACK = [
    "beadrec",
    "monalisa",
    "monalisa-legacy",
    "polarisation-map",
    "smlm-localizer",
    "snouty",
    "snouty-projections",
    "tiling-mosaic",
    "time-lapse",
    "view-only",
    "widefield-starss",
]

IMPROCESS_PROCESSOR_FALLBACK = [
    "channel-merge",
    "channel-split",
    "colocalization",
    "convert-type",
    "deconvolve-rl",
    "denoise",
    "drift-correct",
    "filter",
    "frc",
    "image-calculator",
    "label-morphology",
    "make-composite",
    "make-rgb",
    "math",
    "multicolor-apply",
    "multicolor-registration",
    "projection",
    "psf-resolution",
    "python",
    "resize",
    "segmentation",
    "smlm-drift",
    "smlm-drift-comet",
    "smlm-filter",
    "smlm-group",
    "smlm-render",
    "stack-combine",
    "stack-split",
    "stack-subset",
    "subtract-background",
    "table-to-localizations",
    "transform",
]

#: Why the live registries could not be read, or None when they were.
PLUGIN_DISCOVERY_ERROR = None


def _resolve_section_dynamic_options(schema: dict) -> None:
    """Replace supported option placeholders with runtime-discovered values."""
    global PLUGIN_DISCOVERY_ERROR

    resolved = {
        "__improcess_reconstructors__": list(IMPROCESS_RECONSTRUCTOR_FALLBACK),
        "__improcess_processors__": list(IMPROCESS_PROCESSOR_FALLBACK),
    }
    try:
        from imswitch.improcess.reconstructors import available_reconstructor_ids
        from imswitch.improcess.processors import available_processor_ids

        resolved["__improcess_reconstructors__"] = available_reconstructor_ids()
        resolved["__improcess_processors__"] = available_processor_ids()
        PLUGIN_DISCOVERY_ERROR = None
    except Exception as exc:
        # Say so rather than swallowing it: the built-in lists are a snapshot,
        # and a user who cannot find a plugin they know exists deserves to
        # know they are looking at the offline copy. One import failure in any
        # plugin hides every plugin, so this is worth reporting.
        if PLUGIN_DISCOVERY_ERROR is None:
            print(
                f'Config Studio: could not read the ImProcess plugin '
                f'registries ({exc}); showing the built-in list, which may be '
                f'missing recently added plugins.',
                file=sys.stderr,
            )
        PLUGIN_DISCOVERY_ERROR = str(exc)

    # Display names (and, for processors, categories) from the same live
    # registries. Without them the options stay bare ids.
    labels = {"__improcess_reconstructors__": {}, "__improcess_processors__": {}}
    groups = {"__improcess_processors__": {}}
    if PLUGIN_DISCOVERY_ERROR is None:
        try:
            from imswitch.improcess.reconstructors import _all_reconstructor_classes
            from imswitch.improcess.processors import available_processor_specs

            for plugin_id, plugin_cls in _all_reconstructor_classes().items():
                name = str(getattr(plugin_cls, "name", "") or "")
                if name:
                    labels["__improcess_reconstructors__"][plugin_id] = name
            for plugin_id, name, category in available_processor_specs(origin="all"):
                if name:
                    labels["__improcess_processors__"][plugin_id] = str(name)
                if category:
                    groups["__improcess_processors__"][plugin_id] = str(category)
        except Exception:
            labels = {"__improcess_reconstructors__": {}, "__improcess_processors__": {}}
            groups = {"__improcess_processors__": {}}

    for field in schema.get("fields", []):
        opts = field.get("opts") or []
        if len(opts) == 1 and opts[0] in resolved:
            placeholder = opts[0]
            field["opts"] = resolved[placeholder]
            if labels.get(placeholder):
                field["option_labels"] = dict(labels[placeholder])
            if groups.get(placeholder):
                field["option_groups"] = dict(groups[placeholder])


def _hsv_palette(n: int, saturation: float = 0.60, value: float = 0.78) -> list:
    """Return n evenly-spaced HSV hex colours, offset slightly from pure red."""
    out = []
    for i in range(max(n, 1)):
        h = (i / max(n, 1) + 0.05) % 1.0
        r, g, b = colorsys.hsv_to_rgb(h, saturation, value)
        out.append("#{:02X}{:02X}{:02X}".format(int(r * 255), int(g * 255), int(b * 255)))
    return out


def _build_cat_palette() -> tuple:
    """Build (CAT_COLOR, CAT_LABEL) from the authoritative category registry."""
    cats = sorted(_CATEGORY_REGISTRY.keys())
    colours = _hsv_palette(len(cats))
    cat_color = {cat: colours[i] for i, cat in enumerate(cats)}
    cat_label: dict = {}
    for cat in cats:
        override, _ = _CATEGORY_REGISTRY[cat]
        if override:
            cat_label[cat] = override
        else:
            words = cat.replace("_", " ").split()
            cat_label[cat] = " ".join(w.capitalize() for w in words)
    return cat_color, cat_label


# =============================================================================
# Palette & schema registry (derived from JSON files at import time)
# Field helper: (key, label, type, default, required, group, tooltip, options)
# Types: text | int | float | bool | select | path
# =============================================================================
def _f(key, label, tp="text", default="", req=False,
       grp="Basic", tip="", opts=None):
    return dict(key=key, label=label, type=tp, default=default,
                req=req, grp=grp, tip=tip, opts=opts or [])


SCHEMAS = _load_schemas()
BLANK_SCHEMAS = _load_blank_schemas()
SECTION_SCHEMAS = _load_section_schemas()
CATEGORY_SCHEMAS = _load_category_schemas()
DISCOVERED_MANAGERS = _discover_managers()

# Build palette and labels from authoritative registry
CAT_COLOR, CAT_LABEL = _build_cat_palette()

# All categories from the registry, always present
DEVICE_CATS = sorted(_CATEGORY_REGISTRY.keys())
# "others" is a permanent catch-all — always last
if "others" not in DEVICE_CATS:
    DEVICE_CATS = DEVICE_CATS + ["others"]
CAT_COLOR.setdefault("others", "#888888")
CAT_LABEL.setdefault("others", "Others")

DISABLED_DEVICES_KEY = (
    _disabled_devices_module.DISABLED_DEVICES_KEY
    if _disabled_devices_module is not None
    else "disabledDevices"
)
DISABLEABLE_DEVICE_CATS = set(
    _disabled_devices_module.DISABLEABLE_CATEGORIES
    if _disabled_devices_module is not None
    else (
        "detectors", "lasers", "positioners", "rotators",
        "rs232devices", "slms", "flipMirrors",
    )
)

# Category settings are a first-class affordance for every device category,
# even before that category has any settings of its own. Explicit JSON schemas
# (currently lasers) override these empty placeholders. This keeps future
# category settings schema-driven without requiring one empty JSON file per
# category.
for _cat in DEVICE_CATS:
    CATEGORY_SCHEMAS.setdefault(_cat, {
        "category": _cat,
        "display": f"{CAT_LABEL[_cat]} settings",
        "summary": "No category-wide settings are defined yet.",
        "fields": [],
    })


# Build category → [manager names] index from templates
CAT_MANAGERS: dict = {}
for _m, _s in SCHEMAS.items():
    CAT_MANAGERS.setdefault(_s["category"], []).append(_m)

# Merge in discovered managers (union: templated + non-templated)
for _cat, _mgrs in DISCOVERED_MANAGERS.items():
    existing = set(CAT_MANAGERS.get(_cat, []))
    for _mgr in _mgrs:
        if _mgr not in existing:
            CAT_MANAGERS.setdefault(_cat, []).append(_mgr)

# Sort each category's list
for _lst in CAT_MANAGERS.values():
    _lst.sort()


# =============================================================================
# Helpers for reading / writing device data
# =============================================================================
def _json_to_display(value, field_type: str) -> str:
    """Convert a JSON value to a display string for a text/select widget."""
    if _coercion_module is not None:
        return _coercion_module.json_to_display(value, field_type)
    # Fallback implementation
    if value is None:
        return "null"
    if field_type in ("int", "float"):
        return str(value)
    return str(value)


def _display_to_json(text: str, field_type: str):
    """Convert a display string back to the correct Python type."""
    if _coercion_module is not None:
        return _coercion_module.display_to_json(text, field_type)
    # Fallback implementation
    text = text.strip()
    if text.lower() == "null":
        return None
    if field_type == "int":
        try:
            return int(text)
        except ValueError:
            return text
    if field_type == "float":
        try:
            return float(text)
        except ValueError:
            return text
    return text


def _get_category_for_manager(manager_name: str) -> str | None:
    """Find which category a manager belongs to (template or discovered).
    
    Phase 1: Uses the manager catalog when available; falls back to legacy lookup.
    """
    # Use catalog if available
    if _MANAGER_CATALOG is not None:
        cat = _MANAGER_CATALOG.category_for(manager_name)
        if cat is not None:
            return cat
        # Fall through: template-only managers (a JSON template with no
        # matching registry entry or scanned source file) are not in the
        # catalog, so resolve their category from the template below.

    # Legacy lookup (also covers template-only managers)
    # First check templates
    schema = SCHEMAS.get(manager_name)
    if schema:
        return schema.get("category")
    # Then check discovered managers
    for cat, mgrs in DISCOVERED_MANAGERS.items():
        if manager_name in mgrs:
            return cat
    return None


def _manager_display_name(manager_name: str) -> str:
    """Return a UI label for templated and discovered managers.
    
    Phase 1: Prefers template display (so existing rich templates win), then
    bare manager names for non-templated manager IDs. Catalog display names are
    used only when resolving a legacy alias.
    """
    # First check template (existing templates win)
    schema = SCHEMAS.get(manager_name)
    if schema:
        display = schema.get("display")
        if display:
            return display
    
    # Use catalog display names only for aliases. Non-templated manager IDs
    # intentionally fall back to the bare name because they have no editor
    # schema whose display metadata can drive the UI.
    if _MANAGER_CATALOG is not None:
        manager_info = _MANAGER_CATALOG.get(manager_name)
        if manager_info is not None and manager_info.manager_name != manager_name:
            return manager_info.display_name
    
    # Fall back to bare name
    return manager_name


def _all_known_managers() -> list[str]:
    """Return all templated and discovered manager names.
    
    Phase 1: Uses the manager catalog when available; falls back to legacy union.
    """
    # Use catalog if available, unioned with template-only managers so a JSON
    # template with no matching registry entry or scanned source file (e.g.
    # PiezoconceptZManager2, RS232Manager) is still offered in the picker.
    if _MANAGER_CATALOG is not None:
        managers = set(_MANAGER_CATALOG.all_manager_names())
        managers.update(SCHEMAS)
        return sorted(managers)

    # Legacy union (fallback when catalog unavailable)
    managers = set(SCHEMAS)
    for mgrs in CAT_MANAGERS.values():
        managers.update(mgrs)
    return sorted(managers)


def _device_section(data: dict, category: str, *, enabled: bool, create: bool = False) -> dict:
    """Return the active/disabled device mapping used by the editor."""
    if _disabled_devices_module is not None:
        return _disabled_devices_module.device_section(
            data, category, enabled=enabled, create=create
        )
    if enabled:
        section = data.get(category)
        if isinstance(section, dict):
            return section
        if create:
            data[category] = {}
            return data[category]
        return {}
    root = data.get(DISABLED_DEVICES_KEY)
    if not isinstance(root, dict):
        if not create:
            return {}
        root = {}
        data[DISABLED_DEVICES_KEY] = root
    section = root.get(category)
    if isinstance(section, dict):
        return section
    if create:
        root[category] = {}
        return root[category]
    return {}


def _locate_device(data: dict, category: str, name: str):
    if _disabled_devices_module is not None:
        return _disabled_devices_module.locate_device(data, category, name)
    active = _device_section(data, category, enabled=True)
    if name in active:
        return active[name], True
    disabled = _device_section(data, category, enabled=False)
    if name in disabled:
        return disabled[name], False
    return None, None


def _set_device_enabled(data: dict, category: str, name: str, enabled: bool) -> bool:
    if _disabled_devices_module is not None:
        return _disabled_devices_module.set_device_enabled(data, category, name, enabled)
    if category not in DISABLEABLE_DEVICE_CATS:
        raise ValueError(f"category {category!r} does not support temporary disabling")
    source = _device_section(data, category, enabled=not enabled)
    if name not in source:
        return False
    destination = _device_section(data, category, enabled=enabled, create=True)
    if name in destination:
        raise ValueError(f"a device named {name!r} already exists in the destination section")
    destination[name] = source.pop(name)
    root = data.get(DISABLED_DEVICES_KEY)
    if isinstance(root, dict):
        for key in list(root):
            if isinstance(root.get(key), dict) and not root[key]:
                del root[key]
        if not root:
            data.pop(DISABLED_DEVICES_KEY, None)
    return True


def _remove_device(data: dict, category: str, name: str):
    if _disabled_devices_module is not None:
        return _disabled_devices_module.remove_device(data, category, name)
    device, enabled = _locate_device(data, category, name)
    if enabled is None:
        return None
    return _device_section(data, category, enabled=enabled).pop(name, None)


def _compare_configs(left: dict, right: dict) -> dict[tuple[str, str], str]:
    """Return device-level comparison states for the card split view."""
    if _comparison_module is not None:
        return _comparison_module.compare_configs(left, right, DEVICE_CATS)

    # Fallback kept for standalone/editor-only deployments where the shared
    # model helpers are unavailable.  Enabled/disabled state is intentionally
    # part of equality, matching the normal implementation.
    states: dict[tuple[str, str], str] = {}
    for cat in DEVICE_CATS:
        names = set(_device_section(left, cat, enabled=True))
        names.update(_device_section(left, cat, enabled=False))
        names.update(_device_section(right, cat, enabled=True))
        names.update(_device_section(right, cat, enabled=False))
        for name in sorted(names, key=str.casefold):
            left_dev, left_enabled = _locate_device(left, cat, name)
            right_dev, right_enabled = _locate_device(right, cat, name)
            if left_dev is None:
                states[(cat, name)] = "right_only"
            elif right_dev is None:
                states[(cat, name)] = "left_only"
            elif left_enabled == right_enabled and left_dev == right_dev:
                states[(cat, name)] = "same"
            else:
                states[(cat, name)] = "different"
    return states


def _comparison_status_for_side(state: str, side: str) -> str:
    if _comparison_module is not None:
        return _comparison_module.status_for_side(state, side)
    if state in {"same", "different"}:
        return state
    if state == "left_only":
        return "only_here" if side == "left" else "missing_here"
    if state == "right_only":
        return "only_here" if side == "right" else "missing_here"
    return state


def _known_device_references(data: dict, category: str, name: str) -> list[str]:
    """Return well-known config paths that directly reference a device name.

    This is deliberately conservative: it only warns for references ImSwitch
    itself treats as device-name links, rather than flagging every string that
    happens to equal the device name.
    """
    paths_by_category = {
        "detectors": (
            ("focusLock", "camera"),
            ("autofocus", "camera"),
            ("tiling", "camera"),
        ),
        "positioners": (
            ("focusLock", "positioner"),
            ("autofocus", "positioner"),
            ("tiling", "xyPositioner"),
            ("tiling", "zPositioner"),
        ),
        "rs232devices": (
            ("triggerScope", "rs232device"),
            ("microscopeStand", "rs232device"),
        ),
    }
    refs: list[str] = []
    for path in paths_by_category.get(category, ()):
        value = data
        for key in path:
            if not isinstance(value, dict) or key not in value:
                value = None
                break
            value = value[key]
        if value == name:
            refs.append(".".join(path))

    # Managers commonly reference a shared serial connection from their
    # managerProperties. Cover those without treating unrelated strings as
    # references.
    if category == "rs232devices":
        for dev_cat in DEVICE_CATS:
            for dev_name, device in (_device_section(data, dev_cat, enabled=True) or {}).items():
                if not isinstance(device, dict):
                    continue
                props = device.get("managerProperties")
                if isinstance(props, dict) and props.get("rs232device") == name:
                    refs.append(f"{dev_cat}.{dev_name}.managerProperties.rs232device")
    return refs


def _schema_for_manager(manager_name: str) -> dict:
    """Return the editor form schema for a manager, including plugin fields.

    Built-in templates provide the curated layout. The top-level keys of the
    device entry come from the kind's ``SetupInfo`` dataclass
    (``schemas/kinds/``), the ``managerProperties`` from the manager's own
    schema -- generated from its source, or shipped by its plugin.
    """
    template = SCHEMAS.get(manager_name)
    if template is None:
        category = _get_category_for_manager(manager_name)
        if category:
            template = BLANK_SCHEMAS.get(category)

    json_schema = _plugin_schema_for_manager(manager_name)

    if _schema_module is not None:
        return _schema_module.materialize_device_schema(
            template=template, json_schema=json_schema,
            kind_schema=_kind_schema_for_manager(manager_name),
        )
    return copy.deepcopy(template) if template else {}


def _kind_schema_for_category(category: str | None) -> dict | None:
    """The generated schema for the top-level keys of a device in ``category``."""
    if not category or _resources_module is None:
        return None
    kind = next((meta.kind for meta in _setup_kind_metadata if meta.editor_category == category), None)
    return _resources_module.kind_schema_for(kind) if kind else None


def _kind_schema_for_manager(manager_name: str) -> dict | None:
    return _kind_schema_for_category(_get_category_for_manager(manager_name))


def _with_role_fields(schema: dict, category: str, name: str, device: dict, setup: dict | None) -> dict:
    """Optional fields for the properties a consumer reads from this device (``schemas/roles/``).

    A role never requires or seeds a property; it only gives the key a typed
    field in its own group, on the devices its predicate selects.
    """
    if _roles_module is None or _resources_module is None:
        return schema
    known = {f["key"] for f in schema.get("props", [])} | set(schema.get("nested", {}))
    for role in _roles_module.applicable(_resources_module.roles(), section=category, name=name,
                                         device=device, setup=setup or {}):
        for key, prop in (role.get("properties") or {}).items():
            if key in known or not isinstance(prop, dict):
                continue
            schema.setdefault("props", []).append(
                _schema_module.role_field(key, prop, group=role.get("title", role["role"])))
            known.add(key)
    return schema


def _plugin_schema_for_manager(manager_name: str) -> dict | None:
    """Return a manager's resolved plugin JSON Schema, if it has one."""
    if _MANAGER_CATALOG is None:
        return None
    manager_info = _MANAGER_CATALOG.get(manager_name)
    return manager_info.properties_schema if manager_info is not None else None


def _build_default_device(manager_name: str) -> dict:
    """Return a new device dict pre-filled with schema defaults.

    If the manager has a specific template, use it. Otherwise, fall back to
    the category's blank schema. If no category is found, return minimal dict.
    
    Phase 2: Delegates to defaults.build_default_device when available,
    passing both template and schema from the catalog.
    """
    template = SCHEMAS.get(manager_name)
    if template is None:
        category = _get_category_for_manager(manager_name)
        if category:
            template = BLANK_SCHEMAS.get(category)
    schema = _schema_for_manager(manager_name)

    # Try to use Phase 2 defaults module (graceful fallback if unavailable)
    try:
        from imswitch.imcontrol.model.configeditor.defaults import build_default_device as build_default
        
        return build_default(
            manager_name,
            template=template,
            json_schema=_plugin_schema_for_manager(manager_name),
            kind_schema=_kind_schema_for_manager(manager_name),
        )
    except ImportError:
        # Phase 2 module unavailable - use legacy inline implementation
        pass
    
    # Legacy inline implementation (fallback)
    def _default_value(v, tp):
        # A default stated as the value itself keeps its kind; only text
        # needs the field type to say what it is.
        if v == "" or not isinstance(v, str):
            return v
        return _display_to_json(v, tp)

    d: dict = {"managerName": manager_name, "managerProperties": {}}
    for f in schema.get("top", []):
        v = f.get("default", "")
        if f.get("type", "text") == "bool" and isinstance(v, bool):
            d[f["key"]] = v
        elif v == "null":
            d[f["key"]] = None
        else:
            d[f["key"]] = _default_value(v, f.get("type", "text"))
    for f in schema.get("props", []):
        d["managerProperties"][f["key"]] = _default_value(f.get("default", ""), f.get("type", "text"))
    for nest_key, nest_fields in schema.get("nested", {}).items():
        sub = {}
        for f in nest_fields:
            sub[f["key"]] = _default_value(f.get("default", ""), f.get("type", "text"))
        d["managerProperties"][nest_key] = sub
    return d


def _build_default_section(schema: dict) -> dict:
    """Return a new section dict pre-filled with this schema's field defaults."""
    out: dict = {}
    for f in schema.get("fields", []):
        v = f.get("default", "")
        tp = f.get("type", "text")
        if tp == "bool":
            out[f["key"]] = bool(v) if isinstance(v, bool) else False
        elif tp == "multiselect":
            if isinstance(v, list):
                out[f["key"]] = list(v)
            elif isinstance(v, str) and v.strip():
                out[f["key"]] = [x.strip() for x in v.split(",") if x.strip()]
            else:
                out[f["key"]] = []
        elif tp == "json":
            if isinstance(v, str) and v.strip():
                try:
                    out[f["key"]] = json.loads(v)
                except json.JSONDecodeError:
                    out[f["key"]] = {}
            elif v == "":
                out[f["key"]] = {}
            else:
                # A default stated as a value keeps its kind -- None included.
                out[f["key"]] = v
        elif v == "null":
            out[f["key"]] = None
        elif v == "" and tp in ("ref", "text", "path", "select"):
            out[f["key"]] = ""
        else:
            out[f["key"]] = _display_to_json(str(v), tp) if not isinstance(v, (int, float, bool)) else v
    return out


#: ``schema["write_policy"]`` value meaning "write the section as the file has
#: it": Apply writes a field only if the file already had its key, the
#: operator touched it, or it is required. The default policy writes every
#: schema field.
WRITE_AS_WRITTEN = "as_written"


def _writes_as_written(schema: dict) -> bool:
    return schema.get("write_policy") == WRITE_AS_WRITTEN


def _new_section_seed(schema: dict) -> dict:
    """What a brand-new section starts from when the operator adds it.

    Normally every field's default, so the dialog opens fully populated. A
    schema that writes "as written" is seeded with its required fields only:
    seeding the optional ones would make Apply write a key per field for a
    section the operator never asked to spell out.
    """
    seed = _build_default_section(schema)
    if not _writes_as_written(schema):
        return seed
    required = {f["key"] for f in schema.get("fields", []) if f.get("req")}
    return {k: v for k, v in seed.items() if k in required}


#: The ImProcess block of a setup. It is edited on the centre canvas, in its
#: own dialog, and is therefore not offered in the System card.
IMPROCESS_SECTION_KEY = "processing"

#: Sections that have their own place on the canvas instead of a System row.
_CANVAS_SECTIONS = frozenset({IMPROCESS_SECTION_KEY})


def _schema_fields(schema: dict) -> dict:
    return {f["key"]: f for f in schema.get("fields", [])}


def _improcess_choice_names(field_def: dict, ids, *, with_ids: bool) -> str:
    """``MoNaLISA, View only`` -- or ``MoNaLISA (monalisa), ...`` with ``with_ids``."""
    labels = field_def.get("option_labels") or {}
    names = []
    for i in ids:
        name = str(labels.get(str(i)) or i)
        names.append(f"{name} ({i})" if with_ids and name != str(i) else name)
    return ", ".join(names) if names else "none"


def _improcess_not_configured_text(schema: dict) -> str:
    """What ImProcess does with no ``processing`` block, from the schema's own defaults."""
    fields = _schema_fields(schema)
    parts = []
    for key, noun in (("reconstructors", "reconstructors"), ("processors", "processors")):
        field_def = fields.get(key)
        if field_def is not None:
            parts.append(
                f"{noun} {_improcess_choice_names(field_def, _field_default(field_def) or [], with_ids=True)}"
            )
    return (
        "Not configured \u2014 ImProcess starts with its standalone defaults: "
        + " + ".join(parts)
    )


def _improcess_summary_lines(schema: dict, block: dict) -> list[str]:
    """The card of a configured ImProcess section, one line per fact."""
    fields = _schema_fields(schema)
    lines = []
    recon = fields.get("reconstructors")
    if recon is not None:
        listed = block.get("reconstructors")
        shown = listed if isinstance(listed, list) else (_field_default(recon) or [])
        suffix = "" if isinstance(listed, list) else " (default)"
        lines.append(f"Reconstructors: {_improcess_choice_names(recon, shown, with_ids=False)}{suffix}")
    proc = fields.get("processors")
    if proc is not None:
        listed = block.get("processors")
        if isinstance(listed, list):
            lines.append(f"Processors: {len(listed)}")
        else:
            lines.append(f"Processors: {len(_field_default(proc) or [])} (default)")
    panels = [
        f["label"].removeprefix("Show ").removesuffix(" panel")
        for f in schema.get("fields", [])
        if f.get("grp") == "Analysis panels" and block.get(f["key"]) is True
    ]
    if panels:
        lines.append("Analysis panels: " + ", ".join(panels))
    live = []
    for f in schema.get("fields", []):
        if f.get("grp") != "Live mode" or f["key"] not in block:
            continue
        default = _field_default(f)
        default = None if default == "null" else default
        if block[f["key"]] != default:
            live.append(f"{f['label']} = {json.dumps(block[f['key']])}")
    if live:
        lines.append("Live mode: " + "; ".join(live))
    custom = [k for k in block if k not in fields]
    if custom:
        lines.append(f"{len(custom)} custom key{'s' if len(custom) != 1 else ''}")
    return lines


# Maps each widget in availableWidgets that needs a section to the section key.
# Built from section schemas' requires_widget field (single source of truth).
def _build_widget_requires_section_map() -> dict:
    """Build widget→section map from SECTION_SCHEMAS using requires_widget field."""
    mapping = {}
    for section_key, schema in SECTION_SCHEMAS.items():
        widget = schema.get("requires_widget")
        if widget:
            mapping[widget] = section_key
    return mapping

_WIDGET_REQUIRES_SECTION = _build_widget_requires_section_map()


def _section_option_values(section_key: str, field_key: str) -> list:
    schema = SECTION_SCHEMAS.get(section_key) or {}
    for field in schema.get("fields", []):
        if field.get("key") == field_key:
            return list(field.get("opts") or [])
    return []


def _diagnostic_message_to_html(diag) -> str:
    msg = diag.message
    for _kw in ("focusLock", "autofocus", "tiling", "scan", "etSTED",
                "processing", "microscopeStand", "pulseStreamer",
                "availableWidgets"):
        msg = msg.replace(_kw, f"<b>{_kw}</b>")

    fix = getattr(diag, "fix", None)
    if fix and fix.action == "configure_section":
        msg += f" <a href='fixsection:{fix.target}'>Configure…</a>"
    return msg


def _collect_xref_issues(data: dict) -> list[tuple[str, str]]:
    """Collect cross-reference diagnostics as ``(severity, html_message)`` pairs.

    Kept as a compatibility wrapper for tests and external script users after
    validation moved into ``imswitch.imcontrol.model.plugins.validation``.
    """
    try:
        from imswitch.imcontrol.model.plugins.registry import build_default_registry
        from imswitch.imcontrol.model.plugins.validation import (
            ValidationContext,
            validate_setup_data,
        )

        context = ValidationContext(
            widget_requires_section=_WIDGET_REQUIRES_SECTION,
            known_reconstructor_ids=tuple(_section_option_values("processing", "reconstructors")),
            known_processor_ids=tuple(_section_option_values("processing", "processors")),
        )
        report = validate_setup_data(
            data,
            build_default_registry(discover=True),
            context=context,
        )
        return [
            (diag.severity, _diagnostic_message_to_html(diag))
            for diag in report.diagnostics
            if diag.code.startswith(("xref.", "widget.", "legacy."))
        ]
    except Exception:
        issues = []
        widgets = data.get("availableWidgets") or []
        for widget, section_key in _WIDGET_REQUIRES_SECTION.items():
            if widget in widgets and not data.get(section_key):
                issues.append((
                    "warning",
                    f"Widget '{widget}' is enabled but no '{section_key}' section is configured. "
                    f"<a href='fixsection:{section_key}'>Configure…</a>",
                ))
        return issues


# =============================================================================
# Theme helpers
# =============================================================================
def is_dark_mode() -> bool:
    """Whether widgets in this application are drawn on a dark background.

    The application palette is not enough: ImSwitch darkens itself with an
    application style sheet (qdarkstyle) and leaves the palette light, so
    inside ImSwitch the palette alone picked the light card colours -- white
    cards with light text. A widget polished under the style sheet carries
    the background it will really be drawn with, so that is asked as well.
    """
    app = QApplication.instance()
    if app is None:
        return False
    if app.palette().window().color().lightness() < 128:
        return True
    if not app.styleSheet():
        return False
    probe = QWidget()
    probe.ensurePolished()
    return probe.palette().window().color().lightness() < 128


def get_themed_colors(is_dark: bool):
    """Return dict of colors appropriate for current theme."""
    if is_dark:
        return {
            'card_bg': '#2B2B2B',
            'card_bg_selected': '#1E3A52',
            'card_bg_hover': '#3C3F41',
            'card_border': '#3C3F41',
            'header_bg': '#252525',
            'extras_bar_bg': '#1E1E1E',
            'extras_bar_border': '#3C3F41',
            'chip_bg': '#3C3F41',
            'chip_border': '#555555',
            'button_bg': '#3C3F41',
            'button_bg_hover': '#4C5F6B',
        }
    else:
        return {
            'card_bg': '#F8F8F8',
            'card_bg_selected': '#EEF4FF',
            'card_bg_hover': '#EFF6FF',
            'card_border': '#DDD',
            'header_bg': '#F0F4F8',
            'extras_bar_bg': '#F5F5F5',
            'extras_bar_border': '#DDD',
            'chip_bg': '#E0E8F0',
            'chip_border': '#B0C4D8',
            'button_bg': '#EEF2F6',
            'button_bg_hover': '#DDE8F2',
        }


# =============================================================================
# FlowLayout – wraps children into rows like a tag cloud
# =============================================================================
class FlowLayout(QLayout):
    """Layout that arranges items in rows, wrapping to new rows as needed."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._item_list = []
        self._h_spacing = -1
        self._v_spacing = -1

    def __del__(self):
        item = self.takeAt(0)
        while item:
            item = self.takeAt(0)

    def addItem(self, item: QLayoutItem):
        self._item_list.append(item)

    def count(self) -> int:
        return len(self._item_list)

    def itemAt(self, index: int) -> QLayoutItem:
        if 0 <= index < len(self._item_list):
            return self._item_list[index]
        return None

    def takeAt(self, index: int) -> QLayoutItem:
        if 0 <= index < len(self._item_list):
            return self._item_list.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientations(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        height = self._do_layout(QRect(0, 0, width, 0), test_only=True)
        return height

    def setGeometry(self, rect: QRect):
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._item_list:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    def setSpacing(self, spacing: int):
        self._h_spacing = spacing
        self._v_spacing = spacing

    def spacing(self) -> int:
        return self._h_spacing

    def _horizontal_spacing(self) -> int:
        if self._h_spacing >= 0:
            return self._h_spacing
        return self._smart_spacing(QSizePolicy.PushButton, Qt.Horizontal)

    def _vertical_spacing(self) -> int:
        if self._v_spacing >= 0:
            return self._v_spacing
        return self._smart_spacing(QSizePolicy.PushButton, Qt.Vertical)

    def _smart_spacing(self, _control_type, orientation) -> int:
        parent = self.parent()
        if parent is None:
            return -1
        if parent.isWidgetType():
            return parent.style().layoutSpacing(
                QSizePolicy.PushButton, QSizePolicy.PushButton, orientation
            )
        return parent.spacing()

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        left, top, right, bottom = self.getContentsMargins()
        effective_rect = rect.adjusted(left, top, -right, -bottom)
        x = effective_rect.x()
        y = effective_rect.y()
        line_height = 0

        for item in self._item_list:
            widget = item.widget()
            space_x = self._horizontal_spacing()
            space_y = self._vertical_spacing()

            next_x = x + item.sizeHint().width() + space_x
            if next_x - space_x > effective_rect.right() and line_height > 0:
                x = effective_rect.x()
                y = y + line_height + space_y
                next_x = x + item.sizeHint().width() + space_x
                line_height = 0

            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), item.sizeHint()))

            x = next_x
            line_height = max(line_height, item.sizeHint().height())

        return y + line_height - rect.y() + bottom


# =============================================================================
# DeviceCard
# =============================================================================
class DeviceCard(QFrame):
    """Compact coloured card for one device."""

    # Emitted to the canvas; canvas passes signals up to MainWindow
    sig_clicked   = pyqtSignal(str, str)   # (category, name)
    sig_duplicate = pyqtSignal(str, str)
    sig_rename    = pyqtSignal(str, str)
    sig_delete    = pyqtSignal(str, str)
    sig_save_tmpl = pyqtSignal(str, str)
    sig_copy      = pyqtSignal(str, str)
    sig_copy_other = pyqtSignal(str, str)
    sig_set_enabled = pyqtSignal(str, str, bool)

    def __init__(
        self, category: str, name: str, device_data: dict, *,
        enabled: bool = True, compact: bool = False,
        comparison_state: str | None = None,
        copy_direction: str | None = None,
        missing: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.category = category
        self.device_name = name
        self.device_data = device_data
        self.enabled = enabled
        self.compact = compact
        self.comparison_state = comparison_state
        self.copy_direction = copy_direction
        self.missing = missing
        self._selected = False
        self._init_ui()

    def _init_ui(self):
        # Compare mode gets roughly half of the normal canvas width. Keep cards
        # readable there and let the flow layout wrap them instead of forcing a
        # horizontal scrollbar. Normal mode retains the roomier cards.
        self.setFixedWidth(158 if self.compact else 195)
        compare_extra = 18 if self.comparison_state else 0
        self.setMinimumHeight((62 if self.compact else 80) + compare_extra)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        # Missing placeholders are still selectable so selection can stay
        # synchronized with the real card on the opposite pane.
        self.setCursor(Qt.PointingHandCursor)
        self._apply_style(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 7, 7, 7)
        layout.setSpacing(2)

        name_row = QHBoxLayout()
        name_row.setContentsMargins(0, 0, 0, 0)
        name_row.setSpacing(4)
        self._name_lbl = QLabel(self.device_name)
        bold = QFont()
        bold.setBold(True)
        bold.setPointSize(9)
        self._name_lbl.setFont(bold)
        self._name_lbl.setWordWrap(True)
        if not self.enabled and not self.missing:
            self._name_lbl.setStyleSheet("color:#888;")
        name_row.addWidget(self._name_lbl, 1)
        if not self.enabled and not self.missing:
            disabled_lbl = QLabel("DISABLED")
            disabled_lbl.setStyleSheet(
                "color:#888; border:1px solid #888; border-radius:2px; "
                "padding:0 3px; font-size:7pt; font-weight:bold;"
            )
            disabled_lbl.setToolTip(
                "This device is kept in the setup file but will not be instantiated."
            )
            name_row.addWidget(disabled_lbl)
        layout.addLayout(name_row)

        mgr = self.device_data.get("managerName", "")
        self._mgr_lbl = QLabel(
            "Not present in this config" if self.missing else _manager_display_name(mgr)
        )
        self._mgr_lbl.setStyleSheet(
            f"color:{'#999' if self.missing else ('#888' if not self.enabled else '#666')};"
            "font-size:8pt;"
        )
        layout.addWidget(self._mgr_lbl)

        info = self._summary()
        if info and not self.compact and not self.missing:
            lbl = QLabel(info)
            lbl.setStyleSheet(f"color:{'#999' if not self.enabled else '#888'};font-size:8pt;")
            lbl.setWordWrap(True)
            layout.addWidget(lbl)

        layout.addStretch()

        if self.comparison_state:
            compare_row = QHBoxLayout()
            compare_row.setContentsMargins(0, 1, 0, 0)
            compare_row.setSpacing(4)

            labels = {
                "same": "SAME",
                "different": "DIFFERENT",
                "only_here": "ONLY HERE",
                "missing_here": "MISSING HERE",
            }
            badge = QLabel(labels.get(self.comparison_state, self.comparison_state.upper()))
            badge.setStyleSheet(
                "color:#777; border:1px solid #999; border-radius:2px; "
                "padding:0 3px; font-size:7pt; font-weight:bold;"
            )
            if self.comparison_state == "same":
                badge.setToolTip("The stored device definition and enabled state match.")
            elif self.comparison_state == "different":
                badge.setToolTip(
                    "This device exists in both configs but its definition or enabled state differs."
                )
            elif self.comparison_state == "only_here":
                badge.setToolTip("This device exists only in this config.")
            elif self.comparison_state == "missing_here":
                badge.setToolTip("The other config has this device; this config does not.")
            compare_row.addWidget(badge)
            compare_row.addStretch()

            if self.copy_direction and not self.missing:
                arrow = "→" if self.copy_direction == "right" else "←"
                copy_btn = QPushButton(f"Copy {arrow}" if arrow == "→" else f"{arrow} Copy")
                copy_btn.setFixedHeight(19)
                copy_btn.setStyleSheet("font-size:7pt; padding:0 4px;")
                copy_btn.setToolTip("Copy this complete device definition to the other config")
                copy_btn.clicked.connect(
                    lambda _=False: self.sig_copy_other.emit(
                        self.category, self.device_name
                    )
                )
                compare_row.addWidget(copy_btn)
            layout.addLayout(compare_row)

    def _summary(self) -> str:
        d = self.device_data
        props = d.get("managerProperties") or {}
        _skip = {"managerName", "managerProperties", "forAcquisition",
                 "forFocusLock", "forPositioning", "forScanning"}
        parts = []
        for k, v in d.items():
            if k in _skip or v is None or v == "null" or v == "" or isinstance(v, (dict, list, bool)):
                continue
            parts.append(f"{k}: {v}")
            if len(parts) >= 2:
                break
        for k, v in props.items():
            if v is None or v == "" or isinstance(v, (dict, list)):
                continue
            parts.append(f"{k}: {v}")
            if len(parts) >= 3:
                break
        return "  ·  ".join(parts[:3])

    def _apply_style(self, selected: bool):
        color = CAT_COLOR.get(self.category, "#888")
        bw = "5px" if selected else "3px"
        # Get themed colors based on current palette
        dark = is_dark_mode()
        colors = get_themed_colors(dark)
        if self.missing:
            bg = colors['card_bg_selected'] if selected else colors['card_bg']
            border_style = "dashed"
            accent = "#999999"
        elif self.enabled:
            bg = colors['card_bg_selected'] if selected else colors['card_bg']
            border_style = "solid"
            accent = color
        else:
            bg = colors['card_bg_selected'] if selected else colors['card_bg']
            border_style = "dashed"
            accent = "#777777"
        self.setStyleSheet(f"""
            DeviceCard {{
                background: {bg};
                border: 1px {border_style} {colors['card_border']};
                border-left: {bw} {border_style} {accent};
                border-radius: 4px;
            }}
            DeviceCard:hover {{
                background: {colors['card_bg_hover']};
                border-left: 5px {border_style} {accent};
            }}
        """)

    def set_selected(self, selected: bool):
        self._selected = selected
        self._apply_style(selected)

    def refresh(self):
        self._name_lbl.setText(self.device_name)
        self._init_ui()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.sig_clicked.emit(self.category, self.device_name)
        super().mousePressEvent(event)

    def contextMenuEvent(self, event):
        if self.missing:
            return
        menu = QMenu(self)
        a_enabled = None
        if self.category in DISABLEABLE_DEVICE_CATS:
            a_enabled = menu.addAction("Disable device" if self.enabled else "Enable device")
            menu.addSeparator()
        a_copy = menu.addAction("Copy")
        menu.addSeparator()
        a_dup  = menu.addAction("Duplicate")
        a_ren  = menu.addAction("Rename…")
        menu.addSeparator()
        a_tmpl = menu.addAction("Save as Template")
        menu.addSeparator()
        a_del  = menu.addAction("Delete")
        action = menu.exec_(self.mapToGlobal(event.pos()))
        if a_enabled is not None and action == a_enabled:
            self.sig_set_enabled.emit(self.category, self.device_name, not self.enabled)
        elif action == a_copy:
            self.sig_copy.emit(self.category, self.device_name)
        elif action == a_dup:
            self.sig_duplicate.emit(self.category, self.device_name)
        elif action == a_ren:
            self.sig_rename.emit(self.category, self.device_name)
        elif action == a_tmpl:
            self.sig_save_tmpl.emit(self.category, self.device_name)
        elif action == a_del:
            self.sig_delete.emit(self.category, self.device_name)


# =============================================================================
# DeviceCanvas – scrollable grid of cards grouped by category
# =============================================================================
class DeviceCanvas(QScrollArea):
    sig_device_selected = pyqtSignal(str, str)
    sig_device_deleted  = pyqtSignal(str, str)
    sig_device_duped    = pyqtSignal(str, str)
    sig_device_renamed  = pyqtSignal(str, str)
    sig_save_tmpl       = pyqtSignal(str, str)
    sig_device_copied   = pyqtSignal(str, str)
    sig_copy_to_other   = pyqtSignal(str, str)
    sig_device_enabled_changed = pyqtSignal(str, str, bool)
    sig_category_settings = pyqtSignal(str)
    sig_improcess_edit = pyqtSignal()
    sig_improcess_remove = pyqtSignal()

    def __init__(self, parent=None, *, compact: bool = False):
        super().__init__(parent)
        self._compact = compact
        self._data: dict = {}
        self._comparison_states: dict[tuple[str, str], str] = {}
        self._compare_side: str | None = None
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(12, 12, 12, 12)
        self._layout.setSpacing(18)
        self.setWidget(self._container)
        self._cards: dict[tuple, DeviceCard] = {}   # (cat, name) → card
        self._selected: object = None
        self._section_grids: dict[str, QLayout] = {}
        self._section_order: list[str] = []   # "improcess" and category keys, top to bottom
        self._improcess_box: QWidget | None = None
        self._improcess_lay: QVBoxLayout | None = None
        self._improcess_lines: list[QLabel] = []   # the card's text, for tests

    def load(
        self,
        data: dict,
        *,
        comparison_states: dict[tuple[str, str], str] | None = None,
        compare_side: str | None = None,
    ):
        self._data = data
        self._comparison_states = comparison_states or {}
        self._compare_side = compare_side if self._comparison_states else None
        self._clear()
        show_improcess = IMPROCESS_SECTION_KEY in SECTION_SCHEMAS
        # A setup with no device in any category (the processor-only presets)
        # is about ImProcess: it leads. Otherwise it follows the devices.
        has_devices = any(
            (data.get(cat) or {})
            or (cat in DISABLEABLE_DEVICE_CATS and _device_section(data, cat, enabled=False))
            for cat in DEVICE_CATS
        )
        if show_improcess and not has_devices:
            self._add_improcess_section()
        for cat in DEVICE_CATS:
            devices = data.get(cat) or {}
            disabled = _device_section(data, cat, enabled=False) if cat in DISABLEABLE_DEVICE_CATS else {}
            # Always show categories from the registry, even if empty
            if cat in _CATEGORY_REGISTRY or cat == "others":
                self._section_order.append(cat)
                self._add_section(cat, devices, disabled, data)
        if show_improcess and has_devices:
            self._add_improcess_section()
        self._layout.addStretch()

    def _clear(self):
        self._cards.clear()
        self._selected = None
        self._section_grids.clear()
        self._section_order.clear()
        self._improcess_box = None
        self._improcess_lay = None
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _add_section(self, cat: str, devices: dict, disabled_devices: dict, data: dict):
        color = CAT_COLOR[cat]
        label_text = CAT_LABEL[cat]

        # Section header
        header = QWidget()
        hlay = QHBoxLayout(header)
        hlay.setContentsMargins(0, 0, 0, 4)

        dot = QLabel("●")
        dot.setStyleSheet(f"color:{color}; font-size:14px;")
        hlay.addWidget(dot)

        if cat in DISABLEABLE_DEVICE_CATS:
            count_text = f"{len(devices)} active"
            if disabled_devices:
                count_text += f", {len(disabled_devices)} disabled"
        else:
            count_text = f"{len(devices)} device{'s' if len(devices)!=1 else ''}"
        lbl = QLabel(f"<b>{label_text}</b>  <span style='color:#999;font-size:9pt;'>"
                     f"({count_text})</span>")
        lbl.setTextFormat(Qt.RichText)
        hlay.addWidget(lbl)

        category_schema = CATEGORY_SCHEMAS.get(cat)
        if category_schema:
            category_values = (data.get("deviceCategorySettings") or {}).get(cat)
            if not isinstance(category_values, dict):
                category_values = {}
            summary_parts = []
            for field_def in category_schema.get("fields", []):
                if not field_def.get("show_in_header"):
                    continue
                value = category_values.get(field_def["key"], _field_default(field_def))
                display_value = _field_option_label(field_def, value)
                summary_parts.append(f"{field_def.get('label', field_def['key'])}: {display_value}")
            known_keys = {field_def["key"] for field_def in category_schema.get("fields", [])}
            custom_keys = [k for k in category_values if k not in known_keys]
            if custom_keys:
                summary_parts.append(
                    f"{len(custom_keys)} custom propert{'y' if len(custom_keys) == 1 else 'ies'}"
                )
            if summary_parts:
                summary_lbl = QLabel(
                    "<span style='color:#888;font-size:8pt;'>"
                    + " · ".join(summary_parts)
                    + "</span>"
                )
                summary_lbl.setTextFormat(Qt.RichText)
                hlay.addWidget(summary_lbl)

        hlay.addStretch()

        if category_schema:
            settings_btn = QPushButton("⚙ Settings")
            settings_btn.setFixedHeight(22)
            settings_btn.setStyleSheet("font-size:8pt; padding:0 7px;")
            settings_btn.setToolTip(
                f"Edit {category_schema.get('display', label_text + ' settings')}"
            )
            settings_btn.clicked.connect(
                lambda _, c=cat: self.sig_category_settings.emit(c)
            )
            hlay.addWidget(settings_btn)

        add_btn = QPushButton("＋ Add")
        add_btn.setFixedHeight(22)
        add_btn.setStyleSheet(f"""
            QPushButton {{
                background: {color}22; border: 1px solid {color}66;
                border-radius: 3px; color: {color}; font-size: 9pt;
                padding: 0 8px;
            }}
            QPushButton:hover {{ background: {color}44; }}
        """)
        add_btn.clicked.connect(lambda _, c=cat: self._request_add(c))
        hlay.addWidget(add_btn)
        self._layout.addWidget(header)

        # Cards use the existing flow layout so the same canvas can shrink
        # cleanly into one half of Compare mode. The old fixed four-column grid
        # overflowed narrow panes and effectively required horizontal scrolling.
        grid_widget = QWidget()
        grid = FlowLayout(grid_widget)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(8)
        self._section_grids[cat] = grid

        if self._comparison_states and self._compare_side:
            # Use the same union/order on both panes so corresponding cards are
            # visually easy to scan even when the source JSON order differs.
            names = sorted(
                name
                for (state_cat, name) in self._comparison_states
                if state_cat == cat
            )
            card_entries = []
            for name in names:
                if name in devices:
                    card_entries.append((name, devices[name], True, False))
                elif name in disabled_devices:
                    card_entries.append((name, disabled_devices[name], False, False))
                else:
                    card_entries.append((name, {}, True, True))
        else:
            card_entries = [
                (name, dev_data, True, False) for name, dev_data in devices.items()
            ]
            card_entries.extend(
                (name, dev_data, False, False)
                for name, dev_data in disabled_devices.items()
                if name not in devices
            )

        for name, dev_data, enabled, missing in card_entries:
            state = self._comparison_states.get((cat, name))
            perspective = (
                _comparison_status_for_side(state, self._compare_side)
                if state and self._compare_side
                else None
            )
            copy_direction = None
            if perspective in {"different", "only_here"} and not missing:
                copy_direction = "right" if self._compare_side == "left" else "left"
            card = DeviceCard(
                cat, name, dev_data,
                enabled=enabled,
                compact=self._compact,
                comparison_state=perspective,
                copy_direction=copy_direction,
                missing=missing,
            )
            card.sig_clicked.connect(self._on_card_clicked)
            card.sig_duplicate.connect(self.sig_device_duped)
            card.sig_rename.connect(self.sig_device_renamed)
            card.sig_delete.connect(self.sig_device_deleted)
            card.sig_save_tmpl.connect(self.sig_save_tmpl)
            card.sig_copy.connect(self.sig_device_copied)
            card.sig_copy_other.connect(self.sig_copy_to_other)
            card.sig_set_enabled.connect(self.sig_device_enabled_changed)
            grid.addWidget(card)
            self._cards[(cat, name)] = card

        self._layout.addWidget(grid_widget)

    # ── ImProcess section ──────────────────────────────────────────────────
    def _add_improcess_section(self) -> None:
        """The ImProcess section: always shown, one summary card."""
        self._section_order.append("improcess")
        self._improcess_box = QWidget()
        self._improcess_lay = QVBoxLayout(self._improcess_box)
        self._improcess_lay.setContentsMargins(0, 0, 0, 0)
        self._improcess_lay.setSpacing(self._layout.spacing())
        self._layout.addWidget(self._improcess_box)
        self._fill_improcess_section()

    def refresh_improcess(self) -> None:
        """Redraw only the ImProcess section, keeping the device selection."""
        if self._improcess_box is not None:
            self._fill_improcess_section()

    def _fill_improcess_section(self) -> None:
        lay = self._improcess_lay
        while lay.count():
            item = lay.takeAt(0)
            old = item.widget()
            if old is not None:
                old.setParent(None)
                old.deleteLater()
        schema = SECTION_SCHEMAS[IMPROCESS_SECTION_KEY]
        raw = self._data.get(IMPROCESS_SECTION_KEY)
        configured = raw not in (None, {}, [])
        block = raw if isinstance(raw, dict) else {}
        colors = get_themed_colors(is_dark_mode())
        read_only = self._compact or bool(self._compare_side)
        display = schema.get("display", "ImProcess")

        # Header: like a device category's, in theme colours.
        header = QWidget()
        hlay = QHBoxLayout(header)
        hlay.setContentsMargins(0, 0, 0, 4)
        dot = QLabel("●")
        dot.setStyleSheet(f"color:{colors['chip_border']}; font-size:14px;")
        hlay.addWidget(dot)
        state = "configured" if configured else "not configured"
        lbl = QLabel(f"<b>{display}</b>  <span style='color:#999;font-size:9pt;'>({state})</span>")
        lbl.setTextFormat(Qt.RichText)
        hlay.addWidget(lbl)
        hlay.addStretch()
        lay.addWidget(header)

        card = QFrame()
        card.setObjectName("improcessCard")
        card.setStyleSheet(
            f"QFrame#improcessCard {{ background:{colors['card_bg']}; "
            f"border:1px solid {colors['card_border']}; border-radius:6px; }}"
        )
        card.setToolTip(schema.get("summary", ""))
        clay = QVBoxLayout(card)
        clay.setContentsMargins(10, 8, 10, 8)
        clay.setSpacing(2)
        if not configured:
            lines = [_improcess_not_configured_text(schema)]
        elif not isinstance(raw, dict):
            lines = [f"The {IMPROCESS_SECTION_KEY} entry is not an object; Edit\u2026 replaces it."]
        else:
            lines = _improcess_summary_lines(schema, block)
        self._improcess_lines = []
        for line in lines:
            text = QLabel(line)
            text.setTextFormat(Qt.PlainText)
            text.setWordWrap(True)
            text.setStyleSheet("font-size:9pt; border:none; background:transparent;")
            clay.addWidget(text)
            self._improcess_lines.append(text)

        if not read_only:
            buttons = QHBoxLayout()
            buttons.setContentsMargins(0, 6, 0, 0)
            buttons.addStretch()
            button_style = (
                f"QPushButton {{ font-size:8pt; padding:0 8px; border:1px solid {colors['chip_border']}; "
                f"border-radius:3px; background:{colors['button_bg']}; }}"
                f"QPushButton:hover {{ background:{colors['button_bg_hover']}; }}"
            )
            edit = QPushButton("Edit…" if configured else "Configure…")
            edit.setFixedHeight(22)
            edit.setStyleSheet(button_style)
            edit.setToolTip(f"Edit the {display} section of this setup")
            edit.clicked.connect(self.sig_improcess_edit.emit)
            buttons.addWidget(edit)
            if configured:
                remove = QPushButton("Remove")
                remove.setFixedHeight(22)
                remove.setStyleSheet(button_style)
                remove.setToolTip(f"Remove the {display} section from this setup")
                remove.clicked.connect(self.sig_improcess_remove.emit)
                buttons.addWidget(remove)
            clay.addLayout(buttons)
        lay.addWidget(card)

    def _on_card_clicked(self, cat: str, name: str):
        self.select_device(cat, name, emit=True)

    def select_device(self, cat: str, name: str, *, emit: bool = False) -> bool:
        if self._selected:
            old = self._cards.get(self._selected)
            if old:
                old.set_selected(False)
        key = (cat, name)
        card = self._cards.get(key)
        if card is None:
            self._selected = None
            return False
        self._selected = key
        if card:
            card.set_selected(True)
        if emit:
            self.sig_device_selected.emit(cat, name)
        return True

    def _request_add(self, cat: str):
        if cat == "others":
            choices = ["[Free-form / Custom]"] + sorted(SCHEMAS.keys())
        else:
            # Build list: templated managers first, then non-templated with suffix
            templated = []
            non_templated = []
            for mgr in CAT_MANAGERS.get(cat, []):
                if mgr in SCHEMAS:
                    templated.append(mgr)
                else:
                    non_templated.append(f"{mgr}  (no template — blank)")
            choices = templated + non_templated + ["[Free-form / Custom]"]

        if not choices:
            return

        mgr, ok = QInputDialog.getItem(
            self, f"Add {CAT_LABEL[cat]}", "Select manager type:", choices, 0, False
        )
        if not ok:
            return

        # Strip suffix if present
        if mgr.endswith("  (no template — blank)"):
            mgr = mgr.replace("  (no template — blank)", "")

        name, ok2 = QInputDialog.getText(
            self, "Device Name", f"Name for new {CAT_LABEL[cat]} device:"
        )
        if not ok2 or not name.strip():
            return

        mgr_key = "__custom__" if mgr == "[Free-form / Custom]" else mgr
        self.sig_device_duped.emit("__ADD__", f"{cat}|{name.strip()}|{mgr_key}")

    def set_compact(self, compact: bool) -> None:
        compact = bool(compact)
        if self._compact == compact:
            return
        selected = self._selected
        self._compact = compact
        self.load(
            self._data,
            comparison_states=self._comparison_states,
            compare_side=self._compare_side,
        )
        if selected and selected in self._cards:
            self._selected = selected
            self._cards[selected].set_selected(True)

    def clear_comparison(self) -> None:
        """Remove compare badges/placeholders while preserving selection."""
        selected = self._selected
        self.load(self._data)
        if selected:
            cat, name = selected
            self.select_device(cat, name)

    def deselect_all(self):
        if self._selected:
            old = self._cards.get(self._selected)
            if old:
                old.set_selected(False)
        self._selected = None


# =============================================================================
# ConfigPane – reusable canvas wrapper for normal and split compare views
# =============================================================================
class ConfigPane(QWidget):
    """One config canvas with a small identity/action header.

    The primary editor and the reference side of Compare mode intentionally use
    the same DeviceCanvas.  ConfigPane only owns presentation; MainWindow keeps
    file/data lifecycle so existing config-editor semantics remain centralized.
    """

    sig_config_requested = pyqtSignal(str)
    sig_paste_requested = pyqtSignal()
    sig_save_requested = pyqtSignal()

    def __init__(
        self, title: str, *, selector: bool = False, compact: bool = False, parent=None
    ):
        super().__init__(parent)
        self._selector_mode = selector
        self._current_path = ""
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self._header = QFrame()
        self._header.setObjectName("configPaneHeader")
        self._header_colors = get_themed_colors(is_dark_mode())
        self._header_active = False
        self._apply_header_style()
        hl = QHBoxLayout(self._header)
        hl.setContentsMargins(8, 5, 8, 5)
        hl.setSpacing(6)

        self._title_lbl = QLabel(f"<b>{title}</b>")
        self._title_lbl.setTextFormat(Qt.RichText)
        hl.addWidget(self._title_lbl)

        self._selector = QComboBox()
        self._selector.setMinimumWidth(130)
        self._selector.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._selector.currentIndexChanged.connect(self._on_selector_changed)
        self._selector.setVisible(selector)
        hl.addWidget(self._selector, 1)

        self._path_lbl = QLabel()
        self._path_lbl.setTextFormat(Qt.RichText)
        self._path_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._path_lbl.setVisible(not selector)
        hl.addWidget(self._path_lbl, 1)

        self._state_lbl = QLabel()
        self._state_lbl.setTextFormat(Qt.RichText)
        self._state_lbl.setStyleSheet("font-size:8pt;")
        hl.addWidget(self._state_lbl)

        self._paste_btn = QPushButton("Paste")
        self._paste_btn.setFixedHeight(22)
        self._paste_btn.setToolTip("Paste the copied device into this config")
        self._paste_btn.clicked.connect(self.sig_paste_requested.emit)
        hl.addWidget(self._paste_btn)

        self._save_btn = QPushButton("Save")
        self._save_btn.setFixedHeight(22)
        self._save_btn.clicked.connect(self.sig_save_requested.emit)
        hl.addWidget(self._save_btn)
        lay.addWidget(self._header)

        self.canvas = DeviceCanvas(compact=compact)
        lay.addWidget(self.canvas, 1)
        self.set_modified(False)

    def _apply_header_style(self) -> None:
        border = "#3A7FC1" if self._header_active else self._header_colors['card_border']
        width = 2 if self._header_active else 1
        self._header.setStyleSheet(
            f"QFrame#configPaneHeader {{ "
            f"background:{self._header_colors['header_bg']}; "
            f"border-bottom:{width}px solid {border}; }}"
        )

    def set_active(self, active: bool) -> None:
        self._header_active = bool(active)
        self._apply_header_style()

    def set_header_visible(self, visible: bool) -> None:
        self._header.setVisible(bool(visible))

    def set_identity(self, path: str, *, modified: bool = False) -> None:
        self._current_path = str(path or "")
        name = Path(path).name if path else "[new config]"
        self._path_lbl.setText(f"<b>{name}</b>")
        self._path_lbl.setToolTip(path or "New unsaved configuration")
        self.set_modified(modified)

    def set_modified(self, modified: bool) -> None:
        self._state_lbl.setText(
            "<span style='color:#D98C2B;'><b>MODIFIED</b></span>"
            if modified else ""
        )
        can_save_unsaved_primary = not self._selector_mode and not self._current_path
        self._save_btn.setEnabled(bool(modified) or can_save_unsaved_primary)

    def set_selector_paths(self, paths, current: str = "") -> None:
        self._selector.blockSignals(True)
        self._selector.clear()
        current_index = 0
        paths = list(paths)
        if not paths:
            self._selector.addItem("No other configs", "")
        else:
            for path in paths:
                path = str(path)
                self._selector.addItem(Path(path).name, path)
                if current and LeftPanel._same_path(path, current):
                    current_index = self._selector.count() - 1
        self._selector.setCurrentIndex(current_index)
        self._selector.blockSignals(False)

    def set_selected_path(self, path: str) -> None:
        for index in range(self._selector.count()):
            candidate = str(self._selector.itemData(index) or "")
            if candidate and LeftPanel._same_path(candidate, path):
                self._selector.blockSignals(True)
                self._selector.setCurrentIndex(index)
                self._selector.blockSignals(False)
                return

    def _on_selector_changed(self, index: int) -> None:
        path = str(self._selector.itemData(index) or "")
        if path:
            self.sig_config_requested.emit(path)


# =============================================================================
# ComparisonSummary – compact synthetic view over the split comparison
# =============================================================================
class ComparisonSummary(QWidget):
    """Two-column synthetic comparison embedded in the main Compare workspace.

    This intentionally mirrors the original compare-dialog interaction: each
    config gets its own compact device tree, selection is synchronized, and a
    narrow centre strip owns the comparison status plus Copy → / ← Copy actions.
    It remains presentation-only; editing and copy semantics stay owned by the
    normal split-card workspace and 3B comparison backend.
    """

    sig_config_requested = pyqtSignal(str)
    sig_copy_to_other = pyqtSignal(str, str, str)  # category, name, source pane
    sig_device_selected = pyqtSignal(str, str, str)  # category, name, pane

    _STATE_LABELS = {
        "same": "SAME",
        "different": "DIFFERENT",
        "left_only": "ONLY LEFT",
        "right_only": "ONLY RIGHT",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._left_data: dict = {}
        self._right_data: dict = {}
        self._states: dict[tuple[str, str], str] = {}
        self._items: dict[str, dict[tuple[str, str], QTreeWidgetItem]] = {
            "left": {}, "right": {}
        }
        self._selected_key: tuple[str, str] | None = None
        self._syncing_selection = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 8)
        lay.setSpacing(6)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        header.addWidget(QLabel("<b>Compare summary</b>"))
        self._counts_lbl = QLabel()
        self._counts_lbl.setStyleSheet("color:#888; font-size:8pt;")
        header.addWidget(self._counts_lbl)
        header.addStretch()
        self._diff_only = QCheckBox("Differences only")
        self._diff_only.setToolTip(
            "Hide devices whose definition and enabled state are identical"
        )
        self._diff_only.toggled.connect(self._rebuild)
        header.addWidget(self._diff_only)
        lay.addLayout(header)

        selectors = QHBoxLayout()
        selectors.setContentsMargins(0, 0, 0, 0)
        selectors.setSpacing(10)
        left_header = QHBoxLayout()
        left_header.addWidget(QLabel("<b>Left config</b>"))
        self._left_name = QLabel("—")
        self._left_name.setStyleSheet("font-weight:bold;")
        left_header.addWidget(self._left_name, 1)
        left_wrap = QWidget()
        left_wrap.setLayout(left_header)
        selectors.addWidget(left_wrap, 1)

        centre_header = QLabel("Comparison")
        centre_header.setAlignment(Qt.AlignCenter)
        centre_header.setStyleSheet("font-weight:bold; color:#777;")
        centre_header.setFixedWidth(126)
        selectors.addWidget(centre_header)

        right_header = QHBoxLayout()
        right_header.addWidget(QLabel("<b>Right config</b>"))
        self._selector = QComboBox()
        self._selector.setMinimumWidth(180)
        self._selector.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._selector.currentIndexChanged.connect(self._on_selector_changed)
        right_header.addWidget(self._selector, 1)
        right_wrap = QWidget()
        right_wrap.setLayout(right_header)
        selectors.addWidget(right_wrap, 1)
        lay.addLayout(selectors)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(10)
        self._trees = {
            "left": self._make_tree("left"),
            "right": self._make_tree("right"),
        }
        body.addWidget(self._trees["left"], 1)

        centre = QFrame()
        centre.setFrameShape(QFrame.StyledPanel)
        centre.setFixedWidth(126)
        centre_lay = QVBoxLayout(centre)
        centre_lay.setContentsMargins(7, 10, 7, 10)
        centre_lay.setSpacing(7)
        centre_lay.addStretch()
        self._selected_name_lbl = QLabel("Select a device")
        self._selected_name_lbl.setAlignment(Qt.AlignCenter)
        self._selected_name_lbl.setWordWrap(True)
        self._selected_name_lbl.setStyleSheet("font-size:8pt; color:#777;")
        centre_lay.addWidget(self._selected_name_lbl)
        self._selected_status_lbl = QLabel("—")
        self._selected_status_lbl.setAlignment(Qt.AlignCenter)
        self._selected_status_lbl.setStyleSheet("font-weight:bold;")
        centre_lay.addWidget(self._selected_status_lbl)

        self._copy_right_btn = QPushButton("Copy →")
        self._copy_right_btn.setToolTip(
            "Copy the selected left-side device into the right config"
        )
        self._copy_right_btn.clicked.connect(self._copy_selected_right)
        centre_lay.addWidget(self._copy_right_btn)
        self._copy_left_btn = QPushButton("← Copy")
        self._copy_left_btn.setToolTip(
            "Copy the selected right-side device into the left config"
        )
        self._copy_left_btn.clicked.connect(self._copy_selected_left)
        centre_lay.addWidget(self._copy_left_btn)
        centre_lay.addStretch()
        body.addWidget(centre)

        body.addWidget(self._trees["right"], 1)
        lay.addLayout(body, 1)
        self._update_selection_controls()

    def _make_tree(self, side: str) -> QTreeWidget:
        tree = QTreeWidget()
        tree.setColumnCount(4)
        tree.setHeaderLabels(["Device", "State", "Manager", "Comparison"])
        tree.setRootIsDecorated(True)
        tree.setAlternatingRowColors(True)
        tree.setUniformRowHeights(True)
        tree.setSelectionMode(QTreeWidget.SingleSelection)
        tree.setIndentation(14)
        header = tree.header()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        tree.itemSelectionChanged.connect(
            lambda s=side: self._selection_changed(s)
        )
        tree.itemClicked.connect(
            lambda item, _column, s=side: self._item_clicked(s, item)
        )
        return tree

    @staticmethod
    def _device_fields(
        data: dict, category: str, name: str, state: str, side: str
    ) -> tuple[str, str, str, str]:
        device, enabled = _locate_device(data, category, name)
        comparison = _comparison_status_for_side(state, side).replace("_", " ").upper()
        if device is None:
            return name, "Missing", "—", comparison
        manager = str(device.get("managerName", "") or "(custom/unspecified)")
        state_text = "Enabled" if enabled is not False else "Disabled"
        return name, state_text, _manager_display_name(manager), comparison

    def set_selector_paths(self, paths, current: str = "") -> None:
        self._selector.blockSignals(True)
        self._selector.clear()
        current_index = 0
        paths = list(paths)
        if not paths:
            self._selector.addItem("No other configs", "")
        else:
            for path in paths:
                path = str(path)
                self._selector.addItem(Path(path).name, path)
                self._selector.setItemData(
                    self._selector.count() - 1, path, Qt.ToolTipRole
                )
                if current and LeftPanel._same_path(path, current):
                    current_index = self._selector.count() - 1
        self._selector.setCurrentIndex(current_index)
        self._selector.blockSignals(False)

    def set_selected_path(self, path: str) -> None:
        for index in range(self._selector.count()):
            candidate = str(self._selector.itemData(index) or "")
            if candidate and LeftPanel._same_path(candidate, path):
                self._selector.blockSignals(True)
                self._selector.setCurrentIndex(index)
                self._selector.blockSignals(False)
                return

    def load(
        self,
        left: dict,
        right: dict,
        states: dict[tuple[str, str], str],
        *,
        left_path: str = "",
        right_path: str = "",
    ) -> None:
        self._left_data = left or {}
        self._right_data = right or {}
        self._states = states or {}
        self._left_name.setText(Path(left_path).name if left_path else "[new config]")
        self._left_name.setToolTip(left_path or "New unsaved configuration")
        if right_path:
            self.set_selected_path(right_path)
        self._rebuild()

    def _rebuild(self, *_args) -> None:
        previous = self._selected_key
        for tree in self._trees.values():
            tree.clear()
        self._items = {"left": {}, "right": {}}

        shown_states = self._states
        if self._diff_only.isChecked():
            shown_states = {
                key: state for key, state in self._states.items() if state != "same"
            }

        changed_count = sum(1 for state in self._states.values() if state != "same")
        total = len(self._states)
        self._counts_lbl.setText(
            f"{total} device{'s' if total != 1 else ''} · "
            f"{changed_count} difference{'s' if changed_count != 1 else ''}"
        )

        any_rows = False
        for category in DEVICE_CATS:
            entries = sorted(
                (
                    (name, state)
                    for (cat, name), state in shown_states.items()
                    if cat == category
                ),
                key=lambda item: item[0].casefold(),
            )
            if not entries:
                continue
            any_rows = True
            roots = {}
            difference_count = sum(1 for _name, state in entries if state != "same")
            suffix = (
                f" · {difference_count} difference{'s' if difference_count != 1 else ''}"
                if difference_count else ""
            )
            for side in ("left", "right"):
                root = QTreeWidgetItem([f"{CAT_LABEL.get(category, category)}{suffix}"])
                font = root.font(0)
                font.setBold(True)
                root.setFont(0, font)
                self._trees[side].addTopLevelItem(root)
                self._trees[side].setFirstItemColumnSpanned(root, True)
                roots[side] = root

            for name, state in entries:
                for side, data in (("left", self._left_data), ("right", self._right_data)):
                    values = self._device_fields(data, category, name, state, side)
                    item = QTreeWidgetItem(list(values))
                    item.setData(0, Qt.UserRole, (category, name, state))
                    status_font = item.font(3)
                    status_font.setBold(True)
                    item.setFont(3, status_font)
                    device, enabled = _locate_device(data, category, name)
                    if device is None:
                        item.setToolTip(0, "Device is not present in this config")
                    else:
                        manager = str(device.get("managerName", "") or "(custom/unspecified)")
                        item.setToolTip(
                            2,
                            f"Manager: {manager}\nState: "
                            f"{'enabled' if enabled is not False else 'disabled'}",
                        )
                    roots[side].addChild(item)
                    self._items[side][(category, name)] = item

            for root in roots.values():
                root.setExpanded(True)

        if not any_rows:
            message = (
                "No differences"
                if self._diff_only.isChecked() and self._states
                else "No devices to compare"
            )
            for side in ("left", "right"):
                empty = QTreeWidgetItem([message])
                self._trees[side].addTopLevelItem(empty)
                self._trees[side].setFirstItemColumnSpanned(empty, True)

        if previous and previous in self._items["left"]:
            self._select_key(previous, focus_side=None)
        else:
            self._selected_key = None
            self._update_selection_controls()

    def _select_key(
        self, key: tuple[str, str], *, focus_side: str | None = None
    ) -> None:
        if key not in self._items["left"] or key not in self._items["right"]:
            return
        self._syncing_selection = True
        try:
            for side in ("left", "right"):
                tree = self._trees[side]
                item = self._items[side][key]
                tree.setCurrentItem(item)
                item.setSelected(True)
                if focus_side == side:
                    tree.scrollToItem(item)
        finally:
            self._syncing_selection = False
        self._selected_key = key
        self._update_selection_controls()

    def _selection_changed(self, side: str) -> None:
        if self._syncing_selection:
            return
        selected = self._trees[side].selectedItems()
        if not selected:
            return
        payload = selected[0].data(0, Qt.UserRole)
        if not payload:
            return
        category, name, _state = payload
        self._select_key((category, name), focus_side=side)

    def _item_clicked(self, side: str, item: QTreeWidgetItem) -> None:
        payload = item.data(0, Qt.UserRole)
        if not payload:
            return
        category, name, _state = payload
        pane = "primary" if side == "left" else "compare"
        data = self._left_data if pane == "primary" else self._right_data
        device, _enabled = _locate_device(data, category, name)
        if device is None:
            pane = "compare" if pane == "primary" else "primary"
        self.sig_device_selected.emit(category, name, pane)

    def _update_selection_controls(self) -> None:
        key = self._selected_key
        state = self._states.get(key) if key else None
        if not key or not state:
            self._selected_name_lbl.setText("Select a device")
            self._selected_status_lbl.setText("—")
            self._copy_right_btn.setEnabled(False)
            self._copy_left_btn.setEnabled(False)
            return

        category, name = key
        self._selected_name_lbl.setText(name)
        self._selected_name_lbl.setToolTip(CAT_LABEL.get(category, category))
        self._selected_status_lbl.setText(self._STATE_LABELS.get(state, state.upper()))
        self._copy_right_btn.setEnabled(state in {"different", "left_only"})
        self._copy_left_btn.setEnabled(state in {"different", "right_only"})

    def _copy_selected_right(self) -> None:
        if not self._selected_key:
            return
        category, name = self._selected_key
        state = self._states.get(self._selected_key)
        if state in {"different", "left_only"}:
            self.sig_copy_to_other.emit(category, name, "primary")

    def _copy_selected_left(self) -> None:
        if not self._selected_key:
            return
        category, name = self._selected_key
        state = self._states.get(self._selected_key)
        if state in {"different", "right_only"}:
            self.sig_copy_to_other.emit(category, name, "compare")

    def _on_selector_changed(self, index: int) -> None:
        path = str(self._selector.itemData(index) or "")
        if path:
            self.sig_config_requested.emit(path)


_MISSING = object()

#: A number as a setup file writes one: an optional sign, digits with an
#: optional fraction, an optional exponent. Nothing else -- no digit
#: separators (``int("1_000")`` is 1000), no ``nan``/``inf``, no hex -- so
#: what is read back is what the operator plainly typed and what JSON holds.
_INTEGER_TEXT = re.compile(r"[-+]?\d+")
_NUMBER_TEXT = re.compile(r"[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?")


def _parse_number(text: str):
    """What an edited numeric field means: an int, else a float, else the text.

    Empty is null -- clearing a box is how a nullable number is unset -- and
    text that is not a plain finite number is kept exactly as typed rather
    than lost or reinterpreted; Apply says so, and validation says what is
    wrong with it.
    """
    stripped = text.strip()
    if not stripped or stripped.lower() == "null":
        return None
    if _INTEGER_TEXT.fullmatch(stripped):
        return int(stripped)
    if _NUMBER_TEXT.fullmatch(stripped):
        number = float(stripped)
        if math.isfinite(number):
            return number
    return stripped


def _field_default(field_def: dict):
    """The value a field starts from when the file has no such key.

    Templates write a ``json`` field's default as JSON *text* (``"{}"``), and
    the widget must never guess whether a string is text-that-is-JSON or a
    string value -- that guess is how a saved ``"5"`` became ``5``. So the
    decoding happens here, once, on the template side of the boundary.
    """
    default = field_def.get("default", "")
    if field_def.get("type") == "json" and isinstance(default, str):
        if not default.strip():
            return {}
        try:
            return json.loads(default)
        except (ValueError, TypeError):
            return {}
    return default


_OPTION_ESCAPES = {"\\": "\\\\", "\r": "\\r", "\n": "\\n", "\t": "\\t"}


def _option_label(option) -> str:
    """How a select option reads in its combo box.

    A line ending is an option like any other, but a carriage return shown
    as itself is an invisible item. Control characters are written as their
    escapes, and so is a backslash: the two characters ``\\r`` an older
    editor saved must read differently from the carriage return it meant.
    """
    text = str(option)
    if not any(ch in _OPTION_ESCAPES or ord(ch) < 32 for ch in text):
        return text
    return "".join(
        _OPTION_ESCAPES.get(ch, f"\\x{ord(ch):02x}" if ord(ch) < 32 else ch)
        for ch in text
    )


def _field_option_label(field_def: dict, option) -> str:
    """Return an optional schema-supplied display label for a select value."""
    labels = field_def.get("option_labels") or {}
    return str(labels.get(str(option), _option_label(option)))


def _multiselect_item_text(field_def: dict, option) -> str:
    """How one multiselect choice reads: ``Display name  (id)`` when the
    schema names it, else the bare id. The id stays the stored value."""
    name = (field_def.get("option_labels") or {}).get(str(option))
    if name and name != str(option):
        return f"{name}  ({option})"
    return str(option)


def _parse_freeform_value(text: str):
    """Parse the value typed for a freeform field: JSON, then a Python
    literal, else the text itself as a string. Empty text is ``None``."""
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        try:
            return ast.literal_eval(stripped)
        except Exception:
            return stripped


# =============================================================================
# FieldWidget – single editable field row
# =============================================================================
class FieldWidget(QWidget):
    def __init__(self, field_def: dict, current_value, parent=None, device_pool: dict | None = None):
        super().__init__(parent)
        self._def = field_def
        # device_pool: {category_name: [device_name, ...]} — used to populate
        # ref-type combo boxes that reference devices in the live config.
        self._device_pool = device_pool or {}
        # The value as the file had it. Until the operator interacts with the
        # widget, get_value() returns exactly this object: a widget can alter a
        # value it merely displays -- a spin box clamps and rounds, a check box
        # turns None into False, a text box turns the string "null" into null
        # -- and none of that is an edit anyone made.
        self._original = current_value
        self._touched = False
        self._init_widget(current_value)
        self._watch_for_edits()

    def _init_widget(self, value):
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        tp = self._def["type"]
        if tp == "bool":
            self._w = QCheckBox()
            self._w.setChecked(bool(value) if value is not None else False)
        elif tp == "bool_auto":
            # Tri-state boolean: "Automatic" means the key stays ABSENT from
            # the saved config so the consumer's fallback applies (e.g.
            # smoothScan's device-name heuristic). Apply omits the key for
            # Automatic instead of writing a default.
            self._w = QComboBox()
            self._w.addItem("Automatic (not set)", None)
            self._w.addItem("On", True)
            self._w.addItem("Off", False)
            if value is None or value == "null":
                self._w.setCurrentIndex(0)
            else:
                self._w.setCurrentIndex(1 if bool(value) else 2)
        elif tp in ("int", "float"):
            # A plain line edit: no spin box, and no keystroke validator.
            # A spin box is a C++ int that clamps and rounds; a validator
            # drops every keystroke it refuses, so "8000.5" typed into an
            # integer-kind box became 80005 and was saved. The kind is the
            # editor's preference for how to read the text back, not a
            # constraint -- the schema deliberately emits none -- so what is
            # typed stays as typed, and Apply reads it as a number when it is
            # one and warns when it is not.
            self._w = QLineEdit("" if value is None else str(value))
        elif tp == "select":
            self._w = QComboBox()
            options = self._def.get("opts", [])
            if _coercion_module is not None:
                options = _coercion_module.options_like(options, value)
            for option in options:
                self._w.addItem(_field_option_label(self._def, option), option)
            # Configs can outlive their template/plugin version.  Keeping the
            # saved value selectable prevents an open-and-save cycle from
            # silently changing it to the first currently known option.
            idx = self._w.findData(value) if value is not None else -1
            if idx < 0 and value is not None:
                # A ``"9600"`` saved by an older editor should still land on
                # the 9600 entry rather than gaining a second, identical one.
                idx = self._w.findText(_option_label(value))
            if idx < 0 and value is not None:
                self._w.addItem(_option_label(value), value)
                idx = self._w.findData(value)
            if idx >= 0:
                self._w.setCurrentIndex(idx)
        elif tp == "multiselect":
            self._w = QListWidget()
            self._w.setMinimumHeight(92)
            self._w.setMaximumHeight(150)
            listed = value if isinstance(value, list) else []
            selected = {str(v) for v in listed}
            options = list(self._def.get("opts", []) or [])
            known = {str(option) for option in options}
            # Keep choices from newer templates/plugins visible and checked,
            # in the order the file listed them.
            unknown = []
            for selected_value in listed:
                if str(selected_value) not in known:
                    known.add(str(selected_value))
                    unknown.append(selected_value)
            options.extend(unknown)
            #: The order get_value() answers in: the schema's option order,
            #: then ids the file had that the schema does not offer.
            self._option_order = [str(option) for option in options]
            groups = self._def.get("option_groups") or {}
            if groups:
                self._fill_grouped(options, unknown, groups, selected)
            else:
                for opt in options:
                    self._w.addItem(self._choice_item(opt, selected))
        elif tp == "ref":
            # Cross-reference to a device in the live config. opts is the list
            # of categories ("detectors", "positioners", ...) to draw names from.
            self._w = QComboBox()
            self._w.setEditable(True)  # allow names that don't exist yet
            self._w.addItem("")
            seen: set = set()
            for cat in self._def.get("opts", []) or []:
                for name in self._device_pool.get(cat, []):
                    if name not in seen:
                        self._w.addItem(name)
                        seen.add(name)
            current = "" if value is None else str(value)
            if current and self._w.findText(current) < 0:
                self._w.addItem(current)  # preserve dangling reference
            idx = self._w.findText(current)
            if idx >= 0:
                self._w.setCurrentIndex(idx)
        elif tp == "json":
            # Free-form JSON value rendered as a single-line edit, parsed on
            # read. Used for dicts like scanDesignerParams, and for any
            # property no schema can type -- so it must round-trip *every*
            # value. A string shows quoted and None shows as null: showing them
            # bare made "5" come back as 5 and null come back as {}.
            self._w = QLineEdit(json.dumps(value))
        elif tp == "path":
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            self._w = QLineEdit(str(value) if value is not None else "")
            btn = QPushButton("…")
            btn.setFixedWidth(28)
            btn.clicked.connect(self._pick_path)
            rl.addWidget(self._w)
            rl.addWidget(btn)
            lay.addWidget(row)
            if self._def.get("tip"):
                self._w.setToolTip(self._def["tip"])
            return
        else:  # text
            if isinstance(value, list):
                display = ", ".join(str(v) for v in value)
            elif value is None:
                display = "null"
            else:
                display = str(value)
            self._w = QLineEdit(display)
        lay.addWidget(self._w)
        if self._def.get("tip"):
            self._w.setToolTip(self._def["tip"])

    def _choice_item(self, opt, selected: set) -> QListWidgetItem:
        item = QListWidgetItem(_multiselect_item_text(self._def, opt))
        # The id is what the file holds; the text is only how it reads.
        item.setData(Qt.UserRole, str(opt))
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked if str(opt) in selected else Qt.Unchecked)
        return item

    def _fill_grouped(self, options: list, unknown: list, groups: dict, selected: set) -> None:
        """Fill a multiselect under non-checkable category headers.

        Categories read alphabetically; ids the schema offers without a
        category come last under "Other", and ids only the file has under
        their own header, so nothing the file lists is hidden.
        """
        other, not_known = "Other", "Not known here (kept as written)"
        unknown_ids = {str(option) for option in unknown}
        buckets: dict[str, list] = {}
        for opt in options:
            if str(opt) in unknown_ids:
                name = not_known
            else:
                name = groups.get(str(opt)) or other
            buckets.setdefault(name, []).append(opt)
        trailing = (other, not_known)
        def reading_order(name: str):
            return (1, trailing.index(name)) if name in trailing else (0, name.lower())

        for name in sorted(buckets, key=reading_order):
            header = QListWidgetItem(name)
            header.setFlags(Qt.ItemIsEnabled)
            header.setData(Qt.UserRole, None)
            font = header.font()
            font.setBold(True)
            header.setFont(font)
            self._w.addItem(header)
            for opt in buckets[name]:
                self._w.addItem(self._choice_item(opt, selected))

    def fit_to_contents(self) -> None:
        """Show every row of a list field, so a page scrolls instead of the list."""
        if not isinstance(self._w, QListWidget):
            return
        height = sum(self._w.sizeHintForRow(i) for i in range(self._w.count()))
        height += 2 * self._w.frameWidth()
        self._w.setMinimumHeight(height)
        self._w.setMaximumHeight(height)
        self._w.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

    def _watch_for_edits(self):
        """Mark the field touched on user interaction, never on construction.

        Signals that also fire programmatically (``valueChanged``,
        ``itemChanged``) are connected only after the initial value is in
        place, so construction cannot trip them; ``textEdited``, ``clicked``
        and ``activated`` are user-only by contract.
        """
        def touch(*_args):
            self._touched = True

        w = self._w
        if isinstance(w, QCheckBox):
            w.clicked.connect(touch)
        elif isinstance(w, QComboBox):
            w.activated.connect(touch)
            if w.isEditable():
                w.lineEdit().textEdited.connect(touch)
        elif isinstance(w, QListWidget):
            w.itemChanged.connect(touch)
        elif isinstance(w, QLineEdit):
            w.textEdited.connect(touch)

    def is_touched(self) -> bool:
        """Whether the operator interacted with this field since it was built."""
        return self._touched

    def _pick_path(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select file")
        if path:
            self._w.setText(path)
            self._touched = True

    def get_value(self):
        if not self._touched:
            return self._original
        tp = self._def["type"]
        if tp == "bool":
            return self._w.isChecked()
        if tp == "bool_auto":
            return self._w.currentData()  # None (Automatic) / True / False
        if tp in ("int", "float"):
            return _parse_number(self._w.text())
        if tp == "select":
            return self._w.currentData()
        if tp == "multiselect":
            checked = [
                self._w.item(i).data(Qt.UserRole)
                for i in range(self._w.count())
                if self._w.item(i).data(Qt.UserRole) is not None
                and self._w.item(i).checkState() == Qt.Checked
            ]
            rank = {value: n for n, value in enumerate(self._option_order)}
            return sorted(checked, key=lambda value: rank[value])
        if tp == "ref":
            txt = self._w.currentText().strip()
            return txt if txt else ""
        if tp == "json":
            txt = self._w.text().strip()
            if not txt or txt.lower() == "null":
                return None
            try:
                return json.loads(txt)
            except json.JSONDecodeError:
                return txt  # caller validates; preserve raw on error
        # text / path
        txt = self._w.text().strip()
        if tp == "path" or _coercion_module is None:
            if txt.lower() == "null":
                return None
            return txt
        return _coercion_module.text_to_json(txt, self._original, tp)


# =============================================================================
# PropertyEditor – right panel
# =============================================================================
class PropertyEditor(QWidget):
    sig_apply = pyqtSignal(str, str, dict)   # (category, name, new_device_dict)
    sig_rename = pyqtSignal(str, str, str)    # (category, old_name, new_name)
    sig_enabled_changed = pyqtSignal(str, str, bool)  # requested enabled state
    sig_modified = pyqtSignal()               # any change to config settings (widgets, sections)
    sig_show_config = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(320)
        self._cat = ""
        self._name = ""
        self._device = {}
        self._data: dict = {}                 # Reference to full config data for config settings view
        self._setup: dict = {}                # The document the loaded device belongs to
        self._field_widgets: dict[tuple, FieldWidget] = {}   # (section, key) → widget
        self._enabled = True
        self._block_enabled = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        # ── Header ──
        hdr = QFrame()
        colors = get_themed_colors(is_dark_mode())
        hdr.setStyleSheet(f"background:{colors['header_bg']}; border-radius:4px;")
        hdr_lay = QVBoxLayout(hdr)
        hdr_lay.setContentsMargins(8, 6, 8, 6)
        hdr_lay.setSpacing(4)

        row1 = QHBoxLayout()
        self._name_lbl = QLabel("<i>No device selected</i>")
        self._name_lbl.setTextFormat(Qt.RichText)
        bold = QFont()
        bold.setBold(True)
        bold.setPointSize(10)
        self._name_lbl.setFont(bold)
        row1.addWidget(self._name_lbl)
        row1.addStretch()
        self._rename_btn = QPushButton("Rename…")
        self._rename_btn.setFixedHeight(22)
        self._rename_btn.setStyleSheet("font-size:8pt;")
        self._rename_btn.clicked.connect(self._do_rename)
        row1.addWidget(self._rename_btn)
        self._close_device_btn = QPushButton("Config Settings")
        self._close_device_btn.setFixedHeight(22)
        self._close_device_btn.setStyleSheet("font-size:8pt;")
        self._close_device_btn.setToolTip("Close device details and return to general config settings")
        self._close_device_btn.clicked.connect(self.sig_show_config.emit)
        row1.addWidget(self._close_device_btn)
        hdr_lay.addLayout(row1)

        self._enabled_cb = QCheckBox("Enabled")
        self._enabled_cb.setToolTip(
            "Disabled devices stay in this setup file but are not instantiated by ImSwitch."
        )
        self._enabled_cb.toggled.connect(self._on_enabled_toggled)
        hdr_lay.addWidget(self._enabled_cb)

        # Manager type: combo for known schemas + line-edit for custom/free-form
        self._mgr_combo = QComboBox()
        self._mgr_combo.addItem("— Custom / Free-form —", "__custom__")
        for mgr in _all_known_managers():
            self._mgr_combo.addItem(_manager_display_name(mgr), mgr)
        self._mgr_combo.currentIndexChanged.connect(self._on_manager_changed)
        hdr_lay.addWidget(self._mgr_combo)

        self._custom_mgr_edit = QLineEdit()
        self._custom_mgr_edit.setPlaceholderText("Manager class name (e.g. MyCustomManager)…")
        self._custom_mgr_edit.setVisible(False)
        hdr_lay.addWidget(self._custom_mgr_edit)
        outer.addWidget(hdr)
        self._hdr = hdr

        # ── Tab widget (populated dynamically) ──
        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)
        outer.addWidget(self._tabs, 1)

        # ── Validation label ──
        self._val_lbl = QLabel()
        self._val_lbl.setWordWrap(True)
        self._val_lbl.setStyleSheet("color:#C06000; font-size:8pt;")
        outer.addWidget(self._val_lbl)

        # ── Add custom field buttons (for device view) ──
        self._add_row_widget = QWidget(self)
        add_row = QHBoxLayout(self._add_row_widget)
        add_row.setContentsMargins(0, 0, 0, 0)
        self._add_top_btn = QPushButton("⊕ Add field")
        self._add_top_btn.setFixedHeight(22)
        self._add_top_btn.setStyleSheet("font-size:8pt; padding:0 6px;")
        self._add_top_btn.setToolTip("Add an arbitrary top-level field to this device")
        self._add_top_btn.clicked.connect(lambda: self._add_custom_field("top"))
        add_row.addWidget(self._add_top_btn)
        self._add_prop_btn = QPushButton("⊕ Add property")
        self._add_prop_btn.setFixedHeight(22)
        self._add_prop_btn.setStyleSheet("font-size:8pt; padding:0 6px;")
        self._add_prop_btn.setToolTip("Add an arbitrary field inside managerProperties")
        self._add_prop_btn.clicked.connect(lambda: self._add_custom_field("props"))
        add_row.addWidget(self._add_prop_btn)
        outer.addWidget(self._add_row_widget)

        # ── Apply button (for device view) ──
        self._apply_btn = QPushButton("Apply Changes")
        self._apply_btn.setStyleSheet("""
            QPushButton {
                background:#3A7FC1; color:white; border-radius:4px;
                padding:6px; font-size:10pt;
            }
            QPushButton:hover { background:#2A6FAF; }
        """)
        self._apply_btn.clicked.connect(self._do_apply)
        outer.addWidget(self._apply_btn)

        # ── Config Settings view (for when no device is selected) ──
        self._config_view = self._build_config_settings_view()
        outer.addWidget(self._config_view)

        self._block_combo = False

        # Show config settings view initially
        self._show_config_view()

    def _build_config_settings_view(self) -> QWidget:
        """Build the Config Settings view (shown when no device is selected)."""
        view = QWidget()
        lay = QVBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setFrameShape(QFrame.NoFrame)

        inner = QWidget()
        inner_lay = QVBoxLayout(inner)
        inner_lay.setContentsMargins(0, 0, 0, 0)
        inner_lay.setSpacing(16)

        # ── Available Widgets section ──
        w_section = QFrame()
        colors = get_themed_colors(is_dark_mode())
        w_section.setStyleSheet(f"QFrame {{ background:{colors['card_bg']}; border:1px solid {colors['card_border']}; border-radius:4px; }}")
        w_lay = QVBoxLayout(w_section)
        w_lay.setContentsMargins(10, 10, 10, 10)
        w_lay.setSpacing(8)

        w_hdr = QLabel("<b>Available Widgets</b>")
        w_hdr.setTextFormat(Qt.RichText)
        w_lay.addWidget(w_hdr)

        # Widget chips container (uses FlowLayout for wrapping)
        self._widgets_inner = QWidget()
        self._widgets_lay = FlowLayout(self._widgets_inner)
        self._widgets_lay.setContentsMargins(0, 0, 0, 0)
        self._widgets_lay.setSpacing(4)
        w_lay.addWidget(self._widgets_inner)

        # Edit widgets button
        edit_w_btn = QPushButton("Edit Widgets…")
        edit_w_btn.setFixedHeight(26)
        edit_w_btn.setToolTip("Choose which widgets are available")
        edit_w_btn.setStyleSheet(
            f"QPushButton {{ font-size:8pt; padding:0 8px; border:1px solid {colors['chip_border']}; "
            f"border-radius:3px; background:{colors['button_bg']}; }}"
            f"QPushButton:hover {{ background:{colors['button_bg_hover']}; }}"
        )
        edit_w_btn.clicked.connect(self._open_widget_picker)
        w_lay.addWidget(edit_w_btn)

        inner_lay.addWidget(w_section)

        # ── System Sections (schema-driven singletons) ──
        sys_section = QFrame()
        sys_section.setStyleSheet(f"QFrame {{ background:{colors['card_bg']}; border:1px solid {colors['card_border']}; border-radius:4px; }}")
        sys_lay = QVBoxLayout(sys_section)
        sys_lay.setContentsMargins(10, 10, 10, 10)
        sys_lay.setSpacing(8)

        sys_hdr = QLabel("<b>System</b> <span style='color:#888;font-size:8pt;'>— scan, focusLock, nidaq, …</span>")
        sys_hdr.setTextFormat(Qt.RichText)
        sys_hdr.setToolTip(
            "Integral system sections referenced by detector/positioner flags "
            "(forFocusLock, forScanning) and by feature widgets (FocusLock, "
            "Tiling, EtSTED, …). Use the structured editor here instead of "
            "raw JSON."
        )
        sys_lay.addWidget(sys_hdr)

        self._sys_sections_inner = QWidget()
        self._sys_sections_lay = QVBoxLayout(self._sys_sections_inner)
        self._sys_sections_lay.setContentsMargins(0, 0, 0, 0)
        self._sys_sections_lay.setSpacing(4)
        sys_lay.addWidget(self._sys_sections_inner)

        inner_lay.addWidget(sys_section)

        # ── Extras (anything still in the file with no schema) ──
        s_section = QFrame()
        s_section.setStyleSheet(f"QFrame {{ background:{colors['card_bg']}; border:1px solid {colors['card_border']}; border-radius:4px; }}")
        s_lay = QVBoxLayout(s_section)
        s_lay.setContentsMargins(10, 10, 10, 10)
        s_lay.setSpacing(8)

        s_hdr = QLabel("<b>Extras</b> <span style='color:#888;font-size:8pt;'>— rois, laserPresets, widgetLayout, …</span>")
        s_hdr.setTextFormat(Qt.RichText)
        s_hdr.setToolTip(
            "Sections without a structured schema. Opens a raw-JSON editor."
        )
        s_lay.addWidget(s_hdr)

        # Section buttons container
        self._sections_inner = QWidget()
        self._sections_lay = QVBoxLayout(self._sections_inner)
        self._sections_lay.setContentsMargins(0, 0, 0, 0)
        self._sections_lay.setSpacing(4)
        s_lay.addWidget(self._sections_inner)

        inner_lay.addWidget(s_section)
        inner_lay.addStretch()

        scroll.setWidget(inner)
        lay.addWidget(scroll)

        return view

    def _show_config_view(self):
        """Switch to Config Settings view (hide device form)."""
        self._name_lbl.setText("Config Settings")
        self._hdr.setVisible(True)
        self._rename_btn.setVisible(False)
        self._close_device_btn.setVisible(False)
        self._mgr_combo.setVisible(False)
        self._custom_mgr_edit.setVisible(False)
        self._enabled_cb.setVisible(False)
        self._tabs.setVisible(False)
        self._val_lbl.setVisible(False)
        self._add_row_widget.setVisible(False)
        self._apply_btn.setVisible(False)
        self._config_view.setVisible(True)

    def _show_device_view(self):
        """Switch to device editor view (hide config settings)."""
        self._rename_btn.setVisible(True)
        self._close_device_btn.setVisible(True)
        self._mgr_combo.setVisible(True)
        self._enabled_cb.setVisible(self._cat in DISABLEABLE_DEVICE_CATS)
        self._tabs.setVisible(True)
        self._val_lbl.setVisible(True)
        self._add_row_widget.setVisible(True)
        self._apply_btn.setVisible(True)
        self._config_view.setVisible(False)

    def load_config_extras(self, data: dict):
        """Load config data and show Config Settings view."""
        self._data = data
        self._refresh_widgets()
        self._refresh_sections()
        self._show_config_view()

    def load_device(self, cat: str, name: str, device: dict, setup: dict | None = None, *, enabled: bool = True):
        self._cat = cat
        self._name = name
        self._device = copy.deepcopy(device)
        self.set_device_enabled(enabled)
        # The whole document, for role predicates (``scan.scanDesigner``).
        self._setup = setup if setup is not None else self._data
        self._name_lbl.setText(name)

        # Switch to device view
        self._show_device_view()

        mgr = device.get("managerName", "")
        self._block_combo = True
        idx = self._mgr_combo.findData(mgr)
        if idx >= 0:
            self._mgr_combo.setCurrentIndex(idx)
            self._custom_mgr_edit.setVisible(False)
        else:
            self._mgr_combo.setCurrentIndex(0)          # "Custom / Free-form"
            self._custom_mgr_edit.setText(mgr)
            self._custom_mgr_edit.setVisible(True)
        self._block_combo = False

        self._rebuild_form()

    def set_device_enabled(self, enabled: bool):
        """Update the enabled checkbox without requesting another state change."""
        self._enabled = bool(enabled)
        self._block_enabled = True
        self._enabled_cb.setChecked(self._enabled)
        self._block_enabled = False

    def _on_enabled_toggled(self, enabled: bool):
        if self._block_enabled or self._cat not in DISABLEABLE_DEVICE_CATS:
            return
        self.sig_enabled_changed.emit(self._cat, self._name, bool(enabled))

    def _on_manager_changed(self, _):
        if self._block_combo:
            return
        if not self._cat:
            return
        new_mgr = self._mgr_combo.currentData()
        is_custom = new_mgr == "__custom__"
        self._custom_mgr_edit.setVisible(is_custom)
        if is_custom:
            return  # user types the manager name; rebuild happens on Apply
        if new_mgr and new_mgr != self._device.get("managerName"):
            default = _build_default_device(new_mgr)
            default["managerName"] = new_mgr
            self._device = default
            self._rebuild_form()

    def _rebuild_form(self):
        self._tabs.clear()
        self._field_widgets.clear()
        # What the file had, so Apply can leave alone what it did not have:
        # presence of an optional key changes a manager's behaviour, and an
        # aliased key is written back under the spelling it was found in.
        self._present_keys: set = set()
        self._field_defs: dict = {}
        self._alias_spelling: dict = {}

        raw_mgr = self._mgr_combo.currentData()
        if raw_mgr == "__custom__":
            mgr = self._custom_mgr_edit.text().strip() or self._device.get("managerName", "")
        else:
            mgr = raw_mgr or self._device.get("managerName", "")
        schema = _with_role_fields(_schema_for_manager(mgr), self._cat, self._name, self._device, self._setup)
        props = self._device.get("managerProperties") or {}

        # Collect fields grouped by grp
        groups: dict[str, list] = {}
        all_fields = []
        for f in schema.get("top", []):
            all_fields.append(("top", f))
        for f in schema.get("props", []):
            all_fields.append(("props", f))
        for nest_key, nest_fields in schema.get("nested", {}).items():
            for f in nest_fields:
                all_fields.append((f"nested:{nest_key}", f))

        for section, f in all_fields:
            grp = f["grp"]
            groups.setdefault(grp, []).append((section, f))

        # Ensure "Basic" is first, "Advanced" is last
        ordered_groups = sorted(groups.keys(),
                                key=lambda g: (0 if g == "Basic" else
                                               2 if g == "Advanced" else 1))

        for grp in ordered_groups:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            inner = QWidget()
            form = QFormLayout(inner)
            form.setContentsMargins(8, 8, 8, 8)
            form.setSpacing(6)
            form.setLabelAlignment(Qt.AlignRight)

            for section, f in groups[grp]:
                spelling = f["key"]
                if section == "top":
                    current = self._device.get(f["key"], _MISSING)
                elif section == "props":
                    current = props.get(f["key"], _MISSING)
                    if current is _MISSING:
                        # Saved under an alias spelling: load it from there.
                        for alias in f.get("aliases") or []:
                            if alias in props:
                                current, spelling = props[alias], alias
                                break
                else:
                    nest_key = section.split(":", 1)[1]
                    container = props.get(nest_key)
                    current = container.get(f["key"], _MISSING) if isinstance(container, dict) else _MISSING
                if current is _MISSING:
                    current = _field_default(f)
                else:
                    self._present_keys.add((section, f["key"]))
                self._field_defs[(section, f["key"])] = f
                self._alias_spelling[(section, f["key"])] = spelling

                fw = FieldWidget(f, current)
                lbl = f["label"]
                if f["req"]:
                    lbl = f"<b>{lbl}</b> *"
                row_label = QLabel(lbl)
                row_label.setTextFormat(Qt.RichText)
                row_label.setToolTip(f.get("tip", ""))
                form.addRow(row_label, fw)
                self._field_widgets[(section, f["key"])] = fw

            scroll.setWidget(inner)
            self._tabs.addTab(scroll, grp)

        # Unknown managerProperties not covered by schema (shown in "Properties" tab)
        unknown_props, unknown_top = self._collect_unknown(schema)

        def _make_raw_tab(items, section_key, tab_label):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            inner = QWidget()
            form = QFormLayout(inner)
            form.setContentsMargins(8, 8, 8, 8)
            form.setSpacing(4)
            for k, v in items.items():
                display = json.dumps(v) if not isinstance(v, str) else v
                le = QLineEdit(display)
                # Bare text cannot say whether "5" was a string or a number;
                # the read-back asks the original rather than guessing.
                le.setProperty("originalValue", v)
                form.addRow(QLabel(k), le)
                self._field_widgets[(section_key, k)] = le
            scroll.setWidget(inner)
            self._tabs.addTab(scroll, tab_label)

        if unknown_props:
            _make_raw_tab(unknown_props, "raw_prop", "Properties")
        if unknown_top:
            _make_raw_tab(unknown_top, "raw", "Other")

    def _collect_unknown(self, schema: dict):
        """Return (unknown_props dict, unknown_top dict) — fields not covered by schema."""
        known_top = {f["key"] for f in schema.get("top", [])}
        known_top |= {"managerName", "managerProperties"}
        unknown_top = {k: v for k, v in self._device.items() if k not in known_top}

        known_props = {f["key"] for f in schema.get("props", [])}
        for f in schema.get("props", []):
            known_props |= set(f.get("aliases") or [])
        known_props |= set(schema.get("nested", {}).keys())
        props = self._device.get("managerProperties") or {}
        # Expose scalar/list props; leave nested dicts collapsed in JSON
        unknown_props = {
            k: v for k, v in props.items()
            if k not in known_props and not isinstance(v, dict)
        }
        return unknown_props, unknown_top

    def _do_rename(self):
        if not self._cat:
            return
        new_name, ok = QInputDialog.getText(
            self, "Rename Device", "New name:", text=self._name
        )
        if ok and new_name.strip() and new_name.strip() != self._name:
            self.sig_rename.emit(self._cat, self._name, new_name.strip())
            self._name = new_name.strip()
            self._name_lbl.setText(self._name)

    def _do_apply(self):
        if not self._cat:
            return
        raw_mgr = self._mgr_combo.currentData()
        if raw_mgr == "__custom__":
            mgr = self._custom_mgr_edit.text().strip() or self._device.get("managerName", "")
        else:
            mgr = raw_mgr or self._device.get("managerName", "")
        schema = _schema_for_manager(mgr)
        new_device: dict = {"managerName": mgr, "managerProperties": {}}
        props = new_device["managerProperties"]

        nested_keys: dict[str, dict] = {}
        for nest_key in schema.get("nested", {}):
            nested_keys[nest_key] = {}
        # Numeric fields the operator filled with something that is not a
        # number: saved as typed, and said so below.
        not_numbers: list[tuple[str, str]] = []
        props_on_load = self._device.get("managerProperties") or {}
        nested_meta = schema.get("nested_meta", {})
        # Dicts an edit to one of their fields creates (or turns into a dict).
        edited_containers = {
            section.split(":", 1)[1]
            for (section, _key), fw in self._field_widgets.items()
            if section.startswith("nested:") and fw.is_touched()
        }

        def container_exists(nest_key: str) -> bool:
            """The dict will be in what Apply writes: the file has it, the code
            requires it, or an edit creates it.

            Decided before any field is written, so a sub-key the dict's
            schema requires goes in with the edit that creates the dict --
            not on the next Apply. Something under that key that is not a
            dict (``null``) and that nobody edited is kept as it was rather
            than filled in.
            """
            if nest_key in edited_containers:
                return True
            if nest_key in props_on_load:
                return isinstance(props_on_load[nest_key], dict)
            return bool(nested_meta.get(nest_key, {}).get("schema_req"))

        for (section, key), fw in self._field_widgets.items():
            if section in ("raw", "raw_prop"):
                raw_val = fw.text().strip()  # type: ignore[attr-defined]
                original = fw.property("originalValue")  # type: ignore[attr-defined]
                if _coercion_module is not None:
                    value = _coercion_module.raw_text_to_json(raw_val, original)
                else:
                    try:
                        value = json.loads(raw_val)
                    except Exception:
                        value = raw_val
                if section == "raw_prop":
                    props[key] = value
                else:
                    new_device[key] = value
                continue
            val = fw.get_value()
            field = self._field_defs.get((section, key), {})
            if fw.is_touched() and field.get("type") in ("int", "float") and isinstance(val, str):
                not_numbers.append((field.get("label", key), val))
            # A property the file did not have is written only if the
            # manager's code requires it or the operator edited it; a form
            # may show a default without saving one unasked. A template's
            # own "req" is a hint for the label and the warning below, not
            # proof: the file loaded without the key. A sub-key a dict's
            # schema requires is required only where the dict exists.
            required_here = bool(field.get("schema_req"))
            if section.startswith("nested:"):
                required_here = required_here and container_exists(section.split(":", 1)[1])
            if not (required_here or (section, key) in self._present_keys or fw.is_touched()):
                continue
            if fw._def.get("type") == "bool_auto" and val is None:
                # "Automatic": keep the key absent so the consumer's own
                # fallback applies (never write a default for it).
                continue
            if section == "props":
                key = self._alias_spelling.get((section, key), key)
            if section == "top":
                if key == "axes":
                    # Axes stored as array in JSON
                    if isinstance(val, str):
                        val = [a.strip() for a in val.split(",") if a.strip()]
                elif key == "digitalPorts":
                    if isinstance(val, str):
                        val = [p.strip() for p in val.split(",") if p.strip()]
                new_device[key] = val
            elif section == "props":
                props[key] = val
            else:
                nest_key = section.split(":", 1)[1]
                nested_keys[nest_key][key] = val

        for nest_key, nest_vals in nested_keys.items():
            original = props_on_load.get(nest_key, _MISSING)
            if nest_vals:
                props[nest_key] = nest_vals
            elif original is not _MISSING:
                # The container was there with nothing the form knows inside
                # (empty, or not a dict at all): keep it exactly as it was.
                props[nest_key] = copy.deepcopy(original)
            elif container_exists(nest_key):
                # The manager reads this dict unguarded -- a file without it
                # does not start. Its fields show defaults but stay unwritten,
                # like any optional field the file lacks; the dict is written
                # with only what its own schema requires, which here is nothing.
                props[nest_key] = {}

        # A device the file wrote without a managerProperties key, and into
        # which nothing was written now, stays without one.
        if not props and "managerProperties" not in self._device:
            del new_device["managerProperties"]

        # Validate required fields
        warnings = []
        for f in schema.get("top", []) + schema.get("props", []):
            if f["req"]:
                v = new_device.get(f["key"]) if f in schema.get("top", []) else props.get(f["key"])
                if v is None or v == "" or v == []:
                    warnings.append(f"⚠  Required: {f['label']}")
        # The same for the fields of a dict the template lays out, where the
        # dict is in what is written.
        for nest_key, children in schema.get("nested", {}).items():
            container = props.get(nest_key)
            if not isinstance(container, dict):
                continue
            for f in children:
                if f.get("req"):
                    v = container.get(f["key"])
                    if v is None or v == "" or v == []:
                        warnings.append(f"⚠  Required: {f['label']} ({nest_key})")
        for label, text in not_numbers:
            warnings.append(f"⚠  {label}: {text!r} is not a number; saved as typed")
        self._val_lbl.setText("\n".join(warnings))

        # Phase 2: Merge preserving unknown fields (including nested dicts)
        try:
            from imswitch.imcontrol.model.configeditor.defaults import merge_preserving_unknown
            
            # Build known keys sets from schema
            schema_top_keys = {f["key"] for f in schema.get("top", [])}
            schema_prop_keys = {f["key"] for f in schema.get("props", [])}
            schema_prop_keys |= set(schema.get("nested", {}).keys())
            schema_nested_prop_keys = {
                nest_key: {f["key"] for f in fields}
                for nest_key, fields in schema.get("nested", {}).items()
            }
            
            # Merge to restore unknown fields
            new_device = merge_preserving_unknown(
                self._device,
                new_device,
                schema_top_keys=schema_top_keys,
                schema_prop_keys=schema_prop_keys,
                schema_nested_prop_keys=schema_nested_prop_keys,
            )
        except ImportError:
            # Phase 2 module unavailable - continue without merge (legacy behavior)
            pass

        self.sig_apply.emit(self._cat, self._name, new_device)

    def _add_custom_field(self, where: str):
        """Prompt for a key+value and inject it into the in-memory device, then rebuild."""
        if not self._cat:
            return
        key, ok = QInputDialog.getText(self, "Add Field", "Key name:")
        if not ok or not key.strip():
            return
        val_str, ok2 = QInputDialog.getText(
            self, "Add Field",
            'Value  (JSON: 42, true, "text", null, [1,2], {...}):',
            text="null",
        )
        if not ok2:
            return
        value = _parse_freeform_value(val_str)

        if where == "top":
            self._device[key.strip()] = value
        else:
            if not isinstance(self._device.get("managerProperties"), dict):
                self._device["managerProperties"] = {}
            self._device["managerProperties"][key.strip()] = value
        self._rebuild_form()

    # ── Widget chips (from ConfigExtrasBar) ──────────────────────────────────

    def _refresh_widgets(self):
        """Refresh the widget chips display."""
        while self._widgets_lay.count():
            item = self._widgets_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for w in (self._data.get("availableWidgets") or []):
            self._widgets_lay.addWidget(self._make_chip(str(w)))

    def _make_chip(self, name: str) -> QWidget:
        """Create a widget chip with delete button."""
        chip = QFrame()
        colors = get_themed_colors(is_dark_mode())
        chip.setStyleSheet(
            f"QFrame {{ background:{colors['chip_bg']}; border:1px solid {colors['chip_border']}; "
            "border-radius:3px; }"
        )
        lay = QHBoxLayout(chip)
        lay.setContentsMargins(5, 1, 2, 1)
        lay.setSpacing(2)
        lbl = QLabel(name)
        lbl.setStyleSheet("font-size:8pt; border:none; background:transparent;")
        lay.addWidget(lbl)
        # Use Qt's close icon instead of a font glyph.  The old multiplication
        # sign rendered like a small down-arrow with some platform fonts, which
        # made this destructive action unnecessarily ambiguous.
        del_btn = QPushButton()
        del_btn.setIcon(self.style().standardIcon(QStyle.SP_TitleBarCloseButton))
        del_btn.setIconSize(QSize(10, 10))
        del_btn.setFixedSize(18, 18)
        del_btn.setToolTip(f"Remove widget '{name}'")
        del_btn.setAccessibleName(f"Remove widget {name}")
        del_btn.setStyleSheet(
            "QPushButton { border:none; background:transparent; padding:0; }"
            "QPushButton:hover { background:rgba(200, 40, 40, 45); border-radius:3px; }"
        )
        del_btn.clicked.connect(lambda _, n=name: self._remove_widget(n))
        lay.addWidget(del_btn)
        return chip

    def _open_widget_picker(self):
        """Open the widget picker dialog."""
        current = list(self._data.get("availableWidgets") or [])
        dlg = WidgetPickerDialog(current, self)
        if dlg.exec_() == QDialog.Accepted:
            previous = set(current)
            new_widgets = dlg.selected_widgets()
            self._data["availableWidgets"] = new_widgets

            # Auto-offer section configuration for newly-enabled widgets
            newly_enabled = set(new_widgets) - previous
            for widget in newly_enabled:
                section_key = _WIDGET_REQUIRES_SECTION.get(widget)
                if section_key and self._data.get(section_key) in (None, {}, []):
                    schema = SECTION_SCHEMAS.get(section_key)
                    if not schema:
                        continue
                    display = schema.get("display", section_key)
                    reply = QMessageBox.question(
                        self,
                        "Configure required section",
                        f"The '{widget}' widget needs a '{display}' section to initialize.\n\n"
                        f"Configure it now?",
                        QMessageBox.Yes | QMessageBox.No,
                        QMessageBox.Yes
                    )
                    if reply == QMessageBox.Yes:
                        if section_key == IMPROCESS_SECTION_KEY:
                            applied = _edit_improcess_section(self._data, self)
                        else:
                            defaults = _new_section_seed(schema)
                            editor = SectionEditorDialog(section_key, schema, defaults, self._data, self)
                            applied = editor.exec_() == QDialog.Accepted
                            if applied:
                                self._data[section_key] = editor.result_data
                        if applied:
                            self._refresh_sections()
                            self.sig_modified.emit()

            self._refresh_widgets()
            self.sig_modified.emit()

    def _remove_widget(self, name: str):
        """Remove a widget from the available widgets list."""
        widgets = list(self._data.get("availableWidgets") or [])
        if name in widgets:
            widgets.remove(name)
            self._data["availableWidgets"] = widgets

            # Optionally remove orphan section if widget owns it
            section_key = _WIDGET_REQUIRES_SECTION.get(name)
            if section_key and self._data.get(section_key) not in (None, {}, []):
                schema = SECTION_SCHEMAS.get(section_key)
                display = schema.get("display", section_key) if schema else section_key
                reply = QMessageBox.question(
                    self,
                    "Remove orphan section?",
                    f"The '{name}' widget is paired with the '{display}' section.\n\n"
                    f"Remove the '{display}' section as well?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No
                )
                if reply == QMessageBox.Yes:
                    self._remove_section(section_key)

            self._refresh_widgets()
            self.sig_modified.emit()

    # ── Section buttons (from ConfigExtrasBar) ───────────────────────────────

    def _refresh_sections(self):
        """Refresh the System + Extras section listings.

        System: one row per *currently configured* section schema, plus an
        ＋ Add picker for the remaining schemas.
        Extras: top-level keys still in the file that aren't devices,
        availableWidgets, or covered by a section schema; plus an ＋ Add
        button for arbitrary new keys.
        """
        for layout in (self._sys_sections_lay, self._sections_lay):
            while layout.count():
                item = layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()

        colors = get_themed_colors(is_dark_mode())
        excluded_extras = (
            set(DEVICE_CATS)
            | {"availableWidgets", "deviceCategorySettings", DISABLED_DEVICES_KEY}
            | set(SECTION_SCHEMAS.keys())
        )

        # ── System: only configured sections, sorted; + Add at the end ──
        # Sections with their own place on the canvas (ImProcess) are not here.
        system_keys = [k for k in sorted(SECTION_SCHEMAS.keys()) if k not in _CANVAS_SECTIONS]
        configured = [
            k for k in system_keys
            if k in self._data and self._data[k] not in (None, {}, [])
        ]
        if configured:
            for key in configured:
                self._sys_sections_lay.addWidget(
                    self._make_system_row(key, SECTION_SCHEMAS[key], True, colors)
                )
        else:
            empty = QLabel(
                "<span style='color:#888;font-size:8pt;'>No system sections "
                "configured yet.</span>"
            )
            empty.setTextFormat(Qt.RichText)
            self._sys_sections_lay.addWidget(empty)

        unconfigured = [k for k in system_keys if k not in configured]
        if unconfigured:
            add_sys_btn = QPushButton("＋ Add System Section…")
            add_sys_btn.setFixedHeight(26)
            add_sys_btn.setStyleSheet(
                f"QPushButton {{ font-size:8pt; padding:0 8px; border:1px solid "
                f"{colors['chip_border']}; border-radius:3px; "
                f"background:{colors['button_bg']}; }}"
                f"QPushButton:hover {{ background:{colors['button_bg_hover']}; }}"
            )
            add_sys_btn.setToolTip(
                f"{len(unconfigured)} section(s) available: "
                + ", ".join(unconfigured)
            )
            add_sys_btn.clicked.connect(self._open_section_picker)
            self._sys_sections_lay.addWidget(add_sys_btn)

        # ── Extras: unknown sections, raw-JSON fallback ──
        extras_keys = [k for k in self._data.keys() if k not in excluded_extras]
        for key in extras_keys:
            btn = QPushButton(key)
            btn.setFixedHeight(26)
            btn.setStyleSheet(
                f"QPushButton {{ font-size:8pt; padding:0 8px; border:1px solid {colors['chip_border']}; "
                f"border-radius:3px; background:{colors['button_bg']}; }}"
                f"QPushButton:hover {{ background:{colors['button_bg_hover']}; }}"
            )
            btn.setToolTip(f"View / edit '{key}' section (raw JSON)")
            btn.clicked.connect(lambda _, k=key: self._open_section_raw(k))
            self._sections_lay.addWidget(btn)

        # Always show ＋ Add custom for free-form keys
        add_extra_btn = QPushButton("＋ Add Custom Section…")
        add_extra_btn.setFixedHeight(26)
        add_extra_btn.setStyleSheet(
            f"QPushButton {{ font-size:8pt; padding:0 8px; border:1px solid "
            f"{colors['chip_border']}; border-radius:3px; "
            f"background:{colors['button_bg']}; }}"
            f"QPushButton:hover {{ background:{colors['button_bg_hover']}; }}"
        )
        add_extra_btn.setToolTip(
            "Add a new arbitrary top-level key with a free-form JSON value."
        )
        add_extra_btn.clicked.connect(self._add_custom_section)
        self._sections_lay.addWidget(add_extra_btn)

    def _make_system_row(self, key: str, schema: dict, present: bool, colors: dict) -> QWidget:
        """Build one row for the System group: name, status, Edit/Add, Remove."""
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(6)

        legacy = bool(schema.get("legacy"))
        display = schema.get("display", key)
        status = "✓" if present else "+"
        status_color = "#4CAF50" if present else "#888"
        label_html = (
            f"<span style='color:{status_color};'>{status}</span> "
            f"<b>{display}</b> "
            f"<span style='color:#888;font-size:8pt;'>({key})</span>"
        )
        if legacy:
            label_html += " <span style='color:#C04000;font-size:8pt;'>[legacy]</span>"
        lbl = QLabel(label_html)
        lbl.setTextFormat(Qt.RichText)
        tip = schema.get("summary", "")
        if legacy and schema.get("legacy_note"):
            tip = (tip + "\n\n" + schema["legacy_note"]).strip()
        if tip:
            lbl.setToolTip(tip)
        rl.addWidget(lbl, 1)

        # All rows here are present (the System group only shows configured
        # sections now); ＋ Add is handled by the picker below.
        edit_btn = QPushButton("Edit…")
        edit_btn.setFixedHeight(22)
        edit_btn.setStyleSheet(
            f"QPushButton {{ font-size:8pt; padding:0 8px; border:1px solid {colors['chip_border']}; "
            f"border-radius:3px; background:{colors['button_bg']}; }}"
            f"QPushButton:hover {{ background:{colors['button_bg_hover']}; }}"
        )
        edit_btn.clicked.connect(lambda _, k=key: self._open_section_schema(k))
        rl.addWidget(edit_btn)

        if present:
            rm_btn = QPushButton("×")
            rm_btn.setFixedSize(22, 22)
            rm_btn.setToolTip(f"Remove '{key}' from the config")
            rm_btn.setStyleSheet(
                "QPushButton { border:none; color:#888; background:transparent; font-size:11pt; }"
                "QPushButton:hover { color:#C00; }"
            )
            rm_btn.clicked.connect(lambda _, k=key: self._remove_section(k))
            rl.addWidget(rm_btn)
        return row

    def _open_section_schema(self, key: str):
        """Open the structured editor for a known system section."""
        schema = SECTION_SCHEMAS.get(key)
        if schema is None:
            self._open_section_raw(key)
            return
        if key == IMPROCESS_SECTION_KEY:
            if _edit_improcess_section(self._data, self):
                self._refresh_sections()
                self.sig_modified.emit()
            return
        current = self._data.get(key) if isinstance(self._data.get(key), dict) else None
        dlg = SectionEditorDialog(key, schema, current, self._data, self)
        if dlg.exec_() == QDialog.Accepted:
            self._data[key] = dlg.result_data
            self._refresh_sections()
            self.sig_modified.emit()

    def _open_section_picker(self):
        """Open the picker for adding a new system section."""
        candidates = [
            (k, SECTION_SCHEMAS[k]) for k in sorted(SECTION_SCHEMAS.keys())
            if k not in _CANVAS_SECTIONS
            and (k not in self._data or self._data[k] in (None, {}, []))
        ]
        if not candidates:
            return
        picker = SectionPickerDialog(candidates, self)
        if picker.exec_() != QDialog.Accepted:
            return
        key = picker.chosen_key
        if not key:
            return
        schema = SECTION_SCHEMAS[key]
        # Pre-populate with schema defaults so the editor opens with sensible
        # values that the user can then refine.
        defaults = _new_section_seed(schema)
        dlg = SectionEditorDialog(key, schema, defaults, self._data, self)
        if dlg.exec_() == QDialog.Accepted:
            self._data[key] = dlg.result_data
            self._refresh_sections()
            self.sig_modified.emit()

    def _add_custom_section(self):
        """Prompt for a new free-form top-level key and open the raw editor."""
        reserved = (
            set(DEVICE_CATS) | {"availableWidgets", DISABLED_DEVICES_KEY} | set(SECTION_SCHEMAS.keys())
            | set(self._data.keys())
        )
        key, ok = QInputDialog.getText(
            self, "Add custom section",
            "Top-level key name (e.g. myCustomConfig):",
        )
        if not ok or not key.strip():
            return
        key = key.strip()
        if key in reserved:
            QMessageBox.warning(
                self, "Name in use",
                f"'{key}' is already in use. Pick a different name."
            )
            return
        # Start with an empty dict — the JSON editor lets the user replace
        # it with any JSON value.
        self._data[key] = {}
        dlg = JsonEditorDialog(key, self._data[key], self)
        dlg.exec_()
        if dlg.changed:
            self._data[key] = dlg.result_data
        # Even if the user closed without changes, the key now exists; refresh.
        self._refresh_sections()
        self.sig_modified.emit()

    def _open_section_raw(self, key: str):
        """Open the raw-JSON editor for an Extras section."""
        val = self._data.get(key)
        dlg = JsonEditorDialog(key, val, self)
        dlg.exec_()
        if dlg.changed:
            self._data[key] = dlg.result_data
            self._refresh_sections()
            self.sig_modified.emit()

    def _remove_section(self, key: str):
        """Remove a system section from the config after confirmation."""
        r = QMessageBox.question(
            self, "Remove section",
            f"Remove the '{key}' section from this config?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if r != QMessageBox.Yes:
            return
        self._data.pop(key, None)
        self._refresh_sections()
        self.sig_modified.emit()

    def clear(self):
        self._cat = ""
        self._name = ""
        self._device = {}
        self._field_widgets.clear()
        self._tabs.clear()
        self._val_lbl.clear()
        self._custom_mgr_edit.setVisible(False)

        # Show config settings view if data is available, otherwise show empty state
        if self._data:
            self._show_config_view()
        else:
            self._name_lbl.setText("<i>No device selected</i>")
            self._hdr.setVisible(True)
            self._rename_btn.setVisible(False)
            self._mgr_combo.setVisible(False)
            self._tabs.setVisible(False)
            self._val_lbl.setVisible(False)
            self._add_row_widget.setVisible(False)
            self._apply_btn.setVisible(False)
            self._config_view.setVisible(False)


# =============================================================================
# JsonEditorDialog – modal JSON editor for arbitrary config sections
# =============================================================================
class JsonEditorDialog(QDialog):
    def __init__(self, section_key: str, data, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Edit section: {section_key}")
        self.resize(520, 440)
        self._original = json.dumps(data, indent=2, ensure_ascii=False)
        self._result = data
        self._changed = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        hint = QLabel(
            f"<span style='color:#555;font-size:8pt;'>Editing: <b>{section_key}</b>"
            "  —  Valid JSON required  —  Close with unsaved changes to be prompted</span>"
        )
        hint.setTextFormat(Qt.RichText)
        lay.addWidget(hint)

        self._edit = QTextEdit()
        mono = QFont("Courier New", 10)
        self._edit.setFont(mono)
        self._edit.setPlainText(self._original)
        lay.addWidget(self._edit, 1)

        self._err_lbl = QLabel()
        self._err_lbl.setStyleSheet("color:#C04000; font-size:8pt;")
        self._err_lbl.setWordWrap(True)
        lay.addWidget(self._err_lbl)

        btns = QHBoxLayout()
        btns.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = QPushButton("Apply")
        ok_btn.setDefault(True)
        ok_btn.setStyleSheet(
            "QPushButton { background:#3A7FC1; color:white; padding:4px 16px; }"
            "QPushButton:hover { background:#2A6FAF; }"
        )
        ok_btn.clicked.connect(self._try_apply)
        btns.addWidget(cancel_btn)
        btns.addWidget(ok_btn)
        lay.addLayout(btns)

    def _try_apply(self):
        text = self._edit.toPlainText()
        try:
            self._result = json.loads(text)
            self._changed = (text.strip() != self._original.strip())
            self.accept()
        except json.JSONDecodeError as e:
            self._err_lbl.setText(f"JSON error: {e}")

    def closeEvent(self, event):
        text = self._edit.toPlainText()
        if text.strip() != self._original.strip():
            r = QMessageBox.question(
                self, "Unsaved Changes",
                "Apply changes before closing?",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            )
            if r == QMessageBox.Cancel:
                event.ignore()
                return
            if r == QMessageBox.Yes:
                try:
                    self._result = json.loads(text)
                    self._changed = True
                except json.JSONDecodeError as e:
                    QMessageBox.warning(self, "Invalid JSON",
                                        f"Fix JSON before closing:\n{e}")
                    event.ignore()
                    return
        event.accept()

    @property
    def result_data(self):
        return self._result

    @property
    def changed(self):
        return self._changed


# =============================================================================
# WidgetPickerDialog – checkbox-based widget selector
# =============================================================================

def _load_widget_registry() -> dict:
    """Dynamically load widget registry from three sources, union'd together.

    The previous implementation parsed only the ``availableWidgets`` docstring
    in ``ViewSetupInfo.py`` — but that docstring is documentation, not the
    runtime source of truth, and it's missing several widgets that the app
    can actually load (LeicaStand, ViewerTools, LineProfile, Watcher,
    EtMonalisa, SLMs, BFTimelapse).  The hardcoded fallback below is more
    complete but was only used when parsing failed entirely, so the
    incomplete docstring shadowed it.

    Now we union three sources:
      1. The hardcoded fallback (guaranteed base — all known widgets with
         descriptions).
      2. The ``availableWidgets`` docstring in ViewSetupInfo.py (refines
         descriptions when present; may add new widgets).
      3. ``_DOCK_DISPLAY_NAMES`` in ImConMainView.py (the actual runtime
         widget registry; ensures every loadable widget appears).

    This way nothing falls out of the picker just because one of the three
    sources got stale.

    Returns:
        dict: Widget groups in format {group_name: [(widget_name, description), ...]}
    """
    # Map widget names to their display groups
    _WIDGET_GROUP_MAP: dict[str, str] = {
        # Core
        "Image": "Core",
        "Settings": "Core",
        "View": "Core",
        "Recording": "Core",
        "ViewerTools": "Core",
        "LineProfile": "Core",
        "Console": "Core",
        "Log": "Core",
        # Control
        "Laser": "Control",
        "Positioner": "Control",
        "Scan": "Control",
        "Rotator": "Control",
        "RotationScan": "Control",
        "MotCorr": "Control",
        # Microscope
        "FocusLock": "Microscope",
        "Autofocus": "Microscope",
        "LeicaStand": "Microscope",
        # Analysis
        "FFT": "Analysis",
        "FLIMHist": "Analysis",
        "BeadRec": "Analysis",
        "AlignAverage": "Analysis",
        "AlignXY": "Analysis",
        "AlignmentLine": "Analysis",
        "ULenses": "Analysis",      # Code uses ULenses (capital U)
        "uLenses": "Analysis",      # Docstring uses uLenses (lowercase u)
        "BFTimelapse": "Analysis",
        # Advanced
        "SLMs": "Advanced",
        "SLM": "Advanced",          # Docstring uses SLM (singular)
        "EtSTED": "Advanced",
        "EtMonalisa": "Advanced",
        "Tiling": "Advanced",
        "Watcher": "Advanced",
    }

    # ── Source 1: hardcoded baseline (always present) ──
    hardcoded = {
        "Core": [
            ("Image",       "Main image display"),
            ("Settings",    "Detector settings & ROI"),
            ("View",        "Image controls (LUT, zoom)"),
            ("Recording",   "Recording panel"),
            ("ViewerTools", "Napari viewer tools"),
            ("LineProfile", "Line profile tool"),
            ("Console",     "Python scripting console"),
        ],
        "Control": [
            ("Laser",        "Laser / LED power control"),
            ("Positioner",   "Stage positioner"),
            ("Scan",         "Scan widget"),
            ("Rotator",      "Rotator control"),
            ("RotationScan", "Rotation scan"),
            ("MotCorr",      "Leica motorized correction collar"),
        ],
        "Microscope": [
            ("FocusLock",  "IR focus lock"),
            ("Autofocus",  "Software autofocus"),
            ("LeicaStand", "Leica stand control"),
        ],
        "Analysis": [
            ("FFT",            "Live FFT tool"),
            ("FLIMHist",       "FLIM lifetime histogram (needs a FLIM detector)"),
            ("BeadRec",        "Bead reconstruction"),
            ("AlignAverage",   "Axial alignment tool"),
            ("AlignXY",        "Rotational alignment tool"),
            ("AlignmentLine",  "Line alignment overlay"),
            ("ULenses",        "uLenses tool"),
            ("BFTimelapse",    "Brightfield timelapse"),
        ],
        "Advanced": [
            ("SLM",        "SLM control (single)"),
            ("SLMs",       "SLM control (multi)"),
            ("EtSTED",     "EtSTED widget"),
            ("EtMonalisa", "EtMonalisa widget"),
            ("Tiling",     "Spiral tiling scan"),
            ("Watcher",    "File watcher"),
        ],
    }

    # Flatten into {widget_name: description} for merging.
    widgets_found = {name: desc for group in hardcoded.values()
                     for name, desc in group}

    # ── Source 2: parse availableWidgets docstring in ViewSetupInfo.py ──
    view_setup_path = _imcontrol_dir() / "view" / "guitools" / "ViewSetupInfo.py"
    if view_setup_path.exists():
        try:
            with open(view_setup_path, "r", encoding="utf-8") as f:
                content = f.read()
            # Pattern: - ``WidgetName`` (description text)
            for widget_name, description in re.findall(
                r'-\s+``(\w+)``\s+\(([^)]+)\)', content
            ):
                desc = ' '.join(description.split())
                if desc and desc[0].islower():
                    desc = desc[0].upper() + desc[1:]
                # Docstring descriptions are the official ones; let them
                # override hardcoded defaults.
                widgets_found[widget_name] = desc
        except Exception:
            pass

    # ── Source 3: parse _DOCK_DISPLAY_NAMES from ImConMainView.py ──
    # This is the authoritative runtime list — every widget the app can
    # actually load appears here.  Add any names that the other two sources
    # missed, using the display name as a fallback description.
    main_view_path = _imcontrol_dir() / "view" / "ImConMainView.py"
    if main_view_path.exists():
        try:
            with open(main_view_path, "r", encoding="utf-8") as f:
                content = f.read()
            # Scope to the _DOCK_DISPLAY_NAMES dict body.
            block_match = re.search(
                r'_DOCK_DISPLAY_NAMES\s*=\s*\{(.*?)\}', content, re.DOTALL
            )
            if block_match:
                for widget_name, display_name in re.findall(
                    r"'(\w+)'\s*:\s*'([^']+)'", block_match.group(1)
                ):
                    # Don't overwrite existing descriptions; only fill gaps.
                    widgets_found.setdefault(widget_name, display_name)
        except Exception:
            pass

    # Build the grouped output.  Unknown widgets (not in _WIDGET_GROUP_MAP)
    # land in "Other" so they're still pickable.
    groups = {
        "Core": [], "Control": [], "Microscope": [],
        "Analysis": [], "Advanced": [], "Other": [],
    }
    for widget_name, description in sorted(widgets_found.items()):
        group = _WIDGET_GROUP_MAP.get(widget_name, "Other")
        groups[group].append((widget_name, description))
    return {k: v for k, v in groups.items() if v}


WIDGET_GROUPS = _load_widget_registry()  # auto-discovered from ViewSetupInfo.py

# Flat set of all known widget names for fast lookup
_ALL_KNOWN_WIDGETS = {name for group in WIDGET_GROUPS.values() for name, _ in group}


# =============================================================================
# SectionPickerDialog – choose a system section schema to add
# =============================================================================
class SectionPickerDialog(QDialog):
    """Modal picker for choosing which system section to add.

    Lists the section schemas that are NOT yet present in the config, each
    with a one-line summary and (optionally) a legacy badge. Single-select.
    """

    def __init__(self, schemas: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add system section")
        self.resize(520, 420)
        self._chosen: str = ""
        self._schemas = schemas  # list of (key, schema)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        hint = QLabel(
            "<span style='color:#666;font-size:9pt;'>Select a system section "
            "to add to this config. The next dialog will let you fill in its "
            "fields.</span>"
        )
        hint.setTextFormat(Qt.RichText)
        hint.setWordWrap(True)
        lay.addWidget(hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        inner_lay = QVBoxLayout(inner)
        inner_lay.setContentsMargins(4, 4, 4, 4)
        inner_lay.setSpacing(4)

        colors = get_themed_colors(is_dark_mode())
        self._buttons: list = []
        for key, schema in schemas:
            btn = self._make_row(key, schema, colors)
            inner_lay.addWidget(btn)
            self._buttons.append(btn)
        inner_lay.addStretch()
        scroll.setWidget(inner)
        lay.addWidget(scroll, 1)

        btns = QHBoxLayout()
        btns.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btns.addWidget(cancel_btn)
        lay.addLayout(btns)

    def _make_row(self, key: str, schema: dict, colors: dict) -> QPushButton:
        legacy = bool(schema.get("legacy"))
        display = schema.get("display", key)
        summary = schema.get("summary", "")
        label_html = f"<b>{display}</b>"
        if legacy:
            label_html += " <span style='color:#C04000;font-size:8pt;'>[legacy]</span>"
        label_html += f" <span style='color:#888;font-size:8pt;'>({key})</span>"
        if summary:
            label_html += f"<br><span style='color:#666;font-size:8pt;'>{summary}</span>"
        btn = QPushButton()
        btn.setStyleSheet(
            f"QPushButton {{ text-align:left; padding:8px 10px; border:1px solid "
            f"{colors['chip_border']}; border-radius:4px; background:{colors['button_bg']}; }}"
            f"QPushButton:hover {{ background:{colors['button_bg_hover']}; }}"
        )
        # Use a child QLabel for rich text rendering inside the button.
        bl = QVBoxLayout(btn)
        bl.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel(label_html)
        lbl.setTextFormat(Qt.RichText)
        lbl.setWordWrap(True)
        lbl.setAttribute(Qt.WA_TransparentForMouseEvents)
        bl.addWidget(lbl)
        btn.clicked.connect(lambda _, k=key: self._choose(k))
        return btn

    def _choose(self, key: str):
        self._chosen = key
        self.accept()

    @property
    def chosen_key(self) -> str:
        return self._chosen


def _collect_section_result(schema: dict, received, field_widgets: dict,
                            custom_widgets: dict) -> tuple[dict, list]:
    """What Apply writes for a section: ``(block, missing_required_labels)``.

    ``received`` is the keys of the block the dialog was opened on, in file
    order. With the default policy every schema field is written. With
    ``write_policy == "as_written"`` a schema field is written only if the
    file already had its key, the operator touched its widget, or it is
    required, and the keys come back in the file's order. When a required
    field is empty the block is incomplete and ``missing`` names it.
    """
    as_written = _writes_as_written(schema)
    out: dict = {}
    missing: list = []
    for f in schema.get("fields", []):
        fw = field_widgets.get(f["key"])
        if fw is None:
            continue
        val = fw.get_value()
        if f.get("req") and (val is None or val == "" or val == []):
            missing.append(f["label"])
        if as_written and not (
            f["key"] in received or fw.is_touched() or f.get("req")
        ):
            continue
        out[f["key"]] = val
    if missing:
        return out, missing
    for k, le in custom_widgets.items():
        original = le.property("originalValue")
        if _coercion_module is not None:
            out[k] = _coercion_module.raw_text_to_json(le.text(), original)
        else:
            out[k] = _parse_freeform_value(le.text())
    if as_written:
        out = {**{k: out[k] for k in received if k in out},
               **{k: v for k, v in out.items() if k not in received}}
    return out, []


class _CustomRowsMixin:
    """The "Custom" page of a section dialog: the keys the schema has no field for.

    The dialog provides ``_custom_widgets`` (``{}``), ``_field_widgets``,
    ``_allow_custom``, ``_err_lbl`` and ``_show_custom_page()``.
    """

    def _build_custom_page(self, current: dict | None, known: set) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        self._custom_form = QFormLayout(inner)
        self._custom_form.setContentsMargins(8, 8, 8, 8)
        self._custom_form.setSpacing(4)
        self._custom_empty_lbl = QLabel(
            "<span style='color:#888;font-size:8pt;'>No custom properties. "
            "Use ⊕ Add property to add one.</span>"
        )
        self._custom_empty_lbl.setTextFormat(Qt.RichText)
        self._custom_form.addRow(self._custom_empty_lbl)
        for k, v in (current or {}).items():
            if k not in known:
                self._add_custom_row(k, v)
        scroll.setWidget(inner)
        return scroll

    def _add_property_button(self) -> QPushButton:
        add_prop_btn = QPushButton("⊕ Add property")
        add_prop_btn.setFixedHeight(22)
        add_prop_btn.setStyleSheet("font-size:8pt; padding:0 6px;")
        add_prop_btn.setToolTip("Add an arbitrary key to these settings")
        add_prop_btn.clicked.connect(self._prompt_custom_field)
        return add_prop_btn

    def add_custom_field(self, key: str, value) -> str:
        """Add a custom key; returns an error message, or "" on success."""
        if not self._allow_custom:
            return "These settings take no custom properties."
        key = key.strip()
        if not key:
            return "Key name is empty."
        if key in self._field_widgets or key in self._custom_widgets:
            return f"'{key}' already exists in these settings."
        self._add_custom_row(key, value)
        self._show_custom_page()
        return ""

    def _prompt_custom_field(self):
        key, ok = QInputDialog.getText(self, "Add Property", "Key name:")
        if not ok or not key.strip():
            return
        val_str, ok2 = QInputDialog.getText(
            self, "Add Property",
            'Value  (JSON: 42, true, "text", null, [1,2], {...}):',
            text="null",
        )
        if not ok2:
            return
        self._err_lbl.setText(self.add_custom_field(key, _parse_freeform_value(val_str)))

    def _add_custom_row(self, key: str, value):
        le = QLineEdit(json.dumps(value) if not isinstance(value, str) else value)
        # Bare text cannot say whether "5" was a string or a number; the
        # read-back asks the original rather than guessing.
        le.setProperty("originalValue", value)
        row = QWidget()
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(0, 0, 0, 0)
        row_lay.setSpacing(2)
        row_lay.addWidget(le, 1)
        del_btn = QPushButton("×")
        del_btn.setFixedSize(20, 20)
        del_btn.setToolTip(f"Remove '{key}'")
        del_btn.clicked.connect(lambda _, k=key, r=row: self._remove_custom_row(k, r))
        row_lay.addWidget(del_btn)
        self._custom_form.addRow(QLabel(key), row)
        self._custom_widgets[key] = le
        self._custom_empty_lbl.setVisible(False)

    def _remove_custom_row(self, key: str, row: QWidget):
        self._custom_widgets.pop(key, None)
        self._custom_form.removeRow(row)
        self._custom_empty_lbl.setVisible(not self._custom_widgets)


# =============================================================================
# SectionEditorDialog – schema-driven editor for singleton sections
# =============================================================================
class SectionEditorDialog(_CustomRowsMixin, QDialog):
    """Modal form editor for one top-level system section (focusLock, scan, …).

    Reuses :class:`FieldWidget` so its inputs match the device editor.
    Ref-type fields are populated from the live config data dict so that
    e.g. ``focusLock.camera`` is a dropdown of existing detector names.

    With ``allow_custom_fields`` the dialog also lists the keys of
    ``current`` that the schema does not define, in a "Custom" tab, and
    offers "Add property" to add more; Apply writes them back with the
    schema fields instead of dropping them.

    A schema with ``"write_policy": "as_written"`` turns ``allow_custom_fields``
    on and writes a schema field only if ``current`` already had its key, the
    operator touched its widget, or it is required -- so Apply neither expands
    a short block into every field nor loses a key the schema does not know.
    """

    def __init__(self, key: str, schema: dict, current: dict | None, data: dict, parent=None, *,
                 title_prefix="Edit section", allow_custom_fields=False):
        super().__init__(parent)
        self._key = key
        self._schema = schema
        self._data = data  # reference, used to populate ref combos
        self._result: dict = {}
        self._as_written = _writes_as_written(schema)
        # Keeping unknown keys is half of "as written".
        self._allow_custom = allow_custom_fields or self._as_written
        # Insertion order kept: an as-written section is written back in the
        # order the file had it.
        self._received = list(current) if isinstance(current, dict) else []
        self._custom_widgets: dict = {}  # key → QLineEdit

        legacy = schema.get("legacy")
        title = f"{title_prefix}: {schema.get('display', key)}"
        if legacy:
            title += " (legacy)"
        self.setWindowTitle(title)
        self.resize(560, 520)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        # ── Header summary ──
        summary = schema.get("summary", "")
        if summary:
            hint = QLabel(f"<span style='color:#666;font-size:9pt;'>{summary}</span>")
            hint.setTextFormat(Qt.RichText)
            hint.setWordWrap(True)
            lay.addWidget(hint)
        requires = schema.get("requires_widget")
        if requires:
            req = QLabel(
                f"<span style='color:#3A7FC1;font-size:8pt;'>Pairs with widget: "
                f"<b>{requires}</b></span>"
            )
            req.setTextFormat(Qt.RichText)
            lay.addWidget(req)
        if legacy and schema.get("legacy_note"):
            warn = QLabel(
                f"<span style='color:#C04000;font-size:8pt;'>⚠ legacy — "
                f"{schema['legacy_note']}</span>"
            )
            warn.setTextFormat(Qt.RichText)
            warn.setWordWrap(True)
            lay.addWidget(warn)

        # ── Build device pool for ref fields ──
        pool: dict = {}
        for cat in DEVICE_CATS:
            pool[cat] = sorted(list((self._data.get(cat) or {}).keys()))

        # ── Tabs grouped by `grp` ──
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        lay.addWidget(tabs, 1)

        self._field_widgets: dict = {}  # key → FieldWidget
        groups: dict[str, list] = {}
        for f in schema.get("fields", []):
            groups.setdefault(f.get("grp", "Basic"), []).append(f)
        ordered = sorted(groups.keys(), key=lambda g: (0 if g == "Basic" else 2 if g == "Advanced" else 1))

        for grp in ordered:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            inner = QWidget()
            form = QFormLayout(inner)
            form.setContentsMargins(8, 8, 8, 8)
            form.setSpacing(6)
            form.setLabelAlignment(Qt.AlignRight)
            for f in groups[grp]:
                cur = (current or {}).get(f["key"], _MISSING)
                if cur is _MISSING:
                    cur = _field_default(f)
                    # Normalize the template's null sentinel.
                    if cur == "null":
                        cur = None
                fw = FieldWidget(f, cur, device_pool=pool)
                label = f["label"]
                if f.get("req"):
                    label = f"<b>{label}</b> *"
                lbl = QLabel(label)
                lbl.setTextFormat(Qt.RichText)
                lbl.setToolTip(f.get("tip", ""))
                form.addRow(lbl, fw)
                self._field_widgets[f["key"]] = fw
            scroll.setWidget(inner)
            tabs.addTab(scroll, grp)

        # ── Custom tab: keys the schema does not define ──
        if self._allow_custom:
            known = {f["key"] for f in schema.get("fields", [])}
            scroll = self._build_custom_page(current, known)
            self._custom_tab = scroll
            tabs.addTab(scroll, "Custom")
            self._tabs = tabs

            add_row = QHBoxLayout()
            add_row.addWidget(self._add_property_button())
            add_row.addStretch()
            lay.addLayout(add_row)

        # ── Validation label ──
        self._err_lbl = QLabel()
        self._err_lbl.setStyleSheet("color:#C04000; font-size:8pt;")
        self._err_lbl.setWordWrap(True)
        lay.addWidget(self._err_lbl)

        # ── Buttons ──
        btns = QHBoxLayout()
        btns.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = QPushButton("Apply")
        ok_btn.setDefault(True)
        ok_btn.setStyleSheet(
            "QPushButton { background:#3A7FC1; color:white; padding:4px 16px; }"
            "QPushButton:hover { background:#2A6FAF; }"
        )
        ok_btn.clicked.connect(self._try_apply)
        btns.addWidget(cancel_btn)
        btns.addWidget(ok_btn)
        lay.addLayout(btns)

    def _try_apply(self):
        out, missing = _collect_section_result(
            self._schema, self._received, self._field_widgets, self._custom_widgets
        )
        if missing:
            self._err_lbl.setText(
                "Missing required: " + ", ".join(missing)
            )
            return
        self._result = out
        self.accept()

    def _show_custom_page(self):
        self._tabs.setCurrentWidget(self._custom_tab)

    @property
    def result_data(self) -> dict:
        return self._result


# =============================================================================
# ImProcessSectionDialog – the dedicated, explained editor of the ImProcess block
# =============================================================================
@dataclass
class _DropIns:
    """What discovery found in the drop-in plugins folder, for display only."""

    directory: str = ""
    #: ``(id, kind, display name, file)`` per discovered plugin class.
    entries: list = dc_field(default_factory=list)
    #: ``(file, message)`` per file that failed to load.
    errors: list = dc_field(default_factory=list)
    #: Why discovery could not run at all, or "".
    unavailable: str = ""


def _discover_dropins(directory: str | None = None) -> _DropIns:
    """Scan the drop-in plugins folder; install nothing.

    Runs only when the ImProcess dialog opens. ``discover_plugins`` imports
    each plugin file (they are Python), so this is deliberately not done at
    editor import time; it never calls ``load_user_plugins``, so the plugin
    registries are left as they were. Any failure -- ImProcess not importable
    included -- is reported in ``unavailable`` instead of raised.
    """
    found = _DropIns(directory=directory or "")
    try:
        from imswitch.improcess.plugins.user_plugins import (
            discover_plugins, user_plugins_directory,
        )

        found.directory = directory or user_plugins_directory(create=False)
        discovered = discover_plugins(found.directory)
    except Exception as exc:
        found.unavailable = f"{type(exc).__name__}: {exc}"
        return found

    def source_file(plugin_cls) -> str:
        module = sys.modules.get(getattr(plugin_cls, "__module__", ""))
        return str(getattr(module, "__file__", "") or "")

    for kind, classes in (("reconstructor", discovered.reconstructors),
                          ("processor", discovered.processors)):
        for plugin_id, plugin_cls in classes.items():
            found.entries.append((
                plugin_id, kind,
                str(getattr(plugin_cls, "name", "") or plugin_id),
                source_file(plugin_cls),
            ))
    found.errors = [(err.path, err.message) for err in discovered.errors]
    return found


class _EndpointsEditor(QWidget):
    """The napari endpoints list as pretty-printed JSON in a multi-line box.

    Quacks like :class:`FieldWidget` for :func:`_collect_section_result`:
    untouched it answers the value the file had, byte for byte.
    """

    def __init__(self, field_def: dict, value, parent=None):
        super().__init__(parent)
        self._def = field_def
        self._original = value
        self._touched = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._w = QPlainTextEdit(json.dumps(value, indent=2))
        self._w.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._w.setMinimumHeight(self._w.sizeHint().height())
        if field_def.get("tip"):
            self._w.setToolTip(field_def["tip"])
        # Connected after the text is in place: construction is not an edit.
        self._w.textChanged.connect(self._on_changed)
        lay.addWidget(self._w)

    def _on_changed(self) -> None:
        self._touched = True

    def is_touched(self) -> bool:
        return self._touched

    def _text(self) -> str:
        return self._w.toPlainText().strip()

    def get_value(self):
        if not self._touched:
            return self._original
        text = self._text()
        if not text:
            return []
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def validation_error(self) -> str:
        """Why the edited text cannot be written, or "" when it can."""
        if not self._touched or not self._text():
            return ""
        label = self._def.get("label", "Napari endpoints")
        try:
            value = json.loads(self._text())
        except json.JSONDecodeError as exc:
            return f"{label}: not valid JSON ({exc.msg}, line {exc.lineno}, column {exc.colno})."
        if not isinstance(value, list):
            return f"{label}: must be a list of objects, one per endpoint."
        try:
            from imswitch.improcess.model.napari_endpoints import endpoint_from_dict
        except Exception:
            endpoint_from_dict = None
        problems = []
        for number, item in enumerate(value, 1):
            if not isinstance(item, dict):
                problems.append(f"entry {number} is not an object")
            elif endpoint_from_dict is not None:
                try:
                    endpoint_from_dict(item)
                except Exception as exc:
                    problems.append(f"entry {number}: {exc}")
            elif not str(item.get("id") or "").strip():
                problems.append(f"entry {number} has no id")
        if problems:
            return f"{label}: " + "; ".join(problems) + "."
        return ""


class ImProcessSectionDialog(_CustomRowsMixin, QDialog):
    """Modal editor of the setup's ``processing`` block.

    Pages on the left (the schema's groups, then Drop-in plugins, Custom and
    Set on this computer), the page on the right, and under every option its
    one-sentence explanation in plain view. Writes follow the schema's
    ``as_written`` policy through :func:`_collect_section_result`, exactly as
    :class:`SectionEditorDialog` does: open, Apply untouched, and the block is
    what it was.
    """

    #: Field types that sit on one line with their label; the rest (lists,
    #: the endpoints box) get the width of the page below it.
    _INLINE_TYPES = frozenset(
        {"bool", "bool_auto", "int", "float", "select", "text", "path", "ref"}
    )

    def __init__(self, schema: dict, current: dict | None, data: dict, parent=None, *,
                 dropin_directory: str | None = None):
        super().__init__(parent)
        self._key = IMPROCESS_SECTION_KEY
        self._schema = schema
        self._data = data
        self._result: dict = {}
        self._received = list(current) if isinstance(current, dict) else []
        self._allow_custom = True
        self._custom_widgets: dict = {}
        self._field_widgets: dict = {}
        self._endpoints: _EndpointsEditor | None = None
        self._page_titles: list[str] = []
        self._muted = "#AAAAAA" if is_dark_mode() else "#666666"
        self._dropins = _discover_dropins(dropin_directory)
        self.setWindowTitle(f"Edit section: {schema.get('display', self._key)}")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        summary = schema.get("summary", "")
        if summary:
            hint = QLabel(summary)
            hint.setTextFormat(Qt.PlainText)
            hint.setWordWrap(True)
            hint.setStyleSheet(f"color:{self._muted}; font-size:9pt;")
            lay.addWidget(hint)

        body = QHBoxLayout()
        body.setSpacing(8)
        self._page_list = QListWidget()
        self._stack = QStackedWidget()
        body.addWidget(self._page_list)
        body.addWidget(self._stack, 1)
        lay.addLayout(body, 1)

        groups: dict[str, list] = {}
        for f in schema.get("fields", []):
            groups.setdefault(f.get("grp", "Basic"), []).append(f)
        for grp, fields in groups.items():
            self._add_page(grp, self._build_group_page(fields, current))
        self._add_page("Drop-in plugins", self._build_dropin_page(current))
        self._add_page("Custom", self._build_custom_tab(current))
        self._add_page("Set on this computer", self._build_computer_page())

        # The list is as wide as its longest page title, no wider.
        self._page_list.setFixedWidth(
            self._page_list.sizeHintForColumn(0) + 2 * self._page_list.frameWidth() + 8
        )
        self._page_list.currentRowChanged.connect(self._stack.setCurrentIndex)
        self._page_list.setCurrentRow(0)
        self.resize(560 + self._page_list.width(), 560)

        self._err_lbl = QLabel()
        self._err_lbl.setStyleSheet("color:#C04000; font-size:8pt;")
        self._err_lbl.setWordWrap(True)
        lay.addWidget(self._err_lbl)

        btns = QHBoxLayout()
        btns.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = QPushButton("Apply")
        ok_btn.setDefault(True)
        ok_btn.setStyleSheet(
            "QPushButton { background:#3A7FC1; color:white; padding:4px 16px; }"
            "QPushButton:hover { background:#2A6FAF; }"
        )
        ok_btn.clicked.connect(self._try_apply)
        btns.addWidget(cancel_btn)
        btns.addWidget(ok_btn)
        lay.addLayout(btns)

    # ── pages ───────────────────────────────────────────────────────────────
    def _add_page(self, title: str, widget: QWidget) -> None:
        self._page_titles.append(title)
        self._page_list.addItem(title)
        self._stack.addWidget(widget)

    def _show_page(self, title: str) -> None:
        self._page_list.setCurrentRow(self._page_titles.index(title))

    def _show_custom_page(self) -> None:
        self._show_page("Custom")

    def _scrolling(self, inner: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(inner)
        return scroll

    def _note(self, text: str, *, rich: bool = False) -> QLabel:
        label = QLabel(text)
        label.setTextFormat(Qt.RichText if rich else Qt.PlainText)
        label.setWordWrap(True)
        label.setStyleSheet(f"color:{self._muted}; font-size:8pt;")
        return label

    def _build_group_page(self, fields: list, current: dict | None) -> QScrollArea:
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(12)
        for f in fields:
            lay.addWidget(self._field_row(f, current))
        lay.addStretch()
        return self._scrolling(inner)

    def _field_row(self, f: dict, current: dict | None) -> QWidget:
        cur = (current or {}).get(f["key"], _MISSING)
        if cur is _MISSING:
            cur = _field_default(f)
            if cur == "null":
                cur = None
        if f["key"] == "napariEndpoints":
            fw = _EndpointsEditor(f, cur)
            self._endpoints = fw
        else:
            fw = FieldWidget(f, cur)
            fw.fit_to_contents()
        self._field_widgets[f["key"]] = fw

        label = QLabel(f"<b>{f['label']}</b> *" if f.get("req") else f["label"])
        label.setTextFormat(Qt.RichText)
        label.setWordWrap(True)
        label.setToolTip(f.get("tip", ""))

        row = QWidget()
        col = QVBoxLayout(row)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)
        if f["type"] in self._INLINE_TYPES:
            line = QHBoxLayout()
            line.setSpacing(8)
            if f["type"] == "bool":
                line.addWidget(fw)
                line.addWidget(label, 1)
            else:
                line.addWidget(label, 2)
                line.addWidget(fw, 3)
            col.addLayout(line)
        else:
            col.addWidget(label)
            col.addWidget(fw)
        if f.get("tip"):
            col.addWidget(self._note(f["tip"]))
        return row

    def _build_dropin_page(self, current: dict | None) -> QScrollArea:
        found = self._dropins
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)
        lay.addWidget(self._note(
            "Drop-in plugins are active on this computer whenever their file is "
            "in the plugins folder, so they are not listed in the setup file."
        ))
        folder = QLabel(f"Folder: {found.directory or 'unknown'}")
        folder.setTextFormat(Qt.PlainText)
        folder.setWordWrap(True)
        folder.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(folder)

        self._dropin_tree = QTreeWidget()
        self._dropin_tree.setRootIsDecorated(False)
        self._dropin_tree.setHeaderLabels(["Id", "Kind", "Name", "File", ""])
        self._dropin_tree.setEditTriggers(QTreeWidget.NoEditTriggers)
        lay.addWidget(self._dropin_tree)

        self._dropin_notice = None
        if found.unavailable:
            self._dropin_notice = (
                f"Drop-in discovery is unavailable: {found.unavailable}"
            )
        elif not found.entries and not found.errors:
            self._dropin_notice = "No drop-in plugin files found in this folder."
        if self._dropin_notice:
            notice = QLabel(self._dropin_notice)
            notice.setTextFormat(Qt.PlainText)
            notice.setWordWrap(True)
            lay.addWidget(notice)

        listed = set()
        for key in ("reconstructors", "processors"):
            value = (current or {}).get(key)
            if isinstance(value, list):
                listed.update(str(v) for v in value)
        for plugin_id, kind, name, path in found.entries:
            note = (
                "listed in this setup — not portable to computers without the file"
                if plugin_id in listed else ""
            )
            item = QTreeWidgetItem([plugin_id, kind, name, os.path.basename(path), note])
            item.setToolTip(3, path)
            self._dropin_tree.addTopLevelItem(item)
        for column in range(self._dropin_tree.columnCount()):
            self._dropin_tree.resizeColumnToContents(column)
        self._dropin_tree.setVisible(bool(found.entries))

        for path, message in found.errors:
            last = next((ln for ln in reversed(message.splitlines()) if ln.strip()), message)
            err = QLabel(f"Could not load {os.path.basename(path)}: {last.strip()}")
            err.setTextFormat(Qt.PlainText)
            err.setWordWrap(True)
            err.setToolTip(message)
            err.setStyleSheet("color:#C04000;")
            lay.addWidget(err)
        lay.addStretch()
        return self._scrolling(inner)

    def _build_custom_tab(self, current: dict | None) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)
        lay.addWidget(self._note(
            "Keys in the block that this editor has no field for. They are kept "
            "as written; add your own here."
        ))
        known = {f["key"] for f in self._schema.get("fields", [])}
        lay.addWidget(self._build_custom_page(current, known), 1)
        row = QHBoxLayout()
        row.addWidget(self._add_property_button())
        row.addStretch()
        lay.addLayout(row)
        return page

    def _build_computer_page(self) -> QWidget:
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)
        text = QLabel(
            "<p>Some ImProcess settings belong to a computer, not to a setup, so "
            "they are not in this editor and not in the setup file. They are set "
            "inside ImProcess and stored in "
            "<b>ImSwitchConfig/config/improcess_options.json</b>:</p>"
            "<ul>"
            "<li><b>Fiji location</b> — Plugins → Fiji → Fiji location…</li>"
            "<li><b>Image display</b> (napari or separate windows) — "
            "Preferences → Image display</li>"
            "<li><b>Default folders</b> — Preferences → Default folders…</li>"
            "</ul>"
        )
        text.setTextFormat(Qt.RichText)
        text.setWordWrap(True)
        lay.addWidget(text)
        lay.addStretch()
        return self._scrolling(inner)

    # ── Apply ───────────────────────────────────────────────────────────────
    def _try_apply(self):
        if self._endpoints is not None:
            problem = self._endpoints.validation_error()
            if problem:
                self._show_page(
                    self._schema_fields_group("napariEndpoints") or self._page_titles[0]
                )
                self._err_lbl.setText(problem)
                return
        out, missing = _collect_section_result(
            self._schema, self._received, self._field_widgets, self._custom_widgets
        )
        if missing:
            self._err_lbl.setText("Missing required: " + ", ".join(missing))
            return
        self._result = out
        self.accept()

    def _schema_fields_group(self, key: str) -> str:
        field_def = _schema_fields(self._schema).get(key) or {}
        return field_def.get("grp", "")

    @property
    def result_data(self) -> dict:
        return self._result


def _edit_improcess_section(data: dict, parent=None) -> bool:
    """Open the ImProcess dialog on ``data``'s block; write it back on Apply.

    A missing or empty block opens on the new-section seed. Returns whether
    the operator applied; the caller refreshes what shows the block and
    marks the document modified.
    """
    schema = SECTION_SCHEMAS.get(IMPROCESS_SECTION_KEY)
    if schema is None:
        return False
    current = data.get(IMPROCESS_SECTION_KEY)
    if not isinstance(current, dict) or not current:
        current = _new_section_seed(schema)
    dlg = ImProcessSectionDialog(schema, current, data, parent)
    if dlg.exec_() != QDialog.Accepted:
        return False
    data[IMPROCESS_SECTION_KEY] = dlg.result_data
    return True


class WidgetPickerDialog(QDialog):
    """Modal dialog that presents every known widget as a checkbox grouped by
    category.  Unknown (custom) widgets already in the enabled list are shown
    in a separate 'Custom' section at the bottom, also pre-checked."""

    def __init__(self, enabled: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configure Widgets")
        self.setMinimumWidth(400)
        self.setMinimumHeight(480)

        # Track checkboxes: name → QCheckBox
        self._checkboxes: dict = {}

        outer = QVBoxLayout(self)
        outer.setSpacing(8)

        # ── Manual entry: add a widget name not present in the registry ──
        # Lets the user enable an experimental/custom widget by typing its
        # name.  Adds a checked checkbox to the Custom section on submit.
        entry_row = QHBoxLayout()
        entry_row.setSpacing(4)
        entry_row.addWidget(QLabel("Custom widget:"))
        self._custom_entry = QLineEdit()
        self._custom_entry.setPlaceholderText("e.g. MyExperimental")
        self._custom_entry.returnPressed.connect(self._add_custom_from_entry)
        entry_row.addWidget(self._custom_entry, 1)
        add_btn = QPushButton("Add")
        add_btn.setFixedHeight(24)
        add_btn.clicked.connect(self._add_custom_from_entry)
        entry_row.addWidget(add_btn)
        outer.addLayout(entry_row)

        # ── Scrollable checkbox area ──────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        self._inner_lay = QVBoxLayout(inner)
        self._inner_lay.setSpacing(4)
        self._inner_lay.setContentsMargins(4, 4, 4, 4)

        enabled_set = set(enabled)

        for group_name, members in WIDGET_GROUPS.items():
            # Bold group header
            hdr = QLabel(f"<b>{group_name}</b>")
            hdr.setTextFormat(Qt.RichText)
            hdr.setContentsMargins(0, 6, 0, 2)
            self._inner_lay.addWidget(hdr)

            for widget_name, description in members:
                cb = QCheckBox(f"{widget_name}  —  {description}")
                cb.setChecked(widget_name in enabled_set)
                cb.setStyleSheet("font-size:9pt;")
                self._checkboxes[widget_name] = cb
                self._inner_lay.addWidget(cb)

        # ── Custom / unknown entries ──────────────────────────────────────
        # Always create the header so manually-added widgets have somewhere
        # to land even when the enabled list has no customs to start with.
        self._custom_hdr = QLabel("<b>Custom</b>")
        self._custom_hdr.setTextFormat(Qt.RichText)
        self._custom_hdr.setContentsMargins(0, 6, 0, 2)
        self._inner_lay.addWidget(self._custom_hdr)

        # Track index of the trailing stretch so we can insert new custom
        # checkboxes just above it (keeps the dialog layout tidy).
        for widget_name in [n for n in enabled if n not in _ALL_KNOWN_WIDGETS]:
            self._add_custom_checkbox(widget_name, checked=True)

        # Hide the header initially if there are no custom entries yet.
        self._update_custom_hdr_visibility()

        self._stretch_marker = self._inner_lay.count()
        self._inner_lay.addStretch()
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        # ── OK / Cancel ───────────────────────────────────────────────────
        btn_box = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        outer.addWidget(btn_box)

    def _add_custom_from_entry(self):
        """Read the manual-entry field and add a custom checkbox."""
        name = self._custom_entry.text().strip()
        if not name:
            return
        # Don't allow duplicates — if the widget is already in the registry
        # (known group), just check its existing box.  If it's already in
        # the custom section, also just re-check.
        existing = self._checkboxes.get(name)
        if existing is not None:
            existing.setChecked(True)
            self._custom_entry.clear()
            return
        self._add_custom_checkbox(name, checked=True)
        self._update_custom_hdr_visibility()
        self._custom_entry.clear()

    def _add_custom_checkbox(self, widget_name: str, checked: bool):
        """Insert a new checkbox into the Custom section, above the stretch."""
        cb = QCheckBox(widget_name)
        cb.setChecked(checked)
        cb.setStyleSheet("font-size:9pt;")
        self._checkboxes[widget_name] = cb
        # Insert before the final stretch (which is the last item once
        # __init__ has added it).  During __init__ the stretch isn't there
        # yet, so addWidget appends — that's also correct.
        if getattr(self, '_stretch_marker', None) is not None:
            self._inner_lay.insertWidget(self._stretch_marker, cb)
            self._stretch_marker += 1
        else:
            self._inner_lay.addWidget(cb)

    def _update_custom_hdr_visibility(self):
        """Hide the Custom header when no custom checkboxes exist."""
        has_custom = any(
            name not in _ALL_KNOWN_WIDGETS
            for name in self._checkboxes
        )
        self._custom_hdr.setVisible(has_custom)

    def selected_widgets(self) -> list:
        """Return checked widget names in group order, custom entries last."""
        result = []
        # Known widgets in group order
        for members in WIDGET_GROUPS.values():
            for widget_name, _ in members:
                cb = self._checkboxes.get(widget_name)
                if cb is not None and cb.isChecked():
                    result.append(widget_name)
        # Custom widgets (preserve original order)
        for widget_name, cb in self._checkboxes.items():
            if widget_name not in _ALL_KNOWN_WIDGETS and cb.isChecked():
                result.append(widget_name)
        return result


# =============================================================================
# TemplateStore – file-backed user template library
# =============================================================================
class TemplateStore:
    """
    Persists user templates as JSON files under the user config root (see
    :func:`_user_template_dir`).  One sub-directory per category; one JSON file
    per template.
    config_editor_templates/
      detectors/
        MyAPD.json
      lasers/
        My488nm.json
    """

    def __init__(self, store_dir: object = None):
        migrate = store_dir is None
        if store_dir is None:
            store_dir = _user_template_dir()
        self._dir = Path(store_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        if migrate:
            self._migrate_legacy_store()
        self._cache: dict = {}   # cat → {name: device}
        self.reload()

    def _migrate_legacy_store(self) -> None:
        """Carry templates over from the pre-package store, once.

        Until the editor moved into the package, saved templates lived in
        ``utility_scripts/templates`` next to the script. Anyone who had built
        up a library there would otherwise open the editor one day and find it
        empty. Copies rather than moves, so the old checkout keeps working if
        this version is rolled back, and only fills gaps so a template the
        operator has since edited in the new store wins.
        """
        legacy = _imcontrol_dir().parents[1] / "utility_scripts" / "templates"
        if not legacy.is_dir() or legacy.resolve() == self._dir.resolve():
            return
        for cat_dir in sorted(legacy.iterdir()):
            if not cat_dir.is_dir():
                continue
            for f in sorted(cat_dir.glob("*.json")):
                target = self._dir / cat_dir.name / f.name
                if target.exists():
                    continue
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(f.read_bytes())
                except OSError:
                    # A template we cannot copy is not worth failing startup
                    # over; the old file stays where it is.
                    continue

    def reload(self):
        self._cache.clear()
        for cat_dir in sorted(self._dir.iterdir()):
            if not cat_dir.is_dir():
                continue
            templates: dict = {}
            for f in sorted(cat_dir.glob("*.json")):
                try:
                    with open(f, encoding="utf-8") as fh:
                        templates[f.stem] = json.load(fh)
                except Exception:
                    pass
            if templates:
                self._cache[cat_dir.name] = templates

    @property
    def store_dir(self) -> Path:
        return self._dir

    def categories(self) -> list:
        return sorted(self._cache.keys())

    def templates_in(self, cat: str) -> dict:
        return dict(self._cache.get(cat, {}))

    def add(self, cat: str, name: str, device: dict):
        self._cache.setdefault(cat, {})[name] = copy.deepcopy(device)
        self._flush(cat, name, device)

    def delete(self, cat: str, name: str):
        if cat not in self._cache:
            return
        self._cache[cat].pop(name, None)
        p = self._dir / cat / f"{name}.json"
        p.unlink(missing_ok=True)
        if not self._cache[cat]:
            del self._cache[cat]
            try:
                (self._dir / cat).rmdir()
            except OSError:
                pass

    def move(self, from_cat: str, name: str, to_cat: str):
        device = self._cache.get(from_cat, {}).get(name)
        if device is None:
            return
        self.delete(from_cat, name)
        self.add(to_cat, name, device)

    def create_category(self, cat: str):
        if cat not in self._cache:
            self._cache[cat] = {}
            (self._dir / cat).mkdir(exist_ok=True)

    def delete_category(self, cat: str):
        self._cache.pop(cat, None)
        import shutil
        cat_dir = self._dir / cat
        if cat_dir.exists():
            shutil.rmtree(cat_dir, ignore_errors=True)

    def _flush(self, cat: str, name: str, device: dict):
        cat_dir = self._dir / cat
        cat_dir.mkdir(exist_ok=True)
        with open(cat_dir / f"{name}.json", "w", encoding="utf-8") as fh:
            json.dump(device, fh, indent=2, ensure_ascii=False)


# =============================================================================
# LeftPanel – file browser + template library
# =============================================================================
class LeftPanel(QWidget):
    sig_file_open = pyqtSignal(str)
    sig_config_new = pyqtSignal()
    sig_config_duplicate = pyqtSignal(str)
    sig_config_rename = pyqtSignal(str)
    sig_config_delete = pyqtSignal(str)
    sig_config_set_active = pyqtSignal(str)
    # (category, device_or_mgr_name): str mgr_name for builtin, dict for user
    sig_tmpl_add  = pyqtSignal(str, object)
    sig_folder_reloaded = pyqtSignal(str, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        # The left panel lives in the main QSplitter. Keep a practical
        # minimum, but let users resize it narrower or wider as needed.
        self.setMinimumWidth(100)
        self._store = TemplateStore()
        self._config_dir: object = None
        self._current_config_path: str = ""
        self._active_config_path: str = ""
        self._current_modified = False
        
        # Load plugin templates (Phase 3)
        self._plugin_templates: list = []
        self._plugin_template_errors: list = []
        try:
            if _MANAGER_CATALOG is not None:
                from imswitch.imcontrol.model.configeditor.templates import load_plugin_templates
                self._plugin_templates, self._plugin_template_errors = (
                    load_plugin_templates(_MANAGER_CATALOG)
                )
        except Exception:
            # If loading fails, just continue without plugin templates
            pass

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)

        # ── Config files ──
        # Keep folder actions next to the thing they operate on instead of in a
        # separate button row.  This also gives the file list the full remaining
        # height of the panel when templates are hidden.
        config_hdr = QHBoxLayout()
        config_hdr.setSpacing(4)
        hdr = QLabel("<b>Configs</b>")
        hdr.setTextFormat(Qt.RichText)
        hdr.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        config_hdr.addWidget(hdr)
        config_hdr.addStretch()

        open_btn = QPushButton("Folder…")
        open_btn.setFixedHeight(22)
        open_btn.setToolTip("Choose the folder containing ImSwitch setup JSON files")
        open_btn.clicked.connect(self._open_folder)
        config_hdr.addWidget(open_btn)

        reload_btn = QPushButton("Reload")
        reload_btn.setFixedHeight(22)
        reload_btn.setToolTip("Reload JSON configs from the currently selected folder")
        reload_btn.clicked.connect(self.reload_folder)
        config_hdr.addWidget(reload_btn)
        lay.addLayout(config_hdr)

        self._config_filter = QLineEdit()
        self._config_filter.setPlaceholderText("Filter configs…")
        self._config_filter.setClearButtonEnabled(True)
        self._config_filter.setToolTip("Filter the config list by filename")
        self._config_filter.textChanged.connect(self._apply_file_filter)
        lay.addWidget(self._config_filter)

        self._file_list = QTreeWidget()
        self._file_list.setHeaderHidden(True)
        self._file_list.setIndentation(12)
        self._file_list.setTextElideMode(Qt.ElideMiddle)
        self._file_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._file_list.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._file_list.header().setStretchLastSection(True)
        self._file_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self._file_list.customContextMenuRequested.connect(self._file_context_menu)
        self._file_list.itemSelectionChanged.connect(self._update_config_action_state)
        self._file_list.itemDoubleClicked.connect(self._on_file_double_click)
        lay.addWidget(self._file_list, 1)

        # Explicit file-management actions make the setup list useful as more
        # than a file picker.  The same actions are also available from the
        # context menu; shortcuts are scoped to the file list so Delete/F2
        # cannot accidentally act on a config while the device canvas is in use.
        self._config_actions = QWidget()
        config_actions_lay = QHBoxLayout(self._config_actions)
        config_actions_lay.setContentsMargins(0, 0, 0, 0)
        config_actions_lay.setSpacing(3)

        self._new_config_btn = QPushButton("New")
        self._new_config_btn.setToolTip("Create a new unsaved configuration (Ctrl+N)")
        self._new_config_btn.clicked.connect(self.sig_config_new.emit)
        config_actions_lay.addWidget(self._new_config_btn)

        self._duplicate_config_btn = QPushButton("Duplicate")
        self._duplicate_config_btn.setToolTip("Duplicate the selected config (Ctrl+D)")
        self._duplicate_config_btn.clicked.connect(self._request_duplicate)
        config_actions_lay.addWidget(self._duplicate_config_btn)

        self._rename_config_btn = QPushButton("Rename")
        self._rename_config_btn.setToolTip("Rename the selected config (F2)")
        self._rename_config_btn.clicked.connect(self._request_rename)
        config_actions_lay.addWidget(self._rename_config_btn)

        self._delete_config_btn = QPushButton("Delete")
        self._delete_config_btn.setToolTip("Delete the selected config (Delete)")
        self._delete_config_btn.clicked.connect(self._request_delete)
        config_actions_lay.addWidget(self._delete_config_btn)

        for button in (
            self._new_config_btn, self._duplicate_config_btn,
            self._rename_config_btn, self._delete_config_btn,
        ):
            button.setFixedHeight(23)
            button.setStyleSheet("font-size:8pt; padding:0 3px;")
        lay.addWidget(self._config_actions)

        self._install_file_list_shortcut("Ctrl+D", self._request_duplicate)
        self._install_file_list_shortcut("F2", self._request_rename)
        self._install_file_list_shortcut("Delete", self._request_delete)

        show_tmpl_btn = QPushButton("Device Templates…")
        show_tmpl_btn.setToolTip(
            "Show built-in and saved device templates. Templates add devices; "
            "they do not replace the current config."
        )
        show_tmpl_btn.clicked.connect(self._show_templates)
        lay.addWidget(show_tmpl_btn)
        self._show_tmpl_btn = show_tmpl_btn
        self._update_config_action_state()

        # ── Templates (hidden until explicitly requested) ──
        self._tmpl_container = QWidget()
        tmpl_container_lay = QVBoxLayout(self._tmpl_container)
        tmpl_container_lay.setContentsMargins(0, 0, 0, 0)
        tmpl_container_lay.setSpacing(6)

        tmpl_hdr = QHBoxLayout()
        tmpl_lbl = QLabel("<b>Templates</b>")
        tmpl_lbl.setTextFormat(Qt.RichText)
        tmpl_hdr.addWidget(tmpl_lbl)
        tmpl_hdr.addStretch()
        new_cat_btn = QPushButton("＋ Category")
        new_cat_btn.setFixedHeight(20)
        new_cat_btn.setStyleSheet("font-size:8pt; padding:0 4px;")
        new_cat_btn.setToolTip("Create a new user template category")
        new_cat_btn.clicked.connect(self._new_user_category)
        tmpl_hdr.addWidget(new_cat_btn)

        hide_tmpl_btn = QPushButton("Hide Templates")
        hide_tmpl_btn.setFixedHeight(20)
        hide_tmpl_btn.setStyleSheet("font-size:8pt; padding:0 4px;")
        hide_tmpl_btn.clicked.connect(self._hide_templates)
        tmpl_hdr.addWidget(hide_tmpl_btn)
        tmpl_container_lay.addLayout(tmpl_hdr)

        self._tmpl_tree = QTreeWidget()
        self._tmpl_tree.setHeaderHidden(True)
        self._tmpl_tree.setIndentation(12)
        self._tmpl_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tmpl_tree.customContextMenuRequested.connect(self._tmpl_context_menu)
        self._tmpl_tree.itemDoubleClicked.connect(self._on_tmpl_double_click)
        tmpl_container_lay.addWidget(self._tmpl_tree, 1)

        hint = QLabel("<span style='color:#888;font-size:8pt;'>Double-click to instantiate</span>")
        hint.setTextFormat(Qt.RichText)
        tmpl_container_lay.addWidget(hint)
        self._tmpl_container.setVisible(False)
        lay.addWidget(self._tmpl_container, 1)

        self._refresh_tmpl_tree()

    # ── File browser ──────────────────────────────────────────────────────
    def _install_file_list_shortcut(self, key: str, slot) -> None:
        action = QAction(self._file_list)
        action.setShortcut(key)
        action.setShortcutContext(Qt.WidgetShortcut)
        action.triggered.connect(slot)
        self._file_list.addAction(action)

    def selected_config_path(self) -> str:
        items = self._file_list.selectedItems()
        if not items or items[0].isHidden():
            return ""
        return str(items[0].data(0, Qt.UserRole) or "")

    def select_config_path(self, path: str) -> bool:
        for index in range(self._file_list.topLevelItemCount()):
            item = self._file_list.topLevelItem(index)
            item_path = str(item.data(0, Qt.UserRole) or "")
            if self._same_path(item_path, path):
                self._file_list.setCurrentItem(item)
                return True
        return False

    def _update_config_action_state(self) -> None:
        has_selection = bool(self.selected_config_path())
        self._duplicate_config_btn.setEnabled(has_selection)
        self._rename_config_btn.setEnabled(has_selection)
        self._delete_config_btn.setEnabled(has_selection)

    def _request_duplicate(self) -> None:
        path = self.selected_config_path()
        if path:
            self.sig_config_duplicate.emit(path)

    def _request_rename(self) -> None:
        path = self.selected_config_path()
        if path:
            self.sig_config_rename.emit(path)

    def _request_delete(self) -> None:
        path = self.selected_config_path()
        if path:
            self.sig_config_delete.emit(path)

    def _file_context_menu(self, pos) -> None:
        item = self._file_list.itemAt(pos)
        if item is None:
            menu = QMenu(self)
            new_action = menu.addAction("New Config")
            if menu.exec_(self._file_list.mapToGlobal(pos)) == new_action:
                self.sig_config_new.emit()
            return

        self._file_list.setCurrentItem(item)
        path = str(item.data(0, Qt.UserRole) or "")
        menu = QMenu(self)
        open_action = menu.addAction("Open")
        active_action = menu.addAction("Set as Active")
        active_action.setEnabled(
            not self._same_path(path, self._active_config_path)
        )
        menu.addSeparator()
        duplicate_action = menu.addAction("Duplicate…")
        rename_action = menu.addAction("Rename…")
        delete_action = menu.addAction("Delete…")
        action = menu.exec_(self._file_list.mapToGlobal(pos))
        if action == open_action:
            self.sig_file_open.emit(path)
        elif action == active_action:
            self.sig_config_set_active.emit(path)
        elif action == duplicate_action:
            self.sig_config_duplicate.emit(path)
        elif action == rename_action:
            self.sig_config_rename.emit(path)
        elif action == delete_action:
            self.sig_config_delete.emit(path)

    def _open_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Open Config Folder",
                                             str(_default_setup_dir()))
        if d:
            self._config_dir = d
            count = self._refresh_file_list(d)
            self.sig_folder_reloaded.emit(d, count)

    @property
    def config_dir(self) -> str:
        return str(self._config_dir or "")

    @staticmethod
    def _same_path(left: str, right: str) -> bool:
        if not left or not right:
            return False
        try:
            return Path(left).resolve() == Path(right).resolve()
        except Exception:
            return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))

    def set_config_identity(
        self, current_path: str = "", active_path: str = "", modified: bool = False
    ) -> None:
        """Update OPEN / ACTIVE / MODIFIED markers without rebuilding the list."""
        self._current_config_path = str(current_path or "")
        self._active_config_path = str(active_path or "")
        self._current_modified = bool(modified)
        for index in range(self._file_list.topLevelItemCount()):
            item = self._file_list.topLevelItem(index)
            path = str(item.data(0, Qt.UserRole) or "")
            self._decorate_file_item(item, Path(path))
        self._update_config_action_state()

    def _decorate_file_item(self, item: QTreeWidgetItem, path: Path) -> None:
        is_current = self._same_path(str(path), self._current_config_path)
        is_active = self._same_path(str(path), self._active_config_path)
        badges = []
        if is_current and self._current_modified:
            badges.append("MODIFIED")
        if is_active:
            badges.append("ACTIVE")
        suffix = "  " + " ".join(f"[{badge}]" for badge in badges) if badges else ""
        item.setText(0, f"{path.name}{suffix}")
        item.setToolTip(0, str(path))
        font = item.font(0)
        font.setBold(is_current)
        item.setFont(0, font)

    def _apply_file_filter(self, text: str = "") -> None:
        needle = str(text or "").strip().casefold()
        for index in range(self._file_list.topLevelItemCount()):
            item = self._file_list.topLevelItem(index)
            path = str(item.data(0, Qt.UserRole) or "")
            filename = Path(path).name.casefold()
            item.setHidden(bool(needle) and needle not in filename)
        self._update_config_action_state()

    def _refresh_file_list(self, folder: str) -> int:
        selected_before = self.selected_config_path()
        self._file_list.clear()
        count = 0
        preferred_item = None
        current_item = None
        for p in sorted(Path(folder).glob("*.json")):
            item = QTreeWidgetItem()
            item.setData(0, Qt.UserRole, str(p))
            self._decorate_file_item(item, p)
            self._file_list.addTopLevelItem(item)
            if selected_before and self._same_path(str(p), selected_before):
                preferred_item = item
            if self._current_config_path and self._same_path(
                str(p), self._current_config_path
            ):
                current_item = item
            count += 1
        if preferred_item is not None:
            self._file_list.setCurrentItem(preferred_item)
        elif current_item is not None:
            self._file_list.setCurrentItem(current_item)
        self._apply_file_filter(self._config_filter.text())
        return count

    def _on_file_double_click(self, item: QTreeWidgetItem, _col: int):
        path = item.data(0, Qt.UserRole)
        if path:
            self.sig_file_open.emit(path)

    def set_folder(self, folder: str):
        self._config_dir = folder
        self._refresh_file_list(folder)

    def reload_folder(self):
        if not self._config_dir:
            QMessageBox.information(self, "Reload Folder", "Open a config folder first.")
            return
        count = self._refresh_file_list(str(self._config_dir))
        self.sig_folder_reloaded.emit(str(self._config_dir), count)

    def _show_templates(self):
        self._refresh_tmpl_tree()
        self._tmpl_container.setVisible(True)
        self._show_tmpl_btn.setVisible(False)

    def _hide_templates(self):
        self._tmpl_container.setVisible(False)
        self._show_tmpl_btn.setVisible(True)

    # ── Template tree ─────────────────────────────────────────────────────
    def _refresh_tmpl_tree(self):
        self._tmpl_tree.clear()
        from PyQt5.QtGui import QColor as _QColor

        # ── Built-in section ──
        builtin_root = QTreeWidgetItem(["Built-in"])
        bold = QFont(); bold.setBold(True)
        builtin_root.setFont(0, bold)
        builtin_root.setForeground(0, _QColor("#444"))
        self._tmpl_tree.addTopLevelItem(builtin_root)

        for cat in DEVICE_CATS:
            managers = CAT_MANAGERS.get(cat, [])
            if not managers:
                continue
            cat_item = QTreeWidgetItem([CAT_LABEL[cat]])
            cat_item.setForeground(0, _QColor(CAT_COLOR[cat]))
            f = QFont(); f.setBold(True)
            cat_item.setFont(0, f)
            cat_item.setData(0, Qt.UserRole, ("builtin_cat", cat))
            builtin_root.addChild(cat_item)
            for mgr in managers:
                child = QTreeWidgetItem([_manager_display_name(mgr)])
                child.setData(0, Qt.UserRole, ("builtin", cat, mgr))
                cat_item.addChild(child)
            cat_item.setExpanded(True)
        builtin_root.setExpanded(True)

        # ── Plugin Templates section ──
        if self._plugin_templates or self._plugin_template_errors:
            plugin_root = QTreeWidgetItem(["Plugin Templates"])
            plugin_root.setFont(0, bold)
            plugin_root.setForeground(0, _QColor("#444"))
            self._tmpl_tree.addTopLevelItem(plugin_root)

            # Group templates by plugin name
            from collections import defaultdict
            by_plugin = defaultdict(list)
            for tmpl in self._plugin_templates:
                by_plugin[tmpl.plugin_name].append(tmpl)

            # Add each plugin's templates
            for plugin_name in sorted(by_plugin.keys()):
                plugin_templates = by_plugin[plugin_name]
                plugin_item = QTreeWidgetItem([f"{plugin_name}  ({len(plugin_templates)})"])
                f_plugin = QFont(); f_plugin.setBold(True)
                plugin_item.setFont(0, f_plugin)
                plugin_item.setForeground(0, _QColor("#555"))
                plugin_root.addChild(plugin_item)

                # Group by category within plugin
                by_cat = defaultdict(list)
                for tmpl in plugin_templates:
                    by_cat[tmpl.category].append(tmpl)

                for cat in sorted(by_cat.keys()):
                    cat_templates = by_cat[cat]
                    color = CAT_COLOR.get(cat, "#888")
                    label = CAT_LABEL.get(cat, cat)
                    cat_item = QTreeWidgetItem([f"{label}  ({len(cat_templates)})"])
                    cat_item.setForeground(0, _QColor(color))
                    f_cat = QFont(); f_cat.setBold(True)
                    cat_item.setFont(0, f_cat)
                    cat_item.setData(0, Qt.UserRole, ("plugin_cat", plugin_name, cat))
                    plugin_item.addChild(cat_item)

                    for tmpl in sorted(cat_templates, key=lambda t: t.name):
                        child = QTreeWidgetItem([tmpl.name])
                        child.setData(0, Qt.UserRole, ("plugin", cat, tmpl.name, tmpl.device))
                        cat_item.addChild(child)
                    cat_item.setExpanded(True)
                plugin_item.setExpanded(True)

            # Add errors section if any
            if self._plugin_template_errors:
                errors_item = QTreeWidgetItem([f"Errors  ({len(self._plugin_template_errors)})"])
                f_err = QFont(); f_err.setBold(True)
                errors_item.setFont(0, f_err)
                errors_item.setForeground(0, _QColor("#cc0000"))
                plugin_root.addChild(errors_item)

                for err in self._plugin_template_errors:
                    err_label = f"{err.manager_name}: {Path(err.resource).name}"
                    err_child = QTreeWidgetItem([err_label])
                    err_child.setForeground(0, _QColor("#999"))
                    err_child.setToolTip(0, err.message)
                    err_child.setData(0, Qt.UserRole, ("error", err))
                    errors_item.addChild(err_child)
                errors_item.setExpanded(False)

            plugin_root.setExpanded(True)

        # ── My Templates section ──
        user_cats = self._store.categories()
        if user_cats:
            my_root = QTreeWidgetItem(["My Templates"])
            my_root.setFont(0, bold)
            my_root.setForeground(0, _QColor("#444"))
            self._tmpl_tree.addTopLevelItem(my_root)

            for cat in user_cats:
                templates = self._store.templates_in(cat)
                color = CAT_COLOR.get(cat, "#888")
                label = CAT_LABEL.get(cat, cat)
                cat_item = QTreeWidgetItem([f"{label}  ({len(templates)})"])
                cat_item.setForeground(0, _QColor(color))
                f2 = QFont(); f2.setBold(True)
                cat_item.setFont(0, f2)
                cat_item.setData(0, Qt.UserRole, ("user_cat", cat))
                my_root.addChild(cat_item)
                for tname, tdata in templates.items():
                    child = QTreeWidgetItem([tname])
                    child.setData(0, Qt.UserRole, ("user", cat, tname, tdata))
                    cat_item.addChild(child)
                cat_item.setExpanded(True)
            my_root.setExpanded(True)

    def add_user_template(self, cat: str, name: str, device_data: dict):
        self._store.add(cat, name, device_data)
        self._refresh_tmpl_tree()

    def _new_user_category(self):
        name, ok = QInputDialog.getText(self, "New Template Category",
                                        "Category name (e.g. 'my_detectors'):")
        if ok and name.strip():
            self._store.create_category(name.strip())
            self._refresh_tmpl_tree()

    def _on_tmpl_double_click(self, item: QTreeWidgetItem, _col: int):
        payload = item.data(0, Qt.UserRole)
        if not payload:
            return
        kind = payload[0]
        if kind == "builtin":
            _k, cat, mgr = payload
            self.sig_tmpl_add.emit(cat, mgr)
        elif kind == "user":
            _k, cat, tname, tdata = payload
            self.sig_tmpl_add.emit(cat, copy.deepcopy(tdata))
        elif kind == "plugin":
            _k, cat, name, device_dict = payload
            self.sig_tmpl_add.emit(cat, copy.deepcopy(device_dict))

    def _tmpl_context_menu(self, pos):
        item = self._tmpl_tree.itemAt(pos)
        if not item:
            return
        payload = item.data(0, Qt.UserRole)
        if not payload:
            return
        kind = payload[0]
        menu = QMenu(self)

        if kind == "user":
            _k, cat, tname, tdata = payload
            a_move = menu.addAction("Move to Category…")
            menu.addSeparator()
            a_del = menu.addAction("Delete Template")
            action = menu.exec_(self._tmpl_tree.mapToGlobal(pos))
            if action == a_del:
                self._store.delete(cat, tname)
                self._refresh_tmpl_tree()
            elif action == a_move:
                self._move_template(cat, tname)

        elif kind == "user_cat":
            _k, cat = payload
            a_new = menu.addAction("New Category…")
            menu.addSeparator()
            a_del = menu.addAction("Delete Category")
            action = menu.exec_(self._tmpl_tree.mapToGlobal(pos))
            if action == a_new:
                self._new_user_category()
            elif action == a_del:
                templates = self._store.templates_in(cat)
                if templates:
                    QMessageBox.warning(self, "Delete Category",
                                        f"Category '{cat}' still has {len(templates)} template(s). "
                                        f"Delete or move them first.")
                else:
                    self._store.delete_category(cat)
                    self._refresh_tmpl_tree()

    def _move_template(self, from_cat: str, name: str):
        cats = self._store.categories()
        choices = cats + ["── New Category… ──"]
        choice, ok = QInputDialog.getItem(self, "Move Template",
                                          "Move to category:", choices, 0, False)
        if not ok:
            return
        if choice == "── New Category… ──":
            new_cat, ok2 = QInputDialog.getText(self, "New Category", "Category name:")
            if not ok2 or not new_cat.strip():
                return
            choice = new_cat.strip()
        self._store.move(from_cat, name, choice)
        self._refresh_tmpl_tree()


# =============================================================================
# ValidationPanel – DAQ conflict summary
# =============================================================================
class ValidationPanel(QFrame):
    sig_fix_section = pyqtSignal(str)  # emitted with section_key when user clicks "Configure…" link

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setMaximumHeight(180)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lbl = QLabel("<b>Validation</b>")
        lbl.setTextFormat(Qt.RichText)
        lay.addWidget(lbl)
        # Use a scrollable label so long lists are reachable.
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._text = QLabel("No config loaded.")
        self._text.setWordWrap(True)
        self._text.setTextFormat(Qt.RichText)
        self._text.setStyleSheet("font-size:8pt;")
        self._text.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._text.setOpenExternalLinks(False)  # handle links ourselves
        self._text.linkActivated.connect(self._on_link_activated)
        self._scroll.setWidget(self._text)
        lay.addWidget(self._scroll)

    def _on_link_activated(self, link: str):
        """Handle clicks on validation warning links."""
        if link.startswith("fixsection:"):
            section_key = link.split(":", 1)[1]
            self.sig_fix_section.emit(section_key)

    def validate(self, data: dict):
        # Try to use model validation service (Phase 4)
        try:
            from imswitch.imcontrol.model.plugins.registry import build_default_registry
            from imswitch.imcontrol.model.plugins.validation import (
                validate_setup_data,
                ValidationContext,
            )
            
            # Build registry
            registry = build_default_registry(discover=True)
            
            # Build context with editor-supplied inputs
            context = ValidationContext(
                widget_requires_section=_WIDGET_REQUIRES_SECTION,
                known_reconstructor_ids=tuple(_section_option_values("processing", "reconstructors")),
                known_processor_ids=tuple(_section_option_values("processing", "processors")),
            )
            
            # Validate using model
            report = validate_setup_data(data, registry, context=context)
            
            # Render diagnostics to HTML
            self._render_diagnostics(report.diagnostics, data)
            
        except Exception as e:
            # Graceful degradation: fall back to legacy validation
            import logging
            logging.getLogger(__name__).debug(f"Model validation unavailable, using legacy: {e}")
            self._validate_legacy(data)
    
    def _render_diagnostics(self, diagnostics: list, data: dict):
        """Render model diagnostics to HTML."""
        errors: list = []
        warnings: list = []
        notes: list = []
        
        for diag in diagnostics:
            msg = _diagnostic_message_to_html(diag)

            if diag.severity == "error":
                errors.append(msg)
            elif diag.severity == "warning":
                warnings.append(msg)
            else:
                notes.append(msg)
        
        present = [
            CAT_LABEL[c] for c in DEVICE_CATS
            if data.get(c) or (c in DISABLEABLE_DEVICE_CATS and _device_section(data, c, enabled=False))
        ]
        devcount = sum(len(data.get(c) or {}) for c in DEVICE_CATS)
        disabled_count = sum(
            len(_device_section(data, c, enabled=False)) for c in DISABLEABLE_DEVICE_CATS
        )
        
        lines: list = []
        for m in errors:
            lines.append(f"<span style='color:#C04000;'>⚠ {m}</span>")
        for m in warnings:
            lines.append(f"<span style='color:#B07000;'>⚠ {m}</span>")
        for m in notes:
            lines.append(f"<span style='color:#666;'>ℹ {m}</span>")
        
        if lines:
            self._text.setText("<br>".join(lines))
            self._text.setStyleSheet("font-size:8pt;")
        else:
            cats_str = ", ".join(present) if present else "none"
            disabled_text = f", {disabled_count} disabled" if disabled_count else ""
            self._text.setText(
                f"<span style='color:#3A8B3A;'>✓ No issues</span>"
                f"<span style='color:#666;'> — {devcount} active devices{disabled_text}"
                f" ({cats_str})</span>"
            )
            self._text.setStyleSheet("font-size:8pt;")
    
    def _validate_legacy(self, data: dict):
        """Fallback when model validation is unavailable (graceful degradation)."""
        self._text.setText(
            "<span style='color:#B07000;'>⚠ Validation unavailable "
            "(model import failed)</span>"
        )
        self._text.setStyleSheet("font-size:8pt;")


# =============================================================================
# MainWindow
# =============================================================================
class MainWindow(QMainWindow):
    # Well-known location of imcontrol_options.json
    _OPTIONS_SEARCH_ROOTS = [
        _default_config_dir(),
        Path.home() / "Documents" / "ImSwitchConfig" / "config",
        Path("/") / "etc" / "imswitch",
    ]

    #: Emitted once the window has been closed for good, after any
    #: unsaved-changes prompt has been answered. Embedders use it to react to
    #: what the session wrote; a standalone run has nothing connected.
    sig_closed = pyqtSignal()

    def __init__(self, start_folder: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("ImSwitch Config Studio")
        self.resize(1280, 780)
        self._data: dict = {}
        self._path: str = ""
        self._modified = False
        self._options_path: str = ""   # path to imcontrol_options.json if found
        self._copied_device: object = None  # (category, name, device_dict, enabled)
        # Compare mode keeps the normal editor document pinned on the left and
        # loads one additional saved config on the right. The shared property
        # editor follows whichever pane was selected last.
        self._compare_mode = False
        self._compare_data: dict = {}
        self._compare_path: str = ""
        self._compare_modified = False
        self._compare_view = "cards"
        self._active_pane = "primary"
        self._editor_pane = "primary"
        # What this editing session changed on disk. A running ImSwitch reads
        # its setup once at startup, so an embedder needs to know whether
        # anything it is already using was rewritten -- see saved_files() and
        # active_config_changed.
        self._saved_files: set = set()
        self._active_config_changed = False

        self._adopt_dark_theme()
        self._build_toolbar()
        self._build_ui()
        self._build_status_bar()

        if start_folder and os.path.isdir(start_folder):
            self._left.set_folder(start_folder)

        self._detect_options_file()
        self._open_active_config(start_folder)

    def _adopt_dark_theme(self):
        """Look the same inside a dark host as when run on its own.

        Standalone, ``main()`` puts the editor's theme on the application.
        Opened from ImSwitch, the application's style sheet is qdarkstyle, so
        the editor puts its own theme on its window instead, where it wins
        over the application's and reaches nothing else of ImSwitch. A light
        host is left alone: the cards follow ``is_dark_mode()``.

        ImSwitch's 10 px font stays. Any ``font-size`` rule overrides the
        sizes the editor sets with ``setFont``, so no rule here could give
        back the standalone sizes -- only replace one uniform size with
        another.
        """
        app = QApplication.instance()
        if app is None or app.styleSheet() == _DARK_STYLESHEET or not is_dark_mode():
            return
        self.setPalette(_dark_palette(self.palette()))
        self.setStyleSheet(_HOSTED_OVERRIDES + _DARK_STYLESHEET)

    # ── Build UI ──────────────────────────────────────────────────────────
    def _build_toolbar(self):
        tb = QToolBar()
        tb.setMovable(False)
        tb.setStyleSheet("QToolBar { spacing:4px; padding:4px; }")
        self.addToolBar(tb)

        def _btn(label, slot, tip=""):
            a = QAction(label, self)
            a.setToolTip(tip)
            a.triggered.connect(slot)
            tb.addAction(a)
            return a

        _btn("New",        self._new_file,   "Create blank config")
        _btn("Open File…", self._open_file,  "Open a JSON config file")
        _btn("Save",       self._save_active_pane,  "Save the active config pane (Ctrl+S)")
        _btn("Save As…",   self._save_as,    "Save to a new file")
        _btn(
            "Paste Device", lambda: self._paste_device(),
            "Paste the copied device into the active config pane"
        )
        tb.addSeparator()
        self._compare_action = _btn(
            "Compare", self._toggle_compare_mode,
            "Show a second config beside the current one"
        )
        self._compare_action.setCheckable(True)
        tb.addSeparator()
        _btn("Validate",   self._run_validation, "Check for DAQ conflicts")

        self.addAction(self._make_shortcut("Ctrl+S", self._save_active_pane))
        self.addAction(self._make_shortcut("Ctrl+N", self._new_file))

    def _make_shortcut(self, key, slot):
        a = QAction(self)
        a.setShortcut(key)
        a.triggered.connect(slot)
        return a

    def _build_ui(self):
        root = QWidget()
        root_lay = QVBoxLayout(root)
        root_lay.setContentsMargins(0, 0, 0, 0)
        root_lay.setSpacing(0)
        self.setCentralWidget(root)

        # ── Config identity bar ──
        # "Editing" is the primary identity; "Active" is deliberately a
        # secondary line.  The old active-only banner visually implied that the
        # active file was necessarily the file being edited.
        colors = get_themed_colors(is_dark_mode())
        self._active_banner = QFrame()
        self._active_banner.setObjectName("configIdentityBar")
        self._active_banner.setStyleSheet(
            f"QFrame#configIdentityBar {{ background:{colors['header_bg']}; "
            f"border-bottom:1px solid {colors['card_border']}; }}"
        )
        self._active_banner.setFixedHeight(60)
        bl = QVBoxLayout(self._active_banner)
        bl.setContentsMargins(10, 4, 10, 4)
        bl.setSpacing(2)

        editing_row = QHBoxLayout()
        editing_row.setContentsMargins(0, 0, 0, 0)
        editing_row.setSpacing(6)
        self._editing_lbl = QLabel()
        self._editing_lbl.setTextFormat(Qt.RichText)
        self._editing_lbl.setStyleSheet("font-size:10pt;")
        editing_row.addWidget(self._editing_lbl)

        self._reset_editing_btn = QPushButton("Reset")
        self._reset_editing_btn.setFixedHeight(22)
        self._reset_editing_btn.setStyleSheet(
            "QPushButton { color:#D98C2B; border:1px solid #D98C2B; "
            "border-radius:3px; padding:1px 7px; font-weight:600; } "
            "QPushButton:hover { background:rgba(217, 140, 43, 0.12); } "
            "QPushButton:pressed { background:rgba(217, 140, 43, 0.20); }"
        )
        self._reset_editing_btn.setToolTip(
            "Discard unsaved changes and restore the last saved version"
        )
        self._reset_editing_btn.clicked.connect(self._reset_editing_config)
        editing_row.addWidget(self._reset_editing_btn)

        self._set_active_btn = QPushButton("Set as Active")
        self._set_active_btn.setFixedHeight(22)
        self._set_active_btn.setToolTip(
            "Make the config currently being edited the active ImSwitch setup"
        )
        self._set_active_btn.clicked.connect(self._set_as_active_config)
        editing_row.addWidget(self._set_active_btn)
        editing_row.addStretch(1)
        bl.addLayout(editing_row)

        active_row = QHBoxLayout()
        active_row.setContentsMargins(0, 0, 0, 0)
        active_row.setSpacing(6)
        self._active_lbl = QLabel()
        self._active_lbl.setTextFormat(Qt.RichText)
        self._active_lbl.setStyleSheet("font-size:8pt;")
        active_row.addWidget(self._active_lbl)

        self._select_active_btn = QPushButton("Select")
        self._select_active_btn.setFixedHeight(22)
        self._select_active_btn.setToolTip(
            "Open the active ImSwitch config in the editor"
        )
        self._select_active_btn.clicked.connect(self._select_active_config)
        active_row.addWidget(self._select_active_btn)
        active_row.addStretch(1)
        bl.addLayout(active_row)

        root_lay.addWidget(self._active_banner)

        splitter = QSplitter(Qt.Horizontal)
        root_lay.addWidget(splitter, 1)

        # Left
        self._left = LeftPanel()
        self._left.sig_file_open.connect(self._open_file_from_list)
        self._left.sig_config_new.connect(self._new_file)
        self._left.sig_config_duplicate.connect(self._duplicate_config_file)
        self._left.sig_config_rename.connect(self._rename_config_file)
        self._left.sig_config_delete.connect(self._delete_config_file)
        self._left.sig_config_set_active.connect(self._set_path_as_active_config)
        self._left.sig_tmpl_add.connect(self._add_from_template)
        self._left.sig_folder_reloaded.connect(self._on_folder_reloaded)
        splitter.addWidget(self._left)

        # Centre + validation. ConfigPane is reused for both halves of the
        # embedded compare workspace; normal mode simply hides the reference
        # pane and the primary pane's small header.
        centre = QWidget()
        cl = QVBoxLayout(centre)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)

        self._compare_view_bar = QFrame()
        self._compare_view_bar.setStyleSheet(
            f"QFrame {{ background:{colors['header_bg']}; "
            f"border-bottom:1px solid {colors['card_border']}; }}"
        )
        view_bar_lay = QHBoxLayout(self._compare_view_bar)
        view_bar_lay.setContentsMargins(8, 4, 8, 4)
        view_bar_lay.setSpacing(4)
        view_bar_lay.addWidget(QLabel("<b>Compare view:</b>"))
        self._cards_view_btn = QPushButton("Cards")
        self._cards_view_btn.setCheckable(True)
        self._cards_view_btn.setFixedHeight(23)
        self._cards_view_btn.clicked.connect(lambda: self._set_compare_view("cards"))
        view_bar_lay.addWidget(self._cards_view_btn)
        self._summary_view_btn = QPushButton("Summary")
        self._summary_view_btn.setCheckable(True)
        self._summary_view_btn.setFixedHeight(23)
        self._summary_view_btn.clicked.connect(lambda: self._set_compare_view("summary"))
        view_bar_lay.addWidget(self._summary_view_btn)
        view_bar_lay.addStretch()
        self._compare_view_bar.setVisible(False)
        cl.addWidget(self._compare_view_bar)

        self._pane_splitter = QSplitter(Qt.Horizontal)
        self._pane_splitter.setChildrenCollapsible(False)

        self._primary_pane = ConfigPane("Editing")
        self._primary_pane.set_header_visible(False)
        self._primary_pane.sig_paste_requested.connect(
            lambda: self._paste_device("primary")
        )
        self._primary_pane.sig_save_requested.connect(self._save_file)
        self._canvas = self._primary_pane.canvas  # compatibility alias
        self._connect_canvas(self._primary_pane.canvas, "primary")
        self._pane_splitter.addWidget(self._primary_pane)

        self._compare_pane = ConfigPane("Compare with", selector=True, compact=True)
        self._compare_pane.sig_config_requested.connect(self._load_compare_file)
        self._compare_pane.sig_paste_requested.connect(
            lambda: self._paste_device("compare")
        )
        self._compare_pane.sig_save_requested.connect(self._save_compare_file)
        self._compare_canvas = self._compare_pane.canvas
        self._connect_canvas(self._compare_canvas, "compare")
        self._compare_pane.setVisible(False)
        self._pane_splitter.addWidget(self._compare_pane)

        cl.addWidget(self._pane_splitter, 1)

        self._compare_summary = ComparisonSummary()
        self._compare_summary.sig_config_requested.connect(self._load_compare_file)
        self._compare_summary.sig_copy_to_other.connect(self._copy_device_to_other)
        self._compare_summary.sig_device_selected.connect(self._on_summary_device_selected)
        self._compare_summary.setVisible(False)
        cl.addWidget(self._compare_summary, 1)

        self._val_panel = ValidationPanel()
        self._val_panel.sig_fix_section.connect(self._on_fix_section_from_validation)
        cl.addWidget(self._val_panel)
        splitter.addWidget(centre)

        # Right: one shared inspector follows the last selected pane. This is
        # deliberately not duplicated in Compare mode; two inspectors would
        # consume most of the horizontal space we just gained.
        editor_wrap = QWidget()
        editor_lay = QVBoxLayout(editor_wrap)
        editor_lay.setContentsMargins(0, 0, 0, 0)
        editor_lay.setSpacing(0)
        self._editor_source_lbl = QLabel()
        self._editor_source_lbl.setTextFormat(Qt.RichText)
        self._editor_source_lbl.setStyleSheet("padding:5px 8px; font-size:8pt;")
        self._editor_source_lbl.setVisible(False)
        editor_lay.addWidget(self._editor_source_lbl)
        self._editor = PropertyEditor()
        self._editor.sig_apply.connect(self._on_editor_apply)
        self._editor.sig_rename.connect(self._on_device_rename)
        self._editor.sig_enabled_changed.connect(self._on_device_enabled_changed)
        self._editor.sig_modified.connect(self._on_extras_modified)
        self._editor.sig_show_config.connect(self._show_config_settings)
        editor_lay.addWidget(self._editor, 1)
        splitter.addWidget(editor_wrap)
        self._set_editor_source("primary")

        splitter.setCollapsible(2, True)
        splitter.setSizes([242, 758, 340])
        self._main_splitter = splitter

    def _build_status_bar(self):
        sb = QStatusBar()
        self.setStatusBar(sb)
        self._status = QLabel("Ready")
        sb.addWidget(self._status)

    # ── Config panes / embedded compare mode ──────────────────────────────
    def _connect_canvas(self, canvas: DeviceCanvas, pane: str) -> None:
        canvas.sig_device_selected.connect(
            lambda cat, name, p=pane: self._on_device_selected(cat, name, p)
        )
        canvas.sig_device_deleted.connect(
            lambda cat, name, p=pane: self._on_device_deleted(cat, name, p)
        )
        canvas.sig_device_duped.connect(
            lambda cat, name, p=pane: self._on_device_duped_or_add(cat, name, p)
        )
        canvas.sig_device_renamed.connect(
            lambda cat, name, p=pane: self._on_device_rename_from_canvas(cat, name, p)
        )
        canvas.sig_save_tmpl.connect(
            lambda cat, name, p=pane: self._on_save_template(cat, name, p)
        )
        canvas.sig_device_copied.connect(
            lambda cat, name, p=pane: self._copy_device(cat, name, p)
        )
        canvas.sig_copy_to_other.connect(
            lambda cat, name, p=pane: self._copy_device_to_other(cat, name, p)
        )
        canvas.sig_device_enabled_changed.connect(
            lambda cat, name, enabled, p=pane:
                self._on_device_enabled_changed(cat, name, enabled, p)
        )
        canvas.sig_category_settings.connect(
            lambda cat, p=pane: self._on_category_settings(cat, p)
        )
        if pane == "primary":
            canvas.sig_improcess_edit.connect(self._on_improcess_edit)
            canvas.sig_improcess_remove.connect(self._on_improcess_remove)

    def _pane_data(self, pane: str) -> dict:
        return self._compare_data if pane == "compare" else self._data

    def _pane_canvas(self, pane: str) -> DeviceCanvas:
        return self._compare_canvas if pane == "compare" else self._canvas

    def _pane_path(self, pane: str) -> str:
        return self._compare_path if pane == "compare" else self._path

    def _pane_name(self, pane: str) -> str:
        path = self._pane_path(pane)
        return Path(path).name if path else ("compare config" if pane == "compare" else "new config")

    def _set_compare_view(self, view: str) -> None:
        """Switch between the editable card workspace and compact summary."""
        if view not in {"cards", "summary"}:
            return
        self._compare_view = view
        cards = view == "cards"
        self._cards_view_btn.blockSignals(True)
        self._summary_view_btn.blockSignals(True)
        self._cards_view_btn.setChecked(cards)
        self._summary_view_btn.setChecked(not cards)
        self._cards_view_btn.blockSignals(False)
        self._summary_view_btn.blockSignals(False)
        self._pane_splitter.setVisible(cards or not self._compare_mode)
        self._compare_summary.setVisible(self._compare_mode and not cards)
        if self._compare_mode:
            self._status.setText(
                "Compare mode: card workspace" if cards else "Compare mode: summary"
            )

    def _on_summary_device_selected(self, cat: str, name: str, pane: str) -> None:
        """Point the shared inspector at a device chosen from Summary."""
        self._canvas.select_device(cat, name)
        self._compare_canvas.select_device(cat, name)
        self._on_device_selected(cat, name, pane)

    def _activate_pane(self, pane: str) -> None:
        self._active_pane = pane if pane == "compare" and self._compare_mode else "primary"
        if hasattr(self, "_primary_pane"):
            self._primary_pane.set_active(self._active_pane == "primary")
        if hasattr(self, "_compare_pane"):
            self._compare_pane.set_active(self._active_pane == "compare")

    def _set_editor_source(self, pane: str) -> None:
        self._editor_pane = pane if pane == "compare" and self._compare_mode else "primary"
        if hasattr(self, "_editor_source_lbl"):
            self._editor_source_lbl.setText(
                f"<b>Properties:</b>&nbsp;&nbsp;{self._pane_name(self._editor_pane)}"
            )
            self._editor_source_lbl.setToolTip(self._pane_path(self._editor_pane))

    def _load_inspector_for_pane(self, pane: str) -> None:
        self._activate_pane(pane)
        self._set_editor_source(pane)
        canvas = self._pane_canvas(pane)
        selected = canvas._selected
        if selected:
            cat, name = selected
            device, enabled = _locate_device(self._pane_data(pane), cat, name)
            if device is not None:
                self._editor.load_device(
                    cat, name, device, setup=self._pane_data(pane),
                    enabled=True if enabled is None else enabled,
                )
                return
        self._editor.load_config_extras(self._pane_data(pane))

    def _mark_pane_modified(self, pane: str) -> None:
        if pane == "compare":
            self._compare_modified = True
            self._compare_pane.set_modified(True)
        else:
            self._mark_modified()

    def _refresh_pane(self, pane: str) -> None:
        if self._compare_mode and self._compare_path:
            self._refresh_comparison_view()
        else:
            canvas = self._pane_canvas(pane)
            canvas.load(self._pane_data(pane))
        if pane == "primary":
            self._primary_pane.set_identity(self._path, modified=self._modified)
        else:
            self._compare_pane.set_modified(self._compare_modified)

    def _refresh_comparison_view(self) -> None:
        """Rebuild both card panes with one shared device comparison map."""
        if not self._compare_mode or not self._compare_path:
            return
        selected = self._pane_canvas(self._active_pane)._selected
        if selected is None:
            selected = self._canvas._selected or self._compare_canvas._selected

        states = _compare_configs(self._data, self._compare_data)
        self._canvas.load(
            self._data, comparison_states=states, compare_side="left"
        )
        self._compare_canvas.load(
            self._compare_data, comparison_states=states, compare_side="right"
        )
        self._compare_summary.load(
            self._data, self._compare_data, states,
            left_path=self._path, right_path=self._compare_path,
        )
        if selected:
            cat, name = selected
            self._canvas.select_device(cat, name)
            self._compare_canvas.select_device(cat, name)

        self._primary_pane.set_identity(self._path, modified=self._modified)
        self._compare_pane.set_modified(self._compare_modified)

    def _validate_pane(self, pane: str) -> None:
        # The validation panel belongs to the main editing document. Keeping it
        # single avoids presenting a second independent validation UI in 3A.
        if pane == "primary":
            self._val_panel.validate(self._data)

    def _available_compare_paths(self):
        folder = self._left.config_dir
        if not folder:
            return []
        paths = []
        for path in sorted(Path(folder).glob("*.json")):
            if self._path and LeftPanel._same_path(str(path), self._path):
                continue
            paths.append(str(path))
        return paths

    def _refresh_compare_choices(self, *, auto_load: bool = True) -> None:
        paths = self._available_compare_paths()
        keep = (
            self._compare_path
            if self._compare_path
            and any(LeftPanel._same_path(self._compare_path, p) for p in paths)
            else ""
        )
        target = keep or (paths[0] if paths else "")
        self._compare_pane.set_selector_paths(paths, target)
        self._compare_summary.set_selector_paths(paths, target)
        if not auto_load:
            return
        if target:
            if not self._compare_path or not LeftPanel._same_path(target, self._compare_path):
                self._load_compare_file(target, force=True)
        else:
            self._compare_path = ""
            self._compare_data = {}
            self._compare_modified = False
            self._compare_canvas.load({})
            self._compare_summary.load(
                self._data, {}, {}, left_path=self._path, right_path=""
            )
            if self._compare_mode:
                self._canvas.clear_comparison()
            self._compare_pane.set_modified(False)

    def _toggle_compare_mode(self, checked=False) -> None:
        if checked:
            self._enter_compare_mode()
            return
        if not self._leave_compare_mode():
            self._compare_action.blockSignals(True)
            self._compare_action.setChecked(True)
            self._compare_action.blockSignals(False)

    def _enter_compare_mode(self) -> None:
        if self._compare_mode:
            return
        self._compare_mode = True
        self._compare_action.blockSignals(True)
        self._compare_action.setChecked(True)
        self._compare_action.blockSignals(False)
        self._primary_pane.set_header_visible(True)
        self._primary_pane.set_identity(self._path, modified=self._modified)
        self._canvas.set_compact(True)
        self._compare_pane.setVisible(True)
        self._compare_view_bar.setVisible(True)
        self._set_compare_view("cards")
        self._editor_source_lbl.setVisible(True)
        self._val_panel.setVisible(False)
        self._refresh_compare_choices(auto_load=True)
        self._pane_splitter.setSizes([1, 1])
        self._load_inspector_for_pane("primary")
        self._status.setText("Compare mode: current config pinned on the left")

    def _leave_compare_mode(self) -> bool:
        if not self._compare_mode:
            return True
        if not self._confirm_compare_changes("leaving Compare mode"):
            return False
        self._compare_mode = False
        self._compare_pane.setVisible(False)
        self._compare_view_bar.setVisible(False)
        self._compare_summary.setVisible(False)
        self._pane_splitter.setVisible(True)
        self._primary_pane.set_header_visible(False)
        self._canvas.clear_comparison()
        self._canvas.set_compact(False)
        self._editor_source_lbl.setVisible(False)
        self._val_panel.setVisible(True)
        self._load_inspector_for_pane("primary")
        self._compare_action.blockSignals(True)
        self._compare_action.setChecked(False)
        self._compare_action.blockSignals(False)
        self._status.setText("Compare mode closed")
        return True

    def _confirm_compare_changes(self, action: str) -> bool:
        if not self._compare_modified:
            return True
        response = QMessageBox.question(
            self,
            "Unsaved Compare Changes",
            f"Save changes to '{self._pane_name('compare')}' before {action}?",
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            QMessageBox.Yes,
        )
        if response == QMessageBox.Cancel:
            return False
        if response == QMessageBox.Yes:
            return self._save_compare_file()

        # Discard means discard the staged in-memory copy as well, otherwise
        # leaving and re-entering Compare would resurrect changes the user just
        # chose to throw away.
        try:
            if self._compare_path:
                if _io_module is not None:
                    self._compare_data = _io_module.load_config_file(self._compare_path)
                else:
                    with open(self._compare_path, encoding="utf-8") as fh:
                        self._compare_data = json.load(fh)
                self._refresh_comparison_view()
        except Exception:
            self._compare_data = {}
            self._compare_canvas.load({})
            if self._compare_mode:
                self._canvas.clear_comparison()
        self._compare_modified = False
        self._compare_pane.set_modified(False)
        return True

    def _load_compare_file(self, path: str, *, force: bool = False) -> None:
        if not path:
            return
        if self._path and LeftPanel._same_path(path, self._path):
            return
        if (
            not force
            and self._compare_path
            and not LeftPanel._same_path(path, self._compare_path)
            and not self._confirm_compare_changes("switching reference config")
        ):
            self._compare_pane.set_selected_path(self._compare_path)
            return
        try:
            if _io_module is not None:
                data = _io_module.load_config_file(path)
            else:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
        except Exception as exc:
            QMessageBox.critical(self, "Load Compare Config", str(exc))
            if self._compare_path:
                self._compare_pane.set_selected_path(self._compare_path)
            return

        self._compare_data = data
        self._compare_path = path
        self._compare_modified = False
        self._compare_pane.set_selected_path(path)
        self._compare_summary.set_selected_path(path)
        self._compare_pane.set_modified(False)
        self._refresh_comparison_view()
        self._activate_pane("compare")
        self._set_editor_source("compare")
        self._editor.load_config_extras(self._compare_data)
        self._status.setText(f"Compare with: {Path(path).name}")

    def _save_compare_file(self) -> bool:
        if not self._compare_path:
            return False
        try:
            if _io_module is not None:
                data_to_write = _io_module.prepare_for_save(self._compare_data)
            else:
                data_to_write = copy.deepcopy(self._compare_data)
                if "others" in data_to_write and not data_to_write["others"]:
                    del data_to_write["others"]
            with open(self._compare_path, "w", encoding="utf-8") as fh:
                json.dump(data_to_write, fh, indent=2, ensure_ascii=False)
            self._compare_modified = False
            self._compare_pane.set_modified(False)
            self._note_saved(self._compare_path)
            self._status.setText(f"Saved compare config: {self._compare_path}")
            return True
        except Exception as exc:
            QMessageBox.critical(self, "Save Compare Config", str(exc))
            return False

    # ── File operations ───────────────────────────────────────────────────
    def _new_file(self):
        if not self._confirm_discard():
            return
        self._data = {cat: {} for cat in DEVICE_CATS}
        self._data["availableWidgets"] = []
        self._path = ""
        self._set_modified(False)
        self._refresh_canvas()
        self._activate_pane("primary")
        self._set_editor_source("primary")
        self._editor.load_config_extras(self._data)
        if self._compare_mode:
            self._refresh_compare_choices(auto_load=True)
        self._status.setText("New config (unsaved)")

    def _open_file(self):
        if not self._confirm_discard():
            return
        start_dir = str(Path(self._path).parent if self._path else _default_setup_dir())
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Config", start_dir, "JSON files (*.json)"
        )
        if path:
            self._load_file(path)

    def _open_file_from_list(self, path: str) -> None:
        if self._path and LeftPanel._same_path(path, self._path):
            return
        if not self._confirm_discard():
            return
        self._load_file(path)

    def _load_file(self, path: str):
        compare_conflict = False
        if self._compare_mode and self._compare_path:
            compare_conflict = LeftPanel._same_path(path, self._compare_path)
            try:
                compare_conflict = compare_conflict or (
                    Path(path).parent.resolve() != Path(self._compare_path).parent.resolve()
                )
            except Exception:
                pass
        if (
            compare_conflict and self._compare_modified
            and not self._confirm_compare_changes("opening this file as the main config")
        ):
            return
        try:
            # Delegate to shared helper when available, otherwise fallback
            if _io_module is not None:
                self._data = _io_module.load_config_file(path)
            else:
                with open(path, encoding="utf-8") as fh:
                    self._data = json.load(fh)
            self._path = path
            self._left.set_folder(str(Path(path).parent))
            self._set_modified(False)
            self._left.select_config_path(path)
            self._refresh_canvas()
            self._activate_pane("primary")
            self._set_editor_source("primary")
            self._editor.load_config_extras(self._data)
            self._val_panel.validate(self._data)
            if self._compare_mode:
                self._refresh_compare_choices(auto_load=True)
            self._status.setText(f"Loaded: {path}")
            # Try to find options file near the loaded config if not yet found
            if not self._options_path:
                self._detect_options_file()
            else:
                self._update_active_banner()
        except Exception as e:
            QMessageBox.critical(self, "Load Error", str(e))

    def _on_folder_reloaded(self, folder: str, count: int):
        if self._compare_mode:
            compare_outside_folder = (
                self._compare_path
                and Path(self._compare_path).parent.resolve() != Path(folder).resolve()
            )
            if (
                compare_outside_folder and self._compare_modified
                and not self._confirm_compare_changes("changing config folders")
            ):
                self._status.setText(
                    "Folder changed; keeping the unsaved comparison config open"
                )
                return
            self._refresh_compare_choices(auto_load=True)
        self._status.setText(f"Reloaded {count} JSON config(s) from {folder}")

    @staticmethod
    def _normalise_config_filename(value: str) -> str:
        """Return a safe config basename, adding .json when omitted."""
        name = str(value or "").strip()
        if not name:
            return ""
        if Path(name).name != name or "/" in name or "\\" in name:
            return ""
        if not name.lower().endswith(".json"):
            name += ".json"
        return name

    def _prompt_config_target(
        self, source: Path, title: str, initial_name: str
    ) -> object:
        value, ok = QInputDialog.getText(
            self, title, "File name:", text=initial_name
        )
        if not ok:
            return None
        name = self._normalise_config_filename(value)
        if not name:
            QMessageBox.warning(
                self, title, "Enter a file name, not a path."
            )
            return None
        target = source.parent / name
        if target.exists() and not LeftPanel._same_path(str(source), str(target)):
            QMessageBox.warning(
                self, title, f"A config named '{target.name}' already exists."
            )
            return None
        return target

    @staticmethod
    def _resolved_path(path: object) -> str:
        try:
            return str(Path(path).resolve())
        except Exception:
            return str(path)

    def _duplicate_config_file(self, path: str) -> None:
        source = Path(path)
        if not source.is_file():
            QMessageBox.warning(self, "Duplicate Config", "The selected config no longer exists.")
            self._left.reload_folder()
            return

        if self._path and LeftPanel._same_path(str(source), self._path) and self._modified:
            response = QMessageBox.question(
                self,
                "Unsaved Changes",
                "This config has unsaved changes. Save them before duplicating?\n\n"
                "Choose No to duplicate the last saved version.",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                QMessageBox.Yes,
            )
            if response == QMessageBox.Cancel:
                return
            if response == QMessageBox.Yes:
                self._save_file()
                if self._modified:
                    return

        if (
            self._compare_path
            and LeftPanel._same_path(str(source), self._compare_path)
            and self._compare_modified
        ):
            if not self._confirm_compare_changes("duplicating this config"):
                return

        stem = source.stem + "_copy"
        initial = f"{stem}.json"
        index = 2
        while (source.parent / initial).exists():
            initial = f"{stem}{index}.json"
            index += 1
        target = self._prompt_config_target(source, "Duplicate Config", initial)
        if target is None or LeftPanel._same_path(str(source), str(target)):
            return
        try:
            shutil.copy2(source, target)
            self._note_saved(str(target))
            self._left.set_folder(str(source.parent))
            self._left.select_config_path(str(target))
            self._status.setText(f"Duplicated {source.name} as {target.name}")
        except Exception as exc:
            QMessageBox.critical(self, "Duplicate Config", str(exc))

    def _rename_config_file(self, path: str) -> None:
        source = Path(path)
        if not source.is_file():
            QMessageBox.warning(self, "Rename Config", "The selected config no longer exists.")
            self._left.reload_folder()
            return
        target = self._prompt_config_target(source, "Rename Config", source.name)
        if target is None or LeftPanel._same_path(str(source), str(target)):
            return

        active_path = self._active_setup_path(self._left.config_dir)
        was_active = bool(active_path) and LeftPanel._same_path(str(source), active_path)
        was_current = bool(self._path) and LeftPanel._same_path(str(source), self._path)
        was_compare = bool(self._compare_path) and LeftPanel._same_path(
            str(source), self._compare_path
        )
        old_resolved = self._resolved_path(source)
        try:
            source.rename(target)
            if was_active:
                try:
                    self._write_active_setup_name(target.name)
                except Exception:
                    target.rename(source)
                    raise
            if was_current:
                self._path = str(target)
            if was_compare:
                self._compare_path = str(target)
            if old_resolved in self._saved_files:
                self._saved_files.discard(old_resolved)
                self._saved_files.add(self._resolved_path(target))
            self._left.set_folder(str(target.parent))
            self._left.select_config_path(str(target))
            if self._compare_mode:
                self._refresh_compare_choices(auto_load=False)
                if was_compare:
                    self._compare_pane.set_selected_path(str(target))
            self._update_active_banner()
            self._status.setText(f"Renamed {source.name} to {target.name}")
        except Exception as exc:
            QMessageBox.critical(self, "Rename Config", str(exc))
            self._update_active_banner()

    def _delete_config_file(self, path: str) -> None:
        source = Path(path)
        if not source.is_file():
            self._left.reload_folder()
            return

        active_path = self._active_setup_path(self._left.config_dir)
        if active_path and LeftPanel._same_path(str(source), active_path):
            QMessageBox.information(
                self,
                "Delete Config",
                "The active config cannot be deleted. Set another config as active first.",
            )
            return

        is_current = bool(self._path) and LeftPanel._same_path(str(source), self._path)
        is_compare = bool(self._compare_path) and LeftPanel._same_path(
            str(source), self._compare_path
        )
        if is_compare and self._compare_modified:
            if not self._confirm_compare_changes("deleting this config"):
                return
        extra = "\n\nUnsaved changes to this config will also be discarded." if (is_current and self._modified) else ""
        response = QMessageBox.question(
            self,
            "Delete Config",
            f"Delete '{source.name}'?{extra}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if response != QMessageBox.Yes:
            return

        try:
            source.unlink()
            self._saved_files.discard(self._resolved_path(source))
            if is_compare:
                self._compare_path = ""
                self._compare_data = {}
                self._compare_modified = False
                self._compare_canvas.load({})
            if is_current:
                self._path = ""
                self._set_modified(False)
                active_after = self._active_setup_path(str(source.parent))
                if active_after:
                    self._load_file(active_after)
                else:
                    self._new_file()
            else:
                self._left.set_folder(str(source.parent))
                if self._compare_mode:
                    self._refresh_compare_choices(auto_load=True)
                self._update_active_banner()
            self._status.setText(f"Deleted: {source.name}")
        except Exception as exc:
            QMessageBox.critical(self, "Delete Config", str(exc))

    def _save_active_pane(self):
        if self._compare_mode and self._active_pane == "compare":
            self._save_compare_file()
        else:
            self._save_file()

    def _save_file(self):
        if not self._path:
            self._save_as()
            return
        self._write_file(self._path)

    def _save_as(self):
        start_path = self._path or str(_default_setup_dir() / "new_setup.json")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Config As", start_path, "JSON files (*.json)"
        )
        if path:
            self._write_file(path)

    def _write_file(self, path: str):
        # Keep this save funnel usable by lightweight non-UI sessions/tests that
        # deliberately do not construct the full QMainWindow. Missing Qt-backed
        # attributes can raise before getattr()/hasattr() gets a chance to fall
        # back, so optional compare state is read directly from __dict__.
        state = self.__dict__
        compare_mode = bool(state.get("_compare_mode", False))
        compare_path = state.get("_compare_path", "")
        compare_conflict = False
        if compare_mode and compare_path:
            compare_conflict = LeftPanel._same_path(path, compare_path)
            try:
                compare_conflict = compare_conflict or (
                    Path(path).parent.resolve() != Path(compare_path).parent.resolve()
                )
            except Exception:
                pass
        if (
            compare_conflict and state.get("_compare_modified", False)
            and not self._confirm_compare_changes("saving the main config here")
        ):
            return
        try:
            # Delegate transform to shared helper when available
            if _io_module is not None:
                data_to_write = _io_module.prepare_for_save(self._data)
            else:
                data_to_write = copy.deepcopy(self._data)
                # Strip empty "others" dict to keep saved JSON clean
                if "others" in data_to_write and not data_to_write["others"]:
                    del data_to_write["others"]
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data_to_write, fh, indent=2, ensure_ascii=False)
            self._path = path
            # Save As may move the editor to a different setup folder. Refresh
            # that list so the newly written file and its EDITING marker appear
            # immediately.  Keep the bookkeeping method usable by lightweight
            # non-UI tests/embedders that intentionally do not construct LeftPanel.
            left = state.get("_left")
            if left is not None:
                left.set_folder(str(Path(path).parent))
            self._set_modified(False)
            if left is not None:
                left.select_config_path(path)
            self._note_saved(path)
            if compare_mode:
                self._refresh_compare_choices(auto_load=True)
            self._status.setText(f"Saved: {path}")
        except Exception as e:
            QMessageBox.critical(self, "Save Error", str(e))

    def _confirm_discard(self) -> bool:
        if not self._modified:
            return True
        r = QMessageBox.question(
            self, "Unsaved Changes",
            "Discard unsaved changes?",
            QMessageBox.Yes | QMessageBox.No,
        )
        return r == QMessageBox.Yes

    # ── Canvas ↔ data ─────────────────────────────────────────────────────
    def _refresh_canvas(self):
        self._canvas.load(self._data)
        if hasattr(self, "_primary_pane"):
            self._primary_pane.set_identity(self._path, modified=self._modified)

    def _run_validation(self):
        self._val_panel.validate(self._data)

    def _show_config_settings(self):
        pane = self._active_pane if self._compare_mode else "primary"
        self._pane_canvas(pane).deselect_all()
        self._activate_pane(pane)
        self._set_editor_source(pane)
        self._editor.load_config_extras(self._pane_data(pane))
        self._status.setText(f"Showing config settings: {self._pane_name(pane)}")

    def _on_category_settings(self, cat: str, pane: str = "primary"):
        data = self._pane_data(pane)
        schema = CATEGORY_SCHEMAS.get(cat)
        if not schema:
            return
        settings_root = data.get("deviceCategorySettings")
        if not isinstance(settings_root, dict):
            settings_root = {}
            data["deviceCategorySettings"] = settings_root
        current = settings_root.get(cat)
        if not isinstance(current, dict):
            current = {}
        dlg = SectionEditorDialog(
            cat, schema, current, data, self,
            title_prefix="Edit category settings",
            allow_custom_fields=True,
        )
        if dlg.exec_() != QDialog.Accepted:
            return
        settings_root[cat] = dlg.result_data
        self._mark_pane_modified(pane)
        self._refresh_pane(pane)
        self._activate_pane(pane)
        self._set_editor_source(pane)
        self._editor.load_config_extras(data)
        self._validate_pane(pane)
        self._status.setText(
            f"Updated {CAT_LABEL.get(cat, cat)} settings in {self._pane_name(pane)}"
        )

    def _on_device_selected(self, cat: str, name: str, pane: str = "primary"):
        data = self._pane_data(pane)
        device, enabled = _locate_device(data, cat, name)
        if self._compare_mode and self._compare_path:
            other_pane = "compare" if pane == "primary" else "primary"
            self._pane_canvas(other_pane).select_device(cat, name)
        if device is not None:
            self._activate_pane(pane)
            self._set_editor_source(pane)
            self._editor.load_device(
                cat, name, device, setup=data,
                enabled=True if enabled is None else enabled,
            )
        elif self._compare_mode:
            self._activate_pane(pane)
            self._set_editor_source(pane)
            self._editor.load_config_extras(data)
            self._status.setText(
                f"{name} is missing from {self._pane_name(pane)}"
            )

    def _on_editor_apply(self, cat: str, name: str, new_device: dict):
        pane = self._editor_pane if self._compare_mode else "primary"
        data = self._pane_data(pane)
        _, enabled = _locate_device(data, cat, name)
        if enabled is None:
            enabled = True
        section = _device_section(data, cat, enabled=enabled, create=True)
        section[name] = new_device
        self._mark_pane_modified(pane)
        self._refresh_pane(pane)
        self._validate_pane(pane)
        self._status.setText(f"Updated {name} in {self._pane_name(pane)}")
        self._pane_canvas(pane)._on_card_clicked(cat, name)

    def _on_device_rename(
        self, cat: str, old_name: str, new_name: str, pane: str | None = None
    ):
        pane = pane or (self._editor_pane if self._compare_mode else "primary")
        data = self._pane_data(pane)
        _, enabled = _locate_device(data, cat, old_name)
        if enabled is None:
            return
        active = _device_section(data, cat, enabled=True)
        disabled = _device_section(data, cat, enabled=False)
        if new_name != old_name and (new_name in active or new_name in disabled):
            QMessageBox.warning(self, "Rename", f"Name '{new_name}' already exists.")
            return
        section = _device_section(data, cat, enabled=enabled)
        section[new_name] = section.pop(old_name)
        self._mark_pane_modified(pane)
        self._refresh_pane(pane)
        self._pane_canvas(pane)._on_card_clicked(cat, new_name)

    def _on_device_rename_from_canvas(
        self, cat: str, name: str, pane: str = "primary"
    ):
        new_name, ok = QInputDialog.getText(
            self, "Rename Device", "New name:", text=name
        )
        if ok and new_name.strip():
            self._on_device_rename(cat, name, new_name.strip(), pane)

    def _on_device_enabled_changed(
        self, cat: str, name: str, enabled: bool, pane: str | None = None
    ):
        pane = pane or (self._editor_pane if self._compare_mode else "primary")
        data = self._pane_data(pane)
        canvas = self._pane_canvas(pane)
        device, current_enabled = _locate_device(data, cat, name)
        if device is None or current_enabled is None:
            return
        if current_enabled == enabled:
            if self._editor_pane == pane:
                self._editor.set_device_enabled(current_enabled)
            return

        if not enabled:
            refs = _known_device_references(data, cat, name)
            if refs:
                details = "\n".join(f"  • {path}" for path in refs[:8])
                if len(refs) > 8:
                    details += f"\n  • … and {len(refs) - 8} more"
                response = QMessageBox.question(
                    self,
                    "Disable Referenced Device",
                    f"'{name}' is referenced elsewhere in this setup:\n\n"
                    f"{details}\n\n"
                    "Those features may be unavailable until the device is enabled again.\n\n"
                    "Disable it anyway?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if response != QMessageBox.Yes:
                    if self._editor_pane == pane:
                        self._editor.set_device_enabled(current_enabled)
                    return

        selected_before = canvas._selected
        try:
            changed = _set_device_enabled(data, cat, name, enabled)
        except ValueError as exc:
            QMessageBox.warning(self, "Device State", str(exc))
            if self._editor_pane == pane:
                self._editor.set_device_enabled(current_enabled)
            return
        if not changed:
            if self._editor_pane == pane:
                self._editor.set_device_enabled(current_enabled)
            return

        self._mark_pane_modified(pane)
        self._refresh_pane(pane)
        canvas = self._pane_canvas(pane)
        if selected_before:
            card = canvas._cards.get(selected_before)
            if card is not None:
                canvas._selected = selected_before
                card.set_selected(True)
        if selected_before == (cat, name) and self._editor_pane == pane:
            self._editor.set_device_enabled(enabled)
        self._validate_pane(pane)
        state = "Enabled" if enabled else "Disabled"
        self._status.setText(f"{state} {name} in {self._pane_name(pane)}")

    def _on_device_deleted(self, cat: str, name: str, pane: str = "primary"):
        if cat == "__ADD__":
            return
        data = self._pane_data(pane)
        _, enabled = _locate_device(data, cat, name)
        if enabled is None:
            return
        state = "disabled " if enabled is False else ""
        r = QMessageBox.question(
            self, "Delete Device",
            f"Delete {state}device '{name}' from {CAT_LABEL.get(cat, cat)} "
            f"in {self._pane_name(pane)}?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if r == QMessageBox.Yes:
            _remove_device(data, cat, name)
            self._mark_pane_modified(pane)
            self._refresh_pane(pane)
            self._validate_pane(pane)
            if self._editor_pane == pane:
                self._editor.load_config_extras(data)

    def _on_device_duped_or_add(
        self, cat_signal: str, name_signal: str, pane: str = "primary"
    ):
        data = self._pane_data(pane)
        if cat_signal == "__ADD__":
            parts = name_signal.split("|", 2)
            if len(parts) == 3:
                cat, dev_name, mgr = parts
                if mgr == "__custom__":
                    device = {"managerName": "", "managerProperties": {}}
                else:
                    device = _build_default_device(mgr)
                self._add_device(cat, dev_name, device, pane)
            return

        original, enabled = _locate_device(data, cat_signal, name_signal)
        if original is None:
            return
        if enabled is None:
            enabled = True
        section = _device_section(data, cat_signal, enabled=enabled, create=True)
        active = _device_section(data, cat_signal, enabled=True)
        disabled = _device_section(data, cat_signal, enabled=False)
        base = name_signal + "_copy"
        i = 1
        while base in active or base in disabled:
            base = f"{name_signal}_copy{i}"
            i += 1
        section[base] = copy.deepcopy(original)
        self._mark_pane_modified(pane)
        self._refresh_pane(pane)
        self._pane_canvas(pane)._on_card_clicked(cat_signal, base)

    def _copy_device(self, cat: str, name: str, pane: str = "primary"):
        device, enabled = _locate_device(self._pane_data(pane), cat, name)
        if device is None:
            return
        self._copied_device = (
            cat, name, copy.deepcopy(device),
            True if enabled is None else enabled,
        )
        self._activate_pane(pane)
        self._status.setText(
            f"Copied device: {name} from {self._pane_name(pane)}"
        )

    def _copy_device_to_other(self, cat: str, name: str, pane: str) -> None:
        """Fast-path the normal copy/paste operation to the opposite pane."""
        if not self._compare_mode or not self._compare_path:
            return
        target = "compare" if pane == "primary" else "primary"
        device, _enabled = _locate_device(self._pane_data(pane), cat, name)
        if device is None:
            return
        self._copy_device(cat, name, pane)
        self._paste_device(target)

    def _paste_device(self, pane: str | None = None):
        pane = pane or (self._active_pane if self._compare_mode else "primary")
        if pane == "compare" and not self._compare_path:
            QMessageBox.information(
                self, "Paste Device", "Select a comparison config first."
            )
            return
        if not self._copied_device:
            QMessageBox.information(self, "Paste Device", "Copy a device first.")
            return
        data = self._pane_data(pane)
        cat, name, device, enabled = self._copied_device
        existing, _ = _locate_device(data, cat, name)
        if existing is not None:
            response = QMessageBox.question(
                self,
                "Replace Device?",
                f"A device named '{name}' already exists in {CAT_LABEL.get(cat, cat)} "
                f"of {self._pane_name(pane)}.\n\nReplace it with the copied device?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if response != QMessageBox.Yes:
                return
            _remove_device(data, cat, name)
        section = _device_section(data, cat, enabled=enabled, create=True)
        section[name] = copy.deepcopy(device)
        self._mark_pane_modified(pane)
        self._refresh_pane(pane)
        self._validate_pane(pane)
        self._pane_canvas(pane)._on_card_clicked(cat, name)
        self._status.setText(
            f"Pasted device: {name} into {self._pane_name(pane)}"
        )

    def _add_from_template(self, cat: str, device_or_mgr):
        """Instantiate a device template in the currently active pane."""
        if isinstance(device_or_mgr, str):
            tdata = _build_default_device(device_or_mgr)
        else:
            tdata = copy.deepcopy(device_or_mgr)

        name, ok = QInputDialog.getText(
            self, "New Device Name",
            f"Name for new {CAT_LABEL.get(cat, cat)} device:"
        )
        if not ok or not name.strip():
            return
        pane = self._active_pane if self._compare_mode else "primary"
        self._add_device(cat, name.strip(), tdata, pane)

    def _add_device(
        self, cat: str, name: str, device: dict, pane: str = "primary"
    ):
        data = self._pane_data(pane)
        if cat not in data or not isinstance(data.get(cat), dict):
            data[cat] = {}
        active = _device_section(data, cat, enabled=True, create=True)
        disabled = _device_section(data, cat, enabled=False)
        if name in active or name in disabled:
            base = name
            i = 1
            while f"{base}_{i}" in active or f"{base}_{i}" in disabled:
                i += 1
            name = f"{base}_{i}"
        active[name] = device
        self._mark_pane_modified(pane)
        self._refresh_pane(pane)
        self._pane_canvas(pane)._on_card_clicked(cat, name)

    def _on_save_template(
        self, cat: str, name: str, pane: str = "primary"
    ):
        device, _ = _locate_device(self._pane_data(pane), cat, name)
        if device is None:
            return
        tname, ok = QInputDialog.getText(
            self, "Save Template", "Template name:", text=name
        )
        if not ok or not tname.strip():
            return
        tname = tname.strip()

        existing = self._left._store.categories()
        default_cat = SCHEMAS.get(device.get("managerName", ""), {}).get("category", cat)
        choices = existing + ["── New Category… ──"]
        default_idx = choices.index(default_cat) if default_cat in choices else 0
        dest_cat, ok2 = QInputDialog.getItem(
            self, "Template Category",
            "Save to category:", choices, default_idx, False
        )
        if not ok2:
            return
        if dest_cat == "── New Category… ──":
            dest_cat, ok3 = QInputDialog.getText(
                self, "New Category", "Category name:"
            )
            if not ok3 or not dest_cat.strip():
                return
            dest_cat = dest_cat.strip()

        self._left.add_user_template(dest_cat, tname, device)
        self._status.setText(f"Template '{tname}' saved to '{dest_cat}'")

    def _on_extras_modified(self):
        pane = self._editor_pane if self._compare_mode else "primary"
        self._mark_pane_modified(pane)
        # An edit made in the System card or from a validation link can
        # change the ImProcess block; its card on the canvas follows.
        self._pane_canvas(pane).refresh_improcess()
        self._validate_pane(pane)

    def _on_improcess_edit(self):
        """Configure / Edit on the canvas's ImProcess card."""
        if _edit_improcess_section(self._data, self):
            self._after_improcess_change()

    def _after_improcess_change(self):
        # The canvas's card belongs to the primary pane, whichever pane the
        # inspector follows.
        self._editor._refresh_sections()
        self._mark_pane_modified("primary")
        self._canvas.refresh_improcess()
        self._validate_pane("primary")

    def _on_improcess_remove(self):
        """Remove on the canvas's ImProcess card, after confirmation."""
        r = QMessageBox.question(
            self, "Remove section",
            f"Remove the '{IMPROCESS_SECTION_KEY}' section from this config?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if r != QMessageBox.Yes:
            return
        self._data.pop(IMPROCESS_SECTION_KEY, None)
        self._after_improcess_change()

    def _on_fix_section_from_validation(self, section_key: str):
        """Handle 'Configure…' link clicks from validation warnings."""
        schema = SECTION_SCHEMAS.get(section_key)
        if not schema:
            return
        if section_key == IMPROCESS_SECTION_KEY:
            if _edit_improcess_section(self._data, self):
                self._editor._refresh_sections()
                self._on_extras_modified()
            return
        current = self._data.get(section_key)
        if current in (None, {}, []):
            current = _new_section_seed(schema)
        dlg = SectionEditorDialog(section_key, schema, current, self._data, self)
        if dlg.exec_() == QDialog.Accepted:
            self._data[section_key] = dlg.result_data
            self._editor._refresh_sections()
            self._on_extras_modified()

    # ── Active config / options file ──────────────────────────────────────
    def _detect_options_file(self):
        """Search well-known locations for imcontrol_options.json."""
        self._options_path = ""
        candidates = list(self._OPTIONS_SEARCH_ROOTS)
        # Also look next to an already-open config file
        if self._path:
            candidates.insert(0, Path(self._path).parent.parent / "config")
        for root in candidates:
            p = Path(root) / "imcontrol_options.json"
            if p.exists():
                self._options_path = str(p)
                break
        self._update_active_banner()

    def _active_setup_name(self) -> str:
        """The setup file name ``imcontrol_options.json`` marks as active."""
        if not self._options_path:
            return ""
        try:
            with open(self._options_path, encoding="utf-8") as fh:
                return str(json.load(fh).get("setupFileName") or "")
        except Exception:
            return ""

    def _active_setup_path(self, start_folder: str = "") -> str:
        """Locate the active setup file on disk, or "" if it cannot be found.

        The browsed folder is searched first so that a folder passed on the
        command line wins over the default location; a stale or deleted
        ``setupFileName`` simply resolves to nothing.
        """
        name = self._active_setup_name()
        if not name:
            return ""
        roots = []
        if start_folder:
            roots.append(Path(start_folder))
        roots.append(_default_setup_dir())
        for root in roots:
            try:
                candidate = Path(root) / name
                if candidate.is_file():
                    return str(candidate)
            except Exception:
                continue
        return ""

    def _open_active_config(self, start_folder: str = "") -> None:
        """Open the config ImSwitch is actually set up to use, at startup.

        The editor already knows which one that is -- it shows it in the
        banner -- so opening to an empty pane made everyone's first action the
        same one. Silent when there is no options file, no active name, or the
        named file is missing: an editor that opens empty is a far better
        outcome than one that refuses to start.
        """
        path = self._active_setup_path(start_folder)
        if not path:
            return
        try:
            self._load_file(path)
        except Exception as exc:  # noqa: BLE001 - never block startup
            self._status.setText(f"Could not open the active config: {exc}")

    def _update_active_banner(self):
        """Refresh the Editing / Active identity bar and left-list markers."""
        editing_name = Path(self._path).name if self._path else "[new config]"
        modified = " <span style='color:#D98C2B;'><b>MODIFIED</b></span>" if self._modified else ""
        self._editing_lbl.setText(
            f"<b>Editing:</b>&nbsp;&nbsp;<b>{editing_name}</b>{modified}"
        )
        self._editing_lbl.setToolTip(self._path or "New unsaved configuration")

        active = ""
        active_path = ""
        active_error = False
        if self._options_path:
            try:
                with open(self._options_path, encoding="utf-8") as fh:
                    opts = json.load(fh)
                active = str(opts.get("setupFileName") or "<not set>")
            except Exception:
                active = "<error reading options>"
                active_error = True
            active_path = self._active_setup_path(self._left.config_dir)

        if not self._options_path:
            self._active_lbl.setText("<span style='color:#888;'><b>Active:</b>&nbsp;&nbsp;unavailable</span>")
            self._active_lbl.setToolTip("No imcontrol_options.json was found")
        else:
            same = bool(self._path) and bool(active_path) and LeftPanel._same_path(self._path, active_path)
            if not active_path and not active_error and active not in ("", "<not set>"):
                # Keep the configured name visible even if the referenced file
                # has since moved or been deleted.
                state = " <span style='color:#B36B00;'>(file not found)</span>"
            else:
                state = ""
            if same:
                state += " <span style='color:#4B9B67;'>(same as editing)</span>"
            self._active_lbl.setText(f"<b>Active:</b>&nbsp;&nbsp;{active}{state}")
            self._active_lbl.setToolTip(str(self._options_path))

        is_current = bool(self._path) and bool(active_path) and LeftPanel._same_path(self._path, active_path)
        can_set_active = bool(self._path and self._options_path and not is_current)
        self._set_active_btn.setEnabled(can_set_active)
        self._set_active_btn.setToolTip(
            "This file is already the active config" if is_current
            else "No imcontrol_options.json was found" if not self._options_path
            else "Open or save a config file first" if not self._path
            else f"Set {Path(self._path).name} as the active ImSwitch config"
        )

        # Reset is useful only while the primary editing config has unsaved
        # changes. Keep the normal identity bar uncluttered otherwise.
        self._reset_editing_btn.setVisible(self._modified)
        self._reset_editing_btn.setEnabled(self._modified)

        # When Active and Editing already point to the same file, the green
        # '(same as editing)' label above is the affordance. Otherwise Select
        # provides a direct way back to the active setup.
        can_select_active = bool(active_path and not is_current)
        self._select_active_btn.setVisible(can_select_active)
        self._select_active_btn.setEnabled(can_select_active)
        self._select_active_btn.setToolTip(
            f"Open {Path(active_path).name} in the editor"
            if can_select_active else "The active config is already being edited"
        )

        self._left.set_config_identity(
            current_path=self._path,
            active_path=active_path,
            modified=self._modified,
        )
        if hasattr(self, "_primary_pane"):
            self._primary_pane.set_identity(self._path, modified=self._modified)
        if getattr(self, "_editor_pane", "primary") == "primary":
            self._set_editor_source("primary")
        title_name = Path(self._path).name if self._path else "[new]"
        star = " *" if self._modified else ""
        self.setWindowTitle(f"ImSwitch Config Studio — {title_name}{star}")

    def _select_active_config(self) -> None:
        """Open the active ImSwitch setup as the primary editing config."""
        active_path = self._active_setup_path(self._left.config_dir)
        if not active_path:
            return
        self._open_file_from_list(active_path)

    def _reset_editing_config(self) -> None:
        """Discard primary unsaved edits and restore the last saved state."""
        if not self._modified:
            return
        answer = QMessageBox.question(
            self,
            "Discard Unsaved Changes",
            "Discard unsaved changes?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        if self._path:
            self._load_file(self._path)
            return

        # An unsaved new config has no disk state to restore; reset it to the
        # same empty structure created by New without prompting a second time.
        self._data = {cat: {} for cat in DEVICE_CATS}
        self._data["availableWidgets"] = []
        self._set_modified(False)
        self._refresh_canvas()
        self._activate_pane("primary")
        self._set_editor_source("primary")
        self._editor.load_config_extras(self._data)
        self._status.setText("Unsaved changes discarded")

    def _set_modified(self, modified: bool) -> None:
        self._modified = bool(modified)
        state = self.__dict__
        if "_editing_lbl" in state and "_left" in state:
            self._update_active_banner()

    def _mark_modified(self) -> None:
        self._set_modified(True)

    def _write_active_setup_name(self, name: str) -> None:
        """Persist the active setup name without exposing a half-written options file."""
        if not self._options_path:
            raise RuntimeError("No imcontrol_options.json was found")
        options_path = Path(self._options_path)
        with open(options_path, encoding="utf-8") as fh:
            opts = json.load(fh)
        opts["setupFileName"] = name
        temp_path = options_path.with_name(options_path.name + ".tmp")
        try:
            with open(temp_path, "w", encoding="utf-8") as fh:
                json.dump(opts, fh, indent=4, ensure_ascii=False)
            os.replace(temp_path, options_path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        self._active_config_changed = True

    def _set_path_as_active_config(self, path: str) -> None:
        if not path or not self._options_path:
            return
        if self._path and LeftPanel._same_path(path, self._path):
            self._set_as_active_config()
            return
        try:
            self._write_active_setup_name(Path(path).name)
            self._update_active_banner()
            self._status.setText(f"Active config set to: {Path(path).name}")
        except Exception as exc:
            QMessageBox.critical(
                self, "Error", f"Could not update options file:\n{exc}"
            )

    def _set_as_active_config(self):
        if not self._path or not self._options_path:
            return
        # Optionally save unsaved changes first
        if self._modified:
            r = QMessageBox.question(
                self, "Unsaved Changes",
                "Save changes before setting as active config?",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            )
            if r == QMessageBox.Cancel:
                return
            if r == QMessageBox.Yes:
                self._save_file()
                if self._modified:
                    return

        try:
            self._write_active_setup_name(Path(self._path).name)
            self._update_active_banner()
            self._status.setText(
                f"Active config set to: {Path(self._path).name}"
            )
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not update options file:\n{e}")

    # ── What this session changed ─────────────────────────────────────────
    def _note_saved(self, path: str) -> None:
        """Remember a file this session wrote, by resolved path.

        Resolved so that the same file reached through a symlink, a relative
        path or a different spelling still compares equal to what an embedder
        knows as its active setup.
        """
        try:
            self._saved_files.add(str(Path(path).resolve()))
        except Exception:  # noqa: BLE001 - a path we cannot resolve is not worth a crash
            self._saved_files.add(str(path))

    def saved_files(self) -> frozenset:
        """Resolved paths of every config file written during this session."""
        return frozenset(self._saved_files)

    @property
    def active_config_changed(self) -> bool:
        """Whether this session pointed ImSwitch at a different setup file."""
        return self._active_config_changed

    def closeEvent(self, event):
        if self._compare_modified and not self._confirm_compare_changes("closing the editor"):
            event.ignore()
            return
        if self._modified and not self._confirm_discard():
            event.ignore()
            return
        event.accept()
        self.sig_closed.emit()

def _dark_palette(palette):
    """Return ``palette`` with the editor's dark colours set."""
    # Set dark palette colors
    palette.setColor(QPalette.Window, QColor("#2B2B2B"))
    palette.setColor(QPalette.Base, QColor("#1E1E1E"))
    palette.setColor(QPalette.AlternateBase, QColor("#252525"))
    palette.setColor(QPalette.Text, QColor("#E0E0E0"))
    palette.setColor(QPalette.WindowText, QColor("#E0E0E0"))
    palette.setColor(QPalette.ButtonText, QColor("#E0E0E0"))
    palette.setColor(QPalette.Button, QColor("#3C3F41"))
    palette.setColor(QPalette.Highlight, QColor("#2979C0"))
    palette.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
    palette.setColor(QPalette.ToolTipBase, QColor("#2B2B2B"))
    palette.setColor(QPalette.ToolTipText, QColor("#E0E0E0"))
    palette.setColor(QPalette.Link, QColor("#2979C0"))
    palette.setColor(QPalette.BrightText, QColor("#FF5555"))
    # Disabled text
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor("#707070"))
    palette.setColor(QPalette.Disabled, QPalette.WindowText, QColor("#707070"))
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#707070"))
    return palette


# Comprehensive QSS stylesheet
_DARK_STYLESHEET = """
        QFrame {
            background-color: #2B2B2B;
            color: #E0E0E0;
        }
        /* HLine / VLine separators — just a coloured line, no box */
        QFrame[frameShape="4"], QFrame[frameShape="5"] {
            background-color: #3C3F41;
            border: none;
            max-width: 1px;
            max-height: 1px;
        }

        QGroupBox {
            background-color: #2B2B2B;
            color: #E0E0E0;
            border: 1px solid #3C3F41;
            border-radius: 4px;
            margin-top: 8px;
            padding-top: 8px;
        }
        QGroupBox::title {
            color: #E0E0E0;
            subcontrol-origin: margin;
            subcontrol-position: top left;
            padding: 2px 5px;
        }

        QLineEdit, QTextEdit {
            background-color: #1E1E1E;
            color: #E0E0E0;
            border: 1px solid #3C3F41;
            border-radius: 3px;
            padding: 3px;
            selection-background-color: #2979C0;
            selection-color: #FFFFFF;
        }
        QLineEdit:focus, QTextEdit:focus {
            border: 1px solid #2979C0;
        }
        QLineEdit:disabled, QTextEdit:disabled {
            color: #707070;
            background-color: #252525;
        }

        QComboBox {
            background-color: #3C3F41;
            color: #E0E0E0;
            border: 1px solid #3C3F41;
            border-radius: 3px;
            padding: 3px 5px;
        }
        QComboBox:hover {
            border: 1px solid #2979C0;
        }
        QComboBox:disabled {
            color: #707070;
            background-color: #252525;
        }
        QComboBox::drop-down {
            border: none;
            width: 20px;
        }
        QComboBox::down-arrow {
            image: none;
            border-left: 4px solid transparent;
            border-right: 4px solid transparent;
            border-top: 5px solid #E0E0E0;
            margin-right: 5px;
        }
        QComboBox QAbstractItemView {
            background-color: #3C3F41;
            color: #E0E0E0;
            selection-background-color: #2979C0;
            selection-color: #FFFFFF;
            border: 1px solid #2979C0;
        }

        QSpinBox, QDoubleSpinBox {
            background-color: #1E1E1E;
            color: #E0E0E0;
            border: 1px solid #3C3F41;
            border-radius: 3px;
            padding: 3px;
        }
        QSpinBox:focus, QDoubleSpinBox:focus {
            border: 1px solid #2979C0;
        }
        QSpinBox:disabled, QDoubleSpinBox:disabled {
            color: #707070;
            background-color: #252525;
        }
        QSpinBox::up-button, QDoubleSpinBox::up-button,
        QSpinBox::down-button, QDoubleSpinBox::down-button {
            background-color: #3C3F41;
            border: none;
        }
        QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
        QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {
            background-color: #2979C0;
        }

        QScrollArea {
            background-color: #2B2B2B;
            border: none;
        }
        QScrollBar:vertical {
            background-color: #2B2B2B;
            width: 12px;
            border: none;
        }
        QScrollBar::handle:vertical {
            background-color: #3C3F41;
            border-radius: 6px;
            min-height: 20px;
        }
        QScrollBar::handle:vertical:hover {
            background-color: #2979C0;
        }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
            height: 0px;
        }
        QScrollBar:horizontal {
            background-color: #2B2B2B;
            height: 12px;
            border: none;
        }
        QScrollBar::handle:horizontal {
            background-color: #3C3F41;
            border-radius: 6px;
            min-width: 20px;
        }
        QScrollBar::handle:horizontal:hover {
            background-color: #2979C0;
        }
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
            width: 0px;
        }

        QTreeWidget {
            background-color: #1E1E1E;
            color: #E0E0E0;
            border: 1px solid #3C3F41;
            alternate-background-color: #252525;
        }
        QTreeWidget::item:selected {
            background-color: #2979C0;
            color: #FFFFFF;
        }
        QTreeWidget::item:hover {
            background-color: #3C3F41;
        }
        QTreeWidget::branch {
            background-color: #1E1E1E;
        }

        QTabWidget::pane {
            background-color: #2B2B2B;
            border: 1px solid #3C3F41;
            border-radius: 3px;
        }
        QTabBar::tab {
            background-color: #3C3F41;
            color: #E0E0E0;
            border: 1px solid #3C3F41;
            border-bottom: none;
            border-top-left-radius: 3px;
            border-top-right-radius: 3px;
            padding: 5px 10px;
            margin-right: 2px;
        }
        QTabBar::tab:selected {
            background-color: #2979C0;
            color: #FFFFFF;
        }
        QTabBar::tab:hover:!selected {
            background-color: #4C5052;
        }

        QPushButton {
            background-color: #3C3F41;
            color: #E0E0E0;
            border: 1px solid #3C3F41;
            border-radius: 3px;
            padding: 5px 15px;
        }
        QPushButton:hover {
            background-color: #2979C0;
            border: 1px solid #2979C0;
        }
        QPushButton:pressed {
            background-color: #1E5A8E;
        }
        QPushButton:disabled {
            color: #707070;
            background-color: #252525;
            border: 1px solid #252525;
        }

        QCheckBox {
            color: #E0E0E0;
            spacing: 5px;
        }
        QCheckBox::indicator {
            width: 16px;
            height: 16px;
            border: 1px solid #3C3F41;
            border-radius: 3px;
            background-color: #1E1E1E;
        }
        QCheckBox::indicator:checked {
            background-color: #2979C0;
            border: 1px solid #2979C0;
        }
        QCheckBox::indicator:hover {
            border: 1px solid #2979C0;
        }
        QCheckBox:disabled {
            color: #707070;
        }

        QLabel {
            color: #E0E0E0;
            background-color: transparent;
        }

        QToolBar {
            background-color: #2B2B2B;
            border: 1px solid #3C3F41;
            spacing: 3px;
            padding: 3px;
        }
        QToolBar::separator {
            background-color: #3C3F41;
            width: 1px;
            margin: 3px;
        }

        QStatusBar {
            background-color: #2B2B2B;
            color: #E0E0E0;
            border-top: 1px solid #3C3F41;
        }

        QMenu {
            background-color: #3C3F41;
            color: #E0E0E0;
            border: 1px solid #2979C0;
        }
        QMenu::item:selected {
            background-color: #2979C0;
            color: #FFFFFF;
        }
        QMenu::separator {
            height: 1px;
            background-color: #252525;
            margin: 3px 0;
        }
    """


# Put in front of _DARK_STYLESHEET when the editor themes its own window inside
# ImSwitch. They undo the qdarkstyle rules the editor's sheet does not otherwise
# override: a blue-black background on every plain widget, and boxed tool bar
# buttons. They come first so that the editor's own type rules, of equal
# specificity, still win.
_HOSTED_OVERRIDES = """
        QWidget {
            background-color: #2B2B2B;
            color: #E0E0E0;
        }
        QToolBar QToolButton {
            background-color: transparent;
            border: none;
            padding: 3px 6px;
        }
        QToolBar QToolButton:hover {
            background-color: #3C3F41;
        }
"""


def dark_theme(app, palette):
    """Apply complete dark theme with palette and comprehensive QSS stylesheet."""
    app.setStyleSheet(_DARK_STYLESHEET)
    return _dark_palette(palette)

# =============================================================================
# Entry point
# =============================================================================
def main():
    app = QApplication(sys.argv)
    app.setApplicationName("ImSwitch Config Studio")
    app.setStyle("Fusion")

    # Apply dark theme with comprehensive styling
    palette = app.palette()
    palette = dark_theme(app, palette)  # Comment this line for light theme
    app.setPalette(palette)

    if len(sys.argv) > 1:
        folder = sys.argv[1]
    else:
        _default = _default_setup_dir()
        folder = str(_default) if _default.is_dir() else ""
    win = MainWindow(start_folder=folder)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
