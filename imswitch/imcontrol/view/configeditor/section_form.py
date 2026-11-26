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
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
    QScrollArea, QStyle, QTabWidget, QVBoxLayout, QWidget,
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


# =============================================================================
# Palette & schema registry (derived from JSON files at import time)
# Field helper: (key, label, type, default, required, group, tooltip, options)
# Types: text | int | float | bool | select | path
# =============================================================================
def _f(key, label, tp="text", default="", req=False,
       grp="Basic", tip="", opts=None):
    return dict(key=key, label=label, type=tp, default=default,
                req=req, grp=grp, tip=tip, opts=opts or [])


#: ``schema["write_policy"]`` value meaning "write the section as the file has
#: it": Apply writes a field only if the file already had its key, the
#: operator touched it, or it is required.
WRITE_AS_WRITTEN = "as_written"


def _writes_as_written(schema: dict) -> bool:
    """Whether Apply writes this schema's block as the file has it.

    Every singleton section (``"section": true``: the System card and the
    ImProcess block) does, unless its template says otherwise with an explicit
    ``write_policy``. A section is a block of keys the code reads with its own
    defaults, so expanding a short block into every template field, or dropping
    a key the template does not list, changes the file without anyone asking.
    A schema that is not a section (the per-category settings) keeps writing
    every schema field.
    """
    default = WRITE_AS_WRITTEN if schema.get("section") else None
    return schema.get("write_policy", default) == WRITE_AS_WRITTEN


#: The label of the combo that chooses it.
PRIMARY_CHOICE_LABEL = "Starts with:"
PRIMARY_CHOICE_TOOLTIP = (
    "The reconstruction ImProcess has selected when it opens; the others stay "
    "available in the Parameters dock picker."
)


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
        elif v is None or v == "null":
            # A null default means "not set" whatever the field's kind. Passed
            # through str() it became the text "None", which a numeric field
            # then wrote into the setup file and the loader could not read
            # (a new Timelapse section's illuminationSettleMs, 2026-11-18).
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


#: Header of the bucket holding ids the schema offers without a category.
_CHOICE_OTHER = "Other"
#: Header of the bucket holding ids only the file has.
_CHOICE_NOT_KNOWN = "Not known here (kept as written)"


def _grouped_choices(options: list, unknown: list, groups: dict) -> list:
    """``[(header, [option, ...]), ...]`` in reading order.

    Categories read alphabetically; ids the schema offers without a category
    come last under "Other", and ids only the file has under their own
    header, so nothing the file lists is hidden.
    """
    unknown_ids = {str(option) for option in unknown}
    buckets: dict[str, list] = {}
    for opt in options:
        if str(opt) in unknown_ids:
            name = _CHOICE_NOT_KNOWN
        else:
            name = groups.get(str(opt)) or _CHOICE_OTHER
        buckets.setdefault(name, []).append(opt)
    trailing = (_CHOICE_OTHER, _CHOICE_NOT_KNOWN)

    def reading_order(name: str):
        return (1, trailing.index(name)) if name in trailing else (0, name.lower())

    return [(name, buckets[name]) for name in sorted(buckets, key=reading_order)]


