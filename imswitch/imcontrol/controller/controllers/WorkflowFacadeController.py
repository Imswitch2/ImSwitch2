"""WorkflowFacadeController — API-only controller for building workflow facades.

This controller has no widget. It exists solely to provide a clean API method
for constructing a MicroscopeFacade without requiring user scripts to import
internal facade modules or access api._master directly.
"""

from typing import Any

from imswitch.imcommon.model import APIExport
from ..basecontrollers import ImConWidgetController


class WorkflowFacadeController(ImConWidgetController):
    """API-only controller for building workflow facades.
    
    No widget required — this controller exists only to export
    build_facade_from_master via the API.
    """

    def build(self, **kwargs: Any) -> Any:
        """Build a MicroscopeFacade from the current master controller.

        Args:
            **kwargs: Forwarded to ``build_facade_from_master``.

        Returns:
            MicroscopeFacade: facade object with WFS-shaped sub-facades.
        """
        # Lazy import to avoid slowing down ImSwitch startup
        from imswitch.imcontrol.model.workflows.facade import build_facade_from_master

        kwargs.setdefault("scan_workflow", getattr(self._commChannel, "scanWorkflow", None))
        kwargs.setdefault("scan_done_signal", getattr(self._commChannel, "sigScanDone", None))
        return build_facade_from_master(self._master, **kwargs)

    @APIExport()
    def buildWorkflowFacade(self, **kwargs: Any) -> Any:
        """Build a MicroscopeFacade from the current master controller.

        ImSwitch's ``generateAPI`` flattens every ``@APIExport``'d method to
        the top level of ``api.imcontrol`` — so this method is reachable as
        ``api.imcontrol.buildWorkflowFacade(...)`` (not via a sub-namespace).

        Args:
            **kwargs: Forwarded to ``build_facade_from_master``. Common arguments:
                - ``laser_aliases`` (dict[str, str]): logical → setup laser names
                - ``detector_name`` (str): name of detector/camera to expose
                - ``xy_positioner_name`` (str): name of XY positioner
                - ``z_positioner_name`` (str): name of Z positioner
                - ``time_resolved_detector_name`` (str): detector implementing
                  the time-resolved contract
                - ``hwp_name`` / ``qwp_name`` (str): rotator names
                - ``hwp_presets`` / ``qwp_presets`` (RotatorPresets)
                - ``scan_workflow`` / ``scan_done_signal``: optional overrides;
                  by default these are taken from the communication channel

        Returns:
            MicroscopeFacade: facade object with WFS-shaped sub-facades.

        Example:
            >>> facade = api.imcontrol.buildWorkflowFacade(
            ...     laser_aliases={'488': '488 (EXC) sn27311',
            ...                    '405': '405 (ACT) sn26647'},
            ...     detector_name='Kiralux',
            ...     xy_positioner_name='XY',
            ...     z_positioner_name='Z',
            ...     hwp_name='HWP', qwp_name='QWP',
            ... )
            >>> facade.laser_con.set_constant_power(['488'], [50.0])
        """
        return self.build(**kwargs)


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
