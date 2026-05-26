"""Tests for WorkflowFacadeController — API wrapper for build_facade_from_master."""

import pytest
from unittest.mock import MagicMock, patch

from imswitch.imcontrol.controller.controllers.WorkflowFacadeController import (
    WorkflowFacadeController,
)


@pytest.mark.nohardware
def test_build_forwards_to_facade_builder():
    """Controller.build() delegates to build_facade_from_master with master and kwargs."""
    # Create a mock master with expected manager attributes
    mock_master = MagicMock()
    mock_master.lasersManager = MagicMock()
    mock_master.detectorsManager = MagicMock()
    mock_master.positionersManager = MagicMock()
    mock_master.nidaqManager = MagicMock()
    
    # Create mock setup info, comm channel, widget, factory (required by base classes)
    mock_setup_info = MagicMock()
    mock_comm_channel = MagicMock()
    mock_module_comm_channel = MagicMock()
    mock_widget = MagicMock()
    mock_factory = MagicMock()
    
    # Instantiate controller
    controller = WorkflowFacadeController(
        setupInfo=mock_setup_info,
        commChannel=mock_comm_channel,
        master=mock_master,
        widget=mock_widget,
        factory=mock_factory,
        moduleCommChannel=mock_module_comm_channel,
    )
    
    # Mock the build_facade_from_master function (patched where it's imported/used)
    with patch('imswitch.imcontrol.model.workflows.facade.build_facade_from_master') as mock_builder:
        # Create a mock facade to return
        mock_facade = MagicMock()
        mock_builder.return_value = mock_facade
        
        # Call build with some kwargs
        laser_aliases = {'488': 'Laser488', '405': 'Laser405'}
        detector_name = 'Kiralux'
        
        result = controller.build(
            laser_aliases=laser_aliases,
            detector_name=detector_name,
        )
        
        # Verify build_facade_from_master was called with master and kwargs
        mock_builder.assert_called_once_with(
            mock_master,
            laser_aliases=laser_aliases,
            detector_name=detector_name,
        )
        
        # Verify the result is the facade
        assert result is mock_facade


@pytest.mark.nohardware
def test_build_returns_microscope_facade():
    """Controller.build() returns a MicroscopeFacade instance."""
    from imswitch.imcontrol.model.workflows.facade import MicroscopeFacade
    
    # Create a mock master with manager structure
    mock_master = MagicMock()
    mock_master.lasersManager = MagicMock()
    mock_master.detectorsManager = MagicMock()
    mock_master.positionersManager = MagicMock()
    mock_master.nidaqManager = None
    
    # Mock laser manager structure
    mock_laser_488 = MagicMock()
    mock_laser_488.power = 0.0
    mock_master.lasersManager.__contains__ = MagicMock(return_value=True)
    mock_master.lasersManager.__getitem__ = MagicMock(return_value=mock_laser_488)
    
    # Mock detector manager structure
    mock_detector = MagicMock()
    mock_detector.model = MagicMock()
    mock_master.detectorsManager.__contains__ = MagicMock(return_value=True)
    mock_master.detectorsManager.__getitem__ = MagicMock(return_value=mock_detector)
    
    # Create controller with all required arguments
    mock_setup_info = MagicMock()
    mock_comm_channel = MagicMock()
    mock_module_comm_channel = MagicMock()
    mock_widget = MagicMock()
    mock_factory = MagicMock()
    
    controller = WorkflowFacadeController(
        setupInfo=mock_setup_info,
        commChannel=mock_comm_channel,
        master=mock_master,
        widget=mock_widget,
        factory=mock_factory,
        moduleCommChannel=mock_module_comm_channel,
    )
    
    # Call build (should not raise)
    facade = controller.build(
        laser_aliases={'488': 'Laser488'},
        detector_name='Kiralux',
    )
    
    # Verify we got a MicroscopeFacade instance
    assert isinstance(facade, MicroscopeFacade)
    assert facade.laser_con is not None


@pytest.mark.nohardware
def test_build_with_no_arguments():
    """Controller.build() works with no arguments (minimal facade)."""
    mock_master = MagicMock()
    mock_master.lasersManager = MagicMock()
    mock_master.detectorsManager = MagicMock()
    mock_master.positionersManager = MagicMock()
    mock_master.nidaqManager = None
    
    mock_setup_info = MagicMock()
    mock_comm_channel = MagicMock()
    mock_module_comm_channel = MagicMock()
    mock_widget = MagicMock()
    mock_factory = MagicMock()
    
    controller = WorkflowFacadeController(
        setupInfo=mock_setup_info,
        commChannel=mock_comm_channel,
        master=mock_master,
        widget=mock_widget,
        factory=mock_factory,
        moduleCommChannel=mock_module_comm_channel,
    )
    
    # Build with no arguments
    facade = controller.build()
    
    # Should return a facade (even if minimal)
    from imswitch.imcontrol.model.workflows.facade import MicroscopeFacade
    assert isinstance(facade, MicroscopeFacade)


@pytest.mark.nohardware
def test_build_forwards_all_kwargs():
    """All keyword arguments are forwarded to build_facade_from_master."""
    mock_master = MagicMock()
    mock_setup_info = MagicMock()
    mock_comm_channel = MagicMock()
    mock_module_comm_channel = MagicMock()
    mock_widget = MagicMock()
    mock_factory = MagicMock()
    
    controller = WorkflowFacadeController(
        setupInfo=mock_setup_info,
        commChannel=mock_comm_channel,
        master=mock_master,
        widget=mock_widget,
        factory=mock_factory,
        moduleCommChannel=mock_module_comm_channel,
    )
    
    with patch('imswitch.imcontrol.model.workflows.facade.build_facade_from_master') as mock_builder:
        mock_facade = MagicMock()
        mock_builder.return_value = mock_facade
        
        # Call with various kwargs
        result = controller.build(
            laser_aliases={'488': 'L1', '405': 'L2'},
            detector_name='D1',
            z_stage_name='Z1',
            rotation_stage_name='R1',
            trig_device_name='T1',
            custom_arg='custom_value',
        )
        
        # Verify all kwargs were forwarded
        mock_builder.assert_called_once()
        call_args = mock_builder.call_args
        assert call_args[0][0] is mock_master  # First positional arg
        assert call_args[1]['laser_aliases'] == {'488': 'L1', '405': 'L2'}
        assert call_args[1]['detector_name'] == 'D1'
        assert call_args[1]['z_stage_name'] == 'Z1'
        assert call_args[1]['rotation_stage_name'] == 'R1'
        assert call_args[1]['trig_device_name'] == 'T1'
        assert call_args[1]['custom_arg'] == 'custom_value'


@pytest.mark.nohardware
def test_controller_has_master_access():
    """Controller has access to self._master from base class."""
    mock_master = MagicMock()
    mock_setup_info = MagicMock()
    mock_comm_channel = MagicMock()
    mock_module_comm_channel = MagicMock()
    mock_widget = MagicMock()
    mock_factory = MagicMock()
    
    controller = WorkflowFacadeController(
        setupInfo=mock_setup_info,
        commChannel=mock_comm_channel,
        master=mock_master,
        widget=mock_widget,
        factory=mock_factory,
        moduleCommChannel=mock_module_comm_channel,
    )
    
    # Verify controller has _master attribute
    assert hasattr(controller, '_master')
    assert controller._master is mock_master
