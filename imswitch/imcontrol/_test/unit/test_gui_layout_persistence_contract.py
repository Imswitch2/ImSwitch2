from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
MAIN_VIEW_PATH = ROOT / 'imswitch' / 'imcontrol' / 'view' / 'ImConMainView.py'
MAIN_CONTROLLER_PATH = (
    ROOT / 'imswitch' / 'imcontrol' / 'controller' / 'ImConMainController.py'
)


def test_imcon_main_view_exposes_persistable_dock_layout_state():
    source = MAIN_VIEW_PATH.read_text()

    assert 'self.dockArea = DockArea()' in source
    assert "return {\n            'dock_area': self.dockArea.saveState()," in source
    assert "dockAreaState = state.get('dock_area')" in source
    assert (
        "self.dockArea.restoreState(dockAreaState, missing='ignore', extra='bottom')"
        in source
    )
    assert "self.__logger.warning(f'Failed to restore GUI dock layout: {e}')" in source


def test_imcon_main_controller_registers_gui_layout_persistence_adapter():
    source = MAIN_CONTROLLER_PATH.read_text()

    assert 'class _GuiLayoutStateAdapter:' in source
    assert 'self.__guiLayoutStateAdapter = _GuiLayoutStateAdapter(self.__mainView)' in source
    assert "getWidgetStatePersistence().register('GuiLayout', self.__guiLayoutStateAdapter)" in source
    assert 'def getWidgetState(self) -> Dict[str, Any]:' in source
    assert 'return self._view.getLayoutState()' in source
    assert 'def setWidgetState(self, state: Dict[str, Any]) -> None:' in source
    assert 'self._view.setLayoutState(state)' in source
