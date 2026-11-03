"""The editor's document (now :mod:`imswitch.imcommon.model.workfloweditor.document`),
plus the one way into it that is ImProcess's: a document from a result's
provenance, :func:`document_from_result`, also reachable as the
``WorkflowDocument.from_result`` classmethod it always was."""

from __future__ import annotations

from imswitch.imcommon.model.workfloweditor.document import *  # noqa: F401,F403
from imswitch.imcommon.model.workfloweditor.document import (  # noqa: F401 - names callers import
    _ID_PATTERN,
    _ID_PREFIX,
    _KEEP,
    _same,
    WorkflowDocument,
    WorkflowError,
)
from imswitch.imcommon.model.workfloweditor.document import __all__ as _ALL


def document_from_result(result, catalog, *, name: str | None = None) -> WorkflowDocument:
    """The workflow that made ``result``, from its provenance, minimized.

    Raises :class:`~imswitch.improcess.workflows.replay.ReplayError` when
    the record cannot be run again, listing every reason. A step done by
    hand in Fiji or a napari plugin is not such a reason: it becomes an
    input to bind (shown with no path) and a note says so.
    """
    from imswitch.improcess.model.provenance import graph_of
    from imswitch.improcess.model.save_protocol import ProvenanceDocument
    from imswitch.improcess.workflows.replay import workflow_from_provenance_around_interactive_steps

    graph = graph_of(result)
    if graph is None:
        raise WorkflowError(f"'{getattr(result, 'name', 'result')}' carries no provenance")
    replay = workflow_from_provenance_around_interactive_steps(
        ProvenanceDocument(graph=graph), registry=catalog.registry, name=name,
    )
    document = WorkflowDocument(replay.workflow, catalog=catalog)
    document.minimize_params()
    document.notes = [str(warning) for warning in replay.warnings]
    document.inputs = list(replay.inputs)
    document.interactive = list(replay.interactive)
    document.dirty = True
    return document


def _from_result(cls, result, catalog, *, name: str | None = None):
    """``WorkflowDocument.from_result``: see :func:`document_from_result`."""
    return document_from_result(result, catalog, name=name)


_from_result.__doc__ = document_from_result.__doc__
WorkflowDocument.from_result = classmethod(_from_result)

__all__ = list(_ALL) + ["document_from_result"]


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
