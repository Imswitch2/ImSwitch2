"""The values a workflow file's controls take for one run.

**Run workflow…**, **Run workflow on selected results…** and **Run workflow
over files…** open this before running a file that has controls (workflow
schema 2): one field per control, built from its combined field, with its
label and help, filled with the file's values. A control with no active
target in this run (its steps bypassed, or the reconstruction the selected
result replaces) is listed disabled with the reason and sends no value. The
values hold for this run only; the file is not changed.

See ``docs/design/plans/improcess-workflow-schema-2.md`` (sections 4.7, 9).
"""

from __future__ import annotations

from types import SimpleNamespace

from qtpy import QtCore, QtWidgets

from imswitch.improcess.model.param_spec import ParamField
from imswitch.improcess.view.workfloweditor.paramform import ParamForm

#: How a target's inactivity reads in the dialog (``prepare`` reason -> words).
_REASONS = {
    "bypassed": "{step} is bypassed",
    "replaced by a bound result": "{step} is replaced by the selected result",
    "only feeds a replaced step": "{step} only feeds the step the selected result replaces",
    "outside this run": "{step} is outside this run",
}


def reason_words(target: dict) -> str:
    step = str(target.get("target", "")).split(".", 1)[0]
    template = _REASONS.get(str(target.get("reason", "")), "{step}: " + str(target.get("reason", "")))
    return template.format(step=step)


def field_of(entry: dict) -> ParamField | None:
    """A form field for one described control (``describe_controls``)."""
    kind = entry.get("type")
    if not kind:
        return None
    default = entry.get("reset", entry.get("value"))
    try:
        return ParamField(
            entry["name"], kind, default, label=entry.get("label") or entry["name"], help=entry.get("help") or "",
            options=tuple(entry.get("options") or ()), min=entry.get("min"), max=entry.get("max"),
            step=entry.get("step"), decimals=entry.get("decimals"), suffix=entry.get("unit") or "",
            nullable=bool(entry.get("nullable")),
        )
    except ValueError:
        return None


class WorkflowControlsDialog(QtWidgets.QDialog):
    """See the module docstring. ``validate(values) -> str | None`` checks the
    values before the dialog closes (the reason is shown and it stays open)."""

    def __init__(self, workflow_name: str, described: list[dict], *, validate=None, runs: int = 1, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Run {workflow_name}")
        self.setMinimumWidth(460)
        self._validate = validate
        self._active: dict[str, dict] = {}
        layout = QtWidgets.QVBoxLayout(self)
        what = "each run" if runs > 1 else "this run"
        intro = QtWidgets.QLabel(f"<b>{workflow_name}</b> lets you set these values for {what}. "
                                 "The file is not changed.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        fields = []
        values = {}
        self.inactive_labels: dict[str, QtWidgets.QLabel] = {}
        for entry in described:
            name = entry["name"]
            if not entry.get("active", True):
                reasons = [reason_words(t) for t in entry.get("targets", []) if not t.get("active", True)]
                label = QtWidgets.QLabel(f"{entry.get('label') or name} ({name}): no effect in this run — "
                                         + "; ".join(dict.fromkeys(reasons)))
                label.setWordWrap(True)
                label.setEnabled(False)
                self.inactive_labels[name] = label
                continue
            field = field_of(entry)
            if field is None:
                label = QtWidgets.QLabel(f"{name}: cannot be set here (its parameters do not share one field)")
                label.setEnabled(False)
                self.inactive_labels[name] = label
                continue
            self._active[name] = entry
            fields.append(field)
            if "value" in entry:
                values[name] = entry["value"]
        self.form = ParamForm()
        self.form.set_plugin(SimpleNamespace(fields=fields, extra_keys=()), values)
        self.form.set_reset_visible(False)
        self.form.setMinimumHeight(min(34 + 26 * len(fields), 320))
        if fields:
            layout.addWidget(self.form, 1)
        else:
            self.form.hide()
        for label in self.inactive_labels.values():
            layout.addWidget(label)
        self.problem = QtWidgets.QLabel()
        self.problem.setWordWrap(True)
        self.problem.setStyleSheet("color: #d9822b;")
        self.problem.hide()
        layout.addWidget(self.problem)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        self.run_button = buttons.button(QtWidgets.QDialogButtonBox.Ok)
        self.run_button.setText("Run")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> dict:
        """The value of every control that is active in this run."""
        found = self.form.values()
        return {name: found[name] for name in self._active if name in found}

    def accept(self) -> None:
        problems = self.form.problems()
        if problems:
            self._show(next(iter(problems.values())))
            return
        if self._validate is not None:
            problem = self._validate(self.values())
            if problem:
                self._show(problem)
                return
        super().accept()

    def _show(self, text: str) -> None:
        self.problem.setText(str(text))
        self.problem.show()


__all__ = ["WorkflowControlsDialog", "field_of", "reason_words"]


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
