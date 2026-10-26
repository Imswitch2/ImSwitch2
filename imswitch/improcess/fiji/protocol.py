"""The message contract with the Fiji host process, and how the host is launched.

Everything that crosses the process boundary is a plain JSON-compatible dict:
the host is a separate Java-bearing process, and the other side of a socket is
never trusted, so every message is validated on arrival and a malformed one
raises :class:`ProtocolError` instead of reaching the rest of the program.

This module opens no sockets and starts no process; transport and process
management come later. It holds the messages, the typed replies, and
:func:`resolve_launch_config`, which carries the fixes the P0 prototype
verified (bundled JDK, Fiji's own heap policy, exactly two bridge jars).
"""

from __future__ import annotations

import os
import platform
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Sequence

import numpy as np

from imswitch.improcess.fiji.convert import IMAGEJ_DTYPES, FijiImageDescription, SendPlan

#: Bump on any incompatible message change. The host reports its version in
#: ``ping`` and the parent refuses a mismatch.
PROTOCOL_VERSION = 1

_IMAGEJ_DTYPE_NAMES = tuple(d.name for d in IMAGEJ_DTYPES)


class FijiLinkError(RuntimeError):
    """Base of every error the Fiji link raises."""


class ProtocolError(FijiLinkError):
    """A message is malformed, or the two sides do not speak the same protocol."""


class HostError(FijiLinkError):
    """The host ran a request and reported a failure; ``trace`` is its traceback."""

    def __init__(self, message: str, trace: str = ""):
        super().__init__(message)
        self.trace = trace


class FijiNotFound(FijiLinkError):
    """The chosen folder is not a usable Fiji installation."""


class BridgeJarsMissing(FijiLinkError):
    """The imglib2 bridge jars the host needs were not provided."""


# --------------------------------------------------------------------------
# field validation
# --------------------------------------------------------------------------

def _field(d: dict, key: str, kind, *, optional: bool = False, default=None):
    if not isinstance(d, dict):
        raise ProtocolError(f"a message must be an object, got {type(d).__name__}")
    if key not in d:
        if optional:
            return default
        raise ProtocolError(f"message {d.get('op', '?')!r} is missing field {key!r}")
    value = d[key]
    # bool is an int subclass; a flag is never a valid number here.
    if isinstance(value, bool) and kind is not bool:
        raise ProtocolError(f"field {key!r} must be {kind.__name__}, got bool")
    if not isinstance(value, kind):
        raise ProtocolError(f"field {key!r} must be {kind.__name__}, got {type(value).__name__}")
    return value


def _str_list(d: dict, key: str) -> list:
    values = _field(d, key, list)
    if not all(isinstance(v, str) for v in values):
        raise ProtocolError(f"field {key!r} must be a list of strings")
    return list(values)


# --------------------------------------------------------------------------
# requests
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Ping:
    op: ClassVar[str] = "ping"

    def to_dict(self) -> dict:
        return {"op": self.op}


@dataclass(frozen=True)
class Send:
    """Open ``path`` (a raw array file of ``shape_tzcyx``/``dtype``) as an ImagePlus."""

    path: str
    title: str
    calibration: dict
    channel_names: list
    properties: dict
    shape_tzcyx: list
    dtype: str
    op: ClassVar[str] = "send"

    @classmethod
    def from_plan(cls, plan: SendPlan, path) -> "Send":
        return cls(
            path=str(path),
            title=plan.title,
            calibration=dict(plan.calibration),
            channel_names=list(plan.channel_names),
            properties={str(k): str(v) for k, v in plan.properties.items()},
            shape_tzcyx=[int(n) for n in plan.data.shape],
            dtype=np.dtype(plan.data.dtype).name,
        )

    def to_dict(self) -> dict:
        return {
            "op": self.op, "path": self.path, "title": self.title,
            "calibration": dict(self.calibration), "channel_names": list(self.channel_names),
            "properties": dict(self.properties), "shape_tzcyx": list(self.shape_tzcyx),
            "dtype": self.dtype,
        }


