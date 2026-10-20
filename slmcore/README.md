# slmcore

`slmcore` is a standalone Python package for spatial light modulator (SLM) control, pattern generation, computer-generated holography (CGH), calibration, feedback, configuration, and reusable Qt interfaces.

It is developed jointly with **ImSwitch**, which is its primary host application and integration target. At the same time, `slmcore` is deliberately kept host-independent: it can be used from its own standalone Qt host or embedded into another application through its public API.

Host applications should use the public `slmcore` interfaces rather than maintaining host-specific copies of the core implementation.

## Getting started

`slmcore` requires Python 3.10 or newer.

### Installation from PyPI

Install the host-independent core package with:

```bash
python -m pip install slmcore
```

Install the reusable Qt interface when `slmcore` is used as its own Qt application or from a host that does not already provide the required Qt stack:

```bash
python -m pip install "slmcore[qt]"
```

Importing `slmcore` itself does not import Qt. Qt functionality is accessed explicitly through `slmcore.qt`.

### Optional GPU acceleration

The CGH backends can use CuPy for GPU acceleration when a compatible CUDA/CuPy installation is available. GPU support is optional; the package falls back to CPU implementations when CuPy is unavailable.

CuPy publishes different binary packages for different CUDA major versions, so `slmcore` deliberately does not declare a generic `gpu` extra. Install the CuPy package appropriate for the CUDA environment by following the CuPy installation instructions, then install `slmcore` normally. The runtime GPU availability probe will expose the GPU backend only when CuPy and CUDA can execute successfully.

### Core development

From a source checkout, create and activate a Python environment, then install:

```bash
python -m pip install -r requirements-dev.txt
```

`requirements-dev.txt` contains:

```text
-e .[test]
```

This installs `slmcore` in editable mode together with its test dependencies, but without the optional Qt stack. Changes under `src/slmcore/` are immediately available without reinstalling the package.

Run the test suite with:

```bash
python -m pytest
```

The non-Qt tests run normally. Qt-specific tests are skipped when the Qt dependencies are not installed.

### Qt development and standalone demo

To develop the reusable Qt interface and run the full test suite, install:

```bash
python -m pip install -e ".[qt,test]"
```

The standalone demo additionally uses QDarkStyle for its reference-host theme. It is intentionally kept out of the reusable `qt` dependency group. Install the demo dependencies with:

```bash
python -m pip install -e ".[qt,demo,test]"
```

Then run:

```bash
python -m examples.qt_demo
```

The demo uses mock SLM devices and does not require physical hardware. It also provides reference examples for setup files, multiple SLMs, workspace handling, session composition, and host integration. The `examples/` tree is distributed with the source distribution rather than installed as part of the `slmcore` wheel.

### Minimal source installation

If tests and Qt are not required, the base package can be installed from a source checkout in editable mode:

```bash
python -m pip install -e .
```

The base package contains the SLM runtime, computation, configuration, workspace, setup, and host APIs without installing optional Qt or demo dependencies.

### Minimal standalone Qt composition

A minimal programmatic Qt host can be assembled from the public API:

```python
from qtpy import QtWidgets

from slmcore import (
    SectionSplitLayout,
    SLMDefinition,
    SLMGeometry,
    SLMIdentity,
    SLMSectionsDefinition,
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
    on_startup_preferences_changed=lambda preferences: None,
)

session.initialize_device(show_error=False)

panel.show()
app.exec()
```

When using a canonical setup JSON file, load it first with `load_slm_setup_file()` and pass the resulting `SLMDefinition` and `SLMStartupPreferences` to `create()`. Passing the same `setup_file=...` lets the factory persist later startup-preference changes back into that file.

For the complete standalone reference host, see:

```text
examples/qt_demo/
```

## Using slmcore inside ImSwitch

ImSwitch keeps the complete `slmcore` project as a top-level subtree:

```text
ImSwitch/
├── imswitch/
├── slmcore/
│   ├── pyproject.toml
│   ├── requirements-dev.txt
│   ├── src/
│   │   └── slmcore/
│   ├── tests/
│   └── examples/
└── ...
```

