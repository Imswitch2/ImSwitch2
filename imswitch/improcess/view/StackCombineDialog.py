"""Stack/Combine dialog for merging selected results into one output."""

from __future__ import annotations

from qtpy import QtWidgets

from imswitch.improcess.processors._axis_split import (
    axis_labels_for_result,
    shape_for_result,
)
from imswitch.improcess.processors.combine import (
    DEFAULT_PAD_MAX_RATIO,
    STACK_AXIS_LABELS,
    CombineMismatch,
    align_results,
    default_stack_axis_label,
    preferred_candidate,
)
from imswitch.improcess.view.ResultInputList import (  # re-exported
    ResultInputListWidget,
    expand_input_choices,
)

_MODE_STACK = "stack"
_MODE_CONCATENATE = "concatenate"
_MODE_AUTO = "auto"

#: Differences no option of this dialog resolves: the user has to fix the data.
_FUNDAMENTAL = ("labels", "scale", "unit", "unreadable")

#: Padding that adds this share of the result's pixels gets a warning.
_PAD_WARN_FRACTION = 0.25


class StackCombineDialog(QtWidgets.QDialog):
    """Choose how to combine the selected reconstruction-list results.

    *Automatic* (the default) stacks equal-rank inputs on a new axis and
    appends a lower-rank input to the axis it lacks (a plane to a stack);
    *Stack* adds a new leading axis; *Concatenate* appends along an existing
    shared axis. Inputs that differ in size can be padded to a common one.
    Whatever the current settings do is said in plain words, with the shape
    of the result; inputs that cannot be combined as set disable OK and show
    the reason together with the options that would make them work (pad,
    append) or the tool that fixes what no option can (Scale/Resize, Swap /
    Relabel Axes). Multi-layer results are expanded into their components so
    individual layers can be combined.
    """

    def __init__(self, results, parent=None, *, preselected=None):
        super().__init__(parent)
        self.setWindowTitle("Stack/Combine")
        self.setMinimumWidth(520)
        self._alignment = None
        self._appendChoice = None

        # -- input order + selection ------------------------------------
        self.inputWidget = ResultInputListWidget(
            list(results), self, preselected=preselected
        )

        # -- mode + axis --------------------------------------------------
        # Stack and concatenate keep their positions: callers and scripts
        # address them by index. Automatic is the default.
        self.modeCombo = QtWidgets.QComboBox()
        self.modeCombo.addItem("Stack along a new axis", _MODE_STACK)
        self.modeCombo.addItem("Concatenate along an existing axis", _MODE_CONCATENATE)
        self.modeCombo.addItem("Automatic (recommended)", _MODE_AUTO)
        self.modeCombo.setCurrentIndex(self.modeCombo.findData(_MODE_AUTO))
        self.modeCombo.setToolTip(
            "Automatic stacks equal-shaped inputs on a new axis and appends a "
            "lower-rank input (a plane) to the axis it lacks (the stack's Z)"
        )

        self.axisLabelCombo = QtWidgets.QComboBox()
        self.axisLabelCombo.setEditable(True)
        self.axisLabelCombo.addItems(list(STACK_AXIS_LABELS))
        self.axisLabelCombo.setToolTip("Label for the new leading axis")
        self.axisLabelCombo.setCurrentText(self._default_axis_label())

        self.joinAxisCombo = QtWidgets.QComboBox()
        self._rebuild_join_axes()

        # Asked, not guessed: shown when the labels do not decide which axis
        # a lower-rank input is appended along, pre-filled with the likeliest.
        self.appendAxisCombo = QtWidgets.QComboBox()
        self.appendAxisCombo.setToolTip("The axis the lower-rank inputs are appended along")

        self.nameEdit = QtWidgets.QLineEdit()

        # -- padding ----------------------------------------------------------
        self.padCheck = QtWidgets.QCheckBox("Pad to a common size when sizes differ")
        self.padCheck.setToolTip(
            "Pads the Y/X plane of smaller inputs to the largest size. Pixel "
            "sizes must still match: padding is not resampling."
        )
        self.padAnchorCombo = QtWidgets.QComboBox()
        self.padAnchorCombo.addItem("Centre", "centre")
        self.padAnchorCombo.addItem("Origin (keep the top-left corner)", "origin")
        self.padValueCombo = QtWidgets.QComboBox()
        self.padValueCombo.addItem("Zeros", "0")
        self.padValueCombo.addItem("NaN (float data only)", "nan")
        self.padValueCombo.addItem("Repeat the edge pixels", "edge")
        self.padLargeCheck = QtWidgets.QCheckBox(
            f"Allow padding beyond {DEFAULT_PAD_MAX_RATIO:g}x the input's size"
        )
        self.padLargeCheck.setToolTip(
            "Off by default so that a small image is not buried in a much larger one by mistake"
        )

        form = QtWidgets.QFormLayout()
        form.addRow("Mode:", self.modeCombo)
        self._axisLabelRowLabel = QtWidgets.QLabel("New axis:")
        form.addRow(self._axisLabelRowLabel, self.axisLabelCombo)
        self._joinAxisRowLabel = QtWidgets.QLabel("Join axis:")
        form.addRow(self._joinAxisRowLabel, self.joinAxisCombo)
        self._appendAxisRowLabel = QtWidgets.QLabel("Append along:")
        form.addRow(self._appendAxisRowLabel, self.appendAxisCombo)
        form.addRow("", self.padCheck)
        self._padAnchorRowLabel = QtWidgets.QLabel("Pad anchor:")
        form.addRow(self._padAnchorRowLabel, self.padAnchorCombo)
        self._padValueRowLabel = QtWidgets.QLabel("Pad value:")
        form.addRow(self._padValueRowLabel, self.padValueCombo)
        form.addRow("", self.padLargeCheck)
        form.addRow("Output name:", self.nameEdit)

        # -- status + offers + buttons ------------------------------------------
        # What the current settings will do (or why they cannot), in words.
        self.planLabel = QtWidgets.QLabel()
        self.planLabel.setWordWrap(True)
        self.statusLabel = QtWidgets.QLabel()
        self.statusLabel.setWordWrap(True)
        self.warningLabel = QtWidgets.QLabel()
        self.warningLabel.setWordWrap(True)

        # One-click escape hatch for the most common dead end: stacking along
        # a label the inputs already carry (e.g. Z on results with a singleton
        # Z axis), where appending along that existing axis is what the user
        # almost certainly wants.
        self.switchToConcatenateButton = QtWidgets.QPushButton()
        self.switchToConcatenateButton.setVisible(False)
        self.switchToConcatenateButton.clicked.connect(
            self._switch_to_concatenate_along_existing
        )

        # The ways out of a refusal that an option of this dialog provides.
        self.offerPadButton = QtWidgets.QPushButton()
        self.offerAppendButton = QtWidgets.QPushButton()
        self.offerBothButton = QtWidgets.QPushButton()
        for button in (self.offerPadButton, self.offerAppendButton, self.offerBothButton):
            button.setVisible(False)
        self.offerPadButton.clicked.connect(lambda: self._take_offer(pad=True, auto=False))
        self.offerAppendButton.clicked.connect(lambda: self._take_offer(pad=False, auto=True))
        self.offerBothButton.clicked.connect(lambda: self._take_offer(pad=True, auto=True))

        self.buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel("Inputs (checked, in output order):"))
        layout.addWidget(self.inputWidget)
        layout.addLayout(form)
        layout.addWidget(self.planLabel)
        layout.addWidget(self.warningLabel)
        layout.addWidget(self.statusLabel)
        layout.addWidget(self.offerPadButton)
        layout.addWidget(self.offerAppendButton)
        layout.addWidget(self.offerBothButton)
        layout.addWidget(self.switchToConcatenateButton)
        layout.addWidget(self.buttons)

        self.modeCombo.currentIndexChanged.connect(self._refresh)
        self.joinAxisCombo.currentIndexChanged.connect(self._refresh)
        self.appendAxisCombo.currentIndexChanged.connect(self._append_axis_chosen)
        self.axisLabelCombo.editTextChanged.connect(lambda _text: self._refresh())
        self.padCheck.toggled.connect(self._refresh)
        self.padAnchorCombo.currentIndexChanged.connect(self._refresh)
        self.padValueCombo.currentIndexChanged.connect(self._refresh)
        self.padLargeCheck.toggled.connect(self._refresh)
        self.inputWidget.sigInputsChanged.connect(self._refresh)
        self._refresh()

    # -- params -----------------------------------------------------------

    def checked_results(self) -> list:
        """Return the checked input results in list order."""
        return self.inputWidget.checked_results()

    def selected_params(self) -> dict:
        """The processor parameters the dialog stands for.

        ``auto`` that comes down to a plain stack is returned as ``stack``,
        so the step reads as what it does; one that appends carries the axis
        it settled on (``append_axis``) so it is reproducible.
        """
        mode = self.modeCombo.currentData()
        alignment = self._alignment
        params = {
            "name": self.nameEdit.text().strip() or self._default_name(),
            "results": self.checked_results(),
        }
        if mode == _MODE_CONCATENATE:
            params["mode"] = mode
            params["join_axis"] = int(self.joinAxisCombo.currentData() or 0)
        elif mode == _MODE_AUTO and alignment is not None and alignment.mode == _MODE_CONCATENATE:
            params["mode"] = _MODE_AUTO
            labels = list(alignment.labels)
            params["append_axis"] = (
                alignment.join_label
                if labels.count(alignment.join_label) == 1
                else str(alignment.join)
            )
        else:
            params["mode"] = _MODE_STACK if mode == _MODE_AUTO and alignment is not None else mode
            params["axis_label"] = (
                self.axisLabelCombo.currentText().strip() or self._default_axis_label()
            )
        # The mode key comes first for readability of the params.
        params = {"mode": params.pop("mode"), **params}
        if self.padCheck.isChecked():
            params["pad"] = "pad"
            params["pad_anchor"] = self.padAnchorCombo.currentData()
            params["pad_value"] = self.padValueCombo.currentData()
            params["pad_max_ratio"] = 0.0 if self.padLargeCheck.isChecked() else DEFAULT_PAD_MAX_RATIO
        return params

    @classmethod
    def get_params(cls, results, parent=None, preselected=None) -> dict | None:
        dialog = cls(results, parent=parent, preselected=preselected)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return None
        return dialog.selected_params()

    # -- internals ----------------------------------------------------------

    def _default_name(self) -> str:
        alignment = self._alignment
        if alignment is not None:
            return "Concatenated" if alignment.mode == _MODE_CONCATENATE else "Stacked"
        if self.modeCombo.currentData() == _MODE_CONCATENATE:
            return "Concatenated"
        return "Stacked"

    def _reference(self):
        """The input the output's axes come from: the first, or in automatic
        mode the highest-rank one."""
        checked = self.checked_results()
        if not checked:
            return None
        if self.modeCombo.currentData() != _MODE_AUTO:
            return checked[0]
        try:
            ranks = [len(shape_for_result(result)) for result in checked]
        except Exception:
            return checked[0]
        return checked[ranks.index(max(ranks))]

    def _default_axis_label(self) -> str:
        reference = self._reference() if hasattr(self, "modeCombo") else None
        if reference is None:
            checked = self.checked_results()
            reference = checked[0] if checked else None
        if reference is None:
            return "Z"
        try:
            return default_stack_axis_label(axis_labels_for_result(reference))
        except Exception:
            return "Z"

    def _rebuild_join_axes(self) -> None:
        checked = self.checked_results()
        first = checked[0] if checked else None
        try:
            labels = axis_labels_for_result(first) if first is not None else []
        except Exception:
            labels = []
        current = self.joinAxisCombo.currentData()
        self.joinAxisCombo.blockSignals(True)
        self.joinAxisCombo.clear()
        for axis, label in enumerate(labels):
            self.joinAxisCombo.addItem(f"{label} (axis {axis})", axis)
        if current is not None and 0 <= int(current) < len(labels):
            self.joinAxisCombo.setCurrentIndex(int(current))
        self.joinAxisCombo.blockSignals(False)

    def _pad_options(self, pad: bool) -> dict:
        if not pad:
            return {"pad": "none"}
        return {
            "pad": "pad",
            "pad_anchor": self.padAnchorCombo.currentData(),
            "pad_value": self.padValueCombo.currentData(),
            "pad_max_ratio": 0.0 if self.padLargeCheck.isChecked() else DEFAULT_PAD_MAX_RATIO,
        }

    def _align(self, checked, mode, pad: bool, append_axis=None):
        """``(alignment, error)`` for ``checked`` with the given settings."""
        concatenate = mode == _MODE_CONCATENATE
        label = self.axisLabelCombo.currentText().strip() or None
        try:
            return align_results(
                checked,
                mode=mode,
                join_axis=int(self.joinAxisCombo.currentData() or 0),
                new_axis_label=None if concatenate else label,
                append_axis=append_axis,
                **self._pad_options(pad),
            ), None
        except CombineMismatch as exc:
            return None, exc
        except ValueError as exc:  # pragma: no cover - align raises CombineMismatch
            return None, CombineMismatch(str(exc))

    def _append_axis_chosen(self) -> None:
        data = self.appendAxisCombo.currentData()
        self._appendChoice = None if data is None else int(data)
        self._refresh()

    def _refresh(self) -> None:
        mode = self.modeCombo.currentData()
        auto = mode == _MODE_AUTO
        concatenate = mode == _MODE_CONCATENATE
        pad = self.padCheck.isChecked()
        if not self.nameEdit.isModified():
            self.nameEdit.setText(self._default_name())
        self._rebuild_join_axes()

        checked = self.checked_results()

        # Automatic mode: ask which axis to append along when the labels do
        # not say, pre-filled with the likeliest.
        ambiguous = None
        if auto:
            _alignment, error = self._align(checked, mode, pad)
            if error is not None and error.kind == "ambiguous":
                ambiguous = error
        self._fill_append_choices(ambiguous)
        append_axis = self._appendChoice if ambiguous is not None else None

        alignment, error = self._align(checked, mode, pad, append_axis=append_axis)
        self._alignment = alignment
        ok = alignment is not None

        resolved_stack = not concatenate and (not auto or alignment is None or alignment.mode == _MODE_STACK)
        self._axisLabelRowLabel.setVisible(resolved_stack)
        self.axisLabelCombo.setVisible(resolved_stack)
        self._joinAxisRowLabel.setVisible(concatenate)
        self.joinAxisCombo.setVisible(concatenate)
        self._appendAxisRowLabel.setVisible(ambiguous is not None)
        self.appendAxisCombo.setVisible(ambiguous is not None)
        for widget in (self._padAnchorRowLabel, self.padAnchorCombo,
                       self._padValueRowLabel, self.padValueCombo, self.padLargeCheck):
            widget.setVisible(pad)
        if not self.nameEdit.isModified():
            self.nameEdit.setText(self._default_name())

        new_axis_label = self.axisLabelCombo.currentText().strip() or None
        plan = alignment.describe() if ok else ""
        if ambiguous is not None:
            plan = (
                "The axis labels do not say which axis to append along; check "
                "'Append along'. " + plan
            ).strip()
        self.planLabel.setText(plan)
        warn = ok and alignment.padded and alignment.pad_fraction >= _PAD_WARN_FRACTION
        self.warningLabel.setText(
            f"Warning: {alignment.pad_fraction * 100:.0f}% of the result is padding." if warn else ""
        )
        self.warningLabel.setVisible(bool(warn))

        offers = {"pad": None, "append": None, "both": None}
        reason = ""
        if not ok:
            reason, offers = self._explain(checked, mode, pad, error)
        self.statusLabel.setText(reason)
        self.buttons.button(QtWidgets.QDialogButtonBox.Ok).setEnabled(ok)
        for key, button in (
            ("pad", self.offerPadButton),
            ("append", self.offerAppendButton),
            ("both", self.offerBothButton),
        ):
            text = offers[key]
            button.setVisible(text is not None)
            if text is not None:
                button.setText(text)

        redirect = self._existing_axis_redirect(ok, mode, new_axis_label, checked, error)
        if redirect is None:
            self.switchToConcatenateButton.setVisible(False)
        else:
            self.switchToConcatenateButton.setText(
                f"Concatenate along the existing {redirect[0]} axis instead"
            )
            self.switchToConcatenateButton.setVisible(True)

    def _fill_append_choices(self, ambiguous) -> None:
        if ambiguous is None:
            self._appendChoice = None
            return
        candidates = list(ambiguous.candidates)
        current = self._appendChoice
        if current is None or current not in [axis for axis, _label in candidates]:
            guess = preferred_candidate(candidates)
            current = guess[0] if guess else None
        self._appendChoice = current
        self.appendAxisCombo.blockSignals(True)
        self.appendAxisCombo.clear()
        for axis, label in candidates:
            self.appendAxisCombo.addItem(f"{label} (axis {axis})", axis)
        index = self.appendAxisCombo.findData(current)
        if index >= 0:
            self.appendAxisCombo.setCurrentIndex(index)
        self.appendAxisCombo.blockSignals(False)

    def _explain(self, checked, mode, pad, error):
        """The reason text for a refusal and the offers that would fix it.

        An offer is a change of this dialog's own options — pad, append —
        that makes the inputs combine; it is named with the shape it gives.
        A difference no option fixes comes with the tool that does.
        """
        offers = {"pad": None, "append": None, "both": None}
        if error is None:
            return "", offers
        reason = _dialog_text(error)
        if error.kind in ("size", "rank", "pad_limit"):
            tries = []
            if not pad:
                tries.append(("pad", True, mode))
            if mode != _MODE_AUTO:
                tries.append(("append", pad, _MODE_AUTO))
            if not pad and mode != _MODE_AUTO:
                tries.append(("both", True, _MODE_AUTO))
            worked = {}
            for key, with_pad, with_mode in tries:
                alignment, other = self._align(checked, with_mode, with_pad)
                if alignment is None and other is not None and other.kind == "ambiguous":
                    alignment, other = self._align(
                        checked, with_mode, with_pad,
                        append_axis=preferred_candidate(other.candidates)[0],
                    )
                if alignment is not None:
                    worked[key] = alignment
                elif other is not None and other.kind in _FUNDAMENTAL and key != "append":
                    # The padded or appended version fails on something no
                    # option of this dialog fixes: that is the reason to give.
                    if error.kind in ("size", "rank"):
                        reason = _dialog_text(other)
            if "both" in worked and ("pad" in worked or "append" in worked):
                del worked["both"]
            if "pad" in worked:
                shape = tuple(worked["pad"].output_shape)
                offers["pad"] = (
                    f"Pad to common size  →  {shape} "
                    f"({worked['pad'].pad_fraction * 100:.0f}% padding)"
                )
            if "append" in worked:
                alignment = worked["append"]
                offers["append"] = (
                    f"Append to axis {alignment.join_label}  →  {tuple(alignment.output_shape)}"
                )
            if "both" in worked:
                alignment = worked["both"]
                offers["both"] = (
                    f"Append to axis {alignment.join_label} and pad to common size  →  "
                    f"{tuple(alignment.output_shape)} ({alignment.pad_fraction * 100:.0f}% padding)"
                )
        return reason, offers

    def _take_offer(self, *, pad: bool, auto: bool) -> None:
        if pad:
            self.padCheck.setChecked(True)
        if auto:
            index = self.modeCombo.findData(_MODE_AUTO)
            if index >= 0:
                self.modeCombo.setCurrentIndex(index)
        self._refresh()

    @staticmethod
    def _existing_axis_redirect(
        ok: bool, mode: str, new_axis_label: str | None, checked: list, error=None
    ) -> tuple[str, int] | None:
        """Return ``(label, axis)`` when stacking is blocked by a duplicate
        axis label and concatenate along that existing axis is valid."""
        if ok or mode not in (_MODE_STACK, _MODE_AUTO) or not new_axis_label or len(checked) < 2:
            return None
        if error is not None and error.kind != "label_collision":
            return None
        try:
            labels = axis_labels_for_result(checked[0])
        except Exception:
            return None
        if new_axis_label not in labels:
            return None
        axis = labels.index(new_axis_label)
        try:
            align_results(checked, mode=_MODE_CONCATENATE, join_axis=axis)
        except ValueError:
            return None
        return new_axis_label, axis

    def _switch_to_concatenate_along_existing(self) -> None:
        redirect = self._existing_axis_redirect(
            False,
            _MODE_STACK,
            self.axisLabelCombo.currentText().strip() or None,
            self.checked_results(),
        )
        if redirect is None:
            return
        _label, axis = redirect
        mode_index = self.modeCombo.findData(_MODE_CONCATENATE)
        if mode_index >= 0:
            self.modeCombo.setCurrentIndex(mode_index)
        join_index = self.joinAxisCombo.findData(axis)
        if join_index >= 0:
            self.joinAxisCombo.setCurrentIndex(join_index)


def _dialog_text(error: CombineMismatch) -> str:
    """The processor's reason, worded for this dialog's controls."""
    text = str(error)
    if error.kind == "pad_limit":
        head = text.split("; raise", 1)[0]
        return (
            f"{head}. Tick 'Allow padding beyond {DEFAULT_PAD_MAX_RATIO:g}x' to do it anyway, "
            f"or resample with Scale/Resize."
        )
    return text


__all__ = ["ResultInputListWidget", "StackCombineDialog", "expand_input_choices"]
