# ImProcess workflows: chaining reconstructors and iterating over axes

Status: plan, branch `feat/improcess-chaining-iteration` (off `dev`)

## Motivation

A real SNOUTY dataset could not be processed by any workflow:

- The file is one 3D array that holds a whole time-lapse of skewed raw data,
  the timepoints placed **next to each other along a camera axis** (not along
  the plane axis, where `n_timepoints` would already split them).
- A Python step can reshape it into a proper `(T, Z, Y, X)` skewed stack, but
  to run a Python step the data first has to go through `view_only`, and the
  SNOUTY deskew reconstructor then refuses it: a reconstruction's input must
  be a `Source` step (`imcommon/model/workflow/steps.py::validate`,
  "a reconstruction's input must be a source step"; the runner does
  `report.result(step.inputs[0])` and hands it to `plugin.process` as a raw
  `DataObj`).
- After deskewing each timepoint, a MoNaLISA reconstruction should run on the
  deskewed output, probably with a crop step in between. That is a second
  reconstructor in the same chain, which is impossible today.
- There is no way to say "do this per timepoint". The only control flow is
  the smart-pipeline `Gate` (yes/no).

The target workflow:

```
source -> view_only -> python(reshape -> T,Z,Y,X, skewed attrs)
       -> snouty (per T) -> crop (python or processor) -> monalisa (per T) -> save
```

## Design principles

1. **Check the data, not where it came from.** Whether a reconstructor can
   take an input is decided by the input's axes, kind and attrs, never by
   whether it is a raw `Source`. Provenance records the history; it does not
   gate it.
2. **Auto-indexing (LabVIEW-style).** A plugin declares the dimensions it
   works on (its *core dims*). Any extra leading axes on the input (T, C,
   position, ...) are looped over by the runner, and the outputs are stacked
   with those axes kept in front. Most "loops" never need to be written.
3. **Explicit iteration stays a DAG.** For what auto-indexing cannot express,
   a `map` step (a sub-workflow run once per index) and later a `fold` step
   (carried state, like a LabVIEW shift register). No open-ended while-loops:
   every run must stay replayable and fully described by its provenance.
4. **Opt-in, nothing existing changes behaviour.** A plugin that declares no
   core dims behaves exactly as today. Existing workflow files load and run
   unchanged (schema bump only if a new step kind is used).

## Phase 0: baseline

- Branch `feat/improcess-chaining-iteration` from `dev`.
- Run the improcess workflow, runner, snouty, python-step and monalisa test
  modules and record the baseline (pass/fail/skip) in the PR description, so
  regressions are distinguishable from pre-existing failures.

## Phase 1: any image result can feed a reconstructor

Goal: `Reconstruct.inputs[0]` may reference a `Source` **or** any step port
whose result is an image.

- **Bridge `ProcessingResult` -> `DataObj`.** Add one function (e.g.
  `improcess/model/result_source.py::data_obj_from_result(result)`) that
  builds a `DataObj` backed by the result's array (in-memory container,
  see `model/image_sources.open_memory_container`, or a lazy array source
  when the result is lazy), carrying:
  - `axis_labels` (declared), axis scales/units;
  - the result's attrs/metadata, including any acquisition layout and
    SNOUTY geometry, so `recorded_snouty_geometry` and
    `acquisition_layout` keep working where the attrs are present;
  - a provenance link to the parent result, so the reconstruction's
    provenance graph contains the full upstream chain (not a fresh "source").
  Make sure `fingerprint_of` / `checkAndLoadData` / `checkAndUnloadData` work
  on it (no file path).
- **Runner** (`improcess/workflows/runner.py`, `Reconstruct` branch): if the
  input is not a `DataObj`, convert with the bridge before
  `prepare_params` / `run_reconstruction`.
- **Validation** (`imcommon/model/workflow/steps.py::validate`): replace
  "input must be a source step" with "input must be a source or produce an
  image" using `declared_kinds`. Keep "exactly one input".
- **Bindings**: relax the `run()` check that forbids binding a Source that
  feeds a reconstruction only where it is now redundant; keep the message for
  sources without data.
