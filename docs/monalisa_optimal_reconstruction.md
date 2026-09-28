# MoNaLISA reconstruction: the mathematical optimum, and a plan to build it

Date: 2026-09-28. Branch: `claude/monalisa-reconstruction-math-af9btj`.

Builds on `feat/monalisa-fastgauss-lattice-sweep` (general `Lattice`
representation and guess-free detection, exact-pixel sampling, the
scatter-and-grid path for non-rectangular lattices, the parameter sweep, the
xrecon ISM port) and on that branch's `docs/monalisa_fastgauss_audit.md`. It answers three
questions for **arbitrary periodic illumination patterns** (square,
rectangular, hexagonal, rotated): what the mathematically optimal
reconstruction is, how to implement it cleanly on top of the branch, and which
input parameters remain once everything derivable is derived.

Every number quoted here is produced by
`docs/monalisa_optimal_reconstruction_experiments.py` (NumPy + SciPy only;
`python docs/monalisa_optimal_reconstruction_experiments.py` reprints all
tables in about a minute). Tags E1–E6 refer to its sections. The experiments
use synthetic data with known ground truth: Gaussian foci on a known lattice,
known amplitudes and backgrounds, controlled noise. Rig validation is the
last phase of the plan, not a substitute for it.

---

## Summary

**What a scan measures.** Each camera frame is a lattice of diffraction-limited
spots. The *amplitude* of the spot at focus `f` in frame `k` is a sample of
the specimen convolved with the RESOLFT effective PSF `h_e` (50–100 nm FWHM),
taken at the sample-space position `focus − stage offset`. The resolution of a
MoNaLISA image is set by `h_e`; the reconstruction cannot sharpen it (short of
an explicit deconvolution). Its job is to estimate those amplitudes with the
least noise and bias, and to put each one exactly where it belongs.

**What the optimum is.** The maximum-likelihood estimate of the specimen under
the full physical model (every pixel of every frame, Poisson noise). It is
well defined and computable (E5 implements it as a multi-frame
Richardson–Lucy), but for Gaussian noise it *provably decomposes* into two
stages, and the decomposition loses little under Poisson noise:

1. **Per frame:** the joint weighted least-squares fit of *all* foci at once —
   amplitudes plus a background basis — is a sufficient statistic. Its
   per-focus weights are rows of one pseudo-inverse computed once per
   geometry; applying them is one gather and one dot product per focus per
   frame, exactly the cost of today's fast Gauss.
2. **Per timepoint:** weighted least-squares placement of the amplitudes on
   the sample-space sample set. That is exact nearest placement when the scan
   is commensurate with the lattice, and least-squares cubic B-spline gridding
   otherwise; optionally followed by deconvolution of `h_e`.

**Measured against the current fast Gauss** (synthetic data, standard
geometry: 11.05 px period, 2 px spot sigma, 77 nm pixels):

- The isolated per-focus fit is **crosstalk-limited**: its bias grows from
  0.06 to 71 counts as the footprint grows from 1.5 σ to the whole cell
  (E1). That is the only reason the pinhole radius needed a sweep. The joint
  fit has zero crosstalk bias at every radius, so the footprint is chosen by
  noise alone: 2.5× lower amplitude noise (6× lower variance) at 3 σ than the
  isolated 1.5 σ fit.
- With one wide Gaussian per focus in the basis, the joint fit removes
  out-of-focus haze by *shape* instead of rejecting it with a pinhole (E1:
  bias 1e-12 instead of 3.9–61 counts), and returns the haze as a second
  image plane.
- On a full synthetic acquisition the joint extraction has 3.3× lower RMSE
  than the 1.5 σ isolated fit (E5a: 0.081 vs 0.27), and two-stage plus
  `h_e` deconvolution lands within 25% of full-model maximum likelihood at
  1/200 of the compute (E5b: 0.130 vs 0.106 relative RMSE). The isolated-fit
  image cannot be deconvolved at all — it diverges (0.32 → 0.37).
- Poisson/read-noise weighting is worth about 20% in variance at best
  (E2) and nothing measurable in the joint fit on a realistic frame (E5a):
  keep it optional and off.
- ISM pixel reassignment gains nothing for RESOLFT-confined foci: the optimal
  shift is `α = σ_e² / (σ_e² + σ_d²) = 0.03–0.08` and the width gain is ≤ 4%
  (E3a). The ported xrecon default `α = 0.5` *widens* a 63 nm focus to 74 nm
  and loses 16% of its peak (E3b), and the port's Gaussian weight is 11× too
  wide (E3c). The ISM method is not a foundation to build on.
- The branch's bilinear splat is exact when the lattice is commensurate with
  the scan raster but blurs otherwise (E4: 0.10 vs 0.019 relative RMSE for a
  0.3% step mismatch; 0.18 vs 0.089 for a rotated hexagonal lattice; B-spline
  least-squares gridding in both cases).
- The spot width must be calibrated from the data (E6 recovers σ to 0.5%):
  the spot is `h_d ⊗ h_e`, and a matched filter with the nominal detection
  PSF under-estimates amplitudes by 10% (E5a).

**What remains to tweak** after the plan (section 5): pixel size; a rough
period band; background model (constant / plus haze / none); footprint
reach; noise weighting on/off; scan-orientation override; stage-to-camera
rotation; output pitch and raster lock; gridding regularization;
deconvolution (`h_e` FWHM, iterations); per-focus gain and drift correction
(need an overscan); bleaching correction; method and device. Everything else
on today's widget — footprint mode, footprint rectangles, sampling mode, ISM
shift / oversampling / patch mean / frame batch, pattern-geometry mode,
hand-typed periods and offsets — is either derived from the data or retired.

---

## 1. Forward model

### 1.1 Geometry

*Camera.* Pixels `x ∈ Z²`, pitch `p_nm` (77 nm on the MONALISA setups).

