"""The ImSwitch side's owner of one Fiji host process.

A session starts the host (:mod:`host`) as a child process, holds the one
authenticated localhost connection to it, and turns every failure into a
state and an exception that says why, with the tail of the host's log. It
never leaves a child behind: every failure path kills and reaps the process.

States:

* ``stopped``  never started, or stopped on purpose;
* ``starting`` :meth:`FijiSession.start` is running;
* ``ready``    the host answered a ping and speaks our protocol;
* ``failed``   it could not be started (:class:`FijiStartError`);
* ``dead``     it was ready and then died or the connection broke (:class:`FijiDied`).

A JVM can never be restarted inside a process, which is why the host is a
separate process: after ``failed`` or ``dead``, :meth:`~FijiSession.start`
launches a NEW one (its image ids start afresh, so :attr:`~FijiSession.sent`
is cleared).

The session is synchronous and thread-safe (one lock around every request and
reply). Fiji can take seconds to start and a fetch moves whole stacks, so
**call it from a worker thread**, never from a GUI thread; the Qt controller
that does so comes later and this module has no Qt in it.
"""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from multiprocessing.connection import Listener
from pathlib import Path
from typing import Mapping

import numpy as np

from imswitch.improcess.fiji.convert import PROP_RESULT_UID, SentRecord
from imswitch.improcess.fiji.protocol import (
    CloseImage,
    Events,
    Fetch,
    FetchInfo,
    FijiImageDescription,
    FijiLinkError,
    HostError,
    HostLaunchConfig,
    ListImages,
    Ping,
    PingInfo,
    ProtocolError,
    Quit,
    RunCommand,
    Send,
    check_ping,
    images_from_reply,
    reply_value,
)

#: How long :meth:`FijiSession.start` waits for the host to connect and answer.
#: P0 measured a 92 s first start, which included a Maven resolve that the
#: vendored bridge jars removed, and 7-10 s warm starts. 300 s is about three
#: times the worst measured start, so a cold first start on a slow disk or a
#: busy machine still fits, while a hung host is reported within five minutes.
#: Overridable per session.
STARTUP_TIMEOUT_S = 300.0

#: How long :meth:`FijiSession.stop` waits for the host to exit after ``Quit``
#: before killing it. Fiji's disposal took about 0.2 s in P0; 10 s is ~50 times
#: that, enough for a slow machine, short enough that closing ImSwitch never
#: seems hung.
STOP_TIMEOUT_S = 10.0

#: Granularity of the early-exit check while waiting for the host to connect:
#: it bounds how late a host that died at once is noticed.
STARTUP_POLL_S = 0.05

#: Lines of the host's log quoted in an error message: enough to hold the head
#: of a Java stack trace or a Python traceback, few enough for a dialog.
LOG_TAIL_LINES = 20

#: How long to wait for the exit status of a host whose connection broke,
#: before killing it. The socket closes when the process exits, so the status
#: is normally there at once; this only covers a half-dead process.
REAP_TIMEOUT_S = 5.0

#: Bound on waiting for a killed process to be reaped (SIGKILL cannot be
#: ignored, so this is only a guard against a stuck wait).
KILL_WAIT_S = 10.0

STATES = ("stopped", "starting", "ready", "failed", "dead")


class FijiStartError(FijiLinkError):
    """The host could not be started; the message ends with its log tail."""


class FijiDied(FijiLinkError):
    """The host process died or the connection to it broke."""


def _host_pythonpath(existing: str) -> str:
    """``existing`` with this ImSwitch's package root in front.

    The host may run in another interpreter (the ``fijiPython`` preference:
    an environment that has pyimagej when ImSwitch's does not), where
    ``imswitch`` is not installed. The host imports only the protocol module
    and numpy, so pointing it at this ImSwitch's source is all it needs, and
    it also guarantees both sides speak the same protocol version.
    """
    import imswitch

    root = str(Path(imswitch.__file__).resolve().parent.parent)
    parts = [p for p in existing.split(os.pathsep) if p and p != root]
    return os.pathsep.join([root, *parts])


