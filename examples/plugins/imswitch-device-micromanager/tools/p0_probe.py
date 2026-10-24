"""Phase 0 spike of docs/design/plans/micromanager-bridge.md (section 5, P0).

Measures how the real MMCore (pymmcore-plus + the pip ``mm-test-adapters``
DemoCamera) behaves on the points the plan needs numbers for.  Hardware-free.

    PYTHONPATH=<worktree> <venv>/bin/python p0_probe.py [all|stream|slow|signals|shutdown|pixel]

``all`` (default) runs every section in its own subprocess, because the signal
backend and the Qt state are decided once per process.  Stdlib + numpy +
pymmcore_plus only.
"""
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime

PY = sys.executable
HERE = os.path.abspath(__file__)
SEQ = 2 ** 31 - 1                      # startSequenceAcquisition takes a C long


def make_core(second_camera=True):
    """Own CMMCorePlus on the demo config, both cameras 512x512 16 bit."""
    from mm_test_adapters import device_adapter_path
    from pymmcore_plus import CMMCorePlus
    core = CMMCorePlus(adapter_paths=[device_adapter_path()])
    core.loadSystemConfiguration(os.path.join(device_adapter_path(), "MMConfig_demo.cfg"))
    core.setAutoShutter(False)
    labels = ["Camera"]
    if second_camera:
        core.loadDevice("Cam2", "DemoCamera", "DCam")
        core.initializeDevice("Cam2")
        labels.append("Cam2")
    for lab in labels:
        core.setProperty(lab, "OnCameraCCDXSize", 512)
        core.setProperty(lab, "OnCameraCCDYSize", 512)
        core.setProperty(lab, "PixelType", "16bit")
    return core, labels


def pct(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))]


def gaps(numbers):
    """Lost frames inferred from ImageNumber continuity (+ ordering check)."""
    lost = sum(max(0, b - a - 1) for a, b in zip(numbers, numbers[1:]))
    ordered = all(b > a for a, b in zip(numbers, numbers[1:]))
    return lost, ordered


# --------------------------------------------------------------------- stream
def stream_once(core, labels, exposure_ms, seconds=5.0):
    for lab in labels:
        core.setExposure(lab, exposure_ms)
    core.clearCircularBuffer()
    wall0, perf0, got = {}, {}, {lab: [] for lab in labels}
    start = time.perf_counter()
    call_ms, first_ms = {}, {}
    for lab in labels:                  # unread frames of the first would be dropped by the second's start
        wall0[lab] = time.time()
        t = time.perf_counter()
        core.startSequenceAcquisition(lab, SEQ, 0, True)
        call_ms[lab] = (time.perf_counter() - t) * 1000
    age_el = {lab: [] for lab in labels}
    age_recv = {lab: [] for lab in labels}
    backlog_max = 0
    while time.perf_counter() - start < seconds:
        n = core.getRemainingImageCount()
        if n == 0:
            time.sleep(0.0002)
            continue
        backlog_max = max(backlog_max, n)
        img, md = core.popNextImageAndMD()
        now = time.time()
        lab = md["Camera"]
        got[lab].append(int(md["ImageNumber"]))
        first_ms.setdefault(lab, (time.perf_counter() - start) * 1000)
        age_el[lab].append((now - wall0[lab]) * 1000.0 - float(md["ElapsedTime-ms"]))
        recv = datetime.strptime(md["TimeReceivedByCore"], "%Y-%m-%d %H:%M:%S.%f").timestamp()
        age_recv[lab].append((now - recv) * 1000.0)
    elapsed = time.perf_counter() - start
    running = {lab: core.isSequenceRunning(lab) for lab in labels}
    overflow = core.isBufferOverflowed()
    for lab in labels:
        core.stopSequenceAcquisition(lab)
    left = core.getRemainingImageCount()
    core.clearCircularBuffer()
    print(f"  exposure {exposure_ms} ms, {len(labels)} camera(s), {elapsed:.2f} s, "
          f"still running at end={running}, isBufferOverflowed={overflow}, "
          f"max backlog seen={backlog_max} frames, left after stop={left}")
    for lab in labels:
        n = len(got[lab])
        lost, ordered = gaps(got[lab])
        a, r = age_el[lab], age_recv[lab]
        print(f"    {lab}: {n} frames = {n / elapsed:.1f} fps; ImageNumber {got[lab][0]}..{got[lab][-1]} "
              f"lost={lost} ordered={ordered}; startSequenceAcquisition call {call_ms[lab]:.1f} ms, "
              f"first frame {first_ms[lab]:.1f} ms after the first call began")
        print(f"      age@pop via ElapsedTime-ms (vs wall clock at start of that sequence): "
              f"median {statistics.median(a):.1f} ms, p95 {pct(a, .95):.1f} ms, min {min(a):.1f} ms")
        print(f"      age@pop via TimeReceivedByCore (wall clock stamp): "
              f"median {statistics.median(r):.1f} ms, p95 {pct(r, .95):.1f} ms, min {min(r):.1f} ms")


