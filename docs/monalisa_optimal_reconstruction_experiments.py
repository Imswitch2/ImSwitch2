"""Numerical experiments behind ``docs/monalisa_optimal_reconstruction.md``.

Standalone: NumPy + SciPy only, no ImSwitch imports, so the numbers in the
design document can be regenerated anywhere::

    python docs/monalisa_optimal_reconstruction_experiments.py            # all
    python docs/monalisa_optimal_reconstruction_experiments.py e1 e3      # some

Every experiment builds its own synthetic MoNaLISA data (Gaussian foci on a
known Bravais lattice, known amplitudes/backgrounds, controlled noise) and
prints one or more Markdown tables. Lengths are in camera pixels unless a
column says otherwise; the ``77 nm`` camera pixel of the MONALISA setups is
used wherever a physical scale is quoted.

The experiments:

* ``e1`` -- per-frame amplitude extraction: isolated per-focus least squares
  (the fast-Gauss estimator) against the joint all-foci least squares, with
  and without an out-of-focus haze term. Crosstalk bias and noise versus the
  footprint radius.
* ``e2`` -- how much a variance-weighted (Poisson + read noise) fit gains over
  the unweighted fit, across signal and background levels.
* ``e3`` -- image-scanning-microscopy pixel reassignment for RESOLFT-confined
  foci: the optimal shift factor, the resolution it buys, what a confocal
  shift of 0.5 does to a confined focus, and the effective width of the
  Gaussian the branch's xrecon kernel fits.
* ``e4`` -- reassignment for hexagonal / rotated lattices: nearest placement,
  bilinear splatting and least-squares gridding on commensurate and
  non-commensurate scan rasters.
* ``e5`` -- the whole pipeline on one synthetic acquisition: two-stage
  (extraction + placement, optionally deconvolved) against the full-model
  multi-frame maximum-likelihood (Richardson-Lucy) estimate.
* ``e6`` -- spot-shape self-calibration from the data.
"""

from __future__ import annotations

import sys
import time

import numpy as np
from numpy.fft import fft2, ifft2
from scipy.optimize import minimize_scalar
from scipy.sparse.linalg import LinearOperator, cg

RNG = np.random.default_rng(20260928)
PIXEL_NM = 77.0


# ----------------------------------------------------------------- helpers
def gaussian(dx, dy, sigma):
    return np.exp(-(dx * dx + dy * dy) / (2.0 * sigma * sigma))


def lattice_points(a1, a2, offset, shape, margin=0.0):
    """Points ``offset + m a1 + n a2`` with ``-margin <= x < cols + margin``."""
    rows, cols = shape
    basis = np.array([[a1[0], a2[0]], [a1[1], a2[1]]], float)
    inv = np.linalg.inv(basis)
    offset = np.asarray(offset, float)
    corners = np.array(
        [[-margin, -margin], [cols + margin, -margin],
         [-margin, rows + margin], [cols + margin, rows + margin]], float
    )
    idx = (inv @ (corners - offset).T).T
    lo = np.floor(idx.min(axis=0)).astype(int) - 1
    hi = np.ceil(idx.max(axis=0)).astype(int) + 1
    mm, nn = np.meshgrid(np.arange(lo[0], hi[0] + 1), np.arange(lo[1], hi[1] + 1))
    pts = offset + mm.reshape(-1, 1) * basis[:, 0] + nn.reshape(-1, 1) * basis[:, 1]
    keep = (
        (pts[:, 0] >= -margin) & (pts[:, 0] < cols + margin)
        & (pts[:, 1] >= -margin) & (pts[:, 1] < rows + margin)
    )
    return pts[keep]


def fwhm_of_profile(x, y):
    """Full width at half maximum of a sampled peak (linear interpolation)."""
    y = np.asarray(y, float)
    imax = int(np.argmax(y))
    half = 0.5 * y[imax]
    left = imax
    while left > 0 and y[left] > half:
        left -= 1
    right = imax
    while right < y.size - 1 and y[right] > half:
        right += 1
    if left == imax or right == imax:
        return float("nan")
    xl = np.interp(half, [y[left], y[left + 1]], [x[left], x[left + 1]])
    xr = np.interp(half, [y[right], y[right - 1]], [x[right], x[right - 1]])
    return float(xr - xl)


def md_table(header, rows, fmt=None):
    print("| " + " | ".join(header) + " |")
    print("|" + "|".join(["---"] * len(header)) + "|")
    for row in rows:
        cells = []
        for value in row:
            if isinstance(value, float):
                cells.append(f"{value:.3g}" if fmt is None else fmt(value))
            else:
                cells.append(str(value))
        print("| " + " | ".join(cells) + " |")
    print()


