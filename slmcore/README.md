# slmcore

`slmcore` is a standalone Python package for spatial light modulator (SLM) control, pattern generation, computer-generated holography (CGH), calibration, feedback, and reusable Qt interfaces.

It is developed jointly with **ImSwitch**, which is its primary host application and integration target. At the same time, `slmcore` is deliberately maintained as a standalone package: it can be installed and used independently, embedded in another application, or integrated into ImSwitch through its public API.

Host applications should use the public `slmcore` interfaces rather than maintaining host-specific copies of the core implementation.

## Getting started

### Standalone use

Once released on PyPI, the package can be installed with:

```bash
python -m pip install slmcore
```

For development from a source checkout, create the standalone Conda environment:

```bash
conda env create -f environment-standalone.yml
conda activate slmcore
```

Then install the local package in editable mode:

```bash
python -m pip install -e . --no-deps
```

The Conda environment provides the third-party dependencies, while the editable install exposes the local `src/slmcore` package.

Run the test suite with:

```bash
python -m pytest
```

Run the hardware-free Qt demo with:

```bash
python -m examples.qt_demo
```

A minimal standalone Qt composition looks like:

```python
from qtpy import QtWidgets

from slmcore import (
    SectionSplitLayout,
    SLMGeometry,
    SLMIdentity,
    SLMSectionsDefinition,
    SLMDefinition,
    SLMWorkspace,
)
from slmcore.host import MockSLMDeviceProvider, SLMHostServices
from slmcore.qt import SLMQtSessionFactory

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

definition = SLMDefinition(
    identity=SLMIdentity(
        key="slm",
        serial_number="example-serial",
        display_name="Example SLM",
    ),
    geometry=SLMGeometry(
        width=1920,
        height=1080,
        pixel_size_um=8.0,
    ),
    sections=SLMSectionsDefinition(
        layout=SectionSplitLayout(n_sections=1),
    ),
)

workspace = SLMWorkspace("slm_data")
factory = SLMQtSessionFactory(workspace=workspace)

session, panel = factory.create(
    definition=definition,
    host_services=SLMHostServices(
        device=MockSLMDeviceProvider(),
    ),
    # A standalone application can instead provide setup_file=... to let
    # slmcore persist startup preferences in its canonical setup JSON file.
    on_startup_preferences_changed=lambda preferences: None,
)

panel.show()
app.exec()
```

For a more complete standalone host, including canonical setup files and multiple SLMs, see `examples/qt_demo/`.

### Using slmcore inside ImSwitch

ImSwitch keeps the complete `slmcore` project as a top-level package:

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

ImSwitch owns and resolves the integrated application environment, so `--no-deps` prevents the nested editable installation from modifying that environment.

ImSwitch then imports `slmcore` normally:

```python
from slmcore import SLMDefinition, SLMWorkspace
from slmcore.qt import SLMQtSessionFactory
```

The editable installation points those imports directly to:

```text
ImSwitch/slmcore/src/slmcore/
```

Changes made to the local `slmcore` sources are therefore immediately available to ImSwitch without publishing or reinstalling a new package version.

On the ImSwitch side, only the host-specific adapter remains responsible for concerns such as:

- translating ImSwitch setup information into `SLMDefinition`;
- providing the application workspace root;
- connecting SLM hardware callbacks;
- providing detector/measurement callbacks;
- persisting host-owned startup preferences.

Runtime control, configuration handling, CGH, calibration, feedback, and reusable Qt session logic remain in `slmcore`.

## Architecture

`slmcore` is split into six top-level architectural areas:

```text
src/slmcore/
├── core/          # SLM state, models and scientific computation
├── application/   # toolkit-independent workflows and session orchestration
├── workspace/     # persisted runtime resources
├── setup/         # SLM definition/startup-file models and JSON I/O
├── host/          # capabilities supplied by an embedding host
└── qt/            # Qt presentation and adapters
```

### Core

`core/` contains everything needed to represent and compute SLM state without Qt,
filesystem ownership, or host-specific services:

- `core/engine/`: parameter/state machinery, section and aggregate runtimes,
  physical identity/geometry, transitions, registries, and the host-neutral
  correction-provider contract.
- `core/patterns/`: analytic patterns and aberrations.
- `core/cgh/`: targets, CGH algorithms, localization, metrics, propagation and
  feedback models.
- `core/calibration/`: calibration geometry/model and localization-calibration
  fitting.
- `core/config/`: the portable complete-runtime snapshot model and config-load
  report types. The first public config format is schema version 1.
- `core/measurement/`: host-neutral measurement records.

The core layer does not import `application`, `workspace`, `host`, or `qt`.
Registration declarations stay beside the implementations they describe; the
standard registrations are assembled explicitly by
`core.engine.registry.load_default_registrations()`.

### Application

