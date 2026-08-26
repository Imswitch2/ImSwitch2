from __future__ import annotations



from qtpy import QtCore

from .control_mode import SLMControlMode
from .session import SLMQtSession


class SLMQtSessionGroup(QtCore.QObject):
    """Optional coordinator for already-created independent SLM Qt sessions.

    The group does not construct, own, or dispose sessions. It only applies
    shared policy atomically enough for host-level coordinated controls.
    """

    sigControlModeChanged = QtCore.Signal(object)
    sigControlModeAvailabilityChanged = QtCore.Signal(bool)

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

    def add_session(self,session: SLMQtSession,*,key: str | None=None) -> str:
        if not isinstance(session,SLMQtSession):
            raise TypeError("session must be an SLMQtSession")
        session_key = str(key or session.runtime.identity.key)
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
        return session_key

    def remove_session(self,session_or_key) -> None:
        if isinstance(session_or_key,SLMQtSession):
            key = next((
                item for item,value in self._sessions.items()
                if value is session_or_key
            ),None)
        else:
            key = str(session_or_key)
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
