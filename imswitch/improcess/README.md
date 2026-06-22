# ImProcess — Architecture & Plugin Guide

ImProcess is ImSwitch's image-processing module. It loads acquired datasets,
runs a **reconstructor** to turn raw data into a viewable result, optionally
runs **processors** on that result, and displays everything in a napari-backed
viewer with the correct axis labels and pixel scales.

It is a **plugin architecture**: reconstructors and processors are
self-contained classes registered into a central registry at startup.
Controllers never import a plugin directly — they look it up by id. This
document describes the contracts you implement to add a plugin and the data
flow that connects them.

> For a critical review of the module (debt, duplication, roadmap), see
> [`IMPROCESS_REVIEW.md`](IMPROCESS_REVIEW.md). This README is the
> "how it works / how to extend it" companion to that review.

---

## 1. The three core contracts

Everything in ImProcess is built on three abstractions:

| Contract | Input → Output | Source |
|---|---|---|
| `Reconstructor` | raw `DataObj` → `ProcessingResult` | [`reconstructors/base.py`](reconstructors/base.py) |
| `Processor` | `ProcessingResult` → `ProcessingResult` (stackable) | [`processors/base.py`](processors/base.py) |
| `ProcessingResult` | the N-D payload that travels between them | [`model/result.py`](model/result.py) |

```
   DataObj ──Reconstructor.process()──► ProcessingResult ──Processor.apply()──► ProcessingResult ──►(...)
  (raw file)         exactly one             (viewable)        zero or more,         (new result)
                                                                stackable
```

### 1.1 `Reconstructor`

Each dataset is reconstructed by **exactly one** reconstructor. It is a pure
function over `(data, params)` with no GUI side effects.

```python
class Reconstructor(ABC):
    name: str = "Unnamed Reconstructor"     # shown in the plugin picker
    id: str = "unnamed"                      # stable id for config + registry lookup
    file_extensions: list[str] = ["hdf5", "tiff", "zarr"]  # watcher dispatch / auto-select
    is_pass_through: bool = False            # True => no-op wrap; auto-route DataObj to viewer
    default_save_subdir: str = "rec"         # watcher writes outputs to {dir}/{subdir}/

    @abstractmethod
    def make_param_widget(self, parent) -> QWidget: ...
        # left-panel parameter editor; must expose `get_values() -> dict`

    @abstractmethod
    def make_metadata_dialog(self, parent) -> QDialog | None: ...
        # acquisition-metadata dialog, or None if not needed

    @abstractmethod
    def process(self, data_obj: DataObj, params: dict) -> ProcessingResult: ...
        # the reconstruction itself — pure, no GUI

    def make_overlay(self, data_obj, params) -> Any | None:   # optional
        # pyqtgraph item drawn over the raw-data view (e.g. SIM grid scatter)
```

`is_pass_through = True` (e.g. `view-only`) tells the UI the plugin does no real
processing: ImProcess auto-routes the current `DataObj` to the viewer on change
and hides the "Reconstruct current" / "Update reconstruction" actions.

### 1.2 `Processor`

A processor transforms a `ProcessingResult` into a new one and is
**modality-agnostic** — drift correction works on anything with a time axis no
matter which reconstructor produced it. Processors are stackable: the output of
one can be the input of the next.

```python
class Processor(ABC):
    name: str = "Unnamed Processor"
    id: str = "unnamed"

    @property
    @abstractmethod
    def applies_to(self) -> Callable[[ProcessingResult], bool]: ...
        # gate: e.g. lambda r: "T" in r.axis_labels   (drift needs a time axis)

    @abstractmethod
    def make_param_widget(self, parent) -> QWidget: ...   # exposes get_values() -> dict

    @abstractmethod
    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult: ...
        # pure function; returns a NEW result (may reuse or copy the array)
```

`applies_to` is what the UI uses to decide whether to offer this processor for a
given result. Keep it cheap and honest.

### 1.3 `ProcessingResult`

The shared payload. It is **genuinely N-dimensional**: axis labels and scales
travel with the data, so the viewer and downstream processors never assume a
fixed dimension count.

```python
class ProcessingResult(ABC):
    def __init__(self, name, data, axis_labels,
                 view_modes=None, display_levels=None,
                 axis_scales=None, scale_unit="px"): ...

    @abstractmethod
    def save(self, path: Path, fmt: str) -> None: ...      # modality-specific serialization

    def plot_payloads(self) -> list[PlotPayload]:          # optional graph data
        return []
    def display_layers(self) -> list[DisplayLayerSpec]:    # optional independently-scaled layers
        return []
    def processor_input_choices(self) -> list[ProcessorInputChoice]:
        # "Whole result" first, plus one wrapper per display layer
        ...
```

