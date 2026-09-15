# ImProcess upgrade plan: napari plugin endpoints + headless workflows

Status: PLAN v4 (WIP markdown; durable docs land as `.rst` when implemented).
Branch: `feat/improcess-napari-endpoints-workflows`, worktree
`../Imswitch2-improcess-workflows`, based on `main` @ 87b3e3b5 (2026-09-05).

v2–v4 answer three rounds of critical review; every finding was verified
against the code. The "Review response" tables at the end map findings to
changes. **Phase 0 is implemented** (see Phasing).

Terminology: "GUI-independent" means *no widget is constructed and no
QApplication is required*; the plugin base modules still import `qtpy`.

Two deliverables, one shared foundation:

* **A. Verified napari plugins as one-way endpoints** for an ImProcess
  result. Improcess → plugin only. Supported: npe2 **dock-widget**
  contributions (auto-discovered, marked unverified until an adapter vouches
  for them) and **reader** contributions with an adapter-declared
  exporter/reader schema pair. Writer contributions are export formats.
* **B. ImProcess workflows**: GUI-independent, testable, batchable pipelines
  chaining a reconstructor with processors, plus **replay** from the
  provenance graph saved in an output file, with a strict source policy.

---

## 0. Ground truth (what the code does today)

Verified against the tree on 2026-09-05.

### Viewer
`EmbeddedNapari` is a real `napari.Viewer` subclass; npe2 dock widgets
attach to it and see its layers (spike, A.5). napari 0.7.1, npe2 0.8.3.
Precedent: `smlm_export.open_in_napari_storm` (export → detached viewer).

### Results
`ProcessingResult` carries `kind`, axes/scales/unit, `metadata`, identities,
`display_layers()`. It stores **scale only**: no translate, rotation, shear
or affine. `LocalizationResult.data` is a lazy histogram **preview binned
relative to `(x_min, y_min)`**; the payload is `locs` in absolute nm, with
`pixel_size_nm` and optional `z_step_nm`. Its CSV writer emits a plain
first-line column header. Multi-output processors: `background`,
`stack_split`, `channel_split` (ports are data-derived: axis label + index).
`multicolor-registration` writes an alignment file from `params["save_path"]`.

### Writers (20 concrete implementations)
Shared writer (6): `array_result`, `make_composite`, `make_rgb`,
`multicolor_apply`, `projection`, `view_only`. **Bypassing (14):**
`localization_result` (CSV; Picasso HDF5 + YAML via export),
`roi_mask_result`, `colocalization`, `denoise`, `drift_correct` (TIFF +
`.drift.npy`), `frc`, `multicolor_registration`, `psf_resolution`,
`segmentation`, `monalisa` (ImageJ TZCYX fold), `snouty`,
`snouty_projections`, `tiling` (one mosaic TIFF), `widefield_starss`.
(`ProcessingResult.save` is abstract; `DisplayLayerProcessingResult.save`
refuses by design; neither is a writer.)

### Execution paths that produce results (all must record provenance)
| Path | Call | Source object |
|---|---|---|
| Inline reconstruct (`ReconstructorManagerController`) | `process(data_obj, params)` | `DataObj` (file) |
| Worker reconstruct (`ReconstructionWorker`) | `process(job.data_obj, job.params, context)` | `DataObj` |
| Consolidation | `consolidate(results)` | N results |
| Legacy MoNaLISA (`MoNaLISAController.runLegacyReconstruct`) | own load/bleach/extract/build, no `process()` | `DataObj` |
| **Live streaming** (`LiveReconstructionController`) | `session.begin(init, params)` / `push(chunk)` / `result()` snapshots / `finish()` | live source + `StackInfo` |
| **Live batch fallback** (`LiveReconstructionController._run_batch_fallback`) | `process(InMemoryStackWrapper, params)` | in-memory stack |
| **RAM recording** (`MemoryLiveController`) | `process(InMemoryStackWrapper, params)` | in-memory stack |
| Headless runner (B.4) | `process` / `consolidate` | `SourceSpec` |

ROI geometry is serialisable (`ROIRecord.to_dict/from_dict`); `DataObj._open`
**requires a dataset name** for multi-dataset containers; tiling depends on
`sourceKind` + manifest metadata.

---

## A. napari plugins as endpoints

### A.1 Scope, descriptors, gating

| npe2 contribution | Lane | Support |
|---|---|---|
| `widgets` | `dock` | Auto-discovered as **unverified**; verified when an adapter declares `kinds` (and optionally an output mapping, A.3). |
| `readers` | `reader` / `detached` | **Verified pairs only**: an adapter declares which exporter schema the reader understands. Filename-pattern compatibility (`iter_compatible_readers`) is a precondition, never the proof. No auto-discovered reader endpoints. |
| `writers` | export format | Separate descriptor `NapariWriterFormat(plugin_name, writer_id, layer_types, extension)`; used to export **this session's layers** via `napari.save_layers(path, layers, plugin=…)`. |
| commands, sample data, themes, menus | — | Out of scope. |

