# slmcore.qt

`slmcore.qt` is the reusable Qt presentation and integration layer for
`slmcore`. Importing `slmcore` itself does not import Qt.

The normal host entry point is `SLMQtSessionFactory`, which returns an
`SLMQtSession` + `SLMPanel` pair. The factory constructs the toolkit-independent
`SLMSession` first; `SLMQtSession` then adapts that application session to Qt.
Runtime, calibration, feedback, and persistence remain application/core
responsibilities rather than widget-owned state.

For the complete ownership model, see `ARCHITECTURE.md` at the repository root.

## Package structure

```text
qt/
├── application/
│   ├── factory.py                # standard Qt/application composition root
│   ├── session.py                # Qt adapter around application.SLMSession
│   ├── measurement_dispatcher.py # queue host acquisition callbacks onto Qt
│   ├── runtime_binding.py        # retained-view/runtime edit binding
│   ├── cgh_executor.py           # default threaded CGH executor
│   ├── interaction.py            # debounce/interaction policy
│   ├── section_settings.py       # topology/presentation/layout workflow
│   ├── geometry_calibration.py   # reusable geometry-calibration coordinator
│   ├── feedback/                 # focused feedback coordinators
│   └── calibration/
│       └── manager.py            # Calibration workspace orchestration
├── calibration/                  # Calibration workspace widgets/dialogs
├── cgh/                          # CGH/session presentation
├── configuration/                # config controls/dialogs/coordinator
├── measurement/                  # measurement/localization workbench
├── panel/                        # standard one-SLM composition
├── preview/                      # reusable SLM preview
├── sections/                     # retained section views/settings
└── widgets/                      # generic parameter/control widgets
```

The top-level `slmcore.qt` namespace is intentionally small and host-facing:
session/factory, standard panel and preview policies, render/display policy, and
interaction settings. Lower-level widgets/coordinators should be imported from
their defining submodules.

## Main integration contract

A host normally:

1. provides an `SLMDefinition` and `SLMStartupPreferences`;
2. creates one shared `SLMWorkspace` rooted at its SLM data directory;
3. supplies external capabilities through `SLMHostServices`;
4. constructs `SLMQtSessionFactory(workspace=...)`;
5. calls `qt_session, panel = factory.create(definition=..., ...)`;
6. mounts/registers those objects in the host;
7. calls `qt_session.initialize_device()` only after host registration is ready.

`SLMQtSessionFactory` resolves the workspace stores, constructs
`SLMRuntimeFactory`, `SLMSession`, the standard `SLMPanel`, and finally the Qt
adapter/coordinators. It does not initialize physical hardware automatically.

Startup preferences contain UI/session defaults rather than physical host
capabilities. Current fields include startup config, default mapping per section,
mapping-scoped default FOV correction, feedback-orientation fallbacks, focused
editing, and section display mode.

## Host capabilities

Current host-facing capabilities are supplied through `SLMHostServices`:

- `device: SLMDeviceProvider` for frame upload and optional explicit
  connect/disconnect;
- `measurement_provider` for asynchronous, cancellable image acquisition.

Detector-manager APIs, camera signals, transport details, and ImSwitch-specific
setup objects remain outside `slmcore`.

The measurement provider exposes source discovery/preference and one generic
acquisition operation returning `ImageMeasurement`. Provider callbacks may occur
on any host thread; `QtMeasurementDispatcher` queues completion through Qt before
application callbacks mutate UI-observed state.

Loading an image from disk is a reusable Qt workflow and therefore stays inside
`slmcore.qt`, not in the host capability contract.

## Application ownership visible from Qt

The Qt layer presents, but does not own, the following toolkit-independent
services:

```text
SLMSession
├── SLMCalibrationService        active mapping + base calibration workflow
├── MappingService               mapping catalog + persisted FOV corrections
├── ObservationPlaneService      observation-plane catalog
├── ObservationGeometryService   saved section × observation-plane geometry
├── GeometryCalibrationService   shared geometry workflow
├── FOVCalibrationService        dedicated FOV fitting workflow
├── FOVMappingRuntimeService     FOV selection/default/apply state
└── SLMFeedbackService           measurement/localization/feedback/references
```