*Illumination lattice.* `L = { o + m·a1 + n·a2 : m, n ∈ Z }`, any 2D Bravais
lattice: square (`a1 ⊥ a2`, `|a1| = |a2|`), rectangular, hexagonal
(`|a1| = |a2|`, 60°), and rotated versions of each. Only foci inside the
frame matter: `F = L ∩ frame`, indexed by `f`, at positions `r_f`. The
branch's `Lattice` (reduced basis, `points_in_frame`, `detect_lattice`) is
exactly this object.

*Scan.* Frame `k` is recorded with the stage displaced by `s_k` (stage
coordinates; from the acquisition layout: loop indices × step sizes ×
directions). In camera coordinates the displacement is `d_k = R s_k / p_nm`,
where `R` is the stage-to-camera transform: an axis permutation and two signs
(the eight candidates the total-variation search already covers) plus a
small rotation and scale that today are assumed to be the identity.

*Sample space.* A specimen point at sample coordinate `u` appears in frame
`k` at camera position `u + d_k`. Focus `f` therefore probes

    q_{k,f} = r_f − d_k                                                     (1)

The set `P = { q_{k,f} } = F − D` (`D = { d_k }`) is the reconstruction's
sampling set. It is `L`-periodic, and inside each unit cell it is the
(mirrored) scan raster `D`. Two properties decide everything downstream.