# ================================================================ E1
def e1_extraction():
    """Isolated vs joint least-squares amplitude extraction on a lattice frame."""
    print("## E1 -- amplitude extraction: isolated vs joint least squares\n")
    period, sigma = 11.05, 2.0
    shape = (100, 100)
    pts = lattice_points((period, 0.0), (0.0, period), (3.3, 5.1), shape, margin=3 * sigma)
    n = len(pts)
    yy, xx = np.mgrid[0:shape[0], 0:shape[1]].astype(float)
    X, Y = xx.ravel(), yy.ravel()
    dist2 = (X[None, :] - pts[:, :1]) ** 2 + (Y[None, :] - pts[:, 1:]) ** 2
    G = np.exp(-dist2 / (2 * sigma**2))                     # (n, npix) spot shapes
    nearest = np.argmin(dist2, axis=0)                       # Voronoi owner of each pixel
    sigma_haze = 6.0
    Hz = np.exp(-dist2 / (2 * sigma_haze**2))                # out-of-focus haze shapes
    A_true = 200.0 * (0.5 + RNG.random(n))
    H_true = 60.0 * (0.5 + RNG.random(n))
    frame_const = G.T @ A_true + 50.0
    frame_haze = frame_const + Hz.T @ H_true
    noise_sd = 5.0
    interior = np.array([
        f for f in range(n)
        if period + 2 < pts[f, 0] < shape[1] - period - 2
        and period + 2 < pts[f, 1] < shape[0] - period - 2
    ])
    print(f"Lattice period {period} px, sigma {sigma} px, {n} foci "
          f"({interior.size} interior), amplitudes 100-300, constant background 50, "
          f"haze sigma {sigma_haze} px amplitude 30-90, noise sd {noise_sd}.\n")

    rows = []
    for r_sigma in (1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.5):
        r = r_sigma * sigma
        inside = dist2 <= r * r
        # ---- isolated per-focus fit (fast Gauss): amplitude + constant
        iso_bias_c, iso_bias_h, iso_std = [], [], []
        for f in interior:
            idx = inside[f]
            D = np.stack([G[f, idx], np.ones(idx.sum())], axis=1)
            w = np.linalg.pinv(D)[0]
            iso_bias_c.append(w @ frame_const[idx] - A_true[f])
            iso_bias_h.append(w @ frame_haze[idx] - A_true[f])
            iso_std.append(noise_sd * np.linalg.norm(w))
        # ---- joint fits over the union of footprints
        mask = inside.any(axis=0)
        consts = np.stack([(nearest == f) & mask for f in range(n)]).astype(float)
        D_c = np.concatenate([G[:, mask], consts[:, mask]], axis=0).T
        W_c = np.linalg.pinv(D_c)[:n]
        D_ch = np.concatenate([G[:, mask], Hz[:, mask], consts[:, mask]], axis=0).T
        W_ch = np.linalg.pinv(D_ch)[:n]
        jc_bias_c = (W_c @ frame_const[mask] - A_true)[interior]
        jc_bias_h = (W_c @ frame_haze[mask] - A_true)[interior]
        jc_std = noise_sd * np.linalg.norm(W_c, axis=1)[interior]
        jch_bias_h = (W_ch @ frame_haze[mask] - A_true)[interior]
        jch_std = noise_sd * np.linalg.norm(W_ch, axis=1)[interior]
        rows.append((
            f"{r_sigma:.1f} sigma", int(inside[interior[0]].sum()),
            float(np.max(np.abs(iso_bias_c))), float(np.mean(iso_std)),
            float(np.max(np.abs(jc_bias_c))), float(np.mean(jc_std)),
            float(np.sqrt(np.mean(np.square(iso_bias_h)))),
            float(np.sqrt(np.mean(np.square(jc_bias_h)))),
            float(np.sqrt(np.mean(np.square(jch_bias_h)))), float(np.mean(jch_std)),
        ))
    md_table(
        ["footprint r", "px/focus", "ISO max|bias| (const bg)", "ISO std",
         "JOINT max|bias| (const bg)", "JOINT std",
         "ISO rms bias (haze)", "JOINT-const rms bias (haze)",
         "JOINT+haze rms bias", "JOINT+haze std"],
        rows,
    )
    print("ISO = per-focus Gaussian+constant fit on the focus' own disc (the fast-Gauss "
          "estimator). JOINT = all foci fitted together on the union of discs, one "
          "constant per Voronoi cell. JOINT+haze adds one wide Gaussian per focus.\n")


# ================================================================ E2
def e2_noise_weighting():
    """Efficiency of unweighted vs variance-weighted amplitude fits."""
    print("## E2 -- noise weighting: unweighted vs variance-weighted least squares\n")
    sigma, read_noise = 2.0, 1.6
    r = 2.0 * sigma
    yy, xx = np.mgrid[-4:5, -4:5].astype(float)
    inside = (xx**2 + yy**2) <= r * r
    g = gaussian(xx[inside], yy[inside], sigma)
    D = np.stack([g, np.ones(g.size)], axis=1)
    w_unw = np.linalg.pinv(D)[0]
    rows = []
    for amp in (20.0, 50.0, 200.0, 1000.0):
        for bg in (2.0, 20.0, 100.0):
            mu = amp * g + bg
            var = mu + read_noise**2                       # Poisson + Gaussian read noise
            var_unw = float(np.sum(w_unw**2 * var))
            Wd = D * (1.0 / var)[:, None]
            w_w = np.linalg.solve(D.T @ Wd, Wd.T)[0]          # (D'WD)^-1 D'W
            var_w = float(np.sum(w_w**2 * var))            # == CRLB for this model
            rows.append((amp, bg, np.sqrt(var_unw), np.sqrt(var_w), var_unw / var_w))
    md_table(["peak amplitude", "background", "std unweighted", "std weighted (=CRLB)",
              "variance ratio"], rows)
    print(f"Single focus, sigma {sigma} px, footprint {r:g} px ({int(inside.sum())} px), "
          f"read noise {read_noise} e-. The weighted fit uses the true per-pixel "
          "variance (oracle weights); in practice they come from a model fit or a "
          "smoothed frame, so the gain is an upper bound.\n")


