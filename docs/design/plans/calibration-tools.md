# Calibration tools: optional instruments with a live readout

Status: **SUPERSEDED 2026-10-07 by `transient-instruments-step-scans.md`** (instruments as transient setup devices with virtual sampled detectors, a software step scan and an ImProcess polarisation reconstructor). Kept for the instrument details and review rounds 0-1. Previous status: **plan, review round 1 answered** (2026-10-06; Lenny's PM100D + PAX1000 helpers folded into §8; round-1 findings → §5 freshness + actions, §6 sample path + shutdown, §8 zeroing; prior art → §9). Branch `feat/calibration-tools`,
worktree `../Imswitch2-calibration-tools`. Nothing implemented.

## 1. Motivation

Some instruments are used to *calibrate* a microscope but are not part of it:
a Thorlabs PM100 power meter held in the back focal plane, a Thorlabs PAX1000
polarimeter behind a pair of motorised waveplates. Today they are driven from
ImScripting through private helper functions (pyvisa). That works for the
procedure, but:

- there is no quick "plug it in and look at the number" — every check needs a
  script;
- each lab re-writes the connection and read code;
- nothing in ImSwitch knows the instrument exists, so the result of a
  calibration (a laser power LUT, an angle → polarisation table) has no
  standard shape or home.

Goal: a **Calibration** menu in ImControl listing the calibration tools that
are installed. Clicking one opens a window where the user enters how the
instrument connects (VISA resource, COM port, serial number — whatever that
tool needs), connects, and gets a live reading. Scripts use the **same**
connected instrument, so calibration procedures stay scripts.

## 2. Scope

In scope:

- a new plugin contribution type, `calibration_tools`, next to
  `device_managers` in the existing `imswitch.manifest` JSON manifests;
- a tool contract in `imswitch.pluginapi` (driver: Qt-free; optional custom
  view);
- a session hub in ImControl that owns connected tools, the Calibration menu,
  and one generic tool window;
- a scripting handle (`api.imcontrol…`) onto the same sessions;
- a mock power meter (tests, CI, demos);
- two reference tools: Thorlabs PM100 and Thorlabs PAX1000, built from
  Lenny's existing helper functions;
- the two procedures (laser power → LUT, waveplate angles → polarisation state)
  shipped as ImScripting example scripts.

Out of scope (later plans):

- lasers *consuming* a power LUT (§9 sketches it);
- calibration tools as setup-file devices, or in recordings' metadata;
- drop-in single-file calibration tools (the ImProcess drop-in route);
- remote control (web API) of calibration tools beyond what `APIExport`
  gives for free — see Q-6.

## 3. Decisions

**D-1 — Not a setup device kind.** Calibration instruments are borrowed,
moved between rigs, and plugged in for an afternoon. As setup devices
(`MultiManager` kinds) they would need a config edit + restart to use, would
fail or fall back to mock at startup when unplugged, and would appear in every
recording's metadata as part of the microscope. They are a separate
contribution type, connected at runtime.

**D-2 — Reuse the device-plugin manifest and discovery, not a second plugin
system.** Same `imswitch.manifest` entry point, same JSON manifest, a second
contributions list. Discovery already reads manifests without importing
implementation modules (`model/plugins/discovery.py`), which is exactly what
a menu of optional tools needs: listing costs nothing, the tool's module (and
pyvisa) is imported only on click.

**D-3 — "Installed" is the visibility rule; users can hide more.** A tool is
in the menu iff its package is installed (or it is a built-in). Each user can
additionally hide tools (Calibration ▸ Manage tools…), persisted in
`imcontrol_options.json`. This is the answer to "it gets messy when everyone
adds their calibration devices": a lab that never installed a package never
sees its tools, and a lab that has many can trim the menu.

**D-4 — A menu, not a toolbar, and a floating window per session.** ImControl
has a menu bar (File / Hardware / View / Shortcuts / Preferences), no
toolbar. A top-level **Calibration** menu sits between Hardware and View,
grouped by the manifest's `category`. Each connected instrument gets a
non-modal top-level window, not a dock: a calibration tool is transient and
must not disturb the saved dock layout (the dock sizing work in PR #38 showed
how fragile that is).