**Coverage.** The scan covers one unit cell when the scan rectangle is a
fundamental domain of `L`: its area `n_x Δx · n_y Δy` equals the cell area
`|det(a1, a2)|` (the branch's `scan_cell_ratio = 1.0`) *and* its
`L`-translates tile the plane. Whenever a lattice vector lies along the fast
axis, `a1 = (a, 0)`, any rectangle `a × (cell area / a)` is a fundamental
domain (brick tiling). This is how the reference "diamond" acquisition
(10.41 px lattice at 45°, 32 × 16 steps of 35 nm) covers exactly one cell of
a lattice that has no axis-aligned unit cell: its rectangle is
`a√2 × a/√2`. The diagnostic that replaces the area ratio is a
*count-per-raster-pixel histogram* (0 = hole, 1 = exactly once, > 1 =
overlap).

**Commensurability.** Choose the output raster `R` with pitch `(Δx, Δy)` and
origin at a lattice point. `P ⊂ R` iff `a1, a2 ∈ ZΔx × ZΔy`. Then every
sample lands exactly on a raster pixel and, with coverage 1, each raster
pixel receives exactly one sample: reconstruction is a *placement* — no
interpolation, no bias (E4: 1e-14). Axis-aligned rectangular scans with
`n` steps per period are the special case the legacy integer path
(`scan_geometry.get_1d_indices`) implements. A hexagonal lattice
`a1 = (a, 0)`, `a2 = (a/2, a√3/2)` is commensurate iff `n_x` is even and
`Δy = a√3 / (2 n_y)`: a slightly anisotropic raster, isotropic to 0.3% with
`n_y = round(n_x √3 / 2)` (22 × 19 steps for `a = 11 px`). In practice
commensurability holds only as well as the pixel size and the stage step are
known: a 0.3% mismatch already costs the bilinear splat 10% RMSE (E4), so
stage 2 must handle small residuals gracefully (section 2.4).

### 1.2 Photophysics and the per-frame image

Per focus, the ON / OFF / readout sequence confines emission to an effective
PSF `h_e` around `r_f` (FWHM 50–100 nm, `σ_e = 21–42 nm`). The emitted light
is imaged with the detection PSF `h_d` (`σ_d ≈ 93 nm` for a 220 nm FWHM).
The frame is

    I_k(x) = Σ_f ∫ S(u) · h_e(u − q_{k,f}) · h_d(x − u − d_k) du + B_k(x) + noise     (2)
           = Σ_f A_{k,f} · g(x − r_f) + B_k(x) + noise

with the amplitude

    A_{k,f} = ε_f · (S ∗ h_e)(q_{k,f})                                       (3)

and the spot shape `g = h_d ⊗ h_e ⊗ pixel`, `σ_spot² = σ_d² + σ_e²`,
normalized to unit peak. `ε_f` is the focus' excitation / switching
efficiency (the illumination envelope, a smooth field over the frame). The
exact spot is centered on the *emitters* inside `h_e`, not on `r_f`: its
centroid sits at `α · (local specimen offset)` with
`α = σ_e² / (σ_e² + σ_d²)`. That sub-pixel shift is all the "ISM information"
there is (section 2.5). `B_k` is background: camera offset, out-of-focus haze
(a wide blob per focus, since the OFF pattern confines emission only within
a limited axial range), incompletely switched-off molecules. Noise: Poisson
on the counts plus Gaussian read noise (sCMOS, about 1.6 e⁻).

Three consequences:

1. The reconstruction estimates `S_eff = S ∗ h_e` on `P` (up to `ε_f`). Its
   resolution is `h_e`'s. Nothing in the estimator sharpens it except an
   explicit deconvolution of `h_e` (section 2.4).
2. The spot is not the detection PSF: `σ_spot = √(σ_d² + σ_e²)`, 4% wider at
   220 / 63 nm. A matched filter with the nominal `σ_d` under-estimates
   amplitudes by 10% (E5a), and by a specimen-dependent amount. The spot must
   be measured from the data (E6).
3. Foci are 9–11 `σ_spot` apart: their tails overlap at the 1e-4 level at the
   neighbor's center but at the 1–5% level over the outer half of a cell.
   That is the crosstalk the isolated fit suffers (E1).

---

## 2. The optimum

### 2.1 Full-model maximum likelihood

Unknowns: `S` on a raster (the output) plus nuisance parameters (`B_k`,
`ε_f`, drift). Data: all pixels of all frames. Likelihood: `Poisson(H S + B)`
with `H` the linear operator of (2): shift the specimen by `d_k`, multiply by
the excitation lattice `Σ_f h_e(· − r_f)`, blur with `h_d`, sample onto
camera pixels. The maximum-likelihood estimate (or MAP, with positivity / TV /
Hessian-Schatten priors) is asymptotically efficient — it attains the
Cramér–Rao bound — uses every photon with its correct weight, exploits the
sub-pixel centroid information, handles any lattice and any scan, and needs no
pinhole.

E5 implements it as a multi-frame Richardson–Lucy iteration with the exact
`H` and its adjoint. On a 60 × 60 px, 576-frame synthetic acquisition it
reaches a relative RMSE of 0.106 against the true specimen after 40
iterations, in 18 s on one CPU core. A 512 × 512 px, 1000-frame acquisition
scales to about two minutes per iteration on a CPU, seconds on a GPU. It also
needs `h_e`, which is not known a priori. **This is the reference, not the
workhorse.**

### 2.2 The two-stage decomposition

Write frame `k` as a vector `I_k`, `G` the (pixels × foci) matrix of spot
shapes `g(x − r_f)`, `N` a background basis (per-focus constants, optionally
per-focus wide Gaussians), `θ_k = (A_k, β_k)` the per-frame coefficients.
With Gaussian noise of variance `σ²` the negative log-likelihood is
`Σ_k ‖I_k − [G N] θ_k‖² / 2σ²`. Decompose orthogonally against the column
space of `[G N]`:

    ‖I_k − [G N] θ_k‖² = ‖I_k − [G N] θ̂_k‖² + (θ̂_k − θ_k)ᵀ [G N]ᵀ[G N] (θ̂_k − θ_k)
    θ̂_k = ([G N]ᵀ[G N])⁻¹ [G N]ᵀ I_k                                        (4)

The first term does not depend on `θ_k`, hence not on `S`: **the per-frame
joint least-squares coefficients are a sufficient statistic.** Profiling out
the background coefficients, the maximum-likelihood estimate of `S` solves

    min_S  Σ_k (Â_k − M_k S)ᵀ C_k⁻¹ (Â_k − M_k S),      C_k = cov(Â_k)          (5)

where `(M_k S)_f = ε_f (S ∗ h_e)(q_{k,f})`. `GᵀG` has off-diagonal entries
`π σ² exp(−|r_f − r_f'|² / 4σ²)`, about 5e-4 relative for neighbors 11 px
apart at `σ = 2`, so `C_k` is diagonal to that accuracy and (5) is an
ordinary weighted least squares of `S` against the placed amplitudes. That
is the two-stage pipeline: stage 1 is (4), stage 2 is (5). It is exact for
Gaussian noise. For Poisson noise, weighted least squares with
`W = diag(1 / variance)` is the Gauss–Newton approximation, and E2 / E5
bound what it loses.

What the decomposition discards: the sub-pixel centroid information
(`α ≈ 0.05`, E3a) and the exact Poisson weighting inside the spot. E5b
measures the total price: two-stage plus deconvolution 0.130 versus full
maximum likelihood 0.106 relative RMSE at 40 iterations (within 25%), equal at 20
iterations, at 1/200 of the compute.

### 2.3 Stage 1: the optimal linear extraction

**Estimator.** `Â_k = W I_k`, with `W` the amplitude rows of the
pseudo-inverse in (4), computed once per (lattice, spot model, background
model, footprint reach) and applied per frame as a sparse gather plus dot
product. The rows are translation-invariant up to sub-pixel phase for
interior foci, and individually computed for edge foci. Properties (E1;
period 11.05 px, `σ = 2 px`, amplitudes 100–300, noise sd 5):

| footprint reach | pixels per focus | isolated fit: max crosstalk bias | isolated fit: std | joint fit: bias | joint fit: std |
|---|---|---|---|---|---|
| 1.5 σ | 29 | 0.06 | 4.8 | 0 | 4.8 |
| 2.0 σ | 51 | 0.23 | 2.9 | 0 | 2.9 |
| 2.5 σ | 78 | 0.9 | 2.2 | 0 | 2.2 |
| 3.0 σ | 111 | 2.8 | 1.9 | 0 | 1.9 |
| 4.0 σ | 202 | 17 | 1.6 | 0 | 1.8 |
| whole cell | 380 | 71 | 1.5 | 0 | 1.8 |

The isolated fit's bias column is the audit's F5 curve. It is a property of
the estimator, not of the data, and it is the only reason the footprint had
to stay near 1.5–2 σ. The joint fit removes it at every reach (the weights of
focus `f` carry small negative lobes under its neighbours' spots), so the
reach is chosen by noise alone; 3 σ is within 5% of the noise floor. The
isolated fit's slightly lower std at large reach is the bias–variance
trade-off of assuming the neighbours' amplitudes are zero.

**Background.** Per-focus constants on the Voronoi cells make the background
piecewise constant. Adding one wide Gaussian per focus (`σ_h ≈ period / 2`;
the *Focus-ISM* idea, and the classic path's *Gaussian BG modelling*) models
out-of-focus haze by its shape instead of rejecting it with a pinhole. E1
with a haze component of 30–90 counts and `σ_h = 6 px`:

| footprint reach | isolated fit: rms bias | joint, constants only: rms bias | joint + haze: rms bias | joint + haze: std |
|---|---|---|---|---|
| 1.5 σ | 3.9 | 3.9 | 0 | 5.2 |
| 2.5 σ | 6.5 | 6.7 | 0 | 2.5 |
| 3.0 σ | 8.4 | 8.7 | 0 | 2.2 |
| whole cell | 61 | 10 | 0 | 2.1 |

The haze amplitude comes out as a second image plane — the classic method's
"Base 1" — at no extra cost, and doubles as a sectioning diagnostic.

**Noise weighting.** `W_w = (GᵀΩG)⁻¹ GᵀΩ`, `Ω = diag(1 / (μ̂ + σ_r²))`, with
`μ̂` from an unweighted first pass (two-pass) or from the mean frame. E2
(single focus, oracle weights) bounds the variance gain: up to 15% for peaks
≤ 50 counts, 5–23% for peaks of 200–1000 counts over a low background,
nothing over a high background. E5a (joint fit on a realistic frame): 0.082
versus 0.081 — nothing. Optional, off by default.

**Spot model.** `σ_spot` (optionally an ellipticity, and the haze `σ_h`) is
fitted to the mean frame with per-focus amplitude and constant profiled out
(variable projection: one scalar minimization; E6 recovers 1.99–2.00 px for
2.00 px true; E5: 1.297 for 1.298). No PSF FWHM input is needed; the fitted
value is reported as a diagnostic ("spot FWHM 226 nm").

**Sampling.** Exact integer pixels with per-focus weights evaluated at the
true offsets (audit F3; the branch's `build_exact_sampling`) are a
prerequisite: bilinear sampling low-passes the peak by several percent with a
fixed-pattern phase dependence.

**Edges and uncertainty.** Edge foci have fewer pixels and larger variance,
and are flagged; foci with fewer than about ten in-frame pixels are dropped.
Per-focus variance is `σ² ‖w_f‖²` (Gaussian) or `Σ w_f² var` — an
uncertainty plane for free, and the weight of stage 2.

**Cost.** Identical to today's exact-pixel path at the same reach: one gather
of about 110 px per focus at 3 σ; 1800 foci in a 464² frame is 2e5
multiply-adds per frame. Live-compatible.

### 2.4 Stage 2: placement, gridding, deconvolution

**Placement.** Sample positions from (1); output raster from section 1.1.
Commensurate (residual below about 0.05 px across the frame): nearest
placement is exact, and it is what both legacy paths do — the integer
scatter, and the branch's bilinear splat, which degenerates to it (E4, first
rows). Overlap (coverage > 1): inverse-variance-weighted average; this is
also where per-focus gains and drift become observable (section 2.6).

**Gridding.** Off-raster samples (a rotated or hexagonal lattice with an
isotropic step, or any mismatch between the true and the assumed step) need
an interpolation *model*. The bilinear splat with weight normalization is a
bilinear smoothing: its high-frequency transfer is `sinc²`, 0.65 at 0.35
cycles per step, and it leaves holes where the local sample density dips
(E4: 5.5% for the rotated hexagonal case). The consistent estimator is (5)
with `M_k` the sampling of a raster interpolant: minimize
`Σ_i ((B c)_i − v_i)² + λ ‖c‖²` over cubic B-spline coefficients `c`, where
`B` evaluates the spline (4 × 4 taps) at each sample position, solved by
conjugate gradients (the adjoint is the splat); the raster image is `c`
convolved with `[1 4 1] / 6` along each axis. E4 (relative RMSE, noiseless):

| lattice / scan | nearest | bilinear splat (branch) | B-spline LSQ gridding |
|---|---|---|---|
| hexagonal, commensurate (22 × 19 steps) | 0 | 0 | 0.002 |
| hexagonal, step 0.3% off | 0.26 | 0.10 | 0.019 |
| hexagonal rotated 17°, isotropic step | 0.31 (8.7% holes) | 0.18 (5.5% holes) | 0.089 |
| square 45° ("diamond"), brick domain | 0 | 0 | 0.002 |
| rectangular axis-aligned | 0 | 0 | 0.002 |

With 10% sample noise the ordering is the same (0.131 vs 0.119, 0.194 vs
0.151), and the gridder passes noise through unsmoothed, which is the
unbiased behaviour; denoising is a separate, explicit decision. Periodic
non-uniform sampling theory (Yen; Papoulis) guarantees that a band-limited
specimen is recoverable from an `L`-periodic sample set of average density
≥ 1 per raster pixel, which the fundamental-domain condition provides; the
spline is the practical interpolant.

**Deconvolution of `h_e`.** An optional last step: Richardson–Lucy
(positivity, early stopping) of the placed image with `h_e`, or the
full-model reference of section 2.1. E5b (relative RMSE against the true
specimen):

| iterations | isolated-fit image + RL(`h_e`) | joint-fit image + RL(`h_e`) | full-model RL |
|---|---|---|---|
| 5 | 0.40 | 0.38 | 0.42 |
| 10 | 0.32 | 0.25 | 0.27 |
| 20 | 0.33 | 0.16 | 0.16 |
| 40 | 0.37 | 0.13 | 0.11 |

The joint-extracted image deconvolves (line pairs at 77 nm go from 0.18 to
0.83 contrast); the isolated-fit image diverges because its noise is 3.3×
higher. Deconvolution is only viable on top of the joint extraction, and it
needs `h_e`'s FWHM (a calibration or a user input; not derivable from a
single dataset without point-like structures).

### 2.5 ISM pixel reassignment does not apply to confined foci

For Gaussian `h_e` and `h_d`, the sub-image seen through detector offset `d`
is the specimen filtered with `h_e(u) · h_d(d − u)`: a Gaussian of width
`σ_c = σ_e σ_d / √(σ_e² + σ_d²)` centred at `α d`,
`α = σ_e² / (σ_e² + σ_d²)`. Pixel reassignment shifts every sub-image by
`−α d` and sums; the resolution gain is `σ_c / σ_e`. For confocal
(`σ_e = σ_d`) this gives `α = 0.5` and a gain of `1/√2` — the textbook result
(E3a, last row). For RESOLFT foci (E3a):

| effective PSF FWHM | detection PSF FWHM | optimal α | width after reassignment / `σ_e` |
|---|---|---|---|
| 49 nm | 219 nm | 0.049 | 0.975 |
| 64 nm | 219 nm | 0.078 | 0.960 |
| 99 nm | 219 nm | 0.169 | 0.911 |
| 64 nm | 363 nm | 0.030 | 0.985 |

There is nothing to gain: the fast-Gauss amplitude (`α = 0`) already yields
the `h_e`-limited PSF (E3b: 60 nm FWHM for a 63.5 nm `h_e`; optimal α:
61 nm). The xrecon default `α = 0.5` applied to a 63 nm focus widens the
reconstruction PSF to 74 nm and lowers its peak by 16% (E3b): it moves each
detector pixel's signal by `(0.5 − α) · |d|`, up to about 80 nm for a 2 σ_d
pinhole — more than the resolution. A sweep over `ISM shift` on real data
will find its optimum near zero. Fourier-domain sub-pixel shifting, patch
mean subtraction and oversampling are then machinery without a purpose:
exact placement (section 2.4) already puts every amplitude at its exact
sample-space position.

### 2.6 Systematics the estimator does not see, and how the geometry exposes them

- **Per-focus gain `ε_f`** (illumination envelope, local OFF power) tiles the
  reconstruction with cell-sized blocks. It is not identifiable from a
  one-cell scan without an assumption about the specimen — which is why the
  branch's flat-field solver (differences of near-coincident boundary
  samples, high-passed over the lattice) helped one dataset, not the other,
  and was removed. With an **overscan** of 10–20% beyond the fundamental
  domain, neighbouring foci sample the *same* specimen points and
  `ε_f / ε_f'` is measured directly by the overlap ratios: a sparse least
  squares over the focus adjacency graph, no specimen assumption, exactly
  like tile-stitching intensity correction. Recommendation to the
  acquisition side: overscan each axis by one to two `σ_spot` worth of steps.
- **Stage-to-camera rotation and scale.** A rotation `θ` misplaces the far
  end of a cell by `w · θ`: for `w = 11 px` (0.85 µm) and `θ = 1°`, 15 nm —
  tolerable below about 1°, a visible seam at 2–3°. Calibrate once (BeadRec
  measures exactly this transform) or estimate it from overlaps.
- **Lattice accuracy.** A period error `δ` accumulates to `N δ` over `N`
  periods; for 0.4 px (30 nm) at the edge of a 464 px frame, `δ < 0.02 px`
  (0.2%). Spectral peak refinement alone lands at 0.1–0.25% (bin-limited).
  The final estimate must be a real-space linear regression of all focus
  centroids on their `(m, n)` indices — six parameters from hundreds of foci,
  about 0.01 px — whose residual map also reveals field distortion.
- **Drift** during the scan shifts `q_{k,f}` by `δ(k)`: seams at cell
  boundaries, directly measurable from overlaps when overscanning; otherwise
  only the total-variation seam criterion applies.
- **Bleaching.** The linear energy normalization (audit F4) is right for a
  global loss; a per-focus loss is a gain drift and belongs with `ε_f`.

---

## 3. Findings on the branch to fix before building on it

Numbered B1–B10; each names the code (on `feat/monalisa-fastgauss-lattice-sweep`)
and the experiment that shows it.

- **B1 — xrecon Gaussian weight is 11× too wide.**
  `ism_reassign/kernel.py::_least_square_signal_weights` divides the patch
  coordinates by `floor(p/2)` and then evaluates the Gaussian with a sigma
  expressed in patch pixels. For period 11.05 px, oversampling 2 and PSF
  FWHM 220 nm the effective sigma is 13.3 camera px instead of 1.21 (E3c);
  the Gaussian is 0.93 at the patch edge and the pseudo-inverse turns into a
  centre-positive / corner-negative curvature filter, not a matched filter.
  Either the original xrecon defines sigma in half-patch units (then the
  `PSF FWHM (nm)` parameter is mislabeled by `floor(p/2)`) or the port is
  wrong. Verify against the original before any A/B with the fast path.
- **B2 — xrecon output pitch and the "m steps span one period" assumption.**
  `_compute_geometry` reports `output_pixel_size_nm = pixel · period / p`
  (the *patch* pixel), but `_center_ulenses_and_reassign` builds the output
  as `u·m` pixels across `u` periods, i.e. its pitch is `period / m` — the
  reported size is off by `m / p` — and the reassembly is only right when the
  scan's `m` steps span exactly one period. The integrated path records
  `actual_scan_step_px_xy` and `pattern_subdivision_step_px_xy` but neither
  enforces nor uses them.
- **B3 — ISM shift default 0.5** blurs RESOLFT data (section 2.5, E3b).
- **B4 — bilinear splat** (`lattice_recon.assemble_image`) is exact on the
  raster and a blur off it, with holes (E4). Replace by B-spline
  least-squares gridding; keep the weight map as the coverage diagnostic.
- **B5 — isolated per-focus fit** (`gauss_processor.calculate_gaussian_lsq_weights`,
  `build_exact_sampling`, `extract_lattice_amplitudes`) is crosstalk-limited
  (E1). The pinhole sweep is optimizing an estimator limitation. Superseded
  by the joint operator; `Footprint mode` and `Footprint rectangles` retire.
- **B6 — spot sigma** is the hard-coded 2.0 px default or
  `PSF FWHM / (2.355 · pixel)`: 10% amplitude bias (E5a). Self-calibrate (E6).
- **B7 — lattice refinement** stops at sub-bin spectral interpolation plus
  spectral phases. Good as a detector; add the real-space centroid regression
  and a distortion residual (section 2.6).
- **B8 — two reassignment code paths** (`get_1d_indices` integer scatter for
  axis-aligned grids; scatter-and-grid for everything else). One placement
  pipeline, with the axis-aligned commensurate case as a bit-for-bit
  regression test of the placement step.
- **B9 — general path is offline-only, single line step, single Z.** The
  layout's physical coordinates (`acquisition_layout.iter_physical_coordinates`)
  give `d_k` for every frame of any loop structure, and extraction is per
  frame anyway; live is the same operator plus streaming placement.
- **B10 — `MonalisaLiveSession._resolve_localization`** still reduces the
  detection to rectangular grid parameters; the `Lattice` object
  (`LocalizationResult.lattice` already carries it) should flow through
  unchanged, and `Sampling: exact pixel` should be the only mode.

---

## 4. Clean implementation plan

### 4.1 Principles

1. **One pipeline** for live and offline, for every lattice; the axis-aligned
   commensurate rectangular scan is a special case, not a separate path.
2. **Geometry from the recording.** Scan offsets per frame come from the
   resolved acquisition layout (attributes as the fallback ladder it already
   has); the data decide the orientation (total variation over the eight `R`
   candidates), the layout cross-checks it. Lattice from detection plus
   real-space refinement; spot model from the mean frame. No hand-typed
   periods, offsets or PSF widths are *inputs*; they are *outputs* shown as
   diagnostics, with a rough period band as the only hint.
3. **Every estimator is linear and precomputed.** Per-frame work is a gather
   and a dot product; everything expensive happens once per geometry or once
   per timepoint.
4. **Diagnostics are part of the result:** lattice, spot model, coverage
   histogram, commensurability residual, orientation score, per-focus
   variance, background planes, the spot cloud.
5. **Parity before deletion.** The legacy integer path stays until the new
   placement reproduces it bit for bit on synthetic axis-aligned commensurate
   data, and the new extraction is characterized against it on rig data.

### 4.2 Modules

```
imswitch/improcess/reconstructors/monalisa/
  lattice.py        keep; add commensurate_with(pitch), refine_with_centroids(image),
                    coverage_histogram(positions, raster), fundamental-domain check
  scan_frame.py     NEW  ScanFrame: d_k for every frame from the layout (attrs fallback),
                    the 8 R candidates + optional rotation/scale, orientation by TV with
                    layout cross-check, sample positions q_{k,f} (eq. 1)
  spot_model.py     NEW  SpotModel(sigma, sigma_haze | None, ellipticity | None);
                    calibrate(mean_frame, foci) by variable projection (E6)
  extraction.py     NEW  ExtractionOperator.build(lattice, spot_model, background,
                    reach, frame_shape, weighting=None) -> per-focus (rows, cols, weights),
                    joint pseudo-inverse rows, exact-pixel; apply(frames) -> amplitudes,
                    background planes, variances; NumPy and CuPy backends (same gather)
  placement.py      NEW  OutputRaster(origin, pitch, lock); place_exact(); overlap
                    averaging; grid_bspline_lsq(positions, values, weights, lam)
  deconvolve.py     NEW (optional)  rl_deconvolve(image, h_e, iterations);
                    full_model_rl(frames, geometry, h_e, h_d, iterations) reference
  pipeline.py       NEW  reconstruct_stack(frames, scan_frame, params) -> planes +
                    diagnostics; the one entry point offline and live share
  live_session.py   REWRITE on pipeline: begin() = detect + calibrate + build operator;
                    push() = extract + place; result() = planes (grid at timepoint end)
  reconstructor.py  thin: params -> pipeline; sweep; classic path unchanged
  sweep.py, result.py (spot cloud, diagnostics), params_widget.py: updated
  pattern_finder.py thin wrapper over detection + refinement
  legacy.py, signal_extractor.py, coeffs_to_image.py: classic path, untouched
Removed after parity: gauss_processor.py (both sampling modes, shell footprint),
  scan_geometry.get_1d_indices / get_orientation / _get_bases / _get_shifts,
  lattice_recon.py, ism_reassign/ (quarantined as a reference until B1/B2 are settled)
```

### 4.3 Data flow per stack

A *stack* is the set of frames sharing one `(timepoint, condition, z)` per
the layout placement; the pipeline runs per stack, so line-step conditions,
Z slices and timepoints need no special code.

Once per geometry (first stack, or the loaded file):

1. Mean frame → `detect_lattice` (spectral, guess-free, within the period
   band) → `refine_with_centroids` (index every focus, regress centroids on
   `(m, n)`; residual map = distortion diagnostic) → `Lattice`.
2. `SpotModel.calibrate` on the mean frame at the refined foci: `σ_spot`,
   optional ellipticity, optional `σ_haze` (enabled when the tail residual
   justifies it).
3. `ScanFrame` from the layout: `d_k` per frame for each `R` candidate.
4. `ExtractionOperator.build`: for each focus the in-frame pixel set within
   the reach, the joint design `[G N]` restricted to the footprint union, the
   pseudo-inverse amplitude / background rows (interior foci share one row
   per sub-pixel phase class; edge foci get their own), the variance
   coefficients. Optional weighting from the mean frame.
5. Orientation: extract the first stack, place under each `R` candidate,
   keep the minimum total variation; cross-check against the layout's
   directions; log both.
6. `OutputRaster`: pitch = the intended lattice subdivision when the
   metadata step is within tolerance of it (lattice-locked), otherwise the
   metadata step; origin at a lattice point; coverage histogram and
   commensurability residual logged.

Per frame: gather footprints, dot with weights → amplitudes, backgrounds,
(variances). Per stack: `place_exact` if commensurate else
`grid_bspline_lsq` (weights = inverse variances); overlap averaged; optional
gain / drift refinement from overlaps; optional `rl_deconvolve`. Result: a
`MonalisaProcessingResult` whose Base axis holds amplitude, background, haze,
variance planes; the spot cloud; the diagnostics dict.

### 4.4 Phases, each with its acceptance test

- **Phase 0 — fixtures and characterization (no behaviour change).** A
  synthetic acquisition generator under `_test/` (E5's `_Synthetic`
  generalized: any `Lattice`, any scan raster, `h_e`, `h_d`, `ε_f`, haze,
  Poisson noise). Golden-output tests of the current rectangular path on a
  synthetic axis-aligned commensurate scan (byte-identical arrays) and of the
  branch's general path on the diamond case. E1–E6 turned into regression
  tests with loose tolerances.
- **Phase 1 — geometry.** `Lattice.refine_with_centroids`,
  `commensurate_with`, coverage histogram; `ScanFrame` from the layout;
  orientation search moved onto `ScanFrame`. Tests: synthetic rotated /
  hexagonal / rectangular scans; the diamond dataset's numbers (10.41 px at
  45°, 32 × 16 × 35 nm → coverage exactly one everywhere).
