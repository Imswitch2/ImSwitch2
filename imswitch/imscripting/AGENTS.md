# imswitch/imscripting — agent notes

Root rules: `AGENTS.md` (repository root). User docs: `docs/scripting.rst`.
Cannot run alone (`python -m imswitch.imscripting` raises); always loaded last,
because the script scope snapshots the controllers of modules loaded before it.

## Map

| Path | What |
|---|---|
| `controller/ImScrMainController.py` | builds the script scope in `_createScriptScope`: `api`, `controllers`, `mainWindow`, `moduleCommChannel` + actions |
| `model/actions.py` | global helpers of the scope (`_Actions`: `sleep`, `getWaitForSignal`, `callAndWaitForSignal`, `runScanAndWait`, `waitUntil`, `importScript`, `show`, `temporaryDetectorSettings`, `returningToPositions`, ...) |
| `model/ScriptExecutor.py` | runs a script on an `ExecutionThread` worker; stdout/stderr routed to the output view |
| `model/ScriptSession.py` | persistent namespace for Run cell / Run selection (`# %%` cells) |
| `model/acquisition_script.py` | File > New script from current settings |
| `_test/tutorial_runner.py` | runs one tutorial headless on a setup, one process per script |

## Rules

- Scripts run off the GUI thread. Anything a script reaches that touches Qt must
  be an `@APIExport(runOnUIThread=True)` method (`imswitch/imcommon/model/api.py`).
- Cancellation is cooperative: long helpers call `checkpoint()` /
  `cancellableSleep` from `imswitch/imcommon/model/cancellation.py`;
  `OperationCancelled` derives from `BaseException`, so a bare `except Exception`
  in a helper does not swallow Stop.
- Helpers that change settings restore them on error and Stop (`_Restoring`);
  follow that pattern for new `temporary*` helpers.
- A new method on `_Actions` or a new `@APIExport` changes `docs/api/`: run
  `PYTHONPATH=$PWD python tools/generateapidocs.py` and commit
  [`_test/test_scripting_api_docs.py`]. Every export needs a docstring.
- Shutdown closes the API gate (`apiGate`) and drains the script thread before
  ImControl finalizes hardware; keep new threads joinable.

## Tutorials

`imswitch/_data/user_defaults/scripts/tutorial/` (`basic/`, `scanning/`,
`measurement/`). Each script header has `Mock setup: <file>.json` and
`It simulates:`; `README.md` there lists every script.
`_test/test_shipped_tutorials.py` checks headers and the index and runs each
tutorial on its setup. Any change to a shipped file: `python tools/update_user_defaults_history.py`.

## Tests

Lane `core` (`imswitch/imscripting/_test`, including `unit/`). The `test_*_in_app.py`
tests build ImControl on mock setups in subprocesses and are slow.
