"""Generic panel for applying result-based ImProcess processors."""

import weakref

from qtpy import QtCore, QtWidgets

from imswitch.improcess.view.ResultInputList import ResultInputListWidget

#: "Apply to" scopes offered for single-input processors.
SCOPE_CURRENT = "current"
SCOPE_SELECTED = "selected"
SCOPE_ALL = "all"


class ResultProcessorWidget(QtWidgets.QWidget):
    """Run one registered Processor over one or several results.

    Two shapes, chosen from the processor's declared arity:

    * single-input (``max_inputs == 1``) — pick the input component, then an
      "Apply to" scope so the same operation can sweep the selected or all
      loaded results instead of only the current one;
    * multi-input — a checkable, ordered picker over every loaded result,
      because the processor consumes them together (merge, stack, calculator,
      two-image FRC). Incompatible picks disable Run and show the processor's
      own reason rather than failing on click.
    """

    sigRunRequested = QtCore.Signal(object, dict)
    """``(inputs, params)`` — ``inputs`` is the ordered list of results to run
    on. A list is always emitted, even for one result, so the controller has a
    single code path; it still accepts a bare result from custom panels."""

    def __init__(self, processor, parent=None):
        super().__init__(parent)
        self.processor = processor
        self._currentResult = None
        self._allResults = []
        self._selectedResults = []
        # Building the per-display-layer wrapper results in
        # processor_input_choices() is non-trivial, so cache the filtered
        # choices per result object (weak-keyed to avoid retaining results).
        self._choicesCache = weakref.WeakKeyDictionary()

        self.statusLabel = QtWidgets.QLabel("No result selected.")
        self.statusLabel.setWordWrap(True)
        self.runButton = QtWidgets.QPushButton(f"Run {processor.name}")
        self.runButton.setEnabled(False)

        self.paramWidget = processor.make_param_widget(self)

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(4, 4, 4, 4)

        if self.isMultiInput():
            self.inputCombo = None
            self.scopeCombo = None
            self.inputWidget = ResultInputListWidget(
                [], self, accepts=self.processor.accepts
            )
            self.inputWidget.sigInputsChanged.connect(self._refreshMultiInput)
            layout.addWidget(QtWidgets.QLabel("Inputs (checked, in run order):"))
            layout.addWidget(self.inputWidget)
        else:
            self.inputWidget = None
            self.inputCombo = QtWidgets.QComboBox()
            self.inputCombo.setToolTip(
                "Processor input: whole result or a named result component"
            )
            self.scopeCombo = QtWidgets.QComboBox()
            self.scopeCombo.addItem("Current result", SCOPE_CURRENT)
            self.scopeCombo.addItem("Selected results", SCOPE_SELECTED)
            self.scopeCombo.addItem("All results", SCOPE_ALL)
            self.scopeCombo.setToolTip(
                "Run once on the current result, or once per selected/loaded "
                "result"
            )
            self.scopeCombo.currentIndexChanged.connect(self._scopeChanged)
            form = QtWidgets.QFormLayout()
            form.addRow("Input", self.inputCombo)
            form.addRow("Apply to", self.scopeCombo)
            layout.addLayout(form)

        layout.addWidget(self.paramWidget)
        layout.addWidget(self.statusLabel)
        layout.addWidget(self.runButton)
        layout.addStretch()
        self.setLayout(layout)

        self.runButton.clicked.connect(self._run)

    # -- arity ------------------------------------------------------------

    def isMultiInput(self) -> bool:
        """True when the processor consumes several results in one run."""
        return getattr(self.processor, "max_inputs", 1) != 1

    # -- inputs -----------------------------------------------------------

    def setCurrentResult(self, result) -> None:
        self._currentResult = result
        if self.isMultiInput():
            self._refreshMultiInput()
            return

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
                    # accepts() = semantic kind + shape gate; a table result
                    # with a 2D data array must not be offered to an image
                    # processor.
                    applies = bool(self.processor.accepts(choice.result))
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

        self.inputCombo.blockSignals(False)
        self._refreshSingleInput(bool(choices))

    def setAvailableResults(self, results, selected=None) -> None:
        """Tell the panel which results exist, and which are selected.

        ``results``/``selected`` are ``(displayName, result)`` pairs — the
        shape the reconstruction list publishes through the communication
        channel — or bare results.
        """
        self._allResults = _results_of(results)
        self._selectedResults = _results_of(selected)
        if self.isMultiInput():
            preselected = self._selectedResults or None
            self.inputWidget.setResults(
                self._allResults,
                preselected=preselected,
                accepts=self.processor.accepts,
            )
        else:
            self._refreshSingleInput(self.inputCombo.count() > 0)

    def selectedInputChoice(self):
        if self.inputCombo is None:
            return None
        index = self.inputCombo.currentIndex()
        if index < 0:
            return None
        return self.inputCombo.itemData(index)

    def selectedInputs(self) -> list:
        """The results this panel would run on, in order."""
        if self.isMultiInput():
            return self.inputWidget.checked_results()

        choice = self.selectedInputChoice()
        scope = self.scope()
        if scope == SCOPE_CURRENT:
            return [choice.result] if choice is not None else []

        pool = self._selectedResults if scope == SCOPE_SELECTED else self._allResults
        # A batch runs on whole results: the input-component choice describes
        # the current result and has no meaning for the others.
        inputs = [result for result in pool if _accepts(self.processor, result)]
        if not inputs and choice is not None:
            return [choice.result]
        return inputs

    def scope(self) -> str:
        if self.scopeCombo is None:
            return SCOPE_CURRENT
        return str(self.scopeCombo.currentData() or SCOPE_CURRENT)

    def parameterValues(self) -> dict:
        getter = getattr(self.paramWidget, "get_values", None)
        return dict(getter() if callable(getter) else {})

    def setStatusText(self, text: str) -> None:
        self.statusLabel.setText(text)

    # -- internals --------------------------------------------------------

    def _scopeChanged(self, _index: int) -> None:
        if self.inputCombo is not None:
            self.inputCombo.setEnabled(self.scope() == SCOPE_CURRENT)
        self._refreshSingleInput(self.inputCombo.count() > 0)

    def _refreshSingleInput(self, has_choices: bool) -> None:
        inputs = self.selectedInputs()
        self.runButton.setEnabled(bool(inputs))
        if not has_choices and not inputs:
            self.statusLabel.setText(
                f"{self.processor.name} does not apply to the current result."
            )
        elif self.scope() == SCOPE_CURRENT:
            self.statusLabel.setText("Choose a result input and run the processor.")
        else:
            noun = "result" if len(inputs) == 1 else "results"
            self.statusLabel.setText(f"Will run on {len(inputs)} {noun}.")

    def _refreshMultiInput(self) -> None:
        inputs = self.inputWidget.checked_results()
        ok, reason = _check_inputs(self.processor, inputs)
        self.runButton.setEnabled(ok)
        if ok:
            self.statusLabel.setText(f"Will run on {len(inputs)} results.")
        else:
            self.statusLabel.setText(reason)

    def _run(self) -> None:
        inputs = self.selectedInputs()
        if not inputs:
            self.setStatusText("No processor input selected.")
            return
        if self.isMultiInput():
            ok, reason = _check_inputs(self.processor, inputs)
            if not ok:
                self.setStatusText(reason)
                return
        self.sigRunRequested.emit(inputs, self.parameterValues())


def _results_of(entries) -> list:
    """Normalize ``(name, result)`` pairs or bare results to a result list."""
    results = []
    for entry in entries or []:
        if isinstance(entry, tuple) and len(entry) == 2:
            results.append(entry[1])
        else:
            results.append(entry)
    return [result for result in results if result is not None]


def _accepts(processor, result) -> bool:
    try:
        return bool(processor.accepts(result))
    except Exception:
        return False


def _check_inputs(processor, results) -> tuple[bool, str]:
    checker = getattr(processor, "check_inputs", None)
    if not callable(checker):
        return bool(results), "" if results else "Select at least one result."
    try:
        return checker(results)
    except Exception as exc:  # a processor's own gate must not break the panel
        return False, str(exc)
