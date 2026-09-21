# Config Editor Schema Extraction Plan

Date: 2026-09-21 (plan; revision 2 after review; revision 3 records the
decisions). Status: **accepted — ready to implement as one PR.**

Extends [config-editor-discovery-and-schema.md](config-editor-discovery-and-schema.md)
(the "discovery plan" below). Nothing here contradicts its target architecture;
it supplies the one thing that plan left to hand-writing and that nobody has
hand-written since: the per-manager `managerProperties` schemas.

## Changes in revision 2

Six review findings against revision 1, each verified against the tree before
the plan was changed:

| Finding | What was wrong | Where it is fixed |
| --- | --- | --- |
| Subscript reads are not necessarily required | `AAAOTFLaserManager` reads `calibCsvPath` inside `try/except KeyError` and `example_sted.json` omits it; its toggles are `in`-guarded. Guard-aware counting demotes **23 of 91** "required" keys. | [Extraction rules → Requiredness](#extraction-rules) |
| The JSON widget is not lossless | `None`, `""` and `"null"` display as `{}` and save as `{}`; a string `"5"` displays bare and saves as `5`; `"true"` becomes `true`. | New [Prerequisite](#prerequisite--a-lossless-json-widget), landed as `b6536d80` on PR #34 |
| Phase 2 already changed editor behaviour | Catalog schemas become form fields at once, and Apply writes every displayed field — **120 of 188** keys are optional and would be written into files that omitted them; an APD file holding `mock_photon_count_mean` would gain a `mockPhotonCountMean` default, which the manager prefers. | Phase 2 is now model-layer only with editor consumption **gated**; alias handling and omitted-optional preservation are Phase 3 entry conditions |
| A `None` default is not a type | `ThorCamTSIManager` uses `props.get("cameraSerial", None)`; examples carry strings. **13 keys** have only a `None` default. | [Sources](#sources-and-their-order-of-authority): `None` is nullability evidence, never type evidence |
| CLI hook missed registered managers | `_validate_manager_resolution` validates only when `contribution is not None`, and for registered core managers `resolve_schema()` returns `None` (no `source_package`). Hooking only the legacy branch left `HamamatsuManager`, `NidaqPositionerManager` and the other seven unvalidated. | One `schema_for()` used by catalog and validation, both branches; tests name registered managers and aliases |
| Type authority has three consumers | `materialize_device_schema()` propagates only `req` for template-backed fields; `build_default_device()` and the editor's legacy path index `f["type"]` and would raise once Phase 5 strips it. | Phase 3 lists all consumers and tests schema → form → new device |

## Summary

The editor already knows how to render a form from a JSON Schema
(`configeditor/schemas.py`, discovery plan Phase 2) and the CLI already knows how
to validate a setup against one (`plugins/validation.py`). Two months on,
**zero of 64 managers have a schema**, and the 20 hand-written templates that
stand in for them have drifted — AAAOTF's template does not expose `ttlToggling`,
the key its correct configuration depends on.

The discovery plan rejected asking managers what they expect because that meant
*importing* them (vendor SDKs, hardware-adjacent side effects). It never
considered **static extraction**: parsing the manager source with `ast`, no
import, no side effects. A probe over the current tree shows that works:

| Measure | Result |
| --- | --- |
| Selectable managers in the catalog | 64 (9 registry, 55 legacy scan) |
| … that read any `managerProperties` at all | 55 |
| … whose keys static extraction recovers | **54** (the 55th, `PyCoboltManager`, takes constructor kwargs) |
| Distinct keys recovered | **188**, none dynamic (no `props[f"…"]` anywhere) |
| Required under a guard-aware rule (unguarded subscript, no `.get`/`in` read of the same key) | 68 (a naive subscript rule says 91) |
| Optional | 120 |
| Keys typed from code alone (non-`None` `.get` default or `int()`/`float()`/`bool()`/`Path()` wrapper) | 88 (47 %) |
| … plus the kind of value in the 64 shipped example setups | 116 (61 %) |
| … plus the *Type* column of the hand-written `docs/devices` cards | 149 (79 %) |
| Keys whose only default is `None` | 13 |
| Device references recovered by data-flow (`props['x']` → `lowLevelManagers['rs232sManager'][x]`) | 14 |
| Agreement of extraction with the 118 fields the docs cards document | 111 (94 %) |

That last row is the point: [docs/devices/README.md](../../devices/README.md)
says the cards were hand-derived from "the `managerProperties[...]` and
`managerProperties.get(...)` calls in the manager `.py` files". The extractor
automates the derivation the project already trusts.

**Proposal:** a generator that emits per-manager JSON Schemas from the source
tree into package data, checked in, with a CI test that fails when source and
schema disagree. The schemas feed the *existing* schema path in the editor and
the CLI — but the editor does not consume them until it can do so without
rewriting files (Phase 3). Templates shrink to presentation overlays. Keys no
source can type fall to the editor's JSON widget, which the prerequisite makes
lossless, with a hand-curated overrides file as the escape hatch.

## What this does not do

- It does not import manager classes, probe hardware, or run at editor start.
  Extraction is a build-time tool; the editor loads JSON.
- It does not replace the plugin manifest contract. An external plugin still
  ships `manager_properties_schema` by hand — or runs the same tool over its own
  package (Phase 6).
- It does not finish discovery-plan item A (registering the 53 core managers the
  completeness test currently freezes as `unregistered`). It resolves generated
  schemas by manager *name*, so it works for legacy-scanned managers today and
  keeps working if they are registered later.
- It does not touch discovery-plan items B (resource loader), C (CLI
  `ValidationContext`) or E (UX).

## Invariant: the editor never rewrites what it did not change

Stated once here because three findings trace back to it. A setup file is a
contract with its manager: presence of an optional key changes behaviour
(`ttlToggling` is `in`-guarded), the spelling of an aliased key decides which of
two reads wins (`mockPhotonCountMean` shadows `mock_photon_count_mean`), and a
missing key is what makes `calibCsvPath` optional at all. Therefore:

- **Omitted stays omitted.** Apply writes an optional property only if the file
  already had it or the operator edited its field. A form may *show* a default;
  it does not *save* one unasked.
- **Spelling is preserved.** A property saved under an alias is loaded from,
  and written back to, that alias. The canonical spelling is used only for a
  property the file never had.
- **Unknown stays verbatim.** Already guaranteed by `merge_preserving_unknown`
  (discovery plan Phase 2); this plan does not weaken it.

Today's `PropertyEditor._do_apply` writes every displayed field unconditionally.
Until it honours the first two rules, generated schemas must not reach it.

## Sources, and their order of authority

Four sources describe a manager's properties. The generator merges them with a
fixed precedence and records, per property, which source decided what
(`x-imswitch-source`), so a reviewer can see *why* a type was chosen.

| Rank | Source | Provides | Notes |
| --- | --- | --- | --- |
| 1 | **Hand-curated overrides** — `schemas/overrides/<Manager>.json` | anything | Human-owned, merged last, never overwritten. For the untyped keys, pass-through dict shapes, requiredness the code leaves ambiguous, and corrections. |
| 2 | **Manager source (AST)** | keys, required/optional, type from a non-`None` default literal or a wrapper call, nullability, device references, nesting | The contract. A key the code never reads is not a property. A `None` default says "may be null", nothing more. |
| 3 | **`SetupInfo` dataclasses** (imported — pure dataclasses, no side effects) | the shared top-level fields per kind with their annotations (`analogChannel: Optional[Union[str, int]]`, `wavelength: Union[int, float]`, `axes: List[str]`) | Replaces the category blanks' hand-typed `top` lists, which type `analogChannel` as `text` and caused the int→string bug. |
| 4 | **Shipped example setups** (`_data/user_defaults/imcontrol_setups/*.json`) | the JSON kind of each key's value, per manager; which optional keys shipped setups omit | Usage, not contract — so it only *types* a key the code already reads and left untyped. Two kinds for one key (`cameraListIndex`: int and str; `conversionFactor`: int and float) become a union / `number`. A key every example omits is corroboration of optionality, never a demotion on its own. |
| 5 | **`docs/devices/*.rst` cards** | *Type* column (parsed against a fixed vocabulary), *Meaning* column → `description` | Hand-written; used only where 2–4 are silent. |
| 6 | **Existing templates** | `label`, `grp`, `tip`, `opts`, `select`/`multiselect`/`path`/`ref` presentation types | Presentation only. A template's `type` never overrides a schema type except as a compatible refinement (see Phase 3). |

Precedence for the *type* of a property: 1 → 2 → 4 → 5 → JSON widget. A `None`
default at rank 2 does not stop ranks 4–5 from supplying the type; it adds
`null` to whatever they supply (`cameraSerial` → `["string", "null"]`, which
`_infer_type_from_schema` already renders as a text box).

For *presentation*: 1 → 6 → generated defaults (`Title Case` label, group
"Properties").

## Extraction rules

Everything below is what the probe already does, written down so it can be
unit-tested idiom by idiom. Extraction is per **class**; a manager's schema is
the union over its base classes (`Cobolt0601LaserManager` reads nothing itself;
`LantzLaserManager` reads `digitalPorts`).

**Finding the properties object.** Any of:

- `<info>.managerProperties` — attribute access, any receiver;
- `getattr(<info>, "managerProperties", …)`;
- a name or attribute *bound* to either of the above, including through
  `… or {}` (`props = laserInfo.managerProperties or {}`,
  `self._settings = rs232Info.managerProperties`). Aliases are scoped to the
  class that binds them.

**Reading a key.** Each of these records a property:

| Idiom | Records |
| --- | --- |
| `props["k"]` | `k`; required **only** under the rule below |
| `props.get("k")` / `props.get("k", default)` | `k`, optional; a non-`None` default literal's kind becomes the type (`bool` before `int` — `True` is not an integer); a `None` default records nullability only |
| `"k" in props` | `k`, optional |
| `props.get("camelK", props.get("snake_k", d))` | one property `camelK` with `x-imswitch-aliases: ["snake_k"]`, not two (APD/PMT mock knobs) |
| `int(props["k"])`, `float(…)`, `bool(…)`, `str(…)`, `Path(…)` around any read | type from the wrapper (`Path` → `string` + `x-imswitch-widget: path`) |
| `props["vendor"]["sub"]` | `vendor` is `object`; sub-keys are recorded as `properties` of it |
| `name = props["k"]` … `lowLevelManagers["<bucket>"][name]` (or inline) | `k` is a **device reference**: `string` + `x-imswitch-widget: ref` + `x-imswitch-ref-category` from the bucket (`rs232sManager` → `rs232devices`) via `setup_metadata` |
| `settings = props` passed whole to a driver/`serial.Serial`/`generateDriverClass` | the manager is **open**: `additionalProperties: true` with a note; the template/overrides supply the driver's keys (RS232Manager's `baudrate`, `bytesize`, …) |

**Requiredness.** A key is `required` only when *every* read of it is an
unguarded subscript. A read is guarded when it sits:

- in the body of a `try` whose handlers include `KeyError`, `Exception`, or a
  bare `except` (`calibCsvPath`);
- in the body of an `if "k" in props:` / `if props.get("k"):` for the same key
  (`ttlToggling`, `toggleTrueExternal`);
- anywhere, if the same key is also read with `.get` or `in` in the class or a
  base class.

Anything the rule cannot classify — a read inside a helper that receives the
dict, a subscript under a condition the analysis does not model — is recorded
as `x-imswitch-required: "uncertain"` and treated as optional until an override
says otherwise. The coverage report lists these. On the current tree the rule
yields 68 required, 120 optional, and the report is expected to name the
uncertain ones so a reviewer can settle them.

**Where readers live.** Scan `imswitch/imcontrol/model/managers/**` for
per-manager keys. Also scan `model/signaldesigners/**` and
`controller/controllers/**`: those reads are **role-level**, not per-manager —
any positioner used as a scan device is read for `conversionFactor`, `vel_max`,
`acc_max` (`GalvoScanDesigner`, six TriggerScope controllers), any SLM for
`startConfig`. They become `schemas/roles/<role>.json` fragments applied by
predicate (`forScanning: true`, kind `slm`), not copied into 17 positioner
schemas.

**Not extracted, by design.** Vendor pass-through dicts (`hamamatsu`, applied
key-by-key via `setPropertyValue`) stay `object` with no sub-schema: the keys
belong to the SDK. Keys that no source types render in the JSON widget, which
the prerequisite below makes round-trip any value — the same fallback the
discovery plan chose for unions.

## Prerequisite — a lossless JSON widget

The JSON widget is where every untyped, union and object-valued property will
land, and today it is not a safe place to put a value:

| Value in file | Displayed | Saved as |
| --- | --- | --- |
| `null` | `{}` | `{}` |
| `""` | `{}` | `{}` |
| `"5"` | `5` | `5` (int) |
| `"true"` | `true` | `true` (bool) |
| `{"a": 1}` | `{"a": 1}` | `{"a": 1}` ✓ |

Cause: `FieldWidget._init_widget` maps `None`, `""` and `"null"` to the display
`"{}"` and shows a string value bare instead of as a JSON literal; `get_value`
then parses the text as JSON. `_build_default_section` has the same `{}`
fallback for section defaults. Five section fields use the widget today
(`scan.scanDesignerParams`, `scan.TTLCycleDesignerParams`,
`tiling.detectorTransforms`, `teensyPulse.pinMap`,
`microscopeStand.managerProperties`), all dict-valued, which is why nobody has
hit it yet.

Fix (landed as `b6536d80` on PR #34, beside the other type-preservation
fixes): display every value as its JSON literal —
`json.dumps(value)`, so a string shows quoted and `None` shows as `null` — and
read back `null` → `None`, valid JSON → the parsed value (a quoted string stays
a string), invalid JSON → the raw text, unchanged from today. Section defaults
keep `None` as `None`. Tests cover each row of the table through the real
widget. **No generated schema reaches the editor before this is merged.**

## Output

```
imswitch/imcontrol/model/configeditor/schemas/
  managers/<ManagerName>.json     one Draft 2020-12 schema per manager (generated)
  kinds/<kind>.json               shared top-level fields from SetupInfo (generated)
  roles/<role>.json               role fragments, with their predicate (generated)
  overrides/<ManagerName>.json    hand-curated, merged last (never generated)
  index.json                      generator version, source hashes, coverage table
```

Package data (`MANIFEST.in` graft), loaded with `importlib.resources` — the
loader is the Qt-free `configeditor/resources.py` the discovery plan's item B
asks for, so this plan delivers a slice of B rather than a second loader.

A generated schema, for illustration (AAAOTF, under the revised rules):

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "AAAOTFLaserManager managerProperties",
  "type": "object",
  "additionalProperties": true,
  "required": ["rs232device", "channel"],
  "properties": {
    "rs232device": {"type": "string",
                    "x-imswitch-widget": "ref", "x-imswitch-ref-category": "rs232devices",
                    "x-imswitch-source": ["code:required", "code:ref"]},
    "channel":     {"type": "integer", "x-imswitch-source": ["code:required", "example:int"]},
    "calibCsvPath":{"type": "string", "x-imswitch-widget": "path",
                    "x-imswitch-source": ["code:optional(try/except KeyError)", "template:path"]},
    "frequencyMHz":{"type": ["number", "null"],
                    "x-imswitch-source": ["code:optional", "code:nullable", "example:float"]},
    "ttlToggling": {"type": "boolean",
                    "x-imswitch-source": ["code:optional(in-guard)", "docs:type"]},
    "toggleTrueExternal": {"type": "boolean",
                    "x-imswitch-source": ["code:optional(in-guard)", "docs:type"]},
    "protocolProfile": {"type": "null", "x-imswitch-source": ["code:optional", "code:nullable"],
                    "x-imswitch-widget": "json"}
  }
}
```

`calibCsvPath` is no longer required — the code guards it and `example_sted.json`
omits it. `ttlToggling` and `toggleTrueExternal` are the two keys the
hand-written template lacks. `protocolProfile` has nullability but no type from
any source and gets the JSON widget until someone adds an override.

`x-imswitch-*` is the annotation vocabulary the discovery plan's item D
proposed (`group`, `label`, `help`, `units`, `path`, `secret`, device-reference
category). This plan introduces `widget`, `ref-category`, `aliases`, `required`
(for `"uncertain"`) and `source`; the rest arrive with the template-overlay
merge in Phase 5.

## Where the schemas are resolved

One function, used everywhere a schema is looked up:

```python
def schema_for(kind: str, manager_name: str, contribution: DeviceManagerContribution | None) -> dict | None:
    """The contribution's own schema if it ships one; else the generated one by id or alias."""
```

Resolution: `resolve_schema(contribution)` when it returns a schema (a plugin
that ships its own wins); otherwise the generated schema for
`contribution.id` if there is a contribution, else for `manager_name`; aliases
resolve through the catalog. Callers:

1. **Catalog, registry path** — `build_catalog()` today calls
   `resolve_schema(contribution)`, which returns `None` for every core
   contribution because none declares `source_package` or
   `manager_properties_schema`. Replace with `schema_for`.
2. **Catalog, legacy path** — `_scan_legacy_managers()` builds `ManagerInfo`
   with no schema. Same call. This covers 55 of the 64 today.
3. **Validation, both branches** — `_validate_manager_resolution()` validates
   only `if contribution is not None`, and then only when `resolve_schema`
   finds something, so registered core managers such as `HamamatsuManager` and
   `NidaqPositionerManager` are as unvalidated as the legacy ones. Replace with
   `schema_for` in the registry branch and add it to the `legacy` /
   `legacy-mock` branches. Tests must cover a registered manager, a
   legacy-scanned one, and an alias (`hamamatsu.orca`).

`ManagerInfo.properties_schema` keeps its meaning; only its provenance widens.

## Phases

### Phase 0 — Extractor and coverage report (no product change)

- `configeditor/extraction.py`: pure functions over `ast` — `find_props_aliases`,
  `iter_property_reads` (each read annotated with its guards), `classify_requiredness`,
  `collect_class_schema`, `merge_bases`, `infer_refs`.
- `tools/extract_manager_schemas.py --report`: prints the coverage table above
  from the live tree, plus the list of `uncertain` requiredness cases and the
  reads it discarded as not tracing to the constructor's `*Info` parameter.
- Tests in `test_configeditor_extraction.py`: one fixture source snippet **per
  idiom in the tables** — alias through `or {}`, `getattr`, `self._settings`
  attribute alias, subscript, `.get` with each literal kind, `.get` with `None`,
  `in`, camel/snake fallback, wrapper calls, nested read, inline and
  two-statement ref, open pass-through — and per **guard**: `try/except
  KeyError`, `try/except Exception`, bare `except`, `if "k" in props`, `.get`
  elsewhere for the same key, a base-class `.get` for a subclass subscript.
  Plus one snapshot test that the report over the real tree matches a
  checked-in table.

**Exit criterion:** the report reproduces 54 / 188 / 68 required / 14 refs on
the current tree; `AAAOTFLaserManager` reports `calibCsvPath`, `ttlToggling`,
`toggleTrueExternal` as optional and `cameraSerial` on `ThorCamTSIManager` as
nullable-untyped; every row of both tables has a failing-then-passing test.

*Estimate: ~1 day. Most of the logic exists as the probe; the guard analysis is
the new part.*

### Phase 1 — Checked-in schemas and the drift guard

- `tools/extract_manager_schemas.py --write` emits `schemas/**` deterministically
  (sorted keys, two-space JSON, trailing newline) so diffs are readable.
- `--check` regenerates in memory and diffs against disk; exit 1 with the list
  of managers that changed.
- `test_configeditor_schemas_match_source.py`: the same in-memory regeneration
  asserted equal to the checked-in files — the pattern
  `test_communication_channel_contract.py` already uses for its signal
  inventory. Failure message names the command to run.
- Overrides directory created with an `AAAOTFLaserManager.json` typing
  `protocolProfile`, so the mechanism is exercised from day one.

**Exit criterion:** adding a `props.get("newKey", 3)` to any manager without
running the tool fails CI with a message that says how to fix it; running the
tool produces a one-property diff.

*Estimate: ~0.5 day.*

### Phase 2 — Resolve schemas in the model layer; editor consumption gated

- `configeditor/resources.py`: `generated_schema_for(manager_name)` via
  `importlib.resources`, with an injectable root for tests; `schema_for()` as
  above.
- The three call sites above.
- **Gate.** `build_catalog()` populates `properties_schema` from generated
  schemas only when asked (`include_generated_schemas=True`); the editor's
  `_MANAGER_CATALOG` is built without it until the Phase 3 commits flip it. The
  CLI is built with it from the start. Rationale: the editor's
  `materialize_device_schema` turns any `properties_schema` into form fields on
  the spot, and `_do_apply` writes every field — the invariant above would be
  broken for 120 optional keys and every aliased one the moment a schema
  appeared. Since everything ships in one PR (decision 5), the gate is an
  ordering discipline inside that PR, not a shipped state: the parameter stays
  so the model layer is testable in isolation, and the PR merges with it on.
- Tests: with the flag, `build_catalog()` reports ≥ 54 infos with
  `properties_schema`; without it, the editor catalog reports the same 0 as
  today; `validate-setup` on a fixture with `"channel": "two"` under AAAOTF
  reports a schema error at `(lasers, <name>, managerProperties)`, and the
  same for `"cameraListIndex": "x"` under `HamamatsuManager` (registered) and
  under its alias `hamamatsu.orca`; the existing `test_fake_plugin_contribution`
  still passes with its own schema winning over a generated one of the same id.

**Checkpoint (not a shipped state):** the CLI validates every manager that
has a schema, registered or not; with the gate down, the editor's behaviour is
byte-for-byte unchanged.

*Estimate: ~0.5 day.*

### Phase 3 — Schema is the type authority in the editor

This is where the operator sees the change, and it may only ship once the
invariant holds. Entry conditions, each with its own test before the gate is
lifted:

- **Omitted stays omitted.** `PropertyEditor` records, per field, whether the
  key was present on load; `_do_apply` writes an optional property only if it
  was present or its widget reports a user edit (`FieldWidget.is_dirty()`,
  compared against the value it was constructed with). A required property is
  always written. Test: load AAAOTF without `calibCsvPath`, apply without
  touching it, assert the key is still absent; edit it, assert it is written.
- **Spelling is preserved.** `x-imswitch-aliases` is honoured on load (the
  form shows the value found under the alias) and on save (written back under
  the same spelling). Test: an APD file with `mock_photon_count_mean: 400`
  round-trips with that key, that value, and no `mockPhotonCountMean`.
- **Prerequisite merged** (lossless JSON widget).

Then the type authority, in **all three consumers**:

- `normalized_fields()`: when template and schema both define a property, the
  schema decides the base type; the template's type is kept only as a
  **compatible refinement** (`select`/`multiselect` over `string`/`integer`,
  `ref` over `string`, `path` over `string`). An incompatible template type is a
  drift error (Phase 5 test) and the schema wins.
- `materialize_device_schema()`: for a template-backed field it currently
  propagates only `req`; it must propagate the resolved `type`, `opts` (from
  `enum`), `x-imswitch-widget`/`ref-category`, and `aliases` as well, or the
  form never sees the schema for the 20 templated managers.
- `build_default_device()` and the editor's legacy inline fallback: tolerate a
  template field without `type` (Phase 5 strips them) by taking the type from
  the resolved field spec; seed defaults from the schema; **never** seed an
  optional property the schema marks nullable or that has no default.
- `_infer_type_from_schema`: map the union `["integer", "string"]` to `text`
  rather than `json` — since `ef86d1fe` the text widget preserves kind, so the
  union is lossless there (`cameraListIndex` is int in 4 shipped setups and str
  in 7). `["<type>", "null"]` keeps its current single-type mapping.
- `x-imswitch-widget: ref` renders the existing `ref` combo with its category;
  `path` the existing path picker.
- The raw "Properties" tab shows only keys unknown to **both** template and
  schema.
- Lift the gate: the editor's catalog is built with generated schemas.

**Exit criterion:** opening any of the 44 untemplated managers shows typed
fields, not a raw tab; a full schema → form → Apply → new-device path is tested
end to end for one templated manager (`AAAOTFLaserManager`) and one
untemplated (`ThorCamTSIManager`); every shipped example setup round-trips
through load → apply-without-edits → save **byte-identical** (this is the test
that proves the invariant across all 64 setups at once); a device with a stale
`"9600"` still lands on `9600`.

*Estimate: ~1.5 days (was 1; the invariant work is the difference).*

### Phase 4 — Shared kind and role fragments

- `kinds/<kind>.json` generated from the `SetupInfo` dataclass for that kind
  (`DetectorInfo`, `LaserInfo`, `PositionerInfo`, …): field name, annotation →
  JSON type (`Optional[Union[str, int]]` → `["integer", "string", "null"]` →
  text widget), default. Replaces the `top` lists of the category blanks — the
  ones that typed `analogChannel` as `text`.
- `roles/scan_positioner.json` (`conversionFactor` required, `vel_max`/`acc_max`
  optional numbers, predicate `forScanning == true`); `roles/slm.json`
  (`startConfig`, path). Applied by the editor after the manager schema, under
  the same omitted-stays-omitted rule.

**Exit criterion:** the blank templates' `top` sections are deleted; every
top-level field in the editor is typed from a dataclass annotation; a positioner
with `forScanning: true` shows the galvo fields whichever manager it uses; the
byte-identical round-trip test still passes.

*Estimate: ~0.5 day.*

### Phase 5 — Templates become overlays; drift tests

- Strip `type` from template fields where it equals the schema's; keep
  `label`/`grp`/`tip`/`opts` and refinements. (Safe only after Phase 3 made
  every consumer tolerate a missing `type`.)
- `test_configeditor_template_drift.py`: every template key must exist in the
  manager's schema or in an explicit pass-through allow-list (RS232's pyserial
  kwargs); every template `type` must be the schema type or a compatible
  refinement. Would have caught APD/PMT's 9–13 undeclared keys,
  SwabianTimeTagger's missing `laser_rep_rate_mhz`, AAAOTF's `ttlToggling`.
- `test_devices_docs_drift.py`: every property in a schema appears in that
  manager's docs card, or the card is regenerated. **Decision needed** (see
  below): generate the cards' `list-table` blocks from the schemas, or only
  test them.

**Exit criterion:** a template can no longer describe a property the code does
not read, nor mis-type one it does; a manager cannot gain a property without
the docs noticing.

*Estimate: ~0.5–1 day depending on the docs decision.*

### Phase 6 — Plugins (documentation only)

`tools/extract_manager_schemas.py --package vendor_plugin --write` runs the same
extraction over an installed plugin and writes into its package, so a plugin
author gets a schema without writing one. Document in `docs/devices/plugins.rst`.
No core change.

## Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| A schema marks required a key the code tolerates missing | The guard-aware rule plus `uncertain` for anything it cannot classify; the byte-identical round-trip over all 64 shipped setups fails if a required key is missing from any of them, which is exactly the `calibCsvPath` case. |
| The editor writes an optional key into a file that omitted it, changing behaviour | The invariant, its two tests, and the round-trip test; and the gate keeps schemas out of the editor until they pass. |
| An aliased key is written under the canonical spelling and shadows the saved one | Alias preservation on load and save, tested with the APD case the review named. |
| A `None` default is read as "this property is null" | `None` records nullability only; type comes from lower-ranked sources or is absent. `cameraSerial` is the test case. |
| A read of *another* device's properties inside a manager is attributed to this manager | Only reads whose receiver traces to the constructor's `*Info` parameter (or its aliases) count; alias scoping is per class. The report lists discarded reads so the rule can be checked. |
| Docs *Type* column is free text ("int or str", "dict", "float, seconds") | A small parser with an explicit vocabulary; anything unparsed is ignored, never guessed. Docs are rank 5. |
| Example setups encode usage, not contract | Rank 4, only for keys the code already reads; `x-imswitch-source` says `example:` so reviewers can see it. |
| `jsonschema` is optional (not in `install_requires` or the `test` extra) | The drift test compares JSON files and needs no validator. Validation tests `importorskip`. |
| Non-deterministic output makes every regeneration a noisy diff | Sorted keys, fixed formatting, no timestamps; `index.json` carries source hashes, not dates. |
| Generator and overrides disagree | Overrides are merged last and are the *only* hand-edited files; the generator refuses to write into `overrides/`. |
| Untyped keys look like regressions from templates that typed them | They are not: a template's `text` type was the bug. Phase 3 keeps a template's `select`/`ref`/`path` refinement, and Phase 5's drift test surfaces the rest for an override. |

## Decisions (recorded 2026-09-21)

1. **Name-keyed resolution now.** `schema_for()` resolves by manager name so it
   covers the 55 legacy-scanned managers today. Registration (discovery-plan
   item A; 53 core managers are frozen as `unregistered` in
   `test_setup_metadata.py`) stays independent; the name-keyed fallback branch
   is deleted whenever A lands.
2. **Example setups are a typing source**, rank 4, with `example:` provenance on
   every property they type (47 % → 61 % typed).
3. **Docs cards are drift-tested in Phase 5**, not generated. Generating the
   `list-table` blocks is a later, separate change.
4. **Overrides live beside the generated files**, in
   `configeditor/schemas/overrides/`.
5. **One PR for all phases.** See [Delivery](#delivery).
6. **`uncertain` requiredness ships as optional**, listed in the coverage
   report. The byte-identical round-trip test catches a wrong "required"; a
   wrong "optional" only costs a validation message that did not fire.

## Delivery

One pull request, branch `feat/config-editor-schema-extraction`, stacked on
`feat/config-editor-from-imcontrol` until PR #34 merges (it depends on the
moved editor, the type-preservation fixes and the lossless JSON widget), then
rebased onto `main`.

- Commits follow phase order (0 → 5) so each exit criterion is reviewable at
  its own commit; the Phase 2 gate is down until the Phase 3 commit that lifts
  it, and the PR does not merge with it down.
- The PR description carries the coverage table before (templates only: 20
  managers, 0 schemas) and after, generated by `--report`, plus the list of
  `uncertain` requiredness cases for the reviewer to settle or leave.
- Merge gate: every phase's exit criterion met, the byte-identical round-trip
  over all 64 shipped setups green, all three CI lanes green.
- Estimate: ~4 days of focused work (0: 1 · 1: 0.5 · 2: 0.5 · 3: 1.5 · 4: 0.5 ·
  5: 0.5–1). Phase 6 is documentation and rides along.

## Relation to the discovery plan

| Discovery-plan item | Effect of this plan |
| --- | --- |
| A. Registry metadata complete | Untouched; becomes optional for schema coverage (decision 1). |
| B. Resources out of Qt | Partially delivered: `resources.py` loads generated schemas; templates/blanks loading can follow the same path later. |
| C. CLI `ValidationContext` | Untouched. |
| D. Schema-driven editing | Delivered for core managers; `x-imswitch-*` vocabulary started; "preserve unsupported saved values on round trip" strengthened into the invariant above. |
| E. UX | Untouched. |
| "Can managers tell us what they expect?" | Amended: *runtime* introspection stays rejected; *static* extraction is the recommended default for core, optional for plugins. |

## Verification snapshot (2026-09-21)

All figures above were measured on `feat/config-editor-from-imcontrol` at
`ef86d1fe` with throwaway probes, re-run for revision 2 with guard analysis;
none are estimates. Revision 3 changed no figures. Reproduce with Phase 0's
`--report`.
