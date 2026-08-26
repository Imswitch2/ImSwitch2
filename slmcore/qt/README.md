# slmcore.qt

`slmcore.qt` is the reusable Qt integration and presentation layer for
`slmcore`. Importing `slmcore` itself never imports Qt.

The normal host entry point is `SLMQtSessionFactory`, which returns an
`SLMQtSession` + `SLMPanel` pair. The panel owns the standard reusable Qt
composition for one SLM, while the session owns runtime/workflow behavior and
binds itself to that panel. Embedding applications provide physical capabilities
and setup-level preferences rather than reimplementing SLM workflow.

## Package structure

```text
qt/
├── application/
│   ├── factory.py               # standard SLM Qt session/panel construction
│   ├── session.py               # reusable per-SLM Qt workflow session
│   ├── measurement_dispatcher.py # safe host-measurement callback delivery
│   ├── runtime_binding.py       # retained-view/runtime edit binding
│   ├── cgh_executor.py          # default threaded CGH executor
│   ├── interaction.py           # interaction/debounce policy
│   ├── section_settings.py      # topology/presentation/layout workflow
│   ├── feedback/
│   │   ├── coordinator.py       # manual measurement/feedback workflow + windows
│   │   └── automatic.py         # automatic intensity-feedback sequencer
│   └── calibration/
│       ├── manager.py           # plane + linear/target calibration workflow
│       └── state.py             # target-calibration transient state
├── panel/
│   ├── panel.py                 # standard reusable one-SLM composition
│   └── policy.py                # preview placement/container presentation policy
├── preview/
│   ├── view.py                  # raw independently mountable SLM preview
│   └── panel.py                 # collapsible/plain decorated preview
├── configuration/
│   ├── controls.py              # reusable compact config selector/actions
│   ├── dialogs.py               # generic config dialogs
│   └── manager.py               # config workflow coordinator
├── sections/                    # retained section views and settings
├── widgets/                     # parameter editors and small generic widgets
├── cgh/                         # CGH/session presentation and dialogs
├── measurement/                 # measurement/localization Qt views
└── calibration/                 # calibration Qt views/dialogs
```

The top-level `slmcore.qt` package is intentionally a strict host-facing
facade: session/factory, standard panel/preview policies, section display mode,
render policy, and interaction settings. Lower-level forms, bindings, dialogs,
section collections and workflow helpers are imported from their explicit
submodules. Internal `slmcore.qt` code likewise imports defining modules directly
rather than routing dependencies back through public package facades.

## Main integration contract

A host normally:

1. translates its setup representation into the canonical `SLMSetup`;
2. creates one shared `SLMWorkspace` rooted at its SLM data directory;
3. supplies physical capabilities and any host-specific preference overrides
   through `SLMHostServices`;
4. constructs one `SLMQtSessionFactory(workspace=...)` (optionally with custom registries);
5. calls `qt_session, panel = factory.create(setup=..., host_services=...)`;
6. mounts/registers the returned objects in the host;
7. only then calls `qt_session.initialize_device()` when the configured output
   device should be initialized/published at startup.

`SLMQtSessionFactory` owns default registry selection, workspace-backed config /
correction / calibration / preference resolution, `SLMRuntimeFactory`
construction, and the generic startup/runtime/panel/session assembly. Explicit
host preference capabilities override workspace defaults. The factory does not
initialize hardware, allowing an embedding host to complete its own
registration/mount transaction before physical device side effects begin.

`SLMPanel` owns the standard composition currently used by ImSwitch. Its
presentation-only `SLMPanelLayoutPolicy` controls integrated preview placement:

- `TOP + COLLAPSIBLE` is the default ImSwitch layout. The collapsible preview
  has a retained vertical resize handle; collapse/expand restores the last
  session height.
- `TOP + PLAIN` uses a vertical splitter between preview and sections.
- `LEFT/RIGHT + PLAIN` use a horizontal splitter between preview and sections.
- `NONE` constructs no preview view/panel and mounts sections only.

A collapsible preview is intentionally valid only at `TOP`. Preview size is UI
session state and is not persisted to SLM config or setup preferences.

The raw `SLMPreviewView` remains a separate reusable widget with no collapsible
or layout assumptions. `SLMPreviewPanel` is only a decorated integrated wrapper,
so a standalone host can mount `SLMPreviewView` independently without changing
session behavior. When bound to `SectionsViewHost`, the raw preview outlines
the selected physical section in red for multi-section tab presentation; the
outline is hidden for one section and for horizontal section presentation.

