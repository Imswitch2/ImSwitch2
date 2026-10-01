# Scanner Calibration Widget

A new `imcontrol` widget for **driving scanner DACs by hand** and, later, for
**measuring the calibration constants** that the scan path currently takes on
faith from the setup file.

Status: Proposed — **revision 3**, ready for a second review round. **P-0 is the
agreed first deliverable** (manual per-axis voltage entry). P-1..P-3 are
roadmap.

Revision 2 changed the safety model after review: session-scoped restore
snapshot, "Zero all" removed, a real actuator reservation instead of only
dodging scans, `offsetVolt` withdrawn. Revision 3 is a self-audit of that
revision against the code, and found three defects **in revision 2's own new
material** — the focus-lock integration would have introduced a bug, and the
reservation's stated coverage was wrong in both directions. §10 logs
everything; §11 lists the decisions this review round should rule on.

Cut and recorded in §8 rather than deleted: parked point-detector reading,
field-nonlinearity correction, and `offsetVolt`.

---

## 1. Why

Every analog scanning axis in ImSwitch is described by a few numbers in the
setup JSON's `managerProperties`:

```json
"ND-GalvoX": {
  "managerName": "NidaqPositionerManager",
  "managerProperties": {"conversionFactor": 17.44, "minVolt": -10, "maxVolt": 10,
                        "vel_max": 0.1, "acc_max": 0.0001},
  "axes": ["X"], "forScanning": true
}
```

`conversionFactor` (µm per volt) is the single number that turns every
requested µm into a DAC voltage — in `NidaqPositionerManager.setPosition`, in
`TriggerScopePositionerManager.setPosition`, in `GalvoScanDesigner`, in
`BetaScanDesigner`, and in `TriggerScopeRasterController`. If it is wrong,
every scan is wrong by the same factor and nothing in the software notices.

Today there is **no way in the GUI to send a chosen voltage to a scanner**.
The Positioner widget speaks µm, which means it speaks through the very
constant you are trying to check. Aligning a galvo, finding its mechanical
centre, or checking that ±5 V really sweeps the field you think it does all
require a voltage box.

That is P-0, and it is useful on its own. P-1..P-3 then close the loop:
measure where the beam actually went, fit the gain, and write it back.

---

## 2. Where it plugs in

Naming follows the existing `{Key}Widget` / `{Key}Controller` convention, with
widget key **`ScannerCalib`**.

| File | Change |
|---|---|
| `imswitch/imcontrol/view/widgets/ScannerCalibWidget.py` | **new** — `ScannerCalibWidget(Widget)` |
| `imswitch/imcontrol/controller/controllers/ScannerCalibController.py` | **new** — `ScannerCalibController(ImConWidgetController, StatefulComponentMixin)` |
| `imswitch/imcontrol/model/managers/positioners/_analog_scanner.py` | **new** — the voltage capability (§3) |
| `imswitch/imcontrol/controller/CommunicationChannel.py` | actuator reservation registry + signals (§4); positioner-moved notification (§3) |
| `imswitch/imcontrol/controller/controllers/FocusLockController.py` | generalize scan-suspension bookkeeping to keyed owners, then consume the reservation via its existing conflict predicate (§4) |
| `imswitch/imcontrol/controller/controllers/PositionerController.py` | refuse moves to a reserved actuator in `move`/`setPos`; sync display on external moves (§3, §4) |
| `imswitch/imcontrol/controller/controllers/WellPlateController.py` | honour the reservation — the one remaining unguarded direct writer (§4) |
| `imswitch/imcontrol/controller/basecontrollers.py` | scan start refuses while a manual reservation is held (§4) |
| `imswitch/imcontrol/view/widgets/__init__.py` | add to `_WIDGET_MODULES` |
| `imswitch/imcontrol/controller/controllers/__init__.py` | add to `_CONTROLLER_MODULES` |
| `imswitch/imcontrol/view/ImConMainView.py` | add to `_DOCK_DISPLAY_NAMES` and `_DEFAULT_RIGHT_DOCK_INFOS` (`yPosition=2`, next to `Scan`) |
| `imswitch/imcontrol/model/managers/positioners/{Nidaq,TriggerScope}PositionerManager.py` | mix in the capability |
| `imswitch/imcontrol/view/guitools/ViewSetupInfo.py` | document `ScannerCalib` in the `availableWidgets` docstring |
| `docs/setupinfo-reference.rst` | add `ScannerCalib` to the `availableWidgets` list (§ "Advanced") |
| `docs/gui.rst`, `docs/changelog.rst` | user-facing docs |