```python
@dataclass(frozen=True)
class NapariEndpoint:
    id: str; label: str
    lane: str                        # "dock" | "reader" | "detached"
    plugin_name: str
    widget_name: str | None          # dock
    reader_plugin: str | None        # reader / detached
    export_format: str | None        # reader / detached: an exporter id (A.2 table)
    kinds: tuple[str, ...]           # () only for unverified docks
    verified: bool
    output_mapping: OutputMapping | None = None   # A.3, dock lane
```

Gating: `dock` → `result.kind in LAYERABLE_KINDS` and (unverified or
`kind in kinds`). `reader`/`detached` → adapter pair exists for
`(result.kind, export_format, reader_plugin)`. Table/curve results reach
no endpoint; they are exported to files by the ordinary save path, and a
plugin that reads such a file needs a verified pair like any other.

### A.2 Result → napari layers, by kind
`LAYERABLE_KINDS = ("image", "composite", "rgb", "labels", "localization")`.
image/composite/rgb → Image layers (from `display_layers()` or the primary
array); labels → Labels; localization → **Points from `locs`**: 2D coords
`(y_nm/px, x_nm/px)`, `scale=(px, px)`; 3D coords `(z_nm/z_scale, y_nm/px,
x_nm/px)`, `scale=(z_scale, px, px)`, `z_scale=z_step_nm`, fallback
`pixel_size_nm` flagged `z_scale_assumed`; `properties` from all other
columns; `metadata["coordinate_transform"]` documented. The optional
**context preview** layer gets `translate=(y_min, x_min)` in world units,
because the histogram is binned from the minimum, not the origin. Tests:
2D, 3D, anisotropic z, assumed z, **non-zero-minimum preview overlay**.
table/curve → `NotLayerable`.

Exporter ids for the reader lane: `ome-tiff`, `hdf5`, `ome-zarr`, `labels-tiff`,
`picasso-hdf5`, `localizations-csv`, plus adapter-provided custom exporters.

### A.3 Reverse direction (explicit, user- or adapter-selected, fresh grid)
Import is never inferred from layer-added events. Two ways in:
1. **Adapter output mapping**: `OutputMapping(layer_name_pattern, layer_type,
   result_kind, source_role)` declared by a verified dock adapter.
2. **Explicit user selection**: the user picks the output layer *and* the
   source result in the import dialog.

Mapping: Image → `ArrayProcessingResult`; Labels → `LabelsResult`; Points →
`PointsTableResult` (kind table; never promoted to localizations without the
explicit `table-to-localizations` processor); Shapes → ROI manager; others
refused. **Grid**: fresh coordinate space by default. Inheritance only when
the adapter declares `preserves_grid=True` **and** the layer has the same
`ndim` and axis order, equal `scale`, `translate == 0`, `rotate == identity`,
`shear == 0`, `affine == identity`, and the selected source result is the one
the layers were sent from. `ProcessingResult` cannot represent any other
transform, so nothing else may claim its grid. Every import records a
`napari-import` node (Phase 0 interface).

### A.4 Endpoint sessions
`EndpointSession(endpoint_session_uid)` owns the export temp dir and files,
the exact layer objects it added, the dock widget (closed by the session),
the detached viewer (strong ref), and the export worker. Only export
computation runs in the worker; `viewer.open`, dock creation, layer
mutation and plugin interaction run on the GUI thread. Writer formats
receive only the session's layers. Cleanup on explicit close, removal of all
session layers, detached viewer close, or exit. Failure → status bar + log +
immediate cleanup.

### A.5 Spike (DONE 2026-09-05)
Dock lane works inside `EmbeddedNapari`'s window; reader lane works in our
viewer; `napari.current_viewer()` is ours; discovery via `iter_widgets()` /
`iter_compatible_readers()`; `napari-skimage-regionprops` 0.10.1 resolves
cleanly; napari injects the viewer only into class `__init__`s or magicgui
factories. `EmbeddedNapari` segfaults under `QT_QPA_PLATFORM=offscreen` on
macOS; worktree scripts need `PYTHONPATH=<worktree>`.

### A.6 Docs + examples, A.7 Tests
`docs/improcess-napari-plugins.rst` (scope, lanes, gating, mapping tables,
import rules, sessions, worked examples regionprops + napari-storm, agent
recipe, troubleshooting); `examples/improcess_napari_endpoints/`. Tests in
three tiers: GUI-independent units; stubbed controller (offscreen);
guarded native smoke (`IMSWITCH_NATIVE_GUI_TESTS=1`).

---

## B. ImProcess workflows

