"""Parameter-sweep support for the MoNaLISA-family reconstruction methods.

An advanced, optional mode: instead of one reconstruction, the selected
offline method runs once per value of a chosen parameter and the results are
stacked along a leading Sweep axis, so the best setting can be found by
sliding through the stack.
"""

import math
import re

MAX_SWEEP_VALUES = 64

#: Sweepable numeric parameters per reconstruction method:
#: ``{method: {UI label: params-dict key}}``. Only parameters the method
#: actually consumes are offered — sweeping one it ignores would produce a
#: stack of identical reconstructions. (The fast-Gauss PSF FWHM is absent for
#: that reason: the widget always sends an explicit Gaussian sigma, which
#: takes precedence over the PSF-derived value.) Methods not listed here do
#: not support sweeping.
METHOD_SWEEPABLE_PARAMETERS = {
    "Fast Gauss MoNaLISA": {
        "Pinhole radius (×σ)": "fast_gauss_pinhole_radius_sigma",
        "Gaussian sigma (px)": "fast_gauss_gaussian_sigma_px",
    },
    "ISM reassignment": {
        "ISM shift": "ism_reassign_shift",
        "Oversampling": "ism_reassign_oversampling",
        "PSF FWHM (nm)": "psf_fwhm_nm",
    },
}


def sweepable_parameters(method: object) -> dict[str, str]:
    """UI-label → params-key map of the method's sweepable parameters."""
    return dict(METHOD_SWEEPABLE_PARAMETERS.get(str(method or ""), {}))


def resolve_sweep_parameter(method: object, label: object) -> tuple[str, str]:
    """Resolve a sweep-parameter label for one method.

    Returns ``(params_key, ui_label)``. Matching is exact on the label first,
    then case-insensitive on label or key substrings, so values persisted
    from a widget and programmatic keys both resolve.
    """
    available = sweepable_parameters(method)
    if not available:
        raise ValueError(
            f'Parameter sweep is not supported for the '
            f'{str(method or "selected")!r} method'
        )
    text = str(label or "").strip()
    if not text:
        raise ValueError(
            "No sweep parameter selected; choose one of: "
            + ", ".join(available)
        )
    if text in available:
        return available[text], text
    lowered = text.lower()
    for ui_label, key in available.items():
        if lowered in ui_label.lower() or lowered in key.lower():
            return key, ui_label
    raise ValueError(
        f"Unknown sweep parameter {label!r} for {method!r}; choose one of: "
        + ", ".join(available)
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
