"""Standalone IC4 probe for The Imaging Source cameras (Phase 0 de-risking).

Verifies, OUTSIDE ImSwitch, that the camera can deliver exactly one distinct
frame per trigger via a push-based QueueSink -- the capability the legacy
pyicic/poll-based path cannot provide. See
docs/design/plans/tis-camera-ic4-migration.md.

Usage (from the Imswitch2 conda env):

    # 1. Just list what IC4 can see:
    python utility_scripts/ic4_probe.py --list

    # 2. Validate the whole QueueSink path with software triggers (no TTL needed):
    python utility_scripts/ic4_probe.py --mode software --count 10

    # 3. The real test: wait for external TTL pulses from the TriggerScope:
    python utility_scripts/ic4_probe.py --mode hardware --count 10 --timeout 30

A PASS in hardware mode (N frames, N distinct) means IC4 solves the
duplicate-frame problem and we can build the real TISManager on it.
"""

import argparse
import gc
import sys
import threading
import time

try:
    import imagingcontrol4 as ic4
except ImportError:
    sys.exit("imagingcontrol4 not installed. Run: pip install imagingcontrol4")


class ProbeListener(ic4.QueueSinkListener):
    """Collects every frame the camera pushes, with arrival timestamps."""

    def __init__(self):
        self.frames = []
        self.timestamps = []
        self._lock = threading.Lock()

    def sink_connected(self, sink, image_type, min_buffers_required) -> bool:
        # Must allocate buffers here (or the sink does the minimum itself).
        sink.alloc_and_queue_buffers(max(min_buffers_required, 8))
        print(f"  sink connected: {image_type} (min buffers {min_buffers_required})")
        return True

    def frames_queued(self, sink):
        # Runs on the sink's own thread. Drain everything available.
        while True:
            buf = sink.try_pop_output_buffer()
            if buf is None:
                return
            try:
                # numpy_copy() -- NOT numpy_wrap(): the SDK reuses buffers, so a
                # wrapped view becomes garbage once we release it.
                frame = buf.numpy_copy()
            finally:
                buf.release()
            with self._lock:
                self.frames.append(frame)
                self.timestamps.append(time.perf_counter())

    def count(self) -> int:
        with self._lock:
            return len(self.frames)


