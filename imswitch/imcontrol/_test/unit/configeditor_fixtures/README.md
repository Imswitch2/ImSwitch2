# Value-shape fixtures for the config editor round trip

Hand-written devices whose *values* have shapes the editor once damaged.
`test_configeditor_type_authority.py` opens every device in the form and
applies without touching anything; the result must be type-strictly identical
to the input (`configeditor_testing.assert_json_identical`). They are the
third part of the corpus in `docs/design/plans/config-editor-schema-extraction.md`
(the other two: the shipped setups, and the generated `schemas/fixtures/`).

Each file is `{"why": "...", "setup": {<section>: {<name>: <device>}}}`.
These are editor round-trip cases, not validation cases: some deliberately
hold a value the manager's schema would flag (a string under an integer
key), because the editor's job is to leave that to validation, not to fix it
silently.

| File | Shape |
| --- | --- |
| `nullable_number_null.json` | `null` under nullable keys of kind number, string and unknown |
| `int_beyond_spin_box.json` | integers past the old spin-box clamp and past 32 bits |
| `float_nine_decimals.json` | floats the old 4-decimal spin box rounded |
| `strings_under_string_kind.json` | the strings `"null"`, `""` and `"5"` under string-kind keys |
| `string_under_int_kind.json` | a string where the manager prefers an integer |
| `union_key_each_kind.json` | Hamamatsu `cameraListIndex` as an integer and as a string |
| `alias_each_spelling_and_both.json` | APD `mockPhotonCountMean` under each spelling and under both |
| `vendor_dict.json` | pass-through dicts with keys no schema lists, nested and top-level |
| `mock_scanner_without_conversion_factor.json` | a scanning mock positioner with no properties at all |
| `stale_select_string.json` | a `"9600"` written by the old editor into a numeric select |
| `omitted_optional_keys.json` | required keys only; every optional key absent |
| `top_level_kinds.json` | an integer analog channel, integers under float template fields |
| `nested_container_shapes.json` | a template-known nested dict that is empty, and one that is `null` |