def section_stream():
    print("== stream: frame rate and drain latency, 512x512 16bit, pop loop 5 s")
    core, labels = make_core()
    print(f"  frame bytes {core.getImageBufferSize()} (camera 'Camera'), buffer {core.getCircularBufferMemoryFootprint()} MB")
    t = time.perf_counter()
    core.setExposure(labels[0], 1)
    core.startSequenceAcquisition(labels[0], SEQ, 0, True)
    while not core.getRemainingImageCount():
        time.sleep(0.0002)
    print(f"  cold first start (not part of the 5 s runs): call+first frame {(time.perf_counter() - t) * 1000:.0f} ms")
    core.stopSequenceAcquisition(labels[0])
    for exposure in (1, 5):
        for cams in (labels[:1], labels):
            stream_once(core, cams, exposure)


# ----------------------------------------------------------------------- slow
def section_slow():
    print("== slow: 1 MB buffer, stopOnOverflow=True, 1 ms exposure, consumer sleeps 50 ms between pops")
    core, labels = make_core(second_camera=False)
    cam = labels[0]
    core.setExposure(cam, 1)
    core.setCircularBufferMemoryFootprint(1)
    print(f"  buffer {core.getCircularBufferMemoryFootprint()} MB, capacity {core.getBufferTotalCapacity()} frames")
    core.startSequenceAcquisition(cam, SEQ, 0, True)
    numbers, errors, events = [], [], []
    stopped_at = overflow_at = None
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 3.0:
        time.sleep(0.05)
        t = time.perf_counter() - t0
        try:
            run, ovf, rem = core.isSequenceRunning(cam), core.isBufferOverflowed(), core.getRemainingImageCount()
        except Exception as exc:
            errors.append(f"state query @{t:.2f}s: {type(exc).__name__}: {exc}")
            continue
        if ovf and overflow_at is None:
            overflow_at = t
        if not run and stopped_at is None:
            stopped_at = t
        if len(events) < 6 or ovf != events[-1][2] or run != events[-1][1]:
            events.append((round(t, 2), run, ovf, rem))
        try:
            if rem:
                numbers.append(int(core.popNextImageAndMD()[1]["ImageNumber"]))
        except Exception as exc:
            errors.append(f"pop @{t:.2f}s: {type(exc).__name__}: {exc}")
    print(f"  (t s, running, overflowed, remaining) samples: {events[:3]} ... {events[-2:]}")
    print(f"  first isBufferOverflowed()=True at {overflow_at}; isSequenceRunning() first False at {stopped_at}")
    try:
        core.stopSequenceAcquisition(cam)
        print("  stopSequenceAcquisition after overflow-stop: no exception")
    except Exception as exc:
        errors.append(f"stop: {type(exc).__name__}: {exc}")
    tail = []
    while core.getRemainingImageCount():
        tail.append(int(core.popNextImageAndMD()[1]["ImageNumber"]))
    allnum = numbers + tail
    lost, ordered = gaps(allnum)
    print(f"  delivered {len(numbers)} during the run + {len(tail)} drained afterwards = {len(allnum)} frames; "
          f"ImageNumbers {allnum}; gaps inside delivered={lost}, ordered={ordered}")
    print(f"  exceptions from any call: {errors or 'none'}")
    try:
        core.popNextImageAndMD()
        print("  pop on empty buffer: returned a frame (unexpected)")
    except Exception as exc:
        print(f"  pop on empty buffer raises {type(exc).__name__}: {exc}")
    print(f"  after stop: isBufferOverflowed={core.isBufferOverflowed()}")
    core.setExposure(cam, 100)         # slow frames so a restart cannot overflow again within the check
    core.startSequenceAcquisition(cam, SEQ, 0, True)
    print(f"  restart WITHOUT clearCircularBuffer (1 MB, 100 ms exposure): isBufferOverflowed={core.isBufferOverflowed()}, "
          f"running={core.isSequenceRunning(cam)}")
    core.stopSequenceAcquisition(cam)
    core.clearCircularBuffer()
    print(f"  after clearCircularBuffer: isBufferOverflowed={core.isBufferOverflowed()}")
    core.setExposure(cam, 1)
    core.setCircularBufferMemoryFootprint(250)
    core.startSequenceAcquisition(cam, SEQ, 0, True)
    flag0, run0 = core.isBufferOverflowed(), core.isSequenceRunning(cam)
    time.sleep(0.05)
    print(f"  restart with 250 MB: right after start overflowed={flag0} running={run0}; "
          f"50 ms later overflowed={core.isBufferOverflowed()} running={core.isSequenceRunning(cam)}")
    core.stopSequenceAcquisition(cam)
    core.clearCircularBuffer()
    print(f"  restored buffer {core.getCircularBufferMemoryFootprint()} MB")


