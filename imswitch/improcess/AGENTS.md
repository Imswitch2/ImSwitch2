# imswitch/improcess — agent notes

Root rules: `AGENTS.md` (repository root). The contracts (Reconstructor,
Processor, ProcessingResult, streaming), data flow and the `processing` block
schema are in `README.md` next to this file; user docs in `docs/improcess.rst`,
`docs/improcess-workflows.rst`. This file adds what that README's §4
checklist leaves out: every place a new plugin must be registered and the test
that fails when one is missed. Paths are relative to this package.

## Map

| Path | What |
|---|---|
| `reconstructors/` | raw dataset (`DataObj`) -> result; built-ins in `_AVAILABLE_RECONSTRUCTOR_CLASSES` (`reconstructors/__init__.py`) |
| `processors/` | result -> result; built-ins in `_AVAILABLE_PROCESSOR_CLASSES` (`processors/__init__.py`) |
| `analysis/` | Qt-free numeric kernels the processors delegate to |
| `model/` | result types, `DataObj`, `processing_config.py`, `plugin_contract.py`, `param_spec.py` |
| `plugins/` | drop-in user plugins (`user_plugins.py`), online index client |
| `workflows/` | headless workflow runner: `python -m imswitch.improcess.workflows list` |
| `live/` | live reconstruction while ImControl writes (`LiveSource`, workers) |
| `controller/` | `ImProcessMainController`, sub-controllers, `ProcessorRunner` (runs off the GUI thread) |

Registry access: `get_registry()`; ids: `available_reconstructor_ids()`,
`available_processor_ids()` (sorted, built-ins + drop-ins).

## Add a processor (traced on f1ae1ec1, 8f54d646)

Paths in brackets are under `_test/` unless they start with `imswitch/`.
1. `processors/<op>/__init__.py` + `processor.py`; kernel in `analysis/`.
2. Class attributes: `id`, `name`, `kinds`, `output_kind` (not `unknown`)
   [`test_output_kinds.py`]; `deterministic` stays True (only `python` is not).
3. `default_params()` equal to what the widget's `get_values()` returns
   [`test_plugin_param_contract.py`, `check_plugin_contract`]; `param_spec()`
   agrees with the widget [`test_param_spec_matches_widgets.py`]; bump
   `params_version` + `migrate_params` when stored params change meaning.
4. Register: `'<id>': Class` in `_AVAILABLE_PROCESSOR_CLASSES`.
5. Add the id, sorted, to `IMPROCESS_PROCESSOR_FALLBACK` in
   `imswitch/imcontrol/view/configeditor/editor.py`
   [`imswitch/imcontrol/_test/unit/test_config_editor_plugin_fallback.py`, unit lane].
6. If `kinds` includes localizations or labels: add the id to
   `LOCALIZATION_PROCESSOR_IDS` / `LABELS_PROCESSOR_IDS` [`test_result_kind_matrix.py`].
7. Own test `_test/test_<op>.py`; a section in `docs/improcess.rst`; changelog bullet.
8. Optional dependency: lazy import inside the run, a `setup.cfg` extra, and the
   panel says how to install it (pattern: `processors/smlm_drift_comet/`).

## Add a reconstructor (traced on d091f6bc)

1. `reconstructors/<modality>/__init__.py` + `reconstructor.py`; result class
   (existing one, or a `ProcessingResult` subclass with `kind`).
2. `id`, `name`, `file_extensions`, `output_kind`; `default_params()` equal to the
   widget [`test_plugin_param_contract.py`].
3. Register in `_AVAILABLE_RECONSTRUCTOR_CLASSES`.
4. Add the id, sorted, to `IMPROCESS_RECONSTRUCTOR_FALLBACK`
   (`imswitch/imcontrol/view/configeditor/editor.py`) [`test_config_editor_plugin_fallback.py`].
5. A row in `_RECONSTRUCTOR_RESULTS` [`test_output_kinds.py`: "a new reconstructor needs a row here"].
6. Opening its files from ImControl runs or workflows may need
   `controller/FileIOController.py`, `model/dataset_sources.py`, `workflows/sources.py` (as d091f6bc).
7. Test, `docs/improcess.rst`, changelog.

## Conventions (enforced by the tests above)

- Last two axes are `Y, X`; `len(axis_labels) == data.ndim`; carry
  `axis_scales` / `scale_unit` through.
- `process()` / `apply()` are pure and return new results; no GUI work.
- Controllers reach plugins only through the registry, never by import.
- Long runs go through `ProcessorRunner` / workers, never the GUI thread;
  cancellation is cooperative (`checkpoint()`).

## Tests

Lane `improcess` (`imswitch/improcess/_test`, `test_snouty.py` ignored in CI).
Registry-wide guards iterate every built-in, so a new plugin is tested by
`test_output_kinds.py`, `test_plugin_param_contract.py`,
`test_param_spec_matches_widgets.py`, `test_result_kind_matrix.py`,
`test_result_pipeline_generality.py` without being named in them.
