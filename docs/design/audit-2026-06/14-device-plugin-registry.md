# Device Plugin Registry Follow-up Audit

**Repository:** `/Users/lenny/PycharmProjects/Imswitch2`
**Audit date:** 2026-06-22
**Scope:** `imswitch.imcontrol.model.plugins`, `MultiManager` runtime
resolution, `imswitch.pluginapi`, plugin diagnostics, and device-plugin docs.

## Summary

The device plugin system is a strong direction: entry-point discovery avoids
importing hardware code during discovery, the registry gives clearer errors
than raw import failures, and there is already useful test coverage. The main
remaining risk is that the implementation is only partially aligned with the
contract documented for plugin authors. Registry-first resolution can shadow
many in-tree managers because only six core managers are protected as built-ins,
the manifest parser accepts kinds that the runtime cannot load, discovery
failures are collected and then dropped during normal startup, and the public
plugin API does not cover every registry-backed kind.

## Findings

### [P1] Registry-first resolution can shadow unlisted in-tree managers

**Sites:**

- `imswitch/imcontrol/model/plugins/builtins.py:6-54`
- `imswitch/imcontrol/model/managers/MultiManager.py:54-73`
- `imswitch/imcontrol/_test/unit/test_device_plugin_registry.py:216-240`
- `imswitch/imcontrol/_test/unit/test_device_plugin_diagnostics.py:186-214`

**Evidence:**

`MultiManager._resolveManagerClass()` asks the registry before trying the
legacy internal import path. The built-in contribution table contains only six
protected managers. Tests assert that exact six-manager table, and another test
documents `GRBLStageManager` as a real in-tree positioner manager that is absent
from the built-in registry and therefore resolves only via the legacy path.

**Impact:**

An installed plugin can declare an id or alias equal to an unlisted legacy
manager name, and registry-first resolution will load the plugin instead of the
core manager. That contradicts the intended "existing setup files unchanged"
contract for any in-tree manager not present in `BUILTIN_DEVICE_MANAGERS`. This
is especially risky while extracting managers gradually: accidental name
collisions can silently change hardware behavior.

**Next fix:**

Protect every in-tree `MultiManager`-backed manager name, not only the first six
entries:

- Generate built-in contributions from the core manager packages, or maintain a
  complete built-in table for all supported legacy managers.
- Reject plugin ids and aliases that collide with any legacy manager unless an
  explicit extraction/migration flag is present.
- Add a regression test where a plugin tries to use `GRBLStageManager` or
  another unlisted core manager name and is rejected.

### [P1] Manifest accepts device kinds that cannot be loaded through the registry

**Sites:**

- `imswitch/imcontrol/model/plugins/manifest.py:13-25`
- `imswitch/imcontrol/model/plugins/manifest.py:70-73`
- `imswitch/imcontrol/model/managers/MultiManager.py:15-23`
- `imswitch/imcontrol/model/plugins/validation.py:13-33`
- `imswitch/imcontrol/controller/MasterController.py:26-38`
- `imswitch/imcontrol/controller/MasterController.py:69-71`
- `imswitch/imcontrol/model/managers/StandManager.py:15-26`

**Evidence:**

`DeviceKind` and `parse_manifest()` accept `stand` and `pulse_generator`.
`MultiManager` maps only detector, laser, positioner, rotator, rs232,
flip-mirror, and slm packages to registry kinds. Setup validation checks only
those `MultiManager`-backed sections. At runtime, the pulse generator is created
directly from `setupInfo.teensyPulse`, and microscope stands are loaded by
`StandManager` through bespoke import paths and a mock fallback.

**Impact:**

A plugin can publish a manifest that is syntactically valid and appears in
`list`/`inspect`, but no setup section can actually instantiate it through the
registry. The user docs mention the split, but the parser and CLI do not warn
that these contribution kinds are metadata-only today.

**Next fix:**

Choose one contract and enforce it:

- Either reject unsupported runtime kinds in `parse_manifest()` until they are
  actually loadable.
- Or keep them with an explicit `runtime_supported: false` status and make
  diagnostics warn that they cannot be used in setup files yet.
- Port `StandManager` and pulse-generator construction to the registry before
  advertising those kinds as normal contributions.

### [P2] Plugin discovery and duplicate-registration failures are dropped

**Sites:**

- `imswitch/imcontrol/model/plugins/discovery.py:73-79`
- `imswitch/imcontrol/model/plugins/registry.py:260-272`
- `imswitch/imcontrol/model/plugins/__main__.py:60-68`

**Evidence:**

Discovery collects `DiscoveryError` objects so one broken plugin does not block
the others. `build_default_registry()` then registers discovered contributions,
silently ignores `DuplicateContributionError`, and leaves a TODO to log
discovery errors once logging is configured. The diagnostics CLI builds that
same default registry before running `list`, `inspect`, or `validate-setup`.

