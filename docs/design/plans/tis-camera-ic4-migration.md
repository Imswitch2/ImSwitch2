# TIS Camera → IC Imaging Control 4 (IC4) Migration — Plan

**Status:** Phases 1–3 implemented as the `imswitch-device-tis` **plugin**
(mock-complete, 22 tests green); the real IC4 path is written but **gated on the
Phase 0 rig probe**. In-tree `TISManager` untouched.
**Date:** 2026-07-21 (revised same day after source + vendor-API verification;
re-scoped to a plugin later the same day).
**Scope:** Replace the vendored `pyicic` ctypes wrapper for The Imaging Source
cameras with the first-party `imagingcontrol4` (IC4) Python library, primarily to
get **reliable per-trigger frame capture** for TriggerScope raster / BeadRec.
**Hardware in question:** DMK 33UX250 (USB3 Vision, Sony IMX250, 5 MP global
shutter, 75 fps), external hardware trigger wired to the camera.

> **What changed in this revision.** The diagnosis was correct but imprecise, and
> the plan was missing an in-repo precedent that solves the same problem class.
> Specifically:
> - The root cause is **not polling** — it is polling *a single overwritten
>   buffer with no queue behind it*. [`ThorCamTSIManager`](../../../imswitch/imcontrol/model/managers/detectors/ThorCamTSIManager.py)
>   polls and works correctly, because the TSI SDK queues. See §2.
> - The IC4 code sample was **not runnable**: it omitted `ic4.Library.init()` and
>   the mandatory `sink_connected` listener method. Corrected against the vendor's
>   own example in §3.
> - Prerequisite 1 (Python 3.10 wheel) is **resolved** — no longer an unknown.
> - Prerequisite 3 can be **split**, and its first half validated *without* the
>   TriggerScope, via IC4's software-trigger command. See §4.
> - Added: a lower-risk IC3 fallback that the original ruled out prematurely (§6),
>   three latent bugs in the current TIS code found while verifying (§7), and the
>   docs/config touchpoints the migration breaks (§5, Phase 4).

> **Delivery decision (2026-07-21): this ships as a device plugin, not an in-tree
> rewrite.** `examples/plugins/imswitch-device-tis/`, contributing the detector id
> `tis.camera-ic4`. The plugin boundary *is* the rollback mechanism this plan
> already wanted: install → IC4, don't install → the untouched in-tree
> `TISManager` on IC3. See §5a for the one hazard this creates.

---

## 1. Why we are doing this (the problem)

The current TIS stack cannot reliably capture one distinct frame per hardware
trigger. Two observed symptoms:

- **Triggered scan (1 image/position):** every position returns the *same* image.
- **Continuous recording (e.g. 10 frames):** only the first frame differs; the
  rest are identical.

### Root cause

Every consumer pulls frames on its own schedule through `CameraTIS.grabFrame()`
([tiscamera.py:49](../../../imswitch/imcontrol/model/interfaces/tiscamera.py:49)):

- Live view: `LVWorker` timer → `getLatestFrame()`
- BeadRec: `BeadWorker` → `readChunk()` → `getChunk()`
- Recording: `RecordingManager` → `getLatestFrame()` / `readChunk()`

Neither legacy camera mode fits this for triggered capture:

| Mode | `get_image_data()` (continuous) | `snap_image()` (snap mode) |
|---|---|---|
| Blocking? | No — returns current buffer | **Yes** — waits for a fresh frame |
| No new trigger yet? | Returns last buffer (a stale frame) | **Raises** `IC_Exception` after timeout ([IC_Camera.py:424](../../../imswitch/imcontrol/model/interfaces/pyicic/IC_Camera.py:424)) → frame lost |
| Result | Frames flow but **duplicate** (no poll↔trigger sync) | Single waiter starves the others → **0 frames** for recording/BeadRec |

- Continuous mode + polling → the buffer is re-read between triggers →
  **duplicate frames** (the original bug).
