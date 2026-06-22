#!/usr/bin/env python3
"""
ImSwitch Config Studio
Visual editor for ImSwitch JSON configuration files.

Run:      python tools/imswitch_config_editor.py [/path/to/config/dir]
Requires: pip install PyQt5
"""

import ast
import colorsys
import copy
import json
import os
import re
import sys
from pathlib import Path
import glob
from PyQt5.QtCore import Qt, pyqtSignal, QRect, QSize, QPoint
from PyQt5.QtGui import QFont, QPalette, QColor
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QFrame, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton, QScrollArea,
    QSizePolicy, QSpinBox, QSplitter, QStatusBar, QTabWidget, QToolBar,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QAction, QGridLayout,
    QDialog, QTextEdit, QLayout, QLayoutItem,
)

# =============================================================================
# Authoritative category registry
# =============================================================================
# Maps category name → (display label override OR None, manager directory name)
_CATEGORY_REGISTRY = {
    "detectors":     (None, "detectors"),
    "lasers":        (None, "lasers"),
    "positioners":   (None, "positioners"),
    "rotators":      (None, "rotators"),
    "rs232devices":  ("RS232 Devices", "rs232"),
    "slms":          ("SLMs", "slms"),
    "flipMirrors":   (None, "flipMirrors"),
    "pulsegen":      (None, "pulsegen"),
    "stands":        (None, "stands"),
}


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
            if not cat_dir.is_dir() or cat_dir.name == "sections":
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
            if not cat_dir.is_dir() or cat_dir.name == "sections":
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
    """
    discovered: dict = {}
    # Resolve path relative to this script: utility_scripts/ → imswitch/imcontrol/model/managers/
    script_path = Path(__file__).resolve()
    managers_root = script_path.parents[1] / "imswitch" / "imcontrol" / "model" / "managers"

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


def _resolve_section_dynamic_options(schema: dict) -> None:
    """Replace supported option placeholders with runtime-discovered values."""
    fallback = {
        "__improcess_reconstructors__": [
            "monalisa",
            "snouty",
            "snouty-projections",
            "view-only",
            "widefield-starss",
        ],
        "__improcess_processors__": [
            "denoise",
            "colocalization",
            "drift-correct",
            "frc",
            "multicolor-apply",
            "multicolor-registration",
            "projection",
            "psf-resolution",
            "segmentation",
        ],
    }
    resolved = dict(fallback)
    try:
        from imswitch.improcess.reconstructors import available_reconstructor_ids
        from imswitch.improcess.processors import available_processor_ids

        resolved["__improcess_reconstructors__"] = available_reconstructor_ids()
        resolved["__improcess_processors__"] = available_processor_ids()
    except Exception:
        pass

    for field in schema.get("fields", []):
        opts = field.get("opts") or []
        if len(opts) == 1 and opts[0] in resolved:
            field["opts"] = resolved[opts[0]]


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
    if value is None:
        return "null"
    if field_type in ("int", "float"):
        return str(value)
    return str(value)


def _display_to_json(text: str, field_type: str):
    """Convert a display string back to the correct Python type."""
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
    """Find which category a manager belongs to (template or discovered)."""
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
    """Return a UI label for templated and discovered managers."""
    schema = SCHEMAS.get(manager_name)
    if schema:
        return schema.get("display") or manager_name
    return manager_name


def _all_known_managers() -> list[str]:
    """Return all templated and discovered manager names."""
    managers = set(SCHEMAS)
    for mgrs in CAT_MANAGERS.values():
        managers.update(mgrs)
    return sorted(managers)


def _build_default_device(manager_name: str) -> dict:
    """Return a new device dict pre-filled with schema defaults.

    If the manager has a specific template, use it. Otherwise, fall back to
    the category's blank schema. If no category is found, return minimal dict.
    """
    schema = SCHEMAS.get(manager_name)

    # If no specific template, try to find the category and use its blank
    if not schema:
        cat = _get_category_for_manager(manager_name)
        if cat:
            schema = BLANK_SCHEMAS.get(cat, {})

    d: dict = {"managerName": manager_name, "managerProperties": {}}
    for f in schema.get("top", []):
        v = f["default"]
        if f["type"] == "bool" and isinstance(v, bool):
            d[f["key"]] = v
        elif v == "null":
            d[f["key"]] = None
        else:
            d[f["key"]] = _display_to_json(str(v), f["type"]) if v != "" else v
    for f in schema.get("props", []):
        v = f["default"]
        d["managerProperties"][f["key"]] = (
            _display_to_json(str(v), f["type"]) if v != "" else v
        )
    for nest_key, nest_fields in schema.get("nested", {}).items():
        sub = {}
        for f in nest_fields:
            v = f["default"]
            sub[f["key"]] = _display_to_json(str(v), f["type"]) if v != "" else v
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
            else:
                out[f["key"]] = v if isinstance(v, (dict, list)) else {}
        elif v == "null":
            out[f["key"]] = None
        elif v == "" and tp in ("ref", "text", "path", "select"):
            out[f["key"]] = ""
        else:
            out[f["key"]] = _display_to_json(str(v), tp) if not isinstance(v, (int, float, bool)) else v
    return out


def _collect_daq_channels(data: dict) -> dict:
    """Return {channel_string: [device_name, ...]} for all assigned DAQ lines."""
    used: dict = {}
    for cat in DEVICE_CATS:
        for name, dev in (data.get(cat) or {}).items():
            for key in ("analogChannel", "digitalLine"):
                val = dev.get(key)
                if val and val != "null":
                    used.setdefault(val, []).append(name)
    return used


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


def _collect_xref_issues(data: dict) -> list:
    """Return cross-reference issues as a list of ``(severity, message)`` tuples.

    ``severity`` is ``"error"`` (red — real config problem), ``"warning"``
    (orange — likely problem), or ``"note"`` (grey — legacy / dormant).
    The checks here cover what falls silently between device flags and
    system sections, e.g. a detector flagged ``forFocusLock: true`` while
    the ``focusLock`` section is missing.
    """
    out: list = []

    detectors = data.get("detectors") or {}
    positioners = data.get("positioners") or {}
    rs232s = data.get("rs232devices") or {}
    widgets = data.get("availableWidgets") or []

    # ── focusLock ⇄ detector forFocusLock ────────────────────────────────
    focus_dets = [n for n, d in detectors.items() if d.get("forFocusLock")]
    fl = data.get("focusLock")
    if focus_dets and not fl:
        out.append(("error",
            f"Detector(s) flagged forFocusLock={focus_dets} but no "
            f"<b>focusLock</b> section — flag has no effect."))
    if fl:
        if not focus_dets:
            out.append(("error",
                "<b>focusLock</b> section present but no detector has "
                "forFocusLock=true."))
        cam = fl.get("camera")
        pos = fl.get("positioner")
        if cam and cam not in detectors:
            out.append(("error",
                f"<b>focusLock.camera</b>='{cam}' is not a defined detector."))
        elif cam and not detectors.get(cam, {}).get("forFocusLock"):
            out.append(("warning",
                f"focusLock.camera='{cam}' exists but does not have "
                f"forFocusLock=true."))
        if pos and pos not in positioners:
            out.append(("error",
                f"<b>focusLock.positioner</b>='{pos}' is not a defined "
                f"positioner."))

    # ── autofocus ⇄ devices ──────────────────────────────────────────────
    af = data.get("autofocus")
    if af:
        cam = af.get("camera")
        pos = af.get("positioner")
        if cam and cam not in detectors:
            out.append(("error",
                f"<b>autofocus.camera</b>='{cam}' is not a defined detector."))
        if pos and pos not in positioners:
            out.append(("error",
                f"<b>autofocus.positioner</b>='{pos}' is not a defined "
                f"positioner."))

    # ── tiling ⇄ positioners + optional camera ───────────────────────────
    tl = data.get("tiling")
    if tl:
        xy = tl.get("xyPositioner")
        z = tl.get("zPositioner")
        cam = tl.get("camera")
        if xy and xy not in positioners:
            out.append(("error",
                f"<b>tiling.xyPositioner</b>='{xy}' is not a defined "
                f"positioner."))
        if z and z not in positioners:
            out.append(("error",
                f"<b>tiling.zPositioner</b>='{z}' is not a defined "
                f"positioner."))
        if cam and cam not in detectors:
            out.append(("error",
                f"<b>tiling.camera</b>='{cam}' is not a defined detector."))

    # ── scan ⇄ forScanning positioners ───────────────────────────────────
    if data.get("scan"):
        scanning = [n for n, p in positioners.items() if p.get("forScanning")]
        if not scanning:
            out.append(("warning",
                "<b>scan</b> section present but no positioner has "
                "forScanning=true."))

    # ── etSTED ⇄ scan ────────────────────────────────────────────────────
    if data.get("etSTED") and not data.get("scan"):
        out.append(("warning",
            "<b>etSTED</b> present but no <b>scan</b> section — "
            "coordinate transforms have nothing to drive."))

    # ── ImProcess processing plugin ids ──────────────────────────────────
    processing = data.get("processing")
    if processing:
        known_reconstructors = set(_section_option_values("processing", "reconstructors"))
        known_processors = set(_section_option_values("processing", "processors"))
        reconstructors = processing.get("reconstructors")
        processors = processing.get("processors")
        if reconstructors is not None:
            if not isinstance(reconstructors, list) or not reconstructors:
                out.append(("error",
                    "<b>processing.reconstructors</b> must be a non-empty list "
                    "of ImProcess reconstructor IDs."))
            else:
                unknown = [r for r in reconstructors if r not in known_reconstructors]
                if unknown:
                    out.append(("error",
                        f"<b>processing.reconstructors</b> contains unknown "
                        f"plugin ID(s): {unknown}."))
        if processors is not None:
            if not isinstance(processors, list):
                out.append(("error",
                    "<b>processing.processors</b> must be a list of ImProcess "
                    "processor IDs."))
            else:
                unknown = [p for p in processors if p not in known_processors]
                if unknown:
                    out.append(("error",
                        f"<b>processing.processors</b> contains unknown "
                        f"plugin ID(s): {unknown}."))

    # ── microscopeStand ⇄ rs232devices ───────────────────────────────────
    ms = data.get("microscopeStand")
    if ms:
        port = ms.get("rs232device")
        if port and port not in rs232s:
            out.append(("error",
                f"<b>microscopeStand.rs232device</b>='{port}' is not a "
                f"defined RS232 connection."))

    # ── availableWidgets ↔ matching sections ─────────────────────────────
    for widget, section_key in _WIDGET_REQUIRES_SECTION.items():
        if widget in widgets and not data.get(section_key):
            schema = SECTION_SCHEMAS.get(section_key)
            display = schema.get("display", section_key) if schema else section_key
            out.append(("warning",
                f"Widget <b>{widget}</b> is enabled but no <b>{section_key}"
                f"</b> section is configured — widget will not initialize. "
                f"<a href='fixsection:{section_key}'>Configure {display}…</a>"))

    # ── Legacy / dormant section notes ───────────────────────────────────
    if data.get("pulseStreamer") and (data["pulseStreamer"] or {}).get("ipAddress"):
        out.append(("note",
            "<b>pulseStreamer</b> is configured but MasterController no "
            "longer constructs PulseStreamerManager. Use <b>teensyPulse</b> "
            "instead."))
    if data.get("slm"):
        out.append(("note",
            "<b>slm</b> (singular) is deprecated — prefer <b>slms</b> "
            "(plural) for new setups."))

    return out


def _section_option_values(section_key: str, field_key: str) -> list:
    schema = SECTION_SCHEMAS.get(section_key) or {}
    for field in schema.get("fields", []):
        if field.get("key") == field_key:
            return list(field.get("opts") or [])
    return []


# =============================================================================
# Theme helpers
# =============================================================================
def is_dark_mode() -> bool:
    """Check if the application is in dark mode based on palette lightness."""
    app = QApplication.instance()
    if app is None:
        return False
    return app.palette().window().color().lightness() < 128


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

    def _smart_spacing(self, pm, orientation) -> int:
        parent = self.parent()
        if parent is None:
            return -1
        if parent.isWidgetType():
            return parent.style().pixelMetric(pm, None, parent)
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

    def __init__(self, category: str, name: str, device_data: dict, parent=None):
        super().__init__(parent)
        self.category = category
        self.device_name = name
        self.device_data = device_data
        self._selected = False
        self._init_ui()

    def _init_ui(self):
        self.setFixedWidth(195)
        self.setMinimumHeight(80)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        self.setCursor(Qt.PointingHandCursor)
        self._apply_style(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 7, 7, 7)
        layout.setSpacing(2)

        self._name_lbl = QLabel(self.device_name)
        bold = QFont()
        bold.setBold(True)
        bold.setPointSize(9)
        self._name_lbl.setFont(bold)
        self._name_lbl.setWordWrap(True)
        layout.addWidget(self._name_lbl)

        mgr = self.device_data.get("managerName", "")
        self._mgr_lbl = QLabel(_manager_display_name(mgr))
        self._mgr_lbl.setStyleSheet("color:#666;font-size:8pt;")
        layout.addWidget(self._mgr_lbl)

        info = self._summary()
        if info:
            lbl = QLabel(info)
            lbl.setStyleSheet("color:#888;font-size:8pt;")
            lbl.setWordWrap(True)
            layout.addWidget(lbl)

        layout.addStretch()

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
        bg = colors['card_bg_selected'] if selected else colors['card_bg']
        self.setStyleSheet(f"""
            DeviceCard {{
                background: {bg};
                border: 1px solid {colors['card_border']};
                border-left: {bw} solid {color};
                border-radius: 4px;
            }}
            DeviceCard:hover {{
                background: {colors['card_bg_hover']};
                border-left: 5px solid {color};
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
        menu = QMenu(self)
        a_copy = menu.addAction("Copy")
        menu.addSeparator()
        a_dup  = menu.addAction("Duplicate")
        a_ren  = menu.addAction("Rename…")
        menu.addSeparator()
        a_tmpl = menu.addAction("Save as Template")
        menu.addSeparator()
        a_del  = menu.addAction("Delete")
        action = menu.exec_(self.mapToGlobal(event.pos()))
        if action == a_copy:
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

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(12, 12, 12, 12)
        self._layout.setSpacing(18)
        self.setWidget(self._container)
        self._cards: dict[tuple, DeviceCard] = {}   # (cat, name) → card
        self._selected: object = None
        self._section_grids: dict[str, QGridLayout] = {}

    def load(self, data: dict):
        self._clear()
        for cat in DEVICE_CATS:
            devices = data.get(cat) or {}
            # Always show categories from the registry, even if empty
            if cat in _CATEGORY_REGISTRY or cat == "others":
                self._add_section(cat, devices, data)
        self._layout.addStretch()

    def _clear(self):
        self._cards.clear()
        self._selected = None
        self._section_grids.clear()
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _add_section(self, cat: str, devices: dict, data: dict):
        color = CAT_COLOR[cat]
        label_text = CAT_LABEL[cat]

        # Section header
        header = QWidget()
        hlay = QHBoxLayout(header)
        hlay.setContentsMargins(0, 0, 0, 4)

        dot = QLabel("●")
        dot.setStyleSheet(f"color:{color}; font-size:14px;")
        hlay.addWidget(dot)

        lbl = QLabel(f"<b>{label_text}</b>  <span style='color:#999;font-size:9pt;'>"
                     f"({len(devices)} device{'s' if len(devices)!=1 else ''})</span>")
        lbl.setTextFormat(Qt.RichText)
        hlay.addWidget(lbl)
        hlay.addStretch()

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

        # Grid of cards
        grid_widget = QWidget()
        grid = QGridLayout(grid_widget)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(8)
        self._section_grids[cat] = grid

        col, row = 0, 0
        cols = 4
        for name, dev_data in devices.items():
            card = DeviceCard(cat, name, dev_data)
            card.sig_clicked.connect(self._on_card_clicked)
            card.sig_duplicate.connect(self.sig_device_duped)
            card.sig_rename.connect(self.sig_device_renamed)
            card.sig_delete.connect(self.sig_device_deleted)
            card.sig_save_tmpl.connect(self.sig_save_tmpl)
            card.sig_copy.connect(self.sig_device_copied)
            grid.addWidget(card, row, col)
            self._cards[(cat, name)] = card
            col += 1
            if col >= cols:
                col = 0
                row += 1

        self._layout.addWidget(grid_widget)

    def _on_card_clicked(self, cat: str, name: str):
        if self._selected:
            old = self._cards.get(self._selected)
            if old:
                old.set_selected(False)
        self._selected = (cat, name)
        card = self._cards.get((cat, name))
        if card:
            card.set_selected(True)
        self.sig_device_selected.emit(cat, name)

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

    def deselect_all(self):
        if self._selected:
            old = self._cards.get(self._selected)
            if old:
                old.set_selected(False)
        self._selected = None


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
        self._init_widget(current_value)

    def _init_widget(self, value):
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        tp = self._def["type"]
        if tp == "bool":
            self._w = QCheckBox()
            self._w.setChecked(bool(value) if value is not None else False)
        elif tp == "int":
            self._w = QSpinBox()
            self._w.setRange(-999999, 999999)
            self._w.setValue(int(value) if value is not None else 0)
        elif tp == "float":
            self._w = QDoubleSpinBox()
            self._w.setRange(-1e9, 1e9)
            self._w.setDecimals(4)
            self._w.setValue(float(value) if value is not None else 0.0)
        elif tp == "select":
            self._w = QComboBox()
            self._w.addItems(self._def["opts"])
            idx = self._w.findText(str(value) if value is not None else "")
            if idx >= 0:
                self._w.setCurrentIndex(idx)
        elif tp == "multiselect":
            self._w = QListWidget()
            self._w.setMinimumHeight(92)
            self._w.setMaximumHeight(150)
            selected = set(value if isinstance(value, list) else [])
            for opt in self._def.get("opts", []) or []:
                item = QListWidgetItem(str(opt))
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked if str(opt) in selected else Qt.Unchecked)
                self._w.addItem(item)
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
            # read. Used for dicts like scanDesignerParams.
            if value in (None, "", "null"):
                display = "{}"
            elif isinstance(value, str):
                # Treat a string default like "{}" as the JSON literal.
                display = value
            else:
                display = json.dumps(value)
            self._w = QLineEdit(display)
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

    def _pick_path(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select file")
        if path:
            self._w.setText(path)

    def get_value(self):
        tp = self._def["type"]
        if tp == "bool":
            return self._w.isChecked()
        if tp == "int":
            return self._w.value()
        if tp == "float":
            return self._w.value()
        if tp == "select":
            return self._w.currentText()
        if tp == "multiselect":
            return [
                self._w.item(i).text()
                for i in range(self._w.count())
                if self._w.item(i).checkState() == Qt.Checked
            ]
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
        if txt.lower() == "null":
            return None
        return txt


# =============================================================================
# PropertyEditor – right panel
# =============================================================================
class PropertyEditor(QWidget):
    sig_apply = pyqtSignal(str, str, dict)   # (category, name, new_device_dict)
    sig_rename = pyqtSignal(str, str, str)    # (category, old_name, new_name)
    sig_modified = pyqtSignal()               # any change to config settings (widgets, sections)
    sig_show_config = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(320)
        self._cat = ""
        self._name = ""
        self._device = {}
        self._data: dict = {}                 # Reference to full config data for config settings view
        self._field_widgets: dict[tuple, FieldWidget] = {}   # (section, key) → widget

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

        sys_hdr = QLabel("<b>System</b> <span style='color:#888;font-size:8pt;'>— processing, scan, focusLock, nidaq, …</span>")
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

    def load_device(self, cat: str, name: str, device: dict):
        self._cat = cat
        self._name = name
        self._device = copy.deepcopy(device)
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

        raw_mgr = self._mgr_combo.currentData()
        if raw_mgr == "__custom__":
            mgr = self._custom_mgr_edit.text().strip() or self._device.get("managerName", "")
        else:
            mgr = raw_mgr or self._device.get("managerName", "")
        schema = SCHEMAS.get(mgr, {})
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
                if section == "top":
                    current = self._device.get(f["key"], f["default"])
                elif section == "props":
                    current = props.get(f["key"], f["default"])
                else:
                    nest_key = section.split(":", 1)[1]
                    current = (props.get(nest_key) or {}).get(f["key"], f["default"])

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
        schema = SCHEMAS.get(mgr, {})
        new_device: dict = {"managerName": mgr, "managerProperties": {}}
        props = new_device["managerProperties"]

        nested_keys: dict[str, dict] = {}
        for nest_key in schema.get("nested", {}):
            nested_keys[nest_key] = {}

        for (section, key), fw in self._field_widgets.items():
            if section in ("raw", "raw_prop"):
                raw_val = fw.text().strip()  # type: ignore[attr-defined]
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
            if nest_vals:
                props[nest_key] = nest_vals

        # Validate required fields
        warnings = []
        for f in schema.get("top", []) + schema.get("props", []):
            if f["req"]:
                v = new_device.get(f["key"]) if f in schema.get("top", []) else props.get(f["key"])
                if v is None or v == "":
                    warnings.append(f"⚠  Required: {f['label']}")
        self._val_lbl.setText("\n".join(warnings))

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
        try:
            value = json.loads(val_str.strip()) if val_str.strip() else None
        except json.JSONDecodeError:
            try:
                value = ast.literal_eval(val_str.strip())
            except Exception:
                value = val_str.strip()

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
        del_btn = QPushButton("×")
        del_btn.setFixedSize(14, 14)
        del_btn.setStyleSheet(
            "QPushButton { border:none; color:#666; background:transparent; font-size:9pt; }"
            "QPushButton:hover { color:#C00; }"
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
                        defaults = _build_default_section(schema)
                        editor = SectionEditorDialog(section_key, schema, defaults, self._data, self)
                        if editor.exec_() == QDialog.Accepted:
                            self._data[section_key] = editor.result_data
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
        excluded_extras = set(DEVICE_CATS) | {"availableWidgets"} | set(SECTION_SCHEMAS.keys())

        # ── System: only configured sections, sorted; + Add at the end ──
        configured = [
            k for k in sorted(SECTION_SCHEMAS.keys())
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

        unconfigured = [k for k in sorted(SECTION_SCHEMAS.keys()) if k not in configured]
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
        current = self._data.get(key) if isinstance(self._data.get(key), dict) else None
        dlg = SectionEditorDialog(key, schema, current, self._data, self)
        if dlg.exec_() == QDialog.Accepted:
            self._data[key] = dlg.result_data
            self._refresh_sections()
            self.sig_modified.emit()

    def _on_fix_section_from_validation(self, section_key: str):
        """Handle 'Configure…' link clicks from validation warnings.

        Opens the section editor pre-filled with defaults if the section is not
        yet configured, or with current values if it is.
        """
        schema = SECTION_SCHEMAS.get(section_key)
        if not schema:
            return
        current = self._data.get(section_key)
        if current in (None, {}, []):
            # Section not configured yet - pre-fill with defaults
            current = _build_default_section(schema)
        dlg = SectionEditorDialog(section_key, schema, current, self._data, self)
        if dlg.exec_() == QDialog.Accepted:
            self._data[section_key] = dlg.result_data
            self._refresh_sections()
            self._val_panel.validate(self._data)  # refresh validation to clear the warning
            self.sig_modified.emit()

    def _open_section_picker(self):
        """Open the picker for adding a new system section."""
        candidates = [
            (k, SECTION_SCHEMAS[k]) for k in sorted(SECTION_SCHEMAS.keys())
            if k not in self._data or self._data[k] in (None, {}, [])
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
        defaults = _build_default_section(schema)
        dlg = SectionEditorDialog(key, schema, defaults, self._data, self)
        if dlg.exec_() == QDialog.Accepted:
            self._data[key] = dlg.result_data
            self._refresh_sections()
            self.sig_modified.emit()

    def _add_custom_section(self):
        """Prompt for a new free-form top-level key and open the raw editor."""
        reserved = (
            set(DEVICE_CATS) | {"availableWidgets"} | set(SECTION_SCHEMAS.keys())
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

    script_dir = Path(__file__).resolve().parent

    # ── Source 2: parse availableWidgets docstring in ViewSetupInfo.py ──
    view_setup_path = (script_dir / ".." / "imswitch" / "imcontrol"
                       / "view" / "guitools" / "ViewSetupInfo.py")
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
    main_view_path = (script_dir / ".." / "imswitch" / "imcontrol"
                      / "view" / "ImConMainView.py")
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


# =============================================================================
# SectionEditorDialog – schema-driven editor for singleton sections
# =============================================================================
class SectionEditorDialog(QDialog):
    """Modal form editor for one top-level system section (focusLock, scan, …).

    Reuses :class:`FieldWidget` so its inputs match the device editor.
    Ref-type fields are populated from the live config data dict so that
    e.g. ``focusLock.camera`` is a dropdown of existing detector names.
    """

    def __init__(self, key: str, schema: dict, current: dict | None, data: dict, parent=None):
        super().__init__(parent)
        self._key = key
        self._schema = schema
        self._data = data  # reference, used to populate ref combos
        self._result: dict = {}

        legacy = schema.get("legacy")
        title = f"Edit section: {schema.get('display', key)}"
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
                cur = (current or {}).get(f["key"], f.get("default", ""))
                # Normalize default sentinels.
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
        out: dict = {}
        missing: list = []
        for f in self._schema.get("fields", []):
            fw = self._field_widgets.get(f["key"])
            if fw is None:
                continue
            val = fw.get_value()
            if f.get("req") and (val is None or val == "" or val == []):
                missing.append(f["label"])
            out[f["key"]] = val
        if missing:
            self._err_lbl.setText(
                "Missing required: " + ", ".join(missing)
            )
            return
        self._result = out
        self.accept()

    @property
    def result_data(self) -> dict:
        return self._result


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
    Persists user templates as JSON files in a templates/ directory next to
    the script.  One sub-directory per category; one JSON file per template.
    templates/
      detectors/
        MyAPD.json
      lasers/
        My488nm.json
    """

    def __init__(self, store_dir: object = None):
        if store_dir is None:
            store_dir = Path(os.path.abspath(__file__)).parent / "templates"
        self._dir = Path(store_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict = {}   # cat → {name: device}
        self.reload()

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
    # (category, device_or_mgr_name): str mgr_name for builtin, dict for user
    sig_tmpl_add  = pyqtSignal(str, object)
    sig_folder_reloaded = pyqtSignal(str, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(242)
        self._store = TemplateStore()
        self._config_dir: object = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)

        # ── Config Files ──
        hdr = QLabel("<b>Config Files</b>")
        hdr.setTextFormat(Qt.RichText)
        hdr.setFixedHeight(22)
        hdr.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        lay.addWidget(hdr)

        folder_btns = QHBoxLayout()
        folder_btns.setSpacing(4)

        open_btn = QPushButton("Open Folder…")
        open_btn.clicked.connect(self._open_folder)
        folder_btns.addWidget(open_btn, 1)

        reload_btn = QPushButton("Reload")
        reload_btn.setToolTip("Reload JSON configs from the currently selected folder")
        reload_btn.clicked.connect(self.reload_folder)
        folder_btns.addWidget(reload_btn)
        lay.addLayout(folder_btns)

        self._file_list = QTreeWidget()
        self._file_list.setHeaderHidden(True)
        self._file_list.setIndentation(12)
        self._file_list.setTextElideMode(Qt.ElideNone)
        self._file_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._file_list.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._file_list.setMinimumHeight(180)
        self._file_list.setMaximumHeight(230)
        self._file_list.header().setStretchLastSection(False)
        self._file_list.setColumnWidth(0, 360)
        self._file_list.itemDoubleClicked.connect(self._on_file_double_click)
        lay.addWidget(self._file_list)

        show_tmpl_btn = QPushButton("Add from Template…")
        show_tmpl_btn.setToolTip("Show built-in and saved templates for adding devices")
        show_tmpl_btn.clicked.connect(self._show_templates)
        lay.addWidget(show_tmpl_btn)
        self._show_tmpl_btn = show_tmpl_btn

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

        hide_tmpl_btn = QPushButton("Hide")
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
    def _open_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Open Config Folder",
                                             str(Path.home()))
        if d:
            self._config_dir = d
            count = self._refresh_file_list(d)
            self.sig_folder_reloaded.emit(d, count)

    def _refresh_file_list(self, folder: str) -> int:
        self._file_list.clear()
        count = 0
        for p in sorted(Path(folder).glob("*.json")):
            item = QTreeWidgetItem([p.name])
            item.setData(0, Qt.UserRole, str(p))
            item.setToolTip(0, str(p))
            self._file_list.addTopLevelItem(item)
            count += 1
        self._file_list.resizeColumnToContents(0)
        self._file_list.setColumnWidth(0, max(self._file_list.columnWidth(0), 360))
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
        errors: list = []
        warnings: list = []
        notes: list = []

        # Existing DAQ conflict check
        used = _collect_daq_channels(data)
        for ch, names in used.items():
            if len(names) > 1:
                errors.append(f"DAQ conflict <b>{ch}</b>: {', '.join(names)}")

        # Cross-reference checks between system sections and devices
        for severity, msg in _collect_xref_issues(data):
            if severity == "error":
                errors.append(msg)
            elif severity == "warning":
                warnings.append(msg)
            else:
                notes.append(msg)

        present = [CAT_LABEL[c] for c in DEVICE_CATS if data.get(c)]
        devcount = sum(len(data.get(c) or {}) for c in DEVICE_CATS)

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
            self._text.setText(
                f"<span style='color:#3A8B3A;'>✓ No issues</span>"
                f"<span style='color:#666;'> — {devcount} devices"
                f" ({cats_str})</span>"
            )
            self._text.setStyleSheet("font-size:8pt;")


# =============================================================================
# MainWindow
# =============================================================================
class MainWindow(QMainWindow):
    # Well-known location of imcontrol_options.json
    _OPTIONS_SEARCH_ROOTS = [
        Path.home() / "Documents" / "ImSwitchConfig" / "config",
        Path.home() / "ImSwitchConfig" / "config",
        Path("/") / "etc" / "imswitch",
    ]

    def __init__(self, start_folder: str = ""):
        super().__init__()
        self.setWindowTitle("ImSwitch Config Studio")
        self.resize(1280, 780)
        self._data: dict = {}
        self._path: str = ""
        self._modified = False
        self._options_path: str = ""   # path to imcontrol_options.json if found
        self._copied_device: object = None  # (category, name, device_dict)

        self._build_toolbar()
        self._build_ui()
        self._build_status_bar()

        if start_folder and os.path.isdir(start_folder):
            self._left.set_folder(start_folder)

        self._detect_options_file()

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
        _btn("Save",       self._save_file,  "Save current file (Ctrl+S)")
        _btn("Save As…",   self._save_as,    "Save to a new file")
        _btn("Paste Device", self._paste_device, "Paste the copied device into this config")
        tb.addSeparator()
        _btn("Validate",   self._run_validation, "Check for DAQ conflicts")

        self.addAction(self._make_shortcut("Ctrl+S", self._save_file))

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

        # ── Active-config banner (hidden until options file found) ──
        self._active_banner = QFrame()
        self._active_banner.setStyleSheet(
            "QFrame { background:#1A3A5C; border-bottom:1px solid #0D2540; }"
        )
        self._active_banner.setFixedHeight(36)
        bl = QHBoxLayout(self._active_banner)
        bl.setContentsMargins(10, 0, 10, 0)

        self._active_lbl = QLabel()
        self._active_lbl.setStyleSheet("color:#9CC4F0; font-size:9pt;")
        self._active_lbl.setTextFormat(Qt.RichText)
        bl.addWidget(self._active_lbl)
        bl.addStretch()

        set_active_btn = QPushButton("Set as Active Config")
        set_active_btn.setFixedHeight(24)
        set_active_btn.setStyleSheet("""
            QPushButton {
                background:#2E6DA4; color:white; border-radius:3px;
                font-size:9pt; padding:0 10px;
            }
            QPushButton:hover { background:#3A85C4; }
            QPushButton:disabled { background:#334; color:#668; }
        """)
        set_active_btn.clicked.connect(self._set_as_active_config)
        self._set_active_btn = set_active_btn
        bl.addWidget(set_active_btn)

        self._active_banner.setVisible(False)
        root_lay.addWidget(self._active_banner)

        splitter = QSplitter(Qt.Horizontal)
        root_lay.addWidget(splitter, 1)

        # Left
        self._left = LeftPanel()
        self._left.sig_file_open.connect(self._load_file)
        self._left.sig_tmpl_add.connect(self._add_from_template)
        self._left.sig_folder_reloaded.connect(self._on_folder_reloaded)
        splitter.addWidget(self._left)

        # Centre + validation
        centre = QWidget()
        cl = QVBoxLayout(centre)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        self._canvas = DeviceCanvas()
        self._canvas.sig_device_selected.connect(self._on_device_selected)
        self._canvas.sig_device_deleted.connect(self._on_device_deleted)
        self._canvas.sig_device_duped.connect(self._on_device_duped_or_add)
        self._canvas.sig_device_renamed.connect(self._on_device_rename_from_canvas)
        self._canvas.sig_save_tmpl.connect(self._on_save_template)
        self._canvas.sig_device_copied.connect(self._copy_device)
        cl.addWidget(self._canvas, 1)
        self._val_panel = ValidationPanel()
        self._val_panel.sig_fix_section.connect(self._on_fix_section_from_validation)
        cl.addWidget(self._val_panel)
        splitter.addWidget(centre)

        # Right
        self._editor = PropertyEditor()
        self._editor.sig_apply.connect(self._on_editor_apply)
        self._editor.sig_rename.connect(self._on_device_rename)
        self._editor.sig_modified.connect(self._on_extras_modified)
        self._editor.sig_show_config.connect(self._show_config_settings)
        splitter.addWidget(self._editor)

        splitter.setSizes([242, 758, 340])

    def _build_status_bar(self):
        sb = QStatusBar()
        self.setStatusBar(sb)
        self._status = QLabel("Ready")
        sb.addWidget(self._status)

    # ── File operations ───────────────────────────────────────────────────
    def _new_file(self):
        if not self._confirm_discard():
            return
        self._data = {cat: {} for cat in DEVICE_CATS}
        self._data["availableWidgets"] = []
        self._path = ""
        self._modified = False
        self._refresh_canvas()
        self._editor.load_config_extras(self._data)
        self._status.setText("New config (unsaved)")
        self.setWindowTitle("ImSwitch Config Studio — [new]")

    def _open_file(self):
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Config", "", "JSON files (*.json)"
        )
        if path:
            self._load_file(path)

    def _load_file(self, path: str):
        try:
            with open(path, encoding="utf-8") as fh:
                self._data = json.load(fh)
            self._path = path
            self._left.set_folder(str(Path(path).parent))
            self._modified = False
            self._refresh_canvas()
            self._editor.load_config_extras(self._data)
            self._val_panel.validate(self._data)
            self.setWindowTitle(f"ImSwitch Config Studio — {Path(path).name}")
            self._status.setText(f"Loaded: {path}")
            # Try to find options file near the loaded config if not yet found
            if not self._options_path:
                self._detect_options_file()
            else:
                self._update_active_banner()
        except Exception as e:
            QMessageBox.critical(self, "Load Error", str(e))

    def _on_folder_reloaded(self, folder: str, count: int):
        self._status.setText(f"Reloaded {count} JSON config(s) from {folder}")

    def _save_file(self):
        if not self._path:
            self._save_as()
            return
        self._write_file(self._path)

    def _save_as(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Config As", self._path or "", "JSON files (*.json)"
        )
        if path:
            self._write_file(path)

    def _write_file(self, path: str):
        try:
            data_to_write = copy.deepcopy(self._data)
            # Strip empty "others" dict to keep saved JSON clean
            if "others" in data_to_write and not data_to_write["others"]:
                del data_to_write["others"]
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data_to_write, fh, indent=2, ensure_ascii=False)
            self._path = path
            self._modified = False
            self.setWindowTitle(f"ImSwitch Config Studio — {Path(path).name}")
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

    def _run_validation(self):
        self._val_panel.validate(self._data)

    def _show_config_settings(self):
        self._canvas.deselect_all()
        self._editor.load_config_extras(self._data)
        self._status.setText("Showing config settings")

    def _on_device_selected(self, cat: str, name: str):
        device = (self._data.get(cat) or {}).get(name)
        if device is not None:
            self._editor.load_device(cat, name, device)

    def _on_editor_apply(self, cat: str, name: str, new_device: dict):
        if cat not in self._data or not isinstance(self._data[cat], dict):
            self._data[cat] = {}
        self._data[cat][name] = new_device
        self._modified = True
        self._refresh_canvas()
        self._val_panel.validate(self._data)
        self._status.setText(f"Updated: {name}")
        # Reselect the card
        self._canvas._on_card_clicked(cat, name)

    def _on_device_rename(self, cat: str, old_name: str, new_name: str):
        section = self._data.get(cat)
        if not section or old_name not in section:
            return
        if new_name in section:
            QMessageBox.warning(self, "Rename", f"Name '{new_name}' already exists.")
            return
        section[new_name] = section.pop(old_name)
        self._modified = True
        self._refresh_canvas()
        self._canvas._on_card_clicked(cat, new_name)

    def _on_device_rename_from_canvas(self, cat: str, name: str):
        new_name, ok = QInputDialog.getText(
            self, "Rename Device", "New name:", text=name
        )
        if ok and new_name.strip():
            self._on_device_rename(cat, name, new_name.strip())

    def _on_device_deleted(self, cat: str, name: str):
        if cat == "__ADD__":
            return
        r = QMessageBox.question(
            self, "Delete Device",
            f"Delete '{name}' from {CAT_LABEL.get(cat, cat)}?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if r == QMessageBox.Yes:
            section = self._data.get(cat) or {}
            section.pop(name, None)
            self._modified = True
            self._editor.clear()
            self._refresh_canvas()
            self._val_panel.validate(self._data)

    def _on_device_duped_or_add(self, cat_signal: str, name_signal: str):
        # Handle the "Add" path from canvas's internal _request_add
        if cat_signal == "__ADD__":
            parts = name_signal.split("|", 2)
            if len(parts) == 3:
                cat, dev_name, mgr = parts
                if mgr == "__custom__":
                    device = {"managerName": "", "managerProperties": {}}
                else:
                    device = _build_default_device(mgr)
                self._add_device(cat, dev_name, device)
            return
        # Normal duplicate
        section = self._data.get(cat_signal) or {}
        original = section.get(name_signal)
        if original is None:
            return
        base = name_signal + "_copy"
        i = 1
        while base in section:
            base = f"{name_signal}_copy{i}"
            i += 1
        section[base] = copy.deepcopy(original)
        self._modified = True
        self._refresh_canvas()
        self._canvas._on_card_clicked(cat_signal, base)

    def _copy_device(self, cat: str, name: str):
        device = (self._data.get(cat) or {}).get(name)
        if device is None:
            return
        self._copied_device = (cat, name, copy.deepcopy(device))
        self._status.setText(f"Copied device: {name}")

    def _paste_device(self):
        if not self._copied_device:
            QMessageBox.information(self, "Paste Device", "Copy a device first.")
            return
        cat, name, device = self._copied_device
        if cat not in self._data or not isinstance(self._data.get(cat), dict):
            self._data[cat] = {}
        section = self._data[cat]
        if name in section:
            response = QMessageBox.question(
                self,
                "Replace Device?",
                f"A device named '{name}' already exists in {CAT_LABEL.get(cat, cat)}.\n\n"
                "Replace it with the copied device?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if response != QMessageBox.Yes:
                return
        section[name] = copy.deepcopy(device)
        self._modified = True
        self._refresh_canvas()
        self._val_panel.validate(self._data)
        self._canvas._on_card_clicked(cat, name)
        self._status.setText(f"Pasted device: {name}")

    def _add_from_template(self, cat: str, device_or_mgr):
        """Slot for LeftPanel.sig_tmpl_add(cat, device_or_mgr).

        device_or_mgr is either a manager name string (builtin template)
        or a device dict (user template).
        """
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
        self._add_device(cat, name.strip(), tdata)

    def _add_device(self, cat: str, name: str, device: dict):
        if cat not in self._data or not isinstance(self._data.get(cat), dict):
            self._data[cat] = {}
        if name in self._data[cat]:
            name = name + "_1"
        self._data[cat][name] = device
        self._modified = True
        self._refresh_canvas()
        self._canvas._on_card_clicked(cat, name)
        self._editor.load_device(cat, name, device)

    def _on_save_template(self, cat: str, name: str):
        device = (self._data.get(cat) or {}).get(name)
        if device is None:
            return
        tname, ok = QInputDialog.getText(
            self, "Save Template", "Template name:", text=name
        )
        if not ok or not tname.strip():
            return
        tname = tname.strip()

        # Ask which category to file this under
        existing = self._left._store.categories()
        # Suggest the device's natural category as default
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
        self._modified = True
        t = self.windowTitle()
        if not t.endswith(" *"):
            self.setWindowTitle(t + " *")
        # Re-run validation so cross-reference issues update live as the user
        # adds, edits, or removes system sections.
        self._val_panel.validate(self._data)

    # ── Active config / options file ──────────────────────────────────────
    def _detect_options_file(self):
        """Search well-known locations for imcontrol_options.json."""
        candidates = list(self._OPTIONS_SEARCH_ROOTS)
        # Also look next to an already-open config file
        if self._path:
            candidates.insert(0, Path(self._path).parent.parent / "config")
        for root in candidates:
            p = Path(root) / "imcontrol_options.json"
            if p.exists():
                self._options_path = str(p)
                self._active_banner.setVisible(True)
                self._update_active_banner()
                return
        self._active_banner.setVisible(False)

    def _update_active_banner(self):
        if not self._options_path:
            return
        try:
            with open(self._options_path, encoding="utf-8") as fh:
                opts = json.load(fh)
            active = opts.get("setupFileName", "<not set>")
        except Exception:
            active = "<error reading options>"

        is_current = bool(self._path) and Path(self._path).name == active
        mark = "  <span style='color:#5DADE2;'>◀ currently open</span>" if is_current else ""
        self._active_lbl.setText(
            f"<b style='color:#9CC4F0;'>Active config:</b>"
            f"  <span style='color:white;'>{active}</span>{mark}"
            f"  <span style='color:#5A7A9A; font-size:8pt;'>"
            f"({Path(self._options_path).parent})</span>"
        )
        # Disable "Set as Active" if this file is already active or nothing is open
        already_active = is_current or not self._path
        self._set_active_btn.setEnabled(not already_active)
        self._set_active_btn.setToolTip(
            "This file is already the active config" if is_current
            else "Open a config file first" if not self._path
            else f"Set {Path(self._path).name} as the active ImSwitch config"
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

        try:
            with open(self._options_path, encoding="utf-8") as fh:
                opts = json.load(fh)
            opts["setupFileName"] = Path(self._path).name
            with open(self._options_path, "w", encoding="utf-8") as fh:
                json.dump(opts, fh, indent=4, ensure_ascii=False)
            self._update_active_banner()
            self._status.setText(
                f"Active config set to: {Path(self._path).name}"
            )
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not update options file:\n{e}")

    def closeEvent(self, event):
        if self._modified and not self._confirm_discard():
            event.ignore()
        else:
            event.accept()

def dark_theme(app, palette):
    """Apply complete dark theme with palette and comprehensive QSS stylesheet."""
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

    # Comprehensive QSS stylesheet
    stylesheet = """
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
    app.setStyleSheet(stylesheet)
    return palette

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
        _default = Path.home() / "Documents" / "ImSwitchConfig" / "imcontrol_setups"
        folder = str(_default) if _default.is_dir() else ""
    win = MainWindow(start_folder=folder)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