**D-5 — Driver and view are separate; the generic view covers most tools.** A
tool *declares* its connection fields, its channels (name, unit) and its
settings (e.g. wavelength); the generic window builds the connection bar,
live readout, rolling plot, settings form and CSV recording from those
declarations. A tool *may* add one custom view (e.g. a Poincaré sphere for the
PAX). The driver is Qt-free so the same object serves scripts and tests.

**D-6 — Procedures stay scripts; scripts share the window's session.** Both
known procedures combine a setup device (laser, rotators) with a calibration
tool, and labs vary them. They stay ImScripting scripts. `openCalibrationTool`
returns the session the window already holds if it is open, so the user can
watch the number while the script sweeps.

**D-7 — One I/O lock per session; live polling yields.** VISA calls block and
instruments are not re-entrant. Each session serialises driver I/O through
one lock. The live poller uses a non-blocking acquire and *skips* a tick when
a script holds the lock; it never queues behind it. A script can take the
instrument exclusively (`with tool.exclusive():`) for a sweep; the window
shows "In use by script" and greys the settings and actions, but keeps
updating from the script's own reads (one sample path, §6).

## 4. Manifest and registry

New contributions list, parsed beside `device_managers`:

```json
{
  "contributions": {
    "calibration_tools": [
      {
        "id": "thorlabs.pm100",
        "display_name": "Thorlabs PM100 power meter",
        "category": "Power",
        "python_name": "imswitch_device_thorlabs.calibration:PM100Tool",
        "mock_python_name": "imswitch_device_thorlabs.calibration:MockPM100Tool",
        "requires": ["pyvisa"],
        "docs_url": "https://…",
        "supported_platforms": ["linux", "win32", "darwin"]
      }
    ]
  }
}
```

- `CalibrationToolContribution` dataclass and `parse_calibration_tools()` in
  `model/plugins/manifest.py`; the existing `parse_manifest` stays
  `device_managers`-only so nothing about device resolution changes.
- `category` is free text; the menu groups by it, sorted, with known
  categories (Power, Polarization, Wavelength, Position) first.
- `requires` lists *import names*, checked with `importlib.util.find_spec`
  (no import). A tool whose requirements are missing stays in the menu,
  disabled, with a tooltip naming what to install — a silent absence is
  worse than a greyed entry.
- Built-in tools (the mock, and the reference tools if Q-1 keeps them
  in-tree) register through an explicit table, as `builtins.py` does for
  device managers.
- Id collisions: same policy as the device registry (built-in wins, the
  clash is logged, `python -m imswitch.imcontrol.model.plugins list` shows
  it). The diagnostics CLI lists calibration tools in their own section.

## 5. Tool contract (`imswitch.pluginapi.calibration`)

