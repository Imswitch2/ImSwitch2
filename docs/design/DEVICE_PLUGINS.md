# ImSwitch2 Device Plugin Architecture

Status: proposal and implementation plan
Date: 2026-06-20

This document describes how ImSwitch2 should support external device plugins so
the main repository can stay stable while device support evolves in separate
packages. The goal is a plugin model similar in spirit to napari: plugins are
ordinary Python packages, they advertise their contributions through package
metadata and a manifest, and ImSwitch imports plugin code only when a configured
device actually needs it.

This is a design document for the core implementation. User-facing recipes
should later live under `docs/how-to/`.

## Motivation

ImSwitch2 currently supports many devices in-tree. This is useful for users, but
it has a maintenance cost:

- New devices require changes to the main repository.
- Optional vendor SDKs and platform-specific imports make core maintenance and
  CI harder.
- Device-specific development can destabilize the main branch.
- External contributors and vendors need a clear place to maintain their own
  device support.

The desired future state is:

- The ImSwitch2 core repository contains stable contracts, orchestration,
  safety-critical infrastructure, mock/reference devices, and documentation.
- Device families can live in separate repositories under the ImSwitch2
  organization or in third-party/vendor repositories.
- A plugin package can bundle one or more device managers, setup templates,
  validation schemas, documentation, tests, and optional mock managers.
- Existing setup files continue to work.

## Licensing Policy

ImSwitch2 remains GPLv3-or-later. The plugin system does not introduce a license
exception for proprietary in-process plugins.

Policy:

- In-process plugins that import ImSwitch manager base classes and run inside
  the ImSwitch process are expected to use GPL-compatible licensing.
- Vendor SDKs may still be optional runtime dependencies when their own license
  permits use by ImSwitch users.
- If a vendor cannot release GPL-compatible plugin code, the long-term technical
  path is an out-of-process driver/service boundary that communicates with a
  GPL-compatible ImSwitch plugin over a documented protocol. That bridge can be
  considered later, but it is not part of the first implementation.

Reference: the GNU GPL FAQ discusses static/dynamic linking and plugin cases at
https://www.gnu.org/licenses/gpl-faq.html.

## Current Architecture Constraint

Today, configured devices are loaded by `MultiManager`:

```python
package = importlib.import_module(
    pythontools.joinModulePath(
        f'{currentPackage}.{subManagersPackage}',
        managedDeviceInfo.managerName,
    )
)
manager = getattr(package, managedDeviceInfo.managerName)
```

This means a detector with `"managerName": "HamamatsuManager"` is resolved from:

```text
imswitch.imcontrol.model.managers.detectors.HamamatsuManager
```

The manager class name, file name, internal package location, and setup JSON
value are coupled. A plugin system should break that coupling while preserving
the legacy path.

Related existing pieces:

- `DetectorManager`, `LaserManager`, `PositionerManager`, and `RotatorManager`
  are already useful contracts for external implementations.
- Low-level managers are already injected through `**lowLevelManagers`, for
  example `nidaqManager`, `rs232sManager`, and `pulseGeneratorManager`.
- ImProcess already has a registry-style plugin concept, but its current
  registration is hard-coded. Device plugins should go directly to installed
  package discovery.
- Top-level ImSwitch modules such as `imcontrol`, `improcess`, and
  `imscripting` already have a module discovery concept. Device plugins are a
  separate layer below `imcontrol`; they should not replace the module system.

## Design Overview

Add a device plugin registry that resolves a setup file's `managerName` to a
manager class.

Resolution order:

1. Discover installed plugin manifests through Python package entry points.
2. Register built-in device managers as registry contributions.
3. When `MultiManager` creates a device, ask the registry to resolve
   `managerName`.
4. If the registry has a match, import the contribution's `python_name` and
   instantiate that class.
5. If the registry has no match, use the current legacy internal import path.

This keeps old setup files working and lets new setup files use stable plugin
IDs such as:

```json
"managerName": "acme.supercam"
```

The first implementation should continue using `managerName` rather than adding
a new setup field. A later schema cleanup can introduce `managerId` if desired,
but overloading `managerName` is the smallest compatible step.

## Public Plugin API

Create a stable public package:

```text
imswitch/pluginapi/
  __init__.py
  devices.py
```

