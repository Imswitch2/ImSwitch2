"""MicroManagerFlipMirrorManager: a Micro-Manager State device or Shutter as an ImSwitch2 flip mirror.

A flip mirror is a switch with N named positions: two for a mirror or a shutter, more for a filter
wheel, an objective turret or a light path. They are the same thing to ImSwitch2 -- the user picks one of
N named positions -- so every Micro-Manager State device is presented through this kind.

Built on the hub :class:`~.core.MicroManagerCoreManager` and the shared plumbing
:class:`~._bridge.MicroManagerDeviceMixin`; see plan ``micromanager-bridge.md``
sections 4.2, 4.4 (flip-mirror row) and 4.7.

There is no flip-mirror base class in ImSwitch2. ``FlipMirrorsManager`` instantiates
``cls(info, name, **lowLevelManagers)`` and ``FlipMirrorController`` duck-types
``move_to(state)``, ``get_state()``, ``get_state_names()``, ``is_connected()``,
``get_cached_state()``, ``get_last_error()`` and ``finalize()``; ``FlipMirrorWidget`` draws one button
per position (a drop-down above its button limit). ``ThorlabsMFF`` is the precedent for ``invert``,
``initial_state`` and ``state_names``, which are fields of ``FlipMirrorInfo`` (not of
``managerProperties``).

Manager properties (``schemas/micromanager_flip_mirror.schema.json``):

- ``rs232device`` -- name of the ``micromanager.core`` hub in ``rs232devices``
- ``label`` -- the Micro-Manager device: a **State** device with two or more positions, or a
  **Shutter** presented as a two-state switch
- ``adapter`` + ``device`` (+ ``parent``, ``preInitProperties``) -- *inline mode* for that device
  (see :class:`~._bridge.MicroManagerDeviceMixin`)
- ``states`` (optional, State devices with exactly two positions only) -- ``[a, b]``: the two
  Micro-Manager positions (state labels or indices) used as logical state 0 and 1. Without it,
  position 0 is state 0 and position 1 is state 1. It only orders the two positions of a two-position
  device. A device with more positions exposes every position as the state of the same number, and
  ``states`` is refused there.

Logical and hardware state. For a State device with N positions, logical state ``L`` is Micro-Manager
position ``L`` (``setState`` / ``getState``). ``FlipMirrorInfo.invert`` swaps logical states 0 and 1
against the device, exactly as ``ThorlabsMFF`` does, and is meaningful for **two-state devices only**:
it is refused for a device with more positions. Slot 0 and 1 of a two-state device are the two
Micro-Manager positions in ``states`` order (State device) or *closed* and *open* (Shutter:
``setShutterOpen``).

State names (``get_state_names``). ``{i: text}`` for every position. ``FlipMirrorInfo.state_names`` wins
per state (keys ``"0"`` to ``"N-1"``); a state it does not name is called what Micro-Manager calls the
position behind it: its state label (State device), or ``Closed`` / ``Open`` (Shutter -- the words of
Micro-Manager's own ``getShutterOpen``). The names describe the physical position, so with ``invert``
they swap along with the states. They are read once, when the controller builds its widget; a manager
that was not connected then names the states ``"0"`` and ``"1"`` (and any further state the setup
names). The number of positions is fixed at startup: a reconnect that finds a different count is an
error of this manager, because the widget was built for the old one.

Failure convention (the ThorlabsMFF one, which the controller expects). ``move_to`` and ``get_state``
**raise** -- ``DeviceNotConnectedError`` (a ``RuntimeError``) when the device is not there, otherwise
the reason -- and the controller logs it. A move is waited for (the hub lock is *not* held while
waiting) and **read back**: a device that does not show the new position is a failed move, not a
successful one. The reason of the last failure is :meth:`get_last_error`. A refused command does not
change the connection status.

``initial_state`` is applied once, at startup, when the device is there; it must be a position of the
device (``0`` to ``N-1``). A move that fails then is a connection error of this manager (the mirror is
in an unknown place); nothing replays it on reconnect: the controller's contract is that a reconnect
only observes the mirror.

Lifecycle. The manager keeps the default backend-holder lifecycle of ``DeviceManagerStatusMixin`` (the
other three bridged managers do the same): a reconnect of the shared hub runs through the lifecycle
service's transport path, which calls :meth:`_onTransportReconnected` of every dependent. A flip mirror
has no safe state to move to, so ``_lifecycleSafeState`` moves nothing. ``ThorlabsMFF`` returns a
lifecycle object of its own because it has no hub to share.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.devices.status import DeviceManagerStatusMixin, DeviceNotConnectedError

from ._bridge import MicroManagerDeviceMixin

#: Positions a switch needs: with fewer than two there is nothing to switch between.
MIN_POSITIONS = 2

#: The one count for which ``invert`` and ``states`` mean something: both pick which of *two* positions
#: is logical state 0 and which is 1.
TWO_STATE = 2

KIND_STATE = 'state'
KIND_SHUTTER = 'shutter'

#: What a Shutter's two slots are called when ``state_names`` does not say. Micro-Manager's own
#: words: ``getShutterOpen`` is a boolean, *closed* is slot 0 and *open* is slot 1.
SHUTTER_LABELS = ('Closed', 'Open')


@dataclass(frozen=True)
class _Adopted:
    """What was read from Micro-Manager when the device was adopted."""

    kind: str
    #: Micro-Manager state index behind each hardware slot (State device); empty for a Shutter.
    positions: Tuple[int, ...]
    #: Micro-Manager's word for each hardware slot (two for a Shutter, one per position of a State device).
    labels: Tuple[str, ...]
    model: str

    @property
    def count(self) -> int:
        """Number of positions (states ``0`` to ``count - 1``)."""
        return len(self.labels)


def _is_index(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class MicroManagerFlipMirrorManager(MicroManagerDeviceMixin, DeviceManagerStatusMixin):
    """A Micro-Manager State device (any number of positions >= 2) or Shutter as a flip mirror."""

    BRIDGE_MANAGER_ID = 'micromanager.flip-mirror'
    BRIDGE_CATEGORY = 'flip_mirror'
    BRIDGE_DEVICE_NOUN = 'flip mirror'
    BRIDGE_DEVICE_TYPES = ('State', 'Shutter')
    BRIDGE_TYPE_NOUN = 'State or Shutter'

    def __init__(self, flipMirrorInfo, name, **lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)
        self.name = name
        properties = dict(flipMirrorInfo.managerProperties or {})
        self._bridge_identify(properties, name, self.__logger)
        self.invert = bool(flipMirrorInfo.invert)
        self.initial_state = self._parse_initial_state(flipMirrorInfo.initial_state)
        self._configured_names = self._parse_state_names(flipMirrorInfo.state_names)
        self._states = self._parse_states(properties.get('states'))
        self._bridge_attach(properties, lowLevelManagers)

        self._adopted: Optional[_Adopted] = None
        self._ready = False
        self._last_state: Optional[int] = None
        self._last_error: Optional[str] = None

        self._bridge_install_backend(flipMirrorInfo)
        try:
            adopted = self._bridge_adopt(self._adopt)
        except BaseException:
            self._unload_inline()            # a setup error must not leave the device loaded in the core
            raise
        if adopted is not None:
            self._adopted = adopted
            self._ready = True
            self._setConnected(f'{adopted.model}: {self._label} connected')
            self._read_initial_state()
            self._apply_initial_state()

    # ------------------------------------------------------------------ setup
    def _where(self) -> str:
        return f"flip_mirror {self._setupName!r}"

    def _parse_initial_state(self, value) -> Optional[int]:
        """The shape of ``initial_state``; checking it against the device's position count needs the device."""
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{self._where()}: initial_state must be a state number (0, 1, ...) or null, got {value!r}")
        return int(value)

    def _parse_state_names(self, names) -> Dict[int, str]:
        """``{logical state: text}`` of the names the setup gives; the states it does not name are absent.
        Whether each key is a position of the device is checked at adoption."""
        if not names:
            return {}
        if not isinstance(names, dict):
            raise ValueError(f"{self._where()}: state_names must be an object like {{\"0\": \"..\", \"1\": \"..\"}}")
        parsed: Dict[int, str] = {}
        for key, text in names.items():
            if isinstance(key, bool) or not (isinstance(key, int) or (isinstance(key, str) and key.isdigit())):
                raise ValueError(
                    f"{self._where()}: state_names has the key {key!r}; the keys are state numbers "
                    f"(\"0\", \"1\", ...)")
            parsed[int(key)] = str(text)
        return parsed

    def _parse_states(self, states) -> Optional[Tuple[Any, Any]]:
        """The shape of ``managerProperties.states``; resolving it needs the device."""
        if states is None:
            return None
        where = f"{self._where()}: managerProperties.states"
        if not isinstance(states, (list, tuple)) or len(states) != TWO_STATE:
            raise ValueError(
                f"{where} must list exactly {TWO_STATE} Micro-Manager positions (state labels or "
                f"indices), got {states!r}")
        for entry in states:
            if not ((isinstance(entry, str) and entry) or _is_index(entry)):
                raise ValueError(f"{where} entries must be a state label (non-empty string) or a state index "
                                 f"(whole number >= 0), got {entry!r}")
        if states[0] == states[1]:
            raise ValueError(f"{where} names the same position twice ({states[0]!r}); the two must differ")
        return tuple(states)

    # --------------------------------------------------------------- adoption
    def _missing_message(self, label: str) -> str:
        host = self._host
        from pymmcore_plus import DeviceType

        loaded = host.labels_of_type(DeviceType.State) + host.labels_of_type(DeviceType.Shutter)
        near = difflib.get_close_matches(label, loaded, n=1)
        hint = f" Did you mean {near[0]!r}?" if near else ''
        return f"Micro-Manager has no State or Shutter device {label!r}.{hint} Loaded State and Shutter devices: {loaded}"

    def _adopt(self) -> _Adopted:
        """Resolve the label and check it is a flip mirror; what it is. An inline device loaded by
        this call is unloaded again when anything fails."""
        from pymmcore_plus import DeviceType

        loaded_before = self._inline_loaded
        try:
            with self._host.lock:
                device_type = self._resolve_label()
                core = self._host.core
                label = self._label
                model = f'{core.getDeviceLibrary(label)}/{core.getDeviceName(label)}'
                if device_type is DeviceType.Shutter:
                    if self._states is not None:
                        raise ValueError(
                            f"{self._where()}: managerProperties.states is for State devices; {label!r} is a "
                            f"Shutter (closed is state 0, open is state 1; use invert to swap them)")
                    adopted = _Adopted(KIND_SHUTTER, (), SHUTTER_LABELS, model)
                else:
                    adopted = self._adopt_state_device(core, model)
            self._check_setup_against(adopted)
            return adopted
        except BaseException:
            if self._inline_loaded and not loaded_before:
                self._unload_inline()
            raise

    def _adopt_state_device(self, core, model: str) -> _Adopted:
        label = self._label
        count = int(core.getNumberOfStates(label))
        if count < MIN_POSITIONS:
            raise ValueError(
                f"{self._where()}: Micro-Manager device {label!r} has {count} positions; a flip mirror "
                f"needs at least {MIN_POSITIONS}")
        labels = list(core.getStateLabels(label))
        if count != TWO_STATE:
            if self._states is not None:
                raise ValueError(
                    f"{self._where()}: managerProperties.states only orders the two positions of a "
                    f"two-position device; {label!r} has {count} positions and every position is exposed "
                    f"as the state of the same number. Remove states (state_names renames positions)")
            return _Adopted(KIND_STATE, tuple(range(count)), tuple(labels), model)
        if self._states is None:
            positions: Tuple[int, ...] = tuple(range(TWO_STATE))
        else:
            positions = tuple(self._position_of(ref, labels, count) for ref in self._states)
            if positions[0] == positions[1]:
                raise ValueError(
                    f"{self._where()}: managerProperties.states {list(self._states)!r} are both position "
                    f"{positions[0]} of {label!r}; the two must differ. Its state labels are {labels}")
        return _Adopted(KIND_STATE, positions, (labels[positions[0]], labels[positions[1]]), model)

    def _check_setup_against(self, adopted: _Adopted) -> None:
        """The setup fields that depend on the number of positions, checked once it is known."""
        count, label = adopted.count, self._label
        if self.invert and count != TWO_STATE:
            raise ValueError(
                f"{self._where()}: invert swaps the two states of a two-state device; {label!r} has {count} "
                f"positions. Remove invert (state_names renames positions)")
        if self.initial_state is not None and self.initial_state >= count:
            raise ValueError(
                f"{self._where()}: initial_state {self.initial_state} is not a position of {label!r}, which "
                f"has {count} positions (0 to {count - 1})")
        beyond = sorted(state for state in self._configured_names if state >= count)
        if beyond:
            raise ValueError(
                f"{self._where()}: state_names has the key {str(beyond[0])!r}; {label!r} has {count} positions, "
                f"the states {[str(i) for i in range(count)]}")

    def _position_of(self, ref, labels: List[str], count: int) -> int:
        label = self._label
        if isinstance(ref, str):
            if ref not in labels:
                near = difflib.get_close_matches(ref, labels, n=1)
                hint = f" Did you mean {near[0]!r}?" if near else ''
                raise ValueError(
                    f"{self._where()}: Micro-Manager device {label!r} has no state labelled {ref!r} "
                    f"(managerProperties.states).{hint} Its state labels are {labels}")
            return labels.index(ref)
        if ref >= count:
            raise ValueError(
                f"{self._where()}: Micro-Manager device {label!r} has {count} positions (0 to {count - 1}); "
                f"managerProperties.states has {ref}. Its state labels are {labels}")
        return int(ref)

    # ------------------------------------------------------------- state maps
    def _to_hw_state(self, state) -> int:
        """The hardware slot behind logical ``state``. Needs the adopted device (the position count)."""
        state = int(state)
        count = self._adopted.count
        if not 0 <= state < count:
            raise ValueError(f"Flip mirror state must be a position 0 to {count - 1}, got {state}")
        return 1 - state if self.invert else state

    def _from_hw_state(self, slot: int) -> int:
        return 1 - int(slot) if self.invert else int(slot)

    def get_state_names(self) -> Dict[int, str]:
        """``{state: text}`` for every position: the setup's ``state_names``, else Micro-Manager's own words."""
        adopted = self._adopted
        if adopted is None:
            # Not adopted: the count is unknown. The two classic states, plus any the setup names.
            count = max(MIN_POSITIONS, max(self._configured_names, default=-1) + 1)
            return {state: self._configured_names.get(state, str(state)) for state in range(count)}
        names: Dict[int, str] = {}
        for state in range(adopted.count):
            if state in self._configured_names:
                names[state] = self._configured_names[state]
            else:
                names[state] = adopted.labels[self._to_hw_state(state)]
        return names

    # -------------------------------------------------------------- hardware
    def _read_slot(self, core) -> int:
        """The hardware slot the device shows now. Hub lock held."""
        adopted = self._adopted
        if adopted.kind == KIND_SHUTTER:
            return int(bool(core.getShutterOpen(self._label)))
        position = int(core.getState(self._label))
        if position not in adopted.positions:
            raise RuntimeError(
                f"{self._label!r} shows position {position}, which this flip mirror does not use "
                f"(it uses {list(adopted.positions)})")
        return adopted.positions.index(position)

    def _command(self, slot: int) -> None:
        """Send the move for a hardware slot and wait for the device. Hub lock NOT held while waiting."""
        host, label = self._use('move')
        adopted = self._adopted
        with host.lock:
            if adopted.kind == KIND_SHUTTER:
                host.core.setShutterOpen(label, bool(slot))
            else:
                host.core.setState(label, adopted.positions[slot])
        self._wait_until_idle(host, label)

    def _record_failure(self, exc: BaseException) -> None:
        self._last_error = f'{type(exc).__name__}: {exc}'

    def _read_initial_state(self) -> None:
        """Remember the state the device is in at startup (the cache the controller reads)."""
        try:
            self.get_state()
        except Exception as exc:                          # noqa: BLE001 - recorded as the last error
            self.__logger.error('Flip mirror %s: reading the initial state failed: %s', self._setupName, exc)

    def _apply_initial_state(self) -> None:
        if self.initial_state is None:
            return
        try:
            self.move_to(self.initial_state)
        except Exception as exc:                          # noqa: BLE001 - the mirror is in an unknown place
            self.__logger.error('Flip mirror %s: moving to initial_state %s failed: %s',
                                self._setupName, self.initial_state, exc)
            self._setConnectionError(
                exc, summary=f'{self._label}: initial_state {self.initial_state} could not be applied')

    # ----------------------------------------------------- flip-mirror contract
    def is_connected(self) -> bool:
        """Whether commands reach the device: adopted, status not failed, backend real, hub connected."""
        return bool(self._ready and self.isUsable and self.backendIsReal and self._hub.isConnected)

    def get_last_error(self) -> Optional[str]:
        """Why the last command failed, else the connection problem (``None`` when there is none)."""
        if self._last_error:
            return self._last_error
        if not self.is_connected():
            return self.connectionStatusDetails or self.connectionStatusSummary
        return None

    def get_cached_state(self) -> Optional[int]:
        """The last logical state observed or commanded (``None`` before the first)."""
        return self._last_state

    def move_to(self, state) -> None:
        """Move to a logical state (``0`` to ``N-1``), wait for the device and check it shows the new position."""
        if self._adopted is None:
            self._use('move')                                 # raises DeviceNotConnectedError
            raise DeviceNotConnectedError(f"{self.name}: move refused, the device was never adopted")
        slot = self._to_hw_state(state)
        logical = int(state)
        try:
            self._command(slot)
            host, _label = self._use('read back')
            with host.lock:
                shown = self._read_slot(host.core)
            if shown != slot:
                raise RuntimeError(
                    f"{self._label!r} was told to move to state {logical} but shows state {self._from_hw_state(shown)}")
        except DeviceNotConnectedError:
            raise
        except Exception as exc:
            self._record_failure(exc)
            raise
        self._last_state = logical
        self._last_error = None

    def get_state(self) -> int:
        """The logical state the device shows now (read from the device)."""
        host, _label = self._use('read state')
        try:
            with host.lock:
                slot = self._read_slot(host.core)
        except Exception as exc:
            self._record_failure(exc)
            raise
        state = self._from_hw_state(slot)
        self._last_state = state
        self._last_error = None
        return state

    # --------------------------------------------------------------- lifecycle
    def _lifecycleSafeState(self, *, verified: bool):
        return []                       # a flip mirror stays where it is

    def _reconnected_summary(self) -> str:
        return f'{self._adopted.model}: {self._label} reconnected'

    def _reinitialise(self) -> List[str]:
        """Adopt the (re)loaded device again (re-load an inline one, check it still has the number of
        positions the widget was built for), then read where it is. Nothing moves."""
        try:
            adopted = self._adopt()
            previous = self._adopted
            if previous is not None and adopted.count != previous.count:
                raise ValueError(
                    f"{self._where()}: Micro-Manager device {self._label!r} has {adopted.count} positions, it had "
                    f"{previous.count} when ImSwitch2 started; restart ImSwitch2 to rebuild the selector")
            self._adopted = adopted
            self._ready = True
            self.get_state()
        except Exception as exc:                          # noqa: BLE001 - reported to the lifecycle
            return [f'{type(exc).__name__}: {exc}']
        return []

    def finalize(self):
        """Unload an inline device and record the shutdown. The mirror stays where it is."""
        self._ready = False
        self._unload_inline()
        self._setFinalizedStatus()
