"""Minimal multi-SLM shell around reusable ``SLMPanel`` instances."""

from __future__ import annotations

from typing import Any

from qtpy import QtCore,QtWidgets

from slmcore.qt import SLMControlMode,SLMControlModeSelector,SLMPanel


class DemoMainWindow(QtWidgets.QMainWindow):
    """Thin host window; all per-SLM workflow UI remains inside slmcore."""

    sigControlModeRequested = QtCore.Signal(object)
    sigWorkspaceRequested = QtCore.Signal()

    def __init__(self) -> None:
        super().__init__()
        self._panels: dict[str,SLMPanel] = {}
        self.setWindowTitle("slmcore standalone demo")
        self.resize(1200,850)

        central = QtWidgets.QWidget(self)
        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(3,3,3,3)
        layout.setSpacing(4)

        self.slm_tabs = QtWidgets.QTabWidget(central)
        self.slm_tabs.setMovable(True)
        corner = QtWidgets.QWidget(self.slm_tabs)
        corner_layout = QtWidgets.QHBoxLayout(corner)
        corner_layout.setContentsMargins(0,0,0,0)
        corner_layout.setSpacing(6)
        self.workspace_button = QtWidgets.QPushButton("Workspace…",corner)
        self.workspace_button.clicked.connect(self.sigWorkspaceRequested.emit)
        corner_layout.addWidget(self.workspace_button)
        self.control_mode_selector = SLMControlModeSelector(corner)
        self.control_mode_selector.sigModeRequested.connect(
            self.sigControlModeRequested.emit,
        )
        corner_layout.addWidget(self.control_mode_selector)
        self.slm_tabs.setCornerWidget(corner,QtCore.Qt.TopRightCorner)
        layout.addWidget(self.slm_tabs,1)
        self.setCentralWidget(central)

    def add_slm(self,*,slm_key: str,display_name: str,panel: SLMPanel) -> None:
        if slm_key in self._panels:
            raise KeyError("SLM %r is already registered" % slm_key)
        if not isinstance(panel,SLMPanel):
            raise TypeError("panel must be an SLMPanel")
        self._panels[slm_key] = panel
        self.slm_tabs.addTab(panel,display_name)

    def remove_slm(self,slm_key: str) -> None:
        panel = self._panels.pop(slm_key,None)
        if panel is None:
            return
        index = self.slm_tabs.indexOf(panel)
        if index >= 0:
            self.slm_tabs.removeTab(index)

    def current_slm_key(self) -> str | None:
        current=self.slm_tabs.currentWidget()
        for key,panel in self._panels.items():
            if panel is current:
                return key
        return None

    def set_control_mode(self,mode: Any) -> None:
        self.control_mode_selector.set_mode(SLMControlMode.normalize(mode))

    def set_control_mode_change_enabled(self,enabled: bool) -> None:
        self.control_mode_selector.set_mode_change_enabled(
            bool(enabled),
            "Wait for CGH computation or automatic feedback to finish "
            "before changing control mode.",
        )

    def show_error(self,title: str,message: Any) -> None:
        QtWidgets.QMessageBox.critical(self,str(title),str(message))

    def show_warning(self,title: str,message: Any) -> None:
        QtWidgets.QMessageBox.warning(self,str(title),str(message))

    def show_info(self,title: str,message: Any) -> None:
        QtWidgets.QMessageBox.information(self,str(title),str(message))