Fields:

- **`data`** — the N-D array (`np.ndarray`; `zarr.Array` anticipated).
- **`axis_labels`** — one label per dimension, in `data` order, e.g.
  `["T", "Z", "Y", "X"]` or `["Dataset", "Base", "T", "Z", "Y", "X"]`.
  **Convention: the last two axes are always `(Y, X)`.** Most processors and
  analysis kernels rely on this. (It is a convention, not yet validated in
  code — keep `len(axis_labels) == data.ndim` and trailing `Y,X` true.)
- **`axis_scales`** — physical size per axis (defaults to all `1.0`); paired
  with `scale_unit` (e.g. `"nm"`). Applied to napari layers.
- **`view_modes`** — list of `ViewMode(name, transpose)` axis permutations the
  viewer offers (defaults to a single identity `"Standard"` mode).
- **`display_levels`** — optional `(min, max)` initial LUT range.

Optional extension points:

- **`display_layers()`** → `list[DisplayLayerSpec]`: split a heterogeneous
  `data` array into separately-contrasted napari layers (e.g. each MoNaLISA
  base) without those layers sharing contrast limits.
- **`plot_payloads()`** → `list[PlotPayload]`: 1-D/graph data for the graph
  widget (FRC curves, line profiles, etc.).
- **`processor_input_choices()`** → `list[ProcessorInputChoice]`: the explicit
  inputs a result offers to result-based processors. Always offers the whole
  result first; if `display_layers()` is non-empty, each layer is wrapped as a
  lightweight `DisplayLayerProcessingResult` so a processor can target one named
  component instead of depending on napari's active layer.

Concrete subclasses (e.g. `ViewOnlyResult`, `MonalisaProcessingResult`,
`ProjectionResult`) only have to implement `save()`.

---

## 2. Plugin registry & registration

### 2.1 The registry

[`reconstructors/registry.py`](reconstructors/registry.py) defines
`PluginRegistry`, a process-global singleton obtained via `get_registry()`. It
holds two id→instance maps and is the **only** way controllers reach plugins:

```python
registry = get_registry()
registry.register_reconstructor(plugin)     # / register_processor(plugin)
registry.get_reconstructor("monalisa")      # / get_processor("drift-correct")
registry.reconstructors()                   # / processors()  -> list of instances
registry.clear()                            # wipe before applying a new setup config
registry.auto_select_reconstructor(data_obj)
```

`auto_select_reconstructor(data_obj)` chooses a default reconstructor by:
1. a `"modality"` tag in `data_obj.attrs` matching a reconstructor id;
2. else the file extension (`data_obj.dataPath`) matching a
   `reconstructor.file_extensions`;
3. else `"view-only"`; else the first registered reconstructor (raises if none).

Registering a plugin whose id already exists logs a warning and **replaces** it.

### 2.2 Where the plugin lists come from

Built-in plugins are declared in two module-level dicts (hard-coded imports for
now; entry-point discovery is a future item):

- Reconstructors — [`reconstructors/__init__.py`](reconstructors/__init__.py)
  `_AVAILABLE_RECONSTRUCTOR_CLASSES`:
  `monalisa`, `snouty`, `view-only`, `snouty-projections`, `widefield-starss`.
- Processors — [`processors/__init__.py`](processors/__init__.py)
  `_AVAILABLE_PROCESSOR_CLASSES`:
  `colocalization`, `drift-correct`, `frc`, `multicolor-apply`,
  `multicolor-registration`, `denoise`, `projection`, `psf-resolution`,
  `segmentation`.

`register_default_reconstructors(registry, filter_ids)` and
`register_default_processors(registry, filter_ids)` instantiate and register the
requested ids (or all, if `filter_ids is None`).

### 2.3 Startup: config vs standalone defaults

`ImProcessMainController._initialize_plugins`
([`controller/ImProcessMainController.py:72`](controller/ImProcessMainController.py:72))
clears the registry and repopulates it:

1. Load the setup-JSON `processing` block via
   `load_processing_config()` ([`model/processing_config.py`](model/processing_config.py)).
   It lives in `SetupInfo._catchAll["processing"]` because `processing` is an
   ImProcess-specific addition, not a typed `SetupInfo` field.
