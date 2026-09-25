class InvalidChildClassError(Exception):
    """Exception raised when trying to inialize an invalid child"""

    def __init__(self, message):
        self.message = message


class IncompatibilityError(Exception):
    """Exception raised when initialized object is not compatibile with
    other module/s"""

    def __init__(self, message):
        self.message = message


class ScanDesignRefusedError(Exception):
    """The scan manager refused to build a scan from these parameters.

    The message is the reason, written for the operator ("Scan would take
    20.8 min, above the 1 min cap (scan.maxScanTimeMin)."). ``makeFullScan``
    used to log the reason and return None, and every caller that unpacked
    the result turned the refusal into a TypeError that carried no reason.
    """

    def __init__(self, message):
        super().__init__(message)
        self.message = message


# Copyright (C) 2020-2021 ImSwitch developers
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