```python
@dataclass(frozen=True)
class ConnectionField:
    name: str                 # 'resource', 'port', 'serial'
    label: str
    kind: Literal['visa_resource', 'serial_port', 'text', 'int']
    default: Any = None

@dataclass(frozen=True)
class Channel:
    name: str                 # 'power', 'azimuth', 'ellipticity', 'dop', 'S1'
    unit: str                 # 'W', 'deg', '', …
    plot: bool = True

@dataclass(frozen=True)
class Setting:
    name: str                 # 'wavelength_nm', 'averaging'
    label: str
    kind: Literal['float', 'int', 'choice', 'bool']
    unit: str = ''
    limits: tuple | None = None
    choices: tuple = ()

@dataclass(frozen=True)
class Action:
    name: str                 # 'zero'
    label: str                # 'Zero'
    confirm: str = ''         # shown before running; '' = no confirmation
    requires_dark: bool = False   # see §8 zeroing: needs an explicit "beam blocked"
    timeout_s: float = 10.0   # upper bound the driver waits for completion

@dataclass(frozen=True)
class Reading:
    t_host: float             # time.time() when the read returned
    values: Mapping[str, float]
    sample_id: Hashable | None = None   # instrument-side identity, if the tool has one

class Freshness(Enum):
    PER_READ = 'per_read'     # every read() is a new measurement (verified per tool)
    SAMPLE_ID = 'sample_id'   # read() may repeat; Reading.sample_id tells them apart
    MIN_INTERVAL = 'min_interval'  # read() may repeat; a new one exists only after
                                   # `min_interval_s` since the last accepted sample

class CalibrationTool(ABC):
    connection_fields: ClassVar[tuple[ConnectionField, ...]]
    channels: ClassVar[tuple[Channel, ...]]
    settings: ClassVar[tuple[Setting, ...]] = ()
    actions: ClassVar[tuple[Action, ...]] = ()
    freshness: ClassVar[Freshness]
    min_interval_s: ClassVar[float] = 0.0
    visa_match: ClassVar[tuple[str, ...]] = ()   # IDN / resource substrings for the Scan button

    @abstractmethod
    def connect(self, **params) -> InstrumentIdentity: ...   # vendor, model, serial, firmware
    @abstractmethod
    def close(self) -> None: ...                 # must return within its VISA timeout
    @abstractmethod
    def read(self) -> Reading: ...
    def get_setting(self, name): ...
    def set_setting(self, name, value): ...      # returns the value that applies (readback)
    def run_action(self, name, **kwargs): ...    # blocks until done or Action.timeout_s
    def make_view(self, parent):                  # optional custom Qt view; default None
        return None
```

The driver methods are **never called directly** by the window, the poller
or scripts — only by the session (§6), which serialises them under the
session lock. That is what keeps an action such as zeroing from running
between two reads of a script's sweep.

**Freshness (what counts as a new measurement).** Host time cannot tell a
repeated instrument sample from a new one, and equal values can be two genuine
measurements, so neither is used to detect staleness. Each tool *declares*
its rule (`freshness`), and the session applies it to every read:

- `PER_READ` — accepted as-is. Only allowed once the rig check shows the
  command triggers a fresh measurement (PM100 `READ?`: to verify, §8).
- `SAMPLE_ID` — a reading whose `sample_id` equals the last accepted one is a
  repeat: not published, not recorded, not averaged.
- `MIN_INTERVAL` — a reading earlier than `min_interval_s` after the last
  accepted one is a repeat. The fallback when the instrument exposes no
  counter (e.g. one PAX waveplate revolution).

A tool whose rule is unknown declares `MIN_INTERVAL` with a conservative
interval, never `PER_READ`.

**Actions** are declared, so the generic window renders them as buttons and
scripts call them by name — no PM100-specific code in the window. An action
with `confirm` shows that text first; one with `requires_dark` additionally
needs an explicit "the beam is blocked" confirmation (a checkbox in the
dialog; `confirm_dark=True` from a script — omitting it raises). While an
action runs it holds the session lock, so the poller skips and script calls
wait.

- `visa_resource` fields get a **Scan** button: `list_resources()` filtered by
  `visa_match`, each candidate shown with its `*IDN?` serial, so the user picks
  "PM100D (P0012345)" instead of typing a resource string. Querying `*IDN?`
  on every resource can hang on some serial devices — the scan queries only
  USB resources that match `visa_match`, with a short timeout.
- `serial_port` fields get a port drop-down (`serial.tools.list_ports`).
- The driver never touches Qt; `make_view` imports Qt lazily.

## 6. Session hub, menu and window (ImControl)

- `CalibrationHub` (model, Qt-free) owns sessions keyed by
  `(tool id, connection identity)`: connect, close, the I/O lock (D-7), the
  live poller thread, the last N readings (ring buffer), and the CSV
  recorder.
- **One sample path.** Every driver `read()` — from the poller, from a script's
  `read()`, and each individual read inside `read_mean()` — goes through one
  session method that (1) applies the freshness rule, (2) on a new sample
  appends it to the ring buffer and (3) publishes it to subscribers. The
  window and the CSV recorder are subscribers; nothing reads the driver
  around this path. So during `exclusive()` the poller is paused but the
  window still updates — from the script's own reads.
