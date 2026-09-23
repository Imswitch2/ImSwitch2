"""The extraction rules, pinned one idiom at a time.

Each test feeds the extractor a small class written the way a real manager
is, and asserts exactly what it may conclude. The rules are the ones in
``docs/design/plans/config-editor-schema-extraction.md``; when one of them is
wrong, the failing test here should say which.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from imswitch.imcontrol.model.configeditor import extraction as ex


def _manager(source: str, name: str = "M", **kwargs) -> ex.ManagerExtraction:
    classes = ex.extract_module(textwrap.dedent(source))
    return ex.merge_manager(name, classes, **kwargs)


def _prop(source: str, key: str, name: str = "M", **kwargs) -> ex.PropertySpec:
    return _manager(source, name, **kwargs).properties[key]


# ── finding the properties object ─────────────────────────────────────────
class TestFindingTheProperties:
    def test_direct_attribute_on_the_info_parameter(self):
        spec = _prop('''
            class M:
                def __init__(self, laserInfo, name):
                    self.x = laserInfo.managerProperties["x"]
        ''', "x")
        assert spec.required == ex.REQUIRED

    @pytest.mark.parametrize("binding", [
        'props = laserInfo.managerProperties',
        'props = laserInfo.managerProperties or {}',
        'props = getattr(laserInfo, "managerProperties", {}) or {}',
        'props = dict(laserInfo.managerProperties)',
    ])
    def test_aliases_through_or_getattr_and_copies(self, binding):
        spec = _prop(f'''
            class M:
                def __init__(self, laserInfo, name):
                    {binding}
                    self.x = props["x"]
        ''', "x")
        assert spec.required == ex.REQUIRED

    def test_attribute_alias_read_in_another_method(self):
        """``self._settings = info.managerProperties`` then read later (RS232Manager)."""
        spec = _prop('''
            class M:
                def __init__(self, rs232Info, name):
                    self._settings = rs232Info.managerProperties
                def read(self):
                    return self._settings["recv_termination"]
        ''', "recv_termination")
        assert spec.required == ex.REQUIRED

    def test_info_bound_to_self_then_read(self):
        spec = _prop('''
            class M:
                def __init__(self, laserInfo, name):
                    self._laserInfo = laserInfo
                def later(self):
                    return self._laserInfo.managerProperties.get("k", 3)
        ''', "k")
        assert spec.required == ex.OPTIONAL and spec.kind == ex.KIND_INTEGER

    def test_another_devices_properties_are_discarded_not_claimed(self):
        """TriggerScopeManager reads a positioner's contract; not its own."""
        manager = _manager('''
            class M:
                def __init__(self, scopeInfo, name, targets):
                    self.own = scopeInfo.managerProperties["own"]
                    for targetInfo in targets:
                        self.v = targetInfo.managerProperties["minVolt"]
                        self.w = self.setupInfo.positioners["x"].managerProperties["maxVolt"]
        ''')
        assert set(manager.properties) == {"own"}
        assert {d.key for d in manager.discarded} == {"minVolt", "maxVolt"}
        assert any(d.receiver == "targetInfo" for d in manager.discarded)


# ── the read idioms ───────────────────────────────────────────────────────
class TestReadIdioms:
    def test_unguarded_subscript_is_required(self):
        assert _prop('''
            class M:
                def __init__(self, info, name):
                    self.x = info.managerProperties["x"]
        ''', "x").required == ex.REQUIRED

    @pytest.mark.parametrize("literal, kind", [
        ("3", ex.KIND_INTEGER), ("-3", ex.KIND_INTEGER), ("2.5", ex.KIND_NUMBER),
        ("True", ex.KIND_BOOLEAN), ("'a'", ex.KIND_STRING), ("[1, 2]", ex.KIND_ARRAY),
        ("{}", ex.KIND_OBJECT),
    ])
    def test_get_default_literal_gives_the_editor_kind_and_no_constraint(self, literal, kind):
        spec = _prop(f'''
            class M:
                def __init__(self, info, name):
                    self.x = info.managerProperties.get("x", {literal})
        ''', "x")
        assert spec.required == ex.OPTIONAL
        assert spec.kind == kind
        assert spec.kind_source == [f"code:default:{kind}"]
        assert spec.constraint is None, "a default is evidence, not a constraint"

    def test_true_is_a_boolean_not_an_integer(self):
        assert _prop('''
            class M:
                def __init__(self, info, name):
                    self.x = info.managerProperties.get("x", True)
        ''', "x").kind == ex.KIND_BOOLEAN

    @pytest.mark.parametrize("call", ['get("x", None)', 'get("x")'])
    def test_none_default_is_nullability_not_a_kind(self, call):
        spec = _prop(f'''
            class M:
                def __init__(self, info, name):
                    self.x = info.managerProperties.{call}
        ''', "x")
        assert spec.nullable is True
        assert spec.kind is None and spec.kind_source == []

    def test_membership_test_is_an_optional_read(self):
        spec = _prop('''
            class M:
                def __init__(self, info, name):
                    self.has = "x" in info.managerProperties
        ''', "x")
        assert spec.required == ex.OPTIONAL

    def test_camel_snake_fallback_is_one_property_with_an_alias(self):
        manager = _manager('''
            class M:
                def __init__(self, detectorInfo, name):
                    p = detectorInfo.managerProperties or {}
                    self.seed = p.get("mockRandomSeed", p.get("mock_random_seed", 7))
        ''')
        assert set(manager.properties) == {"mockRandomSeed"}
        spec = manager.properties["mockRandomSeed"]
        assert spec.aliases == ("mock_random_seed",)
        assert spec.kind == ex.KIND_INTEGER, "the innermost default types the property"