2. `plugin_ids_from_config(processing_config)` returns
   `(reconstructor_ids, processor_ids, has_plugin_config)`:
   - if the block contains a `reconstructors` **or** `processors` key →
     **config-driven mode**, using those lists (defaults `["monalisa"]` /
     `["drift-correct"]` if only one key is present);
   - otherwise → **standalone defaults**: `["view-only"]` reconstructor +
     `["drift-correct"]` processor.
3. Register exactly the requested ids.

Beyond startup, processors can also be **runtime-loaded** on demand: the main
controller calls `register_processor_by_id(registry, id)` when the user adds an
analysis tool from the UI, and `_restore_runtime_processor` re-registers any
processor that was loaded in a previous session (persisted layout state).

---

## 3. Data flow

### 3.1 Signal bus

All cross-controller communication goes through
[`controller/CommunicationChannel.py`](controller/CommunicationChannel.py). The
two result signals are the backbone:

- **`sigResultProduced(result, displayName)`** — "a new result is ready for the
  viewer." Producers **emit and forget**.
  `ReconstructionViewController` is the *canonical listener*; it folds the
  result into the reconstruction list. Producers do not need to know which
  widget owns the napari layer list. (Emitted by the legacy MoNaLISA path, the
  plugin reconstruction path, and the processor runner.)
- **`sigCurrentResultChanged(result)`** — "the active result changed." Drives
  the napari display, the graph widget, and the per-processor input dropdowns.

> ⚠️ Semantics gotcha: there is no single source of truth for "the current
> result." `sigCurrentResultChanged` is emitted from **two** places —
> `ReconstructionViewController` when the list selection changes, and
> `ResultProcessorController.runProcessor` after a processor runs. So a
> processor output can become "current" for the processing chain without being
> the selected list item, and the two can briefly disagree. Treat
> `sigResultProduced` as "add to list" and `sigCurrentResultChanged` as "show
> this now"; don't assume they always reference the same object.

Other notable signals: `sigReconstruct(dataObjs, applyOnCurrent)` (triggers a
reconstruction run), `sigCurrentDataChanged(dataObj)`,
`sigScanParamsUpdated(scanParDict, applyOnCurrentRecon)`.

### 3.2 Interactive path

```
DataObj (model/DataObj.py — lazy h5py/tiff/zarr loader)
   │   Reconstructor.process(data_obj, params)         params = make_param_widget().get_values()
   ▼
ProcessingResult
   │   emit sigResultProduced(result, name)
   ▼
ReconstructionViewController  ── adds to recon list ──►  napari viewer
                                                          (axis_labels + axis_scales applied,
                                                           view_modes select the transpose)
   │   user selects a list item  →  emit sigCurrentResultChanged(result)
   ▼
ResultProcessorController (one per runtime-loaded processor)
   │   processor.applies_to(result) gates availability
   │   result.processor_input_choices() populates the input dropdown
   │   processor.apply(input_result, params) → new ProcessingResult
   └── emit sigResultProduced + sigCurrentResultChanged
            └─► chains back into the viewer / next processor
```

### 3.3 File-watcher path

For batch/headless reconstruction,
`WatcherFrameController`
([`controller/WatcherFrameController.py`](controller/WatcherFrameController.py))
watches a directory and emits `sigReconstruct(dataObjs, True)`. This runs the
same reconstruction path and writes each output under
`{watched_dir}/{reconstructor.default_save_subdir}/` via the result's `save()`.

---

## 4. How to add a plugin

### 4.1 Add a reconstructor

1. **Create a package** `reconstructors/<your_modality>/` with a
   `reconstructor.py` and `__init__.py` exporting the class.
2. **Define a result type** (subclass `ProcessingResult`) implementing `save()`.
   In `process()`, set `axis_labels` with the **last two axes = `Y, X`**, and
   set `axis_scales` / `scale_unit` if you know the pixel size.
3. **Subclass `Reconstructor`** and set `name`, `id`, `file_extensions`
   (and `is_pass_through` / `default_save_subdir` if relevant). Implement:
   - `make_param_widget(parent)` — a `QWidget` exposing `get_values() -> dict`;
   - `make_metadata_dialog(parent)` — a `QDialog` or `None`;
   - `process(data_obj, params)` — load the data, compute, return your result;
   - optionally `make_overlay(...)`.
4. **Register it**: add `'<id>': YourReconstructor` to
   `_AVAILABLE_RECONSTRUCTOR_CLASSES` in
   [`reconstructors/__init__.py`](reconstructors/__init__.py).
5. **Enable it** in the setup JSON `processing.reconstructors` list (see §5), or
   rely on `auto_select_reconstructor` via `file_extensions` / modality tag.

