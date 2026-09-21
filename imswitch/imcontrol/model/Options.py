import os
from dataclasses import dataclass, field

from dataclasses_json import dataclass_json, Undefined, CatchAll

from imswitch.imcommon.model import dirtools


@dataclass(frozen=True)
class RecordingOptions:
    outputFolder: str = os.path.join(dirtools.UserFileDirs.Root, 'recordings')
    includeDateInOutputFolder: bool = True


@dataclass(frozen=True)
class WatcherOptions:
    outputFolder: str = os.path.join(dirtools.UserFileDirs.Root, 'scripts')


@dataclass(frozen=True)
class MemoryOptions:
    """How much RAM ImSwitch may spend on buffering and on automatic work.

    A property of the computer, which is why it lives here and not in the
    setup file. Each field is a limit on one thing, in MiB, and defaults to
    what the code did before it was settable; none of them is a process limit
    (camera drivers and datasets are outside them). Adopted at startup by
    ``imswitch.imcommon.model.memory_limits.configure``; a value that is not
    a positive whole number is reported and the default stands.
    """
    #: Backlog the recording writer may hold before the acquisition loop blocks.
    writerQueueMB: int = 512
    #: Backlog any one (detector, consumer) chunk queue may hold before that
    #: consumer's stream is declared incomplete.
    perDetectorQueueMB: int = 256
    #: Working set ImProcess may spend on automatic work: contrast sampling and
    #: the mean preview computed on load.
    processingWorkingSetMB: int = 256


@dataclass_json(undefined=Undefined.INCLUDE)
@dataclass(frozen=True)
class Options:
    setupFileName: str  # JSON file that contains setup info
    recording: RecordingOptions = field(default_factory=RecordingOptions)
    watcher: WatcherOptions = field(default_factory=WatcherOptions)
    memory: MemoryOptions = field(default_factory=MemoryOptions)
    _catchAll: CatchAll = None



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