# ── wrappers: kind from any, constraint only where proven ─────────────────
class TestWrappers:
    @pytest.mark.parametrize("wrapper, kind, constraint", [
        ("int", ex.KIND_INTEGER, {"type": ["integer", "number", "string"]}),
        ("float", ex.KIND_NUMBER, {"type": ["integer", "number", "string"]}),
        ("bool", ex.KIND_BOOLEAN, None),
        ("str", ex.KIND_STRING, None),
        ("Path", ex.KIND_STRING, {"type": "string"}),
        ("open", ex.KIND_STRING, {"type": "string"}),
    ])
    def test_wrapper_kind_and_constraint(self, wrapper, kind, constraint):
        spec = _prop(f'''
            class M:
                def __init__(self, info, name):
                    self.x = {wrapper}(info.managerProperties["x"])
        ''', "x")
        assert spec.kind == kind
        assert spec.constraint == constraint
        if constraint:
            assert spec.constraint_source == f"code:{wrapper}()"

    def test_path_wrapper_also_picks_the_path_widget(self):
        assert _prop('''
            class M:
                def __init__(self, info, name):
                    self.p = Path(info.managerProperties.get("p", "x.csv"))
        ''', "p").widget == "path"

    def test_wrapper_around_a_get_default_keeps_the_default_kind_too(self):
        spec = _prop('''
            class M:
                def __init__(self, info, name):
                    self.b = int(info.managerProperties.get("baudrate", 115200))
        ''', "baudrate")
        assert spec.kind == ex.KIND_INTEGER
        assert spec.constraint == {"type": ["integer", "number", "string"]}


class TestProvableConstraints:
    def test_nested_subscript_proves_an_object(self):
        manager = _manager('''
            class M:
                def __init__(self, detectorInfo, name):
                    self.speed = detectorInfo.managerProperties["hamamatsu"]["readout_speed"]
        ''')
        spec = manager.properties["hamamatsu"]
        assert spec.constraint == {"type": "object"}
        assert spec.constraint_source == "code:mapping-use"
        assert "readout_speed" not in manager.properties, "vendor sub-keys are the SDK's"

    def test_items_on_a_read_proves_an_object(self):
        assert _prop('''
            class M:
                def __init__(self, detectorInfo, name):
                    for k, v in detectorInfo.managerProperties["hamamatsu"].items():
                        pass
        ''', "hamamatsu").constraint == {"type": "object"}

    def test_a_plain_read_proves_nothing(self):
        spec = _prop('''
            class M:
                def __init__(self, info, name):
                    self.x = info.managerProperties["travelRangeUm"]
        ''', "travelRangeUm")
        assert spec.constraint is None and spec.kind is None


