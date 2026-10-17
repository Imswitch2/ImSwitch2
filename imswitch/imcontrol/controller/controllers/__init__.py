"""Lazy controller exports.

Importing this package used to import every controller eagerly, which made
single-controller unit tests load optional GUI and hardware dependencies during
collection. Resolve controller classes on demand instead.
"""

from importlib import import_module


_CONTROLLER_MODULES = {
    "BSC203Controller": "BSC203Controller",
    "AlignAverageController": "AlignAverageController",
    "AlignmentLineController": "AlignmentLineController",
    "AlignXYController": "AlignXYController",
    "AutofocusController": "AutofocusController",
    "BeadRecController": "BeadRecController",
    "BFTimelapseController": "BFTimelapseController",
    "ConsoleController": "ConsoleController",
    "EtMonalisaController": "EtMonalisaController",
    "EtSTEDController": "EtSTEDController",
    "EtSnoutyController": "EtSnoutyController",
    # "EtController": "EtController",  # Prototype on old Monalisa machine.
    "FFTController": "FFTController",
    "FLIMHistController": "FLIMHistController",
    "FlipMirrorController": "FlipMirrorController",
    "FocusLockController": "FocusLockController",
    "ImageController": "ImageController",
    "HardwareStatusController": "HardwareStatusController",
    "LaserController": "LaserController",
    "LightSheetMulticolorController": "LightSheetMulticolorController",
    "LeicaStandController": "LeicaStandController",
    "LineProfileController": "LineProfileController",
    "MotCorrController": "MotCorrController",
    "PositionerController": "PositionerController",
    "RecordingController": "RecordingController",
    "RotationScanController": "RotationScanController",
    "RotatorController": "RotatorController",
    "ScanControllerAdvanced": "ScanControllerAdvanced",
    "ScanControllerBase": "ScanControllerBase",
    "ScanControllerMoNaLISA": "ScanControllerMoNaLISA",
    "ScanControllerPointScan": "ScanControllerPointScan",
    "SettingsController": "SettingsController",
    "SetupStatusController": "SetupStatusController",
    "SetupModesController": "SetupModesController",
    "SLMController": "SLMController",
    "SLMsController": "SLMsController",
    "TilingController": "TilingController",
    "TriggerScopeRasterController": "TriggerScopeRasterController",
    "TriggerScopePLSRController": "TriggerScopePLSRController",
    "TriggerScopeGalvoDetectionController": "TriggerScopeGalvoDetectionController",
    "TriggerScopePLSRMulticolorController": "TriggerScopePLSRMulticolorController",
    "TriggerScopeScanController": "TriggerScopeScanController",
    "TriggerScopeLSXYRController": "TriggerScopeLSXYRController",
    "ULensesController": "ULensesController",
    "ViewController": "ViewController",
    "ViewerToolsController": "ViewerToolsController",
    "WatcherController": "WatcherController",
    "WellPlateController": "WellPlateController",
    "WorkflowFacadeController": "WorkflowFacadeController",
}


def __getattr__(name):
    try:
        module_name = _CONTROLLER_MODULES[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc

    module = import_module(f".{module_name}", __name__)
    controller_class = getattr(module, name)
    globals()[name] = controller_class
    return controller_class


__all__ = list(_CONTROLLER_MODULES)
