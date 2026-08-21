# Single-axis / piezo-fast-axis scans: findings and fix plan

*Status: findings only, nothing fixed yet. WIP working doc (per repo convention,
durable docs go to Sphinx `.rst` once this is implemented). Found 2026-08-21
while rig-testing the ROI Manager 2.0 branch; reproduced headlessly with
`scripts/diagnostics/repro-single-axis-scan.py` against
`example_sted.json` (ND-GalvoX conv 17.44, ND-GalvoY conv 16.63,
ND-PiezoZ conv 1.0 / vel_max 1000 / acc_max 1000).*

## Symptom

A Z-piezo-only scan through the advanced scan widget (GalvoScanDesigner)
crashes:

```
ValueError: zero-size array to reduction operation minimum which has no identity
  at GalvoScanDesigner.py:671 (__add_start_end)
```

## Defect 1 — GalvoScanDesigner cannot build a 1-active-axis scan

The smooth fast-axis signal is built as `(n_d2 - 1)` repetitions of one line
period, with the first/last half-lines glued on afterwards by
`__add_start_end`:

- `make_signal` line ~178: `n_d2 = n_steps_dx[axis + 1] if axis_count_scan > 1 else 1`
- `__generate_smooth_multid2` line ~518: `np.tile(x_bpoly[:-1], n_d2 - 1)`
  → with `n_d2 == 1` this is `np.tile(..., 0)` → **empty array**
- `__add_start_end` line ~671: `np.min(pos)` on the empty array → the ValueError.

Triggers:

- Any scan with exactly one active axis (a Z-only scan; only-axis becomes d1).
- Any XZ scan whose Z range collapses to a single step —
  `_active_axis_indices` drops 1-step axes, so it degenerates to the same
  1-active-axis case.
- The non-smooth d1 path (device name containing "mock") is equally broken but
  differently: `n_steps_dx[axis+1]` at line ~186 → IndexError.

### Fix sketch (worked out, provably behavior-preserving for n_d2 > 1)

Evaluate the single line period once and slice the glue pieces from the
*period*, not from the tiled middle:

- `__generate_smooth_scan`: compute `period = curve_poly(x_eval)`, keep
  `pos = np.tile(period[:-1], n_d2 - 1)` (empty is fine), and pass
  `period[:-1]` into `__add_start_end` as the slicing source.
- `__add_start_end(pos, period, pos_fix, ...)`: take `np.min/np.max`,
  `pre3 = period[last-min:]`, `post1 = period[:first-max]`, and
  `pos_halfscand2step` from `period`; concatenate
  `(pre1, pre2, pre3, pos, post1, post2)` as before.

For `n_d2 > 1` this is byte-identical to today's output: the tail of the tiled
array equals the tail of one trimmed period (`pos[last_min:] ==
period_trimmed[min_idx:]`) and its head equals the head
(`pos[:first_max] == period_trimmed[:max_idx]`), since tiling repeats
`period[:-1]`. For `n_d2 == 1`, `pre3 + post1` = min→center + center→max = the
one full sweep (period start/end are both at center with v = v_scan).

Open decision for the 1-axis case: `n_scan_samples_dx[-1]` (and therefore
`tot_scan_time_s`) stays the *nominal* line length — for multi-axis scans the
last level is an actual signal length. Check what ScanController/watchdog
consume before changing.

## Defect 2 — a piezo as the d1 fast axis breaks even with 2 active axes

Fixing Defect 1 alone does not enable Z-only scans. A ZX scan (piezo d1, galvo
d2 — no empty tile involved) crashes with:

```
ValueError: attempt to get argmax of an empty sequence
```

Cause: rig-realistic piezo params (`vel_max=1000`, `acc_max=1000`, set huge so
its *step-axis* positioning ramps are ~instant) make the smooth-path
init/final positioning pieces shorter than one sample at 100 kHz
(`n_eval = int(t/timestep) = 0`). Zero-length pieces then break the slice
arithmetic, e.g. `__get_axis_reps`: `pos[start_skip:-end_skip]` with
`end_skip == 0` → `pos[start:-0]` → empty → `np.argmax` dies.

