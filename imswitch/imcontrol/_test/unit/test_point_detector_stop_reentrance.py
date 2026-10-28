"""A point detector's teardown completes the scan only once it has finished.

On the rig, the end of a Live run hung for 2-4 s and quarantined an APD
("APD scan teardown did not finish within 2 s"): the last detector to close
its NI input task did so inside its own teardown, ``inputTaskDone`` reported
the scan done right there, and the final scan-done released the detector
leases -- whose bounded stop, on another thread, waited on that very
teardown while the teardown waited for the release to return. The mock never
closes an input task, so only a non-simulated teardown shows it.
"""
import threading
import time
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.managers.detectors import APDManager as apd_module
from imswitch.imcontrol.model.managers.detectors import PMTManager as pmt_module
from .test_frame_publication import _apd, _pmt

GENERATION = 5
#: DetectorsManager.DETECTOR_STOP_TIMEOUT_MS, the bound a lease release waits.
STOP_TIMEOUT_S = 2.0


class _Nidaq:
    """The NI-DAQ manager as a detector's teardown sees it: one input task,
    and a scan-done whose listener releases the detector's lease."""

    def __init__(self):
        self.open = True
        self.done = False
        self.onScanDone = None

    def inputTaskDone(self, name, generation, completeScan=True):
        self.open = False
        if completeScan:
            self.completeScanIfIdle()
        return True

    def completeScanIfIdle(self):
        if self.open or self.done:
            return False
        self.done = True
        self.onScanDone()
        return True


class _Thread:
    def quit(self):
        pass

    def wait(self, timeoutMs=None):
        return True

    def isRunning(self):
        return False


def _releaseStopsBounded(manager, outcome):
    """What DetectorsManager.release does with the last lease: stop the
    detector on its own thread and wait for it, bounded."""
    def stop():
        try:
            manager.stopAcquisition()
        except Exception as error:
            outcome['error'] = error

    thread = threading.Thread(target=stop, daemon=True)
    thread.start()
    thread.join(STOP_TIMEOUT_S)
    outcome['stopped'] = not thread.is_alive()


@pytest.mark.parametrize('make, workerClass', [
    (_apd, apd_module.ScanWorker),
    (_pmt, pmt_module.ScanWorker),
], ids=['APD', 'PMT'])
def test_the_scan_done_release_does_not_wait_on_the_teardown_it_runs_in(
        qtbot, make, workerClass):
    manager = make()
    nidaq = _Nidaq()
    outcome = {}
    nidaq.onScanDone = lambda: _releaseStopsBounded(manager, outcome)
    manager._nidaqManager = nidaq
    manager._simulation_mode = False
    worker = SimpleNamespace(
        _manager=manager, _name=manager.name, _inputTaskGeneration=GENERATION,
        _inputTaskClosed=False, scanning=False, scanGeneration=GENERATION,
    )
    worker.close = lambda: workerClass.close(worker)
    manager._scanWorker, manager._scanThread = worker, _Thread()
    manager._activeScanGeneration = GENERATION

    started = time.monotonic()
    manager.stopAcquisitionLocal(GENERATION)      # the worker's acqDoneSignal
    elapsed = time.monotonic() - started

    assert nidaq.done
    assert outcome == {'stopped': True}
    assert elapsed < STOP_TIMEOUT_S / 4
