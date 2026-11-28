"""TriggerScope serial traffic that used to block the GUI thread.

With the monitor no longer holding the I/O lock through a 100 s read, the GUI
thread could still sit in TriggerScope code: ``setParameter`` pauses 50 ms
after every PARAMETER line and a raster scan uploads about twenty of them, all
inside the scan-start slot (and again for every Repeat iteration), and every
write waits for the serial I/O lock behind whatever holds it. The scan start
now hands the ordered batch to an uploader thread. These tests use a fake port
whose writes are slow or whose lock is held by a silent read, and assert that
the GUI thread comes back within a small bound while the board still receives
every command, in order.
"""

import threading
import time
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.managers.ScanManagerTriggerScope import (
    ScanManagerTriggerScope,
)
from imswitch.imcontrol.model.managers.TriggerScopeManager import (
    TriggerScopeManager,
)

#: What "returns promptly" means for a call that used to take seconds.
GUI_BOUND_S = 0.25


class _Port:
    """Fake RS232Manager: one I/O lock, optionally slow writes, optional
    silent blocking read (the pre-fix monitor behaviour)."""

    def __init__(self, writeDelayS=0.0, failOn=None):
        self.ioLock = threading.RLock()
        self.writeDelayS = writeDelayS
        self.failOn = failOn
        self.written = []
        self.readBlockS = 0.0
        self.writeStarted = threading.Event()

    def setTimeout(self, timeoutMs):
        pass

    def write(self, command):
        self.writeStarted.set()
        with self.ioLock:
            time.sleep(self.writeDelayS)
            if self.failOn is not None and self.failOn in command:
                raise OSError('port went away')
            self.written.append(command)

    def readWaiting(self):
        return ''

    def read(self, *args):
        with self.ioLock:
            time.sleep(self.readBlockS)
        return None

    def finalize(self):
        pass


def _triggerScope(port):
    setup = SimpleNamespace(
        triggerScope=SimpleNamespace(rs232device='ts'),
        getAllDevices=lambda: {},
    )
    manager = TriggerScopeManager(setup, {'ts': port})
    manager.setParSleepTime = 0.02
    manager._deviceInfo = {
        'CAM': {'TTLLine': '3', 'DACChannel': '1', 'MinV': -10, 'MaxV': 10},
        'X': {'TTLLine': '1', 'DACChannel': '2', 'MinV': -10, 'MaxV': 10},
        'Y': {'TTLLine': '1', 'DACChannel': '3', 'MinV': -10, 'MaxV': 10},
    }
    return manager


@pytest.fixture
def board(qtbot):
    created = []

    def make(**kwargs):
        port = _Port(**kwargs)
        manager = _triggerScope(port)
        created.append(manager)
        return manager, port

    yield make
    for manager in created:
        manager.finalize()


def _waitFor(qtbot, flag, timeout=10000):
    qtbot.waitUntil(lambda: bool(flag), timeout=timeout)


# --------------------------------------------------------------------------- #
# The uploader                                                                 #
# --------------------------------------------------------------------------- #

def test_a_slow_batch_does_not_hold_the_calling_thread(qtbot, board):
    manager, port = board(writeDelayS=0.1)
    results = []

    start = time.monotonic()
    manager.uploadCommands(
        [(f'PARAMETER,p{i},1\n', 0.05) for i in range(10)],
        lambda ok, message: results.append((ok, message)),
    )
    elapsed = time.monotonic() - start

    assert elapsed < GUI_BOUND_S
    _waitFor(qtbot, results)
    assert results == [(True, '')]
    assert [c for c in port.written if c.startswith('PARAMETER')] == [
        f'PARAMETER,p{i},1\n' for i in range(10)
    ]