`SLMQtSession` receives the panel as one view dependency and internally wires
frame publication, automatic-operation locking, status, standard messages,
config controls, section views, and optional device connection requests. The
session signals remain observable, but a normal host does not wire those
standard UI behaviors itself.

Current host capabilities/preferences are:

- `device: SLMDeviceProvider` for physical upload and optional explicit
  connect/disconnect;
- `measurement_provider` for asynchronous, cancellable image acquisition;
- `calibration_preferences` for setup-level default active planes;
- `configuration_preferences` for the startup config filename;
- `section_view_preferences` for setup-level section display mode.

`SLMDeviceProvider` is callback-backed and normalizes connect/disconnect results
to `DeviceConnectionResult`. `requires_explicit_connection` determines whether
the standard panel displays its Connect/Disconnect control.
`MockSLMDeviceProvider` provides an explicit in-memory simulation/test device;
`device=None` means no output-device capability and is not interpreted as mock.

The physical device implementation remains a host capability, while generic
connection UI/lifecycle behavior belongs to `SLMQtSession`. A host may use
the callback-backed provider for an existing device manager or later supply a
provider subclass for a native slmcore device backend.

A shared `SLMCalibrationStore` supplies the plane catalog and per-SLM/section
calibration files. Multiple `SLMQtSession` instances may share one store, so
plane additions/deletions are reconciled across every SLM using that catalog.

The measurement provider exposes source discovery/preference plus one generic
`acquire(...) -> ImageMeasurement` operation. Detector-manager APIs, camera
signals and acquisition transport remain host-specific; localization,
measurement provenance, feedback adaptation and automatic-loop sequencing do
not.

`SLMQtSession` owns:

- standard `SLMPanel` wiring and optional device connection workflow;
- `SLMRuntimeViewBinding` and parameter-patch debounce;
- reusable `CghAction` dispatch;
- CGH preparation, request tracking, stale/cancel handling and result commit;
- default asynchronous execution through `QtCGHExecutor`;
- `CGHSessionWindow` lifetime and all `MeasurementsAction` dispatch;
- measurement/localization/intensity/position feedback orchestration;
- plane catalog presentation/selection and plane add/delete actions;
- linear-phase and target-localization calibration orchestration;
- calibration dialog lifetime, target-calibration candidates and persistence;
- automatic intensity-feedback sequencing using the same operations as manual
  feedback;
- CGH/feedback presentation synchronization;
- section topology, presentation/title, display-mode and fixed-count layout
  workflows;
- canonical runtime replacement, including retained collection/view rebuild;
- reusable config listing/load/save/update/rename/duplicate/delete/inspection;
- interactive calibration-geometry compatibility decisions for layout/config
  changes;
- one post-transition path for section view synchronization and frame
  publication;
- automatic physical frame upload when `auto_upload_frame=True`.

`SLMRuntimeViewBinding` remains a low-level implementation primitive; normal
hosts should not own or wire it directly.

## CGH execution

CGH scheduling is injectable. `cgh_executor=None` creates an internally owned
`QtCGHExecutor`. A host may instead provide any object implementing the generic
`slmcore.cgh.execution.CGHExecutor` contract:

```python
executor.submit(job, on_result, on_error)
```

The executor only runs detached `CGHJob` work. Request IDs, runtime generations,
stale-result handling, commit semantics and UI synchronization remain inside
`SLMQtSession`. Injected executors are externally owned and are not disposed
by the session.

## Measurement provider

`SLMHostServices.measurement_provider` implements the host-side physical
acquisition boundary:

```python
provider.available_sources(section_key)
provider.preferred_source(section_key, available)
provider.acquire(
    section_key,
    source,
    metadata=...,
    on_result=...,      # receives ImageMeasurement
    on_error=...,
)
```

The returned request handle may expose `cancel()`. The same provider can serve
manual feedback, automatic feedback and calibration workflows; those workflows
must not each implement detector acquisition independently.

Provider result/error callbacks may run on any host thread. `SLMQtSession`
routes them through `QtMeasurementDispatcher`, which always queues completion
through Qt before feedback or calibration callbacks run. This keeps widget
updates on the Qt thread and gives immediate and asynchronous providers the same
completion ordering.

Image loading from disk is a reusable Qt workflow and therefore stays inside
`slmcore.qt` rather than being a host capability.


## Calibration

Calibration uses the same `measurement_provider` as feedback. Live target
calibration is bound to the detector declared by the selected plane; the user
does not select an arbitrary detector in the calibration workflow. The target
reference remains calibration-free.

Live acquisition is enabled only when the section has a **current** computed CGH
and automatic frame upload can guarantee that the current frame reached the
hardware. Missing, stale or computing CGH states disable Acquire with a reason
in the button tooltip. Loading an image from disk remains available for offline
calibration when the editable base-target reference itself is valid.

