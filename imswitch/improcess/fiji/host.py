"""The process that owns Fiji: ``python -m imswitch.improcess.fiji.host``.

A JVM can be started once per process and never restarted, and on macOS Fiji's
GUI needs the process's main thread for the AppKit loop. So Fiji lives in its
own process: the ImSwitch side starts it, talks to it over a localhost
connection (:mod:`protocol`), and starts a new one if it dies.

This is the ONLY module that imports ``imagej``, ``scyjava`` or ``jpype``, and
it does so inside functions, after the environment is set, so a unit test that
merely imports the module needs none of them. It must also never import
``imswitch.improcess.model`` (directly or through ``convert``): the host's
interpreter does not have ImSwitch's dependencies. It reads only
:mod:`protocol`, which imports numpy and nothing else.

``faulthandler`` must never be enabled here: the JVM uses SIGSEGV internally
for its own bookkeeping (JPype documents this), and Python's handler would
turn that into a crash report and an abort.

Thread layout (verified in the P0 prototype on macOS):

* the main thread runs ``imagej.init(mode="gui")``, which blocks in the AppKit
  loop until Fiji quits;
* ``on_start`` runs on a Java thread when ImageJ is up; it installs the
  provenance probes and starts the request loop on a daemon Python thread, so
  the callback returns and pyimagej can finish.

stdout and stderr go to the session's log file, hence the plain prefixed
``print`` lines.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Sequence

import numpy as np

from imswitch.improcess.fiji.protocol import (
    IMAGEJ_DTYPE_NAMES,
    PROTOCOL_VERSION,
    CloseImage,
    Events,
    Fetch,
    FetchInfo,
    FijiImageDescription,
    ListImages,
    Ping,
    PingInfo,
    ProtocolError,
    Quit,
    Reply,
    RunCommand,
    Send,
    request_from_dict,
)

#: Exit code when the vendored bridge jar does not match the version imglyb
#: asks for (``EX_CONFIG`` of sysexits.h: a configuration error). Distinct from
#: a crash so the session's log tail makes the cause unmistakable.
EXIT_BRIDGE_MISMATCH = 78
#: Exit code for any other startup failure of this process (not a JVM exit).
EXIT_STARTUP_FAILED = 1

LOG_PREFIX = "[fiji-host]"

#: ImageJ's bit depth -> the dtype name of the planes it holds (24 = RGB, which
#: has no single-array form and is refused).
_DTYPE_BY_BIT_DEPTH = {8: "uint8", 16: "uint16", 32: "float32"}
assert set(_DTYPE_BY_BIT_DEPTH.values()) == set(IMAGEJ_DTYPE_NAMES)

_IMGLYB_ENDPOINT = re.compile(r"^net\.imglib2:imglib2-imglyb:(?P<version>[^:\s]+)")
_IMGLYB_JAR = re.compile(r"^imglib2-imglyb-(?P<version>.+)\.jar$")

_T0 = time.monotonic()


def log(*parts) -> None:
    print(LOG_PREFIX, *parts, flush=True)


# --------------------------------------------------------------------------
# pure parts: environment, authkey, version check, plane order
# --------------------------------------------------------------------------

class HostConfigError(RuntimeError):
    """The environment the session provides is incomplete or malformed."""


@dataclass(frozen=True)
class HostEnv:
    fiji_dir: str
    java_home: str
    java_fetch: str
    classpath: tuple
    max_heap_mb: int | None
    address: tuple            # (host, port)


def parse_env(environ: Mapping[str, str]) -> HostEnv:
    """The contract of ``HostLaunchConfig.env()`` plus ``IMSWITCH_FIJI_ADDR``."""
    required = ("FIJI_DIR", "JAVA_HOME", "IMSWITCH_FIJI_JAVA_FETCH", "IMSWITCH_FIJI_CLASSPATH", "IMSWITCH_FIJI_ADDR")
    missing = [key for key in required if not environ.get(key)]
    if missing:
        raise HostConfigError(f"missing environment variables: {', '.join(missing)}")
    heap_text = environ.get("IMSWITCH_FIJI_MAX_HEAP_MB")
    try:
        heap = int(heap_text) if heap_text else None
        host, _, port = environ["IMSWITCH_FIJI_ADDR"].rpartition(":")
        address = (host, int(port))
    except ValueError as exc:
        raise HostConfigError(f"malformed environment: {exc}") from exc
    if not host:
        raise HostConfigError(f"IMSWITCH_FIJI_ADDR must be host:port, got {environ['IMSWITCH_FIJI_ADDR']!r}")
    classpath = tuple(p for p in environ["IMSWITCH_FIJI_CLASSPATH"].split(os.pathsep) if p)
    return HostEnv(
        fiji_dir=environ["FIJI_DIR"], java_home=environ["JAVA_HOME"],
        java_fetch=environ["IMSWITCH_FIJI_JAVA_FETCH"], classpath=classpath,
        max_heap_mb=heap, address=address,
    )


def read_authkey(stream) -> bytes:
    """The authkey as ONE hex line.

    It comes over stdin rather than the environment because other processes of
    the same user can read a process's environment.
    """
    line = stream.readline().strip()
    if not line:
        raise HostConfigError("no authkey on stdin")
    try:
        key = bytes.fromhex(line)
    except ValueError as exc:
        raise HostConfigError("the authkey on stdin is not hexadecimal") from exc
    if not key:
        raise HostConfigError("the authkey on stdin is empty")
    return key


def requested_imglyb_version(endpoints: Sequence[str]) -> str | None:
    """The imglib2-imglyb version scyjava was asked to resolve, or ``None``."""
    for endpoint in endpoints:
        match = _IMGLYB_ENDPOINT.match(str(endpoint))
        if match:
            return match.group("version")
    return None


def vendored_imglyb_version(jars: Sequence) -> str | None:
    for jar in jars:
        match = _IMGLYB_JAR.match(os.path.basename(str(jar)))
        if match:
            return match.group("version")
    return None


def bridge_version_problem(endpoints: Sequence[str], jars: Sequence) -> str | None:
    """Why the vendored bridge jars cannot be used, or ``None`` when they can.

    Versions are never mixed: imglyb's Python side and the Java jar speak a
    private interface, so a mismatch is refused instead of tried.
    """
    requested = requested_imglyb_version(endpoints)
    if requested is None:
        return None            # imglyb asked for no jar, so there is nothing to mismatch
    vendored = vendored_imglyb_version(jars)
    if vendored is None:
        return f"imglyb requests imglib2-imglyb {requested} but no imglib2-imglyb jar is on the host classpath"
    if vendored != requested:
        return (
            f"imglyb requests imglib2-imglyb {requested} but the vendored jar is {vendored}; "
            "update imswitch/improcess/fiji/jars together with the pyimagej pin"
        )
    return None


def stack_index(c: int, z: int, t: int, n_channels: int, n_slices: int) -> int:
    """ImageJ's 1-based stack index of 0-based ``(c, z, t)``: ``c + z*C + t*C*Z`` (+1)."""
    return c + z * n_channels + t * n_channels * n_slices + 1


