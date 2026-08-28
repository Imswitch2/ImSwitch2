# slmcore

`slmcore` is a standalone Python package for spatial light modulator (SLM) control, pattern generation, computer-generated holography (CGH), calibration, feedback, configuration, and reusable Qt interfaces.

It is developed jointly with **ImSwitch**, which is its primary host application and integration target. At the same time, `slmcore` is deliberately kept host-independent: it can be used from its own standalone Qt host or embedded into another application through its public API.

Host applications should use the public `slmcore` interfaces rather than maintaining host-specific copies of the core implementation.

## Getting started

`slmcore` requires Python 3.10 or newer.

### Core development

The standard development environment does not require Qt.

From a source checkout, create and activate a Python environment, then install:

```bash
python -m pip install -r requirements-dev.txt
```

`requirements-dev.txt` contains:

```text
-e .[test]
```

This installs `slmcore` in editable mode together with its test dependencies, but without the optional Qt stack.

Changes under `src/slmcore/` are immediately available without reinstalling the package.

Run the test suite with:

```bash
python -m pytest
```

The non-Qt tests run normally. Qt-specific tests are skipped when the Qt dependencies are not installed.

This is the appropriate environment for development of the core, application, setup, workspace, and host-independent parts of `slmcore`.

### Qt development and standalone application

To develop or use the Qt interface, install both the Qt and test dependency groups:

```bash
python -m pip install -e ".[qt,test]"
```

This installs the same editable `slmcore` package together with the dependencies required by `slmcore.qt`.

The full test suite, including Qt tests, can then be run with:

```bash
python -m pytest
```

Run the hardware-free standalone Qt demo with:

```bash
python -m examples.qt_demo
```

The demo uses mock SLM devices and does not require physical hardware. It also provides reference examples for setup files, multiple SLMs, workspace handling, session composition, and host integration.

### Minimal installation

If tests and Qt are not required, the base package can be installed directly from a source checkout:

```bash
python -m pip install -e .
```

The base package contains the SLM runtime, computation, configuration, workspace, setup, and host APIs without installing the optional Qt dependencies.

Importing `slmcore` itself does not import Qt. Qt functionality is accessed explicitly through `slmcore.qt`.

### Minimal standalone Qt composition

A minimal programmatic Qt host can be assembled from the public API:

```python
from qtpy import QtWidgets

from slmcore import (
    SectionSplitLayout,
    SLMGeometry,
    SLMIdentity,
    SLMSectionsSetup,
    SLMSetup,
    SLMWorkspace,
)
from slmcore.host import MockSLMDeviceProvider, SLMHostServices
from slmcore.qt import SLMQtSessionFactory


app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

setup = SLMSetup(
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
    sections=SLMSectionsSetup(
        layout=SectionSplitLayout(n_sections=1),
    ),
)

workspace = SLMWorkspace("slm_data")
factory = SLMQtSessionFactory(workspace=workspace)

session, panel = factory.create(
    setup=setup,
    host_services=SLMHostServices(
        device=MockSLMDeviceProvider(),
    ),
    on_startup_preferences_changed=lambda preferences: None,
)

session.initialize_device(show_error=False)

panel.show()
app.exec()
```

A standalone application that uses a canonical setup JSON file can instead provide `setup_file=...` to `SLMQtSessionFactory.create()`. In that case, `slmcore` can persist startup preferences directly in that setup file.

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
from slmcore import SLMSetup, SLMStartupPreferences, SLMWorkspace
from slmcore.host import SLMDeviceProvider, SLMHostServices
from slmcore.qt import SLMQtSessionFactory
```

Changes made to the local `slmcore` sources are immediately visible to ImSwitch.

The ImSwitch integration remains deliberately thin. ImSwitch is responsible for adapting its setup model, providing the workspace root, connecting its hardware managers and detector services, and persisting host-owned startup preferences.

Runtime control, pattern generation, CGH, configuration handling, calibration, feedback, workspace resources, and reusable Qt session logic remain owned by `slmcore`.

The public `slmcore` repository can therefore remain usable independently while ImSwitch continues to own and develop the canonical integrated copy.

## Dependencies

Package dependencies are defined in `pyproject.toml`.

Three installation levels are intentionally supported:

```text
-e .                base slmcore only
-e .[test]          core development and testing
-e .[qt,test]       Qt development and full testing
```

The optional dependency groups are:

- `test`: test tooling only.
- `qt`: Qt bindings and the dependencies required by the reusable Qt interface and standalone host.

`requirements-dev.txt` is only a convenience entry point for the normal non-Qt development environment:

```text
-e .[test]
```

`pyproject.toml` remains the single source of truth for package dependencies.

## Architecture

`slmcore` is divided into six top-level architectural areas:

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
├── configs/<serial>/
├── corrections/<serial>/
└── calibrations/
```

Persistent resources are namespaced using `SLMIdentity.serial_number`.

Embedding hosts normally provide only the workspace root. `slmcore` owns the standard layout below it.

### Setup

`setup/` defines the canonical description of an installed SLM.

`SLMSetup` contains:

- `SLMIdentity`: logical key, physical serial number, and display name.
- `SLMGeometry`: width, height, and pixel size.
- `SLMSectionsSetup`: section layout and layout policy.
- optional `SLMHardwareSetup`: hardware binding for hosts that use native `slmcore` hardware definitions.

Section geometries are derived by `slmcore` from the physical SLM geometry and declared section layout.

`SLMStartupPreferences` stores session startup choices such as startup configuration, default calibration planes, and section display mode.

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

## Configuration and frame reproducibility

Saved SLM configurations can contain both editable runtime state and the compiled frame produced from that state.

Correction resolution is captured with the computed section artifacts so that saved configurations retain the effective correction pattern and 2π value used to generate the frame.

Normal editor loading can compare saved corrections and calibration state against the current workspace and request an explicit resolution when required.

Fast Config mode treats the persisted compiled frame as authoritative and avoids reconstructing it from editable state until returning to the normal editor workflow.

## License

`slmcore` is distributed under the GNU General Public License v3.0. See `LICENSE`.