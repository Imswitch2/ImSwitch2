"""A protocol-speaking stand-in for the Fiji host, with no Java.

Same launch contract as :mod:`imswitch.improcess.fiji.host`: the authkey as one
hex line on stdin, ``IMSWITCH_FIJI_ADDR`` in the environment. It keeps arrays
in a dict keyed by negative ids (ImageJ's own convention) and implements every
request, so the session's process handling, failure paths and the conversion
round trip can be tested without Fiji.

``IMSWITCH_FIJI_FAKE`` holds comma-separated scenarios:

* ``exit_at_start``: print a line to stderr and exit 3 before connecting;
* ``never_connect``: sleep forever;
* ``wrong_protocol``: report another protocol version;
* ``headless``: report a headless Fiji;
* ``connect_after=S``: sleep S seconds before connecting, so a stop can land
  while the session is still starting;
* ``die_on_request=N``: ``os._exit`` on the Nth request received (the start-up
  ping is request 1);
* ``hang_on_request=N``: receive the Nth request and never reply, like a Fiji
  sitting in a modal dialog;
* ``exit_after=S``: ``os._exit`` by itself S seconds after connecting, like a
  user closing Fiji's window;
* ``slow_quit``: acknowledge ``Quit`` but keep running, so the session's kill
  path is exercised.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from multiprocessing.connection import Client

import numpy as np

from imswitch.improcess.fiji.protocol import (
    PROTOCOL_VERSION, CloseImage, Events, Fetch, FetchInfo, FijiImageDescription, ListImages,
    Ping, PingInfo, Quit, Reply, RunCommand, Send, request_from_dict,
)

EXIT_AT_START_CODE = 3
DIED_CODE = 7


def scenarios(text: str) -> dict:
    out = {}
    for item in filter(None, (s.strip() for s in text.split(","))):
        name, _, value = item.partition("=")
        out[name] = value
    return out


class FakeFiji:
    def __init__(self, flags: dict):
        self.flags = flags
        self.images: dict[int, dict] = {}
        self.events: list[dict] = []
        self.t0 = time.monotonic()
        self.next_id = -1
        self.current = None

    def _event(self, kind, **fields):
        self.events.append({"time": round(time.monotonic() - self.t0, 3), "kind": kind, **fields})

    def _new_id(self) -> int:
        image_id, self.next_id = self.next_id, self.next_id - 1
        return image_id

    def _image(self, image_id):
        if image_id not in self.images:
            raise KeyError(f"no open image with id {image_id}")
        return self.images[image_id]

    def _describe(self, image_id) -> dict:
        img = self.images[image_id]
        t, z, c, y, x = img["data"].shape
        cal = img["calibration"]
        unit = cal.get("unit") or "pixel"
        names = list(img["channel_names"]) + [None] * c
        interval = cal.get("frame_interval_s")
        return FijiImageDescription(
            image_id=image_id, title=img["title"], dims_xyczt=(x, y, c, z, t),
            bit_depth={"uint8": 8, "uint16": 16, "float32": 32}[img["data"].dtype.name],
            pixel_width=cal.get("pixel_width") or 1.0, pixel_height=cal.get("pixel_height") or 1.0,
            pixel_depth=cal.get("pixel_depth") or 1.0, x_unit=unit, y_unit=unit, z_unit=unit,
            x_origin=0.0, y_origin=0.0, z_origin=0.0,
            frame_interval=interval or 0.0, time_unit="s" if interval else "sec",
            properties=dict(img["properties"]), channel_labels=tuple(names[:c]), changes=False,
        ).to_dict()

    def handle(self, request):
        if isinstance(request, Ping):
            return PingInfo(
                protocol=PROTOCOL_VERSION + 1 if "wrong_protocol" in self.flags else PROTOCOL_VERSION,
                imagej="fake", ij1="fake", java="21", headless="headless" in self.flags,
                pid=os.getpid(), max_heap_mb=int(os.environ.get("IMSWITCH_FIJI_MAX_HEAP_MB", "0")),
            ).to_dict()
        if isinstance(request, Send):
            data = np.load(request.path)
            if list(data.shape) != list(request.shape_tzcyx) or data.dtype.name != request.dtype:
                raise ValueError("the file does not match the message")
            image_id = self._new_id()
            self.images[image_id] = {
                "data": data, "title": request.title, "calibration": dict(request.calibration),
                "channel_names": list(request.channel_names), "properties": dict(request.properties),
            }
            self.current = image_id
            self._event("opened", id=image_id, title=request.title)
            return {"image_id": image_id}
        if isinstance(request, ListImages):
            return [self._describe(i) for i in self.images]
        if isinstance(request, Fetch):
            img = self._image(request.image_id)
            np.save(request.path, img["data"])
            return FetchInfo(
                image_id=request.image_id, shape_tzcyx=list(img["data"].shape),
                dtype=img["data"].dtype.name, description=self._describe(request.image_id),
            ).to_dict()
        if isinstance(request, RunCommand):
            self._event("command", command=request.command, current_id=self.current, options=request.options or None)
            if request.command == "Duplicate...":
                source = self._image(request.image_id)
                image_id = self._new_id()
                self.images[image_id] = {**source, "data": source["data"].copy(), "title": "dup"}
                self.current = image_id
                self._event("opened", id=image_id, title="dup")
            return {"current_id": self.current}
        if isinstance(request, CloseImage):
            self._image(request.image_id)
            del self.images[request.image_id]
            self._event("closed", id=request.image_id)
            return {}
        if isinstance(request, Events):
            out = list(self.events)
            if request.clear:
                self.events.clear()
            return {"events": out, "recorder": None}
        if isinstance(request, Quit):
            return None
        raise TypeError(f"unhandled request {request!r}")


def main() -> int:
    flags = scenarios(os.environ.get("IMSWITCH_FIJI_FAKE", ""))
    authkey = bytes.fromhex(sys.stdin.readline().strip())
    if "exit_at_start" in flags:
        print("fake host: refusing to start", file=sys.stderr, flush=True)
        return EXIT_AT_START_CODE
    if "never_connect" in flags:
        while True:
            time.sleep(1)
    if "connect_after" in flags:
        time.sleep(float(flags["connect_after"]))
    host, _, port = os.environ["IMSWITCH_FIJI_ADDR"].rpartition(":")
    conn = Client((host, int(port)), authkey=authkey)
    fiji = FakeFiji(flags)
    if "exit_after" in flags:
        timer = threading.Timer(float(flags["exit_after"]), os._exit, args=(DIED_CODE,))
        timer.daemon = True
        timer.start()
    die_at = int(flags["die_on_request"]) if "die_on_request" in flags else None
    hang_at = int(flags["hang_on_request"]) if "hang_on_request" in flags else None
    received = 0
    while True:
        try:
            raw = conn.recv()
        except (EOFError, OSError):
            return 0
        received += 1
        if die_at is not None and received == die_at:
            os._exit(DIED_CODE)
        if hang_at is not None and received == hang_at:
            while True:
                time.sleep(1)
        try:
            request = request_from_dict(raw)
            reply = Reply(True, fiji.handle(request))
            quitting = isinstance(request, Quit)
        except Exception as exc:  # noqa: BLE001
            reply, quitting = Reply(False, error=f"{type(exc).__name__}: {exc}", trace=traceback.format_exc()), False
        conn.send(reply.to_dict())
        if quitting and "slow_quit" not in flags:
            return 0


if __name__ == "__main__":
    sys.exit(main())