def plane_order(n_frames: int, n_slices: int, n_channels: int) -> Iterator[tuple]:
    """``(t, z, c)`` in stack order, which is also C-order of a (T,Z,C,Y,X) array."""
    for t in range(n_frames):
        for z in range(n_slices):
            for c in range(n_channels):
                yield t, z, c


# --------------------------------------------------------------------------
# the event log (filled by Java callbacks)
# --------------------------------------------------------------------------

class EventLog:
    """What ImageJ did, in order, with the time since host start in seconds.

    Every event carries a ``seq``, an increasing integer that survives a
    consumer filtering the list. A command event also gets an ``end`` when its
    command finishes; the commands that run on ImageJ's own threads finish
    later than they start, so the end is stamped by :meth:`end_command` and is
    ``None`` until then.
    """

    def __init__(self, clock=time.monotonic, origin: float | None = None):
        self._clock = clock
        self._origin = clock() if origin is None else origin
        self._events: list[dict] = []
        self._by_seq: dict[int, dict] = {}
        self._next_seq = 0
        self._lock = threading.Lock()

    def _now(self) -> float:
        return round(self._clock() - self._origin, 3)

    def record(self, kind: str, **fields) -> int:
        with self._lock:
            seq = self._next_seq
            self._next_seq += 1
            event = {"seq": seq, "time": self._now(), "kind": kind, **fields}
            self._events.append(event)
            if kind == "command":
                self._by_seq[seq] = event
        return seq

    def begin_command(self, command: str, **fields) -> int:
        """Record that ``command`` starts now; the returned ``seq`` is the handle for :meth:`end_command`."""
        return self.record("command", command=command, end=None, **fields)

    def end_command(self, seq: int, **fields) -> None:
        """Stamp the end of a command begun earlier, with any ``fields`` known at that moment.

        A seq already cleared from the log is ignored.
        """
        with self._lock:
            event = self._by_seq.get(seq)
            if event is not None and event.get("end") is None:
                event["end"] = self._now()
                event.update(fields)

    def input_of(self, seq: int) -> int | None:
        """The ``current_id`` a command started with (None when unknown or cleared)."""
        with self._lock:
            event = self._by_seq.get(seq)
            return event.get("current_id") if event is not None else None

    def snapshot(self, clear: bool = False) -> list[dict]:
        """Copies of the events, each with its ``end`` as known right now."""
        with self._lock:
            out = [dict(event) for event in self._events]
            if clear:
                self._events.clear()
                self._by_seq.clear()
            return out


