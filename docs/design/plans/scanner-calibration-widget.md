# Scanner Calibration Widget

A new `imcontrol` widget for **driving scanner DACs by hand** and, later, for
**measuring the calibration constants** that the scan path currently takes on
faith from the setup file.

Status: Proposed. **P-0 is the agreed first deliverable** (manual per-axis
voltage entry). P-1..P-3 are the roadmap toward the calibration tool and are
written down here so P-0's structure does not have to be undone later.

Two things discussed and **deliberately cut** from this plan (§6): reading a
point detector at a parked position, and field-nonlinearity correction.

---

## 1. Why

Every analog scanning axis in ImSwitch is described by three numbers in the
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
measure where the beam actually went, fit the constants, and write them back.

---

## 2. Where it plugs in

Naming follows the existing `{Key}Widget` / `{Key}Controller` convention, with
widget key **`ScannerCalib`**.

| File | Change |
|---|---|
| `imswitch/imcontrol/view/widgets/ScannerCalibWidget.py` | **new** — `ScannerCalibWidget(Widget)` |
| `imswitch/imcontrol/controller/controllers/ScannerCalibController.py` | **new** — `ScannerCalibController(ImConWidgetController, StatefulComponentMixin)` |
| `imswitch/imcontrol/model/managers/positioners/_analog_scanner.py` | **new** — the voltage capability (§3) |
| `imswitch/imcontrol/view/widgets/__init__.py` | add to `_WIDGET_MODULES` |
| `imswitch/imcontrol/controller/controllers/__init__.py` | add to `_CONTROLLER_MODULES` |
| `imswitch/imcontrol/view/ImConMainView.py` | add to `_DOCK_DISPLAY_NAMES` and `_DEFAULT_RIGHT_DOCK_INFOS` (`yPosition=2`, next to `Scan`) |
| `imswitch/imcontrol/view/guitools/ViewSetupInfo.py` | document `ScannerCalib` in the `availableWidgets` docstring |
| `imswitch/imcontrol/model/managers/positioners/{Nidaq,TriggerScope}PositionerManager.py` | mix in the capability |
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
still clamps and persists, Nidaq still updates `_position`, and the Positioner
widget's µm readout follows a voltage entry immediately.

Two details worth stating:

- **Clamping.** `TriggerScopePositionerManager.setPosition` already clamps and
  warns; `NidaqPositionerManager.setPosition` does **not** — it hands the value
  straight to `nidaqmx` with `min_val`/`max_val` bounds, which errors rather
  than clamps. `setVoltage` clamps for both. Widening that to `setPosition`
  itself is a behaviour change to the µm path and is deliberately out of scope.
- **`getVoltage` is a round-trip, not a readback.** Neither board has position
  feedback. The value is "what we last commanded", which is exactly what the
  managers already claim about `position`.

Both `NidaqPositionerManager` and `TriggerScopePositionerManager` gain
`AnalogScannerMixin` and expose `conversionFactor` / `voltageRange` from the
manager properties they already parse. No behaviour change to existing callers.

---

## 4. P-0 — Manual voltage control (the first deliverable)

### What the user gets

A dock titled **Scanner Calibration** with one row per drivable analog axis:

```
 Axis          Voltage (V)              Position (µm)   Range
 ND-GalvoX   [  -1.250 ] ◄─────●──────►    -21.80      -10 .. +10 V
 ND-GalvoY   [   0.000 ] ◄────●───────►      0.00      -10 .. +10 V
 ND-PiezoZ   [   2.500 ] ◄──●─────────►      2.50        0 .. +10 V

 [ Zero all ]  [ Restore entry values ]        ☐ Live (apply while dragging)
 Entry values: ND-GalvoX -0.400 V, ND-GalvoY 0.100 V, ND-PiezoZ 2.500 V
```