The public API should re-export only the supported surface for plugin authors:

```python
from imswitch.pluginapi import (
    DeviceInfo,
    DetectorInfo,
    LaserInfo,
    PositionerInfo,
    RS232Info,
    DetectorManager,
    DetectorAction,
    DetectorNumberParameter,
    DetectorListParameter,
    LaserManager,
    PositionerManager,
    RotatorManager,
)
```

Do not ask plugin authors to import from deep internal paths such as
`imswitch.imcontrol.model.managers.detectors.DetectorManager`. The deep paths can
continue to exist, but `imswitch.pluginapi` becomes the compatibility promise.

Initial API contents:

- Setup dataclasses:
  - `DeviceInfo`
  - `DetectorInfo`
  - `LaserInfo`
  - `PositionerInfo`
  - `RS232Info`
- Manager base classes:
  - `DetectorManager`
  - `LaserManager`
  - `PositionerManager`
  - `RotatorManager`
- Helper classes:
  - detector parameter/action classes
  - any small public helpers needed by current managers

Avoid exposing:

- controller classes
- GUI widget internals
- private utilities
- concrete in-tree manager classes

## Plugin Discovery

Use Python entry points. The entry point group should be:

```text
imswitch.manifest
```

Example plugin `pyproject.toml`:

```toml
[project]
name = "imswitch-acme-devices"
version = "0.1.0"
description = "Acme device support for ImSwitch2"
requires-python = ">=3.10"
dependencies = [
    "ImSwitch>=2.1",
]
classifiers = [
    "License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)",
]
keywords = ["imswitch", "imswitch-plugin", "microscopy"]

[project.entry-points."imswitch.manifest"]
imswitch-acme-devices = "imswitch_acme_devices:imswitch.json"

[tool.setuptools.package-data]
imswitch_acme_devices = [
    "imswitch.json",
    "schemas/*.schema.json",
    "setup_templates/*.json",
]
```

Do not add a `Framework :: ImSwitch` trove classifier. PyPI validates
classifiers against an approved allowlist and rejects unregistered `Framework ::`
values, so `twine upload` would fail. Plugin discoverability is handled by the
`imswitch.manifest` entry point and the `imswitch-plugin` keyword instead.

The entry point value uses the form:

```text
python_package:relative_resource_path
```

This mirrors napari's manifest pattern while keeping the ImSwitch schema
independent. Note that this value is a `package:resource_path` pair, **not** a
`module:attr` import target. Discovery must parse it manually and must never call
`entry_point.load()` — `load()` would try to import an attribute named after the
file (e.g. `imswitch_acme_devices.imswitch.json` as an attribute) and fail.

Implementation detail:

- Use `importlib.metadata.entry_points(group="imswitch.manifest")`.
- Split the raw `entry_point.value` on `:` into `(package, resource_path)`.
- Resolve the manifest with
  `importlib.resources.files(package) / resource_path`.
- Manifests are JSON, parsed with the standard-library `json` module, so the
  core gains no new dependency. The conventional file name is `imswitch.json`.
- Resource paths referenced inside the manifest (`manager_properties_schema`,
  `setup_templates`) are resolved relative to the manifest file's directory:
  `importlib.resources.files(package) / <manifest-dir> / <path>`.
- Discovery should collect errors per plugin and report them without preventing
  unrelated plugins from loading.

## Manifest Schema

Recommended manifest shape (`imswitch.json`):

```json
{
  "name": "imswitch-acme-devices",
  "display_name": "Acme Device Support",
  "schema_version": "0.1",
  "imswitch_min_version": "2.1",
  "license": "GPL-3.0-or-later",
  "contributions": {
    "device_managers": [
      {
        "id": "acme.supercam",
        "kind": "detector",
        "display_name": "Acme SuperCam",
        "python_name": "imswitch_acme_devices.detectors:SuperCamManager",
        "mock_python_name": "imswitch_acme_devices.detectors:MockSuperCamManager",
        "manager_name_aliases": ["AcmeSuperCamManager"],
        "manager_properties_schema": "schemas/supercam.schema.json",
        "setup_templates": ["setup_templates/supercam.json"],
        "docs_url": "https://github.com/imswitch2/imswitch-acme-devices",
        "supported_platforms": ["linux", "win32"]
      }
    ]
  }
}
```