# --------------------------------------------------------------------------
# Java side (everything below needs a running ImageJ)
# --------------------------------------------------------------------------

def _jimport(name: str):
    import scyjava
    return scyjava.jimport(name)


class _RunLocal(threading.local):
    seqs: list | None = None
    #: The options run_command asked for, until the first command of that call has used them.
    requested: str | None = None


class FijiState:
    """The live ImageJ plus the things that must stay referenced for Java."""

    def __init__(self, ij):
        self.ij = ij
        self.events = EventLog(origin=_T0)
        self.recorder = None
        self._jar_cache: dict[str, str | None] = {}
        # ``seqs`` is a list while run_command is on this thread (so the probe
        # knows who stamps the end), None otherwise.
        self._run_local = _RunLocal()
        # Java only holds a weak-ish handle to a JImplements proxy; dropping
        # ours would let Python collect it under a live listener.
        self._proxies: list = []

    def install_probes(self) -> None:
        from jpype import JImplements, JOverride

        WindowManager = _jimport("ij.WindowManager")
        Executer = _jimport("ij.Executer")
        ImagePlus = _jimport("ij.ImagePlus")
        Macro = _jimport("ij.Macro")
        Menus = _jimport("ij.Menus")
        Recorder = _jimport("ij.plugin.frame.Recorder")
        Thread = _jimport("java.lang.Thread")
        events = self.events
        state = self

        def thread_name() -> str:
            return str(Thread.currentThread().getName())

        def recorder_offset() -> int | None:
            if state.recorder is None:
                return None
            try:
                return len(str(state.recorder.getText()))
            except Exception:  # noqa: BLE001  (a recorder that cannot be read must not break the command)
                return None

        def plugin_class(command: str) -> str | None:
            try:
                found = Menus.getCommands().get(command)
                return str(found) if found is not None else None
            except Exception:  # noqa: BLE001
                return None

        @JImplements("ij.CommandListener")
        class CommandProbe:
            @JOverride
            def commandExecuting(self, command):
                imp = WindowManager.getCurrentImage()
                options = Macro.getOptions()
                options = str(options) if options is not None else None
                hint = None
                requested, state._run_local.requested = state._run_local.requested, None
                if options is None and requested is not None:
                    # ImageJ hands an empty options string to the command as "none",
                    # but the host itself passed it: only the first command of the
                    # call is ours, a macro's inner commands are not.
                    options, hint = requested, "request"
                name = str(command)
                class_name = plugin_class(name)
                seq = events.begin_command(
                    name,
                    current_id=int(imp.getID()) if imp is not None else None,
                    options=options, options_source_hint=hint,
                    thread=thread_name(),
                    recorder_offset=recorder_offset(),
                    plugin_class=class_name,
                    plugin_jar=state.plugin_jar(class_name),
                )
                if state._run_local.seqs is not None:
                    # Started by run_command on this very thread: it stamps the end itself.
                    state._run_local.seqs.append(seq)
                else:
                    # Started by ImageJ's own Executer thread (menu use): the command
                    # is over when that Java thread is.
                    state.watch_end(seq, Thread.currentThread(), int(imp.getID()) if imp is not None else None)
                return command

        @JImplements("ij.ImageListener")
        class ImageProbe:
            @JOverride
            def imageOpened(self, imp):
                events.record("opened", id=int(imp.getID()), title=str(imp.getTitle()), thread=thread_name(),
                              changes=bool(imp.changes))

            @JOverride
            def imageClosed(self, imp):
                events.record("closed", id=int(imp.getID()), title=str(imp.getTitle()), thread=thread_name(),
                              changes=bool(imp.changes))

            @JOverride
            def imageUpdated(self, imp):
                events.record("updated", id=int(imp.getID()), title=str(imp.getTitle()), thread=thread_name(),
                              changes=bool(imp.changes))

        command_probe, image_probe = CommandProbe(), ImageProbe()
        self._proxies.extend([command_probe, image_probe])
        Executer.addCommandListener(command_probe)
        ImagePlus.addImageListener(image_probe)
        try:
            # A hidden recorder switches ImageJ's own macro recording on, so
            # every command also leaves its macro text.
            self.recorder = Recorder(False)
            log("hidden Recorder created; recording =", bool(Recorder.record))
        except Exception as exc:  # noqa: BLE001  (Java exceptions have no common Python base)
            self.recorder = None
            log("Recorder failed:", exc)

    def plugin_jar(self, class_name: str | None) -> str | None:
        """File name of the jar (or directory) a plugin class was loaded from, cached per class."""
        if class_name is None:
            return None
        key = class_name.split("(", 1)[0]
        if key not in self._jar_cache:
            self._jar_cache[key] = self._lookup_jar(key)
        return self._jar_cache[key]

    @staticmethod
    def _lookup_jar(class_name: str) -> str | None:
        try:
            JClass = _jimport("java.lang.Class")
            loader = _jimport("ij.IJ").getClassLoader()
            try:
                cls = JClass.forName(class_name, False, loader)
            except Exception:  # noqa: BLE001  (not on the plugin loader: try the default one)
                cls = JClass.forName(class_name)
            location = cls.getProtectionDomain().getCodeSource().getLocation()
            return os.path.basename(str(location.getPath()).split("!", 1)[0].rstrip("/")) or None
        except Exception:  # noqa: BLE001  (no code source, a bootstrap class, a missing class: all "unknown")
            return None

    def watch_end(self, seq: int, java_thread, input_id: int | None) -> None:
        """Stamp ``seq``'s end when ``java_thread`` (the thread running a menu command) dies."""
        def wait() -> None:
            try:
                java_thread.join()
                self.drain_edt()
            except Exception:  # noqa: BLE001  (the end stays None: "not known", which the tracker handles)
                return
            self.events.end_command(seq, end_fingerprints=self.end_fingerprints(input_id))

        threading.Thread(target=wait, name=f"fiji-end-{seq}", daemon=True).start()

    def end_fingerprints(self, input_id: int | None) -> dict[str, str]:
        """Pixel fingerprints, now, of a command's input image and of the current image."""
        WindowManager = _jimport("ij.WindowManager")
        found: dict[str, str] = {}
        current = WindowManager.getCurrentImage()
        images = [WindowManager.getImage(int(input_id)) if input_id is not None else None, current]
        for imp in images:
            if imp is None:
                continue
            key = str(int(imp.getID()))
            if key in found:
                continue
            try:
                found[key] = fingerprint(imp)
            except Exception:  # noqa: BLE001  (an image that cannot be read just has no fingerprint)
                continue
        return found

    @staticmethod
    def drain_edt() -> None:
        """Wait until the AWT event thread has delivered everything queued so far.

        ImageJ hands ImageListener notifications to it with invokeLater, so an
        "opened" for a duplicate made by a command that already returned may
        still be queued.
        """
        from jpype import JProxy

        _jimport("java.awt.EventQueue").invokeAndWait(JProxy("java.lang.Runnable", dict(run=lambda: None)))

    # -- handlers ---------------------------------------------------------

    def ping(self, _req: Ping) -> dict:
        IJ = _jimport("ij.IJ")
        System = _jimport("java.lang.System")
        Runtime = _jimport("java.lang.Runtime")
        GraphicsEnvironment = _jimport("java.awt.GraphicsEnvironment")
        return PingInfo(
            protocol=PROTOCOL_VERSION,
            imagej=str(self.ij.getVersion()),
            ij1=str(IJ.getVersion()),
            java=str(System.getProperty("java.version")),
            headless=bool(GraphicsEnvironment.isHeadless()),
            pid=os.getpid(),
            max_heap_mb=int(Runtime.getRuntime().maxMemory()) // 2 ** 20,
        ).to_dict()

    def send(self, req: Send) -> dict:
        data = np.load(req.path, mmap_mode="r")
        if list(data.shape) != list(req.shape_tzcyx):
            raise ValueError(f"file holds shape {list(data.shape)}, the message declares {list(req.shape_tzcyx)}")
        if data.dtype.name != req.dtype:
            raise ValueError(f"file holds dtype {data.dtype.name}, the message declares {req.dtype}")
        imp = build_imageplus(data, req)
        imp.show()
        self.drain_edt()          # its "opened" must precede whatever command comes next
        return {"image_id": int(imp.getID())}

    def list_images(self, _req: ListImages) -> list:
        WindowManager = _jimport("ij.WindowManager")
        ids = WindowManager.getIDList()
        return [describe(WindowManager.getImage(int(i))).to_dict() for i in (ids if ids is not None else [])]

    def fetch(self, req: Fetch) -> dict:
        imp = self._image(req.image_id)
        description = describe(imp)
        shape = write_tzcyx(imp, req.path)
        return FetchInfo(
            image_id=req.image_id, shape_tzcyx=shape,
            dtype=_DTYPE_BY_BIT_DEPTH[int(imp.getBitDepth())], description=description.to_dict(),
            fingerprint=_safe_fingerprint(imp),
        ).to_dict()

    def run_command(self, req: RunCommand) -> dict:
        IJ = _jimport("ij.IJ")
        WindowManager = _jimport("ij.WindowManager")
        seqs: list[int] = []
        self._run_local.seqs = seqs
        self._run_local.requested = req.options
        try:
            if req.image_id is not None:
                IJ.run(self._image(req.image_id), req.command, req.options)
            else:
                IJ.run(req.command, req.options)
            try:
                self.drain_edt()          # so every image event of this command precedes its end
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._run_local.seqs = None
            self._run_local.requested = None
            prints: dict = {}
            for seq in seqs:
                input_id = self.events.input_of(seq)
                if input_id not in prints:
                    prints[input_id] = self.end_fingerprints(input_id)
                self.events.end_command(seq, end_fingerprints=prints[input_id])
        current = WindowManager.getCurrentImage()
        return {"current_id": int(current.getID()) if current is not None else None}

    def close_image(self, req: CloseImage) -> dict:
        imp = self._image(req.image_id)
        imp.changes = False          # otherwise ImageJ asks the user whether to save
        imp.close()
        return {}

    def events_reply(self, req: Events) -> dict:
        # Draining the AWT queue first makes a snapshot complete for every
        # request that already returned.
        self.drain_edt()
        text = self.recorder.getText() if self.recorder is not None else None
        return {"events": self.events.snapshot(clear=req.clear), "recorder": str(text) if text is not None else None}

    def _image(self, image_id: int):
        imp = _jimport("ij.WindowManager").getImage(int(image_id))
        if imp is None:
            raise KeyError(f"no open image with id {image_id}")
        return imp

    def handle(self, request) -> Any:
        if isinstance(request, Ping):
            return self.ping(request)
        if isinstance(request, Send):
            return self.send(request)
        if isinstance(request, ListImages):
            return self.list_images(request)
        if isinstance(request, Fetch):
            return self.fetch(request)
        if isinstance(request, RunCommand):
            return self.run_command(request)
        if isinstance(request, CloseImage):
            return self.close_image(request)
        if isinstance(request, Events):
            return self.events_reply(request)
        raise ProtocolError(f"the host has no handler for {type(request).__name__}")


