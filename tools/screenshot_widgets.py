"""Auto-screenshot every Imswitch2 widget for the docs.

Runs a headless (offscreen) Qt application, walks
``imswitch.imcontrol.view.widgets`` for every ``Widget`` subclass, tries
to instantiate it with stub arguments, and writes a PNG to
``docs/images/auto/<WidgetName>.png``.

Usage
-----

Regenerate every widget screenshot::

    python tools/screenshot_widgets.py

Regenerate one widget only::

    python tools/screenshot_widgets.py LaserWidget

Pass ``--show`` to render to the real desktop instead of offscreen (use
when you want to capture a tooltip, menu, or other transient UI).

Notes
-----

* Widgets are constructed *standalone*, with no controller, no
  hardware, and no signals wired up.  That means they render their
  static layout (buttons, sliders, headers) but the live elements that
  controllers add at runtime (one row per laser, plots, populated
  combo boxes) will be empty.  This is intentional: the goal is to
  keep the layouts in the docs in sync with the code without depending
  on a particular setup file.
* Widgets that need constructor arguments are skipped with a logged
  reason.  When you add a new widget that needs arguments, add a stub
  entry to ``WIDGET_STUBS`` below.
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import os
import pkgutil
import subprocess
import sys
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# The offscreen platform has no OpenGL surface; force the software path so
# QOpenGLWidget-based pages (vispy / pyqtgraph GL) don't print warnings.
os.environ.setdefault("QT_OPENGL", "software")
os.environ.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
# Quiet Qt's category logger – we'll surface real errors via our own prints.
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false")
# Tell NapariHybridWidget we're in the full-app context, so it renders an
# error frame instead of raising when a napariViewer isn't supplied.
os.environ.setdefault("IMSWITCH_FULL_APP", "1")

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "docs" / "images" / "auto"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def _import_widget_module(modname: str):
    try:
        return importlib.import_module(modname)
    except Exception as exc:  # noqa: BLE001 - we want any import failure logged
        print(f"  skip {modname}: import failed ({exc})")
        return None


def _discover_widget_classes():
    from imswitch.imcontrol.view.widgets.basewidgets import Widget

    pkg = importlib.import_module("imswitch.imcontrol.view.widgets")
    for _finder, modname, _ispkg in pkgutil.iter_modules(pkg.__path__):
        full = f"{pkg.__name__}.{modname}"
        mod = _import_widget_module(full)
        if mod is None:
            continue
        for name, obj in inspect.getmembers(mod, inspect.isclass):
            if obj.__module__ != full:
                continue
            if not issubclass(obj, Widget) or obj is Widget:
                continue
            if inspect.isabstract(obj):
                continue
            yield name, obj


# Constructor stubs for widgets that need positional args beyond `options`.
# Map: WidgetClassName -> dict of kwargs. The stub is materialized lazily so
# we can build heavyweight objects (mock napariViewers, …) only when needed.
def _stub_napari_viewer():
    """A duck-typed napari viewer good enough for __post_init__ paths.

    Real napari init crashes in offscreen mode; a Mock with the right
    attribute graph is enough for widgets that only call
    ``self.addItemToViewer(...)``, which is itself wrapped in a try-except.
    """
    from unittest.mock import MagicMock
    return MagicMock(name="napariViewerStub")


def _widget_stub_kwargs(name: str) -> dict:
    if name == "ULensesWidget":
        return {"napariViewer": _stub_napari_viewer()}
    return {}


WIDGET_STUBS = ("ULensesWidget",)  # widgets that need extra kwargs; see _widget_stub_kwargs


# ---------------------------------------------------------------------------
# Populators
#
# After a widget is constructed standalone, the screenshot would otherwise
# show an empty frame: the controller (which lives in
# imswitch.imcontrol.controller.controllers) is what normally calls
# `addLaser(...)`, `initControls(...)`, etc. to give the widget content.
#
# Each populator below mimics what the corresponding controller does on
# init, with a small set of fake devices so the screenshot is realistic
# but self-contained.  Register more by adding `"ClassName": _populate_X`
# to POPULATORS.  Populator failures are caught and logged but don't fail
# the capture — an empty layout still beats no screenshot.
# ---------------------------------------------------------------------------


def _populate_laser(w) -> None:
    """Mimic LaserController init: add a few representative laser rows."""
    w.addLaser(
        laserName="405 nm", valueUnits="mW", valueDecimals=1,
        wavelength=405, valueRange=(0, 200), valueRangeStep=1,
    )
    w.addLaser(
        laserName="488 nm", valueUnits="mW", valueDecimals=1,
        wavelength=488, valueRange=(0, 100), valueRangeStep=1,
    )
    w.addLaser(
        laserName="561 nm", valueUnits="mW", valueDecimals=1,
        wavelength=561, valueRange=(0, 150), valueRangeStep=1,
        frequencyRange=(1, 1000, 50),  # modulation-capable laser
    )
    w.addLaser(
        laserName="640 nm", valueUnits="mW", valueDecimals=1,
        wavelength=640, valueRange=(0, 200), valueRangeStep=1,
    )


def _populate_positioner(w) -> None:
    """Mimic PositionerController init: add X/Y/Z axes and a stage."""
    w.addPositioner("X", ["X"], speed=None, joystick=None)
    w.addPositioner("Y", ["Y"], speed=None, joystick=None)
    w.addPositioner("Z", ["Z"], speed=None, joystick=None)


def _populate_scan(w) -> None:
    """Mimic ScanController init for the concrete scan widgets.

    Calls `initControls(positionerNames, TTLDeviceNames, ...)` with a
    representative setup so the scan-parameter grid populates.
    """
    positioners = ["X", "Y", "Z"]
    ttl_devices = ["405 nm", "488 nm", "561 nm", "640 nm"]
    # Different concrete subclasses have slightly different signatures.
    try:
        w.initControls(positioners, ttl_devices, "ms")
    except TypeError:
        # ScanWidgetPointScan.initControls(positionerNames, TTLDeviceNames)
        w.initControls(positioners, ttl_devices)


def _populate_recording(w) -> None:
    w.setDetectorList([("Hamamatsu Orca-Flash 4.0", "Camera0"),
                       ("APD-PMT counter", "APD1")])


def _populate_settings(w) -> None:
    """Add a fake camera with a small set of viewable parameters."""
    from imswitch.imcontrol.model.managers.detectors.DetectorManager import (
        DetectorNumberParameter, DetectorListParameter,
    )
    params = {
        "Exposure": DetectorNumberParameter(
            group="Misc", value=10.0, editable=True, valueUnits="ms"),
        "Gain": DetectorNumberParameter(
            group="Misc", value=1.0, editable=True, valueUnits=""),
        "Image height": DetectorNumberParameter(
            group="Image", value=2048, editable=True, valueUnits="px"),
        "Image width": DetectorNumberParameter(
            group="Image", value=2048, editable=True, valueUnits="px"),
        "Trigger source": DetectorListParameter(
            group="Acquisition", value="Internal", editable=True,
            options=["Internal", "External", "Software"]),
    }
    w.addDetector(
        detectorName="Camera0",
        detectorModel="Hamamatsu Orca-Flash 4.0",
        detectorParameters=params,
        detectorActions={},
        supportedBinnings=[1, 2, 4],
        roiInfos={},
    )


def _populate_focuslock(w) -> None:
    """Set proportional/integral gains; mimic FocusLockController init."""
    w.setKp(0.05)
    w.setKi(0.01)


def _populate_rotator(w) -> None:
    w.addRotator("Half-wave plate")
    w.addRotator("Quarter-wave plate")


def _populate_fft(w) -> None:
    """Render a synthetic FFT-looking image so the widget isn't empty."""
    import numpy as np
    n = 256
    yy, xx = np.mgrid[-n//2:n//2, -n//2:n//2].astype(np.float32)
    r = np.hypot(xx, yy)
    # Bright DC + a couple of ring features → looks like a real FFT magnitude.
    img = (
        np.exp(-(r / 4) ** 2) * 6
        + np.exp(-((r - 40) / 6) ** 2) * 2
        + np.exp(-((r - 80) / 8) ** 2) * 1
    )
    rng = np.random.default_rng(0)
    img += rng.standard_normal(img.shape) * 0.05
    w.setImage(img.astype(np.float32))
    w.updateImageLimits(n, n)
    w.updatePosLines(0.5, n, n)


def _populate_flim_hist(w) -> None:
    """Push a synthetic lifetime distribution into the histogram."""
    import numpy as np
    rng = np.random.default_rng(0)
    # Bimodal: a slow (~3.5 ns) and a fast (~1.2 ns) population.
    sample = np.concatenate([
        rng.normal(3.5, 0.4, 4000),
        rng.normal(1.2, 0.2, 2000),
    ])
    sample = sample[sample > 0]
    w.updateHistogram(sample.astype(np.float32))


def _populate_beadrec(w) -> None:
    """Drop a synthetic bead reconstruction image into the viewport."""
    import numpy as np
    rng = np.random.default_rng(42)
    img = np.zeros((128, 128), dtype=np.float32)
    # Sprinkle gaussian beads.
    yy, xx = np.mgrid[:128, :128]
    for _ in range(15):
        cy, cx = rng.integers(10, 118, size=2)
        sigma = rng.uniform(1.5, 3.5)
        amp = rng.uniform(0.6, 1.0)
        img += amp * np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sigma ** 2)))
    img += rng.standard_normal(img.shape) * 0.02
    w.updateImage(img)


