"""One Swabian Time Tagger card, shared by every consumer in the setup.

``TimeTaggerManager`` is a low-level manager like ``NidaqManager``: built once
by ``MasterController`` from the setup's ``timeTagger`` block, handed to the
detector managers in ``lowLevelManagers``, and finalized at shutdown. It owns

* the connection (the real vendor library, or the in-process mock);
* the card's *inputs*, by name -- each a signed channel number with its
  kind, trigger level, dead time and delay -- and the *roles* (``photons``,
  ``laser_sync``, ``line_clock``, ``frame_clock``, ``sted_pulse``) as
  aliases of inputs, so every consumer names an input or a role and the
  cabling is described in one place (``TimeTaggerInfo.resolvedInputs``);
* the conditioning writes themselves, applied at connect and refused while a
  scan holds the card (a trigger-level change mid-frame corrupts the frame);
* the *scan session*: what exists once per card per scan -- the pixel
  markers every detector of the scan counts between, the card hold -- for
  however many detectors take part (``TimeTaggerScanSession``);
* a metadata snapshot that every measurement records.

It does not own the detectors' measurements: Swabian measurement objects run
concurrently on the card, so each detector builds its own ``Flim`` (or
counter) through ``api`` and ``tagger`` on the session's markers, and a
script's ``Counter`` does not disturb them.

Per input: trigger level, dead time, delay (and the ``t0`` a FLIM detector
adds to its own photon input's delay during its scan). Per card: the
conditional filter (it gates the syncs on every detector input at once, so
the TCSPC direction is the card's), the pixel-marker pattern offset, the
markers themselves, and the hold.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import numpy as np

from imswitch.imcommon.framework import Signal, SignalInterface
from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.SetupInfo import (
    TIME_TAGGER_INPUT_KINDS,
    TIME_TAGGER_ROLES,
    TimeTaggerConfigError,
    TimeTaggerInfo,
)

try:
    import TimeTagger as _VendorTimeTagger
except ImportError:  # the vendor installer provides it; pip does not
    _VendorTimeTagger = None


#: Role id -> (channel field, trigger-level field, dead-time field,
#: delay field) on ``TimeTaggerInfo``. ``None`` where a role has no such
#: setting. The order is the order consumers and docs list them in. These
#: are the *legacy* fields; a block with an ``inputs`` map keeps an input's
#: settings in that input (``inputFieldPath``).
ROLE_FIELDS: Dict[str, Tuple[str, str, Optional[str], Optional[str]]] = {
    role: tuple(spec["fields"]) for role, spec in TIME_TAGGER_ROLES.items()
}

ROLES = tuple(ROLE_FIELDS)

#: Role -> the kind of input that plays it.
ROLE_KINDS: Dict[str, str] = {role: spec["kind"] for role, spec in TIME_TAGGER_ROLES.items()}

INPUT_KINDS = TIME_TAGGER_INPUT_KINDS


class TimeTaggerError(RuntimeError):
    """The card is not available, or the request is not valid for it."""


class TimeTaggerBusyError(TimeTaggerError):
    """A conditioning write was refused because a scan holds the card."""


@dataclass(frozen=True)
class ChannelInfo:
    """One input of the card.

    ``role`` is the input's *name*, kept under its old field name: in a
    block without an ``inputs`` map every input is named by the role it
    plays, so a name is a role there. ``plays`` is the role the input plays
    (``None`` for an input that plays none, e.g. the second of two
    detectors).
    """

    role: str
    channel: int
    """ Signed: negative means the falling edge of input ``abs(channel)``. """
    trigger_v: float
    deadtime_ps: int = 0
    delay_ps: int = 0
    kind: str = "detector"
    plays: Optional[str] = None

    @property
    def name(self) -> str:
        """The input's name (``role`` is the same field)."""
        return self.role

    @property
    def input(self) -> int:
        """The physical input number, edge sign stripped."""
        return abs(self.channel)

    @property
    def edge(self) -> str:
        return "falling" if self.channel < 0 else "rising"

    def conditioning(self) -> Dict[str, Any]:
        """What a recording keeps about this input."""
        return {
            "input": self.role,
            "channel": int(self.channel),
            "edge": self.edge,
            "kind": self.kind,
            "role": self.plays,
            "trigger_v": float(self.trigger_v),
            "deadtime_ps": int(self.deadtime_ps),
            "delay_ps": int(self.delay_ps),
        }


