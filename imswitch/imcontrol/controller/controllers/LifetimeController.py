"""The Lifetime widget's controller: FLIM, tau-STED and the card's signals.

What it owns (Lifetime 2.0 plan, section 5.2):

* the live views, fed by the detector's ``sigTimeResolvedProducts``
  (queued from the worker thread) -- decay, lifetime histogram, phasor
  readout, scatter, and the viewer layers (lifetime, intensity, an
  intensity-weighted overlay, the pile-up map), created and updated in
  place through ``sigUpsertStaticLayer``;
* the Run path: ``TimeResolvedScanWorkflow.run()`` on a worker thread
  (``run_once`` refuses the GUI thread), results marshalled back with
  ``_invokeOnControllerThread``; Live re-arms from the worker; Stop is
  ``sigAbortScan`` plus an event the worker polls between runs. The
  widget and the scripts share one code path and produce identical files;
* product configuration *inside* its own run only (the workflow opens and
  clears the session with its run token), never on a mode or gate change;
* the Signals panel: the card's health sampler, trigger levels, sweeps,
  scope snapshots and the pre-flight checklist, every blocking call on the
  worker thread;
* widget state (mode, display, colour range, accumulate, name) through
  ``StatefulComponentMixin``. Device conditioning is setup-file state and
  is never restored from widget state.
"""

from __future__ import annotations

import os
import threading
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Optional

import numpy as np

from imswitch.imcommon.model import initLogger
from imswitch.imcommon.model.cancellation import (
    CancelToken,
    OperationCancelled,
    clearCurrentCancelToken,
    setCurrentCancelToken,
)
from imswitch.imcontrol.model.timeresolved import (
    PILEUP_WARN,
    GateSpec,
    LifetimeFitConfig,
    TimeResolvedScanProducts,
    load_gate_preset,
    pileup_fraction,
)
from imswitch.imcontrol.view.widgets.LifetimeWidget import GATE_COLORMAPS
from ..basecontrollers import (
    ComponentStateApplyMode,
    ImConWidgetController,
    StatefulComponentMixin,
)

#: widget setting key -> detector parameter name
SETTING_TO_PARAMETER = {
    'fit_method': 'fit_method',
    'min_counts': 'min_counts_per_pixel',
    'rep_rate_mhz': 'laser_rep_rate_mhz',
    'binwidth_ps': 'binwidth_ps',
    'n_bins': 'n_bins',
    't0_ps': 't0_ps',
    'background_hz': 'background_rate_hz',
}
#: Default lifetime colour range when the widget has none yet (ns).
DEFAULT_TAU_RANGE = (0.5, 5.0)
#: The scan's timeout per run, generous: a slow scan is not an error.
RUN_TIMEOUT_S = 600.0
#: Roles a scope snapshot shows (never the photons: millions of edges).
SCOPE_ROLES = ('line_clock', 'frame_clock', 'laser_sync')
#: Metadata that must agree between scans summed into one result.
ACCUMULATION_KEYS = ('binwidth_ps', 'n_bins', 'fit_method', 'laser_rep_rate_mhz',
                     't0_ps', 'background_rate_hz', 'min_counts_per_pixel')
#: Where the gate presets live, relative to the user's scripts folder (the
#: same files tutorial 12 loads); the shipped defaults are the fallback.
GATE_PRESETS_RELATIVE = os.path.join('scripts', 'tutorial', 'timetagger', 'gate_presets')


def gate_preset_folders():
    """The user's gate-preset folder first, then the shipped one."""
    from imswitch.imcommon.model import dirtools
    folders = []
    for root in (getattr(dirtools.UserFileDirs, 'Root', None),
                 getattr(dirtools.DataFileDirs, 'UserDefaults', None)):
        if root:
            folder = Path(root) / GATE_PRESETS_RELATIVE
            if folder.is_dir():
                folders.append(folder)
    return folders


def ratio_image(numerator, denominator):
    """``numerator / denominator`` per pixel, 0 where the denominator is 0."""
    num = np.asarray(numerator, dtype=np.float64)
    den = np.asarray(denominator, dtype=np.float64)
    out = np.zeros(np.broadcast(num, den).shape, dtype=np.float32)
    good = den > 0
    out[good] = (num[good] / den[good]).astype(np.float32)
    return out


def tau_overlay_rgb(lifetime_ns, intensity, tau_range, hue_span=(0.0, 0.7)):
    """An intensity-weighted lifetime image as RGB (uint8, ``(..., 3)``):
    hue from the lifetime over ``tau_range`` (red = short, blue = long),
    value from the intensity (its 99.5th percentile is white)."""
    tau = np.asarray(lifetime_ns, dtype=np.float64)
    inten = np.asarray(intensity, dtype=np.float64)
    lo, hi = float(tau_range[0]), float(tau_range[1])
    if hi <= lo:
        hi = lo + 1.0
    hue_lo, hue_hi = hue_span
    frac = np.clip((tau - lo) / (hi - lo), 0.0, 1.0)
    valid = np.isfinite(tau) & (tau > 0)
    hue = hue_lo + frac * (hue_hi - hue_lo)
    top = np.percentile(inten[np.isfinite(inten)], 99.5) if np.isfinite(inten).any() else 0.0
    value = np.clip(inten / top, 0.0, 1.0) if top > 0 else np.zeros_like(inten)
    sat = np.where(valid, 1.0, 0.0)  # pixels without a lifetime show grey
    # HSV -> RGB, vectorised (h in [0, 1)).
    h6 = (hue % 1.0) * 6.0
    i = np.floor(h6).astype(int) % 6
    f = h6 - np.floor(h6)
    p = value * (1 - sat)
    q = value * (1 - sat * f)
    t = value * (1 - sat * (1 - f))
    r = np.choose(i, [value, q, p, p, t, value])
    g = np.choose(i, [t, value, value, q, p, p])
    b = np.choose(i, [p, p, t, value, value, q])
    rgb = np.stack([r, g, b], axis=-1)
    return np.clip(rgb * 255.0 + 0.5, 0, 255).astype(np.uint8)


