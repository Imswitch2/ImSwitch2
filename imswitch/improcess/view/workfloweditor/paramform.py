"""A form for one plugin's parameters, built from its field spec.

Rendered on pyqtgraph's ``ParameterTree``, which the reconstructor widgets
already use, so a field maps onto a parameter type one for one: a number
with bounds, unit and step; a choice list; a check list; a file; text. A
value that differs from the field's default shows pyqtgraph's own
"back to default" button, which is the "only what you change" rule made
visible. Values are handed out per key as they change; the document
decides whether a value is worth writing (it drops the defaults).

Nullable numbers and free-form JSON are text: an empty text is ``None``,
anything else must parse, and until it does the form says so and hands
nothing on.
"""

from __future__ import annotations

import json
import numbers

from pyqtgraph.parametertree import Parameter, ParameterTree
from qtpy import QtCore, QtWidgets

from imswitch.improcess.model.param_spec import ParamField

_ADVANCED = "Advanced"
_OTHER = "Other parameters"

try:  # ``checklist`` arrived in pyqtgraph 0.12.4; older ones get a JSON list
    from pyqtgraph.parametertree.Parameter import PARAM_TYPES as _PARAM_TYPES
except Exception:  # pragma: no cover - very old pyqtgraph
    _PARAM_TYPES = {}
_HAS_CHECKLIST = "checklist" in _PARAM_TYPES


