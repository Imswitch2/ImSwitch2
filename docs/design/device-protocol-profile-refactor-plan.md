# Device Protocol Profile Refactor Plan

## Status

Draft for discussion.

## Motivation

Several ImSwitch hardware managers mix the ImSwitch-facing API, device safety
logic, firmware detection, command translation, response parsing, transport
handling, and simulation fallback in one class. This makes vendor command
changes difficult to understand and risky to update.

The immediate examples are:

- `Cobolt0601NewLaserManager`, which supports several partially overlapping
  Cobolt command dialects and an OEM-specific emission workaround.
- `AAAOTFLaserManager`, which embeds AA Opto-Electronic command strings
  directly in a per-channel laser manager.

The goal is to isolate vendor command dialects without introducing a large
generic driver framework or many small files.

## Agreed design

Each device family will have:

1. One master manager that implements the ImSwitch API, configuration, device
   lifecycle, safety sequencing, state tracking, logging, and simulation
   policy.
2. One small file per supported protocol profile. A profile translates named
   device operations into vendor commands and validates the corresponding
   replies.
3. Optionally, one discovery file when automatic profile selection is too
   complex to keep in the master manager.

There will be no separate files for configuration models, transport wrappers,
state machines, or capability objects unless a concrete need emerges during
implementation. The one deliberate shared support file is a small
`_protocol.py` containing only the cross-vendor exception vocabulary,
`ProbeResult`, and the minimal profile-selection contract. This is shared
vocabulary, not a driver framework.

The first implementation will not introduce a generic framework shared by all
hardware managers. A small amount of duplication between Cobolt and AA Opto is
preferable to an abstraction that has not yet been proven useful.

## Proposed layout

```text
imswitch/imcontrol/model/managers/lasers/
├── _protocol.py                    # shared contract, result, exceptions
├── Cobolt0601NewLaserManager.py
├── cobolt0601_protocols/
│   ├── __init__.py
│   ├── legacy.py
│   ├── scpi_compatible.py
│   ├── mixed.py              # only if hardware evidence requires it
│   └── discovery.py          # optional
├── AAAOTFLaserManager.py
└── aa_aotf_protocols/
    ├── __init__.py
    ├── <profile_name>.py
    └── discovery.py          # optional
```

`_protocol.py` is expected to remain small and vendor-independent. It does not
open hardware, send commands, implement a base manager, or define device
capabilities.

Each vendor package's `__init__.py` may contain its profile registry and one
vendor-local command/reply adapter. This avoids duplicating the same Cobolt
empty/error classification in every Cobolt profile without creating another
file. Profile names should describe observed compatible behavior rather than
an assumed firmware range.

## Responsibility boundaries

### Master manager

The master manager remains the only class visible to the ImSwitch manager
loader. It owns:

- parsing `managerProperties`;
- opening and closing the existing serial/RS232 implementation;
- choosing or discovering the protocol profile;
- the safe initialization sequence;
- enable, disable, scan-mode, and shutdown sequencing;
- cached ImSwitch state and setpoints;
- range validation;
- the explicit master-versus-pause emission policy;
- device identity and health reporting;
- explicit simulation behavior.

The master manager must not construct raw vendor command strings.

### Protocol profile

Each protocol profile is a small class that implements the operations required
by the master manager. The following surface is illustrative; it must be
finalized against the existing safe-state and scan sequences before command
strings are moved:

```python
class CoboltProtocolProfile:
    profile_id = "cobolt.legacy"
    auto_selectable = True

    def probe(self, connection): ...
    def read_identity(self, connection): ...
    def disable_autostart(self, connection): ...
    def master_off(self, connection): ...
    def master_on(self, connection): ...
    def pause(self, connection): ...
    def resume(self, connection): ...
    def set_constant_power_mw(self, connection, value): ...
    def enter_constant_power(self, connection): ...
    def set_modulation_power_mw(self, connection, value): ...
    def enter_modulation_mode(self, connection): ...
    def set_digital_modulation_enabled(self, connection, enabled): ...
```

