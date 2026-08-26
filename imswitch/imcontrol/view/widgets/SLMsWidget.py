from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from qtpy import QtCore,QtWidgets

from slmcore.qt import SLMControlMode,SLMControlModeSelector,SLMPanel
from .basewidgets import Widget


@dataclass
class SlmUi:
    slm_name: str
    panel: SLMPanel


class SLMsWidget(Widget):
    """Thin ImSwitch multi-SLM shell around reusable :class:`SLMPanel`."""

    sigControlModeRequested = QtCore.Signal(object)

    def __init__(self,*args: Any,**kwargs: Any) -> None:
        super().__init__(*args,**kwargs)
        self._slms: dict[str,SlmUi] = {}

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(3,3,3,3)
        layout.setSpacing(4)
        self.slm_tabs = QtWidgets.QTabWidget()
        self.slm_tabs.setMovable(True)
        self.control_mode_selector = SLMControlModeSelector(self.slm_tabs)
        self.control_mode_selector.sigModeRequested.connect(
            self.sigControlModeRequested.emit,
        )
        self.slm_tabs.setCornerWidget(
            self.control_mode_selector,QtCore.Qt.TopRightCorner,
        )
        layout.addWidget(self.slm_tabs,1)

    def add_slm(
        self,*,slm_key: str,slm_name: str,panel: SLMPanel,
    ) -> None:
        if slm_key in self._slms:
            raise KeyError(f"SLM {slm_key!r} is already registered")
        if not isinstance(panel,SLMPanel):
            raise TypeError("panel must be an SLMPanel")
        self._slms[slm_key] = SlmUi(slm_name=slm_name,panel=panel)
        self.slm_tabs.addTab(panel,slm_name)

    def get_panel(self,slm_key: str) -> SLMPanel:
        try:
            return self._slms[slm_key].panel
        except KeyError as error:
            raise KeyError(f"Unknown SLM {slm_key!r}") from error

    def remove_slm(self,slm_key: str) -> None:
        """Unmount one SLM panel without owning its disposal lifecycle."""
        try:
            slm_ui = self._slms.pop(slm_key)
        except KeyError:
            return
        index = self.slm_tabs.indexOf(slm_ui.panel)
        if index >= 0:
            self.slm_tabs.removeTab(index)

    def set_control_mode(self,mode: Any) -> None:
        mode = SLMControlMode.normalize(mode)
        self.control_mode_selector.set_mode(mode)

    def set_control_mode_change_enabled(self,enabled: bool) -> None:
        self.control_mode_selector.set_mode_change_enabled(
            enabled,
            "Wait for CGH computation or automatic feedback to finish before changing control mode.",
        )

    def show_error(self,title: str,message: Any) -> None:
        QtWidgets.QMessageBox.critical(self,str(title),str(message))

    def show_warning(self,title: str,message: Any) -> None:
        QtWidgets.QMessageBox.warning(self,str(title),str(message))

    def show_info(self,title: str,message: Any) -> None:
        QtWidgets.QMessageBox.information(self,str(title),str(message))