class FijiSession:
    """One host process and the connection to it. See the module docstring."""

    def __init__(
        self,
        config: HostLaunchConfig,
        *,
        python: str = sys.executable,
        host_module: str = "imswitch.improcess.fiji.host",
        temp_root=None,
        extra_env: Mapping[str, str] | None = None,
        startup_timeout_s: float = STARTUP_TIMEOUT_S,
    ):
        self.config = config
        self._python = python
        self._host_module = host_module
        self._temp_root = temp_root
        self._extra_env = dict(extra_env or {})
        self._startup_timeout_s = startup_timeout_s

        self._lock = threading.RLock()
        self._state = "stopped"
        self._error = ""
        self._proc: subprocess.Popen | None = None
        self._conn = None
        self._listener = None
        self._temp_dir: Path | None = None
        self._counter = 0
        self.ping_info: PingInfo | None = None
        #: ``SentRecord`` per ImagePlus id this session created.
        self.sent: dict[int, SentRecord] = {}

    # -- state ------------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def error(self) -> str:
        """Why the session is ``failed`` or ``dead``; empty otherwise."""
        return self._error

    @property
    def process(self) -> subprocess.Popen | None:
        return self._proc

    @property
    def temp_dir(self) -> Path | None:
        return self._temp_dir

    @property
    def log_path(self) -> Path | None:
        return self._temp_dir / "host.log" if self._temp_dir is not None else None

    def __enter__(self) -> "FijiSession":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- starting and stopping ---------------------------------------------

    def start(self) -> None:
        """Start a host and wait until it is ready; a no-op while already ready."""
        with self._lock:
            if self._state == "ready":
                return
            self._teardown()
            self._state, self._error = "starting", ""
            try:
                self._launch()
            except (FijiLinkError, OSError) as exc:
                self._kill()
                self._close_channels()
                if not isinstance(exc, FijiLinkError):      # e.g. the interpreter could not be executed
                    exc = FijiStartError(self._describe(f"the Fiji host could not be launched: {exc}"))
                self._state, self._error = "failed", str(exc)
                raise exc
            except BaseException:
                self._kill()
                self._close_channels()
                self._state = "failed"
                raise
            self._state = "ready"

    def _launch(self) -> None:
        self._temp_dir = Path(tempfile.mkdtemp(prefix="imswitch-fiji-", dir=self._temp_root))
        authkey = secrets.token_bytes(32)
        self._listener = Listener(("127.0.0.1", 0), authkey=authkey)
        host, port = self._listener.address
        env = {**os.environ, **self.config.env(), "IMSWITCH_FIJI_ADDR": f"{host}:{port}", **self._extra_env}
        env["PYTHONPATH"] = _host_pythonpath(env.get("PYTHONPATH", ""))
        with open(self.log_path, "wb") as log:
            self._proc = subprocess.Popen(
                [self._python, "-m", self._host_module],
                stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, env=env,
            )
        try:
            # The key goes over stdin, not the environment: other processes of
            # the same user can read a process's environment.
            self._proc.stdin.write(authkey.hex().encode() + b"\n")
            self._proc.stdin.close()
        except OSError:
            pass        # the host already exited; the wait below reports why

        box: dict = {}
        accepted = threading.Event()

        def accept() -> None:
            try:
                box["conn"] = self._listener.accept()
            except BaseException as exc:  # noqa: BLE001  (reported by the waiting thread)
                box["error"] = exc
            finally:
                accepted.set()

        threading.Thread(target=accept, name="fiji-accept", daemon=True).start()
        deadline = time.monotonic() + self._startup_timeout_s
        while not accepted.wait(STARTUP_POLL_S):
            code = self._proc.poll()
            if code is not None:
                self._wake_accept(accepted)
                raise FijiStartError(self._describe(f"the Fiji host exited with code {code} before it connected"))
            if time.monotonic() >= deadline:
                self._wake_accept(accepted)
                raise FijiStartError(self._describe(
                    f"the Fiji host did not connect within {self._startup_timeout_s:g} s"))
        if "error" in box:
            raise FijiStartError(self._describe(f"the Fiji host connection failed: {box['error']!r}"))
        self._conn = box["conn"]

        try:
            info = PingInfo.from_dict(reply_value(self._exchange(Ping(), deadline - time.monotonic())))
            check_ping(info)
        except (FijiLinkError, EOFError, OSError) as exc:
            raise FijiStartError(self._describe(f"the Fiji host failed its first request: {exc}")) from exc
        self.ping_info = info

    def _wake_accept(self, accepted: threading.Event) -> None:
        """Release the blocked ``accept`` with a throwaway connection, then close the listener."""
        try:
            host, port = self._listener.address
            socket.create_connection((host, port), timeout=1.0).close()
        except OSError:
            pass
        accepted.wait(1.0)

    def stop(self, timeout_s: float = STOP_TIMEOUT_S) -> None:
        """Ask the host to quit, kill it after ``timeout_s``, and remove the temp files. Idempotent."""
        with self._lock:
            if self._state == "ready":
                try:
                    self._exchange(Quit(), None)
                except (FijiLinkError, EOFError, OSError):
                    pass
            if self._proc is not None and self._proc.poll() is None:
                try:
                    self._proc.wait(timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    self._kill()
            self._teardown()
            self._state, self._error = "stopped", ""

    def _kill(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=KILL_WAIT_S)
        except subprocess.TimeoutExpired:
            pass

    def _close_channels(self) -> None:
        for name in ("_conn", "_listener"):
            channel = getattr(self, name)
            setattr(self, name, None)
            if channel is not None:
                try:
                    channel.close()
                except OSError:
                    pass

    def _teardown(self) -> None:
        """Release everything of the previous host, process included."""
        self._kill()
        self._close_channels()
        if self._temp_dir is not None:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
        self._proc, self._temp_dir = None, None
        self.ping_info = None
        self.sent = {}

    # -- requests ----------------------------------------------------------

    def request(self, message):
        """Send one request, return the value of its reply.

        Raises :class:`HostError` when the host ran it and failed (the session
        stays ready), :class:`FijiDied` when the process or connection is gone.
        """
        with self._lock:
            if self._state == "dead":
                raise FijiDied(self._error)
            if self._state != "ready":
                raise FijiLinkError(f"the Fiji session is {self._state}, not ready")
            try:
                return reply_value(self._exchange(message, None))
            except (EOFError, OSError) as exc:
                raise self._mark_dead(exc) from exc

    def _exchange(self, message, timeout_s: float | None) -> dict:
        """Send ``message`` and return the raw reply dict; ``timeout_s`` bounds the wait."""
        self._conn.send(message.to_dict())
        if timeout_s is not None and not self._conn.poll(max(timeout_s, 0.0)):
            raise FijiLinkError("no reply from the Fiji host within the start-up allowance")
        return self._conn.recv()

    def _mark_dead(self, cause: BaseException) -> FijiDied:
        proc = self._proc
        code = None
        if proc is not None:
            try:
                code = proc.wait(timeout=REAP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                self._kill()
                code = proc.returncode
        self._close_channels()
        self._state = "dead"
        self._error = self._describe(f"the Fiji host process died (exit code {code}; {type(cause).__name__})")
        return FijiDied(self._error)

    def _describe(self, headline: str) -> str:
        tail = self.log_tail()
        return f"{headline}\n--- host log (last {LOG_TAIL_LINES} lines) ---\n{tail}" if tail else headline

    def log_tail(self, lines: int = LOG_TAIL_LINES) -> str:
        path = self.log_path
        try:
            text = path.read_text(encoding="utf-8", errors="replace") if path is not None else ""
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-lines:])

    # -- conveniences ------------------------------------------------------

    def _scratch(self, stem: str) -> Path:
        self._counter += 1
        return self._temp_dir / f"{stem}-{self._counter}.npy"

    def send(self, plan) -> int:
        """Open ``plan`` (a ``convert.SendPlan``) in Fiji and return the new image id."""
        with self._lock:
            self._require_ready()
            path = self._scratch("send")
            try:
                np.save(path, plan.data)
                image_id = int(self.request(Send.from_plan(plan, path))["image_id"])
            finally:
                path.unlink(missing_ok=True)
            self.sent[image_id] = SentRecord(
                image_id=image_id,
                result_uid=str(plan.properties.get(PROP_RESULT_UID, "")),
                source_axes=tuple(plan.source_axes),
                shape_tzcyx=tuple(int(n) for n in plan.data.shape),
            )
            return image_id

    def list_images(self) -> list[FijiImageDescription]:
        return images_from_reply(self.request(ListImages()))

    def fetch(self, image_id: int) -> tuple[np.ndarray, FijiImageDescription]:
        """The pixels of image ``image_id`` as (T,Z,C,Y,X), with its description."""
        with self._lock:
            self._require_ready()
            path = self._scratch("fetch")
            try:
                info = FetchInfo.from_dict(self.request(Fetch(int(image_id), str(path))))
                data = np.load(path)          # not memory-mapped: the file is deleted below
            finally:
                path.unlink(missing_ok=True)
            if list(data.shape) != list(info.shape_tzcyx) or data.dtype.name != info.dtype:
                raise ProtocolError(
                    f"the fetched file holds {data.dtype.name}{list(data.shape)}, "
                    f"the host reported {info.dtype}{list(info.shape_tzcyx)}"
                )
            return data, FijiImageDescription.from_dict(info.description)

    def run_command(self, image_id: int | None, command: str, options: str = "") -> int | None:
        return self.request(RunCommand(image_id, command, options))["current_id"]

    def close_image(self, image_id: int) -> None:
        self.request(CloseImage(int(image_id)))

    def events(self, clear: bool = False) -> dict:
        """``{"events": [...], "recorder": text or None}``; ``clear`` empties the events only."""
        return self.request(Events(clear=clear))

    def _require_ready(self) -> None:
        # request() checks again; this stops send/fetch writing temp files for a dead session.
        if self._state == "dead":
            raise FijiDied(self._error)
        if self._state != "ready":
            raise FijiLinkError(f"the Fiji session is {self._state}, not ready")


__all__ = [
    "STARTUP_TIMEOUT_S", "STOP_TIMEOUT_S", "STATES", "FijiStartError", "FijiDied", "FijiSession", "HostError",
]
