import numpy as np
import threading

from imswitch.imcommon.framework import Signal, Thread, Worker
from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.timeresolved import (
    TimeResolvedDetectorMixin,
    TimeResolvedScanConfig,
    TimeResolvedScanProducts,
    compute_gate_images,
    copy_time_resolved_products,
)
from .DetectorManager import (
    DetectorManager, DetectorNumberParameter, DetectorListParameter)

try:
    import TimeTagger
    from TimeTagger import Flim, createTimeTagger
    _TIMETAGGER_AVAILABLE = True
except ImportError:
    TimeTagger = None
    Flim = None
    createTimeTagger = None
    _TIMETAGGER_AVAILABLE = False


class SwabianTimeTaggerManager(TimeResolvedDetectorMixin, DetectorManager):
    """
    TimeTagger FLIM detector. Returns fitted fluorescence lifetime per pixel.

    Channel and TCSPC settings are exposed as detector parameters and can be
    changed between scans via the GUI or setParameter(). Changes take effect
    on the next initiateScan() call.

    Required config properties:
      click_channel, start_channel, line_channel

    Optional config properties:
      n_bins (default 64), binwidth_ps (default 32),
      min_counts_per_pixel (default 20), fit_method (default 'moment'),
      laser_rep_rate_mhz (default 80.0; used by phasor fit to set ω),
      trigger_levels (dict {channel_str: volts}, used only to seed
        click_trigger / start_trigger / line_trigger defaults),
      enabled (default True)

    Pixel markers are generated internally via EventGenerator using
    auto-assigned virtual channel IDs — no physical channels are consumed.
    """

    def __init__(self, detectorInfo, name, nidaqManager, **_lowLevelManagers):
        self._logger = initLogger(self, instanceName=name)

        if not _TIMETAGGER_AVAILABLE:
            self._logger.error(
                'TimeTagger Python library not found. Install it from the Swabian Instruments '
                'software package. SwabianTimeTaggerManager will not function.'
            )


        self._detectorInfo = detectorInfo
        self._nidaqManager = nidaqManager
        props = getattr(detectorInfo, 'managerProperties', {}) or {}

        self._enabled = bool(props.get('enabled', True))
        tl = props.get('trigger_levels', {}) or {}

        # Internal state — always kept in sync with parameters via setParameter()
        self._click_ch = int(props['click_channel'])
        self._start_ch = int(props['start_channel'])
        self._line_ch = int(props['line_channel'])
        # Trigger levels keyed by role — defaults pulled from the trigger_levels
        # dict (keyed by channel number string) for backward compatibility.
        self._click_trigger = float(
            props.get('click_trigger', tl.get(str(self._click_ch), 0.5))
        )
        self._start_trigger = float(
            props.get('start_trigger', tl.get(str(self._start_ch), 0.5))
        )
        self._line_trigger = float(
            props.get('line_trigger', tl.get(str(self._line_ch), 0.5))
        )
        self._n_bins = int(props.get('n_bins', 64))
        self._binwidth_ps = int(props.get('binwidth_ps', 32))
        self._t0_ps = int(props.get('t0_ps', 0))
        self._min_counts_per_pixel = int(props.get('min_counts_per_pixel', 20))
        self._fit_method = str(props.get('fit_method', 'moment'))
        self._laser_rep_rate_mhz = float(props.get('laser_rep_rate_mhz', 80.0))
        self._accumulate_mode = False
        self._accum_sum: np.ndarray | None = None    # (Ny, Nx) float64, sum of valid lifetimes
        self._accum_count: np.ndarray | None = None  # (Ny, Nx) int32, number of valid scans per pixel

        parameters = {
            # --- Channel routing ---
            'click_channel': DetectorNumberParameter(
                group='Channels', value=self._click_ch,
                valueUnits='ch', editable=True),
            'start_channel': DetectorNumberParameter(
                group='Channels', value=self._start_ch,
                valueUnits='ch', editable=True),
            'line_channel': DetectorNumberParameter(
                group='Channels', value=self._line_ch,
                valueUnits='ch', editable=True),
            # --- Trigger levels (one per named channel role) ---
            'click_trigger': DetectorNumberParameter(
                group='Trigger Levels', value=self._click_trigger,
                valueUnits='V', editable=True),
            'start_trigger': DetectorNumberParameter(
                group='Trigger Levels', value=self._start_trigger,
                valueUnits='V', editable=True),
            'line_trigger': DetectorNumberParameter(
                group='Trigger Levels', value=self._line_trigger,
                valueUnits='V', editable=True),
            # --- TCSPC settings ---
            'n_bins': DetectorNumberParameter(
                group='TCSPC', value=self._n_bins,
                valueUnits='bins', editable=True),
            'binwidth_ps': DetectorNumberParameter(
                group='TCSPC', value=self._binwidth_ps,
                valueUnits='ps', editable=True),
            't0_ps': DetectorNumberParameter(
                group='TCSPC', value=self._t0_ps,
                valueUnits='ps', editable=True),
            'min_counts_per_pixel': DetectorNumberParameter(
                group='TCSPC', value=self._min_counts_per_pixel,
                valueUnits='counts', editable=True),
            # --- Lifetime fitting ---
            'fit_method': DetectorListParameter(
                group='Fitting', value=self._fit_method,
                options=['moment', 'phasor', 'exp1'],
                editable=True),
            'laser_rep_rate_mhz': DetectorNumberParameter(
                group='Fitting', value=self._laser_rep_rate_mhz,
                valueUnits='MHz', editable=True),
            # --- Accumulation ---
            'accumulate_mode': DetectorListParameter(
                group='Accumulation', value='off',
                options=['off', 'on'], editable=True),
        }

        self._tt = None
        self._flim = None
        self._flim_lock = threading.Lock()
        # Set while the scan coordinator is waiting for this detector's final
        # frame; see finishScan.
        self._finalFrameAck = None
        self._ev_pix_begin = None
        self._ev_pix_end = None
        self._scan = {}
        self._isMock = False  # True when hardware connection failed

        self.acquisition = False
        self._image_display = np.zeros((1, 64, 64), dtype=np.float32)
        self._image_intensity = np.zeros((1, 64, 64), dtype=np.float32)
        self._newFrameReady = False
        self.__pixel_sizes = [1, 1]

        # Latest aggregated TCSPC decay (summed over valid pixels) and the
        # global lifetime fit. Consumed by FLIMHistController's decay-histogram
        # mode. None until the first valid frame is processed.
        self._last_decay_counts: np.ndarray | None = None
        self._last_t_axis_ns: np.ndarray | None = None
        self._last_global_tau_ns: float = 0.0

        self._tr_config = TimeResolvedScanConfig()
        self._tr_enabled = False
        self._tr_last_products: TimeResolvedScanProducts | None = None
        self._tr_final_event = threading.Event()
        self._tr_lock = threading.Lock()

        super().__init__(detectorInfo, name, fullShape=(64, 64),
                         supportedBinnings=[1], model=name,
                         parameters=parameters, croppable=False)

        self._nidaqManager.sigScanBuilt.connect(
            lambda scanInfoDict, signalDict, _: self.initiateScan(scanInfoDict, signalDict)
        )
        self._nidaqManager.sigScanStarted.connect(self.startScan)
        self._nidaqManager.sigScanDone.connect(self._onScanDone)

        self._scanThread = None
        self._scanWorker = None

    def __del__(self):
        try:
            self.stopAcquisition()
        except Exception as e:
            self._logger.warning(f'Failed to stop acquisition during cleanup: {e}')
        try:
            with self._flim_lock:
                self._ev_pix_begin = None
                self._ev_pix_end = None
                self._flim = None
                self._tt = None
        except Exception as e:
            self._logger.warning(f'Failed to clean up TimeTagger objects: {e}')
        if hasattr(super(), '__del__'):
            super().__del__()

    # ------------------------------------------------------------------ #
    # Parameter handling                                                   #
    # ------------------------------------------------------------------ #

    def setParameter(self, name, value):
        """Update a parameter and mirror it into the corresponding internal attr."""
        super().setParameter(name, value)
        if name == 'click_channel':
            self._click_ch = int(value)
        elif name == 'start_channel':
            self._start_ch = int(value)
        elif name == 'line_channel':
            self._line_ch = int(value)
        elif name == 'click_trigger':
            self._click_trigger = float(value)
        elif name == 'start_trigger':
            self._start_trigger = float(value)
        elif name == 'line_trigger':
            self._line_trigger = float(value)
        elif name == 'n_bins':
            self._n_bins = int(value)
        elif name == 'binwidth_ps':
            self._binwidth_ps = int(value)
        elif name == 't0_ps':
            self._t0_ps = int(value)
        elif name == 'min_counts_per_pixel':
            self._min_counts_per_pixel = int(value)
        elif name == 'fit_method':
            self._fit_method = str(value)
        elif name == 'laser_rep_rate_mhz':
            self._laser_rep_rate_mhz = float(value)
        elif name == 'accumulate_mode':
            self._accumulate_mode = (str(value).lower() == 'on')
            if not self._accumulate_mode:
                self._accum_sum = None
                self._accum_count = None
        return self.parameters

    # ------------------------------------------------------------------ #
    # Scan lifecycle                                                        #
    # ------------------------------------------------------------------ #

    def _teardownScanThread(self):
        """Clean up any existing scan worker and thread before starting a new scan."""
        worker = self._scanWorker
        thread = self._scanThread
        self._scanWorker = None
        self._scanThread = None
        if worker is not None:
            try:
                worker.stop()
            except RuntimeError:
                pass  # C++ object already deleted — thread self-cleaned via deleteLater
        if thread is not None:
            try:
                if thread.isRunning():
                    thread.quit()
                    thread.wait()
            except RuntimeError:
                pass  # C++ object already deleted — thread is already done

    def initiateScan(self, scanInfoDict, signalDict):
        if not self._enabled:
            return
        if not _TIMETAGGER_AVAILABLE:
            self._logger.warning('TimeTagger not available — initiateScan skipped.')
            return

        Nx, Ny, S, outer_axes, outer_dims = self._infer_dims_from_scanInfo(scanInfoDict)
        self._validate_time_resolved_scan_shape(outer_axes, outer_dims)

        self._newFrameReady = False
        self._image_display = np.zeros((1, Ny, Nx), dtype=np.float32)
        self._image_intensity = np.zeros((1, Ny, Nx), dtype=np.float32)
        with self._tr_lock:
            self._tr_final_event.clear()
            if self._tr_enabled:
                self._tr_last_products = None

        # pixel_sizes: list from low to high dim (matches APDManager convention)
        self.setPixelSize(list(scanInfoDict.get('pixel_sizes', [1, 1])) or [1, 1])

        pixel_period_s = float(scanInfoDict.get('dwell_time', 0.0))
        if pixel_period_s <= 0:
            raise RuntimeError(
                f"Missing/invalid dwell_time in scanInfoDict: {scanInfoDict.get('dwell_time')}"
            )
        pixel_period_ps = int(round(pixel_period_s * 1e12))

        scan_time_step = float(scanInfoDict.get('scan_time_step', 0.0))
        if scan_time_step > 0:
            self._logger.debug(
                f'Estimated samples_per_pixel ~= {pixel_period_s / scan_time_step:.3f}'
            )

        self._scan = dict(
            Nx=Nx, Ny=Ny,
            n_pixels_total=Nx * Ny,
            pixel_period_ps=pixel_period_ps,
            # end[i] = begin[i] + pixel_period - 1 ps. If end and next begin
            # share a timestamp, TimeTagger's edge ordering can drop or
            # reorder the end edge — Flim's pixel index then stalls mid-line.
            pixel_width_ps=pixel_period_ps - 1,
            scan_info=dict(scanInfoDict),
        )
        self._shape = (Ny, Nx)

        try:
            if self._tt is None:
                self._tt = createTimeTagger()
                self._isMock = False
        except Exception:
            self._logger.exception(
                'createTimeTagger() failed — running in mock mode (no FLIM data).'
            )
            self._isMock = True
            with self._flim_lock:
                self._flim = None
            return

        try:
            self._tt.setTriggerLevel(self._click_ch, self._click_trigger)
            self._tt.setTriggerLevel(self._start_ch, self._start_trigger)
            self._tt.setTriggerLevel(self._line_ch, self._line_trigger)
            # Shift click-channel timestamps so the IRF peak lands at t=0.
            # A negative delay moves photon timestamps earlier by t0_ps,
            # placing the IRF peak at histogram bin 0.
            self._tt.setInputDelay(self._click_ch, -self._t0_ps)

            self._create_virtual_pixel_pulses()
            with self._flim_lock:
                self._flim = Flim(
                    self._tt,
                    start_channel=self._start_ch,
                    click_channel=self._click_ch,
                    pixel_begin_channel=self._ev_pix_begin.getChannel(),
                    pixel_end_channel=self._ev_pix_end.getChannel(),
                    n_pixels=int(self._scan['n_pixels_total']),
                    n_bins=self._n_bins,
                    binwidth=self._binwidth_ps,
                )
        except Exception:
            self._logger.exception(
                'TimeTagger FLIM setup failed — no data this scan.'
            )
            with self._flim_lock:
                self._flim = None
            return

        tot_scan_time_s = float(scanInfoDict.get('tot_scan_time_s', 0.0))
        ideal_scan_time_s = Nx * Ny * pixel_period_s
        overhead_pct = (
            (tot_scan_time_s - ideal_scan_time_s) / ideal_scan_time_s * 100
            if ideal_scan_time_s > 0 and tot_scan_time_s > 0 else 0.0
        )

        self._logger.info(
            f'TimeTagger prepared: click={self._click_ch}@{self._click_trigger}V, '
            f'start={self._start_ch}@{self._start_trigger}V, '
            f'line={self._line_ch}@{self._line_trigger}V, n_bins={self._n_bins}, '
            f'binwidth={self._binwidth_ps}ps, fit={self._fit_method}, '
            f'Nx={Nx}, Ny={Ny}, pixel_period={pixel_period_ps}ps, '
            f'scan_time={tot_scan_time_s:.3f}s '
            f'(ideal={ideal_scan_time_s:.3f}s, +{overhead_pct:.1f}% settling/flyback)'
        )

    def _create_virtual_pixel_pulses(self):
        Nx = int(self._scan['Nx'])
        period_ps = int(self._scan['pixel_period_ps'])
        width_ps = int(self._scan['pixel_width_ps'])

        begin_pattern = np.arange(Nx, dtype=np.int64) * np.int64(period_ps)
        end_pattern = begin_pattern + np.int64(width_ps)

        # Release previous generators before creating new ones
        self._ev_pix_begin = None
        self._ev_pix_end = None

        # No output_channel argument — the API auto-assigns virtual channel IDs
        # that are guaranteed not to collide with any physical channel.
        self._ev_pix_begin = TimeTagger.EventGenerator(
            self._tt, int(self._line_ch), begin_pattern
        )
        self._ev_pix_end = TimeTagger.EventGenerator(
            self._tt, int(self._line_ch), end_pattern
        )

    def startScan(self):
        if not self._enabled:
            return
        
        # Tear down any previous scan thread before starting a new one
        self._teardownScanThread()
        
        with self._flim_lock:
            flim = self._flim
        if flim is None:
            return

        self.acquisition = True
        self._scanWorker = _TTFlimWorker(self)
        self._scanThread = Thread()
        self._scanWorker.moveToThread(self._scanThread)
        self._scanThread.started.connect(self._scanWorker.run)
        self._scanWorker.sigFrameReady.connect(self._on_frame_ready)
        self._scanWorker.sigFinished.connect(self._scanThread.quit)
        self._scanWorker.sigFinished.connect(self._scanWorker.deleteLater)
        self._scanThread.finished.connect(self._scanThread.deleteLater)
        self._scanThread.start()

    def stopScan(self):
        if self._scanWorker is not None:
            self._scanWorker.stop()

    def _onScanDone(self):
        """Triggered by NidaqManager.sigScanDone — flips acquisition off and
        wakes the worker immediately so it reads the final completed frame.
        """
        self.acquisition = False
        if self._scanWorker is not None:
            try:
                self._scanWorker.signal_done()
            except RuntimeError:
                pass  # worker already cleaned up

    def finishScan(self, mode, acknowledge):
        """Hold the scan lease open until the final FLIM frame has landed.

        The final read is asynchronous: signal_done() wakes the worker, which
        emits sigFrameReady(is_final=True) some time later. Acknowledging
        immediately would let the coordinator release the lease, tear the
        worker down and arm the next repeat frame while that read is still in
        flight — losing the last frame of every scan.

        On abort, or with no worker/data to wait for, there is nothing to wait
        on and we acknowledge at once.
        """
        if mode != 'graceful' or self._scanWorker is None:
            acknowledge()
            return

        with self._flim_lock:
            haveFlim = self._flim is not None
        if not haveFlim:
            acknowledge()  # mock mode / setup failed: no final frame is coming
            return

        self._finalFrameAck = acknowledge
        self.acquisition = False
        try:
            self._scanWorker.signal_done()
        except RuntimeError:
            self._fireFinalFrameAck()  # worker already gone

    def _fireFinalFrameAck(self):
        """Release the coordinator's barrier once, whoever gets here first."""
        acknowledge, self._finalFrameAck = self._finalFrameAck, None
        if acknowledge is not None:
            acknowledge()

    def _on_frame_ready(self, intensity_img, lifetime_img, is_final: bool,
                        decay_counts, t_axis_ns, global_tau_ns: float):
        self._image_intensity[0] = intensity_img
        lifetime_ns = (lifetime_img * 1e9).astype(np.float32)
        self._last_decay_counts = decay_counts
        self._last_t_axis_ns = t_axis_ns
        self._last_global_tau_ns = float(global_tau_ns)

        if self._accumulate_mode:
            if is_final:
                # Final frame of this scan — commit to accumulation buffer.
                # Using the explicit flag avoids a race with continuous scanning
                # where acquisition may be True again by the time this slot runs.
                self._accum_add_frame(lifetime_ns)

            # Always display on every poll so the histogram widget keeps updating.
            # Show accumulated average as the baseline; overlay the current scan's
            # live pixels on top so the user sees real-time progress too.
            accum_avg = self._get_accum_avg(lifetime_ns.shape)
            displayed = accum_avg.copy()
            valid = lifetime_ns > 0
            displayed[valid] = lifetime_ns[valid]
            self._image_display[0] = displayed
        else:
            self._image_display[0] = lifetime_ns

        self._newFrameReady = True
        self.updateLatestFrame(True)
        self.sigNewFrame.emit()

        if is_final:
            # The scan's last frame is committed and published — the
            # coordinator may now release the lease and let the next
            # iteration arm.
            self._fireFinalFrameAck()

    # ------------------------------------------------------------------ #
    # Generic time-resolved detector contract                              #
    # ------------------------------------------------------------------ #

    def timeResolvedCapabilities(self) -> dict:
        return {
            "time_axis": "tcspc",
            "supports_binned_cube": True,
            "supports_raw_tags": False,
            "supports_software_gates": True,
            "supports_hardware_gates": False,
            "supports_lifetime_fit": True,
            "supports_outer_scan_axes": False,
            "native_cube_axes": ("y", "x", "tcspc_bin"),
            "vendor": "Swabian Instruments",
            "model": "Time Tagger",
        }

    def configureTimeResolvedProducts(self, config: TimeResolvedScanConfig) -> None:
        if not isinstance(config, TimeResolvedScanConfig):
            config = TimeResolvedScanConfig(
                capture_cube=bool(getattr(config, "capture_cube", False)),
                gates=tuple(getattr(config, "gates", ()) or ()),
                fit=getattr(config, "fit", None) or self._tr_config.fit,
                include_live_products=bool(
                    getattr(config, "include_live_products", False)
                ),
                max_retained_products=int(
                    getattr(config, "max_retained_products", 1)
                ),
            )
        fit = config.fit
        self._fit_method = str(fit.method)
        self._min_counts_per_pixel = int(fit.min_counts_per_pixel)
        if fit.laser_rep_rate_mhz is not None:
            self._laser_rep_rate_mhz = float(fit.laser_rep_rate_mhz)
        self.parameters["fit_method"].value = self._fit_method
        self.parameters["min_counts_per_pixel"].value = self._min_counts_per_pixel
        self.parameters["laser_rep_rate_mhz"].value = self._laser_rep_rate_mhz
        with self._tr_lock:
            self._tr_config = config
            self._tr_enabled = True
            self._tr_last_products = None
            self._tr_final_event.clear()

    def waitForFinalTimeResolvedProducts(
        self,
        timeout_s: float | None = None,
    ) -> TimeResolvedScanProducts:
        if not self._tr_final_event.wait(timeout=timeout_s):
            raise TimeoutError("Timed out waiting for final time-resolved products")
        products = self.getLastTimeResolvedProducts(copy=True)
        if products is None:
            raise RuntimeError("Final time-resolved event set without products")
        return products

    def getLastTimeResolvedProducts(
        self,
        *,
        copy: bool = True,
    ) -> TimeResolvedScanProducts | None:
        with self._tr_lock:
            products = self._tr_last_products
            if copy:
                return copy_time_resolved_products(products)
            return products

    def clearTimeResolvedProducts(self) -> None:
        with self._tr_lock:
            self._tr_config = TimeResolvedScanConfig()
            self._tr_enabled = False
            self._tr_last_products = None
            self._tr_final_event.clear()

    def _store_time_resolved_products(
        self,
        *,
        cube_counts: np.ndarray,
        intensity: np.ndarray,
        lifetime_s: np.ndarray,
        decay_counts: np.ndarray,
        t_axis_ns: np.ndarray,
        global_tau_ns: float,
        peak_bin: int,
        peak_time_ns: float,
        is_final: bool,
    ) -> None:
        with self._tr_lock:
            if not self._tr_enabled:
                return
            config = self._tr_config
            should_store = is_final or config.include_live_products
            if not should_store:
                return

            cube_for_storage = (
                np.array(cube_counts, copy=True) if config.capture_cube else None
            )
            gate_images = compute_gate_images(cube_counts, t_axis_ns, config.gates)
            metadata = {
                "detector_name": self.name,
                "backend": "SwabianTimeTaggerManager",
                "click_channel": int(self._click_ch),
                "start_channel": int(self._start_ch),
                "line_channel": int(self._line_ch),
                "click_trigger_v": float(self._click_trigger),
                "start_trigger_v": float(self._start_trigger),
                "line_trigger_v": float(self._line_trigger),
                "n_bins": int(self._n_bins),
                "binwidth_ps": int(self._binwidth_ps),
                "t0_ps": int(self._t0_ps),
                "fit_method": str(self._fit_method),
                "laser_rep_rate_mhz": float(self._laser_rep_rate_mhz),
                "min_counts_per_pixel": int(self._min_counts_per_pixel),
                "peak_bin": int(peak_bin),
                "peak_time_ns": float(peak_time_ns),
                "scan_info": dict(self._scan.get("scan_info", {})),
            }
            self._tr_last_products = TimeResolvedScanProducts(
                cube_counts=cube_for_storage,
                cube_axes=("y", "x", "tcspc_bin"),
                t_axis_ns=np.array(t_axis_ns, copy=True),
                intensity=np.array(intensity, copy=True).astype(np.float32, copy=False),
                lifetime_ns=(np.array(lifetime_s, copy=True) * 1e9).astype(np.float32),
                gate_images={
                    name: np.array(image, copy=True)
                    for name, image in gate_images.items()
                },
                decay_counts=np.array(decay_counts, copy=True),
                global_tau_ns=float(global_tau_ns),
                metadata=metadata,
                is_final=bool(is_final),
            )
            if is_final:
                self._tr_final_event.set()

    def _accum_add_frame(self, lifetime_ns: np.ndarray):
        """Add a completed-scan lifetime image (ns) to the running accumulation.
        Only pixels with lifetime > 0 contribute; zeros are treated as "no data"."""
        valid = lifetime_ns > 0
        if self._accum_sum is None or self._accum_sum.shape != lifetime_ns.shape:
            self._accum_sum = np.zeros(lifetime_ns.shape, dtype=np.float64)
            self._accum_count = np.zeros(lifetime_ns.shape, dtype=np.int32)
        self._accum_sum[valid] += lifetime_ns[valid].astype(np.float64)
        self._accum_count[valid] += 1
        n_accum = int(self._accum_count.max())
        self._logger.info(
            f'Accumulate: added scan #{n_accum}, '
            f'valid_px={int(valid.sum())}/{lifetime_ns.size}'
        )

    def _get_accum_avg(self, shape) -> np.ndarray:
        """Return per-pixel mean lifetime (ns) over all accumulated scans.
        Pixels with no data in any scan return 0."""
        if self._accum_sum is None or self._accum_sum.shape != shape:
            return np.zeros(shape, dtype=np.float32)
        with np.errstate(invalid='ignore', divide='ignore'):
            avg = np.where(
                self._accum_count > 0,
                self._accum_sum / self._accum_count,
                0.0,
            )
        return avg.astype(np.float32)

    # ------------------------------------------------------------------ #
    # DetectorManager abstract method implementations                      #
    # ------------------------------------------------------------------ #

    @property
    def isScanDriven(self):
        return True

    @property
    def pixelSizeUm(self):
        return [1, *self.__pixel_sizes]

    @property
    def scale(self):
        return list(self.__pixel_sizes[::-1])

    def setPixelSize(self, pixel_sizes: list):
        """pixel_sizes: list from low dim to high dim (matches APDManager)."""
        self.__pixel_sizes = list(pixel_sizes)

    @property
    def dtype(self):
        """ Override: FLIM intensity/lifetime buffers are always float32. """
        return np.dtype(np.float32)

    def crop(self, hpos, vpos, hsize, vsize):
        pass

    def getLatestFrame(self):
        return self._image_display

    def getChunk(self):
        if not getattr(self, '_newFrameReady', False):
            return np.empty((0, 0))
        self._newFrameReady = False
        return self._image_display.copy()

    def flushBuffers(self):
        pass

    def startAcquisition(self):
        self.acquisition = True
        self._newFrameReady = False

    def stopAcquisition(self):
        self.acquisition = False
        try:
            self._teardownScanThread()
        except Exception as e:
            # Detector stop contract: teardown failure must reach the
            # DetectorsManager, which quarantines this detector as FAULTED.
            self._logger.warning(f'Failed to stop scan thread: {e}')
            raise
        finally:
            self._newFrameReady = True

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    def _infer_dims_from_scanInfo(self, scanInfoDict):
        scan_dims = list(scanInfoDict['img_dims'])
        scan_axes = list(scanInfoDict.get('img_axes_phys', ['x', 'y', 'z'][:len(scan_dims)]))
        S = max(1, int(scanInfoDict.get('n_linesteps', 1)))
        if 'x' in scan_axes and 'y' in scan_axes:
            Nx = int(scan_dims[scan_axes.index('x')])
            Ny = int(scan_dims[scan_axes.index('y')])
        else:
            Nx = int(scan_dims[-1])
            Ny = int(scan_dims[-2])
        outer_axes = [a for a in scan_axes if a not in ('y', 'x')]
        outer_dims = [int(scan_dims[scan_axes.index(a)]) for a in outer_axes]
        return Nx, Ny, S, outer_axes, outer_dims

    def _validate_time_resolved_scan_shape(self, outer_axes, outer_dims):
        """Reject unsupported explicit product capture before acquisition starts."""

        if not self._tr_enabled:
            return
        active_outer = [
            (axis, dim)
            for axis, dim in zip(outer_axes, outer_dims)
            if int(dim) > 1
        ]
        if active_outer:
            detail = ", ".join(f"{axis}={dim}" for axis, dim in active_outer)
            raise RuntimeError(
                "Swabian time-resolved product capture currently supports only "
                f"2D x/y scans; unsupported outer scan axes: {detail}"
            )


