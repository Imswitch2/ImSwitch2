# Single-axis / piezo-fast-axis scans: findings and fix plan

*Status: defect 4 (Beta convFactor + compliance stub) and defect 5
(active-axis collapse misrouting, found in review) FIXED on this branch;
defects 1–3 are findings with the implementation plan below, revised after a
review round (see the review notes inline). WIP working doc (per repo
convention, durable docs go to Sphinx `.rst` once this is implemented). Found
2026-08-21 while rig-testing the ROI Manager 2.0 branch; reproduced headlessly
with `scripts/diagnostics/repro-single-axis-scan.py` against
`example_sted.json` (ND-GalvoX conv 17.44, ND-GalvoY conv 16.63, ND-PiezoZ
conv 1.0 / vel_max 1000 / acc_max 1000).*

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

Timing contract (resolved in review, see Phase C item 4): `scan_samples =
[per_pixel, per_line]` stays *nominal* — the detector uses it to read the
line — but `tot_scan_time_s` becomes truthful: `scan_samples_total *
scan_time_step`. Verified: its only consumer is
`SwabianTimeTaggerManager`'s diagnostics log (:450–463); no watchdog or
progress consumer depends on the old last-level value.

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

Two options existed: (a) support a stepped d1 for non-sweepable devices
(each position held for `sequence_time` — physically the right profile for a
piezo), or (b) reject piezo/stage-like d1 with an actionable error naming
the degenerate-XZ workaround. **The plan resolves this as (a)** — Phase B
introduces per-device sweepability and the stepped d1 — with (b)'s
actionable-error style as the remaining behavior for genuinely unsupported
configurations. Either way the cryptic numpy errors must go. Note "sweepable"
is currently decided purely by `'mock' not in name` (line ~168) — there is
no per-device "this is a galvo" flag; Phase B adds one.

## Defect 3 — downstream consumers assume ≥ 2 scan dims

Even with both designer defects fixed, `APDManager.initiateScan`
(`imswitch/imcontrol/model/managers/detectors/APDManager.py` ~1031–1037)
IndexErrors on 1-dim scans: `y_idx = scan_axes.index("y") if "y" in scan_axes
else 1`, then `self._loop_dims[y_idx]` with `len(_loop_dims) == 1`.

The audit of the sibling consumers is done (results in the Phase C site
list): PMTManager guards that particular index but shares the
`initiateImage` crash and the squeeze-rank line-insert dispatch; both
managers would *silently write zero pixels* for a naive `(N, 1)`
normalization; and `SwabianTimeTaggerManager` crashes on `scan_dims[-2]`.
Image allocation, the assembly loop, TTL designers and recording/OME are
enumerated there as well.

## Defect 4 — BetaScanDesigner mapped convFactors by position — **FIXED**

`BetaScanDesigner.make_signal` built `convFactors` in `setupInfo.positioners`
order but indexed it with the GUI-dim-ordered `target_device[i]`. With the
Z-piezo assigned to scan dim 0 under example_sted, a 10 µm Z request produced
**±0.27 V instead of ±4.75 V** (divided by galvo X's 17.44 instead of the
piezo's 1.0) — a silent 17× range shrink.

Fixed on this branch: conversion factors are looked up per target-device
name (unknown targets raise), and `checkSignalComp` replaces the old
`return True` stub with a real `minVolt`/`maxVolt` check against the
`minmaxes` the designer now emits — one `[min, max]` per **emitted** signal,
parked axes included. The first version of this fix exempted 1-position axes
"like the galvo designer", which review correctly rejected: Galvo omits
inactive axes from its signal dict, while Beta drives parked axes to their
center for the whole scan, so an out-of-range center is a real out-of-range
AO output. Every emitted waveform is checked; positioners without configured
limits are skipped. `test_scan.py` / `test_scan_pixel_count_convention.py`
carry regression tests: the reordered-dims case, out-of-range rejection, and
both the in-range and out-of-range parked-axis cases. Changelog entry under
Unreleased → Bug Fixes.

## Defect 5 (found in review) — active-axis collapse misrouted waveforms — **FIXED**

`GalvoScanDesigner` filters 1-step axes out of the scan
(`_active_axis_indices`), but `sig_dict`, `pixel_sizes` and the
voltage-compliance indexing were built from the *unfiltered* parameter order.
With a configured d1 collapsed to one step and d2/d3 active, d2's sweep was
keyed under d1's device name — and driven onto d1's AO channel — pixel sizes
belonged to other axes, and `checkSignalComp` compared each waveform against
another device's rails (`minmaxes` has active-axes entries but was indexed by
target position). Reachable from the GUI: assign dim 0 a range at or below
1.5 steps.

