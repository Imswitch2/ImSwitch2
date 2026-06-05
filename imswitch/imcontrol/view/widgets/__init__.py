"""Lazy widget exports.

Import widget classes on demand so tests for a single widget do not import
optional Napari-backed widgets and their GUI dependencies.
"""

from importlib import import_module


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
    "SettingsWidget": "SettingsWidget",
    "SetupStatusWidget": "SetupStatusWidget",
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


__all__ = list(_WIDGET_MODULES)
