# Transient instruments and measurement runs

Status: **plan r4 — accepted; P-1, P-3 done, P-5 done on mocks** (2026-10-07). Implementation notes: §17. Branch
`feat/calibration-tools`, worktree `../Imswitch2-calibration-tools`. Nothing
implemented. Dispositions: §14 (rounds 3 and 2), §15 (round 1).

r3 changes the central abstraction. r2 forced a structured measurement through
virtual detectors, frame storage and a grid-first scan engine, and then had to
redesign each of them. r3 makes **`Instrument → Sample → MeasurementRun`**
authoritative; grids, frames and detector views are derived or optional.

**Supersedes** `calibration-tools.md` (kept for instrument details and review
history; everything still applicable is restated here).

Related: `origin/feat/device-reconnection` (unmerged, 17 commits,
72deef340 … 018b4151d). Verifying and stabilising it is part of this plan
(P-0); it runs alongside and does not block proving the measurement model.

## 1. Goal

Two workflows, one mechanism:

1. **Waveplate → polarisation map.** Two motorised waveplates are set to a
   sequence of angle pairs (a grid first; later refinements and fitted
   predictions); at each pair a Thorlabs PAX1000 is sampled. The run is one
   file that ImProcess opens; a reconstructor shows the covered states on the
   Poincaré sphere and reports the angle pairs that reach right/left circular
   and linear polarisation every 10°, or "failed". Its export drives later
   ImScripting scripts.
2. **Laser raw drive → power LUT.** A Thorlabs PM100 in the back focal plane;
   a script sets a sequence of raw drive values and samples the meter. The
   deployable `calibCsvPath` LUT is an **export product** of that run.

Both: *set controls → settle → open acquisition window → sample → commit
point → analyse / export.* Both need **transient instruments**: declared in the
setup, absent at startup without error or mock, connected and disconnected at
runtime, absent from recordings while not connected.

"Ordinary ImSwitch data" means **discoverable and processable** — a run file
ImProcess opens and reconstructors consume — not necessarily frames.

## 2. Model

```
Instrument (setup device, transient)            controls: axes (rotators, positioner axes), laser raw drive
   │ read() under its lock                               │
   ▼                                                     ▼
Sample  = all quantities of ONE read          MeasurementPoint = setpoints for every control
          + generation, sequence, times                │
                                                        ▼
MeasurementRun = ordered sequence of points (from a generator: grid, list, 1-D sweep)
   per point: set controls → settle → window → samples per instrument → commit_point()
   │
   ▼
Run storage (one writer, one file)  ──▶  derived: grid images (if the generator was a grid),
                                          live display, polarisation table, LUT export
```

- **Instrument** — one setup entry (`instruments` section), one VISA session,
  settings, actions, a verified timing rule, a sample generation. No detector
  entries are required.
- **Sample** — the paired quantities of one read (`azimuth, ellipticity, dop,
  power` for the PAX; `power` for the PM100), with `generation`, `sequence`,
  `t_host`, `device_t?`, `device_id?`, and the acquisition window it belongs to.
  Quantities are never acquired separately.
- **MeasurementPoint** — the setpoints for each control (`{"hwp": 30.0,
  "qwp": 12.5}` or `{"775": 0.42 V}`) plus an optional grid index.
- **MeasurementRun** — run id, controls, instruments, an ordered list of
  points, per-point results and status. A **point generator** produces the
  sequence: `grid(axes, ranges, traversal)` (records shape and traversal map
  as metadata), `points([...])` (fitted predictions, refinements),
  `sweep(control, values)` (laser LUT).
- **Derived views** — grid images exist only when the generator was a grid;
  everything else (table, sphere, LUT) is computed from committed points.

## 3. What exists, and what does not

On `main`:

| Need | State on main |
|---|---|
| Rotator control | `RotatorManager` (`move_abs` / `move_rel` / cached `position`); Kinesis `move_abs` blocks in `wait_move()` without a deadline; `RotatorController` and the workflow facade (`model/workflows/facade.py:1289`) call `move_abs` directly — no ownership anywhere. |
| Software point execution | **None.** All point-detector scans are waveform-clocked. |
| Run exclusion | Scan coordinator `_scan_execution.py` (`armWithStarter`, run reservation); its detector composition (l. 345) and waveform lifecycle are waveform-specific. Recording takes a `RECORDING` lease **before** the scan adds `SCAN` (`RecordingManager.py:3750`). |
| Recording service pieces | Recordings-folder preference, session notes, metadata assembly, writer workers — all coupled to detector frame queues; writers set `element_size_um` (l. 708, 757, 1160); `buildOmeMeta` (l. 2980) assumes spatial steps. |
| ImProcess input | `DataObj` opens HDF5/Zarr/TIFF (`improcess/model/DataObj.py:636-651`) and selects one image dataset; `Reconstructor.process(data_obj)`. |
| napari non-image layers | `DisplayLayerSpec` (`improcess/model/result.py:52`): `points`, `shapes`. |
| Laser LUT | `calibCsvPath` (§10); the normal laser setter applies a loaded LUT (`NidaqLaserManager.setValue`, l. 42). |
| Detector actions | `DetectorAction(group, func)` — not needed by the core model any more. |

On `origin/feat/device-reconnection`: status mixin + `DeviceConnectionState`
/ `DeviceRuntimeMode` (REAL/MOCK, no "absent"), device graph (physical devices,
COMPONENT_OF / USES_TRANSPORT), `DeviceLifecycle` protocol (connect /
disconnect declared, unimplemented), `DeviceLifecycleService` (reconnect only,
default-deny, per-device lock, preliminary scan/recording check, plain
callbacks), `HardwareStatusWidget` (Reconnect / Refresh), `DeviceSupervisor`
(fixed sources, `__dict__` inference). Details and gaps: P-0.

## 4. Decisions

**D-1 — One device system.** Instruments are setup devices contributed by
plugins through the existing `device_managers` manifest list. Not a return to
a separate calibration-tool system.