- **Phase 2 — spot model and extraction operator.** `SpotModel.calibrate`;
  `ExtractionOperator` with background basis, optional haze, optional
  weighting, reach; CuPy backend. Parity: with the neighbours excluded from
  the design and the same footprint, the weights equal
  `build_exact_sampling` to 1e-12. E1 reproduced as a test (joint bias
  < 1e-6 at every reach; std monotone).
- **Phase 3 — placement and gridding.** `OutputRaster`, `place_exact`,
  `grid_bspline_lsq`, variance plane. Parity: axis-aligned commensurate →
  identical to the legacy integer scatter given identical amplitudes; E4
  reproduced as a test.
- **Phase 4 — pipeline, live, deletion.** `reconstruct_stack`; live session
  rebuilt on it (calibration on the first complete stack, exact placement
  per push, gridding at timepoint completion); offline fast Gauss switched
  over; `gauss_processor`, `lattice_recon`, the `get_1d_indices` family
  deleted once the Phase 0 golden tests pass on the new placement. The
  extraction change is documented, not hidden: the A/B of Phase 5 is where
  it is judged.
- **Phase 5 — deconvolution and the reference method.** `rl_deconvolve`;
  `full_model_rl` as an offline *MoNaLISA joint deconvolution (reference)*
  method sharing the geometry and spot model; validation protocol on rig
  data: legacy vs new on the rectangular and diamond reference recordings,
  sweep over reach and background model, FRC, the ISM-shift sweep of
  section 2.5 as the empirical confirmation.
