"""Headless workflow primitives — ports of WFS workflows for use from the
ImSwitch scripting module. See docs/design/plans/wfs-workflows-port.md.
"""

from .calibration import CalibrationParams, CalibrationWorkflow
from .cwstarss import CWSTARSSParams, CWSTARSSWorkflow
from .defocus_scan import DefocusScanParams, DefocusScanWorkflow
from .facade import (
    CamFacade,
    LaserConFacade,
    MicroscopeFacade,
    RotatorFacade,
    RotatorPresets,
    StageConFacade,
    TrigFacade,
    ZStageConFacade,
    build_facade_from_master,
)
from .mock_facade import MockMicroscopeFacade, build_mock_facade
from .multi_well_tiling import MultiWellTilingParams, MultiWellTilingWorkflow
from .widefield_starss import WidefieldStarssParams, WidefieldStarssWorkflow
from .serial_cwstarss import SerialCWSTARSSParams, SerialCWSTARSSWorkflow
from .stitched_image import StitchedImage
from .tiling import TilingParams, TilingWorkflow
from .z_stack import ZStackParams, ZStackWorkflow

RecordingParams = WidefieldStarssParams
RecordingWorkflow = WidefieldStarssWorkflow

__all__ = [
    "CalibrationParams",
    "CalibrationWorkflow",
    "CWSTARSSParams",
    "CWSTARSSWorkflow",
    "CamFacade",
    "DefocusScanParams",
    "DefocusScanWorkflow",
    "LaserConFacade",
    "MicroscopeFacade",
    "MockMicroscopeFacade",
    "MultiWellTilingParams",
    "MultiWellTilingWorkflow",
    "RecordingParams",
    "RecordingWorkflow",
    "WidefieldStarssParams",
    "WidefieldStarssWorkflow",
    "RotatorFacade",
    "RotatorPresets",
    "SerialCWSTARSSParams",
    "SerialCWSTARSSWorkflow",
    "StageConFacade",
    "StitchedImage",
    "TilingParams",
    "TilingWorkflow",
    "TrigFacade",
    "ZStackParams",
    "ZStackWorkflow",
    "ZStageConFacade",
    "build_facade_from_master",
    "build_mock_facade",
]
