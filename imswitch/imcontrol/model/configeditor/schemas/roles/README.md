# Role rules

Hand-written, like `overrides/`: the generator never touches this directory.

A *role* is a set of `managerProperties` keys read not by the device's manager
but by whichever consumer drives the device -- `GalvoScanDesigner` reads
`vel_max`/`acc_max` from every real scanning positioner, `SLMsController` reads
`startConfig` from any SLM. No manager schema lists them, and whether they are
required depends on the consumer's own exemptions (the galvo designer skips
axes whose *name* contains `mock`; 13 shipped mock positioners scan with no
properties at all). So a role is a **diagnostic rule** evaluated by the shared
setup validator (`imswitch.imcontrol.model.configeditor.roles`), reported by
the CLI and the editor alike as `role.missing-required` (error, the consumer's
own message) and `role.missing-read` (note, only for reads with a declared
`fallback`). The editor shows a role's `properties` as optional fields on the
devices the predicate selects; it never requires or seeds one.

One file per role:

```json
{
  "role": "galvo_scan_axis",
  "title": "Scan axis (GalvoScanDesigner)",          // the form group
  "applies_to": "positioners",                       // setup section
  "consumer": "module:function that holds the same rule",
  "when": {"all": [{"setup": "scan.scanDesigner", "equals": "GalvoScanDesigner"},
                   {"field": "forScanning", "equals": true},
                   {"name": {"not_contains": "mock"}}]},
  "requires": ["vel_max", "acc_max"],
  "reads": [{"key": "conversionFactor", "fallback": "what the consumer assumes"}, {"key": "jerk_max"}],
  "message": "the consumer's own error text; {names} is the device",
  "properties": {"vel_max": {"x-imswitch-kind": "number", "description": "..."}}
}
```

Predicates: `field` (a top-level key of the device entry), `name` (the entry's
key, case-insensitive `contains`/`not_contains`/`equals`), `setup` (a dotted
path into the whole file) with `equals`/`in`/`exists`; `all`/`any`/`not`
combine; `{}` always holds. An unknown condition is an error, never true.

Every role is pinned to its consumer by a parity test that runs the
consumer's own check on the same fixtures, so the two cannot drift.
