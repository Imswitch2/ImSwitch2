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
from multiprocessing import AuthenticationError
from multiprocessing.connection import Listener
from pathlib import Path
from typing import Callable, Mapping

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

#: How long the throwaway connection that wakes the accept thread may take to
#: connect, and how long the waiter then gives the accept thread to finish.
#: Both are localhost operations that complete in milliseconds; one second is
#: far above that and still keeps a failed start from seeming hung.
WAKE_ACCEPT_S = 1.0

#: Pause after a refused connection attempt before accepting again, so a
#: listener that fails at once (for example no file descriptors left) cannot
#: spin a core. Equal to the start-up poll: the same "noticed within" latency.
ACCEPT_RETRY_S = STARTUP_POLL_S

#: How long :meth:`FijiSession.stop` waits for a start or a request that holds
#: the session lock to let go AFTER it has killed the host. The holder is
#: released by the host's death, then needs: the start loop to notice
#: (STARTUP_POLL_S), wake and release its accept thread (a connect plus a wait,
#: 2 * WAKE_ACCEPT_S), and reap the process (REAP_TIMEOUT_S, KILL_WAIT_S). A
#: request only needs the reap. Every term is a bound that already exists, so
#: this adds no guess of its own.
STOP_HANDOVER_S = STARTUP_POLL_S + 2 * WAKE_ACCEPT_S + REAP_TIMEOUT_S + KILL_WAIT_S

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
        on_exit: Callable[[int | None], None] | None = None,
    ):
        self.config = config
        self._python = python
        self._host_module = host_module
        self._temp_root = temp_root
        self._extra_env = dict(extra_env or {})
        self._startup_timeout_s = startup_timeout_s
        self._on_exit = on_exit

        self._lock = threading.RLock()
        self._state = "stopped"
        self._error = ""
        self._proc: subprocess.Popen | None = None
        self._conn = None
        self._listener = None
        self._temp_dir: Path | None = None
        self._counter = 0
        #: Set by :meth:`stop` BEFORE it asks the host to quit, so the exit
        #: watcher can tell our own stop from the user closing Fiji.
        self._stopping = False
        #: Guards only the watcher's check-and-set of ``dead``; never held
        #: across I/O, so the watcher can never wait on a request.
        self._exit_lock = threading.Lock()
        self.ping_info: PingInfo | None = None
        #: ``SentRecord`` per ImagePlus id this session created.
        self.sent: dict[int, SentRecord] = {}
        #: Pixel fingerprint the host reported with the last fetch of each image (None when it gave none).
        self.fetch_fingerprints: dict[int, str | None] = {}

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
            self._stopping = False          # a stop() of the previous host must not abort this start
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
            self._watch(self._proc)

    def _watch(self, proc: subprocess.Popen) -> None:
        """Notice when ``proc`` exits on its own, without any request being made.

        Closing Fiji's window (or File > Quit) ends the host, and nothing else
        would tell us until the next request failed: the menu would keep
        saying Fiji is running. The thread watches the one ``proc`` it was
        given; a watcher of an old process finds ``self._proc`` is another
        object and does nothing, so it can never touch a newer session.
        """
        threading.Thread(target=self._wait_for_exit, args=(proc,), name="fiji-exit-watch", daemon=True).start()

    def _wait_for_exit(self, proc: subprocess.Popen) -> None:
        code = proc.wait()
        # Not the request lock: a request in flight holds it until its recv()
        # fails, and that is the same event we are reacting to. The tiny
        # _exit_lock only makes "is it still ready? then mark it dead" atomic
        # against a second watcher; the in-flight request still raises
        # FijiDied itself when it reads EOF and may overwrite the message.
        with self._exit_lock:
            if proc is not self._proc or self._stopping:
                return
            if self._state == "ready":
                self._error = self._describe(f"the Fiji host process exited (exit code {code})")
                self._state = "dead"
        if self._on_exit is not None:
            try:
                self._on_exit(code)
            except Exception:  # noqa: BLE001 - a bad callback must not hide the exit
                pass

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

        listener = self._listener
        box: dict = {}
        accepted = threading.Event()
        give_up = threading.Event()     # set when the waiter no longer wants a connection
        refused: list[str] = []         # repr of each connection attempt that failed to authenticate

        def accept() -> None:
            # Anyone on this machine can connect to the port, and CPython's
            # Listener.accept() runs the key challenge on the FIRST peer: a
            # port scanner or endpoint-security probe would otherwise fail the
            # start (a wrong key raises AuthenticationError, a peer that
            # closes raises EOFError) while the real host waits in the backlog.
            # So a failed attempt is recorded and the next peer is accepted.
            # A peer that connects and then sends nothing still blocks inside
            # the stdlib challenge; that is not solved here, the start-up
            # timeout bounds it.
            try:
                while not give_up.is_set():
                    try:
                        box["conn"] = listener.accept()
                        return
                    except (AuthenticationError, EOFError, OSError) as exc:
                        if give_up.is_set():
                            return
                        refused.append(repr(exc))
                        if give_up.wait(ACCEPT_RETRY_S):
                            return
            except BaseException as exc:  # noqa: BLE001  (reported by the waiting thread)
                box["error"] = exc
            finally:
                accepted.set()

        def fail(headline: str) -> FijiStartError:
            if refused:
                headline += (f"; {len(refused)} connection attempt(s) to its port failed authentication "
                             f"or closed early (last: {refused[-1]})")
            return FijiStartError(self._describe(headline))

        threading.Thread(target=accept, name="fiji-accept", daemon=True).start()
        deadline = time.monotonic() + self._startup_timeout_s
        while not accepted.wait(STARTUP_POLL_S):
            code = self._proc.poll()
            if code is not None:
                self._wake_accept(accepted, give_up)
                raise fail(f"the Fiji host exited with code {code} before it connected")
            if self._stopping:
                self._wake_accept(accepted, give_up)
                raise fail("stopped while starting")
            if time.monotonic() >= deadline:
                self._wake_accept(accepted, give_up)
                raise fail(f"the Fiji host did not connect within {self._startup_timeout_s:g} s")
        give_up.set()
        if "error" in box:
            raise fail(f"the Fiji host connection failed: {box['error']!r}")
        self._conn = box["conn"]
        self._close_listener()          # the host is connected: nothing else may connect
        if self._stopping:
            raise fail("stopped while starting")

        try:
            info = PingInfo.from_dict(reply_value(self._exchange(Ping(), deadline - time.monotonic())))
            check_ping(info)
        except (FijiLinkError, EOFError, OSError) as exc:
            if self._stopping:              # stop() killed the host under the ping
                raise fail("stopped while starting") from exc
            raise fail(f"the Fiji host failed its first request: {exc}") from exc
        self.ping_info = info

    def _wake_accept(self, accepted: threading.Event, give_up: threading.Event) -> None:
        """Tell the accept thread to stop, release it with a throwaway connection, wait for it.

        The throwaway connection is refused by the key challenge (and the thread
        then sees ``give_up``), which is what unblocks an ``accept`` that no
        close of the listener would reliably interrupt on every platform.
        """
        give_up.set()
        listener = self._listener
        if listener is not None:
            try:
                host, port = listener.address
                socket.create_connection((host, port), timeout=WAKE_ACCEPT_S).close()
            except OSError:
                pass
        accepted.wait(WAKE_ACCEPT_S)

    def _close_listener(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass

    def stop(self, timeout_s: float = STOP_TIMEOUT_S) -> None:
        """Quit the host, kill it if it will not, and remove the temp files. Idempotent, thread-safe.

        Unlike every other call it works while a :meth:`start` or a request is
        in flight, which is the point: a start can take minutes and a request
        to a Fiji sitting in a modal dialog never returns. It sets the stopping
        flag (a start in progress notices it within ``STARTUP_POLL_S`` and
        gives up), then waits for the session lock for at most ``timeout_s``.
        With the lock it asks the host to quit and kills it if it has not
        exited by the same deadline. Without it, the host is killed at once;
        the in-flight request then reads EOF (or the start sees the exit),
        lets go of the lock, and stop cleans up.

        Total bound: ``timeout_s`` for the polite path (lock wait, ``Quit`` and
        exit share one deadline), plus ``KILL_WAIT_S`` for reaping a host that
        had to be killed, plus ``STOP_HANDOVER_S`` when the lock was held by a
        start or request. The killed-host case normally ends in milliseconds;
        the sum is only the worst case of bounds that already exist.
        """
        self._stopping = True          # before Quit, so the watcher stays silent
        deadline = time.monotonic() + timeout_s
        if self._lock.acquire(timeout=max(timeout_s, 0.0)):
            try:
                self._quit_and_teardown(deadline)
            finally:
                self._lock.release()
            return
        self._kill()
        if not self._lock.acquire(timeout=STOP_HANDOVER_S):
            # The holder is neither a start nor a request waiting on the host
            # (the host is dead), so it is busy elsewhere, e.g. writing a large
            # array to disk. It leaves the session "dead" when it ends, and a
            # later stop() tidies up; tearing down under it would not be safe.
            return
        try:
            self._teardown()
            self._state, self._error = "stopped", ""
        finally:
            self._lock.release()

    def _quit_and_teardown(self, deadline: float) -> None:
        """With the lock held: ask the host to quit, kill it at ``deadline``, tear everything down."""
        if self._state == "ready":
            try:
                self._exchange(Quit(), max(deadline - time.monotonic(), 0.0))
            except (FijiLinkError, EOFError, OSError):
                pass
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.wait(timeout=max(deadline - time.monotonic(), 0.0))
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
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass
        self._close_listener()          # already None once the host has connected

    def _teardown(self) -> None:
        """Release everything of the previous host, process included."""
        self._kill()
        self._close_channels()
        if self._temp_dir is not None:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
        self._proc, self._temp_dir = None, None
        self.ping_info = None
        self.sent = {}
        self.fetch_fingerprints = {}

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
            raise FijiLinkError("no reply from the Fiji host within the time allowed")
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
            self.fetch_fingerprints[int(image_id)] = info.fingerprint
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
    "STARTUP_TIMEOUT_S", "STOP_TIMEOUT_S", "STOP_HANDOVER_S", "STATES", "FijiStartError", "FijiDied", "FijiSession", "HostError",
]