def _populate_alignment_line(w) -> None:
    w.setLineAngle(30)
    w.setLineVisibility(True)


def _populate_align_average(w) -> None:
    """Push a synthetic time-series into the average-over-ROI plot."""
    import numpy as np
    t = np.linspace(0, 6 * np.pi, 200)
    series = 50 + 8 * np.sin(t) + np.random.default_rng(1).standard_normal(200)
    for v in series:
        w.updateGraph(float(v))


def _populate_align_xy(w) -> None:
    """Push a synthetic single-axis trace."""
    import numpy as np
    t = np.linspace(0, 4 * np.pi, 150)
    series = 0.4 * np.sin(t * 1.3) + np.random.default_rng(2).standard_normal(150) * 0.05
    for v in series:
        w.updateGraph(float(v))


def _populate_tiling(w) -> None:
    w.setLabel("Idle")
    w.setProgress(0, 9)
    w.setRunning(False)


def _populate_leicastand(w) -> None:
    w.setCubeChoices(["DAPI", "GFP", "RFP", "Cy5", "Empty"])
    w.setCurrentCube("GFP")
    w.setMode("Manual")
    w.setCurrentPortSide("Right")
    w.setConnected(True)


def _populate_bftimelapse(w) -> None:
    w.setDetectorList(["Camera0", "APD1"])


