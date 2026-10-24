"""MicroManagerFlipMirrorManager: a two-position Micro-Manager device as an ImSwitch2 flip mirror.

Built on the hub :class:`~.core.MicroManagerCoreManager` and the shared plumbing
:class:`~._bridge.MicroManagerDeviceMixin`; see plan ``micromanager-bridge.md``
sections 4.2, 4.4 (flip-mirror row) and 4.7.

There is no flip-mirror base class in ImSwitch2. ``FlipMirrorsManager`` instantiates
``cls(info, name, **lowLevelManagers)`` and ``FlipMirrorController`` duck-types
``move_to(state)``, ``get_state()``, ``get_state_names()``, ``is_connected()``,
``get_cached_state()``, ``get_last_error()`` and ``finalize()``; ``FlipMirrorWidget`` draws exactly
two buttons. ``ThorlabsMFF`` is the precedent for ``invert``, ``initial_state`` and
``state_names``, which are fields of ``FlipMirrorInfo`` (not of ``managerProperties``).

Manager properties (``schemas/micromanager_flip_mirror.schema.json``):

- ``rs232device`` -- name of the ``micromanager.core`` hub in ``rs232devices``
- ``label`` -- the Micro-Manager device: a **State** device with exactly two positions, or a
  **Shutter** presented as a switch
- ``adapter`` + ``device`` (+ ``parent``, ``preInitProperties``) -- *inline mode* for that device
  (see :class:`~._bridge.MicroManagerDeviceMixin`)
- ``states`` (optional, State devices only) -- ``[a, b]``: the two Micro-Manager positions (state
  labels or indices) used as logical state 0 and 1. Without it, position 0 is state 0 and position 1
  is state 1. It orders the two positions of a two-position device; it does **not** turn a wheel into
  a switch, a device with more than two positions is refused whatever ``states`` says.

Logical and hardware state. ``FlipMirrorInfo.invert`` swaps logical states 0 and 1 against the
device, exactly as ``ThorlabsMFF`` does: logical ``L`` is hardware slot ``1 - L`` when inverted.
Slot 0 and 1 are the two Micro-Manager positions in ``states`` order (State device) or *closed* and
*open* (Shutter: ``setShutterOpen``).

Button names (``get_state_names``). ``FlipMirrorInfo.state_names`` wins per state; a state it does not
name is called what Micro-Manager calls the position behind it: its state label (State device), or
``Closed`` / ``Open`` (Shutter -- the words of Micro-Manager's own ``getShutterOpen``). The names
describe the physical position, so with ``invert`` they swap along with the states. They are read once,
when the controller builds its widget; a manager that was not connected then names the states ``"0"``
and ``"1"``.

Failure convention (the ThorlabsMFF one, which the controller expects). ``move_to`` and ``get_state``
**raise** -- ``DeviceNotConnectedError`` (a ``RuntimeError``) when the device is not there, otherwise
the reason -- and the controller logs it. A move is waited for (the hub lock is *not* held while
waiting) and **read back**: a device that does not show the new position is a failed move, not a
successful one. The reason of the last failure is :meth:`get_last_error`. A refused command does not
change the connection status.

``initial_state`` is applied once, at startup, when the device is there. A move that fails then is a
connection error of this manager (the mirror is in an unknown place); nothing replays it on reconnect:
the controller's contract is that a reconnect only observes the mirror.

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

#: Logical states of a flip mirror: the two buttons of ``FlipMirrorWidget``.
LOGICAL_STATES = (0, 1)

#: Positions a Micro-Manager State device must have to be a flip mirror. Same number as
#: ``len(LOGICAL_STATES)``: the widget has two buttons and no way to show a third position.
POSITION_COUNT = len(LOGICAL_STATES)

KIND_STATE = 'state'
KIND_SHUTTER = 'shutter'

#: What a Shutter's two slots are called when ``state_names`` does not say. Micro-Manager's own
#: words: ``getShutterOpen`` is a boolean, *closed* is slot 0 and *open* is slot 1.
SHUTTER_LABELS = ('Closed', 'Open')


@dataclass(frozen=True)
class _Adopted:
    """What was read from Micro-Manager when the device was adopted."""

    kind: str
    #: Micro-Manager state index behind hardware slot 0 and 1 (State device); empty for a Shutter.
    positions: Tuple[int, ...]
    #: Micro-Manager's word for hardware slot 0 and 1.
    labels: Tuple[str, str]
    model: str


def _is_index(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class MicroManagerFlipMirrorManager(MicroManagerDeviceMixin, DeviceManagerStatusMixin):
    """A Micro-Manager two-position State device or Shutter as a flip mirror."""

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
        if value is None:
            return None
        if isinstance(value, bool) or value not in LOGICAL_STATES:
            raise ValueError(f"{self._where()}: initial_state must be 0, 1 or null, got {value!r}")
        return int(value)

    def _parse_state_names(self, names) -> Dict[int, str]:
        """``{logical state: text}`` of the names the setup gives; the states it does not name are absent."""
        if not names:
            return {}
        if not isinstance(names, dict):
            raise ValueError(f"{self._where()}: state_names must be an object like {{\"0\": \"..\", \"1\": \"..\"}}")
        parsed: Dict[int, str] = {}
        for key, text in names.items():
            if str(key) not in {str(state) for state in LOGICAL_STATES}:
                raise ValueError(
                    f"{self._where()}: state_names has the key {key!r}; a flip mirror has the states "
                    f"{list(map(str, LOGICAL_STATES))}")
            parsed[int(key)] = str(text)
        return parsed

    def _parse_states(self, states) -> Optional[Tuple[Any, Any]]:
        """The shape of ``managerProperties.states``; resolving it needs the device."""
        if states is None:
            return None
        where = f"{self._where()}: managerProperties.states"
        if not isinstance(states, (list, tuple)) or len(states) != POSITION_COUNT:
            raise ValueError(
                f"{where} must list exactly {POSITION_COUNT} Micro-Manager positions (state labels or "
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
                    return _Adopted(KIND_SHUTTER, (), SHUTTER_LABELS, model)
                return self._adopt_state_device(core, model)
        except BaseException:
            if self._inline_loaded and not loaded_before:
                self._unload_inline()
            raise

    def _adopt_state_device(self, core, model: str) -> _Adopted:
        label = self._label
        count = int(core.getNumberOfStates(label))
        if count != POSITION_COUNT:
            raise ValueError(
                f"{self._where()}: Micro-Manager device {label!r} has {count} positions; a flip mirror has "
                f"exactly {POSITION_COUNT}. Filter wheels, objective turrets and multi-way light paths are "
                f"not supported (ImSwitch2 has no selector device kind yet), and managerProperties.states "
                f"does not turn them into a switch")
        labels = list(core.getStateLabels(label))
        if self._states is None:
            positions: Tuple[int, ...] = tuple(range(POSITION_COUNT))
        else:
            positions = tuple(self._position_of(ref, labels, count) for ref in self._states)
            if positions[0] == positions[1]:
                raise ValueError(
                    f"{self._where()}: managerProperties.states {list(self._states)!r} are both position "
                    f"{positions[0]} of {label!r}; the two must differ. Its state labels are {labels}")
        return _Adopted(KIND_STATE, positions, (labels[positions[0]], labels[positions[1]]), model)

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
        state = int(state)
        if state not in LOGICAL_STATES:
            raise ValueError("Flip mirror state must be 0 or 1")
        return 1 - state if self.invert else state

    def _from_hw_state(self, slot: int) -> int:
        return 1 - int(slot) if self.invert else int(slot)

    def get_state_names(self) -> Dict[int, str]:
        """``{0: text, 1: text}``: the setup's ``state_names``, else Micro-Manager's own words."""
        adopted = self._adopted
        names: Dict[int, str] = {}
        for state in LOGICAL_STATES:
            if state in self._configured_names:
                names[state] = self._configured_names[state]
            elif adopted is not None:
                names[state] = adopted.labels[self._to_hw_state(state)]
            else:
                names[state] = str(state)
        return names

    # -------------------------------------------------------------- hardware
    def _read_slot(self, core) -> int:
        """The hardware slot (0 or 1) the device shows now. Hub lock held."""
        adopted = self._adopted
        if adopted.kind == KIND_SHUTTER:
            return int(bool(core.getShutterOpen(self._label)))
        position = int(core.getState(self._label))
        if position not in adopted.positions:
            raise RuntimeError(
                f"{self._label!r} shows position {position}, which is neither of the two this flip mirror "
                f"uses ({adopted.positions[0]} and {adopted.positions[1]})")
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
        """Move to logical state 0 or 1, wait for the device and check it shows the new position."""
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
        """Adopt the (re)loaded device again (re-load an inline one, check it still has two positions),
        then read where it is. Nothing moves."""
        try:
            self._adopted = self._adopt()
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