The `slmcore` source remains an independent Python package even though its canonical development copy lives inside the ImSwitch repository.

Inside the ImSwitch environment, the local package is installed in editable mode:

```bash
python -m pip install -e ./slmcore
```

ImSwitch already owns its application Qt environment, so it does not need to install the standalone `slmcore[qt]` extra separately.

The editable installation points normal imports directly to:

```text
ImSwitch/slmcore/src/slmcore/
```

ImSwitch can therefore use the public API normally:

```python
from slmcore import SLMDefinition, SLMStartupPreferences, SLMWorkspace
from slmcore.host import SLMDeviceProvider, SLMHostServices
from slmcore.qt import SLMQtSessionFactory
```

Changes made to the local `slmcore` sources are immediately visible to ImSwitch.

The ImSwitch integration remains deliberately thin. ImSwitch is responsible for adapting its setup model, providing the workspace root, connecting its hardware managers and detector services, and persisting host-owned startup preferences.

Runtime control, pattern generation, CGH, configuration handling, calibration, feedback, workspace resources, and reusable Qt session logic remain owned by `slmcore`.

The public `slmcore` repository can therefore remain usable independently while ImSwitch continues to own and develop the canonical integrated copy.

## Dependencies

Package dependencies are defined in `pyproject.toml`.

The main source-development installation levels are:

```text
-e .                     base slmcore only
-e .[test]               core development and testing
-e .[qt,test]            reusable Qt development and full testing
-e .[qt,demo,test]       standalone demo development
```

The optional dependency groups are:

- `test`: test tooling only.
- `qt`: Qt bindings and dependencies required by the reusable Qt interface.
- `demo`: standalone-demo-only dependencies such as QDarkStyle. Use it together with `qt`.

GPU acceleration is intentionally not represented by a package extra because the appropriate CuPy binary package depends on the CUDA major version installed on the system.

`requirements-dev.txt` is only a convenience entry point for the normal non-Qt development environment:

```text
-e .[test]
```

`pyproject.toml` remains the single source of truth for package dependencies.

## Architecture

`slmcore` is divided into six top-level architectural areas. See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the detailed ownership and calibration model:

```text
src/slmcore/
├── core/          # SLM state, models and scientific computation
├── application/   # toolkit-independent workflows and session orchestration
├── workspace/     # persisted runtime resources
├── setup/         # SLM setup models and JSON I/O
├── host/          # capabilities supplied by an embedding host
└── qt/            # Qt presentation and adapters
```

### Core

`core/` contains the host- and toolkit-independent SLM model and computation layer.

Its main areas include:

- `core/engine/`: runtime state, device identity and geometry, sections, transitions, registries, parameters, and correction contracts.
- `core/patterns/`: analytic patterns and aberrations.
- `core/cgh/`: targets, CGH algorithms, localization, propagation, metrics, and feedback models.
- `core/calibration/`: calibration models and fitting.
- `core/config/`: portable runtime configuration and compiled-frame models.
- `core/measurement/`: host-neutral measurement records.

The core layer does not depend on Qt or on an embedding application.

Standard pattern, target, aberration, and CGH registrations are assembled through the `SLMRegistries` system.

### Application

`application/` owns SLM use cases independently of presentation.

`SLMSession` is the toolkit-independent application controller for one SLM runtime. It coordinates configuration loading, frame publication, device lifecycle, CGH execution, feedback, calibration, section-layout changes, and control modes.

Application services contain the corresponding workflow rules so that Qt and other hosts remain presentation or integration layers rather than owners of SLM behavior.

### Workspace

`workspace/` owns persistent runtime resources.

`SLMWorkspace` defines the standard storage layout and provides access to configuration, calibration, and correction stores.

The default layout is:

```text
<root>/
├── mappings.json
├── observation_planes.json
├── configs/<serial>/
├── corrections/<serial>/
├── calibrations/<serial>/<section>/<mapping>.json
├── fov_mapping_corrections/<serial>/<section>/<mapping>/<correction>.json
├── geometry_orientation_calibrations/<serial>/<section>/<observation-plane>.json
└── position_references/<observation-plane>/<reference>.json
```