def build_imageplus(data, send: Send):
    """Canonical (T,Z,C,Y,X) ``data`` -> an ImageJ1 hyperstack, copying plane by plane.

    The data is already in ImageJ's stack order, so nothing is transposed.
    Calibration entries that are ``None`` leave ImageJ's default untouched.
    """
    from jpype import JArray, JByte, JFloat, JShort

    ImageStack = _jimport("ij.ImageStack")
    ImagePlus = _jimport("ij.ImagePlus")
    CompositeImage = _jimport("ij.CompositeImage")
    ByteProcessor = _jimport("ij.process.ByteProcessor")
    ShortProcessor = _jimport("ij.process.ShortProcessor")
    FloatProcessor = _jimport("ij.process.FloatProcessor")

    n_t, n_z, n_c, height, width = (int(n) for n in data.shape)
    # Java has no unsigned 8/16-bit arrays: the bytes are the same, so reinterpret.
    if data.dtype == np.uint8:
        def make(plane):
            return ByteProcessor(width, height, JArray(JByte)(plane.view(np.int8).ravel()))
    elif data.dtype == np.uint16:
        def make(plane):
            return ShortProcessor(width, height, JArray(JShort)(plane.view(np.int16).ravel()), None)
    elif data.dtype == np.float32:
        def make(plane):
            return FloatProcessor(width, height, JArray(JFloat)(plane.ravel()))
    else:
        raise ValueError(f"dtype {data.dtype} is not one of {IMAGEJ_DTYPE_NAMES}")

    names = list(send.channel_names)
    stack = ImageStack(width, height)
    for t, z, c in plane_order(n_t, n_z, n_c):
        plane = np.ascontiguousarray(data[t, z, c])
        stack.addSlice(names[c] if c < len(names) else None, make(plane))

    imp = ImagePlus(send.title, stack)
    imp.setDimensions(n_c, n_z, n_t)
    imp.setOpenAsHyperStack(True)

    cal, wanted = imp.getCalibration(), send.calibration
    if wanted.get("pixel_width") is not None:
        cal.pixelWidth = float(wanted["pixel_width"])
    if wanted.get("pixel_height") is not None:
        cal.pixelHeight = float(wanted["pixel_height"])
    if wanted.get("pixel_depth") is not None:
        cal.pixelDepth = float(wanted["pixel_depth"])
    if wanted.get("unit") is not None:
        cal.setUnit(str(wanted["unit"]))
    if wanted.get("frame_interval_s") is not None:
        cal.frameInterval = float(wanted["frame_interval_s"])
        cal.setTimeUnit("s")           # ImProcess keeps the T scale in seconds

    for key, value in send.properties.items():
        imp.setProp(str(key), str(value))
    if n_c > 1:
        imp = CompositeImage(imp, CompositeImage.COMPOSITE)
    return imp