An inheritance-based abstract base class is unnecessary. The typing-only
selection contract in `_protocol.py` covers `profile_id`, `auto_selectable`,
and the read-only `probe` surface; vendor-specific operations remain
duck-typed while there are only a few profiles.

### Profile success and failure contract

The manager's fail-closed behavior depends on an explicit profile contract:

- A mutating profile method returns `None` only after receiving the
  profile-specific success response.
- A query returns its parsed value, never an unclassified raw reply.
- A read-only probe returns a small `ProbeResult` describing positive,
  negative, and supporting evidence.
- An explicit vendor rejection raises `CommandRejected`.
- A timeout or connection problem raises `TransportFailure` or its
  `CommandTimeout` subtype.
- An empty or structurally invalid response raises `UnexpectedReply`.
- An operation unavailable in the selected profile raises
  `UnsupportedOperation`.

These exception types, `ProbeResult`, and the minimal selection contract live
in `lasers/_protocol.py`. Both the current monolithic managers and later vendor
profiles import the same vocabulary, allowing the Phase 1A safety fixes to land
before profile packages exist.

For the initial AA compatibility profile, a completed transport exchange is
the defined success condition and returned text is intentionally not
classified. Tightening that policy is the separate evidence-backed step
described below.

The master manager updates its cached state only after all required profile
calls return successfully. Command alternatives are resolved during discovery
or validation, before a committed enable, disable, scan, or safe-state
sequence starts.

An explicitly rejected command is known not to have executed, so discovery or
validation may safely consider a documented alternative. A timeout is
different: the command may have executed even though its reply was lost, so an
alternative must never be attempted after `TransportFailure`,
`CommandTimeout`, or `UnexpectedReply`.

Once a committed safety sequence has started, any exception aborts the
sequence and invokes its defined safe-state recovery. The manager does not hunt
for command variants in the middle of that sequence.

#### Capability validation at startup

`UnsupportedOperation` must be surfaced during initialization, not on first
use. Configuration and profile can be individually valid yet mutually
incompatible: `emissionControl: "pause"` requires `pause` and `resume`, which a
legacy profile does not provide.

After the profile is selected, the manager verifies that the profile implements
every operation its configured policy requires, and fails initialization with
one actionable error naming the profile, the policy, and the missing operation.
Discovering that incompatibility at the first GUI enable would surface it as a
failed emission transition instead of a configuration error.

#### Standalone operations

A committed sequence is any multi-command transition that must complete or fall
back to a safe state: safe-state init, enable, disable, and scan-mode
transitions.

A single setpoint write outside such a transition, for example flushing a new
power value while the laser is already enabled, is a standalone operation. It
may use a documented alternative after `CommandRejected`, because a rejected
command is known not to have executed. It must not retry after
`TransportFailure`, `CommandTimeout`, or `UnexpectedReply`, and a failed
standalone write must not silently leave the cached setpoint claiming a value
the device never accepted.

Profiles own:

- raw command strings;
- command-specific units and formatting;
- operation-specific success/value parsing;
- profile-specific read-only probes;
- profile-specific command alternatives when they are known to be safe.

Profiles do not own ImSwitch configuration, GUI state, scan lifecycle, or
high-level laser safety sequencing.

A profile is a verified set of compatible operations, not necessarily one
clean vendor dialect. Current SCPI-mode code deliberately falls back to some
short-form commands. Hardware transcripts may therefore justify a mixed
profile that uses SCPI for one operation and legacy syntax for another. Phase 2
must not assume that all deployed devices divide cleanly into pure legacy and
pure SCPI profiles.

### Vendor-local connection and reply handling

Profiles deliberately target the existing connection surface for their device
family:

- Cobolt profiles receive the current `Cobolt06`-like object and ultimately use
  `send_cmd(str) -> str`.
- AA profiles receive the selected `RS232Manager` and ultimately use
  `query(str) -> str`.

This coupling is vendor-local and intentional. There is no new generic
transport wrapper.

Each vendor package translates its connection exceptions into the shared
`_protocol.py` taxonomy in one place:

- `cobolt0601_protocols.__init__` provides the common Cobolt send/reply helper.
  It calls `send_cmd`, classifies empty replies and common rejection strings,
  and maps connection exceptions to `CommandTimeout` or `TransportFailure`.
- `aa_aotf_protocols.__init__` provides the AA query helper. Initially it maps
  transport exceptions but deliberately accepts any completed reply text to
  preserve current behavior.

Profiles call the appropriate package helper. They do not independently
reimplement common empty/rejection detection. They remain responsible for
parsing operation-specific values, such as converting a returned power value
from watts to milliwatts.

### Optional discovery helper

The discovery helper coordinates profile probes and returns a selected profile.
It does not perform normal device operations.

For a device family with only one profile, or with trivial selection, the
master manager may select the profile directly and no discovery file is
needed.

## Configuration

Use `protocolProfile`, not `firmware`, as the configuration selector:

```json
{
    "managerName": "Cobolt0601NewLaserManager",
    "managerProperties": {
        "digitalPorts": ["COM7"],
        "protocolProfile": "auto",
        "emissionControl": "master",
        "simulation": false
    }
}
```

An explicit selection could be:

```json
{
    "protocolProfile": "cobolt.scpi-compatible"
}
```

The exact profile names will be finalized after the known hardware has been
inventoried. Once a `profile_id` has shipped in setup JSON it is a stable public
identifier. A later rename requires retaining the old identifier as an alias.

The concepts remain separate:

- `firmware`: observed device metadata;
- `protocolProfile`: selected command dialect, formatting, units, and parsing;
- `emissionControl`: explicit safety policy such as `master` or `pause`;
- `simulation`: explicit request to use simulated hardware.

`protocolProfile: "auto"` means discover the command compatibility profile.
The existing `emissionControl: "auto"` is unrelated: it is a diagnostic mode
that currently resolves to master control. This distinction must be explicit in
the configuration documentation.

Units are defined per profile operation: for example, a profile can format its
constant-power command in watts and its modulation-power command in
milliwatts. A separate `scpiPowerUnit` override may be retained temporarily for
compatibility, but it should eventually map to a verified profile because an
incorrect unit can produce a 1000-fold setpoint error.

### Simulation failure behavior

When the `simulation` key is absent, it defaults to `false`.

When `simulation` is `true`, the manager creates the mock directly and does not
attempt to open the physical port.

When `simulation` is `false`, failure to open the port, validate the selected
profile, or establish the safe state raises an actionable device initialization
error. It must not substitute a mock. The error should identify the device and
port and end with guidance equivalent to:

```text
Check the configured port and device power. To run without hardware, set
managerProperties.simulation to true.
```

If an existing development workflow proves to require automatic mock fallback,
it may temporarily use an explicitly named deprecated compatibility option. It
must not remain the default.

This is an intentional migration change. Existing setup files do not contain a
`simulation` key, so hardware-less development machines and CI jobs that
currently receive an automatic mock will instead fail initialization. Those
workflows must add `"simulation": true` to the applicable manager
configuration or inject a mock connection in their tests. Real hardware setups
do not need to add the key.

#### Scope: Cobolt only

This requirement applies to the Cobolt manager, whose mock substitution is
implemented in the manager itself.

AA is different. `AAAOTFLaserManager` contains no mock fallback; its silent
substitution lives in the shared `RS232Manager._getRS232port`, which replaces
any failed port with `MockRS232Driver` for every RS232 device in ImSwitch.
Making AA simulation explicit therefore means changing shared transport
behavior for unrelated devices, which this work does not do. AA keeps its
current transport behavior, and the RS232-wide mock policy is recorded as a
separate follow-up with its own migration impact.

#### Known impact on shipped example setups

`imswitch/imcontrol/_test/ui/test_example_setups.py` builds a full view and
controller for every file in `imcontrol_setups`, which constructs real laser
managers. `example_kiralux_teensy.json` declares two Cobolt lasers on `COM17`
and `COM4` with no `simulation` key, so on any machine without that hardware
these managers currently load through the silent mock path and the test passes.