Both the widget and the controller are resolved lazily through the existing
`__getattr__` maps, so nothing is imported on rigs that do not enable the
widget. `ImConMainView._addDocks` and `ImConMainController` construct it from
the key automatically — no special-casing (unlike `Scan`, which has to append
`scanWidgetType`).

**Deliberately not** a `SuperScanWidget` subclass. That base carries
`componentName = 'Scan'`, the scan-run lifecycle, and the `'Scan'` state
persistence key. This widget never runs a scan in P-0 and must not compete for
that identity. It subclasses plain `Widget` from `basewidgets.py`.

---

## 3. Model layer: an explicit voltage capability

### The problem with just calling `setAnalog`

`NidaqManager.setAnalog(target, voltage, min_val, max_val)` and
`TriggerScopeManager.setAnalog(target, voltage)` both exist and are reachable
from a controller. Calling them directly is the wrong move:

- `TriggerScopePositionerManager` tracks `_position` from the **clamped**
  voltage it commanded and persists it to `triggerscope_positions.json` so a
  restart re-adopts the hardware's real state. A raw `setAnalog` behind its
  back desyncs both the tracked position and the file, and the next restart
  then silently believes a stale voltage.
- `NidaqPositionerManager` likewise keeps `_position` and is what the
  Positioner widget and `ScanControllerBase`'s pre-scan centring read.

So the widget must move the positioner *through its manager*, in volts.

### `AnalogScannerMixin`

New module `imswitch/imcontrol/model/managers/positioners/_analog_scanner.py`:

```python
class AnalogScannerMixin:
    """Voltage-domain access for positioners whose position *is* a DAC voltage.

    Presence of this mixin is the capability check: a positioner that has it can
    be driven in volts and calibrated; one that has not (a serial stage, a
    piezo behind RS232) cannot, and the calibration widget must not offer it.
    """

    @property
    def conversionFactor(self) -> float: ...      # µm per volt
    @property
    def voltageRange(self) -> tuple[float, float]:  # (minVolt, maxVolt)
    @property
    def safeVoltage(self) -> float:
        """`managerProperties['safeVoltage']` if configured, else the midpoint
        of `voltageRange`. Never blindly 0 — see §5."""

    def getVoltage(self) -> float:
        """Last commanded voltage, derived from the tracked position."""
        return self._position[self.axes[0]] / self.conversionFactor

    def setVoltage(self, volt: float) -> float:
        """Command a voltage; return the value actually commanded after clamping."""
        clamped = min(max(float(volt), self._minVolt), self._maxVolt)
        self.setPosition(clamped * self.conversionFactor, self.axes[0])
        return clamped
```

Routing through `setPosition` is what keeps everything honest: TriggerScope
still clamps and persists, Nidaq still updates `_position`.

Three details worth stating:

- **Clamping.** `TriggerScopePositionerManager.setPosition` already clamps and
  warns; `NidaqPositionerManager.setPosition` does **not** — it hands the value
  straight to `nidaqmx` with `min_val`/`max_val` bounds, which errors rather
  than clamps. `setVoltage` clamps for both. Widening that to `setPosition`
  itself is a behaviour change to the µm path and is deliberately out of scope.
- **`getVoltage` is a round-trip, not a readback.** Neither board has position
  feedback. The value is "what we last commanded", which is exactly what the
  managers already claim about `position`. §5 relies on this to *detect* that
  someone else moved an axis, not to correct for it.
- **No `offsetVolt`.** Revision 1 proposed one. Withdrawn — see §8.

### The Positioner widget does not update itself

Revision 1 claimed the Positioner widget's µm readout would follow a voltage
entry "for free". It will not. The manager's `_position` changes, but the
display is written by `PositionerController.updatePosition(name, axis)`, which
only runs when that controller is the one initiating the move.

Add `CommunicationChannel.sigPositionerPositionChanged = Signal(str, str)`
(positioner name, axis). `ScannerCalibController` emits it after every write;
`PositionerController` connects it to `updatePosition`, which refreshes both the
widget and the shared attribute.

**Why not just write the shared attribute**, which already exists and already
carries positions? Because it is not a notification channel — it is a *command*
channel. `PositionerController.attrChanged` reacts to a `_positionAttr` key by
calling `setPositioner(name, axis, value)` → `setPos` →
`manager.setPosition(...)`. A cross-controller shared-attr write would therefore
round-trip our voltage through µm and **re-command the DAC**, re-quantizing the
value we just set. The `settingAttr` re-entrancy guard does not help: it is set
only around `PositionerController`'s own writes, so it is False for ours.