`device_managers` fields:

| Field | Required | Purpose |
|---|---:|---|
| `id` | yes | Stable setup-facing ID, e.g. `acme.supercam` |
| `kind` | yes | One of `detector`, `laser`, `positioner`, `rotator`, `rs232`, `flip_mirror`, `stand`, `slm`, `pulse_generator` |
| `display_name` | yes | Human-readable name for plugin listing and config tools |
| `python_name` | yes | Import path in `module:object` form |
| `mock_python_name` | no | Optional mock class for tests and examples |
| `manager_name_aliases` | no | Compatibility names accepted in setup JSON |
| `manager_properties_schema` | no | JSON Schema file for `managerProperties` |
| `setup_templates` | no | Example setup JSON snippets or full setup files |
| `docs_url` | no | Plugin documentation |
| `supported_platforms` | no | Informational platform list |

Canonical plugin IDs should use lower-case namespaced IDs:

```text
<vendor-or-project>.<device-or-family>
```

Examples:

```text
uc2.esp32-stage
thorlabs.kinesis-stage
hamamatsu.orca
swabian.time-tagger
acme.supercam
```

Built-in legacy managers can keep their existing class names as aliases:

```text
AVManager
HamamatsuManager
NidaqLaserManager
MockPositionerManager
```

## Registry Implementation

Add a registry package:

```text
imswitch/imcontrol/model/plugins/
  __init__.py
  discovery.py
  manifest.py
  registry.py
```

Core dataclass:

```python
from dataclasses import dataclass, field
from typing import Literal

# NOTE: `stand` and `pulse_generator` are accepted in manifests but are NOT
# loaded through MultiManager in the first implementation (bespoke loaders — see
# "MultiManager-backed vs bespoke kinds"). The other seven are registry-resolved.
DeviceKind = Literal[
    "detector",
    "laser",
    "positioner",
    "rotator",
    "rs232",
    "flip_mirror",
    "stand",
    "slm",
    "pulse_generator",
]

@dataclass(frozen=True)
class DeviceManagerContribution:
    id: str
    kind: DeviceKind
    display_name: str
    python_name: str
    plugin_name: str
    plugin_version: str | None = None
    mock_python_name: str | None = None
    manager_name_aliases: tuple[str, ...] = ()
    manager_properties_schema: str | None = None
    setup_templates: tuple[str, ...] = ()
    docs_url: str | None = None
    supported_platforms: tuple[str, ...] = ()
```

Registry behavior:

```python
class DevicePluginRegistry:
    def register(self, contribution: DeviceManagerContribution) -> None:
        ...

    def resolve(self, kind: str, manager_name: str) -> DeviceManagerContribution | None:
        ...

    def load_manager_class(
        self, kind: str, manager_name: str, *, prefer_mock: bool = False
    ) -> type | None:
        # Returns the mock class only when prefer_mock is set and the
        # contribution declares a mock_python_name (see "Mock Selection").
        ...

    def list_contributions(self, kind: str | None = None) -> list[DeviceManagerContribution]:
        ...

    def format_resolution_error(self, kind: str, manager_name: str) -> str:
        # Builds the actionable "Installed <kind> managers: ..." diagnostic used
        # when both the registry and the legacy fallback miss.
        ...
```

Load helper:

```python
def load_python_object(python_name: str):
    module_name, object_name = python_name.split(":", 1)
    module = importlib.import_module(module_name)
    return getattr(module, object_name)
```

Collision policy:

- A duplicate `id` for the same `kind` is an error.
- Built-in IDs should not be overridden by installed plugins.
- Aliases can collide only if they resolve to the same contribution.
- Error messages must include the plugin distribution name and manifest path.

Discovery policy:

- Plugin discovery should happen once at application startup.
- Discovery must not import device implementation modules. It should only read
  package metadata and manifests.
- Implementation modules are imported only when a setup actually selects that
  device manager.

## Built-In Manager Registration

Existing in-tree managers should be registered as built-in contributions.

Initial practical implementation:

- Use an explicit table, not directory scanning.
- Keep the table near the registry, for example
  `imswitch/imcontrol/model/plugins/builtins.py`.
- Register legacy IDs and aliases so existing configs do not change.

Example:

