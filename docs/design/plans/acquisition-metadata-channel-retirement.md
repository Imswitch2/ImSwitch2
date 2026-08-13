# Acquisition metadata channel retirement

**Status:** Active — P1 and P2 implemented; P3/P4 need a release gate
**Date:** 2026-08-12
**Scope:** The metadata channels crossing the ImControl/ImProcess boundary
**Depends on:** [acquisition layout contract](acquisition-layout-contract.md) (PR 1–7 implemented)

---

## 1. Goal

One interpretation of acquisition semantics across the boundary.

The layout contract made `AcquisitionLayout` authoritative *when present*. It
did not remove anything: every legacy channel is still written by ImControl,
and six ImProcess modules outside the resolver still parse those channels
themselves. A file can therefore still be described twice, and the two
descriptions can still disagree — the resolver just wins when it is consulted.

This plan finishes the job. It is deliberately narrow: it retires **duplicate
interpretation**, not compatibility. Old recordings must keep opening forever.

## 2. What actually crosses the boundary

Measured read counts in ImProcess (excluding tests):

| Category | Channels | Retire? |
|---|---|---|
| **A. Acquisition semantics** | `ScanStage:axis_length` (25), `axis_startpos` (22), `axis_step_size` (21), `axis_step_size_unit` (8), `positive_direction` (6), `target_device` (4); `ScanTTL:Nx` (9), `Ny` (8), `linestep_enable` (6), `n_linesteps` (4), `sequence_time` (2); `MS-RESOLFT_Scan:cycleSteps` (7), `roSteps` (4), `cycleStepSizeUm` (4); `recording:num_timepoints` (25), `frames_per_stack` (18), `expected_frames` (11); `Rec:LapseTime` (12) | **Yes** — the layout expresses all of it |
| **B. Writer lifecycle** | `writing`, `stream_complete`, `recording:frames_committed`, `completion_outcome`, `planned_frames`, `actual_frames`, `planned_partitions`, `actual_partitions` | **No** — a different question (is the file finished), already normalized in PR 2 |
| **C. Source identity** | `recording:detector_name`, `dataset_path`, `source_format`, `scan_source`, `lapse_interval_s`, `planned_start_time` | **No** — provenance, not axis meaning |
| **D. Interoperability** | OME axes, `ome:PhysicalSizeX/Y`, `ome:DimensionOrder`, `element_size_um` | **Never** — these exist for Fiji/OMERO, not for ImSwitch |

`recording:single_lapse_file` and `lapse_index` straddle A and C: the layout's
`AcquisitionPartition` carries both the storage mode and the index, so they
retire with A.

## 3. This is mostly not a schema change

Calling it "schema v2" was wrong, and the distinction matters for sequencing.
Retiring companion attributes does not change `imswitch.acquisition-layout/1`
— the layout already expresses everything in category A. The work splits into
three acts with very different risk:

1. **Stop re-interpreting** the channels in ImProcess. No schema change, no
   file-format change, no release gate. This is the actual "smooth interface"
   win and it can start immediately.
2. **Stop writing** them from ImControl. Breaks external scripts and older
   ImSwitch installs that read those attributes. Needs a deprecation window.
3. **Delete the writers.** Needs a release boundary.

Only one thing needs the schema: see §5.

## 4. What must never be retired

- **Category D.** OME axes and Fiji pixel-size attributes are an export
  contract with other software. §3.4 of the contract already fixes them for
  v1; that is permanent, not transitional.
- **The legacy adapters.** Every recording made before the layout existed is
  read through them. Retirement removes *duplicate* readers, never the
  adapters themselves. `acquisition_layout_resolver.py` keeps its
  `ScanStage`/`ScanTTL`/`MS-RESOLFT`/tiling adapters forever.
- **Category B.** Writer lifecycle is orthogonal to acquisition meaning and
  was already unified once; touching it again buys nothing.

## 5. What the layout cannot yet express

One genuine gap, found by comparing the channels against `AcquisitionLoop`:

- **Device identity per loop.** `ScanStage:target_device` names the positioner
  that drove each axis. The resolver uses it to decide whether an axis is X, Y
  or Z, so the *semantics* survive as `loop.kind` — but the device name itself
  is dropped. That is provenance a rig engineer wants when a scan looks wrong.

  Fix: an optional `device: str | None` on `AcquisitionLoop`. Purely additive,
  defaults to `None`, does not change the meaning of any existing field. It
  can ship in v1 under the existing "open vocabulary, additive fields" rule,
  or as v1.1 if a version marker is wanted.

Everything else in category A maps onto existing fields: counts to
`loop.count`, pitches to `loop.step`/`loop.unit`, directions to
`loop.direction`, line steps to a `condition` loop, detector gating to
`recorded_event_spans`, timepoints to a `time` loop or an
`AcquisitionPartition`, lapse storage to `partition.storage`/`index`.

## 6. Phases

### P1 — Confine interpretation to the resolver

No schema change, no format change, independently shippable.

Six modules parse category-A channels outside the resolver:

| Module | What it re-parses |
|---|---|
| `improcess/controller/MoNaLISAController.py` | `ScanStage:target_device`, `ScanTTL:Nx/Ny/n_linesteps` → scan dialog values |
| `improcess/controller/LiveModeController.py` | completion/geometry keys |
| `improcess/live/sources.py` | `ScanTTL:Nx/Ny`, `recording:frames_per_stack` → live stack geometry |
| `improcess/reconstructors/snouty/metadata.py` | `MS-RESOLFT_Scan:cycleSteps/roSteps` → widget params |
| `improcess/reconstructors/monalisa/live_session.py` | `_scan_steps_from_ttl`/`_from_size`/`_from_endpoint` |
| `improcess/reconstructors/monalisa/reconstructor.py` | `_fast_gauss_geometry_from_attrs` |

Each becomes a call to the resolver. Where a module needs a value the resolver
does not surface, add it to `ResolvedAcquisitionLayout` rather than re-parsing.

**Acceptance (corrected).** The criterion first written here -- "a grep for
category-A keys returns the resolver only" -- is not achievable and should not
be. Those reads are the compatibility fallbacks for sources the resolver cannot
describe, and §4 says the legacy path is permanent. Demanding their absence
would mean dropping support for files that predate the contract.

The property that actually matters is **ordering**: every module consults the
resolver first, and reaches its own parse only when the resolver declines. That
is what stops two readers disagreeing about the same file, which is the failure
mode P1 exists to remove. Retiring the fallbacks themselves belongs to P3/P4,
once ImControl stops writing the channels they read.

**Progress.** Group 1 (the parsers that can disagree with the resolver about
the same file) is done: `live/sources.py` (`209e3364`), and
`monalisa/live_session.py` + `monalisa/reconstructor.py`. Each now asks the
resolver first and keeps its own ladder only for sources the resolver cannot
describe.

Two things this surfaced, both fixed:

- `live/sources.py` counted scan positions with `ceil` where ImControl
  unified on `round`. A 0.52 um axis at 0.05 um is 10 positions to the
  producer and was 11 to the reader, so a 100-frame stack was read as needing
  121 and never completed.
- Confining interpretation is only safe if the resolver is at least as capable
  as what it replaces. Every legacy adapter required `ScanTTL` keys, so a
  stage-geometry-only file resolved to a bare frame stream and routing the
  live sources through the resolver would have *lost* geometry they could
  read. `adapt_scan_stage_metadata` closes that, registered last so any
  modality adapter still wins, and declining rather than raising when the
  geometry cannot explain the frame count -- nothing in such a file claims to
  be a scan.
  A later sanity check tightened it further: stage extents describe what
  was *configured*, so a timelapse that never moved the stage still
  carries them and its frame count can coincide with the configured
  position product. It now declines when the file also says it holds
  several timepoints, and reports `medium` rather than `high` confidence,
  being the least specific adapter in the chain.

Group 2, the UI pre-fill pair, is also done. `scan_params_from_layout` maps a
resolved layout onto MoNaLISA's scan-dialog values, and the SNOUTY parameter
widget takes its cycle/plane counts from the resolver when it is handed the
source. The controller now passes the `DataObj` to `load_from_attrs`, falling
back to the attrs-only signature so the duck-typed hook keeps working.