### B.0 Provenance graph — IMPLEMENTED (Phase 0)
`model/provenance.py`: versioned DAG under `metadata["provenance"]`
(`schema`, `imswitch_version`, `output {node, port}`, `nodes`). Node ops:
`source | reconstruct | consolidate | process | napari-import | opaque`.
Nodes carry plugin id/version/`params_version`, strictly encoded `params`,
ordered `inputs`, named `outputs`, `replayable` + `reasons`, and for
process nodes a runner-level **`restriction`** (ROI geometry via
`ROIRestriction.encode_provenance()`, by reference above 256 KB → non-
replayable). Transitive node union per result, `merge_nodes` conflict
detection, `describe_source` fingerprint (shape, dtype, size, mtime, attrs
digest, manifest), `validate_graph` for untrusted graphs (schema, ≤10 000
nodes, ≤8 MB, refs, ports, acyclic), `derive_history` → the old linear
list. `ProcessorOutput.keys` → ports. Saves are **not** nodes: a written
file carries an `artifact` record (B.3). Compatibility of the derived
history is **schema compatibility**: same step dict shape (`operation`,
`label`, `time`, `params`, `inputs`, plus `step_id`); it now also lists
reconstruction and import steps, which processor-only readers ignore.

### B.1 Parameter codec (Phase 2)
`Processor`/`Reconstructor` gain `params_version`, `encode_params`,
`decode_params(encoded, ctx)`, `migrate_params`, `default_params`. The
strict default is what Phase 0 already applies. Plugin versions are derived
by the runtime, not read from optional class attributes: built-ins →
`importlib.metadata.version("imswitch")`; entry-point plugins → their
distribution version; drop-in files → sha256 of the file. The runtime
stamps `plugin_version` when it registers the plugin.

### B.2 Reconstruction provenance: every producing path (Phase 2)
* `reconstructors/run.py::run_reconstruction(reconstructor, source, params,
  context=None) -> ReconstructionRun` wraps `process()` + `record_reconstruction`.
  `source` is a `DataObj` **or** an `InMemoryStackWrapper`; `describe_source`
  gains `kind: "file" | "memory" | "live"`. A memory/live source without a
  file path records name, dataset, shape/dtype and attrs digest and marks the
  node **non-replayable** ("source was not persisted") unless the wrapper
  reports the path the recording was also written to.
  Call sites: inline, worker, **batch-live fallback**, **RAM recording**,
  headless runner.
* **Streaming**: `record_streaming(result, reconstructor, session_id, params,
  stack_info, completion)` is called on every `result()` snapshot and on
  `finish()`. The node is `reconstruct` with `mode: "streaming"` and a
  `completion` record `{status: "partial" | "complete" | "stalled" | "failed",
  frames_committed, expected_frames}`; snapshots reuse the session's step id
  so the graph does not grow per chunk; `replayable` is true only when
  `status == "complete"` and the source is a file.
* Consolidation → `record_consolidation`. Legacy MoNaLISA → extracted into
  `LegacyMonalisaReconstructor` (id `monalisa-legacy`) with its own codec;
  per-source nodes before its consolidation node; regression test on a
  recorded coefficient set. `scan_params_from_attrs(attrs)` extracted as a
  pure function. `_publishPluginResult` untouched.
* Exit test: a table-driven test enumerating **every row of the §0 path
  table**, each producing a result with a `reconstruct` node.

### B.3 Writers: staged, atomic, receipted (Phase 2)
Protocol, replacing "save returns a receipt then embed it":
1. `plan = result.plan_save(path, fmt) -> SavePlan(primary, companions,
   fmt, view)` — every file the writer will produce, known **before**
   writing (`supported_formats` per type).
2. **No-clobber preflight** over every planned path (unless overwrite).
3. **Stage** all files into a temp directory beside the target.
4. **Embed** the `artifact` manifest (node, port, fmt, planned file list
   with relative names) into the staged primary — the manifest is derived
   from the plan, not from a receipt, so nothing is rewritten.
5. **Publish** companions, then the primary **last**, each by rename; on
   any failure remove every staged and already-published file of this plan.
6. Return `SaveReceipt(files, primary, fmt, node, port)` = what was published.
Containers: OME-TIFF annotation/ImageDescription, HDF5 attrs, OME-NGFF
attrs, Picasso HDF5 attrs inline; **CSV stays plain CSV** and gets a listed
`<name>.provenance.json` companion (the one sidecar, because CSV has no
metadata slot and its first line is a column-header contract). MoNaLISA
keeps its TZCYX fold through `serialization_view()` → OME-TIFF; ImageJ
writer one release as opt-in. All 14 bypassing writers migrate.
**Processor side effects are removed**: `multicolor-registration` returns
the alignment inside its result and the file is written by a `Save` step;
the `save_path` param is deprecated in the GUI (writes through the same
save protocol) and rejected by the headless runner.
Reader: `read_provenance(path) -> ProvenanceDocument(graph, artifact)`;
tests compare `graph` only. Exit test over every type × format, asserting
the receipt equals the files on disk and the document round-trips.

### B.4 GUI-independent core (Phase 3)
* `runtime.bootstrap_registry(config=None, user_plugins=True) -> PluginRegistry`
  returns a **fresh** registry (not the process-wide singleton), rejects
  duplicate ids, stamps plugin versions (B.1).