def phasor_point(t_axis_ns, decay_counts, rep_rate_mhz, peak_ns=0.0):
    """The decay's first-harmonic phasor ``(g, s)`` at the laser frequency,
    referenced to the IRF peak; ``(None, None)`` without photons."""
    t = (np.asarray(t_axis_ns, dtype=np.float64) - float(peak_ns)) * 1e-9
    c = np.asarray(decay_counts, dtype=np.float64)
    total = c.sum()
    if c.size == 0 or total <= 0 or rep_rate_mhz <= 0:
        return None, None
    omega = 2.0 * np.pi * float(rep_rate_mhz) * 1e6
    g = float((c * np.cos(omega * t)).sum() / total)
    s = float((c * np.sin(omega * t)).sum() / total)
    return g, s


def combine_products(products_list):
    """Sum ``n`` scans into one product set: intensities and decays add,
    the lifetime image is the intensity-weighted mean of the per-scan
    lifetimes (pixels without a valid lifetime in a scan do not count),
    the global lifetime the photon-weighted mean. One scan is returned as
    is. ``metadata['frames_accumulated']`` says how many went in."""
    products_list = [p for p in products_list if p is not None]
    if not products_list:
        raise ValueError('nothing to combine')
    if len(products_list) == 1:
        return products_list[0]
    first = products_list[0]
    # Scans summed into one result must be the same acquisition: the same
    # time axis and settings. An edit between scans makes them something
    # else, and the sum would carry the last scan's labels on the first
    # scan's photons.
    for k, p in enumerate(products_list[1:], start=2):
        if (np.shape(p.t_axis_ns) != np.shape(first.t_axis_ns)
                or not np.allclose(p.t_axis_ns, first.t_axis_ns)):
            raise ValueError(f'scan {k} has a different time axis: not accumulated')
        if np.shape(p.intensity) != np.shape(first.intensity):
            raise ValueError(f'scan {k} has a different image size: not accumulated')
        for key in ACCUMULATION_KEYS:
            if (p.metadata or {}).get(key) != (first.metadata or {}).get(key):
                raise ValueError(
                    f'scan {k} was taken with a different {key} '
                    f'({(p.metadata or {}).get(key)!r} vs {(first.metadata or {}).get(key)!r}): '
                    'not accumulated; keep the settings for the whole accumulation')
        if p.tcspc_direction != first.tcspc_direction:
            raise ValueError(f'scan {k} was taken in the other TCSPC direction: not accumulated')
    intensity = sum(np.asarray(p.intensity, dtype=np.float64) for p in products_list)
    decay = sum(np.asarray(p.decay_counts, dtype=np.float64) for p in products_list)
    weighted = np.zeros_like(intensity)
    weight = np.zeros_like(intensity)
    for p in products_list:
        if p.lifetime_ns is None:
            continue
        tau = np.asarray(p.lifetime_ns, dtype=np.float64)
        w = np.asarray(p.intensity, dtype=np.float64) * (np.isfinite(tau) & (tau > 0))
        weighted += np.nan_to_num(tau) * w
        weight += w
    lifetime = np.where(weight > 0, weighted / np.maximum(weight, 1e-30), 0.0).astype(np.float32)
    photons = np.array([float(np.asarray(p.decay_counts).sum()) for p in products_list])
    taus = np.array([float(p.global_tau_ns) for p in products_list])
    global_tau = float((photons * taus).sum() / photons.sum()) if photons.sum() > 0 else 0.0
    metadata = dict(products_list[-1].metadata)
    metadata['frames_accumulated'] = len(products_list)
    # One invalid constituent (dropped tags, a frame the card never
    # closed) makes the sum invalid; say how many.
    invalid = [k for k, p in enumerate(products_list, start=1)
               if not (p.metadata or {}).get('frame_valid', True)]
    metadata['frame_valid'] = not invalid
    metadata['invalid_scans'] = invalid
    gate_images = {}
    for name in first.gate_images:
        images = [p.gate_images[name] for p in products_list if name in p.gate_images]
        gate_images[name] = sum(np.asarray(im, dtype=np.float64) for im in images).astype(np.float32)
    return TimeResolvedScanProducts(
        cube_counts=None,
        cube_axes=first.cube_axes,
        t_axis_ns=np.asarray(first.t_axis_ns),
        intensity=intensity.astype(np.float32),
        lifetime_ns=lifetime if weight.any() else None,
        gate_images=gate_images,
        decay_counts=decay.astype(np.float32),
        global_tau_ns=global_tau,
        metadata=metadata,
        is_final=True,
        tcspc_direction=str(products_list[-1].tcspc_direction),
        background_rate_hz=float(products_list[-1].background_rate_hz),
        pileup_max=max(float(p.pileup_max) for p in products_list),
        overflows=sum(int(p.overflows) for p in products_list),
        frames_accumulated=len(products_list),
    )