# -------------------------------------------------------------------- signals
def child_signals(mode):
    """mode: psygnal | auto | auto-noqt.  Runs in a fresh process."""
    os.environ.pop("PYMM_SIGNALS_BACKEND", None)
    if mode == "psygnal":
        os.environ["PYMM_SIGNALS_BACKEND"] = "psygnal"
    app = None
    if mode != "auto-noqt":
        from PyQt5.QtWidgets import QApplication
        app = QApplication([])
    core, _ = make_core(second_camera=False)
    name = type(core.events).__name__
    print(f"  [{mode}] QApplication={'yes' if app else 'no'}, "
          f"PYMM_SIGNALS_BACKEND={os.environ.get('PYMM_SIGNALS_BACKEND')}, type(core.events)={name} "
          f"({type(core.events).__module__})")
    if mode == "psygnal":
        assert name == "CMMCoreSignaler", name
    main = threading.get_ident()
    hits = []
    core.events.stagePositionChanged.connect(lambda dev, pos: hits.append((dev, pos, threading.get_ident())))
    for who, target in (("main thread", 25.0), ("worker thread", 40.0)):
        hits.clear()
        caller = [main]
        if who == "main thread":
            core.setPosition("Z", target)
        else:
            def work():
                caller.append(threading.get_ident())
                core.setPosition("Z", target)
            caller = []
            th = threading.Thread(target=work)
            th.start()
            th.join()
        core.waitForDevice("Z")
        t_wait = time.perf_counter()
        while not hits and time.perf_counter() - t_wait < 1.0:      # no event loop yet
            time.sleep(0.001)
        before = len(hits)
        delay_before = (time.perf_counter() - t_wait) * 1000 if hits else None
        t_pe = time.perf_counter()
        while app and not hits and time.perf_counter() - t_pe < 1.0:  # now pump the event loop
            app.processEvents()
            time.sleep(0.001)
        after = len(hits)
        ran = {("calling thread" if h[2] == caller[0] else "main thread" if h[2] == main else "another thread")
               for h in hits}
        delay = f"{delay_before:.1f} ms after waitForDevice" if delay_before is not None else "never within 1 s"
        print(f"  [{mode}] Z -> {target} called from the {who}: slot fired {before}x without an event loop "
              f"({delay}), {after}x after processEvents; slot thread {ran or 'n/a'}; payload {[h[:2] for h in hits][:2]}")


def section_signals():
    print("== signals: psygnal under a live QApplication, and what 'auto' picks")
    for mode in ("psygnal", "auto", "auto-noqt"):
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        r = subprocess.run([PY, HERE, "_signals", mode], env=env, capture_output=True, text=True)
        print(r.stdout.rstrip() or f"  [{mode}] no output")
        if r.returncode:
            print(f"  [{mode}] exit code {r.returncode}: {r.stderr.strip()[-300:]}")