* `SourceSpec(path, dataset, source_kind, metadata, fingerprint)`; a
  workflow `Source` step holds one; manifests have `path` and `dataset`
  columns per source id; `--bind <id>=<path>[::<dataset>]`; relocation via
  `--source-root`; tiling manifests bind as `source_kind="tiling-manifest"`.
* Steps: `Source`, `Reconstruct`, `Consolidate`, `Process`, `Save(input,
  fmt, path_template)`; refs `step_id[.port]`.
* **Ports contract**: `Processor.output_spec(params, input_specs) ->
  OutputSpec(ports=(…) | pattern="C{i}")`. Static validation accepts a
  declared pattern; runtime validation after execution checks the produced
  ports against the spec; replay uses the concrete recorded ports.
* Two run modes, named: **`run`** binds sources freely (apply this recipe to
  new data); **`replay`** (B.5) verifies sources.
* Outputs through the B.3 protocol (atomic, no-clobber unless `--overwrite`).
* `RunReport` context manager releases `DataObj` handles on close.
* CLI: `python -m imswitch.improcess.workflows run|replay|validate …`.

### B.5 Replay (Phase 4)
`workflow_from_provenance(document) -> Workflow` maps nodes 1:1
(`Consolidate` explicit), decodes params, emits a parameterised `Save`.
**Replay policy**: fails by default on dataset name, shape/dtype, size/mtime
or manifest mismatch and on non-replayable nodes; `--allow-drift` downgrades
source mismatches to warnings; `--verify-hash` adds a full digest. A run
that bound different sources is a `run`, never a "successful replay". LLM
guide for the gaps; GUI "Export workflow…" / "Run workflow…".

### B.6 Docs + examples, B.7 Tests
As v3, plus: a per-path provenance exit test (B.2), the staged-save protocol
test (crash between companion and primary leaves no orphan), CSV companion
listing, `output_spec` static+runtime validation, non-zero-minimum preview
overlay, replay strict-policy failures and `--allow-drift`.

---

## Phasing (sequential)

