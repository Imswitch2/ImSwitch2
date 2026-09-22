"""Phase 4: top-level device keys from the SetupInfo dataclasses; role rules.

The ``kinds/<kind>.json`` schemas are read from ``SetupInfo.py`` with ``ast``
(the attribute docstrings are invisible at runtime) and checked here against
``dataclasses.fields()`` so the reading cannot drift. The role rules are
hand-written predicates; each is held to its consumer's own check on the
same fixtures.
"""

from __future__ import annotations

import copy
import dataclasses
import importlib
import json
import textwrap
from pathlib import Path

import pytest

from imswitch.imcontrol._test.unit.configeditor_testing import shipped_setups
from imswitch.imcontrol.model.configeditor import kinds, resources, roles as roles_module, schemagen as sg
from imswitch.imcontrol.model.configeditor.defaults import build_default_device
from imswitch.imcontrol.model.configeditor.schemas import _make_label, materialize_device_schema, normalized_fields
from imswitch.imcontrol.model.plugins.registry import build_default_registry
from imswitch.imcontrol.model.plugins.validation import validate_setup_data
from imswitch.imcontrol.model.signaldesigners.GalvoScanDesigner import scan_axes_missing_limits

_REPO = Path(__file__).resolve().parents[4]
SETUP_INFO = _REPO / "imswitch" / "imcontrol" / "model" / "SetupInfo.py"
# The package re-exports the SetupInfo *class* under the module's name.
setup_info_module = importlib.import_module("imswitch.imcontrol.model.SetupInfo")


@pytest.fixture(scope="module")
def info_classes():
    return kinds.load_info_classes(SETUP_INFO)


@pytest.fixture(scope="module")
def registry():
    return build_default_registry(discover=False)


def _codes(report, prefix):
    return [d for d in report.diagnostics if d.code.startswith(prefix)]


# ── the reading of SetupInfo.py ───────────────────────────────────────────
class TestKindReader:
    @pytest.mark.parametrize("kind, class_name", sorted(kinds.KIND_INFO_CLASSES.items()))
    def test_fields_defaults_and_requiredness_match_the_runtime_dataclass(self, info_classes, kind, class_name):
        fields, chain = kinds.resolve_fields(class_name, info_classes)
        runtime = dataclasses.fields(getattr(setup_info_module, class_name))
        runtime = [f for f in runtime if not f.name.startswith("_")]
        assert [f.name for f in fields] == [f.name for f in runtime], class_name
        for read, real in zip(fields, runtime):
            has_default = real.default is not dataclasses.MISSING or real.default_factory is not dataclasses.MISSING
            assert read.required == (not has_default), (class_name, read.name)
            if real.default is not dataclasses.MISSING:
                assert read.default_is_json and read.default == real.default, (class_name, read.name)
            elif real.default_factory is not dataclasses.MISSING:
                assert read.default_is_json and read.default == real.default_factory(), (class_name, read.name)
        assert chain[0] == class_name

    def test_annotations_map_to_json_types(self):
        source = textwrap.dedent('''
            from dataclasses import dataclass, field
            from typing import Any, Dict, List, Optional, Union
            @dataclass(frozen=True, kw_only=True)
            class Base:
                managerName: str
                """ Manager class name. """
                analogChannel: Optional[Union[str, int]] = None
                managerProperties: Dict[str, Any] = field(default_factory=dict)
            @dataclass(frozen=True, kw_only=True)
            class Info(Base):
                axes: List[str]
                """ A list of axes.

                Second paragraph is not the description. """
                wavelength: Union[int, float]
                freq: Optional[int] = 0
                names: Dict[str, str] = field(default_factory=dict)
                opt: Optional[Dict[str, Any]] = None
                step: float = 1.0
                flag: bool = False
                anything: Any = None
                maybe: Optional[list] = field(default_factory=lambda: None)
                _catchAll: CatchAll = None
            class NotADataclass:
                x: int = 1
        ''')
        classes = kinds.extract_info_classes(source)
        assert set(classes) == {"Base", "Info"}
        fields, chain = kinds.resolve_fields("Info", classes)
        assert chain == ["Info", "Base"]
        by_name = {f.name: f for f in fields}
        assert list(by_name) == ["managerName", "analogChannel", "managerProperties", "axes", "wavelength",
                                 "freq", "names", "opt", "step", "flag", "anything", "maybe"]
        schema = {name: kinds.field_schema(f) for name, f in by_name.items()}
        assert schema["managerName"]["type"] == "string" and schema["managerName"]["description"] == "Manager class name."
        assert schema["analogChannel"]["type"] == ["string", "integer", "null"]
        assert schema["analogChannel"]["x-imswitch-kind"] == ["string", "integer"]
        assert schema["analogChannel"]["default"] is None and schema["analogChannel"]["x-imswitch-nullable"] is True
        assert schema["managerProperties"]["type"] == "object" and schema["managerProperties"]["default"] == {}
        assert schema["axes"]["type"] == "array" and schema["axes"]["items"] == {"type": "string"}
        assert schema["axes"]["x-imswitch-widget"] == "text" and schema["axes"]["description"] == "A list of axes."
        assert "default" not in schema["axes"] and by_name["axes"].required
        assert schema["wavelength"]["type"] == ["integer", "number"] and by_name["wavelength"].required
        assert schema["freq"]["type"] == ["integer", "null"] and schema["freq"]["default"] == 0
        assert schema["names"]["type"] == "object"
        assert schema["opt"]["type"] == ["object", "null"] and schema["opt"]["default"] is None
        assert schema["step"]["type"] == "number" and schema["step"]["default"] == 1.0
        assert schema["flag"]["type"] == "boolean" and schema["flag"]["default"] is False
        assert "type" not in schema["anything"], "Any constrains nothing"
        assert schema["maybe"]["default"] is None and schema["maybe"]["x-imswitch-nullable"] is True
        assert schema["step"]["x-imswitch-source"] == ["dataclass:Info.step: float"]

    def test_the_kind_schema_shape(self, info_classes):
        schema = kinds.build_kind_schema("positioner", info_classes, sg.SCHEMA_DIALECT)
        assert schema["title"] == "positioner device entry (PositionerInfo)"
        assert schema["x-imswitch-classes"] == ["PositionerInfo", "DeviceInfo"]
        assert schema["required"] == ["axes", "managerName"]
        assert schema["additionalProperties"] is True
        assert list(schema["properties"]) == sorted(schema["properties"])
        assert schema["properties"]["resetOnClose"]["default"] is True

    def test_every_kind_has_a_checked_in_schema_the_loader_finds(self):
        for kind in kinds.KIND_INFO_CLASSES:
            schema = resources.kind_schema_for(kind)
            assert schema is not None and schema["x-imswitch-info-class"] == kinds.KIND_INFO_CLASSES[kind], kind
        assert resources.kind_schema_for("pulse_generator") is None, "a bespoke loader, no dataclass"
        assert resources.kind_schema_for("../index") is None

    def test_kinds_are_generated_and_roles_are_not(self, tmp_path):
        with pytest.raises(ValueError, match="refusing to generate into roles/"):
            sg.write({"roles/x.json": "{}"}, tmp_path)
        assert set(resources.index()["kinds"]) == set(kinds.KIND_INFO_CLASSES)


