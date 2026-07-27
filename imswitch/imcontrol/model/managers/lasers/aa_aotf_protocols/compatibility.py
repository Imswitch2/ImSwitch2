"""AA Opto-Electronic compatibility profile.

The command forms ``AAAOTFLaserManager`` has always sent, moved verbatim out of
the manager. Named for what it is -- the profile that preserves current field
behavior -- rather than for an assumed controller model or firmware range.

Command vocabulary, per channel ``n``:

- ``Ln O1`` / ``Ln O0``  -- channel emission on / off
- ``Ln P<value>``        -- channel amplitude
- ``Ln I0``              -- external (TTL) control
- ``Ln I1 O0``           -- internal control

Note ``Ln I1O0``: switching to internal control also switches the channel off
in the same command. That is how the manager has always done it, so it is
preserved exactly; whether the trailing ``O0`` is required or incidental is a
question for a hardware transcript.
"""

from . import send_query, validate_amplitude, validate_channel


class AACompatibilityProfile:
    """The AA command set currently in field use."""

    profile_id = 'aa.compatibility'

    #: There is no safe read-only query that distinguishes AA dialects, so this
    #: profile is never selected by probing. It is the documented default and
    #: may also be requested explicitly.
    auto_selectable = False

    unsupported_operations = frozenset()

    def set_channel_enabled(self, connection, channel, enabled) -> str:
        channel = validate_channel(channel)
        return send_query(
            connection, f'L{channel}O{1 if enabled else 0}'
        )

    def set_channel_amplitude(self, connection, channel, value) -> str:
        channel = validate_channel(channel)
        amplitude = validate_amplitude(value)
        return send_query(connection, f'L{channel}P{amplitude}')

    def select_internal_control(self, connection, channel) -> str:
        channel = validate_channel(channel)
        # The trailing O0 is part of the command as the manager has always
        # sent it; see the module docstring.
        return send_query(connection, f'L{channel}I1O0')

    def select_external_control(self, connection, channel) -> str:
        channel = validate_channel(channel)
        return send_query(connection, f'L{channel}I0')


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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