# ── guards: when a subscript is not "required" ────────────────────────────
class TestGuards:
    @pytest.mark.parametrize("handler", ["KeyError", "LookupError", "Exception", "BaseException", ""])
    def test_try_except_that_tolerates_a_missing_key(self, handler):
        spec = _prop(f'''
            class M:
                def __init__(self, laserInfo, name):
                    try:
                        self.calib = laserInfo.managerProperties["calibCsvPath"]
                    except {handler}:
                        pass
        ''', "calibCsvPath")
        assert spec.required == ex.OPTIONAL
        assert spec.reads[0].guard.startswith("try/except")

    def test_try_except_something_else_does_not_guard(self):
        assert _prop('''
            class M:
                def __init__(self, info, name):
                    try:
                        self.x = info.managerProperties["x"]
                    except ValueError:
                        pass
        ''', "x").required == ex.REQUIRED

    def test_read_in_the_handler_or_else_is_not_guarded_by_that_try(self):
        assert _prop('''
            class M:
                def __init__(self, info, name):
                    try:
                        pass
                    except KeyError:
                        self.x = info.managerProperties["x"]
        ''', "x").required == ex.REQUIRED

    @pytest.mark.parametrize("test", [
        '"ttlToggling" in laserInfo.managerProperties',
        'laserInfo.managerProperties.get("ttlToggling")',
        '"ttlToggling" in laserInfo.managerProperties and self.ok',
    ])
    def test_membership_guard_for_the_same_key(self, test):
        spec = _prop(f'''
            class M:
                def __init__(self, laserInfo, name):
                    if {test}:
                        self.t = laserInfo.managerProperties["ttlToggling"]
                    else:
                        self.t = False
        ''', "ttlToggling")
        assert spec.required == ex.OPTIONAL

    def test_conditional_expression_guard(self):
        assert _prop('''
            class M:
                def __init__(self, info, name):
                    self.x = info.managerProperties["x"] if "x" in info.managerProperties else 1
        ''', "x").required == ex.OPTIONAL

    def test_a_guard_for_another_key_leaves_this_one_uncertain(self):
        spec = _prop('''
            class M:
                def __init__(self, info, name):
                    if "a" in info.managerProperties:
                        self.b = info.managerProperties["b"]
        ''', "b")
        assert spec.required == ex.UNCERTAIN

    def test_a_condition_unrelated_to_the_properties_keeps_it_required(self):
        assert _prop('''
            class M:
                def __init__(self, info, name):
                    if name.startswith("x"):
                        self.b = info.managerProperties["b"]
        ''', "b").required == ex.REQUIRED

    def test_get_elsewhere_for_the_same_key_makes_it_optional(self):
        assert _prop('''
            class M:
                def __init__(self, info, name):
                    self._info = info
                    self.a = info.managerProperties["k"]
                def other(self):
                    return self._info.managerProperties.get("k", 0)
        ''', "k").required == ex.OPTIONAL

    def test_one_unguarded_read_among_guarded_ones_is_required(self):
        assert _prop('''
            class M:
                def __init__(self, info, name):
                    try:
                        self.a = info.managerProperties["k"]
                    except KeyError:
                        pass
                    self.b = info.managerProperties["k"]
        ''', "k").required == ex.REQUIRED


# ── inheritance ───────────────────────────────────────────────────────────
class TestInheritance:
    SOURCE = '''
        class Base:
            def __init__(self, laserInfo, name):
                self.ports = laserInfo.managerProperties.get("digitalPorts", [])
                self.driver = laserInfo.managerProperties["digitalDriver"]

        class Child(Base):
            def __init__(self, laserInfo, name):
                super().__init__(laserInfo, name)
                self.driver = laserInfo.managerProperties["digitalDriver"]
                self.extra = laserInfo.managerProperties.get("extra", 1.5)
    '''

    def test_a_subclass_inherits_its_bases_reads(self):
        manager = _manager(self.SOURCE, "Child")
        assert set(manager.properties) == {"digitalPorts", "digitalDriver", "extra"}
        assert manager.classes == ("Child", "Base")

    def test_a_base_class_get_makes_the_subclass_subscript_optional(self):
        assert _prop('''
            class Base:
                def __init__(self, info, name):
                    self.k = info.managerProperties.get("k", 1)
            class Child(Base):
                def __init__(self, info, name):
                    self.k = info.managerProperties["k"]
        ''', "k", "Child").required == ex.OPTIONAL

    def test_a_class_with_no_reads_of_its_own_reports_its_bases(self):
        manager = _manager('''
            class Base:
                def __init__(self, info, name):
                    self.k = info.managerProperties["k"]
            class Child(Base):
                pass
        ''', "Child")
        assert set(manager.properties) == {"k"} and manager.reads_any


# ── device references and pass-through ────────────────────────────────────
class TestReferences:
    def test_two_statement_data_flow_to_the_rs232_bucket(self):
        spec = _prop('''
            class M:
                def __init__(self, laserInfo, name, **lowLevelManagers):
                    rs232 = laserInfo.managerProperties["rs232device"]
                    self._rs232 = lowLevelManagers["rs232sManager"][rs232]
        ''', "rs232device")
        assert spec.ref_category == "rs232devices"
        assert spec.widget == "ref" and spec.kind == ex.KIND_STRING

    def test_inline_reference(self):
        assert _prop('''
            class M:
                def __init__(self, positionerInfo, name, **lowLevelManagers):
                    self._rs232 = lowLevelManagers["rs232sManager"][positionerInfo.managerProperties["rs232device"]]
        ''', "rs232device").ref_category == "rs232devices"

    def test_reference_through_a_self_attribute_and_get(self):
        assert _prop('''
            class M:
                def __init__(self, info, name, **lowLevelManagers):
                    self._name = info.managerProperties.get("rs232device")
                    self._rs232 = lowLevelManagers["rs232sManager"][self._name]
        ''', "rs232device").ref_category == "rs232devices"

    def test_singleton_buckets_are_not_references(self):
        """``nidaqManager`` is one object, not a device chosen by name."""
        assert _prop('''
            class M:
                def __init__(self, info, name, **lowLevelManagers):
                    line = info.managerProperties["line"]
                    self._n = lowLevelManagers["nidaqManager"][line]
        ''', "line").ref_category is None

    @pytest.mark.parametrize("call", [
        "self._port = self._open(port, rs232Info.managerProperties)",
        "self._port = Serial(port, **settings)",
        "self._port = generateDriverClass(settings)",
    ])
    def test_the_whole_dict_handed_to_a_driver_marks_the_manager_open(self, call):
        manager = _manager(f'''
            class M:
                def __init__(self, rs232Info, name):
                    settings = rs232Info.managerProperties
                    port = settings["port"]
                    {call}
        ''')
        assert manager.open_passthrough is True
        assert set(manager.properties) == {"port"}

    def test_a_get_or_copy_is_not_a_pass_through(self):
        assert _manager('''
            class M:
                def __init__(self, info, name):
                    p = dict(info.managerProperties)
                    self.x = p.get("x", 1)
        ''').open_passthrough is False