`updatePosition` internally calls `setSharedAttr`, but that path is safe — it
runs inside the `settingAttr` guard, so the resulting `attrChanged` early-returns
rather than re-commanding.

---

## 4. Actuator reservation

Scan interlocks alone are not exclusive ownership. The Positioner widget,
`APIExport` position setters, the focus lock, and aliased access paths can all
drive the same physical actuator while the calibration widget holds it — and
the analog/serial piezo pair on `example_sted` is exactly the configuration
where that bites.

The codebase already has the right *shape* for this. `FocusLockController`
suspends on `sigScanStarting`, resumes early on `sigScanActuatorsResolved` if
`scanTouchesFocusActuator(actuators)` says the scan cannot reach its axis, and
resumes on `sigScanEnded`. That predicate is conservative by construction and
already handles the `physicalActuator` id match and the same-axis fallback.

### The mechanism

On `CommunicationChannel`, a single-holder reservation registry:

```python
sigActuatorsReserved = Signal(object)   # list[str] of positioner names
sigActuatorsReleased = Signal()

def reserveActuators(self, positionerNames, owner) -> object   # token, or raises if held
def releaseActuators(self, token) -> None
def reservedActuators(self) -> list[str]
```

Participants in P-0:

| Participant | Behaviour |
|---|---|
| `ScannerCalibController` | Holds the reservation for the whole manual session (§5). Every write asserts it still holds it. |
| `FocusLockController` | Suspend on a reservation that its `scanTouchesFocusActuator` predicate says can reach its axis — but **as a separate suspension owner**, see below. |
| `PositionerController` | Refuse a move whose positioner is in `reservedActuators()`, guarded in `move` and `setPos`, with a message naming the holder. |
| `WellPlateController` | Same guard — it calls `move`/`setPosition` on managers directly and is otherwise unguarded. |
| `SuperScanController._beginScanRun` | Refuse to start a scan while a manual reservation is held. This is the direction that makes it *mutual* rather than one-sided. |

### The focus lock needs keyed suspension owners, not the scan counter

Revision 2 said to "connect `sigActuatorsReserved` to the same suspend path it
uses for a scan". That would introduce a bug. The lock's bookkeeping is a single
counter plus a single companion flag:

- `scanUnlockFocus` (`sigScanStarting`) increments `_scanSuspendDepth`;
  `scanLockFocus` (`sigScanEnded`) decrements it.
- `scanActuatorsResolved` releases early **only when `_scanSuspendDepth == 1`**
  and `_scanOwnsFocusActuator` — deliberately, because one flag cannot say "every
  concurrent scan is harmless".

Feed a manual reservation into that same counter and the sources become
indistinguishable: a scan's harmless-resolve sees depth 2 and declines to
release (mildly wrong), or with different ordering releases a suspension the
*reservation* owns (actively wrong — the lock re-engages while the calibration
widget is driving the axis). `scanUnlockFocus` also early-returns on
`not scanBlockEnabled()`, a scan-specific user opt-out that has no business
governing manual control.

So P-0a generalizes the bookkeeping: replace `_scanSuspendDepth` /
`_scanOwnsFocusActuator` with a small map of suspension owners
(`'scan'`, `'reservation'`), each with its own conflict flag. Resume happens
when the map empties; each owner releases only its own entry; `scanBlockEnabled()`
gates only the `'scan'` owner. Existing scan behaviour is preserved exactly —
the current pair is the one-owner case — and the depth==1 reasoning becomes
per-owner instead of global.

This is a focused refactor of another controller's internals, which is why
§9 gives it its own phase and §11 asks for a ruling on its scope.

### Scan-side guard, corrected

`isScanRunning()` returns `self._activeScanSource is not None` and is not a
complete run-level guard — a repeat scan's gap between frames can read false
while the run is still in flight. `ScannerCalibController` therefore also tracks
the interval locally: `sigScanStarting` sets `_scanInFlight`, `sigScanEnded`
clears it, and a write is refused if **either** is true.

### Coverage, stated accurately

Revision 2 claimed `APIExport` position setters would remain unguarded. That was
wrong: `movePositioner` and `setPositioner` delegate to `move`/`setPos`, so
guarding those two methods covers the Positioner widget **and** its whole public
API in one place. Revision 2 was also wrong in the other direction — it implied
the rest was covered. Enumerating every direct writer:

| Writer | Covered by |
|---|---|
| `PositionerController.move` / `setPos` (and the `APIExport`s that delegate to them) | the guard itself |
| `ScanControllerBase` / `PointScan` / `MoNaLISA` / `Advanced` pre-scan centring, `basecontrollers.py` scan parking | the scan-start refusal — none of them runs outside a scan run |
| `FocusLockController` corrections | yields on the reservation |
| `WellPlateController` | **needs the guard added** — the one genuinely unguarded writer |
| `PositionerController.closeEvent` → `execOnAll(resetOnClose)` | not guarded, and deliberately so: it is shutdown policy and §5 already stands down for it |

That is the complete set in `imcontrol/controller`. The honest residual is
therefore narrow — plugin or workflow code reaching a manager directly — rather
than the broad hole revision 2 described.

---

## 5. P-0 — Manual voltage control (the first deliverable)

### What the user gets

A dock titled **Scanner Calibration**. Manual control is a **session**: nothing
can be written until it is begun, which is what bounds both the reservation and
the restore snapshot.

```
 [ Begin manual control ]        session inactive — reservation not held

 Axis          Voltage (V)              Position (µm)   Range
 ND-GalvoX   [  -1.250 ] ◄─────●──────►    -21.80      -10 .. +10 V
 ND-GalvoY   [   0.000 ] ◄────●───────►      0.00      -10 .. +10 V
 ND-PiezoZ   [   2.500 ] ◄──●─────────►      2.50        0 .. +10 V

 [ Park all at safe voltage ]  [ Restore session values ]   ☐ Live
```

- **Voltage spinbox** — 3–4 decimals, `setRange` from the manager's
  `voltageRange`, so a `minVolt=0` axis simply cannot be sent negative.
  Committing (Enter / focus-out, or every change when *Live* is ticked) calls
  `setVoltage`.
- **Slider** — coarse drag across the axis' full range. Commands hardware on
  release unless *Live* is ticked.
- **Position (µm)** — read-only, `volt × conversionFactor`. This is the pair of
  numbers you stare at while checking a calibration, so they belong side by
  side.

### Session lifecycle — snapshot, restore, and shutdown

Revision 1 snapshotted at widget construction and restored on close. Since the
widget is constructed at application startup, that snapshot goes stale within
minutes and restoring it hours later would undo legitimate positioning and
completed scans. Replaced by:

1. **Begin session** — take the reservation (§4), then snapshot the voltages of
   the offered axes *at that moment*.
2. **Per write** — record the value commanded for that axis. Only axes this
   session actually wrote are restore candidates.
3. **Restore session values** — for each written axis, compare the manager's
   current tracked voltage against what we last commanded. If they differ,
   something else moved it: report that axis and skip it rather than blindly
   overwriting. Restore the rest.
4. **End session** — offer restore, then release the reservation.

**Application shutdown is not session end.** `PositionerController.closeEvent`
already commands `setPosition(0, axis)` on every `resetOnClose` positioner. A
restore racing that would fight it and could leave either value in place
depending on controller teardown order. So on app close the controller releases
its reservation, aborts any in-flight automated routine first, and **does not
restore** — `resetOnClose` is the configured shutdown policy and wins.

### "Zero all" is gone

Revision 1 proposed a bulk zero. On a unipolar axis 0 V is not the centre, it is
an *endpoint*: `ND-PiezoZ` on `example_sted` is `minVolt: 0, maxVolt: 10`, as
are all three MoNaLISA axes. A bulk zero would slam those to the bottom of
their travel.

Replaced by **Park all at safe voltage**, using `safeVoltage` from
`managerProperties` when configured and the midpoint of `voltageRange`
otherwise. Commanding electrical zero stays available as an explicit per-axis
action, and carries a warning when 0 V is an endpoint of that axis' range.

### Which axes appear

Positioners that are (a) `forScanning`, and (b) whose manager is an
`AnalogScannerMixin`. A rig can alias one physical actuator twice —
`example_sted` addresses one piezo as the analog `ND-PiezoZ` and the serial
`PiezoZ` — so where `physicalActuator` (or a shared axis name) marks two
offered entries as one device, only the analog one is listed and the widget
says why.

If no axis qualifies, the widget shows one explanatory line naming the
supported manager types rather than an empty grid.

### Persistence

`StatefulComponentMixin` provides the state *contract*, not registration: the
controller must call `getWidgetStatePersistence().register('ScannerCalib', self)`
explicitly, as `PositionerController` and `ScanControllerBase` do.