def test_a_command_sent_during_an_upload_follows_it_in_order(qtbot, board):
    manager, port = board(writeDelayS=0.05)
    port.written.clear()
    results = []

    manager.uploadCommands(
        [('A\n', 0), ('B\n', 0), ('C\n', 0)],
        lambda ok, message: results.append(ok),
    )
    start = time.monotonic()
    manager.send('TTL1,0')          # a laser disarm raised by the GUI
    assert time.monotonic() - start < GUI_BOUND_S

    qtbot.waitUntil(lambda: len(port.written) == 4, timeout=10000)
    assert port.written == ['A\n', 'B\n', 'C\n', 'TTL1,0']


def test_with_nothing_queued_a_send_is_still_a_direct_write(board):
    manager, port = board()
    port.written.clear()

    manager.send('TTL1,1')

    assert port.written == ['TTL1,1']


def test_a_failed_write_is_reported_and_stops_the_batch(qtbot, board):
    manager, port = board(failOn='bad')
    port.written.clear()
    results = []

    manager.uploadCommands(
        [('good\n', 0), ('bad\n', 0), ('never\n', 0)],
        lambda ok, message: results.append((ok, message)),
    )

    _waitFor(qtbot, results)
    ok, message = results[0]
    assert ok is False
    assert 'port went away' in message
    assert port.written == ['good\n']


def test_a_cancelled_batch_sends_nothing_further(qtbot, board):
    manager, port = board(writeDelayS=0.1)
    port.written.clear()
    results = []

    job = manager.uploadCommands(
        [(f'c{i}\n', 0) for i in range(20)],
        lambda ok, message: results.append((ok, message)),
    )
    assert port.writeStarted.wait(2)
    job.cancel()

    _waitFor(qtbot, results)
    assert results[0][0] is False
    assert len(port.written) < 20


def test_a_throwing_completion_handler_does_not_escape_the_slot(qtbot, board):
    manager, port = board()
    done = []

    def boom(ok, message):
        done.append(True)
        raise RuntimeError('handler bug')

    manager.uploadCommands([('A\n', 0)], boom)
    _waitFor(qtbot, done)
    # a second job still runs: the worker and the slot survived
    again = []
    manager.uploadCommands([('B\n', 0)], lambda ok, m: again.append(ok))
    _waitFor(qtbot, again)


def test_finalize_does_not_wait_for_a_port_that_never_answers(board):
    manager, port = board(writeDelayS=3.0)
    port.written.clear()
    manager.SHUTDOWN_WAIT_MS = 200
    manager.uploadCommands([('stuck\n', 0)])
    assert port.writeStarted.wait(2)

    start = time.monotonic()
    manager.finalize()

    assert time.monotonic() - start < 1.5
    # the blocked write returns later; the thread must not be destroyed under it
    for thread in manager._stuckThreads:
        assert thread.wait(10000)


# --------------------------------------------------------------------------- #
# A scan start                                                                 #
# --------------------------------------------------------------------------- #

class _Logger:
    def debug(self, *args, **kwargs):
        pass

    warning = error = debug


_RASTER_PARAMS = {
    'Digital': {
        'sequence_time': 0.001,
        'TTL_start': [0.0],
        'TTL_end': [0.0005],
        'target_device': ['CAM'],
    },
    'Analog': {
        'targets': ['X', 'Y'],
        'startPos': [0.0, 0.0],
        'lengths': [1.0, 1.0],
        'stepSizes': [0.1, 0.1],
    },
}


def _scanManager(manager, events=None):
    scanManager = ScanManagerTriggerScope.__new__(ScanManagerTriggerScope)
    scanManager._ts = manager
    scanManager._logger = _Logger()
    if events is not None:
        manager.sigScanStarted.connect(lambda: events.append('boundary'))
    return scanManager


def test_a_raster_start_returns_at_once_and_the_board_gets_the_same_commands(
        qtbot, board):
    manager, port = board(writeDelayS=0.05)
    scanManager = _scanManager(manager)

    # what the blocking path would send, for the comparison
    reference, referencePort = board()
    referencePort.written.clear()
    _scanManager(reference).runScan(_RASTER_PARAMS, 'rasterScan')
    expected = list(referencePort.written)
    port.written.clear()

    results = []
    start = time.monotonic()
    handle = scanManager.runScanAsync(
        _RASTER_PARAMS, 'rasterScan',
        lambda ok, message: results.append((ok, message)),
    )
    elapsed = time.monotonic() - start

    assert handle is not False
    assert elapsed < GUI_BOUND_S
    _waitFor(qtbot, results)
    assert results == [(True, '')]
    assert port.written == expected
    assert port.written[-1] == 'RASTER_SCAN'