`application/` owns use cases rather than presentation. `SLMSession` is the
toolkit-independent controller for one `SLMRuntime`: CGH execution/commit,
device/frame lifecycle, configuration/control mode, section-layout changes,
feedback, and calibration workflows. `SLMConfigurationService`,
`SLMCalibrationService`, `SLMFeedbackService`, and `SLMSectionLayoutService`
contain the corresponding application rules.

Configuration loading is deliberately `prepare -> decide -> commit`. Normal
editor loads preserve partial recovery (`require_complete=False`); strict startup
and Fast-Config-to-Editor reconstruction use `require_complete=True`.
Calibration and correction mismatches are represented in the prepared load so
interactive Qt code only gathers a decision while the application layer owns
the mutation.

### Workspace

`workspace/` owns persisted runtime resources:

```text
workspace/
├── workspace.py
├── config_store.py
├── _hdf5.py
├── calibration_store.py
└── correction_store.py
```

`SLMWorkspace` defines the standard directory layout and creates stores
namespaced by the physical `SLMIdentity.serial_number`. `SLMConfigStore` is the
single directory-bound config persistence API; there is no separate repository
layer. Calibration catalog/files and device correction resources are likewise
workspace concerns rather than core concerns.

### Correction reproducibility

The runtime depends only on the core `CorrectionProvider` contract. The
filesystem-backed `SLMCorrectionStore` is the standard workspace implementation.
Each frame computation resolves a complete immutable correction snapshot once
and stores that exact resolution with the section artifacts. Saving a config
copies that snapshot; it does not re-query the filesystem. The snapshot includes
the effective correction pattern and 2π value plus read-only provenance such as
source directory, selected filenames and selected wavelengths.

A saved correction snapshot is historical truth; the workspace provider is the
current-environment truth. On an editor load, numerical differences in enabled
corrections require an explicit `USE_SAVED`, `USE_CURRENT`, or cancel/reject
decision. Provenance-path differences alone do not constitute a mismatch. A
section loaded with `USE_SAVED` remains pinned while editing until wavelength or
section geometry changes, which requires an explicit switch to current workspace
corrections. Fast Config bypasses this decision because the persisted
`final_eightbit` frame is authoritative; leaving Fast Config strictly rebuilds
the selected config and resolves any mismatch then.

### Definition, host and Qt

`setup/` contains the portable SLM definition, optional hardware-binding model, startup preferences, and setup-file I/O.
`host/` defines optional external capabilities such as device and measurement
providers. `qt/` contains presentation, dialogs, views and Qt-specific adapters.
No Qt imports exist below the Qt package.

The normal composition is:

```text
core runtime/model
       ↑
 application session  ← workspace + host capabilities
       ↑
   SLMQtSession
       ↑
   Qt panel/views
```

`SLMQtSessionFactory` is the standard Qt composition root. It creates the
workspace stores, runtime factory, application services/session, and finally the
Qt adapter. `SLMQtSession` does not construct a second application/runtime
stack. Runtime commits remain authoritative if presentation synchronization
fails.

Feedback and calibration acquisition use the host-neutral `MeasurementDispatcher`
contract. Qt supplies `QtMeasurementDispatcher`; a headless host can provide a
different dispatcher without changing application semantics.

### Canonical setup and workspace layout

`SLMDefinition` stores the portable SLM description only: logical/physical
identity, human-readable display name, geometry and declared section layout.
Section geometries are derived by `slmcore`, not by the embedding host. Hardware
binding is deliberately separate in optional `SLMHardwareConfig`.

`SLMIdentity.serial_number` is mandatory and is the stable physical namespace
for persistent workspace resources. `key` is the logical runtime identifier;
`display_name` is presentation-only.

`SLMStartupPreferences` stores startup config, default active plane per section,
and section display mode. A standalone setup JSON contains required `definition`, optional `hardware`, and
`startup_preferences` siblings. Embedding hosts may persist the same concepts
inside their own setup format and may use a host-owned hardware mechanism instead
of `SLMHardwareConfig`.

`SLMWorkspace(root)` uses:

```text
<root>/
├── configs/<serial>/
├── corrections/<serial>/
└── calibrations/
```

Hosts normally provide only the root. Optional directory overrides remain
available for advanced integrations.

### Frame publication and interaction

Every accepted frame-changing transition follows the session publication path.
With `auto_upload_frame=True` the configured `SLMDeviceProvider` receives the
new frame automatically; hosts may disable automatic upload and publish
explicitly instead. `defer_frame_upload()` coalesces physical uploads while
keeping runtime/presentation state current.

`RuntimeViewInteractionSettings` is a Qt interaction policy, not part of
`SLMConfig`. Normal Qt-session mutations route through `SLMSession`; Qt gathers
user decisions and synchronizes presentation with already-committed application
state.

## License

`slmcore` is distributed under the GNU General Public License v3.0. See `LICENSE`.