# --------------------------------------------------------------------------- #
# Lifetime fit helpers — pure numpy, no Qt                                     #
# --------------------------------------------------------------------------- #

def _fit_moment(cube, t_axis, peak_bin: int = 0):
    """
    Mean photon arrival time (1st moment of the histogram).
    For an exp decay starting at t_peak the measured mean equals
    t_peak + τ, so we subtract t_peak. Fastest method, still biased by
    background and IRF width but no model required.
    Returns (intensity, lifetime) both shape (Ny, Nx).
    """
    intensity = cube.sum(axis=2)
    numer = (cube * t_axis[None, None, :]).sum(axis=2)
    lifetime = np.zeros_like(intensity, dtype=np.float32)
    good = intensity > 0
    t_peak = float(t_axis[int(peak_bin)]) if 0 <= peak_bin < len(t_axis) else 0.0
    lifetime[good] = (numer[good] / intensity[good]) - t_peak
    return intensity.astype(np.float32), lifetime


def _fit_phasor(cube, binwidth_ps, n_bins, laser_rep_rate_mhz):
    """
    Phasor / Fourier method for single-exponential lifetime.
    Uses the supplied laser repetition rate to set ω = 2π·f_rep.
    Returns tau = s / (omega * g) where g and s are the cosine and sine
    projections of the normalised histogram onto the first harmonic.

    Same speed as moment. Assumes single-exponential decay; gives the
    phase lifetime which is a useful proxy even for multi-exponential samples.
    Returns (intensity, lifetime) both shape (Ny, Nx).
    """
    intensity = cube.sum(axis=2).astype(np.float32)

    T_rep_s = 1.0 / (max(1.0, float(laser_rep_rate_mhz)) * 1e6)
    omega = 2.0 * np.pi / T_rep_s
    t_s = (np.arange(n_bins, dtype=np.float64) + 0.5) * binwidth_ps * 1e-12

    h = cube.astype(np.float64) / intensity.clip(1).astype(np.float64)[:, :, None]

    g = (h * np.cos(omega * t_s)[None, None, :]).sum(axis=2)
    s = (h * np.sin(omega * t_s)[None, None, :]).sum(axis=2)

    denom = omega * g
    lifetime = np.where(np.abs(denom) > 1e-30, s / denom, 0.0).astype(np.float32)
    return intensity, lifetime


