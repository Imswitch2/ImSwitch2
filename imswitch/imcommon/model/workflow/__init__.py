"""The workflow schema, host-independent.

:mod:`.steps` is what a workflow file contains (sources, reconstructions,
processing steps, saves, and the step-type registry) and the static checks
on it; :mod:`.sources` says where a workflow's input lives; :mod:`.output_spec`
asks a plugin which ports a run will produce. Nothing here imports Qt, a
plugin, or a module that opens data: ImProcess runs these workflows
(:mod:`imswitch.improcess.workflows`), and the editor
(:mod:`imswitch.imcommon.model.workfloweditor`,
:mod:`imswitch.imcommon.view.workfloweditor`) edits them for whichever
module supplies the catalog. Whatever a host must supply (how a save
format is spelled, whether a plugin may run headless, how a source handle
is closed) is asked of the *registry* object it hands in, duck-typed; see
:func:`.steps.validate`.
"""


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
