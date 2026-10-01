import uuid
from datetime import datetime
from enum import Enum
from typing import Optional


class ScriptRunStatus(Enum):
    """Status of a script run."""
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ScriptRunResult:
    """Structured result for a script run."""
    
    def __init__(self, script_path: Optional[str] = None):
        self.id: str = str(uuid.uuid4())
        self.script_path: Optional[str] = script_path
        self.status: ScriptRunStatus = ScriptRunStatus.RUNNING
        self.stdout: str = ""
        self.error: Optional[str] = None
        self.started_at: datetime = datetime.now()
        self.ended_at: Optional[datetime] = None
        # True when the run was cancelled and its cleanup did not finish
        # within the executor's cleanup budget (see imcommon.model.cancellation).
        self.cleanup_timed_out: bool = False
    
    def mark_started(self):
        """Reset the start time (a deferred run starts later than it was
        requested)."""
        self.started_at = datetime.now()

    def mark_succeeded(self):
        """Mark the run as successfully completed."""
        self.status = ScriptRunStatus.SUCCEEDED
        self.ended_at = datetime.now()
    
    def mark_failed(self, error: str):
        """Mark the run as failed with an error."""
        self.status = ScriptRunStatus.FAILED
        self.error = error
        self.ended_at = datetime.now()
    
    def mark_cancelled(self):
        """Mark the run as cancelled."""
        self.status = ScriptRunStatus.CANCELLED
        self.ended_at = datetime.now()
    
    def append_stdout(self, text: str):
        """Append text to stdout."""
        self.stdout += text
    
    def __repr__(self):
        return (f"ScriptRunResult(id={self.id}, status={self.status.value}, "
                f"script_path={self.script_path})")


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