class _ChoiceList(QWidget):
    """A checkable list whose rows say what each choice is.

    One row per option: a check box carrying the display name, the id muted
    beside it, and under it a muted description. Used for the ImProcess
    reconstructors and processors, where the name alone does not tell a user
    what they are switching on. The stored value is the id; the rows are
    only how it reads. Choices may sit under non-checkable headers
    (``groups``); ids the schema does not offer (``unknown``) are listed under
    their own header and kept.

    A field with ``primary_choice`` also gets a "Starts with" combo above the
    rows: the ticked ids, by name, in the order ``order`` writes them, and
    the one chosen is the one :meth:`ordered_ids` puts first. ``order`` is the
    field's order rule, a function from the ticked ids to the list as it is
    written.
    """

    #: Emitted when the user clicks a check box or the combo -- never on construction.
    sig_edited = pyqtSignal()

    def __init__(self, field_def: dict, options: list, unknown: list,
                 selected: set, parent=None, order=None):
        super().__init__(parent)
        self._order = order or list
        self._chosen: str | None = None      # None: the first one listed
        self._primary: QComboBox | None = None
        labels = field_def.get("option_labels") or {}
        descriptions = field_def.get("option_descriptions") or {}
        groups = field_def.get("option_groups") or {}
        self._muted = "#AAAAAA" if is_dark_mode() else "#666666"
        self._boxes: dict[str, QCheckBox] = {}
        self._headers: list[str] = []
        unknown_ids = {str(option) for option in unknown}
        # the label's text starts where the box's own text does
        style = self.style()
        self._indent = (style.pixelMetric(QStyle.PM_IndicatorWidth)
                        + style.pixelMetric(QStyle.PM_CheckBoxLabelSpacing))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self._names = {
            str(opt): str(labels.get(str(opt)) or opt) if str(opt) not in unknown_ids
            else str(opt)
            for opt in options
        }
        if field_def.get("primary_choice"):
            lay.addWidget(self._primary_row())
        if groups or unknown_ids:
            sections = _grouped_choices(options, unknown, groups)
        else:
            sections = [(None, list(options))]
        for header, members in sections:
            if header is not None:
                title = QLabel(header)
                title.setTextFormat(Qt.PlainText)
                font = title.font()
                font.setBold(True)
                title.setFont(font)
                title.setContentsMargins(0, 6, 0, 0)
                self._headers.append(header)
                lay.addWidget(title)
            for opt in members:
                known = str(opt) not in unknown_ids
                lay.addWidget(self._row(
                    str(opt),
                    str(labels.get(str(opt)) or opt) if known else str(opt),
                    str(descriptions.get(str(opt), "")) if known else "",
                    str(opt) in selected,
                ))
        lay.addStretch()
        self._refresh_primary()

    def _primary_row(self) -> QWidget:
        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(8)
        label = QLabel(PRIMARY_CHOICE_LABEL)
        label.setTextFormat(Qt.PlainText)
        label.setToolTip(PRIMARY_CHOICE_TOOLTIP)
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        self._primary = QComboBox()
        self._primary.setToolTip(PRIMARY_CHOICE_TOOLTIP)
        self._primary.activated.connect(self._primary_activated)
        line.addWidget(label)
        line.addWidget(self._primary)
        line.addStretch()
        return row

    def _primary_activated(self, index: int) -> None:
        self._chosen = self._primary.itemData(index)
        self.sig_edited.emit()

    def _ticked(self) -> None:
        """A check box was clicked: the combo follows the ticks."""
        self._refresh_primary()
        self.sig_edited.emit()

    def _refresh_primary(self) -> None:
        """List the ticked ids in order; keep the choice while it stays ticked,
        else the first remaining is chosen."""
        if self._primary is None:
            return
        listed = self._order(self.checked_ids())
        if self._chosen not in listed:
            self._chosen = None
        combo = self._primary
        combo.blockSignals(True)
        combo.clear()
        for option_id in listed:
            combo.addItem(self._names.get(option_id, option_id), option_id)
        combo.setCurrentIndex(max(combo.findData(self._chosen), 0) if listed else -1)
        combo.setEnabled(bool(listed))
        combo.blockSignals(False)

    def _row(self, option_id: str, name: str, description: str, checked: bool) -> QWidget:
        row = QWidget()
        col = QVBoxLayout(row)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        line = QHBoxLayout()
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(8)
        box = QCheckBox(name)
        box.setChecked(checked)
        box.clicked.connect(lambda *_: self._ticked())
        self._boxes[option_id] = box
        line.addWidget(box)
        if name != option_id:
            ident = QLabel(option_id)
            ident.setTextFormat(Qt.PlainText)
            ident.setStyleSheet(f"color:{self._muted}; font-size:8pt;")
            line.addWidget(ident)
        line.addStretch()
        col.addLayout(line)
        if description:
            text = QLabel(description)
            text.setTextFormat(Qt.PlainText)
            text.setWordWrap(True)
            text.setContentsMargins(self._indent, 0, 0, 0)
            text.setStyleSheet(f"color:{self._muted}; font-size:8pt;")
            col.addWidget(text)
        return row

    def option_ids(self) -> list[str]:
        """Every id with a row, in the order the rows read."""
        return list(self._boxes)

    def header_texts(self) -> list[str]:
        """The non-checkable headers, in reading order."""
        return list(self._headers)

    def checkbox(self, option_id: str) -> QCheckBox:
        return self._boxes[str(option_id)]

    def checked_ids(self) -> list[str]:
        return [option_id for option_id, box in self._boxes.items() if box.isChecked()]

    def has_primary_choice(self) -> bool:
        return self._primary is not None

    def primary_combo(self) -> QComboBox | None:
        """The "Starts with" combo, or None when the field has none."""
        return self._primary

    def chosen_id(self) -> str | None:
        """The id the combo has chosen (the first listed unless the user chose
        another); None with nothing ticked or no combo."""
        if self._primary is None:
            return None
        return self._primary.currentData()

    def ordered_ids(self) -> list[str]:
        """The ticked ids as the value is written: the field's order rule, then
        the chosen one moved to the front."""
        listed = self._order(self.checked_ids())
        first = self.chosen_id()
        if first is None or first not in listed:
            return listed
        return [first] + [option_id for option_id in listed if option_id != first]


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
            if "option_descriptions" in self._def:
                # The registries were read: rows that say what each choice is.
                self._w = _ChoiceList(self._def, options, unknown, selected,
                                      order=self._ordered)
            else:
                # No registries (or no descriptions): a plain list, and so no
                # "Starts with" combo. The order rule in get_value() still
                # holds, so the first id of the file stays first.
                self._w = QListWidget()
                self._w.setMinimumHeight(92)
                self._w.setMaximumHeight(150)
                if groups:
                    self._fill_grouped(options, unknown, groups, selected)
                else:
                    for opt in options:
                        self._w.addItem(self._choice_item(opt, selected))
        elif tp == "ref":
            # Cross-reference to a device in the live config. opts is the list
            # of categories ("detectors", "positioners", ...) to draw names from;
            # "choices" adds literal names (a Micro-Manager device label).
            self._w = QComboBox()
            self._w.setEditable(True)  # allow names that don't exist yet
            self._fill_ref_items("" if value is None else str(value))
            if self._def.get("placeholder"):
                self._w.lineEdit().setPlaceholderText(self._def["placeholder"])
        elif tp == "json":
            # Free-form JSON value rendered as a single-line edit, parsed on
            # read. Used for dicts like scanDesignerParams, and for any
            # property no schema can type -- so it must round-trip *every*
            # value. A string shows quoted and None shows as null: showing them
            # bare made "5" come back as 5 and null come back as {}.
            # A required field with no value yet starts empty, not as the text
            # "null" the operator would have to delete before typing; an empty
            # box reads back as null, so nothing is lost.
            empty_required = value is None and self._def.get("req")
            self._w = QLineEdit("" if empty_required else json.dumps(value))
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

    def _ordered(self, checked) -> list:
        """The ticked ids in the order the value is written.

        The file's order is kept: for ImProcess the first reconstructor listed
        is the one active at startup, so ticking another one must not reorder
        the list. Newly ticked ids follow, in option order.
        """
        checked = list(checked)
        rank = {value: n for n, value in enumerate(self._option_order)}
        original = self._original if isinstance(self._original, list) else []
        kept = [value for value in original if value in checked]
        added = sorted((value for value in checked if value not in kept),
                       key=lambda value: rank[value])
        return kept + added

    def _choice_item(self, opt, selected: set) -> QListWidgetItem:
        item = QListWidgetItem(_multiselect_item_text(self._def, opt))
        # The id is what the file holds; the text is only how it reads.
        item.setData(Qt.UserRole, str(opt))
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked if str(opt) in selected else Qt.Unchecked)
        return item

    def _fill_grouped(self, options: list, unknown: list, groups: dict, selected: set) -> None:
        """Fill a multiselect under non-checkable category headers."""
        for name, members in _grouped_choices(options, unknown, groups):
            header = QListWidgetItem(name)
            header.setFlags(Qt.ItemIsEnabled)
            header.setData(Qt.UserRole, None)
            font = header.font()
            font.setBold(True)
            header.setFont(font)
            self._w.addItem(header)
            for opt in members:
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

    def _fill_ref_items(self, current: str):
        """(Re)fill a ``ref`` combo and show ``current``, keeping a dangling value selectable."""
        self._w.clear()
        self._w.addItem("")
        seen: set = set()
        names = [name for cat in self._def.get("opts", []) or []
                 for name in self._device_pool.get(cat, [])]
        names += list(self._def.get("choices") or [])
        for name in names:
            if name not in seen:
                self._w.addItem(name)
                seen.add(name)
        if current and self._w.findText(current) < 0:
            self._w.addItem(current)  # preserve dangling reference
        idx = self._w.findText(current)
        if idx >= 0:
            self._w.setCurrentIndex(idx)

    def set_choices(self, choices, placeholder: str = "", tip: str = ""):
        """Replace the literal choices of a ``ref`` field, keeping what is shown.

        Not an edit: the text is restored as it was, and the field stays
        untouched unless the operator touched it before.
        """
        if self._def.get("type") != "ref":
            return
        current = self._w.currentText()
        self._def = {**self._def, "choices": list(choices), "placeholder": placeholder}
        self._fill_ref_items(current)
        self._w.setEditText(current)
        self._w.lineEdit().setPlaceholderText(placeholder)
        if tip:
            self._w.setToolTip(tip)

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
        elif isinstance(w, _ChoiceList):
            w.sig_edited.connect(touch)
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
            if isinstance(self._w, _ChoiceList):
                checked = self._w.checked_ids()
            else:
                checked = [
                    self._w.item(i).data(Qt.UserRole)
                    for i in range(self._w.count())
                    if self._w.item(i).data(Qt.UserRole) is not None
                    and self._w.item(i).checkState() == Qt.Checked
                ]
            if isinstance(self._w, _ChoiceList) and self._w.has_primary_choice():
                # the order rule, then the chosen one in front
                return self._w.ordered_ids()
            return self._ordered(checked)
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