Removing silent mock substitution breaks that test unless the example setups
are migrated. Phase 1A therefore adds `"simulation": true` to the Cobolt
entries in `example_kiralux_teensy.json`. Example setups cannot own the COM
ports of any particular rig, so declaring them as simulated is the honest
default; a user adapting the example for real hardware sets it to `false`.

## Profile selection

### Omitted profile compatibility contract

`protocolProfile` is a new key, so omission is the primary upgrade path for the
entire installed base. An omitted key must reproduce the current manager's
selection behavior without requiring users to edit existing setup files:

- Cobolt omission runs automatic discovery with the current positive-SCPI-
  over-legacy precedence.
- AA omission selects the current command behavior as its stable compatibility
  profile; it does not pretend to auto-discover an unidentified controller.

An explicit `protocolProfile` opts into a pinned profile. Explicit
`protocolProfile: "auto"` requests discovery where the device family supports
it. These omitted-key defaults are compatibility guarantees and require
regression tests.

### Explicit profile

When `protocolProfile` is specified:

1. Load that profile directly.
2. Run a short, read-only validation.
3. Reject the connection if the validation clearly contradicts the requested
   profile.
4. Do not silently switch to a different profile.

### Automatic profile

When `protocolProfile` is `auto`:

1. Read model, serial number, and firmware when available.
2. Run bounded, read-only probes for each candidate profile.
3. Treat explicit command rejection differently from timeout, disconnection,
   and malformed replies.
4. Apply the device family's documented precedence to expected overlaps.
5. Fail with one actionable error when no profile matches, signals are
   contradictory, or overlap cannot be resolved by documented precedence.

For Cobolt, a positive SCPI signature takes precedence over legacy query
responses, preserving the current behavior. Modern controllers may retain
short-form queries such as `l?`, so SCPI and legacy probe overlap is expected
and is not itself an error. A hard failure is reserved for contradictory or
genuinely unresolved evidence.

A mixed profile may differ from an SCPI-compatible profile only for mutating
operations that cannot safely be probed. Such a profile is
`auto_selectable = False` and must be configured explicitly unless a verified
read-only identity fingerprint can distinguish it. Automatic discovery must
not claim to identify behavior it cannot safely observe.

New AA setups should specify the profile explicitly even though omission
remains supported for compatibility.

Firmware is logged as part of the device fingerprint, but is not the primary
selector unless a firmware-to-profile mapping has been verified on real
hardware.

Expected failures from read-only probes are debug information, not warnings.
Identity should be read only once. While `PyCoboltManager` performs identity
queries during connection, discovery should reuse those stored values rather
than immediately sending the same queries again.

### Command alternatives

Command variants should be resolved during discovery or profile validation
where possible. Normal mutating operations should use the resolved command,
not try several alternatives at runtime.

During discovery or validation, a fallback command may be attempted only after
an explicit unsupported-command reply. It must not be attempted after a timeout
or ambiguous transport failure, because the first command may already have
reached the device. After operational sequencing begins, a rejection also
aborts to the defined safe state rather than triggering mid-sequence fallback.

Until the hardware inventory is complete, the current cross-dialect alternatives
must be treated as evidence of possible mixed profiles rather than cleaned up
into an assumed pure-SCPI design.

## Cobolt scope

The existing `Cobolt0601NewLaserManager` already contains important behavior
that must be preserved:

- fail-closed enable and disable transitions;
- cached power while disabled;
- zero-power handling during scans;
- SCPI and legacy command support;
- stable command resolution, so known losing variants are not retried on every
  operation;
- the explicit OEM `pause` emission strategy;
- safe finalization intent;
- identity and selected-profile reporting.

The refactor will:

- move command construction and parsing into the profile files selected by the
  hardware inventory, including a mixed profile if required;
- keep safety sequencing and the master/pause policy in the master manager;
- replace `_scpi` branches with calls through the selected profile;
- preserve the existing SCPI-over-legacy discovery precedence;
- stop defaulting to legacy when all probes fail;
- distinguish command rejection from transport failure;
- ensure empty and unexpected replies are not treated as success;
- make mock use explicit rather than a response to any initialization error;
- close the serial connection during finalization;
- consolidate the duplicate identity queries currently performed by
  `PyCoboltManager` and the manager;