- **Voltage spinbox** — 3–4 decimals, `setRange` taken from the manager's
  `voltageRange`, so a `minVolt=0` axis simply cannot be sent negative.
  Committing the value (Enter / focus-out, or every change when *Live* is
  ticked) calls `setVoltage`.
- **Slider** — coarse drag across the axis' full range, snapped to the
  spinbox's step. Only commands hardware on release unless *Live* is ticked.
- **Position (µm)** — read-only, `volt × conversionFactor`. This is the pair of
  numbers you stare at while checking a calibration, so they belong side by
  side.
- **Zero all / Restore entry values** — the entry values are snapshotted at
  widget construction, so a session of poking at voltages can always be undone.
  Restore also runs automatically on abort and on close (see interlocks).

### Which axes appear

Positioners that are (a) `forScanning`, and (b) whose manager is an
`AnalogScannerMixin`. Two exclusions matter:

- A rig can alias one physical actuator twice — `example_sted` addresses one
  piezo as the analog `ND-PiezoZ` and the serial `PiezoZ`. Only the analog one
  qualifies, which `PositionerInfo.physicalActuator` already lets us detect if
  we ever need to warn about the pair.
- `forPositioning`-only stages never appear; they have no voltage domain.

If no axis qualifies, the widget shows a single explanatory line rather than an
empty grid, and says which manager types are supported.

### Interlocks (this is the part that must not be skipped)

The widget drives the exact same DAC channels a scan drives. Concurrent writes
are not merely wrong, they can slam a galvo.

1. **Refuse while scanning.** Every commit checks
   `self._commChannel.isScanRunning()` first; if true, the write is dropped and
   the widget shows why.
2. **Yield on `sigScanStarting`.** The whole panel goes disabled and any live
   drag is cancelled — precedent: `FocusLockController._cancelCalibrationForScan`.
   Re-enable on `sigScanEnded`.
3. **Restore on close.** `ImConWidgetController.closeEvent` restores the entry
   voltages, unless the user explicitly cleared the snapshot. A scanner left
   parked at a corner of its range after a GUI restart is a real way to lose an
   alignment.
4. **No auto-apply at startup.** The widget reads the current voltages and
   displays them; it never commands anything until the user acts. This matters
   for TriggerScope, whose whole restore design is built on *not* moving.

### Persistence

`StatefulComponentMixin` with `componentName = 'ScannerCalib'`,
`stateSchemaVersion = 1`. Persist only UI intent — the *Live* checkbox, slider
granularity. **Never the voltages.** Restoring a voltage from saved widget
state would command hardware on startup, and it would shadow the config-owned
`conversionFactor` the same way the `cameraPixelSizeUm` regression did (see
`config-vs-persisted-state-precedence`). Any config-derived field is declared in
`configOwnedParameters`.

### Tests for P-0

- `test_analog_scanner_capability.py` — clamp behaviour at both ends for both
  manager types; `setVoltage` → `setPosition` → `setAnalog` carries the right
  voltage; TriggerScope's tracked position and persisted JSON stay in sync
  after a `setVoltage`; `getVoltage`/`setVoltage` round-trip.
- `test_scanner_calib_controller.py` — only capable `forScanning` axes are
  offered; a commit while `isScanRunning()` is refused and commands nothing;
  `sigScanStarting` disables and cancels; close restores entry voltages; a rig
  with no capable axis constructs without raising.
- Widget smoke test under `QT_QPA_PLATFORM=offscreen`.

P-0 needs no hardware to test — the mock setups plus a stub manager cover all
of it. It does want a rig check that the voltage on a DMM matches the box.

---

## 5. Roadmap — the calibration routines

Everything below builds on P-0's panel; each phase adds a section to the same
widget. Scope: three routines, **camera-only** measurement, both DAC backends,
results written back to the setup JSON after an explicit confirmation.

### Measurement: the camera probe (P-1)