- Snap mode + polling (attempted, reverted) → `snap_image()` blocks and raises
  when a poll doesn't line up with a trigger, and multiple consumers compete for
  each trigger's single frame → **0 frames** captured by recording/BeadRec, while
  live view still shows the occasional snapped frame.

### The precise statement of the defect

The original conclusion — *"frame delivery must become push-based"* — is the right
fix but the wrong reason, and the difference changes how much we have to rebuild.

`grabFrame()` reads **one buffer that the driver overwrites in place**. Two polls
between two triggers see the same bytes; two triggers between two polls lose one
frame. Neither is a scheduling problem; both are a **missing queue**.

The proof is in this repo. [`ThorCamTSIManager.getChunk()`](../../../imswitch/imcontrol/model/managers/detectors/ThorCamTSIManager.py:244)
is *also* poll-based — it calls `get_pending_frame()` in a drain loop — and it
captures every hardware trigger correctly, because the Thorlabs SDK retains each
frame in its own queue until popped.

**So the requirement is: a queue that retains every frame, drained
destructively.** Whether the bottom of the stack is push (a callback) or pull (a
polling SDK) is an implementation detail. IC4's `QueueSink` supplies exactly this
queue, and — importantly — the **top** of the stack (`getChunk` → `readChunk` →
consumers) does not change at all. `readChunk()` already fans one destructive
hardware drain out to every registered consumer
([DetectorManager.py:323](../../../imswitch/imcontrol/model/managers/detectors/DetectorManager.py:323)),
which is precisely the multi-consumer contention described above.

> **Note — do not "fix" `TISManager.getChunk()`'s shape.** It already returns
> 3-D via `grabFrame()[np.newaxis, :, :]`
> ([TISManager.py:113](../../../imswitch/imcontrol/model/managers/detectors/TISManager.py:113)),
> so it does *not* have the 2-D corruption bug that ThorCam TSI had (see
> [advanced_scan_triggered_recording_audit.md](../../advanced_scan_triggered_recording_audit.md)).
> Its chunks are correctly shaped — they are just duplicates.

### Secondary pain points with `pyicic`