def test_the_boundary_is_announced_after_the_parameters_and_before_the_command(
        qtbot, board):
    manager, port = board(writeDelayS=0.02)
    events = []
    scanManager = _scanManager(manager, events)
    port.written.clear()
    original = port.write

    def recording(command):
        original(command)
        events.append(f'sent:{command.strip()}')
    port.write = recording

    results = []
    scanManager.runScanAsync(
        _RASTER_PARAMS, 'rasterScan', lambda ok, m: results.append(ok))
    _waitFor(qtbot, results)

    sent = [e for e in events if e.startswith('sent:')]
    lastParameter = max(i for i, e in enumerate(events)
                        if e.startswith('sent:PARAMETER'))
    assert events.index('boundary') > lastParameter
    assert events.index('boundary') < events.index('sent:RASTER_SCAN')
    assert sent[-1] == 'sent:RASTER_SCAN'


def test_a_start_returns_promptly_even_while_a_silent_read_holds_the_lock(
        qtbot, board):
    """The pre-fix monitor shape: a read that holds the I/O lock. The GUI
    thread must not be the one waiting for it."""
    manager, port = board()
    scanManager = _scanManager(manager)
    port.readBlockS = 1.0
    threading.Thread(target=port.read, daemon=True).start()
    time.sleep(0.05)
    results = []

    start = time.monotonic()
    scanManager.runScanAsync(
        _RASTER_PARAMS, 'rasterScan', lambda ok, m: results.append(ok))
    assert time.monotonic() - start < GUI_BOUND_S

    _waitFor(qtbot, results)
    assert results == [True]


def test_a_configuration_error_raises_at_once_and_sends_nothing(board):
    manager, port = board()
    scanManager = _scanManager(manager)
    port.written.clear()
    params = dict(_RASTER_PARAMS)
    params['Digital'] = dict(_RASTER_PARAMS['Digital'], target_device=['NoSuch'])

    with pytest.raises(ValueError, match='no TriggerScope TTL line'):
        scanManager.runScanAsync(params, 'rasterScan', lambda ok, m: None)

    assert port.written == []


def test_an_unknown_scan_type_is_refused_and_sends_nothing(board):
    manager, port = board()
    port.written.clear()

    assert _scanManager(manager).runScanAsync(
        _RASTER_PARAMS, 'noSuchScan', lambda ok, m: None) is False
    assert port.written == []


def test_a_failure_part_way_through_the_upload_is_reported_and_no_scan_is_commanded(
        qtbot, board):
    manager, port = board(failOn='dimOneChan')
    events = []
    scanManager = _scanManager(manager, events)
    port.written.clear()
    results = []

    scanManager.runScanAsync(
        _RASTER_PARAMS, 'rasterScan', lambda ok, m: results.append((ok, m)))

    _waitFor(qtbot, results)
    assert results[0][0] is False
    assert 'RASTER_SCAN' not in port.written
    assert events == []   # no boundary for a scan that was never commanded


def test_cancelling_during_the_upload_sends_no_boundary_and_no_scan_command(
        qtbot, board):
    manager, port = board(writeDelayS=0.1)
    events = []
    scanManager = _scanManager(manager, events)
    port.written.clear()
    results = []

    handle = scanManager.runScanAsync(
        _RASTER_PARAMS, 'rasterScan', lambda ok, m: results.append((ok, m)))
    assert port.writeStarted.wait(2)
    handle.cancel()

    _waitFor(qtbot, results)
    assert results[0][0] is False
    assert 'RASTER_SCAN' not in port.written
    assert events == []