def _populate_rotation_scan(w) -> None:
    w.initControls()
    try:
        w.setPolPositionText("0.0°")
    except Exception:
        pass


def _populate_watcher(w) -> None:
    """Best-effort: trigger a file-list refresh so the empty state renders."""
    try:
        w.updateFileList()
    except Exception:
        pass


def _populate_ulenses(w) -> None:
    """Drop a fake lens-grid onto the (mock) scatter visual."""
    import numpy as np
    xs, ys = np.meshgrid(np.arange(0, 1000, 100), np.arange(0, 1000, 100))
    w.setData(xs.flatten(), ys.flatten())


POPULATORS = {
    "LaserWidget": _populate_laser,
    "PositionerWidget": _populate_positioner,
    "ScanWidgetBase": _populate_scan,
    "ScanWidgetAdvanced": _populate_scan,
    "ScanWidgetMoNaLISA": _populate_scan,
    "ScanWidgetPointScan": _populate_scan,
    "RecordingWidget": _populate_recording,
    "SettingsWidget": _populate_settings,
    "FocusLockWidget": _populate_focuslock,
    "RotatorWidget": _populate_rotator,
    "FFTWidget": _populate_fft,
    "FLIMHistWidget": _populate_flim_hist,
    "BeadRecWidget": _populate_beadrec,
    "AlignmentLineWidget": _populate_alignment_line,
    "AlignAverageWidget": _populate_align_average,
    "AlignXYWidget": _populate_align_xy,
    "TilingWidget": _populate_tiling,
    "LeicaStandWidget": _populate_leicastand,
    "BFTimelapseWidget": _populate_bftimelapse,
    "RotationScanWidget": _populate_rotation_scan,
    "WatcherWidget": _populate_watcher,
    "ULensesWidget": _populate_ulenses,
}


def _populate(widget, name: str) -> None:
    populator = POPULATORS.get(name)
    if populator is None:
        return
    try:
        populator(widget)
    except Exception as exc:  # noqa: BLE001
        print(f"    populate({name}) skipped: {type(exc).__name__}: {exc}")
        if os.environ.get("IMSWITCH_SCREENSHOT_DEBUG"):
            traceback.print_exc(limit=2)


def _get_options():
    """Return a real ``Options`` dataclass for widget construction.

    Several widgets read ``self._options.recording.outputFolder`` /
    ``self._options.watcher.outputFolder`` at __init__ time, so a bare
    dict won't do.  ``optionsBasic`` is a fully populated test fixture.
    """
    from imswitch.imcontrol._test import optionsBasic
    return optionsBasic


