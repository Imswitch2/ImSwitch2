"""Reusable host-driven controls for supplying image measurements."""

from __future__ import annotations

from typing import Sequence

from qtpy import QtCore,QtWidgets

from ...core.measurement import ImageMeasurement
from ..widgets.uitools import ElidedLabel


_MUTED_COLOR = "#888"
_ERROR_COLOR = "#a33"
_WARNING_COLOR = "#a66a00"


class MeasurementControls(QtWidgets.QWidget):
    """Compact source controls without owning an image viewer or hardware.

    The widget only exposes user intent. A host application provides detector
    names, handles :attr:`sigAcquireRequested` / :attr:`sigLoadRequested`, and
    returns the resulting :class:`~slmcore.core.measurement.ImageMeasurement` to the
    containing workflow.
    """

    sigAcquireRequested = QtCore.Signal(str)
    sigLoadRequested = QtCore.Signal()

    def __init__(
        self,parent: QtWidgets.QWidget | None=None,
    ) -> None:
        super().__init__(parent)
        self._busy = False
        self._load_available = False
        self._acquire_available = True
        self._acquire_tooltip = ""
        self._detector_selection_enabled = True

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(6)

        layout.addWidget(QtWidgets.QLabel("Detector:"))

        self.detector_combo = QtWidgets.QComboBox()
        self.detector_combo.setMinimumWidth(150)
        self.detector_combo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.AdjustToContentsOnFirstShow,
        )
        layout.addWidget(self.detector_combo)

        self.acquire_button = QtWidgets.QPushButton("Acquire")
        self.acquire_button.clicked.connect(self._request_acquire)
        layout.addWidget(self.acquire_button)

        self.load_button = QtWidgets.QPushButton("Load...")
        self.load_button.clicked.connect(
            lambda _checked=False:self.sigLoadRequested.emit()
        )
        layout.addWidget(self.load_button)

        self.status_label = ElidedLabel("")
        self.status_label.setStyleSheet("color: %s;" % _MUTED_COLOR)
        self.status_label.setWordWrap(False)
        layout.addWidget(self.status_label,1)

        self._refresh_enabled_state()

    @property
    def current_detector(self) -> str | None:
        value = self.detector_combo.currentData()
        if value is None:
            text = self.detector_combo.currentText().strip()
            return text or None
        text = str(value).strip()
        return text or None

    def set_detectors(
        self,
        detectors: Sequence[str],
        current_detector: str | None=None,
    ) -> None:
        names = tuple(str(value).strip() for value in detectors if str(value).strip())
        blocker = QtCore.QSignalBlocker(self.detector_combo)
        try:
            self.detector_combo.clear()
            for name in names:
                self.detector_combo.addItem(name,name)

            index = -1
            if current_detector is not None:
                requested = str(current_detector)
                index = self.detector_combo.findData(requested)
                if index < 0:
                    index = self.detector_combo.findText(requested)
            if index < 0 and self.detector_combo.count() > 0:
                index = 0
            self.detector_combo.setCurrentIndex(index)
        finally:
            del blocker
        self._refresh_enabled_state()


    def set_acquire_available(self,available: bool,tooltip: str="") -> None:
        self._acquire_available = bool(available)
        self._acquire_tooltip = str(tooltip or "")
        self.acquire_button.setToolTip(self._acquire_tooltip)
        self._refresh_enabled_state()

    def set_detector_selection_enabled(self,enabled: bool) -> None:
        self._detector_selection_enabled = bool(enabled)
        self._refresh_enabled_state()

    def set_load_available(self,available: bool) -> None:
        self._load_available = bool(available)
        self._refresh_enabled_state()

    def set_busy(self,busy: bool,text: str="") -> None:
        self._busy = bool(busy)
        if text:
            self.set_status(text)
        self._refresh_enabled_state()

    def set_measurement(self,measurement: ImageMeasurement) -> None:
        if not isinstance(measurement,ImageMeasurement):
            raise TypeError("measurement must be an ImageMeasurement")

        detector = measurement.detector
        if detector:
            index = self.detector_combo.findData(detector)
            if index < 0:
                index = self.detector_combo.findText(detector)
            if index >= 0:
                self.detector_combo.setCurrentIndex(index)

        height,width = measurement.image.shape
        source = measurement.source
        if detector:
            source = "%s · %s" % (source,detector)
        self.set_status("%s · %d×%d" % (source,width,height))

    def set_status(
        self,text: str,*,error: bool=False,warning: bool=False,
    ) -> None:
        if error and warning:
            raise ValueError("Measurement status cannot be both error and warning")
        self.status_label.set_full_text(str(text or ""))
        color = (
            _ERROR_COLOR if error
            else _WARNING_COLOR if warning
            else _MUTED_COLOR
        )
        self.status_label.setStyleSheet("color: %s;" % color)

    def _request_acquire(self,*_args) -> None:
        detector = self.current_detector
        if detector is not None and not self._busy:
            self.sigAcquireRequested.emit(detector)

    def _refresh_enabled_state(self) -> None:
        has_detector = self.detector_combo.count() > 0
        self.detector_combo.setEnabled(
            has_detector and not self._busy and self._detector_selection_enabled
        )
        self.acquire_button.setEnabled(
            has_detector and not self._busy and self._acquire_available
        )
        self.load_button.setEnabled(self._load_available and not self._busy)


__all__ = ["MeasurementControls"]
