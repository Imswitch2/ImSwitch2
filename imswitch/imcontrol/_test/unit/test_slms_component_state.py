"""Unit tests for the slmcore-backed SLMsController state contract."""

import json
from pathlib import Path
from unittest.mock import Mock

from imswitch.imcontrol.controller.basecontrollers import ComponentStateApplyMode
from imswitch.imcontrol.controller.controllers.SLMsController import SLMsController
from slmcore.qt import SLMControlMode


class _Repository:
    def __init__(self,directory):
        self.directory = Path(directory)

    def resolve(self,path_or_name):
        path = Path(path_or_name)
        if path.is_absolute():
            return path
        return self.directory / path


def _make_session(*,mode=SLMControlMode.EDITOR,current_path=None,fast_path=None,repository=None):
    session = Mock()
    session.control_mode = mode
    session.current_config_path = str(current_path) if current_path else None
    session.fast_config_path = str(fast_path) if fast_path else None
    session.config_repository = repository
    session.dispose = Mock()

    def load_config(path,**_kwargs):
        session.current_config_path = str(path)
        return True

    def activate_compiled_config(path):
        session.fast_config_path = str(path)
        return True

    session.load_config = Mock(side_effect=load_config)
    session.activate_compiled_config = Mock(side_effect=activate_compiled_config)
    return session


def _make_controller(sessions):
    controller = Mock(spec=SLMsController)
    controller._slm_names = {
        key:"SLM_" + key.title()
        for key in sessions
    }
    controller._slm_qt_sessions = dict(sessions)
    controller._slm_qt_group = Mock()
    controller._slm_qt_group.set_control_mode = Mock(return_value=True)
    controller._slm_qt_group.remove_session = Mock()
    controller._SLMsController__logger = Mock()

    controller._slm_qt_session = lambda key: SLMsController._slm_qt_session(
        controller,key,
    )
    controller._resolve_setup_mode_config_path = (
        lambda slm_key,slm_state:
            SLMsController._resolve_setup_mode_config_path(
                controller,slm_key,slm_state,
            )
    )
    controller._saved_control_mode = lambda slm_state: (
        SLMsController._saved_control_mode(slm_state)
    )
    controller._apply_setup_mode_state_to_sessions = lambda state: (
        SLMsController._apply_setup_mode_state_to_sessions(controller,state)
    )
    controller._fmt = lambda value: SLMsController._fmt(controller,value)

    controller.getComponentState = lambda: SLMsController.getComponentState(
        controller,
    )
    controller.applyComponentState = lambda state,applyMode: (
        SLMsController.applyComponentState(
            controller,state,applyMode=applyMode,
        )
    )
    controller.describeComponentState = lambda state: (
        SLMsController.describeComponentState(controller,state)
    )
    controller.getComponentStateHazards = lambda state,applyMode,context=None: (
        SLMsController.getComponentStateHazards(
            controller,state,applyMode=applyMode,context=context,
        )
    )
    controller._dispose_slm_sessions = lambda: (
        SLMsController._dispose_slm_sessions(controller)
    )
    return controller


def test_getComponentState_returns_slmcore_config_references(tmp_path):
    editor_path = tmp_path / "editor_config.h5"
    fast_path = tmp_path / "fast_config.h5"
    editor_path.write_bytes(b"editor")
    fast_path.write_bytes(b"fast")
    controller = _make_controller({
        "left":_make_session(
            mode=SLMControlMode.EDITOR,
            current_path=editor_path,
        ),
        "right":_make_session(
            mode=SLMControlMode.FAST_CONFIG,
            fast_path=fast_path,
        ),
    })

    state = controller.getComponentState()

    assert state["slms"]["left"]["configPath"] == str(editor_path)
    assert state["slms"]["left"]["configName"] == "editor_config.h5"
    assert state["slms"]["left"]["controlMode"] == "editor"
    assert state["slms"]["right"]["configPath"] == str(fast_path)
    assert state["slms"]["right"]["configName"] == "fast_config.h5"
    assert state["slms"]["right"]["controlMode"] == "fast_config"
    assert "cgh_pattern" not in str(state)
    json.dumps(state)


def test_STARTUP_RESTORE_is_a_pure_noop(tmp_path):
    config_path = tmp_path / "saved_config.h5"
    config_path.write_bytes(b"config")
    session = _make_session()
    controller = _make_controller({"left":session})

    warnings = controller.applyComponentState(
        {"slms":{"left":{"configPath":str(config_path),"controlMode":"editor"}}},
        applyMode=ComponentStateApplyMode.STARTUP_RESTORE,
    )

    assert warnings == []
    session.load_config.assert_not_called()
    session.activate_compiled_config.assert_not_called()
    controller._slm_qt_group.set_control_mode.assert_not_called()
    assert session.current_config_path is None