- `pyicic` is a hand-maintained **ctypes translation of the legacy IC Imaging
  Control 3.x C DLL** (`tisgrabber`), vendored from
  [morefigs/py-ic-imaging-control](https://github.com/morefigs/py-ic-imaging-control)
  (MIT). It carries dead and broken code:
  `wait_til_frame_ready()` uses `time.clock()`, removed in Python 3.8
  ([IC_Camera.py:526](../../../imswitch/imcontrol/model/interfaces/pyicic/IC_Camera.py:526))
  — it cannot run at all on our interpreter.
- **Hardware trigger arming is not done in software.** The only call is
  `enable_trigger(False)` at init
  ([tiscamera.py:32](../../../imswitch/imcontrol/model/interfaces/tiscamera.py:32));
  arming happens manually in the vendor's IC properties dialog. The wrapper also
  swallows this call's error return unconditionally
  ([IC_Camera.py:297-299](../../../imswitch/imcontrol/model/interfaces/pyicic/IC_Camera.py:297)),
  so a failed arm is silent.

---

## 2. The IC4 option

The Imaging Source's current SDK is **IC Imaging Control 4 (IC4)**, with a
first-party Python library **`imagingcontrol4`** on PyPI.

- Maintained first-party Python API (bindings alongside C++/.NET/C), **not** a
  bespoke ctypes translation.
- GenICam / GenTL compliant; needs the **IC4 GenTL Producer** (USB3 Vision
  variant) installed alongside `pip install imagingcontrol4`.
- **DMK 33UX250 is supported** — a USB3 Vision device, driven by the IC4-GenTL
  USB3 Vision producer.

### Why IC4 fixes our problem

- **`QueueSink`** retains every delivered frame in a queue and hands them over via
  `sink.pop_output_buffer()`. This is the missing queue from §1 — the thing we
  would otherwise hand-write as a ctypes callback plus a deque against the old DLL.
- **Trigger is a first-class software property**: `TriggerMode` / `TriggerSelector`
  / `TriggerSource` through `grabber.device_property_map`. ImSwitch can **arm per
  bead scan and disarm afterwards**, replacing the manual IC-dialog step.
- **A software trigger command exists** (`PropId.TRIGGER_SOFTWARE`), so the whole
  capture path is testable without the TriggerScope wired up.
- Exposure / Gain / ROI become clean GenICam properties (`ExposureTime`, `Gain`,
  `Width` / `Height` / `OffsetX` / `OffsetY`), replacing the ROI frame-filter hack
  in [`setROI`](../../../imswitch/imcontrol/model/interfaces/tiscamera.py:62).

---

## 3. Verified minimal API shape

Adapted from the vendor's own
[`save-bmp-on-trigger.py`](https://github.com/TheImagingSource/ic4-examples/blob/master/python/image-acquisition/save-bmp-on-trigger/save-bmp-on-trigger.py)
— i.e. our exact use case, checked against the published example rather than
written from the prose docs. **The five commented items below are all things the
first draft of this plan got wrong or omitted; each one is load-bearing.**

```python
import imagingcontrol4 as ic4

# (1) MANDATORY. Nothing in the library works before this; there is also
#     ic4.Library.init_context(...) as a context-manager form.
ic4.Library.init()

grabber = ic4.Grabber(ic4.DeviceEnum.devices()[0])
pm = grabber.device_property_map

# (2) Reset the device to a known state. Our camera is currently configured by
#     hand in the vendor GUI, so it WILL carry leftover state otherwise.
pm.try_set_value(ic4.PropId.USER_SET_SELECTOR, "Default")
pm.try_set_value(ic4.PropId.USER_SET_LOAD, 1)

pm.set_value(ic4.PropId.EXPOSURE_TIME, 5000.0)          # µs
# (3) TriggerSelector must be set before TriggerMode on cameras that expose it;
#     try_set_value tolerates models that don't.
pm.try_set_value(ic4.PropId.TRIGGER_SELECTOR, "FrameStart")
pm.set_value(ic4.PropId.TRIGGER_MODE, "On")             # arm hardware trigger

class Listener(ic4.QueueSinkListener):
    # (4) sink_connected is NOT optional and MUST return True, or the stream
    #     never connects. The first draft only mentioned frames_queued.
    def sink_connected(self, sink, image_type, min_buffers_required) -> bool:
        return True

    def frames_queued(self, sink):
        buffer = sink.pop_output_buffer()
        # (5) numpy_copy(), NOT numpy_wrap(). numpy_wrap returns a VIEW that is
        #     only valid while the buffer lives and must never leave this
        #     function — the vendor example says so explicitly. Handing a wrap
        #     to our deque is exactly the frame-corruption failure mode.
        frame = buffer.numpy_copy()
        ...  # append to bounded deque under a lock

listener = Listener()
sink = ic4.QueueSink(listener)
grabber.stream_setup(sink, setup_option=ic4.StreamSetupOption.ACQUISITION_START)

# Teardown order matters: stream_stop() before the listener can be collected.
grabber.stream_stop()
pm.set_value(ic4.PropId.TRIGGER_MODE, "Off")
grabber.device_close()
```

Signature confirmed from the API reference:
`stream_setup(sink=None, display=None, setup_option=StreamSetupOption.ACQUISITION_START)`,
with `ACQUISITION_START = 1` and `DEFER_ACQUISITION_START = 0`.

---

## 4. Prerequisites & unknowns

| # | Item | Status |
|---|---|---|
| 1 | `imagingcontrol4` wheel for Python 3.10 | **Resolved.** Requires Python ≥3.8; current release 1.5.3.3316 (2026-05-07) ships Windows x86-64/ARM64 and manylinux wheels. Just confirm the install in the `Imswitch2` env. |
| 2 | IC4 GenTL USB3 producer installs; DMK enumerates under it | **Open — rig only.** The camera is currently bound to the legacy IC3 driver. IC3 and IC4 producers generally coexist, but `ic4.DeviceEnum.devices()` must actually list the 33UX250. |
| 3a | `TriggerMode=On` + QueueSink yields exactly one frame per **software** trigger | **Open — but testable without the TriggerScope**, via `pm.execute_command(ic4.PropId.TRIGGER_SOFTWARE)`. |
| 3b | Same, per external **TTL pulse** from the TriggerScope | **Open — rig only.** |

Splitting 3 matters: 3a exercises the entire sink/queue/copy path and fails loudly
on any API misuse, leaving 3b to test only the physical wiring and trigger source.
A failure after 3a passes is a wiring or `TriggerSource` problem, not a code problem.

### De-risking step (do first, before touching ImSwitch)

Write a standalone IC4 probe script outside the app, at
`utility_scripts/ic4_probe.py`:

- `Library.init()` → enumerate → open the DMK 33UX250; print every device's
  model/serial/interface
- reset the user set, set `TriggerSelector`/`TriggerMode`, dump the *actual*
  property names, units and value ranges the 33UX250 exposes
- run a `QueueSink`; in `frames_queued`, `pop_output_buffer()`, `numpy_copy()`,
  and log frame number / timestamp / a cheap content hash
- **first** fire N software triggers and assert N distinct frames in order
  (unknown 3a)
- **then** pulse the TriggerScope and assert the same (unknown 3b)

The content hash is the point: frame *count* alone would have looked healthy under
the original bug, since duplicates still arrive. Distinctness is the assertion that
actually discriminates.

---

## 5a. Shipping this as a plugin

ImSwitch2 already has the device-plugin machinery (registry-first manager
resolution, manifests, schemas, setup templates) and a precedent in
`examples/plugins/imswitch-device-thorlabs`, which bundles the ThorCam TSI
detector and the Kinesis positioner. A TIS/IC4 plugin fits that mould exactly.

It also solves this plan's rollback problem for free. §5 Phase 4 says "don't
delete the old path until the rig signs off"; with a plugin, the old path is
*structurally* untouched rather than untouched by discipline:

| | Resolves to |
|---|---|
| Plugin not installed | in-tree `TISManager` → `pyicic` / IC3 |
| Plugin installed, setup says `TISManager` | in-tree `TISManager` → `pyicic` / IC3 |
| Plugin installed, setup says `tis.camera-ic4` | plugin → IC4 |

### The hazard: do not alias `TISManager`

The Thorlabs plugin declares `manager_name_aliases: ["ThorCamTSIManager"]`, so
existing setups resolve to it unchanged. **Copying that here would be a serious
mistake**, and it is worth being explicit about why, because the precedent
actively invites it.

`MultiManager._resolveManagerClass` resolves the plugin registry **before** the
legacy in-tree import path, and when both would resolve it takes the registry and
logs a warning
([MultiManager.py:57-97](../../../imswitch/imcontrol/model/managers/MultiManager.py:57)).
The Thorlabs extraction was a *pure move* — same class, new home — so that
precedence is harmless. This is a **rewrite onto a different SDK**. An alias would
mean that merely running `pip install imswitch-device-tis` silently switches every
existing TIS setup from IC3 to an IC4 driver that has never seen hardware, with a
log line as the only signal.

(Note the registry's built-in guard does *not* protect us here: it raises on
plugins shadowing registered **built-ins**, but `TISManager` is not in
`builtins.py` — it resolves through the legacy import fallback, which loses.)

So the plugin claims **no aliases**. Migration is opt-in per setup file. After the
rig signs off and the in-tree `TISManager` is deleted, add the alias then — the
switch becomes one deliberate migration instead of a side effect of an install.
A test pins this: `test_manifest_does_not_claim_the_legacy_tismanager_name`.

### What was built

```
examples/plugins/imswitch-device-tis/
├── src/imswitch_device_tis/
│   ├── imswitch.json              # contributes tis.camera-ic4, no aliases
│   ├── _frame_queue.py            # bounded, locked, drop-oldest + warn-once
│   ├── _ic4_driver.py             # IC4Camera + trigger-gated MockIC4Camera
│   ├── detectors/tis_camera_ic4.py# TISCameraIC4Manager
│   ├── schemas/…                  # managerProperties JSON schema
│   └── setup_templates/…          # runnable mock setup
└── tests/                         # 22 tests, no hardware
```

Real and mock drivers share `FrameQueue`, so the mock-backed tests exercise the
same draining and overflow code that will run against hardware.

Verified without installing: the manifest parses through `parse_manifest`,
registers alongside the 9 built-ins without conflict, `tis.camera-ic4` loads
`TISCameraIC4Manager`, and `resolve('detector', 'TISManager')` still returns
`None` — i.e. the legacy fallback keeps it. 103 core plugin/detector tests stay
green.

The two load-bearing tests were **mutation-checked** rather than assumed:
reintroducing the duplicate-frame bug (constant frame content) fails
`test_each_trigger_yields_a_distinct_frame`; returning a 2-D chunk fails seven
tests, including the `readChunk` fan-out. A test suite for a bug this subtle is
worth nothing if it passes against the bug.

---

## 5. Implementation plan

Phases 1–3 are **done inside the plugin**; Phase 0 and Phase 4 remain.

### Phase 0 — Probe & validate (no ImSwitch changes)
- Standalone IC4 probe script; confirm prerequisites 1, 2, 3a, 3b.
- Record the real property names/values for the 33UX250 (exposure units, gain
  range, trigger source string, pixel format) — Phase 2 depends on these being
  facts, not guesses.

### Phase 1 — New IC4-backed interface — **done (in the plugin)**
- ~~Add `imswitch/imcontrol/model/interfaces/tiscamera_ic4.py`~~ → shipped as
  `imswitch_device_tis/_ic4_driver.py`: a `QueueSinkListener` appending each
  `buffer.numpy_copy()` to a **bounded, thread-safe** `FrameQueue`.
- Hold a strong reference to the listener for the grabber's whole lifetime, and
  always `stream_stop()` before dropping it (vendor requirement — see §3).
- Keep [`MockCameraTIS`](../../../imswitch/imcontrol/model/interfaces/tiscamera_mock.py)
  for hardware-free runs. Extend it with the trigger-gated frame production that
  [`MockThorTSICamera`](../../../imswitch/imcontrol/model/interfaces/thorcamera_tsi.py:357)
  already implements (`simulate_hardware_trigger(n)`, frames only on pending
  triggers) — that is what makes the Phase 2 tests meaningful rather than
  tautological.
- Keep `pyicic` in-tree during the transition as a fallback.

### Phase 2 — Manager on IC4 — **done (as `TISCameraIC4Manager`)**

Note this is a *new* manager beside the in-tree one, not a rewire of it.

| `DetectorManager` method | IC4 implementation |
|---|---|
| `startAcquisition` | `grabber.stream_setup(sink, ACQUISITION_START)` |
| `stopAcquisition` | `grabber.stream_stop()` |
| `stopAcquisitionForROIChange` | stop stream; set ROI props; restart |
| `getChunk` | drain the deque → `(N, H, W)`; **`np.empty((0, H, W))` when empty** |
| `getLatestFrame` | newest frame in the deque |
| `flushBuffers` | clear the deque (bounded loop, see below) |
| `setParameter` (`exposure`/`gain`/`brightness`) | `property_map.set_value(...)` |
| `getParameter` | `property_map.get_value(...)` |
| `crop` / ROI | `Width` / `Height` / `OffsetX` / `OffsetY` |
| `finalize` / `close` | `stream_stop()` → `TriggerMode=Off` → `device_close()` |

Two hard rules, both learned from the ThorCam TSI audit:

1. **`getChunk` must never fabricate frames and must never delegate to
   `getLatestFrame`.** Return an empty `(0, H, W)` chunk when nothing is queued.
   `ThorCamTSIManager.getLatestFrame` returns *zeros* when no frame arrives
   ([ThorCamTSIManager.py:172](../../../imswitch/imcontrol/model/managers/detectors/ThorCamTSIManager.py:172));
   that is tolerable for live-view display and catastrophic in a recording.
2. **`getChunk` must stay 3-D.** `readChunk` does `list.extend(...)`, which
   iterates axis 0 — a 2-D return silently decomposes one image into `H` row
   vectors. Full mechanism in
   [advanced_scan_triggered_recording_audit.md](../../advanced_scan_triggered_recording_audit.md).

With a real queue behind it, `getChunk` returns **every** triggered frame, and
`readChunk`'s existing distribution feeds BeadRec, recording and live view without
contention.

### Phase 3 — Software trigger control — **done (manager side)**
- `setTriggerEnabled(bool)` arms/disarms in software, plus a `Trigger Mode`
  `DetectorListParameter` in a `Trigger` group, following the
  `ThorCamTSIManager` UI precedent. Removes the manual IC-dialog step.
- **Still open:** who calls it. Simplest is the TriggerScope/BeadRec scan-start
  hook arming the current detector, mirroring how the scan already gates
  acquisition. Deferred until the rig confirms the trigger works at all.

### Phase 4 — Config, cleanup, docs
- **Setup files.** [`example_sted.json`](../../../imswitch/_data/user_defaults/imcontrol_setups/example_sted.json)
  declares two `TISManager` detectors — both need migrating. **Decided: serial,
  not index** — an index is positional and silently rebinds when USB enumeration
  order changes. The plugin therefore takes `cameraSerial` (matching
  `ThorCamTSIManager`), and `cameraListIndex` is simply not carried over; setups
  migrate by editing `managerName` anyway, so there is no back-compat window to
  honour.
- **Add the `TISManager` alias** to the plugin manifest at this point, and only
  at this point — see §5a.
- **[docs/TISCamera.rst](../../TISCamera.rst) is entirely IC3-specific** — it
  instructs the user to install the IC Imaging Control C Library 3.4.0.51 and put
  `tisgrabber_x64.dll` on `PATH`. All of that becomes wrong. Rewrite for the IC4
  GenTL producer, and keep the IC3 instructions in a clearly-marked legacy section
  until `pyicic` is removed.
- **[ACKNOWLEDGMENTS.md:32](../../../ACKNOWLEDGMENTS.md)** currently records
  `pyicic` as "Vendor SDK — see upstream terms". That is inaccurate today:
  [pyicic/README.txt](../../../imswitch/imcontrol/model/interfaces/pyicic/README.txt)
  states it is MIT-licensed third-party code from `morefigs/py-ic-imaging-control`.
  Fix that row, and add IC4 as a pip dependency (not vendored code) under the
  vendor's own terms.
- Once validated on the rig, remove `pyicic` and `tiscamera.py`.

### Testing
- Unit, mock-backed, no hardware:
  - one frame per simulated trigger; N triggers → N **distinct** frames
  - `getChunk` returns `(0, H, W)` — not zeros, not `None` — when no trigger fired
  - `getChunk` returns 3-D under `readChunk`'s `list.extend`; mirror
    [test_thorcam_tsi_recording_contract.py](../../../imswitch/imcontrol/_test/unit/test_thorcam_tsi_recording_contract.py),
    which encodes exactly this contract for the sibling camera
  - the bounded deque drops oldest and warns once, rather than growing without limit
  - keep [test_tis_manager_setparameter.py](../../../imswitch/imcontrol/_test/unit/test_tis_manager_setparameter.py)
    green — its `StrictFakeCamera` exists because the permissive mock hid a
    real-hardware bug, and the IC4 rewrite must not reintroduce that blind spot
- Integration (on rig): live view, single snap, continuous record, and a
  TriggerScope BeadRec scan — assert **distinct** frames and the correct frame
  **count** per scan.

### Exit criteria & rollback
- **Ship when:** a TriggerScope BeadRec scan of N positions yields N distinct
  frames, recording and BeadRec run concurrently without either starving, and the
  mock suite is green.
- **Rollback:** `pyicic` and `tiscamera.py` stay in-tree through Phases 1–3, and
  `_getTISObj` already falls back to the mock on any construction failure
  ([TISManager.py:158](../../../imswitch/imcontrol/model/managers/detectors/TISManager.py:158)).
  Reverting is a one-line import change until Phase 4 deletes the old path — so
  **do not delete anything until the rig has signed off.**

---

## 6. Fallback if IC4 does not enumerate the camera

The original plan treated IC4 as the only route, on the grounds that "no
poll-based tweak fixes this". That conclusion holds for *reading the same buffer
faster*, but it skips an option that is already sitting in the tree.

`pyicic` **does** implement a frame-ready callback —
[`register_frame_ready_callback()`](../../../imswitch/imcontrol/model/interfaces/pyicic/IC_Camera.py:493)
registers a `C_FRAME_READY_CALLBACK` with the DLL. **ImSwitch never calls it.**
`tiscamera.py` registers no callback at all, which is why `wait_til_frame_ready()`
could never have worked even if `time.clock()` still existed.

So the same queue from §1 is reachable on IC3: register the callback, copy each
frame into the same bounded deque, and drain it from `getChunk`. That is strictly
more code than IC4 (the callback is a raw ctypes trampoline running on a DLL
thread, with all the lifetime hazards that implies) and it keeps the dead-code
liability — it is **not** the recommendation.

But it is the answer to "what if prerequisite 2 fails on the rig", and it means a
Phase-0 failure does not block the fix. Worth knowing before someone spends a day
fighting GenTL producer installation.

---

## 7. Latent bugs found in the current TIS code

Found while verifying this plan. None is the cause of the trigger bug, and none is
currently reachable in production — but all three are in code the migration
rewrites, so fix or delete them rather than porting them forward.

1. **`TISManager.getParameter` cannot work against real hardware.** It guards on
   `self._camera.properties`
   ([TISManager.py:103](../../../imswitch/imcontrol/model/managers/detectors/TISManager.py:103)),
   but `CameraTIS` has no `properties` attribute — only `MockCameraTIS` does
   ([tiscamera_mock.py:9](../../../imswitch/imcontrol/model/interfaces/tiscamera_mock.py:9)).
   Against a real camera this raises `AttributeError`. It survives only because
   nothing calls it. This is the *same* mock-permissiveness trap that
   `test_tis_manager_setparameter.py` was written to catch.
2. **`getPropertyValue('exposure')` reads a non-existent attribute.**
   [tiscamera.py:105](../../../imswitch/imcontrol/model/interfaces/tiscamera.py:105)
   uses `self.cam.exposure.values` (plural) where gain and brightness use
   `.value`. `IC_Property` exposes `value` and `range` — there is no `values`.
   Unconditional `AttributeError`.
3. **Misleading comment.** `self.cam.enable_trigger(False)  # camera will wait for
   trigger` ([tiscamera.py:32](../../../imswitch/imcontrol/model/interfaces/tiscamera.py:32))
   — the comment states the opposite of what the code does. Given that trigger
   arming is currently manual, this comment is actively misleading to the next
   reader.

---

## 8. Risks & notes

- **Driver stack change** (GenTL producer) is the biggest unknown — confirm
  enumeration on the rig in Phase 0. §6 is the contingency.
- **IC4 buffers are recycled by the SDK.** The listener **must** `numpy_copy()`;
  `numpy_wrap()` returns a view that must not outlive the callback. Getting this
  wrong reproduces a *corruption* bug that looks superficially like the *duplicate*
  bug we are fixing — same symptom class, so it would be easy to misdiagnose.
- **The listener runs on the SDK's stream thread.** The deque needs a lock, and it
  must be bounded (drop oldest + warn once) so a stalled consumer cannot grow it
  without limit. `readChunk` already caps its per-consumer queues at
  `MAX_QUEUED_CONSUMER_FRAMES` — mirror that behaviour rather than inventing a
  second policy.
- **Listener lifetime.** `stream_stop()` must precede the listener becoming
  collectable; the vendor example calls this out explicitly. Our `close()` /
  `finalize()` path must enforce that order and be idempotent, as the current
  `CameraTIS.close()` already is
  ([tiscamera.py:121](../../../imswitch/imcontrol/model/interfaces/tiscamera.py:121)).
- **Pixel format:** confirm mono8 vs mono16 to preserve the current `depth == 2 →
  uint16` path ([tiscamera.py:54](../../../imswitch/imcontrol/model/interfaces/tiscamera.py:54)).
  IC4 reports this as a `PixelFormat` property, so the reinterpret hack goes away —
  but the dtype the manager advertises must still match what it delivers.
- **Keep `pyicic` until IC4 is proven on the rig.** Phase 4 is the point of no
  return; everything before it is revertible.

---

## 9. Sources

**Vendor**
- [IC4 Python library docs](https://www.theimagingsource.com/en-us/documentation/ic4python/index.html)
- [Getting Started (Python)](https://www.theimagingsource.com/en-us/documentation/ic4python/guide-getting-started.html) — confirms `Library.init()` is mandatory
- [API reference](https://www.theimagingsource.com/en-us/documentation/ic4python/api-reference.html) — `stream_setup` signature, `StreamSetupOption` values
- [`ic4-examples`: save-bmp-on-trigger.py](https://github.com/TheImagingSource/ic4-examples/blob/master/python/image-acquisition/save-bmp-on-trigger/save-bmp-on-trigger.py) — the verified reference for §3
- [`ic4-examples`: imagebuffer-numpy-opencv-live.py](https://github.com/TheImagingSource/ic4-examples/blob/master/python/thirdparty-integration/imagebuffer-numpy-opencv-live/imagebuffer-numpy-opencv-live.py) — `numpy_wrap` vs `numpy_copy` ownership rules
- [imagingcontrol4 on PyPI](https://pypi.org/project/imagingcontrol4/) — 1.5.3.3316, Python ≥3.8
- [IC4 SDK download](https://www.theimagingsource.com/en-us/support/download/icimagingcontrol4win-1.5.3.3316/)
- [DMK 33UX250 product page](https://www.theimagingsource.com/products/industrial-cameras/usb-3.0-monochrome/dmk33ux250/)
- [morefigs/py-ic-imaging-control](https://github.com/morefigs/py-ic-imaging-control) — upstream of the vendored `pyicic` (MIT)

**In-repo**
- [advanced_scan_triggered_recording_audit.md](../../advanced_scan_triggered_recording_audit.md) — the same problem class on ThorCam TSI, with the `getChunk` shape mechanism traced end to end
- [ThorCamTSIManager.py](../../../imswitch/imcontrol/model/managers/detectors/ThorCamTSIManager.py) — the drain-loop `getChunk` and trigger-mode parameter to mirror
- [test_thorcam_tsi_recording_contract.py](../../../imswitch/imcontrol/_test/unit/test_thorcam_tsi_recording_contract.py) — the contract tests to mirror
- [DetectorManager.readChunk](../../../imswitch/imcontrol/model/managers/detectors/DetectorManager.py:323) — multi-consumer distribution this plan depends on