# ── the other sources ─────────────────────────────────────────────────────
class TestOtherSources:
    def test_example_setups_type_only_what_the_code_left_untyped(self, tmp_path):
        (tmp_path / "a.json").write_text(json.dumps({
            "detectors": {"cam": {"managerName": "M", "managerProperties": {"idx": 0, "serial": "ab", "d": 2.5}}},
            "lasers": {"l": {"managerName": "M", "managerProperties": {"idx": "mock"}}},
        }), encoding="utf-8")
        kinds = ex.example_kinds_from_setups(tmp_path)
        assert kinds[("M", "idx")] == {ex.KIND_INTEGER, ex.KIND_STRING}
        manager = _manager('''
            class M:
                def __init__(self, info, name):
                    self.i = info.managerProperties["idx"]
                    self.s = info.managerProperties.get("serial", None)
                    self.d = info.managerProperties.get("d", 1)
        ''', example_kinds=kinds)
        assert manager.properties["idx"].kind == [ex.KIND_INTEGER, ex.KIND_STRING]
        assert manager.properties["idx"].kind_source == ["example:integer", "example:string"]
        assert manager.properties["serial"].kind == ex.KIND_STRING and manager.properties["serial"].nullable
        assert manager.properties["d"].kind == ex.KIND_INTEGER, "code wins over examples"
        assert all(p.constraint is None for p in manager.properties.values()), "examples never constrain"

    @pytest.mark.parametrize("text, kinds, nullable", [
        ("str", ("string",), False), ("int", ("integer",), False), ("float", ("number",), False),
        ("bool", ("boolean",), False), ("dict", ("object",), False), ("number", ("number",), False),
        ("int or str", ("integer", "string"), False), ("str or null", ("string",), True),
        ("list[str]", ("array",), False), ("dict[str, int]", ("object",), False),
        ("dict (optional)", ("object",), False), ("float, seconds", (), False),
        ("callable", (), False),
    ])
    def test_docs_type_vocabulary_is_fixed_and_never_guessed(self, text, kinds, nullable):
        assert ex.parse_docs_type(text) == (kinds, nullable)

    def test_docs_cards_are_parsed_per_manager(self, tmp_path):
        (tmp_path / "lasers.rst").write_text(textwrap.dedent('''
            AAAOTFLaserManager
            ==================

            .. list-table::
               :header-rows: 1

               * - Field
                 - Type
                 - Meaning
               * - ``ttlToggling``
                 - bool
                 - Whether the channel defaults to TTL.
               * - ``weird``
                 - callable
                 - Not a JSON thing.
        '''), encoding="utf-8")
        cards = ex.docs_cards(tmp_path)
        assert cards["AAAOTFLaserManager"]["ttlToggling"].kinds == ("boolean",)
        assert cards["AAAOTFLaserManager"]["weird"].kinds == ()
        manager = _manager('''
            class AAAOTFLaserManager:
                def __init__(self, laserInfo, name):
                    if "ttlToggling" in laserInfo.managerProperties:
                        self.t = laserInfo.managerProperties["ttlToggling"]
        ''', "AAAOTFLaserManager", docs_cards=cards)
        spec = manager.properties["ttlToggling"]
        assert spec.kind == ex.KIND_BOOLEAN and spec.kind_source == ["docs:boolean"]
        assert spec.description == "Whether the channel defaults to TTL."