# ================================================================ E3
def e3_ism_shift():
    """ISM pixel reassignment for RESOLFT-confined foci."""
    print("## E3 -- ISM pixel reassignment with a RESOLFT-confined focus\n")
    print("### E3a -- optimal shift factor and resolution gain (Gaussian model)\n")
    rows = []
    for sigma_e_nm, sigma_d_nm in ((21.0, 93.0), (27.0, 93.0), (42.0, 93.0),
                                   (27.0, 154.0), (93.0, 93.0)):
        se, sd = sigma_e_nm, sigma_d_nm
        alpha = se**2 / (se**2 + sd**2)
        # numeric check of the centroid slope: sub-image from detector offset d
        u = np.linspace(-600, 600, 24001)
        d = 60.0
        j = np.exp(-u**2 / (2 * se**2)) * np.exp(-(u + d) ** 2 / (2 * sd**2))
        centroid = float(np.sum(u * j) / np.sum(j))
        sigma_c = se * sd / np.sqrt(se**2 + sd**2)
        rows.append((f"{2.355 * se:.0f}", f"{2.355 * sd:.0f}", alpha, -centroid / d,
                     sigma_c / se, 2.355 * sigma_c))
    md_table(["effective PSF FWHM (nm)", "detection PSF FWHM (nm)",
              "alpha = se^2/(se^2+sd^2)", "measured -centroid/d",
              "width after optimal reassignment / se", "reassigned FWHM (nm)"], rows)

    print("### E3b -- reconstruction PSF of a point emitter vs the shift factor used\n")
    rows = []
    step = 0.02
    xs = np.arange(-4.0, 4.0 + step / 2, step)
    XX, YY = np.meshgrid(xs, xs)
    for sigma_e, sigma_d, label in ((0.35, 1.21, "RESOLFT 63/220 nm"),
                                    (0.55, 1.21, "RESOLFT 100/220 nm"),
                                    (1.21, 1.21, "confocal-like 220/220 nm")):
        alpha_opt = sigma_e**2 / (sigma_e**2 + sigma_d**2)
        r_pin = 2.0 * sigma_d
        dd = np.arange(-int(np.ceil(r_pin)), int(np.ceil(r_pin)) + 1)
        DX, DY = np.meshgrid(dd, dd)
        keep = (DX**2 + DY**2) <= r_pin**2
        DX, DY = DX[keep].astype(float), DY[keep].astype(float)
        D = np.stack([gaussian(DX, DY, sigma_d), np.ones(DX.size)], axis=1)
        w = np.linalg.pinv(D)[0]
        ref = None
        for alpha in sorted({0.0, round(alpha_opt, 3), 0.5}):
            R = np.zeros_like(XX)
            for wd, dx, dy in zip(w, DX, DY):
                R += wd * gaussian(XX - alpha * dx, YY - alpha * dy, sigma_e) \
                    * gaussian(XX + (1 - alpha) * dx, YY + (1 - alpha) * dy, sigma_d)
            mid = xs.size // 2
            fw = fwhm_of_profile(xs, R[mid])
            energy = np.abs(R)
            rr = np.hypot(XX, YY).ravel()
            order = np.argsort(rr)
            cum = np.cumsum(energy.ravel()[order])
            r50 = float(rr[order][np.searchsorted(cum, 0.5 * cum[-1])])
            peak = float(R.max())
            if ref is None:
                ref = peak
            rows.append((label, f"{alpha:.3f}", fw * PIXEL_NM, r50 * PIXEL_NM, peak / ref))
        rows.append((label, "(no ISM: FWHM of h_e)", 2.355 * sigma_e * PIXEL_NM, "", ""))
    md_table(["focus / PSF", "shift used", "recon PSF FWHM (nm)",
              "half-energy radius (nm)", "peak vs shift 0"], rows)
    print("Gaussian+constant least-squares weights over a 2-sigma_d pinhole (exactly the "
          "xrecon weighting family); the sub-image of detector offset d is shifted by "
          "alpha*d toward the focus, then summed.\n")

    print("### E3c -- effective width of the Gaussian in the branch's xrecon weights\n")
    period, oversampling, psf_fwhm_nm = 11.05, 2.0, 220.0
    p = int(np.ceil(period * oversampling))
    p = p + int(np.mod(p + 1, 2))
    patch_px_nm = PIXEL_NM * period / p
    sigma_px = psf_fwhm_nm / (2.355 * patch_px_nm)          # what the kernel computes
    x = (np.arange(p, dtype=float) + 1.0) - 0.5 - p / 2.0
    X, Y = np.meshgrid(x, x)
    scale = np.floor(p / 2.0)
    Xn, Yn = X / scale, Y / scale                            # the kernel's normalization
    gaussian_used = np.exp(-(Xn**2 + Yn**2) / (2 * sigma_px**2))
    model = np.stack((gaussian_used, np.ones((p, p)))).reshape(2, p * p)
    weights = np.linalg.pinv(model).T.reshape(2, p, p)[0]
    effective_sigma_patch = sigma_px * scale
    effective_sigma_camera = effective_sigma_patch * period / p
    intended_sigma_camera = psf_fwhm_nm / (2.355 * PIXEL_NM)
    c = p // 2
    md_table(
        ["quantity", "value"],
        [("patch size p (px)", p), ("patch pixel (nm)", patch_px_nm),
         ("sigma_px the kernel computes (patch px)", sigma_px),
         ("normalization divisor floor(p/2)", scale),
         ("effective Gaussian sigma (camera px)", effective_sigma_camera),
         ("intended PSF sigma (camera px)", intended_sigma_camera),
         ("ratio effective / intended", effective_sigma_camera / intended_sigma_camera),
         ("Gaussian value at patch edge (should be ~0 for a matched filter)",
          float(gaussian_used[c, 0])),
         ("weight at patch center", float(weights[c, c])),
         ("weight at patch edge (mid-side)", float(weights[c, 0])),
         ("weight at patch corner", float(weights[0, 0]))],
    )
    print("`_least_square_signal_weights` divides the patch coordinates by floor(p/2) "
          "before evaluating the Gaussian with a sigma expressed in patch pixels.\n")