def _instantiate(cls):
    """Try increasingly forgiving constructor signatures.

    Construction errors other than a signature mismatch (e.g. a widget
    calling a deprecated pyqtgraph API at __init__ time) are raised to
    the caller so they show up in the summary and the widget is skipped.
    """
    options = _get_options()
    if cls.__name__ in WIDGET_STUBS:
        return cls(options, **_widget_stub_kwargs(cls.__name__))
    # Most widgets: Widget.__init__(self, options, *args, **kwargs)
    try:
        return cls(options)
    except TypeError as exc:
        # Distinguish "wrong number of args" (try the no-arg form) from a
        # TypeError raised deeper inside the constructor (propagate it).
        if "argument" not in str(exc) and "positional" not in str(exc):
            raise
    # Some subclasses use *args/**kwargs and don't forward options
    return cls()


def _grab(widget, path: Path) -> None:
    widget.adjustSize()
    size = widget.sizeHint()
    if size.isValid() and size.width() > 0 and size.height() > 0:
        widget.resize(size)
    else:
        widget.resize(480, 320)
    widget.show()
    # Force a render pass without entering the event loop
    from qtpy import QtCore
    QtCore.QCoreApplication.processEvents(
        QtCore.QEventLoop.AllEvents, 200
    )
    pixmap = widget.grab()
    pixmap.save(str(path), "PNG")
    widget.hide()


def _capture_mock_setup(setup_name: str) -> int:
    """Spin up the full Imswitch2 main view with a mock setup and grab every dock.

    Uses the same ``prepareUI`` helper as the UI tests, so each widget is
    constructed with its controller wired up: laser rows are populated
    from the setup's ``lasers`` dict, positioner axes from ``positioners``,
    etc.  Output filenames are prefixed with ``mock-<dockKey>.png``.
    """
    from qtpy import QtCore, QtWidgets

    from imswitch.imcommon.model.dirtools import DataFileDirs
    from imswitch.imcontrol.view import ViewSetupInfo
    from imswitch.imcontrol._test import optionsBasic
    from imswitch.imcontrol._test.ui import getApp, prepareUI

    setup_path = Path(DataFileDirs.UserDefaults) / "imcontrol_setups" / setup_name
    if not setup_path.exists():
        print(f"  ERROR: setup not found: {setup_path}")
        return 2
    setup = ViewSetupInfo.from_json(setup_path.read_text())

    app = getApp()
    main_view = prepareUI(optionsBasic, setup)

    main_view.resize(1400, 900)
    main_view.show()
    QtCore.QCoreApplication.processEvents(QtCore.QEventLoop.AllEvents, 500)
    # Let any deferred painting / napari init settle.
    QtCore.QThread.msleep(200)
    QtCore.QCoreApplication.processEvents(QtCore.QEventLoop.AllEvents, 500)

    successes = failures = 0

    # 1) the whole window
    try:
        main_view.grab().save(str(OUT_DIR / "mock-main-window.png"), "PNG")
        print(f"  wrote {(OUT_DIR / 'mock-main-window.png').relative_to(ROOT)}")
        successes += 1
    except Exception as exc:  # noqa: BLE001
        print(f"  skip mock-main-window: {type(exc).__name__}: {exc}")
        failures += 1

    # 2) each dock individually
    for key, widget in getattr(main_view, "widgets", {}).items():
        path = OUT_DIR / f"mock-{key}.png"
        try:
            widget.adjustSize()
            QtCore.QCoreApplication.processEvents(QtCore.QEventLoop.AllEvents, 100)
            pix = widget.grab()
            if pix.isNull() or pix.width() < 4 or pix.height() < 4:
                raise RuntimeError("empty pixmap (widget may be hidden inside a tab)")
            pix.save(str(path), "PNG")
            print(f"  wrote {path.relative_to(ROOT)}")
            successes += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  skip mock-{key}: {type(exc).__name__}: {exc}")
            if os.environ.get("IMSWITCH_SCREENSHOT_DEBUG"):
                traceback.print_exc(limit=3)
            failures += 1

    main_view.close()
    app.processEvents()
    print(f"\nMock-setup done: {successes} written, {failures} skipped → {OUT_DIR.relative_to(ROOT)}")
    return 0 if failures == 0 else 1


