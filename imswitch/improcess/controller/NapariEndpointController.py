"""Send results to napari plugins, and take layers back on request.

Everything that touches the viewer happens here, on the GUI thread, in
response to a signal. The only work that runs elsewhere is exporting a
result to a file for a reader plugin, which can take a while for a large
stack; the worker hands the path back through a signal and the controller
opens it. Sessions (:mod:`~imswitch.improcess.model.napari_sessions`) own
whatever a send created, so closing one removes exactly its layers, dock,
temp files and detached viewer and nothing else.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from qtpy import QtCore

from imswitch.imcommon.model import initLogger
from imswitch.improcess.model.napari_endpoints import (
    InstalledPlugins,
    NapariEndpoint,
    availability,
    discover_endpoints,
    endpoints_for,
    export_result,
    inspect_installed_plugins,
    resolve_widget,
)
from imswitch.improcess.model.napari_import import (
    NotImportable,
    import_layer,
    snapshot_layer,
)
from imswitch.improcess.model.napari_layers import NotLayerable, result_to_layer_data
from imswitch.improcess.model.napari_sessions import SessionRegistry


class _ExportWorker(QtCore.QObject):
    """Runs one export callable off the GUI thread and reports back."""

    sigDone = QtCore.Signal(str, object)     # session uid, Path
    sigFailed = QtCore.Signal(str, str)      # session uid, message

    def __init__(self, session_uid: str, job: Callable[[], Path], parent=None):
        super().__init__(parent)
        self._uid = session_uid
        self._job = job

    @QtCore.Slot()
    def run(self) -> None:
        try:
            path = self._job()
        except Exception as exc:  # noqa: BLE001 - reported to the user
            self.sigFailed.emit(self._uid, str(exc))
            return
        self.sigDone.emit(self._uid, Path(path))


def _threaded_export_runner(session_uid, job, on_done, on_failed, keepalive: list):
    """Default runner: a QThread per export; the controller keeps the refs."""
    thread = QtCore.QThread()
    worker = _ExportWorker(session_uid, job)
    worker.moveToThread(thread)
    worker.sigDone.connect(on_done)
    worker.sigFailed.connect(on_failed)
    worker.sigDone.connect(thread.quit)
    worker.sigFailed.connect(thread.quit)
    thread.started.connect(worker.run)
    entry = (thread, worker)
    keepalive.append(entry)
    thread.finished.connect(lambda: keepalive.remove(entry) if entry in keepalive else None)
    thread.start()


def _default_add_layer_data(viewer, layer_data):
    """Add ``(data, kwargs, layer_type)`` tuples to ``viewer``; returns the layers."""
    from napari.layers import Layer

    layers = []
    for data, kwargs, layer_type in layer_data:
        layer = Layer.create(data, kwargs, layer_type)
        layers.append(viewer.add_layer(layer))
    return layers


def _default_detached_viewer():
    import napari

    return napari.Viewer()


class NapariEndpointController(QtCore.QObject):
    """Owns the "napari plugins" menu, the endpoint sessions and the import path."""

    def __init__(
        self,
        commChannel,
        mainView,
        reconstructionController,
        *,
        processing_config: dict | None = None,
        installed: InstalledPlugins | None = None,
        registry: SessionRegistry | None = None,
        export_runner=None,
        add_layer_data=None,
        detached_viewer_factory=None,
        parent=None,
    ):
        super().__init__(parent)
        self._logger = initLogger(self)
        self._commChannel = commChannel
        self._mainView = mainView
        self._reconstructionController = reconstructionController
        self._config = dict(processing_config or {})
        self._installed = installed
        self._endpoints: list[NapariEndpoint] = []
        self._registry = registry or SessionRegistry()
        self._export_runner = export_runner or self._run_export_threaded
        self._add_layer_data = add_layer_data or _default_add_layer_data
        self._detached_viewer_factory = detached_viewer_factory or _default_detached_viewer
        self._keepalive: list = []
        self._menu = None
        self._layerEventsConnected = False

        menu_getter = getattr(mainView, "napariPluginsMenu", None)
        if callable(menu_getter):
            self._menu = menu_getter()
            if self._menu is not None:
                self._menu.aboutToShow.connect(self.rebuildMenu)
        closing = getattr(mainView, "sigClosing", None)
        if closing is not None:
            closing.connect(self.closeAll)
        self.refreshEndpoints()

    # -- discovery ------------------------------------------------------------

    @property
    def endpoints(self) -> list[NapariEndpoint]:
        return list(self._endpoints)

    @property
    def registry(self) -> SessionRegistry:
        return self._registry

    def refreshEndpoints(self, *, rescan: bool = False) -> None:
        if rescan or self._installed is None:
            self._installed = inspect_installed_plugins()
        self._endpoints, errors = discover_endpoints(installed=self._installed, config=self._config)
        for error in errors:
            self._logger.warning("Ignored napari endpoint config entry: %s", error)

    def offeredEndpoints(self, result) -> list[tuple[NapariEndpoint, bool, str]]:
        """``(endpoint, available, reason)`` for every endpoint a result may go to."""
        if result is None:
            return []
        offered = []
        for endpoint in endpoints_for(result, self._endpoints):
            ok, reason = availability(endpoint, self._installed or InstalledPlugins())
            offered.append((endpoint, ok, reason))
        return offered

    # -- menu -------------------------------------------------------------------

    def rebuildMenu(self) -> None:
        menu = self._menu
        if menu is None:
            return
        menu.clear()
        result = self._currentResult()
        send = menu.addMenu("Send current result to")
        offered = self.offeredEndpoints(result)
        if result is None:
            send.setEnabled(False)
            send.setTitle("Send current result to (no result selected)")
        elif not offered:
            send.setEnabled(False)
            send.setTitle("Send current result to (nothing accepts this result kind)")
        for endpoint, ok, reason in offered:
            action = send.addAction(endpoint.label)
            action.setEnabled(ok)
            action.setToolTip(endpoint.description or reason)
            if not ok:
                action.setText(f"{endpoint.label} — {reason}")
            action.triggered.connect(
                lambda _checked=False, e=endpoint, r=result: self.sendTo(e, r)
            )
        menu.addSeparator()
        import_action = menu.addAction("Import layer from napari as result...")
        import_action.triggered.connect(lambda _checked=False: self.importLayer())
        sessions = self._registry.sessions()
        sessions_menu = menu.addMenu(f"Endpoint sessions ({len(sessions)})")
        sessions_menu.setEnabled(bool(sessions))
        for session in sessions:
            action = sessions_menu.addAction(f"Close: {session.label}")
            action.triggered.connect(
                lambda _checked=False, uid=session.uid: self.closeSession(uid)
            )
        close_all = menu.addAction("Close all endpoint sessions")
        close_all.setEnabled(bool(sessions))
        close_all.triggered.connect(lambda _checked=False: self.closeAll())
        menu.addSeparator()
        install = menu.addAction("Install napari plugins...")
        install.triggered.connect(lambda _checked=False: self.openPluginInstaller())
        rescan = menu.addAction("Rescan installed napari plugins")
        rescan.triggered.connect(lambda _checked=False: self.refreshEndpoints(rescan=True))

    # -- sending ----------------------------------------------------------------

    def sendTo(self, endpoint: NapariEndpoint, result=None):
        """Send ``result`` (default: the current one) to ``endpoint``; returns the session."""
        result = result if result is not None else self._currentResult()
        if result is None:
            self._status("No result selected.")
            return None
        ok, reason = availability(endpoint, self._installed or InstalledPlugins())
        if not ok:
            self._status(reason)
            return None
        session = self._registry.open(endpoint, result)
        try:
            if endpoint.lane == "dock":
                self._openDock(session, endpoint, result)
            else:
                self._startExport(session, endpoint, result)
        except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
            self._logger.exception("Could not send %s to %s", result, endpoint.id)
            self._registry.mark_failed(session, str(exc))
            self._status(f"Could not send to {endpoint.label}: {exc}")
            return None
        return session

    def _openDock(self, session, endpoint, result) -> None:
        viewer = self._viewer()
        if viewer is None:
            raise RuntimeError("no napari viewer is available")
        try:
            layer_data = result_to_layer_data(
                result, session_uid=session.uid, include_preview=endpoint.include_preview
            )
        except NotLayerable as exc:
            raise RuntimeError(str(exc)) from exc
        layers = self._add_layer_data(viewer, layer_data)
        widget_name = resolve_widget(endpoint, self._installed or InstalledPlugins()) or endpoint.widget_name
        dock, widget = viewer.window.add_plugin_dock_widget(endpoint.plugin_name, widget_name)
        self._registry.mark_open(session, layers=layers, dock=dock, widget=widget)
        self._connectLayerEvents(viewer)
        self._status(f"Sent {result.name} to {endpoint.label}.")

    def _startExport(self, session, endpoint, result) -> None:
        directory = self._registry.temp_dir_for(session)
        self._registry.mark_exporting(session)
        self._status(f"Exporting {result.name} for {endpoint.label}...")

        def job():
            return export_result(endpoint, result, directory)

        self._export_runner(session.uid, job, self._onExportDone, self._onExportFailed)

    def _run_export_threaded(self, session_uid, job, on_done, on_failed):
        _threaded_export_runner(session_uid, job, on_done, on_failed, self._keepalive)

    @QtCore.Slot(str, object)
    def _onExportDone(self, session_uid: str, path) -> None:
        session = self._registry.get(session_uid)
        if session is None or session.state != "exporting":
            return
        endpoint = session.endpoint
        try:
            if endpoint.lane == "reader":
                viewer = self._viewer()
                if viewer is None:
                    raise RuntimeError("no napari viewer is available")
                layers = list(viewer.open(str(path), plugin=endpoint.reader_plugin) or [])
                for layer in layers:
                    try:
                        layer.metadata["endpoint_session_uid"] = session.uid
                    except Exception:
                        pass
                self._registry.mark_open(session, files=[Path(path)], layers=layers)
                self._connectLayerEvents(viewer)
            else:
                detached = self._detached_viewer_factory()
                detached.open(str(path), plugin=endpoint.reader_plugin)
                self._registry.mark_open(session, files=[Path(path)], viewer=detached)
                self._watchDetached(session.uid, detached)
            self._status(f"Opened {Path(path).name} with {endpoint.reader_plugin}.")
        except Exception as exc:  # noqa: BLE001
            self._logger.exception("Reader endpoint %s failed", endpoint.id)
            self._registry.mark_failed(session, str(exc))
            self._status(f"{endpoint.label} failed: {exc}")

    @QtCore.Slot(str, str)
    def _onExportFailed(self, session_uid: str, message: str) -> None:
        session = self._registry.get(session_uid)
        if session is None:
            return
        self._registry.mark_failed(session, message)
        self._logger.error("Export for %s failed: %s", session.endpoint.id, message)
        self._status(f"Export for {session.endpoint.label} failed: {message}")

    def _watchDetached(self, session_uid: str, detached) -> None:
        window = getattr(detached, "window", None)
        qt_window = getattr(window, "_qt_window", None)
        destroyed = getattr(qt_window, "destroyed", None)
        if destroyed is not None:
            try:
                destroyed.connect(lambda *_: self._detachedClosed(session_uid))
                return
            except Exception:
                pass
        hook = getattr(detached, "on_closed", None)
        if callable(hook):
            hook(lambda: self._detachedClosed(session_uid))

    def _detachedClosed(self, session_uid: str) -> None:
        session = self._registry.get(session_uid)
        if session is not None and session.is_open:
            self._registry.close(session)

    # -- closing ------------------------------------------------------------------

    def closeSession(self, uid: str) -> None:
        session = self._registry.get(uid)
        if session is None or session.state == "closed":
            return
        viewer = self._viewer()
        if viewer is not None:
            for layer in list(session.layers):
                try:
                    if layer in viewer.layers:
                        viewer.layers.remove(layer)
                except Exception:
                    self._logger.debug("Could not remove endpoint layer", exc_info=True)
            if session.dock is not None:
                try:
                    viewer.window.remove_dock_widget(session.dock)
                    # napari's removal only hides the dock; it stays a child
                    # of the window until deleted. Delete it, so a session
                    # closed a hundred times does not leave a hundred hidden
                    # widgets behind.
                    delete = getattr(session.dock, "deleteLater", None)
                    if callable(delete):
                        delete()
                except Exception:
                    self._logger.debug("Could not remove endpoint dock", exc_info=True)
        if session.viewer is not None:
            try:
                session.viewer.close()
            except Exception:
                self._logger.debug("Could not close detached viewer", exc_info=True)
        self._registry.close(session)

    def closeAll(self) -> None:
        for session in list(self._registry.sessions()):
            self.closeSession(session.uid)
        self._registry.forget_closed()

    def _connectLayerEvents(self, viewer) -> None:
        if self._layerEventsConnected:
            return
        events = getattr(getattr(viewer, "layers", None), "events", None)
        removed = getattr(events, "removed", None)
        if removed is None:
            return
        removed.connect(self._onLayerRemoved)
        self._layerEventsConnected = True

    def _onLayerRemoved(self, event) -> None:
        layer = getattr(event, "value", None)
        session = self._registry.layer_removed(layer)
        if session is not None:
            # Its last layer is gone: tear down the rest (dock, files).
            self.closeSession(session.uid)

    # -- import -------------------------------------------------------------------

    def importLayer(self, layer=None, source_result=None, *, name: str | None = None, endpoint: NapariEndpoint | None = None):
        """Bring one plugin-created layer back as a typed result (or ROIs).

        Without arguments a dialog asks for the layer and the source result;
        nothing is inferred from which layers appeared when.
        """
        viewer = self._viewer()
        if viewer is None:
            self._status("No napari viewer is available.")
            return None
        if layer is None or source_result is None:
            from imswitch.improcess.view.NapariImportDialog import NapariImportDialog

            layers = [l for l in viewer.layers if not self._isOurLayer(l)]
            results = self._allResults()
            suggestions = self._mappingSuggestions(layers)
            choice = NapariImportDialog.choose(
                self._mainView, layers, results, suggestions=suggestions,
                preselected_layer=layer, preselected_result=source_result,
            )
            if choice is None:
                return None
            layer, source_result, name, mapping_endpoint = choice
            endpoint = endpoint or mapping_endpoint
        session = self._sessionForSource(source_result)
        plugin_name, widget_name, preserves = self._importContext(endpoint, session, layer)
        try:
            imported = import_layer(
                snapshot_layer(layer), source_result,
                plugin_name=plugin_name, widget_name=widget_name,
                preserves_grid=preserves, name=name,
            )
        except NotImportable as exc:
            self._status(str(exc))
            return None
        if imported.rois:
            self._addRois(list(imported.rois))
            self._status(f"Imported {len(imported.rois)} ROI(s) from layer '{layer.name}'.")
            return imported
        self._commChannel.sigResultProduced.emit(imported.result, imported.result.name)
        self._status(
            f"Imported layer '{layer.name}' as {type(imported.result).__name__} "
            f"({imported.grid} grid: {imported.reason})."
        )
        return imported

    def _importContext(self, endpoint, session, layer) -> tuple[str, str | None, bool]:
        endpoint = endpoint or (session.endpoint if session is not None else None)
        if endpoint is None:
            return "napari", None, False
        preserves = False
        for mapping in getattr(endpoint, "output_mappings", ()):
            if mapping.matches(getattr(layer, "name", ""), type(layer).__name__):
                preserves = bool(mapping.preserves_grid)
                break
        return str(endpoint.plugin_name), endpoint.widget_name, preserves

    def _mappingSuggestions(self, layers) -> dict:
        """layer -> (endpoint, mapping) for layers a verified adapter can name."""
        suggestions = {}
        for session in self._registry.sessions():
            endpoint = session.endpoint
            for layer in layers:
                for mapping in getattr(endpoint, "output_mappings", ()):
                    if mapping.matches(getattr(layer, "name", ""), type(layer).__name__):
                        suggestions[id(layer)] = (endpoint, mapping, session.result_uid)
                        break
        return suggestions

    def _sessionForSource(self, source_result):
        uid = str(getattr(source_result, "result_uid", "") or "")
        for session in self._registry.sessions():
            if session.result_uid == uid and session.lane == "dock":
                return session
        return None

    def _isOurLayer(self, layer) -> bool:
        metadata = getattr(layer, "metadata", {}) or {}
        if metadata.get("endpoint_session_uid"):
            return True
        if metadata.get("result_uid") and self._registry.find_by_layer(layer) is None:
            # The viewer's own rendering of a result carries a result_uid.
            return True
        return getattr(layer, "protected", False)

    def _addRois(self, rois) -> None:
        getter = getattr(self._mainView, "getRuntimeAnalysisWidget", None)
        widget = getter("roi-manager") if callable(getter) else None
        add = getattr(widget, "add_rois", None)
        if not callable(add):
            self._status("Load the ROI manager panel to receive imported shapes.")
            return
        add(list(rois))

    # -- plugin installation ---------------------------------------------------------

    def openPluginInstaller(self) -> None:
        try:
            from napari_plugin_manager.qt_plugin_dialog import QtPluginDialog
        except Exception:
            self._status(
                "napari-plugin-manager is not installed; install plugins with "
                "'pip install <plugin>' in this environment, then rescan."
            )
            return
        try:
            dialog = QtPluginDialog(self._mainView)
            dialog.exec_()
            self.refreshEndpoints(rescan=True)
        except Exception as exc:  # noqa: BLE001
            self._logger.exception("napari plugin manager failed")
            self._status(f"Plugin manager failed: {exc}")

    # -- helpers -----------------------------------------------------------------------

    def _viewer(self):
        getter = getattr(self._reconstructionController, "getNapariViewer", None)
        return getter() if callable(getter) else None

    def _currentResult(self):
        getter = getattr(self._reconstructionController, "getActiveResult", None)
        return getter() if callable(getter) else None

    def _allResults(self) -> list:
        try:
            items = self._commChannel.getAllResults()
        except Exception:
            items = []
        results = []
        for item in items:
            if isinstance(item, tuple) and len(item) == 2:
                results.append(item[1])
            else:
                results.append(item)
        return [r for r in results if r is not None]

    def _status(self, message: str) -> None:
        show = getattr(self._mainView, "showStatusMessage", None)
        if callable(show):
            show(message)
        self._logger.info(message)


__all__ = ["NapariEndpointController"]


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