- **Find and remove the "already reconstructed" rejection** in the GUI path
  (`ReconstructorManagerController`, `ReconstructionViewController`, the
  workflow editor's port/connection rules): a user must be able to pick a
  result (including a `view_only` result or a Python-step output) as the
  input of a reconstructor, both interactively and in the workflow editor.
- **Consolidate** is unchanged.

Tests: workflow `source -> view_only -> python -> snouty` validates and runs
on synthetic data; provenance of the snouty result contains the python step;
replay of that workflow reproduces it.

## Phase 2: declared input contracts and metadata propagation

- **Reconstructor contract** (`reconstructors/base.py`), all optional:
  - `core_dims_in: tuple[str, ...] | None`, e.g. SNOUTY `("Z", "Y", "X")`
    (planes, cam_y, cam_x), MoNaLISA whatever one reconstruction consumes.
  - `core_dims_out: tuple[str, ...] | None`.
  - `required_attrs: tuple[str, ...]` (e.g. SNOUTY geometry keys when not
    given as params).
  - `check_input(data_obj, params) -> list[Issue]`: default implementation
    checks the above; plugins may extend. Used by validation (static, from
    declared kinds/axes where known) and by the runner before running.
- **Processors** get the same optional `core_dims_in/out` (the base class
  already has `accepts`; extend, do not replace).
- **Python step**: `make_result(..., attrs=None, inherit_attrs=True)` so a
  script can declare axes **and** pass through or set metadata (skew angle,
  scan step, pixel sizes, a `geometry: skewed|deskewed` marker). Default:
  inherit the first input's attrs, so a reshape keeps the SNOUTY geometry.
  Document this in the Python-step help/snippets.
- **SNOUTY**: accept `(T, Z, Y, X)` input directly in addition to the current
  3D + `n_timepoints` layout (see Phase 3; with core dims declared, the
  runner does the T loop). Set `geometry: deskewed` (and voxel sizes) on its
  output attrs. Keep the legacy path for 3D raw input bit-identical.
- **MoNaLISA / MoNaLISA lattice**: declare core dims; read pixel sizes from
  attrs when present.

Tests: a wrong-shaped input gives a clear `Issue` at validate time when the
kind/axes are known, else a clear error before any pixels are processed.

## Phase 3: auto-indexing over extra axes

In the runner, for `Reconstruct` and `Process` steps whose plugin declares
`core_dims_in`:

- `loop_dims = [ax for ax in input.axis_labels if ax not in core_dims_in]`
  (must be leading axes; if not, transpose a view or error clearly).
- If `loop_dims` is empty: run as today.
- Otherwise iterate over `np.ndindex(*loop_shape)`: slice the input (lazy
  where the source is lazy), build the per-slice `DataObj`/result with the
  core axes and inherited attrs, run the plugin, collect outputs, stack to
  `loop_shape + core_out_shape`, result `axis_labels = loop_dims +
  core_dims_out`. Outputs of differing shape across iterations are an error
  with the offending index named.
- Respect `cancel()` and `progress()` per iteration; report
  `step_id [t=3/120]` in progress text.
- Memory: if the stacked output would exceed a threshold (reuse the
  existing resource-estimate machinery), write iterations into a temporary
  zarr store and return a lazy result instead of holding everything in RAM.
  First version may be in-memory only with a clear TODO and a size guard.
- Provenance: one node for the step with `iterated_over: {"T": n}` (axis ->
  length) and the per-iteration params if they differ (they don't, yet);
  never one node per iteration.
- Step-level opt-out: `params: {_iterate: false}` (reserved key, never
  passed to the plugin), for plugins that want the whole stack.
- The same machinery must be usable from the GUI's interactive reconstruct /
  process paths (call one shared helper, do not duplicate it in the runner).

Tests: snouty on `(T, Z, Y, X)` equals per-timepoint snouty stacked;
a processor with core dims `("Y","X")` on `(T, C, Y, X)` loops T*C times;
cancel mid-loop leaves no half result; provenance has `iterated_over`.

## Phase 4: `map` step (explicit sub-workflow per index)

For chains that should stream frame by frame (huge intermediates) or need a
group of steps applied per index.

```yaml
- step: map
  id: per_t
  input: reshape.out
  over: T
  body:
    - {step: reconstruct, id: deskew, reconstructor: snouty, inputs: [$item]}
    - {step: process, id: crop, processor: python, inputs: [deskew.out], params: {...}}
    - {step: reconstruct, id: mona, reconstructor: monalisa, inputs: [crop.out]}
  output: mona.out
```

- Register via `register_step_type("map", Map, run=run_map)`; `$item` is the
  per-index slice. Body steps may also reference outer results (broadcast).
- Body is validated as a sub-workflow; `Save` is not allowed inside the body
  in v1.
- Runner: for each index, run the body with the slice bound, stack the
  `output` along `over`; same progress/cancel/provenance rules as Phase 3
  (provenance records the body once plus `iterated_over`).
- Workflow editor: render a `map` step as a group node with its body; at
  minimum it must load, display, save and round-trip such a workflow without
  losing the body. Full drag-into-group editing may be a follow-up if large.
- `fold` (carried state between iterations, e.g. drift reference, a crop box
  chosen on frame 0): design the schema now (`carry:` initial value,
  body produces next carry), implement if time permits, otherwise leave a
  documented stub that validates and refuses to run.

## Phase 5: end-to-end example and docs

- `examples/improcess_workflows/snouty_side_by_side_timelapse.yaml` plus a
  generator for a small synthetic dataset (timepoints side by side along X).
  The example: source -> view_only -> python reshape -> snouty (auto-indexed
  over T) -> crop -> monalisa (auto-indexed) -> save. If MoNaLISA cannot
  meaningfully run on synthetic deskewed data, use a minimal configuration
  that exercises the plumbing and say so in the example.
- A second version of the same example using `map`.
- Update `docs/improcess-workflows.rst`: chaining reconstructors, core dims
  and auto-indexing, `map`/`fold`, Python-step `attrs`.

## Out of scope

- Unbounded loops / while-until-converged at the workflow level (belongs
  inside a single plugin).
- Parallel execution across iterations (design so it can be added: the
  per-iteration call must not depend on shared mutable state).

## Risks

- `DataObj` assumes a file in places (paths, fingerprints, live sources):
  the bridge must be tested against every reconstructor's `prepare_params`.
- Reconstructors that already split timepoints internally (SNOUTY
  `n_timepoints`, time_lapse) must not be double-looped: with a declared T
  loop, internal `n_timepoints` must be 1, enforced with a clear error.
- Provenance replay must reproduce auto-indexed and mapped steps exactly.

## Implementation notes

### Phase 0: baseline

The branch was first cut from `dev` and then, on the maintainer's
instruction, rebased onto `main` (`dev` is a one-way integration branch; this
branch must carry no dev-only commits). The baseline below is the one on
`main` (`5c090c56`, plus the plan commit), `QT_QPA_PLATFORM=offscreen`,
Python 3.13, over the 55 test modules that exist there among
`test_workflow*.py`, `test_snouty*.py`, `test_python_step*.py`,
`test_monalisa*.py`, `test_provenance_graph.py` and
`test_reconstruction_provenance_paths.py`:

**9 failed, 1156 passed, 4 skipped.** All 9 failures are pre-existing:
`test_snouty.py` (`TestSnoutyMetadata` x5, `TestSnoutyParamsWidget` x2,
`TestSnoutyWidgetPrefill` x2), each `TypeError: not all arguments converted
during string formatting` from the SNOUTY attribute readers' debug logging.

(For the record, the same modules on `dev` gave 21 failed / 1192 passed /
4 skipped; the 12 extra failures there are MoNaLISA tests and the shipped
`monalisa_reconstruct_chain.yaml` example, both dev-only states.)

#### Differences between `dev` and `main` that shaped the implementation

- `main` has no step-type registry (`register_step_type`), no schema 2
  (controls/bypass) and no `imcommon.model.workflow`: the schema lives in
  `improcess/workflows/steps.py` and the editor model in
  `improcess/model/workfloweditor/`. `map`/`fold` are therefore built-in step
  kinds on this branch (see Phase 4).
- `main`'s runner refuses binding a `Source` to an in-memory result outright;
  that refusal is kept (see Phase 1).