# ── the report ────────────────────────────────────────────────────────────
class TestReport:
    def test_counts_and_lists(self):
        classes = ex.extract_module(textwrap.dedent('''
            class A:
                def __init__(self, info, name, **lowLevelManagers):
                    self.r = info.managerProperties["req"]
                    self.o = info.managerProperties.get("opt", 1)
                    if "z" in info.managerProperties:
                        self.u = info.managerProperties["unc"]
                    dev = info.managerProperties["rs232device"]
                    self.d = lowLevelManagers["rs232sManager"][dev]
                    self.p = Path(info.managerProperties["path"])
                    self.other = otherInfo.managerProperties["foreign"]
            class B:
                def __init__(self, info, name):
                    pass
        '''))
        extractions = {n: ex.merge_manager(n, classes) for n in ("A", "B")}
        report = ex.coverage_report(extractions)
        assert (report.managers, report.reads_any, report.with_keys) == (2, 1, 1)
        # Six keys: the ``"z" in props`` guard is itself a read of ``z``.
        # Three required: req, rs232device and path are unguarded subscripts.
        assert (report.keys, report.required, report.optional, report.uncertain) == (6, 3, 3, 1)
        assert report.refs == 1 and report.constrained == 1
        assert report.uncertain_keys == ["A.unc"]
        assert len(report.discarded) == 1
        assert report.discarded[0].startswith("A.foreign via otherInfo (line ")
        assert "A.opt" in report.unconstrained_keys and "A.path" not in report.unconstrained_keys
        text = ex.format_report(report, extractions)
        assert "A.rs232device: required" in text and "ref->rs232devices" in text
        snapshot = report.snapshot()
        assert snapshot["totals"]["keys"] == 6
        assert snapshot["managers"][0] == {"name": "A", "keys": 6, "required": 3, "uncertain": 1,
                                           "refs": 1, "constrained": 1, "unresolved": 0}


# ── review round 1: what the first extractor got wrong ────────────────────
class TestConstantsAndUnresolvedReads:
    def test_a_module_string_constant_names_the_key(self):
        manager = _manager('''
            KEY = "cameraPixelSizeUm"
            class M:
                def __init__(self, detectorInfo, name):
                    self.p = detectorInfo.managerProperties.get(KEY, None)
        ''')
        spec = manager.properties["cameraPixelSizeUm"]
        assert spec.nullable and spec.reads[0].via == "constant:KEY"

    def test_a_class_level_constant_names_the_key(self):
        manager = _manager('''
            class M:
                KEY = "port"
                def __init__(self, info, name):
                    self.p = info.managerProperties[self.KEY]
        ''')
        assert set(manager.properties) == {"port"}

    def test_a_key_the_module_does_not_define_is_reported_not_dropped(self):
        manager = _manager('''
            class M:
                def __init__(self, info, name):
                    for key in self.keys:
                        self.v = info.managerProperties[key]
        ''')
        assert manager.properties == {}
        assert len(manager.unresolved) == 1
        assert manager.unresolved[0].expr == "info.managerProperties[key]"
        assert manager.reads_any, "an unresolved read still means the manager reads properties"


class TestHelpers:
    def test_a_key_parameter_helper_is_followed_to_its_call_sites(self):
        """ThorlabsMFF: self._read_info("serial_number") reads through a method."""
        manager = _manager('''
            class M:
                def __init__(self, deviceInfo, name):
                    self.deviceInfo = deviceInfo
                    self.serial = self._read_info("serial_number")
                    self.invert = bool(self._read_info("invert", False))
                    self.names = self._read_info("state_names", None)
                def _read_info(self, key, default=None):
                    props = getattr(self.deviceInfo, "managerProperties", None) or {}
                    return props.get(key, default)
        ''')
        assert set(manager.properties) == {"serial_number", "invert", "state_names"}
        assert manager.properties["invert"].kind == ex.KIND_BOOLEAN
        assert manager.properties["serial_number"].nullable, "the helper's own default is None"
        assert manager.properties["state_names"].reads[0].via == "helper:_read_info"
        assert all(p.required == ex.OPTIONAL for p in manager.properties.values())

    def test_a_helper_called_with_a_non_literal_key_is_unresolved(self):
        manager = _manager('''
            class M:
                def __init__(self, deviceInfo, name):
                    self.deviceInfo = deviceInfo
                    self.v = self._read_info(name)
                def _read_info(self, key, default=None):
                    return self.deviceInfo.managerProperties.get(key, default)
        ''')
        assert manager.properties == {}
        assert manager.unresolved[0].reason.startswith("key passed to _read_info()")

    def test_a_key_helper_is_inherited(self):
        manager = _manager('''
            class Base:
                def __init__(self, info, name):
                    self._info = info
                def _prop(self, key):
                    return self._info.managerProperties[key]
            class Child(Base):
                def __init__(self, info, name):
                    super().__init__(info, name)
                    self.x = self._prop("x")
        ''', "Child")
        assert manager.properties["x"].required == ex.REQUIRED

    def test_a_module_function_handed_the_dict_is_followed(self):
        """DetectorManager: configuredCameraPixelSize(managerProperties) at module level."""
        manager = _manager('''
            KEY = "cameraPixelSizeUm"
            def configured(managerProperties):
                return managerProperties.get(KEY, None)
            def other(props):
                return props["unrelated"]
            class M:
                def __init__(self, detectorInfo, name):
                    self.px = configured(detectorInfo.managerProperties)
        ''')
        assert set(manager.properties) == {"cameraPixelSizeUm"}
        assert manager.properties["cameraPixelSizeUm"].reads[0].via == "function:configured"
        assert manager.open_passthrough is False, "a followed call is not a pass-through"

    def test_a_module_function_handed_the_info_is_followed(self):
        manager = _manager('''
            def px(detectorInfo):
                return float(detectorInfo.managerProperties["px"])
            class M:
                def __init__(self, detectorInfo, name):
                    self.px = px(detectorInfo)
        ''')
        assert manager.properties["px"].kind == ex.KIND_NUMBER