- **Phase 6 — self-calibration from overscan.** Per-focus gain, drift, `R`
  refinement from overlaps; the acquisition-side overscan recommendation
  documented in the scan dialog.
- **Phase 7 — decide the classic SignalExtractor's fate.** The joint operator
  with a haze basis reproduces its multi-base model on any OS and GPU without
  the Windows CUDA DLL; retire it once Phase 5 shows parity.

### 4.5 Performance budget

| step | cost | live? |
|---|---|---|
| extraction, 3 σ reach, 1800 foci, 464² frame | 2e5 multiply-adds per frame; about 1 ms NumPy, µs CuPy | yes |
| exact placement | one scatter per frame | yes |
| B-spline LSQ gridding, 1.8e6 samples per timepoint | ~30 CG iterations × 16 taps × 2 ≈ 2e9 flops; a few seconds in NumPy with a `bincount`-based splat (`np.add.at` is ~10× slower), < 0.1 s CuPy | at timepoint end |
| RL deconvolution of `h_e`, 1000² raster, 50 iterations | ~5 s NumPy, < 1 s CuPy | offline |
| full-model RL, 1000 frames × 512² | ~2 min per iteration NumPy, ~2 s CuPy | offline reference |

### 4.6 Widget and parameter keys

New keys: `background_model` (`constant` / `constant+haze` / `none`),
`footprint_reach_sigma`, `noise_weighting`, `spot_sigma_px` (auto or
override), `haze_sigma_px` (auto or override), `output_pitch_mode`
(`lattice-locked` / `metadata step`), `gridding_lambda`,
`deconvolution` group (`enabled`, `effective_psf_fwhm_nm`, `iterations`),
`stage_rotation_deg`, `gain_correction`, `drift_correction`,
`orientation_override`, `period_band_px`. Retired keys:
`fast_gauss_footprint_mode`, `fast_gauss_footprint_num_rects`,
`fast_gauss_pinhole_radius_sigma`, `fast_gauss_sampling_mode`,
`fast_gauss_pattern_geometry`, `ism_reassign_*`, `psf_fwhm_nm` (kept only
for the classic method), the pattern group as inputs. `sweep.py`'s registry
follows: reach, haze sigma, gridding lambda, deconvolution iterations and
`effective_psf_fwhm_nm` become the sweepable parameters.

