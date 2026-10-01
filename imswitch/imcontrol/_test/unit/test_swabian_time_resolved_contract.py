from __future__ import annotations

import time

import numpy as np
import pytest

from imswitch.imcontrol.model.managers.detectors.SwabianTimeTaggerManager import (
    SwabianTimeTaggerManager,
)
from imswitch.imcontrol.model.timeresolved import (
    GateSpec,
    LifetimeFitConfig,
    TimeResolvedScanConfig,
)


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)


class _Nidaq:
    def __init__(self):
        self.sigScanBuilt = _Signal()
        self.sigScanStarted = _Signal()
        self.sigScanDone = _Signal()


class _DetectorInfo:
    forAcquisition = True
    forFocusLock = False

    def __init__(self, props):
        self.managerProperties = props


class _ScanWorker:
    def __init__(self, generation=1):
        self.scanGeneration = generation
        self.stopCalls = 0

    def stop(self):
        self.stopCalls += 1


class _StuckScanThread:
    def __init__(self):
        self.running = True
        self.quitCalls = 0
        self.waitTimeouts = []

    def isRunning(self):
        return self.running

    def quit(self):
        self.quitCalls += 1

    def wait(self, timeout=None):
        self.waitTimeouts.append(timeout)
        return not self.running


def _make_manager(props=None):
    defaults = {
        "click_channel": 1,
        "start_channel": 2,
        "line_channel": 3,
    }
    defaults.update(props or {})
    return SwabianTimeTaggerManager(_DetectorInfo(defaults), "FLIM", _Nidaq())


def _store_synthetic_products(manager, *, is_final=True):
    cube = np.arange(2 * 3 * 4, dtype=np.float32).reshape(2, 3, 4)
    t_axis_ns = np.array([0.5, 1.5, 2.5, 3.5], dtype=np.float32)
    intensity = cube.sum(axis=-1).astype(np.float32)
    lifetime_s = np.ones((2, 3), dtype=np.float32) * 2e-9
    decay = cube.sum(axis=(0, 1)).astype(np.float32)
    manager._scan = {"scan_info": {"img_dims": [3, 2], "dwell_time": 1e-6}}
    manager._store_time_resolved_products(
        cube_counts=cube,
        intensity=intensity,
        lifetime_s=lifetime_s,
        decay_counts=decay,
        t_axis_ns=t_axis_ns,
        global_tau_ns=2.0,
        peak_bin=1,
        peak_time_ns=1.5,
        is_final=is_final,
    )
    return cube, intensity, lifetime_s, decay, t_axis_ns


def test_constructor_honors_direct_trigger_properties():
    manager = _make_manager(
        {
            "click_trigger": -0.1,
            "start_trigger": 0.2,
            "line_trigger": -0.3,
            "trigger_levels": {"1": 9.0, "2": 9.0, "3": 9.0},
        }
    )

    assert manager._click_trigger == -0.1
    assert manager._start_trigger == 0.2
    assert manager._line_trigger == -0.3


def test_constructor_falls_back_to_trigger_levels():
    manager = _make_manager({"trigger_levels": {"1": -0.1, "2": 0.2, "3": -0.3}})

    assert manager._click_trigger == -0.1
    assert manager._start_trigger == 0.2
    assert manager._line_trigger == -0.3


def test_time_resolved_products_retain_cube_when_requested():
    manager = _make_manager()
    manager.configureTimeResolvedProducts(
        TimeResolvedScanConfig(
            capture_cube=True,
            gates=(GateSpec("early", 0.5, 2.5),),
        )
    )
    cube, intensity, lifetime_s, decay, t_axis_ns = _store_synthetic_products(manager)

    products = manager.waitForFinalTimeResolvedProducts(timeout_s=0.01)

    np.testing.assert_array_equal(products.cube_counts, cube)
    np.testing.assert_array_equal(products.intensity, intensity)
    np.testing.assert_allclose(products.lifetime_ns, lifetime_s * 1e9)
    np.testing.assert_array_equal(products.decay_counts, decay)
    np.testing.assert_array_equal(products.t_axis_ns, t_axis_ns)
    np.testing.assert_array_equal(products.gate_images["early"], cube[..., :2].sum(axis=-1))
    assert products.cube_axes == ("y", "x", "tcspc_bin")
    assert products.global_tau_ns == 2.0
    assert products.metadata["peak_bin"] == 1
    assert products.metadata["peak_time_ns"] == 1.5
    assert products.metadata["scan_info"]["img_dims"] == [3, 2]
    assert products.is_final is True


def test_configure_time_resolved_products_applies_fit_settings():
    manager = _make_manager()

    manager.configureTimeResolvedProducts(
        TimeResolvedScanConfig(
            fit=LifetimeFitConfig(
                method="phasor",
                min_counts_per_pixel=7,
                laser_rep_rate_mhz=40.0,
            )
        )
    )

    assert manager._fit_method == "phasor"
    assert manager._min_counts_per_pixel == 7
    assert manager._laser_rep_rate_mhz == 40.0
    assert manager.parameters["fit_method"].value == "phasor"
    assert manager.parameters["min_counts_per_pixel"].value == 7
    assert manager.parameters["laser_rep_rate_mhz"].value == 40.0


def test_time_resolved_products_can_compute_gates_without_retaining_cube():
    manager = _make_manager()
    manager.configureTimeResolvedProducts(
        TimeResolvedScanConfig(
            capture_cube=False,
            gates=(GateSpec("late", 2.5, 4.5),),
        )
    )
    cube, *_ = _store_synthetic_products(manager)

    products = manager.waitForFinalTimeResolvedProducts(timeout_s=0.01)

    assert products.cube_counts is None
    np.testing.assert_array_equal(products.gate_images["late"], cube[..., 2:].sum(axis=-1))