Persist only UI intent — the *Live* checkbox, slider granularity. **Never the
voltages, and never the session state.** Restoring a voltage from saved widget
state would command hardware on startup.

Revision 1 proposed declaring config-derived fields in `configOwnedParameters`.
Withdrawn: that is a `DetectorManager` property consumed by `SettingsController`
for detector parameters, not a generic `StatefulComponentMixin` facility. The
equivalent rule here is simply that `conversionFactor`, `voltageRange` and
`safeVoltage` are read from `setupInfo` on every use and never enter the state
payload.

### No auto-apply at startup

The widget reads current voltages and displays them; it never commands anything
until a session is begun and the user acts. This matters for TriggerScope, whose
whole restore design is built on *not* moving.

### Tests for P-0

- `test_analog_scanner_capability.py` — clamp at both ends for both manager
  types; `setVoltage` → `setPosition` → `setAnalog` carries the right voltage;
  TriggerScope's tracked position and persisted JSON stay in sync;
  `getVoltage`/`setVoltage` round-trip; `safeVoltage` falls back to the midpoint
  and is *not* 0 on a unipolar axis.
- `test_actuator_reservation.py` — a second reserver is refused; focus lock
  suspends on a reservation that touches its actuator and does **not** on one
  that provably cannot; **a scan's `sigScanActuatorsResolved` does not release a
  reservation-owned suspension, and vice versa** (the revision-2 bug); a scan
  and a reservation suspending concurrently resume only when both release;
  `scanBlockEnabled()` off still lets a reservation suspend; `move`, `setPos`
  and the delegating `APIExport`s are all refused for a reserved actuator;
  `WellPlateController` likewise; `_beginScanRun` refuses while held; release
  restores everything.
- `test_scanner_calib_controller.py` — only capable `forScanning` axes are
  offered, aliased duplicates suppressed; no write is possible before session
  start; a write during `sigScanStarting`→`sigScanEnded` is refused *including
  in a repeat gap where `isScanRunning()` reads false*; restore skips an axis
  moved by someone else; app close releases without restoring; a rig with no
  capable axis constructs without raising.
- Widget smoke test under `QT_QPA_PLATFORM=offscreen`.

P-0 needs no hardware to test — mock setups plus a stub manager cover all of
it. It does want a rig check that the voltage on a DMM matches the box.

---

## 6. Roadmap — the calibration routines

Each phase adds a section to the same widget. Scope: **camera-only**
measurement, both DAC backends, results written to the setup file after an
explicit confirmation.

### Measurement: the camera probe (P-1)

Take a `LeasePurpose` lease on the chosen camera (add
`CALIBRATION = 'calibration'` so the lease log is readable), then per point:

1. Command the voltage, wait a configured **settle** interval.
2. **Flush** the buffer — a non-streaming lease only *arms* the camera; it does
   not guarantee the next frame post-dates the move. Discard frames until one
   is provably fresh, then collect N.
3. Average, background-subtract, crop an ROI around the last known spot, fit
   `Gaussian2D` from `imswitch/imcommon/algorithms/bead_fits.py`.
   `FitResult.center_px` is the measurement, `r_squared` the quality gate.
4. Every step under a **timeout**; the lease released in `finally` on every
   path including exception and abort.

Pixels → µm needs two corrections, not one:

- `DetectorManager.pixelSizeUm` is the **unbinned** sample-plane size;
  `setBinning` changes the delivered frame but never that parameter. Multiply by
  binning, exactly as `TilingController._detectorPixelSizeUm` already does and
  documents.
- The precondition is that `cameraPixelSizeUm` was **explicitly configured**,
  checked by looking for the key in the detector's `managerProperties` — not by
  testing the value. (Revision 1 said "refuse at the 1.0 default"; the actual
  default is 0.15 µm, and 1.0 is a legitimate configured value. Testing a
  number was the wrong check regardless.)

This is the only probe. Routines 1 and 2 therefore need a camera that sees the
scanned field; §8 covers what that costs.

### Routine 1 — gain only (P-1)

Sweep one axis over K voltages spanning a safe fraction of its range (computed
from the actual `voltageRange` — `example_monalisa` and `ND-PiezoZ` are
`minVolt: 0` and cannot sweep symmetrically about zero). Measure the spot at
each point, least-squares fit `pos_µm = gain·V + c`.

`gain` is the new `conversionFactor`. **The intercept `c` is discarded.** The
camera's coordinate origin is arbitrary — `c` lumps together sensor origin, ROI,
optical alignment and any true scanner DC offset, and with no external zero
reference those are not separable. Only the gain is identifiable from this
measurement, so only the gain is reported. See §8 for what admitting an offset
would require.

