"""Conservative, one-shot Local footprint suggestions from a localized lattice.

No pixel ownership or per-cell footprint fitting: suggest an odd square side
or circle diameter whose translated supports do not overlap at spot centres.
The suggestion is *only* applied after an explicit user action.
"""

from __future__ import annotations

import math
import numpy as np


def suggest_local_footprint_size(localization, shape: str = "square") -> int:
    """Return a conservative odd integer side/diameter in camera pixels.

    Evaluate small relative lattice translations and all motif-point pairs,
    including a staggered two-point motif. For square footprints, two
    axis-aligned squares do not overlap when separated along *either* axis;
    for discs the Euclidean centre distance determines the safe diameter.
    """
    if shape not in ("square", "circle"):
        raise ValueError("Footprint shape must be 'square' or 'circle'")
    if localization is None:
        raise ValueError("Localize a pattern before suggesting a footprint")
    a1 = np.asarray(localization.lattice.a1, dtype=float)
    a2 = np.asarray(localization.lattice.a2, dtype=float)
    motif = np.asarray(localization.motif, dtype=float).reshape(-1, 2)
    candidates = []
    for i in range(-4, 5):
        for j in range(-4, 5):
            delta = i * a1 + j * a2
            for p, point in enumerate(motif):
                for q, other in enumerate(motif):
                    if i == 0 and j == 0 and p == q:
                        continue
                    shift = delta + other - point
                    if not np.all(np.isfinite(shift)):
                        raise ValueError("Lattice contains non-finite coordinates")
                    separation = (float(np.max(np.abs(shift))) if shape == "square"
                                  else float(np.linalg.norm(shift)))
                    if separation > 1e-6:
                        candidates.append(separation)
    if not candidates:
        raise ValueError("No usable focus spacing in localization")
    # The small epsilon excludes exactly touching footprints, and odd sizes
    # match the WIP Local pixel-stencil convention.
    maximum = int(math.floor(min(candidates) - 1e-6))
    side = maximum if maximum % 2 else maximum - 1
    if side < 3:
        raise ValueError("Lattice is too dense for a >=3 px Local footprint")
    return min(side, 255)
