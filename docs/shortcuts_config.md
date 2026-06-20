# Keyboard Shortcuts Configuration

ImSwitch provides a unified keyboard shortcut system with support for customization via setup configuration files and a GUI editor.

## Configuring Shortcuts via GUI

The easiest way to configure shortcuts is through the built-in editor:

1. Open ImSwitch
2. Go to **Shortcuts** → **Configure Shortcuts…**
3. The editor shows all available shortcuts with their current bindings
4. To change a shortcut:
   - Click in the key sequence editor field and press your desired key combination
   - Or click **Reset** to restore the code default
   - Or click **Disable** to unbind the shortcut
5. The editor provides live conflict detection - if two actions share the same key, both rows are highlighted in red
6. Click **Apply** to test changes immediately without closing the dialog
7. Click **OK** to save changes to your setup config and close
8. Click **Cancel** to discard all changes

**Note:** Mode switch shortcuts (e.g., `mode.bright-field`) are managed in the **Setup Modes** widget and are shown as read-only in the shortcut editor.

## Setup Config Schema

Shortcuts are configured in the setup JSON file under the `shortcuts` section. This is a top-level key alongside `lasers`, `detectors`, etc.

### Format

```json
{
  "shortcuts": {
    "actionId1": "Ctrl+R",
    "actionId2": ["Ctrl+S", "Ctrl+Shift+S"],
    "actionId3": null
  }
}
```

### Schema Details

- **Key**: `actionId` (string) - The stable action identifier (e.g., `app.loadParams`, `Recording.recordToggle`)
- **Value**: One of:
  - `"KeySequence"` (string) - A single key binding (e.g., `"Ctrl+R"`, `"F5"`)
  - `["KeySeq1", "KeySeq2"]` (list) - Multiple alternative key bindings for the same action
  - `null` - Explicitly disable the shortcut (no key binding)

### Key Sequence Syntax

Key sequences follow Qt's `QKeySequence` format:

- Modifiers: `Ctrl`, `Shift`, `Alt`, `Meta` (case-insensitive)
- Keys: Standard key names like `A-Z`, `0-9`, `F1-F12`, `Space`, `Return`, `Left`, `Right`, `Up`, `Down`, etc.
- Format: `Modifier+Modifier+Key` (e.g., `Ctrl+Shift+S`, `Alt+F4`)

**Examples:**
```json
{
  "shortcuts": {
    "app.loadParams": "Ctrl+P",
    "Recording.recordToggle": "Ctrl+R",
    "Recording.snapToggle": ["Ctrl+N", "F9"],
    "Image.histogramToggle": null
  }
}
```

## Merge Order and Conflict Resolution

The effective shortcuts are determined by merging:

1. **Code defaults** (lowest priority) - Defined by developers via `@shortcut` decorators or `registerAction()` calls
2. **Setup config overrides** (highest priority) - Defined in the `shortcuts` section of your setup JSON

### Conflict Policy

When multiple actions request the same key sequence, the system applies priority-based resolution:

1. **Explicit setup config binding** (highest priority)
2. **Code default**
3. **First-registered wins** (tie-breaker for equal priorities)

The losing action(s) are disabled and a warning is logged. Use the GUI editor's conflict detection to identify and resolve conflicts.

## Action ID Namespacing

Action IDs follow a hierarchical naming convention for organization:

- `app.*` - Application-level actions (e.g., `app.loadParams`)
- `<WidgetName>.*` - Widget-specific actions (e.g., `Recording.recordToggle`, `Image.liveviewToggle`)
- `positioner.<name>.<axis>.*` - Per-positioner axis jog actions (e.g., `positioner.Z-Piezo.Z.plus`)
- `mode.<name>` - Mode switch shortcuts (managed in Setup Modes widget, **not in `shortcuts` map**)

## Mode Switch Shortcuts

Setup modes (e.g., bright-field, fluorescence) can have keyboard shortcuts for quick switching. These are **not** configured in the `shortcuts` section but in the per-mode JSON files in the `setup-modes/` directory:

```json
{
  "name": "bright-field",
  "shortcut": "F3"
}
```

