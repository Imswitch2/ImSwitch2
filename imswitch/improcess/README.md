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

## 1. The core contracts

Everything in ImProcess is built on three abstractions, plus an optional
fourth one a reconstructor can implement to support live/streaming mode:

| Contract | Input → Output | Source |
|---|---|---|
| `Reconstructor` | raw `DataObj` → `ProcessingResult` | [`reconstructors/base.py`](reconstructors/base.py) |
| `Processor` | `ProcessingResult` → `ProcessingResult` (stackable) | [`processors/base.py`](processors/base.py) |
| `ProcessingResult` | the N-D payload that travels between them | [`model/result.py`](model/result.py) |
| `StreamingReconstructor` / `StreamingSession` *(optional)* | growing raw frames → incremental `ProcessingResult` | [`reconstructors/base.py`](reconstructors/base.py) |

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

    supported_formats = ("tiff", "hdf5", "zarr")           # what save() accepts for this type
    def plan_save(self, path, fmt) -> SavePlan: ...        # every file the save will produce
    def write_files(self, plan, document) -> None: ...     # write them into the staging paths
    def save(self, path, fmt=None, *, overwrite=False) -> SaveReceipt   # THE protocol: never override

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

Saving is one staged protocol for every type (`model/save_protocol.py`):
`save()` plans, preflights, stages, embeds the provenance document, publishes
companions then the primary by atomic link, and returns a `SaveReceipt`.
Concrete subclasses never override `save()`; the array-backed ones inherit
the OME writers, and a type with its own layout declares `supported_formats`
and implements `plan_save` (every file, companions included) and
`write_files` (write into the staging paths given, embedding the document
where the container allows, or via a `.provenance.json` companion for CSV).

### 1.4 `StreamingReconstructor` / `StreamingSession` (optional)

A reconstructor can additionally support **live mode**: incremental
reconstruction of a recording while it is still being written, instead of
waiting for the file to close and running `process()` once. This is opt-in —
plain `Reconstructor`s work exactly as before and are driven through a
batch-fallback path (see §3.4).

```python
class StreamingReconstructor(Reconstructor):
    supports_streaming: bool = True

    @abstractmethod
    def make_session(self) -> "StreamingSession": ...
        # fresh, stateful session for one live stack or recording


class StreamingSession(ABC):
    @abstractmethod
    def begin(self, init_obj: StreamInit, params: dict) -> StreamPlan: ...
        # inspect the first logical stack, allocate output buffers,
        # return the output shape/labels/view_modes up front

    @abstractmethod
    def push(self, chunk: np.ndarray, start: int, end: int) -> None: ...
        # scatter one more contiguous frame range [start:end) into the buffer

    @abstractmethod
    def result(self) -> ProcessingResult: ...
        # cheap snapshot of the buffer so far, for periodic display refresh

    def finish(self) -> ProcessingResult: ...   # default: return self.result()
    def close(self) -> None: ...                 # default: no-op; free GPU etc.
```

Supporting dataclasses (all in [`reconstructors/base.py`](reconstructors/base.py)):
`StackInfo` (frame shape/dtype/`frames_per_stack`/detector name, returned by
`LiveSource.open()`), `Chunk` (one `(data, start, end)` frame range),
`StreamInit` (first logical stack + attrs, handed to `begin()`), `StreamPlan`
(the `out_shape`/`axis_labels`/`view_modes`/`axis_scales` a session commits to
after inspecting the first stack).

`MonalisaReconstructor` is the only implementation today
([`reconstructors/monalisa/reconstructor.py`](reconstructors/monalisa/reconstructor.py),
session in [`reconstructors/monalisa/live_session.py`](reconstructors/monalisa/live_session.py)):
`begin()` localizes the SIM pattern and allocates the full `(Dataset, Base, T,
Z, Y, X)` output once, `push()` scatters each incoming chunk's pixels into it
by pattern-derived indices, `result()`/`finish()` return a copy of the buffer
so far.

