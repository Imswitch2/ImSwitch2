"""Validation of the FLIM pipeline: refitting a stored cube with every fit
method, and how each method's lifetime converges with the photon count.

Tutorial 10's extended mode and the rig campaign of
:doc:`/timetagger/validation` run this on a reference dye: scans of the
same field are accumulated one by one, each accumulation is refitted with
``moment``, ``phasor`` and ``exp1`` exactly as the detector's worker fits
a frame, and the median lifetime, its spread and its bias against the
reference are tabulated per method and photon count. A method has
*converged* when its median lifetime stops moving with more photons (the
last two accumulations agree within the tolerance); its *bias* against
the reference is reported beside that, because every method carries a
bias of its own: the moment and the phasor read low by the window's
truncation and the IRF width, which no number of photons removes (see the
fitting notes in :doc:`/devices/detectors`), and ``exp1`` reads high
while the tail's bins are sparse (the log of small counts), a bias that
shrinks with photons. The campaign records all three.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from .fitting import fit_exp1, fit_moment, fit_phasor
from .processing import subtract_background
from .types import TimeResolvedScanProducts

METHODS: Tuple[str, ...] = ("moment", "phasor", "exp1")


def refit_cube(
    cube_counts: np.ndarray,
    t_axis_ns: np.ndarray,
    method: str,
    *,
    laser_rep_rate_mhz: float,
    min_counts_per_pixel: int = 20,
    background_per_bin: float = 0.0,
) -> Tuple[np.ndarray, np.ndarray]:
    """Fit a ``(y, x, bins)`` cube in forward time the way the worker does.

    The flat background comes off every histogram, the IRF peak is found on
    the decay summed over the valid pixels, and the method's fit is
    referenced to that peak. Returns ``(intensity, lifetime_ns)``; pixels
    below ``min_counts_per_pixel`` read 0.
    """
    if method not in METHODS:
        raise ValueError(f"method must be one of {METHODS}, not {method!r}")
    cube = np.asarray(cube_counts, dtype=np.float64)
    if cube.ndim != 3:
        raise ValueError("cube_counts must be (y, x, bins)")
    t_ns = np.asarray(t_axis_ns, dtype=np.float64)
    if t_ns.shape != (cube.shape[-1],):
        raise ValueError("t_axis_ns must match the cube's last axis")
    intensity = cube.sum(axis=2)
    cube = subtract_background(cube, float(background_per_bin)).astype(np.float64)
    valid = intensity >= int(min_counts_per_pixel)
    decay = cube[valid].sum(axis=0) if valid.any() else cube.sum(axis=(0, 1))
    peak_bin = int(np.argmax(decay)) if decay.sum() > 0 else 0
    t_s = t_ns * 1e-9
    if method == "phasor":
        omega = 2.0 * np.pi * float(laser_rep_rate_mhz) * 1e6
        lifetime_s = fit_phasor(cube, intensity, omega, np.cos(omega * t_s), np.sin(omega * t_s),
                                float(t_s[peak_bin]))
    elif method == "exp1":
        lifetime_s = fit_exp1(cube, t_s[None, None, :], peak_bin)
    else:
        _, lifetime_s = fit_moment(cube, t_s.astype(np.float32), peak_bin)
    lifetime_ns = (np.asarray(lifetime_s, dtype=np.float64) * 1e9).astype(np.float32)
    lifetime_ns[~valid] = 0.0
    return intensity.astype(np.float32), lifetime_ns


@dataclass
class ConvergencePoint:
    method: str
    scans: int
    photons_per_pixel: float
    """ The median intensity of the valid pixels at this accumulation. """
    tau_median_ns: float
    tau_mean_ns: float
    tau_std_ns: float
    valid_fraction: float
    bias_ns: Optional[float]
    """ ``median(tau - reference)`` over the valid pixels; ``None`` without a
    reference. """

    def line(self) -> str:
        bias = "" if self.bias_ns is None else f"  bias {self.bias_ns:+.3f} ns"
        return (f"{self.method:7s} {self.scans:3d} scan(s)  {self.photons_per_pixel:8.0f} ph/px  "
                f"tau {self.tau_median_ns:.3f} ns (mean {self.tau_mean_ns:.3f}, "
                f"sd {self.tau_std_ns:.3f}), {100 * self.valid_fraction:.0f} % valid{bias}")


@dataclass
class ConvergenceReport:
    points: List[ConvergencePoint] = field(default_factory=list)
    reference_tau_ns: Optional[float] = None
    tolerance_ns: float = 0.1

    def last(self, method: str) -> Optional[ConvergencePoint]:
        rows = [p for p in self.points if p.method == method]
        return rows[-1] if rows else None

    def converged(self, method: str) -> Optional[bool]:
        """Whether the method's median lifetime stopped moving: the last two
        accumulations agree within the tolerance. ``None`` with fewer than
        two scans."""
        rows = [p for p in self.points if p.method == method and p.valid_fraction > 0]
        if len(rows) < 2:
            return None
        return abs(rows[-1].tau_median_ns - rows[-2].tau_median_ns) <= self.tolerance_ns

    def accurate(self, method: str) -> Optional[bool]:
        """Whether the fullest accumulation's bias against the reference is
        within the tolerance; ``None`` without a reference."""
        point = self.last(method)
        if point is None or point.bias_ns is None:
            return None
        return abs(point.bias_ns) <= self.tolerance_ns

    @property
    def methods(self) -> List[str]:
        seen: List[str] = []
        for p in self.points:
            if p.method not in seen:
                seen.append(p.method)
        return seen

    @property
    def ok(self) -> bool:
        results = [self.converged(m) for m in self.methods]
        return bool(results) and all(r is True for r in results)

    def summary(self) -> str:
        head = "FLIM convergence"
        if self.reference_tau_ns is not None:
            head += f" against {self.reference_tau_ns:.3f} ns (tolerance {self.tolerance_ns:g} ns)"
        lines = [head] + ["  " + p.line() for p in self.points]
        for method in self.methods:
            state = self.converged(method)
            verdict = ("needs more scans" if state is None
                       else "converged" if state else "NOT converged")
            accurate = self.accurate(method)
            if accurate is not None:
                verdict += (", within tolerance of the reference" if accurate
                            else f", biased by {self.last(method).bias_ns:+.3f} ns")
            lines.append(f"  {method}: {verdict}")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reference_tau_ns": self.reference_tau_ns,
            "tolerance_ns": self.tolerance_ns,
            "points": [p.__dict__ for p in self.points],
            "converged": {m: self.converged(m) for m in self.methods},
            "accurate": {m: self.accurate(m) for m in self.methods},
        }


def convergence_report(
    products: Sequence[TimeResolvedScanProducts],
    *,
    reference_tau_ns=None,
    methods: Iterable[str] = METHODS,
    tolerance_ns: float = 0.1,
    laser_rep_rate_mhz: Optional[float] = None,
    min_counts_per_pixel: Optional[int] = None,
) -> ConvergenceReport:
    """Accumulate the scans' cubes one by one and refit each accumulation
    with every method.

    Every product needs ``cube_counts`` (``capture_cube=True``) on the same
    axis. ``reference_tau_ns`` is a scalar (a reference dye) or a per-pixel
    map (the mock sample's truth); the rep rate, count threshold and
    background per bin default to the first product's metadata.
    """
    products = list(products)
    if not products:
        raise ValueError("no products to analyse")
    for p in products:
        if p.cube_counts is None:
            raise ValueError("every product needs cube_counts (capture_cube=True)")
    meta = products[0].metadata or {}
    rep = float(laser_rep_rate_mhz if laser_rep_rate_mhz is not None
                else meta.get("laser_rep_rate_mhz", 80.0) or 80.0)
    min_counts = int(min_counts_per_pixel if min_counts_per_pixel is not None
                     else meta.get("min_counts_per_pixel", 20) or 20)
    background = float(meta.get("background_per_bin", 0.0) or 0.0)
    t_axis = np.asarray(products[0].t_axis_ns, dtype=np.float64)
    reference = None if reference_tau_ns is None else np.asarray(reference_tau_ns, dtype=np.float64)
    report = ConvergenceReport(
        reference_tau_ns=None if reference is None else float(np.median(reference)),
        tolerance_ns=float(tolerance_ns),
    )
    accumulated = np.zeros(np.asarray(products[0].cube_counts).shape, dtype=np.float64)
    for k, product in enumerate(products, start=1):
        accumulated += np.asarray(product.cube_counts, dtype=np.float64)
        for method in methods:
            intensity, tau = refit_cube(
                accumulated, t_axis, method, laser_rep_rate_mhz=rep,
                min_counts_per_pixel=min_counts, background_per_bin=background * k,
            )
            valid = (tau > 0) & np.isfinite(tau)
            if not valid.any():
                report.points.append(ConvergencePoint(method, k, 0.0, 0.0, 0.0, 0.0, 0.0, None))
                continue
            taus = tau[valid].astype(np.float64)
            bias = None
            if reference is not None:
                ref = np.broadcast_to(reference, tau.shape)[valid] if reference.ndim else reference
                bias = float(np.median(taus - ref))
            report.points.append(ConvergencePoint(
                method, k, float(np.median(intensity[valid])), float(np.median(taus)),
                float(taus.mean()), float(taus.std()), float(valid.mean()), bias,
            ))
    return report


__all__ = ["METHODS", "ConvergencePoint", "ConvergenceReport", "convergence_report", "refit_cube"]