**D-2 — `Instrument → Sample → MeasurementRun` is authoritative.** Detector
views of instrument quantities are **optional adapters**, added only where an
existing consumer needs them; whether any are worth it is decided after the
mock end-to-end run (P-1, Q-2). (Reverses r2's D-3.)

**D-3 — A run is an ordered sequence of points.** Grids are one generator;
shape and traversal are metadata.

**D-4 — Three shared interfaces, reused at narrow boundaries:**
1. **resource reservation** (§7) — who may command which physical resource;
2. **run lifecycle** — run id, start / stop / abort / complete, exclusion
   against conflicting runs, progress, shutdown participation;
3. **run storage** (§8) — filename and folder policy, metadata (session notes
   included), one writer, completion.
Software point execution is separate from the waveform scan path; the scan
coordinator is not used for detector composition.

**D-5 — Timing is verified per operating profile** (§6.3), and the
verification state travels with every sample into analysis and exports.
Unverified acquisition is exploratory only.

**D-6 — Acquisition stores raw paired samples; mathematics lives in analysis.**
The engine does not aggregate. Aggregation (Stokes, DOP, power statistics) is
a shared analysis library used by live display and reconstructors (§9.2).

**D-7 — Measured, predicted and unverified are never mixed** — in matching,
exports and replay.

## 5. Transient devices (on `feat/device-reconnection`)

**Setup.** `"transient": true` on an `instruments` entry (schema, config
editor, `setup_metadata.py`); `"connectOnStartup"` optional (Q-3).

**States.** `ABSENT` (new runtime mode: intentionally not connected, no
backend, no mock, neutral "not connected"); `FAULTED` (was connected, then
failed — read error, USB removed — shown with cause); connected `REAL` (or
`MOCK` for mock instruments).

**Lifecycle.** `DeviceLifecycleService.connect(id)` / `disconnect(id)`:
default-deny unless declared, per-device lock; parameters from the setup entry,
session override, "Save to setup" separate. A transition takes the
instrument's **reservation** (§7) atomically; if a run, script handle or live
poll holds it, the transition is refused with the holder named.

**Faults.** The instrument manager calls `reportFault(cause)` on read error or
transport loss. Under the device lock the service marks it `FAULTED`,
invalidates cached samples, increments the **generation** (older samples are
rejected everywhere), and cancels the owner of its reservation through the
run-lifecycle abort hook with the cause. Reconnect starts a new generation.

**Notification.** Qt bridge `sigDeviceStatusChanged(HardwareDeviceId,
DeviceStatus)` on the UI thread after every lifecycle change and fault.
Hardware status window: Connect / Disconnect beside Reconnect.

**Recordings.** Ordinary detector recordings never include instruments
(there are no instrument detectors in the core model). Instruments appear only
in measurement runs that used them.

**Shutdown.** Fail-closed: if the scripting drain failed **or** a quarantined
worker (§7.3) is still alive on a backend, that backend is not closed;
otherwise pollers are stopped and joined (bounded) and each instrument is
closed under its reservation with a bound.

## 6. Instruments

### 6.1 Setup

```json
"instruments": {
  "pax1": {"managerName": "ThorlabsPAX1000Manager", "transient": true,
           "managerProperties": {"serial": "M01012314", "visaBackend": "", "wavelengthNm": 633}},
  "pm1":  {"managerName": "ThorlabsPM100Manager", "transient": true,
           "managerProperties": {"serial": "P0011748", "wavelengthNm": 775}}
}
```

One entry per physical instrument. `instruments` is a manager group with its own
supervisor source; no detector entries.

### 6.2 Contract (`imswitch.pluginapi.instruments`)

- `identity` after connect: vendor, model, serial, firmware.
- `quantities`: name, unit, `quantity` id (`polarisation.azimuth`,
  `polarisation.ellipticity`, `polarisation.dop`, `optical.power`) and a
  **valid range** (PAX: azimuth ∈ [−π/2, π/2], ellipticity ∈ [−π/4, π/4],
  DOP ∈ [0, 1 + tolerance]; power finite, ≥ 0). A display or export may select
  quantities; acquisition always takes whole samples.
- `settings` (readback = applied value), `actions` with `confirm` text and
  `requires_dark` (e.g. PM100 `zero`).
- `timing_profiles` (§6.3).
- `read() -> Sample` — called only by the instrument manager, under its I/O
  lock. Two failure kinds, never confused:
  - **invalid sample** — the instrument answered, but the packet is
    malformed or a value is non-finite / out of its valid range. The sample is
    kept with `valid=False` and a reason, is never aggregated or matched, and
    does **not** fault the connection;
  - **transport error** — timeout, I/O error, device gone. Faults the
    connection (§5).
  A run of invalid samples beyond a driver-declared limit ends the window
  with cause `INVALID_SAMPLES` (it does not fault the device).

The manager owns the session, the I/O lock, generations, the ring buffer for
live display, and the window logic. Settings, actions and reads are commands
at the reservation boundary (§7).

### 6.3 Timing: acquisition windows

After a control change and settle, a caller opens a window and samples it:

```
open_window() -> Boundary
sample_window(boundary, n, deadline, cancel) -> WindowResult(
    samples,           # every valid sample accepted, in order — also when incomplete
    invalid,           # invalid samples with reasons (kept, never used)
    discarded,         # count of samples rejected as acquired before the boundary / repeats
    complete,          # True iff len(samples) == n
    cause,             # None | TIMEOUT | CANCELLED | TRANSPORT_FAULT | INVALID_SAMPLES
    profile_id,        # the verified timing profile in force (or None)
    verification,      # VERIFIED | UNVERIFIED
)
```

`sample_window` does not raise for timeout, cancellation, transport fault or
invalid samples — the partial samples it already has are in the result, so
the engine can commit them (§8.3). It raises only for programming errors.

**Timing profiles.** Verification belongs to an operating configuration, not
to a driver. A driver ships a table of **profiles**; each names the settings
and firmware it holds for (PAX: firmware range, `SENS:CALC 9`, waveplate
rotation speed range; PM100: firmware range, averaging count range, sensor
type) and its rule:
- `DEVICE_TIMESTAMP` — samples carry their acquisition start;
- `TRIGGERED` — each read starts a fresh measurement after the command, with a
  known integration time;
- `UPDATE_BOUND` — a measured bound `T` after the boundary.

The manager matches the current settings + firmware against the table at
connect and after **every** settings change. Outside every verified profile the
verification state is `UNVERIFIED`; a settings change into or out of a
profile takes effect for the **next** window only (a window in progress is
ended with cause `CANCELLED` if a setting changes under it — which the
reservation already prevents for foreign owners).

**Host ↔ device time.** Device timestamps and counters are not host time.
- *Counters:* `open_window()` queries the current counter **after** settle;
  accepted samples must start at a counter strictly greater than that value
  plus the driver's declared in-flight allowance.
- *Device clocks:* the manager estimates the offset at connect and at every
  `open_window()` by bracketing a device-time query between two host
  timestamps (offset ± half the round trip); a sample is accepted only if its
  device start, mapped to host time **minus** that uncertainty, is after the
  boundary.
- *No device timing:* `UPDATE_BOUND` from the profile.

**`UNVERIFIED` acquisition.** Live display always works. Quantitative
acquisition with an unverified profile is refused unless the run sets
`allowUnverifiedTiming` (characterisation). Every sample carries its
`verification` and `profile_id`; the state propagates (§9.3, §10): no ordinary
`pass`, no deployable LUT from unverified samples — only exploratory plots,
until a **qualification run** (§9.6, §10) with verified timing confirms them.

### 6.4 Live display

A **Readings** dock lists connected instruments: current values of the
selected quantities with units, rolling traces, settings and actions. Fed by
the instrument's sample path; while a run or script handle holds the
instrument, the display shows the holder's samples (no extra reads). Derived
quantities (e.g. s1–s3) come from the analysis library.

### 6.5 Mock instruments (built-in, no pyvisa)

- **Mock power meter** — power from a mock laser's **raw** drive × transmission
  + noise; configurable flat and non-monotonic regions.
- **Mock PAX** — Jones model of two waveplates (retardances, fast-axis offsets,
  input state) driven by the mock rotators' actual angles; revolution period,
  device counter on/off, sample latency (stale samples across a move boundary
  on purpose), injected read faults and "USB removal".

### 6.6 Real drivers

**PM100 (`ThorlabsPM100Manager`)** — serial as substring of
`USB0::0x1313::0x8078::<serial>::INSTR`; back-end `''` / `'@py'`; timeout 2 s;
no termination characters. `*IDN?`, `READ?` (W), `SENSE:CORR:WAV?` / `<nm>`,
`SENSE:ZERO:INIT`. Action `zero` (`requires_dark`). `utility_scripts/aa_aotf_calibration.py`
imports this driver instead of its own `PM100D`. Timing `UNVERIFIED` until the
rig shows `READ?` triggers afresh (→ `TRIGGERED`). Rig: zeroing completion,
averaging / auto-range.

**PAX1000 (`ThorlabsPAX1000Manager`)** — serial required; back-end; timeout
5 s. Connect `*IDN?` → `SENS:CALC 9` → `INP:ROT:STAT 1`; disconnect
`INP:ROT:STAT 0` first. `SENS:DATA:LAT?` mode 9: field 9 azimuth, 10
ellipticity (rad), 11 DOP, 12 power; length checked, malformed refused.
`wavelength_nm` → `SENS:WAV <m>`. Timing `UNVERIFIED` until fields 0–8 are
known (counter/timestamp → `DEVICE_TIMESTAMP`, else measured `UPDATE_BOUND`).
Rig: power unit, `SENS:WAV` unit, repeat behaviour.

## 7. Resource reservation

### 7.1 Resources and conflict rules

Two different problems, two mechanisms:
- **Transaction ownership** (this section) — who may *change the state* of a
  device during a measurement;
- **byte serialisation** — a shared transport's existing I/O lock, unchanged.

Rules:
1. The reservable unit is the **device that holds motion or output state**:
   a rotator's controller, a multi-axis stage **controller** (reserving one
   axis reserves the controller, so sibling axes are refused for others —
   axes on one controller share motion state), an instrument, a laser.
