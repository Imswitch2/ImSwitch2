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
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

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


class TimeTaggerManager:
    """See the module docstring.

    ``setupInfo`` and ``nidaqManager`` are optional so a detector can build a
    private instance from its own properties (the compatibility path for
    setups without a ``timeTagger`` block) and so tests can construct one
    bare. With a ``nidaqManager``, the mock fallback is honoured only while
    the NI-DAQ is simulated too.
    """

    def __init__(self, info: TimeTaggerInfo, setupInfo=None, nidaqManager=None,
                 api=None):
        self.__logger = initLogger(self)
        self._info = info
        self._setupInfo = setupInfo
        self._nidaqManager = nidaqManager
        self._api = api  # injected vendor-or-mock module (tests, mock rigs)
        self._tagger = None
        self._isMock = False
        self._connectError: Optional[Exception] = None
        self._lock = threading.RLock()
        self._holders: set = set()
        self._finalized = False

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
            try:
                self._applyConditioning()
            except Exception:
                self.__logger.exception(
                    "Time Tagger connected but its channel conditioning "
                    "could not be applied"
                )
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
            self._applyConditioning()
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

    @staticmethod
    def _mockApi():
        from imswitch.imcontrol.model.interfaces.timetagger_mock import (
            MockTimeTaggerApi,
        )
        return MockTimeTaggerApi()

    def ensureConnected(self):
        """Raise ``TimeTaggerError`` unless the card is open (retrying once)."""
        if self._tagger is None and not self.connect():
            raise TimeTaggerError(
                f"Time Tagger is not available: {self._connectError}"
            ) from self._connectError

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
        return "reverse" if self._info.filterSyncByPhotons else "forward"

    # ------------------------------------------------------------------ #
    # Conditioning                                                         #
    # ------------------------------------------------------------------ #

    def _applyConditioning(self):
        tagger = self._tagger
        for channel in self._channels.values():
            tagger.setTriggerLevel(channel.channel, channel.trigger_v)
            if channel.deadtime_ps:
                tagger.setDeadtime(channel.channel, channel.deadtime_ps)
            if channel.delay_ps:
                tagger.setInputDelay(channel.channel, channel.delay_ps)
        if self._info.filterSyncByPhotons:
            tagger.setConditionalFilter(
                trigger=[self.channel("photons")],
                filtered=[self.channel("laser_sync")],
            )

    def _checkWritable(self, what: str):
        if self._holders:
            holders = ", ".join(sorted(self._holders))
            raise TimeTaggerBusyError(
                f"Cannot change the Time Tagger {what} while a scan holds the "
                f"card ({holders}); wait for the scan to end."
            )

    def setTriggerLevel(self, role: str, voltage: float) -> None:
        with self._lock:
            self._checkWritable("trigger level")
            info = self.channelInfo(role)
            self.tagger.setTriggerLevel(info.channel, float(voltage))
            self._channels[role] = ChannelInfo(
                info.role, info.channel, float(voltage),
                info.deadtime_ps, info.delay_ps,
            )

    def setDelay(self, role: str, delay_ps: int) -> None:
        with self._lock:
            self._checkWritable("input delay")
            info = self.channelInfo(role)
            self.tagger.setInputDelay(info.channel, int(delay_ps))
            self._channels[role] = ChannelInfo(
                info.role, info.channel, info.trigger_v,
                info.deadtime_ps, int(delay_ps),
            )

    def setDeadtime(self, role: str, deadtime_ps: int) -> None:
        with self._lock:
            self._checkWritable("dead time")
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
        """A detector is about to acquire: refuse conditioning until released."""
        with self._lock:
            self._holders.add(str(owner))

    def endScanHold(self, owner: str) -> None:
        with self._lock:
            self._holders.discard(str(owner))

    @property
    def scanHeld(self) -> bool:
        return bool(self._holders)

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
        """Free the card. Idempotent; a failure is logged and reported."""
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
    "ROLES",
    "ROLE_FIELDS",
    "TimeTaggerBusyError",
    "TimeTaggerError",
    "TimeTaggerManager",
]
