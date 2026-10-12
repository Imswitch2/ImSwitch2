"""Live mode: watch a root folder and reconstruct the raw data that appears in it."""

from .basecontrollers import ImProcessWidgetController


class LiveModeController(ImProcessWidgetController):
    """Empty. Built up from the requirements, one at a time."""

    def __init__(self, *args, main_controller=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._main_controller = main_controller


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
