"""The TriggerScope's SerialMonitor must not hold the port's I/O lock while
the board is silent.

``RS232Manager`` serialises every backend call under one lock (device
reconnection). The monitor polls ``read()`` every 5 ms for "Scan done", and
a read on an idle port blocks until the port's timeout -- 100 s on the
TriggerScope -- so every DAC, TTL and PARAMETER write waited for that
timeout and the GUI crawled whenever the TriggerScope was involved. The
monitor now asks ``bytesWaiting()`` first and reads only when a line is
coming.
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
    """A port whose read blocks until released (the TriggerScope between
    commands), and which knows nothing is waiting."""

    def __init__(self):
        self.release = threading.Event()
        self.reads = 0
        self.writes = []
        self.waiting = 0

    def bytesWaiting(self):
        return self.waiting

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


def test_a_write_does_not_wait_for_an_idle_monitor_read():
    backend = _BlockingBackend()
    manager = _manager(backend)
    monitor = SerialMonitor(manager, updatePeriod=5)
    try:
        poller = threading.Thread(target=monitor.checkSerial, daemon=True)
        poller.start()
        poller.join(1.0)
        assert not poller.is_alive(), 'an idle poll must return without reading'
        assert backend.reads == 0

        started = time.monotonic()
        manager.write('DAC0,1.0')
        assert time.monotonic() - started < 0.5
        assert backend.writes == ['DAC0,1.0']
    finally:
        backend.release.set()


def test_the_monitor_reads_a_line_that_is_arriving():
    backend = _BlockingBackend()
    backend.waiting = 10
    backend.release.set()
    backend.read = lambda *a, **k: 'Scan done\r'
    manager = _manager(backend)
    monitor = SerialMonitor(manager, updatePeriod=5)
    done = []
    monitor.sigScanDone.connect(lambda: done.append(True))
    monitor.checkSerial()
    assert done == [True]


def test_a_backend_that_cannot_count_is_read_as_before():
    """A backend without bytesWaiting (an older driver, a test double) keeps
    the previous behaviour: every poll reads."""
    backend = Mock(spec=['read', 'write', 'close'])
    backend.read.return_value = 'MSG0001\r'
    manager = _manager(backend)
    assert manager.bytesWaiting() is None
    monitor = SerialMonitor(manager, updatePeriod=5)
    messages = []
    monitor.sigTrigMSG.connect(messages.append)
    monitor.checkSerial()
    assert messages == [1]
    backend.read.assert_called_once()


def test_the_mock_port_never_has_bytes_waiting():
    manager = _manager(MockRS232Driver('COM99', SETTINGS))
    assert manager.bytesWaiting() == 0


def test_the_pyserial_adapter_reports_in_waiting():
    from imswitch.imcontrol.model.interfaces.RS232Driver import _SerialAdapter

    adapter = _SerialAdapter.__new__(_SerialAdapter)
    adapter._ser = Mock(in_waiting=7)
    assert adapter.bytes_in_buffer == 7


def test_the_driver_asks_its_resource():
    from imswitch.imcontrol.model.interfaces.RS232Driver import RS232Driver

    driver = RS232Driver('COM99')
    with pytest.raises(OSError):
        driver.bytesWaiting()
    driver._resource = Mock(bytes_in_buffer=3)
    assert driver.bytesWaiting() == 3
