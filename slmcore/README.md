# slmcore

`slmcore` is a standalone Python package for spatial light modulator control, pattern generation, CGH, calibration, feedback, and reusable Qt interfaces.

It has been developed specifically alongside and in close integration with **ImSwitch**, which remains its primary host application and development use case. However, `slmcore` is intentionally designed as a fully standalone package: it can be installed and used independently, embedded in another application, or integrated into ImSwitch through its public API.

Host applications should use the public `slmcore` interfaces rather than maintaining host-specific copies of the core implementation.

## Getting started

### Standalone development

Create the standalone Conda environment:

```bash
conda env create -f environment-standalone.yml
conda activate slmcore
```

Install the local package in editable mode:

```bash
python -m pip install -e . --no-deps
```

The environment file provides the third-party dependencies, while the editable install makes the local `src/slmcore` package importable.

Run the tests:

```bash
pytest
```

Run the Qt demo:

```bash
python -m examples.qt_demo
```

A basic standalone application can construct a setup, workspace and Qt session directly:

```python
from slmcore import (
    SLMGeometry,
    SLMIdentity,
    SLMSectionsSetup,
    SLMSetup,
    SLMWorkspace,
)

from slmcore.qt import SLMQtSessionFactory

setup = SLMSetup(
    identity=SLMIdentity(
        key="slm",
        serial_number="example-serial",
    ),
    geometry=SLMGeometry(
        width=1920,
        height=1080,
        pixel_size_um=8.0,
    ),
    sections=SLMSectionsSetup(),
)

workspace = SLMWorkspace("slm_data")

factory = SLMQtSessionFactory(workspace=workspace)

session, panel = factory.create(
    setup=setup,
)
```

### Using slmcore inside ImSwitch

The ImSwitch repository keeps the complete `slmcore` project as a top-level package:

```text
ImSwitch/
├── imswitch/
├── slmcore/
│   ├── pyproject.toml
│   ├── environment-standalone.yml
│   ├── src/
│   │   └── slmcore/
│   ├── tests/
│   └── examples/
└── ...
```

Inside an ImSwitch development environment, install the local package with:

```bash
python -m pip install -e ./slmcore --no-deps
```

ImSwitch already owns and resolves the integrated application environment, so `--no-deps` prevents the nested package installation from modifying it.

ImSwitch then imports `slmcore` normally:

```python
from slmcore import SLMSetup, SLMWorkspace
from slmcore.qt import SLMQtSessionFactory
```

The editable installation points those imports directly to:

```text
ImSwitch/slmcore/src/slmcore/
```

Changes made to the local `slmcore` sources are therefore immediately available to ImSwitch without reinstalling or publishing a new package version.

On the ImSwitch side, only the host-specific adapter remains responsible for concerns such as:

* translating ImSwitch setup information into `SLMSetup`;
* providing the application workspace root;
* connecting SLM hardware callbacks;
* providing detector/measurement callbacks;
* persisting host-owned startup preferences.

Runtime control, configuration handling, CGH, calibration, feedback, and reusable Qt session logic remain in `slmcore`.

# slmcore architecture

`slmcore` is organized around a central state/runtime engine plus reusable SLM capabilities and integration layers.

## Central engine

The architectural backbone lives in `engine/`:

```text
ParamSpec / param_field
        ↓
     StateModel
        ↓
GroupStateModel / ItemState / ParameterSetState
        ↓
   SLMSectionState
        ↑
     Registries
        ↓
 SLMSectionRuntime
        ↓
    SLMRuntime
```

The main engine packages are:

* `engine/parameters/`: parameter specifications, links, units, and converters.
* `engine/state/`: generic state models, groups, items, topology, and loading paths.
* `engine/section/`: one SLM section's state, geometry, snapshots, artifacts, and runtime.
* `engine/registry.py`: registration models/decorators and the explicit standard-registration composition point.
* `engine/transition.py`: immutable descriptions of committed state changes.
* `engine/runtime.py`: aggregate SLM runtime and composed artifacts.
* `engine/device.py`: physical SLM identity and geometry.

Registration declarations stay beside the implementations they describe. The standard registrations are assembled by `engine.registry.load_default_registrations()` from `patterns/`, `cgh/targets/`, and `cgh/computations/`.

## Capabilities

* `patterns/`: analytic patterns and aberrations.
* `cgh/`: targets, CGH algorithms, localization, metrics, and feedback.
* `calibration/`: section/plane calibration models and storage.
* `measurement/`: host-neutral image measurement records.
* `corrections/`: device correction-pattern and 2π lookup/storage.

## Integration layers

* `setup/`: canonical installed-SLM setup and startup-preference models plus JSON helpers.
* `workspace/`: standard config/calibration/correction resource layout rooted at one application data directory.
* `config/`: configuration models, serialization, migration, and repositories.
* `application/`: runtime construction from a canonical `SLMSetup`.
* `host/`: optional host capability contracts and callbacks.
* `qt/`: reusable Qt panels, sessions, views, and interaction controllers.

The root `slmcore` package remains the convenient public facade for commonly used types. Internal architectural modules use the `slmcore.engine.*` paths.

## Canonical setup and workspace

`SLMSetup` is the public installed-SLM contract. It stores logical/physical identity, human-readable display name, geometry, declared section layout and optional hardware information.

Section geometries are derived by `slmcore` rather than by the embedding host.

`SLMIdentity.serial_number` is mandatory and is the stable physical namespace for persistent workspace resources. `key` remains the logical runtime identifier. `display_name` is presentation-only and does not participate in identity matching.

`SLMStartupPreferences` stores persistent defaults applied when constructing a session:

* startup config;
* default active plane per section;
* section display mode.

A canonical standalone JSON file contains both `setup` and `startup_preferences`.

Hosts with a larger setup format may embed the same two objects and use `to_dict()` / `from_dict()` directly.

`SLMWorkspace(root)` is the standard runtime-resource layout:

```text
<root>/
├── configs/<serial>/
├── corrections/<serial>/
└── calibrations/
```

Normally the host supplies only `root`; `slmcore` owns the rest.

Advanced hosts may override `configs_dir`, `corrections_dir` and/or `calibrations_dir` when constructing the workspace.

Correction lookup uses the standard per-serial directory and `wavelength.json`. Missing correction data simply follows the normal correction-store fallback behavior.

The normal Qt composition is therefore:

```python
workspace = SLMWorkspace(data_dir)

factory = SLMQtSessionFactory(workspace=workspace)

session, panel = factory.create(
    setup=setup,
    startup_preferences=startup_preferences,
    setup_file=setup_file,  # standard standalone JSON persistence
    host_services=services,
)
```

If the host owns a larger setup file, it omits `setup_file` and supplies `on_startup_preferences_changed`.

In that case, `slmcore` owns startup-preference semantics while the host owns how its setup file is rewritten.