def show_enum(pm, prop_id, label):
    """Print an enumeration property's options and current value."""
    try:
        prop = pm.find_enumeration(prop_id)
        entries = [e.name for e in prop.entries]
        print(f"  {label:18}: {entries}  (now: {prop.value})")
        return entries
    except Exception as exc:
        print(f"  {label:18}: <unavailable: {exc}>")
        return []


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="list devices and exit")
    ap.add_argument("--device", type=int, default=0, help="device index (default 0)")
    ap.add_argument("--mode", choices=["software", "hardware"], default="software",
                    help="software = self-fired triggers; hardware = wait for TTL")
    ap.add_argument("--count", type=int, default=10, help="frames to capture")
    ap.add_argument("--timeout", type=float, default=30.0,
                    help="hardware mode: seconds to wait for all frames")
    ap.add_argument("--exposure", type=float, default=None,
                    help="exposure time in microseconds")
    ap.add_argument("--source", default=None,
                    help="TriggerSource value (e.g. Line1); default = camera's current")
    args = ap.parse_args()

    try:
        ic4.Library.init()
    except RuntimeError:
        pass  # already initialised in this process

    try:
        devices = ic4.DeviceEnum.devices()
        if not devices:
            print("No IC4 devices found.")
            print("Check the IC4 GenTL USB3 Vision producer is installed and the "
                  "camera is not held open by another application.")
            return 1

        print(f"Found {len(devices)} device(s):")
        for i, d in enumerate(devices):
            print(f"  [{i}] {d.model_name}  serial={d.serial}")
        if args.list:
            return 0

        if args.device >= len(devices):
            print(f"--device {args.device} out of range")
            return 1

        grabber = ic4.Grabber()
        grabber.device_open(devices[args.device])
        pm = grabber.device_property_map
        print(f"\nOpened [{args.device}] {devices[args.device].model_name}")

        print("\nTrigger-related properties:")
        show_enum(pm, ic4.PropId.TRIGGER_SELECTOR, "TriggerSelector")
        show_enum(pm, ic4.PropId.TRIGGER_MODE, "TriggerMode")
        sources = show_enum(pm, ic4.PropId.TRIGGER_SOURCE, "TriggerSource")
        show_enum(pm, ic4.PropId.TRIGGER_ACTIVATION, "TriggerActivation")
        show_enum(pm, ic4.PropId.PIXEL_FORMAT, "PixelFormat")

        if args.exposure is not None:
            if pm.try_set_value(ic4.PropId.EXPOSURE_TIME, args.exposure):
                print(f"\nExposureTime set to {args.exposure} us")
            else:
                print(f"\nWARNING: could not set ExposureTime={args.exposure}")

        # --- Arm the trigger -------------------------------------------------
        pm.try_set_value(ic4.PropId.TRIGGER_SELECTOR, "FrameStart")
        if args.mode == "software":
            pm.try_set_value(ic4.PropId.TRIGGER_SOURCE, "Software")
        elif args.source:
            if not pm.try_set_value(ic4.PropId.TRIGGER_SOURCE, args.source):
                print(f"WARNING: could not set TriggerSource={args.source}; "
                      f"available: {sources}")
        pm.set_value(ic4.PropId.TRIGGER_MODE, "On")
        print(f"\nTriggerMode=On, TriggerSource="
              f"{pm.get_value_str(ic4.PropId.TRIGGER_SOURCE)}")

        # --- Stream ----------------------------------------------------------
        listener = ProbeListener()
        print("\nStarting stream...")
        grabber.stream_setup(ic4.QueueSink(listener),
                             setup_option=ic4.StreamSetupOption.ACQUISITION_START)

        t0 = time.perf_counter()
        if args.mode == "software":
            print(f"Firing {args.count} software triggers...")
            for i in range(args.count):
                pm.execute_command(ic4.PropId.TRIGGER_SOFTWARE)
                time.sleep(0.05)
            time.sleep(0.5)  # let the last frame land
        else:
            print(f"Waiting up to {args.timeout}s for {args.count} external "
                  f"trigger pulses -- pulse the TriggerScope now...")
            deadline = t0 + args.timeout
            last_seen = 0
            while listener.count() < args.count and time.perf_counter() < deadline:
                time.sleep(0.05)
                seen = listener.count()
                if seen != last_seen:
                    print(f"  frame {seen}/{args.count}")
                    last_seen = seen

        grabber.stream_stop()

        # --- Report ----------------------------------------------------------
        frames = listener.frames
        stamps = listener.timestamps
        n = len(frames)
        distinct = len({f.tobytes() for f in frames})

        print("\n" + "=" * 58)
        print(f"  frames received : {n}  (expected {args.count})")
        print(f"  DISTINCT frames : {distinct}")
        if n:
            print(f"  shape / dtype   : {frames[0].shape} / {frames[0].dtype}")
            if len(stamps) > 1:
                gaps = [b - a for a, b in zip(stamps, stamps[1:])]
                print(f"  frame intervals : min {min(gaps)*1000:.1f} ms, "
                      f"max {max(gaps)*1000:.1f} ms")
        print("=" * 58)

        if n == 0:
            print("RESULT: FAIL -- no frames. No trigger reached the camera "
                  "(check TTL wiring/TriggerSource), or the trigger never fired.")
            rc = 1
        elif distinct < n:
            print(f"RESULT: FAIL -- only {distinct}/{n} distinct. Duplicate "
                  "frames are still being delivered.")
            rc = 1
        elif n < args.count:
            print(f"RESULT: PARTIAL -- {n}/{args.count} frames, all distinct. "
                  "Frames are unique but some triggers were missed.")
            rc = 1
        else:
            print("RESULT: PASS -- one distinct frame per trigger. "
                  "IC4 QueueSink solves the duplicate-frame problem.")
            rc = 0

        pm.try_set_value(ic4.PropId.TRIGGER_MODE, "Off")
        grabber.device_close()
        return rc

    finally:
        # Release every IC4 object BEFORE closing the library. Grabber,
        # PropertyMap and DeviceInfo finalizers call back into the library, so
        # if they run after Library.exit() each one raises
        # "Library.init was not called" during interpreter shutdown.
        grabber = pm = devices = listener = None  # noqa: F841
        gc.collect()
        try:
            ic4.Library.exit()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