@dataclass(frozen=True)
class ListImages:
    op: ClassVar[str] = "list_images"

    def to_dict(self) -> dict:
        return {"op": self.op}


@dataclass(frozen=True)
class Fetch:
    """Write image ``image_id``'s pixels to ``path`` as a raw (T,Z,C,Y,X) array."""

    image_id: int
    path: str
    op: ClassVar[str] = "fetch"

    def to_dict(self) -> dict:
        return {"op": self.op, "image_id": self.image_id, "path": self.path}


@dataclass(frozen=True)
class Events:
    clear: bool = False
    op: ClassVar[str] = "events"

    def to_dict(self) -> dict:
        return {"op": self.op, "clear": self.clear}


@dataclass(frozen=True)
class CloseImage:
    image_id: int
    op: ClassVar[str] = "close_image"

    def to_dict(self) -> dict:
        return {"op": self.op, "image_id": self.image_id}


@dataclass(frozen=True)
class Quit:
    op: ClassVar[str] = "quit"

    def to_dict(self) -> dict:
        return {"op": self.op}


def request_from_dict(d: dict):
    """Validate and decode one request; raise :class:`ProtocolError` on anything off."""
    op = _field(d, "op", str)
    if op == Ping.op:
        return Ping()
    if op == ListImages.op:
        return ListImages()
    if op == Quit.op:
        return Quit()
    if op == Events.op:
        return Events(clear=_field(d, "clear", bool, optional=True, default=False))
    if op == CloseImage.op:
        return CloseImage(image_id=_field(d, "image_id", int))
    if op == Fetch.op:
        return Fetch(image_id=_field(d, "image_id", int), path=_field(d, "path", str))
    if op == Send.op:
        shape = _field(d, "shape_tzcyx", list)
        if len(shape) != 5 or not all(isinstance(n, int) and not isinstance(n, bool) and n > 0 for n in shape):
            raise ProtocolError("field 'shape_tzcyx' must be five positive integers")
        dtype = _field(d, "dtype", str)
        if dtype not in _IMAGEJ_DTYPE_NAMES:
            raise ProtocolError(f"dtype {dtype!r} is not one of {_IMAGEJ_DTYPE_NAMES}")
        properties = _field(d, "properties", dict)
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in properties.items()):
            raise ProtocolError("field 'properties' must map strings to strings")
        return Send(
            path=_field(d, "path", str), title=_field(d, "title", str),
            calibration=dict(_field(d, "calibration", dict)),
            channel_names=_str_list(d, "channel_names"),
            properties=dict(properties), shape_tzcyx=list(shape), dtype=dtype,
        )
    raise ProtocolError(f"unknown op {op!r}")


# --------------------------------------------------------------------------
# replies
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Reply:
    ok: bool
    value: Any = None
    error: str = ""
    trace: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "value": self.value, "error": self.error, "trace": self.trace}

    @classmethod
    def from_dict(cls, d: dict) -> "Reply":
        return cls(
            ok=_field(d, "ok", bool),
            value=d.get("value") if isinstance(d, dict) else None,
            error=_field(d, "error", str, optional=True, default=""),
            trace=_field(d, "trace", str, optional=True, default=""),
        )


def reply_value(d: dict):
    """The value of a successful reply; raise :class:`HostError` for a failed one."""
    reply = Reply.from_dict(d)
    if not reply.ok:
        raise HostError(reply.error or "the Fiji host reported an error", reply.trace)
    return reply.value


@dataclass(frozen=True)
class PingInfo:
    protocol: int
    imagej: str
    ij1: str
    java: str
    headless: bool
    pid: int
    max_heap_mb: int

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict) -> "PingInfo":
        return cls(
            protocol=_field(d, "protocol", int), imagej=_field(d, "imagej", str),
            ij1=_field(d, "ij1", str), java=_field(d, "java", str),
            headless=_field(d, "headless", bool), pid=_field(d, "pid", int),
            max_heap_mb=_field(d, "max_heap_mb", int),
        )


