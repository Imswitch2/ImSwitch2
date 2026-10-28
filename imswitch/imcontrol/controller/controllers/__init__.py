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
    "FlipMirrorController": "FlipMirrorController",
    "FocusLockController": "FocusLockController",
    "ImageController": "ImageController",
    "HardwareStatusController": "HardwareStatusController",
    "LaserController": "LaserController",
    "LifetimeController": "LifetimeController",
    "LightSheetMulticolorController": "LightSheetMulticolorController",
    "LeicaStandController": "LeicaStandController",
    "LineProfileController": "LineProfileController",
    "LiveReconController": "LiveReconController",
    "MotCorrController": "MotCorrController",
    "PositionerController": "PositionerController",
    "RecordingController": "RecordingController",
    "RotationScanController": "RotationScanController",
    "RotatorController": "RotatorController",
    "ScanControllerAdvanced": "ScanControllerAdvanced",
    "ScanControllerBase": "ScanControllerBase",
    "ScanControllerExpert": "ScanControllerExpert",
    "ScanControllerMoNaLISA": "ScanControllerMoNaLISA",
    "ScanControllerPointScan": "ScanControllerPointScan",
    "ScanControllerSimplePointScan": "ScanControllerSimplePointScan",
    "ScanControllerSimpleMoNaLISA": "ScanControllerSimpleMoNaLISA",
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
    "ReservationController": "ReservationController",
    "InstrumentsController": "InstrumentsController",
}


def resolve_controller(name):
    """Return the controller class called ``name``, whatever the package attribute holds.

    Every controller module carries the name of the class it defines, and importing
    ``...controllers.FlipMirrorController`` (the module) binds that very name on this
    package. Which of the two ``controllers.FlipMirrorController`` is therefore depends
    on import order, and ``import pkg.Module as module`` binds through the same
    attribute, so no attribute-level trick can serve both readers. Code that needs the
    *class* (the controller factory) asks here instead: the module is imported from
    ``_CONTROLLER_MODULES`` and the class read off it.
    """
    try:
        module_name = _CONTROLLER_MODULES[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no controller {name!r}") from exc
    module = import_module(f".{module_name}", __name__)
    return getattr(module, name)


def __getattr__(name):
    # Reached only while the attribute is unbound, i.e. for the first
    # ``from ...controllers import X``. (``from pkg import X`` probes with ``hasattr``
    # first, which imports the submodule and binds the *module*; storing the class
    # here makes the import statement's own lookup see the class.) This is the plain
    # lazy export; code that must get the class regardless of import order calls
    # ``resolve_controller``.
    controller_class = resolve_controller(name)
    globals()[name] = controller_class
    return controller_class


__all__ = ["resolve_controller", *_CONTROLLER_MODULES]
