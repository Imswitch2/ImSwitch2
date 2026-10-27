"""Read a plugin's parameter fields off its own widget.

A plugin's widget knows what its ``default_params()`` do not: which key a
combo box feeds and with which choices, the bounds, step, unit and tooltip
of a spin box, the label next to each control. This module builds the
widget (Qt is needed, on the GUI thread or offscreen), perturbs every
input control and watches ``get_values()`` to learn which key each control
feeds, then describes the key as a :class:`ParamField`.

Two uses. ``python tools/draft_improcess_param_specs.py`` turns the result
into a draft ``param_spec()`` for a plugin author to review; and a test
holds every built-in's declared spec to what its widget says, so the two
cannot drift apart silently. It is deliberately not the runtime source of
a spec: the registry stays headless, and a control the probe cannot map
to exactly one key (a check box that turns a group of spin boxes into
``None``) needs a human to say what the key is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from imswitch.improcess.model.param_spec import ParamField, _same

_UNBOUNDED = 1e8  # a spin box range this wide says "no bound", not a bound
_PROBE_TEXT = "probe-8f3a"


@dataclass
class ProbedField:
    """What one control told us about one key."""

    key: str
    type: str
    label: str = ""
    help: str = ""
    options: tuple = ()
    min: float | None = None
    max: float | None = None
    step: float | None = None
    decimals: int | None = None
    suffix: str = ""
    group: str = ""
    control: str = ""  # the Qt class or pyqtgraph type the key was read from

    def as_field(self, default) -> ParamField:
        return ParamField(
            self.key, self.type, default, label=self.label, help=self.help, options=self.options,
            min=self.min, max=self.max, step=self.step, decimals=self.decimals, suffix=self.suffix,
            group=self.group, nullable=default is None,
        )


@dataclass
class ProbeResult:
    plugin_id: str
    fields: dict[str, ProbedField] = field(default_factory=dict)
    #: keys several controls feed, or that a control feeds together with others
    composite: dict[str, list[str]] = field(default_factory=dict)
    #: controls that fed no key (labels, buttons, disabled parts)
    unmapped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def declared_keys(self) -> set[str]:
        return set(self.fields)


def probe_plugin(plugin, parent=None) -> ProbeResult:
    """Build ``plugin``'s widget and read its fields; never raises."""
    from qtpy import QtWidgets

    result = ProbeResult(plugin_id=str(getattr(plugin, "id", type(plugin).__name__)))
    if parent is None:
        parent = QtWidgets.QWidget()
    try:
        widget = plugin.make_param_widget(parent)
    except Exception as exc:  # noqa: BLE001
        result.notes.append(f"make_param_widget failed: {exc}")
        return result
    if widget is None:
        result.notes.append("no widget")
        return result
    getter = getattr(widget, "get_values", None)
    if not callable(getter):
        result.notes.append("widget has no get_values()")
        return result
    try:
        baseline = dict(getter())
    except Exception as exc:  # noqa: BLE001
        result.notes.append(f"get_values() failed: {exc}")
        return result

    observations: dict[str, list[tuple[str, ProbedField, list]]] = {}  # key -> [(control id, probed, values)]

    def record(control_id: str, probed: ProbedField, seen: dict[str, list]) -> None:
        if not seen:
            result.unmapped.append(control_id)
            return
        for key, values in seen.items():
            observations.setdefault(key, []).append((control_id, probed, values))

    tree = getattr(widget, "p", None)
    if tree is not None and hasattr(tree, "children"):
        _probe_parameter_tree(tree, getter, baseline, record)
    _probe_qt_controls(widget, getter, baseline, record)

    # A control that moves several keys at once (a preset list, a "full
    # frame" box that blanks four spin boxes) is a macro, not a field: its
    # observations say nothing about any one key and are set aside.
    macros = {control for control in _all_controls(observations) if _feeds_several(control, observations)}
    for key, entries in observations.items():
        entries = [entry for entry in entries if entry[0] not in macros]
        controls = [control for control, _probed, _values in entries]
        if len(entries) != 1:
            result.composite[key] = controls or sorted(
                c for c, _p, _v in observations[key])
            continue
        _control, probed, values = entries[0]
        probed.key = key
        if probed.type == "select":
            probed.options = tuple(_unique(values))
        result.fields[key] = probed
    return result


def _all_controls(observations) -> set[str]:
    return {control for entries in observations.values() for control, _p, _v in entries}


def _feeds_several(control_id: str, observations) -> bool:
    return sum(1 for entries in observations.values() if any(c == control_id for c, _p, _v in entries)) > 1


def _unique(values) -> list:
    out: list = []
    for value in values:
        if not any(_same(value, seen) for seen in out):
            out.append(value)
    return out


def _changed(getter, baseline) -> dict[str, Any] | None:
    try:
        now = dict(getter())
    except Exception:
        return None
    return {key: now[key] for key in now if key not in baseline or not _same(now[key], baseline[key])}


# -- plain Qt controls ---------------------------------------------------------------

