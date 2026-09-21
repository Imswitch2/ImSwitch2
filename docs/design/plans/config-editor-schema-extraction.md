# Config Editor Schema Extraction Plan

Date: 2026-09-21 (plan; revision 2 after first review; revision 3 recorded the
decisions; revision 4 after second review). Status: **proposed — revision 4
awaiting review.** Not marked ready to implement: the second review found the
acceptance criterion, the type rules and the role fragments unsound as written.

Extends [config-editor-discovery-and-schema.md](config-editor-discovery-and-schema.md)
(the "discovery plan" below). Nothing here contradicts its target architecture;
it supplies the one thing that plan left to hand-writing and that nobody has
hand-written since: the per-manager `managerProperties` schemas.

## Changes in revision 4

Eight findings against revision 3, each verified against the tree before the
plan changed. Two were defects in code this plan had marked as landed; both are
fixed in `ec3e02f2` on PR #34.

| Finding | What was wrong | Where it is fixed |
| --- | --- | --- |
| Preserving omitted fields does not preserve existing values | Every displayed field was written, and widgets alter what they display: ints clamped to ±999999, floats rounded to 4 decimals, `None` → `0`/`False`, the string `"null"` → `null`. Comparing output with the original cannot tell those from edits. | **Landed `ec3e02f2`:** a field returns the object it was constructed with until the operator interacts with it (`textEdited`/`clicked`/`activated`/post-construction `valueChanged`); a value the spin box cannot hold falls back to the text box instead of crashing on load. [Invariant](#invariant-the-editor-never-rewrites-what-it-did-not-change), [Phase 3](#phase-3--schema-is-the-type-authority-in-the-editor) |
| A default or conversion call is evidence, not a validation type | `BSC203StageManager` defaults `travelRangeUm` to `8000` and divides it; `int(x)` accepts numeric strings. An integer schema would reject valid files. `protocolProfile` was illustrated as `"type": "null"`, which rejects everything but null. | [Editor preference vs validation constraint](#editor-preference-vs-validation-constraint); corrected illustration |
| The scan-role fragment imposes the wrong requirements | 13 shipped `MockPositionerManager` entries scan without `conversionFactor`; `GalvoScanDesigner` *requires* `vel_max`/`acc_max` for non-mock axes (by name) and the plan had them optional. Role fragments were editor-only. | [Roles](#roles-conditions-evaluated-once-for-editor-and-cli): predicates mirror the consumer's own rule, evaluated in `_validate_cross_references` for both CLI and editor; the editor never *requires* a role property |
| Aliases escaped schema validation | Collapsing `mock_photon_count_mean` into an annotation removed it from validation; with `additionalProperties: true` an invalid snake-case value passed. | [Aliases](#aliases): constraints emitted under every spelling; `manager.alias-conflict` when both are present |
| The JSON prerequisite still had a lossy exception | A string equal to the template default was treated as JSON text: `"{}"` → `{}`, `"5"` → `5`, `"true"` → `true`. | **Landed `ec3e02f2`:** template text defaults are decoded once at the two call sites; a value is always rendered with `json.dumps` |
| The registered-manager test contradicted the rules | `cameraListIndex` is int in 4 shipped setups and `"mock"` in 7 → `["integer", "string"]` → `"x"` passes. | Phase 2 fixtures use an object, and the narrow case goes through a reviewed override |
| Byte-identical acceptance was impossible | Saving re-serialises at indent 2 (14 of 15 shipped files are indent 4) and strips an empty `others`: all 15 change bytes before any edit. Normalising `"9600"` also needed an exception. | [Acceptance](#acceptance-what-round-trip-means): type-strict structural equality of the in-memory data; formatting out of scope; no exception needed, since an untouched `"9600"` now stays a string |
| Verification overstated the corpus | "64 shipped example setups" was 64 *managers*; the directory holds **15 files covering 22 managers**. `jsonschema` is absent from the `test` extra, so validation tests could skip in CI. | [Corpus](#corpus): 15 shipped + synthetic per-manager + value-shape fixtures; `jsonschema` added to the `test` extra |

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
| Keys with an *editor preference* from code alone (non-`None` `.get` default or `int()`/`float()`/`bool()`/`Path()` wrapper) | 88 (47 %) |
| … plus the kind of value in the 15 shipped setups (22 managers) | 116 (61 %) |
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
schema disagree. Generated *validation* constraints are never narrower than
what the code provably accepts; everything narrower is an editor preference or
a reviewed override. The schemas feed the *existing* schema path in the editor
and the CLI — but the editor does not consume them until it can do so without
rewriting files (Phase 3). Templates shrink to presentation overlays.

## What this does not do

- It does not import manager classes, probe hardware, or run at editor start.
  Extraction is a build-time tool; the editor loads JSON.
- It does not replace the plugin manifest contract. An external plugin still
  ships `manager_properties_schema` by hand — or runs the same tool over its own
  package (Phase 6).
- It does not finish discovery-plan item A (registering the 53 core managers the
  completeness test currently freezes as `unregistered`). It resolves generated
  schemas by manager *name* (decision 1), so it works for legacy-scanned
  managers today and keeps working if they are registered later.
- It does not preserve source formatting. `_write_file` re-serialises at indent
  2 and `prepare_for_save` drops an empty `others`; both are existing behaviour
  and out of scope (see [Acceptance](#acceptance-what-round-trip-means)).
- It does not touch discovery-plan items B (resource loader), C (CLI
  `ValidationContext`) or E (UX).

## Invariant: the editor never rewrites what it did not change

A setup file is a contract with its manager: presence of an optional key
changes behaviour (`ttlToggling` is `in`-guarded), the spelling of an aliased
key decides which of two reads wins (`mockPhotonCountMean` shadows
`mock_photon_count_mean`), a missing key is what makes `calibCsvPath` optional
at all, and a value's *kind* decides whether `SetupInfo.getAnalogChannel()`
builds a channel name or returns the value verbatim. Therefore:

- **Untouched stays identical.** A field the operator did not interact with is
  written back as the very object it was loaded as — not as what the widget
  displays. Landed in `ec3e02f2`: `FieldWidget` keeps `_original` and a
  `_touched` flag set only by user-interaction signals (`textEdited`,
  `clicked`, `activated`, `itemChanged`/`valueChanged` connected after
  construction, and the path picker); `get_value()` returns `_original` until
  then. This is what makes a stale `"9600"` in a select stay `"9600"` until
  the operator picks an entry — normalisation is an edit, and validation is
  what flags the string.
- **Omitted stays omitted.** Apply writes an optional property only if the file
  already had it or the operator edited its field. A form may *show* a default;
  it does not *save* one unasked. (Phase 3: `_do_apply` must learn which keys
  were present on load; today it writes every field.)
- **Spelling is preserved.** A property saved under an alias is loaded from,
  and written back to, that alias. The canonical spelling is used only for a
  property the file never had.
- **Nothing a widget cannot hold is lost.** A spin box is a C++ `int`;
  `int("two")` raises. A value the typed widget cannot represent gets the
  type-preserving text box for that instance (landed in `ec3e02f2`) rather
  than a crash on load or a clamped substitute.
- **Unknown stays verbatim.** Already guaranteed by `merge_preserving_unknown`
  (discovery plan Phase 2); this plan does not weaken it.

Until `_do_apply` honours the second and third rules, generated schemas must
not reach it.

## Sources, and their order of authority

Four sources describe a manager's properties. The generator merges them with a
fixed precedence and records, per property, which source decided what
(`x-imswitch-source`), so a reviewer can see *why* a type was chosen.

| Rank | Source | Provides | Notes |
| --- | --- | --- | --- |
| 1 | **Hand-curated overrides** — `schemas/overrides/<Manager>.json` | anything, including validation constraints | Human-owned, merged last, never overwritten. The only place a *narrow* validation type comes from. |
| 2 | **Manager source (AST)** | keys, required/optional, nullability, editor preference from a non-`None` default literal or a wrapper call, the few *provable* validation constraints (below), device references, nesting | The contract. A key the code never reads is not a property. A `None` default says "may be null", nothing more. |
| 3 | **`SetupInfo` dataclasses** (imported — pure dataclasses, no side effects) | the shared top-level fields per kind with their annotations (`analogChannel: Optional[Union[str, int]]`, `wavelength: Union[int, float]`, `axes: List[str]`) | Replaces the category blanks' hand-typed `top` lists. Annotations are validation constraints: `dataclasses_json` enforces them on load. |
| 4 | **Shipped setups** (`_data/user_defaults/imcontrol_setups/*.json`, 15 files, 22 managers) | the JSON kind of each key's value, per manager; which optional keys shipped setups omit | Usage, not contract: contributes to the editor preference only, never to validation. Two kinds for one key (`cameraListIndex`: int and `"mock"`; `conversionFactor`: int and float) widen the preference to a union. |
| 5 | **`docs/devices/*.rst` cards** | *Type* column (parsed against a fixed vocabulary), *Meaning* column → `description` | Hand-written; editor preference and description only. |
| 6 | **Existing templates** | `label`, `grp`, `tip`, `opts`, `select`/`multiselect`/`path`/`ref` presentation types | Presentation only. A template's `type` never overrides a schema kind except as a compatible refinement (see Phase 3). |

Precedence for the *editor preference* (`x-imswitch-kind`): 1 → 2 → 4 → 5 →
none (JSON widget). A `None` default at rank 2 does not stop ranks 4–5 from
supplying the kind; it adds nullability (`cameraSerial` → kind `string`,
nullable). For *presentation*: 1 → 6 → generated defaults (`Title Case`
label, group "Properties"). For *validation*: see the next section.

## Editor preference vs validation constraint

Python code almost never proves what it accepts. `travelRangeUm` defaults to
`8000` and is divided by a thousand, so `8000.5` is valid; `int(x)` takes
`"115200"` as readily as `115200`; `if props.get("flag", False)` is true for
the string `"yes"`. A schema that turns the default's kind into `type` rejects
files the manager runs happily. So every generated property carries two
separate things:

- **`x-imswitch-kind`** — the editor's preference for a widget: `integer`,
  `number`, `boolean`, `string`, `array`, `object`, or a list of these for a
  union. Inferred from any source. Drives widget choice only; the widgets never
  convert an untouched value (invariant), and a typed widget that cannot hold
  the value steps aside.
- **`type` and friends** — validation. Generated **only** from what the code
  provably requires:

  | Code | Constraint emitted |
  | --- | --- |
  | `props["vendor"]["sub"]`, `.items()`/`.keys()`/`.values()` on a read | `type: object` |
  | `Path(props["k"])`, `open(props["k"])` | `type: string` |
  | `int(x)` / `float(x)` | `type: ["integer", "number", "string"]` (a bool is an int to Python; the string must parse, which is left to the manager) |
  | iteration `for … in props["k"]` without `.items()` | `type: ["array", "string", "object"]` (all iterate) — in practice, no constraint worth emitting; omitted |
  | anything else, including a literal default | **no `type`** |

  Everything narrower is an **override** (rank 1), which a human writes after
  reading the manager. The coverage report lists, per property, the kind the
  editor inferred and whether validation is constrained, so reviewers can see
  where an override would pay off. `additionalProperties: true` on every
  schema, as before.

A property with no `type` accepts anything — that is the correct meaning of
"we do not know". The revision-3 illustration of `protocolProfile` as
`"type": "null"` was wrong on exactly this point.

`_infer_type_from_schema` (Phase 3) reads `x-imswitch-kind` for widget choice,
and falls back to `type` only when the kind is absent.

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
| `props.get("k")` / `props.get("k", default)` | `k`, optional; a non-`None` default literal's kind is the editor preference (`bool` before `int` — `True` is not an integer); a `None` default records nullability only |
| `"k" in props` | `k`, optional |
| `props.get("camelK", props.get("snake_k", d))` | one property `camelK` with `x-imswitch-aliases: ["snake_k"]` and constraints under both spellings (see [Aliases](#aliases)) |
| `int(props["k"])`, `float(…)`, `bool(…)`, `str(…)`, `Path(…)` around any read | editor preference from the wrapper; validation only per the table above |
| `props["vendor"]["sub"]` | `vendor` is `object` (validated); sub-keys are recorded as `properties` of it |
| `name = props["k"]` … `lowLevelManagers["<bucket>"][name]` (or inline) | `k` is a **device reference**: kind `string` + `x-imswitch-widget: ref` + `x-imswitch-ref-category` from the bucket (`rs232sManager` → `rs232devices`) via `setup_metadata` |
| `settings = props` passed whole to a driver/`serial.Serial`/`generateDriverClass` | the manager is **open**: a note in the schema; the template/overrides supply the driver's keys (RS232Manager's `baudrate`, `bytesize`, …) |

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
as `x-imswitch-required: "uncertain"` and treated as optional (decision 6). The
coverage report lists these. On the current tree the rule yields 68 required
and 120 optional.

**Where readers live.** Scan `imswitch/imcontrol/model/managers/**` for
per-manager keys. Reads in `model/signaldesigners/**` and
`controller/controllers/**` are **role-level** and are handled in
[Roles](#roles-conditions-evaluated-once-for-editor-and-cli).

**Not extracted, by design.** Vendor pass-through dicts (`hamamatsu`, applied
key-by-key via `setPropertyValue`) stay `object` with no sub-schema: the keys
belong to the SDK. Keys with no kind from any source render in the JSON
widget, which is lossless since `b6536d80` and `ec3e02f2`.

## Aliases

An alias is a second spelling the manager accepts for the same property, with
a documented preference (`props.get("mockPhotonCountMean",
props.get("mock_photon_count_mean", 800.0))` — camel wins). Collapsing the
alias into an annotation would drop it from validation, and
`additionalProperties: true` would then let an invalid snake-case value through.
So:

- The generator emits the property under its canonical spelling **and** an
  identical property under each alias, both carrying the same constraints;
  the alias copies carry `x-imswitch-alias-of: "<canonical>"` and are hidden
  from the form.
- The file is validated as it is; no normalised copy is needed.
- `_validate_cross_references` emits `manager.alias-conflict` (warning) when a
  device carries more than one spelling of the same property, naming which one
  the manager will read.
- The editor (Phase 3) shows one field, loads it from whichever spelling is
  present, and writes back to that spelling; both present → the canonical one
  is shown and edited, the other left verbatim, and the warning is visible in
  the validation panel.

Tests: snake only, camel only, both (canonical wins, warning emitted), and an
invalid value under the snake spelling rejected by `validate-setup`. No
shipped setup carries both spellings today.

## Roles: conditions evaluated once, for editor and CLI

Some properties are read not by the manager but by whichever scan designer or
controller drives the device: `GalvoScanDesigner` reads `vel_max`/`acc_max`
and every TriggerScope controller reads `conversionFactor` from any positioner
used as a scan axis; `SLMsController` reads `startConfig` from any SLM. The
requirement depends on the *consumer* and its own exemptions —
`GalvoScanDesigner` requires `vel_max`/`acc_max` only for axes whose name does
not contain `"mock"`, and 13 shipped mock positioners scan with no
`conversionFactor` at all. A fragment that says "scanning positioners require
`conversionFactor`" would make Apply insert a value into every one of them.

So a role fragment is a **diagnostic rule**, not a form requirement:

```
schemas/roles/galvo_scan_axis.json
  applies_to: positioners
  when:       forScanning == true and "mock" not in name.lower()
  requires:   vel_max, acc_max
  reads:      conversionFactor
  consumer:   imswitch.imcontrol.model.signaldesigners.GalvoScanDesigner
```

- `when` is a small predicate language (field comparisons, `name` tests,
  `scan.scanDesigner ==`), evaluated by one function in the model layer and
  called from `_validate_cross_references` — which both the CLI and the editor
  already run. Diagnostics: `role.missing-required` (error, quoting the
  consumer's own message), `role.missing-read` (note).
- Each fragment's predicate is pinned to its consumer by a test that runs the
  consumer's own check on the same fixtures (`GalvoScanDesigner`'s `missing`
  list must equal the fragment's), so the two cannot drift.
- The editor **never** marks a role property required and never seeds one. It
  shows role properties as optional fields (kind from the fragment) and
  surfaces the diagnostics in its validation panel, where they already appear
  for DAQ conflicts.
- Phase 4 ships `galvo_scan_axis` and `slm` fragments; more only when a
  consumer's rule is written down.

## Output

```
imswitch/imcontrol/model/configeditor/schemas/
  managers/<ManagerName>.json     one Draft 2020-12 schema per manager (generated)
  kinds/<kind>.json               shared top-level fields from SetupInfo (generated)
  roles/<role>.json               role diagnostic rules, with their predicate (generated)
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
    "rs232device": {"x-imswitch-kind": "string",
                    "x-imswitch-widget": "ref", "x-imswitch-ref-category": "rs232devices",
                    "x-imswitch-source": ["code:required", "code:ref"]},
    "channel":     {"x-imswitch-kind": "integer",
                    "x-imswitch-source": ["code:required", "example:int"]},
    "calibCsvPath":{"type": "string", "x-imswitch-kind": "string", "x-imswitch-widget": "path",
                    "x-imswitch-source": ["code:optional(try/except KeyError)", "code:Path()", "template:path"]},
    "frequencyMHz":{"x-imswitch-kind": "number", "x-imswitch-nullable": true,
                    "x-imswitch-source": ["code:optional", "code:nullable", "example:float"]},
    "ttlToggling": {"x-imswitch-kind": "boolean",
                    "x-imswitch-source": ["code:optional(in-guard)", "docs:type"]},
    "toggleTrueExternal": {"x-imswitch-kind": "boolean",
                    "x-imswitch-source": ["code:optional(in-guard)", "docs:type"]},
    "protocolProfile": {"x-imswitch-nullable": true,
                    "x-imswitch-source": ["code:optional", "code:nullable"]}
  }
}
```

Only `calibCsvPath` carries a validation `type`, because only there does the
code (`Path(...)`) prove one. `channel` is *displayed* as an integer and
accepts anything until an override says otherwise. `protocolProfile` has no
kind and no constraint: the JSON widget, and validation that passes.

`x-imswitch-*` is the annotation vocabulary the discovery plan's item D
proposed. This plan introduces `kind`, `widget`, `ref-category`, `aliases`,
`alias-of`, `nullable`, `required` (for `"uncertain"`) and `source`; the rest
arrive with the template-overlay merge in Phase 5.

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
   finds something, so registered core managers are as unvalidated as the
   legacy ones. Replace with `schema_for` in the registry branch and add it to
   the `legacy` / `legacy-mock` branches.

`ManagerInfo.properties_schema` keeps its meaning; only its provenance widens.

## Acceptance: what "round trip" means

The revision-3 criterion — byte-identical files — cannot be met: `_write_file`
serialises at indent 2 while 14 of the 15 shipped setups are indent 4, and
`prepare_for_save` drops an empty `others`. All 15 change bytes before any
edit today, and formatting is out of scope.

The criterion is therefore **type-strict structural equality of the in-memory
document**: for every fixture, load → open every device in the form → Apply
without touching anything → the editor's `_data` equals the parsed input under
a comparison that distinguishes `5` from `5.0` from `True` from `"5"`, `None`
from `{}`, and a missing key from a present one. Python's `==` does none of
that (`1 == 1.0 == True`), so the test ships its own `assert_json_identical`.
Serialisation is checked separately and only for what `_write_file` promises:
`json.loads(written) ` is `assert_json_identical` to `_data` minus an empty
`others`.

No exception for `"9600"` is needed: an untouched select returns the string
it was given (`ec3e02f2`); the int is written only when the operator picks an
entry, and that is an edit.

## Corpus

What the round-trip and validation tests run over. Named precisely because
"64 shipped setups" in revision 3 was a conflation with the 64 managers.

- **Shipped:** the 15 files under `imswitch/_data/user_defaults/imcontrol_setups/`,
  covering 22 managers. Fixed by name in the test, so a new file is a
  deliberate addition.
- **Synthetic, one per manager:** generated by the tool from each schema — the
  required keys with the kind's example value, plus every optional key once —
  so all 64 managers exercise the form path without hand-written files.
  Checked in beside the schemas (`schemas/fixtures/<Manager>.json`), drift-
  guarded like them.
- **Value shapes, hand-written:** a nullable number as `null`; an int beyond
  the spin box; a float with nine decimals; the strings `"null"`, `""` and
  `"5"` under string-kind keys; a string under an int-kind key; a union key
  in each of its kinds; an alias in each spelling and both; a vendor dict; a
  role-bearing mock positioner without `conversionFactor`; a stale `"9600"`
  select value. Each has a stated reason in a comment key.

`jsonschema` is added to the `test` extra so the unit lane executes validation
tests instead of skipping them; product runtime keeps it optional.

## Phases

### Phase 0 — Extractor and coverage report (no product change)

- `configeditor/extraction.py`: pure functions over `ast` — `find_props_aliases`,
  `iter_property_reads` (each read annotated with its guards and wrappers),
  `classify_requiredness`, `infer_kind`, `provable_constraints`,
  `collect_class_schema`, `merge_bases`, `infer_refs`.
- `tools/extract_manager_schemas.py --report`: the coverage table above from
  the live tree, plus the `uncertain` requiredness cases, the reads discarded
  as not tracing to the constructor's `*Info` parameter, and per property the
  inferred kind and whether validation is constrained.
- Tests in `test_configeditor_extraction.py`: one fixture snippet **per idiom
  and per guard** in the tables — including `.get` with `None`, each wrapper,
  each guard form, a base-class `.get` for a subclass subscript, and the
  provable-constraint cases (`Path()`, nested subscript, `.items()`).

**Exit criterion:** the report reproduces 54 / 188 / 68 required / 14 refs on
the current tree; `AAAOTFLaserManager` reports `calibCsvPath`, `ttlToggling`,
`toggleTrueExternal` as optional, `calibCsvPath` as the only constrained
property, and `cameraSerial` on `ThorCamTSIManager` as nullable with kind
`string` from examples; every row of the tables has a failing-then-passing
test.

*Estimate: ~1 day.*

### Phase 1 — Checked-in schemas, fixtures and the drift guard

- `--write` emits `schemas/**` and `schemas/fixtures/**` deterministically
  (sorted keys, two-space JSON, trailing newline).
- `--check` regenerates in memory and diffs against disk; exit 1 with the list
  of managers that changed.
- `test_configeditor_schemas_match_source.py`: in-memory regeneration asserted
  equal to the checked-in files — the pattern
  `test_communication_channel_contract.py` uses. Failure message names the
  command to run.
- `overrides/AAAOTFLaserManager.json` typing `protocolProfile` and
  `overrides/HamamatsuManager.json` constraining `cameraListIndex` to
  `["integer", "string"]`, so both the mechanism and the "narrow types are
  reviewed" rule are exercised from day one.
- `jsonschema` added to the `test` extra.

**Exit criterion:** adding a `props.get("newKey", 3)` to any manager without
running the tool fails CI with a message that says how to fix it; running the
tool produces a one-property diff plus one line in that manager's fixture.

*Estimate: ~0.5 day.*

### Phase 2 — Resolve schemas in the model layer; editor consumption gated

- `configeditor/resources.py`: `generated_schema_for(manager_name)` via
  `importlib.resources`, with an injectable root for tests; `schema_for()` as
  above; the alias expansion and role predicate evaluator.
- The three call sites above; alias-conflict and role diagnostics in
  `_validate_cross_references`.
- **Gate.** `build_catalog()` populates `properties_schema` from generated
  schemas only when asked (`include_generated_schemas=True`); the editor's
  `_MANAGER_CATALOG` is built without it until the Phase 3 commits flip it. The
  CLI is built with it from the start. One PR (decision 5), so the gate is an
  ordering discipline inside it, not a shipped state.
- Tests, each naming a fixture that fails **under the stated rules**:
  `SerialDacZManager` (legacy-scanned) with `"baudrate": {"a": 1}` fails the
  `int()`-derived scalar constraint; `HamamatsuManager` (registered) and its
  alias `hamamatsu.orca` with `"cameraListIndex": {"a": 1}` fail the override;
  `"cameraListIndex": "x"` **passes**, and the test says so; an APD fixture
  with `"mock_photon_count_mean": "lots"` fails under the alias spelling; a
  non-mock scanning positioner without `vel_max` yields `role.missing-required`
  from `validate-setup` and a mock one yields nothing; the existing
  `test_fake_plugin_contribution` still passes with its own schema winning
  over a generated one of the same id.

**Checkpoint (not a shipped state):** the CLI validates every manager that
has a schema, registered or not; with the gate down, the editor's behaviour is
unchanged.

*Estimate: ~1 day (was 0.5; aliases and roles).*

### Phase 3 — Schema is the type authority in the editor

Entry conditions, each with its own test before the gate is lifted:

- **Untouched stays identical** — landed (`ec3e02f2`), covered by
  `test_configeditor_field_widget_roundtrip.py`.
- **Omitted stays omitted.** `PropertyEditor` records, per field, whether the
  key was present on load; `_do_apply` writes an optional property only if it
  was present or `FieldWidget.is_touched()`. A required property is always
  written. Test: load AAAOTF without `calibCsvPath`, apply without touching
  it, the key is still absent; edit it, it is written.
- **Spelling is preserved.** Per [Aliases](#aliases). Test: the APD round
  trip with `mock_photon_count_mean: 400`.
- **Lossless JSON widget** — landed (`b6536d80`, `ec3e02f2`).

Then the type authority, in **all three consumers**:

- `normalized_fields()`: kind from `x-imswitch-kind`; when template and schema
  both define a property, the schema's kind decides the widget and the
  template's type is kept only as a **compatible refinement**
  (`select`/`multiselect` over `string`/`integer`, `ref` over `string`, `path`
  over `string`). An incompatible template type is a drift error (Phase 5) and
  the schema wins.
- `materialize_device_schema()`: for a template-backed field it currently
  propagates only `req`; it must propagate the resolved kind, `opts` (from
  `enum`), `x-imswitch-widget`/`ref-category`, `aliases` and `nullable`, or
  the form never sees the schema for the 20 templated managers.
- `build_default_device()` and the editor's legacy inline fallback: tolerate a
  template field without `type` (Phase 5 strips them) by taking the kind from
  the resolved field spec; seed a default only for a **required** property or
  one the template gives a default; never seed an optional or nullable one.
- `_infer_type_from_schema`: prefer `x-imswitch-kind`; map a union of
  `integer`/`number`/`string` kinds to the text widget (lossless since
  `ef86d1fe`); `["<kind>", null]` keeps the single-kind widget.
- Spin boxes for `integer`/`number` kinds are replaced by validated line edits
  (`QIntValidator`/`QDoubleValidator` with a locale-independent decimal point)
  so no in-range limit or decimal count exists to clamp or round an edited
  value; the text box fallback then becomes the only path.
- `x-imswitch-widget: ref` renders the existing `ref` combo with its category;
  `path` the existing path picker.
- The raw "Properties" tab shows only keys unknown to both template and
  schema.
- Lift the gate.

**Exit criterion:** opening any of the 44 untemplated managers shows typed
fields, not a raw tab; the full schema → form → Apply → new-device path is
tested for `AAAOTFLaserManager` (templated) and `ThorCamTSIManager`
(untemplated); the [round-trip](#acceptance-what-round-trip-means) holds over
the whole [corpus](#corpus).

*Estimate: ~2 days (was 1.5; line-edit widgets and the omitted/alias
bookkeeping).*

### Phase 4 — Shared kind fragments and role rules

- `kinds/<kind>.json` generated from the `SetupInfo` dataclass for that kind
  (`DetectorInfo`, `LaserInfo`, `PositionerInfo`, …): field name, annotation →
  validation `type` (annotations are enforced on load, so they are provable)
  and kind, default. Replaces the `top` lists of the category blanks.
- `roles/galvo_scan_axis.json` and `roles/slm.json` as diagnostic rules per
  [Roles](#roles-conditions-evaluated-once-for-editor-and-cli), with the
  consumer-parity tests. The editor shows role properties as optional fields
  and never requires or seeds them.

**Exit criterion:** the blank templates' `top` sections are deleted; every
top-level field in the editor is typed from a dataclass annotation; the 13
shipped mock scanners round-trip with no `conversionFactor` inserted and no
diagnostic; a non-mock scanning positioner without `vel_max` gets the
designer's own message from both CLI and editor.

*Estimate: ~0.5 day.*

### Phase 5 — Templates become overlays; drift tests

- Strip `type` from template fields where it equals the schema kind; keep
  `label`/`grp`/`tip`/`opts` and refinements.
- `test_configeditor_template_drift.py`: every template key must exist in the
  manager's schema or in an explicit pass-through allow-list (RS232's pyserial
  kwargs); every template `type` must be the schema kind or a compatible
  refinement. Would have caught APD/PMT's 9–13 undeclared keys,
  SwabianTimeTagger's missing `laser_rep_rate_mhz`, AAAOTF's `ttlToggling`.
- `test_devices_docs_drift.py`: every property in a schema appears in that
  manager's docs card (decision 3: test, do not generate).

**Exit criterion:** a template can no longer describe a property the code does
not read, nor mis-type one it does; a manager cannot gain a property without
the docs noticing.

*Estimate: ~0.5–1 day.*

### Phase 6 — Plugins (documentation only)

`tools/extract_manager_schemas.py --package vendor_plugin --write` runs the same
extraction over an installed plugin and writes into its package. Document in
`docs/devices/plugins.rst`. No core change.

## Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| A generated constraint rejects a file the manager accepts | Constraints come only from the provable table; everything narrower is a reviewed override. The corpus round-trip and `validate-setup` over the 15 shipped files must report zero errors before merge. |
| A schema marks required a key the code tolerates missing | Guard-aware rule plus `uncertain`; the corpus includes `example_sted.json`, which omits `calibCsvPath`. |
| The editor writes a value into a file that omitted it, or changes an untouched one | The invariant's five rules, each with a test, the corpus round-trip under `assert_json_identical`, and the gate. |
| An aliased key is written under the canonical spelling and shadows the saved one | Alias preservation on load and save, the both-spellings warning, the APD test. |
| A role rule diverges from its consumer | The consumer-parity test runs the consumer's own check on the same fixtures. |
| A `None` default is read as "this property is null" | `None` records nullability only. `cameraSerial` is the test case. |
| Python's `==` hides a type change in the round-trip test | `assert_json_identical` compares kinds and key presence; it has its own tests (`1` vs `1.0` vs `True`, `None` vs `{}`). |
| A read of *another* device's properties inside a manager is attributed to this manager | Only reads whose receiver traces to the constructor's `*Info` parameter (or its aliases) count. The report lists discarded reads. |
| Docs *Type* column is free text | Fixed vocabulary; unparsed text is ignored; docs are rank 5 and never constrain. |
| Non-deterministic output makes every regeneration a noisy diff | Sorted keys, fixed formatting, no timestamps; `index.json` carries source hashes. |
| `jsonschema` skips leave validation untested in CI | Added to the `test` extra; the validation tests assert the validator is present rather than skipping. |

## Decisions (recorded 2026-09-21)

1. **Name-keyed resolution now.** `schema_for()` resolves by manager name;
   registration (discovery-plan item A) stays independent.
2. **Shipped setups are a source** — for the editor preference only (revised:
   never for validation), with `example:` provenance.
3. **Docs cards are drift-tested in Phase 5**, not generated.
4. **Overrides live beside the generated files.**
5. **One PR for all phases.** See [Delivery](#delivery).
6. **`uncertain` requiredness ships as optional**, listed in the report.

## Delivery

One pull request, branch `feat/config-editor-schema-extraction`, stacked on
`feat/config-editor-from-imcontrol` until PR #34 merges (it depends on the
moved editor, the type-preservation fixes, the lossless JSON widget and the
untouched-field invariant), then rebased onto `main`.

- Commits follow phase order (0 → 5) so each exit criterion is reviewable at
  its own commit; the Phase 2 gate is down until the Phase 3 commit that lifts
  it, and the PR does not merge with it down.
- The PR description carries the coverage table before and after, generated by
  `--report`, the `uncertain` requiredness cases, and the list of properties
  whose validation is unconstrained, for the reviewer to settle with overrides
  or leave.
- Merge gate: every phase's exit criterion met, the corpus round-trip green,
  `validate-setup` over the 15 shipped setups reporting zero errors, all three
  CI lanes green with `jsonschema` present.
- Estimate: ~5.5 days of focused work (0: 1 · 1: 0.5 · 2: 1 · 3: 2 · 4: 0.5 ·
  5: 0.5–1). Phase 6 is documentation and rides along.

## Relation to the discovery plan

| Discovery-plan item | Effect of this plan |
| --- | --- |
| A. Registry metadata complete | Untouched; becomes optional for schema coverage (decision 1). |
| B. Resources out of Qt | Partially delivered: `resources.py` loads generated schemas; templates/blanks loading can follow the same path later. |
| C. CLI `ValidationContext` | Untouched; role and alias diagnostics land in the shared validator both callers already run. |
| D. Schema-driven editing | Delivered for core managers; `x-imswitch-*` vocabulary started; "preserve unsupported saved values on round trip" strengthened into the invariant above. |
| E. UX | Untouched. |
| "Can managers tell us what they expect?" | Amended: *runtime* introspection stays rejected; *static* extraction is the recommended default for core, optional for plugins — for the editor's preferences. Validation constraints remain declarative: provable-from-code or human-written. |

## Verification snapshot (2026-09-21)

Figures were measured on `feat/config-editor-from-imcontrol` with throwaway
probes and re-run for each revision; none are estimates. Revision 4 corrected
the corpus (15 files, 22 managers), verified the 13 mock scanners, the galvo
designer's by-name mock exemption, `BSC203`'s fractional `travelRangeUm`, and
that all 15 shipped files change bytes on re-serialisation. Reproduce with
Phase 0's `--report`.