---

## 5. Remaining input parameters

| parameter | default | where it comes from | when to touch it | sweep |
|---|---|---|---|---|
| Pixel size (nm) | metadata | camera calibration | never; required | no |
| Period band (px) | 0.35–2.5 × the last known period | widget hint | only if detection locks onto specimen structure | no |
| Background model | constant + haze (auto-enabled by residual) | data | switch off haze on thin samples; `none` for calibration beads | no |
| Footprint reach (× σ_spot) | 3 | — | 2–4; below 2.5 costs noise, above 4 costs nothing but memory | yes |
| Noise weighting | off | camera gain / offset / read noise | high-signal, low-background data (≤ 20% variance) | no |
| Spot sigma (px) | auto (calibrated) | mean frame | override for degenerate frames (few foci) | yes |
| Haze sigma (px) | auto (calibrated) | mean frame | override when the haze is non-Gaussian | yes |
| Scan orientation | auto (TV) with layout cross-check | data + layout | override when the cross-check disagrees | no |
| Stage-to-camera rotation (°) | 0 | BeadRec calibration or overlap self-calibration | when cell seams appear on straight structures | no |
| Output pitch | lattice-locked when within 1% of the metadata step | lattice + layout | `metadata step` for deliberately non-subdividing scans | no |
| Gridding regularization λ | 1e-3 | — | raise for sparse coverage (rotated lattices) | yes |
| Deconvolution | off | — | on, with `h_e` FWHM from a bead calibration; iterations 10–40 | yes |
| Per-focus gain correction | off | overlaps (needs overscan) | on for overscanned acquisitions with tiling artefacts | no |
| Drift correction | off | overlaps (needs overscan) | on for long scans | no |
| Bleaching correction | off | frame energies | on for strongly bleaching samples (linear ratio) | no |
| Method | unified lattice pipeline | — | classic (SignalExtractor) for comparison; full-model reference for validation | — |
| Device | GPU if available | — | — | — |

