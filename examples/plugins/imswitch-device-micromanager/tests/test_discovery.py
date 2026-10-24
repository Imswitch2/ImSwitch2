"""The plugin is found through its entry point (needs `pip install -e` into the environment)."""
import json
import subprocess
import sys
from importlib import resources

import pytest


def _registry():
    try:
        from imswitch.imcontrol.model.plugins.registry import get_default_registry
        return get_default_registry()
    except Exception:
        return None


def _installed():
    registry = _registry()
    return registry is not None and registry.resolve("rs232", "micromanager.core") is not None


requires_installed = pytest.mark.skipif(
    not _installed(), reason="plugin not installed (entry point undiscoverable); run `pip install -e .`")


def _run_cli(*args):
    return subprocess.run([sys.executable, "-m", "imswitch.imcontrol.model.plugins", *args],
                          capture_output=True, text=True, timeout=120)


@requires_installed
def test_cli_list_names_the_plugin():
    result = _run_cli("list")
    assert result.returncode == 0, result.stderr
    assert any(line.startswith("micromanager.core") and "[rs232]" in line
               and "imswitch-device-micromanager" in line for line in result.stdout.splitlines())


@requires_installed
def test_registry_resolves_and_loads_the_hub_class():
    from imswitch_device_micromanager.core import MicroManagerCoreManager

    registry = _registry()
    contribution = registry.resolve("rs232", "micromanager.core")
    assert contribution is not None and contribution.plugin_name == "imswitch-device-micromanager"
    assert registry.load_manager_class("rs232", "micromanager.core") is MicroManagerCoreManager


@requires_installed
def test_multimanager_resolves_the_hub_through_the_real_loader():
    from imswitch.imcontrol.model.managers.MultiManager import MultiManager

    cls = MultiManager._resolveManagerClass(
        "imswitch.imcontrol.model.managers", "rs232", "rs232", "micromanager.core")
    assert cls.__name__ == "MicroManagerCoreManager"


@requires_installed
def test_validate_setup_passes_on_the_template(tmp_path):
    template = resources.files("imswitch_device_micromanager") / "setup_templates/demo_scope.json"
    result = _run_cli("validate-setup", str(template))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAILED" not in result.stdout


@requires_installed
def test_template_builds_a_connected_hub_through_rs232_manager_loading(adapter_dir):
    """The shipped template boots as a setup: SetupInfo -> RS232sManager -> a connected hub."""
    from imswitch.imcontrol.model.SetupInfo import SetupInfo
    from imswitch.imcontrol.model.managers.RS232sManager import RS232sManager

    raw = json.loads((resources.files("imswitch_device_micromanager")
                      / "setup_templates/demo_scope.json").read_text(encoding="utf-8"))
    raw["rs232devices"]["MMCore"]["managerProperties"]["adapterPaths"] = [adapter_dir]
    info = SetupInfo.from_json(json.dumps(raw), infer_missing=True)
    manager = RS232sManager(info.rs232devices)
    try:
        hub = manager["MMCore"]
        assert hub.isConnected and hub.host.has_device("Camera")
    finally:
        manager.finalize()