def _capture_one(name: str) -> int:
    """Render a single widget by class name (called in a child process)."""
    # Qt imports must follow the env-var setup at module top.
    from qtpy import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

    target = None
    for cand_name, cls in _discover_widget_classes():
        if cand_name == name:
            target = cls
            break
    if target is None:
        print(f"  skip {name}: not found in widgets package")
        return 2

    out = OUT_DIR / f"{name}.png"
    try:
        widget = _instantiate(target)
        _populate(widget, name)
        _grab(widget, out)
        widget.deleteLater()
        app.processEvents()
        print(f"  wrote {out.relative_to(ROOT)}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"  skip {name}: {type(exc).__name__}: {exc}")
        if os.environ.get("IMSWITCH_SCREENSHOT_DEBUG"):
            traceback.print_exc(limit=3)
        return 1


def _enumerate_names() -> list[str]:
    return [name for name, _cls in _discover_widget_classes()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "only",
        nargs="*",
        help="Widget class names to screenshot (default: all).",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Render on the real display instead of offscreen.",
    )
    parser.add_argument(
        "--no-subprocess",
        action="store_true",
        help=(
            "Render every widget in the current process.  Faster, but a "
            "single C-level crash (e.g. SIGSEGV from an OpenGL widget) "
            "kills the whole run.  Default is one subprocess per widget."
        ),
    )
    parser.add_argument(
        "--single",
        metavar="WIDGET",
        help="Internal: render exactly one widget then exit (used by subprocess mode).",
    )
    parser.add_argument(
        "--single-mock",
        metavar="SETUP_JSON",
        help="Internal: run the mock-setup capture once in this process and exit.",
    )
    parser.add_argument(
        "--mock-setup",
        metavar="SETUP_JSON",
        nargs="?",
        const="example_no_hardware.json",
        help=(
            "Also launch Imswitch2 with the given mock setup file "
            "(default: example_no_hardware.json) and screenshot every "
            "populated dock as docs/images/auto/mock-<dock>.png."
        ),
    )
    parser.add_argument(
        "--mock-only",
        action="store_true",
        help="Skip per-widget capture; only run --mock-setup.",
    )
    args = parser.parse_args()

    if args.show:
        os.environ.pop("QT_QPA_PLATFORM", None)

    if args.single:
        return _capture_one(args.single)
    if args.single_mock:
        return _capture_mock_setup(args.single_mock)

    mock_rc = 0
    if args.mock_setup or args.mock_only:
        setup_name = args.mock_setup or "example_no_hardware.json"
        # Run the full-app pass in a subprocess too: napari/vispy teardown
        # leaks resources on exit and can taint a follow-up Qt run.
        cmd = [sys.executable, str(Path(__file__).resolve()), "--single-mock", setup_name]
        if args.show:
            cmd.append("--show")
        try:
            proc = subprocess.run(cmd, env=os.environ.copy(), timeout=120)
            mock_rc = proc.returncode if proc.returncode >= 0 else 1
            if proc.returncode < 0:
                print(f"  mock setup: child killed by signal {-proc.returncode}")
        except subprocess.TimeoutExpired:
            print("  mock setup: timeout after 120s")
            mock_rc = 1
        if args.mock_only:
            return mock_rc

    selected = set(args.only) if args.only else None
    names = [n for n in _enumerate_names() if not selected or n in selected]

    if args.no_subprocess:
        from qtpy import QtWidgets

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
        successes = failures = 0
        for name in names:
            rc = _capture_one(name)
            if rc == 0:
                successes += 1
            else:
                failures += 1
        app.processEvents()
        print(f"\nDone: {successes} written, {failures} skipped → {OUT_DIR.relative_to(ROOT)}")
        return 0 if failures == 0 else 1

    # Subprocess-per-widget: isolates crashes (SIGSEGV from OpenGL contexts
    # in headless mode, vispy/napari teardown, …) so one bad widget never
    # stops the rest.
    successes = failures = 0
    for name in names:
        try:
            proc = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--single", name]
                + (["--show"] if args.show else []),
                env=os.environ.copy(),
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            print(f"  skip {name}: timeout after 60s")
            failures += 1
            continue
        if proc.returncode == 0:
            successes += 1
        elif proc.returncode < 0:
            # Negative on POSIX = killed by signal (e.g. -11 == SIGSEGV).
            print(f"  skip {name}: child killed by signal {-proc.returncode}")
            failures += 1
        else:
            failures += 1

    print(f"\nDone: {successes} written, {failures} skipped → {OUT_DIR.relative_to(ROOT)}")
    return 0 if (failures == 0 and mock_rc == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
