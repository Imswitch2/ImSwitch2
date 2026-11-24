"""The Time Tagger pre-flight checklist, as data.

One list of items, each with what was measured, what was expected and what
to do about it, produced by ``facade.time_tagger.preflight()`` for tutorial
10 and shown by the Lifetime widget's Signals panel. Items that need a scan
(line edges, frame-to-pixel lead, last frame closing) are reported as
*skipped* here and filled in by the scan-aware tutorials.

Only what the card is configured for is checked: without a ``laser_sync``
role (a card that only counts photons) there are no sync, window or
pile-up items, and no time-resolved detector means no detector settings to
check -- an unconfigured input is never a red line.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .processing import PILEUP_RED, PILEUP_WARN

#: How far the measured sync rate may sit from the configured one.
REP_RATE_TOLERANCE = 0.005


def rep_rate_mismatch(measured_hz: float, configured_mhz: float) -> Optional[str]:
    """A sentence when a measured *unfiltered* sync rate disagrees with the
    configured laser repetition rate by more than ``REP_RATE_TOLERANCE``,
    else ``None``. (With the conditional filter on, the sync rate is the
    photon rate and says nothing about the laser.)"""
    configured_mhz = float(configured_mhz or 0.0)
    if configured_mhz <= 0 or measured_hz <= 0:
        return None
    measured_mhz = float(measured_hz) / 1e6
    if abs(measured_mhz - configured_mhz) / configured_mhz <= REP_RATE_TOLERANCE:
        return None
    return (f"the sync counts {measured_mhz:.4f} MHz but laser_rep_rate_mhz is "
            f"{configured_mhz:g}: measure it (tutorial 04) and set it")


@dataclass
class PreflightItem:
    name: str
    status: str
    """ ``"ok"``, ``"warn"``, ``"fail"`` or ``"skipped"``. """
    measured: Any = None
    expected: Any = None
    hint: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "skipped")

    def line(self) -> str:
        mark = {"ok": "[ok]  ", "warn": "[WARN]", "fail": "[FAIL]", "skipped": "[skip]"}[self.status]
        text = f"{mark} {self.name}"
        if self.measured is not None:
            text += f": {self.measured}"
            if self.expected is not None:
                text += f" (expected {self.expected})"
        if self.hint and self.status != "ok":
            text += f" -- {self.hint}"
        return text


@dataclass
class PreflightReport:
    items: List[PreflightItem] = field(default_factory=list)
    title: str = "FLIM pre-flight"

    @property
    def ok(self) -> bool:
        return all(item.status != "fail" for item in self.items)

    @property
    def warnings(self) -> List[PreflightItem]:
        return [item for item in self.items if item.status == "warn"]

    def summary(self) -> str:
        head = f"{self.title}: " + ("green" if self.ok and not self.warnings
                                      else "green with warnings" if self.ok else "RED")
        return "\n".join([head] + ["  " + item.line() for item in self.items])

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "items": [item.__dict__ for item in self.items]}


def _param(detector, name, default=None):
    if detector is None:
        return default
    try:
        return detector.parameters[name].value
    except Exception:
        return getattr(detector, f"_{name}", default)


def run_device_preflight(tt, detector=None, duration_s: float = 1.0) -> PreflightReport:
    """Everything that can be checked without a scan.

    ``tt`` is a ``TimeTaggerFacade``; ``detector`` the FLIM detector manager
    whose settings (rep rate, window, background) the checks are against.
    """
    items: List[PreflightItem] = []
    roles = set(tt.roles())
    timed = "laser_sync" in roles
    title = "FLIM pre-flight" if timed else "Time Tagger pre-flight"

    # 1. the card
    if not tt.connected:
        items.append(PreflightItem("card connected", "fail", "no",
                                   hint="the Time Tagger library or the card is missing"))
        return PreflightReport(items, title)
    items.append(PreflightItem(
        "card connected", "ok", f"{tt.model} {tt.serial}" + (" [mock]" if tt.is_mock else "")))

    # 2. the sync, by the divided-and-unfiltered procedure
    configured_mhz = float(_param(detector, "laser_rep_rate_mhz", 80.0) or 80.0)
    measured_mhz = None
    if timed:
        try:
            rep = tt.rep_rate(duration_s=duration_s)
            measured_mhz = rep.rate_hz / 1e6
            mismatch = rep_rate_mismatch(rep.rate_hz, configured_mhz)
            items.append(PreflightItem(
                "laser sync rate matches laser_rep_rate_mhz",
                "ok" if mismatch is None else "fail",
                f"{measured_mhz:.4f} MHz", f"{configured_mhz:g} MHz",
                hint=f"set laser_rep_rate_mhz = {measured_mhz:.4f} (tutorial 04)",
            ))
        except Exception as error:
            items.append(PreflightItem("laser sync present", "fail", str(error),
                                       hint="check the sync cable and its trigger level "
                                            "(tutorial 02, find_trigger_level)"))

    # 3. the window
    n_bins = int(_param(detector, "n_bins", 0) or 0) if timed else 0
    binwidth = int(_param(detector, "binwidth_ps", 32) or 32)
    period_ps = 1e6 / configured_mhz
    if n_bins:
        window_ps = n_bins * binwidth
        fraction = window_ps / period_ps
        reverse = tt.tcspc_direction == "reverse"
        if fraction >= 1.0:
            status = "ok"
        elif reverse:
            status = "fail"
        else:
            status = "warn" if fraction < 0.8 else "ok"
        items.append(PreflightItem(
            "TCSPC window spans the laser period", status,
            f"{window_ps / 1000:.2f} ns ({100 * fraction:.0f} %)", f">= {period_ps / 1000:.2f} ns",
            hint=("in reverse mode a short window cuts the peak off" if reverse
                  else "a truncated decay reads as a short lifetime; leave n_bins undeclared"),
        ))

    # 4. photons and overflows over one integration
    before = tt.overflows()
    rates = tt.count_rates(duration_s=duration_s)
    overflows = tt.overflows() - before
    photons = rates.rates_hz.get("photons", 0.0)
    if "photons" in roles:
        items.append(PreflightItem(
            "photons arriving", "ok" if photons > 0 else "fail", f"{photons:,.0f} Hz",
            hint="check the detector cable, polarity and trigger level "
                 "(tutorial 02, find_trigger_level)",
        ))
    items.append(PreflightItem(
        "no USB overflows", "ok" if overflows == 0 else "fail", overflows, 0,
        hint="enable the conditional filter or raise the dead time (tutorial 05)",
    ))

    # 5. pile-up proxy on the parked beam
    if measured_mhz:
        per_pulse = photons / (measured_mhz * 1e6)
        status = "ok" if per_pulse < PILEUP_WARN else "warn" if per_pulse < PILEUP_RED else "fail"
        items.append(PreflightItem(
            "photons per excitation pulse (pile-up)", status, f"{100 * per_pulse:.2f} %",
            f"< {100 * PILEUP_WARN:.0f} %", hint="lower the excitation power",
        ))

    # 6. background fraction, when a dark rate is configured
    background = float(_param(detector, "background_rate_hz", 0.0) or 0.0)
    if detector is None or not timed:
        pass  # no time-resolved detector: no background to subtract
    elif background > 0 and photons > 0:
        fraction = background / photons
        items.append(PreflightItem(
            "dark + afterpulsing fraction of the photon rate",
            "ok" if fraction < 0.1 else "warn", f"{100 * fraction:.1f} %", "< 10 %",
            hint="a large flat background biases every fit; cool or shield the detector",
        ))
    else:
        items.append(PreflightItem(
            "background rate measured", "warn" if background <= 0 else "ok",
            f"{background:,.0f} Hz" if background > 0 else "not set",
            hint="measure it with the laser blocked (tutorial 03) and set background_rate_hz",
        ))

    # 7. direction
    if timed:
        items.append(PreflightItem(
            "TCSPC direction consistent with the filter", "ok",
            f"{tt.tcspc_direction}" + (" (conditional filter on)" if tt.tcspc_direction == "reverse" else ""),
        ))

    # 8. what needs a scan, for the clocks that are cabled
    for name, role, tutorial in (("line clock edges = Ny", "line_clock", "07"),
                                 ("frame clock leads pixel 0", "frame_clock", "08"),
                                 ("last frame closes", "line_clock", "08")):
        if role in roles and detector is not None:
            items.append(PreflightItem(name, "skipped", hint=f"needs a scan: tutorial {tutorial}"))

    return PreflightReport(items, title)


__all__ = ["PreflightItem", "PreflightReport", "REP_RATE_TOLERANCE", "rep_rate_mismatch",
           "run_device_preflight"]
