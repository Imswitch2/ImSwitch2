import json
import math
import numpy as np
import threading
import time

from imswitch.imcommon.framework import Signal, Thread, Worker
from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.SetupInfo import TimeTaggerInfo
from imswitch.imcontrol.model.managers.TimeTaggerManager import (
    ROLES, TimeTaggerBusyError, TimeTaggerError, TimeTaggerManager,
)
from imswitch.imcontrol.model.timeresolved import (
    PILEUP_WARN,
    LiveProducts,
    TimeResolvedDetectorMixin,
    TimeResolvedScanConfig,
    TimeResolvedScanProducts,
    background_per_bin,
    compute_gate_images,
    copy_time_resolved_products,
    orient_cube,
    pileup_fraction,
    roll_to_peak,
    subtract_background,
)
from imswitch.imcontrol.model.timeresolved.fitting import (
    fit_exp1, fit_moment, fit_phasor,
)
from .._scan_execution import PARTICIPANTS_KEY
from .DetectorManager import (
    ChunkPayload, DetectorManager, DetectorNumberParameter, DetectorListParameter,
    scanPixelSizesToZYX, _EMPTY_CHUNK)

_SCAN_THREAD_JOIN_TIMEOUT_MS = 2000

#: Detector properties that described the card before the setup-level
#: ``timeTagger`` block existed. Read only when that block is absent.
_LEGACY_CARD_PROPS = frozenset({
    'click_channel', 'start_channel', 'line_channel',
    'click_trigger', 'start_trigger', 'line_trigger', 'trigger_levels',
})


#: A TCSPC window shorter than this fraction of the laser period is warned
#: about: the decay is cut off, and the moment and phasor fits read the cut as
#: a short lifetime.
MIN_WINDOW_FRACTION_OF_PERIOD = 0.8


def histogram_bins_for_period(binwidth_ps, laser_rep_rate_mhz) -> int:
    """Bins of ``binwidth_ps`` that span one period of ``laser_rep_rate_mhz``."""
    period_ps = 1e6 / max(1e-9, float(laser_rep_rate_mhz))
    return max(1, int(math.ceil(period_ps / max(1e-9, float(binwidth_ps)))))


