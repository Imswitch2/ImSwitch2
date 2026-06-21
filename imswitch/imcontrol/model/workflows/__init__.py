"""Headless workflow primitives — ports of WFS workflows for use from the
ImSwitch scripting module. See docs/design/plans/wfs-workflows-port.md.
"""

from .calibration import CalibrationParams, CalibrationWorkflow
from .cwstarss import CWSTARSSParams, CWSTARSSWorkflow
from .defocus_scan import DefocusScanParams, DefocusScanWorkflow
from .event_probe import (
    EventProbe,
    EventProbeParams,
    EventProbeResult,
    EventGatedAcquisitionCallback,
    EventGatedAcquisitionResult,
)
from .facade import (
    CamFacade,
    LaserConFacade,
    MicroscopeFacade,
    RotatorFacade,
    RotatorPresets,
    ScanWorkflowFacade,
    StageConFacade,
    TimeResolvedDetectorFacade,
    TrigFacade,
    ZStageConFacade,
    build_facade_from_master,
)
from imswitch.imcontrol.model.timeresolved import (
    GateSpec,
    LifetimeFitConfig,
    TimeResolvedScanConfig,
    TimeResolvedScanProducts,
)
from .mock_facade import MockMicroscopeFacade, build_mock_facade
from .multi_well_tiling import MultiWellTilingParams, MultiWellTilingWorkflow
from .widefield_starss import WidefieldStarssParams, WidefieldStarssWorkflow
from .serial_cwstarss import SerialCWSTARSSParams, SerialCWSTARSSWorkflow
from .stitched_image import StitchedImage
from .target import Target, TargetList
from .target_timelapse import (
    ModeConfig,
    TargetTimelapseParams,
    TargetTimelapseWorkflow,
    TargetTimelapseResult,
    TargetAcquisitionResult,
)
from .smart_mode_workflow import (
    ModeTransition,
    SmartModeWorkflowAdapter,
)
from .time_resolved import (
    BinnedPhotonArrivalParams,
    BinnedPhotonArrivalWorkflow,
    GatedSTEDParams,
    GatedSTEDWorkflow,
    TauSTEDParams,
    TauSTEDWorkflow,
    TimeResolvedScanWorkflow,
    TimeResolvedWorkflowParams,
    TimeResolvedWorkflowResult,
)
from .tiling import TilingParams, TilingWorkflow
from .z_stack import ZStackParams, ZStackWorkflow

RecordingParams = WidefieldStarssParams
RecordingWorkflow = WidefieldStarssWorkflow

__all__ = [
    "CalibrationParams",
    "CalibrationWorkflow",
    "BinnedPhotonArrivalParams",
    "BinnedPhotonArrivalWorkflow",
    "CWSTARSSParams",
    "CWSTARSSWorkflow",
    "CamFacade",
    "DefocusScanParams",
    "DefocusScanWorkflow",
    "EventProbe",
    "EventProbeParams",
    "EventProbeResult",
    "EventGatedAcquisitionCallback",
    "EventGatedAcquisitionResult",
    "GateSpec",
    "GatedSTEDParams",
    "GatedSTEDWorkflow",
    "LaserConFacade",
    "LifetimeFitConfig",
    "MicroscopeFacade",
    "ModeConfig",
    "ModeTransition",
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
    "ScanWorkflowFacade",
    "SmartModeWorkflowAdapter",
    "StageConFacade",
    "StitchedImage",
    "Target",
    "TargetList",
    "TargetAcquisitionResult",
    "TargetTimelapseParams",
    "TargetTimelapseResult",
    "TargetTimelapseWorkflow",
    "TauSTEDParams",
    "TauSTEDWorkflow",
    "TimeResolvedDetectorFacade",
    "TimeResolvedScanConfig",
    "TimeResolvedScanWorkflow",
    "TimeResolvedScanProducts",
    "TimeResolvedWorkflowParams",
    "TimeResolvedWorkflowResult",
    "TilingParams",
    "TilingWorkflow",
    "TrigFacade",
    "ZStackParams",
    "ZStackWorkflow",
    "ZStageConFacade",
    "build_facade_from_master",
    "build_mock_facade",
]