Fixed on this branch: `sig_dict` keys and `pixel_sizes` are bound through
`active`/`axis_devs_order`, and `checkSignalComp` iterates the aligned
`axis_names`/`minmaxes` pairs the contract already emits instead of
re-deriving the mapping. Regression test:
`test_galvo_collapsed_d1_keeps_signals_on_their_devices` (inverse-collapse:
1-step d1 + active d2/d3). Note the *fully* collapsed case (only one axis
left active) still crashes — that is Defect 1, unchanged.

## Verified behavior matrix (repro script output, example_sted params)

| Case | Result |
| --- | --- |
| Z-only (piezo, 20 steps) | CRASH `np.min` empty, GalvoScanDesigner.py:671 |
| XZ, galvo d1 + piezo d2 | OK — galvo sweep ±0.15 V, piezo staircase ±4.75 V |
| ZX, piezo d1 + galvo d2 | CRASH `argmax of an empty sequence` |
| XZ with 1-step Z | CRASH same as Z-only (active-axis collapse) |
| Degenerate XZ: X = 2 steps × 0.1 µm, Z d2 | OK — X swing ±0.22 µm, piezo full range |
| Beta, dims in setup order (X,Y size 0, Z third) | OK — piezo staircase ±4.75 V |
| Beta, Z on dim 0 | pre-fix: ±0.27 V (Defect 4); post-fix: ±4.75 V, correct |

## Rig workaround (no code/config changes)

Run the Z profile as a **degenerate XZ scan**: galvo X active with 2 steps
(e.g. length 0.2 µm, step 0.1 µm — beam essentially stationary), Z on d2 as
usual. Yields a `[2, Nz]` image; average or discard the second column.

Switching `scan.scanDesigner` to `BetaScanDesigner` also works for a
step-and-settle Z stack (needs `"return_time"` in `scanDesignerParams` +
restart; since the Defect 4 fix, dim order no longer matters and voltages are
compliance-checked) — but Beta + the point-scan TTL/APD chain is untested on
this rig, so the degenerate-XZ form remains the recommended interim.

## 1D scan implementation plan (defects 1–3)

Goal: a Z-piezo-only scan from the advanced widget produces a correct 1-axis
signal, records a correct one-line `(1, N)` image through the APD/PMT chain,
and any 1-active-axis collapse (1-step d2) stops crashing. Phases are ordered
so each lands independently and multi-axis behavior is provably untouched.

### Phase A — designer hardening (no behavior change for existing scans)

1. **Empty-tile fix** (the crash at :671): in `__generate_smooth_scan`,
   evaluate the line period once (`period = curve_poly(x_eval)`), keep
   `pos = np.tile(period[:-1], n_d2 - 1)` (legitimately empty for `n_d2 == 1`),
   and pass `period[:-1]` into `__add_start_end` as the slicing source for
   `pre3`/`post1`/`pos_halfscand2step`. Byte-identical for `n_d2 > 1` (the
   tiled array's head/tail equal one trimmed period's head/tail); for
   `n_d2 == 1`, `pre3 + post1` is exactly the one full sweep (the period
   starts and ends at center with v = v_scan). Enables: smooth 1-axis scans
   (galvo-only line) and the XZ-with-1-step-Z collapse.
2. **Non-smooth d1 guard**: `n_steps_dx[axis+1]` at :186 → 1 when there is no
   d2 axis.
3. **Zero-length-piece hardening**: `__init_positioning`/`__final_positioning`
   with sub-timestep durations return empty arrays; every negative-end slice
   (`pos[start:-end_skip]` in `__get_axis_reps` etc.) breaks on `end == 0`.
   Use explicit `len(pos) - end` indexing / guards. Where a configuration is
   genuinely unsupported, raise an actionable ValueError (pattern:
   `_smooth_scan_bpoly`), never a bare numpy reduction error.