- report the resolved `profile_id` from `getFirmwareInfo()` in place of the
  current `commandSet` string;
- hardware-review whether `l0` or `las:paus 1` should be the first mutating
  startup command.

`getFirmwareInfo()` has no production consumers; it is referenced only by the
manager and its unit test, so its reported keys can be changed together with
that test. The identity, emission-policy, and selected-profile fields remain
reported.

Setpoint range validation is new behavior, not an extraction. `setValue`
currently accepts any numeric value and sends it. The validation must therefore
be introduced deliberately, with an explicit decision between clamping to the
configured `valueRangeMin`/`valueRangeMax` and rejecting out-of-range values,
and with the chosen behavior pinned by a test. Clamping silently changes a
requested power; rejecting can break a script that previously worked. This
choice needs the same hardware review as the safe-state ordering question.

`PyCoboltManager` may continue to provide the serial connection initially, but
the manager and profiles treat it as a dumb connection: they may use
`send_cmd`, stored identity fields, and disconnect/close behavior only. They
must not call its high-level helpers such as `constant_power`,
`set_power`, modulation-mode methods, or pause/resume methods.

Those high-level helpers and model-regex subclass classification are frozen:
no new command logic is added there, and they are deprecated pending removal.
The profile files are the only source of truth for commands used by the
ImSwitch manager.

## AA AOTF scope

The AA AOTF refactor will:

- keep `AAAOTFLaserManager` as the ImSwitch-facing per-channel manager;
- move all `L<channel>...` command formatting and reply handling into profile
  files;
- keep LUT loading and ImSwitch value conversion in the master manager;
- add profile selection only where reliable read-only probes exist;
- require an explicit profile when the controller cannot be identified
  reliably;
- validate channel numbers and values;
- preserve the current internal/external and TTL behavior through regression
  tests.

AA is primarily a command-extraction cleanup, not necessarily an automatic
discovery system. If no reliable read-only identity or dialect probe exists,
`discovery.py` will not be created and the current command behavior becomes one
explicit, stable compatibility profile. Additional profiles are added only
when supported by evidence.

AA reply handling will be tightened in two steps. The extraction step first
pins and preserves the current behavior: a completed `query()` is considered
successful regardless of its returned text, while transport exceptions still
propagate. The reply is available for debug recording but does not newly reject
a previously working controller.

Profile-specific acknowledgement and error validation will be added only in a
separate, explicitly reviewed change backed by a manual or captured hardware
transcripts. A compatibility profile may intentionally retain permissive reply
handling if no reliable acknowledgement contract exists.

The current shared `rs232sManager` connection will continue to be used. A new
general session registry is explicitly out of scope. If repeated discovery for
multiple channels becomes a measurable problem, a small cache keyed by
`rs232device` can be added later.

AA inherits silent mock substitution from that shared transport rather than
from its own manager: `RS232Manager._getRS232port` replaces any port that fails
to initialize with `MockRS232Driver`. The explicit-simulation requirement in
this plan therefore does not apply to AA, because satisfying it would change
behavior for every RS232 device. This is recorded as a known gap, not as
something AA has already achieved.

## Testing

Tests remain grouped by device family rather than split into one file per
profile.

### Cobolt

Preserve the existing tests in:

```text
imswitch/imcontrol/_test/unit/test_cobolt0601_new_laser_manager.py
```

Add coverage for:

- omitted `protocolProfile` preserves current Cobolt discovery behavior;
- explicit profile selection and validation;
- no matching profile;
- expected SCPI/legacy overlap resolves using documented precedence;
- contradictory or genuinely unresolved profile evidence;
- an explicit-only mixed profile is not selected automatically;
- empty reply;
- generic error and permission-denied replies;
- timeout does not trigger command fallback;
- omitted `simulation` behaves as `false`;
- mock use only when explicitly configured;
- `emissionControl: "pause"` against a profile without pause/resume fails at
  startup with a configuration error;
