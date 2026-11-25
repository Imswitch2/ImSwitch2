import dataclasses
import json
import math
import numpy as np

from ._visit_order import require_forward
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
from ._frame_publication import (
    begin_frame_storage,
    commit_frame_storage,
    display_frame,
    read_with_frame_geometry,
    remember_scan_geometry,
)
from .DetectorManager import (
    ChunkPayload, DetectorManager, DetectorNumberParameter, DetectorListParameter,
    scanPixelSizesToZYX, _EMPTY_CHUNK)

_SCAN_THREAD_JOIN_TIMEOUT_MS = 2000
#: The GUI-thread stop paths (scan start, stop, abort) give a worker this
#: long to leave, then retire it instead of joining (see _teardownScanThread).
_RETIRE_JOIN_MS = 20
#: A retired worker still running after this long is wedged in the vendor
#: library: the next teardown raises, and the detector is quarantined.
_RETIRED_WORKER_STUCK_S = 30.0
#: Cube values per second a fit is assumed to process before the worker has
#: measured one (moment and phasor; exp1 takes about four times as long).
_ASSUMED_FIT_VALUES_PER_S = 20e6
#: The longest end-of-scan deadline the detector asks of the coordinator.
_MAX_FINISH_TIMEOUT_S = 300.0


def _threadRunning(thread) -> bool:
    """Whether a (Q)Thread is still running; a deleted one is not."""
    try:
        return bool(thread.isRunning())
    except RuntimeError:
        return False

#: Detector properties that described the card before the setup-level
#: ``timeTagger`` block existed. Retired: the block describes the card, and
#: these are read only to print the block that replaces them.
_RETIRED_CARD_PROPS = frozenset({
    'click_channel', 'start_channel', 'line_channel',
    'click_trigger', 'start_trigger', 'line_trigger', 'trigger_levels',
})

#: Retired property prefix -> the block's channel and trigger-level fields.
_RETIRED_CHANNELS = (
    ('click', 'photonsChannel', 'photonsTriggerV'),
    ('start', 'laserSyncChannel', 'laserSyncTriggerV'),
    ('line', 'lineClockChannel', 'lineClockTriggerV'),
)

#: The role each of the detector's three role properties falls back to.
_DEFAULT_ROLES = {'click': 'photons', 'start': 'laser_sync', 'line': 'line_clock'}

#: Fits the worker knows (``timeresolved.types`` validates the same three).
_FIT_METHODS = ('moment', 'phasor', 'exp1')

_TRUE_WORDS = ('true', '1', 'yes', 'on')
_FALSE_WORDS = ('false', '0', 'no', 'off')


def _setupNumber(value, kind):
    """``value`` as a ``kind`` (``int`` or ``float``), or ``None`` if it is
    not one: blank, not a number, not finite, a JSON ``true``/``false``, or
    a fraction where an ``int`` is wanted. ``"32"`` and ``32.0`` read as 32."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if kind is int:
        return int(number) if number == int(number) else None
    return number


def _setupBool(value):
    """``value`` as a bool, or ``None`` if it is not one. ``"false"`` is
    False (``bool("false")`` is True)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
    return None


def _numberRule(kind, lowest=None, strict=False):
    """A parser for a ``kind`` at or above (``strict``: above) ``lowest``."""
    def parse(value):
        number = _setupNumber(value, kind)
        if number is None or lowest is None:
            return number
        return number if (number > lowest if strict else number >= lowest) else None
    return parse


def _choiceRule(options):
    def parse(value):
        word = value.strip() if isinstance(value, str) else None
        return word if word in options else None
    return parse


#: What each of the detector's own managerProperties must be: key ->
#: (parser returning the value or ``None``, what the warning asks for). A
#: value that does not parse is dropped before the properties are read, so
#: the read falls back to its default: a slip in the setup file -- a blank
#: field, a word where a number goes -- must not stop ImControl.
_ROLE_RULE = (_choiceRule(ROLES), f'one of {", ".join(ROLES)}')
_PROPERTY_RULES = {
    'enabled': (_setupBool, 'true or false'),
    'click_role': _ROLE_RULE,
    'start_role': _ROLE_RULE,
    'line_role': _ROLE_RULE,
    'frame_role': (_choiceRule(('auto', 'none') + ROLES),
                   f'auto, none or one of {", ".join(ROLES)}'),
    'fit_method': (_choiceRule(_FIT_METHODS), f'one of {", ".join(_FIT_METHODS)}'),
    'live_fit_period_s': (_numberRule(float, 0.0), 'a number of seconds, 0 or more'),
    'n_bins': (_numberRule(int, 0, strict=True), 'a whole number above 0'),
    'binwidth_ps': (_numberRule(int, 0, strict=True), 'a whole number above 0'),
    't0_ps': (_numberRule(int), 'a whole number of picoseconds'),
    'min_counts_per_pixel': (_numberRule(int, 0), 'a whole number, 0 or more'),
    'laser_rep_rate_mhz': (_numberRule(float, 0.0, strict=True), 'a number above 0'),
    'background_rate_hz': (_numberRule(float, 0.0), 'a number, 0 or more'),
}


def _blockFromRetiredProps(props):
    """The channel part of a ``timeTagger`` block, for the log line that asks
    for one: the block's defaults, overridden by whatever of a detector's
    retired card properties is readable (``trigger_levels`` included)."""
    defaults = {f.name: f.default for f in dataclasses.fields(TimeTaggerInfo)}
    block = {field: defaults[field]
             for _prefix, channelField, triggerField in _RETIRED_CHANNELS
             for field in (channelField, triggerField)}
    # Only the retired keys, iterated: a read by computed key would be a
    # property the config-editor schema has to offer.
    retired = {key: value for key, value in props.items()
               if key in _RETIRED_CARD_PROPS}
    levels = retired.get('trigger_levels')
    levels = levels if isinstance(levels, dict) else {}
    for prefix, channelField, triggerField in _RETIRED_CHANNELS:
        channel = _setupNumber(retired.get(f'{prefix}_channel'), int)
        if channel:
            block[channelField] = channel
        volts = _setupNumber(retired.get(f'{prefix}_trigger'), float)
        if volts is None:
            volts = _setupNumber(levels.get(str(block[channelField])), float)
        if volts is not None:
            block[triggerField] = volts
    return block