def _fit_exp1(cube, t_axis):
    """
    Vectorised weighted log-linear single-exponential fit.
    Minimises sum_k w_k * (log h_k - a - b*t_k)^2 with w_k = sqrt(h_k)
    (Poisson weighting). Returns tau = -1/slope from the fitted slope b.

    Slower than moment/phasor but correct for clean mono-exponential decays.
    Returns (intensity, lifetime) both shape (Ny, Nx).
    """
    intensity = cube.sum(axis=2).astype(np.float32)
    h = cube.astype(np.float64)
    t = t_axis.astype(np.float64)[None, None, :]   # (1, 1, n_bins)

    # Weights: sqrt(h), zero where h == 0
    w = np.sqrt(np.where(h > 0, h, 0.0))
    log_h = np.where(h > 0, np.log(h), 0.0)       # log(0) replaced with 0, masked by w

    # Weighted normal equations for [intercept, slope]
    sw   = w.sum(axis=2)
    swt  = (w * t).sum(axis=2)
    swt2 = (w * t ** 2).sum(axis=2)
    swlh  = (w * log_h).sum(axis=2)
    swtlh = (w * t * log_h).sum(axis=2)

    det = sw * swt2 - swt ** 2
    with np.errstate(invalid='ignore', divide='ignore'):
        slope = np.where(np.abs(det) > 1e-30,
                         (sw * swtlh - swt * swlh) / det,
                         0.0)

    lifetime = np.where(slope < 0, (-1.0 / slope).astype(np.float32), 0.0)
    return intensity, lifetime.astype(np.float32)


