"""Online plugin store logic (Qt-free): version compat, status, install/uninstall.

Network is mocked by monkeypatching ``_get``; the plugins directory + sidecar
are redirected to a tmp path via ``UserFileDirs.Root``, so nothing here touches
the real registry or the user's home.
"""

import json

import pytest

import imswitch.improcess.plugins.plugin_store as store
import imswitch.improcess.plugins.user_plugins as user_plugins


@pytest.fixture
def plugins_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(user_plugins.dirtools.UserFileDirs, "Root", str(tmp_path))
    return tmp_path / "improcess_plugins"


_MANIFEST = {
    "schema_version": "1",
    "plugins": [
        {
            "id": "invert",
            "display_name": "Invert",
            "description": "Invert intensities.",
            "version": "1.0.0",
            "file": "plugins/invert.py",
            "min_improcess_version": "0.1",
        },
        {
            "id": "future",
            "display_name": "Future tool",
            "version": "2.0.0",
            "file": "plugins/future.py",
            "min_improcess_version": "999.0",  # requires a newer ImProcess
        },
    ],
}


def _fake_get(url):
    if url == store.MANIFEST_URL:
        return json.dumps(_MANIFEST).encode("utf-8")
    if url.endswith("plugins/invert.py"):
        return b"# invert plugin body\n"
    raise AssertionError(f"unexpected url {url}")


# --- version helpers -------------------------------------------------------


def test_parse_and_compare_versions():
    assert store.parse_version("0.1.0a0") == (0, 1, 0, 0)
    assert store.compare_versions("1.2.0", "1.10.0") == -1
    assert store.compare_versions("1.0", "1.0.0") == 0
    assert store.compare_versions("2.0", "1.9") == 1


def test_is_compatible_uses_running_version(monkeypatch):
    monkeypatch.setattr(store, "improcess_version", lambda: "0.1.0")
    assert store.is_compatible({"min_improcess_version": "0.1"})
    assert not store.is_compatible({"min_improcess_version": "999.0"})
    assert store.is_compatible({})  # no minimum -> always compatible


# --- manifest + status -----------------------------------------------------


def test_fetch_manifest(monkeypatch):
    monkeypatch.setattr(store, "_get", _fake_get)
    entries = store.fetch_manifest()
    assert [e["id"] for e in entries] == ["invert", "future"]


def test_status_transitions(monkeypatch):
    monkeypatch.setattr(store, "improcess_version", lambda: "0.1.0")
    invert = _MANIFEST["plugins"][0]
    future = _MANIFEST["plugins"][1]

    state = {"plugins": {}, "trust_acknowledged": False}
    assert store.status_for(invert, state) == store.NOT_INSTALLED
    assert store.status_for(future, state) == store.INCOMPATIBLE

    state["plugins"]["invert"] = {"version": "1.0.0"}
    assert store.status_for(invert, state) == store.UP_TO_DATE

    state["plugins"]["invert"] = {"version": "0.9.0"}
    assert store.status_for(invert, state) == store.UPDATE_AVAILABLE

    assert store.status_for({"id": "x", "_orphan": True}, state) == store.ORPHAN


def test_merged_entries_surfaces_orphans():
    manifest = [{"id": "invert", "display_name": "Invert"}]
    state = {"plugins": {"gone": {"display_name": "Gone", "version": "1.0"}}}
    merged = store.merged_entries(manifest, state)
    ids = {e["id"]: e for e in merged}
    assert set(ids) == {"invert", "gone"}
    assert ids["gone"]["_orphan"] is True


# --- install / uninstall ---------------------------------------------------


def test_install_writes_file_and_sidecar(plugins_dir, monkeypatch):
    monkeypatch.setattr(store, "_get", _fake_get)
    state = store.load_state()

    store.install(_MANIFEST["plugins"][0], state)

    installed_file = plugins_dir / "invert.py"
    assert installed_file.read_text() == "# invert plugin body\n"
    # Sidecar records the installed version.
    reloaded = store.load_state()
    assert reloaded["plugins"]["invert"]["version"] == "1.0.0"
    assert reloaded["plugins"]["invert"]["file"] == "invert.py"


def test_uninstall_removes_file_and_record(plugins_dir, monkeypatch):
    monkeypatch.setattr(store, "_get", _fake_get)
    state = store.load_state()
    store.install(_MANIFEST["plugins"][0], state)
    assert (plugins_dir / "invert.py").exists()

    store.uninstall("invert", state)

    assert not (plugins_dir / "invert.py").exists()
    assert "invert" not in store.load_state()["plugins"]


def test_installed_plugin_is_discoverable(plugins_dir, monkeypatch):
    """A plugin installed from the store is then found by drop-in discovery."""
    real_body = (
        "from imswitch.improcess.processors.base import Processor\n"
        "class P(Processor):\n"
        "    id = 'store.demo'\n"
        "    kinds = ('image',)\n"
        "    @property\n"
        "    def applies_to(self):\n"
        "        return lambda r: True\n"
        "    def make_param_widget(self, parent):\n"
        "        return None\n"
        "    def apply(self, result, params):\n"
        "        return result\n"
    )

    def fake_get(url):
        if url == store.MANIFEST_URL:
            return json.dumps(_MANIFEST).encode("utf-8")
        return real_body.encode("utf-8")

    monkeypatch.setattr(store, "_get", fake_get)
    state = store.load_state()
    store.install(
        {"id": "demo", "file": "plugins/demo.py", "version": "1.0.0"}, state
    )

    classes, errors = user_plugins.discover_processor_plugins(str(plugins_dir))
    assert errors == []
    assert "store.demo" in classes


def test_load_state_tolerates_corrupt_sidecar(plugins_dir):
    plugins_dir.mkdir(parents=True, exist_ok=True)
    (plugins_dir / store._SIDECAR).write_text("{ not valid json")
    state = store.load_state()
    assert state == {"plugins": {}, "trust_acknowledged": False}