Everything that is *not* in this table and is on today's widget is derived
from the data or retired (section 4.6).

Two decisions belong to the maintainers rather than to the plan: whether to
keep the ISM reassignment method at all (B1–B3 argue for retiring it after
the shift sweep of Phase 5 confirms E3 on rig data), and whether to adopt the
overscan in acquisition (it is the only way per-focus gain, drift and the
stage-to-camera transform become self-calibrating; it costs 10–20% more
frames per timepoint).

---

## 6. References

- Masullo LA, Bodén A, Pennacchietti F, Coceano G, Ratz M, Testa I. Enhanced
  photon collection enables four dimensional fluorescence nanoscopy of living
  systems. *Nat Commun* 9, 3281 (2018). — MoNaLISA.
- Chmyrov A, Keller J, Grotjohann T, Ratz M, d'Este E, Jakobs S, Eggeling C,
  Hell SW. Nanoscopy with more than 100,000 'doughnuts'. *Nat Methods* 10,
  737–740 (2013). — parallelized RESOLFT.
- Bodén A, Pennacchietti F, Coceano G, Damenti M, Ratz M, Testa I. Volumetric
  live cell imaging with three-dimensional parallelized RESOLFT microscopy.
  *Nat Biotechnol* 39, 609–618 (2021).
- Sheppard CJR. Super-resolution in confocal imaging. *Optik* 80, 53–54
  (1988); Müller CB, Enderlein J. Image scanning microscopy. *Phys Rev Lett*
  104, 198101 (2010); Sheppard CJR, Mehta SB, Heintzmann R. Superresolution
  by image scanning microscopy using pixel reassignment. *Opt Lett* 38,
  2889–2892 (2013). — pixel reassignment and the shift factor.
