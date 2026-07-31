# TriggerScope firmware quirks

Notes accumulated while integrating ImSwitch2 with the Snouty TriggerScope
firmware (`C:\Users\Snouty\GitHub\Triggerscope\TriggerSwitch_0.1\TriggerSwitch_0.1.ino`,
`parameters.cpp/.h`). Each entry says whether it's fixed (and where) or just
flagged for awareness.

## 1. Merged-TTL "Temporary fix" in `runPixelCycle()` (RASTER_SCAN only) — not fixed, by design for now

`runPixelCycle()` has a "Temporary fix" comment: instead of pulsing each TTL
line according to its own configured row, it pulses **TTL lines 0–3 together**
in one shared window, taken from the *earliest* TTL row's start/end
(`p1StartUs`/`p1EndUs`). `p1Line` and the per-device TTL rows are **ignored**.

Consequence: during a plain `RASTER_SCAN`, laser emission is controlled
purely by which lasers are *armed* (`sigScanBuilt`), not by the TTL table —
all armed lasers fire together every pixel, in the same window as the camera
trigger pulse on TTL3.

The firmware's parameter parser already supports 3 independent pulse windows
(p1/p2/p3) — restoring the commented-out per-line code in `runPixelCycle()`
would enable real independently-timed per-device pulses. If that's ever done,
watch for:
- **`uint16_t` truncation** on `StartUs`/`EndUs` — max representable value is
  ~65.5 ms; longer pulses silently wrap.
- **Panel-label vs 0-based `ttl[]` indexing** mismatch between the UI/config
  and the firmware array.
- The **488 Light Sheet / 640 nm sharing TTL2** config collision (currently
  moot because all lines are merged anyway, but would become a real conflict
  once per-line pulses are restored).

This does **not** affect RESOLFT scans — see #2.

## 2. RESOLFT scans use separate, correctly-sequenced firmware routines — not a bug

`LS-XY-RESOLFT_SCAN` (~ino:504), `pLS-RESOLFT_SCAN` (~ino:627), and
`pLS-RESOLFT-Multicolor_SCAN` (~ino:747) are dedicated routines, structurally
distinct from `RASTER_SCAN`/`runPixelCycle()`. They sequence
activation/depletion/readout pulses one after another (non-overlapping) by
construction. The RASTER_SCAN merged-pulse workaround in #1 does **not**
apply to them — RESOLFT's "don't pulse all lasers at once" requirement is
satisfied independent of that workaround.

## 3. Camera trigger is a separate signal, not slaved 1:1 to a laser pulse

The camera trigger (TTL3 on Snouty's `OrcaStraight`) is its own line with its
own semantics (DCAM level/"bulb" trigger: exposure follows the TTL HIGH
window). How it relates to laser pulses differs per scan type:
- **RASTER_SCAN**: TTL3 is pulsed *inside* the same merged TTL0-3 window as
  the lasers (see #1) — one pulse per pixel, co-edged with the lasers, but
  only because of the merge, not because the camera path is hardcoded to a
  laser.
- **Basic RESOLFT**: camera is gated HIGH only during the readout-laser
  pulse; OFF during activation/depletion.
- **Multicolor RESOLFT**: camera is held HIGH across readout + laser2 +
  laser3 at 3 galvo positions — one exposure integrates all 3 colors.

`WidefieldCamera` (TIS) has no `digitalLine` configured and free-runs
(software-only liveview); it is never hardware-triggered by the firmware at
all. Only `OrcaStraight` (Hamamatsu) is wired to a TTL line.

## 4. `RASTER_SCAN` axis loop is inclusive of the endpoint — fixed (Python-side)

Each of the 4 nested raster axis loops is:
```cpp
while (abs(pos) <= abs(lenV)) {
    ...
    pos += stepSizeV;
}
```
This is **inclusive** (`<=`) of the axis length. When `length` is an *exact*
multiple of `stepSize`, `pos` eventually equals `lenV` exactly and the
boundary check still passes, so the loop visits `length/step + 1` positions
(both endpoints) instead of the `round(length/step)` positions the rest of
the system expects (BeadRec reconstruction dims, GUI step-count display).

Example that surfaced this: scanning 4000 nm at exactly 200 nm/pixel (an
exact ratio, 20.0) produced **21** frames. Using 201 nm/pixel (not an exact
divisor) "fixed" it by accident — the loop overshoots the inclusive
threshold one increment earlier and happens to stop at 20. Not a real fix;
fragile for other length/step combinations.

**Fix applied (no firmware reflash needed)**:
[`trimRasterLengthForFirmwareBoundary(length, stepSize)`](../imswitch/imcontrol/controller/controllers/TriggerScopeRasterController.py)
shrinks the length sent to firmware by half a step before the volt
conversion in `getTriggerscopeParameters()`, so the inclusive boundary
reliably lands between the intended last position and the next one —
regardless of which side of an exact ratio floating-point rounding falls on.
Applies uniformly to all 4 raster dimensions (X/Y/Z/W).
Tests: `_test/unit/test_trigger_scope_raster_length_trim.py` (5 pass,
re-implements the firmware's exact loop in Python).

## 5. `dimOne` zero-step check happens before any DAC write — not fixed, flagged only

The innermost (`dimOne`) loop's `if (dimOneStepSizeV == 0) break;` guard is
checked **before** any DAC write or `runPixelCycle()` call. If `dimOne` (the
X-mapped axis) is ever configured as the *trivial/unused* dimension with
`stepSize == 0` — e.g. a 1D raster scanning only along Y — the scan would
silently produce **zero pixel cycles** for every outer-loop position, instead
of stepping through Y at a fixed X.

Not observed on the rig; only matters if a Y-only (or Z/W-only with `dimOne`
trivial) 1D raster scan is ever attempted. Watch for this if/when that
combination is used.

## See also

- Project memory file `snouty-beadrec-triggerscope.md` (Claude Code memory,
  not in this repo) has the full narrative history of these findings (dates,
  diagnosis steps, related non-firmware fixes like the BeadRec ROI
  pixel-scale bug and the TISManager parameter-forwarding bug).
- [`docs/scan-lifecycle.rst`](scan-lifecycle.rst) for how scan controllers
  (including the raster controller) integrate with the rest of ImSwitch2.
