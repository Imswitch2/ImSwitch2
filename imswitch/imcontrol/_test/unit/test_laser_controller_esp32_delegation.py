from types import SimpleNamespace
from unittest.mock import Mock, call

from imswitch.imcontrol.controller.controllers.LaserController import LaserController


def _controller_with_esp32_manager(esp32_manager):
    controller = LaserController.__new__(LaserController)
    controller._master = SimpleNamespace(rs232sManager={'ESP32': esp32_manager})
    return controller


def test_sendTrigger_delegates_to_esp32_manager():
    esp32_manager = Mock()
    controller = _controller_with_esp32_manager(esp32_manager)

    controller.sendTrigger(triggerId=5)

    esp32_manager.sendTrigger.assert_called_once_with(5)


def test_post_json_delegates_to_esp32_manager():
    esp32_manager = Mock()
    esp32_manager.post_json.return_value = {'response': 'data'}
    controller = _controller_with_esp32_manager(esp32_manager)
    payload = {'key': 'value'}

    result = controller.post_json('/test_path', payload)

    esp32_manager.post_json.assert_called_once_with(
        '/test_path',
        payload=payload,
        headers=None,
        timeout=1
    )
    assert result == {'response': 'data'}


def test_send_serial_delegates_to_esp32_manager_in_order():
    esp32_manager = Mock()
    esp32_manager.readSerial.return_value = 'serial_response'
    controller = _controller_with_esp32_manager(esp32_manager)

    result = controller.send_serial('test_command')

    assert esp32_manager.method_calls == [
        call.writeSerial('test_command'),
        call.readSerial(is_blocking=True, timeout=1),
    ]
    assert result == 'serial_response'