- Each published sample carries its source (`live` / `script`). Subscribers
  run on the reading thread and must not block: the window receives samples
  through a queued Qt signal, the recorder through a queue drained by its own
  writer thread.
- **Recording semantics.** While recording, every published sample is written,
  whatever its source, with a `source` column; repeats are never written
  (they are not published). `read_mean()` does not write its mean as a row —
  the individual samples are already there; a script that wants a marker
  calls `tool.annotate('step 12: 3.5 mW set')`, written as a comment row with
  the current time.
- `CalibrationController` (controller) builds the menu from the registry,
  opens one `CalibrationToolWindow` per session, and exposes the scripting
  API (§7).
- The generic window, top to bottom: connection bar (fields + Scan + Connect)
  · identity line (model, serial, firmware) · big live value(s) with units
  and SI prefixes · rolling plot (pyqtgraph, selectable channels, window
  length) · settings form · Record to CSV / Stop · optional custom view.
- Poll rate: per-window spin box, default 5 Hz, capped by how fast `read()`
  returns (the poller never overlaps reads).
- Remembered per tool in `imcontrol_options.json`: last connection
  parameters, poll rate, plot channels, hidden tools. Not in widget-state
  persistence: these are properties of the computer, not of a session.
- Closing the window closes the session unless a script holds it; then the
  window closes and the session stays until the script releases it.
- **Shutdown** follows the existing fail-closed rule
  (`imcommon/model/shutdown.py`: when the scripting drain fails,
  `ImConMainController` skips hardware-manager finalization because the script
  may still be inside a manager call). The hub runs, in ImControl's close
  path, before the hardware managers:
  1. refuse new `open` / `read` / action calls (they raise);
  2. stop every poller and **join** it, bounded by the tool's VISA timeout +
     margin (the poller only blocks inside one read, which that timeout
     bounds); stop the recorder and flush its file;
  3. if `shutdownState.hardwareFinalizationAllowed()` is false, **do not close
     any driver**: a script may still hold the lock or be inside a VISA call,
     and closing under it is concurrent access. Log which sessions were left
     open; the OS releases the USB session at process exit;
  4. otherwise close each session: acquire its lock with the same bound;
     acquired → `close()` (which, for the PAX, stops the waveplate); not
     acquired → skip and log, as in step 3.

  Nothing in this sequence waits without a bound.

Output: CSV with a header block — tool id, instrument identity, settings at
start (wavelength!), ImSwitch version, start time — then `t, <channels…>`.
Default folder: the recordings folder preference, sub-folder `calibration/`.

## 7. Scripting API

On the ImControl API (`api.imcontrol`, `@APIExport`), next to
`buildWorkflowFacade`:

```python
pm = api.imcontrol.openCalibrationTool('thorlabs.pm100', resource='USB0::…')
api.imcontrol.listCalibrationTools()         # installed ids + open sessions
pm.set('wavelength_nm', 488)
pm.read()                                    # Reading
pm.read_mean(n=20, timeout_s=10)             # 20 DISTINCT samples, see below
pm.action('zero', confirm_dark=True)         # declared actions, run under the lock
pm.annotate('BFP, 775 nm, ND removed')       # comment row in a running recording
with pm.exclusive():                         # pauses the live poller
    for p in powers:
        laser.setValue(p); time.sleep(0.5)
        rows.append((p, pm.read_mean(10, timeout_s=10)['power']))
pm.close()                                   # no-op if the window still shows it
```

- `read_mean(n, timeout_s)` collects **n distinct samples** (freshness rule,
  §5) started after the call, and returns per channel `mean`, `std`, `n` and
  the samples' time span. If the deadline passes first it raises
  `TimeoutError` naming how many it got — it never pads with repeats, which
  would shrink the reported std. Each underlying read is published like any
  other (§6). `timeout_s` defaults to `n × max(min_interval_s, last read
  time) × 3`.