def test_labels_read_as_words():
    assert _make_label("forAcquisition") == "For Acquisition"
    assert _make_label("valueRangeMin") == "Value Range Min"
    assert _make_label("camera_serial") == "Camera Serial"
    assert _make_label("rs232device") == "Rs232device"
    assert _make_label("triggerMode") == "Trigger Mode"


# ── the form: every top-level field typed from its annotation ─────────────
class TestTopLevelFields:
    def test_the_blanks_carry_no_top_lists_any_more(self):
        for blank in (_REPO / "imswitch" / "imcontrol" / "view" / "configeditor" / "builtin_templates").glob("*/_blank.json"):
            assert "top" not in json.loads(blank.read_text(encoding="utf-8")), blank

    def test_kind_only_fields_are_typed_and_grouped_under_device(self):
        kind_schema = resources.kind_schema_for("positioner")
        fields = {f.key: f for f in normalized_fields(template=None, json_schema=None, kind_schema=kind_schema)}
        assert set(fields) == set(kind_schema["properties"]) - {"managerName", "managerProperties"}
        assert fields["axes"].type == "text" and fields["axes"].required and not fields["axes"].required_by_schema
        assert fields["axes"].default == "", "required, no dataclass default: blank"
        assert fields["forScanning"].type == "bool" and fields["forScanning"].default is False
        assert fields["resetOnClose"].default is True
        assert fields["analogChannel"].type == "text" and fields["analogChannel"].nullable
        assert fields["physicalActuator"].type == "text" and fields["physicalActuator"].default is None, "the dataclass default"
        assert all(f.location == "top" and f.group == "Device" for f in fields.values())
        assert fields["forScanning"].tooltip == "Whether the positioner is used for scanning."

    def test_a_template_top_field_is_refined_by_the_kind(self):
        template = {"top": [
            {"key": "wavelength", "label": "Wavelength (nm)", "type": "int", "default": 488, "req": True, "grp": "Basic", "tip": "", "opts": []},
            {"key": "axes", "label": "Axes (comma-sep)", "default": "X", "req": True, "grp": "Basic", "tip": "e.g. X or X,Y", "opts": []},
        ], "props": []}
        form = materialize_device_schema(template=template, json_schema=None, kind_schema=resources.kind_schema_for("positioner"))
        top = {f["key"]: f for f in form["top"]}
        assert top["axes"]["type"] == "text" and top["axes"]["label"] == "Axes (comma-sep)" and top["axes"]["grp"] == "Basic"
        assert top["wavelength"]["type"] == "int", "a key the kind does not know keeps its template type"
        assert top["forScanning"]["grp"] == "Device" and top["forScanning"]["type"] == "bool"
        assert "schema_req" not in top["axes"]
        assert "managerName" not in top and "managerProperties" not in top

    def test_a_new_device_from_a_blank_carries_only_what_the_dataclass_requires(self):
        laser = build_default_device("SomeLaserManager", template={}, json_schema=None,
                                     kind_schema=resources.kind_schema_for("laser"))
        assert laser == {"managerName": "SomeLaserManager", "managerProperties": {},
                         "wavelength": None, "valueRangeMin": None, "valueRangeMax": None}
        positioner = build_default_device("SomeStage", template={}, json_schema=None,
                                          kind_schema=resources.kind_schema_for("positioner"))
        assert positioner == {"managerName": "SomeStage", "managerProperties": {}, "axes": []}
        detector = build_default_device("SomeCam", template={}, json_schema=None,
                                        kind_schema=resources.kind_schema_for("detector"))
        assert detector == {"managerName": "SomeCam", "managerProperties": {}}, "omitting forAcquisition means its default"