def _probe_qt_controls(widget, getter, baseline, record) -> None:
    from qtpy import QtWidgets

    labels = _form_labels(widget)
    for control in widget.findChildren(QtWidgets.QWidget):
        if _internal(control) or _inside_parameter_tree(control):
            continue
        control_id = f"{type(control).__name__}@{id(control):x}"
        label = labels.get(control, "")
        help_text = control.toolTip() or ""
        seen: dict[str, list] = {}

        def note(changes):
            if changes:
                for key, value in changes.items():
                    seen.setdefault(key, []).append(value)

        if isinstance(control, QtWidgets.QComboBox):
            original = control.currentIndex()
            for index in range(control.count()):
                control.setCurrentIndex(index)
                changes = _changed(getter, baseline)
                if changes is None:
                    continue
                for key in changes:
                    seen.setdefault(key, [])
                for key, value in changes.items():
                    seen[key].append(value)
                if index == original:
                    pass
            control.setCurrentIndex(original)
            # the original index feeds the baseline value: include it in the options
            for key, values in seen.items():
                values.insert(0, baseline.get(key))
            probed = ProbedField("", "select", label=label, help=help_text, control="QComboBox")
            record(control_id, probed, seen)
        elif isinstance(control, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
            original = control.value()
            for target in (control.minimum(), control.maximum()):
                control.setValue(target)
                note(_changed(getter, baseline))
            control.setValue(original)
            is_double = isinstance(control, QtWidgets.QDoubleSpinBox)
            kind = "float" if is_double or any(
                isinstance(v, float) for values in seen.values() for v in values) else "int"
            probed = ProbedField(
                "", kind, label=label, help=help_text,
                min=_bound(control.minimum()), max=_bound(control.maximum()),
                step=_step(control.singleStep(), kind), decimals=control.decimals() if is_double else None,
                suffix=control.suffix().strip(), control=type(control).__name__,
            )
            record(control_id, probed, seen)
        elif isinstance(control, QtWidgets.QCheckBox):
            original = control.isChecked()
            control.setChecked(not original)
            changes = _changed(getter, baseline)
            note(changes)
            control.setChecked(original)
            kind = "bool"
            if changes and not all(isinstance(v, bool) for v in changes.values()):
                kind = "json"  # a check box standing in for "unset": not a bool key
            probed = ProbedField("", kind, label=label or control.text(), help=help_text, control="QCheckBox")
            record(control_id, probed, seen)
        elif isinstance(control, QtWidgets.QLineEdit):
            original = control.text()
            control.setText(_PROBE_TEXT)
            changes = _changed(getter, baseline)
            control.setText(original)
            if changes is None:
                record(control_id, ProbedField("", "text"), {})
                continue
            note(changes)
            kind = "path" if _has_browse_button(control) else "text"
            if changes and not all(isinstance(v, str) for v in changes.values()):
                kind = "json"  # parsed text (a list, a number): the author must type it
            probed = ProbedField("", kind, label=label, help=help_text or control.placeholderText(),
                                 control="QLineEdit")
            record(control_id, probed, seen)


def _inside_parameter_tree(control) -> bool:
    """A control pyqtgraph made for a parameter: probed through the tree instead."""
    try:
        from pyqtgraph.parametertree import ParameterTree
    except Exception:  # pragma: no cover - pyqtgraph is a dependency
        return False
    parent = control.parent()
    while parent is not None:
        if isinstance(parent, ParameterTree):
            return True
        parent = parent.parent()
    return False


def _internal(control) -> bool:
    """A control that belongs to another control (a spin box's line edit)."""
    from qtpy import QtWidgets

    parent = control.parent()
    if isinstance(control, QtWidgets.QLineEdit) and isinstance(
        parent, (QtWidgets.QAbstractSpinBox, QtWidgets.QComboBox)
    ):
        return True
    return isinstance(control, (QtWidgets.QAbstractSpinBox, QtWidgets.QComboBox)) and isinstance(
        parent, QtWidgets.QAbstractSpinBox
    )


def _form_labels(widget) -> dict:
    """The label text next to each control laid out in a form."""
    from qtpy import QtWidgets

    labels: dict = {}
    for form in widget.findChildren(QtWidgets.QFormLayout):
        for row in range(form.rowCount()):
            label_item = form.itemAt(row, QtWidgets.QFormLayout.LabelRole)
            field_item = form.itemAt(row, QtWidgets.QFormLayout.FieldRole)
            if label_item is None or field_item is None or label_item.widget() is None:
                continue
            text = label_item.widget().text().rstrip(":").strip()
            for control in _controls_in(field_item):
                labels.setdefault(control, text)
    return labels


def _controls_in(item) -> list:
    from qtpy import QtWidgets

    out = []
    widget = item.widget()
    if widget is not None:
        out.append(widget)
        out.extend(widget.findChildren(QtWidgets.QWidget))
        return out
    layout = item.layout()
    if layout is not None:
        for index in range(layout.count()):
            out.extend(_controls_in(layout.itemAt(index)))
    return out


def _has_browse_button(control) -> bool:
    from qtpy import QtWidgets

    parent = control.parent()
    if parent is None:
        return False
    return any(
        "…" in button.text() or "browse" in button.text().lower()
        for button in parent.findChildren(QtWidgets.QAbstractButton)
    )


def _bound(value) -> float | None:
    return None if abs(float(value)) >= _UNBOUNDED else float(value) if not float(value).is_integer() else int(value)


def _step(value, kind) -> float | None:
    if kind == "int":
        return None if int(value) == 1 else int(value)
    return None if float(value) == 1.0 else float(value)


# -- pyqtgraph parameter trees ---------------------------------------------------------

def _probe_parameter_tree(root, getter, baseline, record) -> None:
    for param, group in _leaves(root, ""):
        opts = dict(getattr(param, "opts", {}) or {})
        kind = str(opts.get("type", ""))
        control_id = f"Parameter[{kind}]@{param.name()}"
        label = str(opts.get("title") or param.name())
        help_text = str(opts.get("tip") or "")
        seen: dict[str, list] = {}

        def note(changes):
            if changes:
                for key, value in changes.items():
                    seen.setdefault(key, []).append(value)

        try:
            original = param.value()
        except Exception:
            continue
        try:
            if kind == "list":
                limits = opts.get("limits")
                if limits is None:
                    limits = opts.get("values") or ()
                candidates = list(limits.values()) if isinstance(limits, dict) else list(limits)
                for candidate in candidates:
                    param.setValue(candidate)
                    changes = _changed(getter, baseline)
                    if changes is None:
                        continue
                    for key in changes:
                        seen.setdefault(key, [])
                    for key, value in changes.items():
                        seen[key].append(value)
                for key, values in seen.items():
                    values.insert(0, baseline.get(key))
                probed = ProbedField("", "select", label=label, help=help_text, group=group, control=control_id)
            elif kind in ("int", "float"):
                limits = opts.get("limits") or (None, None)
                low, high = (limits + (None, None))[:2] if isinstance(limits, tuple) else (limits[0], limits[1])
                targets = [t for t in (low, high) if t is not None] or [original + 1]
                for target in targets:
                    param.setValue(target)
                    note(_changed(getter, baseline))
                probed = ProbedField(
                    "", kind, label=label, help=help_text, group=group,
                    min=None if low is None else _bound(low), max=None if high is None else _bound(high),
                    suffix=str(opts.get("suffix") or "").strip(), control=control_id,
                )
            elif kind == "bool":
                param.setValue(not bool(original))
                note(_changed(getter, baseline))
                probed = ProbedField("", "bool", label=label, help=help_text, group=group, control=control_id)
            elif kind in ("str", "text", "file"):
                param.setValue(_PROBE_TEXT)
                changes = _changed(getter, baseline)
                note(changes)
                probed = ProbedField("", "path" if kind == "file" else "text", label=label, help=help_text,
                                     group=group, control=control_id)
            else:
                continue
        finally:
            try:
                param.setValue(original)
            except Exception:
                pass
        record(control_id, probed, seen)


def _leaves(param, group: str):
    for child in param.children():
        kind = str((getattr(child, "opts", {}) or {}).get("type", ""))
        if kind == "group" or child.hasChildren() and kind not in ("list",):
            yield from _leaves(child, child.name() if not group else f"{group} / {child.name()}")
        else:
            yield child, group
            if child.hasChildren():  # a list with dependent children
                yield from _leaves(child, group)


# -- drafting -----------------------------------------------------------------------------

def draft_spec_source(plugin_cls, probed: ProbeResult) -> str:
    """Python source for a ``param_spec()`` matching ``probed`` and the defaults."""
    defaults = dict(plugin_cls.default_params())
    lines = ["    @classmethod", "    def param_spec(cls) -> tuple:", "        return ("]
    for key, default in defaults.items():
        found = probed.fields.get(key)
        if found is None:
            from imswitch.improcess.model.param_spec import field_from_default

            field_obj = field_from_default(key, default)
            why = "composite control" if key in probed.composite else "not read from the widget"
            lines.append(f"            {_field_source(field_obj)},  # TODO {why}")
        else:
            lines.append(f"            {_field_source(found.as_field(default))},")
    lines.append("        )")
    return "\n".join(lines)


def _field_source(field_obj: ParamField) -> str:
    args = [repr(field_obj.key), repr(field_obj.type), repr(field_obj.default)]
    for name in ("label", "help", "options", "min", "max", "step", "decimals", "suffix", "group",
                 "nullable", "advanced"):
        value = getattr(field_obj, name)
        if value is None or value is False or value == "" or value == ():
            continue  # unset; a bound of 0 is a bound and stays
        args.append(f"{name}={value!r}")
    return f"ParamField({', '.join(args)})"


__all__ = ["ProbeResult", "ProbedField", "draft_spec_source", "probe_plugin"]


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