```python
BUILTIN_DEVICE_MANAGERS = [
    DeviceManagerContribution(
        id="AVManager",
        kind="detector",
        display_name="Generic video detector",
        python_name=(
            "imswitch.imcontrol.model.managers.detectors.AVManager:"
            "AVManager"
        ),
        plugin_name="imswitch-core",
        manager_name_aliases=("builtin.av",),
    ),
    DeviceManagerContribution(
        id="NidaqLaserManager",
        kind="laser",
        display_name="NI-DAQ laser",
        python_name=(
            "imswitch.imcontrol.model.managers.lasers.NidaqLaserManager:"
            "NidaqLaserManager"
        ),
        plugin_name="imswitch-core",
        manager_name_aliases=("builtin.nidaq-laser",),
    ),
]
```

Later, built-in managers can be assigned cleaner namespaced IDs while preserving
the class names as aliases.

## MultiManager Integration

Modify `MultiManager` so manager lookup goes through the registry first.

Target behavior:

```python
manager_cls = device_plugin_registry.load_manager_class(
    kind=device_kind,
    manager_name=managedDeviceInfo.managerName,
)

if manager_cls is None:
    try:
        manager_cls = load_legacy_internal_manager(
            subManagersPackage=subManagersPackage,
            managerName=managedDeviceInfo.managerName,
        )
    except (ImportError, AttributeError) as exc:
        # Neither the registry nor the legacy internal import path resolved the
        # name. Raise the actionable diagnostic (installed managers for this
        # kind + how to fix) instead of the raw import traceback.
        raise UnknownDeviceManagerError(
            device_plugin_registry.format_resolution_error(
                kind=device_kind,
                manager_name=managedDeviceInfo.managerName,
            )
        ) from exc

self._subManagers[managedDeviceName] = manager_cls(
    managedDeviceInfo,
    managedDeviceName,
    **lowLevelManagers,
)
```

The improved error message below is only useful if it is raised when *both* the
registry and the legacy fallback fail. The legacy path raises `ImportError` /
`AttributeError` for an unknown name, so that failure must be caught and
re-raised through the registry's `format_resolution_error(kind, manager_name)`
helper. Letting the raw legacy traceback escape would defeat the diagnostic.

Map existing `subManagersPackage` values to manifest `kind`:

| `subManagersPackage` | `kind` |
|---|---|
| `detectors` | `detector` |
| `lasers` | `laser` |
| `positioners` | `positioner` |
| `rotators` | `rotator` |
| `rs232` | `rs232` |
| `flipMirrors` | `flip_mirror` |
| `slms` | `slm` |

### MultiManager-backed vs bespoke kinds

Only these kinds are loaded through `MultiManager`, so only these get plugin
resolution in the first implementation:

```text
detector, laser, positioner, rotator, rs232, flip_mirror, slm
```

`stand` and `pulse_generator` are **not** loaded by `MultiManager` and must not
be advertised as plugin-resolvable in the first milestone:

- `StandManager` has its own import path with an unconditional mock fallback. It
  should be migrated after `MultiManager` support lands, because it is a one-off
  loader and should not block the first device plugin milestone.
- The pulse generator (`TeensyPulseManager`) is constructed directly in
  `MasterController` and injected as a low-level manager
  (`pulseGeneratorManager`) — as are `nidaqManager` and `triggerScopeManager`.
  None of these flow through the `MultiManager` device loop, so the kind-mapping
  table above has no row for them.

These two kinds stay in the manifest `kind` enum so plugin authors can write
manifests ahead of time, but the registry will not wire them until their bespoke
loaders are migrated to registry-first resolution in a later phase.

Error messages should improve:

```text
Could not resolve detector manager 'acme.supercam'.
No installed ImSwitch device plugin provides this manager.

Installed detector managers:
  - AVManager (imswitch-core)
  - HamamatsuManager (imswitch-core)
  - acme.demo-camera (imswitch-acme-devices)

Install the required plugin package or correct managerName in the setup file.
```

## Mock Selection

A contribution may declare `mock_python_name`. The registry never silently
substitutes the mock — selection is explicit and driven by setup data, so the
registry stays aligned with the safety rule against silent mock fallback:

- A global mock/dev signal (if one is threaded through to `MultiManager`)
  selects `mock_python_name` for every contribution that provides one.
