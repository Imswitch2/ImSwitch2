# Design docs

Internal documentation about how ImSwitch is built — architecture
diagrams, subsystem specifications, and historical plans. These are
**not** user-facing guides; for those see the RST files under `docs/`
(the Sphinx-rendered site) or the practical recipes under
[../how-to/](../how-to/).

## What's in here

| File | Purpose |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Dependency map: modules, managers, controllers, widgets, signal flow, third-party libraries |
| [DEVICE_PLUGINS.md](DEVICE_PLUGINS.md) | Proposed architecture and implementation plan for external device plugin packages |
| [WIDGET_STATE_PERSISTENCE.md](WIDGET_STATE_PERSISTENCE.md) | How widget controllers opt into save/load of their state |
| [plans/](plans/) | Historical and active integration plans (etSTED 2.0, layer lifecycle, WS integration, etc.) |

## When to add a file here vs `how-to/`

- **Here (`design/`):** internal-perspective explanation of how a
  subsystem works, with cross-references to source files.  Audience:
  contributors who need to extend or debug ImSwitch internals.
- **[`how-to/`](../how-to/):** task-oriented recipe with concrete
  step-by-step instructions.  Audience: a user who wants to do X.

A subsystem will often have both: a `design/` doc explaining the
abstraction, plus one or more `how-to/` recipes for common workflows.
