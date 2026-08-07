"""Shared input picker for operations that take several results.

Any operation over more than one reconstruction object — merge channels,
stack/combine, the image calculator, a two-image FRC, a multi-input drop-in
plugin — needs the same three things: the loaded results expanded into
pickable inputs, a way to choose which of them take part, and a way to order
them (channel order, stack order, A-vs-B). This module holds that once so the
dialogs and the generic processor panel do not each grow their own list.
"""

from __future__ import annotations

from qtpy import QtCore, QtWidgets

from imswitch.improcess.processors._axis_split import (
    axis_labels_for_result,
    shape_for_result,
)


def expand_input_choices(results, preselected=None, accepts=None):
    """Expand results into checkable inputs, one per component.

    A multi-layer result (``display_layers``) contributes its whole-result
    entry plus one entry per non-context component via
    ``processor_input_choices()``, so heterogeneous results are listed layer
    by layer instead of hiding their components. Returns
    ``(label, result, checked_by_default)`` tuples.

    ``preselected`` restricts the initially-checked entries to those whole
    results (compared by identity, since results are not required to be
    hashable or comparable); ``None`` checks every whole result, which is what
    a caller passing an already-narrowed selection wants. ``accepts`` is an
    optional predicate — pass ``processor.accepts`` to leave out inputs the
    operation cannot use at all.
    """
    inputs = []
    for result in results:
        checked_by_default = preselected is None or any(
            result is candidate for candidate in preselected
        )
        choices_fn = getattr(result, "processor_input_choices", None)
        choices = choices_fn() if callable(choices_fn) else None
        if not choices:
            if accepts is not None and not _accepts(accepts, result):
                continue
            inputs.append((getattr(result, "name", "result"), result, checked_by_default))
            continue
        for choice in choices:
            whole = choice.id == "result"
            if accepts is not None and not _accepts(accepts, choice.result):
                continue
            label = (
                getattr(result, "name", "result")
                if whole
                else f"{getattr(result, 'name', 'result')} › {choice.label}"
            )
            inputs.append((label, choice.result, whole and checked_by_default))
    return inputs


def describe_input(label, result) -> str:
    """``name — (shape) axes`` summary used in every input list."""
    try:
        shape = tuple(shape_for_result(result))
        labels = axis_labels_for_result(result)
        return f"{label} — {shape} {''.join(labels)}"
    except Exception:
        return str(label)


class ResultInputListWidget(QtWidgets.QWidget):
    """Checkable, reorderable list of the results an operation will consume.

    Order is explicit and editable because it is meaningful: it decides which
    merged channel is which, the order of a stack, and which image is A in a
    two-image operation. Selecting inputs by ctrl-clicking a list elsewhere in
    the window cannot express that.
    """

    sigInputsChanged = QtCore.Signal()

    def __init__(self, results=None, parent=None, *, preselected=None, accepts=None):
        super().__init__(parent)
        self._inputs: list[tuple[str, object, bool]] = []

        self.inputList = QtWidgets.QListWidget()
        self.inputList.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.upButton = QtWidgets.QPushButton("Up")
        self.downButton = QtWidgets.QPushButton("Down")
        self.upButton.clicked.connect(lambda: self._move_selected(-1))
        self.downButton.clicked.connect(lambda: self._move_selected(+1))

        orderButtons = QtWidgets.QVBoxLayout()
        orderButtons.addWidget(self.upButton)
        orderButtons.addWidget(self.downButton)
        orderButtons.addStretch()

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.inputList)
        layout.addLayout(orderButtons)

        self.inputList.itemChanged.connect(lambda _item: self.sigInputsChanged.emit())
        self.setResults(results or [], preselected=preselected, accepts=accepts)

    def setResults(self, results, *, preselected=None, accepts=None) -> None:
        """Rebuild the list from ``results``, keeping prior checks where possible."""
        previously_checked = self.checked_results() if self._inputs else None
        if preselected is None and previously_checked:
            preselected = previously_checked
        self._inputs = expand_input_choices(
            list(results or []), preselected=preselected, accepts=accepts
        )
        self.inputList.blockSignals(True)
        self.inputList.clear()
        for label, result, checked in self._inputs:
            item = QtWidgets.QListWidgetItem(describe_input(label, result))
            item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.Checked if checked else QtCore.Qt.Unchecked)
            self.inputList.addItem(item)
        self.inputList.blockSignals(False)
        self.sigInputsChanged.emit()

    def checked_results(self) -> list:
        """The checked inputs, in list order."""
        return [
            self._inputs[row][1]
            for row in range(self.inputList.count())
            if self.inputList.item(row).checkState() == QtCore.Qt.Checked
        ]

    def _move_selected(self, delta: int) -> None:
        row = self.inputList.currentRow()
        target = row + delta
        if row < 0 or not (0 <= target < len(self._inputs)):
            return
        self._inputs[row], self._inputs[target] = (
            self._inputs[target],
            self._inputs[row],
        )
        item = self.inputList.takeItem(row)
        self.inputList.insertItem(target, item)
        self.inputList.setCurrentRow(target)
        self.sigInputsChanged.emit()


def _accepts(predicate, result) -> bool:
    """Never let a processor's own gate raise its way out of the picker."""
    try:
        return bool(predicate(result))
    except Exception:
        return False


__all__ = [
    "ResultInputListWidget",
    "describe_input",
    "expand_input_choices",
]