@dataclass(frozen=True)
class PixelMarkers:
    """The pixel markers of one scan: an ``EventGenerator`` pair on the line
    clock, begin and end of every pixel of a line."""

    key: tuple
    begin: Any
    end: Any
    offset_ps: int

    @property
    def channels(self) -> Tuple[int, int]:
        return int(self.begin.getChannel()), int(self.end.getChannel())


class TimeTaggerScanSession:
    """What exists once per card per scan, for every detector of the scan.

    The first detector that prepares a scan opens it; every further
    detector of the same scan joins it. It owns

    * the *pixel markers* (``pixelMarkers``): one ``EventGenerator`` pair on
      the line clock, built once for the scan however many detectors count
      between them -- or kept from the previous scan when that one ended
      cleanly with the same geometry (every participant's frame closed by
      the card);
    * the card *hold*: each participant holds the card from joining until
      it leaves (its final frame drained, or its scan aborted/stopped), so
      conditioning stays refused until the *last* one has left;
    * the *start*: every participant joins and builds its measurement while
      the scan is prepared, before the NI-DAQ starts the clocks; the markers
      only fire on line-clock edges, so all measurements start counting on
      the same first edge. A detector that would join after the clocks
      started is refused (it would miss lines and image shifted).

    A later measurement that is not a detector (a correlation, a
    coincidence counter) attaches the same way: join, build on the session's
    inputs and markers, leave when drained.
    """

    def __init__(self, manager: "TimeTaggerManager", number: int):
        self._manager = manager
        self.number = int(number)
        self._participants: Dict[str, Dict[str, Any]] = {}
        self._left: Dict[str, bool] = {}
        self._order: list = []
        self._markers: Optional[PixelMarkers] = None
        self._markersReused = False
        self.started = False
        self.closed = False

    # -- who takes part -------------------------------------------------- #

    @property
    def participants(self) -> Tuple[str, ...]:
        """Who is in the session and has not left."""
        return tuple(self._participants)

    @property
    def everyone(self) -> Tuple[str, ...]:
        """Everyone who joined, in order, including those who left."""
        return tuple(self._order)

    def hasJoined(self, name: str) -> bool:
        return name in self._participants or name in self._left

    def leftClean(self) -> bool:
        """Whether every participant left with a frame the card closed."""
        return bool(self._left) and not self._participants and all(self._left.values())

    # -- the markers ----------------------------------------------------- #

    def pixelMarkers(self, *, line_channel: int, n_x: int, pixel_period_ps: int,
                     pixel_width_ps: int, offset_ps: int) -> Tuple[PixelMarkers, bool]:
        """The scan's pixel markers, and whether they were kept from the
        previous scan. Built on the first call of the scan; every further
        participant gets the same pair, and a participant asking for
        another geometry is refused (one scan, one raster)."""
        return self._manager._sessionMarkers(
            self, (int(line_channel), int(n_x), int(pixel_period_ps),
                   int(pixel_width_ps), int(offset_ps)))

    @property
    def markers(self) -> Optional[PixelMarkers]:
        return self._markers

    def summary(self) -> Dict[str, Any]:
        markers = self._markers
        return {
            "session": self.number,
            "participants": list(self.everyone),
            "pixel_marker_channels": list(markers.channels) if markers is not None else None,
            "started": bool(self.started),
        }


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
        #: The card's I/O thread (``submit``), created on first use.
        self._io: Optional[ThreadPoolExecutor] = None
        self._ioLock = threading.Lock()
        self._ioClosed = False

        self._samplerThread: Optional[threading.Thread] = None
        self._samplerStop = threading.Event()
        self._lastHealth: Optional[TimeTaggerHealth] = None

        #: The scan session in progress (or the last one), and the markers
        #: kept for a repeat scan of the same geometry.
        self._session: Optional[TimeTaggerScanSession] = None
        self._sessionCount = 0
        self._scanEpoch = 0
        self._sessionEpoch = -1
        self._markerCache: Optional[PixelMarkers] = None
        self._markerCacheClean = False

        # Raises TimeTaggerConfigError (a ValueError) with what to change.
        resolved = info.resolvedInputs()
        #: Input name -> ChannelInfo, in declaration order. Without an
        #: ``inputs`` map the names are the roles (the older form).
        self._channels: Dict[str, ChannelInfo] = {}
        for name, spec in resolved.inputs.items():
            self._channels[name] = ChannelInfo(
                role=name,
                channel=int(spec.channel),
                trigger_v=float(spec.triggerV),
                deadtime_ps=int(spec.deadtimePs),
                delay_ps=int(spec.delayPs),
                kind=str(spec.kind),
                plays=resolved.role(name),
            )
        #: Role -> input name, for the roles some input plays.
        self._roles: Dict[str, str] = dict(resolved.roles)
        self._fromInputsBlock = bool(resolved.fromInputsBlock)

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
                f"{' [mock]' if self._isMock else ''}; "
                + ("inputs " if self._fromInputsBlock else "roles ")
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
        """A new scan is being prepared: the detectors that prepare it next
        join a new scan session. On the mock, hand it the designed line and
        frame clocks as edge timestamps (a real card sees the real cables).

        Connected before any detector's own slot (the detectors are built
        after the low-level managers), so the epoch moves before the first
        detector joins."""
        with self._lock:
            self._scanEpoch += 1
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
            # The NI-DAQ plays its digital lines -- the line and frame clocks
            # among them -- the scan's phase delay later than designed, so
            # they follow the beam (scan timing, S5): the real card sees
            # them that much later, and the mock with it.
            played = float(((scanInfoDict or {}).get("lag") or {}).get("gate_delay_s", 0) or 0)
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
                edges[abs(self.channel(role))] = (
                    (rising / sampleRate + played) * 1e12).astype(np.int64)
            loader(edges, samplesTotal / sampleRate)
        except Exception:
            self.__logger.exception("Could not feed the scan's edges to the mock card")

    def _onScanStarted(self, *_args):
        """The scan's clocks start now: the mock card times its edges from
        here, so a measurement sees the ones that fall into its window.
        The session's participants are all armed by now; it is started."""
        with self._lock:
            if self._session is not None and not self._session.closed:
                self._session.started = True
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
        """Every input, by name, in declaration order (without an ``inputs``
        map the names are the roles)."""
        return dict(self._channels)

    def inputs(self) -> Tuple[str, ...]:
        """The input names, in declaration order."""
        return tuple(self._channels)

    def roles(self) -> Dict[str, str]:
        """Role -> the input that plays it, for the roles some input plays."""
        return dict(self._roles)

    @property
    def hasInputsBlock(self) -> bool:
        """Whether the block declared its inputs in an ``inputs`` map."""
        return self._fromInputsBlock

    def inputName(self, nameOrRole: str) -> str:
        """The input a name or a role refers to (an input name wins)."""
        if nameOrRole in self._channels:
            return nameOrRole
        try:
            return self._roles[nameOrRole]
        except KeyError:
            configured = ", ".join(self._channels) or "none"
            roles = ", ".join(f"{r}={n}" for r, n in self._roles.items() if r != n)
            raise TimeTaggerError(
                f"Time Tagger role {nameOrRole!r} is not configured "
                f"(configured {'inputs' if self._fromInputsBlock else 'roles'}: {configured}"
                + (f"; roles: {roles}" if roles else "") + ")"
            ) from None

    def hasRole(self, role: str) -> bool:
        """Whether a role -- or an input of that name -- is configured."""
        return role in self._channels or role in self._roles

    hasInput = hasRole

    def inputsOfKind(self, kind: str) -> Tuple[str, ...]:
        return tuple(name for name, c in self._channels.items() if c.kind == kind)

    def channelInfo(self, role: str) -> ChannelInfo:
        """An input by its name or by the role it plays."""
        return self._channels[self.inputName(role)]

    def channel(self, role: str) -> int:
        """The signed channel number for an input or a role."""
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
            trigger, filtered = self.filterChannels()
            tagger.setConditionalFilter(trigger=trigger, filtered=filtered)

    def filterChannels(self) -> Tuple[list, list]:
        """The conditional filter's ``(trigger, filtered)`` channel lists:
        every detector input lets through the first sync after each of its
        photons, so every FLIM detector on the card has its sync; the
        filtered inputs are the laser syncs. Per card: one filter, so the
        TCSPC direction is the card's, not a detector's.

        Without an ``inputs`` map this is exactly ``[photons]`` and
        ``[laser_sync]``, as it always was."""
        if not self._fromInputsBlock:
            return [self.channel("photons")], [self.channel("laser_sync")]
        trigger = [c.channel for c in self._channels.values() if c.kind == "detector"]
        filtered = [c.channel for c in self._channels.values() if c.kind == "laser_sync"]
        if not trigger or not filtered:
            raise TimeTaggerError(
                "filterSyncByPhotons needs at least one detector input and one "
                "laser_sync input in the timeTagger block"
            )
        return trigger, filtered

    def _hardwareDelayPs(self, channel: ChannelInfo) -> int:
        """The delay the card applies to an input.

        A positive line-clock delay is *not* applied here: it moves the
        detector's pixel-marker pattern instead (``patternOffsetPs``), so
        the raw line channel stays visible to the diagnostics; a negative
        one has to be a card delay. The frame clock has no pattern, so it
        always takes the full delay on the card.
        """
        if channel.kind == "line_clock" and channel.delay_ps > 0:
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
        if self.hasRole(role):
            info = self.channelInfo(role)
            if info.kind == "line_clock":
                offset += max(0, int(info.delay_ps))
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

    def assertWritable(self, what: str = "trigger level",
                       owner: Optional[str] = None) -> None:
        """Raise ``TimeTaggerBusyError`` now if a conditioning write would
        be refused (a scan hold, another calibration) -- no card call, so a
        GUI slot can refuse at once and queue only a write that can land.

        Advisory and lock-free: it must not wait behind a write in flight on
        the I/O thread (which holds the lock across the card call); the
        write itself checks again under the lock."""
        self._checkWritable(what, owner)

    def setTriggerLevel(self, role: str, voltage: float, *,
                        owner: Optional[str] = None) -> None:
        with self._lock:
            self._checkWritable("trigger level", owner)
            info = self.channelInfo(role)
            self.tagger.setTriggerLevel(info.channel, float(voltage))
            self._channels[info.role] = dataclasses.replace(info, trigger_v=float(voltage))

    # ------------------------------------------------------------------ #
    # Inputs: a role or a bare channel                                     #
    # ------------------------------------------------------------------ #

    def resolveInput(self, input) -> Tuple[int, Optional[str]]:
        """``(signed channel, role or None)`` for a role name or a channel.

        Level finding and the level warnings work per input: a configured
        role is a label and a source of hints, not a requirement. A channel
        number that is a role's input (either edge) resolves to that role.
        """
        if isinstance(input, str):
            info = self.channelInfo(input)
            return info.channel, info.role
        channel = int(input)
        if channel == 0:
            raise TimeTaggerError("Time Tagger inputs are numbered from 1 (negative = falling edge)")
        for info in self._channels.values():
            if abs(info.channel) == abs(channel):
                return info.channel, info.role
        return channel, None

    def triggerLevelOf(self, input) -> float:
        """The trigger level an input carries: the role's, or the card's."""
        channel, role = self.resolveInput(input)
        if role is not None:
            return self.channelInfo(role).trigger_v
        return float(self.tagger.getTriggerLevel(channel))

    def setInputTriggerLevel(self, input, voltage: float, *,
                             owner: Optional[str] = None) -> None:
        """``setTriggerLevel`` for any input; a role's input updates the role."""
        channel, role = self.resolveInput(input)
        if role is not None:
            self.setTriggerLevel(role, voltage, owner=owner)
            return
        with self._lock:
            self._checkWritable("trigger level", owner)
            self.tagger.setTriggerLevel(channel, float(voltage))

    # ------------------------------------------------------------------ #
    # Card calls off the caller's thread                                   #
    # ------------------------------------------------------------------ #

    #: How long ``finalize`` waits for queued card calls before freeing it.
    IO_DRAIN_S = 5.0

    def submit(self, function, *args, **kwargs) -> Future:
        """Run ``function`` on the card's own I/O thread; returns a Future.

        One thread, in submission order: a GUI slot hands it a conditioning
        write or the release of a measurement object and returns at once,
        and two writes can never overtake each other. ``finalize`` drains it
        before the card is freed.
        """
        with self._ioLock:
            if self._ioClosed:
                raise TimeTaggerError("TimeTaggerManager has been finalized")
            if self._io is None:
                self._io = ThreadPoolExecutor(max_workers=1, thread_name_prefix="TimeTaggerIO")
            return self._io.submit(function, *args, **kwargs)

    def setTriggerLevelAsync(self, role: str, voltage: float, *,
                             owner: Optional[str] = None) -> Future:
        """``setTriggerLevel`` on the I/O thread (``setInputTriggerLevel``
        semantics: a role or a channel). Refusals (busy, not connected)
        arrive as the Future's exception."""
        return self.submit(self.setInputTriggerLevel, role, voltage, owner=owner)

    def releaseLater(self, box: list) -> None:
        """Drop the measurement objects in ``box`` on the I/O thread.

        A vendor ``Flim`` frees hundreds of megabytes and unregisters from
        the card when its last reference goes; the caller hands over its
        only references in a list (``releaseLater([old])`` after setting its
        own attribute to ``None``) so that happens here, not on the GUI
        thread. Falls back to dropping them in place after ``finalize``.
        """
        try:
            self.submit(box.clear)
        except Exception:
            box.clear()

    def _drainIo(self, timeout_s: float) -> bool:
        if "_ioLock" not in self.__dict__:   # built without __init__ (tests)
            return True
        with self._ioLock:
            self._ioClosed = True
            executor, self._io = self._io, None
        if executor is None:
            return True
        try:
            done = executor.submit(lambda: None)
            done.result(timeout=max(0.0, float(timeout_s)))
            drained = True
        except Exception:
            drained = False
        executor.shutdown(wait=False)
        return drained

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
        """Set an input's delay. For the line clock the frame clock follows
        (one delay for both DAQ-derived clocks), and a positive value is
        applied as a pattern offset rather than a card delay."""
        with self._lock:
            self._checkWritable("input delay", owner)
            for each in self._withFrameClock(role):
                info = self._channels[each]
                updated = dataclasses.replace(info, delay_ps=int(delay_ps))
                self.tagger.setInputDelay(info.channel, self._hardwareDelayPs(updated))
                self._channels[each] = updated

    def setDeadtime(self, role: str, deadtime_ps: int, *,
                    owner: Optional[str] = None) -> None:
        """Set an input's dead time. For the line clock the frame clock
        follows (one dead time for both DAQ-derived clocks, as with the
        delay: the same driver, the same bounce)."""
        with self._lock:
            self._checkWritable("dead time", owner)
            for each in self._withFrameClock(role):
                info = self._channels[each]
                self.tagger.setDeadtime(info.channel, int(deadtime_ps))
                self._channels[each] = dataclasses.replace(info, deadtime_ps=int(deadtime_ps))

    def _withFrameClock(self, role: str) -> list:
        """The input ``role`` names, and the frame clock with it when it is
        the line clock."""
        name = self.inputName(role)
        names = [name]
        if self._channels[name].kind == "line_clock":
            names += [n for n in self.inputsOfKind("frame_clock") if n != name]
        return names

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

    # ------------------------------------------------------------------ #
    # Scan session: what exists once per card per scan                     #
    # ------------------------------------------------------------------ #

    def joinScanSession(self, participant: str) -> TimeTaggerScanSession:
        """Take part in the scan being prepared; returns its session.

        The first participant of a scan opens a new session; the others of
        the same scan join it. A scan is told apart from the next by the
        NI-DAQ's ``sigScanBuilt`` (the epoch) or, without one, by a
        participant joining again. Takes the card hold for ``participant``
        (``beginScanHold``: refused on an unconditioned card or during a
        calibration); ``leaveScanSession`` gives it back.
        """
        name = str(participant)
        self.beginScanHold(name)
        with self._lock:
            session = self._session
            fresh = (
                session is None or session.closed
                or session.hasJoined(name)
                or self._sessionEpoch != self._scanEpoch
            )
            if not fresh and session.started:
                self._holders.discard(name)
                raise TimeTaggerError(
                    f"{name} cannot join the Time Tagger scan session after its clocks "
                    f"started ({', '.join(session.everyone)} are acquiring): it would "
                    "miss the first lines"
                )
            if fresh:
                if session is not None and not session.closed:
                    # The previous scan's participants that never left (a
                    # detector removed from the scan, a crash) hold nothing
                    # of the new one; its markers are not known to be clean.
                    self._closeSession(session, clean=False)
                self._sessionCount += 1
                session = TimeTaggerScanSession(self, self._sessionCount)
                self._session = session
                self._sessionEpoch = self._scanEpoch
            session._participants[name] = {"joined": time.monotonic()}
            session._order.append(name)
            return session

    def leaveScanSession(self, participant: str, *, clean: bool = False,
                         session: Optional[TimeTaggerScanSession] = None) -> None:
        """``participant`` is done with its scan: its final frame drained
        (``clean`` when the card closed it), or it was aborted or stopped.
        Its hold is released; when the last participant has left the
        session closes, and its markers are kept for a repeat scan only if
        every participant left clean. Idempotent; a ``session`` other than
        the current one (a late leave from an old scan) touches nothing of
        the current one but the hold it never had."""
        name = str(participant)
        with self._lock:
            current = self._session
            target = session if session is not None else current
            if target is not None and name in target._participants:
                del target._participants[name]
                target._left[name] = bool(clean)
                if not target._participants and not target.closed:
                    self._closeSession(target, clean=target.leftClean())
            # A late leave from an old session must not drop the hold the
            # same participant took for the current one.
            stillInCurrent = (current is not None and target is not current
                              and name in current._participants)
            if not stillInCurrent:
                self._holders.discard(name)

    def _closeSession(self, session: TimeTaggerScanSession, *, clean: bool) -> None:
        """``_lock`` held."""
        session.closed = True
        if session._markers is not None and session._markers is self._markerCache:
            self._markerCacheClean = bool(clean)

    @property
    def scanSession(self) -> Optional[TimeTaggerScanSession]:
        """The current (or the last) scan session."""
        return self._session

    def _sessionMarkers(self, session: TimeTaggerScanSession, key: tuple):
        """The pixel markers for ``session``: its own if it has them, the
        cached pair of a clean previous scan of the same geometry, or a new
        pair (the old one freed on the I/O thread)."""
        with self._lock:
            markers = session._markers
            if markers is not None:
                if markers.key != key:
                    raise TimeTaggerError(
                        "Every detector of one scan counts between the same pixel "
                        f"markers; this scan's are {markers.key}, asked for {key} "
                        "(a different line clock, raster or pattern offset)"
                    )
                return markers, session._markersReused
            cache = self._markerCache
            if cache is not None and cache.key == key and self._markerCacheClean:
                self._markerCacheClean = False  # in use until this session closes clean
                session._markers = cache
                session._markersReused = True
                return cache, True
            self._markerCache = None
            self._markerCacheClean = False
            if cache is not None:
                self.releaseLater([cache.begin, cache.end])
                cache = None
            line_channel, n_x, period_ps, width_ps, offset_ps = key
            begin_pattern = (np.int64(offset_ps)
                             + np.arange(n_x, dtype=np.int64) * np.int64(period_ps))
            end_pattern = begin_pattern + np.int64(width_ps)
            api, tagger = self.api, self.tagger
            # No output channel: the library assigns virtual channel ids that
            # never collide with a physical input.
            begin = api.EventGenerator(tagger, int(line_channel), begin_pattern)
            end = api.EventGenerator(tagger, int(line_channel), end_pattern)
            markers = PixelMarkers(key=key, begin=begin, end=end, offset_ps=int(offset_ps))
            self._markerCache = markers
            session._markers = markers
            session._markersReused = False
            return markers, False

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
            # By role (the older form's keys; with an inputs map, the roles
            # some input plays), and by input.
            "roles": {
                role: {
                    "channel": c.channel,
                    "edge": c.edge,
                    "trigger_v": c.trigger_v,
                    "deadtime_ps": c.deadtime_ps,
                    "delay_ps": c.delay_ps,
                }
                for role, c in ((role, self._channels[name]) for role, name in self._roles.items())
            },
            "inputs": {name: c.conditioning() for name, c in self._channels.items()},
            "inputs_block": bool(self._fromInputsBlock),
            "filter_channels": (
                [list(x) for x in self.filterChannels()]
                if self._info.filterSyncByPhotons and self._safeFilterChannels() else None
            ),
        }

    def _safeFilterChannels(self) -> bool:
        try:
            self.filterChannels()
            return True
        except TimeTaggerError:
            return False

    def finalize(self) -> bool:
        """Free the card. Idempotent; a failure is logged and reported.

        The health sampler is stopped and the I/O thread drained first:
        nothing may still be reading the card, or holding a measurement
        object on it, when it is freed."""
        if not self.stopHealthSampling():
            self.__logger.error("Health sampler did not stop; not freeing the card")
            return False
        if not self._drainIo(self.IO_DRAIN_S):
            self.__logger.error("Queued Time Tagger calls did not finish; not freeing the card")
            return False
        with self._lock:
            self._finalized = True
            tagger, api = self._tagger, self._api
            self._tagger = None
            self._markerCache = None
            self._session = None
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
    "INPUT_KINDS",
    "PixelMarkers",
    "ROLE_KINDS",
    "TimeTaggerConfigError",
    "TimeTaggerHealth",
    "ROLES",
    "ROLE_FIELDS",
    "TimeTaggerBusyError",
    "TimeTaggerError",
    "TimeTaggerManager",
    "TimeTaggerScanSession",
]
