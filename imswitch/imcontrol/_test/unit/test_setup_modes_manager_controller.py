from unittest.mock import Mock

import pytest

from imswitch.imcontrol.controller.controllers.SetupModesController import SetupModesController


pytestmark = pytest.mark.nohardware


def _make_controller(widget, backend):
    controller = SetupModesController.__new__(SetupModesController)
    controller._widget = widget
    controller._setupModeController = backend
    controller._logger = Mock()
    controller._activeModeName = None
    controller.refreshModes = Mock()
    controller._refreshModeManager = Mock()
    controller._ensureShortcutAvailable = Mock(return_value=True)
    controller._modeExists = lambda name: name in backend.listSetupModes()
    return controller


def test_manager_rename_targets_manager_mode_without_changing_main_selection():
    widget = Mock()
    widget.getSelectedModeName.return_value = "Main active"
    widget.showNameDialog.return_value = "Renamed"
    backend = Mock()
    backend.listSetupModes.return_value = ["Main active", "Other"]

    controller = _make_controller(widget, backend)
    controller.renameModeByName("Other")

    backend.renameSetupMode.assert_called_once_with("Other", "Renamed")
    controller.refreshModes.assert_called_once_with("Main active")
    controller._refreshModeManager.assert_called_once_with("Renamed")


def test_manager_duplicate_targets_manager_mode_and_selects_copy_in_manager():
    widget = Mock()
    widget.getSelectedModeName.return_value = "Main active"
    widget.showNameDialog.return_value = "Other copy"
    backend = Mock()
    backend.listSetupModes.return_value = ["Main active", "Other"]

    controller = _make_controller(widget, backend)
    controller._uniqueCopyName = Mock(return_value="Other copy")
    controller.duplicateModeByName("Other")

    backend.duplicateSetupMode.assert_called_once_with(
        "Other", "Other copy", shortcut=""
    )
    controller.refreshModes.assert_called_once_with("Main active")
    controller._refreshModeManager.assert_called_once_with("Other copy")


def test_components_editor_only_changes_membership_without_snapshotting():
    widget = Mock()
    widget.getSelectedModeName.return_value = "Main active"
    widget.showModeComponentsDialog.return_value = ["Scan"]
    backend = Mock()
    backend.getSetupMode.return_value = {
        "name": "Other",
        "includedComponents": ["Scan", "Laser"],
        "state": {
            "Scan": {"step": 1},
            "Laser": {"power": 20},
        },
    }

    controller = _make_controller(widget, backend)
    controller._componentLabel = lambda name: name
    controller.editModeComponents("Other")

    widget.showModeComponentsDialog.assert_called_once_with(
        "Other",
        [
            {"name": "Scan", "label": "Scan"},
            {"name": "Laser", "label": "Laser"},
        ],
        ["Scan", "Laser"],
    )
    backend.setSetupModeIncludedComponents.assert_called_once_with(
        "Other", ["Scan"]
    )
    assert not backend.snapshotSetupModeState.called
    assert not backend.snapshotSetupModeRuntimeState.called
    controller.refreshModes.assert_called_once_with("Main active")
    controller._refreshModeManager.assert_called_once_with("Other")


def test_manager_load_uses_normal_apply_path():
    widget = Mock()
    backend = Mock()
    controller = _make_controller(widget, backend)
    controller._applyMode = Mock(return_value=True)

    controller.loadModeFromManager("Other")

    controller._applyMode.assert_called_once_with("Other", source="manager")


def test_manager_delete_preserves_unrelated_main_selection():
    widget = Mock()
    widget.getSelectedModeName.return_value = "Main active"
    widget.askDeleteMode.return_value = True
    backend = Mock()

    controller = _make_controller(widget, backend)
    controller.deleteModeByName("Other")

    backend.deleteSetupMode.assert_called_once_with("Other")
    controller.refreshModes.assert_called_once_with("Main active")
    controller._refreshModeManager.assert_called_once_with(None)


