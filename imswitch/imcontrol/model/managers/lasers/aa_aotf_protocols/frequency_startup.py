"""AA profile for controllers whose RF frequency resets at power-up.

This profile preserves every runtime command from ``aa.compatibility`` and
adds the opt-in startup sequence established from the hardware transcript:

1. controller-global ``I0`` (write-only preamble),
2. per-channel ``LnF<frequency MHz>``.

The manager keeps the channel off through its normal legacy initialization;
the Laser controller subsequently also writes zero power and disables the
channel before the user can interact with it.
"""

import math

from . import send_query, send_write, validate_channel
from .compatibility import AACompatibilityProfile


def validate_frequency_mhz(value) -> float:
    """Return a finite, positive RF frequency in MHz."""
    try:
        frequency = float(value)
    except (TypeError, ValueError):
        raise ValueError(
            f'AA frequency must be numeric, got {value!r}'
        ) from None
    if not math.isfinite(frequency) or frequency <= 0:
        raise ValueError(
            f'AA frequency must be finite and positive, got {value!r}'
        )
    return frequency


def format_frequency_mhz(value) -> str:
    """Format MHz compactly while retaining a decimal for whole values."""
    frequency = validate_frequency_mhz(value)
    formatted = format(frequency, '.12g')
    if 'e' not in formatted.lower() and '.' not in formatted:
        formatted += '.0'
    return formatted


class AAFrequencyStartupProfile(AACompatibilityProfile):
    """Compatibility commands plus opt-in power-up frequency restoration."""

    profile_id = 'aa.frequency-startup'

    def prepare_frequency_programming(self, connection, channel) -> None:
        # Validate the configured channel even though I0 is controller-global,
        # so every profile operation enforces the same manager contract.
        validate_channel(channel)
        send_write(connection, 'I0')

    def set_channel_frequency(self, connection, channel, frequency_mhz) -> str:
        channel = validate_channel(channel)
        frequency = format_frequency_mhz(frequency_mhz)
        return send_query(connection, f'L{channel}F{frequency}')


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# This file is distributed under the terms of the GNU General Public License
# version 3 or, at your option, any later version.
