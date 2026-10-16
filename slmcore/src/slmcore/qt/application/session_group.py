from __future__ import annotations


from qtpy import QtCore

from ...application.addressing import SLMSectionAddress
from ...application.control_mode import SLMControlMode
from .session import SLMQtSession


class SLMQtSessionGroup(QtCore.QObject):
    """Optional coordinator and catalog for already-created SLM Qt sessions.

    The group does not construct, own, dispose, or execute arbitrary commands
    on sessions. It exposes explicit lookup of physical SLM/section addresses
    and applies the small set of host-level policies that are genuinely shared.
    """

    sigControlModeChanged = QtCore.Signal(object)
    sigControlModeAvailabilityChanged = QtCore.Signal(bool)
    sigSessionAdded = QtCore.Signal(str,object)
    sigSessionRemoved = QtCore.Signal(str)
    sigCatalogChanged = QtCore.Signal()

    def __init__(self,parent: QtCore.QObject | None=None) -> None:
        super().__init__(parent)
        self._sessions: dict[str, SLMQtSession] = {}
        self._control_mode = SLMControlMode.EDITOR
        self._availability = True

    @property
    def control_mode(self) -> SLMControlMode:
        return self._control_mode

    @property
    def can_change_control_mode(self) -> bool:
        return self._availability

    @property
    def session_keys(self):
        return tuple(self._sessions)

    def contains(self,slm_key: str) -> bool:
        return str(slm_key).strip() in self._sessions

    def session(self,slm_key: str) -> SLMQtSession:
        key = str(slm_key).strip()
        try:
            return self._sessions[key]
        except KeyError as error:
            raise KeyError("SLM Qt session %r is not registered" % key) from error

    def session_items(self) -> tuple[tuple[str,SLMQtSession],...]:
        return tuple(self._sessions.items())

    def section_addresses(self) -> tuple[SLMSectionAddress,...]:
        return tuple(
            SLMSectionAddress(slm_key,section_key)
            for slm_key,session in self._sessions.items()
            for section_key in session.runtime.section_keys
        )

    def resolve(self,address: SLMSectionAddress) -> SLMQtSession:
        if not isinstance(address,SLMSectionAddress):
            raise TypeError("address must be an SLMSectionAddress")
        session = self.session(address.slm_key)
        if address.section_key not in session.runtime.section_keys:
            raise KeyError(
                "Section %r is not present in SLM Qt session %r"
                % (address.section_key,address.slm_key)
            )
        return session

    def add_session(self,session: SLMQtSession,*,key: str | None=None) -> str:
        if not isinstance(session,SLMQtSession):
            raise TypeError("session must be an SLMQtSession")
        session_key = str(key or session.runtime.identity.key).strip()
        if not session_key:
            raise ValueError("session key cannot be empty")
        if session_key in self._sessions:
            raise KeyError("SLM Qt session %r is already registered" % session_key)
        if session.control_mode is not self._control_mode:
            if not session.set_control_mode(self._control_mode):
                raise RuntimeError(
                    "Could not synchronize session %r to group control mode"
                    % session_key
                )
        self._sessions[session_key] = session
        session.sigControlModeAvailabilityChanged.connect(
            self._on_session_availability_changed
        )
        self._refresh_availability()
        self.sigSessionAdded.emit(session_key,session)
        self.sigCatalogChanged.emit()
        return session_key

    def remove_session(self,session_or_key) -> None:
        if isinstance(session_or_key,SLMQtSession):
            key = next((
                item for item,value in self._sessions.items()
                if value is session_or_key
            ),None)
        else:
            key = str(session_or_key).strip()
        if key is None:
            return
        session = self._sessions.pop(key,None)
        if session is None:
            return
        try:
            session.sigControlModeAvailabilityChanged.disconnect(
                self._on_session_availability_changed
            )
        except (RuntimeError,TypeError):
            pass
        self._refresh_availability()
        self.sigSessionRemoved.emit(key)
        self.sigCatalogChanged.emit()

    def set_control_mode(self,mode) -> bool:
        mode = SLMControlMode.normalize(mode)
        if mode is self._control_mode:
            return True
        if not self.can_change_control_mode:
            return False

        changed = []
        previous = self._control_mode
        for key,session in tuple(self._sessions.items()):
            if session.set_control_mode(mode):
                changed.append((key,session))
                continue
            for _rollback_key,rollback_session in reversed(changed):
                rollback_session.set_control_mode(previous)
            return False

        self._control_mode = mode
        self.sigControlModeChanged.emit(mode)
        self._refresh_availability()
        return True

    @QtCore.Slot(bool)
    def _on_session_availability_changed(self,_available: bool) -> None:
        self._refresh_availability()

    def _refresh_availability(self) -> None:
        available = all(
            session.can_change_control_mode
            for session in self._sessions.values()
        )
        if available == self._availability:
            return
        self._availability = available
        self.sigControlModeAvailabilityChanged.emit(available)