@dataclass(frozen=True)
class FetchInfo:
    image_id: int
    shape_tzcyx: list
    dtype: str
    description: dict

    def to_dict(self) -> dict:
        return {"image_id": self.image_id, "shape_tzcyx": list(self.shape_tzcyx),
                "dtype": self.dtype, "description": dict(self.description)}

    @classmethod
    def from_dict(cls, d: dict) -> "FetchInfo":
        shape = _field(d, "shape_tzcyx", list)
        if len(shape) != 5 or not all(isinstance(n, int) and not isinstance(n, bool) for n in shape):
            raise ProtocolError("field 'shape_tzcyx' must be five integers")
        return cls(
            image_id=_field(d, "image_id", int), shape_tzcyx=list(shape),
            dtype=_field(d, "dtype", str), description=dict(_field(d, "description", dict)),
        )


def images_from_reply(value) -> list[FijiImageDescription]:
    """Decode the value of a ``list_images`` reply."""
    if not isinstance(value, list):
        raise ProtocolError("a list_images reply must be a list")
    try:
        return [FijiImageDescription.from_dict(item) for item in value]
    except (KeyError, TypeError, ValueError) as exc:
        raise ProtocolError(f"malformed image description: {exc!r}") from exc


def check_ping(info: PingInfo) -> None:
    """Refuse a host that speaks another protocol or cannot enumerate images."""
    if info.protocol != PROTOCOL_VERSION:
        raise ProtocolError(
            f"the Fiji host speaks protocol {info.protocol}, this ImSwitch speaks {PROTOCOL_VERSION}"
        )
    if info.headless:
        # Verified in the prototype: WindowManager is empty when headless, so a
        # headless Fiji cannot list the images a user has open.
        raise ProtocolError("the Fiji host runs headless and cannot enumerate open images")


# --------------------------------------------------------------------------
# launching the host
# --------------------------------------------------------------------------

#: pyimagej 1.8.0 crashes with fetch="never" (unbound ``major_version`` in
#: ``imagej._guess_java_version``, verified 2026-10-26). "auto" uses JAVA_HOME
#: when it holds a JDK, which :func:`resolve_launch_config` guarantees.
_JAVA_FETCH = "auto"

_HEAP_LINE = re.compile(r"""^\s*cfg\.max-heap\s*=\s*(['"])(?P<value>[^'"]*)\1""", re.MULTILINE)
_HEAP_VALUE = re.compile(r"^\s*(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>%|[gGmM][bB]?)\s*$")
_BYTES_PER_MB = 2 ** 20
_MB_PER_GB = 1024


@dataclass(frozen=True)
class HostLaunchConfig:
    fiji_dir: Path
    java_home: Path
    max_heap_mb: int | None
    classpath_jars: tuple
    java_fetch: str = _JAVA_FETCH
    notes: tuple = field(default_factory=tuple)

    def java_options(self) -> list[str]:
        # Without -Xmx Java takes 25% of RAM, which ran out of memory on a
        # 2 GB stack in the prototype; Fiji's own launcher policy is used instead.
        return [f"-Xmx{self.max_heap_mb}m"] if self.max_heap_mb is not None else []

    def env(self) -> dict[str, str]:
        env = {
            "FIJI_DIR": str(self.fiji_dir),
            "JAVA_HOME": str(self.java_home),
            "IMSWITCH_FIJI_JAVA_FETCH": self.java_fetch,
            # Exactly these jars, instead of letting jgo resolve imglib2-imglyb:
            # that pulls 38 jars including imglib2 5.12 beside Fiji's 8.0
            # (version skew); the host clears scyjava's endpoints and adds
            # only these (7 s offline start, verified).
            "IMSWITCH_FIJI_CLASSPATH": os.pathsep.join(str(p) for p in self.classpath_jars),
        }
        if self.max_heap_mb is not None:
            env["IMSWITCH_FIJI_MAX_HEAP_MB"] = str(self.max_heap_mb)
        return env