# ================================================================ E4
def _bandlimited_field(kmax=0.35, terms=60, seed=3):
    rng = np.random.default_rng(seed)
    k = rng.uniform(0, kmax, terms) * np.exp(1j * rng.uniform(0, 2 * np.pi, terms))
    kx, ky = k.real, k.imag
    amp = rng.normal(size=terms) / np.sqrt(terms)
    phase = rng.uniform(0, 2 * np.pi, terms)

    def field(x, y):
        x = np.asarray(x, float)[..., None]
        y = np.asarray(y, float)[..., None]
        return np.sum(amp * np.cos(2 * np.pi * (kx * x + ky * y) + phase), axis=-1)

    return field


def _bilinear_ops(gx, gy, rows, cols):
    """Interpolation (B) and splat (B^T) for sample positions on a raster."""
    x0 = np.floor(gx).astype(int)
    y0 = np.floor(gy).astype(int)
    fx, fy = gx - x0, gy - y0
    taps = []
    for dy in (0, 1):
        for dx in (0, 1):
            px, py = x0 + dx, y0 + dy
            wgt = (fx if dx else 1 - fx) * (fy if dy else 1 - fy)
            ok = (px >= 0) & (px < cols) & (py >= 0) & (py < rows)
            taps.append((px[ok], py[ok], wgt[ok], ok))

    def interp(img):
        out = np.zeros(gx.size)
        for px, py, wgt, ok in taps:
            out[ok] += wgt * img[py, px]
        return out

    def splat(values):
        acc = np.zeros((rows, cols))
        for px, py, wgt, ok in taps:
            np.add.at(acc, (py, px), wgt * values[ok])
        return acc

    return interp, splat


def _bspline3(t):
    """Cubic B-spline basis, support (-2, 2)."""
    t = np.abs(np.asarray(t, float))
    out = np.zeros_like(t)
    inner = t < 1
    out[inner] = 2.0 / 3.0 - t[inner] ** 2 + t[inner] ** 3 / 2.0
    outer = (t >= 1) & (t < 2)
    out[outer] = (2.0 - t[outer]) ** 3 / 6.0
    return out


def _bspline_ops(gx, gy, rows, cols):
    """Evaluate (A) / splat (A^T) cubic B-spline coefficients at sample positions."""
    x0 = np.floor(gx).astype(int)
    y0 = np.floor(gy).astype(int)
    taps = []
    for dy in (-1, 0, 1, 2):
        for dx in (-1, 0, 1, 2):
            px, py = x0 + dx, y0 + dy
            wgt = _bspline3(gx - px) * _bspline3(gy - py)
            ok = (px >= 0) & (px < cols) & (py >= 0) & (py < rows)
            taps.append((px[ok], py[ok], wgt[ok], ok))

    def evaluate(coef):
        out = np.zeros(gx.size)
        for px, py, wgt, ok in taps:
            out[ok] += wgt * coef[py, px]
        return out

    def splat(values):
        acc = np.zeros((rows, cols))
        for px, py, wgt, ok in taps:
            np.add.at(acc, (py, px), wgt * values[ok])
        return acc

    return evaluate, splat


def _bspline_lsq_grid(gx, gy, values, rows, cols, lam=1e-3):
    """Least-squares cubic B-spline gridding: raster values of the spline whose
    samples at the scattered positions best match ``values``."""
    evaluate, splat = _bspline_ops(gx, gy, rows, cols)

    def matvec(v):
        coef = v.reshape(rows, cols)
        return (splat(evaluate(coef)) + lam * coef).ravel()

    op = LinearOperator((rows * cols, rows * cols), matvec=matvec, dtype=float)
    rhs = splat(values).ravel()
    coef, _ = cg(op, rhs, x0=rhs, rtol=1e-9, maxiter=500)
    coef = coef.reshape(rows, cols)
    kernel = np.array([1.0, 4.0, 1.0]) / 6.0
    img = coef.copy()
    img = kernel[0] * np.roll(img, 1, 0) + kernel[1] * img + kernel[2] * np.roll(img, -1, 0)
    img = kernel[0] * np.roll(img, 1, 1) + kernel[1] * img + kernel[2] * np.roll(img, -1, 1)
    return img


