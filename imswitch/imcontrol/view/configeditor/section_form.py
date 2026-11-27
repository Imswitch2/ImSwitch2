"""The form that edits one top-level section of a setup file.

Shared by the Config Studio (:mod:`.editor`) and by ImControl, which shows
the same form when a panel that needs a section -- Tiling, Autofocus -- is
added while the application runs. Kept apart from the editor so that ImControl
can import it without loading the Studio, which scans the manager catalog and
every built-in template when it is imported.

Like the editor, this uses PyQt5 directly: the two share widgets.
"""

import ast
import copy
import json
import math
import re
from pathlib import Path

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
    QScrollArea, QTabWidget, QVBoxLayout, QWidget,
)

try:
    from imswitch.imcontrol.model.configeditor import coercion as _coercion_module
except Exception:  # noqa: BLE001 -- the editor degrades the same way
    _coercion_module = None


SECTION_TEMPLATE_DIR = Path(__file__).resolve().parent / "builtin_templates" / "sections"


def load_section_schemas(resolve_options=None) -> dict:
    """Load singleton-section schemas from builtin_templates/sections/.

    Returns a dict keyed by the top-level JSON key (e.g. ``"focusLock"``,
    ``"scan"``). Section schemas use ``fields`` for direct children and may
    also use the same ``nested`` mapping as device schemas for structured
    child dictionaries. They additionally carry ``section``, ``group``
    (``"system"`` or ``"extras"``), and optional ``legacy`` flags.

    ``resolve_options(schema)``, when given, replaces option placeholders
    (``"__scan_cloaks__"`` and the like) in place; the editor passes its own.
    """
    sections: dict = {}
    base = SECTION_TEMPLATE_DIR
    if base.is_dir():
        for f in sorted(base.glob("*.json")):
            try:
                with open(f, encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception:
                continue
            key = data.get("key") or f.stem
            if resolve_options is not None:
                resolve_options(data)
            sections[key] = data
    return sections


def load_section_schema(key: str) -> dict | None:
    """The schema of one section, without resolving option placeholders."""
    return load_section_schemas().get(key)


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
    sig_edited = pyqtSignal()   # the operator changed the value (never on construction)

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
            scale = float(self._def.get("display_scale", 1))
            shown = value
            if value is not None and scale != 1:
                try:
                    shown = f"{float(value) * scale:g}"
                except (ValueError, TypeError):
                    pass
            self._w = QLineEdit("" if shown is None else str(shown))
        elif tp == "float_pair":
            # Two related numeric values stored as one two-element JSON list.
            # This is intentionally a small generic widget rather than a scan-
            # specific editor (e.g. manual FOV is naturally X × Y).
            values = value if isinstance(value, (list, tuple)) else ()
            if len(values) == 0 and value not in (None, ""):
                values = (value, value)
            left = values[0] if len(values) > 0 else None
            right = values[1] if len(values) > 1 else left
            self._pair_edits = (
                QLineEdit("" if left is None else str(left)),
                QLineEdit("" if right is None else str(right)),
            )
            pair = QWidget()
            pair_lay = QHBoxLayout(pair)
            pair_lay.setContentsMargins(0, 0, 0, 0)
            pair_lay.setSpacing(5)
            pair_lay.addWidget(self._pair_edits[0])
            pair_lay.addWidget(QLabel("×"))
            pair_lay.addWidget(self._pair_edits[1])
            self._w = pair
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
            selected = set(value if isinstance(value, list) else [])
            options = list(self._def.get("opts", []) or [])
            # Keep choices from newer templates/plugins visible and checked.
            for selected_value in selected:
                if selected_value not in {str(option) for option in options}:
                    options.append(selected_value)
            for opt in options:
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
            for edit in getattr(self, "_pair_edits", ()):
                edit.setToolTip(self._def["tip"])

    def _watch_for_edits(self):
        """Mark the field touched on user interaction, never on construction.

        Signals that also fire programmatically (``valueChanged``,
        ``itemChanged``) are connected only after the initial value is in
        place, so construction cannot trip them; ``textEdited``, ``clicked``
        and ``activated`` are user-only by contract.
        """
        def touch(*_args):
            self._touched = True
            self.sig_edited.emit()

        if self._def.get("type") == "float_pair":
            for edit in self._pair_edits:
                edit.textEdited.connect(touch)
            return

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
            self.sig_edited.emit()

    def get_value(self):
        if not self._touched:
            return self._original
        tp = self._def["type"]
        if tp == "bool":
            return self._w.isChecked()
        if tp == "bool_auto":
            return self._w.currentData()  # None (Automatic) / True / False
        if tp in ("int", "float"):
            value = _parse_number(self._w.text())
            scale = float(self._def.get("display_scale", 1))
            if value is not None and isinstance(value, (int, float)) and scale != 1:
                return value / scale
            return value
        if tp == "float_pair":
            return [_parse_number(edit.text()) for edit in self._pair_edits]
        if tp == "select":
            return self._w.currentData()
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
        if tp == "path" or _coercion_module is None:
            if txt.lower() == "null":
                return None
            return txt
        return _coercion_module.text_to_json(txt, self._original, tp)


def _ref_categories(schema: dict) -> list:
    """The device categories a schema's ref fields draw their options from."""
    fields = list(schema.get("fields", []))
    for nested in schema.get("nested", {}).values():
        fields.extend(nested)
    return sorted({opt for f in fields if f.get("type") == "ref"
                   for opt in (f.get("opts") or []) if isinstance(opt, str)})


# =============================================================================
# SectionEditorDialog – schema-driven editor for singleton sections
# =============================================================================
class SectionEditorDialog(QDialog):
    """Modal form editor for one top-level system section (focusLock, scan, …).

    Reuses :class:`FieldWidget` so its inputs match the device editor.
    Ref-type fields are populated from the live config data dict so that
    e.g. ``focusLock.camera`` is a dropdown of existing detector names.

    With ``allow_custom_fields`` the dialog also lists the keys of
    ``current`` that the schema does not define, in a "Custom" tab, and
    offers "Add property" to add more; Apply writes them back with the
    schema fields instead of dropping them.

    Section schemas may also expose structured child dictionaries through
    ``nested``.  Nested fields follow the same important editing contract as
    device ``managerProperties``: untouched absent containers stay absent and
    unknown/unexposed keys are preserved when an exposed child is edited.
    """

    def __init__(self, key: str, schema: dict, current: dict | None, data: dict, parent=None, *,
                 title_prefix="Edit section", allow_custom_fields=False,
                 device_categories=None, validator=None):
        super().__init__(parent)
        # ``validator(result) -> list[str]`` runs on Apply after the required
        # fields are checked; its messages keep the dialog open.
        self._validator = validator
        self._key = key
        self._schema = schema
        self._data = data  # reference, used to populate ref combos
        self._current = copy.deepcopy(current) if isinstance(current, dict) else {}
        self._result: dict = {}
        self._allow_custom = allow_custom_fields
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
        if device_categories is None:
            device_categories = _ref_categories(schema)
        pool: dict = {}
        for cat in device_categories:
            pool[cat] = sorted(list((self._data.get(cat) or {}).keys()))

        # ── Tabs grouped by `grp` ──
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        lay.addWidget(tabs, 1)

        # Keys are ("field", key) for direct children and
        # ("nested:<container>", key) for structured child dictionaries.
        self._field_widgets: dict = {}
        self._tab_sections: dict = {}   # tab index -> {"field" | "nested:<container>"}
        self._shown_when = schema.get("nested_shown_when", {})
        groups: dict[str, list] = {}
        for f in schema.get("fields", []):
            groups.setdefault(f.get("grp", "Basic"), []).append(("field", f))
        for nest_key, nest_fields in schema.get("nested", {}).items():
            for f in nest_fields:
                groups.setdefault(f.get("grp", "Basic"), []).append((f"nested:{nest_key}", f))
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
            for section, f in groups[grp]:
                if section == "field":
                    cur = self._current.get(f["key"], _MISSING)
                else:
                    nest_key = section.split(":", 1)[1]
                    container = self._current.get(nest_key)
                    cur = container.get(f["key"], _MISSING) if isinstance(container, dict) else _MISSING
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
                self._field_widgets[(section, f["key"])] = fw
            scroll.setWidget(inner)
            index = tabs.addTab(scroll, grp)
            self._tab_sections[index] = {section for section, _f in groups[grp]}

        # A nested container that only matters for one choice (the scan
        # cloak's options) shows its tab only while that choice is made.
        self._tab_widget = tabs
        for alternatives in self._shown_when.values():
            for condition in alternatives:
                for key in condition:
                    fw = self._field_widgets.get(("field", key))
                    if fw is not None:
                        fw.sig_edited.connect(self._update_conditional_tabs)
        self._update_conditional_tabs()

        # A scan configuration is not migrated by opening or applying it.
        # Show missing Beta stage timing without supplying values implicitly.
        self._beta_timing_notice = None
        if self._key == "scan":
            self._beta_timing_notice = QLabel()
            self._beta_timing_notice.setStyleSheet("color:#C0801C; font-size:9pt;")
            self._beta_timing_notice.setWordWrap(True)
            lay.addWidget(self._beta_timing_notice)
            for field_key in ("scanDesigner", "scanDesignerParams"):
                fw = self._field_widgets.get(("field", field_key))
                if fw is not None:
                    fw.sig_edited.connect(self._update_beta_timing_notice)
            for key in ("return_time", "move_time", "settle_time"):
                fw = self._field_widgets.get(("nested:scanDesignerParams", key))
                if fw is not None:
                    fw.sig_edited.connect(self._update_beta_timing_notice)
            self._update_beta_timing_notice()

        # ── Custom tab: keys the schema does not define ──
        if self._allow_custom:
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
            known = self._schema_keys()
            for k, v in (current or {}).items():
                if k not in known:
                    self._add_custom_row(k, v)
            scroll.setWidget(inner)
            self._custom_tab = scroll
            tabs.addTab(scroll, "Custom")
            self._tabs = tabs

            add_row = QHBoxLayout()
            add_prop_btn = QPushButton("⊕ Add property")
            add_prop_btn.setFixedHeight(22)
            add_prop_btn.setStyleSheet("font-size:8pt; padding:0 6px;")
            add_prop_btn.setToolTip("Add an arbitrary key to these settings")
            add_prop_btn.clicked.connect(self._prompt_custom_field)
            add_row.addWidget(add_prop_btn)
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

    def _nested_shown(self, nest_key) -> bool:
        """Whether a nested container applies to the values the form shows now.

        ``nested_shown_when`` lists alternatives, each a {field: value} dict
        that must match entirely; a container it does not name always shows.
        """
        alternatives = self._shown_when.get(nest_key)
        if not alternatives:
            return True
        for condition in alternatives:
            if all(self._form_value(key) == value for key, value in condition.items()):
                return True
        return False

    def _form_value(self, key):
        fw = self._field_widgets.get(("field", key))
        return fw.get_value() if fw is not None else self._current.get(key)

    def _update_conditional_tabs(self):
        """Hide a tab whose every field belongs to a container that does not apply.

        Hiding is display only: an untouched container is applied exactly as
        the file had it, so switching the choice back and forth loses nothing.
        """
        for index, sections in self._tab_sections.items():
            shown = any(
                section == "field" or self._nested_shown(section.split(":", 1)[1])
                for section in sections
            )
            self._tab_widget.setTabVisible(index, shown)

    def _beta_timing_edited_params(self):
        """Preview the effective explicit keys, never the designer fallbacks."""
        raw = self._field_widgets.get(("field", "scanDesignerParams"))
        params = (raw.get_value() if raw is not None and raw.is_touched()
                  else self._current.get("scanDesignerParams"))
        params = copy.deepcopy(params) if isinstance(params, dict) else {}
        for key in ("return_time", "move_time", "settle_time"):
            field = self._field_widgets.get(("nested:scanDesignerParams", key))
            if field is not None and field.is_touched():
                value = field.get_value()
                if value is None:
                    params.pop(key, None)
                else:
                    params[key] = value
        return params

    def _update_beta_timing_notice(self):
        if self._beta_timing_notice is None:
            return
        if self._form_value("scanDesigner") != "BetaScanDesigner":
            self._beta_timing_notice.hide()
            return
        from imswitch.imcontrol.model.beta_scan_timing import missing_beta_timing_keys
        params = self._beta_timing_edited_params()
        missing = missing_beta_timing_keys(params)
        if "return_time" not in params:
            missing = ("return_time",) + missing
        if missing:
            self._beta_timing_notice.setText(
                "Beta stage timing not configured: " + ", ".join(missing)
                + ". Empty fields stay absent until you edit and save them. "
                "Missing move/settle use the designer's 2 ms fallback."
            )
            self._beta_timing_notice.show()
        else:
            self._beta_timing_notice.hide()

    def _try_apply(self):
        # Start from the file's section, not a blank dict.  This is what makes
        # a curated section form safe for plugin/future keys it does not know.
        out: dict = copy.deepcopy(self._current)
        missing: list = []

        # Preserve historical section-editor behaviour for direct fields:
        # applying the form materializes their displayed defaults.
        for f in self._schema.get("fields", []):
            fw = self._field_widgets.get(("field", f["key"]))
            if fw is None:
                continue
            val = fw.get_value()
            if f.get("req") and (val is None or val == "" or val == []):
                missing.append(f["label"])
            out[f["key"]] = val

        # Nested containers are deliberately different: they are optional
        # structured configuration. Merely opening/applying a section must not
        # create simplePointScan/simpleMoNaLISA (or seed their defaults). Only
        # operator edits materialize absent children; existing unknown keys are
        # retained by modifying a copy of the original container in place.
        for nest_key, nest_fields in self._schema.get("nested", {}).items():
            original_container = self._current.get(nest_key)
            # Begin from the direct/raw JSON field if the operator edited it;
            # typed timing fields override only their own keys.
            source = out.get(nest_key, original_container)
            container = copy.deepcopy(source) if isinstance(source, dict) else None
            touched_any = False
            for f in nest_fields:
                fw = self._field_widgets.get((f"nested:{nest_key}", f["key"]))
                if fw is None:
                    continue
                if not fw.is_touched():
                    continue
                touched_any = True
                val = fw.get_value()
                if f.get("req") and (val is None or val == "" or val == []):
                    missing.append(f["label"])
                if (nest_key == "scanDesignerParams"
                        and self._form_value("scanDesigner") == "BetaScanDesigner"):
                    if val is None:
                        if container is not None:
                            container.pop(f["key"], None)
                        continue
                    if (not isinstance(val, (int, float)) or not math.isfinite(val)
                            or val < 0):
                        self._err_lbl.setText(
                            f"{f['label']} must be a finite, nonnegative number."
                        )
                        return
                if container is None:
                    container = {}
                container[f["key"]] = val
            if touched_any:
                out[nest_key] = container if container is not None else {}

        if missing:
            self._err_lbl.setText(
                "Missing required: " + ", ".join(missing)
            )
            return
        for k, le in self._custom_widgets.items():
            original = le.property("originalValue")
            if _coercion_module is not None:
                out[k] = _coercion_module.raw_text_to_json(le.text(), original)
            else:
                out[k] = _parse_freeform_value(le.text())
        if self._validator is not None:
            errors = list(self._validator(copy.deepcopy(out)) or [])
            if errors:
                self._err_lbl.setText("\n".join(errors))
                return
        self._result = out
        self.accept()

    def _schema_keys(self) -> set:
        """The keys the form itself edits: its fields and its nested
        containers. ``_field_widgets`` is keyed by ``(section, key)``, so a
        name is looked up here; a custom property may not take one."""
        return ({f["key"] for f in self._schema.get("fields", [])}
                | set(self._schema.get("nested", {})))

    def add_custom_field(self, key: str, value) -> str:
        """Add a custom key; returns an error message, or "" on success."""
        if not self._allow_custom:
            return "These settings take no custom properties."
        key = key.strip()
        if not key:
            return "Key name is empty."
        if key in self._schema_keys() or key in self._custom_widgets:
            return f"'{key}' already exists in these settings."
        self._add_custom_row(key, value)
        self._tabs.setCurrentWidget(self._custom_tab)
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

    @property
    def result_data(self) -> dict:
        return self._result