- `action(name, **kw)` dispatches through the session lock; there is no way
  to reach the driver around it.

- `openCalibrationTool` with no connection arguments re-uses the open
  session for that id if there is exactly one, else raises with the list.
- The handle's methods run on the calling (script) thread, guarded by the
  session lock — not marshalled to the UI thread.
- The WFS `MicroscopeFacade` (`model/workflows/facade.py`) can later gain a
  `calibration` slot; not part of this plan (Q-5).

## 8. Reference tools and procedures

**Thorlabs PM100 (`thorlabs.pm100`)** — from Lenny's `PM100D` helper and the
775 AOM calibration script (received 2026-10-06):

- *Connection:* serial number (e.g. `P0011748`), matched as a substring of the
  VISA resource (`USB0::0x1313::0x8078::<serial>::INSTR`); back-end `''` /
  `'@py'`; timeout 2 s. No termination characters (USBTMC). The Scan button
  filters on vendor id `0x1313` (Thorlabs) — this also finds the PAX, so
  the list shows `*IDN?` model + serial per entry.
- *Commands:* `*IDN?`; `READ?` → power in W; `SENSE:CORR:WAV?` /
  `SENSE:CORR:WAV <nm>`; `SENSE:ZERO:INIT` (zeroing).
- *Channel:* `power` (W, the window shows SI prefixes). *Settings:*
  `wavelength_nm` (read back after writing — the meter clamps it to the
  sensor's range and the readback is the value that applies). *Action:*
  `zero` — `requires_dark=True`, confirm text "Block the beam at the sensor".
- *Freshness:* `MIN_INTERVAL` with a conservative interval until the rig
  check below shows `READ?` measures afresh; then `PER_READ`.

To verify on the rig (P-2), not assumed:

- **Zeroing.** The helper marks `SENSE:ZERO:INIT` "to be tested". It is a dark
  measurement: it must run with the beam **blocked/laser off**, and it takes
  time — the driver must wait for it to finish (a status query, if the
  firmware has one, else a documented fixed wait) before the next `READ?`.
  The Zero button says "Block the beam first".
- Whether `READ?` triggers a fresh measurement (≈ its averaging time) or
  returns the last one; that sets the useful poll rate.
- Averaging (`SENS:AVER:COUN`) and auto-range commands: not in the helper;
  add only once checked against the PM100D manual / the device.

**Thorlabs PAX1000 (`thorlabs.pax1000`)** — from Lenny's `PAX1000` helper
(VISA + SCPI, received 2026-10-06):

- *Connection:* the user gives the **serial number** (e.g. `M01012314`); the
  driver picks the VISA resource whose name contains it. The helper falls back
  to "first USB resource" when no serial is given — the tool does **not**: with
  a PM100 and a PAX both on USB that would silently open the wrong instrument.
  The Scan button lists the candidates instead. Second field: VISA back-end
  (`''` = NI-VISA, `'@py'` = pyvisa-py), default `''`. Timeout 5 s.
- *Connect sequence:* `*IDN?` (identity + sanity check) → `SENS:CALC 9`
  (measurement mode; the field layout below depends on it) →
  `INP:ROT:STAT 1` (start the rotating waveplate). *Close:* `INP:ROT:STAT 0`,
  then close the resource and the resource manager.
- *Read:* `SENS:DATA:LAT?` → comma-separated floats; in mode 9, field 9 =
  azimuth, 10 = ellipticity, 11 = DOP, 12 = power. Channels: `azimuth`,
  `ellipticity` (**rad** — the helper feeds them to `cos(2·az)` directly; the
  window shows degrees), `dop`, `power`, plus derived `s1 = cos2az·cos2el`,
  `s2 = sin2az·cos2el`, `s3 = sin2el` (normalised Stokes of the polarised part).
- *Settings:* `wavelength_nm` → `SENS:WAV <metres>`.
- *Freshness:* `SAMPLE_ID` if fields 0–8 of the packet include a revolution
  counter or device timestamp (to check on the rig — that field becomes
  `sample_id`); otherwise `MIN_INTERVAL` = one waveplate revolution at the
  configured speed.
- *Custom view:* Poincaré sphere — wireframe, axes, the session's points
  coloured by time, or by trajectory when the data carries a `traj_idx`
  column (the waveplate map writes one per sweep line). It can also **open a
  saved CSV** and show it, replacing `load_calib_measurement_from_csv_to_show_poincare`.
  Rendered with an embedded matplotlib canvas (the helper's code ports
  almost unchanged); no `mpl.use(...)`, no tkinter dialogs.

To verify on the rig (P-3), not assumed:

- **Power unit.** The helper names it `power_mw`; the SCPI layer usually
  returns SI. Compare with the Thorlabs PAX software at one setting.
- **`SENS:WAV` unit** (the helper's own comment says metres, unverified).
- **Field indices.** The helper drops non-numeric tokens before indexing, so
  one unexpected token would shift every field silently. The driver checks the
  packet length for mode 9 and refuses a short or malformed packet instead.
- Whether `SENS:DATA:LAT?` returns a *new* measurement each call or repeats
  the last one until the waveplate completes a revolution (sets the useful
  poll rate; the window should not plot duplicates as new points).

**Mock power meter (`imswitch.mock-power-meter`)** — built-in, no pyvisa;
power follows a settable source (for tests: a mock laser's value × a fixed
transmission + noise), so the laser-LUT script runs end-to-end in CI.

**Procedures** as example scripts under
`_data/user_defaults/scripts/calibration/`:

1. `laser_power_lut.py` — port of the 775 AOM script: `api.imcontrol.setLaserValue`
   over a linspace, settle, read `power`, write the LUT. Fixes to make on the
   way (all found in the current script):
   - **Unit label.** The header says `Power Measured [W]` but the column is
     mW (`read_power() * 1E3`). The LUT states its units explicitly per
     column, in SI (W), and records the laser's own setting unit
     (`valueUnits` — for an AOM that may be V or %, not mW).
   - **Zero in the dark, and "dark" is confirmed, not inferred.** The script
     zeroes while the laser may still be at its previous value, which bakes
     that light into the offset of every later reading. A minimum *value* is
     not dark: the laser contract keeps value and enabled state apart
     (`LaserManager.setValue` / `setEnabled`), an AOM/AOTF line leaks through
     its finite extinction at drive 0, and other lasers may share the path.
     The procedure (a) records the laser's value **and** enabled state,
     (b) disables emission (`setLaserActive(False)`), (c) runs `zero` with
     `confirm_dark=True` — which a script may only pass after the user
     confirmed the beam is blocked (an ImScripting prompt; the example script
     asks), then (d) re-enables for the sweep.
   - **Restore value and enabled state** after the run, also on error or
     Stop — the current script leaves the laser at `max_power`. Order: set
     the value while still disabled, then restore the enabled state.
     ImScripting has no laser *getters* today (`LaserController` exports only
     `getLaserNames`, `setLaserActive`, `setLaserValue`), so P-1 adds
     `getLaserValue` and `getLaserActive`.
   - **A sweep starting at 0 switches the laser off.**
     `LaserController._setLaserValue` disables a non-binary laser when it is
     set to ≤ 0, and setting a positive value afterwards does not re-enable it.
     The current script sweeps from 0, so unless the 775 line ignores its
     enable state, every point after the first may have been measured with
     emission off — **check the existing 775 LUT** for a flat curve. The
     procedure calls `setLaserActive(True)` after each value change (or
     sweeps from the first positive value and measures 0 separately).
   - **Partial runs.** An exception mid-loop leaves fewer readings than
     settings and `np.vstack` then fails, losing the data. Write rows as they
     are measured (or pair setting and reading per row), so a partial LUT
     survives, marked incomplete.
   - **No tkinter.** `asksaveasfilename` opens a Tk dialog from the script
     thread beside Qt, and returns `''` (not `None`) on cancel. Use the
     calibration folder default (§6) and an ImScripting-side file prompt if
     one is wanted.
   - **Wavelength from the laser**, not `int(laser_name)` — that only works
     while lasers are named after their wavelength.
   - Average several reads per step (`read_mean`) instead of one.

   LUT file: `#`-comment header block (laser name, setting unit, wavelength,
   plane label — BFP/sample/fibre, meter identity + sensor, zeroed yes/no,
   settle time, reads per step, date, ImSwitch version, free-text notes —
   the script's `notes` footer moves here), then whitespace-separated
   columns `setting power_W power_std_W`. **The first two columns must stay
   setting / power**, because the existing `calibCsvPath` consumer (§9) reads
   it with `np.loadtxt` and uses columns 0 and 1; `#` lines and a third
   column are compatible with it.
2. `waveplate_polarisation_map.py` — step two rotators over a grid, read the
   PAX at each position, write `traj_idx`, angle₁, angle₂, azimuth,
   ellipticity, DOP, power, s1, s2, s3 (one `traj_idx` per sweep line, so the
   Poincaré view can draw each line as a trajectory).

(Adding files under `user_defaults` requires regenerating the hash history
with `tools/update_user_defaults_history.py`.)

**Relation to the WFS `CalibrationWorkflow`** (`model/workflows/calibration.py`):
that is a QWP/HWP sweep measured with a polarisation *camera*. The PAX map is
the same sweep with a different sensor. This plan does not touch it; whether
to merge the two is Q-5.

## 9. Existing LUT support, and what is left for later

Round 0 missed that part of this already exists:

- **`calibCsvPath`** (laser `managerProperties`) — `NidaqLaserManager` and
  `AAAOTFLaserManager` load a two-column file (`np.loadtxt`: raw setting,
  measured power), subtract the minimum, normalise to 0–100 % and drive the
  laser through the inverse (`create_lut_from_calib`, `interp1d`); the UI
  value becomes a % setpoint (`LaserManager.usesCalibrationLookup`). It
  discards absolute power — only the curve's *shape* is used.
- **`utility_scripts/aa_aotf_calibration.py`** — a standalone Qt utility
  (ImSwitch closed) with its **own `PM100D` class** (same `READ?` /
  `SENSE:CORR:WAV` / `SENSE:ZERO:INIT` commands), frequency and power sweeps
  for AA AOTFs, an atomic LUT writer, and write-back of `calibCsvPath` etc. to
  the setup with a `.bak`. It already does the restore-on-failure right
  (switches the channel off after every sweep, on failure and on close).

Consequences for this plan:

- The LUT the procedure writes is a `calibCsvPath` file (column rule in §8),
  so a lab can point a laser at it with no new consumer code.
- There should be **one PM100 driver**: P-2 builds `thorlabs.pm100` from the
  helpers and the utility's class, and the utility then imports it instead
  of carrying its own (Q-8). The utility stays standalone — it needs the serial
  ports ImSwitch would hold — so it uses the driver directly, not the hub.

Later, its own plan: absolute power — keep the measured W alongside the %
mapping so the laser widget can show "≈ x mW at BFP" and accept a target
power (wavelength, staleness warning, interaction with the illumination
channel model).

## 10. Phases

Each phase ends green on the unit lane and with the docs updated.

- **P-0 Framework.** Contribution dataclass + parser + registry + CLI listing;
  `pluginapi.calibration` contract; `CalibrationHub` with lock, poller, ring
  buffer, one sample path with freshness rules, declared actions, CSV
  recorder with `source` column and annotations; Calibration menu + Manage
  tools…; generic window; mock power meter (configurable to repeat samples,
  so every freshness rule is testable). Tests: manifest parsing (valid,
  missing fields, unknown category, collision), `requires` check without
  import, hub lifecycle (connect / read / exclusive / action / close), poller
  skips while locked, repeats neither published nor recorded nor averaged,
  script reads during `exclusive()` reach the window and the recorder, an
  action cannot interleave with a locked sweep, `requires_dark` refuses
  without confirmation, **shutdown**: pollers joined within the bound, drivers
  NOT closed after a failed scripting drain, a lock held past the bound is
  skipped and logged; window under pytest-qt offscreen against the mock;
  menu greying when a requirement is missing.
- **P-1 Scripting API.** `openCalibrationTool` / `listCalibrationTools`,
  handle with `read_mean(n, timeout_s)` (distinct samples, `TimeoutError` on
  shortfall), `action`, `annotate`, `exclusive`; laser getters
  `getLaserValue` / `getLaserActive` on `LaserController`; mock-laser LUT
  script running end-to-end in a test, including value + enabled-state
  restore on an injected failure.
- **P-2 PM100.** Driver from Lenny's helpers and the AOTF utility's class
  (one driver; the utility then imports it, Q-8); VISA Scan; wavelength
  setting; `zero` action with completion wait.
  Unit tests against a fake VISA resource (scripted SCPI replies). Rig check:
  reading matches the Thorlabs Optical Power Monitor app.
- **P-3 PAX1000.** Driver from Lenny's helpers; multi-channel plot; Poincaré
  view optional. Rig check against the Thorlabs PAX software.
- **P-4 Procedures + docs.** The two example scripts; `docs/` page
  "Calibration tools" (user guide + how to write one, linked from
  `docs/devices/plugins.rst`); plugin template gains a calibration-tool
  example.

## 11. Risks

- **VISA back-ends differ.** On Windows the PM100/PAX need NI-VISA or the
  Thorlabs VISA runtime; `pyvisa-py` needs `pyusb` + libusb for USBTMC, and on
  macOS often needs permissions. The window must show the back-end in use and
  turn "no resources found" into an actionable hint, not an empty list.
- **Another program holds the instrument.** The Thorlabs app keeps the USB
  session open; connect must report that clearly.
- **Script thread vs. poller vs. shutdown.** Covered by D-7 and the shutdown
  sequence in §6, including the failed-drain case; a hung VISA read must not
  hang shutdown (VISA timeout set on connect, every join and lock acquire
  bounded).

## 12. Open questions for review

- **Q-1 Where do the PM100 / PAX tools live?** (a) in-tree, registered as
  built-ins, pyvisa via the existing `hardware` extra — works with a plain
  `pip install imswitch2[hardware]`; or (b) in `imswitch-device-thorlabs`,
  matching the direction of moving vendor code out of tree, but that package
  is not on PyPI yet, so users would install from source. Proposal: (a) now,
  with the contract identical so extraction later is a move, not a rewrite.
- **Q-2 Multiple instances of one tool** (two PM100s at once)? The hub keys
  sessions by connection identity, so it is possible; does the menu need a
  "new connection" entry per tool, or is one-per-tool enough for P-0?
- **Q-3 ImProcess too?** Proposal: ImControl only — the instruments are
  hardware and the procedures need ImControl's devices.
- **Q-4 Should a calibration run be recorded in recording metadata** (e.g.
  "BFP power at 488 nm measured 2026-10-06: …")? Proposal: not in this plan;
  the LUT plan (§9) is where it becomes meaningful.
- **Q-5 WFS facade.** Give `MicroscopeFacade` a `calibration` slot, and/or
  let `CalibrationWorkflow` take a PAX instead of the polarisation camera?
- **Q-6 Remote API.** `@APIExport` also exposes methods to the remote server.
  Is a remote "read the power meter" wanted, or should the calibration
  methods be excluded from it?
- **Q-7 Name.** Menu "Calibration", contribution `calibration_tools`, class
  `CalibrationTool` — or "Instruments", since a power meter is useful for
  plain checks too, not only calibration?
- **Q-8 One PM100 driver.** `utility_scripts/aa_aotf_calibration.py` carries
  its own `PM100D`. Proposal: it imports the `thorlabs.pm100` driver after
  P-2. Should the utility's power sweep itself also become a calibration
  procedure inside ImSwitch, or stay standalone (it needs the AOTF serial
  ports, which a running ImSwitch holds)?
