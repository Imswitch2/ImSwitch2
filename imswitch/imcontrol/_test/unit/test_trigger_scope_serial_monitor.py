"""The TriggerScope's SerialMonitor must never hold the port's I/O lock
while it waits.

``RS232Manager`` serialises every backend call under one lock (device
reconnection). The monitor polls the port every 5 ms for "Scan done", and
``read()`` waits for the configured termination -- up to the port's
timeout, 100 s on the TriggerScope -- so every DAC, TTL and PARAMETER write
waited behind it: on an idle port, and (the Snouty rig, 2026-10-20) after
every reply of a board that ends its lines with CR LF on a port configured
for CR, which left an LF behind each time. The monitor now takes the bytes
that have arrived, no more, and splits the lines itself.
"""

import threading
import time
from unittest.mock import Mock

import pytest

from imswitch.imcontrol.model.interfaces.RS232Driver_mock import MockRS232Driver
from imswitch.imcontrol.model.managers.rs232.RS232Manager import RS232Manager
from imswitch.imcontrol.model.managers.TriggerScopeManager import SerialMonitor

SETTINGS = {'port': 'COM99', 'recv_termination': '\r'}


class _BlockingBackend:
    """A port whose read() waits for a termination that is not coming (the
    TriggerScope between commands, or the stray LF of a CR LF board on a CR
    port), and which hands out what has arrived without waiting."""

    def __init__(self, arrived=''):
        self.release = threading.Event()
        self.reads = 0
        self.writes = []
        self.arrived = arrived

    def readWaiting(self):
        chunk, self.arrived = self.arrived, ''
        return chunk

    def read(self, *args, **kwargs):
        self.reads += 1
        self.release.wait()
        return None

    def write(self, arg):
        self.writes.append(arg)

    def close(self):
        pass


def _manager(backend):
    info = Mock()
    info.managerProperties = dict(SETTINGS)
    manager = RS232Manager.__new__(RS232Manager)
    # The real __init__ opens a port; this one is handed a backend.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(RS232Manager, '_openWithFallback', lambda self, context: backend)
        RS232Manager.__init__(manager, info, 'test')
    return manager


def _monitor(manager):
    monitor = SerialMonitor(manager, updatePeriod=5)
    seen = {'done': 0, 'msg': [], 'other': []}
    monitor.sigScanDone.connect(lambda: seen.__setitem__('done', seen['done'] + 1))
    monitor.sigTrigMSG.connect(seen['msg'].append)
    monitor.sigUnknownMessage.connect(seen['other'].append)
    return monitor, seen


@pytest.mark.parametrize('arrived', ['', '\n'], ids=['idle port', 'stray LF of a CR LF board'])
def test_a_write_does_not_wait_behind_the_monitor(arrived):
    backend = _BlockingBackend(arrived)
    manager = _manager(backend)
    monitor, seen = _monitor(manager)
    try:
        poller = threading.Thread(target=monitor.checkSerial, daemon=True)
        poller.start()
        poller.join(1.0)
        assert not poller.is_alive(), 'a poll must return without waiting for a termination'
        assert backend.reads == 0

        started = time.monotonic()
        manager.write('DAC0,1.0')
        assert time.monotonic() - started < 0.5
        assert backend.writes == ['DAC0,1.0']
        assert seen == {'done': 0, 'msg': [], 'other': []}
    finally:
        backend.release.set()


def test_lines_are_split_on_cr_lf_or_both_and_across_polls():
    backend = _BlockingBackend()
    manager = _manager(backend)
    monitor, seen = _monitor(manager)
    backend.arrived = '!TTL7,0\r\nScan do'
    monitor.checkSerial()
    assert seen == {'done': 0, 'msg': [], 'other': ['!TTL7,0']}
    backend.arrived = 'ne\r\n\nMSG0001\rDAC voltage set to: 0.00 V\n'
    monitor.checkSerial()
    assert seen == {'done': 1, 'msg': [1], 'other': ['!TTL7,0', 'DAC voltage set to: 0.00 V']}
    assert monitor._partial == ''


def test_a_backend_that_cannot_read_waiting_bytes_is_read_as_before():
    """A backend without readWaiting (an older driver, a test double) keeps
    the previous behaviour: every poll reads a line."""
    backend = Mock(spec=['read', 'write', 'close'])
    backend.read.return_value = 'MSG0001\r'
    manager = _manager(backend)
    assert manager.readWaiting() is None
    monitor, seen = _monitor(manager)
    monitor.checkSerial()
    assert seen['msg'] == [1]
    backend.read.assert_called_once()


def test_the_mock_port_never_has_anything_waiting():
    manager = _manager(MockRS232Driver('COM99', SETTINGS))
    assert manager.readWaiting() == ''


def test_the_pyserial_adapter_reads_what_is_waiting():
    from imswitch.imcontrol.model.interfaces.RS232Driver import _SerialAdapter

    adapter = _SerialAdapter.__new__(_SerialAdapter)
    adapter._encoding = 'ascii'
    adapter._ser = Mock(in_waiting=7)
    adapter._ser.read.return_value = b'!TTL7,0'
    assert adapter.bytes_in_buffer == 7
    assert adapter.read_bytes(7) == b'!TTL7,0'
    adapter._ser.read.assert_called_once_with(7)


def test_the_driver_takes_only_the_bytes_in_the_buffer():
    from imswitch.imcontrol.model.interfaces.RS232Driver import RS232Driver

    driver = RS232Driver('COM99')
    with pytest.raises(OSError):
        driver.readWaiting()
    resource = Mock(bytes_in_buffer=0, encoding='ascii')
    driver._resource = resource
    assert driver.readWaiting() == ''
    resource.read_bytes.assert_not_called()
    resource.bytes_in_buffer = 3
    resource.read_bytes.return_value = b'\nOK'
    assert driver.readWaiting() == '\nOK'
    resource.read_bytes.assert_called_once_with(3)