See §3.4 for how a session is driven; §4.3 for how to add streaming support to
your own reconstructor.

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
requested ids (or all, if `filter_ids is None`), plus every user drop-in plugin
of that kind: a `.py` file in the user plugins folder defining a `Processor` or
`Reconstructor` subclass (see [`plugins/`](plugins/)) is installed into
`_USER_PROCESSOR_CLASSES` / `_USER_RECONSTRUCTOR_CLASSES` by one scan
(`imswitch.improcess.plugins.load_user_plugins()`), gated by its presence on
disk rather than by config, with built-in ids winning any collision.

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
Reconstructors likewise: **Tools → Load reconstructor** lists every known but
unregistered reconstructor (`available_reconstructor_specs()` minus the
registry), and `_load_runtime_reconstructor` calls
`register_reconstructor_by_id(registry, id)` and has the reconstructor manager
offer it in the picker and activate it (`reconstructorLoaded`). This is
per-session; the setup file's `processing.reconstructors` makes it permanent.
Runtime tool metadata lives in
[`model/runtime_tools.py`](model/runtime_tools.py): processor-backed tools point
at their processor id and widget kind, while panel-only tools such as
`roi-manager` have no processor id. New processors default to the generic
`ResultProcessorWidget`; add a descriptor only when they need a specialized
Napari-side widget.

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
`sigScanParamsUpdated(scanParDict, applyOnCurrentRecon)`,
`sigLiveResultUpdated(result)` (a live/streaming session has a new partial
`ProcessingResult` — see §3.4; distinct from `sigResultProduced`, which only
fires once the whole recording is done).

### 3.2 Interactive path

```
DataObj (model/DataObj.py — format-aware h5py/tiff/zarr loader;
         data_handle is lazy, data materializes as NumPy)
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

### 3.4 Live streaming path

For a recording that is still being written, ImProcess can reconstruct it
incrementally instead of waiting for it to close. Two drivers feed the same
streaming machinery:

- **`LiveModeController`** ([`controller/LiveModeController.py`](controller/LiveModeController.py))
  — polls a watched folder tree for new recording stores (Zarr/HDF5, including
  per-file timelapses), queues them, and processes one store at a time once it
  is complete enough to open (see `_is_store_complete` / `_lapse_key` for the
  completeness and timelapse-grouping rules).
- **`MemoryLiveController`** ([`controller/MemoryLiveController.py`](controller/MemoryLiveController.py))
  — routes in-RAM HDF5 recordings handed off directly from imcontrol (no disk
  polling); currently runs them through the plain batch `process()` path.

Both hand a `LiveSource` + the active reconstructor to the shared
**`LiveReconstructionController`** ([`controller/LiveReconstructionController.py`](controller/LiveReconstructionController.py)),
which branches on `reconstructor.supports_streaming`:

```
LiveSource.open(path)  ──►  StackInfo
   │  poll() yields Chunks as the file grows
   ▼
supports_streaming?
   │                                                  │
   │ yes (StreamingReconstructor)                     │ no (plain Reconstructor)
   ▼                                                  ▼
LiveStreamWorker (own QThread)                    LiveStreamWorker buffers all
   │ first stack ──► session.begin() (StreamPlan)     Chunks until is_complete()
   │ resume() gate releases remaining chunks              │
   ▼                                                       ▼
LiveProcessWorker (own QThread)                   reconstructor.process(buffered_data)
   │ session.push(chunk) each Chunk                        │
   │ session.result() every N chunks                        │
   │   ──► emit sigLiveResultUpdated(result)                │
   │ source complete ──► session.finish()                   │
   └── emit sigResultProduced(result, "Live Reconstruction") ┘
            └─► ReconstructionViewController.liveResultUpdated / resultProduced
                 (refreshes the current list item in place, or adds one)
