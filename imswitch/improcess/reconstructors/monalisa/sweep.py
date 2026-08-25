"""Parameter-sweep support for the fast-Gauss MoNaLISA reconstruction.

An advanced, optional mode: instead of one reconstruction, the offline
fast-Gauss path runs once per value of a chosen parameter (e.g. the
detection-pinhole radius) and stacks the results along a leading Sweep axis,
so the best setting can be found by sliding through the stack.
"""

import math
import re

MAX_SWEEP_VALUES = 64

#: Canonical sweepable parameters: key -> (params-dict key, UI label).
#: ``pinhole_radius_sigma`` also forces the circular-pinhole footprint mode,
#: since sweeping a radius the shell footprint would ignore is meaningless.
SWEEPABLE_PARAMETERS = {
    "pinhole_radius_sigma": (
        "fast_gauss_pinhole_radius_sigma",
        "Pinhole radius (×σ)",
    ),
    "gaussian_sigma_px": (
        "fast_gauss_gaussian_sigma_px",
        "Gaussian sigma (px)",
    ),
}


def resolve_sweep_parameter(label: object) -> str:
    """Map a UI label (or canonical key) onto a canonical sweep parameter."""
    text = str(label or "").strip().lower()
    if not text:
        raise ValueError(
            "No sweep parameter selected; choose one of: "
            + ", ".join(ui for _, ui in SWEEPABLE_PARAMETERS.values())
        )
    if text in SWEEPABLE_PARAMETERS:
        return text
    if "pinhole" in text:
        return "pinhole_radius_sigma"
    if "sigma" in text:
        return "gaussian_sigma_px"
    raise ValueError(
        f"Unknown sweep parameter {label!r}; choose one of: "
        + ", ".join(ui for _, ui in SWEEPABLE_PARAMETERS.values())
    )


def parse_sweep_values(text: object, max_values: int = MAX_SWEEP_VALUES) -> list[float]:
    """Parse the sweep-values field into a list of positive floats.

    Accepts a separated list (``0.5, 1, 1.5`` — commas, semicolons or
    whitespace) or an inclusive range ``start:step:stop`` (``0.5:0.25:2.5``).

    Raises:
        ValueError: On empty input, unparseable tokens, non-positive or
            non-finite values, a malformed range, or more than ``max_values``
            entries (a sweep multiplies reconstruction work and memory by the
            value count, so a runaway range should fail before it runs).
    """
    body = str(text or "").strip()
    if not body:
        raise ValueError(
            "Sweep values are empty; enter e.g. '0.5, 1.0, 1.5' or '0.5:0.25:2.5'"
        )

    if ":" in body:
        parts = body.split(":")
        if len(parts) != 3:
            raise ValueError(
                f"Range sweep values must be 'start:step:stop', got {body!r}"
            )
        try:
            start, step, stop = (float(part) for part in parts)
        except ValueError as exc:
            raise ValueError(
                f"Could not parse range sweep values {body!r}: {exc}"
            ) from exc
        if step <= 0:
            raise ValueError("Sweep range step must be positive")
        if stop < start:
            raise ValueError("Sweep range stop must not be below start")
        count = int(math.floor((stop - start) / step + 1e-9)) + 1
        values = [start + index * step for index in range(count)]
    else:
        tokens = [token for token in re.split(r"[\s,;]+", body) if token]
        try:
            values = [float(token) for token in tokens]
        except ValueError as exc:
            raise ValueError(
                f"Could not parse sweep values {body!r}: {exc}"
            ) from exc

    if not values:
        raise ValueError("Sweep values are empty")
    if len(values) > max_values:
        raise ValueError(
            f"Sweep has {len(values)} values; the maximum is {max_values}"
        )
    for value in values:
        if not (value > 0 and math.isfinite(value)):
            raise ValueError(
                f"Sweep values must be positive finite numbers, got {value!r}"
            )
    return values


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