Take a `LeasePurpose` lease on the chosen camera (add
`CALIBRATION = 'calibration'` to `LeasePurpose` so the lease log is readable),
average N frames, subtract a background, crop an ROI around the last known spot,
and fit `Gaussian2D` from `imswitch/imcommon/algorithms/bead_fits.py`.
`FitResult.center_px` is the measurement, `r_squared` is the quality gate.
Pixels → µm via `DetectorManager.pixelSizeUm`, which honours
`cameraPixelSizeUm`.

> The whole measurement chain is only as good as `cameraPixelSizeUm`. The widget
> displays it prominently and refuses to run if it is still the 1.0 default.

This is the **only** probe. Routines 1 and 2 therefore need a camera that sees
the scanned field; see §6 for what that costs on point-scanning rigs.

### Routine 1 — scale + offset (P-1)

Sweep one axis over K voltages spanning a safe fraction of its range (computed
from the actual `voltageRange`, not ±X — `example_monalisa` and `ND-PiezoZ` are
`minVolt: 0` and cannot sweep symmetrically). Measure the spot at each point,
least-squares fit `pos_µm = gain·V + offset`.

- `gain` is the new `conversionFactor`.
- `offset` is a real DC error that a scale-only fit leaves on the table. To make
  it actionable rather than decorative, `AnalogScannerMixin` gains an optional
  `offsetVolt` property (`position = (volt − offsetVolt) × conversionFactor`),
  honoured by both managers and written back alongside the factor.
- Report R² and residual RMS; warn when the residual exceeds a threshold. With
  field nonlinearity out of scope (§6) that warning is the *whole* answer on a
  nonlinear field: it tells you the single factor is a poor description of your
  scanner, without offering to model the rest.

### Routine 2 — 2D affine galvo↔camera (P-2)

Command a grid of (Vx, Vy), measure each centroid, least-squares a 2×3 affine
from commanded volts to camera pixels, then decompose into scale_x, scale_y,
rotation, shear, and handedness. This catches a **rotated, swapped, or mirrored
galvo pair** — a common miswiring that per-axis fits cannot see, because each
axis independently looks fine.

Stored as a `DetectorTransform` manifest via
`imswitch/imcommon/algorithms/detector_transform.py`, which already carries
`kind`, `matrix`, `calibration_id`, `measured`, and provenance.

### Routine 3 — temporal phase delay (P-3)

Only meaningful on point-scanning rigs, where `ScanWidgetAdvanced` already
exposes `phaseDelay` and `frameDelay` in samples and the designers consume them.
Scan a sparse bright feature bidirectionally, cross-correlate forward against
reverse line, halve the shift. Sweep dwell time as well — the physical lag is
fixed in *time*, so the sample count scales with dwell; report both the constant
lag in µs and the sample count at the current dwell.

Note this routine needs **no probe**: it reads the image an ordinary scan
already produces through the normal scan path, whatever detector participated.
That is why it survives the cut of the point-detector probe (§6) and why it is
the one routine that works on a rig with no camera in the scan path.

Write-back here is different from the others: `phaseDelay` is a **widget-owned**
value, not a `managerProperties` one. Proposal: report it, offer a one-click
"send to Scan widget", and keep it out of the setup JSON to avoid manufacturing
a new config-vs-persisted-state conflict.

### Write-back (from P-1 onward)

`configfiletools.saveSetupInfo(configfiletools.loadOptions()[0], self._setupInfo)`
— the same path `SettingsController`, `LaserController`, and `SLMsController`
already use. `PositionerInfo` is a frozen dataclass but `managerProperties` is a
plain dict, so in-place update works.

The confirm dialog lists the exact key changes (`old → new`, with Δ%) and
nothing happens without it. One asymmetry to warn about in that dialog: the
signal designers read `setupInfo` **live** on every `make_signal`, so a new
`conversionFactor` reaches *scans* immediately, while the positioner managers
cache theirs at construction and keep the old one for *manual moves* until
restart. Until a live `setConversionFactor` exists, the dialog says **restart
required** rather than leaving the rig in a split state.

