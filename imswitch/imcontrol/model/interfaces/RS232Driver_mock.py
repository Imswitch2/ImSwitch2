from imswitch.imcommon.model import initLogger


class MockRS232Driver:
    """Mock RS232 driver"""

    def __init__(self, name, settings, **kwargs):
        self.__logger = initLogger(self, instanceName=name)
        self._name = name
        self._settings = settings
        pass

    def query(self, arg):
        self.__logger.info(f"Querying to {self._settings['port']}: {arg}")
        pass

    def initialize(self):
        pass

    def close(self):
        pass

    def write(self, arg):
        self.__logger.info(f"Writing to {self._settings['port']}: {arg}")
        pass

    def read(self, *args, **kwargs):
        """No-op read for the mock driver.

        Returns None to mean "no data", matching how a real RS232 read
        behaves on a timeout. Without this method, callers that poll read()
        on a tight loop (e.g. the TriggerScope SerialMonitor, every few ms)
        raise AttributeError on every poll and flood the log whenever a port
        falls back to the mock because the real device could not be opened.
        """
        return None


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
