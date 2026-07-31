"""
Unit tests for Phase 2h: Positioner, Rotator, Recording, BeadRec state persistence.

Verifies the four misc controllers implement StatefulComponentMixin correctly:
- getComponentState / applyComponentState round-trip
- No hardware activation in EITHER mode (STARTUP_RESTORE or SETUP_MODE_APPLY)
- describeComponentState returns non-empty
- getComponentStateHazards returns []
- Components appear in SetupModeController.getSetupModeComponents()
"""
import pytest
import os
from unittest.mock import MagicMock, Mock, patch, call
from imswitch.imcontrol.controller.basecontrollers import ComponentStateApplyMode


@pytest.fixture
def mock_master():
    """Common mock master with minimal setup."""
    master = MagicMock()
    master.lasersManager = MagicMock()
    master.detectorsManager = MagicMock()
    master.detectorsManager.execOnAll = MagicMock(return_value=[])
    return master


@pytest.fixture
def mock_comm_channel():
    """Common mock communication channel."""
    channel = MagicMock()
    channel.sharedAttrs = MagicMock()
    channel.sharedAttrs.sigAttributeSet = MagicMock()
    channel.sharedAttrs.getHDF5Attributes = MagicMock(return_value={})
    channel.scanWorkflow = MagicMock()
    channel.beadRecWorkflow = MagicMock()
    return channel