Report R² and residual RMS; warn when the residual exceeds a threshold. With
field nonlinearity out of scope (§8) that warning is the *whole* answer on a
nonlinear field: it says a single factor describes your scanner poorly, without
offering to model the rest.

### Routine 2 — 2D affine galvo↔camera (P-2), diagnostic first

Command a grid of (Vx, Vy), measure each centroid, least-squares a 2×3 affine
from commanded volts to camera pixels, decompose into scale_x, scale_y,
rotation, shear and handedness. This catches a **rotated, swapped or mirrored
galvo pair** — a common miswiring that per-axis fits cannot see, because each
axis independently looks fine.

**Not** stored as a `DetectorTransform`. That schema means detector-local pixels
→ alignment-grid pixels, and its setup home is `tiling.detectorTransforms`,
which is about detector-to-detector registration. Volts → camera pixels is a
different relation between different spaces; writing it there would corrupt an
unrelated manifest.

P-2 therefore ships **diagnostic-only**: display and plot the decomposition, do
not persist it. Persistence waits on a dedicated scanner↔camera record that
names its two axes, coordinate order, units, matrix direction, and the ROI and
binning it was measured under. Defining that record is part of the P-2 work; the
write-back is only enabled once it is agreed.

### Routine 3 — temporal phase delay (P-3), feasibility first

`ScanWidgetAdvanced` exposes `phaseDelay` and `frameDelay` in samples and the
designers consume them, so the value has somewhere to go. Getting it is the
problem.

Revision 1 claimed this routine could cross-correlate forward and reverse lines
from the ordinary reconstructed image. It cannot. `APDManager`'s reader takes
`line_samples = data_cnts[:self._samples_d_scanstep[1]]` — the forward line only,
with the flyback remainder of the period truncated away — and applies
`_phase_delay` while reading. There are no paired forward/reverse traces to
correlate, and the quantity being solved for is already baked into the read.

P-3 therefore **starts with a feasibility phase**: define how a bidirectional
waveform is generated and how raw directional samples are surfaced without
disturbing the normal image path. Only then is there a routine to design. This
is not a small layer on top of an existing scan result.

### Write-back (from P-1 onward)

Revision 1 mutated `self._setupInfo` in place and told the user to restart. That
knowingly creates a split state — the signal designers read `setupInfo` live on
every `make_signal`, so scans would immediately use the new factor while the
positioner managers keep their construction-time cached one for manual moves.
A message does not stop a scan from running in that state.

Instead: **deep-copy `SetupInfo`, apply the change to the copy, and pass the
copy to `configfiletools.saveSetupInfo`.** The live object is untouched, so the
running session stays wholly on the old calibration and the new one takes effect
at the next start — one consistent state at all times. `saveSetupInfo`
serializes whatever it is handed, so this needs no change to it.

The confirm dialog lists the exact key changes (`old → new`, with Δ%) and
nothing is written without it.

The alternative — one atomic live recalibration covering managers, tracked
positions, designers and TriggerScope controllers — is the better end state and
is deliberately not attempted here.

---

## 7. Risks

| Risk | Mitigation |
|---|---|
| A raw voltage write desyncs TriggerScope's persisted position | `setVoltage` routes through `setPosition`; never `setAnalog` directly |
| Another writer drives an actuator mid-session | Reservation (§4) enforced for focus lock, Positioner widget and scan start; advisory for un-migrated writers, and the plan says so |
| Restoring a stale snapshot undoes legitimate work | Snapshot at session start, restore only axes this session wrote, skip axes another writer has moved since |
| Restore fights `resetOnClose` at shutdown | App close releases the reservation and does not restore; `resetOnClose` is the configured shutdown policy |
| Bulk "zero" slams a unipolar axis to an endpoint | Bulk action parks at `safeVoltage`/midpoint; per-axis 0 V warns when 0 is an endpoint |
| Beam parked on one spot bleaches or damages the sample | Routines gate illumination per measurement; P-0 changes nothing about illumination and leaves it to the operator |
| Wrong or binning-unaware pixel size ⇒ every µm result wrong by that factor | Require `cameraPixelSizeUm` explicitly configured; multiply by binning as `TilingController` does |
| Stale camera frame read as a post-move measurement | Settle interval, buffer flush to a provably fresh frame, N frames, timeouts, lease released in `finally` |
| A calibration written back that is worse than the old one | Confirm dialog shows old → new and Δ%; R²/residual gates block an obviously bad fit; the live session keeps the old value either way |
| Camera-only measurement leaves point-scan rigs unable to fit their own `conversionFactor` | Accepted consequence of the §8 cut. Those rigs still get P-0; the widget says plainly that routines 1–2 need a camera rather than greying out a button with no explanation |

