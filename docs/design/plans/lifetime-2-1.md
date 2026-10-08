# Lifetime 2.1 — follow-up to Lifetime 2.0

Status: **recorded, not planned**. Opened by Lifetime 2.0's P6 as the home
for the items its review moved out (its section 9) and for the rig numbers
the validation campaign (`docs/timetagger/validation.rst`) produces, which
every item below depends on. It becomes a plan once those numbers exist.

## Rig numbers (to be measured; `docs/setup-validation/etsted.md`)

| Number | Measured | Where it is used below |
|---|---|---|
| Card model, serial, USB tag budget | | bandwidth, raw tags |
| IRF FWHM (ps) and `t0_ps` | | deconvolved fits, two-exponential fit |
| Dark rate (Hz) and afterpulse fraction | | background model |
| Rep rate (MHz) and period jitter (ps) | | phasor, software gating |
| Line period and flyback (ms), edge count vs Ny | | outer axes, linesteps |
| Frame-to-line skew (ps) | | `pixelPatternOffsetPs` default |
| Line delay (ps), sign | | M9 detector-sync fix |
| Convergence table (tutorial 10 extended) | | fit-method defaults |
| STED pulse delay (ps), if cabled | | software gating |

## Items (from Lifetime 2.0, section 9)

- **Two-colour FLIM**: two photon roles → two `Flim` objects sharing one
  `EventGenerator` pair, started under `SynchronizedMeasurements`.
- **Intensity-only counter detector** (`CountBetweenMarkers`, fixed
  `n_values`, re-armed per frame) — only if a sync-free rig exists.
- **Outer axes** (z/t cubes): after M9's linestep fix; reading each ready
  frame promptly vs `finish_after_outputframe = N_z`.
- **Hardware-side accumulation**: persistent `Flim` + `getSummedFrames()`.
- **Raw tags and replay**: `FileWriter` and `createTimeTaggerVirtual`
  (`replay`, `setReplaySpeed(-1)`, `waitForCompletion`) — one recorded rig
  session driving the test suite with real tags; the filter emulation to be
  verified against step 5 of the campaign.
- **Recording hook**: a *recorded product* parameter and
  `<rec>_timeresolved.h5` attached via a `RecordingManager` post-scan hook,
  once that hook exists.
- **Phasor per-pixel cloud with cursor selection → labels layer; ROI decay**
  from the cube.
- **IRF-deconvolved tail fit / two-exponential fit** using the stored IRF
  (`irf/` group of the products file).
- **Software-defined gating** (`GatedChannel` / `DelayedChannel`) with the
  STED-pulse photodiode as reference.
- **ImProcess**: open products files (`load_products`), re-gate and re-fit
  offline with `timeresolved/` (`refit_cube`); a lifetime overlay processor.
- `facade.time_tagger.raw`, "Open tutorial…" links from the widget.
- **Linesteps** (`S > 1`): lift the detector's refusal once ROADMAP M9's TTL
  designer emits one line edge per linestep.
- **Reverse-mode acceptance** on a Time Tagger 20 / Ultra: step 5 of the
  campaign; if the card reorders filtered timestamps, the decision between a
  software sort and the vendor's own ordering guarantee.
