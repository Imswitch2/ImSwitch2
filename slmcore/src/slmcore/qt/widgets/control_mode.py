from __future__ import annotations

from qtpy import QtCore,QtWidgets

from ...application.control_mode import SLMControlMode


class SLMControlModeSelector(QtWidgets.QWidget):
    """Compact two-state selector for editor versus fast config control."""

    sigModeRequested = QtCore.Signal(object)

    def __init__(self,parent=None) -> None:
        super().__init__(parent)
        self._mode = SLMControlMode.EDITOR

        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0,0,0,0)
        row.setSpacing(0)
        row.addWidget(QtWidgets.QLabel("Mode:"))
        row.addSpacing(4)

        self.editor_button = QtWidgets.QToolButton(self)
        self.editor_button.setText("Edit")
        self.fast_button = QtWidgets.QToolButton(self)
        self.fast_button.setText("Fast")
        self._buttons = QtWidgets.QButtonGroup(self)
        self._buttons.setExclusive(True)
        self._buttons.addButton(self.editor_button)
        self._buttons.addButton(self.fast_button)
        for button in (self.editor_button,self.fast_button):
            button.setCheckable(True)
            button.setAutoRaise(False)
            button.setFocusPolicy(QtCore.Qt.NoFocus)

        self.editor_button.clicked.connect(
            lambda _checked=False:self._request(SLMControlMode.EDITOR)
        )
        self.fast_button.clicked.connect(
            lambda _checked=False:self._request(SLMControlMode.FAST_CONFIG)
        )
        row.addWidget(self.editor_button)
        row.addWidget(self.fast_button)
        self.set_mode(self._mode)

    @property
    def mode(self) -> SLMControlMode:
        return self._mode

    def set_mode(self,mode) -> None:
        mode = SLMControlMode.normalize(mode)
        self._mode = mode
        for button,button_mode in (
            (self.editor_button,SLMControlMode.EDITOR),
            (self.fast_button,SLMControlMode.FAST_CONFIG),
        ):
            blocker = QtCore.QSignalBlocker(button)
            try:
                button.setChecked(mode is button_mode)
            finally:
                del blocker

    def set_mode_change_enabled(self,enabled: bool,reason: str="") -> None:
        enabled = bool(enabled)
        self.editor_button.setEnabled(enabled)
        self.fast_button.setEnabled(enabled)
        tooltip = "" if enabled else str(reason or "Control mode change is unavailable.")
        self.editor_button.setToolTip(tooltip)
        self.fast_button.setToolTip(tooltip)

    def _request(self,mode: SLMControlMode) -> None:
        # Selection is authoritative only after the owning session/group accepts
        # the transition, so restore the current visual state before emitting.
        self.set_mode(self._mode)
        if mode is not self._mode:
            self.sigModeRequested.emit(mode)
