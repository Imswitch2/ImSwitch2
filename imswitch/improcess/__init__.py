__imswitch_module__ = True
__title__ = 'Image Processing'


def getMainViewAndController(moduleCommChannel, *_args, **_kwargs):
    import os
    from imswitch.imcommon.model import dirtools
    os.environ['PATH'] = os.environ['PATH'] + ';' + dirtools.DataFileDirs.Libs

    from .controller import ImProcessMainController
    from .model.processing_config import (
        are_napari_layer_controls_enabled,
        is_actions_panel_enabled,
        is_colocalization_panel_enabled,
        is_current_data_panel_enabled,
        is_file_watcher_panel_enabled,
        is_frc_panel_enabled,
        is_graph_panel_enabled,
        is_metadata_panel_enabled,
        is_multi_data_panel_enabled,
        is_multicolor_panel_enabled,
        is_napari_storm_viewer_enabled,
        is_parameter_panel_enabled,
        is_profile_panel_enabled,
        is_psf_resolution_panel_enabled,
        is_projection_panel_enabled,
        is_reconstruction_panel_enabled,
        is_results_panel_enabled,
        is_roi_manager_panel_enabled,
        is_roi_stats_panel_enabled,
        is_segmentation_panel_enabled,
        load_processing_config,
    )
    from .view import ImProcessMainView

    processing_config = load_processing_config()
    view = ImProcessMainView(
        showParameterPanel=is_parameter_panel_enabled(processing_config),
        showNapariLayerControls=are_napari_layer_controls_enabled(processing_config),
        showReconstructionPanel=is_reconstruction_panel_enabled(processing_config),
        showActionsPanel=is_actions_panel_enabled(processing_config),
        showFileWatcherPanel=is_file_watcher_panel_enabled(processing_config),
        showMultiDataPanel=is_multi_data_panel_enabled(processing_config),
        showCurrentDataPanel=is_current_data_panel_enabled(processing_config),
        showResultsPanel=is_results_panel_enabled(processing_config),
        showGraphPanel=is_graph_panel_enabled(processing_config),
        showProfilePanel=is_profile_panel_enabled(processing_config),
        showMetadataPanel=is_metadata_panel_enabled(processing_config),
        showFRCPanel=is_frc_panel_enabled(processing_config),
        showROIStatsPanel=is_roi_stats_panel_enabled(processing_config),
        showROIManagerPanel=is_roi_manager_panel_enabled(processing_config),
        showProjectionPanel=is_projection_panel_enabled(processing_config),
        showSegmentationPanel=is_segmentation_panel_enabled(processing_config),
        showPSFResolutionPanel=is_psf_resolution_panel_enabled(processing_config),
        showColocalizationPanel=is_colocalization_panel_enabled(processing_config),
        showMulticolorPanel=is_multicolor_panel_enabled(processing_config),
        useNapariStormViewer=is_napari_storm_viewer_enabled(processing_config),
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