# --------------------------------------------------------------------------- #
# Background worker                                                            #
# --------------------------------------------------------------------------- #

class _TTFlimWorker(Worker):
    # intensity, lifetime, is_final, decay_counts, t_axis_ns, global_tau_ns
    sigFrameReady = Signal(object, object, bool, object, object, float)
    sigFinished = Signal()

    # Live-preview interval: read Flim data once per second during a running
    # scan.  The worker also wakes immediately when signal_done() is called
    # (sigScanDone path), so the final frame is never delayed.
    LIVE_PREVIEW_S = 1.0
    STALL_MAX = 10  # consecutive live-preview ticks with no data → ~10 s

    def __init__(self, m: SwabianTimeTaggerManager):
        super().__init__()
        self._logger = initLogger(self, tryInheritParent=True)
        self._m = m
        self._running = True
        self._last_total_counts = 0.0
        self._done_event = threading.Event()

    def stop(self):
        self._running = False
        self._done_event.set()  # unblock wait() immediately

    def signal_done(self):
        """Called from the main thread when sigScanDone fires."""
        self._done_event.set()

    def run(self):
        try:
            Nx = int(self._m._scan['Nx'])
            Ny = int(self._m._scan['Ny'])
            n_bins = int(self._m._n_bins)
            binwidth_ps = int(self._m._binwidth_ps)
            fit_method = str(self._m._fit_method)
            min_counts = int(self._m._min_counts_per_pixel)
            expected_shape = (Nx * Ny, n_bins)

            t_axis = (np.arange(n_bins, dtype=np.float32) + 0.5) * binwidth_ps * 1e-12
            t_axis_f64 = t_axis.astype(np.float64)[None, None, :]
            # Phasor needs the laser repetition period, NOT the histogram window.
            # The Flim API doesn't expose the rep rate, so take it from the
            # user-supplied parameter. Default 80 MHz is the most common Ti:Sa rate.
            rep_rate_hz = max(1.0, float(self._m._laser_rep_rate_mhz)) * 1e6
            T_rep_s = 1.0 / rep_rate_hz
            omega = 2.0 * np.pi / T_rep_s
            t_s = (np.arange(n_bins, dtype=np.float64) + 0.5) * binwidth_ps * 1e-12
            cos_table = np.cos(omega * t_s)
            sin_table = np.sin(omega * t_s)

            stall_count = 0

            while self._running:
                # Block until scan-done signal OR live-preview tick, whichever
                # comes first.  scan_done=True means the event was set.
                scan_done = self._done_event.wait(timeout=self.LIVE_PREVIEW_S)
                if not self._running:
                    break

                cube = self._poll_frame(expected_shape, Nx, Ny, n_bins)

                if cube is None:
                    if scan_done:
                        break  # scan finished but Flim had no data yet
                    stall_count += 1
                    if stall_count >= self.STALL_MAX:
                        self._logger.error(
                            'TimeTagger: no valid frame for ~10 s. '
                            'Check line trigger signal. Stopping worker.'
                        )
                        break
                    continue

                stall_count = 0
                self._emit_frame(cube, t_axis, t_axis_f64, fit_method,
                                 omega, cos_table, sin_table, min_counts,
                                 rep_rate_hz, is_final=scan_done)

                if scan_done:
                    break  # final frame emitted — exit cleanly

            if self._last_total_counts == 0.0:
                self._logger.warning(
                    'FLIM scan finished with zero photons in all pixels. '
                    'Flim.getCurrentFrame() returned a shape-correct but empty '
                    'buffer on every poll. Likely causes: '
                    '(1) click_channel does not actually carry photon pulses '
                    '(check trigger_level sign and amplitude), '
                    '(2) line_channel does not fire — EventGenerator only '
                    'emits pixel_begin/pixel_end when its trigger channel '
                    'sees edges, '
                    '(3) wrong channel assignment in config '
                    '(your minimal example has LINECLOCK on ch2, LASERSYNC on ch3 — '
                    'verify click/start/line match your actual wiring).'
                )

        except Exception:
            self._logger.exception('TimeTagger FLIM worker crashed.')
        finally:
            self.sigFinished.emit()

    def _poll_frame(self, expected_shape, Nx, Ny, n_bins):
        """Single non-throwing poll. Returns a (Ny, Nx, n_bins) cube or None."""
        with self._m._flim_lock:
            flim = self._m._flim
        if flim is None:
            return None
        try:
            h = flim.getCurrentFrame()
        except Exception:
            self._logger.exception('getCurrentFrame() raised.')
            return None
        if h is None:
            return None
        arr = np.asarray(h)
        if arr.ndim != 2 or arr.shape != expected_shape:
            return None
        # Only cast if needed; avoid copy when already correct type and layout
        if arr.dtype != np.float32:
            arr = arr.astype(np.float32, copy=False)
        if not arr.flags['C_CONTIGUOUS']:
            arr = np.ascontiguousarray(arr)
        return arr.reshape(Ny, Nx, n_bins)

    def _emit_frame(self, cube, t_axis, t_axis_f64, fit_method,
                    omega, cos_table, sin_table, min_counts, rep_rate_hz,
                    *, is_final=False):
        # Pre-pass: intensity image + aggregated decay over valid pixels.
        # The aggregated decay drives IRF peak detection — every fitter is
        # then shifted by t_peak so the reported τ is referenced from the
        # rising edge of the laser pulse, not from t=0 of the histogram.
        intensity = cube.sum(axis=2).astype(np.float32)
        valid_mask = intensity >= min_counts
        if valid_mask.any():
            decay_counts = cube[valid_mask].sum(axis=0).astype(np.float32)
        else:
            decay_counts = cube.sum(axis=(0, 1)).astype(np.float32)

        peak_bin = int(np.argmax(decay_counts)) if decay_counts.sum() > 0 else 0
        t_peak = float(t_axis[peak_bin])

        if fit_method == 'phasor':
            lifetime = self._fit_phasor_cached(
                cube, intensity, omega, cos_table, sin_table, t_peak)
        elif fit_method == 'exp1':
            lifetime = self._fit_exp1_cached(cube, t_axis_f64, peak_bin)
        else:
            _, lifetime = _fit_moment(cube, t_axis, peak_bin)

        lifetime[intensity < min_counts] = 0.0
        lifetime[~np.isfinite(lifetime)] = 0.0
        lifetime[lifetime < 0] = 0.0
        # Clamp outliers: near-zero denominators (phasor g≈0) or near-zero
        # slopes (exp1) produce huge-but-finite values that pass the checks
        # above and explode the mean.  Cap at 5× the measurement window —
        # nothing physical exceeds that.
        lifetime[lifetime > t_axis[-1] * 5] = 0.0

        total = float(intensity.sum())
        self._last_total_counts = total
        self._logger.debug(
            f'FLIM emit: total={int(total)} '
            f'max_pix={int(intensity.max())} '
            f'nonzero_px={int((intensity > 0).sum())} '
            f'peak_bin={peak_bin} t_peak={t_peak * 1e9:.3f}ns '
            f'is_final={is_final}'
        )

        if valid_mask.any():
            global_tau_ns = self._global_tau_ns(
                decay_counts, t_axis, t_axis_f64, fit_method, rep_rate_hz,
                peak_bin,
            )
        else:
            global_tau_ns = 0.0
        t_axis_ns = (t_axis * 1e9).astype(np.float32)

        self._m._store_time_resolved_products(
            cube_counts=cube,
            intensity=intensity,
            lifetime_s=lifetime,
            decay_counts=decay_counts,
            t_axis_ns=t_axis_ns,
            global_tau_ns=float(global_tau_ns),
            peak_bin=peak_bin,
            peak_time_ns=t_peak * 1e9,
            is_final=is_final,
        )

        self.sigFrameReady.emit(
            intensity.astype(np.float32),
            lifetime.astype(np.float32),
            is_final,
            decay_counts,
            t_axis_ns,
            float(global_tau_ns),
        )

    def _global_tau_ns(self, decay_counts, t_axis, t_axis_f64,
                       fit_method, rep_rate_hz, peak_bin: int):
        """Fit a single τ to the aggregated decay using the selected method."""
        cube1 = decay_counts.reshape(1, 1, -1).astype(np.float32)
        intensity1 = cube1.sum(axis=2).astype(np.float32)
        t_peak = float(t_axis[peak_bin])
        if fit_method == 'phasor':
            T_rep_s = 1.0 / rep_rate_hz
            omega = 2.0 * np.pi / T_rep_s
            t_s = t_axis.astype(np.float64)
            cos_t = np.cos(omega * t_s)
            sin_t = np.sin(omega * t_s)
            tau_s = self._fit_phasor_cached(
                cube1, intensity1, omega, cos_t, sin_t, t_peak)
        elif fit_method == 'exp1':
            tau_s = self._fit_exp1_cached(cube1, t_axis_f64, peak_bin)
        else:
            _, tau_s = _fit_moment(cube1, t_axis, peak_bin)
        tau_ns = float(tau_s[0, 0]) * 1e9
        if not np.isfinite(tau_ns) or tau_ns <= 0:
            return 0.0
        return tau_ns

    def _fit_phasor_cached(self, cube, intensity, omega, cos_table, sin_table,
                           t_peak: float):
        """Phasor fit with IRF offset compensation.

        Projects onto cos/sin of the original time axis, then rotates the
        (g, s) phasor by −ω·t_peak.  With the e^{+iωt} convention used
        below (P = g + i·s = ⟨e^{+iωt}⟩) and a shifted decay
        h_meas(t) = h_true(t − t_peak), we have P_meas = e^{+iω·t_peak}·P_true,
        so P_true = e^{−iω·t_peak}·P_meas, i.e.
            g_true = g·cos(φ) + s·sin(φ),
            s_true = s·cos(φ) − g·sin(φ),  with φ = ω·t_peak.
        τ = s_true / (ω · g_true).
        """
        h = cube.astype(np.float64) / intensity.clip(1).astype(np.float64)[:, :, None]
        g = (h * cos_table[None, None, :]).sum(axis=2)
        s = (h * sin_table[None, None, :]).sum(axis=2)

        phi = omega * t_peak
        cphi, sphi = np.cos(phi), np.sin(phi)
        g_true = g * cphi + s * sphi
        s_true = s * cphi - g * sphi

        denom = omega * g_true
        with np.errstate(invalid='ignore', divide='ignore'):
            lifetime = np.where(np.abs(denom) > 1e-30, s_true / denom, 0.0)
        return lifetime.astype(np.float32)

    def _fit_exp1_cached(self, cube, t_axis_f64, peak_bin: int):
        """Exp1 fit, restricted to bins ≥ peak_bin with t-axis shifted so
        the IRF peak is at t=0. This removes the IRF rising edge from the
        fit, which otherwise tilts the slope."""
        n_bins = t_axis_f64.shape[-1]
        k0 = max(0, min(int(peak_bin), n_bins - 2))
        sub_cube = cube[..., k0:]
        # Shift so peak lands at t=0; keeps the fit anchored to the decay only.
        t = (t_axis_f64[..., k0:] - t_axis_f64[..., k0:k0 + 1])
        h = sub_cube.astype(np.float64)

        w = np.sqrt(np.where(h > 0, h, 0.0))
        log_h = np.where(h > 0, np.log(h), 0.0)

        sw   = w.sum(axis=2)
        swt  = (w * t).sum(axis=2)
        swt2 = (w * t ** 2).sum(axis=2)
        swlh  = (w * log_h).sum(axis=2)
        swtlh = (w * t * log_h).sum(axis=2)

        det = sw * swt2 - swt ** 2
        with np.errstate(invalid='ignore', divide='ignore'):
            slope = np.where(np.abs(det) > 1e-30,
                            (sw * swtlh - swt * swlh) / det,
                            0.0)

        lifetime = np.where(slope < 0, (-1.0 / slope).astype(np.float32), 0.0)
        return lifetime.astype(np.float32)