- a failed standalone setpoint write does not leave the cached setpoint
  claiming an unaccepted value;
- serial connection closed on finalization;
- setpoint range validation, pinning the chosen clamp-or-reject behavior;
- approved startup safe-state ordering.

Add direct tests for the Cobolt vendor adapter itself:

- a transport timeout maps to `CommandTimeout`;
- another connection error maps to `TransportFailure`;
- an empty reply maps to `UnexpectedReply`;
- explicit vendor rejection maps to `CommandRejected`.

Existing command-sequence tests should remain unchanged except where an
intentional, hardware-approved safety correction requires a new sequence.

### AA AOTF

Add one test module:

```text
imswitch/imcontrol/_test/unit/test_aa_aotf_laser_manager.py
```

Cover:

- omitted `protocolProfile` selects the current compatibility profile;
- profile selection;
- channel enable and disable;
- power commands;
- internal/external control;
- TTL and scan-mode transitions;
- calibration conversion;
- invalid channels and values;
- returned reply text is ignored during the compatibility-preserving extraction;
- transport failures still propagate;
- expected command sequences for every supported profile.

Strict acknowledgement, rejection, empty-reply, and malformed-reply cases are
added with the later profile-validation change once the real reply contract is
known.

## Implementation sequence

### Phase 1A — Hardware-independent safety fixes

These changes can land without waiting for rig access:

1. Create the small shared `_protocol.py` vocabulary.
2. Add a standalone Cobolt reply-classifier function beside the current
   monolithic manager, importing the shared `_protocol.py` types.
3. Make empty Cobolt replies fail through that classifier.
4. Distinguish command rejection, timeout, transport failure, and malformed
   replies.
5. Permit command fallback only after explicit rejection.
6. Define the absent/false simulation initialization failure path and remove
   silent mock substitution.
7. Close the physical connection during finalization.
8. Add `"simulation": true` to the Cobolt entries in
   `example_kiralux_teensy.json` so the example-setup UI test keeps passing on
   machines without that hardware.
9. Extend the Cobolt test harness for the new failure modes.
10. Add regression tests for the five behavior changes in steps 3 to 7.

The classifier is intentionally written as a standalone function rather than
new manager logic. Phase 2 moves that function into
`cobolt0601_protocols.__init__` with minimal mechanical changes instead of
reimplementing it.

Step 9 is the most easily underestimated item. The existing harness builds the
manager through `object.__new__` with injected attributes and drives a
`FakeLaser` that expresses every failure as returned error text. The new
taxonomy cannot be exercised through that surface alone: the fake must also be
able to raise transport errors and return an empty string, and the builder must
carry the simulation and initialization paths that step 6 introduces. Budget
harness work alongside the safety fixes rather than after them.

### Phase 1B — Hardware inventory and transcripts

1. Inventory the known Cobolt and AA Opto devices.
2. Record model, firmware, serial family, working command dialect, units,
   emission policy, and date tested.
3. Capture redacted command/reply transcripts from representative devices.
4. Determine whether the real compatibility profiles are legacy, SCPI-like,
   mixed, or another grouping.

Phase 1A and Phase 1B can proceed independently.

### Phase 2 — Cobolt profiles

1. Create the protocol package and profile files.
2. Lift the Phase 1A Cobolt send/reply classifier into the package
   `__init__.py`.
3. Move command strings and operation-specific parsers without changing
   manager behavior.
4. Preserve current cross-dialect alternatives inside a compatibility profile
   until Phase 1B provides evidence for a cleaner split.
5. Add explicit `protocolProfile`.
6. Keep the existing SCPI-over-legacy precedence.
7. Freeze and stop calling `PyCoboltManager` high-level command helpers.
8. Run the unit tests and verify representative hardware.

Acceptance gate: "without changing manager behavior" means the wire trace is
unchanged. The existing tests already assert exact command sequences, for
example `laser.cmds == ['@cobas 0', 'slmp 5.0', 'em', 'sdmes 1', 'l0']`, which
makes them a golden-transcript harness for the extraction. Phase 2 must leave
those assertions passing unmodified. Any diff in an existing sequence is either
a bug in the extraction or an intentional, hardware-approved safety correction,
and must be classified as one or the other before the phase is considered
complete.