class TestWritesAreNotReads:
    def test_a_store_is_a_write(self):
        manager = _manager('''
            class M:
                def __init__(self, info, name):
                    info.managerProperties["created"] = 1
                    self.k = info.managerProperties["k"]
        ''')
        assert set(manager.properties) == {"k"}
        assert [(w.key, w.kind) for w in manager.writes] == [("created", "store")]

    def test_a_delete_is_a_write(self):
        manager = _manager('''
            class M:
                def __init__(self, info, name):
                    del info.managerProperties["stale"]
        ''')
        assert manager.properties == {} and manager.writes[0].kind == "delete"


class TestMappingProofs:
    def test_an_integer_index_does_not_prove_an_object(self):
        """props["channels"][0] is as happy with a list as with a dict."""
        assert _prop('''
            class M:
                def __init__(self, info, name):
                    self.c = info.managerProperties["channels"][0]
        ''', "channels").constraint is None

    def test_pop_does_not_prove_an_object(self):
        assert _prop('''
            class M:
                def __init__(self, info, name):
                    self.c = info.managerProperties["labels"].pop()
        ''', "labels").constraint is None

    @pytest.mark.parametrize("use", ['["speed"]', '.items()', '.get("x")', '.update({})'])
    def test_mapping_only_use_does(self, use):
        assert _prop(f'''
            class M:
                def __init__(self, info, name):
                    self.c = info.managerProperties["vendor"]{use}
        ''', "vendor").constraint == {"type": "object"}


class TestReceiverProvenance:
    def test_an_attribute_ending_in_info_is_not_trusted_by_its_name(self):
        manager = _manager('''
            class M:
                def __init__(self, info, name, other):
                    self.otherInfo = other
                    self.own = info.managerProperties["own"]
                    self.f = self.otherInfo.managerProperties["foreign"]
        ''')
        assert set(manager.properties) == {"own"}
        assert [(d.key, d.receiver) for d in manager.discarded] == [("foreign", "self.otherInfo")]

    def test_a_props_name_bound_in_one_method_is_not_the_parameter_of_another(self):
        manager = _manager('''
            class M:
                def __init__(self, info, name):
                    props = info.managerProperties
                    self.a = props["a"]
                def apply(self, props):
                    return props["unrelated"]
        ''')
        assert set(manager.properties) == {"a"}

    def test_an_info_parameter_of_any_method_is_this_managers_info(self):
        manager = _manager('''
            class M:
                def __init__(self, info, name):
                    pass
                def configure(self, laserInfo):
                    self.w = laserInfo.managerProperties.get("wavelength", 488)
        ''')
        assert manager.properties["wavelength"].kind == ex.KIND_INTEGER

    def test_an_attribute_bound_from_info_in_a_base_is_own_in_the_subclass(self):
        manager = _manager('''
            class Base:
                def __init__(self, laserInfo, name):
                    self._laserInfo = laserInfo
            class Child(Base):
                def later(self):
                    return self._laserInfo.managerProperties["k"]
        ''', "Child")
        assert set(manager.properties) == {"k"}


class TestNameToClassResolution:
    def _tree(self, tmp_path, files):
        for name, source in files.items():
            (tmp_path / name).write_text(textwrap.dedent(source), encoding="utf-8")
        return ex.extract_tree_indexed(tmp_path)

    def test_python_name_from_the_registry_wins(self, tmp_path):
        tree = self._tree(tmp_path, {"ThorlabsMFF_mock.py": "class MockThorlabsMFFManager:\n    pass\n"})
        assert ex.resolve_class_name("ThorlabsMFFMockManager", tree,
                                     "x.ThorlabsMFF_mock:MockThorlabsMFFManager") == "MockThorlabsMFFManager"

    def test_a_reexporting_module_resolves(self, tmp_path):
        tree = self._tree(tmp_path, {
            "ThorlabsMFF.py": "class ThorlabsMFFManager:\n    pass\n",
            "ThorlabsMFFManager.py": "from .ThorlabsMFF import ThorlabsMFFManager\n",
        })
        assert ex.resolve_class_name("ThorlabsMFFManager", tree) == "ThorlabsMFFManager"

    def test_a_module_with_one_manager_class_resolves(self, tmp_path):
        tree = self._tree(tmp_path, {"XStageManager.py": "class XStageManagerImpl:\n    pass\n"})
        assert ex.resolve_class_name("XStageManager", tree) is None, "not named like a manager"
        tree = self._tree(tmp_path, {"YManager.py": "class RealYManager:\n    pass\n"})
        assert ex.resolve_class_name("YManager", tree) == "RealYManager"

    def test_a_vendor_driver_module_stays_unresolved(self, tmp_path):
        tree = self._tree(tmp_path, {"PyCoboltManager.py": "class CoboltLaser:\n    pass\nclass Cobolt06(CoboltLaser):\n    pass\n"})
        assert ex.resolve_class_name("PyCoboltManager", tree) is None