- A per-device `managerProperties.useMockOnFailure: true` permits a single,
  logged fallback to `mock_python_name` if the real class raises during
  construction. This is opt-in per device.
- With neither set, a construction failure propagates. The registry does not
  guess.

Concretely, `load_manager_class` takes an explicit flag, for example:

```python
def load_manager_class(
    self, kind, manager_name, *, prefer_mock=False
) -> type | None:
    ...
```

It returns the mock class only when `prefer_mock` is set and the contribution
declares a `mock_python_name`. The current `StandManager` loader — which falls
back to a mock on *any* exception, unconditionally — is exactly the behavior to
retire when stand support migrates; it must not be the template for
registry-driven mock handling.

## Setup JSON Compatibility

Do not require setup migrations for the first version.

Supported values:

```json
"managerName": "AVManager"
```

and:

```json
"managerName": "acme.supercam"
```

This lets plugin IDs coexist with legacy class names.

A future setup schema can add:

```json
"managerId": "acme.supercam"
```

but that should be treated as a cleanup, not a prerequisite.

## Manager Properties Validation

Each contribution may ship a JSON Schema for `managerProperties`.

Example:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "Acme SuperCam managerProperties",
  "type": "object",
  "additionalProperties": false,
  "required": ["serialNumber"],
  "properties": {
    "serialNumber": {
      "type": "string"
    },
    "exposureMs": {
      "type": "number",
      "minimum": 0.1,
      "default": 10.0
    },
    "useMockOnFailure": {
      "type": "boolean",
      "default": false
    }
  }
}
```

Validation should be added in two steps:

1. Add an explicit setup validation command that checks manifests and schemas.
2. Add startup warnings for invalid schemas or invalid manager properties.

Do not make all startup validation fatal immediately. Some existing setups may
depend on loose manager property dictionaries. Fatal validation can be adopted
later for managers that explicitly opt in.

Suggested command (the CLI lives in the registry package,
`imswitch/imcontrol/model/plugins/__main__.py`):

```bash
python -m imswitch.imcontrol.model.plugins validate-setup path/to/setup.json
```

Suggested diagnostics:

```bash
python -m imswitch.imcontrol.model.plugins list
python -m imswitch.imcontrol.model.plugins list --kind detector
python -m imswitch.imcontrol.model.plugins inspect acme.supercam
```

## Plugin Package Layout

Recommended template repository:

```text
imswitch-plugin-template/
  pyproject.toml
  README.md
  LICENSE
  src/
    imswitch_plugin_template/
      __init__.py
      imswitch.json
      detectors/
        __init__.py
        demo_detector.py
      lasers/
        __init__.py
        demo_laser.py
      schemas/
        demo_detector.schema.json
      setup_templates/
        demo_detector_setup.json
  tests/
    test_discovery.py
    test_demo_detector.py
    test_setup_templates.py
  .github/
    workflows/
      test.yml
```

A minimal detector implementation:

```python
import numpy as np

from imswitch.pluginapi import DetectorManager, DetectorNumberParameter


class DemoDetectorManager(DetectorManager):
    def __init__(self, detectorInfo, name, **lowLevelManagers):
        self._shape = (512, 512)
        parameters = {
            "Exposure": DetectorNumberParameter(
                group="Acquisition",
                value=10.0,
                valueUnits="ms",
                editable=True,
            )
        }
        super().__init__(
            detectorInfo,
            name,
            fullShape=self._shape,
            supportedBinnings=[1],
            model="Demo detector",
            parameters=parameters,
        )

    def getLatestFrame(self, is_save=False):
        return np.zeros(self._shape, dtype=np.uint16)

    def startAcquisition(self):
        return None

    def stopAcquisition(self):
        return None

    def finalize(self):
        pass
