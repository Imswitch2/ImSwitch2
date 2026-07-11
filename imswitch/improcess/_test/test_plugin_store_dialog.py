"""Light smoke tests for the online plugin store dialog (network mocked)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import imswitch.improcess.plugins.plugin_store as store
from imswitch.improcess.view.PluginStoreDialog import PluginStoreDialog


_MANIFEST = [
    {"id": "invert", "display_name": "Invert", "description": "d", "version": "1.0.0"},
    {
        "id": "future",
        "display_name": "Future",
        "version": "2.0",
        "min_improcess_version": "999.0",
    },
]


def test_dialog_populates_table_from_manifest(qtbot, monkeypatch):
    monkeypatch.setattr(store, "fetch_manifest", lambda: list(_MANIFEST))
    monkeypatch.setattr(
        store, "load_state", lambda: {"plugins": {}, "trust_acknowledged": True}
    )
    monkeypatch.setattr(store, "improcess_version", lambda: "0.1.0")

    dialog = PluginStoreDialog()
    qtbot.addWidget(dialog)

    # One row per manifest entry, sorted by display name.
    assert dialog.table.rowCount() == 2
    names = {dialog.table.item(r, 0).text() for r in range(dialog.table.rowCount())}
    assert names == {"Invert", "Future"}
    statuses = {dialog.table.item(r, 2).text() for r in range(2)}
    assert "Not installed" in statuses
    assert "Requires newer ImProcess" in statuses  # the future/incompatible one


def test_dialog_reports_offline_registry(qtbot, monkeypatch):
    def _boom():
        raise RuntimeError("no network")

    monkeypatch.setattr(store, "fetch_manifest", _boom)
    monkeypatch.setattr(
        store, "load_state", lambda: {"plugins": {}, "trust_acknowledged": True}
    )

    dialog = PluginStoreDialog()
    qtbot.addWidget(dialog)

    assert dialog.table.rowCount() == 0
    assert "Could not reach" in dialog.status_label.text()


def test_uninstall_triggers_on_change(qtbot, monkeypatch):
    monkeypatch.setattr(store, "fetch_manifest", lambda: list(_MANIFEST))
    monkeypatch.setattr(
        store,
        "load_state",
        lambda: {"plugins": {"invert": {"version": "1.0.0"}}, "trust_acknowledged": True},
    )
    monkeypatch.setattr(store, "improcess_version", lambda: "0.1.0")
    removed = []
    monkeypatch.setattr(store, "uninstall", lambda pid, state: removed.append(pid))

    changed = []
    dialog = PluginStoreDialog(on_change=lambda: changed.append(True))
    qtbot.addWidget(dialog)

    dialog._uninstall({"id": "invert"})

    assert removed == ["invert"]
    assert changed == [True]
