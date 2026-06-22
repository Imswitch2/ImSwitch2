# ImProcess Extension-Point Follow-up Audit

**Repository:** `/Users/lenny/PycharmProjects/Imswitch2`
**Audit date:** 2026-06-21
**Scope:** `imswitch/improcess` reconstructor/processor registration,
processing configuration, and legacy MoNaLISA integration.

## Summary

ImProcess has a useful internal plugin contract: reconstructors and processors
are accessed through a central registry, and most controllers consume the
registry instead of importing plugin implementations directly. The remaining
gap is that this is not yet an external plugin architecture. Built-ins are
hardcoded in module-level dictionaries, unknown setup plugin IDs are easy to
drop silently, and the generic reconstruction coordinator still special-cases
MoNaLISA and widefield STARSS by id.

## Findings

### [P1] ImProcess "plugins" are still hardcoded built-ins

**Sites:**

- `imswitch/improcess/reconstructors/__init__.py:8`
- `imswitch/improcess/reconstructors/__init__.py:14-28`
- `imswitch/improcess/reconstructors/__init__.py:56-63`
- `imswitch/improcess/processors/__init__.py:8-30`
- `imswitch/improcess/processors/__init__.py:68-74`

**Evidence:**

The reconstructor module explicitly says entry-point loading can come later.
Both reconstructors and processors are imported directly and registered from
module-level dictionaries.

**Impact:**

Adding a new reconstructor or processor still requires editing core source.
This is acceptable for current built-ins, but it conflicts with the stated
plugin architecture and will limit external ET variants, new reconstruction
pipelines, and third-party analysis processors.

**Next fix:**

Mirror the device-manager plugin model:

- Define an ImProcess plugin contribution manifest or entry-point group.
- Keep built-ins as built-in contributions.
- Let setup `processing.reconstructors` and `processing.processors` resolve
  installed external IDs with actionable diagnostics.
- Preserve the current built-in dictionaries only as compatibility shims.

### [P1] Unknown processing plugin IDs can be silently ignored

**Sites:**

- `imswitch/improcess/model/processing_config.py:76-88`
- `imswitch/improcess/controller/ImProcessMainController.py:96-111`
- `imswitch/improcess/reconstructors/__init__.py:56-63`
- `imswitch/improcess/processors/__init__.py:68-74`
- `imswitch/improcess/reconstructors/registry.py:103-115`

**Evidence:**

`plugin_ids_from_config()` returns explicit IDs from the setup processing block.
`register_default_reconstructors()` and `register_default_processors()` then
filter module-level dictionaries by `if pid in filter_ids`; unknown IDs are not
reported. If a config requests only unknown reconstructors, the registry can end
up empty and failure is deferred until later selection.

**Impact:**

A typo in a setup file can look like "no reconstructors registered" rather than
"unknown reconstructor id X". That is the same class of configuration drift as
stale `availableWidgets` keys.

**Next fix:**

Make explicit plugin config strict:

- Compare requested IDs against available/installed IDs.
- Raise or log a clear startup error listing unknown IDs and available IDs.
- Extend `test_processing_config.py` to validate both bundled reconstructor and
  processor IDs, not only processors in selected presets.

### [P1] Generic reconstruction flow still special-cases MoNaLISA

**Sites:**

- `imswitch/improcess/controller/ImProcessMainViewController.py:41-45`
- `imswitch/improcess/controller/ImProcessMainViewController.py:56-58`
- `imswitch/improcess/controller/ImProcessMainViewController.py:109-110`
- `imswitch/improcess/controller/ReconstructorManagerController.py:99-106`
- `imswitch/improcess/controller/ReconstructorManagerController.py:121-124`
- `imswitch/improcess/controller/ReconstructorManagerController.py:145-151`

**Evidence:**

`ImProcessMainViewController` always creates `MoNaLISAController` and routes
scan-parameter signals to it. `currentDataChanged()` always calls
`monalisaController.parseScanParamsFromAttrs()`. The generic reconstructor
manager hides/shows actions by checking `reconstructor.id == 'monalisa'`,
keeps the legacy MoNaLISA parameter tree instead of using the plugin widget,
and delegates reconstruction to `runLegacyReconstruct()` when the active
reconstructor id is `monalisa`.

**Impact:**

MoNaLISA is halfway migrated: there is a `MonalisaReconstructor`, but the UI and
legacy signal-extraction path still live in a dedicated controller. Future
modality-specific behavior could repeat this pattern unless plugins get a real
capability/action interface.

**Next fix:**

Move the remaining MoNaLISA scan-parameter and action behavior behind the
reconstructor interface:

- Reconstructor declares whether it needs acquisition metadata.
- Reconstructor provides optional actions such as "update reconstruction" or
  "show scan params".
- Generic controller binds plugin-declared actions instead of checking ids.
- Remove always-on `MoNaLISAController` once the plugin path owns the full
  workflow.

### [P2] Plugin-specific action wiring is hardcoded by id

**Sites:**

- `imswitch/improcess/controller/ReconstructorManagerController.py:127-133`

**Evidence:**

The manager detects `reconstructor.id == "widefield-starss"` and wires batch
signals to `wfsBatchController` directly.

**Impact:**

This works for one built-in, but it does not generalize. The next reconstructor
that needs a side action, batch flow, or auxiliary plot will require another id
check in the generic manager.

**Next fix:**

Expose plugin actions/capabilities through the reconstructor contract. The
controller should ask a reconstructor for action descriptors and callbacks,
then bind them generically.

### [P2] ImProcess setup config still depends on imcontrol internals

**Sites:**

- `imswitch/improcess/model/processing_config.py:8-15`
- `imswitch/improcess/controller/ImProcessMainController.py:54-60`
- `imswitch/improcess/controller/ImProcessMainController.py:232-236`

**Evidence:**

ImProcess reads the selected setup through `imswitch.imcontrol.model` and
persists layout through imcontrol's widget-state persistence service.

**Impact:**

This was already flagged in the cross-layer audit, but it matters especially
here: ImProcess cannot become a clean standalone processing application or
external plugin host while its setup/config bootstrap depends on imcontrol.

**Next fix:**

Move shared setup-processing config and widget-state persistence contracts to
`imcommon`, then inject the concrete persistence backend from the application.

## Suggested sequencing

1. Make explicit `processing.reconstructors` / `processing.processors` config
   strict and add bundled setup tests for both lists.
2. Move MoNaLISA's remaining update/scan-param behavior into the reconstructor
   contract or plugin action contract.
3. Replace id-specific reconstructor action wiring with plugin-declared action
   descriptors.
4. Add entry-point based external ImProcess plugin discovery, using the device
   plugin registry as the design template.
5. Move processing config and layout-persistence contracts from `imcontrol` to
   `imcommon`.