def describe(imp) -> FijiImageDescription:
    cal = imp.getCalibration()
    flat = imp.getPropertiesAsArray()          # [k, v, k, v ...] or None
    items = [str(x) for x in flat] if flat is not None else []
    stack = imp.getStack()
    n_channels = int(imp.getNChannels())
    labels = []
    for c in range(n_channels):
        label = stack.getSliceLabel(int(imp.getStackIndex(c + 1, 1, 1)))
        labels.append(str(label) if label is not None else None)
    width, height, channels, slices, frames = (int(v) for v in imp.getDimensions())
    return FijiImageDescription(
        image_id=int(imp.getID()),
        title=str(imp.getTitle()),
        dims_xyczt=(width, height, channels, slices, frames),
        bit_depth=int(imp.getBitDepth()),
        pixel_width=float(cal.pixelWidth), pixel_height=float(cal.pixelHeight), pixel_depth=float(cal.pixelDepth),
        x_unit=str(cal.getXUnit()), y_unit=str(cal.getYUnit()), z_unit=str(cal.getZUnit()),
        x_origin=float(cal.xOrigin), y_origin=float(cal.yOrigin), z_origin=float(cal.zOrigin),
        frame_interval=float(cal.frameInterval), time_unit=str(cal.getTimeUnit()),
        properties=dict(zip(items[0::2], items[1::2])),
        channel_labels=tuple(labels),
        changes=bool(imp.changes),
    )