Base mapping calibrations, FOV corrections, and geometry calibrations are scoped by
physical `SLMIdentity.serial_number` where appropriate. Position references are
intentionally workspace-wide and observation-plane scoped so different sections
can align against the same detector-space reference.

Embedding hosts normally provide only the workspace root. `slmcore` owns the standard layout below it.

### Setup

`setup/` defines the canonical description of an installed SLM.

`SLMDefinition` contains:

- `SLMIdentity`: logical key, physical serial number, and display name.
- `SLMGeometry`: width, height, and SLM pixel size.
- `SLMSectionsDefinition`: section layout and whether section geometry is customizable.

Optional host-side hardware binding is represented separately by `SLMHardwareConfig`.

Section geometries are derived by `slmcore` from the physical SLM geometry and declared section layout.

`SLMStartupPreferences` stores session startup choices such as startup configuration, default mappings, mapping-scoped default FOV corrections, feedback-orientation fallbacks, focused-editing selections, and section display mode.

Embedding hosts such as ImSwitch may store the same setup concepts inside their own setup format rather than using a standalone `slmcore` setup JSON file.

### Host integration

`host/` defines capabilities supplied by the embedding application, including device and measurement services.

This boundary allows `slmcore` to remain independent of ImSwitch hardware managers, detector managers, communication channels, and application-specific persistence.

A standalone application can provide mock or native implementations. ImSwitch provides adapters backed by its existing managers and services.

### Qt

`qt/` contains the reusable Qt presentation layer.

The standard composition root is `SLMQtSessionFactory`, which constructs the runtime/application stack and binds it to an `SLMPanel`.

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

`SLMQtSession` adapts the toolkit-independent application session to Qt. Runtime/application state remains authoritative; Qt owns presentation, user interaction, dialogs, and interaction policy.

## Mapping, observation and calibration model

The current calibration model deliberately separates **pattern mapping** from
**observation context**. They are independent axes.

- A **mapping** is an SLM-to-sample metric calibration context, for example a
  particular objective/relay configuration. Mapping selection affects metric
  conversion and therefore CGH validity.
- An **observation plane** describes where a measurement is interpreted. It may
  suggest a detector and fallback sample-space pixel size, but changing the
  observation plane does not change the active mapping or the CGH.
- **Observation geometry** is the saved section × observation-plane orientation
  relationship used when localized detector points must be assigned to logical
  target identities.
- An **FOV mapping correction** belongs to one section × mapping. Its calibration
  observation plane is provenance only. Runtime selection/application is handled
  independently from the fitting workflow.

The application layer reflects those boundaries:

```text
SLMCalibrationService        active mapping + base mapping-calibration workflow
MappingService               mapping catalog + persistent FOV corrections
ObservationPlaneService      observation-plane catalog
ObservationGeometryService   persisted section × observation-plane geometry
GeometryCalibrationService   reusable geometry-calibration workflow
FOVCalibrationService        dedicated FOV fitting workflow
FOVMappingRuntimeService     selected/default/applied FOV correction at runtime
SLMFeedbackService           measurement/localization/intensity/position/reference feedback
```

FOV application remains part of the CGH runtime because it changes resolved
target positions. Ownership of the saved correction and its calibration workflow
is outside the CGH session. The effective position path is:

```text
ideal resolved target positions
        ↓
optional mapping-owned FOV correction
        ↓
optional transient position-feedback correction
        ↓
final CGH target positions
```

Phase 1 intentionally keeps ordinary metric conversion effectively diagonal.
The stored 2×2 representation remains available, but generalized off-diagonal
parameter conversion and cross-observation-plane coordinate transforms are not
part of the current contract.

## Configuration and frame reproducibility

Saved SLM configurations can contain both editable runtime state and the compiled frame produced from that state.

Correction resolution is captured with the computed section artifacts so that saved configurations retain the effective correction pattern and 2π value used to generate the frame.

Normal editor loading can compare saved corrections and calibration state against the current workspace and request an explicit resolution when required.

Fast Config mode treats the persisted compiled frame as authoritative and avoids reconstructing it from editable state until returning to the normal editor workflow.

## License

`slmcore` is distributed under the GNU General Public License v3.0. See `LICENSE`.