def resolve_launch_config(
    fiji_dir,
    *,
    ram_bytes: int,
    bridge_jars: Sequence,
) -> HostLaunchConfig:
    """Work out how to start the host for ``fiji_dir`` (pure; RAM is an argument)."""
    notes: list[str] = []
    root = _fiji_root(Path(fiji_dir), notes)
    java_home = _find_java_home(root)
    max_heap_mb = _max_heap_mb(root, ram_bytes, notes)
    return HostLaunchConfig(
        fiji_dir=root,
        java_home=java_home,
        max_heap_mb=max_heap_mb,
        classpath_jars=_bridge_jars(bridge_jars),
        notes=tuple(notes),
    )


def _fiji_root(path: Path, notes: list[str]) -> Path:
    if (path / "jars").is_dir():
        return path
    # Fiji Latest is Fiji/{jars,plugins,java}; the macOS Fiji.app inside it is
    # only the launcher bundle, a natural thing for a user to pick.
    if path.name.lower().endswith(".app") and (path.parent / "jars").is_dir():
        notes.append(f"{path} is the launcher bundle; using its parent {path.parent}")
        return path.parent
    raise FijiNotFound(
        f"{path} has no 'jars' folder; choose the Fiji folder that contains jars, plugins and java "
        "(on macOS the folder holding Fiji.app, not Fiji.app itself)"
    )


def _platform_token() -> str | None:
    machine = platform.machine().lower()
    if sys.platform == "darwin":
        return "macos-arm64" if machine in ("arm64", "aarch64") else "macos-x64"
    if sys.platform.startswith("win"):
        return "win64"
    if sys.platform.startswith("linux"):
        return "linux64"
    return None


def _find_java_home(root: Path) -> Path:
    homes = sorted(
        {
            bin_dir.parent
            for bin_dir in (root / "java").rglob("bin")
            if bin_dir.is_dir() and ((bin_dir / "java").exists() or (bin_dir / "java.exe").exists())
        }
    )
    if not homes:
        raise FijiNotFound("this Fiji has no bundled Java; install Fiji Latest")
    if len(homes) == 1:
        return homes[0]
    token = _platform_token()
    matching = [h for h in homes if token and token in h.relative_to(root / "java").as_posix().lower()]
    if len(matching) == 1:
        return matching[0]
    raise FijiNotFound(
        "this Fiji bundles several Java installations and none is the only match for this platform: "
        + ", ".join(str(h) for h in (matching or homes))
    )


def _max_heap_mb(root: Path, ram_bytes: int, notes: list[str]) -> int | None:
    unknown = "Fiji's heap policy not found; Java's default heap applies"
    try:
        text = (root / "config" / "jaunch" / "fiji.toml").read_text(encoding="utf-8")
    except OSError:
        notes.append(unknown)
        return None
    line = _HEAP_LINE.search(text)
    value = _HEAP_VALUE.match(line.group("value")) if line else None
    if value is None:
        notes.append(unknown)
        return None
    number, unit = float(value.group("number")), value.group("unit").lower()
    if unit == "%":
        return int(ram_bytes * number / 100) // _BYTES_PER_MB
    return int(number * _MB_PER_GB) if unit.startswith("g") else int(number)


def _bridge_jars(bridge_jars: Sequence) -> tuple:
    paths = [Path(p) for p in bridge_jars]
    found, missing = [], []
    for prefix in ("imglib2-imglyb-", "imglib2-unsafe-"):
        match = next((p for p in paths if p.name.startswith(prefix) and p.suffix == ".jar" and p.exists()), None)
        (found if match else missing).append(match or f"{prefix}*.jar")
    if missing:
        raise BridgeJarsMissing(f"missing bridge jars: {', '.join(missing)}")
    return tuple(found)


__all__ = [
    "PROTOCOL_VERSION", "FijiLinkError", "ProtocolError", "HostError", "FijiNotFound",
    "BridgeJarsMissing", "Ping", "Send", "ListImages", "Fetch", "Events", "CloseImage", "Quit",
    "request_from_dict", "Reply", "reply_value", "PingInfo", "FetchInfo", "images_from_reply",
    "check_ping", "HostLaunchConfig", "resolve_launch_config",
]