class TestHelperAccessSemantics:
    """A helper's call sites are as optional as the helper's own read."""

    LASER = '''
        class LaserManager:
            def __init__(self, laserInfo, name):
                self._laserInfo = laserInfo
            def hasProperty(self, key):
                return key in (self._laserInfo.managerProperties or {})
            def getProperty(self, key, default=None):
                return (self._laserInfo.managerProperties or {}).get(key, default)
            def needProperty(self, key):
                return self._laserInfo.managerProperties[key]
            def safeProperty(self, key):
                try:
                    return self._laserInfo.managerProperties[key]
                except KeyError:
                    return None
        class Nidaq(LaserManager):
            def __init__(self, laserInfo, name):
                super().__init__(laserInfo, name)
                if self.hasProperty("calibCsvPath"):
                    self.calib = self.getProperty("calibCsvPath")
                self.line = self.needProperty("line")
                self.opt = self.safeProperty("opt")
                self.w = self.getProperty("wavelength", 488)
    '''

    def test_in_and_get_helpers_make_call_sites_optional(self):
        manager = _manager(self.LASER, "Nidaq")
        assert manager.properties["calibCsvPath"].required == ex.OPTIONAL
        assert {r.access for r in manager.properties["calibCsvPath"].reads} == {"in", "get"}
        assert manager.properties["wavelength"].kind == ex.KIND_INTEGER

    def test_a_bare_subscript_helper_makes_call_sites_required(self):
        assert _manager(self.LASER, "Nidaq").properties["line"].required == ex.REQUIRED

    def test_a_guarded_subscript_helper_carries_its_guard(self):
        spec = _manager(self.LASER, "Nidaq").properties["opt"]
        assert spec.required == ex.OPTIONAL
        assert spec.reads[0].guard.startswith("try/except")

    def test_helper_bodies_are_not_unresolved_reads(self):
        manager = _manager(self.LASER, "Nidaq")
        assert manager.unresolved == []
        base = _manager(self.LASER, "LaserManager")
        assert base.unresolved == [] and base.properties == {}


class TestMethodsHandedTheDict:
    def test_a_method_parameter_read_is_attributed_at_the_call_site(self):
        """PIStageManager: self._resolve_usb_description(manager_properties)."""
        manager = _manager('''
            class M:
                def __init__(self, positionerInfo, name):
                    manager_properties = positionerInfo.managerProperties or {}
                    self.usb = self._resolve(manager_properties)
                def _resolve(self, manager_properties):
                    return manager_properties.get("usb_description")
        ''')
        spec = manager.properties["usb_description"]
        assert spec.required == ex.OPTIONAL and spec.nullable
        assert spec.reads[0].via == "method:_resolve"
        assert manager.open_passthrough is False

    def test_a_method_handed_the_info_is_followed(self):
        manager = _manager('''
            class M:
                def __init__(self, positionerInfo, name):
                    self._configure(positionerInfo)
                def _configure(self, positionerInfo):
                    self.snr = positionerInfo.managerProperties["snr"]
        ''')
        assert manager.properties["snr"].required == ex.REQUIRED

    def test_a_method_called_with_something_else_is_not_followed(self):
        manager = _manager('''
            class M:
                def __init__(self, info, name, other):
                    self.x = info.managerProperties["x"]
                    self.y = self._resolve(other)
                def _resolve(self, props):
                    return props.get("foreign")
        ''')
        assert set(manager.properties) == {"x"}