# ── role rules ────────────────────────────────────────────────────────────
GALVO = {
    "scan": {"scanDesigner": "GalvoScanDesigner"},
    "positioners": {
        "ND-GalvoX": {"managerName": "NidaqPositionerManager", "axes": ["X"], "forScanning": True,
                      "managerProperties": {"conversionFactor": 17.0, "vel_max": 0.1, "acc_max": 0.0001}},
        "ND-GalvoY": {"managerName": "NidaqPositionerManager", "axes": ["Y"], "forScanning": True,
                      "managerProperties": {"conversionFactor": 17.0}},
        "Mock Z": {"managerName": "MockPositionerManager", "axes": ["Z"], "forScanning": True, "managerProperties": {}},
        "Stage": {"managerName": "MHXYStageManager", "axes": ["X", "Y"], "forScanning": False, "managerProperties": {}},
        "Bare": {"managerName": "NidaqPositionerManager", "axes": ["A"], "forScanning": True, "managerProperties": {}},
    },
}


def _galvo_role():
    [role] = [r for r in resources.roles() if r["role"] == "galvo_scan_axis"]
    return role


class TestRolePredicates:
    def test_the_predicate_language(self):
        device = {"forScanning": True, "axes": ["X"]}
        setup = {"scan": {"scanDesigner": "GalvoScanDesigner"}}
        h = lambda cond, name="ND-GalvoX": roles_module.holds(cond, device=device, name=name, setup=setup)  # noqa: E731
        assert h({}) and h({"all": []})
        assert h({"field": "forScanning", "equals": True}) and not h({"field": "forScanning", "equals": False})
        assert h({"field": "missing", "exists": False}) and h({"field": "axes", "exists": True})
        assert h({"name": {"not_contains": "mock"}}) and not h({"name": {"not_contains": "mock"}}, name="Mock X")
        assert h({"name": {"contains": "galvo"}}) and h({"name": {"equals": "ND-GalvoX"}})
        assert h({"setup": "scan.scanDesigner", "equals": "GalvoScanDesigner"})
        assert h({"setup": "scan.scanDesigner", "in": ["GalvoScanDesigner", "Other"]})
        assert not h({"setup": "scan.missing.deeper", "exists": True})
        assert h({"any": [{"field": "forScanning", "equals": False}, {"name": {"contains": "galvo"}}]})
        assert h({"not": {"name": {"contains": "mock"}}})
        with pytest.raises(ValueError, match="unknown role condition"):
            h({"nonsense": 1})
        with pytest.raises(ValueError, match="no comparison"):
            h({"field": "forScanning"})

    def test_the_galvo_role_selects_real_scanning_axes_under_the_galvo_designer(self):
        findings = {f.name: f for f in roles_module.evaluate(_galvo_role(), GALVO)}
        assert set(findings) == {"ND-GalvoX", "ND-GalvoY", "Bare"}, "mock and non-scanning axes are exempt"
        assert findings["ND-GalvoX"].missing_required == ()
        assert findings["ND-GalvoY"].missing_required == ("vel_max", "acc_max")
        assert [(r.key, r.fallback is not None) for r in findings["Bare"].missing_reads] == [("conversionFactor", True), ("jerk_max", False)]
        other = copy.deepcopy(GALVO)
        other["scan"]["scanDesigner"] = "BetaScanDesigner"
        assert roles_module.evaluate(_galvo_role(), other) == []

    def test_parity_with_the_designers_own_check(self):
        """The rule the validator applies is the rule GalvoScanDesigner enforces."""
        for data in [GALVO, *shipped_setups().values()]:
            if (data.get("scan") or {}).get("scanDesigner") != "GalvoScanDesigner":
                continue
            info = setup_info_module.SetupInfo.from_dict(data, infer_missing=True)
            designer = sorted(scan_axes_missing_limits(info.positioners))
            rule = sorted(f.name for f in roles_module.evaluate(_galvo_role(), data) if f.missing_required)
            assert designer == rule
        assert sorted(scan_axes_missing_limits(
            setup_info_module.SetupInfo.from_dict(GALVO, infer_missing=True).positioners)) == ["Bare", "ND-GalvoY"]

    def test_the_slm_role_reads_what_the_controller_reads(self):
        [role] = [r for r in resources.roles() if r["role"] == "slm"]
        assert role["requires"] == [] and [r["key"] for r in role["reads"]] == ["startConfig"]
        controller = (_REPO / "imswitch" / "imcontrol" / "controller" / "controllers" / "SLMsController.py").read_text(encoding="utf-8")
        assert 'managerProperties.get("startConfig")' in controller
        findings = roles_module.evaluate(role, {"slms": {"slm": {"managerName": "X", "managerProperties": {}}}})
        assert [f.missing_required for f in findings] == [()] and findings[0].missing_reads[0].fallback is None