class SwabianTimeTaggerManager(TimeResolvedDetectorMixin, DetectorManager):
    #: ``LiveProducts`` for every frame the worker publishes (previews and the
    #: final frame), emitted on the manager's thread; never carries the cube.
    sigTimeResolvedProducts = Signal(object)

    """
    TimeTagger FLIM detector. Returns fitted fluorescence lifetime per pixel.

    The card itself is the setup's shared ``TimeTaggerManager`` (the
    ``timeTagger`` block): it owns the connection, the channel *roles* and
    their conditioning. This detector only says which roles it uses --
    ``click_role`` (default ``photons``), ``start_role`` (``laser_sync``) and
    ``line_role`` (``line_clock``) -- and builds its ``Flim`` on it. The
    resolved channels show as read-only parameters; the trigger-level
    parameters write through to the card between scans.

    Setups without a ``timeTagger`` block keep working: the detector builds a
    private card manager from its own ``click_channel`` / ``start_channel`` /
    ``line_channel`` and trigger properties and logs the equivalent block
    (deprecated; the block is where a second consumer or a script finds the
    card).

    Optional config properties:
      click_role, start_role, line_role (role names, see above),
      n_bins (default: one laser period), binwidth_ps (default 32),
      min_counts_per_pixel (default 20), fit_method (default 'moment'),
      laser_rep_rate_mhz (default 80.0; used by phasor fit to set ω),
      t0_ps (default 0), enabled (default True)

    Legacy config properties (used only without a ``timeTagger`` block):
      click_channel, start_channel, line_channel,
      click_trigger / start_trigger / line_trigger,
      trigger_levels (dict {channel_str: volts}, seeds the trigger defaults)

    Pixel markers are generated internally via EventGenerator using
    auto-assigned virtual channel IDs — no physical channels are consumed.
    """

    def __init__(self, detectorInfo, name, nidaqManager, timeTaggerManager=None,
                 **_lowLevelManagers):
        self._logger = initLogger(self, instanceName=name)
        self._instanceName = name

        self._detectorInfo = detectorInfo
        self._nidaqManager = nidaqManager
        props = getattr(detectorInfo, 'managerProperties', {}) or {}

        self._enabled = bool(props.get('enabled', True))

        self._click_role = str(props.get('click_role', 'photons'))
        self._start_role = str(props.get('start_role', 'laser_sync'))
        self._line_role = str(props.get('line_role', 'line_clock'))
        # The frame role, when the card has one, lets Flim re-sync its pixel
        # index on every frame edge; 'none' keeps the line-clock-only path.
        self._frame_role = str(props.get('frame_role', 'auto'))
        # Per-pixel fits during the scan every this many seconds; 0 fits the
        # final frame only and previews the intensity (cheap on big frames).
        self._live_fit_period_s = float(props.get('live_fit_period_s', 1.0))
        if timeTaggerManager is not None:
            self._timeTagger = timeTaggerManager
            self._ownsTimeTagger = False
            legacy = sorted(_LEGACY_CARD_PROPS.intersection(props))
            if legacy:
                self._logger.info(
                    f'The setup has a timeTagger block, so these detector '
                    f'properties are ignored: {", ".join(legacy)}. Channels '
                    f'and trigger levels come from the block\'s roles.'
                )
        else:
            self._timeTagger = self._buildPrivateTimeTagger(props, nidaqManager)
            self._ownsTimeTagger = True

        # Internal state — always kept in sync with parameters via setParameter()
        self._resolveRoles(publish=False)
        self._binwidth_ps = int(props.get('binwidth_ps', 32))
        self._laser_rep_rate_mhz = float(props.get('laser_rep_rate_mhz', 80.0))
        # The histogram window and the laser period are one physical fact
        # expressed twice, so only one of them is a free default. The window
        # used to default to 64 bins of 32 ps -- 2.048 ns against the 12.5 ns
        # period this same constructor declares -- and a decay cut off at a
        # sixth of its period reads as a short lifetime: the moment and phasor
        # fits reported 0.84-0.95 ns for anything from 2 to 6 ns, plausibly,
        # with the intensity image looking right and no warning anywhere.
        # Undeclared, the window now spans one laser period.
        declared_bins = props.get('n_bins')
        if declared_bins is None:
            self._n_bins = histogram_bins_for_period(
                self._binwidth_ps, self._laser_rep_rate_mhz
            )
        else:
            self._n_bins = int(declared_bins)
        self._t0_ps = int(props.get('t0_ps', 0))
        self._min_counts_per_pixel = int(props.get('min_counts_per_pixel', 20))
        self._fit_method = str(props.get('fit_method', 'moment'))
        # Dark counts and afterpulsing of the photon detector, flat over the
        # laser period: measured with the laser blocked and subtracted from
        # every pixel's histogram before fitting. 0 = no subtraction.
        self._background_rate_hz = float(props.get('background_rate_hz', 0.0))
        self._warnIfWindowTruncatesTheDecay()
        self._accumulate_mode = False
        self._accum_sum: np.ndarray | None = None    # (Ny, Nx) float64, sum of valid lifetimes
        self._accum_count: np.ndarray | None = None  # (Ny, Nx) int32, number of valid scans per pixel

        roleOptions = list(ROLES)
        parameters = {
            # --- Channel routing: roles are chosen here, channels are what
            # the card's block maps them to ---
            'click_role': DetectorListParameter(
                group='Channels', value=self._click_role,
                options=roleOptions, editable=True),
            'start_role': DetectorListParameter(
                group='Channels', value=self._start_role,
                options=roleOptions, editable=True),
            'line_role': DetectorListParameter(
                group='Channels', value=self._line_role,
                options=roleOptions, editable=True),
            'frame_role': DetectorListParameter(
                group='Channels', value=self._frame_role,
                options=['none'] + roleOptions, editable=True),
            'frame_channel': DetectorNumberParameter(
                group='Channels', value=self._frame_ch if self._frame_ch is not None else 0,
                valueUnits='ch', editable=False),
            'live_fit_period_s': DetectorNumberParameter(
                group='TCSPC', value=self._live_fit_period_s,
                valueUnits='s', editable=True),
            # Whether the detector takes part in scans at all. 'False' lets
            # another detector (the APD) image while the card stays free
            # for a calibration during the scan (tutorials 07 to 09).
            'enabled': DetectorListParameter(
                group='Scan', value='True' if self._enabled else 'False',
                options=['True', 'False'], editable=True),
            'click_channel': DetectorNumberParameter(
                group='Channels', value=self._click_ch,
                valueUnits='ch', editable=False),
            'start_channel': DetectorNumberParameter(
                group='Channels', value=self._start_ch,
                valueUnits='ch', editable=False),
            'line_channel': DetectorNumberParameter(
                group='Channels', value=self._line_ch,
                valueUnits='ch', editable=False),
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
            # --- Background ---
            'background_rate_hz': DetectorNumberParameter(
                group='Background', value=self._background_rate_hz,
                valueUnits='Hz', editable=True),
            # --- Accumulation ---
            'accumulate_mode': DetectorListParameter(
                group='Accumulation', value='off',
                options=['off', 'on'], editable=True),
        }

        self._flim = None
        self._flim_lock = threading.Lock()
        # End-of-scan acknowledgements are indexed by the worker generation
        # that must produce the final frame.  The lock closes the race where a
        # final frame lands while finishScan is installing its callback.
        self._finishAckLock = threading.Lock()
        self._finishAcks = {}
        self._completedFinalFrameGenerations = set()
        self._finishedScanGenerations = set()
        self._scanGeneration = 0
        # Which scan generation holds the card, so a stale worker's
        # completion cannot release a newer scan's hold (_releaseCard).
        self._cardHeldGeneration = None
        self._scanAppliedClickDelay = False
        self._preparedScanGeneration = None
        self._activeScanGeneration = None
        self._scanParticipating = False
        self._ev_pix_begin = None
        self._ev_pix_end = None
        self._scan = {}

        self.acquisition = False
        self._image_display = np.zeros((1, 64, 64), dtype=np.float32)
        self._image_intensity = np.zeros((1, 64, 64), dtype=np.float32)
        self._newFrameReady = False
        # The raw frame a recording saves is the finished lifetime image, which
        # exists once per scan: when the worker's final frame lands. Before
        # that there is nothing to save; after it is delivered, nothing again.
        self._rawReady = False
        self._rawDelivered = True
        self._image_raw = None
        self.__pixel_sizes = [1, 1]

        # Latest aggregated TCSPC decay (summed over valid pixels) and the
        # global lifetime fit. Consumed by FLIMHistController's decay-histogram
        # mode. None until the first valid frame is processed.
        self._last_decay_counts: np.ndarray | None = None
        self._last_t_axis_ns: np.ndarray | None = None
        self._last_global_tau_ns: float = 0.0

        self._tr_config = TimeResolvedScanConfig()
        self._tr_enabled = False
        self._tr_owner: str | None = None
        self._frame_index = -1
        self._tr_last_products: TimeResolvedScanProducts | None = None
        self._tr_final_event = threading.Event()
        self._tr_lock = threading.Lock()

        super().__init__(detectorInfo, name, fullShape=(64, 64),
                         supportedBinnings=[1], model=name,
                         parameters=parameters, croppable=False)

        self._nidaqManager.sigScanBuilt.connect(self._onScanBuilt)
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
            self._releaseCard()
        except Exception as e:
            self._logger.warning(f'Failed to clean up TimeTagger objects: {e}')
        if hasattr(super(), '__del__'):
            super().__del__()

    # ------------------------------------------------------------------ #
    # Parameter handling                                                   #
    # ------------------------------------------------------------------ #

    def setParameter(self, name, value):
        """Update a parameter and mirror it into the corresponding internal attr."""
        if name in ('click_channel', 'start_channel', 'line_channel', 'frame_channel'):
            # Read-only: the channel is whatever the card's block maps the
            # role to. Re-publish the resolved value instead of taking one.
            resolved = getattr(self, f'_{name[:-8]}_ch')
            self.parameters[name].value = resolved if resolved is not None else 0
            return self.parameters
        super().setParameter(name, value)
        if name in ('click_role', 'start_role', 'line_role', 'frame_role'):
            setattr(self, f'_{name}', str(value))
            self._resolveRoles()
        elif name == 'live_fit_period_s':
            self._live_fit_period_s = max(0.0, float(value))
        elif name == 'enabled':
            self._enabled = str(value).strip().lower() in ('true', '1', 'yes', 'on')
            self.parameters['enabled'].value = 'True' if self._enabled else 'False'
        elif name in ('click_trigger', 'start_trigger', 'line_trigger'):
            self._writeTriggerLevel(name[:-8], float(value))
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
            self._warnIfWindowTruncatesTheDecay()
        elif name == 'background_rate_hz':
            self._background_rate_hz = max(0.0, float(value))
        elif name == 'accumulate_mode':
            self._accumulate_mode = (str(value).lower() == 'on')
            if not self._accumulate_mode:
                self._accum_sum = None
                self._accum_count = None
        return self.parameters

    # ------------------------------------------------------------------ #
    # The shared card: roles, trigger levels, scan hold                    #
    # ------------------------------------------------------------------ #

    def _buildPrivateTimeTagger(self, props, nidaqManager):
        """Compatibility: no ``timeTagger`` block, channels on the detector."""
        tl = props.get('trigger_levels', {}) or {}
        # Optional in the schema (a setup with a timeTagger block has none
        # of them), required here: without the block they are the card.
        click_channel = props.get('click_channel')
        start_channel = props.get('start_channel')
        line_channel = props.get('line_channel')
        if click_channel is None or start_channel is None or line_channel is None:
            raise ValueError(
                f'{self._instanceName}: without a top-level "timeTagger" block, '
                f'click_channel, start_channel and line_channel are required '
                f'in managerProperties'
            )
        # Resolved again from the roles once the card exists.
        self._click_ch = int(click_channel)
        self._start_ch = int(start_channel)
        self._line_ch = int(line_channel)
        info = TimeTaggerInfo(
            photonsChannel=self._click_ch,
            photonsTriggerV=float(
                props.get('click_trigger', tl.get(str(self._click_ch), 0.5))
            ),
            laserSyncChannel=self._start_ch,
            laserSyncTriggerV=float(
                props.get('start_trigger', tl.get(str(self._start_ch), 0.5))
            ),
            lineClockChannel=self._line_ch,
            lineClockTriggerV=float(
                props.get('line_trigger', tl.get(str(self._line_ch), 0.5))
            ),
        )
        # The roles must be the three the legacy properties describe.
        self._click_role, self._start_role, self._line_role = (
            'photons', 'laser_sync', 'line_clock'
        )
        block = {
            'photonsChannel': info.photonsChannel,
            'photonsTriggerV': info.photonsTriggerV,
            'laserSyncChannel': info.laserSyncChannel,
            'laserSyncTriggerV': info.laserSyncTriggerV,
            'lineClockChannel': info.lineClockChannel,
            'lineClockTriggerV': info.lineClockTriggerV,
        }
        self._logger.warning(
            'This setup has no top-level "timeTagger" block; the Time Tagger '
            'is configured from this detector\'s click/start/line properties '
            '(deprecated). Move them to the block so scripts and other '
            'consumers can find the card: "timeTagger": '
            + json.dumps(block)
        )
        return TimeTaggerManager(info, nidaqManager=nidaqManager)

    def _resolveRoles(self, publish=True):
        """Mirror the roles' channels and trigger levels into the attributes
        the scan code reads and, unless ``publish`` is false (before the base
        class has its parameters), into the read-only parameters."""
        tt = self._timeTagger
        resolved = {}
        for which, role in (('click', self._click_role),
                            ('start', self._start_role),
                            ('line', self._line_role)):
            try:
                resolved[which] = tt.channelInfo(role)
            except TimeTaggerError as error:
                raise ValueError(
                    f'{self._instanceName}: {which}_role {role!r} is not a '
                    f'configured Time Tagger role: {error}'
                ) from None
        self._click_ch = resolved['click'].channel
        self._start_ch = resolved['start'].channel
        self._line_ch = resolved['line'].channel
        self._click_trigger = resolved['click'].trigger_v
        self._start_trigger = resolved['start'].trigger_v
        self._line_trigger = resolved['line'].trigger_v
        # 'auto': the frame clock when the card has one, else none.
        frame_role = self._frame_role
        if frame_role == 'auto':
            frame_role = 'frame_clock' if tt.hasRole('frame_clock') else 'none'
        if frame_role == 'none':
            self._frame_ch = None
        elif tt.hasRole(frame_role):
            self._frame_ch = tt.channel(frame_role)
        else:
            raise ValueError(
                f'{self._instanceName}: frame_role {frame_role!r} is not a '
                f'configured Time Tagger role'
            )
        self._frame_role_resolved = frame_role
        if publish:
            params = self.parameters
            for which in ('click', 'start', 'line'):
                if f'{which}_channel' in params:
                    params[f'{which}_channel'].value = resolved[which].channel
                if f'{which}_trigger' in params:
                    params[f'{which}_trigger'].value = resolved[which].trigger_v
            if 'frame_channel' in params:
                params['frame_channel'].value = self._frame_ch if self._frame_ch is not None else 0

    def _writeTriggerLevel(self, which, voltage):
        """A trigger-level parameter writes through to the card; refused
        while a scan holds it, in which case the parameter shows the card's
        real value again."""
        role = getattr(self, f'_{which}_role')
        try:
            self._timeTagger.setTriggerLevel(role, voltage)
        except TimeTaggerBusyError as error:
            self._logger.warning(str(error))
        except TimeTaggerError as error:
            self._logger.error(
                f'Could not set the {which} trigger level on the card: {error}'
            )
        self._resolveRoles()

    def _releaseCard(self, generation=None):
        """End this detector's scan hold on the card.

        A worker's completion passes its ``generation``: it releases only
        while that generation is the one holding the card. A newer scan
        may have been prepared (and taken the hold) while the old worker's
        final frame was still in flight; the old worker must not release
        the new scan's hold. Teardown paths pass nothing and release
        unconditionally.
        """
        try:
            held = self._cardHeldGeneration
        except Exception:  # built without __init__ (the stop-contract tests)
            held = None
        if generation is not None and generation != held:
            return
        try:
            self._cardHeldGeneration = None
            self._restoreClickDelay()
            self._timeTagger.endScanHold(self.name)
        except Exception:
            pass

    def _restoreClickDelay(self):
        """Put the photon input back to the card's configured delay.

        A forward-mode scan adds ``-t0_ps`` to it so the IRF peak lands at
        bin 0; the diagnostics (``histogram()``, tutorial 06) measure
        against the configured delay, so a t0 found between scans is
        absolute and never double-counts a delay a scan left behind.
        """
        if not self._scanAppliedClickDelay:
            return
        self._scanAppliedClickDelay = False
        try:
            tt = self._timeTagger
            if tt.connected:
                tt.tagger.setInputDelay(
                    self._click_ch, tt.hardwareDelayPs(self._click_role)
                )
        except Exception as error:
            self._logger.warning(
                f'Could not restore the photon input delay after the scan: {error}'
            )

    @property
    def timeTagger(self):
        """The shared card manager this detector acquires through."""
        return self._timeTagger

    def pixel_marker_channels(self):
        """The virtual channels of the prepared scan's pixel markers,
        ``{'pixel_begin': ch, 'pixel_end': ch}`` (empty before a scan is
        prepared), for ``facade.time_tagger.scope(extra_channels=...)``."""
        out = {}
        begin, end = self._ev_pix_begin, self._ev_pix_end
        if begin is not None:
            out['pixel_begin'] = int(begin.getChannel())
        if end is not None:
            out['pixel_end'] = int(end.getChannel())
        return out

    @property
    def _isMock(self):
        return bool(getattr(self._timeTagger, 'isMock', False))

    # ------------------------------------------------------------------ #
    # Scan lifecycle                                                        #
    # ------------------------------------------------------------------ #

    def _waitForScanThread(self, thread, timeoutMs):
        """Join a Qt/framework thread without a wait-forever fallback."""
        timeoutMs = max(0, int(timeoutMs))
        try:
            running = thread.isRunning()
        except RuntimeError:
            return True

        if not running:
            try:
                result = thread.wait()
            except TypeError:
                result = thread.wait(0)
            except RuntimeError:
                return True
            return result is not False

        try:
            result = thread.wait(timeoutMs)
        except TypeError:
            # framework.Thread exposes wait() without a timeout. Poll its
            # state, and call wait() only after it is known to have stopped.
            deadline = time.monotonic() + timeoutMs / 1000
            while time.monotonic() < deadline:
                try:
                    if not thread.isRunning():
                        thread.wait()
                        return True
                except RuntimeError:
                    return True
                time.sleep(
                    min(0.01, max(0, deadline - time.monotonic()))
                )
            return False
        except RuntimeError:
            return True

        try:
            stillRunning = thread.isRunning()
        except RuntimeError:
            return True
        return result is not False and not stillRunning

    def _teardownScanThread(
        self, timeoutMs=_SCAN_THREAD_JOIN_TIMEOUT_MS
    ):
        """Stop one worker with a deadline, retaining it until exit is proven."""
        worker = self._scanWorker
        thread = self._scanThread
        errors = []
        if worker is not None:
            try:
                worker.stop()
            except RuntimeError:
                pass  # C++ object already deleted — thread self-cleaned via deleteLater
            except Exception as error:
                errors.append(error)
        if thread is not None:
            try:
                if thread.isRunning():
                    thread.quit()
                if not self._waitForScanThread(thread, timeoutMs):
                    errors.append(
                        TimeoutError(
                            'TimeTagger scan thread did not stop within '
                            f'{timeoutMs / 1000:g} seconds'
                        )
                    )
            except RuntimeError:
                pass  # C++ object already deleted — thread is already done
            except Exception as error:
                errors.append(error)

        if errors:
            # Do not discard the only identities through which retry can stop
            # the worker.  Detector lease recovery will call stopAcquisition
            # again and the manager remains quarantined until that succeeds.
            raise errors[0]

        if self._scanWorker is worker:
            self._scanWorker = None
        if self._scanThread is thread:
            self._scanThread = None

    def initiateScan(self, scanInfoDict, signalDict):
        participants = scanInfoDict.get(PARTICIPANTS_KEY)
        self._scanParticipating = (
            participants is None or self.name in participants
        )
        self._preparedScanGeneration = None
        if not self._scanParticipating:
            return

        # Invalidate every object that could make startScan reuse a previous
        # iteration before validating or allocating anything for this one.
        self._activeScanGeneration = None
        with self._flim_lock:
            self._flim = None
        self._scan = {}

        self._scanGeneration += 1
        generation = self._scanGeneration
        with self._finishAckLock:
            oldestRetained = generation - 8
            self._completedFinalFrameGenerations = {
                item for item in self._completedFinalFrameGenerations
                if item >= oldestRetained
            }
            self._finishedScanGenerations = {
                item for item in self._finishedScanGenerations
                if item >= oldestRetained
            }
        if not self._enabled:
            return

        Nx, Ny, S, outer_axes, outer_dims = self._infer_dims_from_scanInfo(scanInfoDict)
        self._validate_time_resolved_scan_shape(outer_axes, outer_dims)
        if S > 1:
            # The TTL designer emits only Ny line-clock edges for a scan of
            # Ny*S line periods (ROADMAP M9, "linestep line_clock count"),
            # so there is no marker pattern that can be driven for S > 1:
            # the image would be the first Ny periods and then nothing.
            # Refused until the designer's clock count is fixed.
            raise RuntimeError(
                f'{self.name}: the Time Tagger cannot image a scan with '
                f'{S} linesteps: the line clock carries one edge per line, '
                f'not per linestep (ROADMAP M9). Set n_linesteps to 1 for FLIM.'
            )

        self._newFrameReady = False
        self._rawReady = False
        self._rawDelivered = True
        self._image_raw = None
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

        tt = self._timeTagger
        direction = tt.tcspcDirection
        period_ps = 1e6 / max(1e-9, float(self._laser_rep_rate_mhz))
        window_ps = self._n_bins * self._binwidth_ps
        if direction == 'reverse' and window_ps < period_ps:
            # Reverse mode (the card's conditional filter: start = photon,
            # click = sync) measures t' = T_rep - t, so the earliest photons
            # sit at the END of the window. A window shorter than the period
            # cuts the peak off, not the tail -- a hard refusal, not a warning.
            raise RuntimeError(
                f'TCSPC window {window_ps / 1000:.2f} ns ({self._n_bins} bins x '
                f'{self._binwidth_ps} ps) is shorter than the {period_ps / 1000:.2f} '
                f'ns laser period at {self._laser_rep_rate_mhz:g} MHz. In reverse '
                f'mode (filterSyncByPhotons) the window must span at least one '
                f'period: declare n_bins >= '
                f'{histogram_bins_for_period(self._binwidth_ps, self._laser_rep_rate_mhz)} '
                f'or leave it undeclared.'
            )

        self._scan = dict(
            Nx=Nx, Ny=Ny,
            n_pixels_total=Nx * Ny,
            pixel_period_ps=pixel_period_ps,
            # end[i] = begin[i] + pixel_period - 1 ps. If end and next begin
            # share a timestamp, TimeTagger's edge ordering can drop or
            # reorder the end edge — Flim's pixel index then stalls mid-line.
            pixel_width_ps=pixel_period_ps - 1,
            dwell_s=pixel_period_s,
            direction=direction,
            period_ps=period_ps,
            scan_info=dict(scanInfoDict),
        )
        self._shape = (Ny, Nx)

        try:
            tt.ensureConnected()
        except TimeTaggerError as error:
            with self._flim_lock:
                self._flim = None
            raise RuntimeError('Time Tagger is not available') from error

        # Conditioning (trigger levels, dead times, delays, the filter) is
        # the card manager's and was applied at connect; from here until the
        # final frame has landed the card is held so nothing changes it.
        try:
            # Refused while a calibration owns the card (Busy) and on a
            # card whose conditioning failed (the filter not set, say).
            tt.beginScanHold(self.name)
        except TimeTaggerError as error:
            with self._flim_lock:
                self._flim = None
            raise RuntimeError(str(error)) from error
        self._cardHeldGeneration = generation
        # Overflows are counted against this baseline: any frame of this scan
        # read after the total has moved is missing tags.
        self._scan['overflow_baseline'] = tt.overflows()
        try:
            # t0_ps is absolute: the IRF peak's position measured with the
            # card's configured conditioning (tutorial 06), so it is added
            # to the configured photon delay, never to whatever a previous
            # scan left on the input; _releaseCard puts it back.
            base_delay = tt.hardwareDelayPs(self._click_role)
            if direction == 'forward':
                # Shift click-channel timestamps so the IRF peak lands at
                # t=0. A negative delay moves photon timestamps earlier by
                # t0_ps, placing the IRF peak at histogram bin 0.
                tt.tagger.setInputDelay(self._click_ch, base_delay - self._t0_ps)
                self._scanAppliedClickDelay = self._t0_ps != 0
                flim_start, flim_click = self._start_ch, self._click_ch
            else:
                # Reverse: the photon starts the histogram and the sync stops
                # it. A delay on the photon channel would drop the earliest
                # photons here (they would land after their chosen sync), so
                # t0 is applied as a circular roll in the worker instead.
                tt.tagger.setInputDelay(self._click_ch, base_delay)
                flim_start, flim_click = self._click_ch, self._start_ch

            self._create_virtual_pixel_pulses()
            flim_kwargs = {}
            if self._frame_ch is not None:
                # The card re-syncs its pixel index on every frame edge: a
                # lost line marker costs one frame, not the rest of the scan.
                flim_kwargs['frame_begin_channel'] = int(self._frame_ch)
            with self._flim_lock:
                self._flim = tt.api.Flim(
                    tt.tagger,
                    start_channel=flim_start,
                    click_channel=flim_click,
                    pixel_begin_channel=self._ev_pix_begin.getChannel(),
                    pixel_end_channel=self._ev_pix_end.getChannel(),
                    n_pixels=int(self._scan['n_pixels_total']),
                    n_bins=self._n_bins,
                    binwidth=self._binwidth_ps,
                    **flim_kwargs,
                )
                # The frame count at arming: the final frame is the first
                # one the card closes after this, whichever side of
                # scan-done that happens on.
                getter = getattr(self._flim, 'getFramesAcquired', None)
                self._scan['frames_baseline'] = (
                    int(getter()) if getter is not None else None
                )
        except Exception as error:
            with self._flim_lock:
                self._flim = None
            self._ev_pix_begin = None
            self._ev_pix_end = None
            self._releaseCard()
            raise RuntimeError('TimeTagger FLIM setup failed') from error

        tot_scan_time_s = float(scanInfoDict.get('tot_scan_time_s', 0.0))
        ideal_scan_time_s = Nx * Ny * pixel_period_s
        overhead_pct = (
            (tot_scan_time_s - ideal_scan_time_s) / ideal_scan_time_s * 100
            if ideal_scan_time_s > 0 and tot_scan_time_s > 0 else 0.0
        )

        self._logger.info(
            f'TimeTagger prepared: click={self._click_ch}@{self._click_trigger}V, '
            f'start={self._start_ch}@{self._start_trigger}V, '
            f'line={self._line_ch}@{self._line_trigger}V, {direction} TCSPC, '
            f'n_bins={self._n_bins}, '
            f'binwidth={self._binwidth_ps}ps, fit={self._fit_method}, '
            f'Nx={Nx}, Ny={Ny}, pixel_period={pixel_period_ps}ps, '
            f'scan_time={tot_scan_time_s:.3f}s '
            f'(ideal={ideal_scan_time_s:.3f}s, +{overhead_pct:.1f}% settling/flyback)'
        )
        # A generation is startable only after validation, hardware setup and
        # FLIM allocation all succeeded.
        self._preparedScanGeneration = generation

    def _onScanBuilt(self, scanInfoDict, signalDict, _devices):
        try:
            self.initiateScan(scanInfoDict, signalDict)
        except Exception as error:
            self._logger.exception('TimeTagger scan preparation failed')
            try:
                self._teardownScanThread()
            except Exception:
                self._logger.exception(
                    'TimeTagger partial preparation cleanup failed'
                )
            self._preparedScanGeneration = None
            self._activeScanGeneration = None
            with self._flim_lock:
                self._flim = None
            self._releaseCard()
            self._reportScanBuildFailure('prepare', error)

    def _reportScanBuildFailure(self, stage, error):
        try:
            self._nidaqManager.reportScanBuildFailure(
                f'{self.name}.{stage}', error
            )
        except Exception:
            self._logger.exception(
                'Failed to report TimeTagger scan %s failure', stage
            )

    def _create_virtual_pixel_pulses(self):
        Nx = int(self._scan['Nx'])
        period_ps = int(self._scan['pixel_period_ps'])
        width_ps = int(self._scan['pixel_width_ps'])

        # The pattern starts a little after the line edge (the block's
        # pixelPatternOffsetPs, plus a positive line delay): the designer
        # raises the frame clock on the same sample as the first line edge,
        # and the frame marker must lead pixel 0 whatever the cable skew.
        offset_ps = np.int64(self._timeTagger.patternOffsetPs(self._line_role))
        self._scan['pattern_offset_ps'] = int(offset_ps)
        begin_pattern = offset_ps + np.arange(Nx, dtype=np.int64) * np.int64(period_ps)
        end_pattern = begin_pattern + np.int64(width_ps)

        # Release previous generators before creating new ones
        self._ev_pix_begin = None
        self._ev_pix_end = None

        # No output_channel argument — the API auto-assigns virtual channel IDs
        # that are guaranteed not to collide with any physical channel.
        tt = self._timeTagger
        self._ev_pix_begin = tt.api.EventGenerator(
            tt.tagger, int(self._line_ch), begin_pattern
        )
        self._ev_pix_end = tt.api.EventGenerator(
            tt.tagger, int(self._line_ch), end_pattern
        )

    def startScan(self):
        try:
            self._startScan()
        except Exception as error:
            self._logger.exception('TimeTagger scan start failed')
            try:
                self._teardownScanThread()
            except Exception:
                self._logger.exception(
                    'TimeTagger partial start cleanup failed'
                )
            self._preparedScanGeneration = None
            self._activeScanGeneration = None
            self._reportScanBuildFailure('start', error)

    def _startScan(self):
        if not self._scanParticipating or not self._enabled:
            return

        generation = self._preparedScanGeneration
        if generation is None:
            return
        with self._flim_lock:
            flim = self._flim
        if flim is None:
            return

        # Tear down any previous scan thread only after confirming that the new
        # generation is fully prepared.
        self._teardownScanThread()

        self.acquisition = True
        self._activeScanGeneration = generation
        self._scanWorker = _TTFlimWorker(self, generation)
        self._scanThread = Thread()
        self._scanWorker.moveToThread(self._scanThread)
        self._scanThread.started.connect(self._scanWorker.run)
        # Both signals carry the generation to make delayed queued delivery
        # harmless after a timeout/re-arm.
        self._scanWorker.sigFrameReady.connect(self._on_frame_ready)
        self._scanWorker.sigTerminated.connect(self._onScanWorkerFinished)
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
        if not self._scanParticipating:
            return
        self.acquisition = False
        if self._scanWorker is not None:
            try:
                self._scanWorker.signal_done()
            except Exception:
                generation = getattr(
                    self._scanWorker, 'scanGeneration',
                    self._activeScanGeneration,
                )
                self._logger.exception(
                    'Failed to wake TimeTagger worker at scan end'
                )
                self._onScanWorkerFinished(generation)

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
        worker = self._scanWorker
        if mode != 'graceful':
            generation = getattr(
                worker, 'scanGeneration', self._activeScanGeneration
            )
            # Abort is iteration-scoped.  It must stop this worker even when
            # another purpose (for example WORKFLOW) keeps the detector's
            # aggregate acquisition lease above zero, otherwise the abandoned
            # worker can survive into the next scan.
            try:
                self._teardownScanThread()
            except Exception:
                self._logger.exception(
                    'Failed to tear down TimeTagger worker on scan abort'
                )
                # Acknowledging while the worker may still be inside the
                # backend would let the coordinator release its lease and arm
                # another iteration over unknown hardware.  Withhold the ack;
                # the coordinator's own bounded finalizer will fail closed.
                return
            # An aborted scan has no finished image; whatever the latch holds
            # is a partial one and must not reach a recording.
            self._rawReady = False
            self._image_raw = None
            if generation is not None:
                with self._finishAckLock:
                    self._finishedScanGenerations.add(generation)
                    callbacks = self._finishAcks.pop(generation, ())
                for callback in callbacks:
                    try:
                        callback()
                    except Exception:
                        self._logger.exception(
                            'TimeTagger abort acknowledgement failed'
                        )
            if self._activeScanGeneration == generation:
                self._activeScanGeneration = None
            if self._preparedScanGeneration == generation:
                self._preparedScanGeneration = None
            with self._flim_lock:
                self._flim = None
            self._ev_pix_begin = None
            self._ev_pix_end = None
            self._releaseCard()
            self.acquisition = False
            acknowledge()
            return

        if worker is None:
            acknowledge()
            return

        with self._flim_lock:
            haveFlim = self._flim is not None
        if not haveFlim:
            acknowledge()  # mock mode / setup failed: no final frame is coming
            return

        generation = getattr(
            worker, 'scanGeneration', self._activeScanGeneration
        )
        if generation is None:
            acknowledge()
            return

        with self._finishAckLock:
            if (generation in self._completedFinalFrameGenerations
                    or generation in self._finishedScanGenerations):
                acknowledgeImmediately = True
            else:
                self._finishAcks.setdefault(generation, []).append(acknowledge)
                acknowledgeImmediately = False

        # sigScanDone can wake the worker before the coordinator calls
        # finishScan.  If that final frame already landed, do not leave a
        # callback waiting for a frame that will never be emitted again.
        if acknowledgeImmediately:
            acknowledge()
            return

        self.acquisition = False
        try:
            worker.signal_done()
        except Exception:
            # Worker already gone: remove this exact callback before falling
            # back to immediate completion.
            self.cancelFinishScan(acknowledge)
            acknowledge()

    def cancelFinishScan(self, acknowledge):
        """Cancel one pending finish callback by object identity.

        The coordinator invokes this before releasing a timed-out/aborted
        iteration.  Identity matching is important: closures for different
        scan tokens may compare similarly but must never cancel each other.
        Returns whether the callback was still pending.
        """
        removed = False
        with self._finishAckLock:
            for generation in tuple(self._finishAcks):
                callbacks = self._finishAcks[generation]
                remaining = []
                for callback in callbacks:
                    if callback is acknowledge:
                        removed = True
                    else:
                        remaining.append(callback)
                if remaining:
                    self._finishAcks[generation] = remaining
                else:
                    self._finishAcks.pop(generation, None)
        return removed

    def _fireFinalFrameAck(self, generation):
        """Release only barriers waiting for this worker generation."""
        if generation is None:
            return
        with self._finishAckLock:
            callbacks = self._finishAcks.pop(generation, ())
            self._completedFinalFrameGenerations.add(generation)
            self._finishedScanGenerations.add(generation)
        # The final frame has landed: the card may be reconditioned again
        # -- unless a newer scan already holds it.
        self._releaseCard(generation)
        for acknowledge in callbacks:
            try:
                acknowledge()
            except Exception:
                self._logger.exception(
                    'TimeTagger final-frame acknowledgement failed'
                )

    def _onScanWorkerFinished(self, generation):
        """Terminal fallback when a worker exits without a valid final frame.

        The worker emits this signal after its last sigFrameReady emission.
        Both are queued to this manager, preserving order: a valid final frame
        therefore commits and acknowledges first, while no-data and crash exits
        still release the coordinator barrier.
        """
        if generation is None:
            return
        with self._finishAckLock:
            callbacks = self._finishAcks.pop(generation, ())
            self._finishedScanGenerations.add(generation)
        self._releaseCard(generation)
        for acknowledge in callbacks:
            try:
                acknowledge()
            except Exception:
                self._logger.exception(
                    'TimeTagger terminal acknowledgement failed'
                )

    def _on_frame_ready(self, intensity_img, lifetime_img, is_final: bool,
                        decay_counts, t_axis_ns, global_tau_ns: float,
                        scanGeneration=None, live=None):
        if scanGeneration is None:
            scanGeneration = self._activeScanGeneration
        if (scanGeneration is not None
                and scanGeneration != self._activeScanGeneration):
            # A timed-out worker may still have a queued signal.  Never let its
            # old image overwrite a newer iteration, and never let it satisfy
            # a newer generation's acknowledgement.
            if is_final:
                self._fireFinalFrameAck(scanGeneration)
            return

        self._image_intensity[0] = intensity_img
        lifetime_ns = (lifetime_img * 1e9).astype(np.float32)
        self._last_decay_counts = decay_counts
        self._last_t_axis_ns = t_axis_ns
        self._last_global_tau_ns = float(global_tau_ns)
        if is_final:
            self._frame_index += 1
        if live is not None:
            live.frame_index = self._frame_index + (0 if is_final else 1)
            # Queued from the worker and re-emitted here, on the manager's
            # thread, so a consumer never sees a frame out of order with the
            # display frame it belongs to.
            self.sigTimeResolvedProducts.emit(live)

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

        if is_final:
            # This scan's own finished image, not the accumulated overlay the
            # display may be showing: a recording saves the measurement.
            self._image_raw = lifetime_ns[np.newaxis].astype(np.float32, copy=True)
            self._rawReady = True
            self._rawDelivered = False

        self._newFrameReady = True
        self.updateLatestFrame(True)
        self.sigNewFrame.emit()

        if is_final:
            # The scan's last frame is committed and published — the
            # coordinator may now release the lease and let the next
            # iteration arm.
            self._fireFinalFrameAck(scanGeneration)

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

    def configureTimeResolvedProducts(
        self,
        config: TimeResolvedScanConfig,
        owner: str | None = None,
    ) -> str | None:
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
        # The check and the claim share one lock block: two runs racing
        # for the session must not both pass the check and both claim it.
        with self._tr_lock:
            current = self._tr_owner
            if self._tr_enabled and current is not None and current != owner:
                raise RuntimeError(
                    f"{self.name}: time-resolved products are owned by another "
                    f"run ({current}); wait for it to finish or clear its session"
                )
            self._fit_method = str(fit.method)
            self._min_counts_per_pixel = int(fit.min_counts_per_pixel)
            if fit.laser_rep_rate_mhz is not None:
                self._laser_rep_rate_mhz = float(fit.laser_rep_rate_mhz)
            self.parameters["fit_method"].value = self._fit_method
            self.parameters["min_counts_per_pixel"].value = self._min_counts_per_pixel
            self.parameters["laser_rep_rate_mhz"].value = self._laser_rep_rate_mhz
            self._tr_config = config
            self._tr_enabled = True
            self._tr_owner = owner
            self._tr_last_products = None
            self._tr_final_event.clear()
        return owner

    def timeResolvedSessionOwner(self) -> str | None:
        with self._tr_lock:
            return self._tr_owner if self._tr_enabled else None

    def waitForFinalTimeResolvedProducts(
        self,
        timeout_s: float | None = None,
        owner: str | None = None,
    ) -> TimeResolvedScanProducts:
        with self._tr_lock:
            current = self._tr_owner
        if owner is not None and current is not None and current != owner:
            raise RuntimeError(
                f"{self.name}: time-resolved products are owned by another "
                f"run ({current})"
            )
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

    def clearTimeResolvedProducts(self, owner: str | None = None) -> None:
        with self._tr_lock:
            current = self._tr_owner
            if self._tr_enabled and current is not None and current != owner:
                # Another run's session: its own cleanup will clear it. A
                # caller without a token never wipes an owned session.
                self._logger.debug(
                    f"clearTimeResolvedProducts({owner!r}) ignored: the session "
                    f"is owned by {current!r}"
                )
                return
            self._tr_config = TimeResolvedScanConfig()
            self._tr_enabled = False
            self._tr_owner = None
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
        extra_metadata: dict | None = None,
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
            gate_images = compute_gate_images(
                cube_counts, t_axis_ns, config.gates, peak_time_ns=peak_time_ns
            )
            metadata = {
                "detector_name": self.name,
                "backend": "SwabianTimeTaggerManager",
                **(extra_metadata or {}),
                "time_tagger": self._timeTagger.metadata(),
                "click_role": self._click_role,
                "start_role": self._start_role,
                "line_role": self._line_role,
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
    def rawFrameIsDeferred(self):
        return True

    def drainChunk(self):
        """Display every tick; the finished lifetime image once, when whole.

        The worker publishes a preview every ``LIVE_PREVIEW_S`` with
        ``is_final`` False and ``getChunk`` hands each one to the screen. A
        recording reading the raw stream used to receive those previews too,
        and since a scan-driven detector is planned for exactly one frame the
        recording was satisfied by the first preview -- one second into a
        scan of a minute -- and closed as *complete* with the rest of the
        lines still zero, with no log line anywhere. The raw half now stays
        empty until the final frame has landed, then yields it exactly once,
        the shape APD and PMT already implement.
        """
        display = self.getChunk()
        raw = _EMPTY_CHUNK
        if (self._rawReady and not self._rawDelivered
                and self._image_raw is not None):
            self._rawDelivered = True
            self._rawReady = False
            raw = self._image_raw
            self._image_raw = None
        return ChunkPayload(display=display, raw=raw)

    def _warnIfWindowTruncatesTheDecay(self):
        window_ps = self._n_bins * self._binwidth_ps
        period_ps = 1e6 / max(1e-9, self._laser_rep_rate_mhz)
        if window_ps < MIN_WINDOW_FRACTION_OF_PERIOD * period_ps:
            self._logger.warning(
                f'TCSPC window {window_ps / 1000:.2f} ns ({self._n_bins} bins x '
                f'{self._binwidth_ps} ps) covers {100 * window_ps / period_ps:.0f}% '
                f'of the {period_ps / 1000:.2f} ns laser period at '
                f'{self._laser_rep_rate_mhz:g} MHz. A decay cut off that early '
                f'reads as a shorter lifetime in the moment and phasor fits; '
                f'declare n_bins >= '
                f'{histogram_bins_for_period(self._binwidth_ps, self._laser_rep_rate_mhz)} '
                f'or leave it undeclared to span the period.'
            )

    @property
    def pixelSizeUm(self):
        return scanPixelSizesToZYX(self.__pixel_sizes)

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
        self._scanParticipating = False
        try:
            self._teardownScanThread()
        except Exception as e:
            # Detector stop contract: teardown failure must reach the
            # DetectorsManager, which quarantines this detector as FAULTED.
            self._logger.warning(f'Failed to stop scan thread: {e}')
            raise
        else:
            self._preparedScanGeneration = None
            self._activeScanGeneration = None
            self._releaseCard()
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
            # a 1-axis scan is a single line: no second dimension to read
            Ny = int(scan_dims[-2]) if len(scan_dims) >= 2 else 1
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
# Background worker                                                            #
# --------------------------------------------------------------------------- #

class _TTFlimWorker(Worker):
    # intensity, lifetime, is_final, decay_counts, t_axis_ns, global_tau_ns,
    # scan generation, LiveProducts
    sigFrameReady = Signal(object, object, bool, object, object, float, int,
                           object)
    sigTerminated = Signal(int)
    sigFinished = Signal()

    # Live-preview interval: read Flim data once per second during a running
    # scan.  The worker also wakes immediately when signal_done() is called
    # (sigScanDone path), so the final frame is never delayed.
    LIVE_PREVIEW_S = 1.0
    STALL_MAX = 10  # consecutive live-preview ticks with no data → ~10 s
    #: Tick while waiting for the card to close the frame, and without live
    #: fits (intensity previews only).
    POLL_S = 0.25
    #: After scan-done, how long to wait for the card to close the frame
    #: before the current frame is taken as the final one.
    FINAL_FRAME_GRACE_S = 2.0

    def __init__(self, m: SwabianTimeTaggerManager, scanGeneration):
        super().__init__()
        self._logger = initLogger(self, tryInheritParent=True)
        self._m = m
        self.scanGeneration = scanGeneration
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
            scan = self._m._scan
            self._direction = str(scan.get('direction', 'forward'))
            period_ps = float(scan.get('period_ps', 0.0)) or (
                1e6 / max(1e-9, float(self._m._laser_rep_rate_mhz))
            )
            self._period_ns = period_ps / 1000.0
            self._dwell_s = float(scan.get('dwell_s', 0.0))
            self._binwidth_ps = binwidth_ps
            # Reverse mode applies t0 as a circular roll (see initiateScan).
            self._t0_roll_bins = (
                int(round(self._m._t0_ps / binwidth_ps)) % n_bins
                if self._direction == 'reverse' and n_bins else 0
            )
            self._background_per_bin = background_per_bin(
                self._m._background_rate_hz, self._dwell_s, binwidth_ps,
                period_ps,
            )
            self._pileup_warned = False
            self._overflow_baseline = int(scan.get('overflow_baseline', 0))
            self._overflows_warned = False

            # The histogram's own axis (bin centres), then the forward-time
            # axis the fits and the user see: identical in forward mode,
            # mirrored on the laser period in reverse mode.
            raw_axis_ns = (np.arange(n_bins, dtype=np.float64) + 0.5) * binwidth_ps * 1e-3
            _, oriented_axis_ns = orient_cube(
                np.zeros((1, 1, n_bins), dtype=np.float32), raw_axis_ns,
                self._direction, period_ns=self._period_ns,
            )
            t_axis = (oriented_axis_ns * 1e-9).astype(np.float32)
            t_axis_f64 = oriented_axis_ns.astype(np.float64)[None, None, :] * 1e-9
            # Phasor needs the laser repetition period, NOT the histogram window.
            # The Flim API doesn't expose the rep rate, so take it from the
            # user-supplied parameter. Default 80 MHz is the most common Ti:Sa rate.
            rep_rate_hz = max(1.0, float(self._m._laser_rep_rate_mhz)) * 1e6
            T_rep_s = 1.0 / rep_rate_hz
            omega = 2.0 * np.pi / T_rep_s
            t_s = oriented_axis_ns.astype(np.float64) * 1e-9
            cos_table = np.cos(omega * t_s)
            sin_table = np.sin(omega * t_s)

            stall_count = 0
            live_fit_s = float(getattr(self._m, '_live_fit_period_s', self.LIVE_PREVIEW_S))
            tick = live_fit_s if live_fit_s > 0 else self.POLL_S
            # The final frame is detected by count, not by signal order: the
            # card closes a frame after n_pixels pixel ends, which can happen
            # before or after sigScanDone reaches us.
            frames_baseline = scan.get('frames_baseline')
            if frames_baseline is None:
                frames_baseline = self._frames_acquired()
            self._frame_closed_by_card = False
            grace_deadline = None
            last_preview = time.monotonic()

            while self._running:
                scan_done = self._done_event.wait(timeout=tick)
                if not self._running:
                    break

                frames = self._frames_acquired()
                if (frames is not None and frames_baseline is not None
                        and frames - frames_baseline >= 1):
                    cube = self._read_ready_frame(expected_shape, Nx, Ny, n_bins)
                    if cube is not None:
                        self._frame_closed_by_card = True
                        self._emit_frame(cube, t_axis, t_axis_f64, fit_method,
                                         omega, cos_table, sin_table, min_counts,
                                         rep_rate_hz, is_final=True)
                        break

                if scan_done:
                    # The scan is over but the card has not closed the frame
                    # yet: give it a moment, then take what it has.
                    now = time.monotonic()
                    if grace_deadline is None:
                        grace_deadline = now + self.FINAL_FRAME_GRACE_S
                        tick = self.POLL_S
                    if now < grace_deadline:
                        continue
                    cube = self._poll_frame(expected_shape, Nx, Ny, n_bins)
                    if cube is None:
                        break  # scan finished but Flim had no data yet
                    if frames_baseline is not None:
                        self._logger.warning(
                            'The Time Tagger did not close the last frame '
                            f'within {self.FINAL_FRAME_GRACE_S:g} s of scan-done: '
                            'fewer pixel markers arrived than expected (a '
                            'missing line edge, or a line delay past the end '
                            'of the scan). Taking the frame as it is.'
                        )
                    self._emit_frame(cube, t_axis, t_axis_f64, fit_method,
                                     omega, cos_table, sin_table, min_counts,
                                     rep_rate_hz, is_final=True)
                    break

                # A preview: the full fit at the live-fit cadence, or the
                # intensity alone when live fits are off.
                if live_fit_s > 0:
                    cube = self._poll_frame(expected_shape, Nx, Ny, n_bins)
                    if cube is None:
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
                                     rep_rate_hz, is_final=False)
                elif time.monotonic() - last_preview >= self.LIVE_PREVIEW_S:
                    last_preview = time.monotonic()
                    self._emit_intensity_preview(Nx, Ny, t_axis)

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
            # Emitted after any final-frame signal.  The manager uses this as a
            # no-data/crash fallback and generation-checks delayed delivery.
            self.sigTerminated.emit(self.scanGeneration)
            self.sigFinished.emit()

    def _frames_acquired(self):
        """How many frames the card has closed, or None when unknown."""
        with self._m._flim_lock:
            flim = self._m._flim
        getter = getattr(flim, 'getFramesAcquired', None) if flim is not None else None
        if getter is None:
            return None
        try:
            return int(getter())
        except Exception:
            self._logger.exception('getFramesAcquired() raised.')
            return None

    def _read_ready_frame(self, expected_shape, Nx, Ny, n_bins):
        """The frame the card closed: getReadyFrameEx (with its validity),
        else getReadyFrame, else the current frame."""
        with self._m._flim_lock:
            flim = self._m._flim
        if flim is None:
            return None
        try:
            ex = getattr(flim, 'getReadyFrameEx', None)
            if ex is not None:
                info = ex()
                if info is None or (hasattr(info, 'isValid') and not info.isValid()):
                    return None
                h = info.getHistograms() if hasattr(info, 'getHistograms') else info
            elif hasattr(flim, 'getReadyFrame'):
                h = flim.getReadyFrame()
            else:
                h = flim.getCurrentFrame()
        except Exception:
            self._logger.exception('Reading the ready frame raised.')
            return None
        return self._as_cube(h, expected_shape, Nx, Ny, n_bins)

    def _emit_intensity_preview(self, Nx, Ny, t_axis):
        """A cheap preview while live fits are off: counts per pixel only."""
        with self._m._flim_lock:
            flim = self._m._flim
        getter = getattr(flim, 'getCurrentFrameIntensity', None) if flim is not None else None
        if getter is None:
            return
        try:
            intensity = np.asarray(getter(), dtype=np.float32).reshape(Ny, Nx)
        except Exception:
            self._logger.exception('getCurrentFrameIntensity() raised.')
            return
        n_bins = int(t_axis.size)
        live = LiveProducts(
            intensity=intensity,
            lifetime_ns=None,
            decay_counts=np.zeros(n_bins, dtype=np.float32),
            t_axis_ns=(t_axis * 1e9).astype(np.float32),
            gate_images={},
            global_tau_ns=0.0,
            peak_time_ns=0.0,
            background_per_bin=float(self._background_per_bin),
            pileup_max=float(pileup_fraction(intensity, max(1.0, float(self._m._laser_rep_rate_mhz)) * 1e6, self._dwell_s).max()) if intensity.size else 0.0,
            tcspc_direction=self._direction,
            frame_index=0,
            is_final=False,
            metadata={"preview": "intensity"},
        )
        self._m.sigTimeResolvedProducts.emit(live)

    def _as_cube(self, h, expected_shape, Nx, Ny, n_bins):
        if h is None:
            return None
        arr = np.asarray(h)
        if arr.ndim != 2 or arr.shape != expected_shape:
            return None
        if arr.dtype != np.float32:
            arr = arr.astype(np.float32, copy=False)
        if not arr.flags['C_CONTIGUOUS']:
            arr = np.ascontiguousarray(arr)
        return arr.reshape(Ny, Nx, n_bins)

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
        # Into forward time: reverse mode measured T_rep - t, so the bin
        # order flips (the axis was mirrored once in run()); then t0 as a
        # roll where a channel delay is not allowed.
        if self._direction == 'reverse':
            cube = cube[..., ::-1]
            if self._t0_roll_bins:
                cube = roll_to_peak(cube, peak_bin=self._t0_roll_bins,
                                    target_bin=0)
        raw_counts = cube

        # Pre-pass: intensity image + aggregated decay over valid pixels.
        # The aggregated decay drives IRF peak detection — every fitter is
        # then shifted by t_peak so the reported τ is referenced from the
        # rising edge of the laser pulse, not from t=0 of the histogram.
        intensity = raw_counts.sum(axis=2).astype(np.float32)
        valid_mask = intensity >= min_counts

        # Dark counts and afterpulsing are flat over the period; they pull
        # the moment towards T_rep / 2 and the phasor towards the origin, so
        # they come off every histogram before anything is fitted or gated.
        cube = subtract_background(raw_counts, self._background_per_bin)

        if valid_mask.any():
            decay_counts = cube[valid_mask].sum(axis=0).astype(np.float32)
        else:
            decay_counts = cube.sum(axis=(0, 1)).astype(np.float32)

        peak_bin = int(np.argmax(decay_counts)) if decay_counts.sum() > 0 else 0
        t_peak = float(t_axis[peak_bin])

        pileup = pileup_fraction(intensity, rep_rate_hz, self._dwell_s)
        pileup_max = float(pileup.max()) if pileup.size else 0.0
        if pileup_max > PILEUP_WARN and not self._pileup_warned:
            self._pileup_warned = True
            self._logger.warning(
                f'Pile-up: the brightest pixel detects {100 * pileup_max:.1f} % '
                f'of the laser pulses (above {100 * PILEUP_WARN:.0f} %). The '
                f'histogram favours the {"later" if self._direction == "reverse" else "earlier"} '
                f'photon of a pair there; lower the excitation power.'
            )

        if fit_method == 'phasor':
            lifetime = fit_phasor(
                cube, intensity, omega, cos_table, sin_table, t_peak)
        elif fit_method == 'exp1':
            lifetime = fit_exp1(cube, t_axis_f64, peak_bin)
        else:
            _, lifetime = fit_moment(cube, t_axis, peak_bin)

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
        peak_time_ns = t_peak * 1e9
        overflows = max(0, self._m._timeTagger.overflows() - self._overflow_baseline)
        if overflows and not self._overflows_warned:
            self._overflows_warned = True
            self._logger.error(
                f'The Time Tagger reported {overflows} USB overflow(s) during '
                f'this scan: the frame is missing tags and its lifetimes are '
                f'not trustworthy. Lower the tag rate (conditional filter, '
                f'dead time) or the excitation power.'
            )
        frame_metadata = {
            "overflows": int(overflows),
            "frame_valid": overflows == 0,
            "frame_closed_by_card": bool(getattr(self, '_frame_closed_by_card', False)) if is_final else None,
            "pattern_offset_ps": int(self._m._scan.get('pattern_offset_ps', 0)),
            "tcspc_direction": self._direction,
            "laser_period_ns": float(self._period_ns),
            "dwell_s": float(self._dwell_s),
            "background_rate_hz": float(self._m._background_rate_hz),
            "background_per_bin": float(self._background_per_bin),
            "pileup_max": pileup_max,
            "t0_roll_bins": int(self._t0_roll_bins),
        }

        self._m._store_time_resolved_products(
            cube_counts=cube,
            intensity=intensity,
            lifetime_s=lifetime,
            decay_counts=decay_counts,
            t_axis_ns=t_axis_ns,
            global_tau_ns=float(global_tau_ns),
            peak_bin=peak_bin,
            peak_time_ns=peak_time_ns,
            is_final=is_final,
            extra_metadata=frame_metadata,
        )

        tr_config = self._m._tr_config if self._m._tr_enabled else None
        live = LiveProducts(
            intensity=intensity.astype(np.float32),
            lifetime_ns=(lifetime * 1e9).astype(np.float32),
            decay_counts=decay_counts,
            t_axis_ns=t_axis_ns,
            gate_images=(
                compute_gate_images(cube, t_axis_ns, tr_config.gates,
                                    peak_time_ns=peak_time_ns)
                if tr_config is not None and tr_config.gates else {}
            ),
            global_tau_ns=float(global_tau_ns),
            peak_time_ns=float(peak_time_ns),
            background_per_bin=float(self._background_per_bin),
            pileup_max=pileup_max,
            tcspc_direction=self._direction,
            frame_index=0,
            is_final=bool(is_final),
            metadata=frame_metadata,
        )

        self.sigFrameReady.emit(
            intensity.astype(np.float32),
            lifetime.astype(np.float32),
            is_final,
            decay_counts,
            t_axis_ns,
            float(global_tau_ns),
            self.scanGeneration,
            live,
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
            tau_s = fit_phasor(
                cube1, intensity1, omega, cos_t, sin_t, t_peak)
        elif fit_method == 'exp1':
            tau_s = fit_exp1(cube1, t_axis_f64, peak_bin)
        else:
            _, tau_s = fit_moment(cube1, t_axis, peak_bin)
        tau_ns = float(tau_s[0, 0]) * 1e9
        if not np.isfinite(tau_ns) or tau_ns <= 0:
            return 0.0
        return tau_ns