- York AG et al. Resolution doubling in live, multicellular organisms via
  multifocal structured illumination microscopy. *Nat Methods* 9, 749–754
  (2012). — multifocal ISM with diffraction-limited foci (where α = 0.5 holds).
- Tortarolo G et al. Focus image scanning microscopy for sharp and gentle
  super-resolved microscopy. *Nat Commun* 13, 7723 (2022). — in-focus /
  out-of-focus separation by shape (the haze basis).
- Zunino A, Castello M, Vicidomini G. Reconstructing the image scanning
  microscopy dataset: an inverse problem. *Inverse Problems* 39, 064004
  (2023). — multi-image deconvolution as the ISM optimum.
- Huang F et al. Video-rate nanoscopy using sCMOS camera-specific
  single-molecule localization algorithms. *Nat Methods* 10, 653–658 (2013).
  — the `μ + σ_r²` variance model.
- Yen JL. On nonuniform sampling of bandwidth-limited signals. *IRE Trans
  Circuit Theory* 3, 251–257 (1956); Papoulis A. Generalized sampling
  expansion. *IEEE Trans Circuits Syst* 24, 652–654 (1977). — periodic
  non-uniform sampling.
- Unser M. Splines: a perfect fit for signal and image processing. *IEEE
  Signal Process Mag* 16, 22–38 (1999). — B-spline interpolation.
- Richardson WH. Bayesian-based iterative method of image restoration. *J Opt
  Soc Am* 62, 55–59 (1972); Lucy LB. An iterative technique for the
  rectification of observed distributions. *Astron J* 79, 745 (1974).

---

## Appendix A — notation

| symbol | meaning |
|---|---|
| `L`, `a1`, `a2`, `o` | illumination lattice, basis vectors, offset (camera px) |
| `F`, `r_f` | foci inside the frame, their positions |
| `s_k`, `d_k`, `R` | stage displacement of frame `k`, its camera-space image, stage-to-camera transform |
| `q_{k,f} = r_f − d_k` | sample-space position probed by focus `f` in frame `k` |
| `P = F − D` | the sampling set; `L`-periodic |
| `h_e`, `σ_e` | RESOLFT effective PSF and its sigma |
| `h_d`, `σ_d` | detection PSF and its sigma |
| `g`, `σ_spot` | camera spot shape `h_d ⊗ h_e ⊗ pixel`, `σ_spot² = σ_d² + σ_e²` |
| `A_{k,f}`, `ε_f` | spot amplitude, per-focus gain |
| `G`, `N`, `W` | spot-shape design matrix, background basis, extraction weights |
| `α = σ_e² / (σ_e² + σ_d²)` | ISM shift factor |
| `Δx, Δy` | scan step = output raster pitch (camera px) |

## Appendix B — experiments to tests

| experiment | claim | regression test (Phase 0) |
|---|---|---|
| E1 | joint fit: zero crosstalk bias at every reach; std floor at 3 σ; haze basis removes haze bias | `test_extraction_operator.py` |
| E2 | weighted fit variance ratio ≤ 1.25 in the tested regimes | `test_extraction_operator.py` (weighting) |
| E3a | reassignment shift `α = σ_e² / (σ_e² + σ_d²)`; width gain ≤ 4% for `σ_e / σ_d ≤ 0.3` | `test_ism_theory.py` |
| E3c | xrecon effective sigma = `sigma_px · floor(p/2)` | guard test on the kernel until B1 is settled |
| E4 | exact placement when commensurate; B-spline LSQ ≤ 0.02 for a 0.3% step mismatch | `test_placement.py` |
| E5 | joint extraction ≤ 1/3 the RMSE of the isolated fit; two-stage + RL within 25% of full-model RL | `test_pipeline_synthetic.py` (slow marker) |
| E6 | spot sigma recovered within 1% | `test_spot_model.py` |
