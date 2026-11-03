"""The smart-pipeline engine: runs a pipeline document tick by tick.

Qt-free and hardware-free: everything it touches is a *port*
(:mod:`.ports`) -- a frame source, a step runner, an action backend, a
transform registry -- with mocks in :mod:`.mock`, so the same engine runs
a recording offline in a test and, in Phase 2, a detector on a rig.

Design: ``docs/design/plans/smart-microscopy-pipeline-designer.md``.
"""

from imswitch.imcontrol.model.smartpipeline.engine import (
    MODE_EFFECTS,
    ArmError,
    Engine,
    EngineError,
    EventLog,
    LogEntry,
    RunMode,
    RunSummary,
    TickResult,
    describe,
)
from imswitch.imcontrol.model.smartpipeline.ports import (
    ActionBackend,
    ActionRequest,
    CoalescingInbox,
    Completion,
    FrameSource,
    SimpleCompletion,
    StepError,
    StepRunner,
)

__all__ = [
    "MODE_EFFECTS",
    "ActionBackend",
    "ActionRequest",
    "ArmError",
    "CoalescingInbox",
    "Completion",
    "Engine",
    "EngineError",
    "EventLog",
    "FrameSource",
    "LogEntry",
    "RunMode",
    "RunSummary",
    "SimpleCompletion",
    "StepError",
    "StepRunner",
    "TickResult",
    "describe",
]


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
