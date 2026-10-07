# slmcore architecture

This document describes the post-refactor ownership model used by the current
`slmcore` implementation. It is intended as a stable developer reference rather
than a migration plan.

## Layering

```text
core/          immutable/domain models, runtime state, scientific computation
application/   toolkit-independent workflows and session orchestration
workspace/     persistent stores
setup/         physical SLM definition and startup preferences
host/          capabilities supplied by an embedding application
qt/            Qt presentation/adapters
```

`core/` must not depend upward on application, workspace, host, or Qt. Normal Qt
mutations route through `SLMSession`; Qt owns presentation and interaction, not
authoritative calibration or feedback state.

## Mapping versus observation

A mapping and an observation plane are independent concepts.

### Mapping

A mapping is the SLM-to-sample metric context. `MappingDefinition` contains only
identity/description metadata. Per-section numerical calibration is stored as an
`SLMSectionCalibration` and selected through `SLMCalibrationService`. Mapping
changes may invalidate/recompute CGH because they change metric conversion.

`MappingService` owns the workspace mapping catalog and persistent
`FOVMappingCorrection` records. Deleting a mapping cascades through its base
calibrations, FOV corrections, defaults, and active runtime state.

### Observation plane

An `ObservationPlaneDefinition` is workspace-level measurement context. It may
contain a preferred detector and a fallback `default_pixel_size_um`. Feedback and
calibration workflows choose observation planes independently; detector/source
overrides do not silently change the selected observation plane.

`ImageMeasurement.pixel_size_um` is authoritative when provided. A workflow-level
explicit override is next, then the observation-plane default is a fallback.

Changing observation plane must not change mapping, FOV selection/application,
or CGH validity.

## Observation geometry

Saved geometry has scope:

```text
SLM serial + section + observation plane
```

`ObservationGeometryService` owns access to those persisted records.
`GeometryCalibrationService` owns the reusable calibration workflow. Feedback is
a consumer/frontend and does not implement a second geometry-calibration engine.

Geometry is required only for workflows that need logical point correspondence.
A manually saved orientation is persisted through the same authoritative geometry
service.

## FOV correction

`FOVMappingCorrection` has scope:

```text
SLM serial + section + mapping + correction name
```

It is fitted by `FOVCalibrationService`, persisted through `MappingService`, and
selected/applied by `FOVMappingRuntimeService`. Observation plane, detector,
pixel size, and geometry used during fitting are provenance rather than validity
keys. Compatibility is determined from the numerical base-mapping signature.

The CGH session retains only the active correction layer because application of
the correction is part of target resolution:

```text
ideal positions -> FOV mapping correction -> transient position correction -> CGH
```

The historical internal CGH-signature payload key `fov_position_calibration` is
retained intentionally for signature compatibility; it is not a public ownership
concept.

## Feedback

`SLMFeedbackService` owns transient measurement/localization and feedback use
cases: intensity adaptation, position correction, position/session references,
and automatic feedback rounds.

Position/reference feedback may use inferred lattice sites when the localization
workflow explicitly resolves them. FOV and Geometry fitting use genuinely
measured sites only.

Position references are workspace-wide, keyed by observation plane, and store
full detector coordinates plus logical lattice indices. No cross-observation-plane
transform is performed inside slmcore.

## Persistence

Default workspace resources are:

```text
<workspace>/
├── mappings.json
├── observation_planes.json
├── configs/<serial>/
├── corrections/<serial>/
├── calibrations/<serial>/<section>/<mapping>.json
├── fov_mapping_corrections/<serial>/<section>/<mapping>/<correction>.json
├── geometry_orientation_calibrations/<serial>/<section>/<observation-plane>.json
└── position_references/<observation-plane>/<reference>.json
```

Setup/startup preferences are separate from the workspace. They contain startup
configuration, default mappings, mapping-scoped default FOV correction names,
feedback-orientation fallbacks, focused-editing preferences, and section display
mode.

## Calibration workspace

The Qt Calibration workspace is the canonical persistent-management surface. It
separates:

- Mapping: mapping profiles, base calibration and FOV corrections.
- Observation & Geometry: observation-plane definitions and section-specific
  observation geometry.

Base mapping calibration, FOV calibration, and geometry calibration reuse the
same measurement/localization infrastructure but have separate application
services and state. Feedback remains a contextual measurement/feedback workspace,
not the persistence owner for FOV or geometry calibration.

## Current numerical scope

The stored base mapping includes a 2×2 representation, but ordinary Phase-1
parameter conversion is intentionally axis-aligned/diagonal. Rotation, shear and
field distortion remain represented by the FOV mapping correction where
applicable. Full off-diagonal grouped/vector conversion is a future extension.