| Phase | Scope | Exit criterion |
|---|---|---|
| 0 | **DONE 2026-09-05.** Spike; `processors/run.py`; `model/provenance.py` incl. runner-level restriction codec; `footprint.record_step` → graph; `ProcessorOutput.keys`; `split_port_keys`; generic annotations exclude the graph until B.3 | improcess suite 1916 passed; 25 graph tests |
| 1 | **DONE 2026-09-05.** `model/napari_layers.py` (per-kind LayerData incl. localization Points + translated preview, portable colormaps), `model/napari_endpoints.py` (descriptors, exporters, built-in adapters, config + drop-in `NAPARI_ENDPOINTS` hook, npe2 discovery incl. npe1 adapters, gating, availability, writer formats), `model/napari_import.py` + `LabelsResult` + `PointsTableResult` (explicit import, identity-transform grid rule, shapes → ROIs), `model/napari_sessions.py` (ownership by object, temp-dir lifetime), `controller/NapariEndpointController.py` (menu under Plugins → napari plugins, three lanes, threaded export, GUI-thread viewer work, layer-removal auto-close, import dialog, plugin-manager hook), `view/NapariImportDialog.py`, `docs/improcess-napari-plugins.rst` + toctree, `examples/improcess_napari_endpoints/` | 64 GUI-independent + stubbed tests green (suite 1980 passed); **native smoke test PASSED on macOS** (dock inside embedded window, reader in our viewer, clean teardown) |
| 2 | **DONE 2026-09-06** (commits cb184944 B.2, a02e221b B.3, + B.1). B.2: `reconstructors/run.py` (`run_reconstruction`/`run_consolidation`/`record_snapshot`) on all 8 paths; `provenance.record_streaming` (per-session node, `completion` status), `source_kind_of` file/memory/live; `LegacyMonalisaReconstructor` + `monalisa/scan_params.apply_scan_attrs`; worker outcome carries `runs`. B.3: `model/save_protocol.py` (SavePlan → preflight → stage → embed → publish companions-then-primary → SaveReceipt, rollback), `ProcessingResult.save()` is the protocol, `write_files`/`plan_save`/`supported_formats`/`serialization_view` on all 20 writers, MoNaLISA OME-TIFF fold + `imagej` opt-in, CSV `.provenance.json` companion, `provenance_io.read_provenance` → `ProvenanceDocument`, multicolor-registration side effect removed. B.1: `default_params()` on all 37 built-ins (generated from the widgets, `default_params_volatile` for machine-dependent keys), `params_version`/`encode_params`/`decode_params`/`migrate_params` hooks used by the recorder, `model/plugin_versions.py` stamping at registration (distribution version / drop-in file digest), drop-in modules registered in `sys.modules` | per-path exit test (10 cases); type × format exit test (48 cases + protocol/reader tests); defaults-vs-widget sync over 37 plugins; suite 2136 passed |
| 3 | **DONE 2026-09-06.** `imswitch/improcess/workflows/`: `sources.py` (`SourceSpec` path/dataset/kind/fingerprint, `open_source` refuses multi-dataset guesses, tiling manifests, `fingerprint_mismatches`), `steps.py` (Source/Reconstruct/Consolidate/Process/Save, refs `id[.port]`, YAML/JSON, `validate` incl. ports via `Processor.output_spec` patterns, fan-out ports `out1…`, arity, unknown params, formats), `runtime.py` (`bootstrap_registry` fresh + versioned + duplicate-rejecting, `describe_registry`), `runner.py` (`run` → `RunReport` context manager; `run`/`replay` modes, `allow_drift`; no-clobber; path templates), `batch.py` (`run_over`, manifests, `--bind`, summary CSV, per-row isolation), `__main__.py` CLI (`validate`/`run`/`list`); `Reconstructor.prepare_params` (MoNaLISA fills `scan_params` from attrs headlessly; `DEFAULT_LABELS` now the GUI's strings); `Processor.output_spec`/`OutputSpec` (background named ports, splits pattern); examples (2 runnable on synthetic data + 2 MoNaLISA YAMLs, all validate); `docs/improcess-workflows.rst` + toctree | run → save → `read_provenance` graph equality (test_workflows, 22 tests); examples ran on synthetic data; suite 2158 passed |
| 4 | **DONE 2026-09-06.** `workflows/replay.py` (`workflow_from_provenance`/`workflow_from_file` → `ReplayResult`; ancestors in dependency order → steps with ordered port refs and ROI restrictions; parameterised `Save`; codec decode + `migrate_params`, newer `params_version` refused; version drift + streaming origin as warnings; non-replayable/uninstalled/by-reference nodes refused with every reason and the LLM hint; schema-0 histories → best-effort workflow with warnings); `Process.restriction` + runner applies it; `Workflow.metadata` (`provenance_nodes`, `replayed_from`); CLI `replay` (`--out-workflow`, `--run --out`, `--allow-drift`) and `show-provenance` (`--json` = the LLM input); GUI File → "Export workflow of current result…" / "Run workflow…" via `controller/WorkflowController.py` (worker thread, results published through `sigResultProduced`); docs replay + LLM prompt section; `examples/improcess_workflows/replay_from_file.py` | replay round-trip (diamond: identical graph modulo ids/times, identical pixels, never the original path), restricted step, refusals, drift policy, schema-0, CLI, GUI hooks: 10 tests; example run on synthetic data (pixels identical: True); suite 2168 passed |
| 5 | Changelog **DONE 2026-09-06** (three entries under Unreleased). **Remaining, manual:** rig/GUI check of (a) a real dock plugin (napari-skimage) in the embedded viewer via Plugins → napari plugins, (b) File → Export workflow / Run workflow on a real reconstruction, (c) Fiji/Bio-Formats opening a MoNaLISA OME-TIFF with the channel names; then PR. Automated so far: native napari smoke test passed on macOS; improcess suite 2168 passed; imcommon 101 passed | |

## Decisions (Lenny, 2026-09-05)
1. MoNaLISA/SNOUTY → OME-TIFF via `serialization_view()`; ImageJ opt-in one release.
2. Explicit `default_params()` + widget sync test; strict codec is the replay contract.
3. Python-first workflows, YAML serialisation.
4. Reverse import in Phase 1, explicit selection or adapter mapping, fresh grid.
5. Dock example: ~~`napari-skimage-regionprops`~~ → **`napari-skimage`**
   (changed during Phase 1: regionprops 0.10.1 has no `napari.yaml`; its
   only npe1 hook is `provide_function`, and its dock entries come from
   `napari-tools-menu`, i.e. napari's own Tools menu, which is outside the
   endpoint table by A.1. `napari-skimage` 0.7.1 is a pure npe2 plugin from
   the napari org with "Gaussian filter" / "Automated Threshold" / "Label
   connected components" widgets that consume an Image and add Labels,
   which also exercises the reverse-import mapping.) Two more findings from
   the native run: ImProcess's private `grayclip` colormap must be mapped to
   `gray` on layers leaving ImProcess (done: `portable_colormap`), and
   napari's `remove_dock_widget` only hides the dock, so the session deletes
   it (done).
6. CLI: `python -m imswitch.improcess.workflows` only.

## Risks
Own-window plugins; Qt-binding mismatches; graph size on long sessions;
legacy MoNaLISA extraction touches a live path (regression test); streaming
snapshots must not leak per-chunk nodes (same step id per session);
`multicolor-registration` behaviour change for GUI users (deprecation note).

## Review response

### Round 10 (first rig session, 2026-09-15) — 3 findings, all fixed
| Finding | Fix (pinned in `_test/test_gui_followups.py`) |
|---|---|
| Metadata panel did not update for results derived in the session | `metadata_tree_from_result` (identity, metadata, provenance history + graph, source file); `MetadataController` follows `sigCurrentResultChanged`, *Reload* rebuilds |
| Export refused built-in processors the setup's `processing` block did not list (`processors: []`) although the GUI had runtime-loaded them | GUI export/run use the full registry (`bootstrap_registry()`), not the config-narrowed one |
| Workflow-published view-only result crashed the viewer (`Hdf5VirtualArray` has no `transpose`); re-run refused to overwrite | Viewer materialises data without `transpose`; run asks about overwriting when the output folder is not empty |

### Round 9 (plugin-author contract review, 2026-09-12) — cleanup before push
| Finding | Fix (pinned in `_test/test_plugin_contract.py`, `_test/test_plugin_param_contract.py`) |
|---|---|
| Examples/template declared no `default_params`; inherited `{}` claimed replayability | `model/plugin_contract.py`: `has_param_contract` (any override above the framework base, resolved through the MRO); `contract_problem`; provenance adds the reason on `process`/`reconstruct` nodes (not consolidation); `validate` refuses such `Process`/`Reconstruct` steps; `workflows list` prints `GUI-ONLY` |
| Widget/declaration drift unchecked for drop-ins | `record_widget_check` on the widget the GUI builds anyway (processor panel, reconstructor manager): key sets, non-volatile values, codec round trip; a mismatch is remembered on the class and reaches provenance + validation; `check_plugin_contract(cls)` for third-party tests; the pin test now covers `examples/improcess_plugins/` |
| Photophysics hidden `tail`/`roi`; `save()` override bypassed staging | Both declared (and returned by `get_values`); `apply` merges the declared defaults; result converted to `supported_formats`/`plan_save`/`write_files` (CSV + `.provenance.json` companion) |
| Docs taught `save()` overrides; no author checklist | `improcess.rst` "What a plugin gets for free, and what it must declare" (`improcess-headless-contract`), reconstructor example without a custom `save`, `improcess-workflows.rst` GUI-only rule, examples README, internal README §1.3/§4, generated template |

### Round 8 (fifth code review, 2026-09-12) — 2 P1 + 1 P2 + 1 P3, all fixed
| # | Finding | Fix (pinned in `_test/test_review_round8.py`) |
|---|---|---|
| 1 | A queued completion delivered after `shutdown()` published a result and retained its source | `_shuttingDown` flag; `_onFinished`/`_onFailed` close the report and publish nothing |
| 2 | Shutdown leases: loaded results kept handles open; a failing holder read as "nobody" | Split `_loadedUids` / `_externalHeldUids`; at shutdown only external holders count, and an unanswerable holder keeps every handle open |
| 3 | `closeAll()` left subscribers with the stale "held" from close time | `sigSessionsChanged` emitted again after `forget_closed()` |
| 4 | Repeated shutdown appended the same orphaned run twice | Membership guard on `_ORPHANED_RUNS` |

### Round 7 (fourth code review, 2026-09-11) — 3 push blockers + 5 secondary, all fixed
| # | Finding | Fix (pinned in `_test/test_review_round7.py`) |
|---|---|---|
| 1 | `shutil.rmtree(onexc=)` is Python 3.12+ | `onerror` callback (project floor is 3.10) |
| 2 | Workflow shutdown could not join: `thread.quit` was queued behind the blocked GUI thread | `cancelRun` calls `thread.quit()` directly before `wait()`; a run that still does not stop is parked in `_ORPHANED_RUNS` (released by `_clear`) — real-QThread test |
| 3 | Leases incomplete (lineage, cancelled exports) | `EndpointSession.result_lineage`; `held_result_uids` includes lineage and cancelled sessions whose worker has not reported back; `WorkflowController.shutdown` keeps handles a holder still leases |
| S4 | Closing an endpoint never triggered a release | `NapariEndpointController.sigSessionsChanged` (every state change); `addHolder(holder, changed=signal)` connects it to `releaseUnusedSources` |
| S5 | Top-level embedded provenance not strict | `from_dict(strict=True)` for the top-level form; `_history` raises on a non-list / non-JSON history |
| S6 | `np.bytes_` hashed as its repr | bytes checked before `np.generic`; generics recurse through `.item()` |
| S7 | Successful save cleanup silent | Checked `_remove`; leftovers on `SaveReceipt.leftovers` + warning log |
| S8 | `"m"` had no napari unit | `_UNITS["m"] = "meter"` |

### Round 6 (third code review, 2026-09-11) — 6 push blockers + 5 secondary, all fixed
| # | Finding | Fix (pinned in `_test/test_review_round6.py`) |
|---|---|---|
| 1 | Rollback failures swallowed (`_remove` ignored errors) | `_remove` returns why a path is still there; every leftover (published, staging, backup) and every unrestored backup is listed in one `SaveError` |
| 2 | Endpoint layers outlived their HDF5 source | Source lease: `WorkflowController.addHolder(...)`, wired to `NapariEndpointController.heldResultUids` (open/exporting sessions); loaded results' `lineage` counts too |
| 3 | Running workflow thread not shut down | `_RunWorker.cancel()` + thread interruption checked via the runner's `cancel` callback between steps; `shutdown()` cancels, joins (bounded) and only then closes handles; returns False and keeps handles when the run did not stop |
| 4 | Translation hid a Points rotation | `transform_problems()` reports every component; the Points bake refuses any rotation/shear/affine regardless of translation |
| 5 | Per-axis napari units collapsed | `normalized_units()`: length units converted onto one unit with the scales converted along; non-length/heterogeneous units refused; a unit differing from the source's forces a fresh grid |
| 6 | Export cancellation teardown | Cancelled sessions keep their worker as a tombstone until it reports back (then forgotten); `closeAll` joins first and forgets after, parks surviving threads in a module-level holder; the worker checks interruption before starting and sends its directory on failure so a stale failure is cleaned up |
| S7 | Orphan mapping targeted a visible result | Mappings whose result is not in the dialog are filtered; `selection()` revalidates the uid |
| S8 | Structurally corrupt provenance read as absent | `ProvenanceDocument.from_dict(strict=True)` type-checks `schema`/`graph`/`artifact`/history; applied to companions and embedded declared provenance |
| S9 | Failed detached open leaked its viewer | Closed in the exception path |
| S10 | Bytes attributes hashed lossily | `{"__bytes__": hex}` |
| S11 | Unused registry in `cmd_run` | Removed |

### Round 5 (second code review, 2026-09-07) — 12 blockers + 5 secondary, all fixed
| # | Finding | Fix (pinned in `_test/test_review_round5.py`) |
|---|---|---|
| 1 | A failure after the link left a published target; restore errors swallowed | `_publish` records the target in `published` the moment it exists (also when `os.link` raised after linking: `samefile` check); unlink of the staged copy is not a failure; a restore that fails keeps the backup dir and raises `SaveError` naming it |
| 2 | Live provenance failed open | `_record` raises; `finalize` emits `sigFailed` instead of `sigStackFinished`; snapshot failures are logged and not emitted |
| 3 | Initial live stack not counted | `setProvenance(..., initial_frames=)` from the controller (`begin()` data shape) |
| 4 | Codec check missed non-string keys | `_not_json_lossless` checks `{key: value}` as JSON would write it; non-string keys reported, popped and re-keyed as strings |
| 5 | `channel-merge` / `stack-combine` hid dialog params | `default_params()` declare `name`/`axis_label` and `mode`/`join_axis`/`name`/`axis_label`; widgets return them |
| 6 | `sample.v1.h5` / `sample.v2.h5` collided | `source_stem_of`: only the container suffix comes off |
| 7 | One pixel size for every axis; `coordinate_scale` ignored | `px`: z uses `z_step_nm` (required); new `table` unit uses the table's per-axis scale/unit; Points import bakes `translate / scale`, refuses rotation/shear/affine |
| 8 | "Appeared after the session" attribution; dialog kept a stale mapping | Suggestion only when `layer.source.widget` is the session's widget or `source.parent` is a session layer; all claims offered as explicit *Adapter mapping* choices; changing the result drops the mapping |
| 9 | Writer constraints as sets; dispatch by plugin name | npe2 grammar (`? + * {k} {m,n}`) with counts (`writer_accepts`); `napari.save_layers(..., _writer=<the chosen contribution>)` |
| 10 | Reader failure leaked layers | Layers added by a failing `viewer.open` are removed before the session is marked failed |
| 11 | Failed batch rows hid published files | `BatchRow.files` lists them, `partial=True`, summary column, CLI prints them |
| 12 | Malformed `.provenance.json` read as empty | `from_json(strict=True)` for declared companions → `ProvenanceReadError` |
| S1 | CLI reused one registry | `run_over(registry=lambda: ...)` in `run` and `replay` |
| S2 | GUI retained sources forever | Released when none of the run's results is loaded (`sigResultsChanged`) and at `sigClosing` |
| S3 | Exporting session closed without interrupt/join | `requestInterruption` on close; `closeAll` joins export threads (bounded) |
| S4 | Attribute digest over truncated `json_safe` | `attrs_digest` over a full rendering (arrays as element lists) |
| S5 | Docs | Consolidate has no params; registry sharing stated precisely; `table-to-localizations` in the inventory |

### Round 4 (code review of the implemented branch, 2026-09-06) — all 20 fixed
| # | Finding | Fix (all pinned in `_test/test_review_round4.py`) |
|---|---|---|
| 1 | Failed overwrite destroyed existing files; preflight/rename race | Existing targets are moved to a backup dir before publish and restored on any failure; publish is `os.link` (atomic no-clobber) + unlink, guarded rename only for directories (`save_protocol.py`) |
| 2 | Save paths doubled `out_dir` / could escape it | `out_dir` resolved once; rendered path resolved and must be inside it (`runner.render_save_path`) |
| 3 | Strict replay ignored dataset/kind, passed on missing fields, no `--verify-hash` | `spec_mismatches` (dataset, kind), every recorded fingerprint field required, `hash_sources` records sha256, `verify_hash` requires and checks it (`sources.py`, `runner.py`, CLI flags) |
| 4 | Plugin codec output trusted; tuples/keys lossy | Codec output must survive a JSON round trip unchanged or the key is replaced by the strict summary and the node is non-replayable; tuples and non-string keys get markers (`provenance.py`) |
| 5 | Fan-out ports used the global result position | Fan-out runs once per input; ports suffixed with the input index (`signal1`, `background1`) (`runner.py`) |
| 6 | GUI closed sources under published lazy results | `RunReport.detach_sources()`; the controller retains the handles (`WorkflowController.py`) |
| 7 | Name-only mapping could inherit the wrong grid | Attribution needs: layer appeared after the session opened (`layers_before`), not session-owned, exactly one claiming session (`NapariEndpointController._mappingSuggestions`) |
| 8 | Dock failure orphaned layers | Layers registered on the session before the dock is built and removed on failure |
| 9 | Closing an exporting session leaked files | Session keeps its worker and a `cancelled` flag; a late completion is discarded (`_discardStaleExport`) |
| 10 | Dropped live chunks recorded as complete | Failed chunks tracked; `complete` only with no failures and `frames_committed >= expected_frames` (`workers.py`) |
| 11 | Unknown params unchecked for reconstructors / empty defaults | `param_keys()` on both base classes (defaults ∪ `extra_param_keys`); checked for every step kind; MoNaLISA declares `scan_params` (`steps._unknown_params`) |
| 12 | Consolidation recorded params it never used | `run_consolidation` rejects params; `Consolidate` params must be empty |
| 13 | Dotted CSV names collided on the companion | `strip_format_suffix`: only the format suffix goes (`sample.v1.provenance.json`) |
| 14 | Corrupt declared provenance read as empty | `ProvenanceReadError` when the declared provenance is not valid JSON / not an object (`provenance_io.py`) |
| 15 | `allow_drift` still labelled the report a replay | `report.mode = "run"` once drift is accepted |
| 16 | One registry across batch rows | `run_over(registry=<factory>)` gives every row fresh plugins |
| 17 | Recursive DFS in replay | Iterative post-order (`replay._ancestors`); 3000-node chain test |
| 18 | Cancellation lost the report | Checked inside the handler; `RunError.report` attached |
| 19 | Writers documented but not wired | `writerFormatsFor` / `saveSessionLayers` (session layers only) + menu entries |
| 20 | `table-to-localizations` missing | New built-in processor with an explicit column mapping; kind-matrix test updated |

### Round 3 (v3 → v4)
| Finding | Change |
|---|---|
| P1 Live paths missing | §0 path table (8 rows); B.2 `run_reconstruction` over `DataObj`/`InMemoryStackWrapper`, `record_streaming` with `completion` status and per-session step id, per-path exit test. |
| P1 Source info lost | B.4 `SourceSpec(path, dataset, source_kind, metadata, fingerprint)`; manifests/`--bind` carry dataset and kind. |
| P1 ROI codec never sees geometry | **Implemented in Phase 0**: restriction is a runner-level node field encoded by `ROIRestriction.encode_provenance()` (geometry via `ROIRecord.to_dict`), by reference above 256 KB → non-replayable; `run_restricted` no longer puts a summary into params; derived history keeps `region`. |
| P1 Writer protocol circular/non-atomic | B.3 `SavePlan → preflight → stage → embed manifest → publish companions → primary last → receipt`, rollback on failure; processor side effects removed. |
| P1 CSV header change | B.3: CSV unchanged; listed `.provenance.json` companion. |
| P1 Dynamic ports vs static validation | B.4 `output_spec` with port patterns; runtime validation; replay uses recorded ports. |
| P1 Spatial round-trip | A.3 inheritance only for identity transform (translate 0, rotate/affine identity, shear 0, equal scale/ndim/order) + selected source; A.2 preview `translate=(y_min, x_min)` + test. |
| P1 Endpoint compatibility/ownership | A.1 verified exporter/reader pairs only, CSV-to-table claim removed; A.3 explicit user selection or adapter output mapping, no inference from layer events. |
| P1 Replay vs run fingerprint policy | B.4/B.5 two named modes; replay fails on drift by default, `--allow-drift`, `--verify-hash`. |
| P2 Writer descriptor | A.1 `NapariWriterFormat`. |
| P2 Inventory count | §0: 20 concrete writers, 6 shared / 14 bypassing; tiling writes one mosaic TIFF. |
| P2 Typed read result | B.3 `ProvenanceDocument(graph, artifact)`; compare `graph`. |
| P2 Compatibility + bootstrap semantics | B.0 schema compatibility stated; B.4 fresh registry, duplicate ids rejected; B.1 versions from package/file metadata. |

### Round 2 (v2 → v3)
Artifact roots instead of save nodes; explicit `Consolidate`; `(z, y, x)`
localization transform; fresh-grid imports; full writer inventory; legacy
MoNaLISA adapter; phase dependencies fixed; gating; session ownership and
thread affinity; replay safety and lifetimes; "GUI-independent".

### Round 1 (v1 → v2)
Versioned DAG; per-plugin codec; execution-point provenance; MoNaLISA
serialization view; per-kind layer mapping; narrowed promise; three test
tiers; endpoint sessions.
