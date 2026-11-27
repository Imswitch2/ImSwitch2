"""ImSwitch plugin API -- experimental part.

The scan-program model and the scan-engine interface
(docs/design/plans/scan-engines-and-expert-scan.md). A board joins ImSwitch
as a ``scan_engine`` device plugin: a manifest entry plus a class deriving
from :class:`ScanEngine` that declares its :class:`EngineCapabilities`.

Nothing here is a compatibility promise yet. It becomes part of the stable
``imswitch.pluginapi`` at M3 at the earliest, once two engines -- the NI-DAQ
and the simulated step board -- have passed the conformance suite and the
acquisition tests through it.
"""

from imswitch.imcontrol.model.scan_program import (
    DetectorBinding, DeviceDrive, DeviceFacts, Loop, Motion, RunContext, ScanProgram,
    Separation, TimelineEvent, Wait, expand, realize, validate, visits,
)
from imswitch.imcontrol.model.scan_program.conformance import (
    engine_log, expected_log, log_differences, trigger_problems,
)
from imswitch.imcontrol.model.scan_program.engine import (
    AnalogOut, DeviceChannels, EMULATED, EngineCapabilities, FeasibilityReport,
    FeatureVerdict, IMPOSSIBLE, NATIVE, ScanEngine, ScanEngineRefused, UNKNOWN,
    assess_program, setup_channels,
)
from imswitch.imcontrol.model.scan_program.policy import load_policy, resolve_policy
from imswitch.imcontrol.model.SetupInfo import ScanEngineInfo

__all__ = [
    'AnalogOut', 'DetectorBinding', 'DeviceChannels', 'DeviceDrive', 'DeviceFacts',
    'EMULATED', 'EngineCapabilities', 'FeasibilityReport', 'FeatureVerdict', 'IMPOSSIBLE',
    'Loop', 'Motion', 'NATIVE', 'RunContext', 'ScanEngine', 'ScanEngineInfo',
    'ScanEngineRefused', 'ScanProgram', 'Separation', 'TimelineEvent', 'UNKNOWN', 'Wait',
    'assess_program', 'engine_log', 'expand', 'expected_log', 'load_policy',
    'log_differences', 'realize', 'resolve_policy', 'setup_channels', 'trigger_problems',
    'validate', 'visits',
]