Use the **Setup Modes** widget to edit mode shortcuts. The shortcut editor displays them as read-only for reference.

## Scope of This Feature

This shortcut system applies to **imcontrol only** (the main microscopy control module). Other modules (e.g., imreconstruct, imnotebook) have independent shortcut systems.

## Default Keyboard Shortcuts

Below is a table of all default keyboard shortcuts defined in code. Custom setups may have additional shortcuts for setup-specific widgets or positioners.

### Application Actions

| Action ID | Default Key | Description |
|-----------|-------------|-------------|
| `app.loadParams` | `Ctrl+P` | Load parameters from saved HDF5 file |
| `app.saveWidgetStates` | `Ctrl+Shift+S` | Save widget states to JSON |
| `app.loadWidgetStates` | `Ctrl+Shift+L` | Load widget states from JSON |

### Recording

| Action ID | Default Key | Description |
|-----------|-------------|-------------|
| `Recording.recordToggle` | `Ctrl+R` | Start/stop recording |
| `Recording.snapToggle` | `Ctrl+N` | Snap single image |

### Image Viewer

| Action ID | Default Key | Description |
|-----------|-------------|-------------|
| `Image.liveviewToggle` | `Ctrl+L` | Toggle live view |
| `Image.gridToggle` | `G` | Toggle grid overlay |
| `Image.crosshairToggle` | `C` | Toggle crosshair |
| `Image.histogramToggle` | `H` | Toggle histogram display |

### Positioner Jog Actions

Positioner jog shortcuts are dynamically generated based on configured positioners. The default keys depend on the `shortcutModifier` attribute in the positioner configuration:

- **No modifier (legacy first-come)**: The first positioner for each axis gets `Ctrl+Arrow/Y/A` keys
  - `positioner.<name>.X.plus` → `Ctrl+Right`
  - `positioner.<name>.X.minus` → `Ctrl+Left`
  - `positioner.<name>.Y.plus` → `Ctrl+Up`
  - `positioner.<name>.Y.minus` → `Ctrl+Down`
  - `positioner.<name>.Z.plus` → `Ctrl+Y`
  - `positioner.<name>.Z.minus` → `Ctrl+A`

- **`shortcutModifier: "ctrl"`**: Explicit `Ctrl+Arrow/Y/A` for this positioner
- **`shortcutModifier: "ctrl-shift"`**: `Ctrl+Shift+Arrow/Y/A` for this positioner

Additional positioners without a `shortcutModifier` are unbound by default unless explicitly configured in the `shortcuts` map.

### Leica Stand (if enabled)

| Action ID | Default Key | Description |
|-----------|-------------|-------------|
| `leica.toggleMode` | `F2` | Toggle Leica stand mode |

### Mode Switch Shortcuts (if configured)

Mode shortcuts are defined per-mode in setup-mode JSON files. Common examples:

- Bright-field: `F3`
- Fluorescence: `F4`
- TIRF: `F5`

(Actual keys vary by setup configuration.)

## Persistence and Restart

- Changes made via the GUI editor or direct JSON edits are persisted immediately to the setup config file.
- Shortcuts take effect live in the current session (no restart required).
- On next launch, the saved `shortcuts` configuration is automatically loaded.

## Troubleshooting

### Shortcut Not Working

1. Check for conflicts using the GUI editor (conflicts are highlighted in red)
2. Verify the key sequence is valid Qt format (e.g., `Ctrl+R`, not `Control+R`)
3. Check the action scope - some shortcuts only work when specific widgets have focus

### Config Ignored

- The setup config must be valid JSON. Use a JSON validator if unsure.
- Action IDs are case-sensitive and must match exactly.
- Unknown action IDs in the config are logged as warnings and ignored.

### Reset to Defaults

To reset all shortcuts to code defaults:

1. Remove the `shortcuts` section from your setup JSON, or
2. Delete individual entries from the `shortcuts` map to reset specific actions

Relaunch ImSwitch for a clean state.

---

For developer documentation on the shortcut system architecture, see `shortcuts_contract_spec.md` and `shortcuts_unification_plan.md`.
