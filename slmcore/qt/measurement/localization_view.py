"""Host-neutral measurement/localization composition widget."""

from __future__ import annotations

from typing import Any,Mapping,Sequence

from qtpy import QtCore,QtWidgets

from .localization_workbench import LocalizationWorkbench


class MeasurementLocalizationView(QtWidgets.QWidget):
    """Thin wrapper around :class:`LocalizationWorkbench`.

    The widget owns generic source controls, localization presentation, optional
    metrics readout and an optional host-defined accept action. It deliberately
    does not assign workflow meaning to accepting a localization.
    """

    sigAcquireRequested = QtCore.Signal(str)
    sigLoadRequested = QtCore.Signal()
    sigRunRequested = QtCore.Signal(object)
    sigAcceptRequested = QtCore.Signal()
    sigCandidateStateChanged = QtCore.Signal(bool)

    def __init__(
        self,
        *,
        parameters: Mapping[str,Any],
        context: Mapping[str, Any] | None=None,
        detectors: Sequence[str]=(),
        current_detector: str | None=None,
        show_metrics: bool=False,
        show_accept: bool=False,
        parent: QtWidgets.QWidget | None=None,
    ) -> None:
        super().__init__(parent)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(0)

        self._read_only = False
        self._accept_enabled = False
        self._workbench = LocalizationWorkbench(
            parameters=parameters,
            context=context,
            presentation="vertical",
            parent=self,
        )
        self._workbench.configure_measurement_sources(
            detectors,
            current_detector=current_detector,
            allow_load=True,
            visible=True,
        )
        self._workbench.sigAcquireRequested.connect(
            lambda detector:self.sigAcquireRequested.emit(detector)
        )
        self._workbench.sigLoadRequested.connect(self.sigLoadRequested.emit)
        self._workbench.sigRunRequested.connect(
            lambda parameters:self.sigRunRequested.emit(parameters)
        )
        self._workbench.sigCandidateStateChanged.connect(
            self.sigCandidateStateChanged.emit
        )

        self._uniformity_label: QtWidgets.QLabel | None = None
        self._efficiency_label: QtWidgets.QLabel | None = None
        if show_metrics:
            self._workbench.add_result_widget(self._build_metrics_widget())

        self._accept_button: QtWidgets.QPushButton | None = None
        if show_accept:
            self._accept_button = QtWidgets.QPushButton("Accept Localization")
            self._accept_button.setMinimumHeight(23)
            self._accept_button.setMinimumWidth(160)
            self._accept_button.clicked.connect(
                lambda _checked=False:self.sigAcceptRequested.emit()
            )
            self._workbench.add_action_widget(self._accept_button)

        layout.addWidget(self._workbench,1)

    @property
    def measurement(self):
        return self._workbench.measurement

    @property
    def candidate(self):
        return self._workbench.candidate

    @property
    def candidate_parameters(self):
        return self._workbench.candidate_parameters

    @property
    def candidate_is_current(self) -> bool:
        return self._workbench.candidate_is_current

    @property
    def current_detector(self) -> str | None:
        return self._workbench.measurement_controls.current_detector

    def configure_detectors(
        self,detectors: Sequence[str],current_detector: str | None=None,
    ) -> None:
        self._workbench.configure_measurement_sources(
            detectors,
            current_detector=current_detector,
            allow_load=True,
            visible=True,
        )


    def set_acquire_available(self,available: bool,tooltip: str="") -> None:
        self._workbench.measurement_controls.set_acquire_available(
            available,tooltip
        )

    def set_detector_selection_enabled(self,enabled: bool) -> None:
        self._workbench.measurement_controls.set_detector_selection_enabled(enabled)

    def set_context(
        self,context: Mapping[str, Any] | None=None,
    ) -> None:
        self._workbench.set_context(context)

    def set_parameters(
        self,parameters: Mapping[str,Any],*,invalidate_candidate: bool=True,
    ) -> None:
        self._workbench.set_parameters(
            parameters,invalidate_candidate=invalidate_candidate,
        )

    def set_measurement(self,measurement) -> None:
        self._workbench.set_measurement(measurement)

    def clear_measurement(self) -> None:
        self._workbench.clear_measurement()

    def set_read_only(self,read_only: bool) -> None:
        self._read_only = bool(read_only)
        self._workbench.set_read_only(self._read_only)
        self._refresh_accept_enabled()

    def set_measurement_busy(self,busy: bool,text: str="") -> None:
        self._workbench.set_measurement_busy(busy,text)

    def set_measurement_error(self,error: Any) -> None:
        self._workbench.set_measurement_error(error)

    def set_measurement_status(
        self,text: str,*,warning: bool=False,error: bool=False,
    ) -> None:
        """Set the compact elided source-row status shown beside Acquire/Load."""
        self._workbench.measurement_controls.set_status(
            text,warning=warning,error=error,
        )

    def refresh_measurement_status(self) -> None:
        """Restore the normal source summary after a transient workflow warning."""
        measurement = self.measurement
        if measurement is None:
            self._workbench.measurement_controls.set_status("")
        else:
            self._workbench.measurement_controls.set_measurement(measurement)

    def set_result(
        self,result: Any,parameters: Mapping[str, Any] | None=None,
    ) -> None:
        self._workbench.set_result(result,parameters)

    def set_error(self,error: Any) -> None:
        self._workbench.set_error(error)

    def set_committed_result(
        self,result: Any,parameters: Mapping[str, Any] | None=None,
    ) -> None:
        self._workbench.set_committed_result(result,parameters)

    def clear_localization_result(self) -> None:
        self._workbench.clear_localization_result()

    def set_auxiliary_image(self,name: str,image: Any) -> None:
        self._workbench.result_view.set_auxiliary_image(name,image)

    def add_result_widget(self,widget: QtWidgets.QWidget) -> None:
        """Add optional host-specific information beside the result viewer."""
        self._workbench.add_result_widget(widget)

    def set_lower_pane_height(self,height: int) -> None:
        """Set the compact lower work-area height in vertical presentation."""
        self._workbench.set_lower_pane_height(height)

    def add_lower_widget(self,widget: QtWidgets.QWidget) -> None:
        self._workbench.add_lower_widget(widget)

    def clear_auxiliary_image(self,name: str) -> None:
        self._workbench.result_view.clear_auxiliary_image(name)

    def set_accept_enabled(self,enabled: bool) -> None:
        self._accept_enabled = bool(enabled)
        self._refresh_accept_enabled()

    def _refresh_accept_enabled(self) -> None:
        if self._accept_button is not None:
            self._accept_button.setEnabled(
                self._accept_enabled and not self._read_only
            )

    def set_accept_tooltip(self,text: str) -> None:
        if self._accept_button is not None:
            self._accept_button.setToolTip(str(text))

    def set_metrics(self,metrics: Any=None) -> None:
        if self._uniformity_label is None or self._efficiency_label is None:
            return
        values = {} if metrics is None else dict(metrics.values)
        uniformity = values.get("uniformity")
        efficiency = values.get("efficiency")
        self._uniformity_label.setText(
            "—" if uniformity is None else "%.4f" % float(uniformity)
        )
        self._efficiency_label.setText(
            "—" if efficiency is None else "%.4f" % float(efficiency)
        )

    def _build_metrics_widget(self) -> QtWidgets.QWidget:
        metrics = QtWidgets.QWidget()
        metrics_layout = QtWidgets.QVBoxLayout(metrics)
        metrics_layout.setContentsMargins(0,0,0,0)
        metrics_layout.setSpacing(3)

        metrics_heading = QtWidgets.QLabel("Measurement Metrics")
        metrics_font = metrics_heading.font()
        metrics_font.setBold(True)
        metrics_heading.setFont(metrics_font)
        metrics_layout.addWidget(metrics_heading)

        metrics_values = QtWidgets.QHBoxLayout()
        metrics_values.setContentsMargins(0,0,0,0)
        metrics_values.setSpacing(8)
        metrics_values.addWidget(QtWidgets.QLabel("Uniformity"))
        self._uniformity_label = QtWidgets.QLabel("—")
        metrics_values.addWidget(self._uniformity_label)
        metrics_values.addSpacing(12)
        metrics_values.addWidget(QtWidgets.QLabel("Efficiency"))
        self._efficiency_label = QtWidgets.QLabel("—")
        metrics_values.addWidget(self._efficiency_label)
        metrics_values.addStretch(1)
        for value_label in (self._uniformity_label,self._efficiency_label):
            value_font = value_label.font()
            value_font.setBold(True)
            value_label.setFont(value_font)
        metrics_layout.addLayout(metrics_values)
        return metrics


__all__ = ["MeasurementLocalizationView"]
