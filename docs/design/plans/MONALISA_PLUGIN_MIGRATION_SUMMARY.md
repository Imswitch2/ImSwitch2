# MoNaLISA Plugin Migration Summary

**Date:** 2026-05-28  
**Milestone:** Phase B.2 Complete  
**Commit:** 103b8388

## Overview

The MoNaLISA structured illumination microscopy (SIM) reconstructor has been successfully migrated from a hard-coded controller component to a standalone plugin that implements the `Reconstructor` base contract. This completes Phase B.2 of the ImProcess generalization refactor.

## What Was Done

### 1. Plugin Structure Created

```
imswitch/improcess/reconstructors/monalisa/
├── __init__.py              # Package exports
├── reconstructor.py         # Main MonalisaReconstructor class
├── params_widget.py         # Parameter editor widget (scaffold)
├── result.py                # MonalisaProcessingResult dataclass
├── coeffs_to_image.py       # Coefficient to image conversion
├── pattern_finder.py        # Pattern detection (from model/)
├── signal_extractor.py      # CUDA signal extraction (from model/)
└── denoiser.py              # Noise reduction (from model/)
```

### 2. Core Components

#### MonalisaReconstructor (reconstructor.py)

Implements the three required abstract methods:

- **`make_param_widget(parent)`**: Returns `MonalisaParamsWidget` for parameter editing
- **`make_metadata_dialog(parent)`**: Stub (returns `None` for now; scan params handled separately during transition)
- **`process(data_obj, params)`**: Full reconstruction pipeline:
  1. Load data from `DataObj`
  2. Apply bleaching correction (optional)
  3. Build illumination pattern from offsets/periods
  4. Configure background modeling (Constant/Gaussian/None)
  5. Extract signal coefficients (CUDA-accelerated)
  6. Convert coefficients to 6D image array
  7. Return `MonalisaProcessingResult` with metadata

Class attributes:
- `id = "monalisa"` - Stable identifier for registry lookups
- `name = "MoNaLISA"` - Human-readable display name
- `file_extensions = ["hdf5", "zarr"]` - Auto-selection hints

#### MonalisaParamsWidget (params_widget.py)

Scaffold widget with:
- `get_values() -> dict` interface (returns parameter dictionary)
- TODOs for actual parameter tree implementation
- Will replace hard-coded `ReconParTree` access in controller

#### MonalisaProcessingResult (result.py)

Extends `ProcessingResult` with:
- `scan_params` dict (dimensions, directions, steps, step_sizes, unidirectional)
- `display_levels` tuple (min, max) for auto-contrast
- 6D data shape: `(datasets, bases, timepoints, slices, rows, cols)`

### 3. Model Migration

The following classes were copied from `imswitch/improcess/model/` with minimal modifications:

- **PatternFinder**: Automatic pattern detection in SIM data
- **SignalExtractor**: CUDA-accelerated coefficient extraction (Windows-only)
- **Denoiser**: Noise reduction algorithms
- **coeffs_to_image**: Coefficient reassignment logic

### 4. Platform Compatibility

**Problem:** `SignalExtractor` requires Windows-specific CUDA DLLs, causing import failures on macOS/Linux.

**Solution:** Lazy initialization pattern:
```python
def __init__(self):
    self._signal_extractor = None  # Lazy-loaded on first use

def _ensure_signal_extractor(self):
    """Lazy-load SignalExtractor (Windows-only, requires CUDA DLLs)."""
    if self._signal_extractor is None:
        try:
            self._signal_extractor = SignalExtractor()
        except RuntimeError as e:
            raise RuntimeError(
                f'SignalExtractor initialization failed: {e}. '
                'MoNaLISA reconstruction requires Windows + CUDA libraries.'
            ) from e
```

This allows the plugin to register successfully on all platforms, with a clear error message if reconstruction is attempted without CUDA support.

### 5. Registry Integration

The plugin is automatically registered at module startup:

```python
# imswitch/improcess/reconstructors/__init__.py
from .monalisa import MonalisaReconstructor

def register_default_reconstructors(registry: PluginRegistry) -> None:
    registry.register_reconstructor(MonalisaReconstructor())
```

Retrieval by ID:
```python
from imswitch.improcess.reconstructors import get_registry
registry = get_registry()
monalisa = registry.get_reconstructor('monalisa')
```

## Testing

All tests passed:

1. **Plugin registration**: ✓ Registers as `monalisa` with name `MoNaLISA`
2. **Auto-selection by ID**: ✓ `registry.get_reconstructor('monalisa')` returns plugin
3. **Method presence**: ✓ All abstract methods implemented
4. **Cross-platform instantiation**: ✓ Works on macOS (Windows CUDA fails gracefully)

## Known Limitations

1. **Windows-only processing**: SignalExtractor requires CUDA DLLs (existing limitation, not introduced by this refactor)
2. **Metadata dialog stub**: `make_metadata_dialog()` returns `None`; scan parameters currently handled by controller
3. **Params widget scaffold**: `MonalisaParamsWidget.get_values()` returns placeholder dict; full UI implementation pending

## What's Next (Phase B.3)

1. **Controller integration**: Wire plugin to `ImProcessMainViewController`
   - Replace hard-coded `self._reconObj` access with plugin registry
   - Call `plugin.make_param_widget()` instead of instantiating `ReconParTree`
   - Call `plugin.process(data_obj, params)` instead of `self._reconObj.reconstructData()`

2. **Params widget implementation**: Populate `MonalisaParamsWidget` with actual parameter tree UI

3. **Metadata dialog migration**: Wrap `ScanParamsDialog` in plugin-specific `MonalisaScanParamsDialog`

4. **Legacy cleanup**: Remove old model files once controller is fully migrated

## Architecture Benefits

✅ **Separation of concerns**: Reconstruction logic isolated from controller  
✅ **Extensibility**: New reconstructors can be added without modifying core controller  
✅ **Testability**: Plugin can be tested in isolation  
✅ **Platform compatibility**: Graceful degradation on unsupported platforms  
✅ **Type safety**: Clean contract defined by `Reconstructor` base class  

## Files Modified

- `imswitch/improcess/reconstructors/__init__.py` - Import and register MoNaLISA
- `imswitch/improcess/reconstructors/monalisa/__init__.py` - NEW
- `imswitch/improcess/reconstructors/monalisa/reconstructor.py` - NEW
- `imswitch/improcess/reconstructors/monalisa/params_widget.py` - NEW
- `imswitch/improcess/reconstructors/monalisa/result.py` - NEW
- `imswitch/improcess/reconstructors/monalisa/coeffs_to_image.py` - NEW
- `imswitch/improcess/reconstructors/monalisa/pattern_finder.py` - NEW
- `imswitch/improcess/reconstructors/monalisa/signal_extractor.py` - NEW
- `imswitch/improcess/reconstructors/monalisa/denoiser.py` - NEW

## Backward Compatibility

✅ **No breaking changes**: Old controller code still works (plugin not yet wired)  
✅ **Coexistence period**: Plugin and legacy code can coexist during transition  
✅ **Gradual migration**: Controller methods can be migrated one at a time  

---

**Status**: ✅ **COMPLETE** - MoNaLISA plugin ready for controller integration