class TestNestedDicts:
    THORCAM = '''
        class M:
            def __init__(self, detectorInfo, name):
                props = detectorInfo.managerProperties or {}
                defaults = props.get("defaults", {})
                self.exposure = defaults.get("exposure_us", 50000)
                self.gain = float(defaults.get("gain", 1.0))
                self.mode = defaults["operation_mode"]
                self.serial = props.get("cameraSerial", None)
    '''

    def test_sub_keys_read_through_an_alias_become_sub_properties(self):
        spec = _manager(self.THORCAM).properties["defaults"]
        assert spec.constraint == {"type": "object"} and spec.kind == ex.KIND_OBJECT
        assert set(spec.sub_properties) == {"exposure_us", "gain", "operation_mode"}
        assert spec.sub_properties["exposure_us"].kind == ex.KIND_INTEGER
        assert spec.sub_properties["gain"].kind == ex.KIND_NUMBER
        assert spec.sub_properties["operation_mode"].required == ex.REQUIRED
        assert spec.sub_properties["exposure_us"].required == ex.OPTIONAL

    def test_the_parent_is_optional_when_only_read_through_get(self):
        assert _manager(self.THORCAM).properties["defaults"].required == ex.OPTIONAL

    def test_sub_keys_are_not_top_level_properties(self):
        assert "exposure_us" not in _manager(self.THORCAM).properties

    def test_a_nested_alias_is_scoped_to_its_function(self):
        manager = _manager('''
            class M:
                def __init__(self, info, name):
                    defaults = info.managerProperties.get("defaults", {})
                    self.a = defaults.get("a", 1)
                def other(self, defaults):
                    return defaults.get("unrelated")
        ''')
        assert set(manager.properties["defaults"].sub_properties) == {"a"}

    def test_nested_sub_properties_reach_the_schema_and_the_fixture(self):
        from imswitch.imcontrol.model.configeditor import schemagen as sg
        manager = _manager(self.THORCAM)
        schema = sg.build_schema(manager)
        defaults = schema["properties"]["defaults"]
        assert defaults["type"] == "object" and defaults["required"] == ["operation_mode"]
        assert defaults["properties"]["exposure_us"]["x-imswitch-kind"] == "integer"
        fixture = sg.build_fixture(manager, "detectors", schema)
        assert fixture["device"]["managerProperties"]["defaults"] == {
            "exposure_us": 1, "gain": 1.5, "operation_mode": "example"}



class TestGuardsInsideHelpers:
    """A helper handed the dict keeps its guards; its call sites inherit them.

    Review of Phases 1-2: reads inside a module function or a method that
    receives the dict were recorded without looking at the surrounding
    ``try`` or ``if``, so ``try: return props["optional_port"] except
    KeyError: return "default"`` came out *required* -- the original
    requiredness error, back through the newly followed helper path.
    """

    def test_a_try_except_in_a_module_function_keeps_the_key_optional(self):
        manager = _manager('''
            def port_of(props):
                try:
                    return props["optional_port"]
                except KeyError:
                    return "default"
            class M:
                def __init__(self, info, name):
                    self.port = port_of(info.managerProperties)
        ''')
        spec = manager.properties["optional_port"]
        assert spec.required == ex.OPTIONAL
        assert spec.reads[0].guard == "try/except KeyError"
        assert spec.reads[0].via == "function:port_of"

    def test_an_in_guard_in_a_method_handed_the_dict(self):
        manager = _manager('''
            class M:
                def __init__(self, info, name):
                    self._resolve(info.managerProperties)
                def _resolve(self, props):
                    if "usb" in props:
                        self.usb = props["usb"]
                    self.must = props["must"]
        ''')
        assert manager.properties["usb"].required == ex.OPTIONAL
        subscript = next(r for r in manager.properties["usb"].reads if r.access == "subscript")
        assert subscript.guard == "in-guard" and subscript.via == "method:_resolve"
        assert manager.properties["must"].required == ex.REQUIRED

    def test_a_get_guard_on_the_info_parameter_counts_too(self):
        manager = _manager('''
            def level(detectorInfo):
                if detectorInfo.managerProperties.get("level"):
                    return detectorInfo.managerProperties["level"]
                return 0
            class M:
                def __init__(self, detectorInfo, name):
                    self.level = level(detectorInfo)
        ''')
        assert manager.properties["level"].required == ex.OPTIONAL

    def test_an_unmodelled_condition_on_the_parameter_is_uncertain(self):
        manager = _manager('''
            def read(props):
                if props.get("mode") == "x":
                    return props["level"]
                return None
            class M:
                def __init__(self, info, name):
                    self.level = read(info.managerProperties)
        ''')
        assert manager.properties["level"].required == ex.UNCERTAIN

    def test_a_guard_on_another_parameter_does_not_count(self):
        """A check on a second dict the helper takes says nothing about this one."""
        manager = _manager('''
            def read(props, other):
                if "k" in other:
                    return props["k"]
            class M:
                def __init__(self, info, name):
                    self.k = read(info.managerProperties, {})
        ''')
        assert manager.properties["k"].required == ex.REQUIRED
        assert manager.properties["k"].reads[0].uncertain is False

    def test_the_tree_is_unchanged_by_the_rule(self):
        """No core helper reads under a guard today; this is the synthetic case only."""
        # The drift test (test_configeditor_schemas_match_source) pins that:
        # the checked-in schemas regenerate byte-identically after this fix.