Decision needed: either

- (a) support a stepped d1 for non-sweepable devices (each position held for
  `sequence_time` — physically the right profile for a piezo), or
- (b) reject piezo/stage-like d1 with an actionable error that names the
  degenerate-XZ workaround (below).

Either way the cryptic numpy errors must go. Note "sweepable" is currently
decided purely by `'mock' not in name` (line ~168) — there is no per-device
"this is a galvo" flag.

## Defect 3 — downstream consumers assume ≥ 2 scan dims

Even with both designer defects fixed, `APDManager.initiateScan`
(`imswitch/imcontrol/model/managers/detectors/APDManager.py` ~1031–1037)
IndexErrors on 1-dim scans: `y_idx = scan_axes.index("y") if "y" in scan_axes
else 1`, then `self._loop_dims[y_idx]` with `len(_loop_dims) == 1`. Check
PMTManager for the same pattern, plus anything else assuming ≥ 2 dims
(`ScanWorker._output_image_dims`, image allocation, recording).

## Defect 4 (found in passing) — BetaScanDesigner maps convFactors by position

`BetaScanDesigner.make_signal` builds `convFactors` in `setupInfo.positioners`
order (lines ~48–55) but indexes it with the GUI-dim-ordered
`target_device[i]`. With the Z-piezo assigned to scan dim 0 under
example_sted, a 10 µm Z request produced **±0.27 V instead of ±4.75 V**
(divided by galvo X's 17.44 instead of the piezo's 1.0) — a silent 17×
range shrink. Fix: look conversion factors (and any other per-device
properties) up by target-device name, as GalvoScanDesigner does via `pos_idx`.
Beta's `checkSignalComp` is also still a `return True` stub — add the
min/maxVolt compliance check while there.

## Verified behavior matrix (repro script output, example_sted params)

| Case | Result |
| --- | --- |
| Z-only (piezo, 20 steps) | CRASH `np.min` empty, GalvoScanDesigner.py:671 |
| XZ, galvo d1 + piezo d2 | OK — galvo sweep ±0.15 V, piezo staircase ±4.75 V |
| ZX, piezo d1 + galvo d2 | CRASH `argmax of an empty sequence` |
| XZ with 1-step Z | CRASH same as Z-only (active-axis collapse) |
| Degenerate XZ: X = 2 steps × 0.1 µm, Z d2 | OK — X swing ±0.22 µm, piezo full range |
| Beta, dims in setup order (X,Y size 0, Z third) | OK — piezo staircase ±4.75 V |
| Beta, Z on dim 0 | OK but WRONG — ±0.27 V (convFactor mismatch, Defect 4) |

## Rig workaround (no code/config changes)

Run the Z profile as a **degenerate XZ scan**: galvo X active with 2 steps
(e.g. length 0.2 µm, step 0.1 µm — beam essentially stationary), Z on d2 as
usual. Yields a `[2, Nz]` image; average or discard the second column.

Switching `scan.scanDesigner` to `BetaScanDesigner` also works for a
step-and-settle Z stack, but: needs `"return_time"` in `scanDesignerParams` +
restart, dims MUST be assigned in setup-JSON positioner order until Defect 4
is fixed, there is no voltage compliance check, and Beta + the point-scan
TTL/APD chain is untested on this rig.

## Test plan for the fix

Regression tests (headless, `QT_QPA_PLATFORM=offscreen`), extending
`imswitch/imcontrol/_test/unit/test_galvo_scan_designer.py`:

1. 1-active-axis smooth scan builds, and multi-axis output is unchanged
   (golden comparison pre/post refactor).
2. XZ with 1-step d2 (collapse case).
3. Piezo-as-d1 with vel/acc = 1000 (whichever of 2a/2b was chosen).
4. APD/PMT `initiateScan` with `img_dims` of length 1.
5. Beta convFactor lookup with reordered dims (10 µm Z on dim 0 → ±4.75 V).