```

`LiveSource` ([`live/sources.py`](live/sources.py)) is the format-agnostic
polling contract — `open(path_or_handle) -> StackInfo`, `poll() -> list[Chunk]`,
`is_complete() -> bool`. Built-in sources: `ZarrLiveSource`/`Hdf5LiveSource`
(single growing store) and `Zarr/Hdf5{MultiFileLapseSource,LapseSource}` (a
per-file timelapse streamed as one continuous frame range across files).
`live/source_factory.make_live_source(path)` picks one by suffix.

Failure semantics: startup/source failures and `session.finish()` exceptions are
routed to `_finish_without_result()`, so `LiveReconstructionController.sigFinished`
still fires without a final `sigResultProduced` payload. This keeps
`LiveModeController`'s queue moving even when one live reconstruction fails.

---

## 4. How to add a plugin

### 4.1 Add a reconstructor

1. **Create a package** `reconstructors/<your_modality>/` with a
   `reconstructor.py` and `__init__.py` exporting the class.
2. **Pick a result type**: an existing class (`ArrayProcessingResult`,
   `LabelsResult`, `LocalizationResult`, …) unless the modality needs its own
   on-disk layout; then subclass `ProcessingResult` with `kind`,
   `supported_formats`, `plan_save` and `write_files` — never an overridden
   `save()` (see §1.3). In `process()`, set `axis_labels` with the **last two
   axes = `Y, X`**, and set `axis_scales` / `scale_unit` if you know the pixel
   size.
3. **Subclass `Reconstructor`** and set `name`, `id`, `file_extensions`
   (and `is_pass_through` / `default_save_subdir` if relevant). Implement:
   - `default_params()` — class method: exactly what a fresh widget hands
     `process()`; required, even `{}` (the headless contract — a plugin that
     inherits the framework default is GUI-only; see `model/plugin_contract.py`);
   - `make_param_widget(parent)` — a `QWidget` exposing `get_values() -> dict`
     with the same keys and defaults;
   - `make_metadata_dialog(parent)` — a `QDialog` or `None`;
   - `process(data_obj, params, context=None)` — load the data, compute,
     return your result;
   - optionally `prepare_params(data_obj, params)` to complete parameters
     from the file headlessly, and `make_overlay(...)`.
4. **Register it**: add `'<id>': YourReconstructor` to
   `_AVAILABLE_RECONSTRUCTOR_CLASSES` in
   [`reconstructors/__init__.py`](reconstructors/__init__.py) — or, for a
   plugin that does not live in the source tree, put the class in a `.py` file
   in the user plugins folder (**Plugins → Add plugin file…**); drop-in
   discovery finds reconstructors as it finds processors.
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
3. **Subclass `Processor`**, set `name`, `id` and `kinds`, and implement:
   - `default_params()` — class method: exactly what a fresh widget hands
     `apply()`, every fallback `apply` reads included; required, even `{}`
     (a plugin that inherits the framework default is GUI-only: workflows
     refuse it and its results are recorded non-replayable);
   - `applies_to` — a cheap gate, e.g. `lambda r: "T" in r.axis_labels` or
     `lambda r: r.data.ndim >= 2`;
   - `make_param_widget(parent)` — `QWidget` exposing `get_values() -> dict`
     with the same keys and defaults (the GUI checks this when it builds the
     widget; `check_plugin_contract(cls)` does it in a test);
   - `apply(result, params)` — pure; return a **new** `ProcessingResult`.
     Resolve axes by label, not position, and respect the **last-2-axes = `Y,X`**
     convention; carry `axis_scales` / `scale_unit` through to the output.
   - as needed: `min_inputs`/`max_inputs`/`check_inputs` (several inputs),
     `output_spec` (several or data-dependent ports), `accepts_roi`/`roi_modes`
     (region restriction), `preserves_grid` (pixel-aligned output),
     `extra_param_keys` (keys no widget default names).
4. **Register it**: add `'<id>': YourProcessor` to
   `_AVAILABLE_PROCESSOR_CLASSES` in
   [`processors/__init__.py`](processors/__init__.py).
5. **Enable it** via the setup JSON `processing.processors` list, or it can be
   runtime-loaded from the analysis-tools UI.

See [`processors/projection/processor.py`](processors/projection/processor.py)
for a compact, fully N-D example.

### 4.3 Add live/streaming support to a reconstructor (optional)

Only worth doing if your modality benefits from an incrementally-updating
display while the recording is still being written; otherwise skip this and
your `Reconstructor` gets streamed via the batch-fallback path automatically
(§3.4) — the whole recording is buffered and passed through your existing
`process()` once it completes.

1. Subclass `StreamingReconstructor` instead of `Reconstructor` and set
   `supports_streaming = True` (the default).
2. Implement `make_session() -> StreamingSession` returning a fresh, stateful
   session per live stack/recording (no shared state between sessions).
3. In your `StreamingSession`:
   - `begin(init_obj, params)` — inspect `init_obj.data` (the first logical
     stack) and `init_obj.attrs`/`init_obj.stack_info`, allocate your output
     buffer once, and return a `StreamPlan` with the final `out_shape` /
     `axis_labels` / `view_modes` / `axis_scales`. This is also the natural
     place to consume `init_obj.data` itself via your own `push()` (see the
     MoNaLISA session) so the caller only streams the remainder.
   - `push(chunk, start, end)` — scatter one more `[start:end)` frame range
     into the buffer. Must tolerate being called with ranges that cross your
     internal "logical stack" boundaries if your modality has them (MoNaLISA's
     `push()` splits an incoming chunk at `num_frames_in_stack` boundaries
     itself, since the source streams multiple timepoints as one continuous
     global range).
   - `result()` — return a cheap snapshot (e.g. `buffer.copy()`) for periodic
     display refresh; keep this fast, it runs on every `update_cadence`-th
     chunk (default every 5).
   - Optionally override `finish()` (default calls `result()`) and `close()`
     (default no-op) to release GPU buffers etc.
4. Keep `finish()` robust and idempotent where possible. If it does raise,
   the live controller treats that recording as failed, emits `sigFinished`
   without a final result, and advances the queue (§3.4).
5. Register/enable it exactly as in §4.1 — streaming is a capability of the
   reconstructor, not a separate registry entry.

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

    // --- core GUI visibility flags (all default true) ---
    "parameterPanel":       true,
    "napariLayerControls":  true,
    "reconstructionPanel":  true,
    "actionsPanel":         true,
    "fileWatcherPanel":     true,
    "multiDataPanel":       true,
    "currentDataPanel":     true,
    "resultsPanel":         true,

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
  the corresponding `_AVAILABLE_*` dict raise a startup `KeyError` that lists
  the unknown IDs and available built-ins. Explicit list order is preserved, so
  the first configured reconstructor is the initial active reconstructor. This
  is the initial registry population; runtime-loaded tools and registry-backed
  startup panels may register their required processor later.
- **Panel flags.** Each `is_*_panel_enabled()` getter reads its boolean key and
  defaults to `false`, so omitted panels stay hidden. Most panel flags only
  toggle UI visibility. Registry-backed startup panels that need a processor
  (currently `projectionPanel` and `frcPanel`) auto-register that processor if
  it is not already present, so the panel is usable even when the processor id
  is omitted from `processors`. Listing a processor explicitly still preloads it
  without opening its panel. Some analysis tools, e.g. `roi-manager`, are
  panel-only with no processor.
- **Core GUI flags.** `parameterPanel`, `actionsPanel`, `fileWatcherPanel`,
  `multiDataPanel`, `currentDataPanel`, `resultsPanel`,
  `napariLayerControls`, and `reconstructionPanel` default to `true` to
  preserve the standard ImProcess layout. Setting any dock flag to `false`
  hides that dock at startup while keeping the backing widget alive for
  controllers and toolbar actions. Setting `napariLayerControls: false` hides
  napari's built-in layer-controls dock. Setting `reconstructionPanel: false`
  hides the Reconstruction dock at startup; it is automatically shown when the
  first result is added to the viewer.

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
- **Streaming sessions should keep `finish()` robust**: if `finish()` raises,
  the live controller fails that recording without a result and advances the
  queue (§3.4).