class ParamForm(QtWidgets.QWidget):
    """Edits the parameters of one step; see the module docstring."""

    #: A key took a new value (which may equal its default: the document decides).
    sigValueChanged = QtCore.Signal(str, object)
    #: An optional key (no default) was emptied: the step must not set it.
    sigValueCleared = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tree = ParameterTree(showHeader=False)
        self._root = None
        self._fields: dict[str, ParamField] = {}
        self._params: dict[str, Parameter] = {}
        self._keys: dict[int, str] = {}
        self._extra: set[str] = set()
        self._problems: dict[str, str] = {}
        self._updating = False

        self._empty = QtWidgets.QLabel("This step has no parameters.")
        self._empty.setEnabled(False)
        self._problem_label = QtWidgets.QLabel()
        self._problem_label.setWordWrap(True)
        self._problem_label.setStyleSheet("color: #d9822b;")
        self._problem_label.hide()
        self._reset = QtWidgets.QPushButton("Reset all to defaults")
        self._reset.clicked.connect(self.reset_all)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._tree, 1)
        layout.addWidget(self._empty)
        layout.addWidget(self._problem_label)
        layout.addWidget(self._reset, 0, QtCore.Qt.AlignRight)
        self.set_plugin(None, {})

    # -- building ---------------------------------------------------------------

    def set_plugin(self, entry, values) -> None:
        """Show ``entry``'s fields with ``values`` (missing keys at default)."""
        values = dict(values or {})
        self._fields = {}
        self._params = {}
        self._keys = {}
        self._extra = set()
        self._problems = {}
        self._updating = True
        try:
            if self._root is not None:
                try:
                    self._root.sigTreeStateChanged.disconnect(self._on_tree_changed)
                except (TypeError, RuntimeError):
                    pass
            children = self._children_for(entry, values)
            self._root = Parameter.create(name="params", type="group", children=children)
            self._tree.setParameters(self._root, showTop=False)
            self._root.sigTreeStateChanged.connect(self._on_tree_changed)
        finally:
            self._updating = False
        has_fields = bool(self._fields)
        self._tree.setVisible(has_fields)
        self._empty.setVisible(not has_fields)
        self._reset.setVisible(has_fields)
        self._show_problems()

    def _children_for(self, entry, values) -> list:
        if entry is None:
            return []
        fields = list(entry.fields)
        for key in entry.extra_keys:
            fields.append(ParamField(
                key, "json", None, nullable=True, group=_OTHER,
                help="Optional: no widget default. Leave empty to not set it; otherwise JSON.",
            ))
            self._extra.add(key)
        groups: dict[str, list] = {}
        top: list = []
        for field in fields:
            self._fields[field.key] = field
            param = self._param_for(field, values.get(field.key, field.default))
            self._params[field.key] = param
            self._keys[id(param)] = field.key
            group = field.group or (_ADVANCED if field.advanced else "")
            if group:
                groups.setdefault(group, []).append(param)
            else:
                top.append(param)
        children = list(top)
        for name, members in groups.items():
            children.append(Parameter.create(
                name=name, type="group", title=name, children=members,
                expanded=name not in (_ADVANCED, _OTHER),
            ))
        return children

    def _param_for(self, field: ParamField, value) -> Parameter:
        kind = self._widget_kind(field)
        opts = {
            "name": field.key, "title": field.title, "type": kind,
            "value": self._to_widget(field, value), "default": self._to_widget(field, field.default),
        }
        if field.help:
            opts["tip"] = field.help
        if kind in ("int", "float"):
            if field.min is not None or field.max is not None:
                opts["limits"] = (field.min, field.max)
            if field.step is not None:
                opts["step"] = field.step
            if field.decimals is not None and kind == "float":
                opts["decimals"] = field.decimals
            if field.suffix:
                opts["suffix"] = field.suffix
        elif kind == "list":
            limits = list(field.options)
            if field.nullable and not any(option is None for option in limits):
                limits.insert(0, None)
            if value is not None and not any(_same(value, option) for option in limits):
                limits.append(value)  # a value from the file that the plugin no longer lists
            opts["limits"] = {_option_label(option): option for option in limits}
        elif kind == "checklist":
            opts["limits"] = list(field.options)
        return Parameter.create(**opts)

    @staticmethod
    def _widget_kind(field: ParamField) -> str:
        if field.type == "bool":
            return "bool"
        if field.type in ("int", "float"):
            return "str" if field.nullable else field.type
        if field.type == "select":
            return "list"
        if field.type == "multiselect":
            return "checklist" if _HAS_CHECKLIST else "str"
        if field.type == "path":
            return "file"
        return "str"

    def _to_widget(self, field: ParamField, value):
        kind = self._widget_kind(field)
        if kind == "bool":
            return bool(value) if value is not None else False
        if kind in ("int", "float"):
            return field.default if value is None else value
        if kind in ("list", "checklist"):
            return list(value or []) if kind == "checklist" else value
        if kind == "file":
            return "" if value is None else str(value)
        # text-backed: text, json, nullable numbers, multiselect fallback
        if field.type in ("json", "multiselect"):
            return "" if value is None else json.dumps(value)
        return "" if value is None else str(value)

    def _from_widget(self, field: ParamField, raw):
        """``(value, problem)`` for what the widget holds."""
        kind = self._widget_kind(field)
        if kind == "bool":
            return bool(raw), None
        if kind in ("int", "float", "list", "checklist"):
            return (list(raw) if kind == "checklist" else raw), None
        if kind == "file":
            text = str(raw or "")
            return (None if field.nullable and not text else text), None
        text = str(raw if raw is not None else "").strip()
        if field.type in ("json", "multiselect"):
            if not text:
                return None, None
            try:
                value = json.loads(text)
            except ValueError:
                return None, "not valid JSON"
            if field.type == "multiselect" and not isinstance(value, list):
                return None, "must be a JSON list"
            return value, None
        if field.type in ("int", "float"):
            if not text:
                return None, None
            try:
                return (int(text) if field.type == "int" else float(text)), None
            except ValueError:
                return None, "must be a whole number" if field.type == "int" else "must be a number"
        return (None if field.nullable and not text else str(raw or "")), None

    # -- values -----------------------------------------------------------------

    def keys(self) -> list[str]:
        return list(self._params)

    def values(self) -> dict:
        """Every field's current value (unparseable text counts as missing)."""
        out = {}
        for key, param in self._params.items():
            value, problem = self._from_widget(self._fields[key], param.value())
            if problem is None:
                out[key] = value
        return out

    def set_values(self, values) -> None:
        """Update the widgets without reporting the changes."""
        values = dict(values or {})
        self._updating = True
        try:
            for key, param in self._params.items():
                field = self._fields[key]
                param.setValue(self._to_widget(field, values.get(key, field.default)))
        finally:
            self._updating = False

    def set_value(self, key: str, value) -> None:
        """Set one value as a user would, so the change is reported."""
        param = self._params[key]
        param.setValue(self._to_widget(self._fields[key], value))

    def reset_all(self) -> None:
        for key, field in self._fields.items():
            self.set_value(key, field.default)

    def problems(self) -> dict[str, str]:
        return dict(self._problems)

    # -- change handling ----------------------------------------------------------

    def _on_tree_changed(self, _root, changes) -> None:
        if self._updating:
            return
        for param, change, _data in changes:
            if change != "value":
                continue
            key = self._keys.get(id(param))
            if key is None:
                continue
            field = self._fields[key]
            value, problem = self._from_widget(field, param.value())
            if problem:
                self._problems[key] = problem
                self._show_problems()
                continue
            self._problems.pop(key, None)
            self._show_problems()
            if key in self._extra and value is None:
                self.sigValueCleared.emit(key)
            else:
                self.sigValueChanged.emit(key, value)

    def _show_problems(self) -> None:
        if not self._problems:
            self._problem_label.hide()
            return
        self._problem_label.setText("\n".join(f"{key}: {text}" for key, text in self._problems.items()))
        self._problem_label.show()


def _option_label(option) -> str:
    if option is None:
        return "(unset)"
    if isinstance(option, bool):
        return "true" if option else "false"
    return str(option)


def _same(a, b) -> bool:
    """Equal and of one kind: ``1`` is not ``True``, ``"1"`` is not ``1``."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, numbers.Real) and isinstance(b, numbers.Real):
        return a == b
    try:
        return type(a) is type(b) and bool(a == b)
    except Exception:
        return False


__all__ = ["ParamForm"]


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