#: Modulus of the fold in :func:`fingerprint`: 64 bits, so the hex result has a fixed width.
_FINGERPRINT_BITS = 64
#: Multiplier of the fold (the 64-bit FNV prime), so plane order matters.
_FINGERPRINT_PRIME = 0x100000001B3


def fold_fingerprint(parts) -> str:
    """Fold integers into one 64-bit hex string; order-sensitive."""
    mask = (1 << _FINGERPRINT_BITS) - 1
    value = 0
    for part in parts:
        value = ((value ^ (int(part) & mask)) * _FINGERPRINT_PRIME) & mask
    return f"{value:0{_FINGERPRINT_BITS // 4}x}"


def fingerprint(imp) -> str:
    """A change detector over ``imp``'s pixels, computed on the Java side plane by plane.

    ``java.util.Arrays.hashCode`` of every plane's pixel array is folded together
    with width, height, bit depth and stack size. It is NOT a cryptographic hash:
    two different images can collide, which only costs a missed "possible" change
    in the history. Equal fingerprints mean the pixels are (almost certainly)
    the ones seen before; ImageJ's own ``changes`` flag cannot say that.
    """
    Arrays = _jimport("java.util.Arrays")
    stack = imp.getStack()
    size = int(stack.getSize())
    parts = [int(imp.getWidth()), int(imp.getHeight()), int(imp.getBitDepth()), size]
    parts.extend(int(Arrays.hashCode(stack.getPixels(i))) for i in range(1, size + 1))
    return fold_fingerprint(parts)