def test_SETUP_MODE_APPLY_loads_editor_config(tmp_path):
    config_path = tmp_path / "editor_config.h5"
    config_path.write_bytes(b"config")
    session = _make_session(mode=SLMControlMode.EDITOR)
    controller = _make_controller({"left":session})

    warnings = controller.applyComponentState(
        {
            "slms":{
                "left":{
                    "slmName":"SLM_Left",
                    "configPath":str(config_path),
                    "configName":config_path.name,
                    "controlMode":"editor",
                }
            }
        },
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
    )

    assert warnings == []
    session.load_config.assert_called_once_with(
        str(config_path),
        confirm_layout_change=False,
        calibration_mismatch_policy="reject",
        show_error=False,
    )
    session.activate_compiled_config.assert_not_called()
    assert session.current_config_path == str(config_path)


def test_SETUP_MODE_APPLY_can_switch_and_activate_fast_config(tmp_path):
    config_path = tmp_path / "fast_config.h5"
    config_path.write_bytes(b"config")
    session = _make_session(mode=SLMControlMode.EDITOR)
    controller = _make_controller({"left":session})

    def set_control_mode(mode):
        session.control_mode = mode
        return True

    controller._slm_qt_group.set_control_mode.side_effect = set_control_mode

    warnings = controller.applyComponentState(
        {
            "slms":{
                "left":{
                    "configPath":str(config_path),
                    "configName":config_path.name,
                    "controlMode":"fast_config",
                }
            }
        },
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
    )

    assert warnings == []
    controller._slm_qt_group.set_control_mode.assert_called_once_with(
        SLMControlMode.FAST_CONFIG,
    )
    session.activate_compiled_config.assert_called_once_with(str(config_path))
    session.load_config.assert_not_called()
    assert session.fast_config_path == str(config_path)


def test_SETUP_MODE_APPLY_resolves_config_name_from_repository(tmp_path):
    config_path = tmp_path / "named_config.h5"
    config_path.write_bytes(b"config")
    session = _make_session(repository=_Repository(tmp_path))
    controller = _make_controller({"left":session})

    warnings = controller.applyComponentState(
        {"slms":{"left":{"configName":"named_config.h5","controlMode":"editor"}}},
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
    )

    assert warnings == []
    session.load_config.assert_called_once()
    assert session.current_config_path == str(config_path)


def test_SETUP_MODE_APPLY_warns_for_missing_slm():
    controller = _make_controller({"left":_make_session()})

    warnings = controller.applyComponentState(
        {
            "slms":{
                "right":{
                    "slmName":"SLM_Right",
                    "configPath":"missing.h5",
                }
            }
        },
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
    )

    assert len(warnings) == 1
    assert "SLM_Right" in warnings[0]
    assert "is not available" in warnings[0]


def test_SETUP_MODE_APPLY_warns_for_missing_file(tmp_path):
    session = _make_session()
    controller = _make_controller({"left":session})

    warnings = controller.applyComponentState(
        {
            "slms":{
                "left":{
                    "configPath":str(tmp_path / "missing.h5"),
                    "configName":"missing.h5",
                    "controlMode":"editor",
                }
            }
        },
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
    )

    assert len(warnings) == 1
    assert "missing.h5" in warnings[0]
    session.load_config.assert_not_called()


def test_SETUP_MODE_APPLY_warns_for_invalid_control_mode(tmp_path):
    config_path = tmp_path / "config.h5"
    config_path.write_bytes(b"config")
    session = _make_session()
    controller = _make_controller({"left":session})

    warnings = controller.applyComponentState(
        {"slms":{"left":{"configPath":str(config_path),"controlMode":"banana"}}},
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
    )

    assert len(warnings) == 1
    assert "saved control mode is invalid" in warnings[0]
    session.load_config.assert_not_called()


def test_describeComponentState_summarizes_configs():
    controller = _make_controller({"left":_make_session()})

    summary = controller.describeComponentState({
        "slms":{
            "left":{
                "slmName":"SLM_Left",
                "configName":"config.h5",
                "controlMode":"editor",
            }
        }
    })

    assert summary == ["  SLM_Left: config.h5 (editor)"]
    assert controller.describeComponentState({}) == ["  no SLM state"]


def test_getComponentStateHazards_returns_empty_list():
    controller = _make_controller({"left":_make_session()})

    hazards = controller.getComponentStateHazards(
        {"slms":{}},
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
        context={},
    )

    assert hazards == []


def test_component_contract_metadata():
    assert SLMsController.componentName == "SLMs"
    assert SLMsController.stateSchemaVersion == 1
    assert SLMsController.legacyStateNames == ()


def test_dispose_slm_sessions_removes_group_members_and_disposes():
    left = _make_session()
    right = _make_session()
    controller = _make_controller({"left":left,"right":right})

    controller._dispose_slm_sessions()

    assert controller._slm_qt_group.remove_session.call_count == 2
    left.dispose.assert_called_once()
    right.dispose.assert_called_once()
    assert controller._slm_qt_sessions == {}