2. **Transports are not reserved.** Other devices on a shared RS232 port stay
   usable by others; the transport lock serialises their bytes. But a
   transport's **lifecycle** (reconnect, close) is refused while any device
   using it is reserved — that would change the reserved device's state.
3. **Waveform output** (NI-DAQ / TriggerScope scan tasks) is a resource of its
   own, see §7.5.

Resource keys come from the device graph (COMPONENT_OF, USES_TRANSPORT) once
P-0 lands; before that each supported adapter declares its keys explicitly.

### 7.2 Atomic command admission

A check followed by a command races with a reservation taken in between. So
admission, in-flight tracking and reservation share **one** synchronisation
boundary, a `ResourceRegistry` with one lock:

- **Admission**: every mutating command on a reservable resource (from a UI,
  a facade, a script, a run) calls `admit(resource, token | None)`. Under the
  registry lock: if the resource is reserved by another token → refused with
  the holder named; else an **in-flight ticket** is registered. The ticket is
  released when the command's worker physically returns (§7.3).
- **Reservation**: `reserve(resources, deadline)` under the same lock marks
  the resources **pending** — new unreserved admissions on them are refused
  from that instant (no starvation) — then waits, bounded, for every in-flight
  ticket on them to be released. All released → reservation granted; deadline
  passed → pending marks removed, reservation **refused**, naming the command
  still running. A reservation never succeeds while a conflicting command is
  admitted.
- **Tokens** are unique and **expire** when their reservation ends; a call with
  an expired token raises `ReservationExpiredError` — a saved handle never
  becomes usable again.

Test the race directly: a command paused immediately **after** admission, then
a reservation attempt — it must wait and then either succeed after the command
returns or be refused at its deadline; never overlap.

### 7.3 Enforcement boundary, owner tokens, worker completion

Admission sits in the **manager's mutating methods**, because controllers,
the workflow facade (`facade.py:1289`) and scripts all call managers directly:
a base-class template method around `RotatorManager.move_abs/move_rel`,
`PositionerManager.move/setPosition` (supported positioners),
`LaserManager.setValue/setEnabled`, the raw-drive control (§7.4), instrument
settings / actions / windows. Widgets grey out from registry state; they are
not the enforcement.

Ownership is an explicit argument. `reserve(...)` returns an **owner-bound
handle** whose methods pass the token through every dispatch hop, including
calls marshalled to the UI thread (a parameter, never a thread-local):

```python
with api.imcontrol.reserve(rotators=['hwp', 'qwp'], instruments=['pax1']) as r:
    res = r.rotator('hwp').apply(30.0, deadline_s=20)      # ControlResult, §7.4
    pax = r.instrument('pax1')
    w = pax.sample_window(pax.open_window(), n=5, deadline_s=10)   # WindowResult
```

**Worker completion.** Moves and reads run on workers with deadlines. A
deadline ends the *wait*, not the hardware operation. Regardless of `stop()`
support, an in-flight ticket — reserved or not — is released, and its backend
may be finalized or receive another command, only after the worker has
**physically returned**. Until then the backend (controller / instrument, not
just an axis name) is **quarantined**: status "busy — operation still
running", visible with the operation named; reservations on it are not
granted; lifecycle transitions refused; shutdown treats it like an undrained
script (fail-closed).

### 7.4 Supported controls: commands with observable outcomes

A returned setter does not prove the output changed — `NidaqLaserManager.setValue`
logs exceptions and the low-level `NIDAQManager.setAnalog` returns `False`
without raising unless `raise_on_error=True`. Run controls therefore use a
dedicated contract:

```
apply(setpoint, token, deadline) -> ControlResult(
    requested,       # what was asked
    acknowledged,    # what the device/driver confirmed it applied (None if it cannot say)
    measured,        # fresh readback after settle (None unless readback is FRESH)
    ok, cause,       # False + cause on any failure; never a silent success
)
```

Run controls never go through the UI setters; they call the low-level path
with error reporting (`setAnalog(..., raise_on_error=True)`; AA AOTF profile
operations' boolean results checked; rotator driver exceptions propagated).
A failed `apply` fails the point. Requested, acknowledged and measured values
are all stored (§8.3).

Initially supported — each audited, stating its real capabilities (deadline,
`stop` yes/no, readback `FRESH` / `CACHED` / `NONE`, acknowledgement yes/no,
settle): mock rotator, mock positioner, Kinesis rotator, Elliptec rotator,
NI-DAQ laser raw drive, AA AOTF raw drive. Nothing else is offered to runs.

### 7.5 Waveform scans and measurement runs

Manager-level admission does not cover waveform output: NI-DAQ scans create
hardware output tasks directly, and NI-DAQ's busy check protects only active
DAQ operations. So existing scan admission is bridged into the registry in
**both directions**, coarsely first:
- a resource `waveform-output` (global, P-3);
- scan arming (`_scan_execution` arm / `armWithStarter`, and every other path
  that starts output tasks) admits on `waveform-output` and holds it until the
  scan run ends;
- a measurement run reserves `waveform-output` for its whole lifetime.

Result: a waveform scan cannot start between two calibration points, and a
measurement run cannot start while a waveform scan holds outputs. Fine-grained
mapping (which DAQ channels a run actually depends on) replaces the global
resource later, once resource mapping is reliable.

## 8. Run lifecycle, execution and storage

### 8.1 Run lifecycle: three separate outcomes

A `MeasurementRun` has a run id, an owner token, controls, instruments, a
point sequence and progress. It reports three things separately, because a
terminal acquisition status does not mean the apparatus is restored or the
resources are free:

| | values |
|---|---|
| **acquisition outcome** | `RUNNING` → `COMPLETE` / `STOPPED` / `FAILED` / (on disk only) `INTERRUPTED` |
| **cleanup outcome** | `PENDING` → `RUNNING` → `DONE` / `FAILED` (with the step that failed) / `QUARANTINED` (a backend still busy) |
| **lifecycle** | `ACTIVE` → `CLEANING_UP` → `FINISHED` |

- Data is finalised as soon as acquisition ends; it never depends on cleanup.
- **Cleanup** is a list of steps: return controls to their start (option,
  default on, not after a movement failure), restore laser value then enabled
  state (laser procedure), close windows. Each step runs inside the
  reservation, with deadlines, and is **never dispatched to a backend whose
  previous command is still in flight** — that step waits for the worker
  (bounded) or is skipped and the backend reported `QUARANTINED`.
- A resource is released when its own cleanup steps are done, or it stays
  quarantined. `FINISHED` is published only when every resource is released
  or explicitly quarantined. A failed restoration is reported as such next to
  complete data.
- At shutdown cleanup does not move anything; it stops and releases.

Starting a run reserves all its controls, instruments and `waveform-output`
(§7.5) in one `reserve` call. Existing recordings are unaffected: a run takes
no detector leases. If cameras ever join runs (Q-7), participation is by run
identity: a lease of the same run is compatible, a lease of another owner is a
conflict — never decided by backend alone.

### 8.2 Execution

Per point in sequence order:
1. `apply` every control (§7.4); a failed result fails the point;
   cancellation checkpoint;
2. settle; with `FRESH` readback and a tolerance, poll until within tolerance
   (deadline); checkpoint;
3. per instrument `open_window()` → `sample_window(...)` (§6.3), keeping the
   `WindowResult` whatever its completeness; checkpoints between instruments;
4. read `FRESH` positions again;
5. `storage.commit_point(...)` with every `ControlResult` and `WindowResult`.

Point status: `COMMITTED` if every control applied and every window is
`complete`; `FAILED_PARTIAL` if some samples were taken; `FAILED` otherwise.
Stop is honoured at every checkpoint (the current point is committed with what
it has, status not `COMMITTED`). A deadline exceeded → `stop()` where
supported, point failed, acquisition `FAILED`, backend quarantined until its
worker returns. An instrument fault → §5 cancels the run.

### 8.3 Storage: one backend first, a journal with commit records

P-1 implements **one** backend and proves its recovery before a second is
added (Q-13). Proposed: an **append-only run journal** during acquisition,
finalised into a single **HDF5** file at the end — HDF5 is not written
incrementally, so no SWMR protocol is needed and a crash cannot corrupt a
half-written HDF5 structure.

Journal (a run directory):
- `run.json` — run metadata, written and fsynced at start;
- `samples/<instrument>.bin` — append-only fixed-size sample records per
  instrument (values, valid flag, reason code, t_host, device_t, device_id,
  generation, sequence, profile, verification);
- `controls.bin` — append-only per-point control records (requested,
  acknowledged, measured per control);
- `commits.log` — one line per committed point.

`commit_point` ordering (single writer thread):
1. append the point's sample records and control records; `fsync` those
   files;
2. append one **commit record** to `commits.log`:
   `{point, status, causes, controls: [offset, count], samples:
   {instrument: [offset, count]}, checksums: {file: crc32 of its extent}}`;
   `fsync` it (and, the first time, the directory).

**Reader validation:** a point exists only if its commit record is a complete
line whose referenced extents lie within the payload files and match their
checksums. A torn last line or extent is ignored. Sample counts per
instrument may differ per point; extents make that explicit.

**Guarantees, stated separately:**
- *application crash* (process dies, OS survives): every point whose commit
  record was appended is recoverable — the data reached the OS before the
  record did;
- *power loss / OS crash*: every point whose commit-record `fsync` returned
  is recoverable, assuming the storage honours `fsync` (Q-12 tunes
  frequency; batching trades the last N points for speed);
- payload written but no commit record → not a point (its samples are
  ignored); the next run never appends to an interrupted journal.

**Finalisation:** at the end (or by an explicit recovery command on an
interrupted journal) the journal is converted into one HDF5 run file
(acquisition outcome, or `INTERRUPTED` for a recovered run) and the journal is
kept until the HDF5 file has been written, fsynced and re-read successfully.

**In-process display** reads committed records from memory (read-your-writes);
ImProcess opens only finalised files (or runs recovery first). Zarr as a second
backend later, with its own recovery proof.

Run metadata: run id, owner, generator (kind, parameters; grid shape +
traversal map if a grid), controls (name, device, model / serial, unit,
readback / acknowledgement capability, zero-reference state §9.6),
instruments (identity, applied settings, timing profile + verification),
**illumination source** (laser entry name + declared wavelength, or "none
declared"), plane label, session notes, ImSwitch version, `schema_version`.
Angles are coordinates with units; no spatial calibration anywhere.

### 8.4 ImProcess discovery

`DataObj` recognises a run file by its schema marker and exposes a
`MeasurementRunSource`: run metadata, committed points, samples, and — when the
generator was a grid — derived per-quantity grid images (for generic viewing
only, with angle coordinates, no µm). Reconstructors for runs receive the run
source and validate it (§9.1).

## 9. Polarisation analysis

### 9.1 Input validation

The reconstructor requires a run with a `polarisation.*` instrument and refuses
on: mixed generations without a reconnect marker, more than one polarisation
instrument (choose one explicitly), missing DOP, units other than declared.
Interrupted / stopped runs are accepted with a warning; only `COMMITTED`
points are used.

### 9.2 Aggregation (shared analysis library, `imcommon/algorithms/polarisation.py`)

Per point from its raw samples:
- per sample: Stokes `S = P·(1, d·cos2χ·cos2ψ, d·cos2χ·sin2ψ, d·sin2χ)`
  (ψ azimuth, χ ellipticity, d instrument DOP, P power);
- **`dop_instrument`** = mean of instrument-reported d (polarisation of the
  light within each measurement);
- **`dop_aggregate`** = |mean(S₁,S₂,S₃)| / mean(S₀) (also falls when the state
  wanders between samples);
- direction = mean polarised Stokes vector normalised; ψ, χ from it;
  dispersion = RMS angular distance on the sphere of sample directions from it;
- undefined when mean S₀ ≤ 0 or |mean polarised vector| = 0 → the point is
  **ineligible** ("undefined direction"), never matched;
- power: mean, std, n.

Arithmetic averaging of ψ is never used (−89° and +89° are the same
orientation).

### 9.3 Matching

Targets: right / left circular, linear every 10° (0°…170°); configurable.
1. Eligible points: `COMMITTED`, aggregated from **valid** samples only,
   direction defined, `dop_instrument ≥ d_min` **and** `dop_aggregate ≥
   d_min` (default 0.95 both).
2. Per target, the nearest eligible point by angular distance on the sphere.
3. `pass` if distance ≤ threshold (default 5°), else `failed` — **only if
   every sample of the chosen point is `VERIFIED`**. With unverified samples
   the status is `unqualified` (shown, plotted, exported as such, never
   `pass`); a **qualification run** — a `points([...])` run of the
   unqualified pairs with verified timing — replaces it with a measured
   status.

Handedness convention stated by the reconstructor, verified on the rig with a
known quarter-wave plate. Result table: target, angle pair (measured or
commanded — flagged), distance, both DOPs, power, dispersion, status.

### 9.4 Model fit and predictions

Jones model (δ₁, δ₂, fast-axis offsets, input Stokes), least-squares on
eligible points; residual reported. Fitted angle pairs are `predicted`, with
predicted distance to target (a target may be unreachable); never `pass`. A
**follow-up run** with `points(predicted_pairs)` — the same run mechanism, no
fake grid — turns predictions into measured results.

### 9.5 Display (napari)

A table result plus `DisplayLayerSpec` layers: sphere wireframe and axes as
`shapes` paths, measured points as `points` (coloured per grid row when the
generator was a grid, else by sequence), paths per row, targets as `points`
with pass / failed / predicted styling.

### 9.6 Export and replay

`polarisation_states.json`: source file, run id, wavelength (from the
instrument's applied setting), plane label, convention, thresholds, control
identities (name, model, serial), angle unit, **zero-reference state** per
rotator, created; per target `{angle1, angle2, distance_deg, dop_instrument,
dop_aggregate, status}`.

Zero-reference states: `VERIFIED` (the device reports a reference identity,
e.g. a homing counter / index, recorded and compared), `USER_CONFIRMED` (the
user confirmed, at run time, that the mount and zero were not changed since a
named reference), `UNKNOWN`.

The replay helper refuses failed targets; refuses `predicted` unless
explicitly allowed; refuses `unqualified`; refuses different control
identities; with `VERIFIED`
compares the current reference and refuses a change; with
`USER_CONFIRMED` / `UNKNOWN` requires either an explicit confirmation or a
**verification measurement** (measure two or three exported states and compare
within threshold). Wavelength: the run records both the **instrument's applied
wavelength setting** and the **declared illumination source** (laser entry +
its `wavelength`); they are different facts — the meter's setting is only a
correction parameter. Export is refused when no illumination source was
declared or the two disagree beyond a tolerance (default 2 nm). At replay the
current wavelength comes from an explicit source (the laser entry used, passed
in or resolved from the active illumination), else the caller passes it.

## 10. Laser power LUT (same run mechanism)

The procedure is a script on the run API: controls = the laser's **raw-drive
control** (§7.4), instrument = PM100, generator = `sweep(raw values)`.

- **Raw drive only, with observable outcome.** The normal setter applies a
  loaded LUT and swallows errors, so the run uses the raw-drive control of
  §7.4 (`apply` → `ControlResult`, low-level error path, owner token only). Until that command
  exists for a laser kind, the procedure refuses when `usesCalibrationLookup()`
  is true and explains how to run from a setup without `calibCsvPath` (Q-11).
- **Dark zero:** the run records the laser's value and enabled state, disables
  emission, runs `zero` (`confirm_dark=True` after the user confirms the beam is
  blocked), re-enables; restores value then enabled state on every exit path.
  `_setLaserValue` disables a non-binary laser at ≤ 0 and never re-enables —
  the raw-drive control sets the enabled state explicitly. **Check the existing
  775 LUT for a flat curve.**
- **Two steps after the run:**
  1. **Acceptance**: acquisition `COMPLETE`; every point `COMMITTED` with
     every control `ok`; all samples valid, finite and **`VERIFIED`**
     (unverified runs give an exploratory plot only, until a qualification
     run with verified timing re-measures the sweep); instrument wavelength
     setting agrees with the declared laser wavelength; dynamic range ≥
     minimum (default 20× the zero-level noise).
  2. **LUT construction**: isotonic (monotone non-decreasing) regression of
     power on drive; collapse plateaus (equal fitted powers → keep the lowest
     drive) so power is strictly increasing; range = measured drive range;
     report the correction (max |measured − fitted| in units of measured
     noise) and refuse if it exceeds a limit (default 3σ). Export
     `setting power_W` (`calibCsvPath`-compatible).
- The run file is the measurement; the LUT file is an export, named so it is
  never mistaken for the measurement and vice versa.

Later, own plan: absolute power beside the % mapping.

## 11. Schema

Versioned from the first commit (`schema_version`, reader fixture committed).
**Frozen after P-1**, once the mock end-to-end run has exercised acquisition,
storage, reload and analysis — not before.

## 12. Phases

Each phase ends green on all CI lanes, docs updated, rig gates explicit.

- **P-0 Verify and stabilise `feat/device-reconnection`** (alongside P-1…P-3).
  Agree with its author (Q-1). Rebase onto `main` in its own worktree; run its
  tests and all lanes; review the commits — at least the supervisor's
  `__dict__` / class-name inference, listener callbacks on worker threads
  touching Qt, the preliminary (not held) scan/recording check, detector
  backend swap without shape notification, the Hamamatsu DCAM reset
  restriction, the PI stage rewrite. Fix on that branch with the author, add
  tests, rig-check reconnect. Exit: rebased, green, reviewed, ideally merged.
- **P-1 Mock end-to-end polarisation run.** In-process, no transient
  lifecycle, minimal reservation: mock PAX + mock rotators, `Sample` with
  validity, timing profiles + windows (`DEVICE_TIMESTAMP`, `UPDATE_BOUND`,
  `UNVERIFIED`) returning `WindowResult`, `ControlResult` from mock controls,
  `grid` and `points` generators, execution with Stop / failure and the three
  run outcomes, **one** storage backend (journal → HDF5) with recovery, reload
  through `DataObj`, aggregation library, matching incl. `unqualified`, napari
  layers, export. Tests: azimuth wrap-around, stale sample across a move,
  counter / clock-offset boundary, Stop mid-point, partial window kept,
  invalid samples vs transport fault, failed `apply` never `COMMITTED`,
  **failures injected during a commit** (after payload before record, torn
  record, bad checksum) and between commits, low-DOP-nearest counterexample,
  undefined direction, two-instrument refusal, unverified run never `pass`.
- **P-2 Settle the schema + decide adapters.** Freeze the schema; decide from
  P-1 whether detector adapters (Q-2) add real reuse.
- **P-3 Reservations** (§7): `ResourceRegistry` with atomic admission +
  in-flight tickets, pending reservations, token expiry, manager-boundary
  admission for the supported controls, owner-bound handles with token
  propagation across the UI-thread hop, worker quarantine, `waveform-output`
  bridged into scan arming in both directions, audited `apply` adapters.
  Tests: **a command paused right after admission vs a reservation**;
  concurrent move / settings / zero refused; facade call refused; sibling axis
  refused; expired token refused; stuck move keeps the backend quarantined and
  blocks cleanup dispatch; waveform scan refused during a run and vice versa;
  NI-DAQ `setAnalog` failure surfaces as `ok=False`; shutdown with a live
  worker is fail-closed.
- **P-4 Transient connection + real drivers** (needs P-0): `ABSENT` /
  `FAULTED`, connect / disconnect / faults / generations / status signal /
  Hardware status buttons; PM100 and PAX drivers. *Rig gates:* each driver vs
  Thorlabs software and its timing rule verified; USB removal mid-run.
- **P-5 Laser procedure** on the same run API: raw-drive control (or refusal),
  dark zero, cleanup restoring value + enabled state with its own outcome,
  acceptance (verified timing, wavelength agreement) + LUT construction, AOTF
  utility on the shared PM100 driver. Tests: recalibration with a loaded LUT,
  flat / non-monotonic / partial / unverified measurements never exported,
  restoration failure reported beside complete data. *Rig gate:* re-measure
  the 775 line.
- **P-6 Expand**: Readings dock polish, a run widget (generators, controls,
  instruments, estimate, progress), more audited adapters, Jones fit +
  follow-up runs, optional detector adapters / camera participation (Q-7);
  docs and `tools/update_user_defaults_history.py` for shipped scripts.

## 13. Risks

- **Reconnection branch** — P-0 may find more than expected; P-1…P-3 do not
  depend on it.
- **Reservation coverage** — only the audited controls are enforced; others
  must not be offered to runs.
- **Quarantine visibility** — a backend held by a stuck worker must be obvious
  to the user, with the operation named.
- **VISA back-ends** and another program holding the device.
- **Timing verification** needs rig time; quantitative use waits for it.
- **Durability cost** of per-point fsync on slow disks (Q-12).
- **Profile tables** need rig time per firmware / mode; outside them the
  instrument is exploratory only.

## 14. Review round 3 (2026-10-07) — dispositions

| # | Finding | Disposition |
|---|---|---|
| 1 | Waveform scans bypass manager-level reservations | §7.5 `waveform-output` resource bridged into scan arming in both directions; coarse global exclusion first |
| 2 | Check-then-act races a reservation | §7.2 one registry lock for admission, in-flight tickets and reservation; pending reservations; token expiry; paused-after-admission race test |
| 3 | Setters can fail silently | §7.4 `apply` → `ControlResult` (requested / acknowledged / measured, ok, cause); low-level error path (`setAnalog(..., raise_on_error=True)`) |
| 4 | Unverified timing can still pass / publish | §6.3 verification on every sample; §9.3 `unqualified` status; §10 acceptance requires verified; qualification runs |
| 5 | Crash guarantee stronger than the protocol | §8.3 journal with commit records (per-instrument extents + checksums), payload-before-record fsync ordering, reader validation, app-crash vs power-loss guarantees, one backend first, HDF5 written only at finalisation (no SWMR) |
| 6 | Partial window samples lost on timeout | §6.3 `WindowResult` carries samples, completeness and cause; no exception for timeout / cancel / fault / invalid |
| 7 | Completion, cleanup and release conflated | §8.1 acquisition outcome, cleanup outcome, lifecycle; no cleanup dispatch to a busy backend; `FINISHED` only after release or explicit quarantine |
| 8 | Timing verification is per configuration | §6.3 timing profiles matched on connect and every settings change; counter and clock-offset mapping |
| + | Transport ownership contradictory | §7.1 rules: devices with state are reserved, transports are not (byte lock), transport lifecycle refused while a user is reserved |
| + | Meter wavelength ≠ illumination | §8.3 run records both; §9.6 / §10 export refused without agreement |
| + | Validate samples | §6.2 valid ranges; invalid sample ≠ transport error; invalid never aggregated |

## 14a. Review round 2 (2026-10-07) — dispositions

| # | Finding | Disposition |
|---|---|---|
| 1 | Virtual detectors should be optional adapters | D-2: `Instrument → Sample → MeasurementRun` authoritative; adapters decided after P-1 (Q-2). No detector entries per quantity; quantities selected for display / export |
| 2 | Data should be discoverable, not frame-first | D-3, §2, §8.4: run = ordered point sequence; generators `grid` / `points` / `sweep`; grid images derived |
| 3 | Reuse ownership / recording at narrow boundaries | D-4: reservation, run lifecycle, run storage; software execution separate from the waveform path |
| 4 | Workflows diverge | §10: laser LUT is a run + export product on the same API |
| 5 | Participant rule rejects its own recording | §8.1: runs take no detector leases; future participation by run identity, never backend alone |
| 6 | Ownership enforcement / propagation underspecified | §7.2 manager-command boundary (covers the facade); §7.3 owner-bound handles, token as a parameter across thread hops; §7.1 controller / transport conflicts |
| 7 | Deadline does not bound the hardware operation | §7.3 release only after physical worker return, backend quarantine, fail-closed shutdown; §7.4 audited adapters only |
| 8 | Two competing write paths | §8.3 one writer, `commit_point`, derived display (r3 watermark replaced in r4 by commit records, see round 3 #5) |
| 9 | Two DOP definitions | §9.2 `dop_instrument` and `dop_aggregate` both stored and both required for eligibility; undefined direction ineligible; maths in an analysis library (D-6) |
| 10 | "Monotonic within noise" ≠ an inverse | §10 acceptance separate from construction; isotonic fit, plateau collapse, correction reported and bounded |
| 11 | Replay guarantees beyond the metadata | §9.6 zero-reference states VERIFIED / USER_CONFIRMED / UNKNOWN, verification measurement; explicit wavelength source |
| order | Prove the model first | §12: P-1 mock end-to-end first, schema frozen after it (§11), P-0 in parallel |

## 15. Review round 1 — dispositions (still valid under r3)

Backend-aware participants (r1 #1) → superseded by §8.1 (no detector
participation in runs). Transaction ownership (#2) → §7. Post-settle windows
(#3) → §6.3. Angle averaging (#4) → §9.2. Raw-drive LUT (#5) → §10. Run bundle
validation (#6) → §8.4, §9.1. Per-point validity (#7) → §8.3. Bounded stop
(#8) → §7.3, §8.2. Matching order (#9) → §9.3. Deployable LUT validation (#10)
→ §10. Non-spatial axes (#11) → §8.3 (no spatial calibration in run files;
grid images carry angle coordinates). Unexpected disconnects (#12) → §5.

## 16. Open questions

- **Q-1 Reconnection branch:** **decided 2026-10-07:** its author handed it
  over; we take over `feat/device-reconnection`, fix it to our needs, and test
  it together with this work (its core is tested; remaining bugs found along
  the way).
- **Q-2 Detector adapters:** **decided 2026-10-07 (Lenny): none for now.**
  Revisit only if a consumer genuinely needs instrument quantities as
  detectors.
- **Q-3 `connectOnStartup`** for transient instruments?
- **Q-4 Readings dock** placement.
- **Q-6 Grid traversal default:** snake or raster?
- **Q-7 Cameras in runs** (snap per point) — later phase that would replace
  `RotationScanController` and the WFS camera sweep?
- **Q-9 Matching defaults:** 5°, DOP ≥ 0.95 (both definitions)?
- **Q-10 Remote API** for reservations, runs and instruments?
- **Q-11 Raw-drive control** for NI-DAQ / AA AOTF lasers in P-5, or refusal
  only at first?
- **Q-12 Durability:** fsync every point, or every N points / seconds?
- **Q-13 First storage backend:** journal → HDF5 (proposed), or Zarr first?

## 17. Implementation notes

**P-1 (done).** Shared types, journal and run file in
`imcommon/model/measurement_run/`; polarisation maths in
`imcommon/algorithms/polarisation.py`; engine, instruments, controls, mocks in
`imcontrol/model/measurement/`; reconstructor `polarisation-map` in
`improcess/reconstructors/polarisation_map/`. Demo:
`python -m imswitch.imcontrol.model.measurement.demo DIR`. Defaults taken for
open questions: Q-13 journal → HDF5, Q-12 fsync every point, Q-6 raster,
Q-9 5° / DOP ≥ 0.95.

**P-2.** Q-2 decided: no detector adapters. The run-file schema stays at
`schema_version` 1 and versioned; it is formally frozen when the first real
instrument has been through it (P-4).

**P-3 (done).**
- `imcontrol/model/resources.py`: one process-wide `ResourceRegistry`
  (admission with in-flight tickets, pending reservations, bounded waits,
  expiring tokens, re-entrant nested calls, non-re-entrant long holds).
- Manager boundary: `RotatorManager`, `PositionerManager` and `LaserManager`
  wrap their subclasses' mutating methods (`move_abs/move_rel`,
  `move/setPosition`, `setValue/setEnabled/applyRawDrive`) in
  `__init_subclass__`, so every manager — built-in or plugin — admits its
  commands; `owner=<token>` passes a reservation. A positioner manager is one
  resource (sibling axes are refused).
- Raw drive: `LaserManager.applyRawDrive` (raises `RawDriveError`, bypasses
  the LUT), implemented for `NidaqLaserManager` (`setAnalog(...,
  raise_on_error=True)`) and `AAAOTFLaserManager` (refused in mock mode).
- Rotators gained `readPosition()` (fresh) and `isSimulated` (fell back to a
  mock: refused by run controls). Kinesis and Elliptec interfaces cannot stop
  a move: deadlines + quarantine only.
- Audited adapters `imcontrol/model/measurement/adapters.py`.
- Waveform outputs: the scan coordinator holds `waveform-output` for every
  scan run and every bare iteration; a measurement run / script reservation
  of it refuses scans and vice versa.
- Scripts: `api.imcontrol.reserve(rotators=…, lasers=…, positioners=…)` →
  owner-bound handle (`ReservationController`, API-only).
- Shutdown: reservations end after the scripting drain (so lasers-off is
  admitted); call-scoped commands still in flight after the controllers
  closed make hardware finalization fail closed
  (`ShutdownState.recordBusyBackends`).

Not yet: widgets greying out from registry state (a refused GUI command is
refused and logged by the exception handler, nothing reaches hardware);
instruments in the setup (P-4).

**P-1 review (fixed, 8998b11a0).** Cancellation-safe runner, bounded instrument
reads with retained ownership, asynchronous stop after quarantine, `corrupt`
recovery outcome, configuration revision on boundaries, unit conversion in
the polarisation map, verified-first matching.

**P-4 steps 1-2 (done on mocks).**
- Setup: `instruments` section (`InstrumentInfo`: `transient`,
  `connectOnStartup`), kind `instrument`, `InstrumentsManager`; base
  `managers/instruments/InstrumentManager.py` owns one `InstrumentSession`;
  `MockPowerMeterManager`, `MockPAXManager`.
- New runtime mode `ABSENT` (intentionally not connected). A failed connect
  is `ERROR` + `ABSENT`, never a mock; a transport fault while connected is
  `ERROR` and notifies status listeners.
- `DeviceLifecycleService.connect / disconnect` beside `reconnect`, one
  transition path (capability, shared-transport veto, shutdown refusal,
  per-device lock). A lifecycle with `affectsAcquisition = False`
  (instruments) skips the scan / recording guard and the acquisition gate:
  instruments are guarded by their reservation, which every transition is
  admitted through.
- `DeviceLifecycleService.addStatusListener(hardware_id)` forwards self-reported
  status changes (instrument faults); the Hardware status controller marshals
  them to the UI thread and refreshes. The window has Connect / Disconnect
  (shown only for devices that offer them) and an "Instruments" group; a
  not-connected instrument is neutral, not a connection issue.
- Not yet: `FAULTED` is shown as `ERROR` (no separate state); the Readings
  dock (§6.4); scripting access (step 4).

**P-4 step 3 (done against a fake VISA; rig gates open).**
`imcontrol/model/measurement/thorlabs.py`: `VisaLink` (resource by serial,
every VISA failure a `TransportError`, pyvisa imported on connect only),
`ThorlabsPM100Driver`, `ThorlabsPAX1000Driver`; managers
`ThorlabsPM100Manager` / `ThorlabsPAX1000Manager` (`serial` required).
Quantity specs shared with the mocks (`measurement/quantities.py`).
Instrument managers read `self._managerProperties` (bound by the base from
the `InstrumentInfo`), so the config editor lists their keys.
Differences from §6.6 and things the rig must confirm:
- PM100 zero sends `SENS:CORR:COLL:ZERO:INIT` (the manual's form; the old
  script sent `SENSE:ZERO:INIT` and never checked it), checks `SYST:ERR?`,
  and waits for `SENS:CORR:COLL:ZERO:STAT?` to read 0 (15 s bound).
- PAX: `SENS:CALC?` / `SENS:WAV?` readbacks; a query the firmware does not
  answer keeps the commanded value and lists the setting in
  `unconfirmed_settings` instead of refusing to connect. Wavelength readback
  below 1 is taken as metres.
- PAX power field unit: manager property `powerUnit` (`W` default, `mW`).
- PAX timing: `UPDATE_BOUND` with `updateBoundS` 0.5 / `updatePeriodS` 0.1,
  unverified; `last_fields` keeps the whole packet for identifying fields 0-8.
- The AA AOTF utility's `PM100D` is now an adapter over the shared driver;
  its Zero blocks until the meter reports zeroing done.

**P-4 step 4 (done).**
- Scripts: `api.imcontrol.reserve(instruments=[...])` →
  `handle.instrument(name)` with `read(n, deadline_s, allow_unverified)`,
  `set`, `action(..., confirm_dark=)`, all carrying the token. API-only
  `InstrumentsController`: `getInstruments`, `connectInstrument`,
  `disconnectInstrument` (through the lifecycle service), `readInstrument`
  (a quick unverified look, refused while held), `measureLaserPowerLut`
  (`confirm_dark` required; laser state from the Laser panel's shared
  attributes, refused without one; runs default to
  `ImSwitchConfig/measurement_runs`).
- `example_no_hardware.json` carries a transient mock power meter and mock
  PAX (history regenerated).
- Docs: `docs/devices/instruments.rst` (setup, Hardware status, scripting,
  LUT, a card per manager), a reservations section in `scripting.rst`.

P-4 rig gates still open: §6.6 per driver, USB removal mid-run.

**P-4 review (8e27a5f69, six findings, all fixed).**
1. VISA: pyvisa shares one resource manager per VISA library and closing it
   closes every session. `VisaLink` now reference-counts it
   (`_ResourceManagers`); the last link closes it.
2. `ElliptecRotatorManager.isSimulated` read a bus attribute that no longer
   exists; it is now `not bus.is_real(address)`. `RotatorManagerControl` and
   `LaserRawDriveControl` re-check simulation on every command (a bus can
   fall back mid-run, and a simulated move "succeeds"); laser controls also
   refuse a mock-mode backend.
3. Lifecycle transitions now admit a ticket on the resource of every
   affected device (the handle's managers plus every device depending on
   it) for the whole transition: refused while a run or script holds one,
   and reservations wait for the transition.
4. `LaserManager.applyEnabled / applyValue`: checked emission and value
   commands (NI-DAQ `setDigital(raise_on_error=)`, AA NAK = failure);
   `ManagerLaserState` uses them, so a failed emission-off stops the
   procedure before the dark confirmation and a failed restore is a FAILED
   cleanup. `applyEnabled` returns False for a laser with no digital line
   (recorded as `emission_switched_off: false`).
5. Cleanup steps run through `ControlExecutor.run_step` (deadline,
   quarantine, resource kept reserved until the step returns); the run now
   reserves every cleanup step's resource.
6. A `TransportError` from `set_setting` / `run_action` faults the session
   like a read does.

Still not bounded by the executor: prepare steps (they rely on the
drivers' own I/O timeouts).

**P-5 (done on mocks).** `imcontrol/model/measurement/laser_lut.py`
(`run_laser_lut`): prepare steps inside the reservation (record laser state,
set + verify the meter wavelength, emission off, caller confirms the beam is
blocked, zero, dark noise, emission on), raw-drive sweep, restore as a
cleanup step. The runner gained `PrepareStep` / `CleanupStep`. Acceptance:
`imcommon/model/measurement_run/power_lut.py`; construction (isotonic fit,
plateau collapse, correction and dynamic-range limits) and the
`calibCsvPath` writer: `imcommon/algorithms/power_lut.py`. Real lasers:
`adapters.ManagerLaserState` + `LaserController.getLaserValue/getLaserActive`.
Mocks: `MockLaser`, `MockPowerMeterDriver` (its zero bakes in whatever light
is on). Still owed: the AA AOTF utility switching to the shared PM100 driver
(needs the P-4 driver); a script entry point once instruments live in the
setup (P-4); the 775 rig re-measure.

