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

        # ROI restriction (P-R). Offered only to processors that declared they
        # accept one, so a processor that ignores ROIs can never be handed a
        # cropped input by a UI that guessed.
        self.roiCombo = None
        self.roiModeCombo = None
        if getattr(processor, "accepts_roi", False):
            self.roiCombo = QtWidgets.QComboBox()
            self.roiCombo.setToolTip(
                "Run over a region instead of the whole image. The ROIs come "
                "from the ROI manager's active set."
            )
            self.roiModeCombo = QtWidgets.QComboBox()
            for mode in getattr(processor, "roi_modes", ("crop", "mask")):
                self.roiModeCombo.addItem(
                    {
                        "crop": "Crop to the region",
                        "mask": "Mask outside the region",
                    }.get(mode, mode),
                    mode,
                )
            self.roiModeCombo.setToolTip(
                "Crop puts the region on its own smaller grid; Mask keeps the "
                "whole frame so the output stays pixel-aligned with the input."
            )
            roiForm = QtWidgets.QFormLayout()
            roiForm.addRow("Region", self.roiCombo)
            roiForm.addRow("", self.roiModeCombo)
            layout.addLayout(roiForm)
            self._refreshROIChoices()

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
        self._retargetParamWidget(result)
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
        values = dict(getter() if callable(getter) else {})
        restriction = self.roiRestriction()
        if restriction is not None:
            from imswitch.improcess.analysis.roi_restriction import ROI_PARAM

            values[ROI_PARAM] = restriction
        return values

    # -- ROI restriction (P-R) --------------------------------------------

    def setROIManagerWidget(self, panel) -> None:
        """The ROI manager whose active set this panel may restrict to.

        Late-bound and optional: both panels are runtime-loaded, in either
        order, and a processor must still run without one.
        """
        self._roiManagerWidget = panel
        self._refreshROIChoices()

    def _retargetParamWidget(self, result) -> None:
        """Tell a parameter widget which result it is now editing.

        Most parameter widgets are the same whatever the input, so this is
        opt-in: a widget that needs the result -- one showing a row per axis,
        say -- declares ``setResult`` and gets told. Without it such a widget
        would keep showing the previous result's axes, and a crop typed
        against those would apply to the wrong extent.
        """
        setter = getattr(self.paramWidget, "setResult", None)
        if not callable(setter):
            return
        try:
            setter(result, self._visibleROIs())
        except Exception:
            # A parameter widget that cannot show this result leaves the panel
            # usable; the status line and the run itself still report properly.
            # This is a view with no logger of its own, and raising here would
            # take down every result switch.
            self.statusLabel.setText(
                "Could not show parameters for this result."
            )

    def _visibleROIs(self) -> list:
        panel = getattr(self, "_roiManagerWidget", None)
        if panel is None:
            return []
        try:
            return [roi for roi in panel.rois() if roi.visible]
        except Exception:
            return []

    def _refreshROIChoices(self) -> None:
        if self.roiCombo is None:
            return
        panel = getattr(self, "_roiManagerWidget", None)
        rois = []
        if panel is not None:
            try:
                rois = [roi for roi in panel.rois() if roi.visible]
            except Exception:
                rois = []

        current = self.roiCombo.currentData()
        self.roiCombo.blockSignals(True)
        self.roiCombo.clear()
        self.roiCombo.addItem("Whole image", None)
        if rois:
            self.roiCombo.addItem(f"All {len(rois)} ROIs", "*")
        for roi in rois:
            self.roiCombo.addItem(roi.name, roi.uid)
        index = self.roiCombo.findData(current)
        self.roiCombo.setCurrentIndex(max(0, index))
        self.roiCombo.blockSignals(False)
        self.roiCombo.setEnabled(bool(rois))
        if not rois:
            self.roiCombo.setToolTip(
                "No ROIs available. Draw some in the ROI manager, or leave "
                "this as Whole image."
            )

    def roiRestriction(self):
        """The restriction this panel is configured for, or None."""
        if self.roiCombo is None:
            return None
        choice = self.roiCombo.currentData()
        if choice is None:
            return None
        panel = getattr(self, "_roiManagerWidget", None)
        if panel is None:
            return None
        try:
            rois = [roi for roi in panel.rois() if roi.visible]
            roi_set = panel.active_set()
        except Exception:
            return None
        if choice != "*":
            rois = [roi for roi in rois if roi.uid == choice]
        if not rois:
            return None

        from imswitch.improcess.analysis.roi_restriction import ROIRestriction

        return ROIRestriction(
            rois=tuple(rois),
            mode=str(self.roiModeCombo.currentData() or "crop"),
            set_uid=getattr(roi_set, "uid", ""),
            set_name=getattr(roi_set, "name", ""),
            set_revision=int(getattr(roi_set, "revision", 0) or 0),
        )

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