class LifetimeController(ImConWidgetController, StatefulComponentMixin):
    """See the module docstring."""

    componentName = 'Lifetime'
    stateSchemaVersion = 1
    legacyStateNames = ()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._logger = initLogger(self)
        self._detectors = self._findTimeResolvedDetectors()
        self._detectorName: Optional[str] = None
        self._connectedDetector = None
        self._lastLive = None
        self._lastProducts: Optional[TimeResolvedScanProducts] = None
        self._lastLifetime = None
        self._pooledLifetimes: list = []
        self._layers: set = set()
        self._worker: Optional[threading.Thread] = None
        self._stopEvent = threading.Event()
        #: The cancel token of the diagnostic running on the worker (sweep,
        #: scope, pre-flight, save): Stop and shutdown request it to stop.
        self._cardToken: Optional[CancelToken] = None
        #: The parameters of the run that produced ``_lastProducts``: Save
        #: writes those gates, not whatever the table says now.
        self._lastRunParams = None
        self._live = False
        self._overflowsSeen = 0
        self._pileupMax = 0.0
        self._card = None
        self._tt = None
        self._closed = False

        widget = self._widget
        widget.sigDetectorChanged.connect(self._onDetectorChanged)
        widget.sigModeChanged.connect(self._onModeChanged)
        widget.sigSettingChanged.connect(self._onSettingChanged)
        widget.sigDisplayChanged.connect(lambda _d: self._renderLast())
        widget.sigPileupMapToggled.connect(lambda _on: self._renderLast())
        widget.sigMeasureRepRate.connect(self._onMeasureRepRate)
        widget.sigFindT0.connect(self._onFindT0)
        widget.sigRunOnce.connect(lambda: self._startRun(live=False))
        widget.sigLiveToggled.connect(self._onLiveToggled)
        widget.sigStop.connect(self._onStop)
        widget.sigSave.connect(self._onSave)
        widget.sigTriggerLevelEdited.connect(self._onTriggerLevelEdited)
        widget.sigTriggerSweep.connect(self._onTriggerSweep)
        widget.sigScopeSnapshot.connect(self._onScopeSnapshot)
        widget.sigPreflight.connect(self._onPreflight)
        widget.histBinsSpin.valueChanged.connect(lambda _v: self._renderHistogram())
        widget.accumulateHistCheck.toggled.connect(self._onPoolToggled)
        widget.sigGatesChanged.connect(self._onGatesChanged)
        widget.sigPresetSelected.connect(self._onPresetSelected)
        widget.sigGateLayersToggled.connect(lambda _on: self._renderLast())
        widget.sigRatioToggled.connect(lambda _on: self._renderLast())
        widget.sigMeasureStedPulse.connect(self._onMeasureStedPulse)
        self._presets = {}
        self._loadPresetList()
        self._stedPeakNs = None

        names = list(self._detectors)
        widget.setDetectors(names, names[0] if names else None)
        if names:
            self._selectDetector(names[0])
        else:
            widget.setFooterStatus('no time-resolved detector in this setup', error=True)
            widget.runButton.setEnabled(False)
            widget.liveButton.setEnabled(False)
        widget.setSetting('tau_min_ns', DEFAULT_TAU_RANGE[0])
        widget.setSetting('tau_max_ns', DEFAULT_TAU_RANGE[1])
        self._attachCard()

    # ------------------------------------------------------------------ #
    # Detectors and the card                                               #
    # ------------------------------------------------------------------ #

    def _findTimeResolvedDetectors(self) -> dict:
        found = {}
        manager = getattr(self._master, 'detectorsManager', None)
        if manager is None:
            return found
        try:
            names = list(manager.getAllDeviceNames())
        except Exception:
            return found
        for name in names:
            try:
                detector = manager[name]
            except Exception:
                continue
            if hasattr(detector, 'configureTimeResolvedProducts') and hasattr(
                    detector, 'sigTimeResolvedProducts'):
                found[name] = detector
        return found

    @property
    def detector(self):
        return self._detectors.get(self._detectorName)

    def _selectDetector(self, name: str):
        if name not in self._detectors:
            return
        if self._connectedDetector is not None:
            try:
                self._connectedDetector.sigTimeResolvedProducts.disconnect(self._onLiveProducts)
            except Exception:
                pass
        self._removeAllLayers()
        self._detectorName = name
        detector = self._detectors[name]
        detector.sigTimeResolvedProducts.connect(self._onLiveProducts)
        self._connectedDetector = detector
        self._loadSettingsFromDetector()
        self._lastLive = None
        self._lastProducts = None
        self._lastLifetime = None
        self._lastRender = None
        self._lastGateImages = None
        self._lastRunParams = None
        self._stedPeakNs = None
        self._t0SourceId = None
        self._pooledLifetimes = []
        self._widget.setStedMarker(None)

    def _onDetectorChanged(self, name: str):
        if name and name != self._detectorName:
            self._selectDetector(name)

    def _loadSettingsFromDetector(self):
        detector = self.detector
        if detector is None:
            return
        for key, param in SETTING_TO_PARAMETER.items():
            try:
                self._widget.setSetting(key, detector.parameters[param].value)
            except Exception:
                pass

    def _attachCard(self):
        """The shared card manager, when the setup has one and it is open."""
        card = getattr(self._master, 'timeTaggerManager', None)
        if card is None or not getattr(card, 'connected', False):
            self._widget.setStatusStrip('● no Time Tagger in this setup' if card is None
                                        else '● Time Tagger not connected', warn=card is not None)
            for button in (self._widget.sweepButton, self._widget.scopeButton,
                           self._widget.preflightButton, self._widget.measureRepRateButton):
                button.setEnabled(False)
            return
        from imswitch.imcontrol.model.workflows.time_tagger_facade import TimeTaggerFacade
        self._card = card
        self._tt = TimeTaggerFacade(card, getattr(self._master, 'detectorsManager', None))
        roles = self._tt.roles()
        self._widget.setRoles(roles)
        for role, report in self._tt.channels().items():
            self._widget.setTriggerLevel(role, report.trigger_v)
        try:
            card.startHealthSampling(1.0, self._onHealthSampled)
        except Exception as error:
            self._logger.warning(f'Could not start the card health sampler: {error}')

    def _onHealthSampled(self, health):
        """Thread-side callback of the sampler: marshal to the GUI thread."""
        if self._closed:
            return
        self._invokeOnControllerThread(partial(self._showHealth, health))

    def _showHealth(self, health):
        if self._closed:
            return
        self._overflowsSeen += int(getattr(health, 'overflows', 0) or 0)
        rates = dict(getattr(health, 'rates_hz', {}) or {})
        direction = getattr(health, 'tcspc_direction', 'forward')
        filter_on = bool(getattr(health, 'filter_on', False))
        self._widget.updateRates(rates, direction=direction, filter_on=filter_on,
                                 overflows=self._overflowsSeen)
        parts = []
        for role, rate in rates.items():
            label = 'sync (filtered)' if role == 'laser_sync' and filter_on else role
            text = ('—' if rate is None else f'{rate / 1e6:.2f} Mcps' if rate >= 1e6
                    else f'{rate / 1e3:.1f} kcps' if rate >= 1e3 else f'{rate:.0f} cps')
            parts.append(f'● {label} {text}')
        parts.append(f'pile-up max {100 * self._pileupMax:.0f} %')
        parts.append(f'overflows {self._overflowsSeen}')
        if getattr(health, 'is_mock', False):
            parts.append('[mock]')
        warn = self._overflowsSeen > 0 or self._pileupMax > PILEUP_WARN
        self._widget.setStatusStrip('   '.join(parts), warn=warn)

    # ------------------------------------------------------------------ #
    # Settings                                                             #
    # ------------------------------------------------------------------ #

    def _onModeChanged(self, mode: str):
        self._renderLast()

    def _onSettingChanged(self, key: str, value):
        param = SETTING_TO_PARAMETER.get(key)
        if param is None:
            self._renderLast()
            return
        detector = self.detector
        if detector is None:
            return
        try:
            detector.setParameter(param, value)
        except Exception as error:
            self._logger.warning(f'{self._detectorName}.{param} = {value!r} refused: {error}')
            self._widget.setFooterStatus(f'{param}: {error}', error=True)
            self._loadSettingsFromDetector()

    # ------------------------------------------------------------------ #
    # Gates                                                                #
    # ------------------------------------------------------------------ #

    def _gateSpecs(self) -> tuple:
        specs = []
        for gate in self._widget.getGates():
            try:
                specs.append(GateSpec(gate['name'], gate['start_ns'], gate['stop_ns'],
                                      reference=gate['reference']))
            except ValueError as error:
                self._widget.setGateStatus(str(error), error=True)
                return ()
        names = [s.name for s in specs]
        if len(set(names)) != len(names):
            self._widget.setGateStatus('gate names must be unique', error=True)
            return ()
        return tuple(specs)

    def _onGatesChanged(self):
        specs = self._gateSpecs()
        if specs:
            self._widget.setGateStatus(f'{len(specs)} gate(s); they apply on the next Run.')
        elif not self._widget.getGates():
            self._widget.setGateStatus('no gates: add one or load a preset', error=False)

    def _loadPresetList(self):
        self._presets = {}
        for folder in gate_preset_folders():
            for path in sorted(folder.glob('*.json')):
                self._presets.setdefault(path.stem, path)
        self._widget.setPresets(list(self._presets))

    def _onPresetSelected(self, name: str):
        path = self._presets.get(name)
        if path is None:
            return
        try:
            preset = load_gate_preset(path)
        except Exception as error:
            self._widget.setGateStatus(f'{name}: {error}', error=True)
            return
        self._widget.setGates(preset['gates'])
        if preset['ratio'] is not None:
            self._widget.ratioCheck.setChecked(True)
        self._onGatesChanged()
        if self._gateSpecs():
            self._widget.setGateStatus(f"preset {preset['name']}: {len(preset['gates'])} gate(s), "
                                       "applied on the next Run.")

    def _onMeasureStedPulse(self):
        if self._tt is None:
            return
        if 'sted_pulse' not in self._tt.roles():
            self._widget.setGateStatus('no sted_pulse role on the card (stedPulseChannel)', error=True)
            return
        rep = float(self._widget.getSetting('rep_rate_mhz'))
        t0_ns = float(self._widget.getSetting('t0_ps')) / 1000.0

        def body():
            hist = self._tt.sted_pulse_delay(duration_s=1.0, laser_rep_rate_mhz=rep)
            # The decay is shown with t0 applied (the IRF peak at 0); the
            # photodiode histogram is absolute, so the marker moves by t0.
            marker = float(hist.peak_ns) - t0_ns

            def show():
                self._stedPeakNs = marker
                self._widget.setStedMarker(marker)
                self._widget.setGateStatus(f'STED pulse at {hist.peak_ns:.3f} ns after the sync '
                                           f'({marker:+.3f} ns on the decay axis)')
            self._invokeOnControllerThread(show)
        self._runOnWorker(body, 'measuring the STED pulse')

    def _onPoolToggled(self, enabled: bool):
        if not enabled:
            self._pooledLifetimes = []
        self._renderHistogram()

    def _onMeasureRepRate(self):
        if self._tt is None:
            return
        self._runOnWorker(self._measureRepRate, 'measuring the rep rate')

    def _measureRepRate(self):
        result = self._tt.rep_rate(duration_s=2.0)
        mhz = result.rate_hz / 1e6

        def apply():
            self._widget.setSetting('rep_rate_mhz', round(mhz, 4))
            self._onSettingChanged('rep_rate_mhz', round(mhz, 4))
            self._widget.setFooterStatus(result.summary())
        self._invokeOnControllerThread(apply)

    def _onFindT0(self):
        """Move t0 by where the last decay peaks: the worker's decay already
        carries the current t0 (a card delay or a roll), so the peak it
        shows is what is left to correct."""
        source = self._lastLive or self._lastProducts
        if source is None:
            self._widget.setFooterStatus('no decay yet: run a scan first', error=True)
            return
        peak_ns = getattr(source, 'peak_time_ns', None)
        if peak_ns is None:
            t = np.asarray(source.t_axis_ns)
            c = np.asarray(source.decay_counts)
            peak_ns = float(t[int(np.argmax(c))]) if c.size and c.sum() > 0 else 0.0
        if getattr(self, '_t0SourceId', None) == id(source):
            self._widget.setFooterStatus('t0 already taken from this decay: run a scan first',
                                         error=True)
            return
        # The decay was taken with the t0 of *its* acquisition, which the
        # widget's field may no longer show; the correction adds to that.
        acquired_t0 = (source.metadata or {}).get('t0_ps')
        base_t0 = float(acquired_t0 if acquired_t0 is not None
                        else self._widget.getSetting('t0_ps'))
        new_t0 = int(round(base_t0 + peak_ns * 1000.0))
        self._t0SourceId = id(source)
        self._widget.setSetting('t0_ps', new_t0)
        self._onSettingChanged('t0_ps', new_t0)
        self._widget.setFooterStatus(f't0_ps = {new_t0} (peak at {peak_ns:.3f} ns on top of the '
                                     f'acquisition\'s {int(base_t0)} ps); takes effect at the next scan')

    # ------------------------------------------------------------------ #
    # Live products                                                        #
    # ------------------------------------------------------------------ #

    def _onLiveProducts(self, live):
        if self._closed or live is None:
            return
        self._lastLive = live
        self._pileupMax = float(getattr(live, 'pileup_max', 0.0) or 0.0)
        self._render(live.intensity, live.lifetime_ns, live.decay_counts, live.t_axis_ns,
                     peak_ns=live.peak_time_ns, background_per_bin=live.background_per_bin,
                     direction=live.tcspc_direction, tau_ns=live.global_tau_ns,
                     metadata=live.metadata, is_final=live.is_final,
                     gate_images=live.gate_images)
        if live.is_final and live.lifetime_ns is not None and self._widget.accumulateHistCheck.isChecked():
            self._pooledLifetimes.append(np.asarray(live.lifetime_ns, dtype=np.float32).ravel())
            self._renderHistogram()

    def _render(self, intensity, lifetime_ns, decay_counts, t_axis_ns, *, peak_ns, background_per_bin,
                direction, tau_ns, metadata, is_final, gate_images=None):
        widget = self._widget
        t = np.asarray(t_axis_ns, dtype=float)
        binwidth_ps = float(t[1] - t[0]) * 1000.0 if t.size > 1 else None
        widget.updateDecay(t, decay_counts, peak_ns=peak_ns, background_per_bin=background_per_bin,
                           direction=direction, tau_ns=tau_ns, binwidth_ps=binwidth_ps,
                           sted_ns=self._stedPeakNs)
        if gate_images:
            self._lastGateImages = dict(gate_images)
        g, s = phasor_point(t, decay_counts, float(widget.getSetting('rep_rate_mhz')), peak_ns or 0.0)
        widget.updatePhasor(g, s)
        # An intensity-only preview (live_fit_period_s = 0, or between fits)
        # carries no lifetime: the views keep the last fitted one.
        if lifetime_ns is not None:
            self._lastLifetime = np.asarray(lifetime_ns, dtype=np.float32)
        lifetime_ns = self._lastLifetime
        self._lastRender = (intensity, lifetime_ns, metadata)
        self._renderHistogram()
        if widget.getMode() == 'Tau STED' and lifetime_ns is not None:
            widget.updateScatter(intensity, lifetime_ns)
        self._renderLayers(intensity, lifetime_ns, metadata)

    def _renderLast(self):
        render = getattr(self, '_lastRender', None)
        if render is None:
            return
        intensity, lifetime_ns, metadata = render
        self._renderHistogram()
        if self._widget.getMode() == 'Tau STED' and lifetime_ns is not None:
            self._widget.updateScatter(intensity, lifetime_ns)
        self._renderLayers(intensity, lifetime_ns, metadata)

    def _renderGateLayers(self, wanted: set):
        """One layer per gate image of the last frame (``FLIM › gate:late``),
        plus the ratio of the last to the first gate, in Gated STED mode."""
        widget = self._widget
        images = getattr(self, '_lastGateImages', None) or {}
        if widget.getMode() != 'Gated STED' or not images:
            return
        names = list(images)
        if widget.gateLayersCheck.isChecked():
            for index, name in enumerate(names):
                kind = f'gate:{name}'
                self._upsertLayer(kind, np.asarray(images[name], dtype=np.float32),
                                  colormap=GATE_COLORMAPS[index % len(GATE_COLORMAPS)])
                wanted.add(self._layerName(kind))
        if widget.ratioCheck.isChecked() and len(names) >= 2:
            self._upsertLayer('gate ratio', ratio_image(images[names[-1]], images[names[0]]),
                              colormap='inferno')
            wanted.add(self._layerName('gate ratio'))

    def _renderHistogram(self):
        widget = self._widget
        if widget.accumulateHistCheck.isChecked() and self._pooledLifetimes:
            widget.updateLifetimeHistogram(np.concatenate(self._pooledLifetimes))
            return
        render = getattr(self, '_lastRender', None)
        if render is None or render[1] is None:
            widget.updateLifetimeHistogram(np.zeros(0))
            return
        widget.updateLifetimeHistogram(np.asarray(render[1]).ravel())

    # ------------------------------------------------------------------ #
    # Viewer layers                                                        #
    # ------------------------------------------------------------------ #

    def _layerName(self, kind: str) -> str:
        return f'{self._detectorName} › {kind}'

    def _layerScale(self):
        detector = self.detector
        try:
            sizes = list(detector.pixelSizeUm)
        except Exception:
            return None
        return [float(v) for v in sizes[-2:]] if len(sizes) >= 2 else None

    def _upsertLayer(self, kind: str, image, **options):
        name = self._layerName(kind)
        image = np.ascontiguousarray(image)
        self._commChannel.sigUpsertStaticLayer.emit(name, image, self._layerScale(), dict(options))
        self._layers.add(name)

    def _removeLayer(self, name: str):
        if name in self._layers:
            self._commChannel.sigRemoveStaticLayer.emit(name)
            self._layers.discard(name)

    def _removeAllLayers(self):
        for name in list(self._layers):
            self._removeLayer(name)

    def _renderLayers(self, intensity, lifetime_ns, metadata):
        widget = self._widget
        display = widget.getDisplay()
        wanted = set()
        tau_range = widget.getColourRange()
        intensity = np.asarray(intensity, dtype=np.float32)
        if display == 'lifetime' and lifetime_ns is not None:
            self._upsertLayer('lifetime', np.asarray(lifetime_ns, dtype=np.float32),
                              colormap='viridis', contrast_limits=tau_range)
            wanted.add(self._layerName('lifetime'))
        elif display == 'intensity':
            self._upsertLayer('intensity', intensity, colormap='gray')
            wanted.add(self._layerName('intensity'))
        elif display == 'overlay' and lifetime_ns is not None:
            self._upsertLayer('τ overlay', tau_overlay_rgb(lifetime_ns, intensity, tau_range), rgb=True)
            wanted.add(self._layerName('τ overlay'))
        self._renderGateLayers(wanted)
        if widget.pileupMapCheck.isChecked() and widget.getMode() == 'Tau STED':
            dwell_s = float((metadata or {}).get('dwell_s', 0.0) or 0.0)
            # Accumulated intensities span that many scans' worth of pulses;
            # the rate is the acquisition's, not the widget's current field.
            frames = int((metadata or {}).get('frames_accumulated', 1) or 1)
            rep_hz = float((metadata or {}).get('laser_rep_rate_mhz', 0.0) or 0.0) * 1e6
            if rep_hz <= 0:
                rep_hz = float(widget.getSetting('rep_rate_mhz')) * 1e6
            if dwell_s > 0 and rep_hz > 0:
                self._upsertLayer('pile-up', pileup_fraction(intensity, rep_hz, dwell_s * frames),
                                  colormap='inferno', contrast_limits=(0.0, 0.1))
                wanted.add(self._layerName('pile-up'))
        for name in list(self._layers):
            if name not in wanted:
                self._removeLayer(name)

    # ------------------------------------------------------------------ #
    # The Run path (worker thread)                                         #
    # ------------------------------------------------------------------ #

    def _buildFacade(self, detectorName: str):
        from imswitch.imcontrol.model.workflows.facade import build_facade_from_master
        return build_facade_from_master(
            self._master,
            time_resolved_detector_name=detectorName,
            scan_workflow=getattr(self._commChannel, 'scanWorkflow', None),
            scan_done_signal=getattr(self._commChannel, 'sigScanDone', None),
        )

    def _workerBusy(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def _fitConfig(self) -> LifetimeFitConfig:
        widget = self._widget
        return LifetimeFitConfig(
            method=str(widget.getSetting('fit_method')),
            min_counts_per_pixel=int(widget.getSetting('min_counts')),
            laser_rep_rate_mhz=float(widget.getSetting('rep_rate_mhz')),
        )

    def _runParams(self):
        from imswitch.imcontrol.model.workflows.time_resolved import TimeResolvedWorkflowParams
        gates = self._gateSpecs() if self._widget.getMode() == 'Gated STED' else ()
        return TimeResolvedWorkflowParams(
            capture_cube=False, gates=gates, fit=self._fitConfig(), timeout_s=RUN_TIMEOUT_S,
            measurement_name=str(self._widget.nameEdit.text() or 'lifetime'),
            save_h5=False, save_npz=False, save_tiff=False,
        )

    def _onLiveToggled(self, enabled: bool):
        if enabled:
            if not self._startRun(live=True):
                self._widget.setRunning(False)
        else:
            self._onStop()

    def _startRun(self, live: bool) -> bool:
        if self._detectorName is None:
            return False
        if self._workerBusy():
            self._widget.setFooterStatus('a run is in progress', error=True)
            return False
        if self._widget.getMode() == 'Gated STED' and not self._gateSpecs():
            self._widget.setFooterStatus('Gated STED needs at least one gate', error=True)
            return False
        self._stopEvent.clear()
        self._live = live
        snapshot = dict(detector=self._detectorName, params=self._runParams(),
                        accumulate=max(1, int(self._widget.accumulateSpin.value())))
        self._worker = threading.Thread(target=self._runLoop, args=(snapshot,),
                                        name='LifetimeRun', daemon=True)
        self._widget.setRunning(True, live)
        self._widget.setFooterStatus('running…')
        self._worker.start()
        return True

    def _runLoop(self, snapshot: dict):
        from imswitch.imcontrol.model.workflows.time_resolved import TimeResolvedScanWorkflow
        error = None
        try:
            facade = self._buildFacade(snapshot['detector'])
            while not self._stopEvent.is_set():
                collected = []
                for _k in range(snapshot['accumulate']):
                    if self._stopEvent.is_set():
                        break
                    result = TimeResolvedScanWorkflow(facade, snapshot['params']).run()
                    collected.append(result.products)
                if collected:
                    products = combine_products(collected)
                    self._invokeOnControllerThread(
                        partial(self._onRunProducts, products, snapshot['params']))
                if not self._live:
                    break
        except Exception as exc:  # reported on the GUI thread
            error = exc
        finally:
            self._invokeOnControllerThread(partial(self._onRunFinished, error))

    def _onRunProducts(self, products: TimeResolvedScanProducts, params=None):
        if self._closed:
            return
        self._lastProducts = products
        self._lastRunParams = params
        if params is not None and params.gates:
            products.metadata['gates_configured'] = [
                dict(name=g.name, start_ns=g.start_ns, stop_ns=g.stop_ns, reference=g.reference)
                for g in params.gates
            ]
        self._pileupMax = float(products.pileup_max or (products.metadata or {}).get('pileup_max', 0.0) or 0.0)
        self._render(products.intensity, products.lifetime_ns, products.decay_counts,
                     products.t_axis_ns, peak_ns=(products.metadata or {}).get('peak_time_ns'),
                     background_per_bin=float((products.metadata or {}).get('background_per_bin', 0.0) or 0.0),
                     direction=str(products.tcspc_direction or 'forward'),
                     tau_ns=float(products.global_tau_ns or 0.0), metadata=products.metadata,
                     is_final=True, gate_images=products.gate_images)
        if products.lifetime_ns is not None and self._widget.accumulateHistCheck.isChecked():
            self._pooledLifetimes.append(np.asarray(products.lifetime_ns, dtype=np.float32).ravel())
            self._renderHistogram()
        n = int((products.metadata or {}).get('frames_accumulated', 1) or 1)
        valid = bool((products.metadata or {}).get('frame_valid', True))
        self._widget.setFooterStatus(
            f'{"live" if self._live else "run"}: {n} scan{"s" if n != 1 else ""}, '
            f'global τ {float(products.global_tau_ns or 0.0):.2f} ns, '
            f'{int(np.asarray(products.decay_counts).sum()):,} photons'
            + ('' if valid else '  -- FRAME INVALID: the card did not close it or dropped tags'),
            error=not valid)

    def _onRunFinished(self, error):
        if self._closed:
            return
        self._live = False
        self._widget.setRunning(False)
        if error is not None:
            self._logger.error(f'Lifetime run failed: {error}')
            self._widget.setFooterStatus(f'run failed: {error}', error=True)
        elif self._stopEvent.is_set():
            self._widget.setFooterStatus('stopped')

    def _onStop(self):
        self._stopEvent.set()
        self._cancelCardCall()
        if self._workerBusy():
            try:
                self._commChannel.sigAbortScan.emit()
            except Exception:
                pass
            self._widget.setFooterStatus('stopping…')

    def _runOnWorker(self, function, what: str):
        """One blocking card call on a worker thread (sweep, scope, preflight)."""
        if self._workerBusy():
            self._widget.setFooterStatus(f'busy; {what} must wait for the run', error=True)
            return

        token = CancelToken()
        self._cardToken = token

        def body():
            error = None
            setCurrentCancelToken(token)   # Stop / shutdown reach the card's waits
            try:
                function()
            except OperationCancelled as exc:
                error = exc
            except Exception as exc:
                error = exc
            finally:
                clearCurrentCancelToken()
                self._invokeOnControllerThread(partial(self._onCardCallFinished, what, error))
        self._worker = threading.Thread(target=body, name='LifetimeCard', daemon=True)
        self._widget.setRunning(True)        # Stop cancels the diagnostic too
        self._widget.setFooterStatus(f'{what}…')
        self._worker.start()

    def _cancelCardCall(self):
        token = self._cardToken
        if token is not None:
            try:
                token.requestStop()
            except Exception:
                pass

    def _onCardCallFinished(self, what: str, error):
        if self._closed:
            return
        self._widget.setRunning(False)
        if error is not None:
            self._logger.warning(f'{what} failed: {error}')
            self._widget.setFooterStatus(f'{what}: {error}', error=True)
            self._widget.appendSignalsText(f'{what}: {error}')
        elif self._widget.footerStatus.text().startswith(what):
            self._widget.setFooterStatus(f'{what} done')

    # ------------------------------------------------------------------ #
    # Save                                                                 #
    # ------------------------------------------------------------------ #

    def _onSave(self):
        products = self._lastProducts
        if products is None:
            self._widget.setFooterStatus('nothing to save: run a scan first', error=True)
            return
        from imswitch.imcontrol.model.workflows.time_resolved import save_products
        folder = None
        getter = getattr(self._commChannel, 'getRecordingFolder', None)
        if callable(getter):
            try:
                folder = getter()
            except Exception:
                folder = None
        # The gates (and fit) that produced these images, not the table's
        # current state: the file must describe what was measured.
        base = self._lastRunParams if self._lastRunParams is not None else self._runParams()
        params = replace(base, save_h5=True, save_tiff=True, save_folder=folder,
                         measurement_name=str(self._widget.nameEdit.text() or base.measurement_name))

        def body():
            paths = save_products(products, params)
            self._invokeOnControllerThread(
                lambda: self._widget.setFooterStatus(
                    'saved ' + ', '.join(str(p) for p in paths.values())))
        self._runOnWorker(body, 'saving')

    # ------------------------------------------------------------------ #
    # Signals panel                                                        #
    # ------------------------------------------------------------------ #

    def _onTriggerLevelEdited(self, role: str, volts: float):
        if self._tt is None:
            return
        try:
            self._tt.set_trigger_level(role, volts)
            self._widget.setFooterStatus(f'{role} trigger level {volts:+.3f} V')
        except Exception as error:
            self._widget.setFooterStatus(str(error), error=True)
            try:
                self._widget.setTriggerLevel(role, self._tt.channels()[role].trigger_v)
            except Exception:
                pass

    def _onTriggerSweep(self, role: str):
        if self._tt is None or not role:
            return

        def body():
            report = self._tt.channels()[role]
            sign = -1.0 if report.channel < 0 else 1.0
            levels = np.linspace(0.05, 1.2, 12) * sign
            sweep = self._tt.trigger_sweep(role, levels, duration_s=0.2)
            lines = [f'trigger sweep on {role} (original {report.trigger_v:+.3f} V restored):']
            lines += [f'  {v:+.3f} V  {r:,.0f} Hz' for v, r in zip(sweep.levels_v, sweep.rates_hz)]
            lines.append(f'  {sweep.summary()}')
            self._invokeOnControllerThread(lambda: self._widget.setSignalsText('\n'.join(lines)))
        self._runOnWorker(body, 'trigger sweep')

    def _onScopeSnapshot(self):
        if self._tt is None:
            return
        roles = [r for r in SCOPE_ROLES if r in self._tt.roles()]
        if not roles:
            self._widget.setSignalsText('no clock roles to show')
            return
        trigger = 'line_clock' if 'line_clock' in roles else roles[0]
        detector = self._detectorName

        def body():
            trace = self._tt.scope(roles, trigger_role=trigger, window_ps=2_000_000_000,
                                   duration_s=0.5, detector_name=detector)
            lines = [f'scope: 2 ms after the first {trigger} edge (rising edges, us)']
            for name, events in trace.items():
                rising = [t for t, state in events if state == 'rising']
                shown = ', '.join(f'{t / 1e6:.3f}' for t in rising[:8])
                more = f' … ({len(rising)} edges)' if len(rising) > 8 else ''
                lines.append(f'  {name:14s} {shown or "no edge"}{more}')
            self._invokeOnControllerThread(lambda: self._widget.setSignalsText('\n'.join(lines)))
        self._runOnWorker(body, 'scope snapshot')

    def _onPreflight(self):
        if self._tt is None:
            return
        detector = self._detectorName

        def body():
            report = self._tt.preflight(detector_name=detector, duration_s=1.0)
            items = [('ok' if report.ok and not report.warnings else 'warn' if report.ok else 'fail',
                      report.summary().split('\n')[0])]
            items += [(item.status, item.line()) for item in report.items]
            self._invokeOnControllerThread(lambda: self._widget.setPreflight(items))
        self._runOnWorker(body, 'pre-flight')

    # ------------------------------------------------------------------ #
    # Component state                                                      #
    # ------------------------------------------------------------------ #

    def getComponentState(self) -> dict:
        widget = self._widget
        return {
            'detector': self._detectorName,
            'mode': widget.getMode(),
            'display': widget.getDisplay(),
            'tau_min_ns': float(widget.getSetting('tau_min_ns')),
            'tau_max_ns': float(widget.getSetting('tau_max_ns')),
            'accumulate': int(widget.accumulateSpin.value()),
            'name': str(widget.nameEdit.text()),
            'log': bool(widget.logCheck.isChecked()),
            'pileup_map': bool(widget.pileupMapCheck.isChecked()),
            'hist_bins': int(widget.histBinsSpin.value()),
            'pool_scans': bool(widget.accumulateHistCheck.isChecked()),
            'gates': widget.getGates(),
            'gate_layers': bool(widget.gateLayersCheck.isChecked()),
            'ratio_layer': bool(widget.ratioCheck.isChecked()),
        }

    def applyComponentState(self, state: dict, *, applyMode: ComponentStateApplyMode) -> list[str]:
        """Settings only, in either mode: never a run, never a card write."""
        warnings = []
        widget = self._widget
        detector = state.get('detector')
        if detector:
            if detector in self._detectors:
                widget.setDetectors(list(self._detectors), detector)
                self._selectDetector(detector)
            else:
                warnings.append(f'detector {detector!r} is not in this setup')
        if 'mode' in state:
            widget.setMode(str(state['mode']))
        if 'display' in state:
            widget.setDisplay(str(state['display']))
        for key in ('tau_min_ns', 'tau_max_ns'):
            if key in state:
                try:
                    widget.setSetting(key, float(state[key]))
                except Exception as error:
                    warnings.append(f'{key}: {error}')
        if 'accumulate' in state:
            widget.accumulateSpin.setValue(int(state['accumulate']))
        if 'name' in state:
            widget.nameEdit.setText(str(state['name']))
        if 'log' in state:
            widget.logCheck.setChecked(bool(state['log']))
        if 'pileup_map' in state:
            widget.pileupMapCheck.setChecked(bool(state['pileup_map']))
        if 'hist_bins' in state:
            widget.histBinsSpin.setValue(int(state['hist_bins']))
        if 'pool_scans' in state:
            widget.accumulateHistCheck.setChecked(bool(state['pool_scans']))
        if 'gates' in state:
            try:
                widget.setGates(list(state['gates']))
            except Exception as error:
                warnings.append(f'gates: {error}')
        if 'gate_layers' in state:
            widget.gateLayersCheck.setChecked(bool(state['gate_layers']))
        if 'ratio_layer' in state:
            widget.ratioCheck.setChecked(bool(state['ratio_layer']))
        return warnings

    def describeComponentState(self, state: dict) -> list[str]:
        return [f"mode: {state.get('mode', 'FLIM')}", f"display: {state.get('display', 'lifetime')}",
                f"colour range: {state.get('tau_min_ns', '?')}–{state.get('tau_max_ns', '?')} ns",
                f"accumulate: {state.get('accumulate', 1)} scans"]

    # ------------------------------------------------------------------ #
    # Shutdown                                                             #
    # ------------------------------------------------------------------ #

    #: How long shutdown waits for a run or a diagnostic to drain.
    CLOSE_JOIN_S = 5.0

    def closeEvent(self):
        """Stop everything this controller runs and say whether it drained.

        A diagnostic (sweep, scope, pre-flight) owns the card's calibration
        transaction while it runs, so it is cancelled cooperatively (the
        facade's waits poll a cancel token) and joined; a run is aborted
        and joined. Returns ``False`` while a worker is still alive, so
        the card is not finalized under it.
        """
        self._closed = True
        self._stopEvent.set()
        self._cancelCardCall()
        if self._card is not None:
            try:
                self._card.stopHealthSampling()
            except Exception:
                pass
        worker = self._worker
        drained = True
        if worker is not None and worker.is_alive():
            try:
                self._commChannel.sigAbortScan.emit()
            except Exception:
                pass
            worker.join(self.CLOSE_JOIN_S)
            if worker.is_alive():
                drained = False
                self._logger.error(
                    f'Lifetime worker {worker.name} is still running after '
                    f'{self.CLOSE_JOIN_S:g} s; the Time Tagger must not be freed under it'
                )
        if self._connectedDetector is not None:
            try:
                self._connectedDetector.sigTimeResolvedProducts.disconnect(self._onLiveProducts)
            except Exception:
                pass
        return drained