`SLMQtSession` owns the corresponding presentation adapters, dialogs, retained
view synchronization, confirmations, plots, and interaction policy.

## Mapping and observation UI

The UI keeps two independent concepts separate:

- **Mapping** is the SLM-to-sample metric context. Changing it changes numerical
  calibration context and may stale/recompute CGH.
- **Observation plane** is the context used to interpret a measurement. It can
  suggest a detector/source and fallback pixel size. Changing it does not change
  Mapping, FOV runtime state, or CGH validity.

Detector/source selection may override the observation plane's preferred source
without silently changing the observation plane.

## Calibration workspace

The section **Calibration** action opens one persistent management workspace with
two domains:

```text
Mapping
    mapping profiles
    base metric calibration
    FOV mapping corrections

Observation & Geometry
    observation-plane definitions
    orientation by section
    observation registration by section
```

### Base mapping calibration

`SLMCalibrationService` owns the headless workflow and active mapping state.
Calibration acquisition explicitly carries mapping, section, observation plane,
source, measurement scale, localization, and candidate state. Observation
information is provenance; it does not become mapping identity.

A measurement-provided `pixel_size_um` is authoritative. A calibration workflow
explicit override is next, and the selected observation plane's default pixel
size is a fallback.

Simple target-localization mapping calibration does not require saved observation
geometry unless the chosen operation actually needs logical point correspondence.

### FOV calibration

`FOVCalibrationService` owns FOV fitting. Accepted records are
`FOVMappingCorrection` objects persisted through `MappingService` under
section × mapping. Observation plane, detector/source, pixel size, and geometry
used during fitting are provenance only.

The current editable-lattice workflow uses genuinely measured localization sites.
The saved correction is compatible only with the numerical base mapping signature
against which it was fitted.

`FOVMappingRuntimeService` separately owns selection/default/application at
runtime. The compact section control only toggles application of the selected
compatible correction; persistent correction management remains in Calibration.

### Observation geometry

`GeometryCalibrationService` is the one reusable calibration workflow.
`ObservationGeometryService` is the authoritative persisted relationship:

```text
SLM serial + section + observation plane -> GeometryOrientationCalibration
```

The same workflow is launchable from the Calibration workspace and contextual
feedback/calibration entry points. Feedback does not maintain a second geometry
calibration implementation.

### Observation registration

Observation registration is a separate continuous measurement-space calibration:

```text
SLM serial + section + observation plane -> native detector frame -> canonical observation frame
```

The Calibration workspace presents it separately from discrete observation
orientation. `ObservationRegistrationCalibrationService` consumes the current
section's accepted feedback localization as the native geometry and a raw named
position reference as the canonical geometry. The reference may be session or
persistent and may originate from another SLM when the application shares the
normal position-reference store across those sessions. The reference is never
resolved/projected through position feedback while calibrating registration, so
the calibration cannot depend on the registration being fitted. Logical sites
are matched directly and affine or polynomial 2/3/4 forward and reverse
transforms are fitted. If the native localization is missing, the workflow
deep-links to the normal feedback localization UI rather than owning another
localizer.

Each observation plane has one explicit canonical frame across the workspace.
The source frame of the selected named reference becomes that canonical frame.
That frame is an implicit identity and does not store a self-registration; every
other saved registration for that plane must agree on the same canonical frame.
Saved compatible registrations are automatically used by named position-reference
resolution; they never alter FOV mapping or CGH generation.

## Measurements and feedback

`CGHSessionWindow` is the contextual Measurements & Corrections workspace. It
presents measurement/localization, intensity feedback, position feedback,
position references, observation geometry status, automatic feedback, and
read-only inspection of the active FOV mapping correction.