---

## 8. Deliberately out of scope

Recorded rather than deleted — the findings behind these cost real
investigation and the questions will come back.

### Reading a point detector at a parked position

**Not possible as stated.** `APDManager` produces data only from
`initiateScan`/`startScan`; there is no counter task outside a scan and
`getChunk` returns an empty array until a scan frame lands. "Park the beam and
read the APD" has nothing to read.

The substitute would be a *scan-based* probe: a small raster around each
commanded offset, locating the feature in the reconstructed image. It carries a
chicken-and-egg — the probe scan's µm axes are converted by the very
`conversionFactor` under test — solvable by driving probe scans in **volts** and
treating the result as a multiplicative ratio of measured to commanded.
Workable, but it makes every measurement point a full scan lifecycle.

**What the cut costs:** a rig with no camera in the scanned path —
`example_sted` is exactly this — gets P-0 and cannot fit its own
`conversionFactor`. There, the voltage box plus a stage micrometer is still a
large improvement over having no voltage entry at all; it just is not automated.

Nothing here forecloses it; it re-enters as a second probe behind the same
interface the camera probe uses.

### `offsetVolt`

Revision 1 proposed `position = (volt − offsetVolt) × conversionFactor`, fitted
from routine 1's intercept. Withdrawn for two independent reasons:

1. **Not identifiable.** The intercept is measured against an arbitrary camera
   origin and confounds sensor origin, ROI, optical alignment and scanner
   offset. Admitting an offset requires an external physical zero reference (a
   fiducial, a mechanical stop) and a stated uncertainty. If "camera centre" is
   ever adopted as the zero, it must be named an *optical registration offset*,
   not scanner error.
2. **Not propagatable cheaply.** It changes the inverse conversion everywhere.
   `GalvoScanDesigner`, `BetaScanDesigner`, `TriggerScopeRasterController` and
   the other TriggerScope controllers each divide by `conversionFactor`
   independently, with their own range checks. Manager-only support would make
   manual moves and scans disagree — a worse failure than having no offset.

Either update every voltage producer and its range checks atomically, or leave
it out. This plan leaves it out.

### Field-nonlinearity measurement and correction

Would have densified routine 2's grid, removed the fitted affine, fit residuals
with a low-order 2D polynomial, and rendered a heat map.

The decisive point: **nothing in the current signal path can apply such a map.**
`GalvoScanDesigner` and `BetaScanDesigner` convert µm→V by a single
`conversionFactor` and nothing else. Measuring the residual would produce a good
diagnostic and no way to act on it; acting on it means a designer-side warp that
touches every scan on the rig and deserves its own plan, flag and validation.

Routine 1 still reports residual RMS and R², so a badly nonlinear field is
visible as a poor fit even though it is not modelled.

---

## 9. Order of work

| Phase | Content | Hardware needed |
|---|---|---|
| **P-0a** | `AnalogScannerMixin`; focus-lock keyed suspension owners; actuator reservation (§4) and its consumers; `sigPositionerPositionChanged` | none |
| **P-0b** | Widget, session lifecycle, registration, docs, tests | none to develop; a DMM check to trust |
| P-1 | Camera probe, routine 1 (gain only), results table, copy-on-write write-back | camera + a spot |
| P-2 | Routine 2 (affine), diagnostic-only; define the scanner↔camera record | camera + a spot |
| P-3 | Phase delay — **feasibility phase first** (bidirectional waveform + raw directional samples) | point-scan rig |

P-0a is separated because the reservation touches four existing controllers and
is the part that wants review on its own, independent of any GUI.

---

## 10. Review log

### Revision 1 → 2 (external review)