4. **Golden tests first**: capture *successful* multi-axis `make_signal`
   outputs at this branch's HEAD and assert byte-identity after the
   refactor. The repro script is NOT the golden set — several of its seven
   cases crash by design before Phase A. Explicit baseline fixtures (all
   currently succeed):
   - XY galvo–galvo (smooth d1 + step d2);
   - XZ galvo d1 + piezo d2 (the rig's standard use);
   - XYZ 3-axis (d3 staircase);
   - a linestep variant (`n_linesteps > 1`);
   - non-smooth/mock combinations from
     `scripts/diagnostics/test-galvoscandesigner.py`'s parameter sets
     (mock d1; mock between active axes);
   - degenerate XZ (2-step d1).
   Keep the repro script's crash cases (Z-only, ZX piezo-d1, 1-step-Z
   collapse) as *characterization tests* that flip from expected-crash to
   asserted-success as Phases A/B land. Baseline = this branch's HEAD (the
   defect-5 fix already changed `sig_dict` keys / `pixel_sizes` for
   collapse cases — deliberately).

Already done on this branch (defect 5): `sig_dict`, `pixel_sizes` and the
compliance check are bound through `active`/`axis_devs_order` — do not redo,
but keep them bound when refactoring.

### Phase B — per-device sweepability (piezo becomes a legal d1)

Today `__smooth_axis` is decided purely by `'mock' in name` (:168): every
real device is assumed galvo-sweepable. The piezo needs to be *stepped*
instead — and its huge vel/acc (1000/1000, set so its d2+ positioning ramps
are instant) degenerates the smooth spline anyway.

1. New optional `managerProperties.smoothScan: bool`; default falls back to
   the existing name heuristic, so no existing config changes behavior.
   PiezoZ gets `smoothScan: false` in the rig / example_sted configs.
   **Resolve the flag ONCE per device and use that result at every site that
   currently applies the name heuristic** (review finding): the
   vel_max/acc_max-required guard (:92–103 — a `smoothScan: false` device
   must not be rejected for lacking galvo limits it will never use), the
   `__dt_fix` jerk-transition candidates (:144–150), `__smooth_axis` (:168),
   and the d1 scan-path selection. A `smoothScan: false` device without
   vel/acc limits must reach the stepped path, not die in the guard.
2. Stepped d1 for **real** (non-mock) non-smooth axes: reuse the existing
   mock-d1 step path but do NOT re-zero positions (:367
   `positions -= positions[0]` is for virtual mock axes; a real stepped axis
   must keep its absolute, center-anchored `axis_pixel_positions` — the AO
   writes designer signals with no offset added). Each position held for
   `sequence_time` (dwell); `smooth_axes[0] = False` flows to the consumers,
   whose non-smooth-d1 parsing already exists (the diagnostics harness runs
   mock-d1 sets).
3. Config-editor touchpoint (corrected in review): the built-in editor field
   belongs in the manager-property template
   `utility_scripts/builtin_templates/positioners/NidaqPositionerManager.json`
   (its `props` array, beside `vel_max`/`acc_max`) — `setup_metadata.py`
   holds device-kind↔category mappings, not manager-property fields. Document
   in `setupinfo-reference.rst`.

Outcome: ZX (piezo d1 + galvo d2) works, and combined with Phase A a Z-only
scan generates a correct signal.

### Phase C — consumer chain for 1-dim scans

Decision: keep `scanInfo.img_dims = [N]` truthful and normalize at the
detector-consumer boundary. **Review correction: `(N, 1)` normalization does
NOT let the image handling "run unchanged".** Passing logical dims `(N, 1)`
allocates a raw buffer shaped `(1, N)`, and both managers dispatch their
line-insert on `np.squeeze(self._image).ndim` (APD :682/:700, PMT :693) —
the squeezed buffer is rank 1, neither the `== 2` nor the `>= 3` branch runs,
and **no pixels are written, silently**. The singleton-line case must be
handled explicitly, preserving this contract:

- logical loop dimensions: `[N, 1]` (one line of N pixels);
- spatial image shape: `(1, N)` — Ny=1, Nx=N — not `(N, 1)`;
- emitted chunk shape: `(frames, 1, N)`;
- a two-dimensional display scale.

Dispatch on the logical rank (e.g. `len(_loop_dims)`), never on the squeezed
buffer's rank. Tests must assert actual pixel contents and final shapes, not
merely that initialization no longer raises.

Known sites:

1. `APDManager.initiateScan` (~:1031–1037): `_loop_dims[y_idx]` with
   `y_idx = 1` on a 1-element list → IndexError. PMTManager already guards
   this (`if len(self._loop_dims) > y_idx`, :939) — mirror it, or normalize
   dims before this point.
2. `initiateImage` in BOTH managers (APD :740, PMT :661):
   `range(max(len(img_dims), 2))` indexes `img_dims[1]` on a 1-dim tuple →
   IndexError. Pad with 1 instead.
3. The line-insert dispatch above (APD :682/:700, PMT :693) plus the
   assembly loop (`run_loop_dx`, `_pos[1]`, `len(self._loop_dims) == 2`
   special cases); verify the first-line throw arithmetic
   (`throw_startzero + initpos + settling + startacc`) against the Phase A
   signal layout.
4. Timing (resolved in review): keep `scan_samples`/`samples_d2_period`
   nominal — that is the detector's line-read contract — and set
   `tot_scan_time_s = scan_samples_total * scan_time_step` for *every* scan
   (its current `n_scan_samples_dx[-1] * timestep` understates the real
   duration). Safe: its only consumer is Swabian's diagnostics log
   (:450–463). The one-line designer change can land with Phase A.
5. TTL designers (PointScan + Advanced) with `scan_samples = [per_pixel,
   per_line]` and `Ny = 1`; frame/line clock emission for a one-line scan.
6. Recording/OME — the 1-D metadata contract (review-specified).
   `axes_for_recording` (`recording_metadata.py` :52) always returns
   `lead + ['y', 'x']`, and the Galvo contract labels the first logical scan
   dimension `x` whatever the device — so a Z-only profile would be stored
   as `YX` with `PhysicalSizeX = Z step` and nothing preserving that
   physical Z was scanned. Contract:
   - stored axes stay compatibility `YX`, with `SizeY = 1`;
   - the fast axis carries the scan-step pixel size (per-axis calibration
     already exists);
   - the physical scan device and axis are retained as metadata (e.g.
     `scan_axis_device = 'ND-PiezoZ'`, `scan_axis_physical = 'Z'`, sourced
     from `scanInfo.axis_names` plus the positioner's `axes`), written by
     all three storers (OME-TIFF, HDF5+OME-XML, OME-NGFF);
   - an OME regression test asserts the stored shape, the scales, and the
     physical scan-axis annotation (listed in Phase D).
7. `SwabianTimeTaggerManager._infer_dims_from_scanInfo` (:1021–1033):
   without both `x` and `y` labels it falls back to `scan_dims[-2]`, which
   IndexErrors on one dimension. In scope for the "must not crash" bar
   (guard → Ny=1); full 1-D *validation* is scoped to APD/PMT, with the
   time-resolved path re-validated separately on the Swabian rig.

### Phase D — validation

1. Unit: 1-axis smooth (galvo), 1-axis stepped (piezo), 1-step-d2 collapse,
   piezo-as-d1 ZX, APD/PMT initiateScan/initiateImage with 1-dim inputs,
   the Phase A golden + characterization tests, and the 1-D OME metadata
   regression (stored shape `(1, N)` as `YX`/`SizeY=1`, scales, physical
   scan-axis annotation — Phase C item 6). Headless:
   `QT_QPA_PLATFORM=offscreen`.
2. Headless end-to-end (established mock-setup pattern) — **review
   correction: no `mock_scan_setup_APD.json` is tracked in the repo** (that
   name exists only in the local `~/ImSwitchConfig`). The tracked fixtures
   are `mock_scan_setup.json` (MoNaLISA/Beta, no APD) and
   `mixed_hamamatsu_apd_mock_scan_setup.json` (APD, but Base widget + Beta
   designer). Add a new tracked Galvo/Advanced/APD fixture (e.g.
   `galvo_apd_mock_scan_setup.json` under
   `imswitch/_data/user_defaults/imcontrol_setups/`), or explicitly adapt
   the mixed fixture to the Advanced widget + GalvoScanDesigner; then run a
   scripted Z-only scan through ScanController → recorded image.
3. Rig: Z-only piezo scan vs. the degenerate-XZ workaround on the same bead
   (profiles must agree); XZ regression (galvo d1) unchanged.

Estimated effort: A+B are each small and well-bounded (the sketches above are
worked out); C is the open-ended half — the assembly-loop and
recording/metadata audit is where unknowns live. The degenerate-XZ workaround
remains the interim answer on the rig until D2 passes.