Persistent FOV fitting belongs to Calibration, not Feedback.

Position/reference feedback may use inferred lattice sites when localization has
resolved them explicitly. FOV and Geometry fitting continue to use genuinely
measured sites only.

Position references are workspace-wide and observation-plane scoped. They may be
shared across SLM sections and, when sessions use the same reference store,
across SLMs in the same observation plane. `slmcore` performs no
cross-observation-plane coordinate transform.

The compact section-level Position checkbox and the session Position checkbox are
two synchronized presentations of the same runtime `position_active` state.

## Automatic intensity feedback

Automatic feedback is application-owned and sequences the same operations used by
manual feedback:

```text
fresh acquisition
-> reuse compatible localization or localize + commit
-> apply intensity adaptation
-> compute adapted CGH
-> successful frame upload
-> next requested round
```

Only one automatic loop may run per physical SLM. While active, Qt locks the
normal interaction surface and leaves the owning Stop action available.

Automatic feedback is unavailable when automatic frame upload, output upload, or
measurement acquisition capabilities required by the loop are missing.

## CGH execution and correction ordering

CGH scheduling is injectable through the toolkit-independent `CGHExecutor`
contract. With no executor supplied, the Qt composition creates a `QtCGHExecutor`.
Request IDs, stale-result handling, commit semantics, and runtime state remain in
`SLMSession`/core rather than in the executor.

The effective target-position ordering is:

```text
ideal resolved positions
-> optional FOV mapping correction
-> optional transient position-feedback correction
-> final CGH
```

The CGH session therefore retains the active FOV correction layer, but it does not
own FOV persistence or fitting.

## Frame publication

Every accepted frame-changing transition publishes the current frame through
`SLMSession`. `SLMQtSession` mirrors it to the standard preview. With
`auto_upload_frame=True`, the device provider is also asked to upload the frame.
With it disabled, the host can explicitly request upload.

`defer_frame_upload()` can coalesce physical uploads while runtime/Qt state
continues to synchronize normally.

## Interaction policy

`RuntimeViewInteractionSettings` is presentation/session policy and is not part of
`SLMConfig`. Standard parameter and CGH-target edits use retained views and
independent debounce buckets. Explicit flush barriers commit pending drafts before
operations that need authoritative state.

Focused editing is startup/UI preference only; it changes which controls are shown,
not active/inactive runtime state.

The compact Mapping/FOV/Position section controls intentionally remain synchronized
with the larger Calibration/Measurements workspaces rather than creating separate
state.

## Configuration and Fast Config

`SLMConfigStore` owns config persistence and `SLMConfigurationService` owns
prepare/validate/commit behavior. `SLMSession` owns `current_config_path` and
runtime restoration. Qt owns selectors, inspection and interactive mismatch
choices.

Configuration loading follows `prepare -> decide -> commit`. Normal editor loads
reconstruct editable state. Fast Config uses the persisted compiled frame as the
authoritative output until the session returns to editor mode.

Current config schema also carries per-section FOV correction intent. Restoration
checks correction existence and base-mapping compatibility; an unavailable or
incompatible requested correction is surfaced rather than silently substituted.

## Panel and preview composition

`SLMPanel` is the standard reusable one-SLM composition. Its
`SLMPanelLayoutPolicy` controls integrated preview placement. `SLMPreviewView`
remains independently mountable and contains no collapsible/container assumptions.

Preview layout/size is UI state, not SLM configuration or calibration state.

## Host boundary

A host owns adaptation of its own setup format into `SLMDefinition` /
`SLMStartupPreferences`, workspace-root selection, outer multi-SLM composition,
and concrete device/measurement providers. `slmcore` owns the reusable one-SLM
runtime, workspace layout, calibration/feedback workflows, configuration behavior,
and standard Qt composition.

Cross-SLM policies remain host-level. For example, a host may propagate one
`RuntimeViewInteractionSettings` choice across several SLM sessions without making
an individual `SLMPanel` aware of other devices.