def _safe_fingerprint(imp) -> str | None:
    try:
        return fingerprint(imp)
    except Exception:  # noqa: BLE001  (the fingerprint is optional evidence, never a reason to fail a fetch)
        return None


def write_tzcyx(imp, path: str) -> list:
    """Write ``imp``'s pixels to ``path`` as a (T,Z,C,Y,X) ``.npy``, plane by plane.

    A memory-mapped output means the host never holds a second full copy of a
    large stack (P0: a 1 GB stack swapped when it was copied whole).
    """
    bit_depth = int(imp.getBitDepth())
    if bit_depth == 24:
        raise ValueError("24-bit RGB images cannot be fetched; convert to 8-bit or split the channels in Fiji first")
    if bit_depth not in _DTYPE_BY_BIT_DEPTH:
        raise ValueError(f"unsupported ImageJ bit depth {bit_depth}")
    dtype = np.dtype(_DTYPE_BY_BIT_DEPTH[bit_depth])
    width, height, n_c, n_z, n_t = (int(v) for v in imp.getDimensions())
    shape = (n_t, n_z, n_c, height, width)
    stack = imp.getStack()
    out = np.lib.format.open_memmap(path, mode="w+", dtype=dtype, shape=shape)
    try:
        for t, z, c in plane_order(n_t, n_z, n_c):
            pixels = stack.getPixels(int(imp.getStackIndex(c + 1, z + 1, t + 1)))
            out[t, z, c] = np.asarray(pixels).view(dtype).reshape(height, width)
        out.flush()
    finally:
        del out
    return [int(n) for n in shape]