#: What a detector makes of its input's pulses (the ``measurement`` property).
MEASUREMENTS = ('flim', 'counting')


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
    One detector on a Swabian Time Tagger card: the pulses of one card input,
    imaged pixel by pixel during a scan.

    ``measurement`` says what it makes of them:

    * ``flim`` (default) -- TCSPC against a laser sync, a ``Flim`` cube per
      frame, the fitted fluorescence lifetime per pixel (the display), with
      gates, products and fits;
    * ``counting`` -- photon counts per pixel, no sync and no fit: a
      ``CountBetweenMarkers`` between the scan's pixel markers. The image is
      the counts.

    The card itself is the setup's shared ``TimeTaggerManager`` (the
    ``timeTagger`` block): it owns the connection, the inputs, the roles
    and their conditioning. This detector only says which inputs it uses --
    ``photon_input`` (or ``click_role``; default the ``photons`` role),
    ``start_role`` (``laser_sync``; FLIM only) and ``line_role``
    (``line_clock``) -- by input name or role. Several detectors on one card
    each name their own photon input and share the sync, the scan clocks,
    the pixel markers and one measurement lifecycle (the card manager's scan
    session). The resolved channels and the photon input's conditioning
    show as read-only parameters (and so reach every recording); the
    trigger-level parameters write through to the card between scans.

    A setup without a ``timeTagger`` block has no card: the detector still
    starts, on a stand-in (``TimeTaggerManager.notConfigured``) that refuses
    every scan, and logs the block to add -- built from the retired
    ``click_channel`` / ``start_channel`` / ``line_channel`` / ``*_trigger``
    / ``trigger_levels`` properties when the detector still has them. Those
    are not read otherwise.

    A value that is blank, unreadable or out of range is dropped with a
    warning and takes its default; a role the card does not have falls back
    to the default role. A setup-file slip never stops ImControl.

    Optional config properties:
      measurement ('flim' or 'counting'),
      photon_input (an input name or role; wins over click_role),
      click_role, start_role, line_role (input names or roles, see above),
      n_bins (default: one laser period), binwidth_ps (default 32),
      min_counts_per_pixel (default 20), fit_method (default 'moment'),
      laser_rep_rate_mhz (default 80.0; used by phasor fit to set ω),
      t0_ps (default 0), enabled (default True)

    Pixel markers are generated internally via EventGenerator using
    auto-assigned virtual channel IDs — no physical channels are consumed.
    """

    def __init__(self, detectorInfo, name, nidaqManager, timeTaggerManager=None,
                 **_lowLevelManagers):
        self._logger = initLogger(self, instanceName=name)
        self._instanceName = name

        self._detectorInfo = detectorInfo
        self._nidaqManager = nidaqManager
        # A copy without the values this detector cannot read (each named in
        # a warning), so every read below falls back to its default instead
        # of stopping ImControl over a blank field.
        props = dict(getattr(detectorInfo, 'managerProperties', {}) or {})
        self._dropUnreadableProperties(props)

        self._enabled = bool(props.get('enabled', True))

        measurement = str(props.get('measurement', 'flim')).strip().lower()
        if measurement not in MEASUREMENTS:
            raise ValueError(
                f'{name}: measurement {measurement!r} is not one of '
                f'{", ".join(MEASUREMENTS)}'
            )
        self._measurement = measurement
        self._click_role = str(props.get('photon_input', props.get('click_role', 'photons')))
        self._start_role = str(props.get('start_role', 'laser_sync'))
        self._line_role = str(props.get('line_role', 'line_clock'))
        # The frame role, when the card has one, lets Flim re-sync its pixel
        # index on every frame edge; 'none' keeps the line-clock-only path.
        self._frame_role = str(props.get('frame_role', 'auto'))
        # Per-pixel fits during the scan every this many seconds; 0 fits the
        # final frame only and previews the intensity (cheap on big frames).
        self._live_fit_period_s = float(props.get('live_fit_period_s', 1.0))
        retired = sorted(_RETIRED_CARD_PROPS.intersection(props))
        if timeTaggerManager is not None:
            self._timeTagger = timeTaggerManager
            if retired:
                self._logger.warning(
                    f'These detector properties are no longer read: '
                    f'{", ".join(retired)}. The setup\'s "timeTagger" block '
                    f'describes the card; remove them from the setup file.'
                )
        else:
            self._timeTagger = self._cardNotConfigured(props, retired)

        # Internal state — always kept in sync with parameters via setParameter()
        self._fallBackToConfiguredRoles()
        self._resolveRoles(publish=False)
        # The card knows which detector images which input: two FLIM
        # detectors on one input are refused here, at startup, and the
        # conditional filter triggers on the FLIM detectors' inputs only.
        self._registerInput()
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

        # Roles, then the card's inputs by name (in a block without an
        # inputs map those are the roles again).
        roleOptions = list(dict.fromkeys(
            list(ROLES) + list(getattr(self._timeTagger, 'inputs', lambda: ())())
        ))
        parameters = {
            # What this detector measures; set in the setup file.
            'measurement': DetectorListParameter(
                group='Channels', value=self._measurement,
                options=list(MEASUREMENTS), editable=False),
            # The photon input's conditioning, read-only: it is the card's
            # (the timeTagger block), shown and recorded with the detector so
            # a file says which input it came from and how it was set.
            'click_deadtime_ps': DetectorNumberParameter(
                group='Channels', value=self._click_deadtime_ps,
                valueUnits='ps', editable=False),
            'click_delay_ps': DetectorNumberParameter(
                group='Channels', value=self._click_delay_ps,
                valueUnits='ps', editable=False),
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
                group='Channels', value=self._start_ch if self._start_ch is not None else 0,
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
                options=list(_FIT_METHODS),
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
        #: The card's scan session this detector's scan takes part in.
        self._session = None
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
        # global lifetime fit, kept for scripts; the Lifetime widget reads
        # LiveProducts instead. None until the first valid frame is processed.
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
        # From here on the base classes exist, so __del__ may ask them to stop.
        self._baseInitialised = True

        self._nidaqManager.sigScanBuilt.connect(self._onScanBuilt)
        self._nidaqManager.sigScanStarted.connect(self.startScan)
        self._nidaqManager.sigScanDone.connect(self._onScanDone)
        scanStopped = getattr(self._nidaqManager, 'sigScanStopped', None)
        if scanStopped is not None:
            scanStopped.connect(self._onScanStopped)
        # The run in progress was stopped (stopScan): its frame is unfinished.
        self._scanStopped = False

        self._scanThread = None
        self._scanWorker = None
        #: Workers asked to stop that had not left yet: (worker, thread,
        #: retired-at). Held until their thread ends (see
        #: _teardownScanThread); their queued signals are stale by generation.
        self._retiredWorkers = []
        #: The previous scan's Flim and pixel markers, kept for a repeat scan
        #: of the same geometry (see _armFlim), and the generation whose
        #: final frame the card closed on it -- only then is it reusable.
        self._flimCache = None
        self._flimCleanGeneration = None
        #: Seconds per cube value the worker measured for read + fit, for
        #: the end-of-scan deadline (finishTimeoutS).
        self._frameCostPerValueS = None

    def finalize(self) -> None:
        """Stop the worker and release the card.

        The card is the setup's TimeTaggerManager, finalized by it; a
        stand-in for a setup without one never opened anything.
        """
        try:
            self.stopAcquisition()
        except Exception as e:
            self._logger.warning(f'Failed to stop acquisition at finalize: {e}')
        self._joinRetiredWorkers(_SCAN_THREAD_JOIN_TIMEOUT_MS)
        try:
            with self._flim_lock:
                self._ev_pix_begin = None
                self._ev_pix_end = None
                self._flim = None
            self._flimCache = None
            self._releaseCard()
        except Exception as e:
            self._logger.warning(f'Failed to clean up TimeTagger objects: {e}')
        unregister = getattr(getattr(self, '_timeTagger', None), 'unregisterDetector', None)
        if unregister is not None:
            try:
                unregister(self.name)
            except Exception:
                pass

    def __del__(self):
        if not self.__dict__.get('_baseInitialised'):
            # __init__ raised before the base classes existed: nothing was
            # started, and asking them to stop only logs that they were never
            # initialised.
            return
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
        if name == 'click_role':
            previous = self._click_role
            self._click_role = str(value)
            try:
                self._resolveRoles()
                self._registerInput()
            except (ValueError, TimeTaggerError) as error:
                # Refused (another FLIM detector images that input, or it is
                # no detector input): back to the input it had.
                self._logger.error(str(error))
                self._click_role = previous
                self.parameters['click_role'].value = previous
                self._resolveRoles()
        elif name in ('start_role', 'line_role', 'frame_role'):
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

    def _dropUnreadableProperties(self, props):
        """Remove from ``props`` (in place) every value this detector cannot
        read, each with a warning, and replace the readable ones by their
        parsed form (``"false"`` becomes False, ``"32"`` becomes 32).

        The reads in ``__init__`` then fall back to their defaults: a blank
        field or a word where a number goes is a slip to warn about, not a
        reason to stop ImControl.
        """
        for key, raw in list(props.items()):
            rule = _PROPERTY_RULES.get(key)
            if rule is None:
                continue
            if raw is None:
                # null reads as absent -- the default, quietly; left in,
                # ``props.get(key, default)`` would return the None.
                del props[key]
                continue
            parse, wanted = rule
            value = parse(raw)
            if value is None:
                del props[key]
                self._logger.warning(
                    f'managerProperties["{key}"] = {raw!r} is not {wanted}; '
                    f'using its default instead. Correct it in the setup file.'
                )
            else:
                props[key] = value

    def _fallBackToConfiguredRoles(self):
        """A role the card does not have falls back to the default role
        (with a warning) when the card has that; ``frame_role`` to 'auto'.

        Only a card without even the default role -- a block that sets the
        line clock's channel to null, say -- is left for
        :meth:`_resolveRoles` to refuse.
        """
        tt = self._timeTagger
        configured = ', '.join(tt.channels()) or 'none'
        for which, default in _DEFAULT_ROLES.items():
            role = getattr(self, f'_{which}_role')
            if tt.hasRole(role) or not tt.hasRole(default):
                continue
            self._logger.warning(
                f'{which}_role {role!r} is not a configured Time Tagger role '
                f'(configured: {configured}); using {default!r}. Correct it '
                f'in the setup file.'
            )
            setattr(self, f'_{which}_role', default)
        if self._frame_role not in ('auto', 'none') \
                and not tt.hasRole(self._frame_role):
            self._logger.warning(
                f'frame_role {self._frame_role!r} is not a configured Time '
                f'Tagger role (configured: {configured}); using \'auto\'. '
                f'Correct it in the setup file.'
            )
            self._frame_role = 'auto'

    def _cardNotConfigured(self, props, retired):
        """No ``timeTagger`` block, so no card: a stand-in that lets the
        detector start and refuses every scan, saying which block to add.

        The block is built from the retired card properties the detector
        still has, if any, and the block's defaults for the rest.
        """
        source = (
            f'from this detector\'s {", ".join(retired)}, which are no longer '
            f'read; remove them once the block is in'
            if retired else
            'with the block\'s default channels; set them to the cabling'
        )
        reason = (
            'the setup has no top-level "timeTagger" block. Add one to the '
            f'setup file ({source}): "timeTagger": '
            + json.dumps(_blockFromRetiredProps(props))
        )
        self._logger.error(
            f'No Time Tagger for this detector -- a scan with it is refused: '
            f'{reason}'
        )
        return TimeTaggerManager.notConfigured(reason)

    def _resolveRoles(self, publish=True):
        """Mirror the roles' channels and trigger levels into the attributes
        the scan code reads and, unless ``publish`` is false (before the base
        class has its parameters), into the read-only parameters."""
        tt = self._timeTagger
        resolved = {}
        counting = getattr(self, '_measurement', 'flim') == 'counting'
        for which, role in (('click', self._click_role),
                            ('start', self._start_role),
                            ('line', self._line_role)):
            if which == 'start' and counting and not tt.hasRole(role):
                # Counting needs no laser sync.
                resolved[which] = None
                continue
            try:
                resolved[which] = tt.channelInfo(role)
            except TimeTaggerError as error:
                hint = ''
                if which == 'start':
                    hint = (' FLIM needs a laser sync; set measurement to "counting" '
                            'to count photons without one.')
                if which == 'click':
                    detectors = list(getattr(tt, 'inputsOfKind', lambda _k: ())('detector'))
                    if detectors:
                        hint = (f' Name this detector\'s input with photon_input: '
                                f'one of {", ".join(detectors)}.')
                raise ValueError(
                    f'{self._instanceName}: {which}_role {role!r} is not a '
                    f'configured Time Tagger role: {error}.{hint}'
                ) from None
        self._click_ch = resolved['click'].channel
        self._start_ch = resolved['start'].channel if resolved['start'] is not None else None
        self._line_ch = resolved['line'].channel
        self._click_trigger = resolved['click'].trigger_v
        self._start_trigger = (resolved['start'].trigger_v
                               if resolved['start'] is not None else 0.0)
        self._line_trigger = resolved['line'].trigger_v
        self._click_input = resolved['click'].role
        self._click_deadtime_ps = int(resolved['click'].deadtime_ps)
        self._click_delay_ps = int(resolved['click'].delay_ps)
        if counting:
            # One detector per input: a sync or a clock is no photon input.
            kind = getattr(resolved['click'], 'kind', 'detector')
            if kind != 'detector':
                raise ValueError(
                    f'{self._instanceName}: photon input {self._click_role!r} is a '
                    f'{kind} input, not a detector input'
                )
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
                info = resolved[which]
                if f'{which}_channel' in params:
                    params[f'{which}_channel'].value = info.channel if info is not None else 0
                if f'{which}_trigger' in params:
                    params[f'{which}_trigger'].value = info.trigger_v if info is not None else 0.0
            if 'frame_channel' in params:
                params['frame_channel'].value = self._frame_ch if self._frame_ch is not None else 0
            if 'click_deadtime_ps' in params:
                params['click_deadtime_ps'].value = self._click_deadtime_ps
            if 'click_delay_ps' in params:
                params['click_delay_ps'].value = self._click_delay_ps

    def _registerInput(self):
        """Tell the card which input this detector images and how; raises
        ``ValueError`` when another FLIM detector images it already."""
        register = getattr(self._timeTagger, 'registerDetector', None)
        if register is None:
            return
        try:
            register(self._instanceName, self._click_role, self._measurement)
        except TimeTaggerError as error:
            raise ValueError(f'{self._instanceName}: {error}') from None

    def _writeTriggerLevel(self, which, voltage):
        """A trigger-level parameter writes through to the card; refused
        while a scan holds it, in which case the parameter shows the card's
        real value again.

        The refusal is decided here, without a card call; the write itself
        goes to the card's I/O thread, so a Detector Settings edit never
        waits on the card on the GUI thread. The parameters show the card's
        level once the write has landed (or failed).
        """
        role = getattr(self, f'_{which}_role')
        tt = self._timeTagger
        try:
            tt.assertWritable("trigger level")
        except TimeTaggerBusyError as error:
            self._logger.warning(str(error))
            self._resolveRoles()
            return
        submit = getattr(tt, 'setTriggerLevelAsync', None)
        if submit is None:
            self._writeTriggerLevelNow(role, which, voltage)
            return
        try:
            future = submit(role, voltage)
        except TimeTaggerError as error:
            self._logger.error(f'Could not set the {which} trigger level on the card: {error}')
            self._resolveRoles()
            return
        future.add_done_callback(
            lambda done, w=which: self._onTriggerLevelWritten(w, done))

    def _writeTriggerLevelNow(self, role, which, voltage):
        try:
            self._timeTagger.setTriggerLevel(role, voltage)
        except TimeTaggerBusyError as error:
            self._logger.warning(str(error))
        except TimeTaggerError as error:
            self._logger.error(
                f'Could not set the {which} trigger level on the card: {error}'
            )
        self._resolveRoles()

    def _onTriggerLevelWritten(self, which, future):
        """On the card's I/O thread: report a failure, show the card's level."""
        error = future.exception()
        if isinstance(error, TimeTaggerBusyError):
            self._logger.warning(str(error))
        elif error is not None:
            self._logger.error(
                f'Could not set the {which} trigger level on the card: {error}'
            )
        try:
            self._resolveRoles()
        except Exception:
            self._logger.exception('Could not re-read the trigger levels')

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
            # Clean when this generation's final frame was closed by the card
            # (_commitFrame): the session keeps its markers for a repeat scan
            # only if every participant left clean.
            clean = (held is not None
                     and self.__dict__.get('_flimCleanGeneration') == held)
            self._cardHeldGeneration = None
            # The pixel markers belong to the scan that just ended; a stale
            # virtual channel must not be handed to the next scope capture.
            self._ev_pix_begin = None
            self._ev_pix_end = None
            self._restoreClickDelay()
            session = self.__dict__.get('_session')
            leave = getattr(self._timeTagger, 'leaveScanSession', None)
            if session is not None and leave is not None:
                leave(self.name, clean=clean, session=session)
            else:
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

    def _teardownScanThread(self, timeoutMs=None):
        """Stop the scan worker, without ever waiting long on the caller.

        Every caller but shutdown runs on the GUI thread (scan start, the
        abort, the lease release's stop), and the worker can be inside
        ``getCurrentFrame()`` or a fit for seconds. So the worker is asked
        to stop and given ``_RETIRE_JOIN_MS`` to leave; if it is still busy
        it is *retired*: held in ``_retiredWorkers`` until its thread ends
        (dropping a running QThread crashes), its queued signals ignored by
        generation, its own reference keeping the Flim it reads alive. It
        leaves at its next check (between read and fit, between fit rows).

        A retired worker still running after ``_RETIRED_WORKER_STUCK_S`` is
        wedged in the vendor library; that is raised, so the stop contract
        quarantines the detector as before.

        With an explicit ``timeoutMs`` this is the strict bounded join of
        old: it waits that long and raises ``TimeoutError`` with the
        identities retained for a retry (shutdown, explicit recovery).
        """
        retire = timeoutMs is None
        if retire:
            timeoutMs = _RETIRE_JOIN_MS
        self._reapRetiredWorkers()
        worker = self.__dict__.get('_scanWorker')
        thread = self.__dict__.get('_scanThread')
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
            stillBusy = (
                retire and thread is not None and _threadRunning(thread)
                and all(isinstance(e, TimeoutError) for e in errors)
            )
            if not stillBusy:
                # Do not discard the only identities through which retry can
                # stop the worker.  Detector lease recovery will call
                # stopAcquisition again and the manager remains quarantined
                # until that succeeds.
                raise errors[0]
            self.__dict__.setdefault('_retiredWorkers', []).append(
                (worker, thread, time.monotonic())
            )

        if self.__dict__.get('_scanWorker') is worker:
            self._scanWorker = None
        if self.__dict__.get('_scanThread') is thread:
            self._scanThread = None
        self._raiseIfRetiredWorkerStuck()

    def _reapRetiredWorkers(self):
        """Forget retired workers whose thread has ended."""
        retired = self.__dict__.get('_retiredWorkers')
        if retired:
            retired[:] = [entry for entry in retired if _threadRunning(entry[1])]

    def _raiseIfRetiredWorkerStuck(self):
        self._reapRetiredWorkers()
        now = time.monotonic()
        for _worker, _thread, since in self.__dict__.get('_retiredWorkers') or ():
            if now - since > _RETIRED_WORKER_STUCK_S:
                raise TimeoutError(
                    'A stopped TimeTagger scan worker has not left the Time '
                    f'Tagger library for {now - since:.0f} s; the card may '
                    'have stalled'
                )

    def _joinRetiredWorkers(self, timeoutMs):
        """Shutdown only: wait (bounded) for retired workers to end."""
        for _worker, thread, _since in list(self.__dict__.get('_retiredWorkers') or ()):
            try:
                self._waitForScanThread(thread, timeoutMs)
            except Exception:
                pass
        self._reapRetiredWorkers()

    def initiateScan(self, scanInfoDict, signalDict):
        geometry = remember_scan_geometry(self, scanInfoDict)
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
            # The line clock fires once per line period -- Ny*S times for S
            # line steps (the TTL designer does: ROADMAP M9's clock half) --
            # but the FLIM image has no line-step axis: it would take each
            # step as an image line. Refused until the detector splits the
            # steps.
            raise RuntimeError(
                f'{self.name}: the Time Tagger cannot image a scan with '
                f'{S} linesteps: the line clock fires {S} times per image line '
                f'and the FLIM image has no line-step axis yet. Set n_linesteps '
                f'to 1 for FLIM.'
            )

        self._newFrameReady = False
        self._rawReady = False
        self._rawDelivered = True
        self._image_raw = None
        # Fresh display storage every scan (as before); a scan carrying a
        # geometry publishes it with its frames (see _frame_publication). In
        # accumulate mode the display mixes earlier scans' lifetimes into this
        # scan's frame; the geometry is this scan's.
        begin_frame_storage(self)
        self._image_display = np.zeros((1, Ny, Nx), dtype=np.float32)
        commit_frame_storage(self, geometry)
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
        # The direction depends on the conditional filter being applied:
        # a conditioning that failed at connect is retried here, before
        # the direction and the window are decided, so a card that comes
        # right only now is still acquired in its real direction.
        try:
            tt.ensureConnected()
            tt.ensureConditioned()
        except TimeTaggerError as error:
            with self._flim_lock:
                self._flim = None
            raise RuntimeError(f'Time Tagger is not available: {error}') from error
        counting = self._measurement == 'counting'
        direction = tt.tcspcDirection if not counting else 'none'
        period_ps = 1e6 / max(1e-9, float(self._laser_rep_rate_mhz))
        window_ps = self._n_bins * self._binwidth_ps
        if not counting and self._start_ch is None:
            raise RuntimeError(
                f'{self.name}: FLIM needs a laser sync (start_role '
                f'{self._start_role!r} is not configured); set measurement to '
                f'"counting" to count photons without one'
            )
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
            # The acquisition's own copy of the settings. The worker and the
            # products read these, never the live parameters, so an edit
            # during the scan reaches the next scan and cannot relabel this
            # one's metadata (fit, bins, t0, background).
            measurement=self._measurement,
            n_bins=int(self._n_bins) if not counting else 1,
            binwidth_ps=int(self._binwidth_ps),
            fit_method=str(self._fit_method),
            min_counts_per_pixel=int(self._min_counts_per_pixel),
            laser_rep_rate_mhz=float(self._laser_rep_rate_mhz),
            t0_ps=int(self._t0_ps),
            background_rate_hz=float(self._background_rate_hz),
        )
        self._shape = (Ny, Nx)

        try:
            tt.ensureConnected()
            self._setConnected("TimeTagger initialized")
        except TimeTaggerError as error:
            self._setConnectionError(error, summary="TimeTagger initialization failed")
            with self._flim_lock:
                self._flim = None
            raise RuntimeError('Time Tagger is not available') from error

        # Conditioning (trigger levels, dead times, delays, the filter) is
        # the card manager's and was applied at connect; from here until the
        # final frame has landed the card is held so nothing changes it.
        try:
            # Joins this scan's session on the card (the first detector of
            # the scan opens it) and holds the card. Refused while a
            # calibration owns the card (Busy) and on a card whose
            # conditioning failed (the filter not set, say).
            join = getattr(tt, 'joinScanSession', None)
            if join is not None:
                self._session = join(self.name)
            else:
                tt.beginScanHold(self.name)
                self._session = None
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
            if counting:
                # No time axis: the photon input keeps its configured delay.
                self._armCounter(tt)
                flim_start = flim_click = None
            elif direction == 'forward':
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

            if not counting:
                self._armFlim(tt, flim_start, flim_click)
            with self._flim_lock:
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

        if counting:
            what = 'photon counting'
        else:
            what = (f'start={self._start_ch}@{self._start_trigger}V, '
                    f'line={self._line_ch}@{self._line_trigger}V, {direction} TCSPC, '
                    f'n_bins={self._n_bins}, '
                    f'binwidth={self._binwidth_ps}ps, fit={self._fit_method}')
        if counting:
            what += f', line={self._line_ch}@{self._line_trigger}V'
        self._logger.info(
            f'TimeTagger prepared: click={self._click_ch}@{self._click_trigger}V, '
            f'{what}, '
            f'Nx={Nx}, Ny={Ny}, pixel_period={pixel_period_ps}ps, '
            f'scan_time={tot_scan_time_s:.3f}s '
            f'(ideal={ideal_scan_time_s:.3f}s, +{overhead_pct:.1f}% settling/flyback)'
            + (f', session {self._session.number} with '
               f'{", ".join(self._session.everyone)}' if self._session is not None else '')
        )
        # A generation is startable only after validation, hardware setup and
        # FLIM allocation all succeeded.
        self._preparedScanGeneration = generation

    def _onScanBuilt(self, scanInfoDict, signalDict, _devices):
        self._scanStopped = False
        try:
            # It builds its image from line pulses in time order; placing
            # interleaved or serpentine visits is not implemented for it.
            require_forward(self.name, scanInfoDict)
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

    def _pixelMarkers(self, tt):
        """The scan's pixel markers: the session's (one pair on the card for
        every detector of the scan), or -- a card manager without sessions --
        this detector's own. Returns ``(begin channel, end channel)``."""
        scan = self._scan
        # The pattern starts a little after the line edge (the block's
        # pixelPatternOffsetPs, plus a positive line delay): the designer
        # raises the frame clock on the same sample as the first line edge,
        # and the frame marker must lead pixel 0 whatever the cable skew.
        offset_ps = int(tt.patternOffsetPs(self._line_role))
        scan['pattern_offset_ps'] = offset_ps
        session = self.__dict__.get('_session')
        if session is None:
            self._create_virtual_pixel_pulses()
            scan['pixel_markers_reused'] = False
        else:
            markers, reused = session.pixelMarkers(
                line_channel=int(self._line_ch), n_x=int(scan['Nx']),
                pixel_period_ps=int(scan['pixel_period_ps']),
                pixel_width_ps=int(scan['pixel_width_ps']), offset_ps=offset_ps,
            )
            self._ev_pix_begin, self._ev_pix_end = markers.begin, markers.end
            scan['pixel_markers_reused'] = bool(reused)
        return int(self._ev_pix_begin.getChannel()), int(self._ev_pix_end.getChannel())

    def _armCached(self, tt, key, build, reuse=None):
        """This detector's measurement for the scan: the previous scan's when
        that one's final frame was closed by the card and ``key`` (what it is
        built from, the markers' channels included) is the same, else a new
        one from ``build()``; a replaced one is freed on the card's I/O
        thread. ``reuse(measurement)`` readies a kept one."""
        cache = self._flimCache
        clean = self._flimCleanGeneration is not None
        # Consumed here: this scan is in progress until its own final frame.
        self._flimCleanGeneration = None
        if cache is not None and clean and cache['key'] == key:
            if reuse is not None:
                reuse(cache['flim'])
            with self._flim_lock:
                self._flim = cache['flim']
            self._scan['flim_reused'] = True
            return
        self._flimCache = None
        if cache is not None:
            release = getattr(tt, 'releaseLater', None)
            # A detector's own markers (no session) go with its measurement;
            # the session's are the card manager's to free.
            box = [cache.pop('flim', None), cache.pop('begin', None), cache.pop('end', None)]
            if release is not None:
                release(box)
            cache = None
        measurement = build()
        with self._flim_lock:
            self._flim = measurement
        self._flimCache = dict(key=key, flim=measurement)
        if self.__dict__.get('_session') is None:
            self._flimCache.update(begin=self._ev_pix_begin, end=self._ev_pix_end)
        self._scan['flim_reused'] = False

    def _armFlim(self, tt, flim_start, flim_click):
        """The scan's ``Flim`` on the session's pixel markers, on the scan's
        (GUI) thread.

        It must exist before the NI-DAQ starts the clocks, which happens
        right after ``sigScanBuilt`` returns, so building it cannot move
        off this thread. A repeat scan does not need to: ``Flim`` acquires
        frame after frame, so when the previous scan's final frame was
        closed by the card (its pixel index is back at 0) and the geometry
        and markers are the same, the same object takes the next frame -- no
        allocation of a cube-sized buffer and no vendor teardown per repeat.
        """
        begin_ch, end_ch = self._pixelMarkers(tt)
        scan = self._scan
        key = (
            'flim', id(tt.tagger), int(flim_start), int(flim_click), begin_ch, end_ch,
            None if self._frame_ch is None else int(self._frame_ch),
            int(scan['n_pixels_total']), int(self._n_bins), int(self._binwidth_ps),
        )

        def build():
            flim_kwargs = {}
            if self._frame_ch is not None:
                # The card re-syncs its pixel index on every frame edge: a
                # lost line marker costs one frame, not the rest of the scan.
                flim_kwargs['frame_begin_channel'] = int(self._frame_ch)
            return tt.api.Flim(
                tt.tagger,
                start_channel=flim_start,
                click_channel=flim_click,
                pixel_begin_channel=begin_ch,
                pixel_end_channel=end_ch,
                n_pixels=int(scan['n_pixels_total']),
                n_bins=self._n_bins,
                binwidth=self._binwidth_ps,
                **flim_kwargs,
            )

        self._armCached(tt, key, build)

    def _armCounter(self, tt):
        """The scan's photon counter on the session's pixel markers: a
        ``CountBetweenMarkers`` of the photon input, one value per pixel
        (verify against the vendor documentation / on a real card: the
        class, its argument order, that it fills ``n_values`` once and that
        ``clear`` re-arms it). Kept for a repeat scan like a ``Flim``, and
        cleared for it."""
        begin_ch, end_ch = self._pixelMarkers(tt)
        scan = self._scan
        n_pixels = int(scan['n_pixels_total'])
        key = ('count', id(tt.tagger), int(self._click_ch), begin_ch, end_ch, n_pixels)

        def build():
            counter = tt.api.CountBetweenMarkers(
                tt.tagger, int(self._click_ch), begin_ch, end_ch, n_pixels)
            return _CountsAsFrames(counter, n_pixels, float(scan['dwell_s']))

        self._armCached(tt, key, build, reuse=lambda adapter: adapter.rearm())

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
        if (generation == self._activeScanGeneration
                and self._scanWorker is not None):
            # A second start for the scan already running (a repeated
            # sigScanStarted, a click while the first start was in flight):
            # the worker is there, nothing to do.
            self._logger.debug(f'Scan generation {generation} already started; ignored')
            return

        # Tear down any previous scan thread only after confirming that the new
        # generation is fully prepared.
        self._teardownScanThread()

        self.acquisition = True
        self._activeScanGeneration = generation
        workerClass = (_TTCountWorker if self._scan.get('measurement') == 'counting'
                       else _TTFlimWorker)
        self._scanWorker = workerClass(self, generation)
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

    def _onScanStopped(self):
        """NidaqManager stopped the scan (stopScan; its sigScanDone follows):
        the FLIM frame in progress is unfinished, so it is not read as the
        final one -- the abort that ends the run tears the worker down and
        drops it (plan scan-stop-now)."""
        if self._scanParticipating:
            self._scanStopped = True

    def _onScanDone(self):
        """Triggered by NidaqManager.sigScanDone — flips acquisition off and
        wakes the worker immediately so it reads the final completed frame.
        Not after a stop: there is no completed frame to read.
        """
        if not self._scanParticipating:
            return
        self.acquisition = False
        if self.__dict__.get('_scanStopped', False):
            return
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

    def finishTimeoutS(self) -> float:
        """How long after scan-done this detector may need to land its final
        frame: the card's grace for closing it, then reading and fitting the
        whole cube. The scan coordinator takes this as the end-of-scan
        deadline when it is longer than its own default, so a large frame
        or a slow fit (exp1) is not cut off and lost at 5 s.

        From the read + fit time the worker measured on this card (per cube
        value), else a conservative assumption for the method.
        """
        scan = self.__dict__.get('_scan') or {}
        values = (int(scan.get('Nx', 0) or 0) * int(scan.get('Ny', 0) or 0)
                  * int(scan.get('n_bins', self.__dict__.get('_n_bins', 0)) or 0))
        perValue = self.__dict__.get('_frameCostPerValueS')
        if perValue is None:
            method = str(scan.get('fit_method', self.__dict__.get('_fit_method', 'moment')))
            perValue = (4.0 if method == 'exp1' else 1.0) / _ASSUMED_FIT_VALUES_PER_S
        work = values * float(perValue)
        return float(min(_MAX_FINISH_TIMEOUT_S,
                         _TTFlimWorker.FINAL_FRAME_GRACE_S + 2.0 * work + 3.0))

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
        """Slot of the worker's queued ``sigFrameReady``. Nothing may escape
        it: an exception in a Qt slot can abort the application. A frame
        that cannot be committed is logged; the worker's terminal signal,
        which follows, still releases a waiting end-of-scan barrier."""
        try:
            self._commitFrame(intensity_img, lifetime_img, is_final, decay_counts,
                              t_axis_ns, global_tau_ns, scanGeneration, live)
        except Exception:
            self._logger.exception('Could not publish a TimeTagger frame')

    def _commitFrame(self, intensity_img, lifetime_img, is_final, decay_counts,
                     t_axis_ns, global_tau_ns, scanGeneration, live):
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
        counting = (self._scan or {}).get('measurement') == 'counting'
        if counting:
            # The image is the counts; there is no lifetime to show.
            self._commitCounts(np.asarray(intensity_img, dtype=np.float32), is_final,
                               scanGeneration, live)
            return
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
            # A frame the card closed leaves its pixel index at 0: the same
            # Flim can take a repeat scan's frame (_armFlim).
            closed = live is not None and (live.metadata or {}).get('frame_closed_by_card') is True
            self._flimCleanGeneration = scanGeneration if closed else None
            # The scan's last frame is committed and published — the
            # coordinator may now release the lease and let the next
            # iteration arm.
            self._fireFinalFrameAck(scanGeneration)

    def _commitCounts(self, counts, is_final, scanGeneration, live):
        """``_commitFrame`` for a counting detector: display and record the
        counts per pixel."""
        if is_final:
            self._frame_index += 1
        if live is not None:
            live.frame_index = self._frame_index + (0 if is_final else 1)
            self.sigTimeResolvedProducts.emit(live)
        self._image_display[0] = counts
        if is_final:
            self._image_raw = counts[np.newaxis].astype(np.float32, copy=True)
            self._rawReady = True
            self._rawDelivered = False
        self._newFrameReady = True
        self.updateLatestFrame(True)
        self.sigNewFrame.emit()
        if is_final:
            closed = live is not None and (live.metadata or {}).get('frame_closed_by_card') is True
            self._flimCleanGeneration = scanGeneration if closed else None
            self._fireFinalFrameAck(scanGeneration)

    # ------------------------------------------------------------------ #
    # Generic time-resolved detector contract                              #
    # ------------------------------------------------------------------ #

    @property
    def measurement(self) -> str:
        """``'flim'`` or ``'counting'`` (the ``measurement`` property)."""
        return self._measurement

    @property
    def photonInput(self) -> str:
        """The name of the card input this detector images."""
        return self._click_input

    def inputProvenance(self) -> dict:
        """Which card input this detector images and how the card conditions
        it: for metadata that must stay interpretable later."""
        tt = self._timeTagger
        out = {
            "detector": self.name,
            "measurement": self._measurement,
            "photon_input": self._click_input,
        }
        try:
            out.update(tt.channelInfo(self._click_role).conditioning())
        except Exception:
            out.update(channel=int(self._click_ch), trigger_v=float(self._click_trigger))
        session = self.__dict__.get('_session')
        if session is not None:
            out["scan_session"] = session.summary()
        return out

    def timeResolvedCapabilities(self) -> dict:
        if self._measurement == 'counting':
            return {
                "time_axis": None,
                "supports_binned_cube": False,
                "supports_raw_tags": False,
                "supports_software_gates": False,
                "supports_hardware_gates": False,
                "supports_lifetime_fit": False,
                "supports_outer_scan_axes": False,
                "native_cube_axes": ("y", "x"),
                "vendor": "Swabian Instruments",
                "model": "Time Tagger",
                "measurement": "counting",
            }
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
        if self._measurement == 'counting':
            raise RuntimeError(
                f"{self.name} counts photons per pixel (measurement 'counting'): it has "
                "no time axis, so no time-resolved products; its image is the counts"
            )
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
        raw_cube_counts: np.ndarray | None = None,
    ) -> None:
        with self._tr_lock:
            if not self._tr_enabled:
                return
            config = self._tr_config
            should_store = is_final or config.include_live_products
            if not should_store:
                return

            # The stored cube is the card's integer counts; the gates and
            # the fits use the background-subtracted one.
            stored = raw_cube_counts if raw_cube_counts is not None else cube_counts
            cube_for_storage = None
            if config.capture_cube:
                stored = np.asarray(stored)
                if np.issubdtype(stored.dtype, np.integer):
                    cube_for_storage = np.array(stored, copy=True)
                else:
                    cube_for_storage = np.rint(np.clip(stored, 0, None)).astype(np.uint32)
            frame = dict(extra_metadata or {})
            snapshot = dict(self._scan or {})
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
                # The input this detector images and its conditioning (the
                # card's metadata above lists every input).
                "photon_input": self.inputProvenance(),
                "start_trigger_v": float(self._start_trigger),
                "line_trigger_v": float(self._line_trigger),
                "n_bins": int(snapshot.get("n_bins", self._n_bins)),
                "binwidth_ps": int(snapshot.get("binwidth_ps", self._binwidth_ps)),
                "t0_ps": int(snapshot.get("t0_ps", self._t0_ps)),
                "fit_method": str(snapshot.get("fit_method", self._fit_method)),
                "laser_rep_rate_mhz": float(snapshot.get("laser_rep_rate_mhz", self._laser_rep_rate_mhz)),
                "min_counts_per_pixel": int(snapshot.get("min_counts_per_pixel", self._min_counts_per_pixel)),
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
                tcspc_direction=str(frame.get("tcspc_direction", "forward")),
                background_rate_hz=float(frame.get("background_rate_hz", 0.0) or 0.0),
                pileup_max=float(frame.get("pileup_max", 0.0) or 0.0),
                overflows=int(frame.get("overflows", 0) or 0),
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
        display, displayGeometry = read_with_frame_geometry(
            self, self.getChunk
        )
        raw = _EMPTY_CHUNK
        if (self._rawReady and not self._rawDelivered
                and self._image_raw is not None):
            self._rawDelivered = True
            self._rawReady = False
            raw = self._image_raw
            self._image_raw = None
        return ChunkPayload(
            display=display, raw=raw, display_geometry=displayGeometry
        )

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
        frame, geometry = read_with_frame_geometry(
            self, lambda: self._image_display
        )
        return display_frame(self, frame, geometry)

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
        # By position, as the line clock plays them: the fast axis, then the
        # line axis the line steps repeat, then the outer axes -- whatever
        # they are called. The designers name their axes by position (x, y,
        # z), an Expert program physically (a fast-Y raster is ('y', 'x')),
        # and taking them by name read its lines along the wrong axis.
        Nx = int(scan_dims[0])
        # a 1-axis scan is a single line: no second dimension to read
        Ny = int(scan_dims[1]) if len(scan_dims) >= 2 else 1
        outer_axes = list(scan_axes[2:])
        outer_dims = [int(d) for d in scan_dims[2:]]
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

