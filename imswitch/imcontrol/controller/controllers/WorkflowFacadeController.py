"""WorkflowFacadeController — API-only controller for building workflow facades.

This controller has no widget. It exists solely to provide a clean API method
for constructing a MicroscopeFacade without requiring user scripts to import
internal facade modules or access api._master directly.
"""

from imswitch.imcommon.model import APIExport
from ..basecontrollers import ImConWidgetController


class WorkflowFacadeController(ImConWidgetController):
    """API-only controller for building workflow facades.
    
    No widget required — this controller exists only to export
    build_facade_from_master via the API.
    """

    @APIExport()
    def build(self, **kwargs):
        """Build a MicroscopeFacade from the current master controller.
        
        Returns a facade object providing simplified access to hardware managers
        for use in headless workflow scripts.
        
        Args:
            **kwargs: Forwarded to build_facade_from_master. Common arguments:
                - laser_aliases (dict[str, str]): Map facade names to setupInfo laser names
                - detector_name (str): Name of detector/camera to expose
                - z_stage_name (str): Name of Z positioner to expose
                - rotation_stage_name (str): Name of rotation stage to expose
                - trig_device_name (str): Name of TTL trigger device
        
        Returns:
            MicroscopeFacade: Facade object with hardware manager wrappers.
        
        Example:
            >>> facade = api.workflowFacade.build(
            ...     laser_aliases={'488': 'Laser488', '405': 'Laser405'},
            ...     detector_name='Kiralux',
            ...     z_stage_name='Z-Piezo'
            ... )
            >>> facade.laser_con.set_constant_power('488', 50.0)
        """
        # Lazy import to avoid slowing down ImSwitch startup
        from imswitch.imcontrol.model.workflows.facade import build_facade_from_master
        
        return build_facade_from_master(self._master, **kwargs)


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
