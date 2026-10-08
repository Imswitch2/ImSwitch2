"""One Swabian Time Tagger card, shared by every consumer in the setup.

``TimeTaggerManager`` is a low-level manager like ``NidaqManager``: built once
by ``MasterController`` from the setup's ``timeTagger`` block, handed to the
detector managers in ``lowLevelManagers``, and finalized at shutdown. It owns

* the connection (the real vendor library, or the in-process mock);
* the channel *roles* -- ``photons``, ``laser_sync``, ``line_clock``,
  ``frame_clock``, ``sted_pulse`` -- each a signed channel number with its
  trigger level, dead time and delay, so every consumer speaks roles and the
  cabling is described in one place;
* the conditioning writes themselves, applied at connect and refused while a
  scan holds the card (a trigger-level change mid-frame corrupts the frame);
* a metadata snapshot that every measurement records.

It does not own measurements: Swabian measurement objects run concurrently on
the card, so the FLIM detector builds its own ``Flim`` through ``api`` and
``tagger``, and a script's ``Counter`` does not disturb it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import numpy as np

from imswitch.imcommon.framework import Signal, SignalInterface
from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.SetupInfo import TimeTaggerInfo

try:
    import TimeTagger as _VendorTimeTagger
except ImportError:  # the vendor installer provides it; pip does not
    _VendorTimeTagger = None


#: Role id -> (channel field, trigger-level field, dead-time field,
#: delay field) on ``TimeTaggerInfo``. ``None`` where a role has no such
#: setting. The order is the order consumers and docs list them in.
ROLE_FIELDS: Dict[str, Tuple[str, str, Optional[str], Optional[str]]] = {
    "photons": ("photonsChannel", "photonsTriggerV", "photonsDeadtimePs", None),
    "laser_sync": ("laserSyncChannel", "laserSyncTriggerV", None, None),
    "line_clock": ("lineClockChannel", "lineClockTriggerV", None, "lineClockDelayPs"),
    "frame_clock": ("frameClockChannel", "frameClockTriggerV", None, "lineClockDelayPs"),
    "sted_pulse": ("stedPulseChannel", "stedPulseTriggerV", None, None),
}

ROLES = tuple(ROLE_FIELDS)


class TimeTaggerError(RuntimeError):
    """The card is not available, or the request is not valid for it."""


class TimeTaggerBusyError(TimeTaggerError):
    """A conditioning write was refused because a scan holds the card."""


@dataclass(frozen=True)
class ChannelInfo:
    """One configured role."""

    role: str
    channel: int
    """ Signed: negative means the falling edge of input ``abs(channel)``. """
    trigger_v: float
    deadtime_ps: int = 0
    delay_ps: int = 0

    @property
    def input(self) -> int:
        """The physical input number, edge sign stripped."""
        return abs(self.channel)

    @property
    def edge(self) -> str:
        return "falling" if self.channel < 0 else "rising"


@dataclass(frozen=True)
class TimeTaggerHealth:
    """One look at the card: what every role is counting, right now."""

    rates_hz: Dict[str, float]
    """ Counts per second per role (the sync's, with the filter on, is the
    photon rate by construction: see ``sync_rate_label``). """
    overflows: int
    """ USB overflows since the previous look; any number above zero means
    frames read meanwhile are missing tags. """
    tcspc_direction: str
    filter_on: bool
    sync_rate_label: str
    """ ``"sync"`` or ``"sync (filtered)"``. """
    model: str
    serial: str
    is_mock: bool
    connected: bool
    sampled_at: float
    """ ``time.monotonic()`` of the look. """

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rates_hz": dict(self.rates_hz),
            "overflows": int(self.overflows),
            "tcspc_direction": self.tcspc_direction,
            "filter_on": bool(self.filter_on),
            "sync_rate_label": self.sync_rate_label,
            "model": self.model,
            "serial": self.serial,
            "is_mock": bool(self.is_mock),
            "connected": bool(self.connected),
        }


class TimeTaggerManager(SignalInterface):
    """See the module docstring.

    ``setupInfo`` and ``nidaqManager`` are optional so a detector can build a
    private instance from its own properties (the compatibility path for
    setups without a ``timeTagger`` block) and so tests can construct one
    bare. With a ``nidaqManager``, the mock fallback is honoured only while
    the NI-DAQ is simulated too, and the mock card is fed the scan's line
    and frame edges from every scan the NI-DAQ builds.
    """

    #: A ``TimeTaggerHealth``, from the background sampler (off unless
    #: ``startHealthSampling`` is called) or from ``health()``.
    sigHealth = Signal(object)

    #: How long one health look integrates the count rates.
    HEALTH_INTEGRATION_S = 0.2

    def __init__(self, info: TimeTaggerInfo, setupInfo=None, nidaqManager=None,
                 api=None):
        super().__init__()
        self.__logger = initLogger(self)
        self._info = info
        self._setupInfo = setupInfo
        self._nidaqManager = nidaqManager
        self._api = api  # injected vendor-or-mock module (tests, mock rigs)
        self._tagger = None
        self._isMock = False
        self._connectError: Optional[Exception] = None
        self._conditioningError: Optional[Exception] = None
        self._lock = threading.RLock()
        self._holders: set = set()
        self._calibrationOwner: Optional[str] = None
        #: Monotonic USB-overflow total. The card's counter is read-and-clear,
        #: so exactly one place reads it; everyone else takes snapshots.
        self._overflowTotal = 0
        self._healthOverflowSeen = 0
        self._finalized = False

        self._samplerThread: Optional[threading.Thread] = None
        self._samplerStop = threading.Event()
        self._lastHealth: Optional[TimeTaggerHealth] = None

        self._channels: Dict[str, ChannelInfo] = {}
        for role, (chField, trigField, deadField, delayField) in ROLE_FIELDS.items():
            channel = getattr(info, chField)
            if channel is None:
                continue
            self._channels[role] = ChannelInfo(
                role=role,
                channel=int(channel),
                trigger_v=float(getattr(info, trigField)),
                deadtime_ps=int(getattr(info, deadField)) if deadField else 0,
                delay_ps=int(getattr(info, delayField)) if delayField else 0,
            )

        self.connect()

        scanBuilt = getattr(nidaqManager, "sigScanBuilt", None)
        if scanBuilt is not None and hasattr(scanBuilt, "connect"):
            # Connected before any detector's own slot (the detectors are
            # built after the low-level managers), so the mock card knows
            # the scan's edges before a Flim is built on it.
            scanBuilt.connect(self._onScanBuilt)
        scanStarted = getattr(nidaqManager, "sigScanStarted", None)
        if scanStarted is not None and hasattr(scanStarted, "connect"):
            scanStarted.connect(self._onScanStarted)
        scanDone = getattr(nidaqManager, "sigScanDone", None)
        if scanDone is not None and hasattr(scanDone, "connect"):
            scanDone.connect(self._onScanDone)

    # ------------------------------------------------------------------ #
    # Connection                                                           #
    # ------------------------------------------------------------------ #

    def connect(self) -> bool:
        """Open the card (or the mock). Returns whether it is connected.

        A failure is logged and remembered, not raised: the application must
        still start on a rig whose card is unplugged, and the detector turns
        the failure into a scan rollback through :meth:`ensureConnected`.
        """
        with self._lock:
            if self._tagger is not None:
                return True
            if self._finalized:
                raise TimeTaggerError("TimeTaggerManager has been finalized")

            info = self._info
            api = self._api
            if info.simulation:
                api = api or self._mockApi()
                self._isMock = True
            elif api is None:
                api = _VendorTimeTagger

            if api is None:
                error = ImportError(
                    "The Swabian TimeTagger Python package is not installed; "
                    "install it from the Swabian Instruments software package."
                )
                return self._handleConnectFailure(error)

            try:
                tagger = api.createTimeTagger(info.serial) if info.serial \
                    else api.createTimeTagger()
            except Exception as error:
                return self._handleConnectFailure(error)

            self._api = api
            self._tagger = tagger
            self._connectError = None
            self._tryApplyConditioning()
            self.__logger.info(
                f"Time Tagger connected: {self.model} ({self.serial})"
                f"{' [mock]' if self._isMock else ''}; roles "
                + ", ".join(f"{c.role}={c.channel}" for c in self._channels.values())
            )
            return True

    def _handleConnectFailure(self, error) -> bool:
        info = self._info
        simulatedRig = bool(
            getattr(self._nidaqManager, "isSimulated", False)
        ) if self._nidaqManager is not None else False
        if info.useMockOnFailure and simulatedRig:
            self.__logger.warning(
                f"Time Tagger unavailable ({error}); the NI-DAQ is simulated "
                "and useMockOnFailure is set, so using the mock card."
            )
            self._api = self._mockApi()
            self._isMock = True
            self._tagger = self._api.createTimeTagger()
            self._connectError = None
            self._tryApplyConditioning()
            return True
        if info.useMockOnFailure and not simulatedRig:
            self.__logger.error(
                f"Time Tagger unavailable ({error}). useMockOnFailure is set "
                "but the NI-DAQ is not simulated, so the mock is refused: a "
                "rig must not image a missing card as zeros."
            )
        else:
            self.__logger.error(f"Time Tagger unavailable: {error}")
        self._connectError = error
        return False

    def _mockApi(self):
        from imswitch.imcontrol.model.interfaces.timetagger_mock import (
            MockTimeTaggerApi,
        )
        return MockTimeTaggerApi.from_info(self._info)

    # ------------------------------------------------------------------ #
    # Feeding the mock the scan's edges                                    #
    # ------------------------------------------------------------------ #

    def _onScanBuilt(self, scanInfoDict, signalDict, _devices=None):
        """Hand the designed line/frame clocks to the mock card as edge
        timestamps. A real card sees the real cables; nothing to do."""
        if not self._isMock or self._tagger is None:
            return
        loader = getattr(self._tagger, "load_scan_edges", None)
        if loader is None:
            return
        try:
            ttl = (signalDict or {}).get("TTLCycleSignalsDict", {}) or {}
            sampleRate = float(
                getattr(getattr(self._setupInfo, "scan", None), "sampleRate", 0) or 0
            )
            if sampleRate <= 0:
                sampleRate = float((scanInfoDict or {}).get("sample_rate", 0) or 0)
            if sampleRate <= 0:
                return
            edges = {}
            samplesTotal = 0
            for role, key in (("line_clock", "line_clock"),
                              ("frame_clock", "frame_start_clock")):
                if not self.hasRole(role):
                    continue
                wave = ttl.get(key)
                if wave is None:
                    continue
                wave = np.asarray(wave).astype(np.int8, copy=False)
                samplesTotal = max(samplesTotal, int(wave.size))
                rising = np.flatnonzero(np.diff(wave, prepend=0) == 1)
                edges[abs(self.channel(role))] = (rising / sampleRate * 1e12).astype(np.int64)
            loader(edges, samplesTotal / sampleRate)
        except Exception:
            self.__logger.exception("Could not feed the scan's edges to the mock card")

    def _onScanStarted(self, *_args):
        """The scan's clocks start now: the mock card times its edges from
        here, so a measurement sees the ones that fall into its window."""
        self._mockHook("start_scan")

    def _onScanDone(self, *_args):
        """The scan's clocks have stopped: tell the mock card so its scan
        inputs count 0 Hz again (the edges stay for the final frame)."""
        self._mockHook("end_scan")

    def _mockHook(self, name: str):
        if not self._isMock or self._tagger is None:
            return
        hook = getattr(self._tagger, name, None)
        if hook is not None:
            try:
                hook()
            except Exception:
                self.__logger.exception(f"Mock card hook {name} failed")

    def ensureConnected(self):
        """Raise ``TimeTaggerError`` unless the card is open (retrying once).

        Open is enough for the diagnostics (count rates, the test signal:
        what you run to find out *why* the conditioning failed); a scan
        needs :meth:`ensureConditioned`, which :meth:`beginScanHold` calls.
        """
        if self._tagger is None and not self.connect():
            raise TimeTaggerError(
                f"Time Tagger is not available: {self._connectError}"
            ) from self._connectError

    def ensureConditioned(self):
        """Raise ``TimeTaggerError`` unless the card is open *and* conditioned.

        A card whose conditioning (trigger levels, dead times, delays, the
        conditional filter) could not be applied is retried once and
        otherwise refused: a scan on an unconditioned card would read
        unfiltered data as reverse TCSPC, or count at the wrong trigger
        level, without anyone noticing.
        """
        self.ensureConnected()
        if self._conditioningError is not None:
            with self._lock:
                self._tryApplyConditioning()
            if self._conditioningError is not None:
                raise TimeTaggerError(
                    "Time Tagger is connected but its channel conditioning "
                    f"could not be applied: {self._conditioningError}"
                ) from self._conditioningError

    @property
    def conditioningError(self) -> Optional[Exception]:
        """The error of the last failed conditioning write, else ``None``."""
        return self._conditioningError

    @property
    def conditioned(self) -> bool:
        """Whether the card carries the configured conditioning."""
        return self._tagger is not None and self._conditioningError is None

    @property
    def connected(self) -> bool:
        return self._tagger is not None

    @property
    def isMock(self) -> bool:
        return self._isMock

    @property
    def api(self):
        """The vendor module, or the mock module: ``api.Flim``, ``api.Counter``…"""
        self.ensureConnected()
        return self._api

    @property
    def tagger(self):
        """The card object measurements are built against."""
        self.ensureConnected()
        return self._tagger

    @property
    def model(self) -> str:
        if self._tagger is None:
            return ""
        try:
            return str(self._tagger.getModel())
        except Exception:
            return ""

    @property
    def serial(self) -> str:
        if self._tagger is None:
            return ""
        try:
            return str(self._tagger.getSerial())
        except Exception:
            return ""

    @property
    def info(self) -> TimeTaggerInfo:
        return self._info

    # ------------------------------------------------------------------ #
    # Roles                                                                #
    # ------------------------------------------------------------------ #

    def channels(self) -> Dict[str, ChannelInfo]:
        return dict(self._channels)

    def hasRole(self, role: str) -> bool:
        return role in self._channels

    def channelInfo(self, role: str) -> ChannelInfo:
        try:
            return self._channels[role]
        except KeyError:
            configured = ", ".join(self._channels) or "none"
            raise TimeTaggerError(
                f"Time Tagger role {role!r} is not configured "
                f"(configured roles: {configured})"
            ) from None

    def channel(self, role: str) -> int:
        """The signed channel number for a role."""
        return self.channelInfo(role).channel

    @property
    def tcspcDirection(self) -> str:
        """``'reverse'`` with the conditional filter on, else ``'forward'``.

        With the filter, only the first sync *after* each photon reaches the
        PC, so the histogram must be started by the photon and stopped by the
        sync. The FLIM detector swaps its ``Flim`` channels accordingly.
        """
        if not self._info.filterSyncByPhotons:
            return "forward"
        if self._tagger is not None and self._conditioningError is not None:
            # The filter was asked for but not applied: the card transmits
            # every sync, so the data is forward whatever the block says.
            return "forward"
        return "reverse"

    # ------------------------------------------------------------------ #
    # Conditioning                                                         #
    # ------------------------------------------------------------------ #

    def _tryApplyConditioning(self) -> bool:
        """Apply the conditioning, remembering a failure (``_lock`` held)."""
        try:
            self._applyConditioning()
        except Exception as error:
            self._conditioningError = error
            self.__logger.exception(
                "Time Tagger connected but its channel conditioning could "
                "not be applied; scans are refused until it is"
            )
            return False
        self._conditioningError = None
        return True

    def _applyConditioning(self):
        tagger = self._tagger
        for channel in self._channels.values():
            tagger.setTriggerLevel(channel.channel, channel.trigger_v)
            if channel.deadtime_ps:
                tagger.setDeadtime(channel.channel, channel.deadtime_ps)
            tagger.setInputDelay(channel.channel, self._hardwareDelayPs(channel))
        if self._info.filterSyncByPhotons:
            tagger.setConditionalFilter(
                trigger=[self.channel("photons")],
                filtered=[self.channel("laser_sync")],
            )

    def _hardwareDelayPs(self, channel: ChannelInfo) -> int:
        """The delay the card applies to an input.

        A positive line-clock delay is *not* applied here: it moves the
        detector's pixel-marker pattern instead (``patternOffsetPs``), so
        the raw line channel stays visible to the diagnostics; a negative
        one has to be a card delay. The frame clock has no pattern, so it
        always takes the full delay on the card.
        """
        if channel.role == "line_clock" and channel.delay_ps > 0:
            return 0
        return int(channel.delay_ps)

    def hardwareDelayPs(self, role: str) -> int:
        """The input delay the card carries for ``role`` (see above); the
        FLIM detector adds its ``t0_ps`` to this on the photon input."""
        return self._hardwareDelayPs(self.channelInfo(role))

    def patternOffsetPs(self, role: str = "line_clock") -> int:
        """Where a detector starts its pixel markers after an edge of ``role``:
        the block's ``pixelPatternOffsetPs``, plus the line clock's positive
        delay when the markers hang off the line clock (the frame clock has
        no pattern: its delay is on the card)."""
        offset = int(getattr(self._info, "pixelPatternOffsetPs", 0) or 0)
        if role == "line_clock" and self.hasRole(role):
            offset += max(0, int(self.channelInfo(role).delay_ps))
        return offset

    def _checkWritable(self, what: str, owner: Optional[str] = None):
        if self._holders:
            holders = ", ".join(sorted(self._holders))
            raise TimeTaggerBusyError(
                f"Cannot change the Time Tagger {what} while a scan holds the "
                f"card ({holders}); wait for the scan to end."
            )
        if self._calibrationOwner is not None and owner != self._calibrationOwner:
            raise TimeTaggerBusyError(
                f"Cannot change the Time Tagger {what}: a calibration "
                f"({self._calibrationOwner}) owns the card's conditioning."
            )

    def setTriggerLevel(self, role: str, voltage: float, *,
                        owner: Optional[str] = None) -> None:
        with self._lock:
            self._checkWritable("trigger level", owner)
            info = self.channelInfo(role)
            self.tagger.setTriggerLevel(info.channel, float(voltage))
            self._channels[role] = ChannelInfo(
                info.role, info.channel, float(voltage),
                info.deadtime_ps, info.delay_ps,
            )

    def setTestSignal(self, roles, enabled: bool, *,
                      owner: Optional[str] = None) -> None:
        """The card's built-in test signal on these roles' inputs.

        Refused while a scan holds the card or another calibration owns
        it: a test signal on the line clock during a scan would inject
        false pixel markers.
        """
        with self._lock:
            self._checkWritable("test signal", owner)
            channels = [self.channel(role) for role in roles]
            self.tagger.setTestSignal(channels, bool(enabled))

    def setDelay(self, role: str, delay_ps: int, *,
                 owner: Optional[str] = None) -> None:
        """Set a role's delay. For ``line_clock`` the frame clock follows
        (one delay for both DAQ-derived roles), and a positive value is
        applied as a pattern offset rather than a card delay."""
        with self._lock:
            self._checkWritable("input delay", owner)
            roles = [role] + (["frame_clock"] if role == "line_clock" and self.hasRole("frame_clock") else [])
            for each in roles:
                info = self.channelInfo(each)
                updated = ChannelInfo(info.role, info.channel, info.trigger_v,
                                      info.deadtime_ps, int(delay_ps))
                self.tagger.setInputDelay(info.channel, self._hardwareDelayPs(updated))
                self._channels[each] = updated

    def setDeadtime(self, role: str, deadtime_ps: int, *,
                    owner: Optional[str] = None) -> None:
        with self._lock:
            self._checkWritable("dead time", owner)
            info = self.channelInfo(role)
            self.tagger.setDeadtime(info.channel, int(deadtime_ps))
            self._channels[role] = ChannelInfo(
                info.role, info.channel, info.trigger_v,
                int(deadtime_ps), info.delay_ps,
            )

    # ------------------------------------------------------------------ #
    # Scan hold                                                            #
    # ------------------------------------------------------------------ #

    def beginScanHold(self, owner: str) -> None:
        """A detector is about to acquire: refuse conditioning until released.

        Held from scan preparation until the detector has drained its final
        frame (or torn down), not until scan-done. Refused while a
        calibration transaction owns the card: a scan must not start on
        temporary settings.
        """
        self.ensureConditioned()
        with self._lock:
            if self._calibrationOwner is not None:
                raise TimeTaggerBusyError(
                    f"The Time Tagger is held by a calibration "
                    f"({self._calibrationOwner}); the scan cannot start until "
                    f"it is finished."
                )
            self._holders.add(str(owner))

    def endScanHold(self, owner: str) -> None:
        with self._lock:
            self._holders.discard(str(owner))

    @property
    def scanHeld(self) -> bool:
        return bool(self._holders)

    @property
    def calibrationOwner(self) -> Optional[str]:
        return self._calibrationOwner

    def calibrationTransaction(self, owner: str):
        """Own the card's conditioning for a calibration, exclusively.

        While the context is held no scan can take a hold (preparation is
        refused and rolled back), and only the owner may change trigger
        levels, delays or dead times -- ``rep_rate()``'s temporary divider
        and filter changes live inside one of these. Refused while a scan
        holds the card. Always released, also when the body raises or the
        script is cancelled (``OperationCancelled`` is a ``BaseException``).
        """
        manager = self

        class _Transaction:
            def __enter__(self_):
                with manager._lock:
                    if manager._holders:
                        holders = ", ".join(sorted(manager._holders))
                        raise TimeTaggerBusyError(
                            f"Cannot calibrate the Time Tagger while a scan "
                            f"holds the card ({holders})."
                        )
                    if manager._calibrationOwner is not None:
                        raise TimeTaggerBusyError(
                            f"Another calibration ({manager._calibrationOwner}) "
                            f"owns the Time Tagger."
                        )
                    manager._calibrationOwner = str(owner)
                return manager

            def __exit__(self_, *exc):
                with manager._lock:
                    if manager._calibrationOwner == str(owner):
                        manager._calibrationOwner = None
                return False

        return _Transaction()

    # ------------------------------------------------------------------ #
    # Overflows                                                            #
    # ------------------------------------------------------------------ #

    def overflows(self) -> int:
        """The monotonic USB-overflow total, after polling the card once.

        The card's own counter is read-and-clear, so this is the one place
        that reads it. Consumers (a frame's validity, the health strip)
        keep their own baseline and compare: one consumer can never hide an
        overflow from another.
        """
        with self._lock:
            tagger = self._tagger
            if tagger is not None:
                try:
                    self._overflowTotal += int(tagger.getOverflowsAndClear())
                except Exception:
                    self.__logger.exception("Could not read the overflow counter")
            return self._overflowTotal

    # ------------------------------------------------------------------ #
    # Health                                                               #
    # ------------------------------------------------------------------ #

    def health(self, integration_s: Optional[float] = None) -> TimeTaggerHealth:
        """One look at every role's count rate and the overflow counter.

        Blocks for ``integration_s`` (default ``HEALTH_INTEGRATION_S``) on a
        real card; call it from a worker thread, never from a GUI slot.
        """
        integration = float(integration_s if integration_s is not None
                            else self.HEALTH_INTEGRATION_S)
        connected = self.connected
        rates: Dict[str, float] = {}
        overflows = 0
        if connected:
            api, tagger = self._api, self._tagger
            roles = list(self._channels)
            try:
                rate = api.Countrate(tagger, [self._channels[r].channel for r in roles])
                rate.startFor(int(max(0.001, integration) * 1e12))
                rate.waitUntilFinished()
                data = np.asarray(rate.getData(), dtype=np.float64)
                rates = {r: float(v) for r, v in zip(roles, data)}
            except Exception:
                self.__logger.exception("Health sample failed")
            total = self.overflows()
            overflows = total - self._healthOverflowSeen
            self._healthOverflowSeen = total
        filterOn = self.tcspcDirection == "reverse"
        health = TimeTaggerHealth(
            rates_hz=rates,
            overflows=overflows,
            tcspc_direction=self.tcspcDirection,
            filter_on=filterOn,
            sync_rate_label="sync (filtered)" if filterOn else "sync",
            model=self.model,
            serial=self.serial,
            is_mock=self._isMock,
            connected=connected,
            sampled_at=time.monotonic(),
        )
        self._lastHealth = health
        return health

    @property
    def lastHealth(self) -> Optional[TimeTaggerHealth]:
        return self._lastHealth

    def startHealthSampling(self, period_s: float = 2.0, callback=None) -> None:
        """Sample ``health()`` every ``period_s`` from a background thread.

        Each sample is emitted on ``sigHealth`` (a queued Qt signal: a GUI
        consumer receives it on its own thread through the event loop) and
        handed to ``callback`` on the sampler thread itself, for consumers
        without an event loop. Off by default (and in tests): a widget that
        wants a live strip turns it on; ``finalize`` and
        ``stopHealthSampling`` join it.
        """
        with self._lock:
            if self._samplerThread is not None and self._samplerThread.is_alive():
                return
            self._samplerStop.clear()
            thread = threading.Thread(
                target=self._sampleLoop,
                args=(max(0.05, float(period_s)), callback),
                name="TimeTaggerHealth", daemon=True,
            )
            self._samplerThread = thread
            thread.start()

    def _sampleLoop(self, period_s: float, callback) -> None:
        while not self._samplerStop.is_set():
            try:
                look = self.health()
                self.sigHealth.emit(look)
                if callback is not None:
                    callback(look)
            except Exception:
                self.__logger.exception("Health sampler failed")
            self._samplerStop.wait(period_s)

    def stopHealthSampling(self, timeout_s: float = 2.0) -> bool:
        """Stop the sampler and wait for it. Returns whether it is gone."""
        thread = self._samplerThread
        self._samplerStop.set()
        if thread is None:
            return True
        thread.join(timeout_s)
        alive = thread.is_alive()
        if not alive:
            self._samplerThread = None
        return not alive

    # ------------------------------------------------------------------ #
    # Metadata, lifecycle                                                  #
    # ------------------------------------------------------------------ #

    def metadata(self) -> Dict[str, Any]:
        """A JSON-ready snapshot for a measurement's metadata."""
        return {
            "model": self.model,
            "serial": self.serial,
            "is_mock": self._isMock,
            "connected": self.connected,
            "tcspc_direction": self.tcspcDirection,
            "filter_sync_by_photons": bool(self._info.filterSyncByPhotons),
            "conditioned": self.conditioned,
            "conditioning_error": (
                str(self._conditioningError) if self._conditioningError else None
            ),
            "roles": {
                role: {
                    "channel": c.channel,
                    "edge": c.edge,
                    "trigger_v": c.trigger_v,
                    "deadtime_ps": c.deadtime_ps,
                    "delay_ps": c.delay_ps,
                }
                for role, c in self._channels.items()
            },
        }

    def finalize(self) -> bool:
        """Free the card. Idempotent; a failure is logged and reported.

        The health sampler is stopped first: nothing may still be reading
        the card when it is freed."""
        if not self.stopHealthSampling():
            self.__logger.error("Health sampler did not stop; not freeing the card")
            return False
        with self._lock:
            self._finalized = True
            tagger, api = self._tagger, self._api
            self._tagger = None
            if tagger is None:
                return True
            try:
                api.freeTimeTagger(tagger)
            except Exception:
                self.__logger.exception("Failed to free the Time Tagger")
                return False
            return True


__all__ = [
    "ChannelInfo",
    "TimeTaggerHealth",
    "ROLES",
    "ROLE_FIELDS",
    "TimeTaggerBusyError",
    "TimeTaggerError",
    "TimeTaggerManager",
]
