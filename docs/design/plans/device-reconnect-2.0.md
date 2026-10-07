# Device reconnect 2.0 — one model for every manager

Status: plan r1 (2026-10-07), for review. Builds on `feat/calibration-tools`
(which carries `feat/device-reconnection`: the lifecycle service, reservation
tickets on transitions, connect/disconnect, the Hardware status window).

## 1. Goal

Today 12 of 66 managers can be reconnected at runtime, each through its own
80–900-line lifecycle class. The goal is that **a manager is reconnectable by
default** — because its base class holds its backend — and that the few
device-specific parts (how to open it, what its safe state is, what to
re-initialise) are three small hooks. On the way, fix the defects the current
model has.

Two principles, decided after the first round (they are what we know now
that the first implementation did not):

- **P-1 The physical device is the thing you unplug, and the code derives it.**
  Managers that share a transport object share a physical device. The device
  graph is built from backend ownership, not from per-manager descriptor
  declarations; a shared transport is the normal case, not a veto.
- **P-2 A device's mode comes from the setup file and never changes at
  runtime.** A failure changes the connection state (`ERROR`, `ABSENT`), never
  the mode. No silent fallback to a simulation.

**Mocks stay.** They are how ImSwitch is tested and taught
(`example_no_hardware.json`, `MOCK_` serials, `MockPositionerManager`, the
mock instruments); nothing here touches a *configured* mock, and
`DeviceRuntimeMode.MOCK` remains a first-class, visible mode. What goes is
the *fallback* mock: a real device that failed at startup being replaced by a
simulation without the setup asking for it. That behaviour is kept as an
explicit opt-in (`useMockOnFailure: true`, per device) for anyone who relies
on it; the default becomes *absent with the error shown, reconnect when it is
back*. §4.0 has the rules.

Non-goals (for now): automatic reconnect on fault (lasers must never re-arm
by themselves); NI-DAQ (its "simulation" is a setup flag, not a plug);
a different GUI than the Hardware status window.

## 2. Inventory (as of 3f5eb7c64)

### 2.1 Already reconnectable (12)

Hamamatsu, TIS, Thorlabs MFF, PI, Märzhäuser XY, Elliptec (shared bus),
CoolLED (channels share one lifecycle), Cobolt 06-01 legacy and new, Leica DMI
stand + objective Z (one lifecycle for both), instruments (PM100, PAX, mocks:
connect / disconnect / reconnect).

### 2.2 Trivial A — RS232-backed through `RS232Manager` (~14)

MPB, AA AOTF, ESP32 LED / LED matrix / light sheet, GRBL laser, Oxxius
combiner, ESP32 / GRBL / SQUID stages, Jena piezo Z, Piezoconcept Z (×2),
TriggerScope.

`RS232Manager.reconnectTransport()` already replaces the serial backend **in
place**; dependents keep their reference to the manager object, so after a
port reconnect their commands reach the device again. What stops that
working today:

- a **startup latch**: `self._isMock = True` set once when the first handshake
  failed (MPB `MPBLaserManager.py:119`, AA AOTF `:249`, ESP32 LED `:29`) —
  commands are routed nowhere for the life of the process, even after the
  port is back. CoolLED solved it (`_isMock` is a property derived from the
  transport's runtime mode, `CoolLEDLaserManager.py:260`);
- **device-side re-initialisation** after the port came back: MPB's APC-mode
  check, limits and ramp-down recovery; AA's profile init; ESP32 identity.
  Today these run only in `__init__`;
- the **shared-transport veto** (§3.1): the lifecycle service refuses to
  reconnect any device whose port another device also uses.

The `rs232devices` section has six manager kinds: `RS232Manager` (has
`reconnectTransport` / `disconnectTransport`), and the vendor managers
`ESP32Manager`, `GRBLManager`, `SQUIDManager`, `KDC101Manager`,
`ElliptecManager` (no transport reconnect; Elliptec has its own shared-bus
reconnect instead).

### 2.3 Trivial B — own driver with a mock fallback (~10)

Kinesis stage (`_getStageObj`), Kinesis rotator (`_getMotorObj`), Standa
(`_getMotorObj`), ThorCam TSI (`_initCamera`), Photometrics (`_getCameraObj`),
AV, PiCam, Hamamatsu SLM usb / dvi, Teensy pulse (`_open_driver`).

All the same shape: a factory that tries the real driver, and on failure
installs a mock and records `_setConnectionError(..., mock_active=True)`
(Teensy / Standa / Kinesis rotator also latch `_mock_fallback = True`).
Reconnect = close the current backend, call the same factory again,
re-apply the device's settings. TIS shows what a camera needs: stop
acquisition under `DetectorsManager.detectorLifecycleMaintenance`, replace,
replay exposure / gain / ROI, `clearFaultAfterHardwareReplacement`, leave
acquisition stopped.

### 2.4 Not trivial (~8)

APD, PMT, NI-DAQ lasers and positioners (the DAQ), PulseStreamer,
PyMicroscope, Swabian time tagger (own SDK sessions, `_isMock` latch at
`:460`), NidaqManager itself.

## 3. Defects in the current model

1. **Shared-transport veto.** `DeviceLifecycleService._findReconnectBlockReasons`
   disables reconnect for every device whose transport another device uses —
   exactly the rig case (one serial port, several logical devices). Leica and
   CoolLED each wrote their own workaround (reconnect the port once inside
   their lifecycle, then re-initialise all participants).
2. **Fallback mocks and their latches** (§2.2, §2.3): a device that failed
   at startup is replaced by a simulation and flagged `_isMock = True`; it can
   never come back, even when its port can, and every caller that must not
   act on a simulation has to check (33 such checks in the model today; the
   measurement adapters re-check on every command because a bus can fall
   back mid-run). With P-2 the mode is fixed by the setup, so the flag and
   the checks go: a real device that is not there is `ABSENT`.
3. **Ten copies of one skeleton.** Every lifecycle re-implements: lock →
   best-effort safe state → replace backend → verify → safe state again,
   verified → fan out status to participants → build `DeviceLifecycleResult`
   with affected / deactivated ids. Each copy is a chance to get the safe-state
   rule wrong. The rule CoolLED and Leica got right: *a safe state is verified
   after the reconnect; never report "off" for a channel nobody switched off*.
4. **One lifecycle object per physical device** is required by the service
   (several managers of one `hardware_id` must return the *same* object, else
   "multiple lifecycle owners" disables actions). CoolLED and Leica each keep
   their own `WeakKeyDictionary` cache; nothing provides it.
5. **Positioner panel does not refresh after a reconnect.** Laser, Rotator,
   Settings, FocusLock, FlipMirror and LeicaStand controllers subscribe to
   lifecycle results; `PositionerController` does not, so after a Märzhäuser
   or PI reconnect the displayed position is stale until the next move.
6. **No cheap health check.** Faults are discovered when a command fails
   (`RS232Manager._callBackend` records them; instruments fault on read).
   Only CoolLED and the legacy Cobolt offer `probe`. The Hardware status
   window has no "Check" for anything else.
7. **`ABSENT` exists only for instruments.** `transient` /
   `connectOnStartup` live on `InstrumentInfo`; a USB stage or camera that is
   plugged in only sometimes still fails at startup and falls back to a mock,
   and the Laser / Positioner / Settings panels have no way to show a device
   that is declared but not connected.

## 4. The model

### 4.0 Mode and state: the rules

| Setup says | Startup | Hardware fails later | Hardware comes back |
|---|---|---|---|
| real device | `REAL` + `CONNECTED`, or `REAL` + `ABSENT` with the error (no mock installed; the panel shows "Not connected: <error>") | `REAL` + `ERROR`, commands refused with the error | Reconnect (or Connect for a transient one) → `CONNECTED` |
| real device, `useMockOnFailure: true` | as today: `MOCK` + the error, a mock installed (explicit opt-in) | — (a mock does not fail) | Reconnect → `REAL` + `CONNECTED` (the one case where the mode changes, because the setup asked for it) |
| mock (`Mock*Manager`, `MOCK_` serial, `simulation: true`) | `MOCK`, as today | — | — |
| transient real device | `REAL` + `ABSENT`, no attempt (unless `connectOnStartup`) | `REAL` + `ERROR` | Connect / Reconnect |

Consequences: `isSimulated` / `_isMock` are read from the holder (which
opener produced the backend), never stored; a command on an `ABSENT` /
`ERROR` real device raises a clear error instead of succeeding on a fake —
so the per-command simulation checks in the measurement adapters become
plain "is it connected" checks, enforced in one place (the holder).

### 4.1 Backend holder (in `DeviceManagerStatusMixin`, so every manager has it)

```python
self._backend = self._installBackend(
    open_real=lambda: StandaMotor(...),      # raises when the hardware is absent
    make_mock=lambda: MockStandaMotor(...),  # or None: no fallback, startup error
    label='Standa motor 0',
)
```

- `make_mock` is used **only** when the setup configures a mock (a
  `Mock*Manager`, a `MOCK_` serial, `useMockOnFailure: true`); otherwise a
  failed `open_real` leaves the device `ABSENT` with the error, no backend
  installed, and every command raises `DeviceNotConnectedError` (one check,
  in the holder). The mock drivers themselves are untouched.
- `self.backendIsReal` / `isSimulated` are **derived** from which opener
  produced the current backend. No latches.
- `self._replaceBackend()` closes the current backend (errors suppressed, as
  `RS232Manager._closeBackend` does for runtime replacement) and runs
  `open_real` again. That is the default reconnect — the same for a device
  that was `ABSENT` since startup and one that faulted later.
- Transient devices: no attempt at startup (`ABSENT`), `open_real` kept for
  `connect()`.
- Holders are the **physical devices** (P-1): a manager says which holder it
  uses (its own, or the transport it was handed), and the device graph is
  derived from that — `hardware_id` is the holder, the `USES_TRANSPORT`
  relation is "uses a holder it does not own", components are "managers on
  one holder". The 14 `getDeviceDescriptorSpec` declarations and the
  `sharedRs232ComponentSpec` / `rs232BackedPrimarySpec` helpers are deleted
  as managers adopt the holder; until then the supervisor accepts both.

Managers that already follow the factory pattern (§2.3) adopt it by moving
their `try/except` into the two lambdas.

### 4.2 Reconnect template (in `devices/lifecycle.py`)

One `DeviceLifecycleTemplate` with the skeleton from §3.3 and three hooks a
manager implements (or inherits):

```python
def _lifecycleSafeState(self, *, verified: bool) -> list[str]:
    """Put the device in its safe state. Before the reconnect: best effort,
    errors ignored. After: errors returned; any error = reconnect FAILED."""
def _lifecycleReplaceBackend(self) -> bool:        # default: self._replaceBackend()
def _lifecycleReinitialise(self) -> None:          # default: nothing
```

Defaults by base class:

- `LaserManager`: safe state = emission off through the checked
  `applyEnabled(False)` (raises on failure, so "off" is never assumed);
  `deactivated_device_ids` = the laser, so the Laser panel shows OFF.
- `PositionerManager` / `RotatorManager`: safe state = nothing moves;
  reinitialise = a fresh position read (what MHXY does).
- `DetectorManager`: replace under `detectorLifecycleMaintenance`, then
  `clearFaultAfterHardwareReplacement`; acquisition stays stopped;
  reinitialise = replay the parameters the manager keeps (TIS's replay,
  generalised: every `DetectorParameter` with a value is re-applied).
- Stands / shutters: safe state = shutters closed, verified (Leica).

The template also provides: the per-physical-device **lifecycle cache**
(defect 4), the `affected_device_ids` from the device graph, the
`_replacingThread` guard (Hamamatsu), and uniform result summaries
(`"<device> reconnected; <safe state>"`, `"<device> reconnect failed; mock
fallback active"`, `"<device> reconnected but <safe state> failed"`).

The ten existing lifecycles are ported onto it one by one, each keeping its
specific hooks; their tests stay as they are (they test behaviour, not the
skeleton).

### 4.3 Transport-level reconnect (in `DeviceLifecycleService`)

The `rs232devices` managers get one contract — `reconnectTransport()`,
`disconnectTransport()`, `probeTransport()` — implemented once on the backend
holder (`RS232Manager` already has the first two; the vendor managers get
them the same way).

`service.reconnect(X)`: if X `USES_TRANSPORT` T and T has that contract,

1. own every device that depends on T (the tickets `_ownDevices` already
   takes — it walks the graph relations whose target is T);
2. best-effort safe state on every dependent (through their hooks);
3. `T.reconnectTransport()` **once**;
4. for every dependent, in graph order: `_lifecycleReinitialise()`, then
   safe state verified;
5. one `DeviceLifecycleResult` for X, with `affected_device_ids` = all
   dependents, and a per-device detail line; every dependent's status set.

This replaces the veto (defect 1): reconnecting one device on a shared port
*is* reconnecting the port, and the service makes that explicit and safe. The
test `test_lifecycle_service_disables_reconnect_for_cross_device_shared_transport`
becomes "reconnecting one device on a shared transport re-initialises both,
and both are affected". CoolLED and Leica keep their lifecycles but drop the
transport-reconnect part (the service does it); their caches go (template).

A manager in §2.2 then needs **no lifecycle class**: its descriptor already
says which transport it uses; the service finds the transport; the manager
contributes at most `_lifecycleReinitialise` (MPB: the APC handshake and
limits; AA: profile init) and the base's safe state.

### 4.4 Probe for everyone

`probe` = the template's verify step, exposed: a cheap identity / status
query through the backend (`*IDN?`-style; `GETSN` for MPB; `get_pos` for a
stage; `device_counter` for an instrument) that updates the status without
replacing anything. The Hardware status window gets a **Check** button beside
Reconnect for every device whose lifecycle has it. An optional periodic check
(setup option, off by default) can keep the window current; it never
reconnects by itself.

### 4.5 `ABSENT` for any device (P-2 in the GUI)

`transient` and `connectOnStartup` move from `InstrumentInfo` to `DeviceInfo`,
and `ABSENT` becomes a state any device can be in — at startup when the
hardware was not found, or by declaration. The panels must show it: the
Laser, Positioner, Rotator and FlipMirror rows of an absent device are
greyed with "Not connected" and a Connect action; an absent detector is
listed but not selectable for live view or recording; scans and recordings
that need an absent device are refused with its name. The Hardware status
window and the Instruments panel already handle `ABSENT`. This is not
optional: without it, P-2 would turn a broken device into a confusing panel
instead of a fake one.

## 5. Phases

| Phase | What | Size |
|---|---|---|
| **R-1** Transport fan-out | §4.3 in the service; `_isMock` derived in MPB / AA / ESP32 LED; reinitialise hooks for MPB and AA; veto removed; Positioner panel listener (defect 5). Brings reconnect to the ~14 RS232-backed managers. | medium |
| **R-2** `ABSENT` everywhere | §4.0 + §4.5: the holder's "no backend" state, `DeviceNotConnectedError`, `useMockOnFailure` default off (opt-in kept), `transient` on `DeviceInfo`, panels grey out absent devices, scans / recordings refuse them by name. Shipped mock setups unchanged (they configure their mocks). | medium–large |
| **R-3** Backend holder + derived graph | §4.1 in the §2.3 managers and the vendor `rs232devices` managers; descriptor declarations deleted as each adopts it; Kinesis stage / rotator, Standa, ThorCam TSI, Photometrics, AV, PiCam, SLMs, Teensy become reconnectable through the template defaults (cameras: parameter replay). | medium |
| **R-4** Template + port | §4.2; port CoolLED, Leica, Cobolt ×2, MHXY, PI, MFF, Elliptec, Hamamatsu, TIS, instruments. No behaviour change intended; their tests are the gate. | medium–large |
| **R-5** Probe + Check | §4.4; Check button; optional periodic check. | small |

Rig gates: R-1 on the Monalisa2 rig (MPB and AA on real ports: unplug the USB-serial adapter, reconnect, verify emission off and limits read); R-2 by starting a rig setup with one device unplugged (its panel greyed, the rest usable, Connect works once plugged in); R-3 with a Kinesis stage and a camera (ThorCam or Photometrics) unplugged mid-live-view.

Why this order: R-1 is the largest rig gain for the least change; R-2 is the
principle with user-visible consequences and should be seen on a rig early;
R-3 / R-4 are the structural clean-up that the first two make possible, and
porting the ten lifecycles (R-4) is cheaper once the holder exists.

## 6. Risks

- **Two different physical devices on one port** (a daisy chain, an Oxxius
  combiner): the transport reconnect re-initialises both; that is correct
  (the cable is one), but the result must name both so the user is not
  surprised. Covered by `affected_device_ids`.
- **Re-initialisation that changes device state.** MPB's startup logic
  darkens the laser and switches it to APC mode; a reconnect does the same.
  That is the intended safe state, but it is a visible change: the result
  says so (`deactivated_device_ids`).
- **Detectors during live view.** The generic camera path must do what TIS
  does (maintenance window, acquisition left stopped). The template encodes
  it; Hamamatsu's two-camera case stays its own hook.
- **P-2 changes startup behaviour on rigs.** A setup with a broken device
  used to start with that device mocked and every panel alive; it will start
  with that device absent and its panel greyed. Anyone who wants the old
  behaviour writes `useMockOnFailure: true`. The release note must say so;
  the config editor should offer the key on every device.
- **Porting ten lifecycles** is the largest risk of regression. Gate: every
  existing lifecycle test unchanged and green, plus a per-device rig check
  where hardware exists (PI, Märzhäuser, Hamamatsu already on the list).

## 7. Open questions

- Q-1 Should `probe` run periodically by default (every 30 s, idle only)?
  Proposal: off by default; a setup option.
- Q-2 (settled 2026-10-07) Mocks stay as configured mocks; the fallback mock
  becomes an explicit opt-in; `ABSENT` for any device is phase R-2, not
  optional.
- Q-3 For R-1, should the vendor `rs232devices` managers (ESP32, GRBL, SQUID,
  KDC101) get the transport contract in the same phase, or only
  `RS232Manager` first (MPB, AA, Oxxius, Piezoconcept, Jena, TriggerScope)?
  Proposal: `RS232Manager` first; the vendor ones in R-3 with the backend
  holder, since that is what they need anyway.
- Q-4 Where does P-2 draw the line for *partial* hardware — an Elliptec bus
  where one of two addresses answers, a Hamamatsu with one of two cameras?
  Proposal: per address / per camera, as the Elliptec bus already does
  (`is_real(address)`): the missing one is `ABSENT`, the rest `CONNECTED`.

## 8. Implementation notes

**R-1 (done 2026-10-08).**
- `DeviceLifecycleService`: the shared-transport veto is gone. At
  construction the service maps every device to the transport it uses when
  that transport's manager has `reconnectTransport` (`transportOf`,
  `devicesOnTransport`); a device without an adapter of its own on such a
  transport gets a `_TransportBackedLifecycle` (capability reconnect). A
  reconnect of any device on a reconnectable transport runs
  `_reconnectThroughTransport`: safe state (best effort) on every device on
  the port → `reconnectTransport()` once → per device, in order, its
  lifecycle's `onTransportReconnected(real)` (once per physical device) or
  each manager's `_onTransportReconnected(real)` or the default (status
  follows the transport); any error fails the reconnect with the device
  named. `affected_device_ids` = every manager on the port; lasers among
  them are `deactivated_device_ids`. Ownership tickets already covered all
  of them.
- CoolLED and Leica lifecycles: `transportSafeState` / `onTransportReconnected`
  split out of `reconnect()`, which now calls them (direct use unchanged).
- MPB and AA AOTF: `_isMock` is a property (port real *and* the startup
  exchange succeeded on it); the startup exchange is `_initialiseHardware()` /
  `_startupExchange()`, run again by `_onTransportReconnected`;
  `_lifecycleSafeState` = immediate OFF before, verified OFF after.
- Positioner panel subscribes to lifecycle results and refreshes the
  affected stages on its thread.
- Not in R-1: the vendor `rs232devices` managers (ESP32, GRBL, SQUID,
  KDC101) have no `reconnectTransport` yet, so ESP32 LED / stages stay as
  they were (Q-3: with the holder in R-3). Piezoconcept, Jena, Oxxius,
  SQUID stage and TriggerScope get the default hook (they keep no device
  state; `RS232Manager` heals their status on the next I/O).