| # | Finding | Resolution |
|---|---|---|
| 1 | Restore snapshot goes stale; conflicts with `resetOnClose` | §5 — session-scoped snapshot, per-axis write tracking, skip externally-moved axes, no restore at app shutdown |
| 2 | "Zero all" unsafe on unipolar axes | §5 — removed; `safeVoltage`/midpoint park, per-axis zero warns |
| 3 | Scan interlocks are not exclusive ownership | §4 — `physicalActuator`-keyed reservation reusing the focus lock's conflict predicate; scan start refuses while held; local `sigScanStarting`→`sigScanEnded` tracking; residual limits stated |
| 4 | Fitted offset not identifiable | §6 — routine 1 is gain-only, intercept discarded; §8 records what admitting an offset would require |
| 5 | `offsetVolt` not propagated through scan paths | §8 — withdrawn, with the full list of producers that would need atomic update |
| 6 | Write-back creates a live split state | §6 — deep-copy `SetupInfo`, save the copy, live object untouched until restart |
| 7 | Camera contract wrong and incomplete | §6 — check `cameraPixelSizeUm` is *configured* (not its value; real default 0.15); apply binning per `TilingController`; settle/flush/timeout/`finally` specified |
| 8 | `DetectorTransform` is the wrong schema | §6 — P-2 is diagnostic-only; a dedicated scanner↔camera record is defined before any persistence |
| 9 | Routine 3 cannot use the ordinary image | §6 — P-3 starts with a feasibility phase; `line_samples` truncation cited |
| 10 | Positioner widget will not self-update | §3 — new `sigPositionerPositionChanged`, consumed by `PositionerController.updatePosition` |
| 11 | `StatefulComponentMixin` does not auto-register | §5 — explicit `getWidgetStatePersistence().register('ScannerCalib', self)` |
| 12 | `configOwnedParameters` is detector-specific | §5 — removed, replaced with a plain "read from `setupInfo`, never persist" rule |
| 13 | Missing from `setupinfo-reference.rst` | §2 — added to the file table |

### Revision 2 → 3 (self-audit)

Revision 2's own new material was audited against the code the same way. Three
defects, all in the parts revision 2 added rather than in what it inherited:

| # | Defect in revision 2 | Resolution |
|---|---|---|
| 14 | "Connect the reservation to the focus lock's existing scan-suspend path" would have introduced a bug: `_scanSuspendDepth` is one counter with one companion flag, and `scanActuatorsResolved`'s `depth == 1` early-release cannot distinguish a scan from a reservation — so a scan's harmless-resolve could release a reservation-owned suspension, re-engaging the lock while the widget drives the axis. `scanUnlockFocus` also gates on the scan-only `scanBlockEnabled()` opt-out. | §4 — keyed suspension owners, per-owner release, `scanBlockEnabled()` gating only `'scan'`. New test asserts the cross-release does not happen. |
| 15 | Reservation coverage was wrong in **both** directions: it claimed `APIExport` setters were unguarded (they delegate to `move`/`setPos`, so guarding those covers them), and implied everything else was guarded (`WellPlateController` writes to managers directly and was not). | §4 — full enumeration of every direct writer in `imcontrol/controller` with what covers each; `WellPlateController` added to the file table. |
| 16 | §3 proposed a new position-changed signal without saying why the existing shared-attribute channel is not the answer — a reviewer would reasonably propose it. It is in fact *unsafe*: `attrChanged` reacts to a position key by calling `setPos` → `manager.setPosition`, so a cross-controller write would re-command the DAC, and the `settingAttr` guard does not cover foreign writes. | §3 — rationale added, including why `updatePosition`'s own internal `setSharedAttr` is safe. |

---

## 11. Decisions for this review round

Four points where I chose conservatively and a reviewer may reasonably rule
otherwise. None blocks drafting P-0a; all change its shape.

1. **Scope of the focus-lock refactor (§4).** Generalizing `_scanSuspendDepth`
   into keyed owners is the correct fix, but it edits a controller with subtle,
   hard-won suspend/resume semantics. Alternatives: (a) do it as specified;
   (b) give the reservation an entirely separate suspension flag inside
   FocusLock, duplicating a little logic but touching the scan path not at all;
   (c) land P-0 without focus-lock participation and accept that a running lock
   can fight a manual voltage on a shared Z actuator. I recommend (a); (b) is
   the defensible smaller step.
2. **`WellPlateController` (§4).** Adding the guard there is a two-line change
   to a controller otherwise unrelated to this work. In scope, or leave it as a
   documented gap?
3. **P-2 persistence (§6).** I made routine 2 diagnostic-only rather than
   dropping persistence entirely or inventing the record now. The record's
   schema is specified but unwritten. Confirm that split.
4. **Session ergonomics (§5).** Manual control requires an explicit "Begin
   manual control" click before any voltage can be sent. That is what bounds
   the reservation and the restore snapshot, but it is friction on what the
   user asked for as a simple voltage box. Alternative: begin the session
   implicitly on the first edit, with the same bounds. I recommend the explicit
   button for P-0 and revisiting once it has been used on a rig.