class _StopRequested(Exception):
    """The worker was asked to stop between a read and the end of a fit."""


class _PreviewSuperseded(Exception):
    """Scan-done arrived during a preview fit: the final frame comes next."""


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
    # Filled from the acquisition snapshot in run(); defaults for a worker
    # asked for a preview before that.
    _rep_rate_mhz = 80.0
    _background_rate_hz = 0.0
    STALL_MAX = 10  # consecutive live-preview ticks with no data → ~10 s
    #: Tick while waiting for the card to close the frame, and without live
    #: fits (intensity previews only).
    POLL_S = 0.25
    #: After scan-done, how long to wait for the card to close the frame
    #: before the current frame is taken as the final one.
    FINAL_FRAME_GRACE_S = 2.0
    #: Cube values fitted per block: a stop request is honoured between
    #: blocks, and the fit's temporaries stay this size, not the cube's.
    FIT_BLOCK_VALUES = 4_000_000

    def __init__(self, m: SwabianTimeTaggerManager, scanGeneration):
        super().__init__()
        self._logger = initLogger(self, tryInheritParent=True)
        self._m = m
        self.scanGeneration = scanGeneration
        self._running = True
        self._last_total_counts = 0.0
        self._done_event = threading.Event()
        self._stop_event = threading.Event()

    def stop(self):
        self._running = False
        self._stop_event.set()
        self._done_event.set()  # unblock wait() immediately

    def signal_done(self):
        """Called from the main thread when sigScanDone fires."""
        self._done_event.set()

    def run(self):
        try:
            Nx = int(self._m._scan['Nx'])
            Ny = int(self._m._scan['Ny'])
            scan = self._m._scan
            # The acquisition's snapshot (initiateScan), not the live
            # parameters: an edit during the scan waits for the next one.
            n_bins = int(scan.get('n_bins', self._m._n_bins))
            binwidth_ps = int(scan.get('binwidth_ps', self._m._binwidth_ps))
            fit_method = str(scan.get('fit_method', self._m._fit_method))
            min_counts = int(scan.get('min_counts_per_pixel', self._m._min_counts_per_pixel))
            self._rep_rate_mhz = float(scan.get('laser_rep_rate_mhz', self._m._laser_rep_rate_mhz))
            self._background_rate_hz = float(scan.get('background_rate_hz', self._m._background_rate_hz))
            t0_ps = int(scan.get('t0_ps', self._m._t0_ps))
            expected_shape = (Nx * Ny, n_bins)
            self._direction = str(scan.get('direction', 'forward'))
            period_ps = float(scan.get('period_ps', 0.0)) or (
                1e6 / max(1e-9, self._rep_rate_mhz)
            )
            self._period_ns = period_ps / 1000.0
            self._dwell_s = float(scan.get('dwell_s', 0.0))
            self._binwidth_ps = binwidth_ps
            # Reverse mode applies t0 as a circular roll (see initiateScan).
            self._t0_roll_bins = (
                int(round(t0_ps / binwidth_ps)) % n_bins
                if self._direction == 'reverse' and n_bins else 0
            )
            self._background_per_bin = background_per_bin(
                self._background_rate_hz, self._dwell_s, binwidth_ps,
                period_ps,
            )
            self._pileup_warned = False
            self._overflow_baseline = int(scan.get('overflow_baseline', 0))
            self._overflows_warned = False

            # The histogram's own axis (bin centres), then the forward-time
            # axis the fits and the user see: identical in forward mode,
            # mirrored on the laser period in reverse mode.
            raw_axis_ns = (np.arange(n_bins, dtype=np.float64) + 0.5) * binwidth_ps * 1e-3
            # A counting detector has no TCSPC direction ('none'): its one
            # bin is a forward axis.
            _, oriented_axis_ns = orient_cube(
                np.zeros((1, 1, n_bins), dtype=np.float32), raw_axis_ns,
                'reverse' if self._direction == 'reverse' else 'forward',
                period_ns=self._period_ns,
            )
            t_axis = (oriented_axis_ns * 1e-9).astype(np.float32)
            t_axis_f64 = oriented_axis_ns.astype(np.float64)[None, None, :] * 1e-9
            # Phasor needs the laser repetition period, NOT the histogram window.
            # The Flim API doesn't expose the rep rate, so take it from the
            # user-supplied parameter. Default 80 MHz is the most common Ti:Sa rate.
            rep_rate_hz = max(1.0, float(self._rep_rate_mhz)) * 1e6
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
            wait_s = tick
            seen_done = False

            while self._running:
                if seen_done:
                    # The done event stays set: sleep on the stop event, or
                    # the grace period spins on getFramesAcquired().
                    self._stop_event.wait(timeout=self.POLL_S)
                    scan_done = True
                else:
                    scan_done = self._done_event.wait(timeout=wait_s)
                    seen_done = scan_done
                wait_s = tick
                if not self._running:
                    break

                frames = self._frames_acquired()
                if (frames is not None and frames_baseline is not None
                        and frames - frames_baseline >= 1):
                    cube = self._read_ready_frame(expected_shape, Nx, Ny, n_bins)
                    if not self._running:
                        break
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
                    if not self._running:
                        break
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
                    started = time.monotonic()
                    cube = self._poll_frame(expected_shape, Nx, Ny, n_bins)
                    if not self._running:
                        break
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
                    try:
                        self._emit_frame(cube, t_axis, t_axis_f64, fit_method,
                                         omega, cos_table, sin_table, min_counts,
                                         rep_rate_hz, is_final=False)
                    except _PreviewSuperseded:
                        continue   # straight to the final frame
                    cost = time.monotonic() - started
                    self._m._frameCostPerValueS = cost / max(1, cube.size)
                    # Never behind its own tick: a preview that took longer
                    # than the tick is followed by at least as long a rest,
                    # so previews never run back to back and the worker
                    # leaves the GUI thread at least half of the interpreter.
                    wait_s = max(tick, cost)
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

        except _StopRequested:
            pass  # stopped mid-fit: the frame is dropped, as a stop asks
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
            # Flim.getCurrentFrameIntensity() is counts per second (the
            # counts divided by the pixel's integration time); the preview
            # shows counts, like every other frame.
            rate = np.asarray(getter(), dtype=np.float32).reshape(Ny, Nx)
        except Exception:
            self._logger.exception('getCurrentFrameIntensity() raised.')
            return
        intensity = rate * np.float32(self._dwell_s)
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
            pileup_max=float(pileup_fraction(intensity, max(1.0, float(self._rep_rate_mhz)) * 1e6, self._dwell_s).max()) if intensity.size else 0.0,
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

        lifetime = self._fit_blocks(fit_method, cube, intensity, t_axis,
                                    t_axis_f64, peak_bin, t_peak, omega,
                                    cos_table, sin_table, is_final=is_final)

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
        closed = bool(getattr(self, '_frame_closed_by_card', False)) if is_final else None
        # A final frame the card never closed is a partial buffer: missing
        # markers, not a measurement. It is published so the user sees
        # something, but marked invalid like an overflowed one.
        frame_valid = overflows == 0 and (closed is None or closed)
        frame_metadata = {
            "overflows": int(overflows),
            "frame_valid": bool(frame_valid),
            "frame_closed_by_card": closed,
            "pattern_offset_ps": int(self._m._scan.get('pattern_offset_ps', 0)),
            "tcspc_direction": self._direction,
            "laser_period_ns": float(self._period_ns),
            "dwell_s": float(self._dwell_s),
            "background_rate_hz": float(self._background_rate_hz),
            "background_per_bin": float(self._background_per_bin),
            "t0_ps": int(self._m._scan.get('t0_ps', self._m._t0_ps)),
            "binwidth_ps": int(self._binwidth_ps),
            "pileup_max": pileup_max,
            "t0_roll_bins": int(self._t0_roll_bins),
        }

        self._m._store_time_resolved_products(
            cube_counts=cube,
            raw_cube_counts=raw_counts,
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

    def _fit_blocks(self, fit_method, cube, intensity, t_axis, t_axis_f64,
                    peak_bin, t_peak, omega, cos_table, sin_table, *,
                    is_final=True):
        """The per-pixel fit, a block of rows at a time.

        Every fitter is per pixel, so the blocks give the same image as one
        call. Between blocks a stop request is honoured (``_StopRequested``:
        the frame is dropped), a preview gives way to the final frame once
        scan-done has arrived (``_PreviewSuperseded``), and the fitters' temporaries -- several
        float64 copies of their input for exp1 -- are a block's size, not
        the cube's.
        """
        ny = int(cube.shape[0])
        per_row = max(1, int(np.prod(cube.shape[1:])))
        step = max(1, int(self.FIT_BLOCK_VALUES // per_row))
        parts = []
        for y0 in range(0, ny, step):
            if not self._running:
                raise _StopRequested()
            if not is_final and self._done_event.is_set():
                raise _PreviewSuperseded()
            block = cube[y0:y0 + step]
            if fit_method == 'phasor':
                part = fit_phasor(block, intensity[y0:y0 + step], omega,
                                  cos_table, sin_table, t_peak)
            elif fit_method == 'exp1':
                part = fit_exp1(block, t_axis_f64, peak_bin)
            else:
                _, part = fit_moment(block, t_axis, peak_bin)
            parts.append(part)
        if not parts:
            return np.zeros(cube.shape[:2], dtype=np.float32)
        return parts[0] if len(parts) == 1 else np.concatenate(parts, axis=0)

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


# --------------------------------------------------------------------------- #
# Counting: photons per pixel, no sync                                         #
# --------------------------------------------------------------------------- #

class _CountsAsFrames:
    """A ``CountBetweenMarkers`` seen through the ``Flim`` calls the worker
    makes, so one worker loop -- final-frame detection, the grace after
    scan-done, stop and retire -- serves both measurements.

    A frame is one value per pixel; it is *closed* (``getFramesAcquired``
    reaches 1) when the counter has filled all its values (``ready()``),
    i.e. every pixel's end marker has passed. ``rearm`` clears it for a
    repeat scan. Vendor calls used: ``ready``, ``getData``, ``clear``,
    ``stop`` -- verify against the vendor documentation / on a real card.
    """

    def __init__(self, counter, n_pixels: int, dwell_s: float):
        self._counter = counter
        self._n_pixels = int(n_pixels)
        self._dwell_s = float(dwell_s)

    @property
    def counter(self):
        return self._counter

    def getFramesAcquired(self) -> int:
        return 1 if bool(self._counter.ready()) else 0

    def getCurrentFrame(self):
        data = np.asarray(self._counter.getData()).reshape(-1)
        if data.size != self._n_pixels:
            return None
        return data.reshape(self._n_pixels, 1)

    getReadyFrame = getCurrentFrame

    def getCurrentFrameIntensity(self):
        """Counts per second per pixel, as ``Flim`` reports its intensity."""
        frame = self.getCurrentFrame()
        if frame is None:
            return np.zeros(self._n_pixels, dtype=np.float64)
        counts = frame[:, 0].astype(np.float64)
        return counts / self._dwell_s if self._dwell_s > 0 else counts

    def rearm(self) -> None:
        self._counter.clear()

    def stop(self) -> None:
        self._counter.stop()


class _TTCountWorker(_TTFlimWorker):
    """The scan worker of a counting detector: the FLIM worker's loop (the
    final frame by count, the grace, previews, stop and retire), with the
    counts per pixel as the image and no fit."""

    def _emit_frame(self, cube, t_axis, t_axis_f64, fit_method,
                    omega, cos_table, sin_table, min_counts, rep_rate_hz,
                    *, is_final=False):
        counts = np.asarray(cube[..., 0], dtype=np.float32)
        self._last_total_counts = float(counts.sum())
        overflows = max(0, self._m._timeTagger.overflows() - self._overflow_baseline)
        if overflows and not self._overflows_warned:
            self._overflows_warned = True
            self._logger.error(
                f'The Time Tagger reported {overflows} USB overflow(s) during '
                f'this scan: the counts are missing tags.'
            )
        closed = bool(getattr(self, '_frame_closed_by_card', False)) if is_final else None
        metadata = {
            "measurement": "counting",
            "overflows": int(overflows),
            "frame_valid": bool(overflows == 0 and (closed is None or closed)),
            "frame_closed_by_card": closed,
            "pattern_offset_ps": int(self._m._scan.get('pattern_offset_ps', 0)),
            "dwell_s": float(self._dwell_s),
        }
        live = LiveProducts(
            intensity=counts,
            lifetime_ns=None,
            decay_counts=np.zeros(0, dtype=np.float32),
            t_axis_ns=np.zeros(0, dtype=np.float32),
            gate_images={},
            global_tau_ns=0.0,
            peak_time_ns=0.0,
            background_per_bin=0.0,
            pileup_max=0.0,
            tcspc_direction="none",
            frame_index=0,
            is_final=bool(is_final),
            metadata=metadata,
        )
        self.sigFrameReady.emit(
            counts, np.zeros_like(counts), is_final, np.zeros(0, dtype=np.float32),
            np.zeros(0, dtype=np.float32), 0.0, self.scanGeneration, live,
        )

    def _emit_intensity_preview(self, Nx, Ny, t_axis):
        """With live previews off: the counts so far, as a preview frame."""
        with self._m._flim_lock:
            adapter = self._m._flim
        if adapter is None:
            return
        frame = adapter.getCurrentFrame()
        if frame is None:
            return
        self._emit_frame(np.asarray(frame, dtype=np.float32).reshape(Ny, Nx, 1),
                         None, None, None, None, None, None, 0, 0.0, is_final=False)
