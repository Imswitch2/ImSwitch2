"""What arming checks, in one readable list the user can trust.

Each check is a line with a level: ``ok`` (said so the user sees it was
checked), ``warn`` (arms, but read it) or ``error`` (refuses to arm; the
line says what to do). The controller gathers the facts (devices, panels,
the ROI set) on the GUI thread; the checks here are plain functions of them,
so they are tested without a microscope.

The ROI check maps every ROI's box through the pipeline's transform and plans
its scan exactly as ``run-scan`` will (:func:`~.boxes.plan_box_scan`, the
panel's step, the scanner range), so a cell outside the scan range is found
before the experiment, not at its trigger.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

OK, WARN, ERROR = "ok", "warn", "error"
_MARK = {OK: "OK", WARN: "WARN", ERROR: "NO"}


@dataclass
class Check:
    level: str
    topic: str
    text: str

    def __str__(self) -> str:
        return f"[{_MARK[self.level]}] {self.topic}: {self.text}"


def errors(checks) -> list[Check]:
    return [c for c in checks if c.level == ERROR]


def classify_rois(rows, transform, *, axes, params, steps, ranges, context=None) -> list[dict]:
    """For each ROI (rows with ``id``, ``centroid`` (row, col) and ``bbox``
    (row0, col0, row1, col1) in camera pixels) what its triggered scan would
    be: ``{"id", "status": ok | clipped | skipped, "reason", "centre_um",
    "size_um", "pixels"}``."""
    from imswitch.imcommon.model.smartpipeline import Points, apply_transform
    from imswitch.imcontrol.model.smartpipeline.boxes import BOX_COLUMNS, box_of_row, map_boxes, plan_box_scan

    rows = list(rows)
    if not rows:
        return []
    source = Points(np.array([row["centroid"] for row in rows], dtype=float), frame=("unknown" if str(transform.source_frame) == "any" else str(transform.source_frame)),
                    properties={key: np.array([row["bbox"][i] for row in rows], dtype=float)
                                for i, key in enumerate(BOX_COLUMNS)})
    context = dict(context or {})
    moved = apply_transform(source, transform, context)
    map_boxes(source, moved, transform, context)
    out = []
    for index, row in enumerate(rows):
        box = box_of_row(moved.properties, index, axes)
        plan = plan_box_scan(
            box, steps=steps, margin_um=float(params.get("margin_um") or 0.0),
            margin_frac=float(params.get("margin_frac") or 0.0), min_size_um=float(params.get("min_size_um") or 0.0),
            max_size_um=float(params.get("max_size_um") or 0.0), ranges=ranges,
            min_overlap=float(params.get("min_overlap", 0.5)))
        clipped = any("clip" in note for note in plan["notes"])
        out.append({"id": int(row["id"]), "status": "skipped" if plan["skip"] else ("clipped" if clipped else "ok"),
                    "reason": plan["skip"] or "; ".join(plan["notes"]), "centre_um": plan["centre"],
                    "size_um": plan["size"], "pixels": plan["pixels"]})
    return out


def roi_checks(classified, *, roi_count_source: str = "") -> list[Check]:
    if not classified:
        return [Check(WARN, "ROIs", "none yet" + (f" ({roi_count_source})" if roi_count_source else ""))]
    skipped = [c for c in classified if c["status"] == "skipped"]
    clipped = [c for c in classified if c["status"] == "clipped"]
    checks = [Check(OK if not skipped else WARN, "ROIs",
                    f"{len(classified)} ROIs: {len(classified) - len(skipped) - len(clipped)} scan whole, "
                    f"{len(clipped)} clipped to the scan range, {len(skipped)} would be skipped")]
    for c in skipped:
        checks.append(Check(WARN, f"ROI {c['id']}", f"would be SKIPPED at its trigger: {c['reason']}"))
    for c in clipped:
        checks.append(Check(WARN, f"ROI {c['id']}", f"would be scanned clipped: {c['reason']}"))
    return checks


def scan_time_check(classified, dwell_s: float | None, *, line_overhead_s: float = 0.0) -> Check | None:
    """The expected time of one triggered scan (pixels x dwell), from the ROI boxes."""
    sizes = [int(np.prod(list(c["pixels"].values()))) for c in classified if c["status"] != "skipped" and c["pixels"]]
    if not sizes or not dwell_s:
        return None
    longest = max(sizes) * float(dwell_s)
    typical = float(np.median(sizes)) * float(dwell_s)
    level = WARN if longest > 10.0 else OK
    return Check(level, "Scan time", f"about {typical:.2f} s per triggered scan (longest {longest:.2f} s) at "
                                     f"{dwell_s * 1e6:.0f} us dwell, {int(np.median(sizes))} px typical")


__all__ = ["ERROR", "OK", "WARN", "Check", "classify_rois", "errors", "roi_checks", "scan_time_check"]


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