That surfaced a third instance of the square-raster guess: the MoNaLISA dialog
was pre-filled with `sqrt(numFrames)` on both axes, which is 25x25 for the
motivating 648-frame scan -- the same wrong shape BeadRec used to invent,
reached by a different route. A scan whose geometry nothing records now leaves
the dialog alone rather than inventing one.

`LiveModeController` is **reclassified**: it reads `recording:num_timepoints`
to know when a lapse has finished, which is category B (lifecycle), not
acquisition semantics. §4 says category B stays, so it is out of scope rather
than outstanding.

P1 is therefore complete: every module consults the resolver first.

### P2 — Add device identity, derive-and-verify — **done**

- `AcquisitionLoop.device` carries the positioner behind each axis. Producers
  populate it through `scan_devices()`, which reuses the axis resolution
  `scan_directions()` already performed and used to discard; the legacy
  adapters recover it from `ScanStage:target_device`, so files that predate
  the field gain it on resolution.
- `RecordingManager.__reportLayoutDisagreements` compares the layout with the
  legacy attributes written beside it and logs where they differ. It does not
  fail the recording: the legacy attributes are on their way out and are not
  worth blocking a measurement over, and the layout is already authoritative.

**Acceptance met.** Regressions cover the device surviving both the producer
path and serialization, the legacy adapter recovering it, and a deliberate
Nx disagreement being reported without stopping the measurement.

**Forward compatibility (fixed).** Adding a field made the contract's gap
concrete: the decoder rejected unknown fields, so a file written with `device`
could not be read by an ImSwitch that predates it. Both halves are now in the
contract (§3.4): unknown fields within the same schema version are ignored and
reported as `UNKNOWN_LAYOUT_FIELD` warnings, and a different schema version is
refused with `UNSUPPORTED_SCHEMA_VERSION` rather than half-understood.

### P3 — Stop writing category A, behind a switch

- A setup-level `writeLegacyScanMetadata` flag, defaulting to **true**.
- Flipping it off writes only the layout plus categories B/C/D.
- Ship one release with the flag available and documented, so rigs with
  external scripts can migrate.

**Acceptance:** with the flag off, every reconstructor still works end to end
(the §P1 seam tests cover this), and Fiji/OMERO round trips are unchanged
because category D is untouched.

### P4 — Default off, then delete

- Flip the default at a release boundary.
- One release later, delete the writing code and the flag.
- The legacy adapters stay.

**Acceptance:** ImControl writes one description of acquisition semantics.
Files written before the change still open, through the adapters, with visible
confidence.

## 7. Risks

| Risk | Mitigation |
|---|---|
| External scripts read `ScanStage:*`/`ScanTTL:*` directly | P3's flag plus a release of overlap; the keys are documented as deprecated before they stop being written |
| A producer's layout silently disagrees with its legacy attributes | That is exactly what P2's derive-and-verify is for, and it runs before anything is removed |
| P1 changes behaviour while claiming to be a refactor | Each module moves to the resolver in its own commit with its existing tests unchanged; the seam tests already pin producer→reconstruction behaviour |
| Retiring `recording:frames_per_stack` breaks live sources mid-acquisition | Live geometry comes from the layout after P1; `frames_committed` (category B) remains the streaming barrier and is untouched |
| "Retirement" is read as dropping old files | Stated in §1 and §4: adapters are permanent; only duplicate interpretation goes |

## 8. Definition of done

- No ImProcess module outside the resolver interprets a category-A channel.
- `AcquisitionLoop` carries the driving device, so nothing in category A is
  information the layout cannot hold.
- ImControl writes the layout, lifecycle, source identity and the OME/Fiji
  export attributes — and nothing that restates the layout.
- Recordings predating the contract still open, through named adapters, with
  their confidence and assumptions visible.
- The imcontrol/improcess boundary carries exactly one answer to "what does
  this frame mean".

---

## Appendix: why P1 is the valuable half

P1 needs no schema change, no flag, no release gate, and it removes the
failure mode that motivated the whole effort: two pieces of code reading the
same file and disagreeing. P2–P4 remove *storage* duplication, which is
tidiness. If this plan is only half-executed, execute P1.
