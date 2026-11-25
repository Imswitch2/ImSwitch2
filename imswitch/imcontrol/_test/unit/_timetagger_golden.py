"""Record what a single FLIM detector does to the mock card, for a golden test.

``record(setup_path)`` loads a shipped setup, builds its ``TimeTaggerManager``
and its Time Tagger detectors against a recording mock card, runs two scans
(the second a repeat of the same geometry), and returns a JSON-ready record:
every conditioning call and every measurement constructed, in order, and a
digest of each final frame's products. Virtual channel numbers (the mock
hands them out from a process-wide counter) are renamed ``V1``, ``V2``... in
order of appearance, so the record does not depend on test order.

The checked-in ``timetagger_legacy_golden.json`` was recorded on the code
before the multi-detector change; ``test_timetagger_legacy_golden`` compares
against it. Regenerate only for an intended behaviour change::

    python -m imswitch.imcontrol._test.unit._timetagger_golden --write
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from imswitch.imcontrol.model.setupfolder import shippedSetupPath
from imswitch.imcontrol.model.interfaces import timetagger_mock as mock
from imswitch.imcontrol.model.managers._scan_execution import PARTICIPANTS_KEY

REPO = Path(__file__).resolve().parents[4]
SETUPS = REPO / "imswitch" / "_data" / "user_defaults" / "imcontrol_setups"
GOLDEN = Path(__file__).with_name("timetagger_legacy_golden.json")

SAMPLE_RATE = 100_000
DWELL_SAMPLES = 10
FLYBACK_SAMPLES = 50
NX, NY = 16, 8


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def disconnect(self, slot=None):
        if slot in self.slots:
            self.slots.remove(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


class _Nidaq:
    isSimulated = True

    def __init__(self):
        self.sigScanBuilt = _Signal()
        self.sigScanStarted = _Signal()
        self.sigScanDone = _Signal()
        self.sigScanStopped = _Signal()
        self.failures = []

    def reportScanBuildFailure(self, stage, error):
        self.failures.append((stage, repr(error)))


class _DetectorInfo:
    forAcquisition = True
    forFocusLock = False

    def __init__(self, props):
        self.managerProperties = dict(props)


def _ttl(ny=NY, nx=NX):
    line = np.zeros(nx * DWELL_SAMPLES + FLYBACK_SAMPLES, dtype=bool)
    line[:10] = True
    frame = np.zeros(line.size * ny, dtype=bool)
    frame[:10] = True
    return {"line_clock": np.tile(line, ny), "frame_start_clock": frame}


def _scan_info(names):
    return {
        PARTICIPANTS_KEY: list(names),
        "img_dims": [NX, NY],
        "img_axes_phys": ["x", "y"],
        "pixel_sizes": [1.0, 1.0],
        "dwell_time": DWELL_SAMPLES / SAMPLE_RATE,
        "n_linesteps": 1,
        "tot_scan_time_s": 1.0,
    }


class _Log:
    """The calls, with virtual channels renamed in order of appearance."""

    def __init__(self):
        self.calls = []
        self._virtual = {}

    def name(self, channel):
        channel = int(channel)
        if channel >= 1000:   # the mock's virtual channels start above 1000
            return self._virtual.setdefault(channel, f"V{len(self._virtual) + 1}")
        return channel

    def add(self, *entry):
        self.calls.append(list(entry))


def _recording_api(info, log: _Log):
    """A mock api whose card and measurement classes log what they are asked."""
    api = mock.MockTimeTaggerApi.from_info(info)
    original_create = api.createTimeTagger

    def create(serial=None):
        tagger = original_create(serial)
        for method in ("setTriggerLevel", "setDeadtime", "setInputDelay",
                       "setConditionalFilter", "setTestSignal", "setEventDivider"):
            real = getattr(tagger, method)

            def logged(*args, _real=real, _method=method, **kwargs):
                # Only the first argument is a channel (or a list of them).
                shown = [log.name(a) if i == 0 and isinstance(a, (int, np.integer)) else a
                         for i, a in enumerate(args)]
                shown += [f"{k}={v}" for k, v in sorted(kwargs.items())]
                log.add(_method, *shown)
                return _real(*args, **kwargs)

            setattr(tagger, method, logged)
        return tagger

    api.createTimeTagger = create

    class EventGenerator(mock.EventGenerator):
        def __init__(self, tagger, trigger_channel, pattern, *args, **kwargs):
            super().__init__(tagger, trigger_channel, pattern, *args, **kwargs)
            log.add("EventGenerator", log.name(self.getChannel()), int(trigger_channel),
                    [int(v) for v in np.asarray(pattern)[:3]], int(np.asarray(pattern).size))

    class Flim(mock.Flim):
        def __init__(self, tagger, **kwargs):
            super().__init__(tagger, **kwargs)
            shown = {k: (log.name(v) if k.endswith("_channel") else v) for k, v in kwargs.items()}
            log.add("Flim", {k: shown[k] for k in sorted(shown)})

    api.EventGenerator = EventGenerator
    api.Flim = Flim
    return api


def _digest(array) -> dict:
    a = np.asarray(array)
    rounded = np.round(a.astype(np.float64), 4)
    return {
        "shape": list(a.shape),
        "sum": round(float(rounded.sum()), 3),
        "sha": hashlib.sha256(rounded.tobytes()).hexdigest()[:16],
    }


def _load_setup(path: Path):
    from imswitch.imcontrol.model.SetupInfo import SetupInfo
    return SetupInfo.from_json(path.read_text(encoding="utf-8"), infer_missing=True)


def _run_scan(nidaq, detectors, log):
    from imswitch.imcontrol.model.managers.detectors.SwabianTimeTaggerManager import _TTFlimWorker

    log.add("-- scan --")
    nidaq.sigScanBuilt.emit(_scan_info([d.name for d in detectors]),
                            {"TTLCycleSignalsDict": _ttl()}, [])
    nidaq.sigScanStarted.emit()
    out = {}
    for det in detectors:
        # The thread the detector started is replaced by a synchronous run
        # of the same worker class, so the record is deterministic.
        try:
            det._teardownScanThread(timeoutMs=2000)
        except Exception:
            pass
        generation = det._preparedScanGeneration
        det._activeScanGeneration = generation
        worker = _TTFlimWorker(det, generation)
        frames = []
        worker.sigFrameReady.connect(lambda *args, _f=frames: _f.append(args))
        worker.sigFrameReady.connect(det._on_frame_ready)
        worker.sigTerminated.connect(det._onScanWorkerFinished)
        det._scanWorker = worker
        worker.signal_done()
        acks = []
        det.finishScan("graceful", lambda _a=acks: _a.append(True))
        worker.run()
        det._scanWorker = None
        final = [f for f in frames if f[2]]
        intensity, lifetime = (final[-1][0], final[-1][1]) if final else (np.zeros(0), np.zeros(0))
        out[det.name] = {
            "frames": len(frames),
            "final": len(final),
            "acked": len(acks),
            "intensity": _digest(intensity),
            "lifetime_ns": _digest(np.asarray(lifetime) * 1e9),
            "raw": _digest(det._image_raw) if det._image_raw is not None else None,
        }
    nidaq.sigScanDone.emit()
    log.add("-- scan done --")
    return out


def record(setup_name: str = "galvo_flim_mock_scan_setup.json", *, reverse: bool = False,
           t0_ps: int | None = None) -> dict:
    import dataclasses

    from imswitch.imcontrol.model.managers.TimeTaggerManager import TimeTaggerManager
    from imswitch.imcontrol.model.managers.detectors.SwabianTimeTaggerManager import (
        SwabianTimeTaggerManager,
    )

    setup = _load_setup(shippedSetupPath(setup_name))
    info = setup.timeTagger
    if reverse:
        info = dataclasses.replace(info, filterSyncByPhotons=True)
    log = _Log()
    nidaq = _Nidaq()
    api = _recording_api(info, log)
    log.add("-- connect --")
    card = TimeTaggerManager(info, SimpleNamespace(scan=SimpleNamespace(sampleRate=SAMPLE_RATE)),
                             nidaq, api=api)
    detectors = []
    for name, det_info in setup.detectors.items():
        if det_info.managerName != "SwabianTimeTaggerManager":
            continue
        props = dict(det_info.managerProperties)
        props["min_counts_per_pixel"] = 5
        if t0_ps is not None:
            props["t0_ps"] = int(t0_ps)
        detectors.append(SwabianTimeTaggerManager(_DetectorInfo(props), name, nidaq,
                                                  timeTaggerManager=card))
    first = _run_scan(nidaq, detectors, log)
    second = _run_scan(nidaq, detectors, log)
    for det in detectors:
        det.finalize()
    log.add("-- finalized --")
    return {
        "setup": setup_name,
        "reverse": reverse,
        "t0_ps": t0_ps,
        "calls": log.calls,
        "scans": [first, second],
        "failures": nidaq.failures,
        "metadata_roles": card.metadata().get("roles"),
    }


def record_all() -> dict:
    return {
        "forward": record(reverse=False),
        "reverse": record(reverse=True),
        "forward_t0": record(reverse=False, t0_ps=700),
    }


if __name__ == "__main__":
    data = record_all()
    if "--write" in sys.argv:
        GOLDEN.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {GOLDEN}")
    else:
        print(json.dumps(data, indent=1, sort_keys=True))