### Phase 3 — Cobolt discovery refinement

1. Implement conservative automatic discovery from the verified profile set.
2. Resolve safe command alternatives during validation where possible.
3. Fail only for no match, contradictory evidence, or overlap without a
   documented precedence.
4. Mark profiles that cannot be distinguished read-only as explicit-only.
5. Verify selection on every representative Cobolt device.

### Phase 4 — AA AOTF profiles

1. Add the AA protocol package and first confirmed profile.
2. Add the vendor-local query/exception adapter in the package `__init__.py`.
3. Move command formatting and operation-specific reply handling out of the
   manager.
4. Add tests that pin current command sequences and permissive reply handling.
5. Add additional profiles only when backed by a manual, vendor code, or
   observed hardware transcript.
6. Tighten acknowledgement validation only as a separate evidence-backed step.
7. Do not add discovery unless a safe read-only discriminator exists.
8. Verify multi-channel operation on a real controller.

### Phase 5 — Documentation and legacy handling

1. Document the available profile names and configuration.
2. Add a small hardware support table with the profiles verified in the lab.
3. Mark the Lantz Cobolt implementation as legacy.
4. Keep existing manager names and setup compatibility during the transition.

## Non-goals

This work will not:

- create a general-purpose hardware-driver framework;
- refactor unrelated managers;
- redesign ImSwitch setup schemas;
- add a separate class or file for every minor concern;
- add a generic connection/transport abstraction shared by Cobolt and AA;
- change the shared `RS232Manager` mock-substitution policy that AA and other
  RS232 devices depend on;
- guarantee automatic detection for devices without safe identifying queries;
- infer the OEM pause policy from mutating probes;
- change manager/thread synchronization or claim that profile objects make
  hardware access thread-safe;
- remove all legacy Cobolt code in the first change.

## Definition of done

- ImSwitch-facing managers contain no raw vendor command construction.
- Each supported compatibility profile has one profile file and one source of
  truth; mixed command syntax is allowed when hardware evidence requires it.
- Shared result and failure semantics have one vendor-independent source in
  `_protocol.py`.
- Common Cobolt reply classification has one source for all Cobolt profiles.
- The success and failure contract between manager and profile is explicit and
  preserves fail-closed state updates.
- Shipped profile identifiers remain valid or resolve through aliases.
- Explicit profile selection works and is validated.
- Automatic discovery is read-only, bounded, and conservative.
- Expected SCPI/legacy overlap follows documented precedence.
- Profiles that cannot be distinguished safely through read-only behavior are
  explicit-only.
- Unknown, contradictory, or genuinely unresolved devices do not default to a
  guessed profile.
- A timeout never triggers a second mutating command variant.
- Committed safety sequences abort to their safe-state recovery on any profile
  exception; they never search for variants mid-sequence.
- A configured emission policy that the selected profile cannot implement fails
  at startup, not at the first enable.
- Cobolt simulation is explicit; AA transport-level mock substitution is
  unchanged and recorded as a separate RS232-wide follow-up.
- An absent `simulation` key means `false`.
- With `simulation: false`, initialization failure raises an actionable error
  rather than loading a mock.
- Shipped example setups load without hardware because they declare simulation,
  not because a fallback hides the missing device.
- Phase 2 leaves the existing command-sequence assertions passing unmodified,
  or classifies each diff as an approved safety correction.
- Setpoint range validation is an explicit, tested clamp-or-reject decision
  rather than an incidental addition.
- Healthy devices produce one concise startup summary and no warnings.
- Cobolt and AA command sequences are covered by unit tests.
- AA reply validation is tightened only in a separate evidence-backed change.
- PyCobolt high-level command helpers are not used by the refactored manager.
- Hardware connections close on finalization.
- Omitted `protocolProfile` preserves the current Cobolt and AA behavior for
  existing setup files.
- Existing setup files remain compatible wherever this does not preserve unsafe
  fallback behavior.
