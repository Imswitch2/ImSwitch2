"""Which trajectory scripts may run on this computer (docs/design/plans/
scan-engines-and-expert-scan.md §7.10).

A trajectory script is Python, run unsandboxed when its program is compiled,
and a scan file can carry one. Loading a file -- or restoring the panel at
startup -- must not run code nobody here has seen. A script runs only once it
has been approved on this computer: written in the Make… form, or shown and
approved when a file brought it. Approved code is remembered by its SHA-256
(line endings ignored, so a copy saved on another system counts as the same
code) in ``ImSwitchConfig/config/approved_scan_scripts.json``, so the same
script does not ask again; changed code is new code and asks again.

Drop-in pattern files (``scan_patterns``) are not gated here: they are
installed by hand, as ImProcess's drop-ins are.

Qt-free.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import tempfile
from typing import Optional

from .program import ProgramError

#: The pattern whose parameters hold code.
SCRIPT_PATTERN = 'script'

FORMAT = 'approved-scan-scripts/1'
FILENAME = 'approved_scan_scripts.json'

_logger = logging.getLogger(__name__)


def script_sha256(code) -> str:
    """The SHA-256 of ``code`` with its line endings made ``\\n``."""
    text = str(code).replace('\r\n', '\n').replace('\r', '\n')
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class ScriptNotApproved(ProgramError):
    """The program's trajectory script has not been approved on this computer,
    so it was not run."""

    def __init__(self, code):
        self.code = str(code)
        self.sha256 = script_sha256(code)
        super().__init__(
            'This program holds a trajectory script (Python) that has not been approved on '
            'this computer, so it was not run. Draw or Run shows it and asks; its template '
            'shows it too.')


class ScriptApprovals:
    """The approved scripts in one file. A file that cannot be read approves
    nothing (said in the log); approving then writes a readable one."""

    def __init__(self, path: str):
        self.path = path

    def _read(self) -> dict:
        try:
            with open(self.path, encoding='utf-8') as file:
                data = json.load(file)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            _logger.warning(f'{self.path} cannot be read ({error}); no trajectory script '
                            'counts as approved.')
            return {}
        if not isinstance(data, dict) or data.get('format') != FORMAT \
                or not isinstance(data.get('approved'), dict):
            _logger.warning(f'{self.path} is not a {FORMAT} file; no trajectory script counts '
                            'as approved.')
            return {}
        return data['approved']

    def approved(self, code) -> bool:
        return script_sha256(code) in self._read()

    def approve(self, code) -> None:
        approved = dict(self._read())
        digest = script_sha256(code)
        if digest in approved:
            return
        first = next((line.strip() for line in str(code).splitlines() if line.strip()), '')
        approved[digest] = {'approved': datetime.datetime.now().isoformat(timespec='seconds'),
                            'first_line': first}
        directory = os.path.dirname(self.path) or '.'
        os.makedirs(directory, exist_ok=True)
        # Written whole, then moved into place: a crash leaves the old file.
        handle, temporary = tempfile.mkstemp(dir=directory, suffix='.tmp')
        try:
            with os.fdopen(handle, 'w', encoding='utf-8') as file:
                json.dump({'format': FORMAT, 'approved': approved}, file, indent=2)
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.remove(temporary)
            except OSError:
                pass
            raise


_approvals: Optional[ScriptApprovals] = None


def approvals() -> ScriptApprovals:
    """This computer's approvals (``ImSwitchConfig/config``)."""
    global _approvals
    if _approvals is None:
        from imswitch.imcommon.model import dirtools
        _approvals = ScriptApprovals(os.path.join(dirtools.UserFileDirs.Config, FILENAME))
    return _approvals


def set_approvals(store: Optional[ScriptApprovals]) -> Optional[ScriptApprovals]:
    """Use ``store`` (``None``: this computer's again); returns the previous one."""
    global _approvals
    previous, _approvals = _approvals, store
    return previous


def unapproved_script(program) -> Optional[str]:
    """The code of ``program``'s trajectory script when it has not been
    approved, else ``None``."""
    trajectory = getattr(program, 'trajectory', None)
    if trajectory is None or trajectory.pattern != SCRIPT_PATTERN:
        return None
    code = str(trajectory.params.get('code', ''))
    return None if approvals().approved(code) else code


__all__ = ['FILENAME', 'FORMAT', 'SCRIPT_PATTERN', 'ScriptApprovals', 'ScriptNotApproved',
           'approvals', 'script_sha256', 'set_approvals', 'unapproved_script']


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
