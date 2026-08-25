# MoNaLISA fast-Gauss audit — 2026-08-25

Numerical sanity check of the fast-Gauss reconstruction path
(`imswitch/improcess/reconstructors/monalisa/`), answering three questions:
is the math right, are there hard-coded magic numbers that are not optimal,
and is the estimator the best we can reasonably do. Every quantitative claim
below was verified with synthetic-data experiments (Gaussian foci on known
grids, known amplitudes/backgrounds, controlled noise); the fixes each carry
regression tests.

Branch: `feat/monalisa-fastgauss-lattice-sweep`.

## What the estimator is

Per focus, the pipeline samples a footprint around the localized center and
solves the linear least-squares problem `sample = A * gauss(offset; sigma) + B`,
keeping the amplitude `A`. That is the right family: for a known spot shape
and iid Gaussian noise the LSQ amplitude is the minimum-variance unbiased
*linear* estimator, and it is what makes the path fast (a single dot product
per focus). The audit therefore focused on the inputs to that estimator —
localization, sampling, footprint, model parameters — rather than replacing
it.

## Findings — fixed on this branch

### F1. Localizer frequency axis was wrong twice, canceling for typical periods

`_estimate_period` built its frequency axis with `fftfreq(n_bins)` instead of
the transform length N. That compresses the axis by ~N/(N/2+1) ≈ 2 — silently
compensated by targeting `2/period` instead of `1/period` — and labels the
upper half of the positive-frequency bins *negative*, so any period below
~4 px was unconditionally unfindable, and a window with no peak crashed with
`IndexError` instead of an explanation. Fixed: true axis (`rfftfreq(N)`),
target `1/period`, same effective window width (verified: in-range estimates
numerically unchanged), readable `ValueError` on an empty window. Measured
after the fix: periods 3.0–30 px all recover to <0.05 px error at
guess == truth.

### F2. The period guess was load-bearing and hard-coded

The search window covers only ~±20% around the guess, but the live path and
*Find pattern* always passed the 10 px default. Measured consequence: a true
14 px pattern "localized" to ~12.4 px — silently, producing a scrambled
reconstruction with no error anywhere. Two fixes:

- Callers that hold pattern values (live params dict, plugin `find_pattern`,
  legacy controller) now seed the search with them.
- `robust_localize()` removes the dependence on any guess: a guess-free 2D
  spectral detection (see F8) finds the lattice first and seeds the precise
  1D refinement with the detected periods. The 1D refinement stays the final
  word on the numbers, so behavior for the standard 10–11 px case is
  unchanged (agreement < 0.02 px in tests).

Related magic number: the band-pass low sigma was a fixed 2.0 px; it is now
`period_guess / 5` — identical at the default, proportional elsewhere.

### F3. Bilinear sampling bias — the headline finding (opt-in fix)

The extraction reads the footprint at **integer offsets from the fractional
focus center**, so every sample is bilinear-interpolated (4 gathers). The
interpolation low-passes the peak, and the Gaussian model is evaluated as if
the samples were exact — a model mismatch that biases the amplitude low, by
an amount depending on each focus' subpixel phase.

Measured on a synthetic 11.05 px grid, sigma 2 px, amp ~200, bg 50, noise
sd 3, pinhole 1.5 sigma:

| method | amplitude bias | RMSE | time/frame |
|---|---|---|---|
| bilinear + shared weights (current) | **−11.6 (−5.8%)** | 12.9 | 0.24 ms |
| exact pixels + per-focus weights | +0.03 | 2.8 (noise-limited) | 0.16 ms |

Because the fractional part of the center cycles across the grid (period
11.05 → phase steps of 0.05), the bias is a **fixed-pattern modulation** of
the reconstruction, not a global scale factor.

The fix — `Sampling: Exact pixel` — fits the true integer pixels with
per-focus weights from the closed-form 2×2 normal equations at the real
pixel-minus-center offsets. No interpolation, ~1.5× faster (one gather
instead of four), and edge foci fit only in-frame pixels instead of the
legacy clamp that double-counts border pixels. **Bilinear remains the
default** so rig output is bit-identical until the mode is validated on real
data; recommendation is to flip the default after rig validation.

### F4. Offline bleaching correction disagreed with live by a power of 4

Offline: `(E_0/E_i)**4`; live: `E_0/E_i` (and its docstring claimed to mirror
the offline path). Fluorescence intensity is linear in the remaining
fluorophore population, so power 1 is correct (this reapplies the earlier
uLens/MoNaLISA-audit decision that never landed on main). Offline now matches
live, preserves input dtype (the SignalExtractor DLL reads the raw buffer),
and skips zero-energy frames instead of dividing by zero.

## Findings — quantified, feature answers them

### F5. The pinhole radius has no closed-form optimum — hence the sweep

The math does bound it. With Gaussian+constant LSQ on an isolated focus
(sigma 2, period 11.05, amp 200, noise sd 5), measured amplitude std and
neighbor-crosstalk bias vs radius:

| r/sigma | pixels | std (isolated) | bias from 4 neighbors |
|---|---|---|---|
| 0.75 | 9 | 22.6 | −0.00 |
| 1.0 | 13 | 10.7 | −0.02 |
| 1.5 | 29 | 4.6 | −0.06 |
| 2.0 | 49 | 3.0 | −0.23 |
| 2.5 | 81 | 2.2 | −0.67 |
| 3.0 | 113 | 2.0 | −2.1 |
| 4.0 | 197 | 1.6 | **−12.3** |

Variance falls steeply to ~2 sigma then flattens; crosstalk blows up past
~2.5 sigma (at this period/sigma ≈ 5.5). So the *statistical* sweet spot is
r ≈ 2–2.5 sigma — but the practical optimum is smaller when out-of-focus
background matters (the pinhole is the sectioning knob, exactly as in
image scanning microscopy) and depends on the sample. That data dependence is
why the new **parameter-sweep mode** (stack over pinhole values, slide to
compare) is the honest tool rather than a formula. The default 1.5 sigma is a
reasonable conservative choice; the legacy `num_rects=3` footprint (a filled
5×5 square ≈ r 2–2.8 px) sits near the SNR optimum but with slightly more
crosstalk exposure.

## Findings — noted, deliberately not changed

### F6. `cos^6` offset template vs band-passed data

The offset/period refinement fits `cos^6(pi (x-o)/p)` to projections that
have already been band-passed to nearly pure sinusoids, and the residual has
no amplitude/offset parameters, so it is not a proper least squares. It still
works because the argmax over `(p, o)` reduces to template cross-correlation,
whose phase is unbiased for symmetric peaks; the harmonics the template
carries correlate with ~nothing in the filtered data. Edge effects (partial
periods at the border) bias offsets by up to ~0.3 px on an 18-period field —
inherent to the method, acceptable relative to sigma ≈ 2 px. Left as is;
simplifying the template to a plain cosine would be cosmetic.

### F7. Redundant/overlapping inputs

- `Footprint rectangles` (shells) and `Pinhole radius` describe the same
  thing (footprint extent) in two systems; the shell footprint is just a
  square pinhole. Kept for Mini_Recon parity; the circular pinhole + radius
  is the parameterization worth standardizing on eventually.
- `Gaussian sigma` is derived from `PSF FWHM / (2.355 * pixel size)` when not
  explicitly set — but the *detected spot* is the emission PSF shaped by the
  excitation focus, so the emission-PSF sigma is only an approximation of the
  right matched-filter width. The sigma sweep now allows checking this
  empirically; auto-estimating sigma from the data (fit the average focus) is
  the natural future step.
- `BG modelling: Gaussian` silently degrades to a constant background term in
  the fast path (only the classic SignalExtractor models a Gaussian
  background base). Minor UX inconsistency, unchanged.
- The `Auto-detect scan orientation` checkbox gates only the classic path;
  fast-Gauss always auto-detects (by total variation) when it has a full
  first stack. Unchanged.

### F8. Grid-only localization — groundwork laid, reassignment still rectangular

The 1D projection localizer cannot represent rotation (a 5°-rotated grid
"localizes" with plausible-looking periods and garbage offsets) or
non-rectangular symmetry (a hexagonal pattern projects to misleading
periodicities). The new `lattice.py` provides the general representation
(any 2D Bravais basis + offset; rectangular/hexagonal constructors; reduced
bases; frame enumeration byte-compatible with the legacy grid) and
`detect_lattice()` — guess-free 2D FFT peak detection with sub-bin
refinement, offsets from spectral phases — verified on rotated-square and
hexagonal synthetic data. Extraction is lattice-agnostic behind the
`get_interp_coords_for_centers` seam.

What is *not* done: the reassignment step (`get_1d_indices` and the output
raster) assumes scan steps subdividing an axis-aligned rectangular unit cell.
A hexagonal reconstruction needs a gridding/splatting reassigner (foci land
off the output raster) plus a decision about the output pixel lattice. Until
then, non-rectangular patterns fail loudly with their measured geometry, and
small tilts (>0.5°) log a warning.

## Verdict on "is fast gauss the best we can do?"

The estimator family is right and fast. The gaps, in impact order:

1. **Sampling bias** (−6%, fixed-pattern) — solved by exact-pixel mode;
   recommend making it the default after rig validation.
2. **Localization fragility** — solved by detection-seeded localization.
3. **Footprint/pinhole choice** — inherently data-dependent; solved
   operationally by the sweep.
4. **Model sigma calibration** — approximation remains; sweepable now,
   auto-estimation from data is future work.
5. **Poisson vs Gaussian noise weighting** — at camera-typical signal levels
   the unweighted LSQ loses only a few percent efficiency vs a
   variance-weighted fit; not worth the speed cost.
6. **Bilinear reassignment geometry** (one output pixel per scan step) is
   exact for commensurate rectangular scans; nothing to gain there until
   non-rectangular lattices arrive.