```

The actual constructor and abstract methods must match the current base class
contracts. The template should include tests that instantiate the mock/demo
manager without hardware.

## Plugin Template README Requirements

Every generated plugin should explain:

- What devices it supports.
- Which ImSwitch versions it supports.
- Which operating systems it has been tested on.
- Which vendor SDKs or native drivers are required.
- How to install it:

```bash
python -m pip install imswitch-acme-devices
```

or for development:

```bash
git clone https://github.com/imswitch2/imswitch-acme-devices
cd imswitch-acme-devices
python -m pip install -e ".[test]"
pytest
```

- How to use its setup template.
- Whether it has a mock mode.
- The license.

## Testing Strategy

Core tests:

- Registry can register built-in contributions.
- Registry can resolve a built-in manager by legacy class name.
- Registry can resolve a plugin manager by namespaced ID.
- Registry rejects duplicate IDs.
- Discovery handles a broken plugin manifest without breaking unrelated plugins.
- `MultiManager` can instantiate a fake plugin manager from a test contribution.
- Legacy internal import fallback still works.
- Error messages include actionable plugin diagnostics.

Plugin template tests:

- Package installs in editable mode.
- Entry point is discoverable.
- Manifest validates.
- Mock/demo manager can instantiate without hardware.
- Setup template references a valid contribution ID.
- Optional manager properties schema accepts the setup template.

CI:

- Core CI should run without hardware and without third-party plugins.
- Plugin template CI should run against the latest released ImSwitch and,
  optionally, against ImSwitch main.
- Hardware tests remain opt-in and should use the existing `hardware` marker
  pattern.

## Migration Plan

### Phase 0 - Design Record

Tasks:

- Add this design document.
- Decide the entry point group: `imswitch.manifest`.
- Decide the first manifest schema version: `0.1`.
- Decide the initial plugin template name: `imswitch-plugin-template`.
- Confirm GPL-compatible plugin policy.

Acceptance criteria:

- Core maintainers agree on the registry shape and compatibility policy.

### Phase 1 - Public Plugin API

Tasks:

- Add `imswitch/pluginapi/__init__.py`.
- Re-export stable device manager contracts and setup dataclasses.
- Add tests that import the public API.
- Document that plugin authors should not import deep internal paths.

Acceptance criteria:

- A plugin can implement a manager using only `imswitch.pluginapi` imports.

### Phase 2 - Manifest Discovery

Tasks:

- Add manifest parsing and entry-point discovery.
- Add `DeviceManagerContribution`.
- Add discovery error collection and logging.
- Add unit tests with monkeypatched entry points or test distributions.

Acceptance criteria:

- ImSwitch can list installed plugin contributions without importing manager
  implementation modules.

### Phase 3 - Registry and Built-Ins

Tasks:

- Add `DevicePluginRegistry`.
- Add explicit built-in contribution table.
- Register built-ins during startup.
- Add collision detection.

Acceptance criteria:

- All existing in-tree managers can be resolved through the registry by their
  existing `managerName`.

### Phase 4 - MultiManager Integration

Tasks:

- Replace direct import in `MultiManager` with registry-first resolution.
- Keep legacy import fallback.
- Improve missing-manager error messages.
- Add tests with a fake plugin manager.

Acceptance criteria:

- Existing setup files still boot.
- A test plugin manager can be selected through setup JSON without adding a file
  under `imswitch/imcontrol/model/managers`.

### Phase 5 - Diagnostics and Validation

Tasks:

- Add plugin list/inspect command.
- Add setup validation command.
- Add optional JSON Schema validation for `managerProperties`.
- Include plugin package name/version in logs.

Acceptance criteria:

- A user can diagnose "which plugin provides this manager?" from the command
  line.

### Phase 6 - Template Repository

Tasks:

- Create `imswitch-plugin-template` in the ImSwitch2 organization.
- Include a demo detector and demo laser.
- Include manifest, schema, setup template, README, tests, and CI.
- Mark it as a GitHub template repository.

Acceptance criteria:

- A contributor can create a new plugin repo, install it editable, and see its
  manager in `imswitch plugins list`.

### Phase 7 - First Real Plugin

Choose one low-risk device family and move or duplicate it into a plugin package.

Good candidates:

- A mock/demo plugin first.
- A vendor family with clear optional dependencies and existing mock fallback.
- A non-safety-critical manager before laser/DAQ-heavy paths.

Acceptance criteria:

- The plugin works outside the core repository.
- Its setup template validates.
- The main branch does not need a new concrete manager class for that device.

### Phase 8 - Gradual Device Extraction

Do not move everything at once.

Recommended policy:

- New device support should default to external plugins once Phase 4 is merged.
- Existing in-tree devices stay until there is a stable plugin package and a
  migration path.
- Moved managers should leave a compatibility alias or clear install error for
  at least two minor releases.
- Safety-critical devices require extra review before extraction.

Possible packages:

```text
imswitch-device-thorlabs
imswitch-device-hamamatsu
imswitch-device-smaract
imswitch-device-swabian
imswitch-device-uc2
imswitch-device-cobolt
```

The package names can be adjusted, but the manager IDs should stay stable once
released.

## Safety Considerations

Device plugins run arbitrary Python code in the ImSwitch process. The plugin
system is not a sandbox.

Core safety expectations:

- Plugin managers must follow the same constructor and cleanup contracts as
  in-tree managers.
- Hardware imports should be lazy and should fail with actionable messages.
- Laser, DAQ, scan, and stage plugins should provide safe `finalize()` behavior.
- Plugins should not start emission, motion, or acquisition during import.
- Plugins should avoid side effects during manifest discovery.
- Plugins should prefer explicit `useMockOnFailure` settings rather than silent
  mock fallback for safety-critical devices.

## Compatibility Policy

Plugin compatibility should be versioned around these layers:

- Manifest schema version, e.g. `schema_version: "0.1"`.
- Minimum ImSwitch version, e.g. `imswitch_min_version: "2.1"`.
- Public plugin API compatibility through `imswitch.pluginapi`.
- Manager base class behavior.

Rules:

- Adding new optional manifest fields is non-breaking.
- Removing or renaming manifest fields requires a schema version bump.
- Removing public plugin API imports requires a deprecation period.
- Built-in legacy `managerName` values should remain valid until a documented
  removal release.

## Documentation Work

After the core implementation lands, add:

- `docs/how-to/write-device-plugin.rst`
- `docs/how-to/port-device-to-plugin.rst`
- `docs/devices/plugins.rst`
- Plugin author checklist in `CONTRIBUTING.md`
- Template README and examples

The existing `docs/adding-device-support.rst` should be updated from "place a
manager class inside the ImSwitch tree" to:

1. Prefer a plugin package for new device support.
2. Use in-tree managers only for core/reference devices or tightly coupled
   infrastructure.

## First PR Scope

The first code PR should be intentionally small:

- Add `imswitch.pluginapi`.
- Add `DeviceManagerContribution`.
- Add registry with built-in registrations for a small subset of managers.
- Add tests for registry resolution.
- Do not change `MultiManager` yet.

The second PR should integrate `MultiManager`.

This split makes it easier to review the public API and registry behavior before
touching hardware instantiation.

The built-in table is an explicit registration, so it does not depend on
manifest discovery. Manifest/entry-point discovery (Phase 2) is independent and
may land in the same first PR or a follow-up; either way it must not modify
`MultiManager`. The subset of built-ins registered in the first PR is just a
starting point — anything not in the table still resolves through the legacy
import fallback once `MultiManager` integration lands, so coverage can grow
incrementally without breaking existing setups.

## Resolved Decisions

- **Manifest format:** JSON (`imswitch.json`), parsed with the standard-library
  `json` module. The core adds no new dependency; YAML/`PyYAML` is explicitly
  rejected for the first implementation.
- **Discovery timing:** at `imcontrol` startup, not `imswitch` process startup.
  Device plugins are an `imcontrol`-layer concern.
- **`managerProperties` validation:** warnings-only by default; a manager may
  opt into strict startup validation. Not fatal globally.
- **First extraction pilot:** a mock/demo plugin first, then a vendor family that
  already has a mock fallback and clean optional dependencies. Explicitly not a
  laser/DAQ-heavy path.

## Open Questions

- Should a future GUI config editor be able to browse installed plugin setup
  templates? (Non-blocking; informational manifest fields already support it.)

## Recommended Initial Decision

Use this path:

1. Keep GPLv3-or-later with no proprietary in-process plugin exception.
2. Use entry point group `imswitch.manifest`.
3. Use JSON manifests parsed with the standard library (no new core dependency).
4. Keep setup JSON field `managerName` for the first release.
5. Allow namespaced manager IDs like `acme.supercam`.
6. Register built-ins explicitly and preserve all legacy class-name IDs.
7. Create `imswitch-plugin-template` as a GitHub template repo.
8. Migrate devices gradually only after the registry is proven.
