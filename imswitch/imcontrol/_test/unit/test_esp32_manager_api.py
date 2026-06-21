import sys
from unittest.mock import Mock

import numpy as np

from imswitch.imcontrol.model.managers.positioners.ESP32StageManager import ESP32StageManager
from imswitch.imcontrol.model.managers.rs232.ESP32Manager import ESP32Manager


class _FakeRS232Info:
    def __init__(self, host=None, serialport=None, identity='UC2_Feather'):
        self.managerProperties = {}
        if host:
            self.managerProperties['host'] = host
        if serialport:
            self.managerProperties['serialport'] = serialport
        if identity:
            self.managerProperties['identity'] = identity


class _FakeESP32Client:
    """Mock UC2Client for testing"""
    def __init__(self):
        self.sendTrigger_calls = []
        self.post_json_calls = []
        self.writeSerial_calls = []
        self.readSerial_calls = []
        self.move_x_calls = []
        self.move_y_calls = []
        self.move_z_calls = []
        self.set_galvo_freq_calls = []
        self.set_galvo_amp_calls = []
        self.send_LEDMatrix_array_calls = []
        self._last_response = None

    def sendTrigger(self, triggerId):
        self.sendTrigger_calls.append(triggerId)
        return {'status': 'ok', 'triggerId': triggerId}

    def post_json(self, path, payload=None, headers=None, timeout=1):
        self.post_json_calls.append({
            'path': path,
            'payload': payload,
            'headers': headers,
            'timeout': timeout
        })
        return {'status': 'ok', 'path': path}

    def writeSerial(self, payload):
        self.writeSerial_calls.append(payload)

    def readSerial(self, is_blocking=True, timeout=1):
        self.readSerial_calls.append({
            'is_blocking': is_blocking,
            'timeout': timeout
        })
        return self._last_response or 'test_response'

    def move_x(self, value, speed, is_blocking=False):
        self.move_x_calls.append({
            'value': value,
            'speed': speed,
            'is_blocking': is_blocking
        })

    def move_y(self, value, speed, is_blocking=False):
        self.move_y_calls.append({
            'value': value,
            'speed': speed,
            'is_blocking': is_blocking
        })

    def move_z(self, value, speed, is_blocking=False):
        self.move_z_calls.append({
            'value': value,
            'speed': speed,
            'is_blocking': is_blocking
        })

    def set_galvo_freq(self, axis, value):
        self.set_galvo_freq_calls.append({'axis': axis, 'value': value})

    def set_galvo_amp(self, axis, value):
        self.set_galvo_amp_calls.append({'axis': axis, 'value': value})

    def send_LEDMatrix_array(self, pattern):
        self.send_LEDMatrix_array_calls.append(pattern)


def _manager_with_client(monkeypatch, fake_client, rs232_info=None):
    fake_uc2 = Mock()
    fake_uc2.UC2Client = Mock(return_value=fake_client)
    monkeypatch.setitem(sys.modules, 'uc2rest', fake_uc2)
    return ESP32Manager(rs232_info or _FakeRS232Info(host='192.168.1.100'), 'ESP32')


def _manager_without_client():
    manager = ESP32Manager.__new__(ESP32Manager)
    manager._ESP32Manager__logger = Mock()
    manager._esp32 = None
    return manager


def test_esp32_manager_sendTrigger_delegates_to_client(monkeypatch):
    """Test that sendTrigger delegates to the underlying ESP32 client"""
    fake_client = _FakeESP32Client()
    manager = _manager_with_client(monkeypatch, fake_client)

    result = manager.sendTrigger(5)

    assert len(fake_client.sendTrigger_calls) == 1
    assert fake_client.sendTrigger_calls[0] == 5
    assert result['triggerId'] == 5


def test_esp32_manager_post_json_delegates_to_client(monkeypatch):
    """Test that post_json delegates to the underlying ESP32 client"""
    fake_client = _FakeESP32Client()
    manager = _manager_with_client(monkeypatch, fake_client)

    payload = {'key': 'value'}
    result = manager.post_json('/test_path', payload, headers={'Custom': 'Header'}, timeout=2)

    assert len(fake_client.post_json_calls) == 1
    call = fake_client.post_json_calls[0]
    assert call['path'] == '/test_path'
    assert call['payload'] == payload
    assert call['headers'] == {'Custom': 'Header'}
    assert call['timeout'] == 2
    assert result['path'] == '/test_path'


def test_esp32_manager_post_json_uses_default_payload(monkeypatch):
    """Test that post_json uses empty dict as default payload"""
    fake_client = _FakeESP32Client()
    manager = _manager_with_client(monkeypatch, fake_client)

    manager.post_json('/test_path')

    call = fake_client.post_json_calls[0]
    assert call['payload'] == {}


def test_esp32_manager_writeSerial_delegates_to_client(monkeypatch):
    """Test that writeSerial delegates to the underlying ESP32 client"""
    fake_client = _FakeESP32Client()
    rs232_info = _FakeRS232Info(serialport='/dev/ttyUSB0')
    manager = _manager_with_client(monkeypatch, fake_client, rs232_info)

    manager.writeSerial('test_command')

    assert len(fake_client.writeSerial_calls) == 1
    assert fake_client.writeSerial_calls[0] == 'test_command'