Plane definitions and calibration files live in `SLMCalibrationStore`. Every
persisted valid calibration records the explicit `SectionGeometry` under which
it was measured. Geometry equality is therefore inspectable and not represented
only by an opaque signature. A calibration whose recorded geometry differs from
the current section remains valid but is presented with a warning.

Interactive layout/config changes that would retain geometry-mismatched
calibrations ask the user to **Keep**, **Clear**, or **Cancel**. Selecting a
stored plane calibration with a different geometry asks whether to use it
anyway. A startup config receives no modal override: any internal
calibration/section geometry mismatch rejects the startup config and the host
can surface the returned startup warning in its status UI.

The host only supplies the storage root and optional `CalibrationPreferences`
callbacks for its setup-level default plane per section. A successfully loaded
startup config's calibration remains authoritative; default-plane calibration
is applied only when no startup config was restored.

## Automatic intensity feedback

Automatic feedback is a sequencer over the normal manual operations:

```text
fresh acquisition
→ reuse previous localization when requested and compatible
  otherwise localize + commit
→ apply intensity adaptation
→ compute adapted CGH through the normal CGH executor
→ successful frame upload
→ next requested round
```

The requested count means **new adapted/computed rounds**, regardless of the
current round number. No extra measurement is acquired after the final
requested CGH completes.

Only one automatic loop may run per physical SLM. While it is active the SLM
interaction surface and all CGH-session windows are locked; only the owning
window's **Stop** action remains available. Stop cancels an in-flight
measurement immediately. If CGH computation has already started, that
computation is allowed to finish and commit before the loop stops.

Automatic feedback is intentionally unavailable when
`auto_upload_frame=False`, when no upload capability is supplied, or when no
measurement provider is supplied. Host-controlled frame-ready semantics for the
`auto_upload_frame=False` case are deliberately left undefined for now.

## Frame publication

Every accepted frame-changing transition follows the same session path.
The current frame is always emitted through `sigFrameChanged` and the standard
`SLMPanel` preview is updated internally. With `auto_upload_frame=True` (the
default), the session also invokes `SLMDeviceProvider.upload_frame()`. With
`auto_upload_frame=False`, the host can call `upload_current_frame()`
explicitly.

`defer_frame_upload()` may be used around a batch of transitions to keep Qt
state/preview synchronization immediate while coalescing physical upload to the
latest frame.

## Interaction policy

`RuntimeViewInteractionSettings` is a Qt/application interaction policy, not
part of `SLMConfig`. Standard and CGH-target drafts use independent debounce
buckets. Explicit `flush_*` barriers commit pending drafts without generating a
second auto-compute request.

`CghGroupView` owns the per-section **Auto recompute** preference. After a
successful target commit, `SLMRuntimeViewBinding` can request recomputation;
`SLMQtSession` executes it through the same CGH path as manual computation.
Automatic replacement remains suppressed when it would discard meaningful
feedback/session state.

## Configuration, layout and runtime replacement

`SLMSetup.sections` defines setup-level physical layout constraints: the default
split, whether layout editing is allowed, and a fixed section count. slmcore
derives the setup section geometries from that declaration. A customized current
split belongs to the SLM config and is not written back into setup data.

`SLMConfigRepository` is the non-Qt directory-bound persistence facade.
`ConfigurationManager` coordinates `ConfigControls` and dialogs with the
repository and `SLMQtSession`; serialization remains in `SLMConfigStore`.
`SectionSettingsManager` owns topology, presentation/title, display mode and
layout editing. Both route runtime replacement through
`SLMQtSession.replace_runtime()` so hosts do not rebuild collections,
reinstall bindings, or resynchronize reusable managers themselves.

Config identity and physical geometry remain strict. Section count remains
fixed. Startup config loading is deterministic/non-interactive; interactive
loads may explicitly accept or clear calibration geometry mismatches.

## Current host boundary

The host owns only translation from its setup objects into `SLMSetup`, workspace
root selection, host-specific capability/preference overrides, SetupMode
integration, the outer multi-SLM shell, and the concrete callbacks/driver behind
`SLMDeviceProvider`. Standard config/calibration/correction/preferences
persistence is supplied by `SLMWorkspace`. The reusable one-SLM UI composition,
preview, connection control, config controls and section presentation live in
`SLMPanel`.

Cross-SLM policies remain host-level. For example, ImSwitch deliberately
propagates `RuntimeViewInteractionSettings` changes from one SLM session to
its other SLM sessions rather than making an individual `SLMPanel` aware of
other devices.
