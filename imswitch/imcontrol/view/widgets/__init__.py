"""Lazy widget exports.

Import widget classes on demand so tests for a single widget do not import
optional Napari-backed widgets and their GUI dependencies.
"""

import sys
from importlib import import_module
from types import ModuleType


_WIDGET_MODULES = {
    "BSC203Widget": "BSC203Widget",
    "AlignAverageWidget": "AlignAverageWidget",
    "AlignmentLineWidget": "AlignmentLineWidget",
    "AlignXYWidget": "AlignXYWidget",
    "AutofocusWidget": "AutofocusWidget",
    "BeadRecWidget": "BeadRecWidget",
    "BFTimelapseWidget": "BFTimelapseWidget",
    "ConsoleWidget": "ConsoleWidget",
    "EtMonalisaWidget": "EtMonalisaWidget",
    "EtSTEDWidget": "EtSTEDWidget",
    "EtSnoutyWidget": "EtSnoutyWidget",
    # "EtWidget": "EtWidget",  # Prototype on old Monalisa machine.
    "FFTWidget": "FFTWidget",
    "FLIMHistWidget": "FLIMHistWidget",
    "FlipMirrorWidget": "FlipMirrorWidget",
    "FocusLockWidget": "FocusLockWidget",
    "ImageWidget": "ImageWidget",
    "LaserWidget": "LaserWidget",
    "LightSheetMulticolorWidget": "LightSheetMulticolorWidget",
    "LeicaStandWidget": "LeicaStandWidget",
    "LineProfileWidget": "LineProfileWidget",
    "MotCorrWidget": "MotCorrWidget",
    "PositionerWidget": "PositionerWidget",
    "RecordingWidget": "RecordingWidget",
    "RotationScanWidget": "RotationScanWidget",
    "RotatorWidget": "RotatorWidget",
    "ScanWidgetAdvanced": "ScanWidgetAdvanced",
    "ScanWidgetBase": "ScanWidgetBase",
    "ScanWidgetMoNaLISA": "ScanWidgetMoNaLISA",
    "ScanWidgetPointScan": "ScanWidgetPointScan",
    "ScanWidgetSimplePointScan": "ScanWidgetSimplePointScan",
    "SettingsWidget": "SettingsWidget",
    "SetupStatusWidget": "SetupStatusWidget",
    "SetupModesWidget": "SetupModesWidget",
    "SLMWidget": "SLMWidget",
    "SLMsWidget": "SLMsWidget",
    "TilingWidget": "TilingWidget",
    "TriggerScopeLSXYRWidget": "TriggerScopeLSXYRWidget",
    "TriggerScopeRasterWidget": "TriggerScopeRasterWidget",
    "TriggerScopePLSRWidget": "TriggerScopePLSRWidget",
    "TriggerScopePLSRMulticolorWidget": "TriggerScopePLSRMulticolorWidget",
    "TriggerScopeScanWidget": "TriggerScopeScanWidget",
    "TriggerScopeGalvoDetectionWidget": "TriggerScopeGalvoDetectionWidget",
    "ULensesWidget": "ULensesWidget",
    "ViewWidget": "ViewWidget",
    "ViewerToolsWidget": "ViewerToolsWidget",
    "WatcherWidget": "WatcherWidget",
    "WellPlateWidget": "WellPlateWidget",
    "WidgetFactory": "basewidgets",
}


def __getattr__(name):
    try:
        module_name = _WIDGET_MODULES[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc

    module = import_module(f".{module_name}", __name__)
    widget_class = getattr(module, name)
    globals()[name] = widget_class
    return widget_class


class _LazyExportModule(ModuleType):
    """Keeps like-named submodules from shadowing the lazy widget classes.

    Importing a submodule binds it onto this package (``from .ScanWidgetAdvanced
    import ScanWidgetAdvanced`` makes ``ScanWidgetAdvanced`` the module), after
    which ``getattr(package, 'ScanWidgetAdvanced')`` would hand back the module
    instead of the class. As in ``imswitch.imcontrol.view``: such bindings are
    ignored, and ``__getattr__`` resolves the class.
    """

    def __setattr__(self, name, value):
        if name in _WIDGET_MODULES and isinstance(value, ModuleType):
            return
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _LazyExportModule

__all__ = list(_WIDGET_MODULES)
