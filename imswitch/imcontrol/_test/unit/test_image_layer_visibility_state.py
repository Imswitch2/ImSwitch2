from unittest.mock import Mock, patch

from imswitch.imcontrol.controller.SetupModeController import SetupModeController
from imswitch.imcontrol.controller.controllers.ImageController import ImageController
from imswitch.imcontrol.model.WidgetStatePersistence import WidgetStatePersistence
from imswitch.imcontrol.model.state_contracts import ComponentStateApplyMode


class _Signal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *args):
        for callback in self.callbacks:
            callback(*args)


def _controller_with_fake_widget():
    widget = Mock()
    widget.getLayerVisibilityState.return_value = {
        'layers': {'Live: CamA': True, 'Snap 1': False},
        'liveLayers': {'CamA': True},
    }
    widget.applyLayerVisibilityState.return_value = []

    commChannel = Mock()
    commChannel.sigSetVisibleLayers = _Signal()

    master = Mock()
    master.detectorsManager.hasDevices.return_value = False

    registry = Mock()
    with patch(
        'imswitch.imcontrol.controller.controllers.ImageController.getWidgetStatePersistence',
        return_value=registry,
    ):
        controller = ImageController(
            Mock(),
            commChannel,
            master,
            widget=widget,
            factory=Mock(),
            moduleCommChannel=Mock(),
        )

    return controller, widget, commChannel, registry


def test_image_controller_registers_visibility_state_component():
    controller, _, _, registry = _controller_with_fake_widget()

    registry.register.assert_called_once_with('Image', controller)
    assert controller.componentName == 'Image'


def test_image_controller_snapshots_and_applies_layer_visibility():
    controller, widget, _, _ = _controller_with_fake_widget()
    state = controller.getComponentState()

    assert state == {
        'layers': {'Live: CamA': True, 'Snap 1': False},
        'liveLayers': {'CamA': True},
    }

    warnings = controller.applyComponentState(
        {'layers': {'Snap 1': True}, 'liveLayers': {'CamA': False}},
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
    )

    assert warnings == []
    widget.applyLayerVisibilityState.assert_called_once_with(
        {'layers': {'Snap 1': True}, 'liveLayers': {'CamA': False}}
    )


def test_image_controller_routes_visible_layer_signal_to_widget():
    _, widget, commChannel, _ = _controller_with_fake_widget()

    commChannel.sigSetVisibleLayers.emit(('CamA',))

    widget.setVisibleLayers.assert_called_once_with(('CamA',))


def test_image_component_is_setup_mode_eligible():
    controller, _, _, _ = _controller_with_fake_widget()
    setupModes = SetupModeController({'Image': controller})

    assert setupModes.getSetupModeComponents() == ['Image']


def test_image_controller_alias_resolves_to_canonical_image():
    registry = WidgetStatePersistence()
    controller = Mock()
    controller.getComponentState.return_value = {'layers': {}, 'liveLayers': {}}
    controller.applyComponentState.return_value = []

    registry.register('ImageController', controller)

    assert registry.isRegistered('Image')
    assert registry.isRegistered('ImageController')
    assert registry.getRegisteredControllers() == ['Image']