# --------------------------------------------------------------------------
# request loop and startup
# --------------------------------------------------------------------------

def _exit_java(code: int) -> None:
    _jimport("java.lang.System").exit(code)


def serve(state: FijiState, env: HostEnv, authkey: bytes) -> None:
    from multiprocessing.connection import Client

    try:
        conn = Client(env.address, authkey=authkey)
    except Exception as exc:  # noqa: BLE001
        log("could not connect to the parent:", repr(exc))
        os._exit(EXIT_STARTUP_FAILED)
    log("connected to the parent")
    while True:
        try:
            raw = conn.recv()
        except (EOFError, OSError):
            log("the parent closed the connection; exiting")
            _exit_java(0)
            return
        quitting = False
        try:
            request = request_from_dict(raw)
            if isinstance(request, Quit):
                quitting = True
                reply = Reply(True, None)
            else:
                reply = Reply(True, state.handle(request))
        except Exception as exc:  # noqa: BLE001  (every failure must reach the parent)
            reply = Reply(False, error=f"{type(exc).__name__}: {exc}", trace=traceback.format_exc())
        try:
            conn.send(reply.to_dict())
        except (EOFError, OSError):
            log("the parent went away before the reply; exiting")
            _exit_java(0)
            return
        if quitting:
            log("quit requested")
            _exit_java(0)
            return


def _configure_java(env: HostEnv):
    import scyjava

    # pyimagej 1.8.0 crashes with fetch="never" (unbound major_version in
    # imagej._guess_java_version, verified 2026-10-26); "auto" uses JAVA_HOME
    # when it holds a JDK, which the launch configuration guarantees.
    scyjava.config.set_java_constraints(fetch=env.java_fetch)
    if env.max_heap_mb is not None:
        # Fiji's own launcher policy; Java's default heap (25% of RAM) ran out
        # of memory on a 2 GB stack in the prototype.
        scyjava.config.add_option(f"-Xmx{env.max_heap_mb}m")
    return scyjava


def _use_vendored_bridge(scyjava, env: HostEnv) -> None:
    # Importing imagej imports imglyb, which appends its Maven endpoint. jgo
    # would resolve 38 jars including imglib2 5.12 beside Fiji's 8.0; instead
    # the endpoints are cleared and exactly the vendored jars are added.
    endpoints = list(scyjava.config.endpoints)
    log("scyjava endpoints requested:", endpoints)
    problem = bridge_version_problem(endpoints, env.classpath)
    if problem:
        print(f"{LOG_PREFIX} refusing to start: {problem}", file=sys.stderr, flush=True)
        sys.exit(EXIT_BRIDGE_MISMATCH)
    scyjava.config.endpoints.clear()
    for jar in env.classpath:
        scyjava.config.add_classpath(jar)


def main() -> int:
    try:
        env = parse_env(os.environ)
        authkey = read_authkey(sys.stdin)
    except HostConfigError as exc:
        print(f"{LOG_PREFIX} bad launch contract: {exc}", file=sys.stderr, flush=True)
        return EXIT_STARTUP_FAILED
    sys.stdin.close()

    scyjava = _configure_java(env)
    import imagej  # noqa: PLC0415  (after the Java constraints; appends imglyb's endpoint)

    _use_vendored_bridge(scyjava, env)

    def on_start(ij) -> None:
        try:
            log("ImageJ started:", ij.getVersion())
            state = FijiState(ij)
            state.install_probes()
            # On macOS this callback runs on a Java thread; serving from a
            # daemon Python thread lets it return so pyimagej can finish.
            threading.Thread(target=serve, args=(state, env, authkey), name="fiji-ipc", daemon=True).start()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            sys.stderr.flush()
            os._exit(EXIT_STARTUP_FAILED)

    imagej.when_imagej_starts(on_start)
    log("imagej.init", env.fiji_dir, "(gui)")
    imagej.init(env.fiji_dir, mode="gui")
    log("init returned (Fiji closed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
