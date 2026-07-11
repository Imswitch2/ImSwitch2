"""Browse and manage online ImProcess drop-in analysis plugins.

A thin Qt dialog over :mod:`imswitch.improcess.plugins.plugin_store`: it lists
the registry manifest, shows each plugin's install status, and offers install /
update / uninstall. A one-time trust prompt precedes the first install because
plugins run arbitrary Python at startup. On any change it invokes an
``on_change`` callback so the host can re-scan the plugins folder.

Ported from Picasso's ``PluginStoreDialog``.
"""

from __future__ import annotations

from qtpy import QtCore, QtGui, QtWidgets

from imswitch.improcess.plugins import plugin_store as store

_STATUS_TEXT = {
    store.NOT_INSTALLED: "Not installed",
    store.UP_TO_DATE: "Installed",
    store.UPDATE_AVAILABLE: "Update available",
    store.INCOMPATIBLE: "Requires newer ImProcess",
    store.ORPHAN: "Installed (unlisted)",
}


class PluginStoreDialog(QtWidgets.QDialog):
    """Browse and manage online ImProcess analysis plugins."""

    def __init__(self, parent=None, on_change=None):
        super().__init__(parent)
        self._on_change = on_change
        self.state = store.load_state()
        self.manifest: list[dict] = []

        self.setWindowTitle("Online analysis plugins")
        self.resize(720, 440)

        layout = QtWidgets.QVBoxLayout(self)

        self.status_label = QtWidgets.QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["Plugin", "Description", "Status", "Action"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.table.horizontalHeader().setSectionResizeMode(
            1, QtWidgets.QHeaderView.Stretch
        )
        layout.addWidget(self.table)

        buttons = QtWidgets.QHBoxLayout()
        repo_btn = QtWidgets.QPushButton("Open repository...")
        repo_btn.clicked.connect(
            lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl(store.REPO_URL))
        )
        refresh_btn = QtWidgets.QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh)
        close_btn = QtWidgets.QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        buttons.addWidget(repo_btn)
        buttons.addStretch()
        buttons.addWidget(refresh_btn)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

        self._refresh()

    # -- data -----------------------------------------------------------------

    def _refresh(self) -> None:
        """(Re)download the manifest and rebuild the table."""
        self.state = store.load_state()
        QtWidgets.QApplication.setOverrideCursor(QtGui.QCursor(QtCore.Qt.WaitCursor))
        try:
            self.manifest = store.fetch_manifest()
            self.status_label.setText(
                f"Plugins available from {store.REPO}. Install only plugins you "
                "trust — they run arbitrary Python at ImProcess startup."
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            self.manifest = []
            self.status_label.setText(
                "Could not reach the online plugin registry "
                f"({exc}). Check your connection and press Refresh."
            )
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        self._populate()

    def _entries(self) -> list[dict]:
        entries = store.merged_entries(self.manifest, self.state)
        return sorted(entries, key=lambda e: e.get("display_name", e["id"]).lower())

    def _populate(self) -> None:
        entries = self._entries()
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            status = store.status_for(entry, self.state)

            name_item = QtWidgets.QTableWidgetItem(
                entry.get("display_name", entry["id"])
            )
            tip = []
            if entry.get("author"):
                tip.append(f"Author: {entry['author']}")
            if entry.get("version"):
                tip.append(f"Latest version: {entry['version']}")
            if tip:
                name_item.setToolTip("\n".join(tip))
            self.table.setItem(row, 0, name_item)
            self.table.setItem(
                row, 1, QtWidgets.QTableWidgetItem(entry.get("description", ""))
            )

            status_text = _STATUS_TEXT.get(status, status)
            installed = self.state["plugins"].get(entry["id"])
            if status == store.UPDATE_AVAILABLE and installed:
                status_text += f" ({installed.get('version')} → {entry.get('version')})"
            self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(status_text))
            self.table.setCellWidget(row, 3, self._action_widget(entry, status))
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(
            1, QtWidgets.QHeaderView.Stretch
        )

    # -- per-row actions ------------------------------------------------------

    def _action_widget(self, entry: dict, status: str) -> QtWidgets.QWidget:
        container = QtWidgets.QWidget()
        box = QtWidgets.QHBoxLayout(container)
        box.setContentsMargins(2, 2, 2, 2)
        box.setSpacing(4)

        def add(label, slot):
            button = QtWidgets.QPushButton(label)
            button.clicked.connect(slot)
            box.addWidget(button)

        if status == store.NOT_INSTALLED:
            add("Install", lambda: self._install(entry))
        elif status == store.UPDATE_AVAILABLE:
            add("Update", lambda: self._install(entry))
            add("Uninstall", lambda: self._uninstall(entry))
        elif status in (store.UP_TO_DATE, store.ORPHAN):
            add("Uninstall", lambda: self._uninstall(entry))
        elif status == store.INCOMPATIBLE:
            label = QtWidgets.QLabel("—")
            label.setToolTip(
                "This plugin requires a newer version of ImProcess "
                f"(>= {entry.get('min_improcess_version')})."
            )
            box.addWidget(label)
        box.addStretch()
        return container

    def _confirm_trust(self) -> bool:
        """Show the one-time trust warning before the first install."""
        if self.state.get("trust_acknowledged"):
            return True
        reply = QtWidgets.QMessageBox.warning(
            self,
            "Install plugin?",
            "Analysis plugins are Python files that run with full access to your "
            "computer every time ImProcess starts. Only install plugins from "
            "sources you trust.\n\nContinue?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if reply != QtWidgets.QMessageBox.Yes:
            return False
        self.state["trust_acknowledged"] = True
        store.save_state(self.state)
        return True

    def _install(self, entry: dict) -> None:
        if not self._confirm_trust():
            return
        try:
            store.install(entry, self.state)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            QtWidgets.QMessageBox.critical(self, "Installation failed", str(exc))
            return
        self._notify_change()
        self._populate()

    def _uninstall(self, entry: dict) -> None:
        store.uninstall(entry["id"], self.state)
        self._notify_change()
        self._populate()

    def _notify_change(self) -> None:
        """Ask the host to re-scan the plugins folder after a change."""
        if callable(self._on_change):
            try:
                self._on_change()
            except Exception:
                pass
