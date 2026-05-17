# Dynamic Napari Layer Lifecycle — Plan

**Status:** NOT IMPLEMENTED (verified 2026-05-17)  
**Current code:** `ImageController.__init__` still calls `setLiveViewLayers` once at init with all `forAcquisition` detectors (see line 20-22).  
**Proposal:** Replace with lazy per-detector layer creation on first `sigUpdateImage`.

---

## Problem

Every `forAcquisition` detector gets a permanent napari layer created at startup
(`ImageController.__init__` → `setLiveViewLayers(getAllDeviceNames(forAcquisition))`).
Layers are never removed, so idle detectors (e.g. APD while doing live view,
Hamamatsu while doing a scan) always clutter the layer list and interfere with
"Update levels" / colormap operations.

---

## Signal inventory

| Signal | Where defined | Carries detector name? | Notes |
|---|---|---|---|
| `sigAcquisitionStarted` | `DetectorsManager` → `CommunicationChannel` | No (global) | Fires when first handle opens |
| `sigAcquisitionStopped` | `DetectorsManager` → `CommunicationChannel` | No (global) | Fires when last handle closes |
| `sigUpdateImage(detectorName, im, init, scale, isCurrent)` | `CommunicationChannel` | Yes | Fires per-detector on every new frame |
| `forAcquisition` | `DetectorManager` property | N/A | Static config flag, not runtime state |

**Key gap:** no per-detector "started/stopped" signal exists today. There is no way
to know at start time which detectors will actually produce frames in a given
acquisition mode (APD only fires during scans; Hamamatsu fires during live view).

---

## Chosen approach — lazy creation via sigUpdateImage

### Layer creation
Create the napari layer **lazily** on the first `sigUpdateImage` for a given
detector name. Until a frame actually arrives, no layer exists. This is truthful
and requires no new signals.

### Layer removal
On `sigAcquisitionStopped`, schedule removal of all live layers after a short
debounce (≈ 500 ms) so the last frame stays visible. Live-view layers follow the
same rule when the live view handle closes.

### Why not use sigAcquisitionStarted for creation?
It carries no detector name, so we'd have to create layers for all `forAcquisition`
detectors — recreating the current problem.

---

## Implementation sketch

### ImageController changes

```python
def __init__(...):
    # Remove the upfront setLiveViewLayers call — layers are created on demand
    self._activeLayers: set[str] = set()
    self._commChannel.sigUpdateImage.connect(self.update)
    self._commChannel.sigAcquisitionStopped.connect(self._scheduleLayerCleanup)

def update(self, detectorName, im, init, scale, isCurrentDetector):
    if detectorName not in self._activeLayers:
        self._widget.addLiveLayer(detectorName)   # new method
        self._activeLayers.add(detectorName)
    # ... existing update logic ...

def _scheduleLayerCleanup(self):
    QTimer.singleShot(500, self._removeLiveLayers)

def _removeLiveLayers(self):
    for name in list(self._activeLayers):
        self._widget.removeLiveLayer(name)        # new method
        self._activeLayers.discard(name)
```

### ImageWidget changes

```python
def addLiveLayer(self, name):
    """Create a live layer for detector `name` (called on first frame)."""
    # Same logic as current setLiveViewLayers but for a single detector

def removeLiveLayer(self, name):
    """Remove the live layer for detector `name`."""
    if name in self.imgLayers:
        self.napariViewer.layers.remove(self.imgLayers[name], force=True)
        del self.imgLayers[name]
```

`setLiveViewLayers` can be kept for any code that still calls it, or removed
once all callers are migrated.

---

## Contrast limits fix (already applied)

`NapariUpdateLevelsWidget._on_update_levels` now:
1. Uses `viewer.layers.selection` (not `.selected` — napari ≥ 0.4.15 API)
2. Expands `contrast_limits_range` before setting `contrast_limits` so integer-
   count detectors (APD, PMT) are not clamped to [0, 1]

---

## Files to change

| File | Change |
|---|---|
| `imswitch/imcontrol/controller/controllers/ImageController.py` | Remove upfront `setLiveViewLayers`; add lazy creation + `sigAcquisitionStopped` cleanup |
| `imswitch/imcontrol/view/widgets/ImageWidget.py` | Add `addLiveLayer` / `removeLiveLayer`; keep `setLiveViewLayers` as no-op or remove |

No new signals needed. No changes to `DetectorsManager` or `CommunicationChannel`.

---

## Open questions before implementing

1. **Live view persistence**: should the layer stay visible between acquisitions
   if live view is still running? Probably yes — only remove on full acquisition
   stop, not just scan stop.
2. **Colormap assignment**: currently done in `setLiveViewLayers` using the
   detector name as colormap key. Needs to carry over to lazy creation.
3. **`protected` flag**: layers are created with `protected=True` to prevent
   accidental deletion. Keep this.
4. **Multi-detector simultaneous view**: two detectors can both be live (e.g.
   Hamamatsu + APD during a confocal scan). Both layers should coexist.
   The `_activeLayers` set already handles this correctly.
