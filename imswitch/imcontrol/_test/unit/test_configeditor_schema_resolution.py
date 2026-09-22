"""Phase 2: generated schemas reach the catalog and the validator, gated.

One ``schema_for()`` decides which schema describes a manager -- its own if
it ships one, else the generated one -- and it is used on every branch of
``validate-setup``, registered and legacy-scanned alike. The catalog resolves
the same schemas only when asked, because the editor turns any
``properties_schema`` into form fields on the spot and must not until Phase 3.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from imswitch.imcontrol.model.configeditor import resources
from imswitch.imcontrol.model.configeditor.catalog import build_catalog
from imswitch.imcontrol.model.plugins.manifest import DeviceManagerContribution
from imswitch.imcontrol.model.plugins.registry import DevicePluginRegistry, build_default_registry
from imswitch.imcontrol.model.plugins.validation import schema_for, validate_setup_data

_REPO = Path(__file__).resolve().parents[4]
MANAGERS_ROOT = _REPO / "imswitch" / "imcontrol" / "model" / "managers"
SETUPS_DIR = _REPO / "imswitch" / "_data" / "user_defaults" / "imcontrol_setups"


@pytest.fixture(scope="module")
def registry():
    return build_default_registry(discover=False)


@pytest.fixture(autouse=True)
def _jsonschema_present():
    pytest.importorskip("jsonschema", reason="in the test extra; these tests must not run without it")


def _codes(report, code):
    return [d for d in report.diagnostics if d.code == code]


def _setup(section, name, manager, props, **top):
    return {section: {name: {"managerName": manager, "managerProperties": props, **top}}}


# ── the loader ────────────────────────────────────────────────────────────
class TestResources:
    def test_generated_schema_is_read_from_package_data(self):
        schema = resources.generated_schema_for("AAAOTFLaserManager")
        assert schema["title"] == "AAAOTFLaserManager managerProperties"
        assert resources.generated_schema_for("NoSuchManager") is None
        assert resources.generated_schema_for("../index") is None

    def test_an_injected_root_is_honoured_and_cached_per_root(self, tmp_path):
        (tmp_path / "managers").mkdir()
        (tmp_path / "managers" / "X.json").write_text('{"title": "X"}', encoding="utf-8")
        assert resources.generated_schema_for("X", tmp_path) == {"title": "X"}
        assert resources.generated_schema_for("X") is None
        (tmp_path / "managers" / "X.json").write_text('{"title": "changed"}', encoding="utf-8")
        assert resources.generated_schema_for("X", tmp_path) == {"title": "X"}, "cached"
        resources.clear_cache()
        assert resources.generated_schema_for("X", tmp_path) == {"title": "changed"}

    def test_index_lists_every_generated_manager(self):
        index = resources.index()
        assert index["generator_version"] >= 1
        assert "HamamatsuManager" in index["managers"]

    def test_alias_conflicts_and_canonical_spelling(self):
        schema = {"properties": {
            "mockRandomSeed": {"x-imswitch-aliases": ["mock_random_seed"]},
            "mock_random_seed": {"x-imswitch-alias-of": "mockRandomSeed"},
            "plain": {},
        }}
        assert resources.alias_conflicts(schema, {"mockRandomSeed": 1, "mock_random_seed": 2}) == \
            [("mockRandomSeed", ["mockRandomSeed", "mock_random_seed"])]
        assert resources.alias_conflicts(schema, {"mock_random_seed": 2}) == []
        assert resources.canonical_spelling(schema, "mock_random_seed") == "mockRandomSeed"
        assert resources.canonical_spelling(schema, "plain") == "plain"


# ── schema_for ────────────────────────────────────────────────────────────
class TestSchemaFor:
    def test_a_contributions_own_schema_wins(self, tmp_path):
        package = tmp_path / "vendor_plugin"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "schema.json").write_text('{"type": "object", "title": "own"}', encoding="utf-8")
        import sys
        sys.path.insert(0, str(tmp_path))
        try:
            contribution = DeviceManagerContribution(
                id="HamamatsuManager", kind="detector", display_name="v", python_name="x:Y",
                plugin_name="vendor", source_package="vendor_plugin", manager_properties_schema="schema.json",
            )
            assert schema_for("detector", "HamamatsuManager", contribution)["title"] == "own"
        finally:
            sys.path.remove(str(tmp_path))

    def test_a_contribution_without_a_schema_gets_the_generated_one_by_id(self, registry):
        contribution = registry.resolve("detector", "hamamatsu.orca")
        assert contribution is not None and contribution.id == "HamamatsuManager"
        schema = schema_for("detector", "hamamatsu.orca", contribution)
        assert schema["title"] == "HamamatsuManager managerProperties"

    def test_a_legacy_manager_gets_the_generated_one_by_name(self):
        assert schema_for("positioner", "SerialDacZManager", None)["title"] == "SerialDacZManager managerProperties"

    def test_nothing_describes_an_unknown_manager(self):
        assert schema_for("laser", "NoSuchManager", None) is None


# ── the catalog gate ──────────────────────────────────────────────────────
class TestCatalogGate:
    def test_off_by_default_so_the_editor_is_unchanged(self, registry):
        catalog = build_catalog(registry=registry, managers_root=MANAGERS_ROOT)
        assert sum(1 for info in catalog.managers() if info.properties_schema) == 0

    def test_on_request_registered_and_legacy_managers_get_schemas(self, registry):
        catalog = build_catalog(registry=registry, managers_root=MANAGERS_ROOT, include_generated_schemas=True)
        with_schema = {info.manager_name for info in catalog.managers() if info.properties_schema}
        assert len(with_schema) >= 58
        assert {"HamamatsuManager", "SerialDacZManager", "RS232Manager"} <= with_schema

    def test_rs232_manager_is_offered_now(self, registry):
        """Shipped setups select it by name; the legacy scan used to skip it as a base class."""
        catalog = build_catalog(registry=registry, managers_root=MANAGERS_ROOT)
        assert catalog.get("RS232Manager") is not None
        assert catalog.get("RS232Manager").category == "rs232devices"

    def test_a_contributions_own_schema_is_not_replaced(self, tmp_path):
        package = tmp_path / "vendor_plugin"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "schema.json").write_text('{"type": "object", "title": "own"}', encoding="utf-8")
        import sys
        sys.path.insert(0, str(tmp_path))
        try:
            registry = DevicePluginRegistry()
            registry.register(DeviceManagerContribution(
                id="HamamatsuManager", kind="detector", display_name="v", python_name="x:Y",
                plugin_name="vendor", source_package="vendor_plugin", manager_properties_schema="schema.json",
            ), is_builtin=False)
            catalog = build_catalog(registry=registry, include_legacy_scan=False, include_generated_schemas=True)
            assert catalog.get("HamamatsuManager").properties_schema["title"] == "own"
        finally:
            sys.path.remove(str(tmp_path))


# ── validate-setup, both branches ─────────────────────────────────────────
class TestValidation:
    def test_a_legacy_manager_is_validated_against_its_generated_schema(self, registry):
        """SerialDacZManager reads int(props.get("baudrate", ...)): a provable scalar."""
        bad = _setup("positioners", "z", "SerialDacZManager", {"port": "COM3", "baudrate": {"a": 1}}, axes=["Z"])
        report = validate_setup_data(bad, registry)
        assert any("baudrate" in d.message for d in _codes(report, "manager.schema"))
        good = _setup("positioners", "z", "SerialDacZManager", {"port": "COM3", "baudrate": "115200"}, axes=["Z"])
        assert _codes(validate_setup_data(good, registry), "manager.schema") == []

    @pytest.mark.parametrize("manager_name", ["HamamatsuManager", "hamamatsu.orca"])
    def test_a_registered_manager_and_its_alias_are_validated(self, registry, manager_name):
        """The narrowing comes from the reviewed override, not from the rules."""
        bad = _setup("detectors", "cam", manager_name, {"cameraListIndex": {"a": 1}, "hamamatsu": {}})
        assert any("cameraListIndex" in d.message for d in _codes(validate_setup_data(bad, registry), "manager.schema"))
        ok = _setup("detectors", "cam", manager_name, {"cameraListIndex": "x", "hamamatsu": {}})
        assert _codes(validate_setup_data(ok, registry), "manager.schema") == []

    def test_an_alias_spelling_is_validated_too(self, registry):
        """The snake_case spelling carries the same constraint as the camelCase one.

        float() proves integer-or-number-or-string, so "lots" is the manager's
        to reject at runtime; an object is what the schema itself rejects."""
        bad = _setup("detectors", "apd", "APDManager", {"mock_photon_count_mean": {"a": 1}})
        assert any("mock_photon_count_mean" in d.message for d in _codes(validate_setup_data(bad, registry), "manager.schema"))
        ok = _setup("detectors", "apd", "APDManager", {"mock_photon_count_mean": "800"})
        # APDManager has required keys this minimal device omits; only the
        # alias spelling's own verdict is under test here.
        assert not any("mock_photon_count_mean" in d.message
                       for d in _codes(validate_setup_data(ok, registry), "manager.schema"))

    def test_both_spellings_present_is_a_conflict(self, registry):
        both = _setup("detectors", "apd", "APDManager", {"mockPhotonCountMean": 400.0, "mock_photon_count_mean": 800.0})
        [conflict] = _codes(validate_setup_data(both, registry), "manager.alias-conflict")
        assert "reads 'mockPhotonCountMean'" in conflict.message
        assert conflict.path[-1] == "mockPhotonCountMean"
        one = _setup("detectors", "apd", "APDManager", {"mock_photon_count_mean": 800.0})
        assert _codes(validate_setup_data(one, registry), "manager.alias-conflict") == []

    def test_every_shipped_setup_validates_cleanly(self, registry):
        """The merge gate: generated constraints never reject a file the tree ships."""
        offending = {}
        for path in sorted(SETUPS_DIR.glob("*.json")):
            report = validate_setup_data(json.loads(path.read_text(encoding="utf-8")), registry)
            bad = [d for d in report.diagnostics if d.code in ("manager.schema", "manager.alias-conflict")]
            if bad:
                offending[path.name] = [f"{'.'.join(map(str, d.path))}: {d.message}" for d in bad]
        assert offending == {}


# ── review of Phases 1-2: a core name is not a core implementation ────────
def _vendor(python_name: str) -> DeviceManagerContribution:
    return DeviceManagerContribution(
        id="AAAOTFLaserManager", kind="laser", display_name="vendor AOTF",
        python_name=python_name, plugin_name="vendor",
    )


CORE_AOTF = "imswitch.imcontrol.model.managers.lasers.AAAOTFLaserManager:AAAOTFLaserManager"


class TestPluginsReusingCoreNames:
    """A plugin registering an unregistered core name with its own class must
    not inherit the core contract: it would be told ``channel`` and
    ``rs232device`` are required by code it does not run."""

    def test_a_plugin_with_its_own_class_gets_no_generated_schema(self):
        assert schema_for("laser", "AAAOTFLaserManager", _vendor("vendor_plugin.managers:AAAOTFLaserManager")) is None

    def test_a_plugin_running_the_core_class_gets_the_core_schema(self):
        schema = schema_for("laser", "AAAOTFLaserManager", _vendor(CORE_AOTF))
        assert schema["title"] == "AAAOTFLaserManager managerProperties"

    def test_a_core_module_with_another_class_is_another_implementation(self):
        other = "imswitch.imcontrol.model.managers.lasers.AAAOTFLaserManager:SomethingElse"
        assert schema_for("laser", "AAAOTFLaserManager", _vendor(other)) is None

    def test_a_look_alike_package_prefix_does_not_count(self):
        assert schema_for("laser", "AAAOTFLaserManager", _vendor("imswitch.imcontrol.model.managersx.A:AAAOTFLaserManager")) is None

    def test_generated_off_is_the_catalogs_gate(self):
        assert schema_for("positioner", "SerialDacZManager", None, generated=False) is None
        assert schema_for("positioner", "SerialDacZManager", None) is not None

    def test_the_catalog_uses_the_same_resolver(self):
        registry = DevicePluginRegistry()
        registry.register(_vendor("vendor_plugin.managers:AAAOTFLaserManager"), is_builtin=False)
        catalog = build_catalog(registry=registry, include_legacy_scan=False, include_generated_schemas=True)
        assert catalog.get("AAAOTFLaserManager").properties_schema is None
        registry = DevicePluginRegistry()
        registry.register(_vendor(CORE_AOTF), is_builtin=False)
        catalog = build_catalog(registry=registry, include_legacy_scan=False, include_generated_schemas=True)
        assert catalog.get("AAAOTFLaserManager").properties_schema["title"] == "AAAOTFLaserManager managerProperties"

    def test_the_validator_does_not_hold_the_plugin_to_the_core_contract(self):
        registry = DevicePluginRegistry()
        registry.register(_vendor("vendor_plugin.managers:AAAOTFLaserManager"), is_builtin=False)
        report = validate_setup_data(_setup("lasers", "l", "AAAOTFLaserManager", {}), registry)
        assert _codes(report, "manager.schema") == []
        registry = DevicePluginRegistry()
        registry.register(_vendor(CORE_AOTF), is_builtin=False)
        report = validate_setup_data(_setup("lasers", "l", "AAAOTFLaserManager", {}), registry)
        assert any("channel" in d.message for d in _codes(report, "manager.schema"))