def _hf_transfer(est, truth, kmin=0.25, kmax=0.35):
    window = np.outer(np.hanning(est.shape[0]), np.hanning(est.shape[1]))
    E = np.abs(fft2((est - est.mean()) * window))
    T = np.abs(fft2((truth - truth.mean()) * window))
    ky = np.fft.fftfreq(est.shape[0])[:, None]
    kx = np.fft.fftfreq(est.shape[1])[None, :]
    band = (np.hypot(kx, ky) >= kmin) & (np.hypot(kx, ky) <= kmax)
    return float(E[band].sum() / T[band].sum())


def e4_gridding():
    """Reassignment onto a raster for general lattices."""
    print("## E4 -- reassignment (gridding) for hexagonal and rotated lattices\n")
    field = _bandlimited_field()
    a = 11.0
    hex_area = a * a * np.sqrt(3) / 2
    cases = []
    # commensurate hexagonal: a1 along x, 22 x 19 steps
    n1, n2 = 22, 19
    cases.append(("hexagonal 0 deg, commensurate", (a, 0.0),
                  (a / 2, a * np.sqrt(3) / 2), n1, n2, (a / n1, a * np.sqrt(3) / 2 / n2)))
    # slightly off step (real-world): dy = 0.5 instead of 0.50138
    cases.append(("hexagonal 0 deg, step 0.3% off", (a, 0.0),
                  (a / 2, a * np.sqrt(3) / 2), n1, n2, (a / n1, 0.5)))
    # rotated hexagonal, isotropic step of the right area: non-commensurate
    th = np.radians(17.0)
    d_iso = np.sqrt(hex_area / (n1 * n2))
    cases.append(("hexagonal 17 deg, non-commensurate",
                  (a * np.cos(th), a * np.sin(th)),
                  (a * np.cos(th + np.pi / 3), a * np.sin(th + np.pi / 3)),
                  n1, n2, (d_iso, d_iso)))
    # diamond (45 deg square) with the brick fundamental domain: commensurate
    s = 10.41
    cases.append(("square 45 deg (diamond), brick domain", (s / np.sqrt(2), s / np.sqrt(2)),
                  (-s / np.sqrt(2), s / np.sqrt(2)), 32, 16,
                  (s * np.sqrt(2) / 32, s / np.sqrt(2) / 16)))
    # axis-aligned rectangular (the legacy path)
    cases.append(("rectangular axis-aligned", (11.05, 0.0), (0.0, 11.05), 22, 22,
                  (11.05 / 22, 11.05 / 22)))

    shape = (110, 110)
    rows = []
    for name, a1, a2, nx, ny, (dx, dy) in cases:
        foci = lattice_points(a1, a2, (2.2, 3.7), shape, margin=max(nx * dx, ny * dy))
        offs = np.array([(i * dx, j * dy) for j in range(ny) for i in range(nx)])
        pos = (foci[None, :, :] + offs[:, None, :]).reshape(-1, 2)
        cell_area = abs(a1[0] * a2[1] - a1[1] * a2[0])
        ratio = nx * dx * ny * dy / cell_area
        # commensurability: lattice vectors in units of the step raster
        frac = np.array([a1[0] / dx, a1[1] / dy, a2[0] / dx, a2[1] / dy])
        commensurate = float(np.max(np.abs(frac - np.round(frac))))
        rows_out, cols_out = int(shape[0] / dy), int(shape[1] / dx)
        origin = np.array([2.2, 3.7])                    # a lattice point: raster aligned to P
        gx, gy = (pos[:, 0] - origin[0]) / dx, (pos[:, 1] - origin[1]) / dy
        inside = (gx >= -2) & (gx < cols_out + 1) & (gy >= -2) & (gy < rows_out + 1)
        gx, gy = gx[inside], gy[inside]
        RY, RX = np.mgrid[0:rows_out, 0:cols_out].astype(float)
        truth = field(RX, RY)
        values = field(gx, gy)
        rms = float(np.sqrt(np.mean(truth**2)))
        b = 12
        sl = (slice(b, rows_out - b), slice(b, cols_out - b))
        interp, splat = _bilinear_ops(gx, gy, rows_out, cols_out)
        wsum = splat(np.ones(gx.size))
        covered = wsum >= 0.1
        for noise_sd in (0.0, 0.1 * rms):
            v = values + noise_sd * RNG.normal(size=values.size)
            nearest = np.full((rows_out, cols_out), np.nan)
            ix, iy = np.round(gx).astype(int), np.round(gy).astype(int)
            ok = (ix >= 0) & (ix < cols_out) & (iy >= 0) & (iy < rows_out)
            nearest[iy[ok], ix[ok]] = v[ok]
            with np.errstate(invalid="ignore", divide="ignore"):
                bilinear = np.where(covered, splat(v) / wsum, np.nan)
            bspline = np.where(covered, _bspline_lsq_grid(gx, gy, v, rows_out, cols_out), np.nan)
            for method, est in (("nearest", nearest), ("bilinear splat (branch)", bilinear),
                                ("cubic B-spline LSQ gridding", bspline)):
                e = est[sl]
                t = truth[sl]
                finite = np.isfinite(e)
                err = float(np.sqrt(np.mean((e[finite] - t[finite]) ** 2))) / rms
                holes = 1.0 - finite.mean()
                hf = _hf_transfer(np.where(finite, e, t), t)
                rows.append((name, f"{ratio:.3f}", f"{commensurate:.3f}",
                             f"{noise_sd / rms:.0%}", method, err, holes, hf))
    md_table(["lattice / scan", "scan/cell area", "max frac. residual (0 = commensurate)",
              "sample noise / rms", "method", "rel. RMSE", "holes", "HF transfer 0.25-0.35"],
             rows)
    print("Band-limited random specimen (|k| <= 0.35 cycles/step) sampled at "
          "focus + scan offset; output raster pitch = scan step; errors over the "
          "interior. HF transfer = spectral energy ratio estimate/truth in the top band.\n")