# ------------------------------------------------------------------- shutdown
def child_shutdown(unload, applaunch):
    """unload: yes|no; applaunch: plain|import|disown (+ ':auto' = Qt signaler)."""
    applaunch, _, backend = applaunch.partition(":")
    if backend != "auto":
        os.environ["PYMM_SIGNALS_BACKEND"] = "psygnal"
    else:
        os.environ.pop("PYMM_SIGNALS_BACKEND", None)
    from PyQt5.QtWidgets import QApplication
    global APP, CORE
    APP = QApplication([])
    if applaunch != "plain":
        import imswitch.imcommon.applaunch as al
    CORE, _ = make_core()
    CORE.setExposure("Camera", 2)
    CORE.startSequenceAcquisition("Camera", SEQ, 0, True)
    time.sleep(0.3)
    print(f"child: backend={type(CORE.events).__name__} running={CORE.isSequenceRunning('Camera')}", flush=True)
    if unload == "yes":
        CORE.stopSequenceAcquisition()
        CORE.unloadAllDevices()
    if applaunch == "disown":
        al.disownQtObjects(APP)
    # fall off the end: module globals (core, QApplication) die in interpreter finalization


def section_shutdown(repeats=3):
    print(f"== shutdown: exit code of a subprocess that ends with a live QApplication, loaded devices, a running sequence ({repeats} runs each)")
    home = tempfile.mkdtemp(prefix="p0home")        # importing imswitch syncs ~/ImSwitchConfig
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", HOME=home)
    cases = [(u, a) for a in ("plain", "import", "disown") for u in ("yes", "no")]
    cases += [("no", "plain:auto"), ("no", "disown:auto")]
    for unload, how in cases:
        codes, last = [], ""
        for _ in range(repeats):
            r = subprocess.run([PY, HERE, "_shutdown", unload, how], env=env, capture_output=True, text=True, timeout=120)
            codes.append(r.returncode if "child: backend=" in r.stdout and "running=True" in r.stdout else f"{r.returncode}(child did not reach the exit)")
            if r.returncode and r.stderr.strip():
                last = r.stderr.strip().splitlines()[-1][:120]
        label = {"plain": "no imswitch", "import": "applaunch imported", "disown": "applaunch imported + disownQtObjects(app)"}[how.split(":")[0]]
        sig = "Qt signaler (auto)" if ":auto" in how else "psygnal"
        print(f"  {label:42s} {sig:18s} unload={unload:3s} -> exit codes {codes}" + (f"  stderr: {last}" if last else ""))


# ----------------------------------------------------------------- pixel size
def section_pixel():
    print("== pixel size on the demo config")
    core, _ = make_core(second_camera=False)
    print(f"  after loadSystemConfiguration: getPixelSizeUm()={core.getPixelSizeUm()} "
          f"current pixel config={core.getCurrentPixelSizeConfig()!r}, available={core.getAvailablePixelSizeConfigs()}, "
          f"Objective label={core.getStateLabel('Objective')!r}")
    for res in core.getAvailablePixelSizeConfigs():
        print(f"    pixel config {res}: PixelSizeUm by id = {core.getPixelSizeUmByID(res)}")
    for preset in ("20X", "40X", "10X"):
        core.setConfig("Objective", preset)
        core.waitForSystem()
        print(f"  setConfig('Objective','{preset}') -> getPixelSizeUm()={core.getPixelSizeUm()} "
              f"(cached {core.getPixelSizeUm(True)}), config={core.getCurrentPixelSizeConfig()!r}")
    core.setProperty("Camera", "Binning", 2)
    print(f"  Camera Binning=2 at 10X -> getPixelSizeUm()={core.getPixelSizeUm()}")
    core.setProperty("Camera", "Binning", 1)
    core.setConfig("Objective", "10X")
    core.setState("Objective", 5)
    print(f"  Objective at a position with no pixel config (state 5) -> getPixelSizeUm()={core.getPixelSizeUm()}, "
          f"config={core.getCurrentPixelSizeConfig()!r}")


SECTIONS = {"stream": section_stream, "slow": section_slow, "signals": section_signals,
            "shutdown": section_shutdown, "pixel": section_pixel}


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "all"
    if cmd == "_signals":
        return child_signals(argv[2])
    if cmd == "_shutdown":
        return child_shutdown(argv[2], argv[3])
    os.environ.setdefault("PYMM_SIGNALS_BACKEND", "psygnal")   # before pymmcore_plus is imported
    if cmd == "all":
        import pymmcore_plus
        import mm_test_adapters
        print(f"pymmcore-plus {pymmcore_plus.__version__}, python {sys.version.split()[0]}, "
              f"adapters {mm_test_adapters.device_adapter_path()}")
        for name in SECTIONS:
            r = subprocess.run([PY, HERE, name], env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
            if r.returncode:
                print(f"!! section {name} exited with {r.returncode}")
                return 1
        return 0
    SECTIONS[cmd]()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
