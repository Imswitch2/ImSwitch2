"""Generic panel for applying result-based ImProcess processors."""

import weakref

from qtpy import QtCore, QtWidgets


class ResultProcessorWidget(QtWidgets.QWidget):
    """Run one registered Processor on an explicit result/component input."""

    sigRunRequested = QtCore.Signal(object, dict)

    def __init__(self, processor, parent=None):
        super().__init__(parent)
        self.processor = processor
        self._currentResult = None
        # Building the per-display-layer wrapper results in
        # processor_input_choices() is non-trivial, so cache the filtered
        # choices per result object (weak-keyed to avoid retaining results).
        self._choicesCache = weakref.WeakKeyDictionary()

        self.inputCombo = QtWidgets.QComboBox()
        self.inputCombo.setToolTip("Processor input: whole result or a named result component")
        self.statusLabel = QtWidgets.QLabel("No result selected.")
        self.statusLabel.setWordWrap(True)
        self.runButton = QtWidgets.QPushButton(f"Run {processor.name}")
        self.runButton.setEnabled(False)

        self.paramWidget = processor.make_param_widget(self)

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(4, 4, 4, 4)
        form = QtWidgets.QFormLayout()
        form.addRow("Input", self.inputCombo)
        layout.addLayout(form)
        layout.addWidget(self.paramWidget)
        layout.addWidget(self.statusLabel)
        layout.addWidget(self.runButton)
        layout.addStretch()
        self.setLayout(layout)

        self.runButton.clicked.connect(self._run)

    def setCurrentResult(self, result) -> None:
        self._currentResult = result
        self.inputCombo.blockSignals(True)
        self.inputCombo.clear()

        if result is None or not hasattr(result, "processor_input_choices"):
            self.statusLabel.setText("No compatible result selected.")
            self.runButton.setEnabled(False)
            self.inputCombo.blockSignals(False)
            return

        choices = self._choicesCache.get(result)
        if choices is None:
            choices = []
            for choice in result.processor_input_choices():
                try:
                    applies = bool(self.processor.applies_to(choice.result))
                except Exception:
                    applies = False
                if applies:
                    choices.append(choice)
            try:
                self._choicesCache[result] = choices
            except TypeError:
                pass  # result not weak-referenceable; skip caching

        for choice in choices:
            self.inputCombo.addItem(choice.label, userData=choice)

        self.runButton.setEnabled(bool(choices))
        self.statusLabel.setText(
            "Choose a result input and run the processor."
            if choices else
            f"{self.processor.name} does not apply to the current result."
        )
        self.inputCombo.blockSignals(False)

    def selectedInputChoice(self):
        index = self.inputCombo.currentIndex()
        if index < 0:
            return None
        return self.inputCombo.itemData(index)

    def parameterValues(self) -> dict:
        getter = getattr(self.paramWidget, "get_values", None)
        return dict(getter() if callable(getter) else {})

    def setStatusText(self, text: str) -> None:
        self.statusLabel.setText(text)

    def _run(self) -> None:
        choice = self.selectedInputChoice()
        if choice is None:
            self.setStatusText("No processor input selected.")
            return
        self.sigRunRequested.emit(choice.result, self.parameterValues())