# ================================================================ E5
class _Synthetic:
    """One synthetic MoNaLISA acquisition on a periodic sample raster."""

    def __init__(self, N=120, period=24, sigma_e=0.7, sigma_d=2.5, bg=10.0):
        self.N, self.period, self.sigma_e, self.sigma_d, self.bg = N, period, sigma_e, sigma_d, bg
        yy, xx = np.mgrid[0:N, 0:N].astype(float)
        c = N // 2
        self.he = np.roll(gaussian(xx - c, yy - c, sigma_e), (-c, -c), (0, 1))
        self.hd = np.roll(gaussian(xx - c, yy - c, sigma_d), (-c, -c), (0, 1))
        self.HE, self.HD = fft2(self.he), fft2(self.hd)
        # foci on the raster: (7 + 24 m, 9 + 24 n)
        self.foci = np.array([(7 + period * m, 9 + period * n)
                              for n in range(N // period) for m in range(N // period)])
        E0 = np.zeros((N, N))
        for fx, fy in self.foci:
            E0 += np.roll(np.roll(self.he, fy, 0), fx, 1)
        self.E0 = E0
        self.shifts = [(i, j) for j in range(period) for i in range(period)]

    def conv(self, img, H):
        return np.real(ifft2(fft2(img) * H))

    def forward(self, S):
        frames = np.empty((len(self.shifts), self.N // 2, self.N // 2))
        for k, (i, j) in enumerate(self.shifts):
            Sk = np.roll(np.roll(S, j, 0), i, 1)              # sample moved by +s_k
            frames[k] = self.conv(Sk * self.E0, self.HD)[1::2, 1::2]
        return frames

    def adjoint(self, frames):
        out = np.zeros((self.N, self.N))
        for k, (i, j) in enumerate(self.shifts):
            up = np.zeros((self.N, self.N))
            up[1::2, 1::2] = frames[k]
            back = self.conv(up, np.conj(self.HD)) * self.E0
            out += np.roll(np.roll(back, -j, 0), -i, 1)
        return out

    def place(self, amplitudes):
        """Two-stage placement: amplitude (k, f) -> sample position r_f - s_k."""
        img = np.zeros((self.N, self.N))
        for k, (i, j) in enumerate(self.shifts):
            for f, (fx, fy) in enumerate(self.foci):
                img[(fy - j) % self.N, (fx - i) % self.N] = amplitudes[k, f]
        return img


def _make_sample(N):
    S = np.zeros((N, N))
    # line pairs (vertical lines, 1 raster px wide) at separations 2..6 raster px
    seps = (2, 3, 4, 5, 6)
    x = 8
    pairs = []
    for sep in seps:
        S[10:52, x] = 100.0
        S[10:52, x + sep] = 100.0
        pairs.append((sep, x))
        x += sep + 12
    # isolated points
    pts = [(70, 20), (85, 40), (100, 25), (75, 95), (95, 80)]
    for py, px in pts:
        S[py, px] = 400.0
    # smooth blob
    yy, xx = np.mgrid[0:N, 0:N].astype(float)
    S += 40.0 * gaussian(xx - 40, yy - 85, 12.0)
    return S, pairs, pts


def _line_pair_contrast(img, pairs):
    out = []
    for sep, x in pairs:
        profile = img[14:48, x - 3:x + sep + 4].mean(axis=0)
        p1, p2 = profile[3], profile[3 + sep]
        valley = profile[3:4 + sep].min()
        peak = 0.5 * (p1 + p2)
        out.append((peak - valley) / (peak + valley) if peak + valley > 0 else 0.0)
    return out


def _rl(data, forward, adjoint, iters, init, bg=0.0, eps=1e-9):
    S = init.copy()
    norm = adjoint(np.ones_like(data))
    for _ in range(iters):
        model = forward(S) + bg
        S *= adjoint(data / np.maximum(model, eps)) / np.maximum(norm, eps)
        yield S


def e5_two_stage_vs_ml(iters=40):
    """Two-stage extraction+placement vs full-model Richardson-Lucy."""
    print("## E5 -- two-stage pipeline vs full-model maximum likelihood\n")
    syn = _Synthetic()
    N = syn.N
    S_true, pairs, _ = _make_sample(N)
    target = syn.conv(S_true, syn.HE)              # what the two-stage image estimates
    clean = syn.forward(S_true)
    frames = RNG.poisson(clean + syn.bg).astype(float)
    sigma_e_nm = syn.sigma_e * PIXEL_NM / 2
    print(f"Sample raster {N}x{N} at {PIXEL_NM / 2:g} nm, camera {N // 2}x{N // 2} px, "
          f"lattice period {syn.period / 2:g} px, effective PSF sigma "
          f"{sigma_e_nm:.0f} nm (FWHM {2.355 * sigma_e_nm:.0f} nm), "
          f"detection sigma {syn.sigma_d * PIXEL_NM / 2:.0f} nm, {len(syn.shifts)} frames, "
          f"Poisson noise, background {syn.bg:g} counts, spot peaks ~300 counts.\n")

    # ---- extraction designs in camera coordinates
    cam = N // 2
    yy, xx = np.mgrid[0:cam, 0:cam].astype(float)
    Xc, Yc = xx.ravel(), yy.ravel()
    foci_cam = (syn.foci - 1.0) / 2.0
    dist2 = (Xc[None, :] - foci_cam[:, :1]) ** 2 + (Yc[None, :] - foci_cam[:, 1:]) ** 2
    sigma_spot_true = np.sqrt(syn.sigma_d**2 + syn.sigma_e**2) / 2.0
    # self-calibrated spot sigma (E6 procedure) on the mean frame
    sigma_cal = e6_spot_calibration(frames.mean(axis=0), foci_cam, verbose=False)
    print(f"Spot sigma: true {sigma_spot_true:.3f} px, nominal detection-only "
          f"{syn.sigma_d / 2:.3f} px, self-calibrated {sigma_cal:.3f} px.\n")
    flat = frames.reshape(frames.shape[0], -1)

    def joint_extract(sigma_model):
        G = np.exp(-dist2 / (2 * sigma_model**2))
        D = np.concatenate([G, np.ones((1, G.shape[1]))], axis=0).T
        W = np.linalg.pinv(D)[:-1]
        return (W @ flat.T).T                      # (frames, foci)

    def joint_extract_weighted(sigma_model, read_noise=1.6):
        """Two-pass: unweighted fit -> per-pixel variance model -> weighted fit."""
        G = np.exp(-dist2 / (2 * sigma_model**2))
        D = np.concatenate([G, np.ones((1, G.shape[1]))], axis=0).T
        coef0 = np.linalg.pinv(D) @ flat.T                # (foci+1, frames)
        out = np.empty((flat.shape[0], len(foci_cam)))
        for k in range(flat.shape[0]):
            mu = np.maximum(D @ coef0[:, k] + syn.bg, 1.0)
            wgt = 1.0 / (mu + read_noise**2)
            Dw = D * wgt[:, None]
            coef = np.linalg.solve(D.T @ Dw, Dw.T @ flat[k])
            out[k] = coef[:-1]
        return out

    def iso_extract(sigma_model, r_sigma=1.5):
        r = r_sigma * sigma_model
        out = np.empty((flat.shape[0], len(foci_cam)))
        for f in range(len(foci_cam)):
            idx = dist2[f] <= r * r
            D = np.stack([np.exp(-dist2[f, idx] / (2 * sigma_model**2)), np.ones(idx.sum())], 1)
            w = np.linalg.pinv(D)[0]
            out[:, f] = flat[:, idx] @ w
        return out

    two = {}
    two["ISO 1.5 sigma, nominal sigma"] = syn.place(iso_extract(syn.sigma_d / 2))
    two["ISO 1.5 sigma, calibrated sigma"] = syn.place(iso_extract(sigma_cal))
    two["JOINT all pixels, calibrated sigma"] = syn.place(joint_extract(sigma_cal))
    two["JOINT weighted (2-pass), calibrated sigma"] = syn.place(
        joint_extract_weighted(sigma_cal))
    rms_t = float(np.sqrt(np.mean(target**2)))
    rows = []
    for name, img in two.items():
        scale = float(np.sum(img * target) / np.sum(target * target))
        err = float(np.sqrt(np.mean((img - target) ** 2))) / rms_t
        rows.append((name, scale, err, *_line_pair_contrast(img, pairs)))
    hdr = ["two-stage image", "scale vs S*h_e", "rel. RMSE vs S*h_e"] + \
        [f"contrast {sep * PIXEL_NM / 2:.0f} nm" for sep, _ in pairs]
    md_table(hdr, rows)
    print("The isolated fit with the nominal (detection-only) sigma is biased in scale by "
          "the effective-PSF broadening of the spot; the joint fit on all pixels has the "
          "lowest noise. Contrast = Michelson contrast of the line pairs "
          "(truth S*h_e: " + ", ".join(f"{c:.2f}" for c in _line_pair_contrast(target, pairs))
          + ").\n")

    # ---- deconvolution: two-stage image + RL with h_e, vs full-model RL
    print(f"### E5b -- deconvolving h_e: two-stage + RL vs full-model RL ({iters} iterations)\n")
    base = np.maximum(two["JOINT weighted (2-pass), calibrated sigma"], 1e-3)
    base_iso = np.maximum(two["ISO 1.5 sigma, calibrated sigma"], 1e-3)
    init = np.full((N, N), float(base.mean()))
    rms_s = float(np.sqrt(np.mean(S_true**2)))

    def fwd_e(S):
        return syn.conv(S, syn.HE)

    def adj_e(img):
        return syn.conv(img, np.conj(syn.HE))

    started = time.perf_counter()
    err_two = [float(np.sqrt(np.mean((S - S_true) ** 2))) / rms_s
               for S in _rl(base, fwd_e, adj_e, iters, init)]
    t_two = time.perf_counter() - started
    err_iso = [float(np.sqrt(np.mean((S - S_true) ** 2))) / rms_s
               for S in _rl(base_iso, fwd_e, adj_e, iters, init)]
    started = time.perf_counter()
    err_full, last = [], None
    for S in _rl(frames, syn.forward, syn.adjoint, iters, init, bg=syn.bg):
        err_full.append(float(np.sqrt(np.mean((S - S_true) ** 2))) / rms_s)
        last = S
    t_full = time.perf_counter() - started
    # the two-stage RL result at the same iteration count, for the contrast table
    S_two = None
    for S in _rl(base, fwd_e, adj_e, iters, init):
        S_two = S
    rows = []
    for it in (1, 5, 10, 20, 30, iters):
        rows.append((it, err_iso[it - 1], err_two[it - 1], err_full[it - 1]))
    md_table(["iteration", "ISO two-stage + RL(h_e)", "JOINT-weighted two-stage + RL(h_e)",
              "full-model RL"], rows)
    print("Relative RMSE against the true specimen S.\n")
    rows = [("JOINT-weighted two-stage + RL(h_e)", *_line_pair_contrast(S_two, pairs)),
            ("full-model RL", *_line_pair_contrast(last, pairs)),
            ("truth S", *_line_pair_contrast(S_true, pairs))]
    md_table(["estimate"] + [f"contrast {sep * PIXEL_NM / 2:.0f} nm" for sep, _ in pairs], rows)
    print(f"Wall time: two-stage RL {t_two:.1f} s, full-model RL {t_full:.1f} s "
          f"({len(syn.shifts)} forward+adjoint frame convolutions per iteration).\n")


# ================================================================ E6
def e6_spot_calibration(mean_frame=None, foci=None, verbose=True):
    """Fit one spot sigma to all foci of a frame (variable projection)."""
    if mean_frame is None:
        print("## E6 -- spot-shape self-calibration\n")
        period, sigma_true = 11.05, 2.0
        shape = (100, 100)
        foci = lattice_points((period, 0.0), (0.0, period), (3.3, 5.1), shape, margin=6)
        yy, xx = np.mgrid[0:shape[0], 0:shape[1]].astype(float)
        frame = np.full(shape, 50.0)
        amps = 200.0 * (0.5 + RNG.random(len(foci)))
        for (fx, fy), a in zip(foci, amps):
            frame += a * gaussian(xx - fx, yy - fy, sigma_true)
        mean_frame = RNG.poisson(frame).astype(float)
    rows_n, cols_n = mean_frame.shape
    r = 4
    patches = []
    for fx, fy in foci:
        cx, cy = int(round(fx)), int(round(fy))
        if cx - r < 0 or cy - r < 0 or cx + r >= cols_n or cy + r >= rows_n:
            continue
        yy, xx = np.mgrid[cy - r:cy + r + 1, cx - r:cx + r + 1].astype(float)
        patches.append((xx.ravel() - fx, yy.ravel() - fy,
                        mean_frame[cy - r:cy + r + 1, cx - r:cx + r + 1].ravel()))

    def cost(sigma):
        total = 0.0
        for dx, dy, v in patches:
            D = np.stack([gaussian(dx, dy, sigma), np.ones(v.size)], 1)
            coef, *_ = np.linalg.lstsq(D, v, rcond=None)
            total += float(np.sum((D @ coef - v) ** 2))
        return total

    res = minimize_scalar(cost, bounds=(0.5, 4.0), method="bounded")
    if verbose:
        md_table(["quantity", "value"],
                 [("foci used", len(patches)), ("true sigma (px)", 2.0),
                  ("fitted sigma (px)", float(res.x))])
        print("One shared Gaussian sigma fitted to every focus at once (per-focus "
              "amplitude and constant solved linearly inside the sigma search).\n")
    return float(res.x)


EXPERIMENTS = {
    "e1": e1_extraction,
    "e2": e2_noise_weighting,
    "e3": e3_ism_shift,
    "e4": e4_gridding,
    "e5": e5_two_stage_vs_ml,
    "e6": e6_spot_calibration,
}


def main(argv):
    names = [a for a in argv if a in EXPERIMENTS] or list(EXPERIMENTS)
    for name in names:
        started = time.perf_counter()
        EXPERIMENTS[name]()
        print(f"_({name} took {time.perf_counter() - started:.1f} s)_\n")


if __name__ == "__main__":
    main(sys.argv[1:])
