# slmcore.qt

`slmcore.qt` is the reusable Qt integration and presentation layer for
`slmcore`. Importing `slmcore` itself never imports Qt.

The normal host entry point is `SLMQtSessionFactory`, which returns an
`SLMQtSession` + `SLMPanel` pair. The panel owns the standard reusable Qt
composition for one SLM. `SLMQtSession` adapts the toolkit-independent
`SLMSession` application controller to Qt views, dialogs and thread-aware host
integration. Embedding applications provide physical capabilities and
startup preferences rather than reimplementing SLM workflow.

## Package structure

```text
qt/
├── application/
│   ├── factory.py               # standard SLM Qt session/panel construction
│   ├── session.py               # Qt adapter around application.SLMSession
│   ├── measurement_dispatcher.py # safe host-measurement callback delivery
│   ├── runtime_binding.py       # retained-view/runtime edit binding
│   ├── cgh_executor.py          # default threaded CGH executor
│   ├── interaction.py           # interaction/debounce policy
│   ├── section_settings.py      # topology/presentation/layout workflow
│   ├── feedback/
│   │   ├── coordinator.py       # feedback windows/actions/confirmations adapter
│   │   └── automatic.py         # compatibility import; sequencer is application-owned
│   └── calibration/
│       ├── manager.py           # calibration dialogs/actions presentation adapter
│       └── state.py             # compatibility re-export of application state
├── panel/
│   ├── panel.py                 # standard reusable one-SLM composition
│   └── policy.py                # preview placement/container presentation policy
├── preview/
│   ├── view.py                  # raw independently mountable SLM preview
│   └── panel.py                 # collapsible/plain decorated preview
├── configuration/
│   ├── controls.py              # reusable compact config selector/actions
│   ├── dialogs.py               # generic config dialogs
│   └── manager.py               # Qt config dialogs/controls coordinator
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

1. loads/adapts its canonical `SLMDefinition` and `SLMStartupPreferences`;
2. creates one shared `SLMWorkspace` rooted at its SLM data directory;
3. supplies physical capabilities through `SLMHostServices`;
4. constructs one `SLMQtSessionFactory(workspace=...)` (optionally with custom registries);
5. calls `qt_session, panel = factory.create(...)`, either with `setup_file` for
   standard slmcore JSON persistence or with `on_startup_preferences_changed`
   when the host owns a larger setup file;
6. mounts/registers the returned objects in the host;
7. only then calls `qt_session.initialize_device()` when the configured output
   device should be initialized/published at startup.

`SLMQtSessionFactory` owns default registry selection, workspace-backed config /
correction / calibration resolution, startup preference semantics,
`SLMRuntimeFactory` construction, and the generic startup/runtime/panel/session
assembly. The factory now constructs `SLMSession` explicitly and injects it into
`SLMQtSession`; the Qt session is therefore an adapter around an already-created
application session rather than the owner of application construction. The factory does not initialize hardware, allowing an embedding host
to complete its own registration/mount transaction before physical device side
effects begin.

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

Current host capabilities are:

- `device: SLMDeviceProvider` for physical upload and optional explicit
  connect/disconnect;
- `measurement_provider` for asynchronous, cancellable image acquisition.

Startup config, default active planes and section display mode are represented
together by `SLMStartupPreferences`, not as host capabilities.

`SLMDeviceProvider` is callback-backed and normalizes connect/disconnect results
to `DeviceConnectionResult`. `requires_explicit_connection` determines whether
the standard panel displays its Connect/Disconnect control.
`MockSLMDeviceProvider` provides an explicit in-memory simulation/test device;
`device=None` means no output-device capability and is not interpreted as mock.

The physical device implementation remains a host capability. Generic
connect/disconnect and frame-upload lifecycle belongs to toolkit-independent
`SLMSession`; `SLMQtSession` owns the connection control, status and mode-aware
choice of which frame is currently active. A host may use the callback-backed
provider for an existing device manager or later supply a provider subclass
for a native slmcore device backend.

A shared `SLMCalibrationStore` supplies the plane catalog and per-SLM/section
calibration files. Multiple `SLMQtSession` instances may share one store, so
plane additions/deletions are reconciled across every SLM using that catalog.

The measurement provider exposes source discovery/preference plus one generic
`acquire(...) -> ImageMeasurement` operation. Detector-manager APIs, camera
signals and acquisition transport remain host-specific; localization,
measurement provenance, feedback adaptation and automatic-loop sequencing do
not.

The underlying `SLMSession` owns:

- generic device connect/disconnect and frame upload;
- preview-frame publication events and automatic/deferred upload policy;
- CGH preparation, request IDs, cancellation/stale handling and runtime commit;
- current config metadata/path and config-store-backed application operations;
- side-effect-free config preparation plus authoritative config commit;
- editor/Fast-Config mode, strict compiled-frame activation and `fast_config_path`;
- measurement commit, localization, intensity/position feedback operations and
  automatic intensity-feedback sequencing through `SLMFeedbackService`;
- automatic-feedback availability/stop state and cancellation across runtime changes;
- plane catalog reconciliation, active-plane/default-plane semantics, calibration
  acquisition/localization/fitting/persistence through `SLMCalibrationService`;
- application-owned transient target-calibration state and startup calibration defaults;
- generic section patch/topology/presentation/calibration and CGH-session mutations
  used by Qt adapters;
- committed application events, with runtime/config/feedback/calibration state
  remaining authoritative if a presentation observer fails.

`SLMQtSession` owns the Qt/application adaptation around that core:

- standard `SLMPanel` wiring and optional device connection workflow;
- `SLMRuntimeViewBinding` and parameter-patch debounce;
- reusable `CghAction` dispatch;
- default asynchronous execution through `QtCGHExecutor`;
- `CGHSessionWindow` lifetime and all `MeasurementsAction` dispatch;
- feedback file dialogs, destructive-operation confirmations, plots/status and
  rendering of application-owned feedback state;
- plane catalog presentation plus interactive plane add/delete/selection actions;
- calibration dialog lifetime, file selection and target/localization rendering;
- interactive calibration-geometry mismatch decisions translated into application policy;
- CGH/feedback presentation synchronization;
- section topology, presentation/title, display-mode and fixed-count layout
  workflows;
- retained collection/view rebuild after an application-owned runtime replacement;
- config selector/actions, file/update dialogs and config inspection presentation;
- interactive calibration-geometry compatibility decisions translated into
  application `KEEP` / `CLEAR` / `REJECT` policies;
- Fast Config visibility/write-lock presentation after application mode changes;
- section view synchronization after committed application transitions.

`SLMRuntimeViewBinding` remains a low-level implementation primitive; normal
hosts should not own or wire it directly.

## CGH execution

CGH scheduling is injectable. `cgh_executor=None` creates an internally owned
`QtCGHExecutor`. A host may instead provide any object implementing the generic
`slmcore.core.cgh.execution.CGHExecutor` contract:

```python
executor.submit(job, on_result, on_error)
```

The executor only runs detached `CGHJob` work. Request IDs, runtime generations,
stale-result handling and commit semantics live in `SLMSession`; Qt view
synchronization remains in `SLMQtSession`. Injected executors are externally
owned and are not disposed by the application session.

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

Provider result/error callbacks may run on any host thread. Both application
feedback and calibration services depend only on the `MeasurementDispatcher`
protocol. In the Qt composition, `QtMeasurementDispatcher` implements that
protocol and always queues completion through Qt before application callbacks
run. This keeps widget updates on the Qt thread without making either workflow
depend on Qt.

Image loading from disk is a reusable Qt workflow and therefore stays inside
`slmcore.qt` rather than being a host capability.


## Calibration

`SLMCalibrationService` owns the toolkit-independent calibration workflow:
plane catalog reconciliation, active-plane/default-plane state, startup default
application, live-acquisition eligibility/dispatch, target-reference and
localization state, calibration fitting, persistence and rollback. The Qt
`CalibrationManager` is a presentation adapter that owns dialogs, file picking,
interactive confirmations and rendering only.

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
anyway. Plane selection itself follows an application `prepare -> decide -> commit`
flow: headless selection rejects a geometry mismatch by default, while Qt may
explicitly pass `KEEP` after user confirmation. A startup config receives no modal override: any internal
calibration/section geometry mismatch rejects the startup config and the host
can surface the returned startup warning in its status UI.

`SLMStartupPreferences` supplies the default active plane per section. A successfully loaded startup config's calibration
remains authoritative; default-plane calibration is applied only when no
startup config was restored. Preference changes are persisted either directly
to a canonical setup JSON file or through the host callback supplied to the
factory.

## Automatic intensity feedback

Automatic feedback is owned by toolkit-independent `SLMFeedbackService` and is a
sequencer over the same application operations used by manual feedback:

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

Only one automatic loop may run per physical SLM. `AutomaticFeedbackState` is
application state observed by Qt. While active, the Qt adapter locks the SLM
interaction surface and all CGH-session windows; only the owning window's **Stop**
action remains available. Stop cancels an in-flight measurement immediately. If
CGH computation has already started, that computation is allowed to finish and
commit before the loop stops.

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

`SLMDefinition.sections` defines definition-level physical layout constraints: the default
split, whether layout editing is allowed, and a fixed section count. slmcore
derives the definition section geometries from that declaration. A customized current
split belongs to the SLM config and is not written back into the SLM definition.

`SLMConfigStore` is the non-Qt directory-bound persistence API in `slmcore.workspace`,
while `application.SLMConfigurationService` performs config preparation,
validation, persistence operations and runtime restoration. `SLMSession` owns
`current_config_path` and the authoritative commit. `QtConfigurationManager`
only mirrors that state into `ConfigControls`, owns config dialogs/inspection,
and translates interactive calibration/correction decisions into application policies.

Config loading follows `prepare -> decide -> commit`. A Qt synchronization
failure after commit is reported but never rolls the runtime/config state back.
Normal editor loading intentionally uses `require_complete=False`. Fast Config
uses the saved compiled frame directly on entry; leaving Fast Config strictly
reconstructs the active config with `require_complete=True` and resolves any
calibration/correction mismatch before returning to the editor.
`current_config_path` is the config represented by the editable runtime, while
`fast_config_path` is the compiled config currently selected/displayed during
Fast Config mode.

`SectionSettingsManager` owns only the section-settings interaction: topology,
presentation/title and display-mode UI plus Keep/Clear/Cancel decisions for
layout changes. Section-layout validation and replacement planning are owned by
the application layer, and all normal topology/presentation/layout mutations
route through `SLMSession`. `SLMRuntimeViewBinding` similarly routes normal
section patches and committed-target restore through the application session
when used by `SLMQtSession`.

Config identity and physical geometry remain strict. Section count remains
fixed. Startup config loading is deterministic/non-interactive; interactive
loads may explicitly keep or clear calibration geometry mismatches. Headless
loading rejects such mismatches unless a non-default policy is explicitly
supplied.

## Current host boundary

The host owns only adaptation of its setup format into canonical `SLMDefinition` /
`SLMStartupPreferences`, workspace root selection, SetupMode integration, the
outer multi-SLM shell, and the concrete callbacks/driver behind
`SLMDeviceProvider`. `SLMWorkspace` supplies standard config/calibration/
correction resource persistence. Startup preferences remain in the setup
configuration and are rewritten either by slmcore's standard JSON path or by a
host callback. The reusable one-SLM UI composition, preview, connection control,
config controls and section presentation live in `SLMPanel`.

Cross-SLM policies remain host-level. For example, ImSwitch deliberately
propagates `RuntimeViewInteractionSettings` changes from one SLM session to
its other SLM sessions rather than making an individual `SLMPanel` aware of
other devices.

### Boundary hardening

`SLMQtSession` now requires an already constructed `SLMSession`; the Qt adapter no longer has a second application-construction path. Section-layout validation/replacement is application-owned, and normal Qt operation routes runtime mutations through `SLMSession`. The only direct `SLMRuntime.apply_section_patch` path retained in Qt is the intentional low-level fallback of `SLMRuntimeViewBinding` when no application session is supplied.