class _SectionFieldWidgets(dict):
    """A section form's widgets by ``(section, key)``; a plain key name is
    the direct field ``("field", key)``, as the flat section dialogs key it."""

    def _key(self, key):
        return ("field", key) if isinstance(key, str) else key

    def __getitem__(self, key):
        return super().__getitem__(self._key(key))

    def __contains__(self, key):
        return super().__contains__(self._key(key))

    def get(self, key, default=None):
        return super().get(self._key(key), default)


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

    def _schema_keys(self) -> set:
        """The keys the form edits; a custom property may not take one."""
        return set(self._field_widgets)

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
        if key in self._schema_keys() or key in self._custom_widgets:
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

    Section schemas may also expose structured child dictionaries through
    ``nested``.  Nested fields follow the same important editing contract as
    device ``managerProperties``: untouched absent containers stay absent and
    unknown/unexposed keys are preserved when an exposed child is edited.

    A section schema (``"section": true``, see :func:`_writes_as_written`)
    turns ``allow_custom_fields`` on and writes a schema field only if
    ``current`` already had its key, the operator touched its widget, or it is
    required -- so Apply neither expands a short block into every field nor
    loses a key the schema does not know. The per-category settings schemas
    are not sections and keep writing every field.
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
        # ("nested:<container>", key) for structured child dictionaries; a
        # plain name looks up the direct field.
        self._field_widgets: dict = _SectionFieldWidgets()
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
            scroll = self._build_custom_page(current, self._schema_keys())
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
        fields = {key: fw for (section, key), fw in self._field_widgets.items()
                  if section == "field"}
        out, missing = _collect_section_result(
            self._schema, self._received, fields, self._custom_widgets
        )

        # Nested containers are optional structured configuration. Merely
        # opening/applying a section must not create simplePointScan /
        # simpleMoNaLISA (or seed their defaults): only operator edits
        # materialize an absent child, and a container the file had is kept
        # with its unknown keys, edited in a copy.
        for nest_key, nest_fields in self._schema.get("nested", {}).items():
            original_container = self._current.get(nest_key)
            # Begin from the direct/raw JSON field if the operator edited it;
            # typed timing fields override only their own keys.
            source = out.get(nest_key, original_container)
            container = copy.deepcopy(source) if isinstance(source, dict) else None
            touched_any = False
            for f in nest_fields:
                fw = self._field_widgets.get((f"nested:{nest_key}", f["key"]))
                if fw is None or not fw.is_touched():
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
            elif nest_key in self._current and nest_key not in out:
                out[nest_key] = copy.deepcopy(original_container)
        if not self._allow_custom:
            # No Custom page shows them: a key the form does not show is kept
            # as it was (with a Custom page, a removed row is a removed key).
            known = self._schema_keys()
            for k, v in self._current.items():
                if k not in known and k not in out:
                    out[k] = copy.deepcopy(v)
        out = {**{k: out[k] for k in self._received if k in out},
               **{k: v for k, v in out.items() if k not in self._received}}

        if missing:
            self._err_lbl.setText(
                "Missing required: " + ", ".join(missing)
            )
            return
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

    def _show_custom_page(self):
        self._tabs.setCurrentWidget(self._custom_tab)

    @property
    def result_data(self) -> dict:
        return self._result

