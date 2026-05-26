# Widget State Persistence — Rollout Plan

## Framework status

Already implemented and working:
- **LaserController** — laser values, modulation settings, selected preset (not enable state)
- **ScanControllerBase** — positioner size/step/center, TTL timing, sequence time
- **SettingsController** — detector parameters, frame settings

The framework (`WidgetStatePersistence`) is complete and production-ready:
- Opt-in registration per controller
- Safety-first: hardware-active states (enable, running) are never auto-restored
- Version awareness and corruption handling
- Auto-save on shutdown, Ctrl+Shift+S/L for manual save/load
- Storage: `~/.imswitch/imcontrol_widget_states/<ControllerName>/`

---

## Known gaps in already-implemented controllers

### ScanControllerBase
Missing fields (worth adding before new controllers):
- Scan vs. Cont. Laser Pulses radio button selection
- Repeat checkbox state
- Scan dimension combo selections (which axis maps to which index)
- Advanced scan widget parameters (`ScanWidgetAdvanced`)

---

## Tier 1 — High value, used every session

| Controller | Widget | State to persist |
|---|---|---|
| ScanControllerBase (gap fill) | ScanWidgetBase | mode radio, repeat, dimension combos |
| AdvancedScanController (if separate) | ScanWidgetAdvanced | all advanced scan parameters |
| PositionerController | PositionerWidget | step size per axis |
| RecordingController | RecordingWidget | format, frames/time, save path, mode selection |

---

## Tier 2 — Commonly used

| Controller | Widget | State to persist |
|---|---|---|
| RotatorController | RotatorWidget | speed, step size |
| AutofocusController | AutofocusWidget | step, range, threshold |
| FocusLockController | FocusLockWidget | PID params (P/I/D), setpoint |
| ViewController | ViewWidget | colormap, display range (min/max) |

---

## Tier 3 — Specialised, add when needed

| Controller | Widget | State to persist |
|---|---|---|
| FFTController | FFTWidget | ROI selection, display mode |
| TilingController | TilingWidget | tiling parameters |
| WatcherController | WatcherWidget | watch folder path, processing settings |
| BeadRecController | BeadRecWidget | reconstruction parameters |
| AlignmentLineController | AlignmentLineWidget | line parameters |

---

## Explicitly excluded (not worth adding)

| Widget | Reason |
|---|---|
| ImageWidget | Napari manages its own state |
| ConsoleWidget | Session-only, nothing persistent |
| LineProfileWidget | No persistent settings; line position is napari layer state |
| ViewerToolsWidget | Tool mode is transient |
| LeicaStandWidget | Hardware state — unsafe to auto-restore |

---

## Implementation pattern (identical for every controller)

```python
def getWidgetState(self) -> dict:
    return {
        'version': 1,
        'someParam': self._widget.getSomeParam(),
        # ...
    }

def setWidgetState(self, state: dict) -> None:
    try:
        self._widget.setSomeParam(state.get('someParam', default))
        # ...
    except Exception:
        pass  # never crash on restore

# In __init__, after widget signals are connected:
getWidgetStatePersistence().register('MyController', self)
```

Safety rules:
- Never restore hardware-active state (laser enabled, scan running, acquisition active)
- Always use `.get(key, default)` so missing keys don't crash restore
- Wrap the entire `setWidgetState` body in try/except