def test_time_resolved_get_last_copy_is_defensive():
    manager = _make_manager()
    manager.configureTimeResolvedProducts(TimeResolvedScanConfig(capture_cube=True))
    _store_synthetic_products(manager)

    products = manager.getLastTimeResolvedProducts(copy=True)
    products.intensity[:] = -1

    fresh = manager.getLastTimeResolvedProducts(copy=True)
    assert np.all(fresh.intensity >= 0)


def test_nonfinal_products_are_ignored_unless_live_products_requested():
    manager = _make_manager()
    manager.configureTimeResolvedProducts(TimeResolvedScanConfig(capture_cube=True))
    _store_synthetic_products(manager, is_final=False)

    assert manager.getLastTimeResolvedProducts() is None

    manager.configureTimeResolvedProducts(
        TimeResolvedScanConfig(capture_cube=True, include_live_products=True)
    )
    _store_synthetic_products(manager, is_final=False)

    assert manager.getLastTimeResolvedProducts() is not None


def test_time_resolved_product_capture_rejects_outer_scan_axes():
    manager = _make_manager()
    manager.configureTimeResolvedProducts(TimeResolvedScanConfig(capture_cube=True))

    with pytest.raises(RuntimeError, match="2D x/y scans"):
        manager._validate_time_resolved_scan_shape(["z"], [3])


def test_scan_thread_teardown_is_bounded_and_retains_retry_identity():
    manager = _make_manager()
    worker = _ScanWorker()
    thread = _StuckScanThread()
    manager._scanWorker = worker
    manager._scanThread = thread

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="did not stop"):
        manager._teardownScanThread(timeoutMs=5)
    assert time.monotonic() - started < 0.2
    assert manager._scanWorker is worker
    assert manager._scanThread is thread
    assert worker.stopCalls == 1
    assert thread.waitTimeouts == [5]

    thread.running = False
    manager._teardownScanThread(timeoutMs=5)
    assert worker.stopCalls == 2
    assert manager._scanWorker is None
    assert manager._scanThread is None


def test_abort_withholds_ack_when_scan_worker_cannot_be_stopped(
        monkeypatch):
    manager = _make_manager()
    worker = _ScanWorker(generation=7)
    manager._scanWorker = worker
    manager._activeScanGeneration = 7
    manager._preparedScanGeneration = 7
    acknowledgements = []

    def failTeardown():
        raise TimeoutError("worker stuck")

    monkeypatch.setattr(manager, "_teardownScanThread", failTeardown)
    manager.finishScan("abort", lambda: acknowledgements.append(True))

    assert acknowledgements == []
    assert manager._scanWorker is worker
    assert manager._activeScanGeneration == 7
    assert manager._preparedScanGeneration == 7


# ---------------------------------------------------------------------------
# The raw-chunk contract, and the histogram window's default
# ---------------------------------------------------------------------------


def _frame(manager, *, is_final, value=2.0):
    ny, nx = manager._image_display.shape[1:]
    intensity = np.full((ny, nx), 10.0, dtype=np.float32)
    lifetime_s = np.full((ny, nx), value * 1e-9, dtype=np.float32)
    manager._on_frame_ready(intensity, lifetime_s, is_final, None, None, value)


def test_flim_declares_its_raw_frame_deferred():
    """A FLIM image is whole only when the worker's final frame lands."""
    assert _make_manager().rawFrameIsDeferred is True


def test_recording_receives_the_finished_image_once_not_the_previews():
    """The worker previews every second with is_final False; a recording used
    to be satisfied by the first preview, one second into a minute's scan."""
    manager = _make_manager()
    manager.startAcquisition()

    _frame(manager, is_final=False, value=1.0)
    payload = manager.drainChunk()
    assert payload.display.shape == (1, 64, 64)
    assert payload.raw.size == 0, "a preview reached the recording"

    _frame(manager, is_final=False, value=1.5)
    assert manager.drainChunk().raw.size == 0

    _frame(manager, is_final=True, value=3.0)
    payload = manager.drainChunk()
    assert payload.raw.shape == (1, 64, 64)
    assert float(payload.raw[0, 0, 0]) == 3.0
    assert manager.drainChunk().raw.size == 0, "the finished image was delivered twice"


def test_an_aborted_scan_delivers_no_raw_frame():
    manager = _make_manager()
    manager.startAcquisition()
    _frame(manager, is_final=True, value=3.0)
    manager.finishScan('abort', acknowledge=lambda *a, **k: None)
    assert manager.drainChunk().raw.size == 0


def test_the_histogram_window_spans_one_laser_period_unless_declared():
    from imswitch.imcontrol.model.managers.detectors.SwabianTimeTaggerManager import (
        histogram_bins_for_period,
    )
    assert histogram_bins_for_period(32, 80.0) == 391  # 12.5 ns / 32 ps, rounded up
    assert _make_manager()._n_bins == 391
    assert _make_manager({"n_bins": 64})._n_bins == 64
    assert _make_manager({"laser_rep_rate_mhz": 40.0, "binwidth_ps": 50})._n_bins == 500


def test_a_declared_window_shorter_than_the_period_is_warned_about(caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        _make_manager({"n_bins": 64})
    assert any("covers 16% of the 12.50 ns laser period" in r.getMessage()
               for r in caplog.records), [r.getMessage() for r in caplog.records]
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        _make_manager()
    assert not any("laser period" in r.getMessage() for r in caplog.records)