def test_esp32_manager_readSerial_delegates_to_client(monkeypatch):
    """Test that readSerial delegates to the underlying ESP32 client"""
    fake_client = _FakeESP32Client()
    fake_client._last_response = 'custom_response'
    rs232_info = _FakeRS232Info(serialport='/dev/ttyUSB0')
    manager = _manager_with_client(monkeypatch, fake_client, rs232_info)

    result = manager.readSerial(is_blocking=True, timeout=3)

    assert len(fake_client.readSerial_calls) == 1
    call = fake_client.readSerial_calls[0]
    assert call['is_blocking'] is True
    assert call['timeout'] == 3
    assert result == 'custom_response'


def test_esp32_manager_handles_missing_client_gracefully():
    """Test that public API methods handle missing client gracefully"""
    manager = _manager_without_client()

    # These should not raise exceptions
    assert manager.sendTrigger(1) is None
    assert manager.post_json('/test', {}) is None
    manager.writeSerial('test')  # Returns None
    assert manager.readSerial() == ''
    assert manager.move_x(1, 1000) is False
    assert manager.move_y(1, 1000) is False
    assert manager.move_z(1, 1000) is False
    manager.set_galvo_freq(axis=0, value=100)
    manager.set_galvo_amp(axis=1, value=50)
    manager.send_LEDMatrix_array(np.zeros((3, 4, 4), dtype=np.uint8))


def test_esp32_manager_readSerial_defaults(monkeypatch):
    """Test that readSerial uses correct defaults"""
    fake_client = _FakeESP32Client()
    rs232_info = _FakeRS232Info(serialport='/dev/ttyUSB0')
    manager = _manager_with_client(monkeypatch, fake_client, rs232_info)

    manager.readSerial()

    call = fake_client.readSerial_calls[0]
    assert call['is_blocking'] is True
    assert call['timeout'] == 1


def test_esp32_manager_stage_move_wrappers_delegate_to_client(monkeypatch):
    fake_client = _FakeESP32Client()
    manager = _manager_with_client(monkeypatch, fake_client)

    assert manager.move_x(100, 1000, is_blocking=False) is True
    assert manager.move_y(200, 1500, is_blocking=True) is True
    assert manager.move_z(50, 800, is_blocking=False) is True

    assert fake_client.move_x_calls == [{
        'value': 100,
        'speed': 1000,
        'is_blocking': False
    }]
    assert fake_client.move_y_calls == [{
        'value': 200,
        'speed': 1500,
        'is_blocking': True
    }]
    assert fake_client.move_z_calls == [{
        'value': 50,
        'speed': 800,
        'is_blocking': False
    }]


def test_esp32_manager_galvo_wrappers_delegate_to_client(monkeypatch):
    fake_client = _FakeESP32Client()
    manager = _manager_with_client(monkeypatch, fake_client)

    manager.set_galvo_freq(axis=0, value=1000)
    manager.set_galvo_amp(axis=1, value=500)

    assert fake_client.set_galvo_freq_calls == [{'axis': 0, 'value': 1000}]
    assert fake_client.set_galvo_amp_calls == [{'axis': 1, 'value': 500}]


def test_esp32_manager_led_matrix_wrapper_delegates_to_client(monkeypatch):
    fake_client = _FakeESP32Client()
    manager = _manager_with_client(monkeypatch, fake_client)
    pattern = np.zeros((3, 4, 4), dtype=np.uint8)

    manager.send_LEDMatrix_array(pattern)

    assert len(fake_client.send_LEDMatrix_array_calls) == 1
    np.testing.assert_array_equal(fake_client.send_LEDMatrix_array_calls[0], pattern)


def test_esp32_stage_manager_does_not_update_cached_position_when_move_fails():
    esp32_manager = Mock()
    esp32_manager.move_x.return_value = False
    stage_manager = ESP32StageManager.__new__(ESP32StageManager)
    stage_manager._rs232manager = esp32_manager
    stage_manager._position = {'X': 10}
    stage_manager.SPEED = 1000
    stage_manager.PHYS_FACTOR = 2
    stage_manager._ESP32StageManager__logger = Mock()

    stage_manager.move(5, 'X')

    esp32_manager.move_x.assert_called_once_with(10, 1000, is_blocking=False)
    assert stage_manager._position['X'] == 10


def test_esp32_stage_manager_updates_cached_position_after_move_dispatch():
    esp32_manager = Mock()
    esp32_manager.move_z.return_value = True
    stage_manager = ESP32StageManager.__new__(ESP32StageManager)
    stage_manager._rs232manager = esp32_manager
    stage_manager._position = {'Z': 1}
    stage_manager.SPEED = 800
    stage_manager.PHYS_FACTOR = 3
    stage_manager._ESP32StageManager__logger = Mock()

    stage_manager.move(4, 'Z')

    esp32_manager.move_z.assert_called_once_with(12, 800, is_blocking=False)
    assert stage_manager._position['Z'] == 5