The minimal pattern is [`reconstructors/view_only/reconstructor.py`](reconstructors/view_only/reconstructor.py):
a `ViewOnlyResult(ProcessingResult)` with a `save()`, a no-op `process()` that
wraps `data_obj.data`, and `_DEFAULT_AXIS_LABELS[-ndim:]` for labels.

### 4.2 Add a processor

1. **Create a package** `processors/<your_op>/` with a `processor.py`
   (and a `result.py` if you need a custom result type).
2. **Put the numeric kernel in `analysis/`**, UI-free and unit-testable, and
   have the processor delegate to it. This `analysis/` ⇄ `processors/` split is
   a deliberate design rule — see [`analysis/projections.py`](analysis/projections.py)
   for the model of correct N-D axis handling (`_normalize_axis`, label/scale
   propagation, dimension shrink).
3. **Subclass `Processor`**, set `name` and `id`, and implement:
   - `applies_to` — a cheap gate, e.g. `lambda r: "T" in r.axis_labels` or
     `lambda r: r.data.ndim >= 2`;
   - `make_param_widget(parent)` — `QWidget` exposing `get_values() -> dict`;
   - `apply(result, params)` — pure; return a **new** `ProcessingResult`.
     Resolve axes by label, not position, and respect the **last-2-axes = `Y,X`**
     convention; carry `axis_scales` / `scale_unit` through to the output.
4. **Register it**: add `'<id>': YourProcessor` to
   `_AVAILABLE_PROCESSOR_CLASSES` in
   [`processors/__init__.py`](processors/__init__.py).
5. **Enable it** via the setup JSON `processing.processors` list, or it can be
   runtime-loaded from the analysis-tools UI.

See [`processors/projection/processor.py`](processors/projection/processor.py)
for a compact, fully N-D example.

---

## 5. Setup-JSON `processing` block schema

ImProcess reads an optional `processing` object from the active setup JSON. It
is **not** a typed `SetupInfo` field, so it lands in `SetupInfo._catchAll` and is
read by [`model/processing_config.py`](model/processing_config.py). All keys are
optional.

```jsonc
{
  // ... normal SetupInfo fields ...
  "processing": {
    // --- plugin selection ---
    // Presence of EITHER key switches ImProcess into config-driven mode.
    "reconstructors": ["monalisa", "view-only"],   // ids from
                                                   // _AVAILABLE_RECONSTRUCTOR_CLASSES
    "processors":     ["drift-correct", "projection"], // ids from
                                                   // _AVAILABLE_PROCESSOR_CLASSES

    // --- optional analysis-panel enable flags (all default false) ---
    "graphPanel":          false,
    "profilePanel":        false,
    "frcPanel":            false,
    "roiStatsPanel":       false,
    "roiManagerPanel":     false,
    "projectionPanel":     false,
    "segmentationPanel":   false,
    "psfResolutionPanel":  false,
    "colocalizationPanel": false,
    "multicolorPanel":     false
  }
}
```

Rules:

- **Plugin lists.** If neither `reconstructors` nor `processors` is present,
  ImProcess uses **standalone defaults**: reconstructor `["view-only"]`,
  processor `["drift-correct"]`. If at least one of the two keys is present,
  ImProcess is in **config-driven mode**; a missing companion key defaults to
  `["monalisa"]` (reconstructors) / `["drift-correct"]` (processors). Ids not in
  the corresponding `_AVAILABLE_*` dict are silently skipped at registration.
- **Panel flags.** Each `is_*_panel_enabled()` getter reads its boolean key and
  defaults to `false`, so omitted panels stay hidden. Panels are independent of
  the processor that backs them: a panel flag toggles UI visibility, while the
  `processors` list controls which processor plugins are registered (some
  analysis tools, e.g. `roi-manager`, are panel-only with no processor).

---

## 6. Conventions checklist

- **Last two axes are `(Y, X)`** in every `axis_labels`, always.
- **`len(axis_labels) == data.ndim`** and `len(axis_scales) == data.ndim`.
- **Plugins are pure**: `process()` / `apply()` do no GUI work and have no side
  effects; the UI is driven by signals, not by the plugin.
- **Resolve axes by label**, not hard-coded index, wherever possible.
- **Numeric work lives in `analysis/`**; processors orchestrate.
- **Reach plugins through the registry** (`get_registry()`), never by importing
  the class into a controller.
- **Carry scales through**: propagate `axis_scales` / `scale_unit` so the viewer
  shows physical units.
</content>
</invoke>
