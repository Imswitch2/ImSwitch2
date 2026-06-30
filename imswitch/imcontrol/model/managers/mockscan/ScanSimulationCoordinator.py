import time
from dataclasses import dataclass

import numpy as np

from imswitch.imcommon.framework import Signal, SignalInterface, Thread


@dataclass(frozen=True)
class SimulatedScanPlan:
    """Per-scan plan for software-only NI-DAQ simulation."""

    frameCounts: dict
    duration: float
    nPositions: int
    samplesTotal: int

    @classmethod
    def fromScan(cls, setupInfo, signalDict, scanInfoDict):
        ttlDict = (signalDict or {}).get('TTLCycleSignalsDict', {}) or {}
        try:
            imgDims = list((scanInfoDict or {}).get('img_dims', []) or [])
            nPositions = int(np.prod(imgDims)) if imgDims else 0
            samplesTotal = int((scanInfoDict or {}).get('scan_samples_total', 0) or 0)
        except Exception:
            nPositions, samplesTotal = 0, 0

        sampleRate = float(getattr(setupInfo.scan, 'sampleRate', 0) or 0)
        duration = samplesTotal / sampleRate if sampleRate > 0 else 0.0

        frameCounts = {}
        for detectorName, detectorInfo in (setupInfo.detectors or {}).items():
            if not getattr(detectorInfo, 'forAcquisition', True):
                continue

            nFrames = cls._countRisingEdges(ttlDict.get(detectorName))
            if nFrames <= 0:
                # No explicit camera TTL in the scan signal. For scan recordings
                # the expected fallback is one frame per physical scan position.
                nFrames = nPositions
            if nFrames > 0:
                frameCounts[detectorName] = int(nFrames)

        return cls(
            frameCounts=frameCounts,
            duration=float(duration),
            nPositions=int(nPositions),
            samplesTotal=int(samplesTotal),
        )

    @staticmethod
    def _countRisingEdges(ttl):
        if ttl is None:
            return 0
        try:
            ttlArr = np.asarray(ttl).astype(np.int8, copy=False)
            if ttlArr.size < 1:
                return 0
        except Exception:
            return 0
        return int(np.count_nonzero(np.diff(ttlArr, prepend=0) == 1))


class ScanSimulationCoordinator(SignalInterface):
    """Owns software-only scan timing and virtual frame-trigger dispatch."""

    sigFrameTrigger = Signal(str, int)
    sigDone = Signal()

    def __init__(self, setupInfo):
        super().__init__()
        self._setupInfo = setupInfo
        self._worker = None
        self._activePlan = None

    @property
    def activePlan(self):
        return self._activePlan

    @property
    def isRunning(self):
        return self._worker is not None and self._worker.isRunning()

    @property
    def isActive(self):
        return self._worker is not None

    def start(self, signalDict, scanInfoDict):
        self.stop(wait=True)
        self._activePlan = SimulatedScanPlan.fromScan(
            self._setupInfo, signalDict, scanInfoDict
        )
        self._worker = SimulatedScanWorker(self._activePlan)
        self._worker.sigFrameTrigger.connect(self.sigFrameTrigger)
        self._worker.sigDone.connect(self.sigDone)
        self._worker.finished.connect(self._clearFinishedWorker)
        self._worker.start()
        return self._activePlan

    def stop(self, wait=False):
        worker = self._worker
        if worker is None:
            return
        worker.stop()
        worker.quit()
        if wait and worker.isRunning():
            worker.wait()
        if not worker.isRunning():
            self._worker = None

    def _clearFinishedWorker(self):
        self._worker = None


class SimulatedScanWorker(Thread):
    """Runs one software-only scan plan.

    Frame triggers are spread across a capped wall-clock duration so consumers
    see progressive arrival rather than one burst at scan end.
    """

    sigFrameTrigger = Signal(str, int)
    sigDone = Signal()

    _MAX_DURATION = 5.0
    _MIN_DURATION = 0.2
    _TICK = 0.03

    def __init__(self, plan):
        super().__init__()
        self._plan = plan
        self._running = True

    def stop(self):
        self._running = False

    def run(self):
        duration = min(
            self._MAX_DURATION,
            max(self._MIN_DURATION, float(self._plan.duration)),
        )
        nTicks = max(1, int(round(duration / self._TICK)))
        delivered = {det: 0 for det in self._plan.frameCounts}

        for tick in range(1, nTicks + 1):
            if not self._running:
                return
            time.sleep(self._TICK)
            for detectorName, total in self._plan.frameCounts.items():
                want = (int(total) * tick) // nTicks
                n = want - delivered[detectorName]
                if n > 0:
                    delivered[detectorName] += n
                    self.sigFrameTrigger.emit(detectorName, int(n))

        if not self._running:
            return

        for detectorName, total in self._plan.frameCounts.items():
            n = int(total) - delivered[detectorName]
            if n > 0:
                self.sigFrameTrigger.emit(detectorName, int(n))
        self.sigDone.emit()