**Impact:**

Broken manifests, missing resources, and duplicate ids can disappear from the
normal startup path. Plugin authors and users may only see that a manager is not
available, without knowing that discovery failed or a contribution was skipped
because of a collision.

**Next fix:**

Carry registry diagnostics alongside the registry:

- Preserve discovery errors and duplicate-registration warnings.
- Log them during application startup once logging exists.
- Show them in the CLI by default or behind `--verbose`.
- Add a strict validation mode that fails on discovery errors and duplicate
  plugin contributions.

### [P2] Compatibility metadata is documented but not enforced

**Sites:**

- `docs/devices/plugins.rst:228-236`
- `imswitch/imcontrol/model/plugins/manifest.py:46-113`

**Evidence:**

The user-facing docs say `schema_version` and `imswitch_min_version` gate
plugin/host compatibility. `parse_manifest()` only reads the
`contributions.device_managers` list and validates each entry's required
manager fields and kind. It does not read or enforce top-level `schema_version`,
`imswitch_min_version`, plugin name, license, or compatibility fields.

**Impact:**

An incompatible future manifest can be accepted as long as its manager entries
look like the current shape. Conversely, plugin authors may believe the
compatibility fields are active when they are currently documentation-only.

**Next fix:**

Make manifest compatibility explicit:

- Parse top-level manifest metadata into a `PluginManifest` object.
- Enforce supported `schema_version` values.
- Check `imswitch_min_version` against the running host version.
- Report unknown future manifest versions as actionable diagnostics rather than
  accepting them silently.

### [P2] Manager property schema validation silently degrades

**Sites:**

- `imswitch/imcontrol/model/plugins/validation.py:60-70`
- `imswitch/imcontrol/model/plugins/validation.py:73-96`
- `imswitch/imcontrol/model/plugins/validation.py:99-123`
- `imswitch/imcontrol/model/plugins/validation.py:233-241`
- `imswitch/imcontrol/model/plugins/__main__.py:141-159`

**Evidence:**

If `jsonschema` is unavailable, validation returns no manager-property errors.
If a contribution declares `manager_properties_schema` but the resource cannot
be read or parsed, `resolve_schema()` returns `None`. `validate_setup_file()`
then validates properties only when a contribution and schema object are both
available.

**Impact:**

The CLI can report that managers resolved successfully while declared schemas
were missing, broken, or skipped. That is acceptable for application startup
tolerance, but it is weak for plugin authoring and setup review.

**Next fix:**

Distinguish "not declared" from "declared but unavailable":

- Add schema warnings when a declared schema cannot be resolved.
- Add a strict CLI flag that fails if `jsonschema` is missing or a declared
  schema cannot be loaded.
- Encourage CI for plugin packages to run strict setup-template validation.

### [P2] Public plugin API does not cover every registry-backed kind

**Sites:**

- `imswitch/pluginapi/__init__.py:9-23`
- `imswitch/pluginapi/devices.py:7-39`
- `imswitch/imcontrol/model/managers/MultiManager.py:15-23`
- `docs/devices/plugins.rst:91-108`
- `docs/design/DEVICE_PLUGINS.md:189-204`

**Evidence:**

`MultiManager` supports registry-backed `rs232`, `flip_mirror`, and `slm` kinds
in addition to detector, laser, positioner, and rotator. The documented public
plugin API exports setup dataclasses plus detector, laser, positioner, and
rotator manager bases. It does not expose a stable public base or contract for
RS232, flip mirrors, or SLMs.

**Impact:**

Authors can declare plugins for registry-backed kinds that do not yet have a
complete public authoring surface. They must either rely on duck-typing without
a documented base or import deep internal paths, which the docs explicitly tell
them not to do.

**Next fix:**

Align `imswitch.pluginapi` with the registry-backed kind list:

- Export stable contracts for RS232, flip-mirror, and SLM managers, or mark
  those kinds as advanced/internal until the contracts exist.
- Add plugin-template examples for every supported kind class.
- Add tests that each registry-backed kind has either a public plugin API
  contract or an explicit "unsupported for external authors" marker.

## Suggested sequencing

1. Protect all in-tree `MultiManager`-backed manager names from plugin
   collisions before extracting more device families.
2. Decide whether `stand` and `pulse_generator` are supported plugin kinds now;
   reject or warn on them until runtime loading exists.
3. Preserve and surface plugin discovery, duplicate-registration, and schema
   diagnostics in startup logs and CLI output.
4. Enforce manifest compatibility metadata (`schema_version`,
   `imswitch_min_version`) instead of documenting it as active.
5. Extend `imswitch.pluginapi` or narrow the advertised registry-backed kind
   list so plugin authors have a real stable contract.