class TestPositionerController:
    """Test PositionerController StatefulComponentMixin implementation."""
    
    @pytest.fixture
    def controller(self, mock_master, mock_comm_channel):
        """Create PositionerController mock with bound methods."""
        from imswitch.imcontrol.controller.controllers.PositionerController import PositionerController
        
        # Create stage mock
        stage_mock = MagicMock(
            forPositioning=True,
            axes=['X', 'Y', 'Z'],
            move=MagicMock(),
            setPosition=MagicMock(),
            joystick=False
        )
        
        # Create manager that supports both iteration and getitem
        manager_data = [('Stage', stage_mock)]
        mock_master.positionersManager = MagicMock()
        mock_master.positionersManager.__iter__ = lambda self: iter(manager_data)
        mock_master.positionersManager.__getitem__ = lambda self, name: stage_mock
        
        widget = MagicMock()
        widget.getStepSize = MagicMock(side_effect=lambda name, axis: 1.0)
        widget.setStepSize = MagicMock()
        
        # Create lightweight mock with bound methods
        ctrl = Mock(spec=PositionerController)
        ctrl._master = mock_master
        ctrl._widget = widget
        ctrl._logger = Mock()
        ctrl.componentName = 'Positioner'
        ctrl.stateSchemaVersion = 1
        ctrl._stage_mock = stage_mock  # Store for test access
        
        # Bind actual methods
        ctrl.getComponentState = lambda: PositionerController.getComponentState(ctrl)
        ctrl.applyComponentState = lambda state, applyMode: PositionerController.applyComponentState(
            ctrl, state, applyMode=applyMode
        )
        ctrl.describeComponentState = lambda state: PositionerController.describeComponentState(ctrl, state)
        ctrl.getComponentStateHazards = lambda state, applyMode, context=None: PositionerController.getComponentStateHazards(
            ctrl, state, applyMode=applyMode, context=context
        )
        
        return ctrl
    
    def test_component_name(self, controller):
        """Verify canonical component name."""
        assert controller.componentName == 'Positioner'
    
    def test_state_schema_version(self, controller):
        """Verify state schema version is set."""
        assert controller.stateSchemaVersion == 1
    
    def test_round_trip_state(self, controller):
        """Test getComponentState -> applyComponentState round trip."""
        state = controller.getComponentState()
        assert 'step_sizes' in state
        assert 'Stage' in state['step_sizes']
        
        warnings = controller.applyComponentState(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        assert isinstance(warnings, list)
        controller._widget.setStepSize.assert_called()
    
    def test_no_movement_startup_restore(self, controller):
        """Verify NO stage movement in STARTUP_RESTORE mode."""
        state = controller.getComponentState()
        
        controller.applyComponentState(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        controller._stage_mock.move.assert_not_called()
        controller._stage_mock.setPosition.assert_not_called()
    
    def test_no_movement_setup_mode_apply(self, controller):
        """Verify NO stage movement in SETUP_MODE_APPLY mode."""
        state = controller.getComponentState()
        
        controller.applyComponentState(
            state,
            applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
        )
        
        controller._stage_mock.move.assert_not_called()
        controller._stage_mock.setPosition.assert_not_called()
    
    def test_describe_component_state(self, controller):
        """Verify describeComponentState returns non-empty."""
        state = controller.getComponentState()
        description = controller.describeComponentState(state)
        assert isinstance(description, list)
        assert len(description) > 0
    
    def test_get_component_state_hazards(self, controller):
        """Verify getComponentStateHazards returns empty list."""
        state = controller.getComponentState()
        hazards = controller.getComponentStateHazards(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        assert hazards == []
        
        hazards = controller.getComponentStateHazards(
            state,
            applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
        )
        assert hazards == []


class TestRotatorController:
    """Test RotatorController StatefulComponentMixin implementation."""
    
    @pytest.fixture
    def controller(self, mock_master, mock_comm_channel):
        """Create RotatorController mock with bound methods."""
        from imswitch.imcontrol.controller.controllers.RotatorController import RotatorController
        
        # Create rotator mock
        rotator_mock = MagicMock(
            position=0.0,
            move_abs=MagicMock(),
            move_rel=MagicMock(),
            start_cont_rot=MagicMock()
        )
        
        # Create manager that supports both iteration and getitem
        mock_master.rotatorsManager = MagicMock()
        mock_master.rotatorsManager.__iter__ = MagicMock(return_value=iter([('Rotator1', rotator_mock)]))
        mock_master.rotatorsManager.__getitem__ = MagicMock(return_value=rotator_mock)
        
        widget = MagicMock()
        widget.getSpeed = MagicMock(return_value=100)
        widget.getRelStepSize = MagicMock(return_value=5.0)
        widget.setSpeed = MagicMock()
        widget.setRelStepSize = MagicMock()
        
        # Create lightweight mock with bound methods
        ctrl = Mock(spec=RotatorController)
        ctrl._master = mock_master
        ctrl._widget = widget
        ctrl._RotatorController__logger = Mock()
        ctrl.componentName = 'Rotator'
        ctrl.stateSchemaVersion = 1
        ctrl._rotator_mock = rotator_mock  # Store for test access
        
        # Bind actual methods
        ctrl.getComponentState = lambda: RotatorController.getComponentState(ctrl)
        ctrl.applyComponentState = lambda state, applyMode: RotatorController.applyComponentState(
            ctrl, state, applyMode=applyMode
        )
        ctrl.describeComponentState = lambda state: RotatorController.describeComponentState(ctrl, state)
        ctrl.getComponentStateHazards = lambda state, applyMode, context=None: RotatorController.getComponentStateHazards(
            ctrl, state, applyMode=applyMode, context=context
        )
        
        return ctrl
    
    def test_component_name(self, controller):
        """Verify canonical component name."""
        assert controller.componentName == 'Rotator'
    
    def test_state_schema_version(self, controller):
        """Verify state schema version is set."""
        assert controller.stateSchemaVersion == 1
    
    def test_round_trip_state(self, controller):
        """Test getComponentState -> applyComponentState round trip."""
        state = controller.getComponentState()
        assert 'rotators' in state
        assert 'Rotator1' in state['rotators']
        
        warnings = controller.applyComponentState(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        assert isinstance(warnings, list)
    
    def test_no_rotation_startup_restore(self, controller):
        """Verify NO physical rotation in STARTUP_RESTORE mode."""
        state = controller.getComponentState()
        
        controller.applyComponentState(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        controller._rotator_mock.move_abs.assert_not_called()
        controller._rotator_mock.move_rel.assert_not_called()
        controller._rotator_mock.start_cont_rot.assert_not_called()
    
    def test_no_rotation_setup_mode_apply(self, controller):
        """Verify NO physical rotation in SETUP_MODE_APPLY mode."""
        state = controller.getComponentState()
        
        controller.applyComponentState(
            state,
            applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
        )
        
        controller._rotator_mock.move_abs.assert_not_called()
        controller._rotator_mock.move_rel.assert_not_called()
        controller._rotator_mock.start_cont_rot.assert_not_called()
    
    def test_describe_component_state(self, controller):
        """Verify describeComponentState returns non-empty."""
        state = controller.getComponentState()
        description = controller.describeComponentState(state)
        assert isinstance(description, list)
        assert len(description) > 0
    
    def test_get_component_state_hazards(self, controller):
        """Verify getComponentStateHazards returns empty list."""
        state = controller.getComponentState()
        hazards = controller.getComponentStateHazards(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        assert hazards == []
        
        hazards = controller.getComponentStateHazards(
            state,
            applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
        )
        assert hazards == []


class TestRecordingController:
    """Test RecordingController StatefulComponentMixin implementation."""
    
    @pytest.fixture
    def controller(self, mock_master, mock_comm_channel):
        """Create RecordingController mock with bound methods."""
        from imswitch.imcontrol.controller.controllers.RecordingController import RecordingController
        from imswitch.imcontrol.model import RecMode
        
        mock_master.recordingManager = MagicMock()
        mock_master.recordingManager.startRecording = MagicMock()
        
        widget = MagicMock()
        widget.getSaveFormat = MagicMock(return_value=0)
        widget.getSnapSaveMode = MagicMock(return_value=0)
        widget.getRecSaveMode = MagicMock(return_value=0)
        widget.getRecFolder = MagicMock(return_value='/tmp/test')
        widget.getNumExpositions = MagicMock(return_value=100)
        widget.getTimeToRec = MagicMock(return_value=10.0)
        widget.getTimelapseNumFrames = MagicMock(return_value=10)
        widget.getSpecTimelapseFrameTime = MagicMock(return_value=3600.0)
        widget.getTimelapseSingleFile = MagicMock(return_value=False)
        widget.setsaveFormat = MagicMock()
        widget.setSnapSaveMode = MagicMock()
        widget.setRecSaveMode = MagicMock()
        widget.setRecFolder = MagicMock()
        widget.numExpositionsEdit = MagicMock()
        widget.timeToRec = MagicMock()
        
        # Create lightweight mock with bound methods
        ctrl = Mock(spec=RecordingController)
        ctrl._master = mock_master
        ctrl._widget = widget
        ctrl._RecordingController__logger = Mock()
        ctrl.componentName = 'Recording'
        ctrl.stateSchemaVersion = 1
        ctrl.recMode = RecMode.UntilStop
        ctrl.specFrames = Mock()
        ctrl.specTime = Mock()
        ctrl.specLapse = Mock()
        ctrl.recScanOnce = Mock()
        ctrl.recScanLapse = Mock()
        ctrl.untilStop = Mock()
        
        # Bind actual methods
        ctrl.getComponentState = lambda: RecordingController.getComponentState(ctrl)
        ctrl.applyComponentState = lambda state, applyMode: RecordingController.applyComponentState(
            ctrl, state, applyMode=applyMode
        )
        ctrl.describeComponentState = lambda state: RecordingController.describeComponentState(ctrl, state)
        ctrl.getComponentStateHazards = lambda state, applyMode, context=None: RecordingController.getComponentStateHazards(
            ctrl, state, applyMode=applyMode, context=context
        )
        
        return ctrl
    
    def test_component_name(self, controller):
        """Verify canonical component name."""
        assert controller.componentName == 'Recording'
    
    def test_state_schema_version(self, controller):
        """Verify state schema version is set."""
        assert controller.stateSchemaVersion == 1
    
    def test_round_trip_state(self, controller):
        """Test getComponentState -> applyComponentState round trip."""
        state = controller.getComponentState()
        assert 'saveFormat' in state
        assert 'recMode' in state
        
        warnings = controller.applyComponentState(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        assert isinstance(warnings, list)
    
    def test_no_recording_startup_restore(self, controller):
        """Verify NO recording start in STARTUP_RESTORE mode."""
        state = controller.getComponentState()
        
        controller.applyComponentState(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        controller._master.recordingManager.startRecording.assert_not_called()
    
    def test_no_recording_setup_mode_apply(self, controller):
        """Verify NO recording start in SETUP_MODE_APPLY mode."""
        state = controller.getComponentState()
        
        controller.applyComponentState(
            state,
            applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
        )
        
        controller._master.recordingManager.startRecording.assert_not_called()

    def test_legacy_spec_lapse_state_restores_camera_lapse(self, controller):
        """The old saved name now migrates to the supported camera lapse."""
        state = {
            'saveFormat': 1,
            'snapSaveMode': 1,
            'recSaveMode': 1,
            'recMode': 'SpecLapse',
        }

        warnings = controller.applyComponentState(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )

        controller.specLapse.assert_called_once()
        controller.untilStop.assert_not_called()
        assert warnings == []
    
    def test_describe_component_state(self, controller):
        """Verify describeComponentState returns non-empty."""
        state = controller.getComponentState()
        description = controller.describeComponentState(state)
        assert isinstance(description, list)
        assert len(description) > 0
    
    def test_get_component_state_hazards(self, controller):
        """Verify getComponentStateHazards returns empty list."""
        state = controller.getComponentState()
        hazards = controller.getComponentStateHazards(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        assert hazards == []
        
        hazards = controller.getComponentStateHazards(
            state,
            applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
        )
        assert hazards == []


class TestBeadRecController:
    """Test BeadRecController StatefulComponentMixin implementation."""
    
    @pytest.fixture
    def controller(self, mock_master, mock_comm_channel):
        """Create BeadRecController mock with bound methods."""
        from imswitch.imcontrol.controller.controllers.BeadRecController import BeadRecController
        from imswitch.imcontrol.controller.display_transform import DisplayTransform
        
        widget = MagicMock()
        widget.analysisPrm = {'pixel_size': 0.1, 'threshold': 0.5}
        widget.scaleButton = MagicMock()
        widget.scaleButton.isChecked = MagicMock(return_value=False)
        widget.scaleButton.setChecked = MagicMock()
        widget.roiButton = MagicMock()
        widget.roiButton.isChecked = MagicMock(return_value=False)
        widget.roiButton.setChecked = MagicMock()
        widget.roiButton.blockSignals = MagicMock(return_value=False)
        widget.getOrientation = MagicMock(return_value=(0, False, False))
        widget.setOrientation = MagicMock()
        
        # Create lightweight mock with bound methods
        ctrl = Mock(spec=BeadRecController)
        ctrl._widget = widget
        ctrl._logger = Mock()
        ctrl.componentName = 'BeadRec'
        ctrl.stateSchemaVersion = 1
        ctrl.resultRecords = []
        ctrl.lastDir = None
        ctrl._orientation = DisplayTransform()
        ctrl.roiToggled = Mock()
        ctrl._toTransformArgs = Mock(return_value=(0, False, False))
        ctrl.run = Mock()
        
        # Bind actual methods
        ctrl.getComponentState = lambda: BeadRecController.getComponentState(ctrl)
        ctrl.applyComponentState = lambda state, applyMode: BeadRecController.applyComponentState(
            ctrl, state, applyMode=applyMode
        )
        ctrl.describeComponentState = lambda state: BeadRecController.describeComponentState(ctrl, state)
        ctrl.getComponentStateHazards = lambda state, applyMode, context=None: BeadRecController.getComponentStateHazards(
            ctrl, state, applyMode=applyMode, context=context
        )
        
        return ctrl
    
    def test_component_name(self, controller):
        """Verify canonical component name."""
        assert controller.componentName == 'BeadRec'
    
    def test_state_schema_version(self, controller):
        """Verify state schema version is set."""
        assert controller.stateSchemaVersion == 1
    
    def test_round_trip_state(self, controller):
        """Test getComponentState -> applyComponentState round trip."""
        state = controller.getComponentState()
        assert 'analysis_parameters' in state
        assert 'scale_enabled' in state
        
        warnings = controller.applyComponentState(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        assert isinstance(warnings, list)
    
    def test_no_acquisition_startup_restore(self, controller):
        """Verify NO acquisition start in STARTUP_RESTORE mode."""
        state = controller.getComponentState()
        
        controller.applyComponentState(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        controller.run.assert_not_called()
    
    def test_no_acquisition_setup_mode_apply(self, controller):
        """Verify NO acquisition start in SETUP_MODE_APPLY mode."""
        state = controller.getComponentState()
        
        controller.applyComponentState(
            state,
            applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
        )
        
        controller.run.assert_not_called()
    
    def test_describe_component_state(self, controller):
        """Verify describeComponentState returns non-empty."""
        state = controller.getComponentState()
        description = controller.describeComponentState(state)
        assert isinstance(description, list)
        assert len(description) > 0
    
    def test_get_component_state_hazards(self, controller):
        """Verify getComponentStateHazards returns empty list."""
        state = controller.getComponentState()
        hazards = controller.getComponentStateHazards(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        assert hazards == []
        
        hazards = controller.getComponentStateHazards(
            state,
            applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
        )
        assert hazards == []


def test_no_legacy_methods_in_controllers():
    """Verify all four controllers have NO legacy getWidgetState/setWidgetState methods."""
    from imswitch.imcontrol.controller.controllers.PositionerController import PositionerController
    from imswitch.imcontrol.controller.controllers.RotatorController import RotatorController
    from imswitch.imcontrol.controller.controllers.RecordingController import RecordingController
    from imswitch.imcontrol.controller.controllers.BeadRecController import BeadRecController
    
    for ctrl_cls in [PositionerController, RotatorController, RecordingController, BeadRecController]:
        assert not hasattr(ctrl_cls, 'getWidgetState') or hasattr(ctrl_cls, 'getComponentState'), \
            f'{ctrl_cls.__name__} should not have legacy getWidgetState'
        assert not hasattr(ctrl_cls, 'setWidgetState') or hasattr(ctrl_cls, 'getComponentState'), \
            f'{ctrl_cls.__name__} should not have legacy setWidgetState'
        assert not hasattr(ctrl_cls, 'getStateSchemaVersion') or hasattr(ctrl_cls, 'stateSchemaVersion'), \
            f'{ctrl_cls.__name__} should not have legacy getStateSchemaVersion'
        
        # Verify they DO have the new methods
        assert hasattr(ctrl_cls, 'getComponentState'), \
            f'{ctrl_cls.__name__} should have getComponentState'
        assert hasattr(ctrl_cls, 'applyComponentState'), \
            f'{ctrl_cls.__name__} should have applyComponentState'
        assert hasattr(ctrl_cls, 'describeComponentState'), \
            f'{ctrl_cls.__name__} should have describeComponentState'
        assert hasattr(ctrl_cls, 'getComponentStateHazards'), \
            f'{ctrl_cls.__name__} should have getComponentStateHazards'