class TestRoleDiagnostics:
    def test_the_consumer_message_reaches_the_validator(self, registry):
        report = validate_setup_data(GALVO, registry)
        errors = {d.path[1]: d for d in _codes(report, "role.missing-required")}
        assert set(errors) == {"ND-GalvoY", "Bare"}
        assert errors["ND-GalvoY"].severity == "error"
        assert "GalvoScanDesigner requires 'vel_max'" in errors["ND-GalvoY"].message
        assert "['ND-GalvoY']" in errors["ND-GalvoY"].message
        notes = _codes(report, "role.missing-read")
        assert [d.path for d in notes] == [("positioners", "Bare", "managerProperties", "conversionFactor")]
        assert notes[0].severity == "note" and "assumes a conversion factor of 1" in notes[0].message

    def test_another_designer_gets_no_role_diagnostic(self, registry):
        other = copy.deepcopy(GALVO)
        other["scan"]["scanDesigner"] = "BetaScanDesigner"
        assert _codes(validate_setup_data(other, registry), "role.") == []

    def test_the_mock_scanners_the_tree_ships_get_no_diagnostic(self, registry):
        mocks = 0
        for name, setup in shipped_setups().items():
            for device_name, device in (setup.get("positioners") or {}).items():
                if device.get("managerName") == "MockPositionerManager" and device.get("forScanning"):
                    mocks += 1
            assert _codes(validate_setup_data(setup, registry), "role.") == [], name
        assert mocks == 13


class TestKindDiagnostics:
    def test_a_wrongly_typed_top_level_key_is_a_warning(self, registry):
        data = {"lasers": {"l": {"managerName": "NidaqLaserManager", "wavelength": "488",
                                 "valueRangeMin": 0, "valueRangeMax": 1, "managerProperties": {}}}}
        [warning] = _codes(validate_setup_data(data, registry), "kind.schema")
        assert warning.severity == "warning" and warning.path == ("lasers", "l")
        assert "wavelength" in warning.message
        data["lasers"]["l"]["wavelength"] = 488
        assert _codes(validate_setup_data(data, registry), "kind.schema") == []

    def test_a_missing_required_key_is_a_warning_and_the_stand_is_covered(self, registry):
        data = {"positioners": {"p": {"managerName": "MockPositionerManager", "managerProperties": {}}}}
        [warning] = _codes(validate_setup_data(data, registry), "kind.schema")
        assert "'axes' is a required property" in warning.message
        data = {"microscopeStand": {"managerName": "MockStandManager", "managerProperties": "no"}}
        warnings = _codes(validate_setup_data(data, registry), "kind.schema")
        assert warnings and warnings[0].path == ("microscopeStand",)

    def test_unknown_top_level_keys_are_tolerated(self, registry):
        data = {"detectors": {"d": {"managerName": "APDManager", "vendorTop": {"x": 1},
                                    "managerProperties": {"ctrInputLine": "Dev1/ctr0", "terminal": "PFI0"}}}}
        assert _codes(validate_setup_data(data, registry), "kind.schema") == []
