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
    writer_formats_for,
)
from imswitch.improcess.model.napari_import import (
    NotImportable,
    import_layer,
    snapshot_layer,
)
from imswitch.improcess.model.napari_layers import NotLayerable, result_to_layer_data
from imswitch.improcess.model.napari_sessions import SessionRegistry


class _ExportWorker(QtCore.QObject):
    """Runs one export callable off the GUI thread and reports back.

    ``directory`` is where the job writes; it rides on the failure signal
    too, so a failed export whose session is already gone can still be
    cleaned up by the receiver.
    """

    sigDone = QtCore.Signal(str, object)          # session uid, Path
    sigFailed = QtCore.Signal(str, str, object)   # session uid, message, directory

    def __init__(self, session_uid: str, job: Callable[[], Path], directory=None, parent=None):
        super().__init__(parent)
        self._uid = session_uid
        self._job = job
        self._directory = directory

    @QtCore.Slot()
    def run(self) -> None:
        thread = QtCore.QThread.currentThread()
        if thread is not None and thread.isInterruptionRequested():
            # Cancelled before it started: nothing is written, nothing to keep.
            self.sigFailed.emit(self._uid, "cancelled", self._directory)
            return
        try:
            path = self._job()
        except Exception as exc:  # noqa: BLE001 - reported to the user
            self.sigFailed.emit(self._uid, str(exc), self._directory)
            return
        # A cancellation that arrived mid-write cannot stop the write; the
        # completion is reported and the receiver discards it as stale.
        self.sigDone.emit(self._uid, Path(path))


#: Export threads that outlived their controller's shutdown wait. Kept
#: referenced here so a running QThread is never destroyed under its
#: worker; they finish on their own and are released then.
_ORPHANED_EXPORTS: list = []


def _threaded_export_runner(session_uid, job, on_done, on_failed, keepalive: list, directory=None):
    """Default runner: a QThread per export; the controller keeps the refs."""
    thread = QtCore.QThread()
    worker = _ExportWorker(session_uid, job, directory)
    worker.moveToThread(thread)
    worker.sigDone.connect(on_done)
    worker.sigFailed.connect(on_failed)
    worker.sigDone.connect(thread.quit)
    worker.sigFailed.connect(thread.quit)
    thread.started.connect(worker.run)
    entry = (thread, worker)
    keepalive.append(entry)

    def release():
        for holder in (keepalive, _ORPHANED_EXPORTS):
            if entry in holder:
                holder.remove(entry)

    thread.finished.connect(release)
    thread.start()
    return entry


def _save_with_writer(path: str, layers: list, writer) -> list[str]:
    """Save ``layers`` with *this* writer contribution, not the plugin's first one.

    A plugin may contribute several writers (lossy and lossless, say); the
    user picked one by its ``writer_id``, so that is the command dispatched.
    """
    manager = _npe2_manager()
    layer_types = [type(layer).__name__.lower() for layer in layers]
    contribution = None
    for candidate in manager.iter_compatible_writers(layer_types):
        if str(getattr(candidate, "command", "")) == str(writer.writer_id):
            contribution = candidate
            break
    if contribution is None:
        raise RuntimeError(
            f"writer {writer.writer_id!r} of {writer.plugin_name} is not available for "
            f"layers of type {layer_types}"
        )
    return _napari_save_layers()(str(path), list(layers), plugin=writer.plugin_name, _writer=contribution)


def _npe2_manager():
    from npe2 import PluginManager

    return PluginManager.instance()


