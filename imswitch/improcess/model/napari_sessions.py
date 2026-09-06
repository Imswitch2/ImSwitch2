"""Endpoint sessions: who owns what while a plugin is looking at a result.

Sending a result to a plugin creates things that outlive the click: layers
added to the viewer, a dock widget, a temp directory with an exported file
that a lazy reader may still be memory-mapping, perhaps a detached viewer.
Something has to own them, or the temp file is deleted under the reader and
the layers pile up. That something is a session.

A session owns **exactly the objects it created** -- the layer objects, not
"layers with this result's uid" (the viewer's own display of the result
carries that uid too) -- and it closes when the user says so, when every
layer it added is gone, when its detached viewer is closed, or at exit.

This module is the state machine only. The controller performs the viewer
work (on the GUI thread) and tells the session what happened.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from imswitch.imcommon.algorithms.spatial_frame import mint_uid

STATES = ("pending", "exporting", "open", "closed", "failed")


class SessionError(RuntimeError):
    """A session was asked to do something its state does not allow."""


@dataclass
class EndpointSession:
    uid: str
    endpoint: Any                    # NapariEndpoint
    result_uid: str
    result_name: str
    state: str = "pending"
    created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    temp_dir: Path | None = None
    files: list[Path] = field(default_factory=list)
    layers: list[Any] = field(default_factory=list)       # exact layer objects
    dock: Any = None
    widget: Any = None
    viewer: Any = None                                    # detached viewer, when any
    error: str = ""
    #: The layers the viewer held when this session opened. A layer not in
    #: this set appeared afterwards, which is the only attribution the
    #: session can honestly make for a plugin's output.
    layers_before: tuple = ()
    #: Set when the session was closed while its export was still running;
    #: the export's completion is then stale and its files are discarded.
    cancelled: bool = False
    #: The export worker/thread, so closing the session can disown them.
    worker: Any = None

    @property
    def lane(self) -> str:
        return str(getattr(self.endpoint, "lane", ""))

    @property
    def label(self) -> str:
        return f"{getattr(self.endpoint, 'label', self.endpoint)} ← {self.result_name}"

    @property
    def is_open(self) -> bool:
        return self.state == "open"

    def owns_layer(self, layer) -> bool:
        return any(layer is item for item in self.layers)

    def layer_removed(self, layer) -> bool:
        """Forget ``layer``; returns True when no layer of this session is left."""
        self.layers = [item for item in self.layers if item is not layer]
        return not self.layers


class SessionRegistry:
    """Every live session, and the temp space they share."""

    def __init__(self, temp_root: Path | None = None):
        self._sessions: dict[str, EndpointSession] = {}
        self._temp_root = Path(temp_root) if temp_root else None

    # -- lifecycle ----------------------------------------------------------

    def open(self, endpoint, result) -> EndpointSession:
        session = EndpointSession(
            uid=mint_uid("endpoint"),
            endpoint=endpoint,
            result_uid=str(getattr(result, "result_uid", "") or ""),
            result_name=str(getattr(result, "name", "") or "result"),
        )
        self._sessions[session.uid] = session
        return session

    def get(self, uid: str) -> EndpointSession | None:
        return self._sessions.get(uid)

    def sessions(self) -> list[EndpointSession]:
        return [s for s in self._sessions.values() if s.state not in ("closed", "failed")]

    def all_sessions(self) -> list[EndpointSession]:
        return list(self._sessions.values())

    def temp_dir_for(self, session: EndpointSession) -> Path:
        """A directory of the session's own, created on first use."""
        if session.temp_dir is None:
            if self._temp_root is not None:
                self._temp_root.mkdir(parents=True, exist_ok=True)
            session.temp_dir = Path(tempfile.mkdtemp(
                prefix=f"imswitch_endpoint_{session.uid[-8:]}_", dir=self._temp_root
            ))
        return session.temp_dir

    def mark_exporting(self, session: EndpointSession) -> None:
        self._transition(session, "pending", "exporting")

    def mark_open(self, session: EndpointSession, *, files=(), layers=(), dock=None, widget=None, viewer=None) -> None:
        if session.state not in ("pending", "exporting"):
            raise SessionError(f"session {session.uid} is {session.state}, cannot open")
        session.files = [Path(f) for f in files]
        session.layers = list(layers)
        session.dock = dock
        session.widget = widget
        session.viewer = viewer
        session.state = "open"

    def mark_failed(self, session: EndpointSession, error: str) -> None:
        session.error = str(error)
        session.state = "failed"
        self._remove_files(session)

    def close(self, session: EndpointSession) -> EndpointSession:
        """Mark closed and delete the session's files. The caller tears down
        viewer objects first; this never touches them."""
        if session.state == "closed":
            return session
        if session.state == "exporting":
            # The worker is still writing into a directory we are about to
            # delete; its completion must be treated as stale.
            session.cancelled = True
        session.state = "closed"
        session.layers = []
        session.dock = None
        session.widget = None
        session.viewer = None
        session.worker = None
        self._remove_files(session)
        return session

    def close_all(self) -> list[EndpointSession]:
        closed = []
        for session in list(self._sessions.values()):
            if session.state != "closed":
                closed.append(self.close(session))
        return closed

    def forget_closed(self) -> None:
        self._sessions = {uid: s for uid, s in self._sessions.items() if s.state not in ("closed", "failed")}

    # -- events from the viewer ----------------------------------------------

    def find_by_layer(self, layer) -> EndpointSession | None:
        for session in self._sessions.values():
            if session.is_open and session.owns_layer(layer):
                return session
        return None

    def find_by_viewer(self, viewer) -> EndpointSession | None:
        for session in self._sessions.values():
            if session.is_open and session.viewer is not None and session.viewer is viewer:
                return session
        return None

    def layer_removed(self, layer) -> EndpointSession | None:
        """Tell the registry a layer went away; returns the session that should
        now close (its last layer is gone), or None."""
        session = self.find_by_layer(layer)
        if session is None:
            return None
        if session.layer_removed(layer):
            return session
        return None

    # -- internals -------------------------------------------------------------

    def _transition(self, session: EndpointSession, expected: str, new: str) -> None:
        if session.state != expected:
            raise SessionError(f"session {session.uid} is {session.state}, expected {expected}")
        session.state = new

    def _remove_files(self, session: EndpointSession) -> None:
        if session.temp_dir is not None:
            shutil.rmtree(session.temp_dir, ignore_errors=True)
            session.temp_dir = None
        session.files = []


__all__ = ["STATES", "EndpointSession", "SessionError", "SessionRegistry"]


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
