__imswitch_module__ = True
__title__ = 'Image Processing'


def getMainViewAndController(moduleCommChannel, *_args, **_kwargs):
    import os
    from imswitch.imcommon.model import dirtools
    os.environ['PATH'] = os.environ['PATH'] + ';' + dirtools.DataFileDirs.Libs

    from .controller import ImProcessMainController
    from .model.processing_config import (
        is_colocalization_panel_enabled,
        is_frc_panel_enabled,
        is_graph_panel_enabled,
        is_profile_panel_enabled,
        is_psf_resolution_panel_enabled,
        is_projection_panel_enabled,
        is_roi_manager_panel_enabled,
        is_roi_stats_panel_enabled,
        is_segmentation_panel_enabled,
        load_processing_config,
    )
    from .view import ImProcessMainView

    processing_config = load_processing_config()
    view = ImProcessMainView(
        showGraphPanel=is_graph_panel_enabled(processing_config),
        showProfilePanel=is_profile_panel_enabled(processing_config),
        showFRCPanel=is_frc_panel_enabled(processing_config),
        showROIStatsPanel=is_roi_stats_panel_enabled(processing_config),
        showROIManagerPanel=is_roi_manager_panel_enabled(processing_config),
        showProjectionPanel=is_projection_panel_enabled(processing_config),
        showSegmentationPanel=is_segmentation_panel_enabled(processing_config),
        showPSFResolutionPanel=is_psf_resolution_panel_enabled(processing_config),
        showColocalizationPanel=is_colocalization_panel_enabled(processing_config),
    )
    try:
        controller = ImProcessMainController(
            view, moduleCommChannel, processingConfig=processing_config
        )
    except Exception as e:
        view.close()
        raise e

    return view, controller


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
