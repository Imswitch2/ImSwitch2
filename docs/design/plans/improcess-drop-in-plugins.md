# ImProcess drop-in analysis plugins (Picasso-style)

Status: **All four slices done (2026-07-10 / 07-11).**  Registry repo:
`Imswitch2/Improcess-plugins` (must be made **public** for the store's anonymous
fetch to work).

## Motivation

ImSwitch2 already has a *heavyweight* plugin system — `imcontrol` device
plugins: pip-installed packages advertised through `imswitch.manifest` entry
points, with JSON manifests and a registry. That is the right shape for
**hardware**: device drivers carry vendor SDKs, versioned dependencies and real
packaging.

ImProcess **analysis processors** are the opposite case. A `Processor` is a
small, dependency-light class (`accepts` / `apply` / `make_param_widget` /
`kinds` / `category` / `id` / `name`). Today they can only be added by editing
the in-tree `_AVAILABLE_PROCESSOR_CLASSES` dict — there is no user-facing way to
add one. A scientist who wrote a NumPy function should not have to build a
package to use it inside ImProcess.

[Picasso](https://github.com/jungmannlab/picasso) solved exactly this with a
beautifully low-friction model (`picasso/gui/plugins_loader.py`):

- **Discovery is a folder scan** — `~/.picasso/plugins/*.py` (skip `_`-prefixed).
  No pip, no entry points.
- **The interface is duck-typed** — a plugin is any `.py` with a `Plugin` class;
  there is no base class to import.
- **Loading is tolerant** — a broken plugin prints a traceback and is skipped;
  it can never crash startup.
- **An in-app store** — a single `index.json` in a GitHub repo lists plain `.py`
  files; a dialog installs/updates/uninstalls, gates on `min_version`, tracks an
  `.installed.json` sidecar, and shows a one-time "this runs arbitrary Python"
  trust warning.

The elegance is a *friction gradient*: authoring a plugin = drop one file;
sharing one = add a row to a JSON file.

## Why this fits ImProcess especially well

Because of the machinery already in place, a dropped-in `Processor` gets, for
free and with zero UI code:

- a parameter **panel** (the generic result-processor panel, from
  `make_param_widget`),
- **semantic-kind gating** (`accepts()` — it only offers itself on compatible
  result kinds),
- **results-table + graph** integration (table/curve results, `plot_payloads`),
- **runtime load/unload** via the existing runtime-tools loader.

The only missing piece is discovery + registration from a user directory.

## Design principles

1. **Keep it separate from device plugins.** Different problem, different shape:
   drop-in `.py` for analysis vs. pip-packaged SDK drivers for hardware. Do not
   conflate them.
2. **Tolerant loading.** One broken plugin never blocks ImProcess startup.
3. **Reuse the existing `Processor` contract and registry.** Discovered
   processors flow through the *same* enumeration, panel, gating and registry as
   built-ins — no parallel UI path.
4. **Trust is explicit.** These run arbitrary Python; the online store (later
   slice) shows a one-time warning, mirroring Picasso.
5. **Built-ins win id collisions**, with a logged warning, so a stray user file
   can never shadow a core processor silently.

## The plugin contract

A plugin is a single `.py` file under the user plugins directory that defines
one or more `Processor` subclasses. Discovery instantiates each and registers
it. Minimal example (`~/.imswitch/improcess_plugins/invert.py`):

```python
from imswitch.improcess.processors.base import Processor
from imswitch.improcess.model.array_result import ArrayProcessingResult


class InvertProcessor(Processor):
    name = "Invert"
    id = "user.invert"          # dotted, user-namespaced ids recommended
    category = "User"
    kinds = ("image",)

    @property
    def applies_to(self):
        return lambda result: getattr(result.data, "ndim", 0) >= 2

    def make_param_widget(self, parent):
        from qtpy import QtWidgets
        w = QtWidgets.QWidget(parent)
        w.get_values = lambda: {}
        return w

    def apply(self, result, params):
        data = result.data
        return ArrayProcessingResult(
            name=f"{result.name} (inverted)",
            data=data.max() - data,
            axis_labels=list(result.axis_labels),
        )
```

An optional `register(registry)` module-level hook is supported for advanced
cases (a plugin that wants to register several processors, or do custom setup),
but the common case needs no boilerplate beyond the class.

## Architecture

```
imswitch/improcess/plugins/            # NEW package (mirrors imcontrol/.../plugins)
  user_plugins.py                      # directory + discovery + tolerant import
processors/__init__.py                 # enumeration consults built-ins + discovered
controller/ImProcessMainController.py  # _initialize_plugins discovers before registering
view/ImProcessMainView.py              # "Analysis plugins" menu (slice 2)
```

- **`user_plugins_directory()`** → `{UserFileDirs.Root}/improcess_plugins/`,
  created on first use; a `README` + `_example_plugin.py` template dropped in.
- **`discover_processor_plugins()`** → `(classes: dict[id, type], errors:
  list[PluginLoadError])`. Scans `*.py` (skip `_`), imports each via
  `importlib.util.spec_from_file_location`, collects `Processor` subclasses and
  honours an optional `register(registry)` hook. Never raises.
- **Enumeration merge.** `processors/__init__.py` gains
  `_all_processor_classes()` = `{**built_in, **discovered}` (built-ins win, warn
  on collision). `available_processor_ids/choices/specs`,
  `register_processor_by_id` and `register_default_processors` route through it,
  so discovered processors appear in the runtime "Load tool" combo and register
  into the registry like any built-in.
- **Controller hook.** `_initialize_plugins()` calls
  `load_user_processor_plugins(logger)` before `register_default_processors`,
  logging discovered ids and any load errors.

## Slices

- **Slice 1 (this change): local discovery + registry integration + template +
  tests.** Drop a `.py` → restart → the processor appears as a fully-integrated
  analysis tool. No network. Fully unit-testable.
- **Slice 2 (done): menu + hot reload.** A "Drop-in plugins" submenu under
  &Analyze with "Open plugins folder…" (`QDesktopServices` →
  `user_plugins_directory`) and "Reload plugins"
  (`sigReloadPluginsRequested`). The controller's `_reload_user_plugins`
  re-discovers, re-registers current plugins with fresh instances (edited code
  takes effect on the next panel open) and refreshes the runtime-tool combo —
  no restart. An already-open panel keeps its instance until closed and
  reopened.
- **Slice 3 (done): online store.** `index.json` manifest in the
  `Imswitch2/Improcess-plugins` GitHub repo (filled with 4 example plugins);
  `plugins/plugin_store.py` (Qt-free: manifest fetch, version compat,
  `.installed.json` sidecar, install/uninstall, status model) +
  `view/PluginStoreDialog.py` (browse/install/update/uninstall, one-time trust
  warning), wired as "Browse online plugins…" which reloads on change. Ports
  Picasso's store. **The registry repo must be public** for the anonymous
  `raw.githubusercontent.com` fetch to work.
- **Slice 4 (done): docs + examples.** "Drop-in analysis plugins" section in
  `docs/improcess.rst`; ready-to-copy `examples/improcess_plugins/`
  (`invert.py`, `gaussian_blur.py`) guarded by `test_example_plugins.py`. (The
  *online* example repo waits for slice 3.)

## Security / trust

Drop-in plugins execute arbitrary Python at startup — identical to Picasso.
Mitigations: the online store (slice 3) shows a one-time trust acknowledgement;
the loader is tolerant (a crash is contained to that plugin); and everything is
opt-in (the directory starts empty apart from an inert `_example_plugin.py`
template that discovery skips).

## Relationship to the device-plugin system

Complementary, not competing. Device plugins stay pip-packaged with entry
points (hardware, SDKs, versioning). Analysis plugins are drop-in `.py`
(lightweight compute). A processor that grows heavy dependencies can always
graduate to a packaged distribution later; the drop-in path is for the long
tail of small, personal analysis steps.