---

## 6. Deliberately out of scope

Both were discussed and cut. Recorded rather than deleted, because the findings
behind them cost real investigation and the questions will come back.

### Reading a point detector at a parked position

**It is not possible as stated, and the substitute was not worth its cost.**

`APDManager` produces data only from `initiateScan` / `startScan` — there is no
counter task outside a scan, and `getChunk` returns an empty array until a scan
frame lands. "Park the beam and read the APD" has nothing to read.

The substitute would have been a *scan-based* probe: run a small raster through
the normal scan machinery around each commanded offset and locate the feature in
the reconstructed image. That carries a chicken-and-egg — the probe scan's µm
axes are converted by the very `conversionFactor` under test — solvable by
driving probe scans in **volts** and treating the result as a multiplicative
ratio of measured to commanded. Workable, but it makes each measurement point a
full scan lifecycle, and it is the largest single piece of the original plan.

**What the cut costs.** Routines 1 and 2 are camera-only, so a rig with no
camera in the scanned path — `example_sted` is exactly this — gets P-0 (manual
voltage) and P-3 (phase delay), and cannot fit its own `conversionFactor`. On
such a rig the voltage box plus a stage micrometer is still a large improvement
over having no voltage entry at all, it just is not automated.

Nothing in P-0..P-3 forecloses this. It re-enters as a second probe behind the
same interface the camera probe uses.

### Field-nonlinearity measurement and correction

Would have densified routine 2's grid, removed the fitted affine, fit the
residuals with a low-order 2D polynomial per axis, and rendered a heat map.

The decisive point: **nothing in the current signal path can apply such a map.**
`GalvoScanDesigner` and `BetaScanDesigner` convert µm→V by a single
`conversionFactor` and nothing else. Measuring the residual would have produced
a good diagnostic ("your field is 4 % pincushioned at the edges") and no way to
act on it; acting on it means a designer-side warp that touches every scan on
the rig and deserves its own plan, its own flag, and its own validation.

Routine 1 still *reports* residual RMS and R², so a badly nonlinear field is
visible as a poor fit even though it is not modelled.

## 7. Risks

| Risk | Mitigation |
|---|---|
| Beam parked on one spot bleaches or damages the sample | Routines gate illumination per measurement (laser on only for the frame grab); P-0 changes nothing about illumination and leaves that to the operator |
| A raw voltage write desyncs TriggerScope's persisted position | `setVoltage` routes through `setPosition`; never `setAnalog` directly |
| Concurrent writes during a scan | `isScanRunning()` refusal + `sigScanStarting` yield + close-time restore |
| `cameraPixelSizeUm` wrong ⇒ every µm result wrong by that factor | Displayed prominently; run refused at the 1.0 default |
| Widget state restoring a voltage on startup | Voltages are never persisted; config-derived fields declared `configOwnedParameters` |
| A calibration written back that is worse than the old one | Confirm dialog shows old → new and Δ%; R² / residual gates block an obviously bad fit from being offered |
| Camera-only measurement leaves point-scan rigs unable to fit their own `conversionFactor` | Accepted consequence of the §6 cut. Those rigs still get P-0 and P-3; the widget says plainly that routines 1–2 need a camera rather than offering a greyed-out button with no explanation |

---

## 8. Order of work

| Phase | Content | Hardware needed |
|---|---|---|
| **P-0** | `AnalogScannerMixin`, registration, manual voltage panel, interlocks, tests | none to develop; a DMM check to trust |
| P-1 | Camera probe, routine 1 (scale + offset), results table, write-back | camera + a spot |
| P-2 | Routine 2 (affine) + decomposition + `DetectorTransform` | camera + a spot |
| P-3 | Routine 3 (phase delay) | point-scan rig |

Cut for now, see §6: the point-detector probe and field nonlinearity.
