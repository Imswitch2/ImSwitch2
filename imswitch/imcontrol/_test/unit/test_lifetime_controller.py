"""The Lifetime widget's controller on fakes (Lifetime 2.0, P4).

Live products become decay, histogram and viewer layers; Run configures
the product session inside its own worker run and clears it after; a
script's session is never disturbed; Stop aborts and joins; settings
write through to the detector; the card panel degrades without a card.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcommon.framework import Signal, SignalInterface
from imswitch.imcontrol.controller.basecontrollers import ComponentStateApplyMode
from imswitch.imcontrol.controller.controllers.LifetimeController import (
    LifetimeController,
    combine_products,
    phasor_point,
    tau_overlay_rgb,
)
from imswitch.imcontrol.model.timeresolved import (
    LiveProducts,
    TimeResolvedScanConfig,
    TimeResolvedScanProducts,
)
from imswitch.imcontrol.view.widgets.LifetimeWidget import LifetimeWidget
from imswitch.imcontrol.view.widgets.basewidgets import WidgetFactory

pytestmark = pytest.mark.nohardware

NY, NX, NBINS = 4, 6, 20


class _FakeDetector(SignalInterface):
    sigTimeResolvedProducts = Signal(object)

    def __init__(self):
        super().__init__()
        self.parameters = {
            name: SimpleNamespace(value=value) for name, value in dict(
                fit_method='moment', min_counts_per_pixel=20, laser_rep_rate_mhz=80.0,
                binwidth_ps=32, n_bins=NBINS, t0_ps=0, background_rate_hz=0.0,
            ).items()
        }
        self.pixelSizeUm = [1.0, 0.2, 0.1]
        self.writes = []
        self.owner = None
        self.configured = []
        self.cleared = []
        self.final = None

    def setParameter(self, name, value):
        self.writes.append((name, value))
        self.parameters[name].value = value

    # the time-resolved contract
    def configureTimeResolvedProducts(self, config, owner=None):
        if self.owner is not None and owner != self.owner:
            raise RuntimeError(f"owned by another run ({self.owner})")
        self.owner = owner
        self.configured.append((config, owner))
        return owner

    def waitForFinalTimeResolvedProducts(self, timeout_s=None, owner=None):
        return self.final

    def getLastTimeResolvedProducts(self, copy=True):
        return self.final

    def clearTimeResolvedProducts(self, owner=None):
        if self.owner is not None and owner != self.owner:
            return
        self.cleared.append(owner)
        self.owner = None

    def timeResolvedSessionOwner(self):
        return self.owner

    def timeResolvedCapabilities(self):
        return {}


class _Comm(SignalInterface):
    sigUpsertStaticLayer = Signal(str, np.ndarray, object, object)
    sigRemoveStaticLayer = Signal(str)
    sigAbortScan = Signal()
    sigScanDone = Signal()

    def __init__(self):
        super().__init__()
        self.layers = {}
        self.removed = []
        self.aborts = 0
        self.folder = None
        self.sigUpsertStaticLayer.connect(self._upsert)
        self.sigRemoveStaticLayer.connect(self._remove)
        self.sigAbortScan.connect(self._abort)

    def _upsert(self, name, image, scale, options):
        self.layers[name] = (np.asarray(image), scale, dict(options))

    def _remove(self, name):
        self.removed.append(name)
        self.layers.pop(name, None)

    def _abort(self):
        self.aborts += 1

    def getRecordingFolder(self):
        return self.folder


class _Detectors:
    def __init__(self, items):
        self._items = dict(items)

    def getAllDeviceNames(self, condition=None):
        return list(self._items)

    def __getitem__(self, name):
        return self._items[name]


def _products(seed=0, tau=2.5):
    rng = np.random.default_rng(seed)
    t = (np.arange(NBINS) + 0.5) * 0.5
    decay = (1000 * np.exp(-t / tau)).astype(np.float32)
    intensity = rng.uniform(50, 150, size=(NY, NX)).astype(np.float32)
    lifetime = np.full((NY, NX), tau, dtype=np.float32)
    lifetime[0, 0] = 0.0  # one pixel below the count threshold
    return TimeResolvedScanProducts(
        cube_counts=None, cube_axes=('y', 'x', 'tcspc_bin'), t_axis_ns=t,
        intensity=intensity, lifetime_ns=lifetime, gate_images={}, decay_counts=decay,
        global_tau_ns=tau, metadata={'dwell_s': 1e-5, 'tcspc_direction': 'forward',
                                      'peak_time_ns': 0.25, 'pileup_max': 0.02},
        is_final=True,
    )


def _live(is_final=True, lifetime=True):
    p = _products()
    return LiveProducts(
        intensity=p.intensity, lifetime_ns=p.lifetime_ns if lifetime else None,
        decay_counts=p.decay_counts, t_axis_ns=p.t_axis_ns, gate_images={},
        global_tau_ns=p.global_tau_ns, peak_time_ns=0.25, background_per_bin=0.01,
        pileup_max=0.02, tcspc_direction='forward', frame_index=1, is_final=is_final,
        metadata=dict(p.metadata),
    )


@pytest.fixture
def rig(qapp):
    detector = _FakeDetector()
    comm = _Comm()
    master = SimpleNamespace(detectorsManager=_Detectors({'FLIM': detector}),
                             timeTaggerManager=None)
    widget = WidgetFactory(None).createWidget(LifetimeWidget)
    controller = LifetimeController(
        setupInfo=SimpleNamespace(), commChannel=comm, master=master,
        widget=widget, factory=None, moduleCommChannel=None,
    )
    yield SimpleNamespace(detector=detector, comm=comm, widget=widget, controller=controller)
    controller.closeEvent()


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def test_tau_overlay_is_rgb_with_hue_from_lifetime_and_value_from_intensity():
    tau = np.array([[0.5, 5.0, 0.0]])
    intensity = np.array([[100.0, 100.0, 100.0]])
    rgb = tau_overlay_rgb(tau, intensity, (0.5, 5.0))
    assert rgb.shape == (1, 3, 3) and rgb.dtype == np.uint8
    assert rgb[0, 0].argmax() == 0, "short lifetime: red"
    assert rgb[0, 1].argmax() == 2, "long lifetime: blue"
    assert len(set(rgb[0, 2].tolist())) == 1, "no lifetime: grey"


def test_phasor_of_a_mono_exponential_sits_on_the_semicircle():
    t = (np.arange(4000) + 0.5) * 0.003125  # 12.5 ns in 3.125 ps bins
    tau = 2.5
    decay = np.exp(-t / tau)
    g, s = phasor_point(t, decay, 80.0, 0.0)
    omega_tau = 2 * np.pi * 80e6 * tau * 1e-9
    assert g == pytest.approx(1 / (1 + omega_tau ** 2), abs=0.02)
    assert s == pytest.approx(omega_tau / (1 + omega_tau ** 2), abs=0.02)
    assert phasor_point(t, np.zeros_like(t), 80.0) == (None, None)


def test_combine_products_sums_photons_and_weights_lifetimes():
    a, b = _products(1, tau=2.0), _products(2, tau=4.0)
    out = combine_products([a, b])
    np.testing.assert_allclose(out.intensity, a.intensity + b.intensity)
    np.testing.assert_allclose(out.decay_counts, a.decay_counts + b.decay_counts)
    assert out.metadata['frames_accumulated'] == 2
    assert 2.0 < out.lifetime_ns[1, 1] < 4.0
    assert out.lifetime_ns[0, 0] == 0.0, "no valid lifetime in either scan"
    assert combine_products([a]) is a


# --------------------------------------------------------------------------- #
# The controller                                                               #
# --------------------------------------------------------------------------- #


def test_settings_load_from_the_detector_and_write_through(rig):
    widget, detector = rig.widget, rig.detector
    assert widget.getSetting('rep_rate_mhz') == 80.0
    assert widget.getSetting('n_bins') == NBINS
    widget.settingFields['t0_ps'].setValue(1300)
    assert ('t0_ps', 1300) in detector.writes
    widget.settingFields['fit_method'].setCurrentText('exp1')
    assert ('fit_method', 'exp1') in detector.writes


def test_live_products_feed_the_views_and_one_stable_layer(rig, qtbot):
    widget, comm, detector = rig.widget, rig.comm, rig.detector
    detector.sigTimeResolvedProducts.emit(_live())
    qtbot.waitUntil(lambda: 'FLIM › lifetime' in comm.layers, timeout=2000)
    image, scale, options = comm.layers['FLIM › lifetime']
    assert image.shape == (NY, NX) and scale == [0.2, 0.1]
    assert options['colormap'] == 'viridis' and options['contrast_limits'] == (0.5, 5.0)
    assert 'Valid pixels: 23' in widget.histStatLabel.text()
    assert 'phasor (g, s): (' in widget.phasorLabel.text()
    assert 'peak 0.25 ns' in widget._decayInfo.text()

    # A second frame updates the same layer; the display choice swaps it.
    detector.sigTimeResolvedProducts.emit(_live())
    qtbot.wait(50)
    assert list(comm.layers) == ['FLIM › lifetime']
    widget.setDisplay('overlay')
    qtbot.waitUntil(lambda: 'FLIM › τ overlay' in comm.layers, timeout=2000)
    assert 'FLIM › lifetime' in comm.removed
    rgb, _, options = comm.layers['FLIM › τ overlay']
    assert rgb.shape == (NY, NX, 3) and options['rgb'] is True

    # An intensity-only preview keeps the last lifetime layer untouched.
    widget.setDisplay('lifetime')
    qtbot.waitUntil(lambda: 'FLIM › lifetime' in comm.layers, timeout=2000)
    detector.sigTimeResolvedProducts.emit(_live(is_final=False, lifetime=False))
    qtbot.wait(50)
    assert 'FLIM › lifetime' in comm.layers


def test_tau_sted_mode_adds_the_pileup_map_on_request(rig, qtbot):
    widget, comm, detector = rig.widget, rig.comm, rig.detector
    widget.setMode('Tau STED')
    widget.pileupMapCheck.setChecked(True)
    detector.sigTimeResolvedProducts.emit(_live())
    qtbot.waitUntil(lambda: 'FLIM › pile-up' in comm.layers, timeout=2000)
    pileup, _, options = comm.layers['FLIM › pile-up']
    # about 100 photons in 10 us at 80 MHz (800 pulses): 0.125 per pulse
    assert 0.05 < pileup.mean() < 0.2 and options['contrast_limits'] == (0.0, 0.1)
    assert 'median 2.50 ns' in widget.tauStatLabel.text()
    widget.pileupMapCheck.setChecked(False)
    qtbot.waitUntil(lambda: 'FLIM › pile-up' in comm.removed, timeout=2000)


def test_run_configures_inside_its_own_run_and_clears_after(rig, qtbot, monkeypatch):
    controller, detector, widget = rig.controller, rig.detector, rig.widget
    detector.final = _products()
    scans = []

    class _Scan:
        def run_once(self, timeout_s=None):
            scans.append(timeout_s)
            assert detector.owner is not None, "configured before the scan"

    facade = SimpleNamespace(time_resolved=_facade_detector(detector), scan=_Scan())
    monkeypatch.setattr(controller, '_buildFacade', lambda name: facade)
    assert detector.owner is None, "nothing configured before Run"
    widget.accumulateSpin.setValue(2)
    widget.runButton.click()
    qtbot.waitUntil(lambda: widget.runButton.isEnabled(), timeout=5000)
    assert len(scans) == 2
    # The workflow clears an unowned leftover before each configure (None)
    # and its own session after each run (the token).
    assert detector.owner is None and len([c for c in detector.cleared if c]) == 2
    assert all(isinstance(cfg, TimeResolvedScanConfig) for cfg, _ in detector.configured)
    assert detector.configured[0][0].fit.method == 'moment'
    assert controller._lastProducts.metadata['frames_accumulated'] == 2
    assert '2 scans' in widget.footerStatus.text()


def test_a_scripts_session_is_not_disturbed_by_run(rig, qtbot, monkeypatch):
    controller, detector, widget = rig.controller, rig.detector, rig.widget
    detector.configureTimeResolvedProducts(TimeResolvedScanConfig(), owner='script-1')
    facade = SimpleNamespace(time_resolved=_facade_detector(detector),
                             scan=SimpleNamespace(run_once=lambda timeout_s=None: None))
    monkeypatch.setattr(controller, '_buildFacade', lambda name: facade)
    widget.runButton.click()
    qtbot.waitUntil(lambda: widget.runButton.isEnabled(), timeout=5000)
    assert detector.owner == 'script-1', "the script still owns its session"
    assert 'run failed' in widget.footerStatus.text()


def test_stop_aborts_the_scan_and_the_worker_joins(rig, qtbot, monkeypatch):
    controller, detector, widget, comm = rig.controller, rig.detector, rig.widget, rig.comm
    detector.final = _products()
    release = threading.Event()

    class _Scan:
        def run_once(self, timeout_s=None):
            release.wait(5.0)

    facade = SimpleNamespace(time_resolved=_facade_detector(detector), scan=_Scan())
    monkeypatch.setattr(controller, '_buildFacade', lambda name: facade)
    widget.liveButton.setChecked(True)
    qtbot.waitUntil(lambda: controller._workerBusy(), timeout=2000)
    widget.stopButton.click()
    assert comm.aborts == 1
    release.set()
    qtbot.waitUntil(lambda: not controller._workerBusy(), timeout=5000)
    qtbot.waitUntil(lambda: widget.runButton.isEnabled(), timeout=2000)
    assert not widget.liveButton.isChecked()
    assert detector.owner is None


def test_component_state_round_trips_without_running(rig):
    controller, widget = rig.controller, rig.widget
    widget.setMode('Tau STED')
    widget.setDisplay('intensity')
    widget.nameEdit.setText('cells')
    widget.accumulateSpin.setValue(3)
    state = controller.getComponentState()
    assert state['mode'] == 'Tau STED' and state['display'] == 'intensity'
    widget.setMode('FLIM')
    widget.setDisplay('lifetime')
    warnings = controller.applyComponentState(
        dict(state, detector='missing'), applyMode=ComponentStateApplyMode.STARTUP_RESTORE)
    assert warnings == ["detector 'missing' is not in this setup"]
    assert widget.getMode() == 'Tau STED' and widget.getDisplay() == 'intensity'
    assert widget.nameEdit.text() == 'cells' and widget.accumulateSpin.value() == 3
    assert not controller._workerBusy()


def test_without_a_card_the_signals_panel_is_disabled(rig):
    widget = rig.widget
    assert not widget.sweepButton.isEnabled() and not widget.preflightButton.isEnabled()
    assert 'no Time Tagger' in widget._statusStrip.text()


def _facade_detector(detector):
    """The slice of TimeResolvedDetectorFacade the workflow uses."""
    from contextlib import nullcontext
    return SimpleNamespace(
        acquisition_lease=lambda: nullcontext(),
        configure=lambda config, owner=None: detector.configureTimeResolvedProducts(config, owner),
        wait_for_final=lambda timeout_s=None, owner=None: detector.waitForFinalTimeResolvedProducts(timeout_s, owner),
        clear=lambda owner=None: detector.clearTimeResolvedProducts(owner),
    )


def test_gated_run_carries_the_gates_and_shows_gate_and_ratio_layers(rig, qtbot, monkeypatch):
    controller, detector, widget, comm = rig.controller, rig.detector, rig.widget, rig.comm
    widget.setMode('Gated STED')
    widget.runButton.click()
    assert 'at least one gate' in widget.footerStatus.text(), 'no gates: refused before the scan'
    widget.setGates([dict(name='early', start_ns=0.5, stop_ns=2.5, reference='peak'),
                     dict(name='late', start_ns=2.5, stop_ns=8.0, reference='peak')])
    widget.ratioCheck.setChecked(True)
    products = _products()
    products.gate_images = {'early': products.intensity * 0.4, 'late': products.intensity * 0.6}
    detector.final = products
    facade = SimpleNamespace(time_resolved=_facade_detector(detector),
                             scan=SimpleNamespace(run_once=lambda timeout_s=None: None))
    monkeypatch.setattr(controller, '_buildFacade', lambda name: facade)
    widget.runButton.click()
    qtbot.waitUntil(lambda: widget.runButton.isEnabled(), timeout=5000)
    config = detector.configured[-1][0]
    assert [g.name for g in config.gates] == ['early', 'late']
    assert config.gates[0].reference == 'peak'
    qtbot.waitUntil(lambda: 'FLIM › gate ratio' in comm.layers, timeout=2000)
    assert 'FLIM › gate:early' in comm.layers and 'FLIM › gate:late' in comm.layers
    ratio, _, options = comm.layers['FLIM › gate ratio']
    assert np.allclose(ratio, 1.5) and options['colormap'] == 'inferno'
    assert comm.layers['FLIM › gate:early'][2]['colormap'] == 'red'
    widget.gateLayersCheck.setChecked(False)
    qtbot.waitUntil(lambda: 'FLIM › gate:early' in comm.removed, timeout=2000)
    assert 'FLIM › gate ratio' in comm.layers
    # Component state carries the gates.
    state = controller.getComponentState()
    assert [g['name'] for g in state['gates']] == ['early', 'late'] and state['ratio_layer']


def test_presets_load_from_the_shipped_folder(rig):
    controller, widget = rig.controller, rig.widget
    assert 'sted_early_late' in controller._presets
    widget.presetCombo.setCurrentText('sted_early_late')
    widget.loadPresetButton.click()
    gates = widget.getGates()
    assert [g['name'] for g in gates] == ['early', 'late']
    assert widget.ratioCheck.isChecked(), 'the preset names a ratio'
    assert 'preset sted_early_late' in widget.gateStatusLabel.text()


def test_combine_products_sums_gate_images_and_version_2_fields():
    a, b = _products(1), _products(2)
    a.gate_images = {'g': np.ones((NY, NX))}
    b.gate_images = {'g': np.ones((NY, NX)) * 2}
    a.overflows, b.overflows, b.pileup_max = 1, 2, 0.2
    out = combine_products([a, b])
    assert np.all(out.gate_images['g'] == 3)
    assert out.overflows == 3 and out.pileup_max == 0.2 and out.frames_accumulated == 2


# --------------------------------------------------------------------------- #
# Review round 3                                                               #
# --------------------------------------------------------------------------- #


def test_shutdown_cancels_a_running_diagnostic_and_reports_a_worker_that_will_not_stop(rig, qtbot):
    from imswitch.imcommon.model.cancellation import cancellableSleep
    controller = rig.controller
    controller.CLOSE_JOIN_S = 0.3
    # A cooperative diagnostic (the facade's waits poll the cancel token).
    controller._runOnWorker(lambda: cancellableSleep(30.0), 'sweeping')
    qtbot.waitUntil(lambda: controller._workerBusy(), timeout=2000)
    assert controller.closeEvent() is True, 'cancelled and drained'
    assert not controller._workerBusy()

    # One that ignores cancellation: shutdown must not report success.
    rig2 = rig
    rig2.controller._closed = False
    release = threading.Event()
    rig2.controller._runOnWorker(lambda: release.wait(10.0), 'stuck')
    qtbot.waitUntil(lambda: rig2.controller._workerBusy(), timeout=2000)
    assert rig2.controller.closeEvent() is False, 'still alive: the card must not be freed'
    release.set()
    qtbot.waitUntil(lambda: not rig2.controller._workerBusy(), timeout=5000)


def test_stop_cancels_a_diagnostic(rig, qtbot):
    from imswitch.imcommon.model.cancellation import cancellableSleep
    controller, widget = rig.controller, rig.widget
    controller._runOnWorker(lambda: cancellableSleep(30.0), 'sweeping')
    qtbot.waitUntil(lambda: controller._workerBusy(), timeout=2000)
    widget.stopButton.click()
    qtbot.waitUntil(lambda: not controller._workerBusy(), timeout=5000)
    qtbot.waitUntil(lambda: 'cancelled' in widget.footerStatus.text(), timeout=2000)


def test_save_writes_the_gates_of_the_acquisition_not_the_table(rig, qtbot, monkeypatch, tmp_path):
    import imswitch.imcontrol.model.workflows.time_resolved as tr_module
    controller, detector, widget, comm = rig.controller, rig.detector, rig.widget, rig.comm
    widget.setMode('Gated STED')
    widget.setGates([dict(name='early', start_ns=0.0, stop_ns=1.0, reference='absolute')])
    products = _products()
    products.gate_images = {'early': products.intensity}
    detector.final = products
    facade = SimpleNamespace(time_resolved=_facade_detector(detector),
                             scan=SimpleNamespace(run_once=lambda timeout_s=None: None))
    monkeypatch.setattr(controller, '_buildFacade', lambda name: facade)
    widget.runButton.click()
    qtbot.waitUntil(lambda: widget.runButton.isEnabled(), timeout=5000)
    # The gate is edited after the acquisition, then Save.
    widget.setGates([dict(name='early', start_ns=5.0, stop_ns=6.0, reference='absolute')])
    widget.setMode('FLIM')
    saved = {}

    def fake_save(products, params):
        saved['params'] = params
        return {}
    monkeypatch.setattr(tr_module, 'save_products', fake_save)
    comm.folder = str(tmp_path)
    widget.saveButton.click()
    qtbot.waitUntil(lambda: 'params' in saved, timeout=5000)
    gate = saved['params'].gates[0]
    assert (gate.start_ns, gate.stop_ns) == (0.0, 1.0), 'what was measured, not the table'
    assert saved['params'].save_folder == str(tmp_path)
    assert controller._lastProducts.metadata['gates_configured'][0]['stop_ns'] == 1.0


def test_pileup_map_uses_the_accumulated_exposure(rig, qtbot):
    widget, comm, detector = rig.widget, rig.comm, rig.detector
    widget.setMode('Tau STED')
    widget.pileupMapCheck.setChecked(True)
    one = _products()
    ten = _products()
    ten.intensity = one.intensity * 10
    ten.metadata = dict(one.metadata, frames_accumulated=10, laser_rep_rate_mhz=80.0)
    rig.controller._onRunProducts(one)
    single = comm.layers['FLIM › pile-up'][0].copy()
    rig.controller._onRunProducts(ten)
    accumulated = comm.layers['FLIM › pile-up'][0]
    np.testing.assert_allclose(accumulated, single, rtol=1e-5)


def test_an_invalid_frame_is_said_so_in_the_footer(rig):
    products = _products()
    products.metadata['frame_valid'] = False
    rig.controller._onRunProducts(products)
    assert 'FRAME INVALID' in rig.widget.footerStatus.text()
