"""Contract tests for the runtime-loaded-tool persistence path.

Verifies the new layout-state contract: getLayoutState includes the list
of runtime-loaded analysis tool ids, and setLayoutState re-registers the
matching processors before recreating the widget docks. Most checks use small
stubs; the view method checks call unbound methods without constructing Qt
widgets.
"""

from __future__ import annotations

from types import SimpleNamespace

from imswitch.improcess.controller.ImProcessMainController import (
    _GuiLayoutStateAdapter,
)
from imswitch.improcess.view.ImProcessMainView import ImProcessMainView


def _make_view_stub():
    """View stub that mimics the relevant pieces of ImProcessMainView."""
    calls = []

    def get_layout_state():
        return {
            'dock_area': {'fake': 'state'},
            'runtime_analysis_tool_ids': ['psf-resolution', 'roi-manager'],
        }

    def set_layout_state(state):
        calls.append(('setLayoutState', state))

    view = SimpleNamespace(
        getLayoutState=get_layout_state,
        setLayoutState=set_layout_state,
        _calls=calls,
    )
    return view


def _make_controller_stub():
    """Controller stub that records every restored / refreshed tool id."""

    class _Ctl:
        def __init__(self):
            self.restored = []
            self.refresh_calls = 0

        def _restore_runtime_processor(self, tool_id):
            self.restored.append(tool_id)

        def _refresh_runtime_processor_choices(self):
            self.refresh_calls += 1

    return _Ctl()


def test_get_widget_state_passes_through_layout_state():
    view = _make_view_stub()
    adapter = _GuiLayoutStateAdapter(view)

    state = adapter.getWidgetState()

    assert state['dock_area'] == {'fake': 'state'}
    assert state['runtime_analysis_tool_ids'] == ['psf-resolution', 'roi-manager']


def test_set_widget_state_restores_runtime_tools_before_layout():
    view = _make_view_stub()
    controller = _make_controller_stub()
    adapter = _GuiLayoutStateAdapter(view, main_controller=controller)

    saved_state = {
        'dock_area': {'fake': 'state'},
        'runtime_analysis_tool_ids': ['psf-resolution', 'roi-manager'],
    }
    adapter.setWidgetState(saved_state)

    # Controller restored both tools, then refreshed the choice list once.
    assert controller.restored == ['psf-resolution', 'roi-manager']
    assert controller.refresh_calls == 1

    # And THEN the layout state went to the view (not before).
    assert view._calls == [('setLayoutState', saved_state)]


def test_set_widget_state_tolerates_missing_runtime_tool_ids():
    view = _make_view_stub()
    controller = _make_controller_stub()
    adapter = _GuiLayoutStateAdapter(view, main_controller=controller)

    adapter.setWidgetState({'dock_area': {'fake': 'state'}})

    assert controller.restored == []
    # Refresh runs unconditionally so the combo state is always consistent.
    assert controller.refresh_calls == 1
    assert view._calls == [('setLayoutState', {'dock_area': {'fake': 'state'}})]


def test_set_widget_state_swallows_restore_exceptions():
    view = _make_view_stub()

    class _Ctl:
        def __init__(self):
            self.restored = []
            self.refresh_calls = 0

        def _restore_runtime_processor(self, tool_id):
            self.restored.append(tool_id)
            raise RuntimeError(f'simulated failure for {tool_id}')

        def _refresh_runtime_processor_choices(self):
            self.refresh_calls += 1

    controller = _Ctl()
    adapter = _GuiLayoutStateAdapter(view, main_controller=controller)

    adapter.setWidgetState({
        'dock_area': {'fake': 'state'},
        'runtime_analysis_tool_ids': ['psf-resolution', 'roi-manager'],
    })

    # Both attempts ran; refresh + setLayoutState still happened.
    assert controller.restored == ['psf-resolution', 'roi-manager']
    assert controller.refresh_calls == 1
    assert view._calls == [
        ('setLayoutState', {
            'dock_area': {'fake': 'state'},
            'runtime_analysis_tool_ids': ['psf-resolution', 'roi-manager'],
        })
    ]


def test_schema_version_bumped():
    """Bump signals to the persistence service that older saved states may
    be missing the runtime_analysis_tool_ids key."""
    adapter = _GuiLayoutStateAdapter(_make_view_stub())
    assert adapter.getStateSchemaVersion() == 2


def test_startup_hidden_docks_are_rehidden_after_layout_restore():
    class _Dock:
        def __init__(self):
            self.hidden = False

        def hide(self):
            self.hidden = True

    parameters = _Dock()
    actions = _Dock()
    current_data = _Dock()
    reconstruction = _Dock()
    results = _Dock()
    view = SimpleNamespace(
        docks={
            'Parameters': parameters,
            'Actions': actions,
            'Current data': current_data,
            'Reconstruction': reconstruction,
            'Results': results,
        },
        _defaultDockVisibility={
            'Parameters': False,
            'Actions': False,
            'Current data': False,
            'Reconstruction': False,
            'Results': False,
        },
    )

    ImProcessMainView._applyStartupHiddenDocks(view)

    assert parameters.hidden
    assert actions.hidden
    assert current_data.hidden
    assert reconstruction.hidden
    assert results.hidden


def test_auto_reveal_reconstruction_dock_runs_once():
    raised = []
    view = SimpleNamespace(
        _autoRevealReconstructionDock=True,
        raiseDockByTitle=lambda title: raised.append(title),
    )

    ImProcessMainView._maybeAutoRevealReconstructionDock(view)
    ImProcessMainView._maybeAutoRevealReconstructionDock(view)

    assert raised == ['Reconstruction']
    assert view._autoRevealReconstructionDock is False