def _napari_save_layers():
    import napari

    return napari.save_layers


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

    #: Emitted whenever a session opens, fails, closes or is forgotten, so
    #: whoever leases sources on a session's behalf can re-check.
    sigSessionsChanged = QtCore.Signal()

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
            # Writer contributions: save exactly this session's layers with a
            # plugin's writer, as an extra export format.
            for writer in self.writerFormatsFor(session):
                save_action = sessions_menu.addAction(
                    f"Save layers of {session.label} with {writer.display_name} ({writer.plugin_name})"
                )
                save_action.triggered.connect(
                    lambda _checked=False, uid=session.uid, w=writer: self.saveSessionLayers(uid, w)
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
        viewer = self._viewer()
        # What the viewer held before this send: a layer that is not in this
        # set appeared afterwards, which is the only honest attribution of a
        # plugin's output to a session.
        session.layers_before = tuple(getattr(viewer, "layers", []) or ()) if viewer is not None else ()
        try:
            if endpoint.lane == "dock":
                self._openDock(session, endpoint, result)
            else:
                self._startExport(session, endpoint, result)
        except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
            self._logger.exception("Could not send %s to %s", result, endpoint.id)
            self._registry.mark_failed(session, str(exc))
            self.sigSessionsChanged.emit()
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
        # Owned from this moment: if the dock cannot be built, the layers
        # this send added are taken back out, not left behind.
        session.layers = list(layers)
        widget_name = resolve_widget(endpoint, self._installed or InstalledPlugins()) or endpoint.widget_name
        try:
            dock, widget = viewer.window.add_plugin_dock_widget(endpoint.plugin_name, widget_name)
        except Exception:
            for layer in layers:
                try:
                    if layer in viewer.layers:
                        viewer.layers.remove(layer)
                except Exception:
                    self._logger.debug("Could not roll back endpoint layer", exc_info=True)
            session.layers = []
            raise
        self._registry.mark_open(session, layers=layers, dock=dock, widget=widget)
        self.sigSessionsChanged.emit()
        self._connectLayerEvents(viewer)
        self._status(f"Sent {result.name} to {endpoint.label}.")

    def _startExport(self, session, endpoint, result) -> None:
        directory = self._registry.temp_dir_for(session)
        self._registry.mark_exporting(session)
        self._status(f"Exporting {result.name} for {endpoint.label}...")

        def job():
            return export_result(endpoint, result, directory)

        session.worker = self._export_runner(session.uid, job, self._onExportDone, self._onExportFailed)

    def _run_export_threaded(self, session_uid, job, on_done, on_failed):
        session = self._registry.get(session_uid)
        directory = session.temp_dir if session is not None else None
        return _threaded_export_runner(session_uid, job, on_done, on_failed, self._keepalive, directory)

    @QtCore.Slot(str, object)
    def _onExportDone(self, session_uid: str, path) -> None:
        session = self._registry.get(session_uid)
        if session is None or session.state != "exporting" or session.cancelled:
            # The session was closed while the export ran: its directory was
            # deleted and the worker has just recreated it. Take that back,
            # and let the tombstone go now that its worker is back.
            self._discardStaleExport(path)
            if session is not None:
                session.worker = None
                self._registry.forget(session_uid)
                self.sigSessionsChanged.emit()
            return
        endpoint = session.endpoint
        try:
            if endpoint.lane == "reader":
                viewer = self._viewer()
                if viewer is None:
                    raise RuntimeError("no napari viewer is available")
                # A reader may add some layers and then fail on the rest;
                # whatever it added is this session's to take back.
                before = list(getattr(viewer, "layers", []) or [])
                try:
                    layers = list(viewer.open(str(path), plugin=endpoint.reader_plugin) or [])
                except Exception:
                    self._removeLayersAddedSince(viewer, before)
                    raise
                for layer in layers:
                    try:
                        layer.metadata["endpoint_session_uid"] = session.uid
                    except Exception:
                        pass
                self._registry.mark_open(session, files=[Path(path)], layers=layers)
                self.sigSessionsChanged.emit()
                self._connectLayerEvents(viewer)
            else:
                detached = self._detached_viewer_factory()
                try:
                    detached.open(str(path), plugin=endpoint.reader_plugin)
                except Exception:
                    # The viewer was made for this session; a failed open
                    # must not leave an empty window behind.
                    try:
                        detached.close()
                    except Exception:
                        self._logger.debug("Could not close the detached viewer", exc_info=True)
                    raise
                self._registry.mark_open(session, files=[Path(path)], viewer=detached)
                self.sigSessionsChanged.emit()
                self._watchDetached(session.uid, detached)
            self._status(f"Opened {Path(path).name} with {endpoint.reader_plugin}.")
        except Exception as exc:  # noqa: BLE001
            self._logger.exception("Reader endpoint %s failed", endpoint.id)
            self._registry.mark_failed(session, str(exc))
            self.sigSessionsChanged.emit()
            self._status(f"{endpoint.label} failed: {exc}")

    def _removeLayersAddedSince(self, viewer, before) -> None:
        for layer in list(getattr(viewer, "layers", []) or []):
            if any(layer is item for item in before):
                continue
            try:
                viewer.layers.remove(layer)
            except Exception:
                self._logger.debug("Could not roll back a reader's layer", exc_info=True)

    def _discardStaleExport(self, path) -> None:
        """Remove what a worker wrote for a session that no longer exists."""
        import shutil

        try:
            directory = Path(path).parent
            if directory.name.startswith("imswitch_endpoint_"):
                shutil.rmtree(directory, ignore_errors=True)
            elif Path(path).exists():
                Path(path).unlink()
        except Exception:
            self._logger.debug("Could not discard a stale export", exc_info=True)

    @QtCore.Slot(str, str, object)
    def _onExportFailed(self, session_uid: str, message: str, directory=None) -> None:
        session = self._registry.get(session_uid)
        if session is None or session.cancelled or session.state != "exporting":
            # The session was closed while the export ran: whatever the
            # worker recreated in its directory is stale and goes.
            if directory is not None:
                self._discardStaleExport(Path(directory) / "export")
            if session is not None:
                session.worker = None
                self._registry.forget(session_uid)
                self.sigSessionsChanged.emit()
            return
        self._registry.mark_failed(session, message)
        self.sigSessionsChanged.emit()
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
            self.sigSessionsChanged.emit()

    # -- closing ------------------------------------------------------------------

    def closeSession(self, uid: str) -> None:
        session = self._registry.get(uid)
        if session is None or session.state == "closed":
            return
        if session.state == "exporting":
            # The export cannot be stopped mid-write, but it is told to stop,
            # kept alive until it does (``_keepalive``), and its completion
            # is discarded because the session is marked cancelled below.
            self._interruptExport(session)
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
        self.sigSessionsChanged.emit()

    def closeAll(self, wait_ms: int = 5000) -> bool:
        """Close every session; at shutdown also wait for export threads.

        A thread still writing when the window goes away would be destroyed
        under its worker; each one is joined here, bounded by ``wait_ms``.
        A cancelled session is forgotten only once its worker has come
        back, so the late completion still finds the tombstone that tells
        it to discard what it wrote. Returns False when an export outlived
        the wait; that thread is then parked in a module-level holder and
        finishes on its own.
        """
        for session in list(self._registry.all_sessions()):
            self.closeSession(session.uid)
        all_done = self.joinExports(wait_ms)
        self._registry.forget_closed(keep=lambda s: s.cancelled and self._workerRunning(s))
        return all_done

    def joinExports(self, wait_ms: int = 5000) -> bool:
        """Wait for every running export thread; False when one is still running.

        A thread that does not finish in time is *not* dropped: its entry
        moves to a module-level holder so the QThread object stays alive
        until it finishes, and the session's late completion is discarded
        the normal way.
        """
        all_done = True
        for entry in list(self._keepalive):
            thread = entry[0] if isinstance(entry, tuple) else None
            wait = getattr(thread, "wait", None)
            if thread is None or not callable(wait):
                continue
            try:
                if thread.isRunning():
                    thread.requestInterruption()
                    thread.quit()
                    if not wait(int(wait_ms)):
                        all_done = False
                        self._logger.warning("An endpoint export is still running after %d ms", wait_ms)
                        if entry not in _ORPHANED_EXPORTS:
                            _ORPHANED_EXPORTS.append(entry)
            except Exception:
                self._logger.debug("Could not join an export thread", exc_info=True)
        return all_done

    def heldResultUids(self) -> set[str]:
        """Result uids an endpoint session still reads (a lease for lazy sources)."""
        return self._registry.held_result_uids()

    @staticmethod
    def _workerRunning(session) -> bool:
        entry = session.worker
        thread = entry[0] if isinstance(entry, tuple) else None
        running = getattr(thread, "isRunning", None)
        try:
            return bool(running()) if callable(running) else False
        except Exception:
            return False

    def _interruptExport(self, session) -> None:
        entry = session.worker
        thread = entry[0] if isinstance(entry, tuple) else None
        request = getattr(thread, "requestInterruption", None)
        if callable(request):
            try:
                request()
            except Exception:
                self._logger.debug("Could not interrupt an export thread", exc_info=True)

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
            candidates = self._mappingCandidates(layers)
            choice = NapariImportDialog.choose(
                self._mainView, layers, results, suggestions=suggestions, candidates=candidates,
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

    def _mappingCandidates(self, layers) -> dict:
        """layer id -> every (endpoint, mapping, source uid) an open session *could* offer.

        Offered to the user to pick from explicitly; none of them is applied
        on its own (see :meth:`_mappingSuggestions`).
        """
        return {
            layer_id: [(session.endpoint, mapping, session.result_uid) for session, mapping in claims]
            for layer_id, claims in self._claims(layers).items()
        }

    def _claims(self, layers) -> dict:
        """layer id -> [(session, mapping)] for every open session whose mapping matches."""
        claims: dict = {}
        for layer in layers:
            if self._registry.find_by_layer(layer) is not None:
                continue
            for session in self._registry.sessions():
                if any(layer is before for before in session.layers_before):
                    continue
                for mapping in getattr(session.endpoint, "output_mappings", ()):
                    if mapping.matches(getattr(layer, "name", ""), type(layer).__name__):
                        claims.setdefault(id(layer), []).append((session, mapping))
                        break
        return claims

    def _mappingSuggestions(self, layers) -> dict:
        """layer id -> (endpoint, mapping, source uid) where the attribution is *known*.

        A name pattern cannot say which plugin made a layer, and neither can
        "it appeared after the session opened": an unrelated layer added
        later would inherit the session's grid. A suggestion is made only
        when napari's own record of where the layer came from
        (``layer.source``) points at the session -- the dock widget the
        session opened produced it, or its parent layer is one the session
        added -- and exactly one open session's mapping claims it. A layer
        without that record gets no suggestion; the user chooses a mapping
        explicitly, or the layer gets a fresh coordinate space.
        """
        suggestions = {}
        by_id = {id(layer): layer for layer in layers}
        for layer_id, claims in self._claims(layers).items():
            attributed = [
                (session.endpoint, mapping, session.result_uid) for session, mapping in claims
                if self._layerCameFrom(by_id.get(layer_id), session)
            ]
            if len(attributed) == 1:
                suggestions[layer_id] = attributed[0]
        return suggestions

    def _layerCameFrom(self, layer, session) -> bool:
        """napari's ``layer.source`` says the session's widget or layers made it."""
        if layer is None or session is None:
            return False
        source = getattr(layer, "source", None)
        if source is None:
            return False
        widget = getattr(source, "widget", None)
        if widget is not None and session.widget is not None:
            ours = session.widget
            if widget is ours or getattr(widget, "native", None) is ours or getattr(ours, "native", None) is widget:
                return True
        parent = getattr(source, "parent", None)
        if callable(parent):                   # napari keeps it as a weakref
            try:
                parent = parent()
            except Exception:
                parent = None
        if parent is not None and session.owns_layer(parent):
            return True
        return False

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

    # -- writer contributions ----------------------------------------------------------

    def writerFormatsFor(self, session) -> list:
        """Writer plugins able to save every layer the session added."""
        if not session.is_open or not session.layers:
            return []
        layer_types = [type(layer).__name__.lower() for layer in session.layers]
        return writer_formats_for(layer_types, self._installed or InstalledPlugins())

    def saveSessionLayers(self, session_uid: str, writer, path=None, *, save_layers=None):
        """Save the session's own layers (never the whole viewer) with ``writer``."""
        session = self._registry.get(session_uid)
        if session is None or not session.is_open:
            self._status("That endpoint session is no longer open.")
            return None
        if path is None:
            from qtpy import QtWidgets

            suffix = writer.extensions[0] if writer.extensions else ""
            path, _filter = QtWidgets.QFileDialog.getSaveFileName(
                self._mainView, f"Save layers with {writer.display_name}",
                f"{session.result_name}{suffix}", f"{writer.display_name} (*{suffix or '.*'})",
            )
            if not path:
                return None
        save_layers = save_layers or _save_with_writer
        try:
            written = save_layers(str(path), list(session.layers), writer)
        except Exception as exc:  # noqa: BLE001
            self._logger.exception("Writer %s failed", writer.writer_id)
            self._status(f"{writer.display_name} failed: {exc}")
            return None
        self._status(f"Saved {len(session.layers)} layer(s) with {writer.display_name}: {path}")
        return written

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