def test_manager_shortcut_targets_manager_mode_without_changing_main_selection():
    widget = Mock()
    widget.getSelectedModeName.return_value = "Main active"
    widget.showShortcutDialog.return_value = "F8"
    backend = Mock()
    backend.listSetupModes.return_value = ["Main active", "Other"]
    backend.getSetupMode.return_value = {"name": "Other", "shortcut": ""}

    controller = _make_controller(widget, backend)
    controller.setShortcutByName("Other")

    widget.showShortcutDialog.assert_called_once_with("Other", "")
    backend.updateSetupModeMetadata.assert_called_once_with("Other", shortcut="F8")
    controller.refreshModes.assert_called_once_with("Main active")
    controller._refreshModeManager.assert_called_once_with("Other")


def test_manager_description_targets_manager_mode_without_changing_main_selection():
    widget = Mock()
    widget.getSelectedModeName.return_value = "Main active"
    widget.showDescriptionDialog.return_value = "Updated description"
    backend = Mock()
    backend.getSetupMode.return_value = {
        "name": "Other",
        "description": "Old description",
    }

    controller = _make_controller(widget, backend)
    controller.editDescriptionByName("Other")

    widget.showDescriptionDialog.assert_called_once_with(
        "Other", "Old description"
    )
    backend.updateSetupModeMetadata.assert_called_once_with(
        "Other", description="Updated description"
    )
    controller.refreshModes.assert_called_once_with("Main active")
    controller._refreshModeManager.assert_called_once_with("Other")


def test_selective_update_preserves_dormant_component_payload():
    widget = Mock()
    widget.getSelectedModeName.return_value = "Mode"
    widget.showUpdateModeDialog.return_value = {
        "components": ["Scan"],
        "selectedParts": {},
        "captureOptions": {},
    }
    backend = Mock()
    saved_mode = {
        "name": "Mode",
        "includedComponents": ["Scan"],
        "state": {
            "Scan": {"step": 1},
            "Laser": {"power": 80},
        },
    }
    backend.listSetupModes.return_value = ["Mode"]
    backend.getSetupMode.return_value = saved_mode
    backend.snapshotSetupModeRuntimeState.return_value = {
        "state": {"Scan": {"step": 2}},
        "warnings": [],
    }
    backend.getSetupModeUpdateCaptureOptions.return_value = None
    backend.prepareSetupModeStateSnapshot.return_value = {
        "state": {"Scan": {"step": 2}},
        "warnings": [],
    }
    backend.mergeSetupModeComponentState.return_value = {"step": 2}
    backend.saveSetupModeFromState.return_value = {"mode": {}, "warnings": []}

    controller = _make_controller(widget, backend)
    controller._buildUpdateSummaries = Mock(return_value=[])
    controller._showWarnings = Mock()
    controller.updateCurrentMode()

    backend.snapshotSetupModeRuntimeState.assert_called_once_with(["Scan"])
    saved_args = backend.saveSetupModeFromState.call_args
    assert saved_args.args[0] == "Mode"
    assert saved_args.args[1] == {
        "Scan": {"step": 2},
        "Laser": {"power": 80},
    }
    assert saved_args.kwargs["includedComponents"] == ["Scan"]


def test_manager_details_label_inactive_state_and_show_its_summary():
    widget = Mock()
    backend = Mock()
    backend.listSetupModes.return_value = ["Mode"]
    backend.getSetupModeComponentLabel.side_effect = lambda name: {
        "Scan": "Scan",
        "Laser": "Lasers",
    }.get(name, name)
    backend.describeModeComponent.side_effect = lambda name, state: [str(state)]

    controller = _make_controller(widget, backend)
    controller._summarizeSmartModeRoles = Mock(return_value=[])
    details = controller._makeInspectModeDetails({
        "name": "Mode",
        "description": "",
        "shortcut": None,
        "includedComponents": ["Scan"],
        "state": {
            "Scan": {"step": 1},
            "Laser": {"power": 80},
        },
    })

    assert details["includedComponentLabels"] == ["Scan"]
    assert details["inactiveComponentLabels"] == ["Lasers"]
    assert details["stateSummary"] == [
        "Scan:",
        "  {'step': 1}",
        "",
        "Lasers (inactive):",
        "  {'power': 80}",
    ]
