"""From an extracted contract to a checked-in schema, fixture and index.

The schema shape, the override mechanism, the fixtures, determinism, and the
Phase 1 exit criterion end to end: a manager that gains a property without the
tool being run fails ``--check`` with a message naming the fix, and running
the tool produces a one-property diff plus one line in the fixture.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from imswitch.imcontrol.model.configeditor import extraction as ex
from imswitch.imcontrol.model.configeditor import schemagen as sg

_REPO = Path(__file__).resolve().parents[4]


def _manager(source: str, name: str = "M") -> ex.ManagerExtraction:
    return ex.merge_manager(name, ex.extract_module(textwrap.dedent(source)))


SOURCE = '''
    class M:
        def __init__(self, laserInfo, name, **lowLevelManagers):
            props = laserInfo.managerProperties
            self.channel = props["channel"]
            self.dev = props["rs232device"]
            self._rs232 = lowLevelManagers["rs232sManager"][self.dev]
            self.calib = Path(props.get("calibCsvPath", "calib.csv"))
            self.freq = props.get("frequencyMHz", None)
            self.seed = props.get("mockRandomSeed", props.get("mock_random_seed", 7))
            self.vendor = props["vendor"]["speed"]
            if "a" in props:
                self.b = props["b"]
'''


# ── schema shape ──────────────────────────────────────────────────────────
class TestBuildSchema:
    @pytest.fixture
    def schema(self):
        return sg.build_schema(_manager(SOURCE))

    def test_envelope(self, schema):
        assert schema["$schema"] == sg.SCHEMA_DIALECT
        assert schema["title"] == "M managerProperties"
        assert schema["type"] == "object" and schema["additionalProperties"] is True
        assert schema["x-imswitch-classes"] == ["M"]

    def test_required_lists_only_unguarded_reads(self, schema):
        assert schema["required"] == ["channel", "rs232device", "vendor"]

    def test_a_kind_is_a_preference_and_a_constraint_is_a_type(self, schema):
        channel = schema["properties"]["channel"]
        assert "type" not in channel, "a bare read proves nothing"
        assert "x-imswitch-kind" not in channel
        assert channel["x-imswitch-source"] == ["code:required"]
        vendor = schema["properties"]["vendor"]
        assert vendor["type"] == "object" and vendor["x-imswitch-source"] == ["code:mapping-use", "code:required"]

    def test_path_wrapper_gives_type_kind_and_widget(self, schema):
        calib = schema["properties"]["calibCsvPath"]
        assert calib["type"] == "string"
        assert calib["x-imswitch-kind"] == "string"
        assert calib["x-imswitch-widget"] == "path"
        assert "code:Path()" in calib["x-imswitch-source"] and "code:optional(get)" in calib["x-imswitch-source"]

    def test_nullable_without_constraint_is_an_annotation_only(self, schema):
        freq = schema["properties"]["frequencyMHz"]
        assert freq["x-imswitch-nullable"] is True and "type" not in freq

    def test_nullable_with_a_constraint_adds_null_to_the_type(self):
        schema = sg.build_schema(_manager('''
            class M:
                def __init__(self, info, name):
                    self.p = Path(info.managerProperties.get("p"))
        '''))
        assert schema["properties"]["p"]["type"] == ["string", "null"]

    def test_reference_annotations(self, schema):
        dev = schema["properties"]["rs232device"]
        assert dev["x-imswitch-widget"] == "ref"
        assert dev["x-imswitch-ref-category"] == "rs232devices"
        assert dev["x-imswitch-kind"] == "string"

    def test_alias_is_emitted_under_both_spellings(self, schema):
        canonical = schema["properties"]["mockRandomSeed"]
        alias = schema["properties"]["mock_random_seed"]
        assert canonical["x-imswitch-aliases"] == ["mock_random_seed"]
        assert alias["x-imswitch-alias-of"] == "mockRandomSeed"
        assert "x-imswitch-aliases" not in alias
        assert alias["x-imswitch-kind"] == canonical["x-imswitch-kind"] == "integer"

    def test_uncertain_requiredness_is_marked_and_not_required(self, schema):
        b = schema["properties"]["b"]
        assert b["x-imswitch-required"] == "uncertain"
        assert "b" not in schema["required"]
        assert b["x-imswitch-source"] == ["code:uncertain"]

    def test_a_required_property_with_an_alias_accepts_either_spelling(self):
        """Only an override can require an aliased key: the nested .get that
        reveals the alias makes it optional. Either spelling then satisfies it."""
        schema = sg.build_schema(_manager('''
            class M:
                def __init__(self, info, name):
                    p = info.managerProperties
                    self.a = p.get("camelKey", p.get("snake_key"))
                    self.z = p["other"]
        '''), {"required": ["camelKey"]})
        assert schema["required"] == ["other"]
        assert schema["anyOf"] == [{"required": ["camelKey"]}, {"required": ["snake_key"]}]
        jsonschema = pytest.importorskip("jsonschema")
        validator = jsonschema.Draft202012Validator(schema)
        assert validator.is_valid({"other": 1, "snake_key": 2})
        assert validator.is_valid({"other": 1, "camelKey": 2})
        assert not validator.is_valid({"other": 1})

    def test_open_passthrough_is_noted(self):
        schema = sg.build_schema(_manager('''
            class M:
                def __init__(self, rs232Info, name):
                    settings = rs232Info.managerProperties
                    self.port = settings["port"]
                    self.driver = Serial(settings)
        '''))
        assert schema["x-imswitch-open-passthrough"] is True
        assert "driver" in schema["description"]

    def test_docs_description_is_carried(self):
        cards = {"M": {"x": ex.DocsField("x", "int", ("integer",), False, "The x.")}}
        manager = ex.merge_manager("M", ex.extract_module(textwrap.dedent('''
            class M:
                def __init__(self, info, name):
                    self.x = info.managerProperties["x"]
        ''')), docs_cards=cards)
        prop = sg.build_schema(manager)["properties"]["x"]
        assert prop["description"] == "The x." and prop["x-imswitch-kind"] == "integer"


# ── overrides ─────────────────────────────────────────────────────────────
class TestOverrides:
    def test_properties_deep_merge_and_win(self):
        schema = sg.build_schema(_manager(SOURCE), {
            "properties": {"channel": {"type": "integer", "minimum": 0}},
        })
        channel = schema["properties"]["channel"]
        assert channel["type"] == "integer" and channel["minimum"] == 0
        assert "override" in channel["x-imswitch-source"] and "code:required" in channel["x-imswitch-source"]

    def test_a_new_property_can_be_added(self):
        schema = sg.build_schema(_manager(SOURCE), {"properties": {"extra": {"type": "boolean"}}})
        assert schema["properties"]["extra"] == {"type": "boolean", "x-imswitch-source": ["override"]}

    def test_required_adds_and_optional_removes(self):
        schema = sg.build_schema(_manager(SOURCE), {"required": ["b"], "optional": ["vendor"]})
        assert schema["required"] == ["b", "channel", "rs232device"]

    def test_description_replaces(self):
        assert sg.build_schema(_manager(SOURCE), {"description": "D"})["description"] == "D"

    @pytest.mark.parametrize("bad", [{"propreties": {}}, {"type": "object"}, {"x-imswitch-kind": "x"}])
    def test_unknown_top_level_keys_are_refused(self, bad):
        with pytest.raises(ValueError, match="unknown top-level keys"):
            sg.build_schema(_manager(SOURCE), bad)

    def test_a_non_object_property_patch_is_refused(self):
        with pytest.raises(ValueError, match="must be an object"):
            sg.build_schema(_manager(SOURCE), {"properties": {"channel": "integer"}})

    def test_overrides_load_from_disk_by_manager_name(self, tmp_path):
        (tmp_path / "overrides").mkdir()
        (tmp_path / "overrides" / "X.json").write_text('{"required": ["k"]}', encoding="utf-8")
        assert sg.load_overrides(tmp_path) == {"X": {"required": ["k"]}}
        assert sg.load_overrides(tmp_path / "nowhere") == {}


# ── fixtures ──────────────────────────────────────────────────────────────
class TestFixtures:
    def test_every_non_alias_property_once_with_a_value_the_schema_accepts(self):
        manager = _manager(SOURCE)
        schema = sg.build_schema(manager)
        fixture = sg.build_fixture(manager, "lasers", schema)
        assert fixture["category"] == "lasers"
        props = fixture["device"]["managerProperties"]
        assert set(props) == set(schema["properties"]) - {"mock_random_seed"}
        assert props["calibCsvPath"] == "example"   # type: string
        assert props["vendor"] == {}                 # type: object
        assert props["mockRandomSeed"] == 1          # kind: integer
        assert props["frequencyMHz"] is None         # nullable, no kind
        assert props["channel"] == "example"         # nothing known: a string
        jsonschema = pytest.importorskip("jsonschema")
        jsonschema.Draft202012Validator(schema).validate(props)

    def test_example_follows_enum_then_type_then_kind(self):
        assert sg._example_for({"enum": ["a", "b"], "type": "string"}) == "a"
        assert sg._example_for({"type": ["null", "integer"]}) == 1
        assert sg._example_for({"type": "null"}) is None
        assert sg._example_for({"x-imswitch-kind": ["number", "string"]}) == 1.5
        assert sg._example_for({"x-imswitch-nullable": True}) is None
        assert sg._example_for({}) == "example"

    def test_an_overridden_enum_is_respected_by_the_fixture(self):
        manager = _manager(SOURCE)
        schema = sg.build_schema(manager, {"properties": {"channel": {"enum": [3, 4]}}})
        assert sg.build_fixture(manager, "lasers", schema)["device"]["managerProperties"]["channel"] == 3


# ── the whole set: determinism, write, check ──────────────────────────────
def _inputs(managers_root: Path, schemas_root: Path) -> sg.GenerationInputs:
    classes = ex.extract_tree(managers_root)
    names = sorted(classes)
    extractions = {name: ex.merge_manager(name, classes) for name in names}
    return sg.GenerationInputs(
        extractions=extractions, classes=classes,
        categories={name: "lasers" for name in names},
        overrides=sg.load_overrides(schemas_root),
        report=ex.coverage_report(extractions),
    )


def _tree(tmp_path: Path, source: str) -> tuple[Path, Path]:
    managers = tmp_path / "managers"
    managers.mkdir()
    (managers / "SynthManager.py").write_text(textwrap.dedent(source), encoding="utf-8")
    schemas = tmp_path / "schemas"
    return managers, schemas


SYNTH = '''
    class SynthManager:
        def __init__(self, laserInfo, name):
            self.a = laserInfo.managerProperties["a"]
            self.b = laserInfo.managerProperties.get("b", 2.5)
'''


class TestGeneration:
    def test_output_is_deterministic_and_rendered_one_way(self, tmp_path):
        managers, schemas = _tree(tmp_path, SYNTH)
        first = sg.generate_all(_inputs(managers, schemas))
        second = sg.generate_all(_inputs(managers, schemas))
        assert first == second
        assert set(first) == {"managers/SynthManager.json", "fixtures/SynthManager.json", "index.json"}
        for text in first.values():
            assert text.endswith("\n") and json.loads(text) is not None
            assert text == sg.render(json.loads(text)), "sorted keys, two-space indent"

    def test_index_carries_version_hash_coverage_and_regenerate_command(self, tmp_path):
        managers, schemas = _tree(tmp_path, SYNTH)
        index = json.loads(sg.generate_all(_inputs(managers, schemas))["index.json"])
        assert index["generator_version"] == sg.GENERATOR_VERSION
        assert index["regenerate"] == sg.REGENERATE_COMMAND
        entry = index["managers"]["SynthManager"]
        assert entry["properties"] == 2 and entry["required"] == 1 and entry["overridden"] is False
        assert len(entry["source_sha256"]) == 64
        assert index["coverage"]["keys"] == 2

    def test_write_then_check_is_clean_and_write_is_idempotent(self, tmp_path):
        managers, schemas = _tree(tmp_path, SYNTH)
        files = sg.generate_all(_inputs(managers, schemas))
        assert len(sg.write(files, schemas)) == 3
        assert sg.check(files, schemas) == []
        assert sg.write(files, schemas) == []

    def test_write_never_touches_overrides_and_removes_stale_files(self, tmp_path):
        managers, schemas = _tree(tmp_path, SYNTH)
        (schemas / "overrides").mkdir(parents=True)
        (schemas / "overrides" / "Keep.json").write_text("{}", encoding="utf-8")
        (schemas / "managers").mkdir()
        (schemas / "managers" / "Gone.json").write_text("{}", encoding="utf-8")
        written = sg.write(sg.generate_all(_inputs(managers, schemas)), schemas)
        assert "removed managers/Gone.json" in written
        assert (schemas / "overrides" / "Keep.json").read_text(encoding="utf-8") == "{}"
        with pytest.raises(ValueError, match="refusing"):
            sg.write({"overrides/X.json": "{}"}, schemas)

    def test_check_reports_missing_changed_and_stale(self, tmp_path):
        managers, schemas = _tree(tmp_path, SYNTH)
        files = sg.generate_all(_inputs(managers, schemas))
        assert sg.check(files, schemas) == [
            "missing: fixtures/SynthManager.json", "missing: index.json", "missing: managers/SynthManager.json",
        ]
        sg.write(files, schemas)
        (schemas / "managers" / "SynthManager.json").write_text("{}\n", encoding="utf-8")
        (schemas / "fixtures" / "Old.json").write_text("{}\n", encoding="utf-8")
        assert sg.check(files, schemas) == ["changed: managers/SynthManager.json", "stale: fixtures/Old.json"]


class TestExitCriterion:
    """A manager gains a property; nobody runs the tool; CI says exactly what to do."""

    def test_new_property_fails_check_and_a_write_is_a_one_line_diff(self, tmp_path):
        managers, schemas = _tree(tmp_path, SYNTH)
        sg.write(sg.generate_all(_inputs(managers, schemas)), schemas)
        before_schema = (schemas / "managers" / "SynthManager.json").read_text(encoding="utf-8")
        before_fixture = (schemas / "fixtures" / "SynthManager.json").read_text(encoding="utf-8")

        (managers / "SynthManager.py").write_text(textwrap.dedent(SYNTH) +
                                                  '        self.c = laserInfo.managerProperties.get("newKey", 3)\n',
                                                  encoding="utf-8")
        files = sg.generate_all(_inputs(managers, schemas))
        problems = sg.check(files, schemas)
        assert problems == ["changed: fixtures/SynthManager.json", "changed: index.json",
                            "changed: managers/SynthManager.json"]

        sg.write(files, schemas)
        after_schema = json.loads((schemas / "managers" / "SynthManager.json").read_text(encoding="utf-8"))
        assert set(after_schema["properties"]) - set(json.loads(before_schema)["properties"]) == {"newKey"}
        assert after_schema["properties"]["newKey"] == {
            "x-imswitch-kind": "integer",
            "x-imswitch-source": ["code:default:integer", "code:optional(get)"],
        }
        after_fixture = (schemas / "fixtures" / "SynthManager.json").read_text(encoding="utf-8")
        before_props = json.loads(before_fixture)["device"]["managerProperties"]
        after_props = json.loads(after_fixture)["device"]["managerProperties"]
        assert set(after_props) - set(before_props) == {"newKey"} and after_props["newKey"] == 1
        # One new line; the previous last property only gained JSON's trailing comma.
        added = {line.rstrip(",") for line in after_fixture.splitlines()} - \
                {line.rstrip(",") for line in before_fixture.splitlines()}
        assert added == {'      "newKey": 1'}, added
        assert sg.check(files, schemas) == []

    def test_the_tool_names_the_fix(self, tmp_path):
        """``--check`` prints the regenerate command; the drift test quotes it too."""
        import subprocess, sys
        managers, schemas = _tree(tmp_path, SYNTH)
        tool = _REPO / "tools" / "extract_manager_schemas.py"
        common = ["--managers-root", str(managers), "--schemas-root", str(schemas),
                  "--templates-dir", str(tmp_path / "no-templates"), "--no-examples", "--no-docs"]
        env = {"QT_QPA_PLATFORM": "offscreen", "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
        # The generation set comes from the catalog + templates; with neither
        # naming SynthManager the tool must still run cleanly and write nothing for it.
        result = subprocess.run([sys.executable, str(tool), "--check", *common],
                                capture_output=True, text=True, env={**env, **_pythonpath()})
        assert result.returncode in (0, 1)
        assert "Regenerate with: " + sg.REGENERATE_COMMAND in result.stdout or "in sync" in result.stdout


def _pythonpath() -> dict[str, str]:
    import os
    return {"PYTHONPATH": str(_REPO) + os.pathsep + os.environ.get("PYTHONPATH", "")}


# ── review of Phases 1-2: overrides, aliases, nesting, fixtures ───────────
NESTED = '''
    class M:
        def __init__(self, info, name):
            defaults = info.managerProperties.get("defaults", {})
            self.gain = defaults.get("gain", 1)
            self.exposure = defaults.get("exposure", 10)
'''


class TestOverridesReachEverySpelling:
    def test_an_override_reaches_every_alias_spelling(self):
        """Alias copies were made before the override: "seed": "wrong" failed, "old_seed": "wrong" passed."""
        schema = sg.build_schema(_manager(SOURCE), {"properties": {"mockRandomSeed": {"type": "integer"}}})
        alias = schema["properties"]["mock_random_seed"]
        assert alias["type"] == "integer"
        assert alias["x-imswitch-alias-of"] == "mockRandomSeed" and "x-imswitch-aliases" not in alias
        assert "override" in alias["x-imswitch-source"]
        jsonschema = pytest.importorskip("jsonschema")
        for spelling in ("mockRandomSeed", "mock_random_seed"):
            validator = jsonschema.Draft202012Validator(schema["properties"][spelling])
            assert not validator.is_valid("wrong"), spelling
            assert validator.is_valid(3), spelling

    def test_an_override_naming_an_alias_spelling_applies_on_top_of_the_copy(self):
        schema = sg.build_schema(_manager(SOURCE), {"properties": {
            "mockRandomSeed": {"type": "integer"},
            "mock_random_seed": {"description": "the old spelling"},
        }})
        alias = schema["properties"]["mock_random_seed"]
        assert alias["type"] == "integer" and alias["description"] == "the old spelling"
        assert alias["x-imswitch-alias-of"] == "mockRandomSeed"
        assert "description" not in schema["properties"]["mockRandomSeed"]

    def test_properties_stay_sorted_after_the_copies_are_added(self):
        schema = sg.build_schema(_manager(SOURCE), {"properties": {"zzz": {"type": "boolean"}}})
        assert list(schema["properties"]) == sorted(schema["properties"])


class TestOverridesMergeRecursively:
    def test_a_nested_override_keeps_siblings_and_the_existing_keys(self):
        """`prop.update()` replaced a nested `properties` wholesale."""
        schema = sg.build_schema(_manager(NESTED), {"properties": {
            "defaults": {"properties": {"gain": {"minimum": 5}}},
        }})
        defaults = schema["properties"]["defaults"]
        assert defaults["type"] == "object", "the generated constraint survives"
        assert set(defaults["properties"]) == {"gain", "exposure"}, "the sibling survives"
        gain = defaults["properties"]["gain"]
        assert gain["minimum"] == 5 and gain["x-imswitch-kind"] == "integer", "gain keeps its kind"
        assert "override" in gain["x-imswitch-source"] and "override" in defaults["x-imswitch-source"]
        assert "override" not in defaults["properties"]["exposure"]["x-imswitch-source"]

    def test_a_nested_required_list_replaces_and_a_fixture_still_satisfies_it(self):
        manager = _manager(NESTED)
        schema = sg.build_schema(manager, {"properties": {
            "defaults": {"required": ["gain"], "properties": {"gain": {"type": "integer", "minimum": 5}}},
        }})
        assert schema["properties"]["defaults"]["required"] == ["gain"]
        fixture = sg.build_fixture(manager, "detectors", schema)
        assert fixture["device"]["managerProperties"]["defaults"] == {"gain": 5, "exposure": 1}

    def test_lists_replace_rather_than_merge(self):
        schema = sg.build_schema(_manager(SOURCE), {"properties": {"calibCsvPath": {"type": ["string", "null"]}}})
        assert schema["properties"]["calibCsvPath"]["type"] == ["string", "null"]


class TestFixturesHonourConstraints:
    def test_a_bound_moves_the_example_inside_it(self):
        assert sg._example_for({"type": "integer", "minimum": 5}) == 5
        assert sg._example_for({"type": "integer", "exclusiveMinimum": 5}) == 6
        assert sg._example_for({"type": "integer", "maximum": 0}) == 0
        assert sg._example_for({"type": "integer", "exclusiveMaximum": 1}) == 0
        assert sg._example_for({"type": "integer", "minimum": 5, "multipleOf": 10}) == 10
        assert sg._example_for({"type": "number", "minimum": 2, "maximum": 3}) == 2.0
        assert sg._example_for({"type": "number", "exclusiveMinimum": 0, "maximum": 0.1}) == 0.1
        assert sg._example_for({"x-imswitch-kind": "integer", "type": ["integer", "string"], "minimum": 9}) == 9

    def test_const_examples_default_enum_in_that_order(self):
        assert sg._example_for({"const": 4, "examples": [5], "default": 6, "enum": [7]}) == 4
        assert sg._example_for({"examples": [5], "default": 6, "enum": [7]}) == 5
        assert sg._example_for({"default": 6, "enum": [7]}) == 6
        assert sg._example_for({"enum": [7]}) == 7

    def test_a_string_pattern_needs_a_hand_authored_example(self):
        with pytest.raises(sg.FixtureError, match=r"port: .*examples"):
            sg._example_for({"type": "string", "pattern": "^COM"}, "port")
        assert sg._example_for({"type": "string", "pattern": "^COM", "examples": ["COM7"]}) == "COM7"

    def test_min_length_pads_and_max_length_truncates(self):
        assert sg._example_for({"type": "string", "minLength": 10}) == "exampleexa"
        assert sg._example_for({"type": "string", "maxLength": 3}) == "exa"

    def test_an_array_with_min_items_is_filled_from_its_items_schema(self):
        assert sg._example_for({"type": "array", "minItems": 2, "items": {"type": "integer", "minimum": 3}}) == [3, 3]
        with pytest.raises(sg.FixtureError, match="items"):
            sg._example_for({"type": "array", "minItems": 1}, "list")

    def test_an_unsatisfiable_bound_names_the_property_and_the_fix(self):
        with pytest.raises(sg.FixtureError, match=r"level: no example satisfies .*examples"):
            sg._example_for({"type": "integer", "minimum": 5, "maximum": 4}, "level")

    def test_an_overridden_minimum_is_respected_by_the_fixture(self):
        """An integer override with minimum 5 generated 1 and failed the fixture test."""
        manager = _manager(SOURCE)
        schema = sg.build_schema(manager, {"properties": {"channel": {"type": "integer", "minimum": 5}}})
        assert sg.build_fixture(manager, "lasers", schema)["device"]["managerProperties"]["channel"] == 5

    def test_the_fixture_is_validated_before_it_is_written(self):
        """A constraint the example builder does not model fails at --write, naming the fix."""
        pytest.importorskip("jsonschema")
        manager = _manager(SOURCE)
        schema = sg.build_schema(manager, {"properties": {"channel": {"type": "integer", "not": {"const": 1}}}})
        with pytest.raises(sg.FixtureError, match=r"M: .*channel.*examples"):
            sg.build_fixture(manager, "lasers", schema)
        schema = sg.build_schema(manager, {"properties": {
            "channel": {"type": "integer", "not": {"const": 1}, "examples": [2]},
        }})
        assert sg.build_fixture(manager, "lasers", schema)["device"]["managerProperties"]["channel"] == 2

    def test_generate_all_refuses_rather_than_writing_a_rejected_fixture(self, tmp_path):
        pytest.importorskip("jsonschema")
        managers, schemas = _tree(tmp_path, SYNTH)
        (schemas / "overrides").mkdir(parents=True)
        (schemas / "overrides" / "SynthManager.json").write_text(
            '{"properties": {"a": {"type": "integer", "not": {"const": 1}}}}', encoding="utf-8")
        with pytest.raises(sg.FixtureError, match="SynthManager"):
            sg.generate_all(_inputs(managers, schemas))
