"""The editor's run session (now :mod:`imswitch.imcommon.model.workfloweditor.session`),
plus :func:`execute`, which runs a plan through ImProcess's runner.

``RunSession(registry)`` with ImProcess's ``PluginRegistry`` closes source
handles and names a run's files the way the runner does: the session asks
the registry for ``close_source`` and ``source_stem_of``.
"""

from __future__ import annotations

from imswitch.imcommon.model.workfloweditor.previews import make_preview
from imswitch.imcommon.model.workfloweditor.session import *  # noqa: F401,F403
from imswitch.imcommon.model.workfloweditor.session import (  # noqa: F401 - names callers import
    RunPlan,
    _digest,
    _jsonable,
    _step_or_none,
    _unique,
)
from imswitch.imcommon.model.workfloweditor.session import __all__ as _ALL


def execute(plan: RunPlan, registry, *, on_step=None, progress=None, cancel=None, preview_size: int = 192,
            interrupts=None):
    """Run ``plan`` on the calling thread; returns ``(report, error)``.

    ``on_step(step_id, ports, seconds, cached, previews)`` is called as each
    step finishes, with previews made here (a worker thread, in the editor).
    ``interrupts(False)`` is called first: a step that finished is no longer
    one a Stop may interrupt (the caller allows it again at the next step).
    """
    from imswitch.imcommon.model import OperationCancelled
    from imswitch.improcess.workflows.runner import RunError, run

    def done(step_id, ports, seconds, cached):
        if interrupts is not None:
            interrupts(False)
        if on_step is None:
            return
        previews = {} if cached else {port: make_preview(result, preview_size) for port, result in ports.items()}
        on_step(step_id, ports, seconds, cached, previews)

    try:
        report = run(
            plan.workflow, registry=registry, bindings=plan.bindings, out_dir=plan.out_dir,
            overwrite=plan.overwrite, write_saves=plan.write_saves, source_stem=plan.source_stem,
            source_root=plan.source_root, progress=progress, cancel=cancel, on_step_done=done,
            step_state=plan.step_state,
        )
    except RunError as exc:
        return getattr(exc, "report", None), str(exc)
    except OperationCancelled:
        # Landed outside a step (the runner turns one inside a step into a
        # RunError with its report): the run is over, cancelled.
        return None, "cancelled"
    except Exception as exc:  # noqa: BLE001 - reported in the editor
        return None, str(exc)
    return report, None


__all__ = list(_ALL) + ["execute"]


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
