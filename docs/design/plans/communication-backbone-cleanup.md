# Communication Backbone Cleanup Plan

## Goal

Clean up the imcontrol communication backbone without changing hardware
behavior. The current `CommunicationChannel` is a compatibility surface that
mixes manager relays, widget-controller signals, public API helpers, shared
attributes, scripting hooks, and workflow coordination.

## Safety Boundaries

The backbone coordinates red-zone behavior indirectly:

- scan start/stop lifecycle,
- recording-triggered scans,
- laser scan mode transitions,
- event-triggered acquisition,
- rotator synchronized movement,
- stage/galvo center updates.

Cleanup must not remove signals, alter signal signatures, change scan timing,
change laser behavior, or change DAQ/TTL semantics without separate review.

## Phase 0: Contract Baseline

Add a machine-readable signal inventory and tests that fail if signal names,
signatures, duplicate method names, or public API signal aliases change
accidentally.

Status: implemented with `communication_channel_signal_inventory.json` and
`test_communication_channel_contract.py`.

## Phase 1: No-Behavior File Cleanup

Reorganize `CommunicationChannel.py` into explicit ownership sections, remove
obvious duplicate helper code, and keep the legacy public surface intact.

Status: implemented. Signal names and signatures are preserved. The duplicate
`getNumCamTTL()` helper was removed and controller lookup now goes through one
private helper.

## Phase 2: Deprecation Marking

Mark apparently unused signals as deprecated while keeping them available:

- `sigGridToggled`,
- `sigCrosshairToggled`,
- `sigScanFrameFinished`,
- `sigClockWidefield`.

`sigSaveFocus` should stay until external API usage is decided because it is
currently exposed through `api.imcontrol.signals()`.

Status: implemented. Deprecated signals are listed in
`CommunicationChannel.DEPRECATED_SIGNALS` and covered by contract tests. They
remain available as compatibility signals.

## Phase 3: Domain Event Facades

Introduce domain-specific event groups behind the existing compatibility
surface:

- acquisition/image events,
- recording events,
- scan events,
- event-triggered workflow events,
- bead-recognition events,
- rotation events,
- scripting/API events.

Keep existing `commChannel.sigX` names as aliases during the transition.

Status: compatibility aliases implemented. `CommunicationChannel` now exposes
read-only event groups such as `scanEvents`, `recordingEvents`, and
`eventTriggeredEvents` while preserving all legacy `sigX` attributes.

## Phase 4: Workflow Services

Move multi-step choreography out of the global signal bus:

- scan request/response coordination,
- recording-triggered scan coordination,
- ET slow-scan triggering,
- bead-recognition center queries.

Status: started. `ScanWorkflowService` now wraps the scan/recording workflow
signals, and `EtSTEDTriggeredScanRunner` can use this narrow service instead
of the full communication channel while keeping a compatibility adapter for
older callers. EtSTED, EtMonalisa, and RecordingController now route scan
request/start notifications through this workflow service while preserving the
same underlying legacy signals. `BeadRecWorkflowService` now wraps the
bead-recognition and MoNaLISA center-query signals, and the scan/bead
controllers route those interactions through the service.

## Phase 5: Removal Window

Remove deprecated signals only after internal usage, documentation, and public
API compatibility have been reviewed. This requires no-hardware startup tests
and representative widget-set tests.
